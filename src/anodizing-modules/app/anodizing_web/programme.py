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
    def __init__(self, root, request, steps):
        root = Path(root)
        root.mkdir(parents=True, exist_ok=True)
        self.path = root / 'programme.json'
        if self.path.is_symlink():
            raise ValueError('Programme record must be a regular file.')
        self.request = request
        self.lock, self.tick_lock, self.lease_lock = RLock(), Lock(), Lock()
        self.close_requested = Event()
        self.worker = None
        self.session, self.sequence = uuid4().hex, 0
        self.identity, self.next_pulse = None, 0
        if self.path.exists():
            self.data = json.loads(self.path.read_text())
            if self.run and self.run['state'] == 'running':
                self.run['state'] = 'held'
                self.run['reason'] = 'Controller restarted. Review the outstanding job and explicitly resume.'
                self._save()
        else:
            revision = draft(steps)
            self.data = dict(recipe=revision['id'], revisions={revision['id']: revision}, run=None, runs={})
            self._save()

    @property
    def run(self):
        return self.data['runs'].get(self.data['run'])

    def _save(self):
        atomic_json(self.path, self.data)

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
                if step_id not in steps or not isinstance(limits, dict) or set(limits) != {'min', 'overstay', 'air'}:
                    raise ValueError('Supply all three limits for a known step.')
                step = steps[step_id]
                for key, val in limits.items():
                    if val is None:
                        continue
                    if not finite(val) or not ((0 < val <= step['setpoint']) if key == 'min' else
                                                val > 0 if key == 'air' else val >= 0):
                        raise ValueError('Use finite bounds, a positive minimum up to the setpoint and positive air time.')
                step['limits'] = dict(limits)
            revision.update(id=uuid4().hex, state='DRAFT')
            self.data['recipe'] = revision['id']
            self.data['revisions'][revision['id']] = revision
            self._save()
            return effective(revision)

    def finalize(self, ident):
        with self.lock:
            if ident != self.data['recipe']:
                raise ValueError('Review the current recipe revision before finalizing.')
            revision = effective(self.data['revisions'][ident])
            for step in revision['steps']:
                limits = step['limits']
                if limits['min'] is None or limits['overstay'] is None or (
                    not step['terminal'] and limits['air'] is None
                ):
                    raise ValueError('Resolve every applicable bound before finalizing.')
            self.data['revisions'][ident]['state'] = 'FINAL'
            self._save()
            return effective(self.data['revisions'][ident])

    def start(self, ident, basket):
        if not isinstance(basket, str) or not basket.strip() or len(basket) > 80:
            raise ValueError('Identify the basket before starting.')
        with self.lock:
            if ident != self.data['recipe']:
                raise ValueError('Review the current recipe revision before starting.')
            if self.run and self.run['state'] not in TERMINAL:
                raise ValueError('Finish or stop the current basket first.')
            if self.run and self.run['state'] in ('recovery required', 'inspection'):
                raise ValueError('Record a disposition before starting another basket.')
            revision = effective(self.data['revisions'][ident])
            run = dict(id=uuid4().hex, basket=basket.strip(), recipe=revision, state='running', cursor=0,
                       jobs=[], steps=[], pending=None, pending_missing=False, ack_pending=None, stop_pending=False,
                       step_kind='station_visit', setup=True, reason=None, boot=None, approval=None,
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
            if meta['protocol'] != PROTOCOL or not meta['motion_enabled'] or any(
                meta['job_types'].get(job_type) != 1 for job_type in ('station_visit', 'dip')
            ):
                raise ValueError('The configured simulation executor is incompatible.')
            identity = dict(session=self.session, boot=meta['boot'], protocol=PROTOCOL)
            self.sequence += 1
            self.request('heartbeat', dict(identity, sequence=self.sequence))
            self.identity, self.next_pulse = identity, now + HEARTBEAT_S
            return identity

    def _hold(self, state, reason):
        with self.lock:
            if self.run and self.run['state'] not in TERMINAL and not self.run['stop_pending']:
                self.run.update(state=state, reason=reason)
                self._save()

    def _do_stop(self):
        try:
            report = self.request('stop', {})
        except (OSError, ValueError):
            return
        with self.lock:
            if report['received']:
                self.run.update(stop_pending=not report['confirmed'],
                                state='stopped' if report['confirmed'] else 'Stop unconfirmed',
                                stop=report)
                self._save()

    def stop(self):
        with self.lock:
            if not self.run:
                return self.request('stop', {})
            self.run.update(stop_pending=True, state='Stop unconfirmed')
            self._save()
        self._do_stop()
        with self.lock:
            return deepcopy(self.run)

    def resume(self, ident):
        with self.lock:
            if not self.run or self.run['id'] != ident or self.run['state'] != 'held' or self.run['stop_pending']:
                raise ValueError('Review a held basket before resuming.')
            if self.run['pending'] and not self.run.get('pending_missing'):
                raise ValueError('Wait for the outstanding job evidence before resuming.')
            self.run.update(state='running', setup=True, reason=None)
            self._save()
            return deepcopy(self.run)

    def disposition(self, ident, note):
        if not isinstance(note, str) or not note.strip() or len(note) > 500:
            raise ValueError('Record a disposition for the basket.')
        with self.tick_lock, self.lock:
            if not self.run or self.run['id'] != ident or self.run['state'] not in ('inspection', 'recovery required'):
                raise ValueError('Select a basket awaiting inspection or recovery.')
            if self.run['pending']:
                raise ValueError('Wait for the outstanding executor evidence before recording recovery.')
            if self.run['state'] == 'recovery required':
                status = self.request('status')
                if status['recovery_required']:
                    job_id = next((entry['job']['job_id'] for entry in reversed(self.run['jobs'])
                                   if entry['job']['outcome'] == 'unknown'), None)
                    if not job_id:
                        raise ValueError('The uncertain executor job must be identified before recovery.')
                    self.request('reconcile', dict(self.pulse(), job_id=job_id, disposition=note.strip()))
            self.run.update(state='stopped', disposition=note.strip())
            self._save()
            return deepcopy(self.run)

    def _dispatch(self, run, identity):
        if run['pending'] is not None:
            payload = run['pending']
            job = self.request('status', {'job_id': payload['job_id']})['job']
            if job is None:
                if run['state'] == 'running':
                    return self.request('submit', dict(identity, **payload))
                with self.lock:
                    if self.run['pending'] == payload and not self.run.get('pending_missing'):
                        self.run['pending_missing'] = True
                        self._save()
            return job
        if run['state'] != 'running':
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
        payload = dict(job_id=uuid4().hex, type=kind, version=1, params=params,
                       stop_generation=run['approval'])
        with self.lock:
            if self.run['state'] != 'running' or self.run['pending']:
                return None
            self.run['pending'] = payload
            self._save()
        return self.request('submit', dict(identity, **payload))

    def _merge(self, job):
        if job is None or job['state'] in ('accepted', 'running', 'waiting'):
            return
        with self.lock:
            run = self.run
            payload = run['pending']
            if not payload or payload['job_id'] != job['job_id']:
                return
            run['jobs'].append(dict(type=payload['type'], job=job))
            run.update(pending=None, pending_missing=False, ack_pending=job['job_id'])
            result = job['result']
            records = deepcopy(result.get('steps', []))
            excursion = any(record['inspection'] for record in records)
            boot = (job.get('checkpoint') or {}).get('boot')
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
            if result.get('hold'):
                run['final_hold'] = result['hold']['location']
            if payload['type'] == 'dip' and records:
                run['cursor'] += len(records)
                run['step_kind'] = 'station_visit'
            elif job['outcome'] in ('succeeded', 'held') and payload['type'] == 'station_visit':
                run['step_kind'] = 'dip'
            if run['state'] not in TERMINAL | {'Stop unconfirmed'}:
                if excursion:
                    run.update(state='inspection', reason='Quality limit exceeded. Review the basket at its hold.')
                elif job['outcome'] != 'succeeded':
                    run.update(state={'unknown': 'recovery required', 'held': 'held',
                                      'stopped': 'stopped'}.get(job['outcome'], 'failed'),
                               reason=result.get('reason') or job['error'])
                elif run['cursor'] == len(run['recipe']['steps']):
                    run.update(state='succeeded', finished_utc=time.time())
            self._save()

    def tick(self):
        with self.tick_lock:
            with self.lock:
                run = deepcopy(self.run)
            if run is None:
                return
            if run['stop_pending']:
                self._do_stop()
                return
            if run['state'] in TERMINAL and not run['ack_pending'] and not run['pending']:
                return
            try:
                identity = self.pulse()
                status = self.request('status')
                if run['boot'] and run['boot'] != status['boot']:
                    self._hold('recovery required', 'Executor rebooted. Reconcile the last confirmed location and intent.')
                if run['ack_pending']:
                    self.request('acknowledge', dict(identity, job_id=run['ack_pending']))
                    with self.lock:
                        self.run['ack_pending'] = None
                        self._save()
                if run['setup'] and self.run['state'] == 'running':
                    if not status['device']['connected']:
                        self.request('connect', identity)
                    status = self.request('arm', identity)
                    with self.lock:
                        if self.run['state'] != 'running':
                            return
                        self.run.update(setup=False, boot=status['boot'], approval=status['stop_generation'])
                        if self.run.get('pending_missing'):
                            self.run['pending']['stop_generation'] = status['stop_generation']
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
