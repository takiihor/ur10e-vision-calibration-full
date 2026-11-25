"""Versioned IPC job service and device boundaries."""
from copy import deepcopy
import json
import math
from pathlib import Path
from threading import Event, RLock, Thread
import time
from uuid import uuid4

from .jobs import ID, JobOwner, JobStop, atomic_json

PROTOCOL = 1
HEARTBEAT_S = 1
LEASE_S = 3
MAX_BODY = 32768
PAIRED_ROLES = {'etch', 'desmut', 'anodize'}
ROLES = PAIRED_ROLES | {'ultrasonic', 'rinse', 'seal'}


def stop_report(attempted, confirmed):
    return dict(received=True, halt_attempted=attempted, confirmed=confirmed)


def finite(value):
    return type(value) in (int, float) and math.isfinite(value)


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
    equipment_on = False
    alarm = False
    stale_done = False
    setpoints = None
    dwell_offset = 0
    transfer_s = 1
    travel_s = 1

    def placed(self, step):
        self.cycle += 1
        self.equipment_on = step['role'] in ('anodize', 'ultrasonic')
        return dict(cycle=self.cycle, start_s=self.clock, elapsed_s=0,
                    done=False, basket_present=True, equipment_on=self.equipment_on)

    def finish(self, step, baseline):
        if self.stale_done or baseline['cycle'] != self.cycle:
            raise ValueError('PLC done is stale or belongs to another timer cycle.')
        dwell = max(0, step['setpoint'] + self.dwell_offset)
        self.clock += dwell
        self.equipment_on = False
        return dict(cycle=self.cycle, elapsed_s=dwell, done=True, basket_present=True,
                    equipment_on=False, sample_s=self.clock)


class FakeDevices:
    job_types = {'station_visit': 1, 'dip': 1}
    simulated = True

    def __init__(self, *, clock=time.monotonic):
        self.agv, self.robot, self.plc = FakeAGV(), FakeRobot(), FakePLC()
        self.clock, self.started = clock, clock()
        self.last_confirmed_station = None
        self.effects = []
        self.connected = self.armed = False
        self.battery = self.pose = self.fresh = True
        self.hold_available = True
        self.baseline = None

    def check(self, job_type, params):
        if not self.fresh:
            raise ValueError('Device feedback is stale.')
        if not self.battery:
            raise ValueError('Insufficient battery reserve for job and recovery.')
        if not self.pose:
            raise ValueError('Robot interlock has no qualified pose.')
        if not self.hold_available:
            raise ValueError('A verified receiving hold is unavailable.')
        if job_type == 'dip':
            if self.plc.alarm:
                raise ValueError('PLC level alarm prevents immersion.')
            if self.plc.setpoints is not None and any(
                self.plc.setpoints.get(s['tank']) != s['setpoint'] for s in params['steps']
            ):
                raise ValueError('PLC setpoint is zero, missing or differs from the reviewed plan.')

    def check_handoff(self, step, source, gripper, basket):
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
            evidence = self.agv.visit(value)
            self.plc.clock += self.plc.travel_s
            from .agv import timestamp
            self.last_confirmed_station = (value, timestamp())
            self.effects.append((effect, value))
        elif effect == 'place':
            self.check_handoff(value, value['source'], value['gripper'], value['basket'])
            if value['source'] != 'input':
                self.plc.clock += self.plc.transfer_s
            self.robot.place(value)
            self.robot.basket = value['basket']
            evidence = self.baseline = dict(self.plc.placed(value), start_s=self._clock())
            self.effects.append((effect, value['id']))
        elif effect == 'dwell':
            evidence = self.plc.finish(value, self.baseline)
            evidence['sample_s'] = self._clock()
        elif effect == 'lift':
            self.robot.lift(value)
            self.effects.append((effect, value))
            evidence = dict(location=value, released=self.robot.gripper == 'released', lift_s=self._clock())
        else:
            raise ValueError('Unknown simulated effect.')
        return dict(evidence, clock_s=self._clock())

    def hold(self):
        if not (self.fresh and self.hold_available and self.agv.stopped and
                self.robot.gripper == 'released' and self.robot.location in ('input', 'park', 'output')
                and not self.plc.equipment_on):
            raise ValueError('Safe hold is unverified.')
        return dict(location=self.robot.location, station=self.agv.station, gripper=self.robot.gripper,
                    stopped=True, equipment_on=False)

    def connect(self):
        self.connected = True

    def arm(self):
        if not self.connected or not self.fresh or not self.agv.stopped:
            raise ValueError('Connect and verify fresh stopped feedback before arming.')
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
        return dict(connected=self.connected, armed=self.armed, connection_id=1, sequence=0,
                    simulated=True, stale=not self.fresh, log=[], results={}, settings={'mode': 'wired'},
                    last_confirmed_station=self.last_confirmed_station,
                    status_age_s=0 if self.fresh else None, status=dict(current_station=self.agv.station,
                    stopped=self.agv.stopped, station_arrival=self.last_confirmed_station is not None,
                    fault_code=0, battery_percent=100 if self.battery else 0,
                    obstacle_stop=False, actual_speed_m_h=0, mode=2, max_stations=20))


class Stopped(Exception):
    pass


class Executor:
    def __init__(self, root, devices, *, clock=time.monotonic, build='development'):
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
        self.armed = False
        self.stop_result = None
        self.stop_at = None
        self.stop_workers = set()
        self.active = None
        self.loss = Event()
        self.closing = Event()
        self.active_path = Path(root) / '.executor-active.json'
        self.durable_id = None
        self.damaged = {path.stem for path in self.owner.directory.glob('*.json')
                        if path.stem not in self.owner.jobs}
        if self.active_path.exists():
            try:
                if self.active_path.is_symlink():
                    raise ValueError('Active record must be a regular file.')
                self.durable_id = json.loads(self.active_path.read_text())['job_id']
                if not ID.fullmatch(self.durable_id):
                    raise ValueError('Invalid active job ID.')
            except (OSError, ValueError, KeyError, TypeError):
                self.durable_id = None
                self.damaged.add(self.active_path.name)
        self.uncertain = {ident for ident, job in self.owner.jobs.items() if job.get('execution_uncertain')}
        if self.durable_id and self.durable_id not in self.owner.jobs:
            self.uncertain.add(self.durable_id)
        self.quality_alarm = None
        for ident, job in self.owner.jobs.items():
            reason = (job.get('result') or {}).get('alarm') or (job.get('checkpoint') or {}).get('alarm')
            if reason and not job.get('reconciliation'):
                self.quality_alarm = dict(job_id=ident, reason=reason)
        self.watch = Thread(target=self._watch, daemon=True, name='executor-lease')
        self.watch.start()

    def health(self):
        return dict(protocol=PROTOCOL, build=self.build, boot=self.boot, job_types=self.devices.job_types,
                    motion_enabled=self.devices.simulated, simulated=self.devices.simulated,
                    heartbeat_s=HEARTBEAT_S, lease_s=LEASE_S)

    @property
    def recovery(self):
        return bool(self.uncertain or self.damaged or self.owner.storage_error)

    def _watch(self):
        while not self.closing.wait(.1):
            with self.guard:
                self._expire()
                self._confirm_stop()

    def _expire(self):
        if self.deadline is not None and self.clock() >= self.deadline:
            self.deadline = None
            self.loss.set()
            self.generation += 1
            self.armed = False
            self.devices.disarm()

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
                self.deadline = self.clock() + LEASE_S
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
        return dict(job_id=ident, state=job['state'], outcome=result.get('outcome', {
                    'interrupted': 'unknown', 'failed': 'unknown' if job.get('execution_uncertain') else 'failed',
                    }.get(job['state'])), checkpoint=job.get('checkpoint'), result=result,
                    error=job.get('error'), acknowledged=job.get('executor_ack', False),
                    reconciliation=job.get('reconciliation'), observation=job.get('recovery_observation'))

    def status(self, job_id=None):
        ident = job_id or self.owner.active
        job = self._public_job(ident) if ident in self.owner.jobs else None
        with self.guard:
            # Read only. The lease watcher and control operations own expiry.
            valid = self.deadline is not None and self.clock() < self.deadline
            result = dict(boot=self.boot, lease_valid=valid, armed=self.armed and valid,
                          session=self.session, stop_generation=self.generation,
                          stop=deepcopy(self.stop_result), recovery_required=self.recovery,
                          recovery_ids=sorted(self.uncertain | self.damaged), quality_alarm=self.quality_alarm)
        return dict(result, job=job, device=self.devices.status())

    def control(self, action, envelope):
        with self.admission:
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
                with self.guard:
                    self._confirm_stop()
            elif action == 'arm':
                with self.guard:
                    self._lease(envelope)
                    if type(envelope.get('stop_generation')) is not int or envelope['stop_generation'] != self.generation:
                        raise ValueError('Stop or lease expiry cancelled the reviewed Arm generation.')
                    if self.recovery or self.quality_alarm:
                        raise ValueError('Reconcile the interrupted job or quality hold before arming.')
                    if self.stop_result and not self.stop_result['confirmed']:
                        raise ValueError('Stop remains unconfirmed.')
                    self.devices.arm()
                    self.armed = True
                    self.stop_result = None
            elif action in ('disarm', 'disconnect'):
                with self.guard:
                    self.armed = False
                    self.generation += 1
                    self.devices.disarm()
                if action == 'disconnect':
                    self.devices.close()
            else:
                raise ValueError('Unknown control operation.')
        return self.status()

    def _confirm_stop(self):
        if (self.stop_result and not self.stop_result['confirmed'] and not self.stop_workers
                and self.active is None and self.owner.active is None
                and self.devices.stop_evidence(self.stop_at)):
            self.stop_result['confirmed'] = True

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
                self._confirm_stop()
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
                if ident in self.owner.jobs:
                    if self.owner.jobs[ident].get('missing_result'):
                        raise ValueError('This missing job was reconciled and cannot be replayed.')
                    self.owner.submit(ident, 'executor', payload, None)
                    return self._public_job(ident)
            if not isinstance(job_type, str) or type(version) is not int or version != self.devices.job_types.get(job_type):
                raise ValueError('Incompatible job type or version.')
            self._validate(job_type, params)
            with self.guard:
                self._lease(envelope)
                if self.recovery or self.quality_alarm:
                    raise ValueError('Reconcile the interrupted job before submitting.')
                if self.active is not None or self.owner.active is not None:
                    raise ValueError('Executor busy.')
                if (not self.armed or type(envelope.get('stop_generation')) is not int or
                        envelope['stop_generation'] != self.generation):
                    raise ValueError('Arm explicitly and refresh the Stop generation before submitting.')
                handle = JobStop('executor', ident)
                loss = self.loss = Event()
                self.active = handle
            try:
                self.devices.check(job_type, params)
                atomic_json(self.active_path, {'job_id': ident}, sync_directory=True)
                self.durable_id = ident
                self.owner.submit(ident, 'executor', payload, lambda _: self._run(handle, loss, payload))
            except BaseException:
                with self.guard:
                    self.active = None
                    if self.durable_id == ident and ident not in self.owner.jobs:
                        self.uncertain.add(ident)
                raise
        return self._public_job(ident)

    @staticmethod
    def _validate(job_type, params):
        if not isinstance(params, dict):
            raise ValueError('Job parameters must be an object.')
        if job_type == 'station_visit':
            if (set(params) != {'station'} or type(params['station']) is not int or
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

    def _run(self, handle, loss, payload):
        checkpoint = dict(sequence=0, boot=self.boot, intent=None, confirmed=[])
        records, excursion = [], False
        job_type, params = payload['type'], payload['params']

        def alarm():
            nonlocal excursion
            if not excursion:
                excursion = True
                checkpoint['alarm'] = 'Quality limit exceeded'
                with self.guard:
                    self.quality_alarm = dict(job_id=handle.job_id, reason=checkpoint['alarm'])
                self._checkpoint(handle.job_id, checkpoint, uncertain=True)

        def effect(name, value):
            if handle.requested.is_set():
                raise Stopped
            self.devices.check(job_type, params)
            if not checkpoint['confirmed'] and loss.is_set():
                raise Stopped
            checkpoint['intent'] = dict(effect=name, value=value)
            checkpoint['sequence'] += 1
            self._checkpoint(handle.job_id, checkpoint, uncertain=True)
            if handle.requested.is_set():
                raise Stopped
            result = self.devices.perform(name, value)
            checkpoint['confirmed'].append(dict(effect=name, value=value, evidence=result))
            checkpoint['intent'] = None
            checkpoint['sequence'] += 1
            self._checkpoint(handle.job_id, checkpoint, uncertain=True)
            return result

        try:
            if job_type == 'station_visit':
                effect('station', params['station'])
            else:
                self._dip(params, effect, records, alarm)
            if handle.requested.is_set():
                raise Stopped
            hold = self.devices.hold()
            with self.guard:
                self._expire()
                outcome = 'held' if loss.is_set() or excursion else 'succeeded'
            self._checkpoint(handle.job_id, checkpoint)
            return dict(outcome=outcome, hold=hold, steps=records, alarm=checkpoint.get('alarm'),
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
                    self.devices.disarm()

    def _dip(self, params, effect, records, alarm):
        source, gripper, basket = params['source'], params['expected_basket'], params['basket']
        steps = params['steps']
        self.devices.check_handoff(steps[0], source, gripper, basket)
        for index, step in enumerate(steps):
            placed = effect('place', dict(step, source=source, gripper=gripper, basket=basket))
            if (type(placed.get('cycle')) is not int or placed['cycle'] < 1
                    or placed.get('done') is not False or placed.get('basket_present') is not True):
                raise ValueError('PLC has no fresh placement and timer-start evidence.')
            prior = params.get('previous_lift') if not records else {
                'lift_s': records[-1]['lift_s'], 'air_limit': records[-1]['limits']['air'],
            }
            incoming_air_s = None
            inspection = False
            if prior is not None:
                incoming_air_s = placed['clock_s'] - prior['lift_s']
                if incoming_air_s < 0:
                    raise ValueError('Lift and immersion times are out of order.')
                if records:
                    records[-1]['air_s'] = incoming_air_s
                if prior['air_limit'] is not None and incoming_air_s > prior['air_limit']:
                    if records:
                        records[-1]['inspection'] = True
                    else:
                        inspection = True
                    alarm()

            done = effect('dwell', step)
            if (type(done.get('cycle')) is not int or done['cycle'] != placed['cycle']
                    or done.get('done') is not True or done.get('basket_present') is not True
                    or done.get('equipment_on') is not False or not finite(done.get('sample_s'))
                    or not finite(done.get('elapsed_s')) or done['elapsed_s'] < 0
                    or done['sample_s'] < placed['start_s']):
                raise ValueError('PLC completion is unqualified for lift: verify this cycle, basket and equipment off.')
            limits = step['limits']
            rinsing = index + 1 < len(steps)
            lifted = effect('lift', 'transfer' if rinsing else params['hold'])
            post_done = lifted['clock_s'] - done['sample_s']
            dwell = done['elapsed_s'] + post_done
            overstay = post_done if step['role'] == 'anodize' else dwell - step['setpoint']
            if ((limits['min'] is not None and dwell < limits['min']) or
                    (limits['overstay'] is not None and overstay > limits['overstay'])):
                inspection = True
                alarm()
            records.append(dict(id=step['id'], tank=step['tank'], dwell_s=dwell, placed_s=placed['clock_s'],
                                limits=limits, air_s=None, lift_s=lifted['clock_s'],
                                incoming_air_s=incoming_air_s, plc=done, inspection=inspection))
            if rinsing:
                source, gripper = 'transfer', 'held'
                if steps[index+1]['station'] != step['station']:
                    effect('station', steps[index+1]['station'])

    def acknowledge(self, envelope):
        with self.guard:
            self._lease(envelope)
        with self.owner.lock:
            job = self.owner.jobs.get(envelope.get('job_id'))
            if not job or job['state'] in ('waiting', 'running'):
                raise ValueError('No terminal result to acknowledge.')
            job['executor_ack'] = True
            self.owner._save(job)
        return {'acknowledged': True}

    def reconcile(self, envelope):
        with self.admission:
            with self.guard:
                self._lease(envelope)
                ident, disposition = envelope.get('job_id'), envelope.get('disposition')
                missing = envelope.get('missing') is True
                observed_only = envelope.get('observed_only') is True
                if (not self.devices.simulated or self.active or self.owner.active
                        or not isinstance(ident, str) or not ID.fullmatch(ident)
                        or (missing and observed_only)
                        or not isinstance(disposition, str) or not disposition.strip() or len(disposition) > 1024):
                    raise ValueError('Verify physical evidence and record a reconciliation disposition.')
                alarm_job = self.quality_alarm and self.quality_alarm['job_id'] == ident
            with self.owner.lock:
                job = self.owner.jobs.get(ident)
                evidence_key = 'recovery_observation' if observed_only else 'reconciliation'
                if (job and job.get(evidence_key) and job[evidence_key]['disposition'] == disposition.strip()
                        and bool(job.get('missing_result')) == missing):
                    return self._public_job(ident)
                eligible = ((missing and job is None)
                            or (not missing and job and (job.get('execution_uncertain') or alarm_job
                                or (observed_only and job['state'] == 'done'
                                    and (job.get('result') or {}).get('hold')))))
                if not eligible:
                    raise ValueError('Select the uncertain job to reconcile.')
            with self.guard:
                self._lease(envelope)
                self.armed = False
                self.generation += 1
                self.devices.disarm()
            with self.owner.lock:
                hold = self.devices.hold()
                resolution = dict(disposition=disposition.strip(), hold=hold, observed_utc=time.time())
                if observed_only:
                    job = dict(self.owner.jobs[ident], recovery_observation=resolution)
                    self.owner._save(job)
                    self.owner.jobs[ident] = job
                    return dict(self._public_job(ident), observation=resolution)
                if missing:
                    if ident in self.damaged:
                        # Retain the damaged file before replacing its API record.
                        original = self.owner.directory / f'{ident}.json'
                        quarantine = self.owner.directory / f'{ident}.unreadable'
                        if not quarantine.exists() and not original.is_symlink():
                            with quarantine.open('xb') as stream:
                                stream.write(original.read_bytes())
                    job = dict(id=ident, module='executor', state='interrupted', fingerprint=None,
                               missing_result=True, result=dict(outcome='unknown', reconciliation=resolution))
                job = dict(self.owner.jobs.get(ident, job), execution_uncertain=False, reconciliation=resolution)
                self.owner._save(job)
                self.owner.jobs[ident] = job
                self.owner.execution_uncertain = any(j.get('execution_uncertain') for j in self.owner.jobs.values())
            with self.guard:
                self.uncertain.discard(ident)
                self.damaged.discard(ident)
                if alarm_job:
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
