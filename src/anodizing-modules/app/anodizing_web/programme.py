"""Katana owns the recipe, one basket's dispatch cursor and its durable record."""
from copy import deepcopy
import json
from pathlib import Path
from threading import Event, Lock, RLock, Thread
import time
from uuid import uuid4

from .executor import HEARTBEAT_S, PAIRED_ROLES, PROTOCOL, ROLES, finite
from .jobs import atomic_json


DEFAULTS = {
    'ultrasonic': (0, 60, 60), 'rinse': (0, None, None), 'etch': (5, 5, 15),
    'desmut': (10, 30, 30), 'anodize': (None, 60, 30), 'seal': (0, None, None),
}
TERMINAL = {'succeeded', 'stopped', 'inspection', 'recovery required', 'failed'}


def draft(steps):
    seen, result = set(), []
    if not isinstance(steps, list) or not 1 <= len(steps) <= 60:
        raise ValueError('Provide up to 60 ordered recipe steps.')
    for index, step in enumerate(steps):
        if not isinstance(step, dict):
            raise ValueError('Each recipe step needs an identity, tank, role and setpoint.')
        ident, tank, role = (step.get(key) for key in ('id', 'tank', 'role'))
        setpoint, station = step.get('setpoint'), step.get('station')
        if (not all(isinstance(v, str) and 0 < len(v) <= 80 for v in (ident, tank, role))
                or ident in seen or role not in ROLES or not finite(setpoint) or setpoint <= 0
                or type(station) is not int or not 1 <= station <= 255):
            raise ValueError('Resolve recipe identity, stations, and active setpoints before review.')
        if role in PAIRED_ROLES and (index+1 == len(steps) or not isinstance(steps[index+1], dict)
                                     or steps[index+1].get('role') != 'rinse'):
            raise ValueError('Every time-critical process needs its immediate rinse.')
        seen.add(ident)
        early, overstay, air = DEFAULTS[role]
        minimum = setpoint - early if early is not None and early < setpoint else None
        result.append(dict(id=ident, tank=tank, role=role, station=station, setpoint=setpoint,
                           limits=dict(min=minimum, overstay=overstay, air=air),
                           terminal=index+1 == len(steps)))
    return dict(id=uuid4().hex, state='DRAFT', steps=result)


def effective(revision):
    revision = deepcopy(revision)
    steps = revision['steps']
    for index in range(len(steps)-2, -1, -1):
        step = steps[index]
        if step['role'] == 'rinse' and step['limits']['air'] is None:
            step['limits']['air'] = steps[index+1]['limits']['air']
            step['air_source'] = steps[index+1]['id']
    return revision


class Programme:
    def __init__(self, root, request, steps, *, pulse=None):
        root = Path(root)
        root.mkdir(parents=True, exist_ok=True)
        self.path = root / 'programme.json'
        if self.path.is_symlink():
            raise ValueError('Programme record must be a regular file.')
        self.request, self.shared_pulse = request, pulse
        self.lock, self.tick_lock, self.lease_lock = RLock(), Lock(), Lock()
        self.stop_lock, self.stops = Lock(), {}
        self.close_requested = Event()
        self.worker = None
        self.session, self.sequence = uuid4().hex, 0
        self.identity, self.next_pulse = None, 0
        if self.path.exists():
            self.data = json.loads(self.path.read_text())
            current = self.data['revisions'][self.data['recipe']]
            if current['state'] == 'FINAL' and not current.get('review'):
                revision = dict(current, id=uuid4().hex, state='DRAFT')
                self.data['recipe'] = revision['id']
                self.data['revisions'][revision['id']] = revision
                self._save()
            if self.run:
                if self.run['state'] in ('running', 'Stop unconfirmed'):
                    self.run.update(state='held',
                                    reason='Controller restarted. Review outstanding evidence and explicitly resume.')
                    self._save()
                if self.run['stop_pending'] and self.run.get('stop_id'):
                    self.stops[self.run['id']] = self.run['stop_id']
        else:
            revision = draft(steps)
            self.data = dict(recipe=revision['id'], revisions={revision['id']: revision}, run=None, runs={})
            self._save()

    @property
    def run(self):
        return self.data['runs'].get(self.data['run'])

    def _save(self):
        atomic_json(self.path, self.data, sync_directory=True)

    def status(self):
        with self.lock:
            return dict(recipe=effective(self.data['revisions'][self.data['recipe']]), run=deepcopy(self.run),
                        baskets=[{key: run[key] for key in ('id', 'basket', 'state')}
                                 for run in self.data['runs'].values()])

    def save(self, ident, changes):
        with self.lock:
            if ident != self.data['recipe'] or not isinstance(changes, dict):
                raise ValueError('Reload the current recipe revision before editing.')
            revision = deepcopy(self.data['revisions'][ident])
            steps = {step['id']: step for step in revision['steps']}
            for step_id, limits in changes.items():
                if (step_id not in steps or not isinstance(limits, dict) or not limits
                        or not set(limits) <= {'min', 'overstay', 'air'}):
                    raise ValueError('Supply the edited limits for a known step.')
                step = steps[step_id]
                for key, val in limits.items():
                    if val is None:
                        continue
                    if not finite(val) or not ((0 < val <= step['setpoint']) if key == 'min' else
                                                val > 0 if key == 'air' else val >= 0):
                        raise ValueError('Use finite bounds, a positive minimum up to the setpoint and positive air time.')
                step['limits'].update(limits)
            revision.pop('review', None)
            revision.update(id=uuid4().hex, state='DRAFT')
            self.data['recipe'] = revision['id']
            self.data['revisions'][revision['id']] = revision
            self._save()
            return effective(revision)

    def finalize(self, ident, review=None):
        with self.lock:
            if ident != self.data['recipe']:
                raise ValueError('Review the current recipe revision before finalizing.')
            revision = effective(self.data['revisions'][ident])
            if revision['state'] == 'FINAL':
                raise ValueError('Edit the reviewed limits to create a new draft.')
            for step in revision['steps']:
                limits = step['limits']
                if limits['min'] is None or limits['overstay'] is None or (
                    not step['terminal'] and limits['air'] is None
                ):
                    raise ValueError('Resolve every applicable bound before finalizing.')
            keys = ('terminal_handling', 'timing_evidence')
            if (not isinstance(review, dict) or set(review) != set(keys)
                    or any(not isinstance(review[key], str) or not 0 < len(review[key].strip()) <= 500
                           for key in keys)):
                raise ValueError('Record reviewed terminal handling and timing evidence before finalizing.')
            evidence = {key: review[key].strip() for key in keys}
            evidence['overstay_basis'] = {step['id']: 'plc_done' if step['role'] == 'anodize' else 'setpoint'
                                          for step in revision['steps']}
            self.data['revisions'][ident].update(state='FINAL', review=evidence)
            self._save()
            return effective(self.data['revisions'][ident])

    def start(self, ident, basket):
        if not isinstance(basket, str) or not basket.strip() or len(basket) > 80:
            raise ValueError('Identify the basket before starting.')
        reviewed = self.request('status')
        with self.lock:
            if ident != self.data['recipe']:
                raise ValueError('Review the current recipe revision before starting.')
            if self.run and self.run['state'] in ('recovery required', 'inspection'):
                raise ValueError('Record a disposition before starting another basket.')
            if self.run and (self.run['pending'] or self.run['ack_pending'] or self._stopping(self.run)):
                raise ValueError('Resolve the outstanding executor evidence before starting another basket.')
            if self.run and self.run['state'] not in TERMINAL:
                raise ValueError('Finish or stop the current basket first.')
            revision = effective(self.data['revisions'][ident])
            run = dict(id=uuid4().hex, basket=basket.strip(), recipe=revision, state='running', cursor=0,
                       jobs=[], steps=[], pending=None, pending_missing=False, ack_pending=None, stop_pending=False,
                       step_kind='station_visit', setup=True, reason=None, boot=reviewed['boot'],
                       approval=reviewed['stop_generation'],
                       final_hold=None, created_utc=time.time())
            self.data['run'] = run['id']
            self.data['runs'][run['id']] = run
            self._save()
            return deepcopy(run)

    def pulse(self):
        with self.lease_lock:
            now = time.monotonic()
            if self.identity is not None and now < self.next_pulse:
                return self.identity
            meta = self.request('health')
            if meta['protocol'] != PROTOCOL or not meta.get('simulated') or not meta['motion_enabled'] or any(
                meta['job_types'].get(job_type) != 1 for job_type in ('station_visit', 'dip')
            ):
                raise ValueError('The configured simulation executor is incompatible.')
            if self.shared_pulse:
                identity = self.shared_pulse()
            else:
                identity = dict(session=self.session, boot=meta['boot'], protocol=PROTOCOL)
                self.sequence += 1
                heartbeat = self.request('heartbeat', dict(identity, sequence=self.sequence))
                if not heartbeat['lease_valid']:
                    raise ValueError('The controller lease is unavailable.')
            if identity['boot'] != meta['boot']:
                raise ValueError('The executor boot changed while establishing the lease.')
            self.identity, self.next_pulse = identity, now + HEARTBEAT_S
            return identity

    def _stopping(self, run):
        with self.stop_lock:
            return run['stop_pending'] or run['id'] in self.stops

    def _hold(self, state, reason):
        with self.lock:
            run = self.run
            if (run and run['state'] not in TERMINAL
                    and (run['state'], run.get('reason')) != (state, reason)
                    and (not self._stopping(run) or state == 'recovery required')):
                run.update(state=state, reason=reason)
                self._save()

    def _settle_stop(self, run):
        if (run.get('stop', {}).get('confirmed') and not run['pending']
                and run['state'] in ('running', 'held')):
            run.update(state='stopped', reason='Stop confirmed.')

    def _deliver_stop(self, ident, stop_id, job_id):
        try:
            return self.request('stop', dict(stop_id=stop_id, run_id=ident, job_id=job_id))
        except (OSError, ValueError):
            return None

    def _record_stop(self, ident, stop_id, report):
        with self.lock:
            run = self.data['runs'][ident]
            # A newly received Stop has priority over the result of an older delivery.
            with self.stop_lock:
                if self.stops.get(ident) != stop_id:
                    return deepcopy(run)
                confirmed = bool(report and report['received'] and report['confirmed'])
                if confirmed:
                    self.stops.pop(ident)
            if report is None and run.get('stop_id') == stop_id:
                return deepcopy(run)
            if (run.get('stop_id'), run.get('stop'), run['stop_pending']) != (stop_id, report, not confirmed):
                run.update(stop_pending=not confirmed, stop_id=stop_id, stop=report or {})
                self._settle_stop(run)
                self._save()
            return deepcopy(run)

    def stop(self):
        run = self.run
        if run is None:
            return self.request('stop', {})
        ident = run['id']
        with self.stop_lock:
            stop_id = self.stops.setdefault(ident, uuid4().hex)
        pending = run['pending']
        report = self._deliver_stop(ident, stop_id, pending['job_id'] if pending else None)
        return self._record_stop(ident, stop_id, report)

    def resume(self, ident):
        reviewed = self.request('status')
        with self.lock:
            run = self.run
            if not run or run['id'] != ident or run['state'] != 'held' or self._stopping(run):
                raise ValueError('Review a held basket before resuming.')
            if run['boot'] != reviewed['boot']:
                raise ValueError('Wait for recovery review after the executor reboot.')
            if run['pending'] and not (run.get('pending_missing') and run.get('pending_attempted') is False):
                raise ValueError('Wait for the outstanding job evidence before resuming.')
            run.update(state='running', setup=True, reason=None, approval=reviewed['stop_generation'])
            if run['pending']:
                run['pending']['stop_generation'] = run['approval']
            self._save()
            return deepcopy(run)

    def disposition(self, ident, note):
        if not isinstance(note, str) or not note.strip() or len(note) > 500:
            raise ValueError('Record a disposition for the basket.')
        note = note.strip()
        with self.tick_lock:
            with self.lock:
                run = self.run
                if (not run or run['id'] != ident
                        or (run['state'] not in ('inspection', 'recovery required') and not run.get('ack_missing'))):
                    raise ValueError('Select a basket awaiting inspection or recovery.')
                intent = run.get('disposition_intent')
                if intent:
                    if intent['disposition'] != note:
                        raise ValueError('Retry the recorded disposition for this executor job.')
                else:
                    pending = run['pending']
                    missing = bool(pending and run.get('pending_missing'))
                    if pending and not missing:
                        raise ValueError('Wait for the outstanding executor evidence before recording recovery.')
                    job_id = pending['job_id'] if missing else run.get('recovery_job')
                    if run.get('ack_missing'):
                        job_id, missing = run['ack_pending'], True
                    observed_only = False
                    if job_id is None and run['state'] == 'recovery required' and run['jobs']:
                        job_id = next((entry['job']['job_id'] for entry in reversed(run['jobs'])
                                       if entry['job']['outcome'] == 'unknown'), None)
                        if job_id is None:
                            job_id = run['jobs'][-1]['job']['job_id']
                            observed_only = True
                        missing = self.request('status', {'job_id': job_id})['job'] is None
                    if job_id:
                        intent = dict(job_id=job_id, disposition=note, missing=missing)
                        if observed_only and not missing:
                            intent['observed_only'] = True
                        run['disposition_intent'] = intent
                        self._save()
            if intent:
                identity = self.pulse()
                try:
                    job = self.request('reconcile', dict(identity, **intent))
                except ValueError:
                    with self.lock:
                        run['disposition_intent'] = None
                        self._save()
                    raise
                if intent['missing']:
                    if job.get('job_id') != intent['job_id'] or job['outcome'] != 'unknown':
                        raise ValueError('The executor has not returned a durable missing-result disposition.')
                    self._merge(job)
            with self.lock:
                if intent:
                    hold = (job.get('result', {}).get('reconciliation') or job.get('reconciliation') or {}).get('hold')
                    if hold:
                        run['final_hold'] = hold['location']
                    if run.get('ack_missing'):
                        run.update(ack_missing=False, reason=None)
                if run['state'] != 'succeeded':
                    run['state'] = 'stopped'
                run.update(disposition=note, disposition_intent=None)
                self._save()
                return deepcopy(run)

    def _send_pending(self, payload, identity):
        with self.lock:
            run = self.run
            if (run['state'] != 'running' or self._stopping(run) or run['pending'] != payload
                    or run.get('pending_attempted') is not False):
                return None
            # A crash from this point onward may have delivered the request.
            run['pending_attempted'] = True
            self._save()
        return self.request('submit', dict(identity, **payload))

    def _dispatch(self, run, identity):
        if run['pending'] is not None:
            payload = run['pending']
            job = self.request('status', {'job_id': payload['job_id']})['job']
            if job is not None:
                return job
            with self.lock:
                current = self.run
                if current['id'] != run['id'] or current['pending'] != payload:
                    return None
                attempted = current.get('pending_attempted') is not False
                if (attempted or identity['boot'] != run['boot']) and current['state'] not in TERMINAL:
                    current.update(state='recovery required',
                                   reason='The original job result is missing. Verify the retained hold and record a disposition.')
                if not current.get('pending_missing'):
                    current['pending_missing'] = True
                    self._save()
            if not attempted and identity['boot'] == run['boot']:
                return self._send_pending(payload, identity)
            return None
        if run['state'] != 'running' or self._stopping(run):
            return None
        steps = run['recipe']['steps']
        step = steps[run['cursor']]
        kind = run['step_kind']
        if kind == 'station_visit':
            params = {'station': step['station']}
        else:
            pair = step['role'] in PAIRED_ROLES
            grouped = steps[run['cursor']:run['cursor']+2] if pair else [step]
            params = dict(steps=grouped, source=run['final_hold'] or 'input',
                          hold='output' if grouped[-1]['terminal'] else 'park',
                          expected_basket='released', run_id=run['id'], basket=run['basket'],
                          recipe_revision=run['recipe']['id'])
            prior = run['steps'][-1] if run['steps'] else None
            if (prior and prior.get('boot') == identity['boot'] and prior['air_s'] is None
                    and prior['limits']['air'] is not None):
                params['previous_lift'] = dict(lift_s=prior['lift_s'], air_limit=prior['limits']['air'])
        payload = dict(job_id=uuid4().hex, type=kind, version=1, params=params,
                       stop_generation=run['approval'])
        with self.lock:
            if self.run['state'] != 'running' or self.run['pending'] or self._stopping(self.run):
                return None
            self.run.update(pending=payload, pending_attempted=False, pending_missing=False, checkpoint=None)
            self._save()
        return self._send_pending(payload, identity)

    def _merge(self, job):
        if job is None:
            return
        with self.lock:
            run = self.run
            payload = run['pending']
            if not payload or payload['job_id'] != job['job_id']:
                return
            checkpoint = job.get('checkpoint')
            updated = bool(checkpoint and checkpoint != run.get('checkpoint'))
            if updated:
                run['checkpoint'] = checkpoint
            if job['state'] in ('accepted', 'running', 'waiting'):
                if updated:
                    self._save()
                return
            run['jobs'].append(dict(type=payload['type'], job=job))
            run.update(pending=None, pending_missing=False, ack_pending=job['job_id'])
            result = job['result']
            records = deepcopy(result.get('steps', []))
            excursion = any(record['inspection'] for record in records)
            boot = (run.get('checkpoint') or {}).get('boot')
            for record in records:
                record['boot'] = boot
            if (records and run['steps'] and run['steps'][-1]['air_s'] is None
                    and boot and run['steps'][-1].get('boot') == boot):
                previous = run['steps'][-1]
                previous['air_s'] = records[0]['placed_s'] - previous['lift_s']
                bound = previous['limits']['air']
                if bound is not None and previous['air_s'] > bound:
                    previous['inspection'] = excursion = True
            run['steps'].extend(records)
            hold = result.get('hold') or (result.get('reconciliation') or {}).get('hold')
            if hold:
                run['final_hold'] = hold['location']
            if payload['type'] == 'dip' and records:
                run['cursor'] += len(records)
                run['step_kind'] = 'station_visit'
            elif job['outcome'] in ('succeeded', 'held') and payload['type'] == 'station_visit':
                run['step_kind'] = 'dip'
            if job['outcome'] == 'unknown':
                run.update(state='recovery required', recovery_job=job['job_id'],
                           reason=result.get('reason') or job['error'])
            elif excursion:
                run.update(state='inspection', recovery_job=job['job_id'],
                           reason='Quality limit exceeded. Review the basket at its hold.')
            elif run['state'] not in TERMINAL:
                if job['outcome'] != 'succeeded':
                    run.update(state={'held': 'held', 'stopped': 'stopped'}.get(job['outcome'], 'failed'),
                               reason=result.get('reason') or job['error'])
                elif run['cursor'] == len(run['recipe']['steps']):
                    run.update(state='succeeded', finished_utc=time.time())
            self._settle_stop(run)
            self._save()

    def tick(self):
        with self.tick_lock:
            with self.lock:
                run = deepcopy(self.run)
            if run is None:
                return
            if run['stop_pending']:
                stop_id = run.get('stop_id')
                if stop_id:
                    pending = run['pending']
                    report = self._deliver_stop(run['id'], stop_id, pending['job_id'] if pending else None)
                    if report:
                        self._record_stop(run['id'], stop_id, report)
                else:
                    self.stop()
            if run['state'] in TERMINAL and not run['ack_pending'] and not run['pending']:
                return
            try:
                identity = self.pulse()
                status = self.request('status')
                if run['boot'] != status['boot']:
                    self._hold('recovery required', 'Executor rebooted. Reconcile the last confirmed location and intent.')
                    if identity['boot'] != status['boot']:
                        self.next_pulse = 0
                        identity = self.pulse()
                if run['ack_pending']:
                    if run.get('ack_missing'):
                        return
                    if run['boot'] != status['boot']:
                        evidence = self.request('status', {'job_id': run['ack_pending']})
                        if evidence['job'] is None:
                            with self.lock:
                                current = self.data['runs'][run['id']]
                                current.update(ack_missing=True,
                                               reason='IPC lost the terminal acknowledgement. Verify the retained hold.')
                                self._save()
                            return
                    self.request('acknowledge', dict(identity, job_id=run['ack_pending']))
                    with self.lock:
                        current = self.data['runs'][run['id']]
                        current['ack_pending'] = None
                        self._save()
                with self.lock:
                    run = deepcopy(self.run)
                    if (run['state'] == 'running' and not run['pending'] and not self._stopping(run)
                            and run['cursor'] >= len(run['recipe']['steps'])):
                        if run['final_hold'] == 'output' and run['cursor'] == len(run['recipe']['steps']):
                            self.run.update(state='succeeded', finished_utc=time.time())
                        else:
                            self.run.update(state='recovery required', reason='The final output hold is unverified.')
                        self._save()
                        return
                if run['setup'] and run['state'] == 'running' and not self._stopping(run):
                    if not status['device']['connected']:
                        self.request('connect', identity)
                    self.request('arm', dict(identity, stop_generation=run['approval']))
                    with self.lock:
                        if self.run['state'] != 'running' or self._stopping(self.run):
                            return
                        self.run['setup'] = False
                        self._save()
                elif run['state'] == 'running' and not status['armed'] and not run['pending']:
                    self._hold('held', 'Executor disarmed. Review the hold before resuming.')
                with self.lock:
                    run = deepcopy(self.run)
                self._merge(self._dispatch(run, identity))
            except OSError:
                self._hold('held', 'Executor unavailable. Review outstanding status on reconnect.')
            except ValueError as exc:
                self._hold('held', str(exc))

    def start_service(self):
        def loop():
            while not self.close_requested.wait(.15):
                self.tick()
        self.worker = Thread(target=loop, daemon=True, name='recipe-dispatch')
        self.worker.start()

    def close(self):
        self.close_requested.set()
        if self.worker:
            self.worker.join()
