"""Exercise the authenticated HTTP handler with an in-process AGV, without sockets or devices."""
import ast
from concurrent.futures import ThreadPoolExecutor
from email.message import Message
from http.server import BaseHTTPRequestHandler
from io import BytesIO
import json
from pathlib import Path
import secrets
from threading import Event, RLock
from time import monotonic
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


@pytest.mark.parametrize('command', ['stop', 'pause'])
def test_urgent_write_bypasses_silent_read_and_owner_lock(session, monkeypatch, command):
    read_written = Event()
    read_times, urgent_times = [], []

    class ObservedLink(agv.Simulator):
        def write(self, raw):
            if agv.parse_frame(raw).command == 0x26:
                read_times.append(monotonic())
                read_written.set()
            else:
                urgent_times.append(monotonic())
            return super().write(raw)

    monkeypatch.setattr(agv, 'Simulator', ObservedLink)
    owner, request = session
    connection_id = open_simulator(request)
    request(dict(action='silence', silent=True, connection_id=connection_id))
    owner.active = 'robot-job-1'
    with ThreadPoolExecutor(max_workers=2) as pool, owner.lock:
        read = pool.submit(send, request, 'query_status', connection_id)
        assert read_written.wait(2)

        def submit_urgent():
            started = monotonic()
            status, state = send(request, command)
            assert status == 200 and state['results'][command]['state'] == agv.SENT
            return started

        started = pool.submit(submit_urgent).result(timeout=3)
        status, state = read.result(timeout=3)
        read_elapsed = monotonic() - read_times[0]

    latency = urgent_times[0] - started
    print(f'{command} reached transport.write in {latency * 1000:.2f} ms during a {read_elapsed:.3f} s read')
    assert latency < .050
    assert read_elapsed >= agv.REPLY_TIMEOUT
    assert status == 200 and state['results']['query_status']['state'] == 'no reply'


def test_concurrent_reads_keep_the_silent_read_window_free_of_other_writes(session, monkeypatch):
    first_written = Event()
    writes = []

    class ObservedLink(agv.Simulator):
        def write(self, raw):
            command = agv.parse_frame(raw).command
            writes.append((command, monotonic()))
            if command == 0x21:
                first_written.set()
                return len(raw)
            return super().write(raw)

    monkeypatch.setattr(agv, 'Simulator', ObservedLink)
    _, request = session
    connection_id = open_simulator(request)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(send, request, 'read_speed', connection_id)
        assert first_written.wait(2)
        second = pool.submit(send, request, 'query_status', connection_id)
        first_status, first_state = first.result(timeout=3)
        second_status, second_state = second.result(timeout=3)

    (first_command, first_at), (second_command, second_at) = writes
    assert (first_command, second_command) == (0x21, 0x26)
    assert second_at - first_at >= agv.REPLY_TIMEOUT
    assert first_status == second_status == 200
    assert first_state['results']['read_speed']['state'] == 'no reply'
    assert second_state['results']['query_status']['state'] == 'reply: status'


def test_urgent_write_can_receive_the_pending_read_reply(session, monkeypatch, agv_clock):
    reply = b''

    class ReplyOnNextCheck(agv.Simulator):
        @property
        def in_waiting(self):
            nonlocal reply
            self.buffer += reply
            reply = b''
            return super().in_waiting

        def write(self, raw):
            if agv.parse_frame(raw).command == 0x21:
                return len(raw)
            return super().write(raw)

    monkeypatch.setattr(agv, 'Simulator', ReplyOnNextCheck)
    _, request = session
    connection_id = open_simulator(request)
    with ThreadPoolExecutor(max_workers=1) as pool:
        def interleave(seconds):
            nonlocal reply
            agv_clock.now += seconds
            reply = bytes.fromhex('A5 5A 07 21 DC 05 08')
            status, state = pool.submit(send, request, 'stop', connection_id).result(timeout=1)
            assert status == 200
            assert state['results']['read_speed']['state'] == 'reply: 1500 m/h'

        monkeypatch.setattr(agv, 'sleep', interleave)
        status, state = send(request, 'read_speed', connection_id)

    assert status == 200
    assert state['results']['read_speed']['state'] == 'reply: 1500 m/h'


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


def test_command_confirmation_expires_during_silent_read(session, agv_clock):
    _, request = session
    connection_id = open_simulator(request)
    send(request, 'query_status')
    send(request, 'pause')
    request(dict(action='silence', silent=True, connection_id=connection_id))
    agv_clock.now += agv.STALE_AFTER - 1

    status, state = send(request, 'query_status', connection_id)
    assert status == 200 and state['results']['query_status']['state'] == 'no reply'
    assert state['results']['pause']['state'] == 'not confirmed'


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
    assert state['results']['query_status']['state'] == 'reply: status'

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
    assert state['results']['read_speed']['state'] == 'reply: 1500 m/h'

    clock.now += agv.STALE_AFTER + .01
    state = send(request, 'read_speed', connection_id)[1]
    assert state['status_age_s'] == pytest.approx(7.01) and state['stale']
    assert state['results']['read_speed']['state'] == 'reply: 1500 m/h'


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


@pytest.fixture
def agv_clock(monkeypatch):
    clock = SimpleNamespace(now=10.0)

    def sleep(seconds):
        clock.now = round(clock.now + seconds, 2)

    monkeypatch.setattr(agv, 'monotonic', lambda: clock.now)
    monkeypatch.setattr(agv, 'sleep', sleep)
    return clock


def test_echoed_speed_request_is_followed_by_a_real_reply(session, monkeypatch):
    class EchoingLink(agv.Simulator):
        def write(self, raw):
            self.buffer += raw
            return super().write(raw)

    monkeypatch.setattr(agv, 'Simulator', EchoingLink)
    _, request = session
    connection_id = open_simulator(request)
    state = send(request, 'read_speed', connection_id)[1]
    echo, reply = state['log'][-2:]
    assert echo['hex'] == '00 01 04 A5 5A 07 21 7D 02 A6'
    assert echo['decode'] == {'description': 'echo of our transmission'}
    assert reply['hex'] == '00 FF 04 A5 5A 07 21 DC 05 08'
    assert reply['decode']['read_speed_m_h'] == 1500
    assert state['results']['read_speed']['state'] == 'reply: 1500 m/h'


def test_echo_without_speed_reply_times_out_at_the_reply_deadline(session, monkeypatch, agv_clock):
    class EchoOnlyLink(agv.Simulator):
        def write(self, raw):
            self.buffer += raw
            return len(raw)

    monkeypatch.setattr(agv, 'Simulator', EchoOnlyLink)
    _, request = session
    connection_id = open_simulator(request)
    state = send(request, 'read_speed', connection_id)[1]
    assert state['log'][-1]['decode'] == {'description': 'echo of our transmission'}
    assert agv_clock.now == 10.0 + agv.REPLY_TIMEOUT
    assert state['results']['read_speed']['state'] == 'no reply'
    agv_clock.now = 10.0 + agv.STALE_AFTER
    assert request()[1]['results']['read_speed']['state'] == 'no reply'


def test_echoed_status_query_does_not_refresh_status_age(session, monkeypatch, agv_clock):
    class EchoOnSilenceLink(agv.Simulator):
        def write(self, raw):
            if self.silent:
                self.buffer += raw
            return super().write(raw)

    monkeypatch.setattr(agv, 'Simulator', EchoOnSilenceLink)
    _, request = session
    connection_id = open_simulator(request)
    initial = send(request, 'query_status', connection_id)[1]
    assert initial['status_age_s'] == 0
    request(dict(action='silence', silent=True, connection_id=connection_id))
    agv_clock.now += 2
    state = send(request, 'query_status', connection_id)[1]
    assert state['log'][-1]['hex'] == state['log'][-2]['hex']
    assert '26 FF' in state['log'][-1]['hex']
    assert state['log'][-1]['decode'] == {'description': 'echo of our transmission'}
    assert state['status_age_s'] == pytest.approx(agv_clock.now - 10.0)
    assert state['status_hex'] == initial['status_hex']
    assert state['results']['query_status']['state'] == 'no reply'


@pytest.mark.parametrize('delay', [.58, .60, 1.50])
def test_prefixless_radio_speed_reply_completes_read_without_status(session, monkeypatch, agv_clock, delay):
    class DelayedLink(agv.Simulator):
        reply_at = None

        @property
        def in_waiting(self):
            if self.reply_at is not None and agv_clock.now >= self.reply_at:
                self.buffer += bytes.fromhex('A5 5A 07 21 DC 05 08')
                self.reply_at = None
            return super().in_waiting

        def write(self, raw):
            if agv.parse_frame(raw).command == 0x21:
                self.reply_at = agv_clock.now + delay
                return len(raw)
            return super().write(raw)

    monkeypatch.setattr(agv, 'Simulator', DelayedLink)
    _, request = session
    connection_id = open_simulator(request)
    state = send(request, 'read_speed', connection_id)[1]
    assert agv_clock.now == 10.0 + delay
    assert state['log'][-1]['decode'] == {'read_speed_m_h': 1500}
    assert state['results']['read_speed']['state'] == 'reply: 1500 m/h'
    assert state['status'] is None


def test_query_waits_for_reply_or_full_timeout_before_next_request(session, monkeypatch, agv_clock):
    class DelayedStatusLink(agv.Simulator):
        reply_at = 10.60

        @property
        def in_waiting(self):
            return super().in_waiting if agv_clock.now >= self.reply_at else 0

    monkeypatch.setattr(agv, 'Simulator', DelayedStatusLink)
    _, request = session
    connection_id = open_simulator(request)
    state = send(request, 'query_status', connection_id)[1]
    assert agv_clock.now == 10.60
    assert state['results']['query_status']['state'] == 'reply: status'

    request(dict(action='silence', silent=True, connection_id=connection_id))
    state = send(request, 'query_status', connection_id)[1]
    assert agv_clock.now == 12.10
    assert state['results']['query_status']['state'] == 'no reply'
