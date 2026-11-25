"""Separate HTTP surfaces for authenticated job control and cached IPC display."""
import fcntl
import hmac
from http.client import HTTPConnection
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
from threading import Event, Lock, Thread
from time import monotonic
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

from . import agv
from .executor import MAX_BODY, Executor, FakeDevices, Stopped, finite
from .recipe_agv import POSE_FRESH_S, StationVisit, stopped

CONTROLS = {'connect', 'disconnect', 'status_read', 'arm', 'disarm', 'submit', 'heartbeat',
            'stop', 'acknowledge', 'reconcile'}
READS = {'health', 'status'}


class WiredDevices:
    job_types = {'station_visit': 1}
    simulated = False

    def __init__(self, root, trace, *, pose=None, reserve=None):
        owner = SimpleNamespace(root=root, lock=Lock(), closing=False, active=None,
                                storage_error=None, execution_uncertain=False)
        self.pose, self.reserve = pose, reserve
        self.simulated = pose is not None and reserve is not None
        self.cancel = Event()
        self.link = agv.Connection(owner, commissioning_arm=True, wire_trace=trace,
                                   wired_station_jobs=self.simulated)

    def _pose_stamp(self):
        stamp = self.pose()
        if not finite(stamp) or not 0 <= monotonic() - stamp < POSE_FRESH_S:
            raise ValueError('Robot travel pose evidence is missing or stale.')
        return stamp

    def check(self, job_type, params):
        if not self.simulated:
            raise ValueError('Phase 1 blocks live travel and dips pending qualified local interlocks.')
        if not self.reserve():
            raise ValueError('Insufficient battery reserve for travel and recovery.')
        self._pose_stamp()

    def perform(self, effect, value):
        if effect != 'station':
            raise ValueError('The wired handler accepts only a station visit.')
        state = self.status()
        visit = StationVisit(self.link, self.cancel.set, connection_id=state['connection_id'],
                             stop_generation=state['stop_generation'], allow_wired=True)
        with visit.watch():
            try:
                while not visit.ready.wait(.05):
                    if self.cancel.is_set():
                        raise Stopped
                if visit.error:
                    raise ValueError(visit.error)
                if visit.station == value:
                    visit.require_endpoint(value)
                    return self.hold()
                while not visit.start_leg(value, 1500, 30, self._pose_stamp(), self.cancel):
                    if self.cancel.wait(.05):
                        raise Stopped
                while not self.cancel.wait(.05):
                    if not self.reserve():
                        raise ValueError('Battery reserve became inadequate during travel.')
                    progress = visit.progress(self._pose_stamp())
                    if progress.get('arrival_s') is not None:
                        return dict(self.hold(), acknowledgement_s=progress['ack_s'], arrival_s=progress['arrival_s'])
                raise Stopped
            except BaseException:
                visit.abort()
                raise

    def hold(self):
        state = self.status()
        report = state['status'] or {}
        if (state['status_age_s'] is None or state['status_age_s'] >= agv.RECIPE_STALE_S
                or not stopped(report) or report.get('obstacle_stop') is not False or report.get('fault_code') != 0):
            raise ValueError('Stopped station hold is unverified.')
        return dict(location='station', station=report['current_station'], stopped=True)

    def connect(self):
        return self.link.submit({'action': 'connect', 'mode': 'wired'})

    def read(self):
        return self.link.submit(dict(action='send', command='query_status'))

    def arm(self):
        result = self.link.submit(dict(action='arm', armed=True, connection_id=self.link.id))
        self.cancel.clear()
        return result

    def disarm(self):
        with self.link.lock:
            self.link.armed = False

    def stop(self):
        self.cancel.set()
        self.disarm()
        return self.link.recipe_stop() if self.simulated else False

    def stop_evidence(self, requested_at):
        with self.link.lock:
            report = self.link.status or {}
            stamp = self.link.status_at
            return bool(self.link.transport is not None and stamp is not None and
                        requested_at < stamp and 0 <= monotonic() - stamp < agv.RECIPE_STALE_S and
                        stopped(report) and report.get('fault_code') == 0 and report.get('obstacle_stop') is False)

    def status(self):
        return self.link.commissioning()

    def close(self):
        self.link.close()


class WireLog:
    def __init__(self, path):
        self.lock = Lock()
        fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        self.file = os.fdopen(fd, 'a', buffering=1)

    def __call__(self, direction, raw):
        with self.lock:
            self.file.write(json.dumps(dict(utc=agv.timestamp(), direction=direction, hex=raw.hex())) + '\n')
            self.file.flush()
            os.fsync(self.file.fileno())

    def close(self):
        self.file.close()


class Loopback(ThreadingHTTPServer):
    daemon_threads = False

    def __init__(self, port, handler):
        super().__init__(('127.0.0.1', port), handler)
        self.hosts = {f'{name}:{self.server_port}' for name in ('127.0.0.1', 'localhost')}


class Reply(BaseHTTPRequestHandler):
    def setup(self):
        super().setup()
        self.connection.settimeout(8)

    def log_message(self, *args):
        pass

    def send(self, code, value, mime='application/json'):
        body = value if isinstance(value, bytes) else json.dumps(value, allow_nan=False).encode()
        self.send_response(code)
        self.send_header('Content-Type', mime)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Content-Security-Policy', "default-src 'self'; frame-ancestors 'none'; base-uri 'none'")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def allowed_host(self):
        host = self.headers.get('Host')
        return host in self.server.hosts and self.headers.get('Origin') in (None, f'http://{host}')


def control_listener(port, executor, token):
    if not isinstance(token, str) or len(token) < 32 or not token.isascii():
        raise ValueError('Provide a private executor service token of at least 32 ASCII characters.')

    class Control(Reply):
        def handle_request(self, method):
            provided = self.headers.get('Authorization', '').encode('utf-8')
            if not self.allowed_host() or not hmac.compare_digest(provided, ('Bearer ' + token).encode()):
                return self.send(403, {'error': 'Request is not authorized.'})
            url = urlsplit(self.path)
            operation = url.path.removeprefix('/v1/')
            if not url.path.startswith('/v1/') or operation not in (READS if method == 'GET' else CONTROLS):
                return self.send(404, {'error': 'Not found.'})
            try:
                if method == 'POST':
                    if (self.headers.get('Transfer-Encoding') or len(self.headers.get_all('Content-Length', [])) != 1
                            or self.headers.get_content_type() != 'application/json'):
                        raise ValueError('Send one bounded JSON object.')
                    size = int(self.headers['Content-Length'])
                    if not 0 < size <= MAX_BODY:
                        raise ValueError('Request size is invalid.')
                    body = json.loads(self.rfile.read(size), parse_constant=lambda _: None)
                    if not isinstance(body, dict):
                        raise ValueError('Send a JSON object.')
                else:
                    query = parse_qs(url.query, strict_parsing=True)
                    if query and (operation != 'status' or set(query) != {'job_id'} or len(query['job_id']) != 1):
                        raise ValueError('Select one job ID.')
                    body = {key: values[0] for key, values in query.items()}
                result = executor.dispatch(operation, body)
                return self.send(200, result)
            except (ValueError, TypeError, KeyError) as exc:
                return self.send(409, {'error': str(exc)})
            except OSError:
                return self.send(503, {'error': 'IPC device or storage unavailable.'})

        def do_GET(self):
            self.handle_request('GET')

        def do_POST(self):
            self.handle_request('POST')

    return Loopback(port, Control)


DISPLAY = b"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>AGV status</title><link rel="stylesheet" href="/screen.css"><script src="/screen.js" defer></script></head>
<body><main><h1>AGV status</h1><p id="link" role="status">Waiting for executor status.</p>
<dl><dt>Station</dt><dd id="station">Unknown</dd><dt>Target</dt><dd id="target">Unknown</dd>
<dt>Battery</dt><dd id="battery">Unknown</dd><dt>Motion</dt><dd id="motion">Unknown</dd>
<dt>Fault</dt><dd id="fault">Unknown</dd><dt>Job</dt><dd id="job">Idle</dd></dl></main></body></html>"""
STYLE = b"""body{color:#e6eaf1;background:#151b26;font:clamp(18px,3vw,38px) system-ui;margin:0}
main{padding:6vw;max-width:24em;margin:auto}h1{margin:0}p{color:#b8cce5}
dl{display:grid;grid-template-columns:1fr 1fr;gap:.5em}dt{color:#b8cce5}dd{margin:0}"""
SCRIPT = rb"""const text = (id, value) => document.getElementById(id).textContent = value ?? 'Unknown';
async function refresh() {
  try {
    const res = await fetch('/status', {cache:'no-store', signal:AbortSignal.timeout(2000)});
    if (!res.ok) throw Error();
    const state = await res.json();
    const device = state.device;
    const s = device.status || {};
    const age = device.status_age_s;
    const link = !device.connected ? 'Disconnected' : age == null ? 'Waiting for status' :
      (age < 3 ? (state.armed ? 'Armed' : 'Disarmed') : 'Status stale') + ' \u00b7 ' + age.toFixed(1) + ' s ago';
    text('link', link + (state.recovery_required ? ' \u00b7 Recovery required' : ''));
    text('station', s.current_station);
    text('target', s.target_station);
    text('battery', s.battery_percent == null ? null : s.battery_percent + '%');
    text('motion', s.stopped === true ? 'Stopped' : s.stopped === false ? 'Moving' : null);
    text('fault', s.fault_code);
    text('job', state.quality_alarm ? state.quality_alarm.reason + ' \u00b7 ' + state.quality_alarm.job_id :
      state.job ? state.job.job_id + ' \u00b7 ' + (state.job.outcome || state.job.state) : 'Idle');
  } catch { text('link', 'Executor status unavailable'); }
  setTimeout(refresh, 1000);
}
refresh();"""


def display_listener(port, read):
    class Display(Reply):
        def do_GET(self):
            if not self.allowed_host():
                return self.send(403, {'error': 'Request is not authorized.'})
            if self.path == '/':
                return self.send(200, DISPLAY, 'text/html; charset=utf-8')
            if self.path == '/screen.css':
                return self.send(200, STYLE, 'text/css')
            if self.path == '/screen.js':
                return self.send(200, SCRIPT, 'text/javascript')
            if self.path == '/status':
                state = read()
                if state is None:
                    return self.send(503, {'error': 'Executor status unavailable.'})
                device = state['device']
                allowed = ('status', 'status_age_s', 'connected', 'stale')
                job = state.get('job')
                return self.send(200, dict(boot=state['boot'], armed=state['armed'],
                                           recovery_required=state['recovery_required'],
                                           quality_alarm=state.get('quality_alarm'),
                                           job={k: job[k] for k in ('job_id', 'state', 'outcome')} if job else None,
                                           device={key: device.get(key) for key in allowed}))
            return self.send(404, {'error': 'Not found.'})

        def do_POST(self):
            return self.send(405, {'error': 'Status display is read-only.'})

    return Loopback(port, Display)


class Client:
    def __init__(self, port, token):
        if type(port) is not int or not 1 <= port <= 65535:
            raise ValueError('Choose a fixed loopback executor port.')
        self.port, self.token = port, token

    def __call__(self, operation, body=None):
        from urllib.parse import urlencode
        path = f'/v1/{operation}'
        headers = {'Authorization': 'Bearer ' + self.token}
        method = 'GET' if operation in READS else 'POST'
        payload = None
        if method == 'POST':
            payload = json.dumps(body or {}, allow_nan=False)
            headers['Content-Type'] = 'application/json'
        elif body:
            path += '?' + urlencode(body)
        conn = HTTPConnection('127.0.0.1', self.port, timeout=5 if operation == 'status_read' else .8)
        try:
            conn.request(method, path, payload, headers)
            reply = conn.getresponse()
            value = json.load(reply)
            if reply.status >= 500:
                raise OSError('IPC executor unavailable.')
            if reply.status != 200:
                raise ValueError(value.get('error', 'IPC executor refused the request.'))
            return value
        finally:
            conn.close()


def serve(args):
    root = Path(args.data_dir).resolve()
    root.mkdir(parents=True, exist_ok=True)
    token = Path(args.executor_token_file).read_text().strip()
    with (root / '.executor.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        wire = None if args.fake_executor else WireLog(root / 'agv-wire.jsonl')
        executor = Executor(root, FakeDevices() if wire is None else WiredDevices(root, wire),
                            build=args.executor_build)
        try:
            with control_listener(args.port, executor, token) as control, display_listener(
                args.status_display_port, executor.status
            ) as display:
                thread = Thread(target=display.serve_forever, daemon=True, name='ipc-status-display')
                thread.start()
                try:
                    control.serve_forever(poll_interval=.2)
                finally:
                    display.shutdown()
                    thread.join()
        finally:
            executor.close()
            if wire:
                wire.close()


def interrupt(signum, frame):
    raise KeyboardInterrupt


def serve_display(port, source):
    def cached():
        conn = HTTPConnection('127.0.0.1', source, timeout=.8)
        try:
            conn.request('GET', '/status')
            reply = conn.getresponse()
            return json.load(reply) if reply.status == 200 else None
        except (OSError, ValueError):
            return None
        finally:
            conn.close()

    if port == source:
        raise ValueError('Choose a different display source port.')
    with display_listener(port, cached) as server:
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
