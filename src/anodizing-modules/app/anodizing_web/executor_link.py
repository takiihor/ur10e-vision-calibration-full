"""Katana's AGV page adapter. Browser traffic never renews the IPC lease."""
from contextlib import contextmanager, nullcontext
from copy import deepcopy
from hashlib import sha256
import json
from threading import Event, Lock, RLock, Thread
from time import monotonic, time
from uuid import uuid4

from .executor import (AdmissionRefused, HEARTBEAT_S, PROTOCOL, check_manual_attestation,
                       check_manual_feedback, heartbeat_interval)
from .executor_http import (
    JobEvidenceError, StopEvidenceError, deliver_priority_stop, legacy_display_status, stop_corroborated,
)
from .jobs import ID, atomic_json
from .line_link import LineLink, line_carrier


PENDING_STATES = {"accepted", "running", "waiting"}
NO_STOP_RECEIPT = dict(received=False, halt_attempted=False, confirmed=False)


class ExecutorLink:
    def __init__(self, client, local, *, bluetooth=None, route_address=None):
        self.client = client
        self.job_gateway = getattr(client, 'tls', None) is not None
        self.calibration_client = client.for_owner('calibration') if self.job_gateway else client
        self.local = local
        self.bluetooth = bluetooth
        self.line_link = LineLink(line_carrier(route_address))
        self.last_rs485_station_count = None
        self.session = uuid4().hex
        self.sequence = self.heartbeat_sequence = self.connection_id = 0
        self.device_identity = self.identity = None
        self.stop_path = local.owner.root / 'executor-stop.json'
        self.manual_path = local.owner.root / 'executor-manual-visit.json'
        self.manual_witness = self.manual_path.with_name('executor-manual-active.json')
        self.manual_history_directory = local.owner.root / 'executor-manual-history'
        self.stop_latch = {}
        self.journal_fault = None
        self.stop_witness = self.stop_path.with_name('executor-stop-witness.json')
        self.storage_check = None
        self.stop_intent = self._load_checked(self.stop_path)
        witness = self._load_checked(self.stop_witness)
        self.stop_written = witness
        primary = (self.stop_intent or {}).get('id')
        if not self.journal_fault and not witness and not self.stop_path.exists():
            try:
                atomic_json(self.stop_path, None, sync_directory=True)
            except OSError as exc:
                self.storage_check = f'{self.stop_path.name}: {exc}'
        if witness and witness['id'] != primary:
            if primary in witness.get('previous', ()) and self.stop_path.is_file():
                self.storage_check = f'{self.stop_path.name}: Stop publication was interrupted.'
            else:
                self.journal_fault = f'{self.stop_path.name} is missing or inconsistent. Please call a technician.'
            self.stop_latch['pending'] = witness
        elif self.stop_intent:
            if not witness:
                self.journal_fault = f'{self.stop_witness.name} is missing. Please call a technician.'
            if (not self.stop_intent['report']['confirmed'] or
                    witness and witness.get('revision') != self.stop_intent.get('revision')):
                self.stop_latch['pending'] = witness or self.stop_intent
        if witness and witness['id'] != primary and witness.get('storage_check'):
            self.storage_check = witness['storage_check']
        if self.stop_intent and self.stop_intent.get('storage_check'):
            self.storage_check = self.stop_intent['storage_check']
        recovery_stop = self._pending_stop() or self.stop_intent or {}
        self.storage_context = dict(run_id=recovery_stop.get('run_id'), job_id=recovery_stop.get('job_id'))
        self.remote_event_ids = {}
        self.manual = self._load_checked(self.manual_path)
        active = self.manual_marker = self._load_checked(self.manual_witness)
        self.manual_history = None
        if active:
            self._verify_manual_history()
            archives = active.get('archives', {})
            if archives:
                self.manual_history = self._load_checked(self.manual_history_directory / (next(reversed(archives)) + '.json'))
            if self.manual and any(self.manual.get(key) != active['intent'].get(key)
                                   for key in ('id', 'station', 'boot', 'session', 'stop_generation', 'request_id',
                                               'attestation', 'attested_at')):
                self.journal_fault = f'{self.manual_path.name} conflicts with its intent. Please call a technician.'
            elif active['id'] not in archives and not active['committed']:
                self.manual = dict(self.manual or active['intent'], doubt='Station submission was interrupted.')
            elif active['id'] not in archives and not self.manual:
                self.journal_fault = f'{self.manual_path.name} is missing or inconsistent. Please call a technician.'
        elif self.manual or self.manual_history_directory.exists():
            self.journal_fault = f'{self.manual_witness.name} is missing. Please call a technician.'
        self.stop_lock, self.stop_receipts, self.manual_lock = Lock(), Lock(), RLock()
        self.closing = Event()
        self.lock = Lock()
        self.stop_boot_lock = Lock()
        self.stop_boot = None
        self.retired_boots = set()
        self.cached = local.commissioning(ports=True)
        self.cached.update(ports=[], serial_available=True, wired_port='IPC executor',
                           executor_link=True, motion_block='Waiting for executor capabilities.')
        self.sampled = monotonic()
        self.worker = Thread(target=self._loop, daemon=True, name='agv-executor-heartbeat')
        self.worker.start()

    @staticmethod
    def _load(path):
        if path.is_symlink():
            raise ValueError('Executor intents require regular journal files.')
        if path.exists():
            return json.loads(path.read_text())

    def _load_checked(self, path):
        try:
            value = self._load(path)
            if value is not None and (not isinstance(value, dict) or
                                      not isinstance(value.get('id'), str) or not ID.fullmatch(value['id'])):
                raise ValueError('The executor journal has no valid ID.')
            if path in (self.stop_path, self.stop_witness) and value is not None and (
                    not isinstance(value.get('report'), dict) or any(type(value['report'].get(key)) is not bool
                    for key in NO_STOP_RECEIPT)):
                raise ValueError('The Stop journal has no valid report.')
            if path == self.manual_witness and value is not None and (
                    type(value.get('committed')) is not bool or not isinstance(value.get('intent'), dict)
                    or value['intent'].get('id') != value['id']
                    or not isinstance(value.get('archives'), dict)
                    or any(not ID.fullmatch(ident) or not isinstance(digest, str) or len(digest) != 64
                           for ident, digest in value['archives'].items())):
                raise ValueError('The manual visit witness has no valid intent.')
            if path == self.manual_path and value is not None and (
                    'result' not in value or type(value.get('station')) is not int):
                raise ValueError('The manual visit journal has no valid intent.')
            return value
        except (OSError, ValueError):
            self.journal_fault = f'{path.name} is unreadable. Please call a technician.'

    def _pending_stop(self):
        return self.stop_latch.get('pending')

    def _publish_stop(self, intent):
        intent = dict(intent, storage_check=self.storage_check, revision=uuid4().hex)
        self.stop_latch['pending'] = intent
        return intent

    def _persist_stop_witness(self, intent):
        with self.stop_receipts:
            current = self._pending_stop()
            if current is None or current.get('revision') != intent.get('revision'):
                return
            if self.stop_written and self.stop_written.get('revision') == intent.get('revision'):
                return
            previous = (self.stop_written or {}).get('id'), (self.stop_intent or {}).get('id')
            witness = dict(intent, previous=list(dict.fromkeys(previous)))
            try:
                atomic_json(self.stop_witness, witness, sync_directory=True)
                self.stop_written = witness
            except OSError as exc:
                self._stop_write_error(intent, self.stop_witness, exc)

    def _stop_write_error(self, intent, path, exc):
        self.flag_storage_failure(f'{path.name}: {exc}', run_id=intent.get('run_id'), job_id=intent.get('job_id'))

    def _deliver_stop(self, intent):
        try:
            body = {'stop_id': intent['id']}
            if self.stop_written and self.stop_written.get('revision') == intent.get('revision'):
                report = self.client('stop', body)
            else:
                report = deliver_priority_stop(self.client, body, lambda: self._persist_stop_witness(intent))
            if (report.get('stop_id') != intent['id'] or report.get('received') is not True
                    or not isinstance(report.get('boot'), str) or type(report.get('generation')) is not int
                    or report['generation'] < 1):
                raise ValueError('The Stop reply conflicts with the pending request.')
            if report.get('confirmed'):
                try:
                    status = self.client('status')
                except StopEvidenceError as exc:
                    status = exc.status
                if not stop_corroborated(status, report):
                    report = dict(report, confirmed=False)
        except (OSError, ValueError):
            report = dict(NO_STOP_RECEIPT)
        with self.stop_receipts:
            current = self._pending_stop()
            if current is None or current.get('revision') != intent.get('revision'):
                return dict(report, confirmed=False)
            if not self.stop_written or self.stop_written.get('revision') != current.get('revision'):
                return dict(report, confirmed=False)
            previous = current['report']
            if previous['received'] and (not report['received'] or
                    (previous.get('boot') == report.get('boot') and previous['halt_attempted']
                     and not report['halt_attempted'])):
                report = dict(previous, confirmed=False)
            try:
                # Serialize durable confirmation with boot observation, outside Stop ingress.
                with self.stop_boot_lock if report['confirmed'] else nullcontext():
                    if self.stop_boot != report.get('boot'):
                        report = dict(report, confirmed=False)
                    updated = dict(current, report=report, storage_check=self.storage_check)
                    if updated != self.stop_intent:
                        atomic_json(self.stop_path, updated, sync_directory=True)
                        self.stop_intent = updated
                    with self.stop_lock:
                        if self._pending_stop() is not current:
                            return dict(report, confirmed=False)
                        if report['confirmed']:
                            del self.stop_latch['pending']
                        else:
                            self.stop_latch['pending'] = updated
            except OSError as exc:
                self._stop_write_error(intent, self.stop_path, exc)
                return dict(report, confirmed=False)
        if report['confirmed']:
            with self.lock:
                if (self.stop_boot == self.cached.get('executor_boot') == status['boot'] and
                        self.cached.get('stop_generation', -1) <= status['stop_generation']):
                    self.cached.update(armed=False, executor_stop=deepcopy(status['stop']),
                                       stop_generation=status['stop_generation'],
                                       executor_job=deepcopy(status['job']))
        return report

    def _recheck_stop(self, status):
        intent = self.stop_intent
        report = (intent or {}).get('report') or {}
        current = status.get('stop')
        # Arm consumes the current receipt. An older unrelated sample does not cover this Stop.
        if not report.get('confirmed') or not current or status.get('boot') != report.get('boot'):
            return
        generation = current.get('generation')
        if (current.get('stop_id') != report.get('stop_id') and type(generation) is int
                and type(report.get('generation')) is int and generation < report['generation']):
            return
        if stop_corroborated(status, report):
            return
        with self.stop_lock:
            if self.stop_intent is intent and not self._pending_stop():
                self._publish_stop(dict(intent, report=dict(report, confirmed=False)))

    def stop(self, context=None):
        context = context or {}
        key = context.get('stop_id')
        with self.stop_lock:
            pending = self._pending_stop()
            if not pending or key and pending['request'] != key:
                job = self.cached.get('executor_job') or self.manual or {}
                ident = key if isinstance(key, str) and ID.fullmatch(key) else uuid4().hex
                pending = self._publish_stop(dict(
                    id=ident, request=key, boot=self.cached.get('executor_boot'),
                    run_id=context.get('run_id'), job_id=context.get('job_id') or job.get('job_id') or job.get('id'),
                    report=dict(NO_STOP_RECEIPT)))
        return self._deliver_stop(pending)

    def pulse(self):
        with self.lock:
            if self.identity is None:
                if self.cached.get('executor_reachable') is False:
                    raise ValueError(self.line_link.failure_message())
                raise ValueError('The executor control lease is unavailable.')
            return dict(self.identity)

    def calibration_request(self, operation, body=None):
        client = self.client if operation in ('hold', 'operator_check') else self.calibration_client
        return self.request(operation, body, client=client)

    def request(self, operation, body=None, *, client=None):
        if operation == 'stop':
            return self.stop(body)
        if self.bluetooth and operation in ('connect', 'arm'):
            with self.bluetooth.wired_control():
                return self._request(operation, body, client=client)
        return self._request(operation, body, client=client)

    def _request(self, operation, body, *, client=None):
        if operation in ('arm', 'submit'):
            if self._pending_stop():
                raise ValueError('Stop is unconfirmed. Verify a safe halt before arming or submitting.')
            with self.manual_lock:
                if not self.journal_fault:
                    self._verify_manual_history()
                    if self.journal_fault:
                        self.stop()
            if self.journal_fault or self.storage_check:
                raise ValueError(self.journal_fault or self.storage_check)
            if self.manual and self.manual.get('doubt'):
                raise ValueError('Complete the operator check for the retained station visit.')
        state = (client or self.client)(operation, body)
        if (operation == 'operator_check' and self.storage_check and body.get('run_id')
                and self._decision_applied(state, body)):
            self._clear_storage_check()
        return state

    def bluetooth_write_block(self):
        status = self.client('status')
        if status.get('armed') is not False or status.get('device', {}).get('connected') is not False:
            return 'Disconnect and disarm the executor before changing Bluetooth settings.'
        if (status.get('active_job_id') or status.get('job') or status.get('recovery_required')
                or status.get('operator_check') or (status.get('stop') or {}).get('confirmed') is False
                or status.get('evidence_faults') or self._pending_stop() or self.journal_fault or self.manual):
            return 'Resolve the current executor job or operator check before changing Bluetooth settings.'
        return None

    def _loop(self):
        while not self.closing.is_set():
            started = monotonic()
            interval = HEARTBEAT_S
            try:
                stop = self._pending_stop()
                receipt = self._deliver_stop(stop) if stop else None
                if receipt is not None and not receipt['received']:
                    raise OSError('Stop delivery is unconfirmed.')
                meta = self.client('health')
                if self.job_gateway and meta.get('gateway_enabled') is not True:
                    raise ValueError('The IPC HTTPS gateway is not ready.')
                interval = heartbeat_interval(meta.get('heartbeat_s'), meta.get('lease_s'))
                identity = None
                if (not self.journal_fault and meta['protocol'] == PROTOCOL and meta['job_types'].get('station_visit') == 1 and
                        (receipt is None or receipt.get('boot', meta['boot']) == meta['boot'])):
                    binding = dict(boot=meta['boot'], session=self.session, protocol=PROTOCOL)
                    self.heartbeat_sequence += 1
                    try:
                        lease = self.client('heartbeat', dict(binding, sequence=self.heartbeat_sequence))
                        if lease.get('boot') != binding['boot']:
                            raise OSError('The executor boot changed while establishing the lease.')
                        if lease['lease_valid']:
                            identity = binding
                    except ValueError:
                        # A retiring controller's lease still fences this session.
                        pass
                if meta['protocol'] == 1:
                    read_legacy = getattr(self.client, 'read_legacy_status', None)
                    status = (read_legacy(meta['boot']) if read_legacy else
                              legacy_display_status(self.client('status'), meta['boot']))
                else:
                    try:
                        status = self.client('status')
                    except StopEvidenceError as exc:
                        status = exc.status
                if status['boot'] != meta['boot']:
                    raise OSError('The executor boot changed while sampling status.')
                self._apply(status, identity, meta)
                if meta['protocol'] == PROTOCOL:
                    self._sync_storage_check(status, identity)
                    self._refresh_manual()
            except JobEvidenceError as exc:
                with self.manual_lock:
                    if self.manual and not self.manual.get('doubt'):
                        try:
                            self._manual_hold(self.manual, str(exc))
                        except OSError:
                            # The write failure already requested Stop and retained the check.
                            pass
            except (OSError, ValueError):
                with self.lock:
                    self.identity = None
                    age = self.cached.get('status_age_s')
                    now = monotonic()
                    self.cached.update(armed=False, armed_for=None, stale=True, executor_reachable=False,
                                       status_age_s=None if age is None else age+now-self.sampled,
                                       executor_session=None,
                                       error='Executor unavailable. Use the AGV e-stop if needed.')
                    self.sampled = now
            with self.lock:
                reachable = self.cached.get('executor_reachable')
            self.line_link.sample(reachable, monotonic())
            self.closing.wait(max(0, interval - (monotonic()-started)))

    def _apply(self, status, identity=None, meta=None):
        boot = status['boot']
        with self.stop_boot_lock:
            if boot in self.retired_boots:
                raise ValueError('The executor boot was retired while sampling status.')
            if self.stop_boot is not None and self.stop_boot != boot:
                self.retired_boots.add(self.stop_boot)
            self.stop_boot = boot
            self._recheck_stop(status)
        receipt = status.get('stop')
        if receipt and receipt.get('confirmed') and not stop_corroborated(status, receipt):
            status = dict(status, stop=dict(receipt, confirmed=False))
        manual_id = status.get('latest_manual_job_id')
        if (not self.journal_fault and manual_id and not self.manual and not
                (self.manual_history_directory / f'{manual_id}.json').is_file()):
            self.journal_fault = f'{self.manual_path.name} is missing. Please call a technician.'
            self.stop()
        device = dict(status['device'])
        with self.lock:
            if self.stop_boot != boot:
                raise ValueError('The executor boot was retired while applying status.')
            if meta is not None:
                self.identity = identity
                self.cached['executor_health'] = meta
            key = status['boot'], device.get('connection_id')
            if key != self.device_identity:
                self.connection_id += 1
                self.device_identity = key
            self._record_remote_events(key, device)
            self.sequence += 1
            device.update(connection_id=self.connection_id, sequence=self.sequence,
                          armed=status['armed'], stop_generation=status['stop_generation'],
                          executor_boot=status['boot'],
                          executor_session=status.get('session') if self.identity else None,
                          executor_job=status['job'], recovery_required=status['recovery_required'],
                          operator_check=status.get('operator_check'),
                          quality_alarm=status.get('quality_alarm'),
                          evidence_faults=status.get('evidence_faults', {}),
                          recovery_ids=status.get('recovery_ids', []), executor_stop=status.get('stop'),
                          motion_block=self._motion_reason(status))
            if self.job_gateway:
                device.update(job_gateway=True, owner=status.get('owner'), armed_for=status.get('armed_for'),
                              recent_jobs=status.get('recent_jobs', self.cached.get('recent_jobs', [])))
            station_count = (device.get('status') or {}).get('max_stations')
            if (type(station_count) is int and device.get('status_time')
                    and self.cached.get('executor_health', {}).get('simulated') is False):
                self.last_rs485_station_count = dict(value=station_count, observed_at=device['status_time'],
                                                    source='IPC executor RS485')
            self.cached.update(device, wired_port='IPC executor', executor_reachable=True,
                               executor_observed_utc=time(), error=device.get('error'))
            self.observed = self.sampled = monotonic()
        return self.status()

    def _record_remote_events(self, key, device):
        for stream in ('log', 'events'):
            cursor = (*key, stream)
            last = self.remote_event_ids.get(cursor, 0)
            for entry in device.get(stream, []):
                if not isinstance(entry, dict) or type(entry.get('id')) is not int or entry['id'] <= last:
                    continue
                ident = entry['id']
                context = dict(connection_id=self.connection_id, executor_boot=key[0],
                               executor_connection_id=key[1], transport='wired')
                if ident > last + 1:
                    self.local.record_event('agv', 'gap', **context, stream=stream,
                                            first_missing=last+1, last_missing=ident-1)
                name = 'frame' if stream == 'log' else entry.get('field', 'status')
                fields = {k: entry[k] for k in ('id', 'timestamp', 'direction', 'hex', 'decode', 'field',
                                                'before', 'after', 'reason') if k in entry}
                self.local.record_event('agv', name, **fields, **context)
                self.remote_event_ids[cursor] = last = ident

    def _motion_reason(self, status):
        if status.get('evidence_faults'):
            return ' '.join(status['evidence_faults'].values())
        if self.cached.get('executor_health', {}).get('protocol') != PROTOCOL:
            return 'The executor protocol is incompatible. Cached status is read-only and local control is unavailable.'
        if self.identity is None or not status['lease_valid']:
            return 'The executor control lease is unavailable.'
        if status['recovery_required']:
            return (status.get('operator_check') or {}).get('reason', 'Complete the operator check before arming.')
        health = self.cached.get('executor_health', {})
        if health.get('manual_travel_until') is not None:
            if time() >= health['manual_travel_until']:
                return 'The manual travel window expired.'
        elif health.get('manual_travel_missing_evidence'):
            return 'Manual travel requires qualified robot pose, battery reserve, and clear tank and equipment-off feedback.'
        elif not health.get('motion_enabled'):
            return 'Phase 1 live travel is disabled.'
        return None

    def status(self):
        pending = self._pending_stop()
        manual = self.manual
        history = self.stop_intent['report'] if self.stop_intent else None
        with self.lock:
            self.sequence += 1
            age = self.cached.get('status_age_s')
            state = dict(deepcopy(self.cached), sequence=self.sequence,
                         status_age_s=None if age is None else age+monotonic()-self.sampled,
                         stop_unconfirmed=pending is not None, manual_visit=deepcopy(manual),
                         manual_visit_history=deepcopy(self.manual_history))
        if self.storage_check and not self.journal_fault:
            check = state.get('operator_check') or {}
            state.update(armed=False, motion_block=self.storage_check, operator_check=(
                dict(check, reason=self.storage_check) if check else dict(reason=self.storage_check,
                    technician=False, location=None, basket=None, step=None, run_id=self.storage_context['run_id'],
                    job_id=self.storage_context['job_id'] or (manual or {}).get('id'))))
        if self.journal_fault:
            state.update(armed=False, motion_block=self.journal_fault, operator_check=dict(
                reason=self.journal_fault, technician=True, location=None, basket=None, job_id=None, step=None))
        current = state.get('executor_stop')
        if pending:
            report = pending['report']
            if current and current.get('stop_id') == pending['id']:
                report = dict(current, confirmed=False)
            state.update(armed=False, motion_block='Stop unconfirmed. Verify a safe halt first.',
                         executor_stop=deepcopy(report))
        elif not (current and current.get('boot') == state.get('executor_boot') and
                  current.get('generation') <= state.get('stop_generation') and not state.get('armed')
                  and state.get('executor_reachable')):
            state['executor_stop'] = None
        state['executor_stop_history'] = deepcopy(history) if history != state['executor_stop'] else None
        if self.job_gateway and not state.get('armed'):
            state['armed_for'] = None
        return state

    def flag_storage_failure(self, reason, *, run_id=None, job_id=None):
        self.storage_context = dict(run_id=run_id, job_id=job_id)
        self.storage_check = reason

    def storage_failure(self, reason):
        self.flag_storage_failure(reason)
        self.stop()

    def _sync_storage_check(self, status, identity):
        if self.storage_check and identity and not self._pending_stop() and not status.get('operator_check'):
            run_id = self.storage_context['run_id']
            job_id = self.storage_context['job_id'] or (self.manual or {}).get('id')
            self._apply(self.client('hold', dict(identity, reason=self.storage_check, manual=run_id is None,
                                                run_id=run_id, job_id=job_id, idle=job_id is None,
                                                stop_generation=status['stop_generation'])))

    def _clear_storage_check(self):
        with self.stop_receipts:
            if self.stop_intent:
                updated = dict(self.stop_intent, storage_check=None)
                atomic_json(self.stop_path, updated, sync_directory=True)
                self.stop_intent = updated
            self.storage_check = None
            self.storage_context = dict(run_id=None, job_id=None)

    def _verify_manual_history(self):
        marker = self.manual_marker
        try:
            if self.manual_witness.is_symlink() or self._load(self.manual_witness) != marker:
                raise ValueError(self.manual_witness)
            if self.manual_history_directory.is_symlink():
                raise ValueError(self.manual_history_directory)
            if self.manual and marker and marker['committed'] and not self.manual_path.is_file():
                raise ValueError(self.manual_path)
            for ident, digest in (marker or {}).get('archives', {}).items():
                path = self.manual_history_directory / (ident + '.json')
                if path.is_symlink() or sha256(path.read_bytes()).hexdigest() != digest:
                    raise ValueError(path)
        except (OSError, ValueError) as exc:
            name = getattr(exc, 'filename', None) or str(exc)
            self.journal_fault = f'{name}: retained manual evidence is missing or inconsistent. Please call a technician.'

    def _write_manual(self, path, value):
        try:
            atomic_json(path, value, sync_directory=True)
        except OSError as exc:
            self.storage_failure(f'{path}: {exc}')
            raise

    def _record_manual(self, intent, result):
        if result is None or self.manual is None or self.manual['id'] != intent['id']:
            return
        if result.get('job_id') != intent['id']:
            raise ValueError('The returned executor job does not match the manual station visit.')
        previous = self.manual['result']
        if result['outcome'] in ('succeeded', 'held'):
            terminal = result.get('result') or {}
            hold = terminal.get('hold') or {}
            effects = (result.get('checkpoint') or {}).get('confirmed') or []
            arrival = effects[-1] if effects and isinstance(effects[-1], dict) else {}
            observed = arrival.get('evidence') or {}
            if (result['state'] != 'done' or terminal.get('outcome') != result['outcome'] or terminal.get('steps')
                    or hold.get('station') != intent['station'] or hold.get('stopped') is not True
                    or arrival.get('effect') != 'station' or arrival.get('value') != intent['station']
                    or not isinstance(observed, dict) or observed.get('station') != intent['station']
                    or observed.get('stopped') is not True):
                raise ValueError('The terminal station evidence does not confirm the requested visit.')
        if result['state'] not in PENDING_STATES and result['outcome'] != 'succeeded':
            raise ValueError(result.get('error') or 'The station visit ended in an uncertain hold.')
        if previous and previous['state'] not in PENDING_STATES:
            return
        updated = dict(self.manual, result=result)
        if result['state'] not in PENDING_STATES:
            self._write_manual(self.manual_path, updated)
        self.manual = updated

    def _manual_hold(self, intent, reason):
        updated = dict(intent, doubt=reason)
        self.manual = updated
        try:
            identity = self.pulse()
            status = self.client('status')
            self._apply(self.client('hold', dict(identity, reason=reason, job_id=intent['id'], manual=True,
                                                stop_generation=status['stop_generation'])))
        except (OSError, ValueError):
            self.stop()
        finally:
            self._write_manual(self.manual_path, updated)

    def manual_request(self, request_id):
        intent = self.manual
        if intent and intent.get('request_id') == request_id:
            return intent['id']
        for path in self.manual_history_directory.glob('*.json'):
            history = self._load(path)
            if history and history.get('request_id') == request_id:
                return history['id']
        return None

    @staticmethod
    def _same_manual_authority(intent, status):
        return all(intent.get(key) == status.get(key) for key in ('boot', 'session', 'stop_generation'))

    def _refresh_manual(self):
        intent = self.manual
        if intent is None:
            return
        previous = intent['result']
        if intent.get('doubt'):
            status = self.client('status')
            if not status.get('operator_check'):
                self._manual_hold(intent, intent['doubt'])
            return
        if previous and previous['state'] not in PENDING_STATES:
            return
        try:
            status = self.client('status', {'job_id': intent['id']})
        except JobEvidenceError as exc:
            with self.manual_lock:
                if self.manual is intent:
                    self._manual_hold(intent, str(exc))
            return
        with self.manual_lock:
            if self.manual is not intent:
                return
            if not self._same_manual_authority(intent, status) or status['job'] is None:
                self._manual_hold(intent, 'The station visit outcome is uncertain. Check the AGV station.')
            else:
                try:
                    self._record_manual(intent, status['job'])
                except ValueError as exc:
                    self._manual_hold(intent, str(exc))

    def _manual_visit(self, body):
        station = body.get('value')
        if type(station) is not int or not 1 <= station <= 255:
            raise ValueError('Choose a station from 1 to 255.')
        with self.manual_lock:
            intent = self.manual
            if intent:
                raise ValueError('Review the retained station visit before starting another.')
            state = self.status()
            if self.job_gateway and state.get('armed_for') != 'agv-page':
                raise ValueError('Select the AGV page as job owner and Arm for it before calling a station.')
            identity = self.pulse()
            health = state.get('executor_health', {})
            expires = health.get('manual_travel_until')
            attestation = None
            if expires is not None:
                now = time()
                if now >= expires:
                    raise ValueError('The manual travel window expired.')
                attestation = body.get('attestation')
                check_manual_attestation(attestation, station, now=now)
                check_manual_feedback(state, health['manual_travel_min_battery'], station)
                if (self.manual_history_directory / (attestation['id'] + '.json')).exists():
                    raise ValueError('This station confirmation has already been used.')
            elif not health.get('motion_enabled') or not health.get('simulated'):
                raise ValueError(state['motion_block'] or 'Phase 1 live station visits are disabled.')
            self._reviewed(body, identity)
            generation = body.get('stop_generation')
            if generation != state.get('stop_generation') or not state['armed'] or state['motion_block']:
                raise ValueError('Review the current executor status and arm before requesting a station.')
            intent = dict(id=uuid4().hex, station=station, boot=identity['boot'],
                          session=identity['session'], stop_generation=generation, result=None,
                          request_id=body.get('request_id'))
            if attestation is not None:
                intent.update(id=attestation['id'], attestation=attestation, attested_at=time())
            witness = dict(id=intent['id'], intent=intent, committed=False,
                           archives=(self.manual_marker or {}).get('archives', {}))
            self._write_manual(self.manual_witness, witness)
            self.manual_marker, self.manual = witness, intent
            self._write_manual(self.manual_path, intent)
            committed = dict(witness, committed=True)
            self._write_manual(self.manual_witness, committed)
            self.manual_marker = committed
            payload = dict(identity, job_id=intent['id'], stop_generation=generation, type='station_visit',
                           version=1, params={'station': station})
            if attestation is not None:
                payload['attestation'] = attestation
            try:
                result = self.request('submit', payload)
                self._record_manual(intent, result)
            except AdmissionRefused as exc:
                if attestation is not None:
                    try:
                        observed = self.request('status')
                    except (OSError, ValueError):
                        self._manual_hold(intent, str(exc))
                        raise
                    if self._same_manual_authority(intent, observed) and not observed['recovery_required']:
                        self._archive_manual(dict(intent, refused=str(exc)))
                    else:
                        self._manual_hold(intent, str(exc))
                else:
                    self._manual_hold(intent, str(exc))
                raise
            except (OSError, ValueError) as exc:
                self._manual_hold(intent, str(exc))
                raise
            return self.status()

    def _operator_check(self, body):
        identity = self.pulse()
        self._reviewed(body, identity)
        if self.journal_fault:
            raise ValueError(self.journal_fault)
        payload = {key: body[key] for key in
                   ('confirmed', 'choice', 'operator', 'location', 'basket', 'job_id', 'step',
                    'stop_generation') if key in body}
        with self.manual_lock:
            intent = self.manual
            check = self.request('status').get('operator_check') or {}
            if (not check or check.get('run_id') is not None or check.get('job_id') != payload.get('job_id')
                    or (intent and check.get('job_id') is not None and intent['id'] != payload.get('job_id')) or payload.get('choice') != 'abort'
                    or payload.get('basket') is not None):
                raise ValueError('Select the retained manual station visit and confirm its disposition.')
            state = self.request('operator_check', dict(identity, **payload))
            if not self._decision_applied(state, dict(identity, **payload)):
                raise ValueError('The executor did not confirm the operator decision.')
            if intent and intent['id'] == payload.get('job_id'):
                self._archive_manual(dict(intent, operator_check=payload, acknowledged=True))
            if self.storage_check:
                self._clear_storage_check()
        return self._apply(state)

    @staticmethod
    def _decision_applied(status, request):
        return (all(status.get(key) == request.get(key) for key in ('boot', 'session', 'stop_generation'))
                and status.get('lease_valid') is True and status.get('armed') is False
                and 'operator_check' in status and status['operator_check'] is None
                and status.get('recovery_required') is False)

    def _acknowledge_manual(self, job_id):
        with self.manual_lock:
            intent = self.manual
            if intent is None and self.manual_history and job_id == self.manual_history['id']:
                return self.status()
            result = (intent or {}).get('result') or {}
            if (not intent or job_id != intent['id'] or intent.get('doubt')
                    or result.get('state') != 'done' or result['outcome'] != 'succeeded'):
                raise ValueError('Confirm the outcome of the retained station visit first.')
            try:
                observed = self.request('status', {'job_id': job_id})
            except JobEvidenceError as exc:
                self._manual_hold(intent, str(exc))
                raise
            if observed.get('evidence_faults') or observed.get('job') != result:
                self._manual_hold(intent, 'The saved station result differs from the IPC evidence.')
                raise ValueError('Check the retained station visit against the IPC evidence.')
            self._archive_manual(dict(intent, acknowledged=True))
        return self.status()

    def _archive_manual(self, history):
        if self.manual_history_directory.is_symlink():
            raise ValueError('Manual visit history requires a regular directory.')
        path = self.manual_history_directory / (history['id'] + '.json')
        try:
            self.manual_history_directory.mkdir(exist_ok=True)
        except OSError as exc:
            self.storage_failure(f'{self.manual_history_directory}: {exc}')
            raise
        self._write_manual(path, history)
        try:
            digest = sha256(path.read_bytes()).hexdigest()
        except OSError as exc:
            self.storage_failure(f'{path}: {exc}')
            raise
        archives = dict((self.manual_marker or {}).get('archives', {}))
        archives[history['id']] = digest
        marker = dict(id=history['id'], intent=self.manual, archives=archives, committed=True)
        self._write_manual(self.manual_witness, marker)
        self.manual_marker = marker
        self._write_manual(self.manual_path, None)
        self.manual_history, self.manual = history, None

    @staticmethod
    def _reviewed(body, identity):
        if body.get('executor_boot') != identity['boot']:
            raise ValueError('The executor boot changed. Review its status before arming or submitting.')
        if body.get('executor_session') != identity['session']:
            raise ValueError('The controller session changed. Review its status before arming or submitting.')
        if type(body.get('stop_generation')) is not int:
            raise ValueError('Review the Stop generation before arming or submitting.')

    def select_owner(self, body):
        requested = body.get('owner')
        built_in = {'agv-page': self.client, 'calibration': self.calibration_client}
        if not self.job_gateway or requested is not None and (not isinstance(requested, str) or requested not in built_in):
            raise ValueError('Choose the AGV page or Calibration module as job owner.')
        if self.manual:
            raise ValueError('Resolve the retained station visit before changing the job owner.')
        identity = self.pulse()
        self._reviewed(body, identity)
        present = self.client('status')
        owner = present.get('owner')
        if body.get('stop_generation') != present['stop_generation'] or body.get('executor_owner') != owner:
            raise ValueError('The owner or Stop changed. Review the AGV page before selecting an owner.')
        if owner == requested:
            return self.status()
        if owner is not None:
            if owner not in built_in:
                raise ValueError(f'{owner} owns the executor. Ask that client to release control or use Stop.')
            self._apply(built_in[owner]('release', identity))
        if requested is not None:
            self._apply(built_in[requested]('claim', identity))
        return self.status()

    def submit(self, body):
        action, command = body.get('action'), body.get('command')
        if action == 'send' and command == 'stop':
            self.stop({'stop_id': body.get('request_id')})
            return self.status()
        if action == 'acknowledge_station':
            return self._acknowledge_manual(body.get('job_id'))
        if action == 'operator_check':
            return self._operator_check(body)
        with self.lock:
            connection_id = self.connection_id
        if action == 'connect' and body.get('mode') != 'wired':
            raise ValueError('This installation uses the IPC executor link.')
        if action != 'connect' and body.get('connection_id', connection_id) != connection_id:
            raise ValueError('The AGV connection changed. Refresh before sending another command.')
        if action == 'send' and command == 'call_station':
            return self._manual_visit(body)
        identity = self.pulse()
        if action == 'arm':
            if type(body.get('armed')) is not bool:
                raise ValueError('Choose Arm or Disarm.')
            action = 'arm' if body['armed'] else 'disarm'
            if action == 'arm':
                if self._pending_stop() or self.journal_fault:
                    raise ValueError(self.journal_fault or 'Finish the pending Stop before arming.')
                self._reviewed(body, identity)
                if self.job_gateway and body.get('executor_owner') != self.status().get('owner'):
                    raise ValueError('The job owner changed. Review the AGV page before arming.')
                identity['stop_generation'] = body['stop_generation']
        if action == 'send' and command == 'query_status':
            action = 'status_read'
        if action not in ('connect', 'disconnect', 'arm', 'disarm', 'status_read'):
            raise ValueError('Only executor job controls are available on this installation.')
        try:
            status = self.request(action, identity)
        except OSError as exc:
            raise ValueError(self.line_link.failure_message()) from exc
        return self._apply(status)

    @contextmanager
    def recording_context(self):
        with self.lock:
            state = self.cached
            yield (dict(connection_id=self.connection_id, executor_boot=state['executor_boot'],
                        settings=dict(state['settings']), executor_reachable=state['executor_reachable'],
                        observed_utc=state['executor_observed_utc'], age_s=monotonic()-self.observed)
                   if state.get('connected') else None)

    def state_sample(self):
        state = self.status()
        now, age = monotonic(), state.get('status_age_s')
        return (state.get('status'), state.get('status_time'), None if age is None else now-age,
                state['connected'], 'wired', bool(state.get('executor_job')),
                state.get('last_confirmed_station'),
                state.get('connection_started_at'), state.get('error'), now,
                state['connection_id'], state['sequence'])

    def cached_motion_feedback(self):
        state = self.status()
        if not state['connected']:
            return {}
        return dict(status=state.get('status'), status_time=state.get('status_time'),
                    status_age_s=state.get('status_age_s'), connection_id=state['connection_id'],
                    stop_generation=state.get('stop_generation'), mode='wired', executor_link=True,
                    movement_pending=bool(state.get('executor_job') or state.get('stop_unconfirmed')))

    def motion_feedback(self, *, poll=False):
        # Native-page refreshes must not transmit through the executor.
        return self.cached_motion_feedback()

    def close(self):
        self.closing.set()
        self.worker.join()
