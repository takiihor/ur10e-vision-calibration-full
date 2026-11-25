"""Katana owns the recipe, one basket's dispatch cursor and its durable record."""
from copy import deepcopy
import json
from pathlib import Path
from threading import Event, Lock, RLock, Thread
import time
from uuid import uuid4

from .executor import AdmissionRefused, PAIRED_ROLES, PROTOCOL, ROLES, finite, heartbeat_interval
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
    def __init__(self, root, request, steps, *, pulse=None, order_intent=None):
        root = Path(root)
        root.mkdir(parents=True, exist_ok=True)
        self.path = root / 'programme.json'
        if self.path.is_symlink():
            raise ValueError('Programme record must be a regular file.')
        self.request, self.shared_pulse, self.order_intent = request, pulse, order_intent
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

    def retained_job(self, job_id):
        if not isinstance(job_id, str):
            return False
        with self.lock:
            return any(r.get('recovery_job') == job_id or r.get('ack_pending') == job_id
                       or (r.get('pending') or {}).get('job_id') == job_id
                       or any(entry['job']['job_id'] == job_id for entry in r['jobs'])
                       for r in self.data['runs'].values())

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

    @staticmethod
    def compatible(meta):
        return (meta.get('protocol') == PROTOCOL and meta.get('simulated') is True
                and meta.get('motion_enabled') is True and
                all(meta.get('job_types', {}).get(kind) == 1 for kind in ('station_visit', 'dip')))

    def review(self):
        try:
            meta = self.request('health')
            if not self.compatible(meta):
                raise ValueError('The configured simulation executor is incompatible.')
            identity = self.shared_pulse() if self.shared_pulse else {'session': self.session, 'boot': meta['boot']}
            status = self.request('status')
            if status['boot'] != meta['boot'] or identity['boot'] != status['boot']:
                raise ValueError('The executor boot changed. Refresh before reviewing the simulation.')
            if self.shared_pulse and (not status['lease_valid'] or status['session'] != identity['session']):
                raise ValueError('The controller lease is unavailable.')
            if (status.get('stop') or {}).get('confirmed') is False:
                raise ValueError('Stop is unconfirmed. Verify a safe halt first.')
            approval = dict(boot=status['boot'], session=identity['session'],
                            stop_generation=status['stop_generation'])
            return dict(approval=approval, block=None)
        except (OSError, ValueError) as exc:
            return dict(approval=None, block=str(exc))

    def _check_approval(self, approval):
        reviewed = self.review()
        if reviewed['block']:
            raise ValueError(reviewed['block'])
        if (not isinstance(approval, dict) or type(approval.get('stop_generation')) is not int
                or reviewed['approval'] != approval):
            raise ValueError('Review the current executor boot, controller session and Stop generation.')

    def retained_run(self, run_id):
        with self.lock:
            return deepcopy(self.data['runs'].get(run_id))

    def request_run(self, request_id, action):
        with self.lock:
            return next((deepcopy(run) for run in self.data['runs'].values()
                         if request_id in run.get(f'{action}_requests', [])), None)

    def control_owned(self):
        with self.lock:
            run = self.run
            return bool(run and (run['state'] not in TERMINAL or run['state'] in ('recovery required', 'inspection')
                                 or run.get('failed_hold') or run['pending'] or run['ack_pending'] or self._stopping(run)))

    def latest_executor_intent(self):
        with self.lock:
            for run in reversed(list(self.data['runs'].values())):
                pending = run.get('pending')
                if pending and run.get('pending_attempted') is not False:
                    return dict(job_id=pending['job_id'], intent_utc_ns=run.get('pending_utc_ns', 0),
                                intent_order=run.get('pending_order', 0),
                                basket=run['basket'], run_id=run['id'])
                if run['jobs']:
                    entry = run['jobs'][-1]
                    job = entry['job']
                    when = entry.get('intent_utc_ns') or int((job.get('created') or run['created_utc']) * 1e9)
                    return dict(job_id=job['job_id'], intent_utc_ns=when, intent_order=entry.get('intent_order', 0),
                                basket=run['basket'], run_id=run['id'])
        return None

    def start(self, ident, basket, approval, *, request_id=None):
        if not isinstance(basket, str) or not basket.strip() or len(basket) > 80:
            raise ValueError('Identify the basket before starting.')
        self._check_approval(approval)
        with self.lock:
            if ident != self.data['recipe']:
                raise ValueError('Review the current recipe revision before starting.')
            if self.run and (self.run['state'] in ('recovery required', 'inspection') or self.run.get('failed_hold')):
                raise ValueError('Record a disposition before starting another basket.')
            if self.run and (self.run['pending'] or self.run['ack_pending'] or self._stopping(self.run)):
                raise ValueError('Resolve the outstanding executor evidence before starting another basket.')
            if self.run and self.run['state'] not in TERMINAL:
                raise ValueError('Finish or stop the current basket first.')
            revision = effective(self.data['revisions'][ident])
            run = dict(id=uuid4().hex, basket=basket.strip(), recipe=revision, state='running', cursor=0,
                       jobs=[], steps=[], pending=None, pending_missing=False, ack_pending=None, stop_pending=False,
                       step_kind='station_visit', setup=True, reason=None, boot=approval['boot'], approval_session=approval['session'],
                       approval=approval['stop_generation'],
                       final_hold=None, created_utc=time.time(),
                       start_requests=[request_id] if request_id else [])
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
            interval = heartbeat_interval(meta.get('heartbeat_s'), meta.get('lease_s'))
            if not self.compatible(meta):
                raise ValueError('The configured simulation executor is incompatible.')
            if self.shared_pulse:
                identity = self.shared_pulse()
            else:
                identity = dict(session=self.session, boot=meta['boot'], protocol=PROTOCOL)
                self.sequence += 1
                heartbeat = self.request('heartbeat', dict(identity, sequence=self.sequence))
                if heartbeat.get('boot') != identity['boot']:
                    raise OSError('The executor boot changed while establishing the lease.')
                if not heartbeat['lease_valid']:
                    raise ValueError('The controller lease is unavailable.')
            if identity['boot'] != meta['boot']:
                raise ValueError('The executor boot changed while establishing the lease.')
            self.identity, self.next_pulse = identity, now + interval
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
            last = run['jobs'][-1]['job'] if run['jobs'] else None
            if last and run['final_hold'] in ('input', 'park'):
                run.update(state='recovery required', recovery_job=last['job_id'], stopped_hold=True,
                           reason='Stop confirmed. Review the held basket and record a disposition.')
            else:
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

    def resume(self, ident, approval, *, request_id=None):
        self._check_approval(approval)
        with self.lock:
            run = self.run
            if not run or run['id'] != ident or run['state'] != 'held' or self._stopping(run):
                raise ValueError('Review a held basket before resuming.')
            if run['boot'] != approval['boot']:
                raise ValueError('Wait for recovery review after the executor reboot.')
            if run['pending'] and not (run.get('pending_missing') and run.get('pending_attempted') is False):
                raise ValueError('Wait for the outstanding job evidence before resuming.')
            run.update(state='running', setup=True, reason=None,
                       approval=approval['stop_generation'], approval_session=approval['session'])
            if run['pending']:
                run['pending']['stop_generation'] = run['approval']
            if request_id:
                run.setdefault('resume_requests', []).append(request_id)
            self._save()
            return deepcopy(run)

    def disposition(self, ident, note, *, allow_unaccounted=False, request_id=None):
        if not isinstance(note, str) or not note.strip() or len(note) > 500:
            raise ValueError('Record a disposition for the basket.')
        if type(allow_unaccounted) is not bool:
            raise ValueError('Choose whether to record the basket as unaccounted for.')
        note = note.strip()
        with self.tick_lock:
            with self.lock:
                run = self.run
                if (not run or run['id'] != ident
                        or (run['state'] not in ('inspection', 'recovery required')
                            and not (run.get('ack_missing') or run.get('failed_hold')))):
                    raise ValueError('Select a basket awaiting inspection or recovery.')
                intent = run.get('disposition_intent')
                if intent:
                    if intent['disposition'] != note or intent.get('allow_unaccounted', False) != allow_unaccounted:
                        raise ValueError('Retry the recorded disposition for this executor job.')
                else:
                    pending = run['pending']
                    missing = bool(pending and run.get('pending_missing'))
                    if pending and not (missing or run.get('invalid_result')):
                        raise ValueError('Wait for the outstanding executor evidence before recording recovery.')
                    job_id = pending['job_id'] if pending else run.get('recovery_job')
                    if run.get('ack_missing'):
                        job_id, missing = run['ack_pending'], True
                    observed_only = bool(run.get('failed_hold') or run.get('stopped_hold') or run.get('invalid_result'))
                    if job_id is None and run['state'] == 'recovery required' and run['jobs']:
                        job_id = next((entry['job']['job_id'] for entry in reversed(run['jobs'])
                                       if entry['job']['outcome'] == 'unknown'), None)
                        if job_id is None:
                            job_id = run['jobs'][-1]['job']['job_id']
                            observed_only = True
                    if job_id:
                        status = self.request('status', {'job_id': job_id})
                        missing = status['job'] is None
                        intent = dict(job_id=job_id, disposition=note, missing=missing, observation_id=uuid4().hex,
                                      basket=run['basket'], run_id=run['id'])
                        if allow_unaccounted:
                            intent['allow_unaccounted'] = True
                        if not allow_unaccounted:
                            intent['release_basket'] = True
                        if '.executor-active.json' in status.get('recovery_ids', []):
                            intent['repair_active_marker'] = True
                        if observed_only and not missing:
                            intent['observed_only'] = True
                        run['disposition_intent'] = intent
                        self._save()
            if intent:
                identity = self.pulse()
                try:
                    job = self.request('reconcile', dict(identity, **intent))
                except ValueError as exc:
                    with self.lock:
                        run['disposition_intent'] = None
                        if 'unaccounted' in str(exc).lower():
                            run.update(basket_unaccounted=True, final_hold=None, reason=str(exc))
                        self._save()
                    raise
                key = 'observation' if intent.get('observed_only') else 'reconciliation'
                evidence = job.get(key) if isinstance(job, dict) else None
                hold = evidence.get('hold') if isinstance(evidence, dict) else None
                unaccounted = isinstance(evidence, dict) and evidence.get('unaccounted') is True
                unload = evidence.get('unload') if isinstance(evidence, dict) else None
                if (not isinstance(hold, dict) or hold.get('location') not in ('input', 'park', 'output')
                        or hold.get('stopped') is not True or hold.get('equipment_on') is not False
                        or hold.get('gripper') != 'released' or 'basket' not in hold
                        or type(hold.get('station')) is not int
                        or job.get('job_id') != intent['job_id'] or evidence.get('boot') != identity['boot']
                        or evidence.get('observation_id') != intent.get('observation_id')
                        or evidence.get('disposition') != note
                        or not finite(evidence.get('observed_utc')) or evidence['observed_utc'] <= 0
                        or not finite(evidence.get('observed_mono_s')) or evidence['observed_mono_s'] < 0
                        or (intent.get('repair_active_marker') and evidence.get('repaired_active_marker') is not True)
                        or (intent['missing'] and job.get('outcome') != 'unknown')
                        or (unaccounted and not allow_unaccounted)
                        or (not unaccounted and (hold['basket'], hold.get('run_id')) != (run['basket'], run['id']))
                        or (intent.get('release_basket') and not unaccounted and hold['location'] in ('input', 'park')
                            and (not isinstance(unload, dict) or unload.get('location') != 'input'
                                 or unload.get('basket') is not None or unload.get('run_id') is not None
                                 or unload.get('stopped') is not True or unload.get('equipment_on') is not False
                                 or unload.get('gripper') != 'released'))):
                    raise OSError('Awaiting valid executor recovery evidence for the original job.')
                expected = self._recipe_steps(run, intent['job_id'])
                checkpoint = run.get('checkpoint') if run['pending'] else None
                recovered_rows, recovered_boot = self._confirmed_rows(job, expected, checkpoint)
                if intent['missing']:
                    self._merge(job)
            with self.lock:
                if intent:
                    self._merge_rows(run, recovered_rows, recovered_boot)
                    run['recovery_observation'] = dict(evidence, job_id=intent['job_id'])
                    run.update(final_hold=None if unaccounted else hold['location'], basket_unaccounted=unaccounted)
                    if intent['missing']:
                        run.update(ack_pending=intent['job_id'], ack_missing=False)
                    if run['state'] == 'succeeded':
                        run['reason'] = None
                    if run['pending'] and run.get('invalid_result'):
                        run['jobs'].append(dict(type=run['pending']['type'], job=job,
                                                invalid_terminal_evidence=True,
                                                intent_utc_ns=run.get('pending_utc_ns'),
                                                intent_order=run.get('pending_order', 0)))
                        run.update(pending=None, ack_pending=job['job_id'], invalid_result=None)
                if run['state'] != 'succeeded' or (intent and unaccounted):
                    run['state'] = 'stopped'
                run.update(disposition=note, disposition_intent=None, failed_hold=False)
                if request_id:
                    run.setdefault('disposition_requests', []).append(request_id)
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
            run['pending_order'] = self.order_intent() if self.order_intent else 0
            self._save()
        try:
            return self.request('submit', dict(identity, **payload))
        except AdmissionRefused as exc:
            with self.lock:
                if run['pending'] == payload:
                    last = run['jobs'][-1]['job'] if run['jobs'] else None
                    run.update(state='failed', reason=str(exc), pending=None, pending_missing=False,
                               failed_hold=last is not None, recovery_job=last and last['job_id'],
                               refusal=dict(job_id=payload['job_id'], reason=str(exc)))
                    self._save()
            return None

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
                if not attempted and identity['boot'] != run['boot']:
                    current.update(pending=None, pending_missing=False)
                    self._save()
                    return None
                if attempted and current['state'] not in TERMINAL:
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
            params = dict(station=step['station'], run_id=run['id'], basket=run['basket'],
                          load_input=run['cursor'] == 0)
        else:
            pair = step['role'] in PAIRED_ROLES
            grouped = steps[run['cursor']:run['cursor']+2] if pair else [step]
            params = dict(steps=grouped, source='input' if run['cursor'] == 0 else run['final_hold'],
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
            self.run.update(pending=payload, pending_attempted=False, pending_missing=False, checkpoint=None,
                            pending_utc_ns=time.time_ns())
            self._save()
        return self._send_pending(payload, identity)

    @staticmethod
    def _valid_rows(rows, expected):
        if not isinstance(rows, list) or len(rows) > len(expected):
            return False
        return all(isinstance(row, dict) and row.get('id') == step['id'] and row.get('tank') == step['tank']
                   and row.get('limits') == step['limits'] and type(row.get('inspection')) is bool
                   and all(finite(row.get(key)) for key in ('placed_s', 'lift_s', 'dwell_s'))
                   and 'air_s' in row and row['placed_s'] <= row['lift_s'] and row['dwell_s'] >= 0
                   and (row['air_s'] is None or (finite(row['air_s']) and row['air_s'] >= 0))
                   for row, step in zip(rows, expected))

    @staticmethod
    def _recipe_steps(run, job_id):
        pending = run['pending']
        if pending and pending['job_id'] == job_id:
            return pending['params'].get('steps', [])
        prior = set()
        for entry in run['jobs']:
            if entry['job']['job_id'] == job_id:
                if entry['type'] != 'dip':
                    return []
                steps = run['recipe']['steps']
                index = len(prior)
                if index < len(steps):
                    count = 2 if steps[index]['role'] in PAIRED_ROLES else 1
                    return steps[index:index+count]
                return []
            prior.update(row['id'] for row in (entry['job'].get('result') or {}).get('steps', []))
        return []

    def _confirmed_rows(self, job, expected, previous_checkpoint=None):
        result = job.get('result') or {}
        checkpoint = job.get('checkpoint') or {}
        candidates = [(result.get('steps', []), checkpoint.get('boot')),
                      (checkpoint.get('steps', []), checkpoint.get('boot'))]
        if previous_checkpoint:
            candidates.append((previous_checkpoint.get('steps', []), previous_checkpoint.get('boot')))
        return max(((rows, boot) for rows, boot in candidates if self._valid_rows(rows, expected)),
                   key=lambda candidate: len(candidate[0]), default=([], None))

    @staticmethod
    def _merge_rows(run, rows, boot):
        if not rows:
            return False
        confirmed = {row['id']: row for row in run['steps']}
        for record in rows:
            saved = confirmed.get(record['id'])
            if saved:
                saved['inspection'] |= record['inspection']
                if saved['air_s'] is None and saved.get('boot') == boot:
                    saved['air_s'] = record['air_s']
            else:
                confirmed[record['id']] = dict(deepcopy(record), boot=boot)
        run['steps'] = [confirmed[step['id']] for step in run['recipe']['steps'] if step['id'] in confirmed]
        run['cursor'] = len(run['steps'])
        run['step_kind'] = 'station_visit'
        excursion = any(row['inspection'] for row in rows)
        for previous, following in zip(run['steps'], run['steps'][1:]):
            if previous['air_s'] is not None or not previous.get('boot') or previous.get('boot') != following.get('boot'):
                continue
            measured = following['placed_s'] - previous['lift_s']
            if measured >= 0:
                previous['air_s'] = measured
                bound = previous['limits']['air']
                if previous['inspection'] or (bound is not None and measured > bound):
                    previous['inspection'] = excursion = True
        return excursion

    def _terminal_matches(self, payload, job, run):
        result = job.get('result')
        if not isinstance(result, dict):
            return False
        params = payload['params']
        rows = result.get('steps', [])
        expected = params.get('steps', [])
        if not self._valid_rows(rows, expected):
            return False
        if job.get('outcome') not in ('succeeded', 'held'):
            return job.get('outcome') in ('unknown', 'failed', 'stopped')
        hold = result.get('hold')
        if (not isinstance(hold, dict) or hold.get('gripper') != 'released'
                or hold.get('stopped') is not True or hold.get('equipment_on') is not False
                or (hold.get('basket'), hold.get('run_id')) != (run['basket'], run['id'])):
            return False
        if payload['type'] == 'dip':
            return len(rows) == len(expected) and hold.get('location') == params['hold']
        destination = 'input' if params.get('load_input', run['cursor'] == 0) else 'park'
        return hold.get('station') == params['station'] and hold.get('location') == destination

    def _refresh_air(self, run, alarm):
        if not alarm or not alarm.get('open_air') or not run['steps']:
            return
        entry = next((entry for entry in reversed(run['jobs']) if entry['job']['job_id'] == alarm.get('job_id')), None)
        if not entry:
            return
        job = self.request('status', {'job_id': alarm['job_id']})['job']
        row = next((row for row in ((job or {}).get('result') or {}).get('steps', [])
                    if isinstance(row, dict) and row.get('id') == alarm.get('step_id')
                    and row.get('inspection') is True
                    and (row.get('air_s') is None or (finite(row['air_s']) and row['air_s'] >= 0))), None)
        if not row:
            return
        with self.lock:
            current = self.data['runs'][run['id']]
            saved = next((step for step in current['steps'] if step['id'] == row['id']), None)
            if saved and (not saved['inspection'] or (saved['air_s'] is None and row.get('air_s') is not None)):
                saved.update(inspection=True, air_s=row.get('air_s', saved['air_s']))
                self._save()

    def _merge(self, job):
        if job is None:
            return
        with self.lock:
            run = self.run
            payload = run['pending']
            if not isinstance(job, dict) or not payload or payload['job_id'] != job.get('job_id'):
                return
            checkpoint = job.get('checkpoint')
            previous_checkpoint = run.get('checkpoint')
            updated = bool(isinstance(checkpoint, dict) and checkpoint != previous_checkpoint
                           and (not previous_checkpoint or checkpoint.get('sequence', -1) >=
                                previous_checkpoint.get('sequence', -1)))
            if updated:
                run['checkpoint'] = checkpoint
            if job.get('state') in ('accepted', 'running', 'waiting'):
                if updated:
                    self._save()
                return
            if (job.get('state') not in ('done', 'failed', 'interrupted', 'stopped')
                    or not self._terminal_matches(payload, job, run)):
                failures = (run.get('invalid_result') or 0) + 1
                run.update(invalid_result=min(failures, 2),
                           state='held' if failures == 1 and run['state'] not in TERMINAL else 'recovery required',
                           reason='The original executor result is incomplete or inconsistent. Verify its hold and evidence.')
                if failures <= 2:
                    self._save()
                return
            run.pop('invalid_result', None)
            previous_job = run['jobs'][-1]['job'] if run['jobs'] else None
            run['jobs'].append(dict(type=payload['type'], job=job, intent_utc_ns=run.get('pending_utc_ns'),
                                    intent_order=run.get('pending_order', 0)))
            run.update(pending=None, pending_missing=False, ack_pending=job['job_id'])
            result = job['result']
            rows, boot = self._confirmed_rows(job, payload['params'].get('steps', []), previous_checkpoint)
            excursion = self._merge_rows(run, rows, boot)
            hold = result.get('hold')
            if not hold:
                reconciliation = result.get('reconciliation') or {}
                if not reconciliation.get('unaccounted'):
                    hold = reconciliation.get('hold')
            if hold:
                run['final_hold'] = hold['location']
            if job['outcome'] in ('succeeded', 'held') and payload['type'] == 'station_visit':
                run['step_kind'] = 'dip'
            if job['outcome'] == 'unknown':
                run.update(state='recovery required', recovery_job=job['job_id'],
                           reason=result.get('reason') or job['error'])
            elif excursion:
                run.update(state='inspection', recovery_job=job['job_id'],
                           reason='Quality limit exceeded. Review the basket at its hold.')
            elif run['state'] not in TERMINAL:
                if job['outcome'] != 'succeeded':
                    outcome = job['outcome']
                    run.update(state={'held': 'held', 'stopped': 'stopped'}.get(outcome, 'failed'),
                               reason=result.get('reason') or job['error'])
                    if previous_job and run['final_hold'] in ('input', 'park') and outcome in ('failed', 'stopped'):
                        run.update(failed_hold=outcome == 'failed', stopped_hold=outcome == 'stopped',
                                   recovery_job=previous_job['job_id'])
                        if outcome == 'stopped':
                            run.update(state='recovery required',
                                       reason='Stop confirmed. Review the held basket and record a disposition.')
                elif run['cursor'] == len(run['recipe']['steps']):
                    if (hold and hold.get('location') == 'output' and hold.get('gripper') == 'released'
                            and hold.get('stopped') is True and hold.get('equipment_on') is False
                            and (hold.get('basket'), hold.get('run_id')) == (run['basket'], run['id'])):
                        run.update(state='succeeded', finished_utc=time.time())
                    else:
                        run.update(state='recovery required', recovery_job=job['job_id'],
                                   reason='The final output hold is unverified.')
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
                self._refresh_air(run, status.get('quality_alarm'))
                if run['boot'] != status['boot']:
                    self._hold('recovery required', 'Executor rebooted. Reconcile the last confirmed location and intent.')
                    if identity['boot'] != status['boot']:
                        self.next_pulse = 0
                        identity = self.pulse()
                if run['ack_pending']:
                    if run.get('ack_missing'):
                        return
                    evidence = self.request('status', {'job_id': run['ack_pending']})
                    if evidence['job'] is None:
                        with self.lock:
                            current = self.data['runs'][run['id']]
                            current.update(ack_missing=True,
                                           reason='IPC lost the terminal acknowledgement. Verify the retained hold.')
                            self._save()
                        return
                    acknowledgement = self.request('acknowledge', dict(identity, job_id=run['ack_pending']))
                    if acknowledgement != {'acknowledged': True, 'job_id': run['ack_pending']}:
                        raise OSError('Waiting for acknowledgement of the original executor job.')
                    with self.lock:
                        current = self.data['runs'][run['id']]
                        current['ack_pending'] = None
                        self._save()
                    return
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
                    if run.get('approval_session') != identity['session']:
                        raise ValueError('The controller session changed. Review the hold before resuming.')
                    if run['approval'] != status['stop_generation']:
                        raise ValueError('Stop or lease expiry changed the reviewed generation.')
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
