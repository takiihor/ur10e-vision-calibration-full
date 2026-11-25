"""Versioned IPC job service and device boundaries."""
from copy import deepcopy
import json
import math
import os
from pathlib import Path
from threading import TIMEOUT_MAX, Event, RLock, Thread
import time
from uuid import uuid4

from .jobs import ID, JobOwner, JobStop, atomic_json

PROTOCOL = 1
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
    reserve_capacity_s = 24 * 60 * 60
    recovery_margin_s = 10

    def __init__(self, *, clock=time.monotonic, setpoints=None, state_path=None):
        self.agv, self.robot, self.plc = FakeAGV(), FakeRobot(), FakePLC(setpoints, self._clock)
        self.state_path = Path(state_path) if state_path is not None else None
        self.state_known = None
        self.state_observed = False
        self.recovery_input = None
        self.released_basket = None
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
                recovery_input = data.get('recovery_input')
                released_basket = data.get('released_basket')
                if recovery_input is not None and (
                    not isinstance(recovery_input, dict)
                    or not finite(recovery_input.get('observed_utc')) or recovery_input['observed_utc'] <= 0
                    or not all(isinstance(recovery_input.get(key), str) and 0 < len(recovery_input[key]) <= 500
                               and recovery_input[key].strip() for key in ('source', 'disposition'))
                ):
                    raise ValueError('Invalid independently supplied recovery observation.')
                if (not isinstance(location, str) or not location or gripper not in ('held', 'released')
                        or (basket is not None and not isinstance(basket, str))
                        or (run_id is not None and not isinstance(run_id, str))
                        or type(station) is not int or not 0 <= station <= 255
                        or type(cycle) is not int or cycle < 0
                        or type(stopped) is not bool or type(equipment_on) is not bool):
                    raise ValueError('Invalid simulated device observation.')
                if released_basket is not None and (not isinstance(released_basket, dict)
                        or not isinstance(released_basket.get('hold'), dict)
                        or not isinstance(released_basket.get('observation_id'), str)):
                    raise ValueError('Invalid simulated unload observation.')
            except (OSError, ValueError, KeyError, TypeError):
                self.state_known = False
                return
            self.agv.station, self.agv.stopped = station, stopped
            self.robot.location, self.robot.gripper, self.robot.basket = location, gripper, basket
            self.plc.cycle, self.plc.equipment_on = cycle, equipment_on
            self.run_id, self.recovery_input, self.reserve_s = run_id, recovery_input, reserve
            self.released_basket = released_basket
            self.state_observed = data.get('state_observed', bool(
                recovery_input or location != 'input' or basket or run_id or station or cycle)) is True
            self.state_known = True
        elif self.state_known:
            self._persist()

    def _persist(self, *, pending=False):
        if self.state_path is None or not self.state_known:
            return
        state = {'effect_pending': True} if pending else dict(
            location=self.robot.location, gripper=self.robot.gripper, basket=self.robot.basket,
            station=self.agv.station, stopped=self.agv.stopped, cycle=self.plc.cycle,
            equipment_on=self.plc.equipment_on, run_id=self.run_id,
            state_observed=self.state_observed,
            reserve_s=self.reserve_s if self.reserve_fresh and finite(self.reserve_s) else None)
        if not pending and self.recovery_input is not None:
            state['recovery_input'] = self.recovery_input
        if not pending and self.released_basket is not None:
            state['released_basket'] = self.released_basket
        try:
            atomic_json(self.state_path, state, sync_directory=True)
        except OSError:
            self.state_known = False
            raise

    def check(self, job_type, params):
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
        self._check_input(params)
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
        self._persist(pending=True)
        self.recovery_input = None
        self.released_basket = None
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
            self.check_handoff(value, value['source'], value['gripper'], value['basket'], value['run_id'])
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
        if not (self.state_known and self.fresh and self.hold_available and self.agv.stopped and
                self.robot.gripper == 'released' and self.robot.location in ('input', 'park', 'output')
                and not self.plc.equipment_on):
            raise ValueError('Safe hold is unverified.')
        return dict(location=self.robot.location, station=self.agv.station, gripper=self.robot.gripper,
                    stopped=True, equipment_on=False, basket=self.robot.basket, run_id=self.run_id,
                    reserve=self.reserve_reading(self.recovery_margin_s))

    def observe_recovery_hold(self):
        hold = self.hold()
        self._persist()
        if self.recovery_input is not None:
            hold['recovery_input'] = deepcopy(self.recovery_input)
        return hold

    def unload_basket(self, hold, intent):
        if hold['location'] not in ('input', 'park') or hold['basket'] is None or hold['run_id'] is None:
            raise ValueError('Verify the released basket at input or park before unloading.')
        self.robot.location, self.robot.basket, self.run_id = 'input', None, None
        unloaded = self.hold()
        self.released_basket = dict(intent, hold=hold)
        self._persist()
        return unloaded

    def connect(self):
        self.connected = True

    def arm(self):
        if not self.connected or not self.state_known or not self.fresh or not self.agv.stopped or not self.pose:
            raise ValueError('Connect and verify fresh stopped feedback before arming.')
        self.hold()
        self.armed = True

    def disarm(self):
        self.armed = False

    def close(self):
        self.disarm()
        self.connected = False
        self.last_confirmed_station = None

    def stop(self):
        self.disarm()
        self.agv.stopped = True
        return True

    def stop_evidence(self, requested_at):
        # A fake read observes retained device state at the time of this call.
        try:
            self.hold()
        except ValueError:
            return False
        return True

    def read(self):
        return self.status()

    def status(self):
        fresh = self.state_known and self.fresh
        reserve = self.reserve_reading(self.recovery_margin_s) if fresh else None
        percent = (None if not reserve or not reserve['fresh'] or reserve['usable_s'] is None
                   else min(100, round(100 * reserve['usable_s'] / self.reserve_capacity_s)))
        return dict(connected=self.connected, armed=self.armed, connection_id=1, sequence=0,
                    simulated=True, stale=not fresh, reserve=reserve, log=[], results={}, settings={'mode': 'wired'},
                    last_confirmed_station=self.last_confirmed_station,
                    status_age_s=0 if fresh else None, status=dict(
                    current_station=self.agv.station if fresh else None,
                    stopped=self.agv.stopped if fresh else None, station_arrival=self.last_confirmed_station is not None,
                    fault_code=0, battery_percent=percent,
                    obstacle_stop=False, actual_speed_m_h=0, mode=2, max_stations=20))


class Stopped(Exception):
    pass


class Executor:
    def __init__(self, root, devices, *, clock=time.monotonic, build='development'):
        self.lease_s = float(os.environ.get('ANODIZING_LEASE_S', LEASE_S))
        self.heartbeat_s = heartbeat_interval(float(os.environ.get('ANODIZING_HEARTBEAT_S', HEARTBEAT_S)),
                                              self.lease_s)
        self.owner = JobOwner(root)
        self.devices, self.clock, self.build = devices, clock, build
        self.boot = uuid4().hex
        self.guard = RLock()
        self.admission = RLock()
        self.session = None
        self.retired = set()
        self.last_sequence = -1
        self.deadline = None
        self.generation = 0
        self.armed = self.arming = self.disarm_pending = False
        self.stop_result = None
        self.stop_at = None
        self.confirmed_stop_generation = None
        self.stop_workers = set()
        self.active = None
        self.loss = Event()
        self.closing = Event()
        self.active_path = Path(root) / '.executor-active.json'
        self.accepted_path = Path(root) / '.executor-accepted.json'
        self.refused_path = Path(root) / '.executor-refused.json'
        self.durable_id = None
        self.durable_count = None
        self.damaged = {path.stem for path in self.owner.directory.glob('*.json')
                        if path.stem not in self.owner.jobs}
        if self.active_path.is_symlink() or self.active_path.exists():
            try:
                if self.active_path.is_symlink():
                    raise ValueError('Active record must be a regular file.')
                marker = json.loads(self.active_path.read_text())
                self.durable_id = marker['job_id']
                self.durable_count = marker.get('accepted_count')
                if (not ID.fullmatch(self.durable_id) or (self.durable_count is not None
                        and (type(self.durable_count) is not int or self.durable_count < 1))):
                    raise ValueError('Invalid active job ID.')
            except (OSError, ValueError, KeyError, TypeError):
                self.durable_id = None
                self.durable_count = None
                self.damaged.add(self.active_path.name)
        self.accepted = self._accepted_ids()
        self.refused = {}
        if self.refused_path.exists() or self.refused_path.is_symlink():
            try:
                if self.refused_path.is_symlink():
                    raise ValueError('Refusal inventory must be a regular file.')
                refused = json.loads(self.refused_path.read_text())
                if not isinstance(refused, dict) or not all(
                        ID.fullmatch(ident) and isinstance(reason, str) for ident, reason in refused.items()):
                    raise ValueError('Refusal inventory is malformed.')
                if refused.keys() & set(self.accepted):
                    raise ValueError('Refusal and acceptance inventories disagree.')
                self.refused = refused
            except (OSError, ValueError, TypeError):
                self.damaged.add(self.refused_path.name)
        if isinstance(devices, FakeDevices):
            devices.restore(bool(self.owner.jobs or self.damaged or self.accepted))
        self.uncertain = {ident for ident, job in self.owner.jobs.items() if job.get('execution_uncertain')}
        self.uncertain.update(set(self.accepted) - self.owner.jobs.keys())
        if self.durable_id and self.durable_id not in self.owner.jobs:
            self.uncertain.add(self.durable_id)
        self.open_air = None
        self.quality_alarm = None
        for ident, job in self.owner.jobs.items():
            legacy_rows = self._legacy_rows(job)
            if legacy_rows:
                job['checkpoint']['steps'] = legacy_rows
                if any(row['inspection'] for row in legacy_rows):
                    job['checkpoint'].setdefault('alarm', 'Quality limit exceeded')
                try:
                    self.owner._save(job)
                except OSError as exc:
                    self.owner.storage_error = str(exc)
            result = job.get('result') or {}
            reason = result.get('alarm') or (job.get('checkpoint') or {}).get('alarm')
            if reason and not job.get('reconciliation'):
                self.quality_alarm = dict(job_id=ident, reason=reason)
            if (result.get('open_air') and not result.get('open_air_disposition')
                    and not job.get('reconciliation') and not job.get('recovery_observation')
                    and result['steps'][-1]['air_s'] is None):
                row = result['steps'][-1]
                row['inspection'] = True
                result.setdefault('open_air_alarm', dict(job_id=ident, step_id=row['id'], open_air=True,
                    reason='Open air exposure elapsed time is unknown after executor restart'))
                if self.quality_alarm is None:
                    self.quality_alarm = result['open_air_alarm']
                try:
                    self.owner._save(job)
                except OSError as exc:
                    self.owner.storage_error = str(exc)
        self.watch = Thread(target=self._watch, daemon=True, name='executor-lease')
        self.watch.start()

    def _accepted_ids(self):
        if self.accepted_path.exists() or self.accepted_path.is_symlink():
            try:
                if self.accepted_path.is_symlink():
                    raise ValueError('Acceptance inventory must be a regular file.')
                ids = json.loads(self.accepted_path.read_text())
                if (not isinstance(ids, list) or not ids
                        or not all(isinstance(ident, str) and ID.fullmatch(ident) for ident in ids)
                        or len(set(ids)) != len(ids)):
                    raise ValueError('Acceptance inventory is malformed.')
                if self.durable_count is not None and (self.durable_count > len(ids)
                        or ids[self.durable_count - 1] != self.durable_id):
                    raise ValueError('Acceptance inventory conflicts with the retained active marker.')
                if any(job['module'] == 'executor' and job.get('executor_payload')
                       and ident not in ids for ident, job in self.owner.jobs.items()):
                    raise ValueError('Acceptance inventory omits a retained job result.')
            except (OSError, ValueError, TypeError):
                self.damaged.add(self.accepted_path.name)
                return []
            if self.durable_id != ids[-1] or self.durable_count not in (None, len(ids)):
                self.damaged.add(self.active_path.name)
            return ids
        if self.durable_count is not None:
            self.damaged.add(self.accepted_path.name)
            return []
        if self.durable_id:
            jobs = sorted(self.owner.jobs.values(), key=lambda job: job['created'] if finite(job.get('created')) else 0)
            ids = [job['id'] for job in jobs if job['module'] == 'executor'
                   and job['id'] != self.durable_id and not job.get('missing_result')]
            ids.append(self.durable_id)
            try:
                atomic_json(self.accepted_path, ids, sync_directory=True)
            except OSError as exc:
                self.owner.storage_error = str(exc)
            return ids
        return []

    def health(self):
        return dict(protocol=PROTOCOL, build=self.build, boot=self.boot, job_types=self.devices.job_types,
                    motion_enabled=self.devices.simulated, simulated=self.devices.simulated,
                    heartbeat_s=self.heartbeat_s, lease_s=self.lease_s)

    @property
    def recovery(self):
        return bool(self.uncertain or self.damaged or self.owner.storage_error)

    def _watch(self):
        while not self.closing.wait(.1):
            try:
                self._update_open_air()
            except OSError as exc:
                self.owner.storage_error = str(exc)
            with self.guard:
                self._expire()
            if self.disarm_pending and self.admission.acquire(blocking=False):
                try:
                    self._apply_disarm()
                except (OSError, ValueError):
                    pass
                finally:
                    self.admission.release()
            self._confirm_stop()

    def _can_continue_air(self, params=None):
        exposure = self.open_air
        alarm = self.quality_alarm
        if not exposure or not alarm or not alarm.get('open_air') or alarm['job_id'] != exposure['job_id']:
            return False
        return params is None or self._matches_air(exposure, params)

    @staticmethod
    def _matches_air(exposure, params):
        return (
            params.get('basket') == exposure['basket'] and params.get('run_id') == exposure['run_id']
            and params.get('load_input') is not True
            and (params.get('load_input') is False or 'steps' in params))

    def _update_open_air(self, immersed_s=None):
        with self.owner.lock:
            exposure = self.open_air
            if exposure is None or self.owner.active == exposure['job_id']:
                return False
            if immersed_s is None and self.devices.robot.location not in ('park', 'transfer'):
                return False
            elapsed = (self.devices._clock() if immersed_s is None else immersed_s) - exposure['lift_s']
            if immersed_s is None and elapsed <= exposure['air_limit']:
                return False
            ident = exposure['job_id']
            job = self._stored_job(ident)
            if job is None:
                return False
            result = job['result']
            row = result['steps'][-1]
            alarm = result.get('open_air_alarm')
            exceeded = elapsed > exposure['air_limit']
            if exceeded and not alarm:
                alarm = dict(job_id=ident, step_id=row['id'], open_air=True, reason='Air exposure limit exceeded')
                row['inspection'] = True
                result['open_air_alarm'] = alarm
            elif immersed_s is None:
                return exceeded
            if immersed_s is not None:
                row['air_s'] = elapsed
                self.open_air = None
            self.owner._save(job)
        if alarm:
            with self.guard:
                if self.quality_alarm is None:
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
        if self.deadline is not None and self.clock() >= self.deadline:
            self.deadline = None
            self.loss.set()
            self.generation += 1
            self.armed = False
            self.disarm_pending = True

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

    def _public_job(self, ident):
        if ident is None:
            return None
        job = self.owner.get(ident)
        result = job.get('result') or {}
        checkpoint = job.get('checkpoint')
        if not result and job['state'] == 'interrupted' and (checkpoint or {}).get('steps'):
            result = dict(steps=checkpoint['steps'], alarm=checkpoint.get('alarm'))
        return dict(job_id=ident, state=job['state'], outcome=result.get('outcome', {
                    'interrupted': 'unknown', 'failed': 'unknown' if job.get('execution_uncertain') else 'failed',
                    }.get(job['state'])), checkpoint=checkpoint, result=result,
                    error=job.get('error'), acknowledged=job.get('executor_ack', False),
                    reconciliation=job.get('reconciliation'), observation=job.get('recovery_observation'))

    def _stored_job(self, ident):
        job = self.owner.jobs.get(ident)
        if job is not None and ident != self.owner.active:
            path = self.owner.directory / f'{ident}.json'
            if path.is_symlink() or not path.is_file():
                with self.guard:
                    if ident not in self.uncertain:
                        self.uncertain.add(ident)
                        self.armed = False
                        self.generation += 1
                        self.disarm_pending = True
                return None
        return job

    def _scan_accepted(self):
        with self.owner.lock:
            for ident in self.accepted:
                self._stored_job(ident)

    def status(self, job_id=None):
        ident = job_id or self.owner.active
        with self.owner.lock:
            job = self._public_job(ident) if self._stored_job(ident) is not None else None
        with self.guard:
            # Read only. The lease watcher and control operations own expiry.
            valid = self.deadline is not None and self.clock() < self.deadline
            result = dict(boot=self.boot, lease_valid=valid, armed=self.armed and valid,
                          session=self.session, stop_generation=self.generation,
                          stop=deepcopy(self.stop_result), recovery_required=self.recovery,
                          recovery_ids=sorted(self.uncertain | self.damaged), quality_alarm=self.quality_alarm,
                          active_job_id=self.durable_id, latest_job_id=self.accepted[-1] if self.accepted else None)
        return dict(result, job=job, device=self.devices.status())

    def control(self, action, envelope):
        with self.admission:
            if action == 'arm':
                self._scan_accepted()
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
                    if self.disarm_pending:
                        raise ValueError('Wait for the device disarm to complete.')
                    if type(envelope.get('stop_generation')) is not int or envelope['stop_generation'] != self.generation:
                        raise ValueError('Stop or lease expiry cancelled the reviewed Arm generation.')
                    if self.recovery or (self.quality_alarm and not self._can_continue_air()):
                        raise ValueError('Reconcile the interrupted job or quality hold before arming.')
                    if self.stop_result and not self.stop_result['confirmed']:
                        raise ValueError('Stop remains unconfirmed.')
                    self.arming = True
                    self.armed = False
                granted = False
                try:
                    self.devices.arm()
                    with self.guard:
                        self._expire()
                        if envelope['stop_generation'] != self.generation:
                            raise ValueError('Stop or lease expiry cancelled the reviewed Arm generation.')
                        self._lease(envelope)
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
                    self.generation += 1
                    self.disarm_pending = True
                self._apply_disarm()
                if action == 'disconnect':
                    self.devices.close()
            else:
                raise ValueError('Unknown control operation.')
        return self.status()

    def _confirm_stop(self):
        with self.guard:
            report, requested = self.stop_result, self.stop_at
            if (not report or report['confirmed'] or self.stop_workers or self.arming
                    or self.active is not None or self.owner.active is not None):
                return
        try:
            verified = self.devices.stop_evidence(requested)
        except (OSError, ValueError):
            return
        with self.guard:
            if verified and self.stop_result is report:
                report['confirmed'] = True
                self.confirmed_stop_generation = report['generation']

    def _attempt_stop(self, generation):
        attempted = False
        try:
            attempted = self.devices.stop()
        except (OSError, ValueError):
            pass
        finally:
            with self.guard:
                if self.stop_result and self.stop_result['generation'] == generation:
                    self.stop_result['halt_attempted'] = attempted
                self.stop_workers.remove(generation)
            self._confirm_stop()

    def stop(self, envelope=None):
        ident = (envelope or {}).get('stop_id', uuid4().hex)
        if not isinstance(ident, str) or not ID.fullmatch(ident):
            raise ValueError('Choose a valid Stop ID.')
        # Latch before storage or any status projection.
        with self.guard:
            if self.stop_result and self.stop_result['stop_id'] == ident:
                return deepcopy(self.stop_result)
            self.generation += 1
            self.armed = False
            if self.active is not None:
                self.active.requested.set()
            generation = self.generation
            self.stop_at = time.monotonic()
            self.stop_result = dict(stop_report(False, False), stop_id=ident, boot=self.boot, generation=generation)
            self.stop_workers.add(generation)
            Thread(target=self._attempt_stop, args=(generation,), daemon=True, name='executor-stop').start()
            return deepcopy(self.stop_result)

    def submit(self, envelope):
        ident = envelope.get('job_id')
        job_type, version, params = (envelope.get(key) for key in ('type', 'version', 'params'))
        if not isinstance(ident, str) or not ID.fullmatch(ident):
            raise ValueError('Choose a valid job ID.')
        payload = dict(type=job_type, version=version, params=params)
        with self.admission:
            # Compare the immutable payload even when a reply is retried after disarm.
            self._identity(envelope)
            with self.owner.lock:
                if self._stored_job(ident) is not None:
                    if self.owner.jobs[ident].get('missing_result'):
                        raise ValueError('This missing job was reconciled and cannot be replayed.')
                    self.owner.submit(ident, 'executor', payload, None)
                    return self._public_job(ident)
            self._scan_accepted()
            if ident in self.accepted or ident == self.durable_id or ident in self.damaged:
                raise ValueError('Restore or reconcile the missing accepted job record.')
            if ident in self.refused:
                raise AdmissionRefused(ident, self.refused[ident])
            self._update_open_air()
            handle = JobStop('executor', ident)
            try:
                if not isinstance(job_type, str) or type(version) is not int or version != self.devices.job_types.get(job_type):
                    raise ValueError('Incompatible job type or version.')
                self._validate(job_type, params)
                if job_type == 'station_visit' and 'run_id' in params and not isinstance(self.devices, FakeDevices):
                    raise ValueError('Basket input handoff is available in simulation only.')
                with self.guard:
                    self._lease(envelope)
                    if self.recovery:
                        # Recovery can conceal an accepted ID whose record disappeared.
                        raise ValueError('Reconcile the interrupted job before submitting.')
                    if self.quality_alarm and not self._can_continue_air(params):
                        raise ValueError('Disposition the quality hold before submitting.')
                    if self.open_air is not None and not self._matches_air(self.open_air, params):
                        raise ValueError('Resolve the retained basket exposure before starting unrelated travel.')
                    if self.active is not None or self.owner.active is not None:
                        raise ValueError('Executor busy.')
                    if (not self.armed or type(envelope.get('stop_generation')) is not int or
                            envelope['stop_generation'] != self.generation):
                        raise ValueError('Arm explicitly and refresh the Stop generation before submitting.')
                    loss = self.loss = Event()
                    self.active = handle
                self.devices.check(job_type, params)
                reserve = None
                if isinstance(self.devices, FakeDevices):
                    reserve = self.devices.reserve_reading(self.devices.reserve_budget(job_type, params))
                    self.devices.check_reserve(reserve)
            except (ValueError, TypeError, KeyError) as exc:
                with self.guard:
                    if self.active is handle:
                        self.active = None
                    if self.recovery:
                        raise
                refused = dict(self.refused, **{ident: str(exc)})
                try:
                    atomic_json(self.refused_path, refused, sync_directory=True)
                except OSError as storage_error:
                    self.owner.storage_error = str(storage_error)
                    raise
                self.refused = refused
                raise AdmissionRefused(ident, str(exc)) from exc
            except BaseException:
                with self.guard:
                    if self.active is handle:
                        self.active = None
                raise
            try:
                # Neither an older duplicate nor a newer marker can hide accepted intent.
                accepted = [*self.accepted, ident]
                atomic_json(self.accepted_path, accepted, sync_directory=True)
                self.accepted = accepted
                atomic_json(self.active_path, {'job_id': ident, 'accepted_count': len(accepted)}, sync_directory=True)
                self.durable_id = ident
                self.owner.submit(ident, 'executor', payload, lambda _: self._run(handle, loss, payload, reserve))
            except BaseException:
                with self.guard:
                    self.active = None
                    self.uncertain.add(ident)
                    if self.durable_id != ident:
                        self.damaged.add(self.active_path.name)
                raise
        return self._public_job(ident)

    @staticmethod
    def _validate(job_type, params):
        if not isinstance(params, dict):
            raise ValueError('Job parameters must be an object.')
        if job_type == 'station_visit':
            keys = set(params)
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

    def _legacy_rows(self, job):
        checkpoint = job.get('checkpoint') or {}
        payload = job.get('executor_payload') or {}
        if (job['state'] != 'interrupted' or job.get('result')
                or not isinstance(checkpoint, dict) or not isinstance(payload, dict) or payload.get('type') != 'dip'
                or 'steps' in checkpoint or not isinstance(checkpoint.get('boot'), str)
                or not ID.fullmatch(checkpoint['boot'])):
            return []
        rows = []
        try:
            params = payload['params']
            self._validate('dip', params)
            effects = iter(checkpoint['confirmed'])

            def evidence(effect, value):
                entry = next(effects)
                if entry['effect'] != effect or entry['value'] != value:
                    raise ValueError('Saved effect is out of order or differs from the accepted dip.')
                return entry['evidence']

            source, gripper = params['source'], params['expected_basket']
            steps = params['steps']
            for index, step in enumerate(steps):
                placed = evidence('place', dict(step, source=source, gripper=gripper,
                                                basket=params['basket'], run_id=params['run_id']))
                immersed_s = self._immersion(placed)
                incoming_air_s = None
                inspection = False
                prior = (dict(lift_s=rows[-1]['lift_s'], air_limit=rows[-1]['limits']['air'])
                         if rows else params.get('previous_lift'))
                if prior is not None:
                    incoming_air_s = immersed_s - prior['lift_s']
                    if incoming_air_s < 0:
                        break
                    excursion = prior['air_limit'] is not None and incoming_air_s > prior['air_limit']
                    if rows:
                        rows[-1]['air_s'] = incoming_air_s
                        rows[-1]['inspection'] |= excursion
                    else:
                        inspection = excursion
                done = evidence('dwell', step)
                self._confirm_plc(placed, done, immersed_s)
                rinsing = index + 1 < len(steps)
                destination = 'transfer' if rinsing else params['hold']
                lifted = evidence('lift', destination)
                if lifted.get('location') != destination or lifted.get('released') is not (not rinsing):
                    break
                row, _ = self._completed_row(step, immersed_s, done, lifted, incoming_air_s, inspection)
                rows.append(row)
                if rinsing:
                    source, gripper = 'transfer', 'held'
                    station = steps[index + 1]['station']
                    if station != step['station']:
                        observed = evidence('station', station)
                        if observed.get('station') != station or observed.get('stopped') is not True:
                            break
        except (StopIteration, AttributeError, KeyError, TypeError, ValueError):
            pass
        return rows

    def _run(self, handle, loss, payload, reserve):
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

        def effect(name, value, confirm=None):
            nonlocal remaining
            if handle.requested.is_set():
                raise Stopped
            self.devices.check(job_type, params)
            if not checkpoint['confirmed'] and loss.is_set():
                raise Stopped
            if remaining is not None:
                reading = self.devices.reserve_reading(remaining)
                checkpoint['reserve'] = reading
                self.devices.check_reserve(reading)
            checkpoint['intent'] = dict(effect=name, value=value)
            checkpoint['sequence'] += 1
            self._checkpoint(handle.job_id, checkpoint, uncertain=True)
            if handle.requested.is_set():
                raise Stopped
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
            if handle.requested.is_set():
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
                outcome = 'held' if loss.is_set() or excursion else 'succeeded'
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
                with self.guard:
                    self.uncertain.add(handle.job_id)
            self._checkpoint(handle.job_id, checkpoint, uncertain=was_unknown)
            return dict(outcome='unknown' if was_unknown else 'stopped', steps=records, alarm=checkpoint.get('alarm'))
        except Exception as exc:
            was_unknown = bool(checkpoint['intent'] or checkpoint['confirmed'])
            if was_unknown:
                with self.guard:
                    self.uncertain.add(handle.job_id)
            self._checkpoint(handle.job_id, checkpoint, uncertain=was_unknown)
            return dict(outcome='unknown' if was_unknown else 'failed', reason=str(exc),
                        steps=records, alarm=checkpoint.get('alarm'))
        finally:
            with self.guard:
                self.active = None
                if self.recovery or loss.is_set() or excursion:
                    self.armed = False
                    self.generation += 1
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

    def acknowledge(self, envelope):
        with self.guard:
            self._lease(envelope)
        ident = envelope.get('job_id')
        with self.owner.lock:
            job = self._stored_job(ident)
            if not job or job['state'] in ('waiting', 'running'):
                raise ValueError('No terminal result to acknowledge.')
            job['executor_ack'] = True
            self.owner._save(job)
        return {'acknowledged': True, 'job_id': ident}

    def _dispose_open_air(self, expected, observation_id, job_id):
        alarm_id = self.quality_alarm['job_id'] if (self.quality_alarm or {}).get('open_air') else None
        exposure_id = (self.open_air or {}).get('job_id') or alarm_id
        if not exposure_id:
            return False
        job = self._stored_job(exposure_id)
        exposure = ((job or {}).get('result') or {}).get('open_air')
        if not exposure or any(exposure.get(key) != value for key, value in expected.items()):
            return False
        job['result']['open_air_disposition'] = dict(job_id=job_id, observation_id=observation_id)
        self.owner._save(job)
        self.open_air = None
        return exposure_id == alarm_id

    def reconcile(self, envelope):
        with self.admission:
            with self.guard:
                self._lease(envelope)
                ident, disposition = envelope.get('job_id'), envelope.get('disposition')
                missing = envelope.get('missing') is True
                observed_only = envelope.get('observed_only') is True
                repair_requested = envelope.get('repair_active_marker') is True
                marker = self.active_path.name
                repair = repair_requested and marker in self.damaged
                observation_id = envelope.get('observation_id')
                expected = {key: envelope.get(key) for key in ('basket', 'run_id')}
                has_basket = any(value is not None for value in expected.values())
                release_basket = envelope.get('release_basket') is True
                allow_unaccounted = envelope.get('allow_unaccounted') is True
                fake = isinstance(self.devices, FakeDevices)
                if (not self.devices.simulated or self.active or self.owner.active
                        or not isinstance(ident, str) or not ID.fullmatch(ident)
                        or (missing and observed_only)
                        or (observation_id is not None and
                            (not isinstance(observation_id, str) or not ID.fullmatch(observation_id)))
                        or (has_basket and not all(isinstance(value, str) and value.strip() and len(value) <= 80
                                                   for value in expected.values()))
                        or ((release_basket or allow_unaccounted) and (not fake or not has_basket
                                                                     or observation_id is None))
                        or (release_basket and allow_unaccounted)
                        or not isinstance(disposition, str) or not disposition.strip() or len(disposition) > 1024):
                    raise ValueError('Verify physical evidence and record a reconciliation disposition.')
                alarm_job = self.quality_alarm and self.quality_alarm['job_id'] == ident
            with self.owner.lock:
                job = self._stored_job(ident)
                params = ((job or {}).get('executor_payload') or {}).get('params') or {}
                if 'basket' in params:
                    if has_basket and any(params.get(key) != value for key, value in expected.items()):
                        raise ValueError('The selected job belongs to a different basket and run.')
                    expected = {key: params[key] for key in expected}
                    has_basket = True
                evidence_key = 'recovery_observation' if observed_only else 'reconciliation'
                saved = job and job.get(evidence_key)
                retry = (saved and saved['disposition'] == disposition.strip()
                         and bool(job.get('missing_result')) == missing
                         and saved.get('observation_id') == observation_id
                         and bool(saved.get('release_basket', saved.get('unload'))) == release_basket
                         and (not saved.get('unaccounted') or allow_unaccounted)
                         and (not saved.get('expected_basket') or saved['expected_basket'] == expected))
                repaired = retry and saved.get('repaired_active_marker')
                if repair_requested and not repair and not repaired:
                    raise ValueError('There is no unreadable active marker to repair.')
                if repair and (not self.accepted or ident != self.accepted[-1]):
                    raise ValueError('The latest independently retained accepted job ID must match the damaged marker.')
                if retry and not repair and saved.get('boot') == self.boot:
                    return self._public_job(ident)
                eligible = ((missing and job is None)
                            or retry
                            or (not missing and job and (job.get('execution_uncertain') or alarm_job
                                or (observed_only and job['state'] == 'done'
                                    and (job.get('result') or {}).get('hold')))))
                if not eligible:
                    raise ValueError('Select the uncertain job to reconcile.')
            with self.guard:
                self._lease(envelope)
                self.armed = False
                self.generation += 1
                self.disarm_pending = True
            self._apply_disarm()
            with self.owner.lock:
                replay_unload = self.devices.released_basket if release_basket else None
                hold = self.devices.observe_recovery_hold() if fake else self.devices.hold()
                intent_generation = envelope.get('intent_stop_generation')
                confirmed_manual_stop = (
                    not has_basket and ident not in self.accepted
                    and envelope.get('intent_boot') == self.boot and type(intent_generation) is int
                    and self.confirmed_stop_generation is not None
                    and 0 <= intent_generation < self.confirmed_stop_generation)
                if missing and fake and not (self.devices.state_observed or self.devices.recovery_input
                                             or confirmed_manual_stop):
                    raise ValueError('Independent safe plant evidence is required for the unaccounted job.')
                unload = None
                if replay_unload and replay_unload['observation_id'] == observation_id:
                    if replay_unload.get('job_id') != ident or replay_unload.get('disposition') != disposition.strip():
                        raise ValueError('Retry the original recorded inspection disposition.')
                    if hold['location'] != 'input' or hold['basket'] is not None or hold['run_id'] is not None:
                        raise ValueError('The retained unloaded input is no longer empty.')
                    unload, hold = hold, replay_unload['hold']
                unaccounted = has_basket and any(hold.get(key) != value for key, value in expected.items())
                if unaccounted and not allow_unaccounted:
                    raise ValueError('The original basket and run are unaccounted for in the verified plant hold.')
                if release_basket and not unaccounted and not unload and hold['location'] in ('input', 'park'):
                    unload = self.devices.unload_basket(hold, dict(observation_id=observation_id, job_id=ident,
                                                                  disposition=disposition.strip()))
                resolution = dict(disposition=disposition.strip(), hold=hold, observed_utc=time.time(),
                                  observed_mono_s=self.clock(), boot=self.boot, observation_id=observation_id)
                if has_basket:
                    resolution['expected_basket'] = expected
                if unaccounted:
                    resolution['unaccounted'] = True
                if release_basket:
                    resolution['release_basket'] = True
                if unload:
                    resolution['unload'] = unload
                if repair or repaired:
                    resolution['repaired_active_marker'] = True
                if repair:
                    if self.active_path.is_symlink() or (self.active_path.exists() and not self.active_path.is_file()):
                        raise ValueError('Preserve the unreadable marker as a regular file before repair.')
                    # Retain the original inode and bytes before replacing the active marker.
                    if self.active_path.exists():
                        quarantine = self.active_path.with_name(f'{marker}.{uuid4().hex}.unreadable')
                        quarantine.hardlink_to(self.active_path)
                if missing and job is None:
                    if ident in self.damaged:
                        # Retain the damaged file before replacing its API record.
                        original = self.owner.directory / f'{ident}.json'
                        quarantine = self.owner.directory / f'{ident}.unreadable'
                        if not quarantine.exists() and not original.is_symlink():
                            with quarantine.open('xb') as stream:
                                stream.write(original.read_bytes())
                    job = dict(id=ident, module='executor', state='interrupted', fingerprint=None,
                               missing_result=True, result=dict(outcome='unknown', reconciliation=resolution))
                exposure_resolved = (has_basket and (release_basket or unaccounted)
                                     and self._dispose_open_air(expected, observation_id, ident))
                job = dict(job, **{evidence_key: resolution})
                if not observed_only:
                    job['execution_uncertain'] = False
                self.owner._save(job)
                self.owner.jobs[ident] = job
                if repair:
                    atomic_json(self.active_path, {'job_id': ident, 'accepted_count': len(self.accepted)},
                                sync_directory=True)
                self.owner.execution_uncertain = any(j.get('execution_uncertain') for j in self.owner.jobs.values())
            with self.guard:
                if repair:
                    self.durable_id = ident
                    self.damaged.discard(marker)
                if not observed_only:
                    self.uncertain.discard(ident)
                    self.damaged.discard(ident)
                if exposure_resolved or (alarm_job and (not observed_only or release_basket)):
                    self.quality_alarm = None
            self._confirm_stop()
        return self._public_job(ident)

    def dispatch(self, operation, body=None):
        if operation == 'health':
            return self.health()
        if operation == 'status':
            return self.status((body or {}).get('job_id'))
        if operation == 'stop':
            return self.stop(body)
        if operation in ('heartbeat', 'submit', 'acknowledge', 'reconcile'):
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
        self.devices.close()
