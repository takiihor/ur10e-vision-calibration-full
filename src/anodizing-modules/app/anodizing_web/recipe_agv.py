"""Stationary AGV admission, hold and supervised recipe travel."""
from contextlib import contextmanager, nullcontext
import threading
import time

from . import agv

ALARM = 'AGV may still be moving. Use its e-stop.'
APPROVAL = 'AGV app is in Manual mode with auto-start after stop OFF; the line is clear; a person is at the AGV e-stop'
ACK_S = 3
OBSTACLE_S = 30
WATCH_S = .1
POSE_FRESH_S = .75
UNKNOWN = 'AGV state unknown: connect the AGV on the AGV page, or confirm it is switched off'
REPORTING = 'The AGV is reporting, so it is not switched off. Use AGV parked.'
STANDARD_MODE = ('Pick/place needs station-call mode to verify and call learned stations. '
                 'Enable the station-call function in the XBOT app before starting a new run.')


def motion_state(connection, *, poll=False, cached=False):
    link = {}
    if connection:
        link = connection.cached_motion_feedback() if cached else connection.motion_feedback(poll=poll)
    status = link.get('status')
    age = link.get('status_age_s')
    reporting = ((link.get('mode') == 'serial' or link.get('executor_link')) and status is not None
                 and age is not None and age < agv.RECIPE_STALE_S)
    parked = (reporting and stopped(status) and status.get('obstacle_stop') is False
              and status.get('fault_code') == 0 and not link['movement_pending'])
    station = status.get('current_station') if reporting else None
    if parked:
        position = f' · station {station}' if station else ''
        detail = f'AGV parked{position} · {age:.1f} s ago'
    elif reporting:
        detail = f'AGV state unknown · last report {age:.1f} s ago. Check that the AGV is parked without a fault or obstacle stop.'
    elif age is not None:
        detail = f'{UNKNOWN} · last report {age:.1f} s ago'
    else:
        detail = UNKNOWN
    return dict(parked=parked, reporting=reporting, detail=detail, station=station,
                connection_id=link.get('connection_id'), stop_generation=link.get('stop_generation'), status_age_s=age,
                status_time=link.get('status_time'), transport=link.get('mode'),
                teaching_station=agv.arrived_station(status) if parked else None,
                teaching_generation=link.get('teaching_generation'), status_monotonic=link.get('status_monotonic'))


def admit(connection, body, *, require_approval=False, poll=False):
    state = motion_state(connection, poll=poll)
    if body.get('agv_off') is True:
        if state['reporting']:
            raise ValueError(REPORTING)
        return 'off', None, None
    if not state['parked']:
        raise ValueError(state['detail'])
    if require_approval and any(type(body.get(key)) is not int or body[key] != state[key]
                                for key in ('connection_id', 'stop_generation')):
        raise ValueError('AGV approval expired. Review the link and approve the recipe again.')
    return 'parked', state['connection_id'], state['stop_generation']


def stopped(status):
    return status.get('stopped') is True and status.get('actual_speed_m_h') == 0


def require_serial(mode):
    if mode != 'serial':
        raise ValueError('Connect the serial AGV on the AGV page before running a robot recipe.')


def check_speed(status, speed_cap):
    speed = status.get('set_speed_m_h')
    if speed is None or not 500 <= speed <= speed_cap:
        raise ValueError(f'AGV set speed is unavailable or above the recipe cap of {speed_cap} m/h. Check Set speed on the AGV page.')


def check_status(status, stamp, *, require_unpaused=True):
    if stamp is None or time.monotonic()-stamp >= agv.RECIPE_STALE_S:
        raise ValueError('AGV status is stale. Check the radio and reconnect on the AGV page.')
    if status.get('fault_code') != 0:
        fault = status.get('fault_code')
        raise ValueError(f'AGV fault {fault}: {status.get("fault_description", "unknown fault")}. Check the AGV.')
    if require_unpaused and status.get('paused') is not False:
        raise ValueError('AGV pause state is active or unavailable. Check the AGV before starting a new run.')


def learned(status, station):
    return status.get('mode') == 2 and 1 <= station <= agv.station_limit(status.get('max_stations', 0))


class StationVisit:
    def __init__(self, connection, robot_stop, *, strict_station=True, connection_id=None,
                 stop_generation=None, switched_off=False, allow_wired=False):
        self.connection = connection
        self.allow_wired = allow_wired
        self.robot_stop = robot_stop
        self.strict_station = strict_station
        self.connection_id = connection_id
        self.stop_generation = stop_generation
        self.switched_off = switched_off
        self.stop_error = None
        self.robot_stop_error = None
        self.cancelled = threading.Event()
        self.lock = threading.Lock()
        self.finished = threading.Event()
        self.ready = threading.Event()
        self.error = None
        self.alarm = None
        self.stop_sent = None
        self.station = self.status = self.status_at = None
        self.leg = None
        self.sent = self.travel_deadline = self.pose_at = self.obstacle_at = None
        self.arrivals = 0

    @contextmanager
    def watch(self):
        link = (nullcontext() if self.switched_off else
                self.connection.recipe_link(self.connection_id, stop_generation=self.stop_generation))
        with link as transport:
            if not self.switched_off and not (self.allow_wired and transport == 'wired'):
                require_serial(transport)
            self.transport = transport
            self.started = time.monotonic()
            thread = threading.Thread(target=self._watch, name='recipe-agv-watch')
            thread.start()
            try:
                yield
            finally:
                self.finished.set()
                thread.join()

    def _watch(self):
        while not self.finished.is_set():
            with self.lock:
                try:
                    self._sample()
                except Exception as exc:
                    self.error = str(exc)
                    self.cancelled.set()
            if self.error:
                self.ready.set()
                robot = threading.Thread(target=self._stop_robot, name='recipe-robot-stop')
                robot.start()
                try:
                    if not self.switched_off:
                        with self.lock:
                            self._stop()
                finally:
                    robot.join()
                return
            self.finished.wait(WATCH_S)

    def _stop_robot(self):
        try:
            self.robot_stop()
        except Exception as exc:
            self.robot_stop_error = str(exc)

    def _sample(self):
        if self.switched_off:
            admit(self.connection, {'agv_off': True}, poll=True)
            self.ready.set()
            return
        status, stamp, updates, ack_s = self.connection.recipe_feedback()
        now = time.monotonic()
        if not self.ready.is_set() and (stamp is None or stamp < self.started):
            if now-self.started < agv.RECIPE_STALE_S:
                return
            raise ValueError('AGV status is stale. Check the radio and reconnect on the AGV page.')
        if self.sent is not None:
            self._check_travel(status, stamp, updates, ack_s, now)
            return
        for current, received in updates or ((status, stamp),):
            self.station = self._check_hold(current, received)
        self.status, self.status_at = status, stamp
        self.ready.set()

    def _check_hold(self, status, stamp):
        check_status(status, stamp, require_unpaused=self.strict_station)
        if self.strict_station and status.get('mode') != 2:
            raise ValueError(STANDARD_MODE)
        if status.get('obstacle_stop') is not False:
            raise ValueError('AGV obstacle stop is active or unavailable. Park the AGV before robot work.')
        actual = status.get('current_station', 0)
        if not stopped(status) or (self.station is not None and actual != self.station):
            raise ValueError('AGV moved during robot work or was already moving. Check the AGV before a new run.')
        if self.strict_station and (not learned(status, actual) or status.get('station_arrival') is not True):
            raise ValueError('AGV station is unconfirmed. Check route learning and station-call mode on the AGV page.')
        return actual

    def require_endpoint(self, target):
        if type(target) is not int or not 1 <= target <= 255:
            raise ValueError('The saved AGV station must be an integer from 1 to 255.')
        with self.lock:
            if self.error:
                raise ValueError(self.error)
            status = self.status
            actual = self._check_hold(status, self.status_at)
            if actual != target or status.get('station_arrival') is not True or not learned(status, target):
                raise ValueError(f'Park the AGV at station {target} before starting robot work there.')

    def _check_travel(self, status, stamp, updates, ack_s, now):
        sent = self.sent
        if self.cancelled.is_set():
            raise ValueError('Recipe stopped during AGV travel. Check the AGV before starting a new run.')
        check_status(status, stamp)
        if now-self.pose_at >= POSE_FRESH_S:
            raise ValueError('Lost confirmation that the robot is still at the travel pose.')
        if ack_s is not None and ack_s <= ACK_S:
            self.leg['ack_s'] = ack_s
        elif now-self.sent >= ACK_S:
            raise ValueError('No AGV acknowledgement within 3 s. Check the radio before starting a new run.')
        if now >= self.travel_deadline:
            raise ValueError('AGV travel timed out. Check the line and target station.')
        for report, received in updates:
            if received < sent:
                continue
            check_status(report, received)
            target = self.leg['to_station']
            if not learned(report, target):
                raise ValueError('AGV left station-call mode or the reviewed station is unavailable.')
            check_speed(report, self.speed_cap)
            reported_target = report.get('target_station')
            first_report = self.first_travel_report
            self.first_travel_report = False
            if reported_target != target:
                if not first_report:
                    raise ValueError(f'AGV reported target station {reported_target}. Expected station {target}.')
                continue  # One older status reply may still have been in flight at dispatch.
            if report.get('obstacle_stop'):
                if self.obstacle_at is None:
                    self.obstacle_at = received
                elif now-self.obstacle_at > OBSTACLE_S:
                    raise ValueError('AGV obstacle stop exceeded 30 s. Clear the line before starting a new run.')
            else:
                self.obstacle_at = None
            arrived = (report.get('current_station') == target and stopped(report)
                       and report.get('station_arrival') is True and report.get('obstacle_stop') is False)
            self.arrivals = self.arrivals+1 if arrived else 0
            if self.arrivals >= 2 and 'ack_s' in self.leg and self.sent is not None:
                self.leg['arrival_s'] = received-self.sent
                self.station, self.sent = target, None
                self.status, self.status_at = report, received
            if self.sent is None and not arrived:
                raise ValueError('AGV moved after arrival. Check the AGV before a new run.')

    def check_route(self, targets, speed_cap):
        with self.lock:
            if self.error:
                raise ValueError(self.error)
            for target in targets:
                if not learned(self.status, target):
                    raise ValueError(f'AGV station {target} is not learned. Check the AGV route.')
            check_speed(self.status, speed_cap)

    def start_leg(self, target, speed_cap, timeout, pose_at, cancelled):
        with self.lock:
            if self.error:
                raise ValueError(self.error)

            unsafe = False

            def validate(status, stamp, updates):
                nonlocal unsafe
                try:
                    for report, received in updates:
                        self._check_hold(report, received)
                    self._check_hold(status, stamp)
                except ValueError:
                    unsafe = True
                    raise
                if not learned(status, target):
                    raise ValueError(f'AGV station {target} is not learned. Check the AGV route.')
                check_speed(status, speed_cap)
                if self.cancelled.is_set() or cancelled.is_set() or time.monotonic()-pose_at >= POSE_FRESH_S:
                    raise ValueError('Robot travel-pose check expired or was stopped. Start a new run.')

            def retry_check(status, stamp, updates):
                now = time.monotonic()
                if self.cancelled.is_set() or cancelled.is_set() or now-self.pose_at >= POSE_FRESH_S:
                    raise ValueError('Robot travel-pose check expired or was stopped. Start a new run.')
                if now >= self.travel_deadline or now-self.sent >= ACK_S:
                    raise ValueError('No AGV acknowledgement within 3 s. Check the radio before starting a new run.')
                for report, received in updates or ((status, stamp),):
                    self._check_hold(report, received)
                    check_speed(report, speed_cap)

            # A transport write can partially reach the AGV before raising.
            try:
                self.sent = self.connection.recipe_call(target, validate, retry_check=retry_check)
            except OSError:
                self._stop()
                raise ValueError('AGV station call write failed. Check the radio before a new run.')
            except ValueError:
                if unsafe:
                    self._stop()
                raise
            if self.sent is None:
                return False
            self.pose_at = pose_at
            self.speed_cap = speed_cap
            self.travel_deadline = self.sent+timeout
            self.obstacle_at = None
            self.arrivals = 0
            self.first_travel_report = True
            self.leg = dict(from_station=self.station, to_station=target, sent_at=agv.timestamp(),
                            deadline_epoch=time.time()+timeout, ack_deadline_epoch=time.time()+ACK_S)
            return True

    def progress(self, pose_at):
        with self.lock:
            if self.error:
                raise ValueError(self.error)
            self.pose_at = pose_at
            return dict(self.leg)

    def abort(self):
        self.cancelled.set()
        with self.lock:
            if self.sent is not None:
                self._stop()

    def _stop(self):
        if self.alarm is not None:
            return
        self.cancelled.set()
        self.alarm = ALARM
        try:
            self.stop_sent = self.connection.recipe_stop()
        except Exception as exc:
            self.stop_sent = False
            self.stop_error = str(exc)
