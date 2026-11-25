"""Katana's AGV page adapter. Browser traffic never renews the IPC lease."""
from concurrent.futures import Future
from contextlib import contextmanager
from copy import deepcopy
import json
from threading import Event, Lock, Thread
from time import monotonic, time
from uuid import uuid4

from .executor import HEARTBEAT_S, PROTOCOL, finite, heartbeat_interval
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
        self.manual_path = local.owner.root / 'executor-manual-visit.json'
        self.manual_history_directory = local.owner.root / 'executor-manual-history'
        self.recovery_path = local.owner.root / 'executor-recovery.json'
        self.recovery_history_directory = local.owner.root / 'executor-recovery-history'
        self.recovery_evidence = self._load(self.recovery_path)
        self.stop_intent = self._load(self.stop_path)
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
        intent = self.stop_intent
        return intent if intent and not intent['report']['confirmed'] else None

    def _deliver_stop(self, intent):
        try:
            report = self.client('stop', {'stop_id': intent['id']})
            if report.get('stop_id') != intent['id'] or not report.get('received'):
                raise ValueError('The Stop receipt does not match the pending request.')
        except (OSError, ValueError):
            return dict(NO_STOP_RECEIPT)
        with self.stop_lock:
            current = self.stop_intent
            if current and current['id'] == intent['id'] and not current['report']['confirmed']:
                if report != current['report']:
                    updated = dict(current, report=report)
                    atomic_json(self.stop_path, updated, sync_directory=True)
                    self.stop_intent = updated
        return report

    def stop(self, context=None):
        context = context or {}
        key = context.get('stop_id')
        delivery = None
        with self.stop_lock:
            intent = self.stop_intent
            if key is not None and intent and intent.get('request') == key:
                if intent['report']['confirmed']:
                    return intent['report']
            elif not self._pending_stop():
                state = self.status()
                job = state.get('executor_job') or state.get('manual_visit') or {}
                intent = dict(id=uuid4().hex, request=key, boot=state.get('executor_boot'),
                              run_id=context.get('run_id'),
                              job_id=context.get('job_id') or job.get('job_id') or job.get('id'),
                              report=dict(NO_STOP_RECEIPT))
                # Latch now. Send while syncing the intent, then sync the reply in order.
                self.stop_intent = intent
                delivery = Future()

                def send():
                    try:
                        delivery.set_result(self._deliver_stop(intent))
                    except Exception as exc:
                        delivery.set_exception(exc)

                Thread(target=send, daemon=True, name='katana-executor-stop').start()
                atomic_json(self.stop_path, intent, sync_directory=True)
        return delivery.result() if delivery is not None else self._deliver_stop(intent)

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
            self.sequence += 1
            device.update(connection_id=self.connection_id, sequence=self.sequence,
                          armed=status['armed'], stop_generation=status['stop_generation'],
                          executor_boot=status['boot'],
                          executor_session=status.get('session') if self.identity else None,
                          executor_job=status['job'], recovery_required=status['recovery_required'],
                          recovery_ids=status.get('recovery_ids', []), executor_stop=status.get('stop'),
                          motion_block=self._motion_reason(status))
            self.cached.update(device, wired_port='IPC executor', executor_reachable=True,
                               executor_observed_utc=time(), error=device.get('error'))
            self.observed = self.sampled = monotonic()
        return self.status()

    def _motion_reason(self, status):
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
        if previous and (result == previous or (previous['state'] not in PENDING_STATES
                and not (previous['outcome'] == 'unknown' and not previous.get('reconciliation')
                         and result['outcome'] == 'unknown' and result.get('reconciliation')))):
            return
        updated = dict(self.manual, result=result)
        updated.pop('missing', None)
        if result['state'] not in PENDING_STATES:
            atomic_json(self.manual_path, updated, sync_directory=True)
        self.manual = updated

    @staticmethod
    def _same_manual_authority(intent, status):
        return all(intent.get(key) == status.get(key) for key in ('boot', 'session', 'stop_generation'))

    def _refresh_manual(self):
        intent = self.manual
        if intent is None:
            return
        previous = intent['result']
        if (previous and previous['state'] not in PENDING_STATES and
                (previous['outcome'] != 'unknown' or previous.get('reconciliation'))):
            return
        status = self.client('status', {'job_id': intent['id']})
        result = status['job']
        with self.manual_lock:
            self._record_manual(intent, result)
            if (result is None and not self._same_manual_authority(intent, status) and
                    self.manual and self.manual['id'] == intent['id'] and not self.manual.get('missing')):
                self.manual = dict(self.manual, missing=True)
                atomic_json(self.manual_path, self.manual, sync_directory=True)

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
                              session=identity['session'], stop_generation=generation, result=None)
                atomic_json(self.manual_path, intent, sync_directory=True)
                self.manual = intent
            elif not self._same_manual_authority(intent, dict(identity, stop_generation=generation)):
                raise ValueError('Executor authority changed. Reconcile the original station visit.')
            payload = dict(identity, job_id=intent['id'], stop_generation=generation, type='station_visit',
                           version=1, params={'station': station})
            result = self.request('submit', payload)
            self._record_manual(intent, result)
            return self.status()

    def _reconcile_manual(self, job_id, note, *, orphan=False, programme_job=False):
        if not isinstance(job_id, str) or not ID.fullmatch(job_id):
            raise ValueError('Supply the original executor job ID.')
        if not isinstance(note, str) or not note.strip() or len(note) > 500:
            raise ValueError('Enter the verified station disposition.')
        note = note.strip()
        with self.manual_lock:
            intent = self.manual
            if (orphan and intent) or (not orphan and (not intent or intent['id'] != job_id)):
                raise ValueError('Select the original station visit to reconcile.')
            if orphan and not programme_job:
                history = self._load(self.manual_history_directory / f'{job_id}.json')
                if not history or history.get('id') != job_id or not history.get('acknowledged'):
                    raise ValueError('Restore the original controller job evidence before reconciliation.')
            identity = self.pulse()
            status = self.request('status', {'job_id': job_id})
            result = status['job']
            missing = result is None
            recovery_ids = status.get('recovery_ids', [])
            repair = '.executor-active.json' in recovery_ids
            if not orphan and missing and not repair and self._same_manual_authority(intent, status):
                raise ValueError('Wait for the original station visit evidence.')
            observed = bool(orphan and result and result['state'] == 'done'
                            and result['outcome'] != 'unknown' and result['result'].get('hold'))
            key = 'observation' if observed else 'reconciliation'
            previous = (result or {}).get(key) or {}
            prior_event = self.recovery_evidence or {}
            pending = orphan and prior_event.get('acknowledged') is False
            if pending and prior_event['job_id'] != job_id:
                raise ValueError('Acknowledge the pending executor recovery evidence first.')
            pending_evidence = (prior_event.get('observation') or prior_event.get('reconciliation') or {}) if pending else {}
            if pending and pending_evidence.get('disposition') != note:
                raise ValueError('Retry the disposition already recorded for this executor job.')
            retry = orphan and previous.get('disposition') == note
            if orphan and not repair and job_id not in recovery_ids and not (retry or pending):
                raise ValueError('Select an unresolved original executor job.')
            observation_id = None
            if orphan:
                if pending:
                    observation_id = prior_event['event_id']
                elif retry:
                    observation_id = previous.get('observation_id')
                if observation_id is None:
                    observation_id = uuid4().hex
            # A lost response can leave the IPC evidence committed before Katana acknowledged it.
            if retry and not repair and previous.get('boot') == identity['boot']:
                evidence = result
            else:
                if not (missing or observed or
                        (result['outcome'] == 'unknown' and result['state'] not in PENDING_STATES)):
                    raise ValueError('Select an uncertain station visit or a terminal hold.')
                was_missing = retry and not observed and bool(result['result'].get('reconciliation'))
                evidence = self.request('reconcile', dict(identity, job_id=job_id, missing=missing or was_missing,
                                        disposition=note, repair_active_marker=repair or (
                                            retry and previous.get('repaired_active_marker') is True),
                                        observed_only=observed, observation_id=observation_id))
            reconciliation = evidence.get(key) or {}
            hold = reconciliation.get('hold') or {}
            outcome = result['outcome'] if observed else 'unknown'
            if (evidence.get('job_id') != job_id or evidence.get('outcome') != outcome
                    or reconciliation.get('boot') != identity['boot'] or reconciliation.get('disposition') != note
                    or (orphan and reconciliation.get('observation_id') != observation_id)
                    or not finite(reconciliation.get('observed_utc')) or not finite(reconciliation.get('observed_mono_s'))
                    or hold.get('stopped') is not True or hold.get('equipment_on') is not False):
                raise ValueError('The executor did not return verified evidence for this reconciliation.')
            if intent:
                self._record_manual(intent, evidence)
            else:
                if self.recovery_history_directory.is_symlink():
                    raise ValueError('Executor recovery history requires a regular directory.')
                self.recovery_history_directory.mkdir(exist_ok=True)
                saved = dict(evidence, acknowledged=False, event_id=observation_id)
                atomic_json(self.recovery_path, saved, sync_directory=True)
                self.recovery_evidence = saved
                self.request('acknowledge', dict(self.pulse(), job_id=job_id))
                saved = dict(saved, acknowledged=True)
                atomic_json(self.recovery_history_directory / f'{observation_id}.json', saved, sync_directory=True)
                atomic_json(self.recovery_path, saved, sync_directory=True)
                self.recovery_evidence = saved
        return self.status()

    def _acknowledge_manual(self, job_id):
        with self.manual_lock:
            intent = self.manual
            if (intent is None or job_id != intent['id'] or not intent['result'] or
                    intent['result']['state'] in PENDING_STATES):
                raise ValueError('Review the terminal result of this station visit first.')
            result = intent['result']
            if result['outcome'] == 'unknown' and not result.get('reconciliation'):
                raise ValueError('Reconcile the uncertain executor job first.')
            self.request('acknowledge', dict(self.pulse(), job_id=job_id))
            history = dict(intent, acknowledged=True)
            if self.manual_history_directory.is_symlink():
                raise ValueError('Manual visit history requires a regular directory.')
            self.manual_history_directory.mkdir(exist_ok=True)
            atomic_json(self.manual_history_directory / f'{job_id}.json', history, sync_directory=True)
            atomic_json(self.manual_path, None, sync_directory=True)
            self.manual_history, self.manual = history, None
        return self.status()

    @staticmethod
    def _reviewed(body, identity):
        if body.get('executor_boot') != identity['boot']:
            raise ValueError('The executor boot changed. Review its status before arming or submitting.')
        if body.get('executor_session') != identity['session']:
            raise ValueError('The controller session changed. Review its status before arming or submitting.')
        if type(body.get('stop_generation')) is not int:
            raise ValueError('Review the Stop generation before arming or submitting.')

    def submit(self, body, *, programme_job=False):
        action, command = body.get('action'), body.get('command')
        if action == 'send' and command == 'stop':
            self.stop()
            return self.status()
        if action == 'acknowledge_station':
            return self._acknowledge_manual(body.get('job_id'))
        if action in ('reconcile_station', 'repair_executor_marker'):
            return self._reconcile_manual(body.get('job_id'), body.get('note'),
                                          orphan=action == 'repair_executor_marker', programme_job=programme_job)
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
