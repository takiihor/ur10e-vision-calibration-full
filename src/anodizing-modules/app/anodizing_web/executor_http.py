"""Separate HTTP surfaces for authenticated job control and cached IPC display."""
from concurrent.futures import Future
from contextlib import ExitStack
import fcntl
from http.client import HTTPConnection, HTTPSConnection, HTTPException
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import math
import os
from pathlib import Path
from threading import Event, Lock, Thread
from time import monotonic, time
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

from . import agv
from .executor import (AdmissionRefused, MAX_BODY, Executor, FakeDevices, Stopped, check_manual_attestation,
                       check_manual_feedback, check_manual_report, finite, validate_measured_rows)
from .executor_lan import GatewayNetwork, HttpsListener, client_context
from .job_client import Client as JobClient, calibration_key
from .job_gateway import GatewayForbidden
from .jobs import ID
from .module_tokens import KeyStore, ServicePrincipal
from .recipe_agv import POSE_FRESH_S, StationVisit, stopped

CONTROLS = {'connect', 'disconnect', 'status_read', 'arm', 'disarm', 'submit', 'heartbeat',
            'stop', 'hold', 'operator_check', 'claim', 'release'}
READS = {'health', 'status', 'jobs'}


class WiredDevices:
    job_types = {'station_visit': 1}
    simulated = False

    def __init__(self, root, trace, *, pose=None, reserve=None, tank_clear=None):
        owner = SimpleNamespace(root=root, lock=Lock(), closing=False, active=None,
                                storage_error=None, execution_uncertain=False)
        self.pose, self.reserve = pose, reserve
        self.tank_clear = tank_clear
        self.manual_travel_qualified = all(callback is not None for callback in (pose, reserve, tank_clear))
        self.manual_travel_until = None
        self.isolated_track = False
        self.cancel = Event()
        self.link = agv.Connection(owner, commissioning_arm=True, wire_trace=trace,
                                   wired_station_jobs=self.manual_travel_qualified)

    def configure_manual_travel(self, expires, battery_floor, isolated_track):
        self.isolated_track = isolated_track
        self.link.wired_station_jobs = self.manual_travel_qualified or isolated_track
        self.manual_travel_until = expires
        self.manual_travel_min_battery = battery_floor

    def _pose_stamp(self):
        stamp = self.pose()
        if not finite(stamp) or not 0 <= monotonic() - stamp < POSE_FRESH_S:
            raise ValueError('Robot travel pose evidence is missing or stale.')
        return stamp

    def check(self, job_type, params):
        if job_type != 'station_visit' or set(params) != {'station'} or self.manual_travel_until is None:
            raise ValueError('Live travel requires a manual station call and qualified local interlocks.')
        self._check_dispatch(params['station'])

    @property
    def journal_error(self):
        return self.link.wire_trace_failure

    @property
    def journal_write_pending(self):
        return self.link.wire_trace_error is not None

    def acknowledge_journal_write(self):
        if self.link.wire_trace_error is None:
            self.link.wire_trace_failure = None

    def _travel_interlocks(self):
        if self.journal_error:
            raise ValueError('AGV wire logging failed. Please call a technician.')
        if self.isolated_track:
            return None  # The operator checks the arm, path and tank equipment before each call.
        if not self.reserve():
            raise ValueError('Insufficient battery reserve for travel and recovery.')
        stamp = self.tank_clear()
        if not finite(stamp) or not 0 <= monotonic() - stamp < POSE_FRESH_S:
            raise ValueError('Fresh clear tank and equipment-off evidence is required before wired travel.')
        return self._pose_stamp()

    def _check_dispatch(self, station):
        if time() >= self.manual_travel_until:
            raise ValueError('The manual travel window expired before dispatch.')
        check_manual_feedback(self.status(), self.manual_travel_min_battery, station)
        return self._travel_interlocks()

    def perform(self, effect, value, *, attestation=None):
        if effect != 'station':
            raise ValueError('The wired handler accepts only a station visit.')

        def check_track(report, stamp, *, dispatch):
            now = time()
            if now >= self.manual_travel_until:
                raise ValueError('The isolated test track window expired.')
            if self.journal_error:
                raise ValueError('AGV wire logging failed. Please call a technician.')
            if dispatch:
                check_manual_attestation(attestation, value, now=now)
            check_manual_report(report or {}, monotonic() - stamp if stamp is not None else None,
                                self.manual_travel_min_battery, value)

        if self.isolated_track:
            check_manual_attestation(attestation, value, now=time())
        state = self.status()
        visit = StationVisit(self.link, self.cancel.set, connection_id=state['connection_id'],
                             stop_generation=state['stop_generation'], allow_wired=True,
                             check_attested_track=check_track if self.isolated_track else None)
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
                def check_dispatch():
                    return self._check_dispatch(value)

                while not visit.start_leg(value, 1500, 30, check_dispatch(), self.cancel,
                                          validate_travel=check_dispatch):
                    if self.cancel.wait(.05):
                        raise Stopped
                while not self.cancel.wait(.05):
                    progress = visit.progress(self._travel_interlocks())
                    if progress.get('arrival_s') is not None:
                        return dict(self.hold(), acknowledgement_s=progress['ack_s'], arrival_s=progress['arrival_s'])
                raise Stopped
            except BaseException:
                visit.abort()
                raise

    def hold(self):
        hold = self.operator_station_hold()
        if hold['station'] is None:
            raise ValueError('Arrival at a station is unverified.')
        return hold

    def operator_station_hold(self):
        state = self.status()
        report = state['status'] or {}
        if (not state['connected'] or state['status_age_s'] is None
                or not 0 <= state['status_age_s'] < agv.RECIPE_STALE_S
                or not stopped(report) or report.get('obstacle_stop') is not False or report.get('fault_code') != 0):
            raise ValueError('Stopped station hold is unverified.')
        station = agv.arrived_station(report)
        if station is not None:
            return dict(location='station', station=station, stopped=True)
        current, target = report.get('current_station'), report.get('target_station')
        maximum = agv.station_limit(report.get('max_stations', 0))
        if report.get('mode') == 2 and report.get('station_arrival') is False:
            location = 'between stations'
            if (type(current) is int and type(target) is int
                    and 1 <= current <= maximum and 1 <= target <= maximum and current != target):
                location = f'{location} {current} and {target}'
            return dict(location=location, station=None, stopped=True)
        raise ValueError('Stopped station hold is unverified.')

    def connect(self):
        return self.link.submit({'action': 'connect', 'mode': 'wired'})

    def read(self):
        return self.link.submit(dict(action='send', command='query_status'))

    def arm(self):
        if self.link.wired_station_jobs:
            self.hold()
        result = self.link.submit(dict(action='arm', armed=True, connection_id=self.link.id))
        self.cancel.clear()
        return result

    def disarm(self):
        with self.link.lock:
            self.link.armed = False

    def stop(self):
        self.cancel.set()
        self.link.armed = False
        return self.link.recipe_stop(external=True) if self.link.wired_station_jobs else False

    def stop_evidence(self, requested_at, attempted_at):
        if attempted_at is None:
            return None
        with self.link.lock:
            report = self.link.status or {}
            stamp = self.link.status_at
            halts = (self.link.results.get('stop'), self.link.results.get('pause'))
            sent = [result['sent_mono_s'] for result in halts if result
                    and result.get('requested_command') == 'stop' and result.get('confirmed_stopped')
                    and result.get('sent_mono_s', 0) >= requested_at]
            if (sent and self.link.transport is not None and stamp is not None
                    and max(sent) < stamp and 0 <= monotonic() - stamp < agv.RECIPE_STALE_S
                    and stopped(report) and report.get('fault_code') == 0 and report.get('obstacle_stop') is False):
                return max(sent)
            return None

    def status(self):
        return self.link.commissioning()

    def close(self, *, final=False):
        self.link.close()
        if final:
            self.link.drain_wire_trace(final=True)


class WireLog:
    def __init__(self, path):
        self.path = path
        self.fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        self.pending = None

    def __call__(self, direction, raw):
        opened = os.fstat(self.fd)
        try:
            named = os.stat(self.path, follow_symlinks=False)
        except FileNotFoundError:
            raise FileNotFoundError(f'{self.path}: wire journal was removed.') from None
        if not opened.st_nlink or (opened.st_dev, opened.st_ino) != (named.st_dev, named.st_ino):
            raise FileNotFoundError(f'{self.path}: wire journal was replaced.')
        if self.pending is None:
            line = json.dumps(dict(utc=agv.timestamp(), direction=direction, hex=raw.hex()))
            self.pending = (direction, raw, (line + '\n').encode(), 0)
        queued_direction, queued_raw, line, written = self.pending
        if (direction, raw) != (queued_direction, queued_raw):
            raise OSError(f'{self.path}: retry the uncommitted wire frame first.')
        try:
            while written < len(line):
                count = os.write(self.fd, line[written:])
                if not count:
                    raise OSError('Short wire journal write.')
                written += count
                self.pending = (direction, raw, line, written)
            os.fsync(self.fd)
        except OSError as exc:
            raise OSError(f'{self.path}: {exc}') from exc
        self.pending = None

    def close(self):
        os.close(self.fd)


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
        scheme = getattr(self.server, 'scheme', 'http')
        return host in self.server.hosts and self.headers.get('Origin') in (None, f'{scheme}://{host}')


def control_handler(executor, token, token_store=None):
    if executor.gateway_enabled:
        executor.gateway.configure_keys(token, token_store)
        authenticate = executor.gateway.authenticate
    else:
        legacy_keys = KeyStore()
        legacy_keys.configure_service_keys(token)
        authenticate = legacy_keys.authenticate_client

    class Control(Reply):
        def handle_request(self, method):
            if not self.allowed_host():
                return self.send(403, {'error': 'Request is not authorized.'})
            provided = self.headers.get_all('Authorization', [])
            bearer = provided[0].removeprefix('Bearer ') if len(provided) == 1 and provided[0].startswith('Bearer ') else ''
            principal = authenticate(bearer, allow_modules=executor.gateway_enabled and token_store is not None)
            if principal is None or not executor.gateway_enabled and principal.name != 'agv-page':
                reject = getattr(self.server, 'reject_key', None)
                status = reject(self.client_address[0]) if reject is not None else 403
                return self.send(status, {'error': 'Request is not authorized.'})
            url = urlsplit(self.path)
            operation = url.path.removeprefix('/v1/')
            if not url.path.startswith('/v1/') or operation not in (READS if method == 'GET' else CONTROLS):
                return self.send(404, {'error': 'Not found.'})
            owner_header = self.headers.get_all('X-Client-Owner', [])
            if (operation != 'stop' and executor.gateway_enabled and isinstance(principal, ServicePrincipal)
                    and owner_header and owner_header != [principal.name]):
                return self.send(403, {'error': 'The request owner does not match the authenticated key.'})
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
                    query = parse_qs(url.query, keep_blank_values=True, strict_parsing=True)
                    if query and (operation != 'status' or set(query) != {'job_id'} or len(query['job_id']) != 1):
                        raise ValueError('Select one job ID.')
                    body = {key: values[0] for key, values in query.items()}
                if executor.gateway_enabled:
                    result = executor.gateway(operation, body, principal=principal)
                elif operation not in ('claim', 'release', 'jobs'):
                    result = executor.dispatch(operation, body)
                else:
                    return self.send(404, {'error': 'Job gateway unavailable.'})
                return self.send(200, result)
            except AdmissionRefused as exc:
                return self.send(409, {'error': str(exc), 'admitted': False, 'job_id': exc.job_id})
            except GatewayForbidden as exc:
                return self.send(403, {'error': str(exc)})
            except (ValueError, TypeError, KeyError, RecursionError) as exc:
                return self.send(409, {'error': str(exc)})
            except OSError:
                return self.send(503, {'error': 'IPC device or storage unavailable.'})

        def do_GET(self):
            self.handle_request('GET')

        def do_POST(self):
            self.handle_request('POST')

    return Control


def control_listener(port, executor, token, token_store=None):
    return Loopback(port, control_handler(executor, token, token_store))


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
    const faults = Object.values(state.evidence_faults || {});
    text('link', faults.length ? [link, ...faults].join(' \u00b7 ') : link + (state.recovery_required ? ' \u00b7 Operator check' : ''));
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
                                           evidence_faults=state.get('evidence_faults', {}),
                                           quality_alarm=state.get('quality_alarm'),
                                           job={k: job[k] for k in ('job_id', 'state', 'outcome')} if job else None,
                                           device={key: device.get(key) for key in allowed}))
            return self.send(404, {'error': 'Not found.'})

        def do_POST(self):
            return self.send(405, {'error': 'Status display is read-only.'})

    return Loopback(port, Display)


def _reply_fields(value, **fields):
    if not isinstance(value, dict) or any(key not in value or type(value[key]) not in types
                                          for key, types in fields.items()):
        raise ValueError('Malformed executor response.')


def _reply_float(raw):
    result = float(raw)
    if not math.isfinite(result):
        raise ValueError('Executor response contains a non-finite number.')
    return result


def _reply_int(raw):
    result = int(raw)
    if abs(result) >= 2**53:
        raise ValueError('Executor response integer is outside the interoperable range.')
    return result


class JobEvidenceError(ValueError):
    pass


class StopEvidenceError(ValueError):
    def __init__(self, status):
        super().__init__('Stop confirmation conflicts with current executor evidence.')
        self.status = status


def stop_corroborated(status, report):
    current = status.get('stop') or {}
    generation = status.get('stop_generation')
    device = status.get('device') or {}
    sample = device.get('status') or {}
    age = device.get('status_age_s')
    halt_at, observed_at = current.get('halt_monotonic'), device.get('status_monotonic')
    return (report.get('confirmed') is True and report.get('received') is True
            and current.get('confirmed') is True and current.get('received') is True
            and isinstance(report.get('boot'), str) and bool(report['boot'])
            and report['boot'] == current.get('boot') == status.get('boot')
            and type(generation) is int and type(report.get('generation')) is int
            and type(current.get('generation')) is int and 0 < report['generation'] <= current['generation'] <= generation
            and isinstance(report.get('stop_id'), str) and bool(report['stop_id'])
            and current.get('halt_attempted') is True
            and (report['generation'] < current['generation'] or (
                current.get('stop_id') == report['stop_id'] and
                report.get('halt_attempted') is True))
            and finite(halt_at) and finite(observed_at) and 0 <= halt_at < observed_at
            and status.get('armed') is False and 'active_job_id' in status and status['active_job_id'] is None
            and 'job' in status and (status['job'] or {}).get('state') not in ('waiting', 'accepted', 'running')
            and (device.get('connected') is True or device.get('simulated') is True)
            and device.get('stale') is False and type(age) in (int, float)
            and 0 <= age < agv.RECIPE_STALE_S and stopped(sample)
            and sample.get('fault_code') == 0 and sample.get('obstacle_stop') is False)


def _validate_stop(value):
    _reply_fields(value, received=(bool,), halt_attempted=(bool,), confirmed=(bool,),
                  boot=(str,), generation=(int,), stop_id=(str,))
    if (value['generation'] < 1 or not value['received'] or not value['boot'] or not value['stop_id']):
        raise ValueError('Invalid Stop evidence.')


def _validate_job(job):
    empty = type(None)
    _reply_fields(job, job_id=(str,), state=(str,), outcome=(str, empty),
                  result=(dict,), checkpoint=(dict, empty), error=(str, empty))
    if job['state'] not in ('waiting', 'accepted', 'running', 'done', 'failed', 'interrupted', 'stopped', 'refused'):
        raise ValueError('Malformed executor job state.')
    if job['outcome'] not in (None, 'succeeded', 'held', 'unknown', 'stopped', 'failed'):
        raise ValueError('Malformed executor job outcome.')
    result = job['result']
    hold = result.get('hold')
    if hold is not None:
        _reply_fields(hold, location=(str,))
    if job['state'] not in ('waiting', 'accepted', 'running'):
        validate_measured_rows(result, job['checkpoint'])
    if job['state'] == 'done' and (job['outcome'] is None or job['outcome'] != result.get('outcome')):
        raise ValueError('The executor job outcomes conflict.')
    if job['state'] in ('waiting', 'accepted', 'running') and job['outcome'] is not None:
        raise ValueError('The executor returned an outcome for an unfinished job.')
    exposure = result.get('open_air')
    if exposure is not None:
        _reply_fields(exposure, job_id=(str,), basket=(str,), run_id=(str,), lift_s=(int, float),
                      air_limit=(int, float))
        if (exposure['job_id'] != job['job_id'] or not result.get('steps') or
                (exposure['lift_s'], exposure['air_limit']) != (
                    result['steps'][-1]['lift_s'], result['steps'][-1]['limits']['air'])):
            raise ValueError('Malformed executor open-air evidence.')


def _validate_job_evidence(job):
    try:
        _validate_job(job)
    except ValueError as exc:
        raise JobEvidenceError(str(exc)) from exc


def _validate_reply(operation, value):
    empty = type(None)
    _reply_fields(value)
    if operation == 'health':
        _reply_fields(value, protocol=(int,), job_types=(dict,), simulated=(bool,), motion_enabled=(bool,),
                      boot=(str,), heartbeat_s=(int, float), lease_s=(int, float))
        if not all(type(version) is int for version in value['job_types'].values()):
            raise ValueError('Malformed executor job versions.')
        if 'manual_travel_isolated_track' in value:
            _reply_fields(value, manual_travel_isolated_track=(bool,))
        if 'manual_travel_missing_evidence' in value:
            _reply_fields(value, manual_travel_missing_evidence=(bool,))
        if value.get('manual_travel_until') is not None:
            _reply_fields(value, manual_travel_until=(int, float), manual_travel_min_battery=(int, float))
            if not 0 <= value['manual_travel_min_battery'] <= 100:
                raise ValueError('Malformed manual travel battery floor.')
    elif operation == 'heartbeat':
        _reply_fields(value, lease_valid=(bool,), boot=(str,))
    elif operation == 'stop':
        _validate_stop(value)
    elif operation == 'submit':
        _validate_job_evidence(value)
    elif operation == 'jobs':
        _reply_fields(value, jobs=(list,))
        for job in value['jobs']:
            _validate_job_evidence(job)
    elif operation in READS | CONTROLS:
        _reply_fields(value, device=(dict,), boot=(str,), lease_valid=(bool,), armed=(bool,),
                      session=(str, empty), stop_generation=(int,), stop=(dict, empty),
                      recovery_required=(bool,), recovery_ids=(list,), quality_alarm=(dict, empty),
                      job=(dict, empty), operator_check=(dict, empty))
        if value['stop_generation'] < 0:
            raise ValueError('Invalid executor generation.')
        if 'owner' in value:
            _reply_fields(value, owner=(str, empty), armed_for=(str, empty))
            if value['armed_for'] != (value['owner'] if value['armed'] else None) or (
                    value['armed'] and value['owner'] is None):
                raise ValueError('The executor Arm and owner do not agree.')
        check = value['operator_check']
        if check is not None:
            _reply_fields(check, reason=(str,), location=(str, empty), basket=(str, empty),
                          job_id=(str, empty), step=(int, empty), technician=(bool,))
            if 'run_id' in check:
                _reply_fields(check, run_id=(str, empty))
            if value['armed']:
                raise ValueError('The executor is armed during an operator check.')
        device = value['device']
        _reply_fields(device, connected=(bool,), status=(dict, empty), status_age_s=(int, float, empty),
                      settings=(dict, empty), results=(dict,), log=(list,), connection_id=(int,), stale=(bool,))
        if device['connected']:
            _reply_fields(device['settings'], mode=(str,))
        for name in ('log', 'events'):
            entries = device.get(name, [])
            if not isinstance(entries, list):
                raise ValueError('Malformed executor event stream.')
            for entry in entries:
                _reply_fields(entry, id=(int,))
                if entry['id'] < 1:
                    raise ValueError('Malformed executor event ID.')
                for key in ('timestamp', 'direction', 'hex', 'field', 'reason'):
                    if key in entry:
                        _reply_fields(entry, **{key: (str,)})
                if 'decode' in entry:
                    _reply_fields(entry, decode=(dict,))
        if not all(isinstance(ident, str) for ident in value['recovery_ids']):
            raise ValueError('Malformed executor recovery IDs.')
        if 'evidence_faults' in value and (not isinstance(value['evidence_faults'], dict)
                or not all(isinstance(reason, str) for reason in value['evidence_faults'].values())):
            raise ValueError('Malformed executor evidence faults.')
        for key in ('active_job_id', 'latest_job_id', 'latest_manual_job_id'):
            if key in value:
                _reply_fields(value, **{key: (str, empty)})
                if value[key] is not None and not ID.fullmatch(value[key]):
                    raise ValueError('Malformed executor job ID.')
        if value['quality_alarm'] is not None:
            _reply_fields(value['quality_alarm'], job_id=(str,), reason=(str,))
        if value['job'] is not None:
            _validate_job_evidence(value['job'])
        if value['stop'] is not None:
            _validate_stop(value['stop'])
            if value['stop']['boot'] != value['boot'] or value['stop']['generation'] > value['stop_generation']:
                raise StopEvidenceError(value)
            if value['stop']['confirmed'] and not stop_corroborated(value, value['stop']):
                raise StopEvidenceError(value)


def legacy_display_status(value, boot):
    _reply_fields(value, boot=(str,))
    if value['boot'] != boot:
        raise ValueError('The legacy status boot does not match health.')
    projected = dict(value, armed=False, lease_valid=False, session=None, job=None,
                     stop=None, operator_check=None)
    for key in ('active_job_id', 'latest_job_id', 'latest_manual_job_id'):
        projected.pop(key, None)
    _validate_reply('status', projected)
    return projected


def _validate_reply_identity(operation, request, reply):
    if 'boot' in request and 'boot' in reply and request['boot'] != reply['boot']:
        raise ValueError('Executor response boot does not match the request.')
    if operation == 'stop' and 'stop_id' in request and request['stop_id'] != reply['stop_id']:
        raise ValueError('Executor Stop identity does not match the request.')
    if operation in ('status', 'submit'):
        job = reply['job'] if operation == 'status' else reply
        if job is not None and 'job_id' in request and job['job_id'] != request['job_id']:
            raise JobEvidenceError('Executor job identity does not match the request.')


def deliver_priority_stop(request, body, persist):
    if isinstance(request, Client):
        return request('stop', body, after_send=persist)
    if getattr(request, '__func__', None) is Executor.dispatch:
        try:
            return request('stop', body)
        finally:
            persist()

    # Injected transports can block inside the call without exposing their send boundary.
    entered, reply = Event(), Future()

    def deliver():
        entered.set()
        try:
            reply.set_result(request('stop', body))
        except BaseException as exc:
            reply.set_exception(exc)

    Thread(target=deliver, name='priority-stop', daemon=True).start()
    entered.wait()
    persist()
    return reply.result()


class Client(JobClient):
    def __init__(self, port, token, *, host='127.0.0.1', certificate=None):
        if type(port) is not int or not 1 <= port <= 65535:
            raise ValueError('Choose a fixed executor port.')
        if certificate is None and host != '127.0.0.1':
            raise ValueError('The line-LAN executor requires a pinned HTTPS certificate.')
        super().__init__(token)
        self.port, self.host, self.certificate = port, host, certificate
        self.tls = client_context(certificate) if certificate is not None else None

    def read_legacy_status(self, boot):
        return self('status', legacy_boot=boot)

    def for_owner(self, owner):
        if owner == self.owner_id:
            return self
        if self.owner_id == 'calibration':
            raise ValueError('Calibration may use only its own key.')
        if owner == 'agv-page':
            cls, key = OperatorApp, self.key
        elif owner == 'calibration':
            cls, key = CalibrationModule, calibration_key(self.key)
        else:
            raise ValueError('Choose agv-page or calibration as the built-in owner.')
        return cls(self.port, key, host=self.host, certificate=self.certificate)

    def __call__(self, operation, body=None, *, legacy_boot=None, after_send=None):
        from urllib.parse import urlencode
        path = f'/v1/{operation}'
        headers = {'Authorization': 'Bearer ' + self.key}
        if self.owner_id is not None:
            headers['X-Client-Owner'] = self.owner_id
        method = 'GET' if operation in READS else 'POST'
        payload = None
        if method == 'POST':
            payload = json.dumps(body or {}, allow_nan=False)
            headers['Content-Type'] = 'application/json'
        elif body:
            path += '?' + urlencode(body)
        timeout = 5 if operation == 'status_read' else .8
        conn = (HTTPSConnection(self.host, self.port, timeout=timeout, context=self.tls) if self.tls is not None
                else HTTPConnection(self.host, self.port, timeout=timeout))
        try:
            try:
                conn.request(method, path, payload, headers)
            finally:
                if after_send is not None:
                    after_send()
            reply = conn.getresponse()
            if reply.status >= 500:
                raise OSError('IPC executor unavailable.')
            try:
                value = json.load(reply, parse_int=_reply_int, parse_float=_reply_float, parse_constant=_reply_float)
                _reply_fields(value)
                if reply.status == 200:
                    if operation == 'status' and legacy_boot is not None:
                        value = legacy_display_status(value, legacy_boot)
                    else:
                        _validate_reply(operation, value)
                    _validate_reply_identity(operation, body or {}, value)
                elif 'admitted' in value:
                    _reply_fields(value, admitted=(bool,), error=(str,), job_id=(str,))
                    if (operation != 'submit' or reply.status != 409 or value['admitted'] is not False
                            or value['job_id'] != (body or {}).get('job_id')):
                        raise ValueError('Malformed IPC preadmission refusal.')
            except (JobEvidenceError, StopEvidenceError):
                raise
            except (ValueError, RecursionError) as exc:
                raise OSError('Invalid IPC executor response.') from exc
            if reply.status != 200:
                if value.get('admitted') is False:
                    raise AdmissionRefused(value['job_id'], value['error'])
                raise ValueError(value.get('error', 'IPC executor refused the request.'))
            return value
        except HTTPException as exc:
            raise OSError('Interrupted IPC executor response.') from exc
        finally:
            conn.close()


class OperatorApp(Client):
    owner_id = 'agv-page'
    scopes = frozenset({'operator'})


class CalibrationModule(Client):
    owner_id = 'calibration'
    scopes = frozenset({'read', 'jobs'})

    def __call__(self, operation, body=None, **options):
        if operation not in READS | {'claim', 'release', 'submit', 'heartbeat', 'stop'}:
            raise ValueError('This operation requires the operator app key.')
        return super().__call__(operation, body, **options)


def serve(args):
    root = Path(args.data_dir).resolve()
    root.mkdir(parents=True, exist_ok=True)
    token = Path(args.executor_token_file).read_text().strip()
    network = GatewayNetwork.configured(root, simulated=args.fake_executor)
    if network is not None and not args.fake_executor:
        from .module_tokens import KeyStore

        if not token.startswith('sha256:') or KeyStore.service_credentials(token)[1] is None:
            raise ValueError('Store the Operator and Calibration service keys as hashes on the IPC.')
    with (root / '.executor.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        wire = None if args.fake_executor else WireLog(root / 'agv-wire.jsonl')
        executor = Executor(root, FakeDevices.configured(root) if wire is None else WiredDevices(root, wire),
                            build=args.executor_build, gateway_enabled=network is not None)
        try:
            with ExitStack() as stack:
                control = stack.enter_context(control_listener(args.port, executor, token))
                listeners = [(stack.enter_context(display_listener(args.status_display_port, executor.status)),
                              'ipc-status-display')]
                if network is not None:
                    handler = control_handler(executor, token, executor.gateway.keys)
                    lan = stack.enter_context(HttpsListener(network, root, handler))
                    listeners.append((lan, 'ipc-job-api'))
                workers = []
                try:
                    for listener, name in listeners:
                        thread = Thread(target=listener.serve_forever, daemon=True, name=name)
                        thread.start()
                        workers.append((listener, thread))
                    control.serve_forever(poll_interval=.2)
                finally:
                    for listener, thread in workers:
                        listener.shutdown()
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
        except (OSError, ValueError, HTTPException):
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
