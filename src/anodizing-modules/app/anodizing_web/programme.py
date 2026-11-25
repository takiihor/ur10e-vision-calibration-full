"""Katana owns the recipe, one basket's dispatch cursor and its durable record."""
from copy import deepcopy
import hashlib
import json
from math import isclose
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


def retained_run_references(directory):
    directory = Path(directory)
    references = {}
    path = directory
    try:
        if directory.is_symlink():
            raise ValueError('Expected a regular journal directory.')
        if not directory.exists():
            return references
        for path in directory.iterdir():
            if path.suffix != '.json':
                continue
            if path.is_symlink() or not path.is_file():
                raise ValueError('Expected a regular journal file.')
            entry = json.loads(path.read_text())
            result = entry.get('result')
            if result is not None and not isinstance(result, dict):
                raise ValueError('Invalid stored operator result.')
            if isinstance(result, dict) and 'run_id' in result:
                run_id = result['run_id']
                if not isinstance(run_id, str) or not run_id:
                    raise ValueError('Invalid stored programme run identity.')
                references[run_id] = path
    except (AttributeError, OSError, ValueError, RecursionError) as exc:
        raise ValueError(f'{path}: {exc} Preserve the journal and call a technician.') from exc
    return references


def _read_record(path):
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise ValueError(f'{path}: Expected a regular history file.')
    return path.read_bytes() if path.exists() else None


def _history_digest(raw):
    return hashlib.sha256(raw).hexdigest() if raw is not None else None


def verify_programme_history(path, raw, data):
    """Check the exact retained bytes, including either side of an interrupted publish."""
    witness = path.with_name('programme-history.json')
    try:
        content = _read_record(witness)
        if content is None:
            if data.get('history_witness'):
                raise ValueError('The programme history witness is missing.')
            return None
        document = json.loads(content)
        if not isinstance(document, dict) or set(document) not in ({'current'}, {'current', 'previous'}):
            raise ValueError('Invalid programme history witness.')
        for label, digest in document.items():
            if digest is None and label == 'previous':
                continue
            if not isinstance(digest, str) or len(digest) != 64 or any(c not in '0123456789abcdef' for c in digest):
                raise ValueError('Invalid programme history digest.')
        if _history_digest(raw) not in document.values():
            raise ValueError(f'The retained digest disagrees with {path}.')
        return content
    except (OSError, ValueError, RecursionError) as exc:
        raise ValueError(f'{witness}: {exc} Preserve the history and call a technician.') from exc


class Programme:
    def __init__(self, root, request, steps, *, pulse=None, order_intent=None, retained_actions_path=None):
        root = Path(root)
        root.mkdir(parents=True, exist_ok=True)
        self.path = root / 'programme.json'
        self.actions_path = (Path(retained_actions_path) if retained_actions_path is not None
                             else root.parent / 'executor-operator-actions')
        self.witness_path = self.path.with_name('programme-history.json')
        self.history_fault, self.persisted, self.witness = None, None, None
        self.request, self.shared_pulse, self.order_intent = request, pulse, order_intent
        self.lock, self.tick_lock, self.lease_lock = RLock(), Lock(), Lock()
        self.stop_lock, self.stops = Lock(), {}
        self.close_requested = Event()
        self.worker = None
        self.session, self.sequence = uuid4().hex, 0
        self.identity, self.next_pulse = None, 0
        revision = draft(steps)
        self.data = dict(recipe=revision['id'], revisions={revision['id']: revision}, run=None, runs={})
        try:
            self.persisted = _read_record(self.path)
            if self.persisted is not None:
                data = json.loads(self.persisted)
                self._validate_history(data)
                self.data = data
            self.witness = verify_programme_history(self.path, self.persisted, self.data)
            self._check_references()
        except (OSError, ValueError, KeyError, TypeError, RecursionError) as exc:
            self.history_fault = f'{self.path}: {exc} Preserve the history and call a technician.'
            return
        if self.persisted is None or self.witness is None:
            self._save()
        elif 'previous' in json.loads(self.witness):
            self._settle_history_witness()
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

    @classmethod
    def _validate_history(cls, data):
        current = data['revisions'][data['recipe']]
        if (current['state'] not in ('DRAFT', 'FINAL') or not isinstance(current['steps'], list)
                or not isinstance(data['runs'], dict) or
                (data['run'] is None and data['runs']) or
                (data['run'] is not None and data['run'] not in data['runs'])):
            raise ValueError('Invalid programme history structure.')
        for ident, run in data['runs'].items():
            if (run['id'] != ident or not isinstance(run['jobs'], list) or not isinstance(run['steps'], list)
                    or not isinstance(run['basket'], str) or not isinstance(run['state'], str)
                    or any(key not in run for key in ('pending', 'ack_pending', 'stop_pending'))
                    or type(run['cursor']) is not int or run['cursor'] != len(run['steps'])
                    or not cls._valid_rows(run['steps'], run['recipe']['steps'])):
                raise ValueError(f'Invalid stored basket record for {ident}.')
            if any(not isinstance(entry['job']['job_id'], str) for entry in run['jobs']):
                raise ValueError(f'Invalid stored executor job identity for basket {ident}.')

    def _check_references(self):
        for ident, path in retained_run_references(self.actions_path).items():
            if ident not in self.data['runs']:
                raise ValueError(f'Missing retained run {ident} referenced by {path}.')

    def _require_history(self):
        if not self.history_fault:
            try:
                if _read_record(self.path) != self.persisted:
                    raise ValueError('Programme history changed on disk.')
                if _read_record(self.witness_path) != self.witness:
                    raise ValueError(f'{self.witness_path}: Programme history witness changed on disk.')
                self._check_references()
            except (OSError, ValueError) as exc:
                self.history_fault = f'{self.path}: {exc} Preserve the history and call a technician.'
        if self.history_fault:
            raise ValueError(self.history_fault)

    @staticmethod
    def _require_executor_evidence(status):
        faults = status.get('evidence_faults') or {}
        if faults:
            raise ValueError('Executor evidence is fenced. ' + ' '.join(
                f'{name}: {reason}' for name, reason in faults.items()) + ' Call a technician.')

    @property
    def run(self):
        return self.data['runs'].get(self.data['run'])

    def _settle_history_witness(self):
        atomic_json(self.witness_path, {'current': _history_digest(self.persisted)}, sync_directory=True)
        self.witness = self.witness_path.read_bytes()

    def _save(self):
        self._require_history()
        self.data['history_witness'] = 1
        encoded = json.dumps(self.data, allow_nan=False, separators=(',', ':')).encode()
        try:
            # Publish both admissible digests before replacing the programme journal.
            atomic_json(self.witness_path, {'current': _history_digest(encoded),
                                           'previous': _history_digest(self.persisted)}, sync_directory=True)
            atomic_json(self.path, self.data, sync_directory=True)
            self.persisted = encoded
            self._settle_history_witness()
        except OSError as exc:
            self.history_fault = (f'{self.path}, {self.witness_path}: {exc} '
                                  'Preserve the history and call a technician.')
            raise

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
            self._require_history()
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
            self._require_history()
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
            with self.lock:
                self._require_history()
            meta = self.request('health')
            if not self.compatible(meta):
                raise ValueError('The configured simulation executor is incompatible.')
            identity = self.shared_pulse() if self.shared_pulse else {'session': self.session, 'boot': meta['boot']}
            status = self.request('status')
            self._require_executor_evidence(status)
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
            try:
                self._require_history()
            except ValueError:
                return True
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
            self._require_history()
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
        latest = self.stops.get(run['id'])
        return run['stop_pending'] or (latest is not None and latest != run.get('stop_id'))

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
            if report is None and run.get('stop_id') == stop_id:
                return deepcopy(run)
            if (run.get('stop_id'), run.get('stop'), run['stop_pending']) != (stop_id, report, not confirmed):
                run.update(stop_pending=not confirmed, stop_id=stop_id, stop=report or {})
                self._settle_stop(run)
                self._save()
            return deepcopy(run)

    def stop(self, request_id=None):
        run = self.run
        if run is None:
            return self.request('stop', {'stop_id': request_id} if request_id else {})
        ident = run['id']
        stop_id = request_id or (self._stopping(run) and self.stops.get(ident)) or uuid4().hex
        self.stops[ident] = stop_id
        pending = run['pending']
        report = self._deliver_stop(ident, stop_id, pending['job_id'] if pending else None)
        try:
            return self._record_stop(ident, stop_id, report)
        except ValueError:
            if not self.history_fault:
                raise
            return report

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
                self._require_history()
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
                        self._require_executor_evidence(status)
                        missing = status['job'] is None
                        intent = dict(job_id=job_id, disposition=note, missing=missing, observation_id=uuid4().hex,
                                      basket=run['basket'], run_id=run['id'])
                        if allow_unaccounted:
                            intent['allow_unaccounted'] = True
                        if not allow_unaccounted:
                            intent['release_basket'] = True
                        if observed_only and not missing:
                            intent['observed_only'] = True
                        run['disposition_intent'] = intent
                        self._save()
            if intent:
                self._require_executor_evidence(self.request('status', {'job_id': intent['job_id']}))
                identity = self.pulse()
                try:
                    job = self.request('reconcile', dict(identity, **intent))
                except ValueError as exc:
                    with self.lock:
                        run['disposition_intent'] = None
                        if 'unload' in str(exc).lower() and allow_unaccounted:
                            run.update(basket_unaccounted=False, reason=str(exc))
                        elif 'unaccounted' in str(exc).lower():
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
                if allow_unaccounted and not unaccounted and hold['location'] in ('input', 'park'):
                    reason = 'The original basket is present. Confirm the verified unload to close this run.'
                    with self.lock:
                        run.update(disposition_intent=None, basket_unaccounted=False,
                                   final_hold=hold['location'], reason=reason)
                        self._save()
                    raise ValueError(reason)
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
            if exc.job_id != payload['job_id']:
                raise OSError('Waiting for the original executor refusal identity.') from exc
            self._record_refusal(payload, str(exc))
            return None

    def _record_refusal(self, payload, reason):
        with self.lock:
            run = self.run
            if run['pending'] != payload:
                return
            last = run['jobs'][-1]['job'] if run['jobs'] else None
            occupied = bool(last and run['final_hold'] in ('input', 'park'))
            run.update(state='failed', reason=reason, pending=None, pending_missing=False,
                       failed_hold=occupied, recovery_job=last['job_id'] if occupied else None,
                       refusal=dict(job_id=payload['job_id'], reason=reason))
            self._save()

    def _dispatch(self, run, identity):
        if run['pending'] is not None:
            payload = run['pending']
            evidence = self.request('status', {'job_id': payload['job_id']})
            self._require_executor_evidence(evidence)
            job = evidence['job']
            if (isinstance(job, dict) and job.get('job_id') == payload['job_id']
                    and job.get('state') == 'refused' and job.get('outcome') == 'failed'
                    and isinstance(job.get('error'), str) and job['error'].strip()
                    and job.get('checkpoint') is None and not job.get('result')
                    and not run.get('checkpoint') and not run.get('pending_accepted')):
                self._record_refusal(payload, job['error'])
                return None
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
                            pending_accepted=False, pending_utc_ns=time.time_ns())
            self._save()
        return self._send_pending(payload, identity)

    @staticmethod
    def _valid_rows(rows, expected):
        if not isinstance(rows, list) or len(rows) > len(expected):
            return False
        for row, step in zip(rows, expected):
            if (not isinstance(row, dict) or row.get('id') != step['id'] or row.get('tank') != step['tank']
                    or row.get('limits') != step['limits'] or type(row.get('inspection')) is not bool
                    or not all(finite(row.get(key)) for key in ('placed_s', 'lift_s', 'dwell_s'))
                    or 'air_s' not in row
                    or (row['air_s'] is not None and (not finite(row['air_s']) or row['air_s'] < 0))
                    or row['dwell_s'] < 0
                    or not isclose(row['dwell_s'], row['lift_s'] - row['placed_s'], abs_tol=1e-6)):
                return False
            plc = row.get('plc')
            if (not isinstance(plc, dict) or type(plc.get('cycle')) is not int or plc['cycle'] < 1
                    or plc.get('done') is not True or plc.get('basket_present') is not True
                    or plc.get('equipment_on') is not False
                    or not all(finite(plc.get(key)) for key in ('done_s', 'sample_s', 'elapsed_s'))
                    or plc['elapsed_s'] < 0
                    or not row['placed_s'] <= plc['done_s'] <= plc['sample_s'] <= row['lift_s']):
                return False
        return True

    @staticmethod
    def _rows_agree(first, second):
        # Closing an air interval may enrich the result after its checkpoint was saved.
        for left, right in zip(first, second):
            if ({k: v for k, v in left.items() if k not in ('air_s', 'inspection', 'boot')}
                    != {k: v for k, v in right.items() if k not in ('air_s', 'inspection', 'boot')}):
                return False
            if left.get('air_s') is not None and right.get('air_s') is not None and left['air_s'] != right['air_s']:
                return False
        return True

    @staticmethod
    def _lifts_match_checkpoint(rows, checkpoint):
        confirmed = checkpoint.get('confirmed')
        if not isinstance(confirmed, list):
            return not rows
        measurements = {}
        for name in ('place', 'dwell', 'lift'):
            measurements[name] = [entry.get('evidence') for entry in confirmed
                                  if isinstance(entry, dict) and entry.get('effect') == name]
        if any(len(effects) < len(rows) for effects in measurements.values()):
            return False
        for row, placement, plc, lift in zip(rows, measurements['place'],
                                             measurements['dwell'], measurements['lift']):
            if (not isinstance(placement, dict) or not isinstance(lift, dict)
                    or placement.get('start_s') != row['placed_s']
                    or placement.get('cycle') != row['plc']['cycle']
                    or placement.get('done') is not False or placement.get('basket_present') is not True
                    or plc != row['plc'] or lift.get('lift_s') != row['lift_s']
                    or lift.get('location') not in ('transfer', 'park', 'output', 'input')):
                return False
        return True

    def _checkpoint_extends(self, checkpoint, previous):
        if not previous:
            return True
        confirmed, retained = checkpoint.get('confirmed', []), previous.get('confirmed', [])
        return (isinstance(confirmed, list) and isinstance(retained, list)
                and confirmed[:len(retained)] == retained
                and type(checkpoint.get('sequence')) is int
                and checkpoint['sequence'] >= previous.get('sequence', -1)
                and checkpoint.get('boot') == previous.get('boot')
                and self._rows_agree(previous.get('steps', []), checkpoint.get('steps', [])))

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
        verified = []
        for rows, boot in reversed(candidates):
            if self._valid_rows(rows, expected) and all(self._rows_agree(rows, prior) for prior, _ in verified):
                verified.append((rows, boot))
        return max(reversed(verified), key=lambda candidate: len(candidate[0]), default=([], None))

    @staticmethod
    def _merge_rows(run, rows, boot):
        if not rows:
            return
        confirmed = {row['id']: row for row in run['steps']}
        recipe_steps = {step['id']: step for step in run['recipe']['steps']}
        for record in rows:
            step = recipe_steps[record['id']]
            limits = step['limits']
            overstay = (record['lift_s'] - record['plc']['done_s'] if step['role'] == 'anodize'
                        else record['dwell_s'] - step['setpoint'])
            if ((limits['min'] is not None and record['dwell_s'] < limits['min'])
                    or (limits['overstay'] is not None and overstay > limits['overstay'])
                    or (limits['air'] is not None and record['air_s'] is not None
                        and record['air_s'] > limits['air'])):
                record = dict(record, inspection=True)
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
        for previous, following in zip(run['steps'], run['steps'][1:]):
            if previous['air_s'] is not None or not previous.get('boot') or previous.get('boot') != following.get('boot'):
                continue
            measured = following['placed_s'] - previous['lift_s']
            if measured >= 0:
                previous['air_s'] = measured
                bound = previous['limits']['air']
                if previous['inspection'] or (bound is not None and measured > bound):
                    previous['inspection'] = True
        run['inspection'] = any(row['inspection'] for row in run['steps'])

    def _terminal_matches(self, payload, job, run, previous_checkpoint=None):
        result = job.get('result')
        if not isinstance(result, dict):
            return False
        params = payload['params']
        rows = result.get('steps', [])
        expected = params.get('steps', [])
        checkpoint = job.get('checkpoint') or {}
        checkpoint_rows = checkpoint.get('steps', [])
        if (not self._valid_rows(rows, expected) or not self._valid_rows(checkpoint_rows, expected)
                or not self._rows_agree(rows, checkpoint_rows)
                or (rows and not self._lifts_match_checkpoint(rows, checkpoint))):
            return False
        if previous_checkpoint:
            if checkpoint and not self._checkpoint_extends(checkpoint, previous_checkpoint):
                return False
            previous_rows = previous_checkpoint.get('steps', [])
            if (not self._valid_rows(previous_rows, expected) or not self._rows_agree(previous_rows, rows)
                    or not self._rows_agree(previous_rows, checkpoint_rows)):
                return False
        if job.get('outcome') not in ('succeeded', 'held'):
            if job.get('outcome') not in ('unknown', 'failed', 'stopped'):
                return False
            hold = result.get('hold')
            if hold is None:
                return True
            if not isinstance(hold, dict):
                return False
            if 'basket' not in hold and isinstance(job.get('reconciliation'), dict):
                return True
            return (hold.get('location') in ('input', 'park', 'output') and hold.get('gripper') == 'released'
                and hold.get('stopped') is True and hold.get('equipment_on') is False
                and (hold.get('basket'), hold.get('run_id')) == (run['basket'], run['id']))
        hold = result.get('hold')
        if (not isinstance(hold, dict) or hold.get('gripper') != 'released'
                or hold.get('stopped') is not True or hold.get('equipment_on') is not False
                or (hold.get('basket'), hold.get('run_id')) != (run['basket'], run['id'])):
            return False
        if payload['type'] == 'dip':
            if len(rows) != len(expected) or not expected:
                return False
            last_lift = next((effect['evidence'] for effect in reversed(checkpoint['confirmed'])
                              if effect.get('effect') == 'lift'), {})
            return (hold.get('location') == last_lift.get('location') == params['hold']
                    and hold.get('station') == expected[-1]['station']
                    and last_lift.get('released') is True)
        destination = 'input' if params.get('load_input', run['cursor'] == 0) else 'park'
        return hold.get('station') == params['station'] and hold.get('location') == destination

    def _refresh_air(self, run, alarm):
        entry = next((entry for entry in reversed(run['jobs'])
                      if ((entry['job'].get('result') or {}).get('open_air') or {}).get('job_id') ==
                      entry['job']['job_id']), None)
        if not entry or not run['steps']:
            return
        ident = entry['job']['job_id']
        saved_rows = (entry['job'].get('result') or {}).get('steps', [])
        saved = next((row for row in run['steps'] if saved_rows and row['id'] == saved_rows[-1]['id']), None)
        if not saved or saved['air_s'] is not None:
            return
        job = self.request('status', {'job_id': ident})['job']
        result = (job or {}).get('result') or {}
        rows = result.get('steps', [])
        expected = self._recipe_steps(run, ident)
        if (not job or job.get('job_id') != ident or not self._valid_rows(rows, expected)
                or len(rows) != len(saved_rows) or not self._rows_agree(rows, saved_rows)):
            return
        row = rows[-1]
        reported = result.get('open_air_alarm')
        if not isinstance(reported, dict) or (reported.get('job_id'), reported.get('step_id')) != (ident, row['id']):
            return
        sample = reported.get('measured_air_s')
        if isinstance(alarm, dict) and alarm.get('job_id') == ident and alarm.get('measured_air_s') != sample:
            return
        final = row['air_s']
        elapsed = sample if sample is not None else final
        if (row['limits']['air'] is None or not finite(elapsed) or elapsed <= row['limits']['air']
                or (final is not None and final < elapsed)):
            return
        with self.lock:
            current = self.data['runs'][run['id']]
            saved = next(step for step in current['steps'] if step['id'] == row['id'])
            if not saved['inspection'] or final is not None or 'open_air_alarm_s' not in saved:
                saved['inspection'] = current['inspection'] = True
                saved.setdefault('open_air_alarm_s', elapsed)
                if final is not None:
                    saved['air_s'] = final
                self._save()

    def _ack_matches(self, run, job):
        ident = run['ack_pending']
        expected = next((entry['job'] for entry in run['jobs'] if entry['job']['job_id'] == ident), None)
        if run.get('disposition') and isinstance(job, dict) and job.get('job_id') == ident:
            evidence = run.get('recovery_observation')
            if evidence and (job.get('reconciliation') or job.get('observation')) == {
                    key: val for key, val in evidence.items() if key != 'job_id'}:
                return True
        if not expected or not isinstance(job, dict):
            return False
        if any(job.get(key) != expected.get(key) for key in ('job_id', 'state', 'outcome', 'checkpoint', 'error')):
            return False
        old_result, result = deepcopy(expected.get('result') or {}), deepcopy(job.get('result') or {})
        old_rows, rows = old_result.pop('steps', []), result.pop('steps', [])
        for key in ('open_air_alarm', 'open_air_disposition'):
            old_result.pop(key, None)
            result.pop(key, None)
        return (old_result == result and self._valid_rows(rows, self._recipe_steps(run, ident))
                and len(old_rows) == len(rows) and self._rows_agree(old_rows, rows))

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
            expected = payload['params'].get('steps', [])
            updated = bool(isinstance(checkpoint, dict) and checkpoint != previous_checkpoint
                           and type(checkpoint.get('sequence')) is int and checkpoint.get('sequence', -1) >= 0
                           and self._valid_rows(checkpoint.get('steps', []), expected)
                           and self._lifts_match_checkpoint(checkpoint.get('steps', []), checkpoint)
                           and self._checkpoint_extends(checkpoint, previous_checkpoint))
            if job.get('state') in ('accepted', 'running', 'waiting'):
                if updated or not run.get('pending_accepted'):
                    run['pending_accepted'] = True
                    if updated:
                        run['checkpoint'] = checkpoint
                    self._save()
                return
            if (job.get('state') not in ('done', 'failed', 'interrupted', 'stopped')
                    or not self._terminal_matches(payload, job, run, previous_checkpoint)):
                failures = (run.get('invalid_result') or 0) + 1
                run.update(invalid_result=min(failures, 2),
                           state='held' if failures == 1 and run['state'] not in TERMINAL else 'recovery required',
                           reason='The original executor result is incomplete or inconsistent. Verify its hold and evidence.')
                if failures <= 2:
                    self._save()
                return
            if updated:
                run['checkpoint'] = checkpoint
            run.pop('invalid_result', None)
            previous_job = run['jobs'][-1]['job'] if run['jobs'] else None
            run['jobs'].append(dict(type=payload['type'], job=job, intent_utc_ns=run.get('pending_utc_ns'),
                                    intent_order=run.get('pending_order', 0)))
            run.update(pending=None, pending_missing=False, ack_pending=job['job_id'])
            result = job['result']
            rows, boot = self._confirmed_rows(job, payload['params'].get('steps', []), previous_checkpoint)
            self._merge_rows(run, rows, boot)
            hold = result.get('hold')
            if not hold:
                reconciliation = result.get('reconciliation') or {}
                if not reconciliation.get('unaccounted'):
                    hold = reconciliation.get('hold')
            if hold and (hold.get('basket'), hold.get('run_id')) == (run['basket'], run['id']):
                run['final_hold'] = hold['location']
            if job['outcome'] in ('succeeded', 'held') and payload['type'] == 'station_visit':
                run['step_kind'] = 'dip'
            if job['outcome'] == 'unknown':
                run.update(state='recovery required', recovery_job=job['job_id'],
                           reason=result.get('reason') or job['error'])
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
                try:
                    self._require_history()
                except ValueError:
                    return
                run = deepcopy(self.run)
            if run is None:
                return
            if self._stopping(run):
                stop_id = self.stops.get(run['id']) or run.get('stop_id')
                if stop_id:
                    pending = run['pending']
                    report = self._deliver_stop(run['id'], stop_id, pending['job_id'] if pending else None)
                    if report:
                        self._record_stop(run['id'], stop_id, report)
                else:
                    self.stop()
            try:
                if run['state'] in TERMINAL and not run['ack_pending'] and not run['pending']:
                    if run['final_hold'] == 'park':
                        self._refresh_air(run, None)
                    return
                identity = self.pulse()
                status = self.request('status')
                self._refresh_air(run, status.get('quality_alarm'))
                if run['boot'] != status['boot']:
                    self._hold('recovery required', 'Executor rebooted. Reconcile the last confirmed location and intent.')
                    if identity['boot'] != status['boot']:
                        self.next_pulse = 0
                        identity = self.pulse()
                if run['ack_pending']:
                    evidence = self.request('status', {'job_id': run['ack_pending']})
                    if evidence['job'] is None or evidence.get('evidence_faults'):
                        reason = 'IPC lost the terminal acknowledgement. Verify the retained hold.'
                        try:
                            self._require_executor_evidence(evidence)
                        except ValueError as exc:
                            reason = str(exc)
                        if run.get('ack_missing') and run.get('reason') == reason:
                            return
                        with self.lock:
                            current = self.data['runs'][run['id']]
                            current.update(ack_missing=True, reason=reason)
                            self._save()
                        return
                    if not self._ack_matches(run, evidence['job']):
                        raise OSError('Waiting for matching terminal evidence for the original executor job.')
                    acknowledgement = self.request('acknowledge', dict(identity, job_id=run['ack_pending']))
                    if acknowledgement != {'acknowledged': True, 'job_id': run['ack_pending']}:
                        raise OSError('Waiting for acknowledgement of the original executor job.')
                    with self.lock:
                        current = self.data['runs'][run['id']]
                        current['ack_pending'] = None
                        if current.get('ack_missing'):
                            current.update(ack_missing=False, reason=None)
                        self._save()
                    return
                self._require_executor_evidence(status)
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
                if not self.history_fault:
                    self._hold('held', 'Executor unavailable. Review outstanding status on reconnect.')
            except ValueError as exc:
                if not self.history_fault:
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
