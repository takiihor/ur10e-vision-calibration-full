"""Stationary AGV admission, hold and supervised recipe travel."""
from contextlib import contextmanager
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


def motion_state(connection, *, poll=False):
    link = connection.motion_feedback(poll=poll) if connection else {}
    status = link.get('status')
    age = link.get('status_age_s')
    reporting = status is not None and age is not None and age < agv.RECIPE_STALE_S
    parked = (reporting and link['mode'] == 'serial' and stopped(status)
              and status.get('fault_code') == 0 and not link['movement_pending'])
    station = status.get('current_station') if reporting else None
    if parked:
        position = f' · station {station}' if station else ''
        detail = f'AGV parked{position} · {age:.1f} s ago'
    elif reporting:
        detail = f'AGV state unknown · last report {age:.1f} s ago. Check that the serial AGV is parked without a fault.'
    elif age is not None:
        detail = f'{UNKNOWN} · last report {age:.1f} s ago'
    else:
        detail = UNKNOWN
    return dict(parked=parked, reporting=reporting, detail=detail, station=station,
                connection_id=link.get('connection_id'), status_age_s=age,
                status_time=link.get('status_time'), transport=link.get('mode'),
                teaching_station=station if parked and station and learned(status, station) else None)


def admit(connection, body):
    state = motion_state(connection)
    if body.get('agv_off') is True:
        if state['reporting']:
            raise ValueError(REPORTING)
        return 'off', None
    if not state['parked']:
        raise ValueError(state['detail'])
    return 'parked', state['connection_id']


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
    return 1 <= station <= status.get('max_stations', 0) and status.get('mode') == 2


class StationVisit:
    def __init__(self, connection, robot_stop, *, strict_station=True, connection_id=None):
        self.connection = connection
        self.robot_stop = robot_stop
        self.strict_station = strict_station
        self.connection_id = connection_id
        self.stop_error = None
        self.cancelled = threading.Event()
        self.lock = threading.Lock()
        self.finished = threading.Event()
        self.ready = threading.Event()
        self.error = None
        self.alarm = None
        self.stop_sent = None
        self.station = self.status = None
        self.leg = None
        self.sent = self.travel_deadline = self.pose_at = self.obstacle_at = None
        self.arrivals = 0

    @contextmanager
    def watch(self):
        if self.connection is None:
            raise ValueError('Connect the AGV on the AGV page before starting the recipe.')
        with self.connection.recipe_link(self.connection_id) as transport:
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
            if self.error:
                # Neither Stop depends on the outcome of the other.
                try:
                    try:
                        self.robot_stop()
                    finally:
                        with self.lock:
                            if self.alarm is None:
                                self._stop()
                except Exception as stop_error:
                    self.stop_error = str(stop_error)
                finally:
                    self.ready.set()
                return
            self.finished.wait(WATCH_S)

    def _sample(self):
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
        self.status = status
        self.ready.set()

    def _check_hold(self, status, stamp):
        check_status(status, stamp, require_unpaused=self.strict_station)
        if self.strict_station and status.get('mode') == 0:
            raise ValueError(STANDARD_MODE)
        actual = status.get('current_station', 0)
        if not stopped(status) or (self.station is not None and actual != self.station):
            raise ValueError('AGV moved during robot work or was already moving. Check the AGV before a new run.')
        if self.strict_station and not learned(status, actual):
            raise ValueError('AGV station is unconfirmed. Check route learning and station-call mode on the AGV page.')
        return actual

    def _check_travel(self, status, stamp, updates, ack_s, now):
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
            check_status(report, received)
            if report.get('obstacle_stop'):
                if self.obstacle_at is None:
                    self.obstacle_at = received
                elif now-self.obstacle_at > OBSTACLE_S:
                    raise ValueError('AGV obstacle stop exceeded 30 s. Clear the line before starting a new run.')
            else:
                self.obstacle_at = None
            target = self.leg['to_station']
            arrived = (report.get('current_station') == target and stopped(report)
                       and report.get('station_arrival') is True and learned(report, target))
            self.arrivals = self.arrivals+1 if arrived else 0
            if self.arrivals >= 2 and 'ack_s' in self.leg and self.sent is not None:
                self.leg['arrival_s'] = received-self.sent
                self.station, self.sent = target, None
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
                if cancelled.is_set() or time.monotonic()-pose_at >= POSE_FRESH_S:
                    raise ValueError('Robot travel-pose check expired or was stopped. Start a new run.')

            def retry_check(status, stamp, updates):
                now = time.monotonic()
                if cancelled.is_set() or now-self.pose_at >= POSE_FRESH_S:
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
            self.travel_deadline = self.sent+timeout
            self.obstacle_at = None
            self.arrivals = 0
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
        with self.lock:
            if self.sent is not None and self.alarm is None:
                self._stop()

    def _stop(self):
        self.alarm = ALARM
        self.stop_sent = self.connection.recipe_stop()
