"""Katana's AGV page adapter. Browser traffic never renews the IPC lease."""
from concurrent.futures import Future
from contextlib import contextmanager
from copy import deepcopy
import json
from threading import Event, Lock, Thread
from time import monotonic, time, time_ns
from uuid import uuid4

from .executor import AdmissionRefused, HEARTBEAT_S, PROTOCOL, finite, heartbeat_interval
from .jobs import ID, atomic_json


PENDING_STATES = {"accepted", "running", "waiting"}
NO_STOP_RECEIPT = dict(received=False, halt_attempted=False, confirmed=False)


class ExecutorLink:
    def __init__(self, client, local):
        self.client = client
        self.local = local
        self.session = uuid4().hex
        self.sequence = self.heartbeat_sequence = self.connection_id = 0
        self.device_identity = self.identity = None
        self.stop_path = local.owner.root / 'executor-stop.json'
        self.intent_order_path = local.owner.root / 'executor-intent-sequence.json'
        self.intent_order = self._load(self.intent_order_path) or 0
        self.intent_order_lock = Lock()
        self.manual_path = local.owner.root / 'executor-manual-visit.json'
        self.manual_history_directory = local.owner.root / 'executor-manual-history'
        self.recovery_path = local.owner.root / 'executor-recovery.json'
        self.recovery_history_directory = local.owner.root / 'executor-recovery-history'
        self.recovery_evidence = self._load(self.recovery_path)
        self.stop_intent = self._load(self.stop_path)
        self.stop_latch = {}
        if self.stop_intent and not self.stop_intent['report']['confirmed']:
            self.stop_latch['pending'] = self.stop_intent
        self.remote_event_ids = {}
        self.manual = self._load(self.manual_path)
        if self.manual_history_directory.is_symlink():
            raise ValueError('Manual visit history requires a regular directory.')
        latest = max(self.manual_history_directory.glob('*.json'),
                     key=lambda path: path.stat().st_mtime_ns, default=None)
        self.manual_history = self._load(latest) if latest else None
        self.stop_lock, self.manual_lock = Lock(), Lock()
        self.closing = Event()
        self.lock = Lock()
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

    def _pending_stop(self):
        return self.stop_latch.get('pending')

    def _deliver_stop(self, intent):
        try:
            report = self.client('stop', {'stop_id': intent['id']})
            if report.get('stop_id') != intent['id'] or not report.get('received'):
                raise ValueError('The Stop receipt does not match the pending request.')
        except (OSError, ValueError):
            report = dict(NO_STOP_RECEIPT)
        with self.stop_lock:
            current = self._pending_stop()
            if current and current['id'] == intent['id']:
                previous = current['report']
                if previous['received'] and (not report['received'] or
                        (previous.get('boot') == report.get('boot') and previous['halt_attempted']
                         and not report['halt_attempted'])):
                    report = previous
                updated = dict(current, report=report)
                if updated != self.stop_intent:
                    atomic_json(self.stop_path, updated, sync_directory=True)
                    self.stop_intent = updated
                if report['confirmed']:
                    del self.stop_latch['pending']
                else:
                    self.stop_latch['pending'] = updated
        return report

    def stop(self, context=None):
        context = context or {}
        key = context.get('stop_id')
        previous = self.stop_intent
        if key and previous and previous.get('request') == key and previous['report']['confirmed']:
            return previous['report']
        # Concurrent Stops share one retry identity until durable physical confirmation.
        job = self.cached.get('executor_job') or self.manual or {}
        proposal = dict(id=uuid4().hex, request=key, boot=self.cached.get('executor_boot'),
                        run_id=context.get('run_id'), job_id=context.get('job_id') or job.get('job_id') or job.get('id'),
                        report=dict(NO_STOP_RECEIPT))
        intent = self.stop_latch.setdefault('pending', proposal)
        delivery = Future()

        def send():
            try:
                delivery.set_result(self._deliver_stop(intent))
            except Exception as exc:
                delivery.set_exception(exc)

        Thread(target=send, daemon=True, name='katana-executor-stop').start()
        with self.stop_lock:
            if self._pending_stop() is intent and self.stop_intent != intent:
                atomic_json(self.stop_path, intent, sync_directory=True)
                self.stop_intent = intent
        return delivery.result()

    def pulse(self):
        with self.lock:
            if self.identity is None:
                raise ValueError('The executor control lease is unavailable.')
            return dict(self.identity)

    def request(self, operation, body=None):
        if operation == 'stop':
            return self.stop(body)
        if operation in ('arm', 'submit'):
            if self._pending_stop():
                raise ValueError('Stop is unconfirmed. Verify a safe halt before arming or submitting.')
            if self.recovery_evidence and not self.recovery_evidence['acknowledged']:
                raise ValueError('Acknowledge the executor recovery evidence first.')
        return self.client(operation, body)

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
                interval = heartbeat_interval(meta.get('heartbeat_s'), meta.get('lease_s'))
                identity = None
                if (meta['protocol'] == PROTOCOL and meta['job_types'].get('station_visit') == 1 and
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
                status = self.client('status')
                if status['boot'] != meta['boot']:
                    raise OSError('The executor boot changed while sampling status.')
                self._apply(status, identity, meta)
                self._refresh_manual()
            except (OSError, ValueError):
                with self.lock:
                    self.identity = None
                    age = self.cached.get('status_age_s')
                    now = monotonic()
                    self.cached.update(armed=False, stale=True, executor_reachable=False,
                                       status_age_s=None if age is None else age+now-self.sampled,
                                       executor_session=None,
                                       error='Executor unavailable. Use the AGV e-stop if needed.')
                    self.sampled = now
            self.closing.wait(max(0, interval - (monotonic()-started)))

    def _apply(self, status, identity=None, meta=None):
        device = dict(status['device'])
        with self.lock:
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
                          quality_alarm=status.get('quality_alarm'),
                          evidence_faults=status.get('evidence_faults', {}),
                          recovery_ids=status.get('recovery_ids', []), executor_stop=status.get('stop'),
                          motion_block=self._motion_reason(status))
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
        if self.identity is None or not status['lease_valid']:
            return 'The executor control lease is unavailable.'
        if status['recovery_required']:
            return 'Reconcile executor job evidence before arming.'
        if not self.cached.get('executor_health', {}).get('motion_enabled'):
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
                         manual_visit_history=deepcopy(self.manual_history),
                         executor_recovery_evidence=deepcopy(self.recovery_evidence))
        if self.recovery_evidence and not self.recovery_evidence['acknowledged']:
            state['motion_block'] = 'Finish the recorded executor recovery before arming.'
        current = state.get('executor_stop')
        if pending:
            report = pending['report']
            if current and current.get('stop_id') == pending['id']:
                report = dict(current, confirmed=False)
            state.update(armed=False, motion_block='Stop unconfirmed. Verify a safe halt first.',
                         executor_stop=deepcopy(report))
        else:
            if (history and history.get('boot') == state.get('executor_boot') and
                    history['generation'] > state.get('stop_generation', -1)):
                state.update(armed=False, stop_generation=history['generation'], executor_stop=deepcopy(history))
            elif not (current and current.get('boot') == state.get('executor_boot') and
                      current.get('generation') == state.get('stop_generation') and not state.get('armed')):
                state['executor_stop'] = None
            elif history and current.get('stop_id') == history.get('stop_id'):
                state['executor_stop'] = deepcopy(history)
        state['executor_stop_history'] = deepcopy(history) if history != state['executor_stop'] else None
        return state

    def _record_manual(self, intent, result):
        if result is None or self.manual is None or self.manual['id'] != intent['id']:
            return
        if result.get('job_id') != intent['id']:
            raise ValueError('The returned executor job does not match the manual station visit.')
        previous = self.manual['result']
        if result['state'] == 'refused':
            if previous is not None or result.get('outcome') != 'failed' or not result.get('error'):
                raise ValueError('The refusal contradicts the retained station visit.')
            self._archive_manual(dict(intent, result=result, admission_refused=True, acknowledged=False))
            return
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
        reconciled = result.get('reconciliation') and (not previous or not previous.get('reconciliation'))
        if previous and previous['state'] not in PENDING_STATES and not reconciled:
            if not self.manual.get('missing') or result != previous:
                return
        updated = dict(self.manual, result=result)
        if reconciled and previous and previous['outcome'] != 'unknown':
            updated['prior_result'] = previous
        updated.pop('missing', None)
        if result['state'] not in PENDING_STATES:
            atomic_json(self.manual_path, updated, sync_directory=True)
        self.manual = updated

    def _manual_missing(self, intent):
        if self.manual and self.manual['id'] == intent['id'] and not self.manual.get('missing'):
            updated = dict(self.manual, missing=True)
            atomic_json(self.manual_path, updated, sync_directory=True)
            self.manual = updated

    def manual_request(self, request_id):
        intent = self.manual
        if intent and intent.get('request_id') == request_id:
            return intent['id']
        for path in self.manual_history_directory.glob('*.json'):
            history = self._load(path)
            if history and history.get('request_id') == request_id:
                return history['id']
        return None

    def order_intent(self):
        with self.intent_order_lock:
            order = self.intent_order + 1
            atomic_json(self.intent_order_path, order, sync_directory=True)
            self.intent_order = order
            return order

    def latest_manual_intent(self):
        current = self.manual
        if current:
            return dict(job_id=current['id'], intent_order=current.get('intent_order', 0),
                        intent_utc_ns=current.get('intent_utc_ns') or self.manual_path.stat().st_mtime_ns)
        history = self.manual_history
        if history:
            return dict(job_id=history['id'], intent_order=history.get('intent_order', 0), intent_utc_ns=history.get('intent_utc_ns') or (
                self.manual_history_directory / f"{history['id']}.json").stat().st_mtime_ns)
        return None

    @staticmethod
    def _same_manual_authority(intent, status):
        return all(intent.get(key) == status.get(key) for key in ('boot', 'session', 'stop_generation'))

    def _refresh_manual(self):
        intent = self.manual
        if intent is None:
            return
        previous = intent['result']
        if (previous and previous['state'] not in PENDING_STATES and not intent.get('missing')
                and intent['boot'] == self.status().get('executor_boot')
                and (previous['outcome'] != 'unknown' or previous.get('reconciliation'))):
            return
        status = self.client('status', {'job_id': intent['id']})
        result = status['job']
        with self.manual_lock:
            self._record_manual(intent, result)
            if result is None and (previous or not self._same_manual_authority(intent, status)):
                self._manual_missing(intent)

    def _manual_visit(self, body):
        station = body.get('value')
        if type(station) is not int or not 1 <= station <= 255:
            raise ValueError('Choose a station from 1 to 255.')
        with self.manual_lock:
            intent = self.manual
            if intent:
                if station != intent['station']:
                    raise ValueError('Review the previous station visit before requesting a different visit.')
                result = self.client('status', {'job_id': intent['id']})['job']
                self._record_manual(intent, result)
                if result is not None or intent['result'] is not None:
                    return self.status()
            state = self.status()
            identity = self.pulse()
            if (not state.get('executor_health', {}).get('motion_enabled') or
                    not state.get('executor_health', {}).get('simulated')):
                raise ValueError('Phase 1 live station visits are disabled.')
            self._reviewed(body, identity)
            generation = body.get('stop_generation')
            if generation != state.get('stop_generation') or not state['armed'] or state['motion_block']:
                raise ValueError('Review the current executor status and arm before requesting a station.')
            if intent is None:
                intent = dict(id=uuid4().hex, station=station, boot=identity['boot'],
                              session=identity['session'], stop_generation=generation, result=None,
                              request_id=body.get('request_id'), intent_utc_ns=time_ns(), intent_order=self.order_intent())
                atomic_json(self.manual_path, intent, sync_directory=True)
                self.manual = intent
            elif not self._same_manual_authority(intent, dict(identity, stop_generation=generation)):
                raise ValueError('Executor authority changed. Reconcile the original station visit.')
            payload = dict(identity, job_id=intent['id'], stop_generation=generation, type='station_visit',
                           version=1, params={'station': station})
            try:
                result = self.request('submit', payload)
            except AdmissionRefused as exc:
                self._record_manual(intent, dict(job_id=intent['id'], state='refused', outcome='failed', error=str(exc)))
                raise
            self._record_manual(intent, result)
            return self.status()

    def _reconcile_manual(self, job_id, note, *, orphan=False, programme_intent=None):
        if not isinstance(job_id, str) or not ID.fullmatch(job_id):
            raise ValueError('Supply the original executor job ID.')
        if not isinstance(note, str) or not note.strip() or len(note) > 500:
            raise ValueError('Enter the verified station disposition.')
        note = note.strip()
        with self.manual_lock:
            intent = self.manual
            if (orphan and intent) or (not orphan and (not intent or intent['id'] != job_id)):
                raise ValueError('Select the original station visit to reconcile.')
            journal = self.recovery_evidence or {}
            pending = orphan and journal.get('acknowledged') is False
            if pending and (journal['job_id'] != job_id or
                            journal.get('disposition', (journal.get('observation') or
                                                        journal.get('reconciliation') or {}).get('disposition')) != note):
                raise ValueError('Retry the original job ID and recorded executor recovery disposition.')
            latest = max((record for record in (self.latest_manual_intent(), programme_intent) if record),
                         key=lambda record: (record['intent_order'], record['intent_utc_ns']), default=None)
            if orphan and not pending and (not latest or latest['job_id'] != job_id):
                raise ValueError('Supply the latest unresolved controller job ID before reconciliation.')
            identity = self.pulse()
            status = self.request('status', {'job_id': job_id})
            if status.get('evidence_faults'):
                raise ValueError(' '.join(status['evidence_faults'].values()))
            result = status['job']
            missing = result is None
            recovery_ids = status.get('recovery_ids', [])
            repair = '.executor-active.json' in recovery_ids
            if orphan and not pending:
                if not repair and job_id not in recovery_ids:
                    raise ValueError('Select an unresolved original executor job.')
                if any(status.get(key) not in (None, job_id) for key in ('active_job_id', 'latest_job_id')):
                    raise ValueError('The requested job does not account for the active executor record.')
            if not orphan and missing and not repair and not intent.get('missing') and self._same_manual_authority(intent, status):
                raise ValueError('Wait for the original station visit evidence.')
            observed = bool(orphan and result and result['state'] == 'done'
                            and result['outcome'] != 'unknown' and result['result'].get('hold'))
            payload = journal.get('intent') if pending else (intent or {}).get('reconcile_intent')
            if payload and payload['disposition'] != note:
                raise ValueError('Retry the original job ID and recorded executor recovery disposition.')
            if payload is None:
                if not (missing or observed or
                        (result['outcome'] == 'unknown' and result['state'] not in PENDING_STATES)):
                    raise ValueError('Select an uncertain station visit or a terminal hold.')
                payload = dict(job_id=job_id, missing=missing, disposition=note,
                               repair_active_marker=repair, observed_only=observed,
                               observation_id=journal.get('event_id') if pending else uuid4().hex)
                if intent:
                    payload.update(intent_boot=intent['boot'], intent_stop_generation=intent['stop_generation'])
                if latest and latest['job_id'] == job_id and latest.get('run_id') is not None:
                    payload.update(run_id=latest['run_id'], basket=latest['basket'])
            event_id = payload['observation_id']
            key = 'observation' if payload['observed_only'] else 'reconciliation'
            saved = (result or {}).get(key) or {}
            if orphan:
                journal = dict(journal if pending else {}, job_id=job_id, disposition=note,
                               event_id=event_id, intent=payload, acknowledged=False)
                atomic_json(self.recovery_path, journal, sync_directory=True)
                self.recovery_evidence = journal
            elif not intent.get('reconcile_intent'):
                intent = dict(intent, reconcile_intent=payload)
                atomic_json(self.manual_path, intent, sync_directory=True)
                self.manual = intent
            if saved.get('observation_id') == event_id and saved.get('boot') == identity['boot']:
                evidence = result
            else:
                evidence = self.request('reconcile', dict(identity, **payload))
            reconciliation = evidence.get(key) or {}
            hold = reconciliation.get('hold') or {}
            if (evidence.get('job_id') != job_id
                    or (not payload['observed_only'] and evidence.get('outcome') != 'unknown')
                    or reconciliation.get('boot') != identity['boot'] or reconciliation.get('disposition') != note
                    or reconciliation.get('observation_id') != event_id
                    or (payload.get('repair_active_marker') and reconciliation.get('repaired_active_marker') is not True)
                    or not finite(reconciliation.get('observed_utc')) or not finite(reconciliation.get('observed_mono_s'))
                    or hold.get('stopped') is not True or hold.get('equipment_on') is not False
                    or (payload.get('run_id') is not None
                        and (hold.get('run_id'), hold.get('basket')) != (payload['run_id'], payload['basket']))):
                raise OSError('The executor did not return verified evidence for this reconciliation.')
            if intent:
                self._record_manual(intent, evidence)
            else:
                if self.recovery_history_directory.is_symlink():
                    raise ValueError('Executor recovery history requires a regular directory.')
                self.recovery_history_directory.mkdir(exist_ok=True)
                journal = dict(journal, **evidence)
                journal['acknowledged'] = False
                atomic_json(self.recovery_path, journal, sync_directory=True)
                self.recovery_evidence = journal
                self._acknowledge(job_id)
                journal = dict(journal, acknowledged=True)
                atomic_json(self.recovery_history_directory / f'{event_id}.json', journal, sync_directory=True)
                atomic_json(self.recovery_path, journal, sync_directory=True)
                self.recovery_evidence = journal
        return self.status()

    def _acknowledge(self, job_id):
        reply = self.request('acknowledge', dict(self.pulse(), job_id=job_id))
        if reply != {'acknowledged': True, 'job_id': job_id}:
            raise OSError('Waiting for acknowledgement of the original executor job.')

    def _acknowledge_manual(self, job_id):
        with self.manual_lock:
            intent = self.manual
            if intent is None and isinstance(job_id, str) and ID.fullmatch(job_id):
                history = self._load(self.manual_history_directory / f'{job_id}.json')
                if history and history.get('acknowledged'):
                    return self.status()
            if (intent is None or job_id != intent['id'] or not intent['result'] or
                    intent['result']['state'] in PENDING_STATES):
                raise ValueError('Review the terminal result of this station visit first.')
            result = intent['result']
            if result['outcome'] == 'unknown' and not result.get('reconciliation'):
                raise ValueError('Reconcile the uncertain executor job first.')
            status = self.request('status', {'job_id': job_id})
            if status.get('evidence_faults'):
                raise ValueError(' '.join(status['evidence_faults'].values()))
            observed = status['job']
            if observed is None:
                self._manual_missing(intent)
                raise ValueError('The IPC result is missing. Reconcile the original station visit.')
            if any(observed.get(key) != result.get(key) for key in (
                    'job_id', 'state', 'outcome', 'checkpoint', 'result', 'error', 'reconciliation')):
                raise ValueError('The executor reply conflicts with the retained station evidence.')
            try:
                self._acknowledge(job_id)
            except ValueError:
                if self.request('status', {'job_id': job_id})['job'] is None:
                    self._manual_missing(intent)
                raise
            self._archive_manual(dict(intent, acknowledged=True))
        return self.status()

    def _archive_manual(self, history):
        if self.manual_history_directory.is_symlink():
            raise ValueError('Manual visit history requires a regular directory.')
        self.manual_history_directory.mkdir(exist_ok=True)
        atomic_json(self.manual_history_directory / (history['id'] + '.json'), history, sync_directory=True)
        atomic_json(self.manual_path, None, sync_directory=True)
        self.manual_history, self.manual = history, None

    @staticmethod
    def _reviewed(body, identity):
        if body.get('executor_boot') != identity['boot']:
            raise ValueError('The executor boot changed. Review its status before arming or submitting.')
        if body.get('executor_session') != identity['session']:
            raise ValueError('The controller session changed. Review its status before arming or submitting.')
        if type(body.get('stop_generation')) is not int:
            raise ValueError('Review the Stop generation before arming or submitting.')

    def submit(self, body, *, programme_intent=None):
        action, command = body.get('action'), body.get('command')
        if action == 'send' and command == 'stop':
            self.stop({'stop_id': body.get('request_id')})
            return self.status()
        if action == 'acknowledge_station':
            return self._acknowledge_manual(body.get('job_id'))
        if action in ('reconcile_station', 'repair_executor_marker'):
            return self._reconcile_manual(body.get('job_id'), body.get('note'),
                                          orphan=action == 'repair_executor_marker', programme_intent=programme_intent)
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
                if self._pending_stop() or (self.recovery_evidence and not self.recovery_evidence['acknowledged']):
                    raise ValueError('Finish the pending Stop or executor recovery before arming.')
                self._reviewed(body, identity)
                identity['stop_generation'] = body['stop_generation']
        if action in ('connect', 'disconnect', 'arm', 'disarm'):
            return self._apply(self.request(action, identity))
        if action == 'send' and command == 'query_status':
            return self._apply(self.request('status_read', identity))
        raise ValueError('Only executor job controls are available on this installation.')

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
