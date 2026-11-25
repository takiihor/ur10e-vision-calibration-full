"""Katana's AGV page adapter. Browser traffic never renews the IPC lease."""
from concurrent.futures import Future
from copy import deepcopy
import json
from threading import Event, Lock, Thread
from time import monotonic
from uuid import uuid4

from .executor import HEARTBEAT_S, PROTOCOL
from .jobs import atomic_json


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
        self.stop_intent = self._load(self.stop_path)
        self.manual = self._load(self.manual_path)
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
        if operation in ('arm', 'submit') and self._pending_stop():
            raise ValueError('Stop is unconfirmed. Verify a safe halt before arming or submitting.')
        return self.client(operation, body)

    def _loop(self):
        while not self.closing.is_set():
            started = monotonic()
            try:
                stop = self._pending_stop()
                receipt = self._deliver_stop(stop) if stop else None
                if receipt is not None and not receipt['received']:
                    raise OSError('Stop delivery is unconfirmed.')
                meta = self.client('health')
                identity = None
                if (meta['protocol'] == PROTOCOL and meta['job_types'].get('station_visit') == 1 and
                        (receipt is None or receipt.get('boot', meta['boot']) == meta['boot'])):
                    binding = dict(boot=meta['boot'], session=self.session, protocol=PROTOCOL)
                    self.heartbeat_sequence += 1
                    try:
                        lease = self.client('heartbeat', dict(binding, sequence=self.heartbeat_sequence))
                        if lease['lease_valid']:
                            identity = binding
                    except ValueError:
                        # A retiring controller's lease still fences this session.
                        pass
                self._apply(self.client('status'), identity, meta)
                self._refresh_manual()
            except (OSError, ValueError):
                with self.lock:
                    self.identity = None
                    age = self.cached.get('status_age_s')
                    now = monotonic()
                    self.cached.update(armed=False, stale=True,
                                       status_age_s=None if age is None else age+now-self.sampled,
                                       error='Executor unavailable. Use the AGV e-stop if needed.')
                    self.sampled = now
            self.closing.wait(max(0, HEARTBEAT_S - (monotonic()-started)))

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
                          executor_job=status['job'], recovery_required=status['recovery_required'],
                          motion_block=self._motion_reason(status))
            self.cached.update(device, wired_port='IPC executor')
            self.sampled = monotonic()
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
        with self.lock:
            self.sequence += 1
            age = self.cached.get('status_age_s')
            state = dict(deepcopy(self.cached), sequence=self.sequence,
                         status_age_s=None if age is None else age+monotonic()-self.sampled,
                         stop_unconfirmed=pending is not None, manual_visit=deepcopy(manual),
                         executor_stop=deepcopy(self.stop_intent['report']) if self.stop_intent else None)
        stop = state['executor_stop']
        if pending:
            state.update(armed=False, motion_block='Stop unconfirmed. Verify a safe halt first.')
        elif (stop and stop.get('boot') == state.get('executor_boot') and
              stop['generation'] > state.get('stop_generation', -1)):
            state['armed'] = False
        return state

    def _record_manual(self, intent, result):
        if result is None or self.manual is None or self.manual['id'] != intent['id']:
            return
        previous = self.manual['result']
        if previous and (previous['state'] not in PENDING_STATES or result == previous):
            return
        updated = dict(self.manual, result=result)
        if result['state'] not in PENDING_STATES:
            atomic_json(self.manual_path, updated, sync_directory=True)
        self.manual = updated

    def _refresh_manual(self):
        intent = self.manual
        if intent is None or intent['result'] and intent['result']['state'] not in PENDING_STATES:
            return
        result = self.client('status', {'job_id': intent['id']})['job']
        with self.manual_lock:
            self._record_manual(intent, result)

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
                intent = dict(id=uuid4().hex, station=station, boot=identity['boot'], result=None)
                atomic_json(self.manual_path, intent, sync_directory=True)
                self.manual = intent
            elif intent['boot'] != identity['boot']:
                raise ValueError('The executor restarted. Reconcile the original station visit before retrying.')
            payload = dict(identity, job_id=intent['id'], stop_generation=generation, type='station_visit',
                           version=1, params={'station': station})
            result = self.request('submit', payload)
            self._record_manual(intent, result)
            return self.status()

    def _acknowledge_manual(self, job_id):
        with self.manual_lock:
            intent = self.manual
            if (intent is None or job_id != intent['id'] or not intent['result'] or
                    intent['result']['state'] in PENDING_STATES):
                raise ValueError('Review the terminal result of this station visit first.')
            result = intent['result']
            if result['outcome'] == 'unknown':
                raise ValueError('Reconcile the uncertain executor job first.')
            self.request('acknowledge', dict(self.pulse(), job_id=job_id))
            atomic_json(self.manual_path, None, sync_directory=True)
            self.manual = None
        return self.status()

    @staticmethod
    def _reviewed(body, identity):
        if body.get('executor_boot') != identity['boot']:
            raise ValueError('The executor boot changed. Review its status before arming or submitting.')
        if type(body.get('stop_generation')) is not int:
            raise ValueError('Review the Stop generation before arming or submitting.')

    def submit(self, body):
        action, command = body.get('action'), body.get('command')
        if action == 'send' and command == 'stop':
            report = self.stop()
            return dict(self.status(), executor_stop=report)
        if action == 'acknowledge_station':
            return self._acknowledge_manual(body.get('job_id'))
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
