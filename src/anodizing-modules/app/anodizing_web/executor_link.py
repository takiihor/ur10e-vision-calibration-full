"""Katana's AGV page adapter. Browser traffic never renews the IPC lease."""
from copy import deepcopy
from threading import Event, Lock, Thread
from time import monotonic
from uuid import uuid4

from .executor import HEARTBEAT_S, PROTOCOL


class ExecutorLink:
    def __init__(self, client, local):
        self.client = client
        self.local = local
        self.session = uuid4().hex
        self.sequence = self.heartbeat_sequence = self.connection_id = 0
        self.device_identity = self.identity = None
        self.stop_pending = False
        self.generation = None
        self.closing = Event()
        self.lock = Lock()
        self.cached = local.commissioning(ports=True)
        self.cached.update(ports=[], serial_available=True, wired_port='IPC executor',
                           executor_link=True, motion_block='Waiting for executor capabilities.')
        self.sampled = monotonic()
        self.worker = Thread(target=self._loop, daemon=True, name='agv-executor-heartbeat')
        self.worker.start()

    def _loop(self):
        while not self.closing.is_set():
            started = monotonic()
            try:
                if self.stop_pending:
                    self.stop_pending = not self.client('stop', {})['confirmed']
                meta = self.client('health')
                identity = dict(boot=meta['boot'], session=self.session, protocol=PROTOCOL)
                if meta['protocol'] == PROTOCOL and meta['job_types'].get('station_visit') == 1:
                    self.heartbeat_sequence += 1
                    self.client('heartbeat', dict(identity, sequence=self.heartbeat_sequence))
                else:
                    identity = None
                self._apply(self.client('status'), identity, meta)
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
            self.generation = status['stop_generation']
            key = status['boot'], device.get('connection_id')
            if key != self.device_identity:
                self.connection_id += 1
                self.device_identity = key
            self.sequence += 1
            device.update(connection_id=self.connection_id, sequence=self.sequence,
                          armed=status['armed'], stop_generation=self.generation,
                          executor_job=status['job'], recovery_required=status['recovery_required'],
                          motion_block=self._motion_reason(status))
            self.cached.update(device, wired_port='IPC executor')
            self.sampled = monotonic()
            return deepcopy(self.cached)

    def _motion_reason(self, status):
        if self.identity is None or not status['lease_valid']:
            return 'The executor control lease is unavailable.'
        if status['recovery_required']:
            return 'Reconcile executor job evidence before arming.'
        if not self.cached.get('executor_health', {}).get('motion_enabled'):
            return 'Phase 1 live travel is disabled.'
        return None

    def status(self):
        with self.lock:
            self.sequence += 1
            age = self.cached.get('status_age_s')
            return dict(deepcopy(self.cached), sequence=self.sequence,
                        status_age_s=None if age is None else age+monotonic()-self.sampled,
                        stop_unconfirmed=self.stop_pending)

    def submit(self, body):
        action, command = body.get('action'), body.get('command')
        if action == 'send' and command == 'stop':
            self.stop_pending = True
            try:
                report = self.client('stop', {})
                self.stop_pending = not report['confirmed']
            except (OSError, ValueError):
                report = dict(received=False, halt_attempted=False, confirmed=False)
            return dict(self.status(), executor_stop=report)
        with self.lock:
            identity = self.identity
            generation, connection_id = self.generation, self.connection_id
        if identity is None:
            raise ValueError('The executor control lease is unavailable.')
        if action == 'connect' and body.get('mode') != 'wired':
            raise ValueError('This installation uses the IPC executor link.')
        if action != 'connect' and body.get('connection_id', connection_id) != connection_id:
            raise ValueError('The AGV connection changed. Refresh before sending another command.')
        if action == 'arm':
            if type(body.get('armed')) is not bool:
                raise ValueError('Choose Arm or Disarm.')
            action = 'arm' if body['armed'] else 'disarm'
        if action in ('connect', 'disconnect', 'arm', 'disarm'):
            return self._apply(self.client(action, identity))
        if action == 'send' and command == 'query_status':
            return self._apply(self.client('status_read', identity))
        if action == 'send' and command == 'call_station':
            job = self.client('submit', dict(identity, job_id=body.get('request_id'),
                              stop_generation=body.get('stop_generation'), type='station_visit',
                              version=1, params={'station': body.get('value')}))
            return dict(self.status(), executor_job=job)
        raise ValueError('Only executor job controls are available on this installation.')

    def state_sample(self):
        state = self.status()
        now, age = monotonic(), state.get('status_age_s')
        station = (state.get('status') or {}).get('current_station')
        return (state.get('status'), state.get('status_time'), None if age is None else now-age,
                state['connected'], 'wired', bool(state.get('executor_job')),
                (station, state.get('status_time')) if station is not None else None,
                state.get('connection_started_at'), state.get('error'), now,
                state['connection_id'], state['sequence'])

    def close(self):
        self.closing.set()
        self.worker.join()
