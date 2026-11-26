"""Versioned IPC job service and device boundaries."""
from copy import deepcopy
from hashlib import sha256
from itertools import count
import json
import math
import os
from pathlib import Path
import re
from threading import TIMEOUT_MAX, Event, RLock, Thread
import time
from uuid import uuid4

from .agv import RECIPE_STALE_S, station_limit
from .job_gateway import Job, JobGateway, MODULE_LEASE_S
from .jobs import ID, JobOwner, JobStop, atomic_json

PROTOCOL = 2
HEARTBEAT_S = 1
LEASE_S = 3
MAX_BODY = 32768
PAIRED_ROLES = {'etch', 'desmut', 'anodize'}
ROLES = PAIRED_ROLES | {'ultrasonic', 'rinse', 'seal'}


class AdmissionRefused(ValueError):
    def __init__(self, job_id, reason):
        super().__init__(reason)
        self.job_id = job_id


def stop_report(attempted, confirmed):
    return dict(received=True, halt_attempted=attempted, confirmed=confirmed)


def finite(value):
    return type(value) in (int, float) and math.isfinite(value)


def check_manual_feedback(state, battery_floor, station=None):
    if state.get('connected') is not True or state.get('stale') is True:
        raise ValueError('Manual travel requires fresh AGV status.')
    check_manual_report(state.get('status') or {}, state.get('status_age_s'), battery_floor, station)


def check_manual_report(report, age, battery_floor, station):
    if not finite(age) or not 0 <= age < RECIPE_STALE_S:
        raise ValueError('Manual travel requires fresh AGV status.')
    fault = report.get('fault_code')
    if type(fault) is not int or fault != 0:
        raise ValueError(f'Clear AGV fault {fault} before manual travel.')
    if report.get('obstacle_stop') is not False:
        raise ValueError('Clear the AGV obstacle stop before manual travel.')
    battery = report.get('battery_percent')
    if not finite(battery) or not battery_floor <= battery <= 100:
        raise ValueError(f'AGV battery reading is {battery!r}. Verify at least {battery_floor:g}% before travel.')
    if station is not None:
        maximum = station_limit(report.get('max_stations'))
        if report.get('mode') != 2 or not 1 <= station <= maximum:
            learned = f'1 to {maximum}' if maximum else 'no callable stations'
            raise ValueError(f'Choose a station in the learned range ({learned}) with station-call mode enabled. '
                             f'The request was for station {station}, mode {report.get("mode")}.')


def check_manual_attestation(attestation, station, *, now):
    if (not isinstance(attestation, dict) or set(attestation) != {'id', 'station', 'confirmed_at'}
            or not isinstance(attestation['id'], str) or not ID.fullmatch(attestation['id'])
            or type(attestation['station']) is not int or attestation['station'] != station
            or not finite(attestation['confirmed_at']) or not 0 <= now - attestation['confirmed_at'] < 30):
        raise ValueError('Give a fresh confirmation for this station call. '
                         '"I am at the e-stop, the arm is folded, and the path and tank equipment are clear."')


def validate_measured_rows(result, checkpoint):
    """Validate both copies of measured rows without filling absent evidence."""
    if not isinstance(result, dict) or (checkpoint is not None and not isinstance(checkpoint, dict)):
        raise ValueError('Malformed executor result or checkpoint.')
    rows = result.get('steps', [])
    saved = (checkpoint or {}).get('steps')
    for collection in (rows,) if saved is None else (rows, saved):
        if not isinstance(collection, list):
            raise ValueError('Malformed executor measured rows.')
        for row in collection:
            if (not isinstance(row, dict) or type(row.get('inspection')) is not bool
                    or not all(isinstance(row.get(key), str) and row[key] for key in ('id', 'tank'))
                    or not all(finite(row.get(key)) for key in ('placed_s', 'lift_s', 'dwell_s'))
                    or not isinstance(row.get('limits'), dict) or 'air' not in row['limits']
                    or 'air_s' not in row
                    or any(row.get(key) is not None and (not finite(row[key]) or row[key] < 0)
                           for key in ('air_s', 'incoming_air_s'))):
                raise ValueError('Malformed executor measured row.')
            placed, lifted, dwell = (row[key] for key in ('placed_s', 'lift_s', 'dwell_s'))
            plc = row.get('plc')
            if (not isinstance(plc, dict) or type(plc.get('cycle')) is not int or plc['cycle'] < 1
                    or plc.get('done') is not True or plc.get('basket_present') is not True
                    or plc.get('equipment_on') is not False
                    or not all(finite(plc.get(key)) for key in ('done_s', 'sample_s', 'elapsed_s'))
                    or not 0 <= placed <= plc['done_s'] <= plc['sample_s'] <= lifted
                    or plc['elapsed_s'] < 0 or dwell < 0
                    or not math.isclose(dwell, lifted - placed, rel_tol=0, abs_tol=.001)):
                raise ValueError('Contradictory PLC completion or measured dwell evidence.')
    if saved is None:
        if rows:
            raise ValueError('Measured rows are missing their checkpoint copy.')
        return
    if saved:
        effects = checkpoint.get('confirmed')
        if not isinstance(effects, list):
            raise ValueError('Measured rows are missing their confirmed effects.')
        measurements = {
            name: [entry.get('evidence') for entry in effects
                   if isinstance(entry, dict) and entry.get('effect') == name]
            for name in ('place', 'dwell', 'lift')
        }
        if any(len(items) < len(saved) for items in measurements.values()):
            raise ValueError('Measured rows are missing a placement, PLC sample or lift.')
        for row, placement, plc, lift in zip(saved, measurements['place'], measurements['dwell'],
                                            measurements['lift']):
            if (not isinstance(placement, dict) or not isinstance(lift, dict)
                    or placement.get('start_s') != row['placed_s'] or placement.get('done') is not False
                    or placement.get('basket_present') is not True or placement.get('cycle') != row['plc']['cycle']
                    or plc != row['plc'] or lift.get('lift_s') != row['lift_s']
                    or lift.get('location') not in ('input', 'park', 'transfer', 'output')):
                raise ValueError('Measured rows disagree with the confirmed placement, PLC sample or lift.')
    if rows == saved:
        return
    # A later parked exposure updates the result after the lift checkpoint was saved.
    if (result.get('open_air') and rows and len(rows) == len(saved) and rows[:-1] == saved[:-1]
            and saved[-1]['air_s'] is None and (not saved[-1]['inspection'] or rows[-1]['inspection'])
            and dict(rows[-1], air_s=None, inspection=saved[-1]['inspection']) == saved[-1]):
        return
    raise ValueError('Result and checkpoint disagree on measured rows.')


def heartbeat_interval(interval, expiry):
    if not (finite(interval) and finite(expiry) and 0 < interval < expiry <= TIMEOUT_MAX):
        raise ValueError('Set finite positive heartbeat and lease intervals with heartbeat shorter than lease.')
    return interval


class FakeAGV:
    station = 0
    stopped = True

    def visit(self, station):
        self.stopped = False
        self.station = station
        self.stopped = True
        return dict(station=station, stopped=True)


class FakeRobot:
    location = 'input'
    gripper = 'released'
    basket = None

    def place(self, step):
        self.location, self.gripper = step['tank'], 'released'

    def lift(self, destination):
        self.location = destination
        self.gripper = 'held' if destination == 'transfer' else 'released'


class FakePLC:
    """Timer edges come from placement and virtual time, independent of polling."""

    clock = 0
    cycle = 0
    _equipment_on = False
    done_s = None
    alarm = False
    stale_done = False
    dwell_offset = 0
    transfer_s = 1
    travel_s = 1

    def __init__(self, setpoints, observation_clock):
        self.setpoints = dict(setpoints or {})
        self.observation_clock = observation_clock

    @property
    def equipment_on(self):
        return self._equipment_on and (self.done_s is None or self.observation_clock() < self.done_s)

    @equipment_on.setter
    def equipment_on(self, value):
        self._equipment_on, self.done_s = value, None

    def placed(self, step):
        self.cycle += 1
        self.equipment_on = step['role'] in ('anodize', 'ultrasonic')
        start_s = self.observation_clock()
        self.done_s = start_s + max(0, self.setpoints[step['tank']] + self.dwell_offset)
        return dict(cycle=self.cycle, timer_start_s=self.clock, start_s=start_s, elapsed_s=0,
                    done=False, basket_present=True, equipment_on=self.equipment_on)

    def finish(self, step, baseline):
        if self.stale_done or baseline['cycle'] != self.cycle:
            raise ValueError('PLC done is stale or belongs to another timer cycle.')
        done_s = self.done_s
        self.clock += max(0, done_s - self.observation_clock())
        self.equipment_on = False
        return dict(cycle=self.cycle, elapsed_s=done_s - baseline['start_s'],
                    timer_s=self.clock, done=True, basket_present=True, equipment_on=False,
                    done_s=done_s)


class FakeDevices:
    job_types = {'station_visit': 1, 'dip': 1}
    simulated = True
    manual_travel_qualified = False
    reserve_capacity_s = 24 * 60 * 60
    recovery_margin_s = 10

    def __init__(self, *, clock=time.monotonic, setpoints=None, state_path=None):
        self.agv, self.robot, self.plc = FakeAGV(), FakeRobot(), FakePLC(setpoints, self._clock)
        self.state_path = Path(state_path) if state_path is not None else None
        self.state_known = None
        self.snapshot = None
        self.before_effect = None
        self.state_observed = False
        self.effect_pending = False
        self.run_id = None
        self.clock, self.started = clock, clock()
        self.last_confirmed_station = None
        self.effects = []
        self.connected = self.armed = False
        self.pose = self.fresh = self.reserve_fresh = True
        self.reserve_s = self.reserve_capacity_s
        self.hold_available = True
        self.baseline = None

    @classmethod
    def configured(cls, root):
        root = Path(root)
        try:
            setpoints = json.loads((root / 'fake-plc-setpoints.json').read_text())
        except FileNotFoundError:
            setpoints = {}
        if not isinstance(setpoints, dict):
            raise ValueError('Configure fake-plc-setpoints.json as a tank-to-seconds object.')
        return cls(setpoints=setpoints, state_path=root / '.fake-devices.json')

    def restore(self, has_history):
        if self.state_known is not None:
            return
        self.state_known = not has_history
        if self.state_path is not None and (self.state_path.is_symlink() or self.state_path.exists()):
            try:
                if self.state_path.is_symlink():
                    raise ValueError('The simulated device snapshot must be a regular file.')
                data = json.loads(self.state_path.read_text())
                location, gripper, basket = (data[key] for key in ('location', 'gripper', 'basket'))
                station, stopped = data['station'], data['stopped']
                cycle, equipment_on, run_id = (data[key] for key in ('cycle', 'equipment_on', 'run_id'))
                reserve = data['reserve_s']
                if reserve is not None and (not finite(reserve) or reserve < 0):
                    raise ValueError('Invalid simulated reserve reading.')
                if (not isinstance(location, str) or not location or gripper not in ('held', 'released')
                        or (basket is not None and not isinstance(basket, str))
                        or (run_id is not None and not isinstance(run_id, str))
                        or type(station) is not int or not 0 <= station <= 255
                        or type(cycle) is not int or cycle < 0
                        or any(type(data[key]) is not bool for key in ('effect_pending', 'state_observed'))
                        or type(stopped) is not bool or type(equipment_on) is not bool):
                    raise ValueError('Invalid simulated device observation.')
            except (OSError, ValueError, KeyError, TypeError):
                self.state_known = False
                return
            self.snapshot = data
            self.agv.station, self.agv.stopped = station, stopped
            self.robot.location, self.robot.gripper, self.robot.basket = location, gripper, basket
            self.plc.cycle, self.plc.equipment_on = cycle, equipment_on
            self.run_id, self.reserve_s = run_id, reserve
            self.effect_pending = data['effect_pending']
            self.state_observed = data['state_observed']
            self.state_known = True
        elif self.state_known:
            self._persist()

    def snapshot_intact(self):
        return self.state_path is None or Executor._matches_file(self.state_path, self.snapshot)

    def _persist(self, *, pending=False):
        if self.state_path is None or not self.state_known:
            return
        if not self.snapshot_intact():
            raise SnapshotDamage(self.state_path.name)
        state = dict(effect_pending=pending,
            location=self.robot.location, gripper=self.robot.gripper, basket=self.robot.basket,
            station=self.agv.station, stopped=self.agv.stopped, cycle=self.plc.cycle,
            equipment_on=self.plc.equipment_on, run_id=self.run_id,
            state_observed=self.state_observed,
            reserve_s=self.reserve_s if self.reserve_fresh and finite(self.reserve_s) else None)
        try:
            atomic_json(self.state_path, state, sync_directory=True)
        except OSError as exc:
            if Executor._matches_file(self.state_path, state):
                self.snapshot = state
            raise SnapshotWriteError(self.state_path.name, str(exc)) from exc
        self.snapshot = state

    def check(self, job_type, params):
        if self.effect_pending:
            raise ValueError('The previous physical effect has no confirmed observation.')
        if not self.state_known or not self.fresh:
            raise ValueError('Device feedback is stale or retained simulation state is unknown.')
        if not self.pose:
            raise ValueError('Robot interlock has no qualified pose.')
        if not self.hold_available:
            raise ValueError('A verified receiving hold is unavailable.')
        if job_type == 'dip':
            if self.plc.alarm:
                raise ValueError('PLC level alarm prevents immersion.')
            for step in params['steps']:
                setting = (self.plc.setpoints or {}).get(step['tank'])
                if not finite(setting) or setting <= 0 or setting != step['setpoint']:
                    raise ValueError('Configure the simulated PLC tank setpoint to match the reviewed plan.')
        elif params.get('load_input', 'run_id' in params):
            self._check_input(params)
        elif job_type == 'station_visit':
            self.check_travel(params if 'run_id' in params else None)

    def check_travel(self, permission=None):
        if (not self.state_known or not self.fresh or not self.pose or not self.agv.stopped
                or self.plc.equipment_on):
            raise ValueError('Fresh stopped feedback, equipment off and a qualified travel pose are required.')
        if permission and permission.get('transfer'):
            if (self.robot.location != 'transfer' or self.robot.gripper != 'held'
                    or self.robot.basket != permission['basket'] or self.run_id != permission['run_id']):
                raise ValueError('The verified transfer must retain the basket in the gripper.')
        else:
            hold = self.hold()
            if permission and (hold['basket'], hold['run_id']) != (permission['basket'], permission['run_id']):
                raise ValueError('The travel hold does not belong to this basket and run.')
            if not permission and hold['location'] != 'output' and hold['basket'] is not None:
                raise ValueError('Reconcile the retained basket before manual travel.')

    def reserve_reading(self, required_s):
        usable_s = self.reserve_s if finite(self.reserve_s) and self.reserve_s >= 0 else None
        return dict(usable_s=usable_s, required_s=required_s, fresh=self.reserve_fresh,
                    observed_mono_s=self.clock())

    @staticmethod
    def check_reserve(reading):
        if not reading['fresh'] or reading['usable_s'] is None:
            raise ValueError('Simulated battery reserve feedback is stale or unknown.')
        if reading['usable_s'] < reading['required_s']:
            raise ValueError('Insufficient battery reserve for job and recovery.')

    def effect_cost(self, effect, value):
        if effect == 'station':
            return self.plc.travel_s
        if effect == 'place' and value['source'] != 'input':
            return self.plc.transfer_s
        if effect == 'dwell':
            return max(0, self.plc.setpoints[value['tank']] + self.plc.dwell_offset)
        return 0

    def reserve_budget(self, job_type, params):
        if job_type == 'station_visit':
            return self.recovery_margin_s + self.effect_cost('station', params['station'])
        steps = params['steps']
        budget = self.recovery_margin_s + sum(self.effect_cost('dwell', step) for step in steps)
        budget += self.effect_cost('place', params)
        if len(steps) == 2:
            budget += self.plc.transfer_s
            if steps[0]['station'] != steps[1]['station']:
                budget += self.effect_cost('station', steps[1]['station'])
        return budget

    def _check_input(self, params):
        hold = self.hold()
        if (hold['location'] == 'input'
                and (hold['basket'] is None or (hold['basket'] == params['basket']
                     and self.run_id in (None, params['run_id'])))):
            return
        if hold['location'] == 'output' and hold['basket'] and self.run_id and self.run_id != params['run_id']:
            return
        raise ValueError('Verify an available input or unload the previous basket from a verified output.')

    def _load_input(self, params):
        self.robot.location, self.robot.basket = 'input', params['basket']
        self.run_id = params['run_id']
        return self.hold()

    def check_handoff(self, step, source, gripper, basket, run_id=None):
        if self.run_id is not None and self.run_id != run_id:
            raise ValueError('The retained basket belongs to a different run.')
        if not self.agv.stopped or self.agv.station != step['station']:
            raise ValueError('The AGV is not at the required immersion station.')
        if self.robot.location != source or self.robot.gripper != gripper:
            raise ValueError('The source or gripper does not match the verified basket handoff.')
        if self.robot.basket != basket and (source != 'input' or self.robot.basket is not None):
            raise ValueError('The retained basket does not match the basket in the job.')

    def _clock(self):
        return self.plc.clock + self.clock() - self.started

    def perform(self, effect, value):
        if effect == 'station':
            self.check_travel(value if isinstance(value, dict) else None)
        elif effect == 'load_input':
            self._check_input(value)
        elif effect == 'place':
            self.check_handoff(value, value['source'], value['gripper'], value['basket'], value['run_id'])
        self._persist(pending=True)
        if self.before_effect is not None:
            try:
                self.before_effect()
            except Stopped:
                self._persist()
                raise
        if effect == 'load_input':
            evidence = self._load_input(value)
        elif effect == 'station':
            value = value['station'] if isinstance(value, dict) else value
            evidence = self.agv.visit(value)
            self.plc.clock += self.plc.travel_s
            from .agv import timestamp
            self.last_confirmed_station = (value, timestamp())
            self.effects.append((effect, value))
        elif effect == 'place':
            if value['source'] != 'input':
                self.plc.clock += self.plc.transfer_s
            self.robot.place(value)
            self.robot.basket = value['basket']
            self.run_id = value['run_id']
            evidence = self.baseline = self.plc.placed(value)
            self.effects.append((effect, value['id']))
        elif effect == 'dwell':
            evidence = self.plc.finish(value, self.baseline)
            evidence['sample_s'] = self._clock()
        elif effect == 'lift':
            self.robot.lift(value)
            self.effects.append((effect, value))
            evidence = dict(location=self.robot.location, released=self.robot.gripper == 'released', lift_s=self._clock())
        else:
            raise ValueError('Unknown simulated effect.')
        self.reserve_s -= self.effect_cost(effect, value)
        self.state_observed = True
        self._persist()
        return dict(evidence, clock_s=self._clock())

    def hold(self):
        if not self.reserve_fresh or not finite(self.reserve_s) or self.reserve_s <= 0:
            raise ValueError('Safe hold reserve is unverified.')
        if not (self.state_known and not self.effect_pending and self.fresh and self.hold_available and self.agv.stopped and
                self.robot.gripper == 'released' and self.robot.location in ('input', 'park', 'output')
                and not self.plc.equipment_on):
            raise ValueError('Safe hold is unverified.')
        return dict(location=self.robot.location, station=self.agv.station, gripper=self.robot.gripper,
                    stopped=True, equipment_on=False, basket=self.robot.basket, run_id=self.run_id,
                    reserve=self.reserve_reading(self.recovery_margin_s))

    operator_station_hold = hold

    def connect(self):
        self.connected = True

    def arm(self):
        if not self.connected or not self.state_known or not self.fresh or not self.agv.stopped or not self.pose:
            raise ValueError('Connect and verify fresh stopped feedback before arming.')
        self.hold()
        self.armed = True

    def disarm(self):
        self.armed = False

    def close(self, *, final=False):
        self.disarm()
        self.connected = False
        self.last_confirmed_station = None

    def stop(self):
        self.disarm()
        self.agv.stopped = True
        self.plc.equipment_on = False
        return True

    def stop_evidence(self, requested_at, attempted_at):
        if (attempted_at is not None and self.state_known and self.fresh
                and self.agv.stopped and not self.plc.equipment_on):
            return attempted_at
        return None

    def read(self):
        return self.status()

    def status(self):
        fresh = self.state_known and not self.effect_pending and self.fresh
        reserve = self.reserve_reading(self.recovery_margin_s) if fresh else None
        percent = (None if not reserve or not reserve['fresh'] or reserve['usable_s'] is None
                   else min(100, round(100 * reserve['usable_s'] / self.reserve_capacity_s)))
        return dict(connected=self.connected, armed=self.armed, connection_id=1, sequence=0,
                    simulated=True, stale=not fresh, reserve=reserve, log=[], results={}, settings={'mode': 'wired'},
                    last_confirmed_station=self.last_confirmed_station,
                    status_age_s=0 if fresh else None, status_monotonic=time.monotonic() if fresh else None,
                    status=dict(
                    current_station=self.agv.station if fresh else None,
                    stopped=self.agv.stopped if fresh else None, station_arrival=self.last_confirmed_station is not None,
                    fault_code=0, battery_percent=percent,
                    obstacle_stop=False, actual_speed_m_h=0, mode=2, max_stations=20))


class SnapshotDamage(ValueError):
    pass


class SnapshotWriteError(OSError):
    pass


class Stopped(Exception):
    pass


class _ExecutorJobs(JobOwner):
    def __init__(self, root, append):
        super().__init__(root, load_saved_jobs=False)
        self.append = append

    def _save(self, job):
        self.append(dict(job=job))


class Executor:
    def __init__(self, root, devices, *, clock=time.monotonic, build='development', gateway_enabled=False):
        self.lease_s = float(os.environ.get('ANODIZING_LEASE_S', LEASE_S))
        self.heartbeat_s = heartbeat_interval(float(os.environ.get('ANODIZING_HEARTBEAT_S', HEARTBEAT_S)),
                                              self.lease_s)
        expires = os.environ.get('ANODIZING_MANUAL_TRAVEL_UNTIL')
        expires = None if expires is None else float(expires)
        self.manual_travel_min_battery = float(os.environ.get('ANODIZING_MANUAL_TRAVEL_MIN_BATTERY', '20'))
        if ((expires is not None and not finite(expires))
                or not finite(self.manual_travel_min_battery) or not 0 <= self.manual_travel_min_battery <= 100):
            raise ValueError('Set a finite manual travel expiry and a battery floor from 0 to 100.')
        self.isolated_track = expires is not None and os.environ.get('ANODIZING_MANUAL_TRAVEL_ISOLATED_TRACK') == '1'
        self.manual_travel_until = expires if self.isolated_track or devices.simulated or devices.manual_travel_qualified else None
        self.manual_travel_missing_evidence = expires is not None and self.manual_travel_until is None
        if self.manual_travel_until is not None and not devices.simulated:
            devices.configure_manual_travel(self.manual_travel_until, self.manual_travel_min_battery, self.isolated_track)
        self.owner = _ExecutorJobs(root, self._append)
        self.devices, self.clock, self.build = devices, clock, build
        self.boot = uuid4().hex
        self.guard = RLock()
        self.admission = RLock()
        self.gateway = JobGateway(self, Path(root) / 'module-keys.json', enabled=gateway_enabled)
        self.session = None
        self.retired = set()
        self.last_sequence = -1
        self.deadline = None
        self.control_generation = self.stop_generation = 0
        self.generations = count(1)
        self.stop_lock = RLock()
        self.stop_tickets = {}
        self.armed = self.arming = self.disarm_pending = False
        self.stop_result = None
        self.stop_at = None
        self.stop_attempt_at = None
        self.stop_workers = set()
        self.active = None
        self.loss = Event()
        self.closing = Event()
        self.check = None
        self.journal = Path(root) / ".executor-journal"
        self.journal_tip = Path(root) / ".executor-journal-tip.json"
        self.journal_hashes = []
        self.journal_stamps = []
        self.choice_job_id = None
        self.aborted_runs = set()
        self.saved_tip = None
        self.damaged = set()
        self.accepted = []
        self.write_fault = None
        self.wire_fault_seen = None
        self.pending_check_write = False
        self._read_journal()
        self.open_air = None
        self.quality_alarm = None
        if isinstance(devices, FakeDevices) and not self.damaged:
            try:
                devices.restore(bool(self.accepted))
            except SnapshotWriteError as exc:
                self._record_write_fault(*exc.args)
            if devices.state_path is not None and not devices.state_known:
                self.damaged.add(devices.state_path.name)
        for ident, job in self.owner.jobs.items():
            if job['state'] in ('running', 'waiting'):
                job.update(state='interrupted', pending=None, error='The executor restarted during this job.')
            result = job.get('result') or {}
            open_air = result.get('open_air')
            if (open_air and open_air['run_id'] == getattr(devices, 'run_id', None)
                    and open_air['run_id'] not in self.aborted_runs
                    and result['steps'][-1]['air_s'] is None and not result.get('open_air_closed_unknown')
                    and devices.robot.location in ('park', 'transfer')):
                self.open_air = dict(open_air, clock_unknown=True)
            reason = result.get('alarm') or (job.get('checkpoint') or {}).get('alarm')
            if result.get('open_air_alarm'):
                self.quality_alarm = deepcopy(result['open_air_alarm'])
            elif reason:
                self.quality_alarm = dict(job_id=ident, reason=reason)
        self.owner.execution_uncertain = False
        latest = self.owner.jobs[self.accepted[-1]] if self.accepted else None
        if latest and latest['id'] != self.choice_job_id and (
                latest['state'] == 'interrupted' or latest.get('execution_uncertain') or (
                    (latest.get('executor_payload') or {}).get('params', {}).get('run_id') and
                    ((latest.get('result') or {}).get('hold') or {}).get('location') != 'output')):
            self._hold('The executor restarted during a basket or station visit.', job=latest)
        elif self.check:
            self._hold(self.check['reason'], facts=self.check)
        elif isinstance(devices, FakeDevices) and devices.effect_pending:
            self._hold('The executor restarted with an unverified device effect.', job=latest)
        self._refresh_manual_check()
        self.watch = Thread(target=self._watch, daemon=True, name='executor-lease')
        self.watch.start()

    @staticmethod
    def _validate_saved_job(job):
        if job.get('module') != 'executor' or job.get('state') not in (
                'waiting', 'running', 'done', 'failed', 'interrupted', 'stopped', 'refused'):
            raise ValueError('Invalid saved executor job.')
        result = job.get('result')
        checkpoint = job.get('checkpoint')
        if job['state'] == 'done' and (not isinstance(result, dict) or result.get('outcome') not in (
                'succeeded', 'held', 'unknown', 'stopped', 'failed')):
            raise ValueError('Invalid saved terminal executor result.')
        if result is None and isinstance(checkpoint, dict):
            result = {'steps': checkpoint.get('steps', [])}
        validate_measured_rows(result if result is not None else {}, checkpoint)
        if result and result.get('open_air'):
            exposure = result['open_air']
            if (not isinstance(exposure, dict) or exposure.get('job_id') != job['id']
                    or not result.get('steps') or exposure.get('lift_s') != result['steps'][-1]['lift_s']
                    or exposure.get('air_limit') != result['steps'][-1]['limits']['air']
                    or not all(isinstance(exposure.get(key), str) for key in ('basket', 'run_id'))):
                raise ValueError('Invalid saved open-air evidence.')

    def _journal_path(self, index):
        return self.journal / f'{index:012d}.json'

    @staticmethod
    def _digest(entry):
        return sha256(json.dumps(entry, sort_keys=True, allow_nan=False).encode()).hexdigest()

    @staticmethod
    def _stamp(path):
        stat = path.lstat()
        return stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns

    def _read_journal(self):
        path = self.journal_tip
        try:
            if self.journal.is_symlink():
                path = self.journal
                raise ValueError('The executor journal requires a regular directory.')
            if self.journal_tip.is_symlink():
                raise ValueError('The executor journal requires regular files.')
            paths = sorted(self.journal.glob('*.json'))
            if self.journal_tip.exists():
                self.saved_tip = json.loads(self.journal_tip.read_text())
            if paths and self.saved_tip is None:
                self.damaged.add(self.journal_tip.name)
                return
            tip = self.saved_tip or dict(count=0, digest=None)
            if (type(tip['count']) is not int or tip['count'] < 0 or
                    len(paths) > tip['count'] + 1 or
                    (tip['count'] == 0 and tip['digest'] is not None)):
                raise ValueError('The executor journal count differs from its durable tip.')
            previous = None
            for index, path in enumerate(paths, 1):
                expected = self._journal_path(index - 1)
                if path != expected:
                    path = expected
                    raise ValueError('Executor journal sequence is damaged.')
                if path.is_symlink():
                    raise ValueError('Executor journal sequence is damaged.')
                entry = json.loads(path.read_text())
                if entry['previous'] != previous:
                    raise ValueError('Executor journal chain is damaged.')
                previous = self._digest(entry)
                if index == tip['count'] and previous != tip['digest']:
                    raise ValueError('Executor journal entry differs from its durable tip.')
                if 'job' in entry:
                    job = entry['job']
                    ident = job['id']
                    if not ID.fullmatch(ident):
                        raise ValueError('Invalid executor job ID.')
                    self._validate_saved_job(job)
                    if ident not in self.owner.jobs and job['state'] != 'refused':
                        self.accepted.append(ident)
                    self.owner.jobs[ident] = job
                elif 'check' in entry:
                    self.check = entry['check']
                elif 'choice' in entry:
                    self.choice_job_id = entry['check_at_choice']['job_id']
                    if entry['choice']['choice'] == 'abort':
                        self.aborted_runs.add(entry['choice']['run_id'])
                    self.check = None
                else:
                    raise ValueError('Invalid executor journal event.')
                self.journal_hashes.append(previous)
                self.journal_stamps.append(self._stamp(path))
            if len(paths) < tip['count']:
                path = self._journal_path(len(paths))
                raise ValueError('Executor journal sequence is damaged.')
        except (AttributeError, KeyError, OSError, TypeError, ValueError):
            self.damaged.add(str(path.relative_to(self.journal.parent)))

    def _record_write_fault(self, path, exc):
        if not self.write_fault:
            self.stop()
        self.write_fault = f'Executor write failed for {path}: {exc}'
        self.pending_check_write = True
        check = self._hold(self.write_fault)
        if not check['technician']:
            self.check = dict(check, reason=self.write_fault)

    def _write_tip(self, count, digest):
        tip = dict(count=count, digest=digest)
        try:
            atomic_json(self.journal_tip, tip, sync_directory=True)
        except OSError:
            if self._matches_file(self.journal_tip, tip):
                self.saved_tip = tip
            raise
        self.saved_tip = tip

    def _append(self, entry):
        self._scan_accepted()
        self._require_intact_evidence()
        path = self.journal
        count = len(self.journal_hashes)
        try:
            path.mkdir(exist_ok=True)
            previous = self.journal_hashes[-1] if count else None
            if self.saved_tip is None or count != self.saved_tip['count']:
                path = self.journal_tip
                self._write_tip(count, previous)
            path = self._journal_path(count)
            entry = dict(entry, previous=previous)
            digest = self._digest(entry)
            try:
                atomic_json(path, entry, sync_directory=True)
            except OSError:
                if self._matches_file(path, entry):
                    self.journal_hashes.append(digest)
                    self.journal_stamps.append(self._stamp(path))
                raise
            self.journal_stamps.append(self._stamp(path))
            self.journal_hashes.append(digest)
            path = self.journal_tip
            self._write_tip(count + 1, digest)
        except OSError as exc:
            job = entry.get('job')
            if job and len(self.journal_hashes) > count and job['id'] not in self.owner.jobs:
                self.owner.jobs[job['id']] = dict(job, state='interrupted') if job['state'] != 'refused' else job
                if job['state'] != 'refused':
                    self.accepted.append(job['id'])
            self._record_write_fault(path, exc)
            raise

    @property
    def generation(self):
        return max(self.control_generation, self.stop_generation)

    @property
    def gateway_enabled(self):
        return self.gateway.enabled

    @property
    def armed(self):
        return self.gateway.ownership.armed

    @armed.setter
    def armed(self, armed):
        self.gateway.ownership.armed = armed

    def health(self):
        return dict(protocol=PROTOCOL, build=self.build, boot=self.boot, job_types=self.devices.job_types,
                    motion_enabled=self.devices.simulated and self.manual_travel_until is None,
                    simulated=self.devices.simulated, manual_travel_until=self.manual_travel_until,
                    manual_travel_min_battery=self.manual_travel_min_battery,
                    manual_travel_missing_evidence=self.manual_travel_missing_evidence,
                    manual_travel_isolated_track=self.isolated_track and time.time() < self.manual_travel_until,
                    heartbeat_s=self.heartbeat_s, lease_s=self.lease_s, gateway_enabled=self.gateway_enabled,
                    module_lease_s=MODULE_LEASE_S)

    @property
    def recovery(self):
        return bool(self.check or self.damaged or self.owner.storage_error or self.write_fault)

    def _watch(self):
        while not self.closing.wait(.1):
            with self.guard:
                self._expire()
                self._check_device_journal()
            try:
                self._update_open_air()
                if self.pending_check_write:
                    with self.admission, self.owner.lock:
                        if self.pending_check_write and self.check:
                            self._append(dict(check=self.check))
                            self.pending_check_write = False
            except (OSError, ValueError):
                pass
            if self.disarm_pending and self.admission.acquire(blocking=False):
                try:
                    self._apply_disarm()
                except (OSError, ValueError):
                    pass
                finally:
                    self.admission.release()
            self._confirm_stop()

    @staticmethod
    def _matches_air(exposure, params):
        return (
            params.get('basket') == exposure['basket'] and params.get('run_id') == exposure['run_id']
            and params.get('load_input') is not True
            and (params.get('load_input') is False or 'steps' in params))

    def _update_open_air(self, immersed_s=None):
        with self.owner.lock:
            exposure = self.open_air
            if self.damaged or self.owner.storage_error or exposure is None or self.owner.active == exposure['job_id']:
                return False
            clock_unknown = exposure.get('clock_unknown', False)
            if immersed_s is None and (clock_unknown or self.devices.robot.location not in ('park', 'transfer')):
                return False
            elapsed = (None if clock_unknown else
                       (self.devices._clock() if immersed_s is None else immersed_s) - exposure['lift_s'])
            if immersed_s is None and elapsed <= exposure['air_limit']:
                return False
            ident = exposure['job_id']
            job = self._stored_job(ident)
            if job is None:
                return False
            result = job['result']
            row = result['steps'][-1]
            alarm = result.get('open_air_alarm')
            exceeded = elapsed is not None and elapsed > exposure['air_limit']
            if exceeded and not alarm:
                alarm = dict(job_id=ident, step_id=row['id'], open_air=True, measured_air_s=elapsed,
                             reason='Air exposure limit exceeded')
                row['inspection'] = True
                result['open_air_alarm'] = alarm
            elif immersed_s is None:
                return exceeded
            if immersed_s is not None:
                row['air_s'] = elapsed
                if clock_unknown:
                    row['inspection'] = True
                    result['open_air_closed_unknown'] = True
                    if not alarm:
                        alarm = dict(job_id=ident, step_id=row['id'], open_air=True, measured_air_s=None,
                                     reason='Air exposure could not be measured across an IPC restart')
                        result['open_air_alarm'] = alarm
                self.open_air = None
            self.owner._save(job)
        if alarm:
            with self.guard:
                self.quality_alarm = alarm
        return exceeded or bool(alarm)

    def _apply_disarm(self):
        # Admission serializes native disarm with a later Arm.
        with self.guard:
            if not self.disarm_pending:
                return
            generation = self.generation
        self.devices.disarm()
        with self.guard:
            if generation == self.generation:
                self.disarm_pending = False

    def _expire(self):
        now = self.clock()
        controller_expired = self.deadline is not None and now >= self.deadline
        if controller_expired:
            self.deadline = None
        if self.gateway.watch_heartbeat(now):
            return
        if controller_expired:
            armed = self.armed
            if self.gateway.ownership.owner is not None:
                self.gateway.change_owner(None)
            basket_active = self._retained_simulated_basket()
            if armed or self.active or self.open_air or basket_active:
                self.stop()
                idle = not (basket_active or self.active or self.open_air)
                self._hold('The controller lease expired.', facts=dict(idle=True, manual=True) if idle else None)
            else:
                self.control_generation = next(self.generations)

    def _retained_simulated_basket(self):
        return (isinstance(self.devices, FakeDevices) and self.devices.run_id and
                self.devices.run_id not in self.aborted_runs and self.devices.robot.location != 'output')

    def _identity(self, envelope):
        session = envelope.get('session')
        if (type(envelope.get('protocol')) is not int or envelope['protocol'] != PROTOCOL or
            envelope.get('boot') != self.boot or not isinstance(session, str) or not ID.fullmatch(session)):
            raise ValueError('Incompatible protocol, boot identity or controller session.')
        return session

    def heartbeat(self, envelope):
        with self.guard:
            session = self._identity(envelope)
            self._expire()
            seq = envelope.get('sequence')
            if type(seq) is not int or seq < 0:
                raise ValueError('Heartbeat needs a nonnegative sequence.')
            if session != self.session:
                if self.deadline is not None or session in self.retired:
                    raise ValueError('The controller session is fenced.')
                if self.session is not None:
                    self.retired.add(self.session)
                self.session, self.last_sequence = session, -1
            if seq > self.last_sequence:
                self.last_sequence = seq
                self.deadline = self.clock() + self.lease_s
            return dict(lease_valid=self.deadline is not None, boot=self.boot)

    def _lease(self, envelope):
        self._expire()
        if self._identity(envelope) != self.session or self.deadline is None:
            raise ValueError('The controller lease expired or was fenced.')

    def _public_job(self, ident) -> Job | None:
        if ident is None:
            return None
        job = self.owner.jobs[ident]
        result = job.get('result') or {}
        checkpoint = job.get('checkpoint')
        if not result and job['state'] == 'interrupted' and (checkpoint or {}).get('steps'):
            result = dict(steps=checkpoint['steps'], alarm=checkpoint.get('alarm'))
        payload = job.get('executor_payload') or {}
        return dict(job_id=ident, state=job['state'], outcome=result.get('outcome', {
                    'interrupted': 'unknown', 'refused': 'failed', 'stopped': 'stopped',
                    'failed': 'unknown' if job.get('execution_uncertain') else 'failed',
                    }.get(job['state'])), checkpoint=deepcopy(checkpoint), result=deepcopy(result),
                    error=job.get('error'), **{key: payload[key] for key in ('client_name', 'token_id') if key in payload})

    def _fence_evidence(self, ident):
        with self.guard:
            if ident not in self.damaged:
                self.damaged.add(ident)
                self.armed = False
                self.control_generation = next(self.generations)
                self.disarm_pending = True
                self.loss.set()
                if self.active is not None:
                    self.active.requested.set()

    def _evidence_faults(self):
        faults = {}
        for ident in sorted(self.damaged):
            file = ident if ident.startswith('.') or ident == 'agv-wire.jsonl' else f'.jobs/{ident}.json'
            faults[file] = f'IPC evidence is missing, unreadable or inconsistent in {file}. Please call a technician.'
        return faults

    def _check_device_journal(self):
        failure = getattr(self.devices, 'journal_error', None)
        if not failure or failure is self.wire_fault_seen:
            return
        self.wire_fault_seen = failure
        if isinstance(failure, OSError) and not isinstance(failure, FileNotFoundError):
            self._record_write_fault('agv-wire.jsonl', failure)
        else:
            if self.active:
                self.stop()
            self._fence_evidence('agv-wire.jsonl')

    def _require_intact_evidence(self):
        self._check_device_journal()
        if self.owner.storage_error and not self.write_fault:
            raise ValueError(f'{self.owner.storage_error} Please call a technician.')
        faults = self._evidence_faults()
        if faults:
            raise ValueError(' '.join(faults.values()))

    @staticmethod
    def _matches_file(path, expected):
        try:
            if path.is_symlink():
                return False
            if expected is None:
                return not path.exists()
            return path.is_file() and json.loads(path.read_text()) == expected
        except (OSError, ValueError, RecursionError):
            return False

    def _stored_job(self, ident):
        return self.owner.jobs.get(ident) if ident not in self.damaged else None

    def _scan_accepted(self):
        with self.owner.lock:
            if not self._matches_file(self.journal_tip, self.saved_tip):
                self._fence_evidence(self.journal_tip.name)
            path = self.journal
            try:
                if path.is_symlink():
                    raise ValueError()
                for index, stamp in enumerate(self.journal_stamps):
                    path = self._journal_path(index)
                    current = self._stamp(path)
                    if current != stamp:
                        if path.is_symlink() or self._digest(json.loads(path.read_text())) != self.journal_hashes[index]:
                            raise ValueError()
                        self.journal_stamps[index] = current
                paths = list(self.journal.glob('*.json'))
                if len(paths) != len(self.journal_stamps):
                    path = min(set(paths) - {
                        self._journal_path(index) for index in range(len(self.journal_stamps))
                    }, default=self.journal)
                    raise ValueError()
            except (OSError, TypeError, ValueError):
                self._fence_evidence(str(path.relative_to(self.journal.parent)))

    def _hold(self, reason, *, job=None, facts=None, technician=False):
        facts = facts or {}
        ident = self.active.job_id if self.active else (self.accepted[-1] if self.accepted else None)
        if facts.get('idle') is True:
            ident = None
        job = job or self.owner.jobs.get(ident) or {}
        payload = job.get('executor_payload') or {}
        params = payload.get('params') or {}
        hold = (job.get('result') or {}).get('hold') or {}
        if isinstance(self.devices, FakeDevices) and self.devices.state_known and not self.devices.effect_pending:
            hold = dict(location=self.devices.robot.location, basket=self.devices.robot.basket)
        step = params.get('step')
        if type(step) is int and params.get('steps'):
            recorded = (job.get('checkpoint') or {}).get('steps', [])
            step += min(len(recorded), len(params['steps']) - 1)
        manual = facts.get('manual') is True or (
            payload.get('type') == 'station_visit' and not params.get('run_id'))
        if manual:
            station = hold.get('station')
            if isinstance(self.devices, FakeDevices) and self.devices.state_known:
                station = self.devices.agv.station
            hold = dict(location=f'station {station}' if station is not None else facts.get('location'), basket=None)
        value = dict(reason=reason, location=hold.get('location', facts.get('location', facts.get('observed_location'))),
                     basket=hold.get('basket', facts.get('basket')),
                     job_id=facts.get('job_id') or job.get('id') or ident,
                     step=facts.get('step', step),
                     run_id=facts.get('run_id') or params.get('run_id'),
                     technician=technician or facts.get('technician') is True)
        if manual:
            value['manual'] = True
        self.armed = False
        self.disarm_pending = True
        self.loss.set()
        if self.active:
            self.active.requested.set()
        if not self.check or value['technician'] and self.check != value:
            self.control_generation = next(self.generations)
            self.check = value
        return self.check

    def _refresh_manual_check(self):
        if not self.check or not self.check.get('manual') or self.active or self.owner.active:
            return
        try:
            evidence = self.devices.operator_station_hold()
        except (OSError, ValueError):
            return
        station = evidence['station']
        location = f"station {station}" if station is not None else evidence['location']
        if self.check['location'] != location:
            self.check = dict(self.check, location=location,
                              earlier_location=self.check.get('earlier_location', self.check['location']))

    def _check_simulated_snapshot(self):
        if (isinstance(self.devices, FakeDevices) and self.devices.state_known
                and not self.devices.snapshot_intact()):
            self._fence_evidence(self.devices.state_path.name)

    def hold(self, envelope):
        with self.guard:
            self._lease(envelope)
            if not self.check and envelope.get('stop_generation') != self.generation:
                raise ValueError('Review the current Stop generation before holding.')
            reason = envelope.get('reason')
            if not isinstance(reason, str) or not reason.strip() or len(reason) > 1000:
                raise ValueError('Supply the reason for the operator check.')
            for key in ('basket', 'location', 'observed_location', 'run_id', 'job_id'):
                field = envelope.get(key)
                if field is not None and (not isinstance(field, str) or not 0 < len(field) <= 500):
                    raise ValueError('The operator check contains invalid evidence.')
            if envelope.get('step') is not None and (type(envelope['step']) is not int or envelope['step'] < 1):
                raise ValueError('The operator check contains an invalid step.')
            if self.active:
                self.stop()
            check = self._hold(reason, facts=envelope)
        with self.owner.lock:
            self._append(dict(check=check))
        return self.status()

    def operator_check(self, envelope):
        with self.admission:
            with self.owner.lock:
                self._scan_accepted()
                self._check_simulated_snapshot()
                self._require_intact_evidence()
                with self.guard:
                    self._lease(envelope)
                    if (type(envelope.get('stop_generation')) is not int or
                            envelope['stop_generation'] != self.generation):
                        raise ValueError('Review the current Stop generation before confirming.')
                    if getattr(self.devices, 'journal_write_pending', False):
                        raise ValueError('Wait for agv-wire.jsonl to finish writing before confirming.')
                    if not self.check or self.check['technician']:
                        raise ValueError('The operator check is unavailable. Review status or call a technician.')
                    if any(self.check.get(key) is not None and self.check[key] != envelope.get(key)
                           for key in ('job_id', 'run_id')):
                        raise ValueError('Review the basket and job named in this operator check.')
                    if self.active or self.owner.active or self._stop_pending() or self.stop_workers:
                        raise ValueError('Wait for the physical job and Stop to finish.')
                    if self.stop_result and not self.stop_result['confirmed']:
                        raise ValueError('Stop remains unconfirmed.')
                    choice = envelope.get('choice')
                    if (envelope.get('confirmed') is not True or choice not in ('resume', 'abort') or
                            not all(isinstance(envelope.get(key), str) and envelope[key].strip()
                                    and len(envelope[key]) <= 500 for key in ('operator', 'location')) or
                            envelope.get('basket') is not None and not isinstance(envelope['basket'], str)):
                        raise ValueError('Confirm the physical basket and location, and supply the operator.')
                    step = envelope.get('step')
                    if choice == 'resume' and (type(step) is not int or step < 1):
                        raise ValueError('Choose the step to resume.')
                    self._refresh_manual_check()
                    if self.check.get('manual'):
                        try:
                            evidence = self.devices.operator_station_hold()
                        except (OSError, ValueError) as exc:
                            raise ValueError('The stopped station is not freshly verified.') from exc
                        station = evidence['station']
                        if station is None and choice != 'abort':
                            raise ValueError('Resume requires arrival at a station.')
                        observed = envelope['location']
                        matches = (observed == f'station {station}' if station is not None else
                                   re.fullmatch(r'between stations(?: [1-9][0-9]{0,2} and [1-9][0-9]{0,2})?', observed))
                        if not matches or envelope.get('basket') is not None:
                            raise ValueError('The confirmed station conflicts with the observed station.')
                    handoff = False
                    if isinstance(self.devices, FakeDevices):
                        if (not self.devices.state_known or choice == 'resume' and self.devices.plc.equipment_on or
                                not self.devices.agv.stopped):
                            raise ValueError('The simulated basket is not in a verified hold.')
                        observed = self.devices.robot
                        handoff = (self.devices.run_id is not None and self.devices.run_id in self.aborted_runs
                                   and envelope.get('run_id') is not None
                                   and envelope['run_id'] != self.devices.run_id
                                   and envelope['location'] == 'input' and bool(envelope.get('basket')))
                        if self.check.get('manual') or handoff:
                            pass
                        elif envelope.get('basket') is None and envelope.get('run_id') is None:
                            if self.check.get('location') not in (None, envelope['location']):
                                raise ValueError('The confirmed station conflicts with the observed station.')
                        elif (not self.devices.effect_pending and ((observed.basket is not None and
                                envelope.get('basket') != observed.basket) or envelope['location'] != observed.location)):
                            raise ValueError('The confirmed basket or location conflicts with the observed hold.')
                    if isinstance(self.devices, FakeDevices) and (self.devices.effect_pending or handoff):
                        if envelope['location'] not in ('input', 'park', 'output'):
                            raise ValueError('Confirm a safe, released hold before continuing.')
                        self.devices.robot.location = envelope['location']
                        self.devices.robot.gripper = 'released'
                        self.devices.robot.basket = envelope.get('basket')
                        self.devices.run_id = envelope.get('run_id')
                        try:
                            self.devices._persist()
                        except SnapshotWriteError as exc:
                            self._record_write_fault(*exc.args)
                            raise
                        self.devices.effect_pending = False
                    self._append(dict(choice={key: envelope.get(key) for key in
                                      ('choice', 'operator', 'basket', 'location', 'step', 'run_id')},
                                      check_at_choice=self.check))
                    if envelope['stop_generation'] != self.generation or self._stop_pending():
                        raise ValueError('Stop cancelled the reviewed operator check.')
                    if choice == 'abort' and envelope.get('run_id') is not None:
                        self.aborted_runs.add(envelope['run_id'])
                    self.choice_job_id = self.check['job_id']
                    self.check = None
                    self.pending_check_write = False
                    if self.write_fault:
                        if self.wire_fault_seen is not None:
                            self.devices.acknowledge_journal_write()
                            self.wire_fault_seen = None
                        self.write_fault = self.owner.storage_error = None
                    if (choice == 'abort' or envelope['location'] != 'park' or
                            (self.open_air and not self._matches_air(self.open_air, dict(envelope, load_input=False)))):
                        self.open_air = None
            self._apply_disarm()
        return self.status()

    def status(self, job_id=None, *, recent=False):
        ident = job_id or self.owner.active
        with self.owner.lock:
            self._scan_accepted()
            if self.active is None and self.owner.active is None:
                self._check_simulated_snapshot()
            job = self._public_job(ident) if self._stored_job(ident) is not None else None
            latest_id = self.accepted[-1] if self.accepted else None
            latest = self.owner.jobs.get(latest_id, {}).get('executor_payload') or {}
            manual_id = (latest_id if latest.get('type') == 'station_visit' and latest.get('client_name', 'agv-page') == 'agv-page' and
                         not (latest.get('params') or {}).get('run_id') else None)
            recent_jobs = self.gateway.records(summaries=True) if recent else None
        now = self.clock()
        with self.guard, self.stop_lock:
            faults = self._evidence_faults()
            if faults or self.owner.storage_error and not self.write_fault:
                self._hold(" ".join(faults.values()) or self.owner.storage_error, technician=True)
            self._refresh_manual_check()
            # The lease watcher and control operations own expiry.
            valid = self.deadline is not None and now < self.deadline
            ownership = self.gateway.ownership
            owner_valid = (now < ownership.lease.deadline) if ownership.lease is not None else valid
            armed = self.armed and owner_valid and not self._stop_pending()
            result = dict(boot=self.boot, lease_valid=valid, armed=armed,
                          session=self.session, stop_generation=self.generation,
                          stop=deepcopy(self.stop_result), recovery_required=self.recovery,
                          recovery_ids=sorted(self.damaged), evidence_faults=faults, operator_check=deepcopy(self.check),
                          quality_alarm=deepcopy(self.quality_alarm),
                          active_job_id=self.owner.active, latest_job_id=latest_id, latest_manual_job_id=manual_id)
            if self.gateway_enabled:
                result.update(owner=ownership.owner, armed_for=ownership.armed_for if armed else None)
            if recent:
                result['recent_jobs'] = recent_jobs
        return dict(result, job=job, device=self.devices.status())

    def control(self, action, envelope):
        with self.admission:
            if action == 'arm':
                self._scan_accepted()
                if self.active is None and self.owner.active is None:
                    self._check_simulated_snapshot()
                self._require_intact_evidence()
            with self.guard:
                self._lease(envelope)
                if (self.active or self.owner.active) and action != 'status_read':
                    raise ValueError('A job is active. Use Stop first.')
            if action == 'connect':
                if self.devices.status()['connected']:
                    raise ValueError('Disconnect the existing link first.')
                self.devices.connect()
            elif action == 'status_read':
                self.devices.read()
                self._confirm_stop()
            elif action == 'arm':
                self._apply_disarm()
                with self.guard:
                    self._lease(envelope)
                    if self.gateway_enabled and self.gateway.ownership.owner is None:
                        raise ValueError('Claim an owner before arming.')
                    with self.stop_lock:
                        if self.disarm_pending:
                            raise ValueError('Wait for the device disarm to complete.')
                        if type(envelope.get('stop_generation')) is not int or envelope['stop_generation'] != self.generation:
                            raise ValueError('Stop or lease expiry cancelled the reviewed Arm generation.')
                        if self.recovery:
                            raise ValueError('Complete the operator check before arming.')
                        if self._stop_pending() or (self.stop_result and not self.stop_result['confirmed']):
                            raise ValueError('Stop remains unconfirmed.')
                        self.arming = True
                        self.armed = False
                granted = False
                try:
                    if self.manual_travel_until is not None or not self.devices.simulated:
                        check_manual_feedback(self.devices.status(), self.manual_travel_min_battery)
                    self.devices.arm()
                    with self.guard:
                        self._lease(envelope)
                        if self.gateway_enabled and self.gateway.ownership.owner is None:
                            raise ValueError('Claim an owner before arming.')
                        with self.stop_lock:
                            if self._stop_pending() or envelope['stop_generation'] != self.generation:
                                raise ValueError('Stop or lease expiry cancelled the reviewed Arm generation.')
                            self.armed = granted = True
                            self.stop_result = None
                finally:
                    try:
                        if not granted:
                            with self.guard:
                                self.disarm_pending = True
                            self._apply_disarm()
                    finally:
                        with self.guard:
                            self.arming = False
            elif action in ('disarm', 'disconnect'):
                with self.guard:
                    self.armed = False
                    self.control_generation = next(self.generations)
                    self.disarm_pending = True
                self._apply_disarm()
                if action == 'disconnect':
                    self.devices.close()
            else:
                raise ValueError('Unknown control operation.')
        return self.status()

    def _stop_pending(self):
        return any(not registered.is_set() for registered, _ in tuple(self.stop_tickets.values()))

    def _confirm_stop(self):
        with self.guard, self.stop_lock:
            report, requested, attempted = self.stop_result, self.stop_at, self.stop_attempt_at
            if (not report or report['confirmed'] or self.stop_workers or self._stop_pending()
                    or not report['halt_attempted']
                    or self.arming or self.active is not None or self.owner.active is not None):
                return
        try:
            cutoff = self.devices.stop_evidence(requested, attempted)
        except (OSError, ValueError):
            return
        with self.guard, self.stop_lock:
            if (cutoff is not None and self.stop_result is report and not self.stop_workers and not self._stop_pending()
                    and not self.arming and self.active is None and self.owner.active is None):
                # Current feedback after the last halt covers each earlier receipt from this boot.
                report['halt_monotonic'] = cutoff
                for _, pending_report in tuple(self.stop_tickets.values()):
                    if pending_report['generation'] <= report['generation']:
                        pending_report['confirmed'] = True

    def _attempt_stop(self, registered, report):
        attempted = False
        try:
            attempted = self.devices.stop()
        except (OSError, ValueError):
            pass
        finally:
            attempted_at = time.monotonic() if attempted else None
            registered.wait()
            with self.stop_lock:
                report['halt_attempted'] = attempted
                if report is self.stop_result:
                    self.stop_attempt_at = attempted_at
                self.stop_workers.remove(report['generation'])
            self._confirm_stop()

    def stop(self, envelope=None, *, source=None):
        ident = (envelope or {}).get('stop_id', uuid4().hex)
        if not isinstance(ident, str) or not ID.fullmatch(ident):
            raise ValueError('Choose a valid Stop ID.')
        generation = next(self.generations)
        requested = time.monotonic()
        report = dict(stop_report(False, False), stop_id=ident, boot=self.boot, generation=generation)
        registered = Event()
        ticket = registered, report
        ticket_id = ident if source is None else (source, ident)
        original = self.stop_tickets.setdefault(ticket_id, ticket)
        if original is not ticket:
            registered, report = original
            registered.wait()
            with self.stop_lock:
                if (report is self.stop_result and not report['confirmed'] and not report['halt_attempted']
                        and report['generation'] not in self.stop_workers):
                    self.stop_workers.add(report['generation'])
                    Thread(target=self._attempt_stop, args=original, daemon=True, name='executor-stop').start()
            return deepcopy(report)
        # The ingress ticket fences authority and sends the halt before taking any shared operation lock.
        self.armed = False
        active = self.active
        if active is not None:
            active.requested.set()
        self.stop_workers.add(generation)
        Thread(target=self._attempt_stop, args=ticket, daemon=True, name='executor-stop').start()
        with self.stop_lock:
            if generation > self.stop_generation:
                self.armed = False
                self.stop_generation = generation
                self.stop_at = requested
                self.stop_attempt_at = None
                self.stop_result = report
            registered.set()
        return deepcopy(report)

    def submit(self, envelope, *, client_name=None, principal=None, client_token_id='katana') -> Job:
        ident = envelope.get('job_id')
        job_type, version, params = (envelope.get(key) for key in ('type', 'version', 'params'))
        if not isinstance(ident, str) or not ID.fullmatch(ident):
            raise ValueError('Choose a valid job ID.')
        attestation = envelope.get('attestation')
        auto_job = principal is not None or client_name == 'calibration'
        payload = dict(type=job_type, version=version, params=params)
        if client_name is not None:
            payload.update(client_name=client_name, token_id=client_token_id)
        if attestation is not None:
            payload['attestation'] = attestation
        with self.admission:
            if principal is None:
                self._identity(envelope)
            if self.gateway_enabled:
                with self.guard:
                    self.gateway.check_owner(envelope, client_name, principal)
            self._scan_accepted()
            if self.active is None and self.owner.active is None:
                self._check_simulated_snapshot()
            self._require_intact_evidence()
            if ident in self.owner.jobs:
                old = self.owner.jobs[ident]
                previous = old.get('executor_payload', payload)
                candidate = payload
                if principal is None and previous.get('client_name') in (None, client_name):
                    fields = {'token_id'} if 'client_name' in previous else {'client_name', 'token_id'}
                    candidate = {key: value for key, value in payload.items() if key not in fields}
                    previous = {key: value for key, value in previous.items() if key not in fields}
                if previous != candidate:
                    raise ValueError('Request ID was already used for a different request.')
                return self._public_job(ident)
            self._update_open_air()
            handle = JobStop('executor', ident)
            try:
                if not isinstance(job_type, str) or type(version) is not int or version != self.devices.job_types.get(job_type):
                    raise ValueError('Incompatible job type or version.')
                if self.manual_travel_until is not None:
                    if job_type != 'station_visit' or not isinstance(params, dict) or set(params) != {'station'}:
                        raise AdmissionRefused(ident, 'Recipe and programme travel and live dips are disabled.')
                    now = time.time()
                    if now >= self.manual_travel_until:
                        raise AdmissionRefused(ident, 'The manual travel window expired.')
                    if auto_job and self.isolated_track and not self.devices.simulated:
                        raise AdmissionRefused(ident, 'Live manual travel requires the operator’s per-call confirmation. Ask the operator to use the AGV page.')
                    try:
                        if not auto_job:
                            check_manual_attestation(attestation, params['station'], now=now)
                            if attestation['id'] != ident:
                                raise ValueError('The confirmation must match the station call ID.')
                        self._validate(job_type, params)
                        check_manual_feedback(self.devices.status(), self.manual_travel_min_battery, params['station'])
                    except ValueError as exc:
                        raise AdmissionRefused(ident, str(exc)) from exc
                elif not self.devices.simulated:
                    raise AdmissionRefused(ident, 'Supervised live travel is disabled. An active manual travel window and qualified interlocks are required.')
                else:
                    self._validate(job_type, params)
                    if auto_job:
                        try:
                            check_manual_feedback(self.devices.status(), self.manual_travel_min_battery,
                                                  params['station'] if job_type == 'station_visit' else None)
                        except ValueError as exc:
                            raise AdmissionRefused(ident, str(exc)) from exc
                with self.guard:
                    self.gateway.check_owner(envelope, client_name, principal)
                    generation = envelope.get('stop_generation')
                    if self.recovery:
                        raise ValueError('Complete the operator check before submitting.')
                    if self.open_air is not None and not self._matches_air(self.open_air, params):
                        raise ValueError('The requested job does not match the retained basket.')
                    self.gateway.check_armed(generation)
                    if self.active is not None or self.owner.active is not None:
                        raise AdmissionRefused(ident, 'Executor busy.')
                    loss = self.loss = Event()
                    self.active = handle
                self.devices.check(job_type, params)
                reserve = None
                if isinstance(self.devices, FakeDevices):
                    reserve = self.devices.reserve_reading(self.devices.reserve_budget(job_type, params))
                    self.devices.check_reserve(reserve)
                self.owner.submit(ident, 'executor', payload,
                                  lambda _: self._run(handle, loss, payload, reserve, generation,
                                                      module_job=auto_job))
                self.accepted.append(ident)
            except (ValueError, TypeError, KeyError) as exc:
                if self.active is handle:
                    self._hold(str(exc), facts=dict(job_id=ident, run_id=params.get('run_id')))
                    self.active = None
                elif not isinstance(exc, AdmissionRefused):
                    raise
                with self.owner.lock:
                    record = dict(id=ident, module='executor', state='refused', error=str(exc),
                                  executor_payload=payload)
                    self.owner._save(record)
                    self.owner.jobs[ident] = record
                raise AdmissionRefused(ident, str(exc)) from exc
            except BaseException:
                if self.active is handle:
                    self.active = None
                raise
        return self._public_job(ident)

    @staticmethod
    def _validate(job_type, params):
        if not isinstance(params, dict):
            raise ValueError('Job parameters must be an object.')
        if 'step' in params and (type(params['step']) is not int or params['step'] < 1):
            raise ValueError('An executor step must be a positive integer.')
        if job_type == 'station_visit':
            keys = set(params) - {'step'}
            handoff = keys in ({'station', 'run_id', 'basket'}, {'station', 'run_id', 'basket', 'load_input'}) and all(
                isinstance(params[key], str) and 0 < len(params[key]) <= 80 and params[key].strip()
                for key in ('run_id', 'basket')) and type(params.get('load_input', True)) is bool
            if ((keys != {'station'} and not handoff) or type(params['station']) is not int or
                    not 1 <= params['station'] <= 255):
                raise ValueError('Choose a station from 1 to 255.')
            return
        steps = params.get('steps')
        if (not isinstance(steps, list) or not 1 <= len(steps) <= 2 or params.get('hold') not in ('park', 'output')
                or params.get('expected_basket') != 'released'
                or not all(isinstance(params.get(key), str) and 0 < len(params[key]) <= 80
                           for key in ('run_id', 'basket', 'recipe_revision', 'source'))):
            raise ValueError('A dip needs a reviewed basket, ordered steps and a verified receiving hold.')
        if 'previous_lift' in params:
            prior = params['previous_lift']
            if (not isinstance(prior, dict) or set(prior) != {'lift_s', 'air_limit'}
                    or not finite(prior['lift_s']) or prior['lift_s'] < 0
                    or (prior['air_limit'] is not None and
                        (not finite(prior['air_limit']) or prior['air_limit'] <= 0))):
                raise ValueError('Cross-job exposure needs a verified lift and positive air limit.')
        for step in steps:
            if (not isinstance(step, dict) or not isinstance(step.get('role'), str) or step['role'] not in ROLES
                    or not finite(step.get('setpoint')) or step['setpoint'] <= 0
                    or not all(isinstance(step.get(key), str) and 0 < len(step[key]) <= 80 for key in ('id', 'tank'))
                    or type(step.get('station')) is not int or not 1 <= step['station'] <= 255
                    or not isinstance(step.get('limits'), dict) or set(step['limits']) != {'min', 'overstay', 'air'}):
                raise ValueError('A dip step has invalid identifiers, limits or timing.')
            low, high, air = (step['limits'][key] for key in ('min', 'overstay', 'air'))
            if ((low is not None and (not finite(low) or not 0 < low <= step['setpoint']))
                    or (high is not None and (not finite(high) or high < 0))
                    or (air is not None and (not finite(air) or air <= 0))):
                raise ValueError('A dip step contains an invalid limit.')

        if steps[0]['role'] in PAIRED_ROLES:
            if len(steps) != 2 or steps[1]['role'] != 'rinse':
                raise ValueError('A time-critical dip requires its immediate rinse.')
        elif len(steps) != 1:
            raise ValueError('Only a time-critical dip can be paired.')

    def _checkpoint(self, ident, checkpoint, uncertain=False):
        with self.owner.lock:
            record = self.owner.jobs[ident]
            record.update(checkpoint=deepcopy(checkpoint), execution_uncertain=uncertain)
            self.owner._save(record)

    @staticmethod
    def _immersion(placed):
        if (type(placed.get('cycle')) is not int or placed['cycle'] < 1
                or placed.get('done') is not False or placed.get('basket_present') is not True):
            raise ValueError('PLC has no fresh placement and timer-start evidence.')
        immersed_s = placed.get('start_s')
        if not finite(immersed_s):
            raise ValueError('PLC placement has no qualified immersion timestamp.')
        return immersed_s

    @staticmethod
    def _confirm_plc(placed, done, immersed_s):
        if (type(done.get('cycle')) is not int or done['cycle'] != placed['cycle']
                or done.get('done') is not True or done.get('basket_present') is not True
                or done.get('equipment_on') is not False or not finite(done.get('sample_s'))
                or not finite(done.get('elapsed_s')) or done['elapsed_s'] < 0
                or not finite(done.get('done_s'))
                or not immersed_s <= done['done_s'] <= done['sample_s']):
            raise ValueError('PLC completion is unqualified for lift: verify this cycle, basket and equipment off.')

    @staticmethod
    def _completed_row(step, immersed_s, done, lifted, incoming_air_s, inspection):
        lift_s = lifted.get('lift_s')
        if not finite(lift_s) or lift_s < done['sample_s']:
            raise ValueError('Lift has no qualified evidence after PLC completion.')
        dwell = lift_s - immersed_s
        limits = step['limits']
        overstay = lift_s - done['done_s'] if step['role'] == 'anodize' else dwell - step['setpoint']
        excursion = ((limits['min'] is not None and dwell < limits['min']) or
                     (limits['overstay'] is not None and overstay > limits['overstay']))
        row = dict(id=step['id'], tank=step['tank'], dwell_s=dwell, placed_s=immersed_s,
                   limits=limits, air_s=None, lift_s=lift_s,
                   incoming_air_s=incoming_air_s, plc=done, inspection=inspection or excursion)
        return row, excursion

    def _run(self, handle, loss, payload, reserve, generation, *, module_job=False):
        records, excursion = [], False
        checkpoint = dict(sequence=0, boot=self.boot, intent=None, confirmed=[], steps=records)
        job_type, params = payload['type'], payload['params']
        remaining = None
        if reserve is not None:
            checkpoint['admission_reserve'] = reserve
            remaining = reserve['required_s']

        def alarm(*, save=True):
            nonlocal excursion
            if not excursion:
                excursion = True
                checkpoint['alarm'] = 'Quality limit exceeded'
                with self.guard:
                    self.quality_alarm = dict(job_id=handle.job_id, reason=checkpoint['alarm'])
                if save:
                    self._checkpoint(handle.job_id, checkpoint, uncertain=True)

        def before_effect():
            with self.guard:
                self._expire()
            if handle.requested.is_set() or loss.is_set() or generation != self.generation or self._stop_pending():
                raise Stopped
            if self.manual_travel_until is not None:
                if time.time() >= self.manual_travel_until:
                    raise ValueError('The manual travel window expired before dispatch.')
                check_manual_feedback(self.devices.status(), self.manual_travel_min_battery, params['station'])
            elif module_job:
                check_manual_feedback(self.devices.status(), self.manual_travel_min_battery,
                                      params['station'] if job_type == 'station_visit' else None)

        def effect(name, value, confirm=None):
            nonlocal remaining
            before_effect()
            self.devices.check(job_type, params)
            if remaining is not None:
                reading = self.devices.reserve_reading(remaining)
                checkpoint['reserve'] = reading
                self.devices.check_reserve(reading)
            checkpoint['intent'] = dict(effect=name, value=value)
            checkpoint['sequence'] += 1
            self._checkpoint(handle.job_id, checkpoint, uncertain=True)
            before_effect()
            if isinstance(self.devices, FakeDevices):
                self.devices.before_effect = before_effect
            if self.isolated_track and not self.devices.simulated:
                result = self.devices.perform(name, value, attestation=payload['attestation'])
            else:
                result = self.devices.perform(name, value)
            if remaining is not None:
                remaining = max(self.devices.recovery_margin_s, remaining - self.devices.effect_cost(name, value))
                result = dict(result, reserve=reading)
            checkpoint['confirmed'].append(dict(effect=name, value=value, evidence=result))
            checkpoint['intent'] = None
            checkpoint['sequence'] += 1
            try:
                if confirm is not None:
                    confirm(result)
            finally:
                self._checkpoint(handle.job_id, checkpoint, uncertain=True)
            return result

        try:
            if job_type == 'station_visit':
                if params.get('load_input', 'run_id' in params):
                    effect('load_input', params)
                travel = params['station']
                if 'run_id' in params:
                    travel = {key: params[key] for key in ('station', 'basket', 'run_id')}
                effect('station', travel)
            else:
                self._dip(params, effect, records, alarm)
            if handle.requested.is_set() or loss.is_set() or generation != self.generation or self._stop_pending():
                raise Stopped
            hold = self.devices.hold()
            open_air = None
            if job_type == 'dip':
                if (hold['location'] != params['hold'] or hold['gripper'] != 'released'
                        or (hold['basket'], hold['run_id']) != (params['basket'], params['run_id'])):
                    raise ValueError('The verified released basket did not arrive at the required destination.')
                row = records[-1]
                if hold['location'] == 'park' and row['limits']['air'] is not None:
                    open_air = dict(job_id=handle.job_id, basket=params['basket'], run_id=params['run_id'],
                                    lift_s=row['lift_s'], air_limit=row['limits']['air'])
            with self.guard:
                self._expire()
                outcome = 'held' if loss.is_set() else 'succeeded'
            self._checkpoint(handle.job_id, checkpoint)
            if open_air is not None:
                with self.owner.lock:
                    self.open_air = open_air
            return dict(outcome=outcome, hold=hold, steps=records, alarm=checkpoint.get('alarm'),
                        open_air=open_air,
                        reason='Quality limit exceeded' if excursion else 'Controller lease expired' if loss.is_set() else None)
        except Stopped:
            was_unknown = bool(checkpoint['intent'] or checkpoint['confirmed'])
            if was_unknown:
                self._hold('The job stopped with a physical effect or outcome in doubt.', job=self.owner.jobs[handle.job_id])
            self._checkpoint(handle.job_id, checkpoint, uncertain=was_unknown)
            return dict(outcome='unknown' if was_unknown else 'stopped', steps=records, alarm=checkpoint.get('alarm'))
        except Exception as exc:
            if isinstance(exc, SnapshotDamage):
                self._fence_evidence(str(exc))
                self.stop()
            elif isinstance(exc, SnapshotWriteError):
                self.devices.effect_pending = True
                self._record_write_fault(*exc.args)
            was_unknown = bool(checkpoint['intent'] or checkpoint['confirmed'])
            self._hold(str(exc), job=self.owner.jobs[handle.job_id])
            self._checkpoint(handle.job_id, checkpoint, uncertain=was_unknown)
            return dict(outcome='unknown' if was_unknown else 'failed', reason=str(exc),
                        steps=records, alarm=checkpoint.get('alarm'))
        finally:
            if isinstance(self.devices, FakeDevices):
                self.devices.before_effect = None
            with self.guard:
                self.active = None
                if self.recovery or loss.is_set() or handle.requested.is_set():
                    self.armed = False
                    self.disarm_pending = True

    def _dip(self, params, effect, records, alarm):
        source, gripper, basket = params['source'], params['expected_basket'], params['basket']
        steps = params['steps']
        self.devices.check_handoff(steps[0], source, gripper, basket, params['run_id'])
        for index, step in enumerate(steps):
            immersed_s = None
            incoming_air_s = None
            inspection = False

            def record_immersion(placed):
                nonlocal immersed_s, incoming_air_s, inspection
                immersed_s = self._immersion(placed)
                prior = (dict(lift_s=records[-1]['lift_s'], air_limit=records[-1]['limits']['air'])
                         if records else self.open_air or params.get('previous_lift'))
                if prior is None:
                    return
                if prior.get('clock_unknown'):
                    inspection = self._update_open_air(immersed_s)
                    return
                incoming_air_s = immersed_s - prior['lift_s']
                if incoming_air_s < 0:
                    raise ValueError('Lift and immersion times are out of order.')
                if records:
                    records[-1]['air_s'] = incoming_air_s
                if prior['air_limit'] is not None and incoming_air_s > prior['air_limit']:
                    if records:
                        records[-1]['inspection'] = True
                    else:
                        inspection = True
                    alarm(save=False)
                if not records and self._update_open_air(immersed_s):
                    inspection = True
                    alarm(save=False)

            placed = effect('place', dict(step, source=source, gripper=gripper, basket=basket,
                                          run_id=params['run_id']), record_immersion)
            done = effect('dwell', step)
            self._confirm_plc(placed, done, immersed_s)
            rinsing = index + 1 < len(steps)
            destination = 'transfer' if rinsing else params['hold']

            def record_lift(lifted):
                if lifted.get('location') not in ('input', 'park', 'transfer', 'output'):
                    raise ValueError('The basket has not been verified out of the tank after the lift.')
                row, excursion = self._completed_row(step, immersed_s, done, lifted, incoming_air_s, inspection)
                records.append(row)
                if excursion:
                    alarm(save=False)
                if lifted.get('location') != destination or lifted.get('released') is not (not rinsing):
                    raise ValueError('The lift did not reach the required destination and gripper state.')

            effect('lift', destination, record_lift)
            if rinsing:
                source, gripper = 'transfer', 'held'
                if steps[index+1]['station'] != step['station']:
                    effect('station', dict(station=steps[index+1]['station'], transfer=True,
                                           basket=params['basket'], run_id=params['run_id']))

    def dispatch(self, operation, body=None):
        if operation == 'health':
            return self.health()
        if operation == 'status':
            return self.status((body or {}).get('job_id'))
        if operation == 'stop':
            return self.stop(body)
        if operation in ('heartbeat', 'submit', 'hold', 'operator_check'):
            return getattr(self, operation)(body)
        return self.control(operation, body)

    def close(self):
        self.closing.set()
        self.watch.join()
        with self.guard:
            if self.active:
                self.active.requested.set()
        self.owner.shutdown()
        while self.stop_workers:
            time.sleep(.01)
        self.devices.close(final=True)
