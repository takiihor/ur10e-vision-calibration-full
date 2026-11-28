"""Katana owns the recipe, one basket's dispatch cursor and its durable record."""
from copy import deepcopy
import hashlib
import json
from math import isclose
from pathlib import Path
from threading import Event, Lock, RLock, Thread
import time
from uuid import uuid4

from .executor import AdmissionRefused, MODULE_ATTESTATION_EXPIRED, PAIRED_ROLES, PROTOCOL, ROLES, finite, heartbeat_interval
from .executor_http import JobEvidenceError, deliver_priority_stop, stop_corroborated
from .jobs import atomic_json


DEFAULTS = {
    'ultrasonic': (0, 60, 60), 'rinse': (0, None, None), 'etch': (5, 5, 15),
    'desmut': (10, 30, 30), 'anodize': (None, 60, 30), 'seal': (0, None, None),
}
TERMINAL = {'succeeded', 'stopped', 'aborted'}


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
            if not isinstance(entry, dict) or not isinstance(entry.get('signature'), str):
                raise ValueError('Invalid operator journal record.')
            references[path] = None
            result = entry.get('result')
            if result is not None and not isinstance(result, dict):
                raise ValueError('Invalid stored operator result.')
            if isinstance(result, dict) and 'run_id' in result:
                run_id = result['run_id']
                if not isinstance(run_id, str) or not run_id:
                    raise ValueError('Invalid stored programme run identity.')
                references[path] = run_id
    except (AttributeError, OSError, ValueError, RecursionError) as exc:
        raise ValueError(f'{path}: {exc} Preserve the journal and call a technician.') from exc
    return references


def _read_record(path):
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise ValueError(f'{path}: Expected a regular history file.')
    return path.read_bytes() if path.exists() else None


def _history_digest(raw):
    return hashlib.sha256(raw).hexdigest() if raw is not None else None


def verify_programme_history(path, raw):
    """Check the exact retained bytes, including either side of an interrupted publish."""
    witness = path.with_name('programme-history.json')
    try:
        content = _read_record(witness)
        if content is None:
            if raw is not None:
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
    def __init__(self, root, request, steps, *, pulse=None, retained_actions_path=None, operator_arm=False):
        root = Path(root)
        root.mkdir(parents=True, exist_ok=True)
        self.path = root / 'programme.json'
        self.require_action_records = retained_actions_path is not None
        self.actions_path = (Path(retained_actions_path) if self.require_action_records
                             else root.parent / 'executor-operator-actions')
        self.witness_path = self.path.with_name('programme-history.json')
        self.stop_path = self.path.with_name('programme-stop.json')
        self.stop_lock, self.stop_ingress = Lock(), Lock()
        self.stop_epoch = 0
        self.local_stop = None
        self.stop_bytes, self.stop_synced = None, True
        self.history_fault, self.write_fault, self.persisted, self.witness = None, None, None, None
        self.request, self.shared_pulse = request, pulse
        self.operator_arm = operator_arm
        self.lock, self.tick_lock, self.lease_lock = RLock(), Lock(), Lock()
        self.stops = {}
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
            self.witness = verify_programme_history(self.path, self.persisted)
            self._check_references()
            if not self.shared_pulse:
                self.stop_bytes = _read_record(self.stop_path)
                if self.stop_bytes is not None:
                    self.local_stop = json.loads(self.stop_bytes)
                    if (not isinstance(self.local_stop, dict)
                            or not isinstance(self.local_stop.get('stop_id'), str)
                            or type(self.local_stop.get('confirmed')) is not bool
                            or ('run_id' not in self.local_stop)
                            or self.local_stop['run_id'] is not None
                            and self.local_stop['run_id'] not in self.data['runs']):
                        raise ValueError(f'{self.stop_path}: Invalid Stop intent.')
                    ident = self.local_stop['run_id']
                    if (ident is not None and (not self.local_stop['confirmed']
                            or self.data['runs'][ident].get('stop_id') != self.local_stop['stop_id'])):
                        self.stops[ident] = self.local_stop['stop_id']
                elif self.run and self.run.get('stop_id'):
                    raise ValueError(f'{self.stop_path}: The retained Stop intent is missing.')
        except (OSError, ValueError, KeyError, TypeError, RecursionError) as exc:
            self.history_fault = f'{self.path}: {exc} Preserve the history and call a technician.'
            return
        try:
            if self.persisted is None:
                self._save()
            elif 'previous' in json.loads(self.witness):
                self._settle_history_witness()
            if self.run and self.run.get('stop_pending') and self.run.get('stop_id'):
                self.stops.setdefault(self.run['id'], self.run['stop_id'])
            if (self.run and self.run['state'] not in TERMINAL and self.run['state'] != 'operator_check'
                    and not self._stopping(self.run)):
                self._mark_check('Katana restarted with a basket in progress.')
        except OSError as exc:
            self.write_fault = f'{self.path}: {exc} Verify the basket after the interrupted write.'

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
                    or any(key not in run for key in ('pending', 'stop_pending'))
                    or type(run['cursor']) is not int or not 0 <= run['cursor'] <= len(run['recipe']['steps'])
                    or any(not cls._valid_rows([row], [step for step in run['recipe']['steps']
                                                     if step['id'] == row['id']]) for row in run['steps'])):
                raise ValueError(f'Invalid stored basket record for {ident}.')
            if any(not isinstance(entry['job']['job_id'], str) for entry in run['jobs']):
                raise ValueError(f'Invalid stored executor job identity for basket {ident}.')

    def _check_references(self):
        records = retained_run_references(self.actions_path)
        for path, ident in records.items():
            if ident is not None and ident not in self.data['runs']:
                raise ValueError(f'Missing retained run {ident} referenced by {path}.')
        if self.require_action_records:
            for ident, run in self.data['runs'].items():
                for action in ('start', 'resume', 'abort'):
                    for request_id in run.get(f'{action}_requests', []):
                        filename = hashlib.sha256(request_id.encode()).hexdigest() + '.json'
                        path = self.actions_path / filename
                        if path not in records or records[path] not in (None, ident):
                            raise ValueError(f'{path}: Missing or inconsistent operator record for run {ident}.')

    def _require_history(self):
        if self.write_fault:
            raise ValueError(self.write_fault)
        if not self.history_fault:
            try:
                if _read_record(self.path) != self.persisted:
                    raise ValueError('Programme history changed on disk.')
                if _read_record(self.witness_path) != self.witness:
                    raise ValueError(f'{self.witness_path}: Programme history witness changed on disk.')
                with self.stop_lock:
                    if not self.shared_pulse and _read_record(self.stop_path) != self.stop_bytes:
                        raise ValueError(f'{self.stop_path}: The retained Stop intent changed on disk.')
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
        encoded = json.dumps(self.data, allow_nan=False, separators=(',', ':')).encode()
        try:
            # Publish both admissible digests before replacing the programme journal.
            atomic_json(self.witness_path, {'current': _history_digest(encoded),
                                           'previous': _history_digest(self.persisted)}, sync_directory=True)
            atomic_json(self.path, self.data, sync_directory=True)
            self.persisted = encoded
            self._settle_history_witness()
        except OSError as exc:
            self.write_fault = f'{self.path}: {exc} Verify the basket after the interrupted write.'
            raise

    def _recover_write(self):
        reason = self.write_fault
        try:
            raw = _read_record(self.path)
            witness = verify_programme_history(self.path, raw)
            data = json.loads(raw) if raw is not None else self.data
            self._validate_history(data)
            self.data, self.persisted, self.witness = data, raw, witness
            self._check_references()
        except OSError:
            return
        except (ValueError, KeyError, TypeError, RecursionError) as exc:
            self.history_fault = f'{self.path}: {exc} Preserve the history and call a technician.'
            self.write_fault = None
            return
        self.write_fault = None
        try:
            if self.run and self.run['state'] not in TERMINAL and self.run['state'] != 'operator_check':
                self._mark_check(reason)
            elif raw is None:
                self._save()
            elif witness is not None and 'previous' in json.loads(witness):
                self._settle_history_witness()
        except OSError:
            self.write_fault = reason

    def status(self):
        with self.lock:
            try:
                self._require_history()
            except ValueError:
                pass
            run = deepcopy(self.run)
            fault = self.history_fault or self.write_fault
            if fault and run and run['state'] not in TERMINAL:
                run.update(state='operator_check', operator_check=self._check_facts(run, fault, bool(self.history_fault)))
            return dict(recipe=effective(self.data['revisions'][self.data['recipe']]), run=run,
                        technician=self.history_fault,
                        baskets=[{key: saved[key] for key in ('id', 'basket', 'state')}
                                 for saved in self.data['runs'].values()])

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

    def review(self, *, starting=False):
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
            current_stop = status.get('stop')
            checking = self.run and self.run['state'] == 'operator_check'
            if ((self.local_stop and not self.local_stop['confirmed'])
                    or current_stop is not None and (current_stop.get('confirmed') is not True
                                                     or checking and not stop_corroborated(status, current_stop))):
                raise ValueError('Stop is unconfirmed. Verify a safe halt first.')
            if self.operator_arm:
                if status.get('owner') != 'calibration':
                    raise ValueError('Select the Calibration module as job owner on the AGV page.')
                if not checking and status.get('armed_for') != 'calibration':
                    raise ValueError('Arm for the Calibration module on the AGV page.')
            if starting and status.get('operator_check') is not None:
                raise ValueError('Resolve the outstanding IPC Operator check before starting another basket.')
            approval = dict(boot=status['boot'], session=identity['session'],
                            stop_generation=status['stop_generation'])
            return dict(approval=approval, block=None)
        except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
            return dict(approval=None, block=str(exc))

    def _check_approval(self, approval, *, starting=False):
        reviewed = self.review(starting=starting)
        if reviewed['block']:
            raise ValueError(reviewed['block'])
        if (not isinstance(approval, dict) or type(approval.get('stop_generation')) is not int
                or reviewed['approval'] != approval):
            raise ValueError('Review the current executor boot, controller session and Stop generation.')

    def retained_run(self, run_id):
        with self.lock:
            run = self.data['runs'].get(run_id)
            unresolved = [choice['pending']['job_id'] for choice in (run or {}).get('operator_choices', [])
                          if choice.get('evidence_recovery')]
        for job_id in unresolved:
            self._refresh_retained_evidence(run_id, job_id)
        with self.lock:
            return deepcopy(self.data['runs'].get(run_id))

    def request_run(self, request_id, action):
        with self.lock:
            return next((deepcopy(run) for run in self.data['runs'].values()
                         if request_id in run.get(f'{action}_requests', [])), None)

    def awaiting_arm(self):
        with self.lock:
            run = self.run
            return bool(self.operator_arm and run and run['state'] == 'running' and run['setup']
                        and not self._stopping(run))

    def control_owned(self):
        with self.lock:
            try:
                self._require_history()
            except ValueError:
                return True
            if self.local_stop and not self.local_stop['confirmed']:
                return True
            run = self.run
            return bool(run and (run['state'] not in TERMINAL or self._stopping(run)))

    def start(self, ident, basket, approval, *, request_id=None):
        if not isinstance(basket, str) or not basket.strip() or len(basket) > 80:
            raise ValueError('Identify the basket before starting.')
        stop_epoch = self.stop_epoch
        self._check_approval(approval, starting=True)
        with self.lock:
            self._require_history()
            if ident != self.data['recipe']:
                raise ValueError('Review the current recipe revision before starting.')
            if self.run and (self.run['state'] not in TERMINAL or self._stopping(self.run)):
                raise ValueError('Finish the current basket or resolve its operator check first.')
            revision = effective(self.data['revisions'][ident])
            run = dict(id=uuid4().hex, basket=basket.strip(), recipe=revision, state='running', cursor=0,
                       jobs=[], steps=[], pending=None, stop_pending=False, operator_choices=[],
                       step_kind='station_visit', setup=True, reason=None, boot=approval['boot'], approval_session=approval['session'],
                       approval=approval['stop_generation'],
                       final_hold=None, created_utc=time.time(),
                       start_requests=[request_id] if request_id else [])
            with self.stop_ingress:
                if self.stop_epoch != stop_epoch or (self.local_stop and not self.local_stop['confirmed']):
                    raise ValueError('Stop was requested. Review the current executor before starting.')
                self.data['runs'][run['id']] = run
                self.data['run'] = run['id']
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

    @staticmethod
    def _check_facts(run, reason, technician=False):
        pending = run.get('pending') or {}
        last = run['jobs'][-1]['job'] if run['jobs'] else {}
        return dict(reason=reason, location=run.get('final_hold'), basket=None,
                    job_id=pending.get('job_id') or last.get('job_id'),
                    step=min(run['cursor'] + 1, len(run['recipe']['steps'])), technician=technician)

    def _mark_check(self, reason, technician=False):
        with self.lock:
            run = self.run
            if not run or run['state'] in TERMINAL:
                return
            old = run.get('operator_check') or {}
            if old and (old.get('technician') or not technician):
                return
            run.update(state='operator_check', reason=reason,
                       operator_check=self._check_facts(run, reason, technician))
            self._save()

    def _sync_check(self, run, identity, status):
        if status['boot'] != identity['boot'] or status['session'] != identity['session'] or not status['lease_valid']:
            return
        facts = dict(run.get('operator_check') or self._check_facts(run, self.history_fault, True)) if run else dict(
            reason=self.history_fault, technician=True)
        # Never release a new hold using status from a previous Stop generation.
        if status.get('operator_check') is None or (self.history_fault and not status['operator_check'].get('technician')):
            status = self.request('hold', dict(identity, **facts, run_id=run and run['id'],
                                               stop_generation=status['stop_generation']))
        remote = status.get('operator_check')
        if not isinstance(remote, dict) or self.history_fault:
            return
        if status.get('boot') != identity['boot']:
            return
        if remote.get('run_id') not in (None, run['id']):
            raise ValueError('The outstanding IPC Operator check belongs to another basket.')
        if remote.get('technician') is True and isinstance(remote.get('reason'), str):
            facts.update(technician=True, reason=remote['reason'])
        for key, saved_key in (('location', 'observed_location'), ('basket', 'basket')):
            val = remote.get(key)
            facts[saved_key] = val if isinstance(val, str) and 0 < len(val) <= 80 else None
        remote_step = remote.get('step')
        if (remote.get('job_id') == facts['job_id'] and type(remote_step) is int
                and 1 <= remote_step <= len(run['recipe']['steps'])):
            facts['step'] = remote_step
        with self.lock:
            current = self.run
            if current['id'] == run['id'] and current['state'] == 'operator_check' and current['operator_check'] != facts:
                current['operator_check'] = facts
                self._save()

    def _deliver_stop(self, ident, stop_id, job_id, *, persist=None):
        try:
            body = dict(stop_id=stop_id, run_id=ident, job_id=job_id)
            report = deliver_priority_stop(self.request, body, persist) if persist else self.request('stop', body)
            if (not isinstance(report, dict) or report.get('stop_id') != stop_id or
                    type(report.get('generation')) is not int or report['generation'] < 1):
                return None
            # The shared link corroborates under the same lock that tracks the latest boot.
            if not self.shared_pulse and report.get('confirmed') is True:
                status = self.request('status')
                if not stop_corroborated(status, report):
                    return None
            return report
        except (OSError, ValueError, TypeError, AttributeError):
            return None

    def _record_stop(self, ident, stop_id, report):
        with self.lock:
            run = self.data['runs'][ident]
            if self.stops.get(ident, run.get('stop_id')) != stop_id:
                return deepcopy(run)
            confirmed = bool(report and report.get('received') is True and report.get('confirmed') is True)
            if not confirmed and run.get('stop_id') == stop_id and not run['stop_pending']:
                return deepcopy(run)
            values = dict(stop_pending=not confirmed, stop_id=stop_id, stop=report or {})
            if any(run.get(key) != value for key, value in values.items()):
                run.update(values)
                if run['state'] not in TERMINAL and not run.get('operator_check'):
                    reason = 'Stop requested. Physically verify the basket before choosing how to proceed.'
                    run.update(state='operator_check', reason=reason, operator_check=self._check_facts(run, reason))
                self._save()
            return deepcopy(run)

    def _persist_local_stop(self, intent, *, confirmed=False):
        with self.stop_lock:
            if self.local_stop['stop_id'] != intent['stop_id']:
                return False
            if self.local_stop['confirmed']:
                return True
            try:
                raw = _read_record(self.stop_path)
                updated = dict(intent, confirmed=confirmed)
                encoded = json.dumps(updated, allow_nan=False, separators=(',', ':')).encode()
                if raw != self.stop_bytes:
                    raise ValueError('The retained Stop intent changed on disk.')
                if raw != encoded or not self.stop_synced:
                    try:
                        atomic_json(self.stop_path, updated, sync_directory=True)
                    except OSError:
                        self.stop_synced = False
                        # A failed directory sync can leave the replacement visible.
                        if _read_record(self.stop_path) == encoded:
                            self.stop_bytes = encoded
                        raise
                    self.stop_bytes, self.stop_synced = encoded, True
                with self.stop_ingress:
                    if self.local_stop['stop_id'] != intent['stop_id']:
                        return False
                    if confirmed:
                        self.local_stop = updated
                return True
            except OSError:
                return False
            except ValueError as exc:
                self.history_fault = f'{self.stop_path}: {exc} Preserve the history and call a technician.'
                return False

    def _send_stop(self, intent):
        ident, stop_id, job_id = (intent[key] for key in ('run_id', 'stop_id', 'job_id'))
        durable = self.shared_pulse is not None

        def publish():
            nonlocal durable
            durable = self._persist_local_stop(intent)

        report = self._deliver_stop(ident, stop_id, job_id, persist=None if self.shared_pulse else publish)
        if not self.shared_pulse:
            settled = report and report.get('received') is True and report.get('confirmed') is True
            if not durable or settled and not self._persist_local_stop(intent, confirmed=True):
                report = None
        if ident is None or self.history_fault or self.write_fault:
            return report or {'received': False, 'confirmed': False}
        return self._record_stop(ident, stop_id, report)

    def stop(self):
        with self.stop_ingress:
            run = self.run
            ident = run['id'] if run else None
            pending = run and run['pending']
            intent = dict(stop_id=uuid4().hex, run_id=ident,
                          job_id=pending and pending['job_id'], confirmed=False)
            self.stop_epoch += 1
            if ident is not None:
                self.stops[ident] = intent['stop_id']
            if not self.shared_pulse:
                self.local_stop = intent
        return self._send_stop(intent)

    def _retry_stop(self, intent):
        try:
            self._send_stop(intent)
        except OSError:
            if not self.write_fault:
                raise

    def decide(self, ident, choice, approval, *, operator, basket, location, confirmed, step=None, request_id=None):
        if (choice not in ('resume', 'abort') or confirmed is not True
                or any(not isinstance(value, str) or not 0 < len(value.strip()) <= 80
                       for value in (operator, basket, location))):
            raise ValueError('Physically check the basket and provide the operator, basket ID and location.')
        operator, basket, location = operator.strip(), basket.strip(), location.strip()
        self._check_approval(approval)
        with self.tick_lock:
            with self.lock:
                self._require_history()
                run = self.run
                if not run or run['id'] != ident or run['state'] != 'operator_check' or self._stopping(run):
                    raise ValueError('Review the basket and wait for Stop confirmation.')
                if run['operator_check']['technician']:
                    raise ValueError(run['operator_check']['reason'])
                if choice == 'resume' and (basket != run['basket'] or location not in ('input', 'park')
                                            or type(step) is not int or not 1 <= step <= len(run['recipe']['steps'])):
                    raise ValueError('Confirm the original basket at input or park and choose a recipe step.')
                record = dict(choice=choice, operator=operator, basket=basket, location=location, step=step,
                              confirmed=True, reviewed_utc=time.time(), pending=deepcopy(run['pending']),
                              facts=deepcopy(run['operator_check']), applied=False)
                run.setdefault('operator_choices', []).append(record)
                self._save()
            identity = self.pulse()
            status = self.request('status')
            self._require_executor_evidence(status)
            if (identity['boot'], identity['session'], status.get('boot'), status.get('session'),
                    status.get('stop_generation'), status.get('lease_valid')) != (
                    approval['boot'], approval['session'], approval['boot'], approval['session'],
                    approval['stop_generation'], True):
                raise ValueError('Refresh the executor controls before deciding.')
            remote = status.get('operator_check')
            current_stop = status.get('stop')
            if (not isinstance(remote, dict) or remote.get('run_id') not in (None, ident)
                    or status.get('armed') is not False or status.get('active_job_id') is not None
                    or current_stop is not None and not stop_corroborated(status, current_stop)):
                raise ValueError('Wait for the executor to confirm the settled Operator check for this basket.')
            evidence_note = self._read_pending_evidence(deepcopy(run), identity, status)

            decision = dict(choice=choice, operator=operator, basket=basket, location=location, job_id=remote.get('job_id'),
                            confirmed=True, run_id=ident, stop_generation=approval['stop_generation'])
            if choice == 'resume':
                decision['step'] = step
            reply = self.request('operator_check', dict(identity, **decision))
            self._require_executor_evidence(reply)
            if (reply.get('boot') != approval['boot'] or reply.get('session') != approval['session']
                    or reply.get('stop_generation') != approval['stop_generation']
                    or 'operator_check' not in reply or reply['operator_check'] is not None
                    or reply.get('armed') is not False or reply.get('lease_valid') is not True):
                raise ValueError('Operator choice is unconfirmed. Check the basket again.')
            with self.lock:
                if self._stopping(run):
                    raise ValueError('Stop superseded this choice. Review the basket again.')
                record['applied'] = True
                record['checkpoint'] = deepcopy(run.get('checkpoint'))
                if evidence_note:
                    job_id = run['pending']['job_id']
                    record['evidence_note'] = evidence_note
                    record['evidence_recovery'] = dict(
                        cursor=run['cursor'], boot=run['boot'], close_air=run.get('close_air', True),
                        row_offset=sum(row.get('job_id') != job_id for row in run['steps']),
                        job_offset=len(run['jobs']))
                run.update(pending=None, checkpoint=None, operator_check=None)
                if choice == 'abort':
                    run.update(state='aborted', inspection=True, reason='Basket aborted after physical operator check.',
                               finished_utc=time.time())
                else:
                    run.update(state='running', cursor=step - 1, setup=True, source=location,
                               step_kind='station_visit', reason=None, boot=approval['boot'],
                               approval_session=approval['session'], approval=approval['stop_generation'],
                               close_air=location == 'park' and not evidence_note)
                    if location == 'park' and run['steps']:
                        prior = run['steps'][-1]
                        if (prior.get('boot') != approval['boot'] and prior['air_s'] is None
                                and prior['limits']['air'] is not None):
                            prior['inspection'] = run['inspection'] = True
                if request_id:
                    run.setdefault(f'{choice}_requests', []).append(request_id)
                self._save()
                return deepcopy(run)

    def _dispatch(self, run, identity):
        pending = run['pending']
        if pending:
            evidence = self.request('status', {'job_id': pending['job_id']})
            self._require_executor_evidence(evidence)
            if (evidence.get('boot'), evidence.get('session'), evidence.get('stop_generation'),
                    evidence.get('lease_valid')) != (run['boot'], run['approval_session'], run['approval'], True):
                raise ValueError('The executor control changed while a job was outstanding.')
            if evidence.get('job') is None:
                raise ValueError('The executor has no result for the original job.')
            return evidence['job']

        steps = run['recipe']['steps']
        step = steps[run['cursor']]
        kind = run['step_kind']
        source = run.get('source') or run['final_hold'] or 'input'
        if kind == 'station_visit':
            params = dict(station=step['station'], run_id=run['id'], basket=run['basket'],
                          load_input=source == 'input')
        else:
            grouped = steps[run['cursor']:run['cursor']+2] if step['role'] in PAIRED_ROLES else [step]
            params = dict(steps=grouped, source=source, hold='output' if grouped[-1]['terminal'] else 'park',
                          expected_basket='released', run_id=run['id'], basket=run['basket'],
                          recipe_revision=run['recipe']['id'])
            prior = run['steps'][-1] if run['steps'] and run.get('close_air', True) else None
            if (prior and prior.get('boot') == identity['boot'] and prior['air_s'] is None
                    and prior['limits']['air'] is not None):
                params['previous_lift'] = dict(lift_s=prior['lift_s'], air_limit=prior['limits']['air'])
        params['step'] = run['cursor'] + 1
        payload = dict(job_id=uuid4().hex, type=kind, version=1, params=params, stop_generation=run['approval'])
        with self.lock:
            if self.run['state'] != 'running' or self.run['pending'] or self._stopping(self.run):
                return None
            self.run.update(pending=payload, checkpoint=None)
            self._save()
        if self._stopping(self.run) or self.history_fault:
            return None
        return self.request('submit', dict(identity, **payload))

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
                    or not 0 <= row['placed_s'] <= plc['done_s'] <= plc['sample_s'] <= row['lift_s']
                    or not isclose(plc['elapsed_s'], plc['done_s'] - row['placed_s'], rel_tol=0, abs_tol=.001)
                    or (row.get('incoming_air_s') is not None and not finite(row['incoming_air_s']))):
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
        if not isinstance(confirmed, list) or any(not isinstance(entry, dict) for entry in confirmed):
            return False
        measurements = {name: [entry for entry in confirmed if entry.get('effect') == name]
                        for name in ('place', 'dwell', 'lift')}
        verified_lifts = sum(isinstance(entry.get('evidence'), dict)
                             and entry['evidence'].get('location') in ('transfer', 'park', 'output', 'input')
                             for entry in measurements['lift'])
        if verified_lifts != len(rows) or any(len(effects) < len(rows) for effects in measurements.values()):
            return False
        for index, (row, placement, dwell, lift) in enumerate(zip(rows, measurements['place'],
                                                               measurements['dwell'], measurements['lift'])):
            observed, done, lifted = (entry.get('evidence') for entry in (placement, dwell, lift))
            placed_step, dwell_step = placement.get('value'), dwell.get('value')
            if (not all(isinstance(value, dict) for value in (observed, done, lifted, placed_step, dwell_step))
                    or any(step.get('id') != row['id'] or step.get('tank') != row['tank']
                           for step in (placed_step, dwell_step))
                    or observed.get('start_s') != row['placed_s'] or observed.get('cycle') != row['plc']['cycle']
                    or observed.get('done') is not False or observed.get('basket_present') is not True
                    or done != row['plc'] or lifted.get('lift_s') != row['lift_s']
                    or lifted.get('location') not in ('transfer', 'park', 'output', 'input')):
                return False
            if index:
                prev = rows[index - 1]
                measured = row['placed_s'] - prev['lift_s']
                if (measured < 0 or prev['air_s'] is None or not isclose(prev['air_s'], measured, abs_tol=1e-6)
                        or row.get('incoming_air_s') is None or not isclose(row['incoming_air_s'], measured, abs_tol=1e-6)):
                    return False
        return True

    def _checkpoint_matches(self, checkpoint, run, expected):
        previous = run.get('checkpoint')
        if (not isinstance(checkpoint, dict) or checkpoint.get('boot') != run['boot']
                or type(checkpoint.get('sequence')) is not int or checkpoint['sequence'] < 0):
            return False
        rows = checkpoint.get('steps')
        intent = checkpoint.get('intent')
        payload = run['pending']
        confirmed = checkpoint.get('confirmed')
        if (not isinstance(confirmed, list) or not self._effects_match(payload, confirmed)
                or 'intent' not in checkpoint or checkpoint['sequence'] != 2 * len(confirmed) + (intent is not None)
                or intent is not None and (not isinstance(intent, dict)
                    or not self._effects_match(payload, [*confirmed, dict(intent, evidence={})], pending=True))
                or not self._valid_rows(rows, expected) or not self._lifts_match_checkpoint(rows, checkpoint)):
            return False
        if rows and (rows[-1]['air_s'] is not None and len(rows) == len(expected)):
            return False
        prior = run['pending']['params'].get('previous_lift')
        if rows and prior:
            measured = rows[0]['placed_s'] - prior['lift_s']
            incoming = rows[0].get('incoming_air_s')
            if measured < 0 or not finite(incoming) or not isclose(measured, incoming, rel_tol=0, abs_tol=1e-6):
                return False
        return (not previous or (checkpoint['sequence'] >= previous['sequence']
                and checkpoint['confirmed'][:len(previous['confirmed'])] == previous['confirmed']
                and len(rows) >= len(previous['steps']) and self._rows_agree(previous['steps'], rows)))

    @staticmethod
    def _effects_match(payload, confirmed, *, complete=False, pending=False):
        params = payload['params']
        basket = {key: params[key] for key in ('basket', 'run_id')}
        effects = []
        if payload['type'] == 'station_visit':
            if params['load_input']:
                effects.append(('load_input', params))
            effects.append(('station', dict(basket, station=params['station'])))
        else:
            steps = params['steps']
            source, gripper = params['source'], params['expected_basket']
            for index, step in enumerate(steps):
                paired = index + 1 < len(steps)
                effects.extend([
                    ('place', dict(step, **basket, source=source, gripper=gripper)),
                    ('dwell', step),
                    ('lift', 'transfer' if paired else params['hold']),
                ])
                if paired:
                    source, gripper = 'transfer', 'held'
                    station = steps[index + 1]['station']
                    if station != step['station']:
                        effects.append(('station', dict(basket, station=station, transfer=True)))
        if len(confirmed) > len(effects) or (complete and len(confirmed) != len(effects)):
            return False
        for index, (entry, (effect, value)) in enumerate(zip(confirmed, effects)):
            if (not isinstance(entry, dict) or (entry.get('effect'), entry.get('value')) != (effect, value)
                    or not isinstance(entry.get('evidence'), dict)):
                return False
            if pending and index + 1 == len(confirmed):
                break
            evidence = entry['evidence']
            if effect == 'station' and (evidence.get('station') != value['station']
                                        or evidence.get('stopped') is not True):
                return False
            if effect == 'load_input' and any(evidence.get(key) != expected for key, expected in {
                    'basket': value['basket'], 'run_id': value['run_id'], 'location': 'input',
                    'gripper': 'released', 'stopped': True, 'equipment_on': False,
            }.items()):
                return False
            if effect == 'lift':
                location = evidence.get('location')
                if (complete or location in ('transfer', 'park', 'output', 'input')) and (
                        location != value or evidence.get('released') is not (value != 'transfer')):
                    return False
        return True

    @staticmethod
    def _save_rows(run, rows, boot, job_id, close_air):
        if not rows:
            return
        saved = run['steps']
        pending_rows = [row for row in saved if row.get('job_id') == job_id]
        for index, observed in enumerate(rows):
            row = dict(deepcopy(observed), boot=boot, job_id=job_id)
            step = run['recipe']['steps'][run['cursor'] + index]
            limits = row['limits']
            overstay = (row['lift_s'] - row['plc']['done_s'] if step['role'] == 'anodize'
                        else row['dwell_s'] - step['setpoint'])
            if ((limits['min'] is not None and row['dwell_s'] < limits['min'])
                    or (limits['overstay'] is not None and overstay > limits['overstay'])
                    or (limits['air'] is not None and row['air_s'] is not None and row['air_s'] > limits['air'])):
                row['inspection'] = True
            run['inspection'] = run.get('inspection', False) or row['inspection']
            if index < len(pending_rows):
                original = pending_rows[index]
                original['inspection'] |= row['inspection']
                if original['air_s'] is None:
                    original['air_s'] = row['air_s']
                continue
            if index == 0 and close_air and saved:
                previous = saved[-1]
                measured = row['placed_s'] - previous['lift_s']
                if previous.get('boot') == boot and previous['air_s'] is None and measured >= 0:
                    previous['air_s'] = measured
                    bound = previous['limits']['air']
                    if bound is not None and measured > bound:
                        previous['inspection'] = run['inspection'] = True
            saved.append(row)

    def _terminal_matches(self, payload, job, run):
        result = job.get('result')
        checkpoint = job.get('checkpoint')
        expected = payload['params'].get('steps', [])
        exceptional_outcomes = {'failed': ('failed', 'unknown'), 'stopped': ('stopped',),
                                'interrupted': ('unknown',), 'refused': ('failed',)}
        if job.get('outcome') in exceptional_outcomes.get(job.get('state'), ()):
            if checkpoint is None:
                return result == {} and not run.get('checkpoint')
            return (job['state'] != 'refused' and isinstance(result, dict)
                    and result.get('outcome', job['outcome']) == job['outcome']
                    and set(result) <= {'outcome', 'steps', 'alarm'}
                    and self._checkpoint_matches(checkpoint, run, expected)
                    and result.get('steps', []) == checkpoint['steps'])
        if (not isinstance(result, dict) or job.get('outcome') not in ('succeeded', 'held', 'unknown', 'failed', 'stopped')
                or result.get('outcome') != job['outcome'] or job.get('state') != 'done'
                or not self._checkpoint_matches(checkpoint, run, expected)):
            return False
        rows = result.get('steps', [])
        if (not self._valid_rows(rows, expected) or len(rows) != len(checkpoint['steps'])
                or not self._rows_agree(rows, checkpoint['steps'])
                or any(row['air_s'] != saved['air_s'] for row, saved in zip(rows, checkpoint['steps']))):
            return False
        params = payload['params']
        hold, exposure = result.get('hold'), result.get('open_air')
        if exposure is not None and (not rows or not isinstance(exposure, dict) or exposure != dict(
                job_id=job['job_id'], basket=run['basket'], run_id=run['id'],
                lift_s=rows[-1]['lift_s'], air_limit=rows[-1]['limits']['air'])):
            return False
        if hold is not None and (not isinstance(hold, dict) or (hold.get('basket'), hold.get('run_id')) != (
                run['basket'], run['id']) or hold.get('location') not in ('input', 'park', 'output')
                or hold.get('stopped') is not True or hold.get('equipment_on') is not False
                or hold.get('gripper') != 'released' or type(hold.get('station')) is not int):
            return False
        if job['outcome'] not in ('succeeded', 'held'):
            return True
        if (hold is None or checkpoint['intent'] is not None
                or not self._effects_match(payload, checkpoint['confirmed'], complete=True)):
            return False
        if payload['type'] == 'dip':
            if not expected or len(rows) != len(expected):
                return False
            return hold['location'] == params['hold'] and hold['station'] == expected[-1]['station']
        destination = 'input' if params['load_input'] else 'park'
        return hold['station'] == params['station'] and hold['location'] == destination

    def _refresh_air(self, run, alarm):
        if not isinstance(alarm, dict) or not finite(alarm.get('measured_air_s')):
            return
        ident = alarm.get('job_id')
        row = next((row for row in reversed(run['steps'])
                    if row.get('job_id') == ident and row['id'] == alarm.get('step_id')), None)
        if (not row or row['limits']['air'] is None or row['air_s'] is not None
                or 'open_air_alarm_s' in row or alarm['measured_air_s'] <= row['limits']['air']):
            return
        status = self.request('status', {'job_id': ident})
        self._require_executor_evidence(status)
        job = status.get('job') or {}
        result = job.get('result') or {}
        exposure = result.get('open_air')
        if (status.get('boot') != row.get('boot') or job.get('job_id') != ident
                or result.get('open_air_alarm') != alarm
                or exposure != dict(job_id=ident, basket=run['basket'], run_id=run['id'],
                                    lift_s=row['lift_s'], air_limit=row['limits']['air'])):
            raise ValueError('The measured air alarm contradicts the retained lift or basket.')
        with self.lock:
            for row in self.run['steps']:
                if row.get('job_id') == ident and row['id'] == alarm['step_id']:
                    row.update(inspection=True, open_air_alarm_s=alarm['measured_air_s'])
                    self.run['inspection'] = True
                    self._save()
                    break

    def _merge_checkpoint(self, run, job):
        checkpoint = job.get('checkpoint')
        payload = run['pending']
        if checkpoint is None:
            if run.get('checkpoint'):
                raise ValueError('The physical checkpoint disappeared.')
            return False
        if not self._checkpoint_matches(checkpoint, run, payload['params'].get('steps', [])):
            raise ValueError('The physical checkpoint is incomplete or contradicts confirmed evidence.')
        if checkpoint == run.get('checkpoint'):
            return False
        run['checkpoint'] = checkpoint
        self._save_rows(run, checkpoint['steps'], checkpoint['boot'], job['job_id'], run.get('close_air', True))
        return True

    def _observe_pending(self, run, evidence):
        job = evidence.get('job')
        job_id = run['pending']['job_id']
        if job is None:
            if run.get('checkpoint') or job_id in (evidence.get('latest_job_id'), evidence.get('active_job_id')):
                return False, 'The executor omitted the original job.'
            return False, None
        if not isinstance(job, dict) or job.get('job_id') != job_id:
            return False, 'The original job lookup returned a different job.'
        try:
            changed = self._merge_checkpoint(run, job)
        except ValueError as exc:
            return False, str(exc)
        if not self._terminal_matches(run['pending'], job, run):
            return changed, 'The original job outcome is incomplete or contradicts confirmed evidence.'
        if any(entry['job']['job_id'] == job_id for entry in run['jobs']):
            return changed, None
        run['jobs'].append(dict(type=run['pending']['type'], job=job))
        return True, None

    def _read_pending_evidence(self, run, identity, status):
        pending = run['pending']
        if pending is None:
            return None
        try:
            evidence = self.request('status', {'job_id': pending['job_id']})
        except JobEvidenceError as exc:
            return str(exc)
        self._require_executor_evidence(evidence)
        if (evidence.get('boot'), evidence.get('session'), evidence.get('lease_valid'),
                evidence.get('stop_generation')) != (
                identity['boot'], identity['session'], True, status['stop_generation']):
            raise ValueError('Refresh the executor status before releasing the Operator check.')
        with self.lock:
            current = self.run
            if current['id'] != run['id'] or current['pending'] != pending or current['state'] != 'operator_check':
                return None
            changed, note = self._observe_pending(current, evidence)
            if changed:
                self._save()
            return note

    def _refresh_retained_evidence(self, run_id, job_id):
        # Record refresh reads the original ID. Dispatch never replays it.
        try:
            status = self.request('status')
            evidence = self.request('status', {'job_id': job_id})
            self._require_executor_evidence(evidence)
            if status.get('boot') != evidence.get('boot') or evidence.get('job') is None:
                return
            with self.lock:
                self._require_history()
                self._attach_retained_evidence(run_id, job_id, evidence)
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            pass

    def _attach_retained_evidence(self, run_id, job_id, evidence):
        run = self.data['runs'][run_id]
        for index, choice in enumerate(run['operator_choices']):
            saved = choice.get('evidence_recovery')
            if saved and choice['pending']['job_id'] == job_id:
                break
        else:
            return
        start = saved['row_offset']
        end = start + sum(row.get('job_id') == job_id for row in run['steps'])
        view = dict(run, **saved, pending=choice['pending'], checkpoint=choice['checkpoint'],
                    steps=deepcopy(run['steps'][:end]), jobs=[])
        changed, note = self._observe_pending(view, evidence)
        if not changed:
            return
        rows_added = len(view['steps']) - end
        run['steps'][:end] = view['steps']
        job_offset = saved['job_offset']
        run['jobs'][job_offset:job_offset] = view['jobs']
        for later in run['operator_choices'][index + 1:]:
            recovery = later.get('evidence_recovery')
            if recovery:
                recovery['row_offset'] += rows_added
                recovery['job_offset'] += len(view['jobs'])
        run['inspection'] = run.get('inspection', False) or view.get('inspection', False)
        choice['checkpoint'] = view['checkpoint']
        if not note:
            choice.pop('evidence_recovery')
            choice.pop('evidence_note')
        self._save()

    def _merge(self, job):
        with self.lock:
            run = self.run
            payload = run['pending']
            if run['state'] != 'running' or self._stopping(run):
                return
            if not isinstance(job, dict) or job.get('job_id') != payload['job_id']:
                raise ValueError('The executor returned a different or incomplete physical job.')
            expected = payload['params'].get('steps', [])
            waiting = job.get('state') in ('accepted', 'running', 'waiting')
            if waiting and (job.get('outcome') is not None or job.get('result') != {}):
                raise ValueError('The running job returned contradictory evidence.')
            if not waiting and not self._terminal_matches(payload, job, run):
                raise ValueError('The physical job outcome is incomplete or contradicts confirmed evidence.')
            if self._merge_checkpoint(run, job):
                self._save()
            if waiting:
                return
            result = job['result']
            run['jobs'].append(dict(type=payload['type'], job=job))
            run['pending'] = None
            hold = result.get('hold')
            if hold:
                run['final_hold'] = hold['location']
            if job['outcome'] != 'succeeded':
                self._mark_check(result.get('reason') or job.get('error') or 'The executor held the physical job.')
                return
            if payload['type'] == 'station_visit':
                run['step_kind'] = 'dip'
            else:
                run.update(cursor=run['cursor'] + len(expected), source=None, close_air=True, step_kind='station_visit')
                if run['cursor'] == len(run['recipe']['steps']):
                    run.update(state='succeeded', finished_utc=time.time())
            self._save()

    def tick(self):
        with self.tick_lock:
            with self.lock:
                if self.write_fault:
                    self._recover_write()
                try:
                    self._require_history()
                except ValueError:
                    pass
                run = deepcopy(self.run)
            if self.local_stop and not self.local_stop['confirmed']:
                self._retry_stop(self.local_stop)
                return
            if self.write_fault and run and run['state'] not in TERMINAL and not self._stopping(run):
                self.stop()
                return
            if not self.history_fault and (run is None or run['state'] in TERMINAL and not self._stopping(run)):
                return
            if run and self._stopping(run):
                stop_id = self.stops.get(run['id']) or run.get('stop_id')
                if stop_id:
                    pending = run['pending']
                    intent = dict(stop_id=stop_id, run_id=run['id'], job_id=pending and pending['job_id'])
                    self._retry_stop(intent)
                return
            if self.write_fault:
                return
            identity = status = None
            try:
                # Status gets a chance to report the reason and observations even after local history failure.
                identity = self.pulse()
                status = self.request('status')
                previous_stop = run and run.get('stop')
                if (run and run['state'] == 'operator_check' and previous_stop
                        and previous_stop.get('confirmed') and not stop_corroborated(status, previous_stop)):
                    self.stop()
                    return
                if self.history_fault or run['state'] == 'operator_check':
                    if not self.history_fault:
                        try:
                            self._require_executor_evidence(status)
                        except ValueError as exc:
                            self._mark_check(str(exc), technician=True)
                    self._sync_check(deepcopy(self.run), identity, status)
                    if not self.history_fault:
                        self._read_pending_evidence(run, identity, status)
                        self._refresh_air(run, status.get('quality_alarm'))
                    return
                self._require_executor_evidence(status)
                if (identity['boot'], status['boot'], identity['session'], status['session'], status['lease_valid'],
                        status['stop_generation']) != (run['boot'], run['boot'], run['approval_session'],
                                                       run['approval_session'], True, run['approval']):
                    raise ValueError('The executor boot, lease or Stop generation changed.')
                if status.get('operator_check') is not None:
                    raise ValueError('The executor requires a physical operator check.')
                self._refresh_air(run, status.get('quality_alarm'))
                if run['setup']:
                    if self.operator_arm:
                        if status.get('owner') != 'calibration':
                            raise ValueError('The Calibration module lost executor ownership.')
                        if status.get('armed_for') != 'calibration':
                            return
                    else:
                        if not status['device']['connected']:
                            self.request('connect', identity)
                        armed = self.request('arm', dict(identity, stop_generation=run['approval']))
                        if (armed.get('armed') is not True or armed.get('boot') != run['boot']
                                or armed.get('session') != run['approval_session']
                                or armed.get('stop_generation') != run['approval']):
                            raise ValueError('Fresh Arm could not be verified.')
                    with self.lock:
                        if self.run['state'] != 'running' or self._stopping(self.run):
                            return
                        self.run['setup'] = False
                        self._save()
                elif status['armed'] is not True or self.operator_arm and status.get('armed_for') != 'calibration':
                    if not (self.operator_arm and status.get('owner') == 'calibration'
                            and status.get('module_attestation_expired') is True):
                        raise ValueError('The executor disarmed during the basket programme.')
                    if not run['pending']:
                        self._await_rearm()
                        return
                try:
                    job = self._dispatch(run, identity)
                except AdmissionRefused as exc:
                    if self.operator_arm and str(exc) == MODULE_ATTESTATION_EXPIRED:
                        self._await_rearm(refused_job=exc.job_id)
                        return
                    raise
                if job is not None:
                    self._merge(job)
                elif not self._stopping(self.run):
                    raise ValueError('The physical job reply was missing.')
            except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
                if not self.history_fault:
                    try:
                        self._mark_check(str(exc) or 'The executor reply was lost.',
                                         technician='call a technician' in str(exc).lower())
                    except (OSError, ValueError):
                        pass
                if identity and (self.run is None or not self._stopping(self.run)):
                    try:
                        self._sync_check(deepcopy(self.run), identity, self.request('status'))
                    except (OSError, ValueError, KeyError, TypeError, AttributeError):
                        try:
                            self.stop()
                        except (OSError, ValueError):
                            pass

    def _await_rearm(self, *, refused_job=None):
        with self.lock:
            run = self.run
            if run['state'] == 'running' and not self._stopping(run) and (
                    (run['pending'] or {}).get('job_id') == refused_job):
                run.update(setup=True, pending=None, checkpoint=None)
                self._save()

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
