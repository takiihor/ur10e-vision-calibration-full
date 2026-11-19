"""Exercise the authenticated HTTP handler with an in-process AGV, without sockets or devices."""
import ast
from email.message import Message
from http.server import BaseHTTPRequestHandler
from io import BytesIO
import json
from pathlib import Path
import secrets
from threading import RLock
from types import SimpleNamespace
from urllib.parse import urlsplit

import pytest

from anodizing_web import agv


source = Path(__file__).resolve().parents[1] / 'anodizing_web/server.py'
handler = next(node for node in ast.parse(source.read_text()).body
               if isinstance(node, ast.ClassDef) and node.name == 'Handler')
namespace = dict(BaseHTTPRequestHandler=BaseHTTPRequestHandler, json=json, secrets=secrets,
                 urlsplit=urlsplit, MAX_BODY=32768)
exec(compile(ast.Module(body=[handler], type_ignores=[]), str(source), 'exec'), namespace)


@pytest.fixture
def session():
    owner = SimpleNamespace(lock=RLock(), active=None, closing=False, storage_error=None,
                            execution_uncertain=False)
    connection = agv.Connection(owner)

    def request(body=None, *, token='secret', origin=None):
        h = object.__new__(namespace['Handler'])
        h.server = SimpleNamespace(owner=owner, agv=connection, hosts={'localhost:8870'}, token='secret')
        h.path = '/api/agv'
        h.headers = Message()
        h.headers['Host'] = 'localhost:8870'
        h.headers['X-Module-Token'] = token
        if origin:
            h.headers['Origin'] = origin
        if body is not None:
            data = json.dumps(body).encode()
            h.headers['Content-Type'] = 'application/json'
            h.headers['Content-Length'] = str(len(data))
            h.rfile = BytesIO(data)
        h.reply = lambda status, result: setattr(h, 'response', (status, result))
        (h.do_GET if body is None else h.do_POST)()
        return h.response

    try:
        yield owner, request
    finally:
        connection.close()


def open_simulator(request):
    status, state = request({'action': 'connect'})
    assert status == 200 and state['connected'] and state['settings']['mode'] == 'simulator'
    assert state['armed'] is False and state['stale'] is False
    return state['connection_id']


def send(request, command, connection_id=None, **fields):
    return request(dict(action='send', command=command, connection_id=connection_id, **fields))


def test_motion_requires_arming_for_the_current_connection(session):
    _, request = session
    first = open_simulator(request)
    assert 'Armed' in send(request, 'forward', first)[1]['error']
    assert request(dict(action='arm', armed=True, connection_id=first))[0] == 200
    assert send(request, 'forward', first)[0] == 200
    assert request(dict(action='disconnect', connection_id=first))[0] == 200
    second = open_simulator(request)
    assert first != second
    assert request(dict(action='arm', armed=True, connection_id=first))[0] == 400
    assert 'Armed' in send(request, 'forward', second)[1]['error']


@pytest.mark.parametrize(('field', 'value', 'error'), [
    ('active', 'robot-job-1', 'owns the service'),
    ('closing', True, 'closing'),
    ('storage_error', 'write failed', 'storage'),
    ('execution_uncertain', True, 'uncertain'),
])
def test_owner_blocks_motion_but_allows_stop_and_pause_even_without_arm(session, field, value, error):
    owner, request = session
    connection_id = open_simulator(request)
    request(dict(action='arm', armed=True, connection_id=connection_id))
    setattr(owner, field, value)
    assert error in send(request, 'forward', connection_id)[1]['error']
    request(dict(action='arm', armed=False, connection_id=connection_id))
    for command_text, payload in [('stop', '23 87 04 00'), ('pause', '23 5B 01 00')]:
        status, state = send(request, command_text)
        assert status == 200 and state['log'][-1]['direction'] == 'TX'
        assert payload in state['log'][-1]['hex']


def test_simulator_reports_motion_speed_target_pause_and_stop(session):
    _, request = session
    connection_id = open_simulator(request)
    send(request, 'query_status')
    request(dict(action='arm', armed=True, connection_id=connection_id))
    assert send(request, 'set_speed', connection_id, value=1257)[1]['results']['set_speed']['state'] == 'sent (protocol has no ACK)'
    state = send(request, 'query_status')[1]
    assert state['status']['set_speed_m_h'] == 1257
    assert state['results']['set_speed']['state'] == 'confirmed by status'
    assert send(request, 'read_speed')[1]['log'][-1]['decode'] == {
        'read_speed_m_h': 1257, 'reply_prefix': '00 FF 04',
    }
    state = send(request, 'call_station', connection_id, value=255)[1]
    assert 'maximum' in state['results']['call_station']['warning']
    state = send(request, 'query_status')[1]
    assert state['status']['target_station'] == 255
    assert state['status']['actual_speed_m_h'] == 1257
    for command, stopped, paused in [('pause', True, True), ('resume', False, False), ('stop', True, False)]:
        send(request, command, connection_id)
        state = send(request, 'query_status')[1]
        assert (state['status']['stopped'], state['status']['paused']) == (stopped, paused)
        assert state['results'][command]['state'] == 'confirmed by status'


def test_silent_simulator_marks_link_stale_and_times_out_confirmation(session, monkeypatch):
    clock = SimpleNamespace(now=10.0)
    monkeypatch.setattr(agv, 'monotonic', lambda: clock.now)
    _, request = session
    connection_id = open_simulator(request)
    state = send(request, 'query_status')[1]
    assert state['stale'] is False and state['status_age_s'] == 0
    assert request(dict(action='silence', silent=True, connection_id=connection_id))[0] == 200
    send(request, 'pause')
    clock.now = 15.01
    status, state = request()
    assert status == 200 and state['connected'] and state['stale']
    assert state['status_age_s'] == pytest.approx(5.01)
    assert state['results']['pause']['state'] == 'not confirmed'


def test_unchanged_status_does_not_confirm_repeat_of_completed_command(session, monkeypatch):
    clock = SimpleNamespace(now=10.0)
    monkeypatch.setattr(agv, 'monotonic', lambda: clock.now)
    _, request = session
    open_simulator(request)
    send(request, 'query_status')
    send(request, 'stop')
    assert send(request, 'query_status')[1]['results']['stop']['state'] == 'confirmed by status'
    send(request, 'stop')
    clock.now += 5
    assert send(request, 'query_status')[1]['results']['stop']['state'] == 'not confirmed'


def test_agv_uses_existing_request_authorization(session):
    _, request = session
    assert request(token='wrong')[0] == 403
    assert request({'action': 'connect'}, origin='http://elsewhere')[0] == 403
    assert request()[1]['connected'] is False


def test_captured_replies_confirm_and_only_status_refreshes_the_link(session, monkeypatch):
    clock = SimpleNamespace(now=10.0)
    monkeypatch.setattr(agv, 'monotonic', lambda: clock.now)
    capture = Path(__file__).with_name('fixtures') / 'agv600-protocol-vectors.txt'
    status_reply, speed_reply, unidentified = (bytes.fromhex(line) for line in capture.read_text().splitlines())

    class CapturedReplies(agv.Simulator):
        def write(self, raw):
            command = agv.parse_frame(raw).command
            self.buffer += {0x26: status_reply, 0x21: speed_reply + unidentified}[command]
            return len(raw)

    monkeypatch.setattr(agv, 'Simulator', CapturedReplies)
    _, request = session
    connection_id = open_simulator(request)
    state = send(request, 'query_status', connection_id)[1]
    assert state['status_hex'] == status_reply.hex(' ').upper()
    assert state['status']['reply_prefix'] == '00 FF 04'
    assert state['results']['query_status']['state'] == 'confirmed by status'

    clock.now += 4
    state = send(request, 'query_status', connection_id)[1]
    assert state['status_age_s'] == 0 and not state['stale']
    clock.now += 2
    state = send(request, 'read_speed', connection_id)[1]
    reply, unknown = state['log'][-2:]
    assert reply['hex'] == speed_reply.hex(' ').upper()
    assert reply['decode']['reply_prefix'] == '00 FF 04' and reply['decode']['read_speed_m_h'] == 1500
    assert unknown['hex'] == unidentified.hex(' ').upper()
    assert unknown['decode']['description'] == 'unidentified frame 01 (not in the XBOT protocol)'
    assert 'error' not in unknown['decode']
    assert state['status_age_s'] == 2 and not state['stale']
    assert state['results']['read_speed']['state'] == agv.SENT

    state = send(request, 'query_status', connection_id)[1]
    assert state['status_age_s'] == 0 and not state['stale']
    assert state['results']['read_speed']['state'] == 'confirmed by status'

    clock.now += agv.STALE_AFTER + .01
    state = send(request, 'read_speed', connection_id)[1]
    assert state['status_age_s'] == pytest.approx(5.01) and state['stale']
    assert state['results']['read_speed']['state'] == agv.SENT


def test_simulator_uses_captured_wire_forms_and_reports_01_periodically(session, monkeypatch):
    clock = SimpleNamespace(now=0.0)
    monkeypatch.setattr(agv, 'monotonic', lambda: clock.now)
    _, request = session
    connection_id = open_simulator(request)
    assert request()[1]['log'] == []

    state = send(request, 'query_status', connection_id)[1]
    assert state['status_hex'].startswith('00 FF 04 A5 5A 1D 25 ')
    assert state['status']['obstacle_input'] == 0xCC
    assert not {'attachment_active', 'roller_speed'} & state['status'].keys()
    assert send(request, 'read_speed', connection_id)[1]['log'][-1]['hex'] == '00 FF 04 A5 5A 07 21 DC 05 08'

    for tick in (11.22, 22.44):
        clock.now = tick - .01
        before = request()[1]['log']
        clock.now = tick
        state = request()[1]
        assert len(state['log']) == len(before) + 1
        assert state['log'][-1]['hex'] == 'A5 5A 05 01 05'
        assert state['status_age_s'] == tick and state['stale']
        assert request()[1]['log'] == state['log']

    request(dict(action='silence', silent=True, connection_id=connection_id))
    clock.now += 12
    assert request()[1]['log'] == state['log']
