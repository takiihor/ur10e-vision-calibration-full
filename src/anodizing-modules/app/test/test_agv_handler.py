"""Exercise the authenticated HTTP handler with an in-process AGV, without sockets or devices."""
import ast
from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
from email.message import Message
from http.server import BaseHTTPRequestHandler
from io import BytesIO
import json
import random
import statistics
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


def test_set_speed_argument_defaults_to_slowest_speed(session):
    _, request = session
    status, state = request()
    assert status == 200
    argument = next(command['argument'] for command in state['commands'] if command['name'] == 'set_speed')
    assert argument['default'] == argument['min'] == 500


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
    ('active', 'robot-job-1', 'robot job is running'),
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
def test_urgent_write_waits_for_silent_status_reply_cutoff_without_owner_lock(session, monkeypatch, command):
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
    assert .90 <= latency <= 1.05
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
    assert second_state['results']['query_status']['state'] == 'status received'


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
    assert send(request, 'set_speed', connection_id, value=1257)[1]['results']['set_speed']['state'] == agv.SENT
    state = send(request, 'query_status')[1]
    assert state['status']['set_speed_m_h'] == 1257
    assert state['results']['set_speed']['state'] == 'confirmed by status'
    assert send(request, 'read_speed')[1]['log'][-1]['decode'] == {
        'read_speed_m_h': 1257, 'reply_prefix': '00 FF 04',
    }
    assert send(request, 'call_station', connection_id, value=2)[0] == 200
    state = send(request, 'query_status')[1]
    assert state['status']['target_station'] == 2
    assert state['status']['actual_speed_m_h'] == 1257
    for command, stopped, paused in [('pause', True, True), ('resume', False, False), ('stop', True, False)]:
        send(request, command, connection_id)
        state = send(request, 'query_status')[1]
        assert (state['status']['stopped'], state['status']['paused']) == (stopped, paused)
        assert state['results'][command]['state'] == 'confirmed by status'


def test_silent_simulator_marks_link_stale_without_inventing_a_confirmation(session, monkeypatch):
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
    assert state['results']['pause']['state'] == 'sent'
    assert state['results']['pause']['tries'] == 2


def test_status_confirms_repeat_stop_when_agv_is_already_stopped(session, agv_clock):
    _, request = session
    open_simulator(request)
    send(request, 'query_status')
    send(request, 'stop')
    assert send(request, 'query_status')[1]['results']['stop']['state'] == 'confirmed by status'
    send(request, 'stop')
    agv_clock.now += 5
    assert send(request, 'query_status')[1]['results']['stop']['state'] == 'confirmed by status'


def test_command_confirmation_expires_during_silent_read(session, agv_clock):
    _, request = session
    connection_id = open_simulator(request)
    send(request, 'query_status')
    request(dict(action='arm', armed=True, connection_id=connection_id))
    send(request, 'rotate_clockwise', connection_id)
    agv_clock.now += 1
    assert request()[1]['results']['rotate_clockwise']['ack_elapsed_s'] == pytest.approx(1)
    send(request, 'query_status')
    request(dict(action='silence', silent=True, connection_id=connection_id))
    agv_clock.now += agv.STALE_AFTER - 2

    status, state = send(request, 'query_status', connection_id)
    assert status == 200 and state['results']['query_status']['state'] == 'no reply'
    assert state['results']['rotate_clockwise']['state'] == 'not confirmed'


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
    assert state['results']['query_status']['state'] == 'status received'

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


def test_simulator_uses_captured_wire_forms_and_reports_01_periodically(session, agv_clock):
    agv_clock.now = 0.0
    _, request = session
    connection_id = open_simulator(request)
    assert request()[1]['log'] == []

    state = send(request, 'query_status', connection_id)[1]
    assert state['status_hex'].startswith('00 FF 04 A5 5A 1D 25 ')
    assert state['status']['obstacle_input'] == 0xCC
    assert not {'attachment_active', 'roller_speed'} & state['status'].keys()
    assert send(request, 'read_speed', connection_id)[1]['log'][-1]['hex'] == '00 FF 04 A5 5A 07 21 DC 05 08'

    for tick in (11.22, 22.44):
        agv_clock.now = tick - .01
        before = request()[1]['log']
        agv_clock.now = tick
        state = request()[1]
        assert len(state['log']) == len(before) + 1
        assert state['log'][-1]['hex'] == 'A5 5A 05 01 05'
        assert state['status_age_s'] == tick and state['stale']
        assert request()[1]['log'] == state['log']

    request(dict(action='silence', silent=True, connection_id=connection_id))
    agv_clock.now += 12
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
    assert state['results']['query_status']['state'] == 'status received'

    request(dict(action='silence', silent=True, connection_id=connection_id))
    state = send(request, 'query_status', connection_id)[1]
    assert agv_clock.now == 12.25
    assert state['results']['query_status']['state'] == 'no reply'


def test_station_arrival_uses_a_later_stop_and_measures_travel(session, agv_clock):
    _, request = session
    connection_id = open_simulator(request)
    assert send(request, 'query_status')[1]['status']['max_stations'] == 3
    assert send(request, 'call_station', connection_id, value=2)[0] == 400
    assert send(request, 'set_speed', connection_id, value=500)[0] == 400
    request(dict(action='arm', armed=True, connection_id=connection_id))
    assert send(request, 'call_station', connection_id, value=4)[0] == 400
    status, state = send(request, 'call_station', connection_id, value=2)
    assert status == 200
    assert state['results']['call_station']['state'] == agv.SENT
    agv_clock.now += 1
    assert request()[1]['results']['call_station']['ack_elapsed_s'] == pytest.approx(1)
    state = send(request, 'query_status')[1]
    assert not state['status']['stopped']
    assert state['status']['current_station'] == 1 and state['status']['target_station'] == 2
    agv_clock.now += 4
    state = send(request, 'query_status')[1]
    assert state['status']['current_station'] == 2 and state['status']['stopped']
    assert state['results']['call_station']['state'] == 'arrived'
    assert state['results']['call_station']['elapsed_s'] == pytest.approx(5)
    assert {'stopped', 'current_station', 'target_station'} <= {event['field'] for event in state['events']}


def test_stop_counter_cannot_confirm_a_station_call(session, agv_clock):
    _, request = session
    _, connected = request({'action': 'connect', 'mode': 'simulator', 'stations': 0})
    connection_id = connected['connection_id']
    assert send(request, 'query_status')[1]['status']['max_stations'] == 0
    request(dict(action='arm', armed=True, connection_id=connection_id))
    assert send(request, 'call_station', connection_id, value=1)[0] == 200
    agv_clock.now += 1
    assert request()[1]['results']['call_station']['ack_elapsed_s'] == pytest.approx(1)
    agv_clock.now += 5
    result = send(request, 'query_status')[1]['results']['call_station']
    assert result['state'] == 'not confirmed'
    assert 'maximum' in result['warning']


def test_ack_does_not_replace_status_and_manual_check_can_be_late(session, agv_clock):
    _, request = session
    connection_id = open_simulator(request)
    initial = send(request, 'query_status')[1]
    send(request, 'pause', auto_poll=False)
    agv_clock.now += 1
    state = request()[1]
    assert state['status'] == initial['status']
    assert state['status_age_s'] == 1
    assert state['results']['pause']['ack_elapsed_s'] == pytest.approx(1)
    agv_clock.now += 5
    state = request()[1]
    assert state['stale'] and state['results']['pause']['state'] == 'waiting for status'
    state = send(request, 'query_status')[1]
    assert state['results']['pause']['state'] == 'confirmed by status'
    assert [(e['after'], e['before']) for e in state['events'] if e['field'] == 'link_stale'] == [
        (True, False), (False, True),
    ]


def test_later_motion_supersedes_unchecked_stop(session, agv_clock):
    _, request = session
    connection_id = open_simulator(request)
    send(request, 'stop')
    send(request, 'pause')
    assert request()[1]['results']['stop']['state'] == 'not checked'


def test_status_events_record_both_edges_and_manual_fault_page(session, monkeypatch):
    fields = {'stopped': True, 'obstacle_stop': False, 'command_stop': False, 'paused': False,
              'current_station': 1, 'target_station': 1, 'fault_code': 0}

    class StatusLink(agv.Simulator):
        def write(self, raw):
            data = bytearray(24)
            data[0], data[13], data[14] = 1, 3, 2
            for offset, (field, _) in agv.STATUS_FIELDS.items():
                if field in fields:
                    data[offset - 4] = fields[field]
            self.buffer += agv.Frame(0x25, data).encode()
            return len(raw)

    monkeypatch.setattr(agv, 'Simulator', StatusLink)
    _, request = session
    connection_id = open_simulator(request)
    send(request, 'query_status', connection_id)
    original = fields.copy()
    fields.update(stopped=False, obstacle_stop=True, command_stop=True, paused=True,
                  current_station=2, target_station=3, fault_code=61)
    state = send(request, 'query_status', connection_id)[1]
    assert state['status']['fault_description'] == 'Off track'
    assert state['status']['fault_page'] == 16
    assert {(e['field'], e['after']) for e in state['events']} == set(fields.items())
    fields.update(original)
    state = send(request, 'query_status', connection_id)[1]
    assert {(e['field'], e['after']) for e in state['events'][-len(fields):]} == set(original.items())


@pytest.mark.parametrize(('number', 'message'), [(13, 'Log out and back in'), (16, 'Reseat the USB plug')])
def test_serial_open_errors_give_a_next_step(session, monkeypatch, number, message):
    def fail_open(**kwargs):
        raise OSError(number, 'driver diagnostic')
    monkeypatch.setattr(agv, 'serial_ports', lambda: [{'device': '/dev/fake', 'description': 'Test'}])
    monkeypatch.setattr(agv, 'serial', SimpleNamespace(Serial=fail_open))
    _, request = session
    code, denied = request(dict(action='connect', mode='serial', port='/dev/fake'))
    assert code == 400 and message in denied['error']
    assert 'driver diagnostic' not in denied['error']
    assert request()[1]['log'][-1]['decode']['error'].endswith('driver diagnostic')


def test_wrong_ack_id_is_rejected_and_transport_failure_has_an_alarm(session, monkeypatch):
    class BrokenLink(agv.Simulator):
        def write(self, raw):
            self.buffer += agv.Frame(0x25, b'\x02').encode()
            return len(raw)

        def read(self, size):
            if self.silent:
                raise OSError('driver diagnostic')
            return super().read(size)

    monkeypatch.setattr(agv, 'Simulator', BrokenLink)
    _, request = session
    connection_id = open_simulator(request)
    send(request, 'stop', connection_id)
    snapshot = request()[1]
    assert snapshot['status'] is None
    assert 'vehicle ID' in snapshot['log'][-1]['decode']['error']
    assert 'ack_elapsed_s' not in snapshot['results']['stop']
    request(dict(action='silence', silent=True, connection_id=connection_id))
    snapshot = request()[1]
    assert not snapshot['connected']
    assert snapshot['error'] == agv.LINK_LOST
    assert snapshot['events'][-1]['field'] == 'transport_error'


def test_stop_before_first_status_confirms_the_reported_stop(session):
    _, request = session
    connection_id = open_simulator(request)
    send(request, 'stop', connection_id)
    state = send(request, 'query_status', connection_id)[1]
    assert state['status']['stopped'] is True
    assert state['results']['stop']['state'] == 'confirmed by status'


def test_disconnecting_while_stale_does_not_report_that_radio_recovered(session, agv_clock):
    _, request = session
    connection_id = open_simulator(request)
    agv_clock.now += agv.STALE_AFTER
    assert request()[1]['stale']
    state = request({'action': 'disconnect', 'connection_id': connection_id})[1]
    assert state['events'][-1]['field'] == 'link_stale'
    assert state['events'][-1]['disconnected'] is True


def test_repeating_a_press_marks_the_previous_unchecked_press(session):
    _, request = session
    connection_id = open_simulator(request)
    _, first = send(request, 'pause', connection_id)
    _, second = send(request, 'pause', connection_id)
    name, replaced = second['replaced'][-1]
    assert name == 'pause' and replaced['state'] == 'not checked'
    assert replaced['tx_id'] == first['results']['pause']['tx_id']
    assert second['results']['pause']['tx_id'] != replaced['tx_id']


@pytest.mark.parametrize('command', ['stop', 'pause'])
def test_robot_interlock_refuses_motion_and_urgent_commands_bypass_it(session, monkeypatch, command):
    from anodizing_web import travel_pose
    owner, request = session
    owner.robot_configured = True
    connection_id = open_simulator(request)
    request(dict(action='arm', armed=True, connection_id=connection_id))
    entered, release = Event(), Event()

    def refuse(owner):
        entered.set()
        assert release.wait(2)
        return 'Robot is outside the saved AGV travel pose. Fold the arm before movement.'

    monkeypatch.setattr(travel_pose,'motion_block',refuse)
    with ThreadPoolExecutor(max_workers=2) as pool:
        motion=pool.submit(send,request,'forward',connection_id)
        try:
            assert entered.wait(1)
            start=monotonic()
            status,state=send(request,command)
            assert status==200 and state['results'][command]['state']==agv.SENT
            assert monotonic()-start < .050
        finally:
            release.set()
        status,state=motion.result(timeout=1)
    assert status==400 and 'travel pose' in state['error']


def test_stop_cancels_manual_motion_awaiting_travel_pose_check(session, monkeypatch):
    from anodizing_web import travel_pose
    owner, request = session
    owner.robot_configured = True
    connection_id = open_simulator(request)
    request(dict(action='arm', armed=True, connection_id=connection_id))
    entered, release = Event(), Event()

    def at_travel_pose(owner):
        entered.set()
        assert release.wait(2)
        return ''

    monkeypatch.setattr(travel_pose,'motion_block',at_travel_pose)
    with ThreadPoolExecutor(max_workers=2) as pool:
        motion = pool.submit(send,request,'forward',connection_id)
        try:
            assert entered.wait(1)
            assert send(request,'stop')[0] == 200
        finally:
            release.set()
        status,state = motion.result(timeout=1)
    assert status == 400 and 'cancelled' in state['error']
    assert 'forward' not in request()[1]['results']


def test_recipe_keeps_the_radio_reply_window_clear_of_ordinary_writes(agv_clock):
    link = agv.Connection(SimpleNamespace(closing=False, active=None, storage_error=None, execution_uncertain=False))
    link.submit({'action':'connect'})
    simulator = link.transport
    write = simulator.write
    writes = []

    def delayed_status(raw):
        command = agv.parse_frame(raw).command
        writes.append(command)
        return len(raw) if command == 0x26 else write(raw)

    simulator.write = delayed_status
    with link.recipe_link():
        link.recipe_feedback()
        agv_clock.now += .6
        link.recipe_feedback()
        assert writes == [0x26]
        assert link.recipe_call(2, lambda *args: None) is None
        write(agv.command_frame('query_status', None, simulator.prefix).encode())
        agv_clock.now = 10.75
        assert link.recipe_call(2, lambda *args: None) is not None
        agv_clock.now += .6
        status, _, _, ack = link.recipe_feedback()
        assert status['current_station'] == 1 and ack is None
        assert writes == [0x26, 0x23]
        agv_clock.now += .5
        _, _, _, ack = link.recipe_feedback()
        assert 1 <= ack < agv.REPLY_TIMEOUT
        assert writes == [0x26, 0x23, 0x26]
    link.close()


@pytest.mark.parametrize('agv_mode', [0, 2])
def test_recipe_receives_fresh_status_with_and_without_timed_reports(agv_clock, monkeypatch, agv_mode):
    monkeypatch.setattr(agv.Connection, '_start_worker', lambda *args, **kwargs: None)
    link = agv.Connection(SimpleNamespace(lock=RLock(), closing=False, active=None,
                                          storage_error=None, execution_uncertain=False))
    try:
        link.submit(dict(action='connect', mode='simulator', agv_mode=agv_mode))
        with link.recipe_link():
            for _ in range(16):
                agv_clock.now += .5
                status, status_at, _, _ = link.recipe_feedback()
                assert status is not None and agv_clock.now - status_at <= 1
    finally:
        link.close()


def command_writes(state, name):
    return [entry for entry in state['log']
            if entry['direction'] == 'TX' and entry['decode']['command'] == name]


@pytest.mark.parametrize(('command', 'interval', 'limit', 'reply_wait'), [
    ('stop', 1.5, 5, 1), ('pause', 1.5, 5, 1), ('forward', 2, 4, 2),
])
def test_unanswered_commands_resend_the_identical_frame_to_the_try_limit(
        session, agv_clock, command, interval, limit, reply_wait):
    _, request = session
    connection_id = open_simulator(request)
    request(dict(action='silence', silent=True, connection_id=connection_id))
    request(dict(action='arm', armed=True, connection_id=connection_id))

    status, state = send(request, command, connection_id)
    assert status == 200
    first, = command_writes(state, command)
    for attempt in range(2, limit + 1):
        agv_clock.now += interval - .01
        assert len(command_writes(request()[1], command)) == attempt - 1
        agv_clock.now += .01
        assert len(command_writes(request()[1], command)) == attempt

    agv_clock.now += reply_wait
    state = request()[1]
    writes = command_writes(state, command)
    assert [entry['hex'] for entry in writes] == [first['hex']] * limit
    assert [entry['decode']['try'] for entry in writes] == list(range(1, limit + 1))
    assert state['results'][command]['state'] == 'no reply'
    quality = state['link_quality']
    assert (quality['sent'], quality['acknowledged'], quality['resent'], quality['no_reply']) == (
        limit, 0, limit - 1, 1)
    agv_clock.now += 20
    assert len(command_writes(request()[1], command)) == limit


def test_retry_acknowledgement_ends_resends_without_confirming_status(session, agv_clock):
    _, request = session
    connection_id = open_simulator(request)
    request(dict(action='silence', silent=True, connection_id=connection_id))
    send(request, 'stop', connection_id)
    request(dict(action='silence', silent=False, connection_id=connection_id))
    agv_clock.now += 1.5
    assert len(command_writes(request()[1], 'stop')) == 2

    agv_clock.now += 1
    state = request()[1]
    assert state['results']['stop']['ack_elapsed_s'] == pytest.approx(1)
    assert state['status'] is None
    assert state['link_quality']['acknowledged'] == 1
    agv_clock.now += 10
    assert len(command_writes(request()[1], 'stop')) == 2


@pytest.mark.parametrize(('action', 'fields'), [
    ('send', {'command': 'stop'}),
    ('send', {'command': 'pause'}),
    ('arm', {'armed': False}),
    ('disconnect', {}),
])
def test_urgent_controls_disarm_and_disconnect_cancel_pending_motion_resends(
        session, agv_clock, action, fields):
    _, request = session
    connection_id = open_simulator(request)
    request(dict(action='silence', silent=True, connection_id=connection_id))
    request(dict(action='arm', armed=True, connection_id=connection_id))
    assert send(request, 'forward', connection_id)[0] == 200
    agv_clock.now += 2
    assert len(command_writes(request()[1], 'forward')) == 2

    assert request(dict(action=action, connection_id=connection_id, **fields))[0] == 200
    for _ in range(5):
        agv_clock.now += 2
        assert len(command_writes(request()[1], 'forward')) == 2
    if action == 'disconnect':
        assert open_simulator(request) != connection_id
        agv_clock.now += 2
        assert not command_writes(request()[1], 'forward')


def test_stop_status_ends_resends_even_if_the_ack_was_lost(session, agv_clock, monkeypatch):
    class StatusWithoutAck(agv.Simulator):
        def write(self, raw):
            written = super().write(raw)
            self.acknowledgements.clear()
            return written

    monkeypatch.setattr(agv, 'Simulator', StatusWithoutAck)
    _, request = session
    connection_id = open_simulator(request)
    send(request, 'query_status', connection_id)
    send(request, 'stop', connection_id)
    agv_clock.now += 1.5
    assert len(command_writes(request()[1], 'stop')) == 2

    state = send(request, 'query_status', connection_id)[1]
    assert state['results']['stop']['state'] == 'confirmed by status'
    assert 'ack_elapsed_s' not in state['results']['stop']
    agv_clock.now += 2
    assert len(command_writes(request()[1], 'stop')) == 2


def test_recipe_station_call_uses_shared_resend_and_urgent_cancel(agv_clock):
    link = agv.Connection(SimpleNamespace(lock=RLock(), closing=False, active=None,
                                          storage_error=None, execution_uncertain=False))
    link.submit({'action': 'connect'})
    simulator = link.transport
    real_write = simulator.write

    def drop_calls(raw):
        frame = agv.parse_frame(raw)
        return len(raw) if frame.command == 0x23 and frame.data[0] == 0x9D else real_write(raw)

    simulator.write = drop_calls
    try:
        with link.recipe_link():
            link.recipe_feedback()
            agv_clock.now += .75
            assert link.recipe_call(2, lambda *args: None) is not None
            agv_clock.now += 2
            link.recipe_feedback()
            first, retry = command_writes(link.capability(), 'call_station')
            assert first['hex'] == retry['hex']
            assert retry['decode']['try'] == 2
            assert link.recipe_stop()
            agv_clock.now += 2
            assert len(command_writes(link.capability(), 'call_station')) == 2
    finally:
        link.close()


def test_late_motion_ack_does_not_prevent_stop_resend(session, agv_clock, monkeypatch):
    class DropFirstStop(agv.Simulator):
        drop_stop = True

        def write(self, raw):
            frame = agv.parse_frame(raw)
            if self.drop_stop and frame.command == 0x23 and frame.data == b'\x87\x04\x00':
                self.drop_stop = False
                return len(raw)
            return super().write(raw)

    monkeypatch.setattr(agv, 'Simulator', DropFirstStop)
    _, request = session
    connection_id = open_simulator(request)
    request(dict(action='arm', armed=True, connection_id=connection_id))
    send(request, 'forward', connection_id)
    agv_clock.now += .9
    send(request, 'stop', connection_id)
    agv_clock.now += .1

    state = request()[1]
    assert 'ack_elapsed_s' not in state['results']['stop']
    agv_clock.now += 1.41
    state = request()[1]
    assert len(command_writes(state, 'stop')) == 2
    assert len(command_writes(state, 'forward')) == 1


def test_standard_simulator_reports_status_without_requests_and_rejects_station_calls(session, agv_clock):
    _, request = session
    _, connected = request(dict(action='connect', mode='simulator', agv_mode=0))
    for _ in range(4):
        agv_clock.now += 1
        state = request()[1]
    assert state['status']['mode'] == 0 and not state['stale']
    assert state['link_quality']['timed_report_interval_s'] == pytest.approx(1)
    assert not [entry for entry in state['log'] if entry['direction'] == 'TX']
    cid = connected['connection_id']
    assert send(request, 'query_status', cid)[1]['results']['query_status']['state'] == 'status received'
    request(dict(action='arm', armed=True, connection_id=cid))
    status, refused = send(request, 'call_station', cid, value=1)
    assert status == 400 and 'station-call function' in refused['error']


def test_query_only_simulator_gets_background_status_without_page_requests(session):
    _, request = session
    open_simulator(request)
    Event().wait(6.2)
    state = request()[1]
    assert len(command_writes(state, 'query_status')) >= 3
    assert state['status_age_s'] < 2.5
    assert not state['stale']
    assert not [event for event in state['events'] if event['field'] == 'link_stale' and event['after']]


def test_one_missed_poll_keeps_status_age_below_three_seconds(agv_clock, monkeypatch):
    class DelayedReply(agv.Simulator):
        queries = 0
        reply_at = None

        def write(self, raw):
            if agv.parse_frame(raw, offset=3).command == 0x26:
                self.queries += 1
                if self.queries != 2:
                    self.reply_at = agv_clock.now + .83
                return len(raw)
            return super().write(raw)

        @property
        def in_waiting(self):
            if self.reply_at is not None and agv_clock.now >= self.reply_at:
                self._report()
                self.reply_at = None
            return super().in_waiting

    monkeypatch.setattr(agv, 'Simulator', DelayedReply)
    monkeypatch.setattr(agv.Connection, '_start_worker', lambda *args, **kwargs: None)
    link = agv.Connection(SimpleNamespace(lock=RLock(), closing=False, active=None,
                                          storage_error=None, execution_uncertain=False))
    try:
        link.submit(dict(action='connect'))
        max_age = 0
        for step in range(800):
            agv_clock.now = 10 + step / 100
            if link.status_at is not None:
                max_age = max(max_age, agv_clock.now - link.status_at)
            with link.lock:
                link._receive()
            link._poll_status(link.transport)
        assert link.transport.queries >= 3
        assert max_age < 3, f'maximum status age was {max_age:.3f} s'
    finally:
        link.close()


def test_query_replies_do_not_extend_timed_report_presence(agv_clock, monkeypatch):
    monkeypatch.setattr(agv.Connection, '_start_worker', lambda *args, **kwargs: None)
    link = agv.Connection(SimpleNamespace(lock=RLock(), closing=False, active=None,
                                          storage_error=None, execution_uncertain=False))
    try:
        link.submit(dict(action='connect', mode='simulator', agv_mode=0))
        for _ in range(4):
            agv_clock.now += 1
            state = link.capability()
        assert state['link_quality']['timed_report_interval_s'] == pytest.approx(1)
        link.transport.mode = 2
        last_report = agv_clock.now
        for offset in (.12, 1.7):
            agv_clock.now = last_report + offset
            link.submit(dict(action='send', command='query_status', connection_id=link.id))
        agv_clock.now = last_report + 2.49
        assert link.capability()['link_quality']['timed_report_interval_s'] == pytest.approx(1)
        agv_clock.now = last_report + 2.51
        state = link.capability()
        assert state['status_age_s'] < 1
        assert state['link_quality']['timed_report_interval_s'] is None
    finally:
        link.close()


def test_background_read_protects_reply_window_for_stop(agv_clock, monkeypatch):
    class NoStatusReply(agv.Simulator):
        def write(self, raw):
            if agv.parse_frame(raw, offset=3).command == 0x26:
                return len(raw)
            return super().write(raw)

    monkeypatch.setattr(agv, 'Simulator', NoStatusReply)
    monkeypatch.setattr(agv.Connection, '_start_worker', lambda *args, **kwargs: None)
    link = agv.Connection(SimpleNamespace(lock=RLock(), closing=False, active=None,
                                          storage_error=None, execution_uncertain=False))
    try:
        link.submit(dict(action='connect'))
        agv_clock.now += agv.REPLY_TIMEOUT
        link._poll_status(link.transport)
        started = agv_clock.now
        link.submit(dict(action='send', command='stop'))
        assert .90 <= agv_clock.now - started <= 1.05
        link.submit(dict(action='send', command='read_speed', connection_id=link.id))
        assert agv_clock.now - started >= agv.REPLY_TIMEOUT
    finally:
        link.close()


def test_timed_reports_pace_ordinary_send_but_never_delay_stop(session, agv_clock):
    _, request = session
    request(dict(action='connect', mode='simulator', agv_mode=0))
    for _ in range(4):
        agv_clock.now += 1
        request()
    report_at = agv_clock.now

    assert send(request, 'read_speed')[0] == 200
    assert .099 <= agv_clock.now - report_at <= .150

    stopped_at = agv_clock.now
    assert send(request, 'stop')[0] == 200
    assert agv_clock.now == stopped_at


def test_stop_retry_cannot_collide_with_the_first_ack(session, monkeypatch, agv_clock):
    class HalfDuplex(agv.Simulator):
        def write(self, raw):
            now = agv_clock.now
            tx_end = now + .289
            ack = self.acknowledgements[0] if self.acknowledgements else None
            if ack is not None and now < ack and tx_end > ack - .289:
                self.acknowledgements.popleft()
                return len(raw)
            if now < self.next_report and tx_end > self.next_report - .494:
                return len(raw)
            written = super().write(raw)
            self.acknowledgements[-1] = now + .611
            return written

    monkeypatch.setattr(agv, 'Simulator', HalfDuplex)
    _, request = session
    request(dict(action='connect', mode='simulator', agv_mode=0))
    for _ in range(4):
        agv_clock.now += 1
        request()
    agv_clock.now += .067
    send(request, 'stop')
    agv_clock.now += .043
    request()
    agv_clock.now += .58
    state = request()[1]
    assert state['results']['stop'].get('ack_elapsed_s') == pytest.approx(.623)
    assert len(command_writes(state, 'stop')) == 1


@pytest.mark.parametrize(('delay', 'late'), [(.102, True), (.301, False)])
def test_final_stop_ack_grace_window(session, monkeypatch, agv_clock, delay, late):
    class FinalAck(agv.Simulator):
        writes = 0

        def write(self, raw):
            self.writes += 1
            if self.writes == 5:
                self.acknowledgements.append(agv_clock.now + 1 + delay)
            return len(raw)

    monkeypatch.setattr(agv, 'Simulator', FinalAck)
    _, request = session
    open_simulator(request)
    send(request, 'stop')
    for _ in range(4):
        agv_clock.now += 1.5
        request()
    agv_clock.now += 1
    assert request()[1]['results']['stop']['state'] == 'no reply'
    agv_clock.now += delay + .001
    state = request()[1]
    assert len(command_writes(state, 'stop')) == 5
    assert state['results']['stop']['state'] == ('acknowledged late' if late else 'no reply')
    assert state['link_quality']['acknowledged'] == int(late)
    assert state['link_quality']['no_reply'] == int(not late)
    assert state['log'][-1]['decode'].get('acknowledged_command') == ('stop' if late else None)


@pytest.mark.parametrize(('command', 'initially_stopped', 'press_phase'), [
    ('stop', False, .02), ('pause', False, .995), ('stop', True, .995),
])
def test_urgent_retry_reaches_the_next_report_gap(session, monkeypatch, agv_clock, command,
                                                 initially_stopped, press_phase):
    class PhaseLossSimulator(agv.Simulator):
        delivered = None

        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.stopped = initially_stopped
            self.command_stop = initially_stopped
            instances.append(self)

        def write(self, raw):
            frame = agv.parse_frame(raw, offset=3)
            if frame.command == 0x23 and frame.data[0] in (0x87, 0x5B):
                phase = agv_clock.now - (self.next_report - 1)
                if phase < .05 or phase + .015 >= 1:
                    return len(raw)
                self.delivered = agv_clock.now
            return super().write(raw)

    instances = []
    monkeypatch.setattr(agv, 'Simulator', PhaseLossSimulator)
    _, request = session
    request(dict(action='connect', mode='simulator', agv_mode=0))
    for _ in range(4):
        agv_clock.now += 1
        request()
    last_report = agv_clock.now
    agv_clock.now += press_phase
    pressed_at = agv_clock.now
    status, state = send(request, command)
    assert status == 200 and len(command_writes(state, command)) == 1
    assert agv_clock.now == pressed_at and instances[0].delivered is None

    early_report = last_report + (press_phase >= agv.REPORT_GAP[0])
    agv_clock.now = max(early_report, pressed_at)
    request()
    agv_clock.now = early_report + .12
    assert len(command_writes(request()[1], command)) == 1
    gap_report = early_report + 1
    agv_clock.now = gap_report
    request()
    agv_clock.now = gap_report + .09
    request()
    assert instances[0].delivered is None
    agv_clock.now = gap_report + .12
    state = request()[1]
    assert len(command_writes(state, command)) == 2
    assert instances[0].delivered == agv_clock.now
    interval = state['link_quality']['timed_report_interval_s']
    assert instances[0].delivered - pressed_at <= interval + agv.REPORT_GAP[1] + .1
    agv_clock.now = gap_report + 1.13
    state = request()[1]
    assert state['results'][command]['acknowledged_at']
    assert len(command_writes(state, command)) == 2


def test_duplicate_stop_does_not_postpone_the_pending_retry(session, agv_clock):
    _, request = session
    connection_id = open_simulator(request)
    request(dict(action='silence', silent=True, connection_id=connection_id))
    first = send(request, 'stop', connection_id)[1]['results']['stop']
    agv_clock.now += .9
    repeated = send(request, 'stop', connection_id)[1]
    assert repeated['results']['stop']['tx_id'] == first['tx_id']
    assert len(command_writes(repeated, 'stop')) == 1
    agv_clock.now += .61
    state = request()[1]
    assert len(command_writes(state, 'stop')) == 2
    assert state['results']['stop']['tries'] == 2


def test_auto_poll_does_not_keep_postponing_stop_retries(session, agv_clock):
    _, request = session
    connection_id = open_simulator(request)
    request(dict(action='silence', silent=True, connection_id=connection_id))
    send(request, 'stop')
    agv_clock.now += 1
    status, state = send(request, 'query_status', connection_id, background=True)
    assert status == 200 and not command_writes(state, 'query_status')
    agv_clock.now += .5
    assert len(command_writes(request()[1], 'stop')) == 2


@pytest.mark.parametrize('action', ['disarm', 'disconnect', 'link_change'])
def test_pending_urgent_retry_is_cancelled_when_link_is_disarmed_or_changed(session, agv_clock, action):
    _, request = session
    _, state = request(dict(action='connect', mode='simulator', agv_mode=0))
    connection_id = state['connection_id']
    for _ in range(4):
        agv_clock.now += 1
        request()
    agv_clock.now += .03
    send(request, 'stop', connection_id)
    if action == 'disarm':
        request(dict(action='arm', armed=False, connection_id=connection_id))
    else:
        request(dict(action='disconnect', connection_id=connection_id))
        if action == 'link_change':
            assert open_simulator(request) != connection_id
    for delay in (.09, 1, 1):
        agv_clock.now += delay
        state = request()[1]
        assert len(command_writes(state, 'stop')) == (0 if action == 'link_change' else 1)


@pytest.mark.parametrize('command', ['stop', 'pause'])
def test_timed_reports_allow_four_urgent_gap_retries_before_no_reply(session, monkeypatch, agv_clock, command):
    class DropUrgent(agv.Simulator):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.stopped = False

        def write(self, raw):
            frame = agv.parse_frame(raw, offset=3)
            if frame.command == 0x23 and frame.data[0] in (0x87, 0x5B):
                return len(raw)
            return super().write(raw)

    monkeypatch.setattr(agv, 'Simulator', DropUrgent)
    _, request = session
    request(dict(action='connect', mode='simulator', agv_mode=0))
    for _ in range(4):
        agv_clock.now += 1
        request()
    agv_clock.now += .25
    send(request, command)
    for attempt in range(2, 6):
        agv_clock.now = int(agv_clock.now) + 1
        request()
        agv_clock.now += .11
        assert len(command_writes(request()[1], command)) == attempt
    agv_clock.now = int(agv_clock.now) + 1
    request()
    agv_clock.now += .11
    state = request()[1]
    assert len(command_writes(state, command)) == 5
    assert state['results'][command]['state'] == 'no reply'


def test_late_timed_reports_fall_back_to_urgent_retry_timer(session, agv_clock):
    _, request = session
    _, state = request(dict(action='connect', mode='simulator', agv_mode=0))
    cid = state['connection_id']
    for _ in range(4):
        agv_clock.now += 1
        request()
    request(dict(action='silence', silent=True, connection_id=cid))
    agv_clock.now += .1
    send(request, 'stop', cid)
    agv_clock.now += 1.49
    assert len(command_writes(request()[1], 'stop')) == 1
    agv_clock.now += .02
    state = request()[1]
    assert len(command_writes(state, 'stop')) == 2
    agv_clock.now += 1.49
    assert len(command_writes(request()[1], 'stop')) == 2
    agv_clock.now += .02
    assert len(command_writes(request()[1], 'stop')) == 3


def test_recipe_stop_retries_in_the_next_report_gap_after_recipe_exit(agv_clock):
    owner = SimpleNamespace(lock=RLock(), closing=False, active=None, storage_error=None,
                            execution_uncertain=False)
    link = agv.Connection(owner)
    link.submit(dict(action='connect', mode='simulator', agv_mode=0))
    try:
        for _ in range(4):
            agv_clock.now += 1
            link.capability()
        link.transport.loss_fraction = 1
        agv_clock.now += .03
        with link.recipe_link():
            assert link.recipe_stop()
            first, = command_writes(link.capability(), 'stop')
            assert first['decode']['try'] == 1
        link.transport.loss_fraction = 0
        agv_clock.now += .09
        assert len(command_writes(link.capability(), 'stop')) == 1
        agv_clock.now = 15
        link.capability()
        agv_clock.now += .12
        state = link.capability()
        assert [tx['decode']['try'] for tx in command_writes(state, 'stop')] == [1, 2]
        assert link.transport.command_stop
    finally:
        link.close()


@pytest.fixture
def half_duplex(monkeypatch, agv_clock):
    class HalfDuplex(agv.Simulator):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.pending_status = None
            self.writes = []
            self.delay = .85
            self.ack_delay = .61

        def write(self, raw):
            now = agv_clock.now
            frame = agv.parse_frame(raw, offset=3)
            self.writes.append((frame.command, now))
            if ((self.pending_status is not None and now < self.pending_status)
                    or (self.acknowledgements and now < self.acknowledgements[0])):
                self.pending_status = None
                self.acknowledgements.clear()
                return len(raw)
            if frame.command == 0x26:
                self.pending_status = now + self.delay
                return len(raw)
            result = super().write(raw)
            if frame.command == 0x23 and self.acknowledgements:
                self.acknowledgements[-1] = now + self.ack_delay
            return result

        @property
        def in_waiting(self):
            if self.pending_status is not None and agv_clock.now >= self.pending_status:
                self.pending_status = None
                self._report()
            return super().in_waiting

    monkeypatch.setattr(agv, 'Simulator', HalfDuplex)
    monkeypatch.setattr(agv.Connection, '_start_worker', lambda *args, **kwargs: None)
    return HalfDuplex


def test_random_stop_phases_ack_within_latency_targets(agv_clock, half_duplex):
    rng = random.Random(20261120)
    latencies = []
    for _ in range(30):
        agv_clock.now = 10.0
        link = agv.Connection(SimpleNamespace(lock=RLock(), closing=False, active=None,
                                              storage_error=None, execution_uncertain=False))
        try:
            link.submit(dict(action='connect'))
            agv_clock.now += agv.REPLY_TIMEOUT
            link.transport.delay = rng.uniform(.83, .88)
            link._poll_status(link.transport)
            agv_clock.now += rng.uniform(0, agv.PAGE_POLL_S)
            pressed = agv_clock.now
            link.submit(dict(action='send', command='stop'))
            for _ in range(400):
                agv_clock.now += .01
                with link.lock:
                    link._receive()
                link._service_resend()
                link._poll_status(link.transport)
                result = link.results['stop']
                if result.get('acknowledged_at'):
                    latencies.append(agv_clock.now - pressed)
                    break
            assert result.get('acknowledged_at'), result
        finally:
            link.close()
    median, worst = statistics.median(latencies), max(latencies)
    print(f'{len(latencies)}/30 ACKs, median {median:.3f} s, worst {worst:.3f} s')
    assert median <= 1.2, latencies
    assert worst <= 1.8, latencies


@pytest.mark.parametrize('command', ['pause', 'recipe_stop'])
@pytest.mark.parametrize(('delay', 'expected'), [(.83, .83), (2.0, .95)])
def test_urgent_waits_for_status_or_cutoff(agv_clock, half_duplex, command, delay, expected):
    link = agv.Connection(SimpleNamespace(lock=RLock(), closing=False, active=None,
                                          storage_error=None, execution_uncertain=False))
    try:
        link.submit(dict(action='connect'))
        link.transport.delay = delay
        with link.recipe_link() if command == 'recipe_stop' else nullcontext():
            if command == 'recipe_stop':
                link.recipe_feedback()
            else:
                agv_clock.now += agv.REPLY_TIMEOUT
                link._poll_status(link.transport)
            started = agv_clock.now
            if command == 'recipe_stop':
                assert link.recipe_stop()
            else:
                link.submit(dict(action='send', command=command))
            urgent_at = next(at for code, at in link.transport.writes if code == 0x23)
            assert urgent_at - started == pytest.approx(expected, abs=.03)
    finally:
        link.close()


def test_recipe_one_lost_poll_keeps_status_fresh(agv_clock, half_duplex):
    link = agv.Connection(SimpleNamespace(lock=RLock(), closing=False, active=None,
                                          storage_error=None, execution_uncertain=False))
    try:
        link.submit(dict(action='connect'))
        max_age = 0
        dropped = False
        with link.recipe_link():
            for step in range(80):
                agv_clock.now = 10 + step / 10
                if link.status_at is not None:
                    max_age = max(max_age, agv_clock.now - link.status_at)
                link.recipe_feedback()
                if not dropped and len([code for code, _ in link.transport.writes if code == 0x26]) == 2:
                    link.transport.pending_status = None
                    dropped = True
        assert dropped
        print(f'Peak recipe status age with one missed poll: {max_age:.3f} s')
        assert max_age < 2.8
    finally:
        link.close()


@pytest.mark.parametrize('poll', ['worker', 'browser', 'motion', 'recipe'])
def test_automatic_poll_waits_for_stop_ack_after_status_confirmation(agv_clock, half_duplex, poll):
    link = agv.Connection(SimpleNamespace(lock=RLock(), closing=False, active=None,
                                          storage_error=None, execution_uncertain=False))
    try:
        link.submit(dict(action='connect'))
        agv_clock.now += agv.REPLY_TIMEOUT
        link.transport.ack_delay = 1
        with link.recipe_link() if poll == 'recipe' else nullcontext():
            if poll == 'recipe':
                link.recipe_feedback()
            else:
                link._poll_status(link.transport)
            agv_clock.now += .86
            if poll == 'recipe':
                assert link.recipe_stop()
            else:
                link.submit(dict(action='send', command='stop'))
            link.transport._report()
            agv_clock.now += .05
            assert link.capability()['results']['stop']['state'] == 'confirmed by status'
            agv_clock.now += .75
            if poll == 'recipe':
                link.recipe_feedback()
            elif poll == 'motion':
                link.motion_feedback(poll=True)
            elif poll == 'worker':
                link._poll_status(link.transport)
            else:
                link.submit(dict(action='send', command='query_status', background=True))
            assert [code for code, _ in link.transport.writes] == [0x26, 0x23]
            agv_clock.now += .21
            assert 'ack_elapsed_s' in link.capability()['results']['stop']
    finally:
        link.close()
