"""Exercise the authenticated HTTP handler with an in-process AGV, without sockets or devices."""
import ast
from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
from email.message import Message
import errno
from http.server import BaseHTTPRequestHandler
from io import BytesIO
import json
import os
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
from agv_examples import EXAMPLE_PREFIX, EXAMPLE_REPLY_PREFIX, EXAMPLE_ROUTE, EXAMPLE_SPEED, synthetic_replies


source = Path(__file__).resolve().parents[1] / 'anodizing_web/server.py'
handler = next(node for node in ast.parse(source.read_text()).body
               if isinstance(node, ast.ClassDef) and node.name == 'Handler')
namespace = dict(BaseHTTPRequestHandler=BaseHTTPRequestHandler, json=json, os=os, secrets=secrets,
                 urlsplit=urlsplit, MAX_BODY=32768)
exec(compile(ast.Module(body=[handler], type_ignores=[]), str(source), 'exec'), namespace)


@pytest.fixture
def session(tmp_path, request):
    if hasattr(request, 'param'):
        (tmp_path / 'site-profile.json').write_text(json.dumps(request.param))
    owner = SimpleNamespace(lock=RLock(), active=None, closing=False, storage_error=None,
                            execution_uncertain=False, root=tmp_path)
    connection = agv.Connection(owner)

    def exchange(body=None, *, token='secret', origin=None):
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
        exchange.stop_generation = h.response[1].get('stop_generation', exchange.stop_generation)
        return h.response

    exchange.connection = connection
    exchange.stop_generation = 0
    try:
        yield owner, exchange
    finally:
        connection.close()


def open_simulator(request, *, agv_mode=2, **route):
    status, state = request({'action': 'connect', 'agv_mode': agv_mode, **route})
    assert status == 200 and state['connected'] and state['settings']['mode'] == 'simulator'
    assert state['armed'] is False and state['stale'] is False
    return state['connection_id']


def send(request, command, connection_id=None, **fields):
    if command not in agv.READS and command not in agv.URGENT:
        fields.setdefault('stop_generation', request.stop_generation)
    return request(dict(action='send', command=command, connection_id=connection_id, **fields))


def test_set_speed_argument_defaults_to_slowest_speed(session):
    _, request = session
    status, state = request()
    assert status == 200
    argument = next(command['argument'] for command in state['commands'] if command['name'] == 'set_speed')
    assert argument['default'] == argument['min'] == 500


def test_simulator_calls_parking_on_a_route_with_sixteen_learned_legs(session):
    _, request = session
    code, state = request({'action': 'connect', 'mode': 'simulator', 'stations': 17})
    assert code == 200
    connection_id = state['connection_id']
    code, state = send(request, 'query_status', connection_id)
    assert code == 200
    assert state['status']['max_stations'] == 16
    assert state['station_profile']['parking_station'] == 17
    assert request(dict(action='arm', armed=True, connection_id=connection_id))[0] == 200
    assert send(request, 'call_station', connection_id, value=18)[0] == 400
    code, state = send(request, 'call_station', connection_id, value=17)
    assert code == 200
    code, state = send(request, 'query_status', connection_id)
    assert code == 200
    assert state['status']['target_station'] == 17


@pytest.mark.parametrize(('session', 'parking'), [
    ({'agv': {'parking_station': configured, 'station_labels': {
            '1': ' Load bay ', '2': 42, '0': 'Invalid', '256': 'Invalid',
        }, 'unused': 'Private'},
       'robot_control': {'robot_ip': 'Private'}}, expected)
    for configured, expected in [(2, 2), (True, 17), (256, 17)]
], indirect=['session'])
def test_exposes_only_validated_agv_display_fields_from_the_local_profile(session, parking):
    _, request = session
    _, state = request()
    assert state['station_profile'] == {'parking_station': parking, 'station_labels': {'1': 'Load bay'}}


def test_motion_requires_arming_for_the_current_connection(session):
    _, request = session
    first = open_simulator(request)
    assert 'Armed' in send(request, 'forward', first)[1]['error']
    send(request, 'query_status')
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
            reply = agv.Frame(0x21, EXAMPLE_SPEED.to_bytes(2, 'little')).encode()
            status, state = pool.submit(send, request, 'stop', connection_id).result(timeout=1)
            assert status == 200
            assert state['results']['read_speed']['state'] == f'reply: {EXAMPLE_SPEED} m/h'

        monkeypatch.setattr(agv, 'sleep', interleave)
        status, state = send(request, 'read_speed', connection_id)

    assert status == 200
    assert state['results']['read_speed']['state'] == f'reply: {EXAMPLE_SPEED} m/h'


def test_simulator_reports_motion_speed_target_pause_and_stop(session, agv_clock):
    _, request = session
    connection_id = open_simulator(request)
    send(request, 'query_status')
    request(dict(action='arm', armed=True, connection_id=connection_id))
    assert send(request, 'set_speed', connection_id, value=1257)[1]['results']['set_speed']['state'] == agv.SENT
    state = send(request, 'query_status')[1]
    assert state['status']['set_speed_m_h'] == 1257
    assert state['results']['set_speed']['state'] == 'confirmed by status'
    assert send(request, 'read_speed')[1]['log'][-1]['decode'] == {
        'read_speed_m_h': 1257, 'reply_prefix': agv.hex_bytes(agv.reply_prefix(agv.DEFAULT_PREFIX)),
    }
    assert send(request, 'call_station', connection_id, value=2)[0] == 200
    state = send(request, 'query_status')[1]
    assert state['status']['target_station'] == 2
    assert state['status']['actual_speed_m_h'] == 1257
    agv_clock.now += .3
    for command, stopped, paused in [('pause', True, True), ('resume', False, False), ('stop', True, True)]:
        code, state = send(request, command, connection_id)
        assert code == 200
        agv_clock.now += 1.01
        state = send(request, 'query_status')[1]
        assert (state['status']['stopped'], state['status']['paused']) == (stopped, paused)
        assert state['results']['pause' if command == 'stop' else command]['state'] == 'confirmed by status'


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
    open_simulator(request, agv_mode=0)
    send(request, 'query_status')
    send(request, 'stop')
    agv_clock.now += 1.01
    assert request()[1]['results']['stop']['state'] == 'confirmed by status'
    send(request, 'stop')
    agv_clock.now += 1.01
    assert request()[1]['results']['stop']['state'] == 'confirmed by status'


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


def test_synthetic_replies_confirm_and_only_status_refreshes_the_link(session, monkeypatch):
    clock = SimpleNamespace(now=10.0)
    monkeypatch.setattr(agv, 'monotonic', lambda: clock.now)
    status_reply, speed_reply, unidentified = synthetic_replies(EXAMPLE_PREFIX)

    class ExampleReplies(agv.Simulator):
        def write(self, raw):
            command = agv.parse_frame(raw).command
            self.buffer += {0x26: status_reply, 0x21: speed_reply + unidentified}[command]
            return len(raw)

    monkeypatch.setattr(agv, 'Simulator', ExampleReplies)
    _, request = session
    connection_id = open_simulator(request, **EXAMPLE_ROUTE)
    state = send(request, 'query_status', connection_id)[1]
    assert state['status_hex'] == status_reply.hex(' ').upper()
    assert state['status']['reply_prefix'] == agv.hex_bytes(EXAMPLE_REPLY_PREFIX)
    assert state['results']['query_status']['state'] == 'status received'

    clock.now += 4
    state = send(request, 'query_status', connection_id)[1]
    assert state['status_age_s'] == 0 and not state['stale']
    clock.now += 2
    state = send(request, 'read_speed', connection_id)[1]
    reply, unknown = state['log'][-2:]
    assert reply['hex'] == speed_reply.hex(' ').upper()
    assert reply['decode']['reply_prefix'] == agv.hex_bytes(EXAMPLE_REPLY_PREFIX) and reply['decode']['read_speed_m_h'] == EXAMPLE_SPEED
    assert unknown['hex'] == unidentified.hex(' ').upper()
    assert unknown['decode']['description'] == 'unidentified frame 01 (not in the XBOT protocol)'
    assert 'error' not in unknown['decode']
    assert state['status_age_s'] == 2 and not state['stale']
    assert state['results']['read_speed']['state'] == f'reply: {EXAMPLE_SPEED} m/h'

    clock.now += agv.STALE_AFTER + .01
    state = send(request, 'read_speed', connection_id)[1]
    assert state['status_age_s'] == pytest.approx(7.01) and state['stale']
    assert state['results']['read_speed']['state'] == f'reply: {EXAMPLE_SPEED} m/h'


def test_simulator_generates_replies_and_reports_01_periodically(session, agv_clock, monkeypatch):
    agv_clock.now = 0.0
    monkeypatch.setattr(agv, 'UNIDENTIFIED_INTERVAL', 8)
    _, request = session
    # Real-time polling can enqueue status frames when the fake clock jumps.
    monkeypatch.setattr(request.connection, '_start_worker', lambda **kwargs: None)
    connection_id = open_simulator(request, **EXAMPLE_ROUTE)
    assert request()[1]['log'] == []

    state = send(request, 'query_status', connection_id)[1]
    assert state['status_hex'].startswith(f'{agv.hex_bytes(EXAMPLE_REPLY_PREFIX)} A5 5A 1D 25 ')
    assert state['status']['obstacle_input'] == 0xCC
    assert not {'attachment_active', 'roller_speed'} & state['status'].keys()
    assert send(request, 'read_speed', connection_id)[1]['log'][-1]['hex'] == agv.Frame(0x21, (1500).to_bytes(2, 'little'), EXAMPLE_REPLY_PREFIX).encode().hex(' ').upper()

    for tick in (8, 16):
        agv_clock.now = tick - .01
        before = request()[1]['log']
        agv_clock.now = tick
        state = request()[1]
        assert len(state['log']) == len(before) + 1
        assert state['log'][-1]['hex'] == agv.Frame(0x01, b'').encode().hex(' ').upper()
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
    connection_id = open_simulator(request, **EXAMPLE_ROUTE)
    state = send(request, 'read_speed', connection_id)[1]
    echo, reply = state['log'][-2:]
    assert echo['hex'] == agv.command_frame('read_speed', None, EXAMPLE_PREFIX).encode().hex(' ').upper()
    assert echo['decode'] == {'description': 'echo of our transmission'}
    assert reply['hex'] == agv.Frame(0x21, (1500).to_bytes(2, 'little'), EXAMPLE_REPLY_PREFIX).encode().hex(' ').upper()
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
                self.buffer += agv.Frame(0x21, EXAMPLE_SPEED.to_bytes(2, 'little')).encode()
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
    assert state['log'][-1]['decode'] == {'read_speed_m_h': EXAMPLE_SPEED}
    assert state['results']['read_speed']['state'] == f'reply: {EXAMPLE_SPEED} m/h'
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
    _, connected = request({'action': 'connect', 'mode': 'simulator', 'stations': 2})
    connection_id = connected['connection_id']
    assert send(request, 'query_status')[1]['status']['max_stations'] == 1
    assert send(request, 'call_station', connection_id, value=2)[0] == 400
    assert send(request, 'set_speed', connection_id, value=500)[0] == 400
    request(dict(action='arm', armed=True, connection_id=connection_id))
    assert send(request, 'call_station', connection_id, value=0)[0] == 400
    code, refused = send(request, 'call_station', connection_id, value=3)
    assert code == 400 and 'from 1 to 2' in refused['error']
    assert not command_writes(request()[1], 'call_station')
    status, state = send(request, 'call_station', connection_id, value=2)
    assert status == 200
    assert state['results']['call_station']['state'] == agv.SENT
    agv_clock.now += 1
    assert request()[1]['results']['call_station']['ack_elapsed_s'] == pytest.approx(1)
    state = send(request, 'query_status')[1]
    assert not state['status']['stopped']
    assert state['status']['current_station'] == 1 and state['status']['target_station'] == 2
    assert state['results']['call_station']['state'] == 'travelling'
    agv_clock.now += 4
    state = send(request, 'query_status')[1]
    assert state['status']['current_station'] == 2 and state['status']['stopped']
    assert state['results']['call_station']['state'] == 'arrived'
    assert state['results']['call_station']['elapsed_s'] == pytest.approx(5)
    assert {'stopped', 'current_station', 'target_station'} <= {event['field'] for event in state['events']}
    assert send(request, 'call_station', connection_id, value=1)[0] == 200


@pytest.mark.parametrize('stations', [0, 1])
def test_stop_counter_cannot_confirm_a_station_call(session, agv_clock, stations):
    _, request = session
    _, connected = request({'action': 'connect', 'mode': 'simulator', 'stations': stations})
    connection_id = connected['connection_id']
    assert send(request, 'query_status')[1]['status']['max_stations'] == 0
    request(dict(action='arm', armed=True, connection_id=connection_id))
    status, state = send(request, 'call_station', connection_id, value=1)
    assert status == 400 and 'station learning is unverified' in state['error']
    assert not command_writes(request()[1], 'call_station')


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


def test_status_events_record_both_edges_and_private_fault_description(session, monkeypatch):
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
    request.connection.fault_descriptions = {'61': 'Example guide fault'}
    connection_id = open_simulator(request)
    send(request, 'query_status', connection_id)
    original = fields.copy()
    fields.update(stopped=False, obstacle_stop=True, command_stop=True, paused=True,
                  current_station=2, target_station=3, fault_code=61)
    state = send(request, 'query_status', connection_id)[1]
    assert state['status']['fault_description'] == 'Example guide fault'
    assert 'fault_page' not in state['status']
    assert {(e['field'], e['after']) for e in state['events']} == set(fields.items())
    fields.update(original)
    state = send(request, 'query_status', connection_id)[1]
    assert {(e['field'], e['after']) for e in state['events'][-len(fields):]} == set(original.items())


@pytest.mark.parametrize(('number', 'message'), [
    (errno.EACCES, 'Log out and back in'),
    (errno.EAGAIN, 'Disconnect the AGV link in the other instance'),
    (errno.EBUSY, 'Disconnect the AGV link in the other instance'),
    (errno.ENOENT, 'Reseat the USB plug'),
])
def test_serial_open_errors_give_a_next_step(session, monkeypatch, number, message):
    def fail_open(**kwargs):
        assert kwargs['exclusive'] is True
        raise OSError(number, 'driver diagnostic')
    monkeypatch.setattr(agv, 'serial_ports', lambda: [{'device': '/dev/fake', 'description': 'Test'}])
    monkeypatch.setattr(agv, 'serial', SimpleNamespace(Serial=fail_open))
    _, request = session
    code, denied = request(dict(action='connect', mode='serial', port='/dev/fake'))
    assert code == 400 and message in denied['error']
    assert 'driver diagnostic' not in denied['error']
    state = request()[1]
    assert state['connected'] is False and state['armed'] is False
    assert state['log'][-1]['decode']['error'].endswith('driver diagnostic')


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


def test_stop_before_first_status_confirms_the_reported_stop(session, agv_clock):
    _, request = session
    connection_id = open_simulator(request, agv_mode=0)
    send(request, 'stop', connection_id)
    agv_clock.now += 1.01
    state = request()[1]
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


def test_repeating_pause_keeps_the_original_pending_delivery(session):
    _, request = session
    connection_id = open_simulator(request)
    _, first = send(request, 'pause', connection_id)
    _, second = send(request, 'pause', connection_id)
    assert not second['replaced']
    assert second['results']['pause']['tx_id'] == first['results']['pause']['tx_id']


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
    with link.recipe_link(stop_generation=link.stop_generation):
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
        with link.recipe_link(stop_generation=link.stop_generation):
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
    ('stop', 1.5, 5, 1), ('pause', 1.5, 5, 1),
])
def test_unanswered_halt_retries_to_the_try_limit(
        session, agv_clock, command, interval, limit, reply_wait):
    _, request = session
    connection_id = open_simulator(request, agv_mode=0)
    send(request, 'query_status')
    request(dict(action='silence', silent=True, connection_id=connection_id))
    request(dict(action='arm', armed=True, connection_id=connection_id))

    status, state = send(request, command, connection_id)
    assert status == 200
    def writes(state):
        names = ('stop', 'pause') if command == 'stop' else ('pause',)
        return [entry for entry in state['log'] if entry['direction'] == 'TX'
                and entry['decode'].get('command') in names]

    for attempt in range(2, limit + 1):
        agv_clock.now += interval - .01
        assert len(writes(request()[1])) == attempt - 1
        agv_clock.now += .01
        assert len(writes(request()[1])) == attempt

    agv_clock.now += reply_wait
    state = request()[1]
    attempts = writes(state)
    if command == 'pause':
        assert len({entry['hex'] for entry in attempts}) == 1
    assert [entry['decode']['try'] for entry in attempts] == list(range(1, limit + 1))
    assert state['results'][command]['state'] == 'no reply'
    quality = state['link_quality']
    assert (quality['sent'], quality['acknowledged'], quality['resent'], quality['no_reply']) == (
        limit + 1, 0, limit - 1, 1)
    agv_clock.now += 20
    assert len(writes(request()[1])) == limit


def test_retry_acknowledgement_ends_resends_without_confirming_status(session, agv_clock):
    _, request = session
    connection_id = open_simulator(request)
    request(dict(action='silence', silent=True, connection_id=connection_id))
    send(request, 'pause', connection_id)
    request(dict(action='silence', silent=False, connection_id=connection_id))
    agv_clock.now += 1.5
    assert len(command_writes(request()[1], 'pause')) == 2

    agv_clock.now += 1
    state = request()[1]
    assert state['results']['pause']['ack_elapsed_s'] == pytest.approx(1)
    assert state['status'] is None
    assert state['link_quality']['acknowledged'] == 1
    agv_clock.now += 10
    assert len(command_writes(request()[1], 'pause')) == 2


@pytest.mark.parametrize(('action', 'fields'), [
    ('send', {'command': 'pause'}),
    ('send', {'command': 'pause'}),
    ('arm', {'armed': False}),
    ('disconnect', {}),
])
def test_urgent_controls_disarm_and_disconnect_cancel_pending_motion_resends(
        session, agv_clock, action, fields):
    _, request = session
    connection_id = open_simulator(request)
    send(request, 'query_status')
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
    connection_id = open_simulator(request, agv_mode=0)
    send(request, 'query_status', connection_id)
    send(request, 'stop', connection_id)
    transport = request.connection.transport
    agv_clock.now += .5
    transport._report()
    assert request()[1]['results']['stop']['confirmed_stopped'] is False
    agv_clock.now += .5
    state = request()[1]
    assert state['results']['stop']['state'] == 'confirmed by status'
    assert 'ack_elapsed_s' not in state['results']['stop']
    agv_clock.now += 2
    assert len(command_writes(request()[1], 'stop')) == 1


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
        with link.recipe_link(stop_generation=link.stop_generation):
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
    connection_id = open_simulator(request, agv_mode=0)
    request.connection.transport.next_report = float('inf')
    send(request, 'query_status')
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
    assert len(command_writes(state, 'query_status')) >= 2
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
        ages = []
        initial_at = lost_at = None
        for step in range(800):
            agv_clock.now = 10 + step / 100
            if link.status_at is not None:
                ages.append(agv_clock.now - link.status_at)
            with link.lock:
                link._receive()
            if initial_at is None and link.status_at is not None:
                initial_at = link.status_at
                assert link.status['mode'] == 2
            link._poll_status(link.transport)
            if lost_at is None and link.transport.queries == 2:
                lost_at = agv_clock.now
        assert initial_at is not None and lost_at is not None
        assert initial_at <= lost_at < link.status_at
        assert max(ages) < 3, f'maximum status age was {max(ages):.3f} s'
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
        agv_clock.now += agv.TIMED_REPORT_LISTEN_S
        link._poll_status(link.transport)
        started = agv_clock.now
        link.submit(dict(action='send', command='stop'))
        assert .90 <= agv_clock.now - started <= 1.05
        with pytest.raises(ValueError, match='awaiting delivery'):
            link.submit(dict(action='send', command='read_speed', connection_id=link.id))
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
    airtime = .24

    class HalfDuplex(agv.Simulator):
        def write(self, raw):
            now = agv_clock.now
            tx_end = now + airtime
            ack = self.acknowledgements[0] if self.acknowledgements else None
            if ack is not None and now < ack and tx_end > ack - airtime:
                self.acknowledgements.popleft()
                return len(raw)
            if now < self.next_report and tx_end > self.next_report - .4:
                return len(raw)
            written = super().write(raw)
            self.acknowledgements[-1] = now + .52
            return written

    monkeypatch.setattr(agv, 'Simulator', HalfDuplex)
    _, request = session
    request(dict(action='connect', mode='simulator', agv_mode=0))
    for _ in range(4):
        agv_clock.now += 1
        request()
    agv_clock.now += .04
    send(request, 'stop')
    agv_clock.now += .1
    request()
    agv_clock.now += .43
    state = request()[1]
    assert state['results']['stop'].get('ack_elapsed_s') == pytest.approx(.53)
    assert len(command_writes(state, 'stop')) == 1


@pytest.mark.parametrize(('delay', 'late'), [(.2, True), (.31, False)])
def test_final_urgent_ack_grace_window(session, monkeypatch, agv_clock, delay, late):
    class FinalAck(agv.Simulator):
        writes = 0

        def write(self, raw):
            self.writes += 1
            if self.writes == 5:
                self.acknowledgements.append(agv_clock.now + 1 + delay)
            return len(raw)

    monkeypatch.setattr(agv, 'Simulator', FinalAck)
    _, request = session
    monkeypatch.setattr(request.connection, '_start_worker', lambda **kwargs: None)
    open_simulator(request)
    send(request, 'pause')
    for _ in range(4):
        agv_clock.now += 1.5
        request()
    agv_clock.now += 1
    assert request()[1]['results']['pause']['state'] == 'no reply'
    agv_clock.now += delay + .001
    state = request()[1]
    assert len(command_writes(state, 'pause')) == 5
    assert state['results']['pause']['state'] == ('acknowledged late' if late else 'no reply')
    assert state['link_quality']['acknowledged'] == int(late)
    assert state['link_quality']['no_reply'] == int(not late)
    assert state['log'][-1]['decode'].get('acknowledged_command') == ('pause' if late else None)


@pytest.mark.parametrize(('command', 'initially_stopped', 'press_phase'), [
    ('pause', False, .02), ('pause', False, .995), ('pause', True, .995),
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

        @property
        def in_waiting(self):
            if self.mode == 2 and agv_clock.now >= self.next_report:
                self._report()
                self.next_report = agv_clock.now + 1
            return super().in_waiting

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
    request(dict(action='connect', mode='simulator', agv_mode=2 if command == 'pause' else 0))
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
    connection_id = open_simulator(request, agv_mode=0)
    send(request, 'query_status')
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
    connection_id = open_simulator(request, agv_mode=0)
    send(request, 'query_status')
    request(dict(action='silence', silent=True, connection_id=connection_id))
    send(request, 'stop')
    agv_clock.now += 1
    status, state = send(request, 'query_status', connection_id, background=True)
    assert status == 200 and len(command_writes(state, 'query_status')) == 1
    agv_clock.now += .5
    assert len(command_writes(request()[1], 'stop')) == 2


@pytest.mark.parametrize('action', ['disarm'])
def test_pending_urgent_retry_survives_disarm_on_the_same_link(session, agv_clock, action):
    _, request = session
    _, state = request(dict(action='connect', mode='simulator', agv_mode=0))
    connection_id = state['connection_id']
    for _ in range(4):
        agv_clock.now += 1
        request()
    agv_clock.now += .03
    request.connection.transport.loss_fraction = 1
    send(request, 'stop', connection_id)
    if action == 'disarm':
        request(dict(action='arm', armed=False, connection_id=connection_id))
    else:
        request(dict(action='disconnect', connection_id=connection_id))
        if action == 'link_change':
            assert open_simulator(request) != connection_id
    agv_clock.now = 15
    request()
    agv_clock.now += .12
    state = request()[1]
    expected = {'disarm': 2, 'disconnect': 1, 'link_change': 0}
    assert len(command_writes(state, 'stop')) == expected[action]


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
        with link.recipe_link(stop_generation=link.stop_generation):
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
            agv_clock.now += agv.TIMED_REPORT_LISTEN_S
            link.transport.delay = rng.uniform(.83, .88)
            link._poll_status(link.transport)
            agv_clock.now += rng.uniform(0, agv.PAGE_POLL_S)
            pressed = agv_clock.now
            state = link.submit(dict(action='send', command='stop'))
            halt = state['urgent_command']
            for _ in range(400):
                agv_clock.now += .01
                with link.lock:
                    link._receive()
                link._service_resend()
                link._poll_status(link.transport)
                result = link.results[halt]
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
        with link.recipe_link(stop_generation=link.stop_generation) if command == 'recipe_stop' else nullcontext():
            if command == 'recipe_stop':
                link.recipe_feedback()
            else:
                agv_clock.now += agv.TIMED_REPORT_LISTEN_S
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
        ages = []
        initial_at = lost_at = None
        with link.recipe_link(stop_generation=link.stop_generation):
            for step in range(80):
                agv_clock.now = 10 + step / 10
                if link.status_at is not None:
                    ages.append(agv_clock.now - link.status_at)
                link.recipe_feedback()
                if initial_at is None and link.status_at is not None:
                    initial_at = link.status_at
                    assert link.status['mode'] == 2
                if lost_at is None and len([code for code, _ in link.transport.writes if code == 0x26]) == 2:
                    link.transport.pending_status = None
                    lost_at = agv_clock.now
        assert initial_at is not None and lost_at is not None
        assert initial_at <= lost_at < link.status_at
        print(f'Peak recipe status age with one missed poll: {max(ages):.3f} s')
        assert max(ages) < 2.8
    finally:
        link.close()


@pytest.mark.parametrize('poll', ['worker', 'browser', 'motion', 'recipe'])
def test_automatic_poll_waits_for_stop_ack_after_status_confirmation(agv_clock, half_duplex, poll):
    link = agv.Connection(SimpleNamespace(lock=RLock(), closing=False, active=None,
                                          storage_error=None, execution_uncertain=False))
    try:
        link.submit(dict(action='connect'))
        agv_clock.now += agv.TIMED_REPORT_LISTEN_S
        link.transport.ack_delay = 1
        with link.recipe_link(stop_generation=link.stop_generation) if poll == 'recipe' else nullcontext():
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
            assert link.capability()['results']['pause']['state'] == 'confirmed by status'
            agv_clock.now += .75
            if poll == 'recipe':
                with pytest.raises(ValueError, match='cancelled the recipe'):
                    link.recipe_feedback()
            elif poll == 'motion':
                link.motion_feedback(poll=True)
            elif poll == 'worker':
                link._poll_status(link.transport)
            else:
                link.submit(dict(action='send', command='query_status', background=True))
            assert [code for code, _ in link.transport.writes] == [0x26, 0x23]
            agv_clock.now += .21
            assert 'ack_elapsed_s' in link.capability()['results']['pause']
    finally:
        link.close()


def test_software_stop_pauses_active_station_trip(session, agv_clock):
    _, request = session
    cid = open_simulator(request)
    state = send(request, 'query_status')[1]
    request(dict(action='arm', armed=True, connection_id=cid))
    assert send(request, 'call_station', cid, value=2, stop_generation=state.get('stop_generation'))[0] == 200
    assert send(request, 'query_status')[1]['status']['actual_speed_m_h'] > 0
    code, state = send(request, 'stop')
    assert code == 200 and state['urgent_command'] == 'pause'
    assert command_writes(state, 'pause') and not command_writes(state, 'stop')
    agv_clock.now += 1.5
    request()
    agv_clock.now += 1.5
    request()
    state = request()[1]
    assert state['status']['paused'] is True and state['status']['actual_speed_m_h'] == 0


@pytest.mark.parametrize(('mode', 'motion', 'value', 'wrong', 'correct'), [
    (0, 'forward', None, 'pause', 'stop'),
    (2, 'call_station', 2, 'stop', 'pause'),
])
def test_simulator_ignores_wrong_halt_for_mode(agv_clock, mode, motion, value, wrong, correct):
    simulator = agv.Simulator(agv.DEFAULT_PREFIX)
    simulator.mode = mode
    for command, argument in [(motion, value), (wrong, None)]:
        simulator.write(agv.command_frame(command, argument, agv.DEFAULT_PREFIX).encode())
    assert simulator.stopped is False
    simulator.write(agv.command_frame(correct, None, agv.DEFAULT_PREFIX).encode())
    assert simulator.stopped is True


def test_missing_or_pre_stop_approval_cannot_send_movement(session, agv_clock):
    _, request = session
    cid = open_simulator(request)
    approved = send(request, 'query_status')[1].get('stop_generation')
    request(dict(action='arm', armed=True, connection_id=cid))
    code, _ = request(dict(action='send', command='forward', connection_id=cid))
    assert code == 400
    stopped = send(request, 'stop')[1]
    assert stopped['stop_generation'] != approved
    agv_clock.now += 1.5
    code, _ = send(request, 'forward', cid, stop_generation=approved)
    assert code == 400
    assert not command_writes(request()[1], 'forward')


def test_disarm_and_foreground_reads_cannot_supplant_pending_stop(session, agv_clock):
    _, request = session
    cid = open_simulator(request, agv_mode=0)
    assert send(request, 'query_status')[1]['status']['mode'] == 0
    request(dict(action='silence', silent=True, connection_id=cid))
    send(request, 'stop')
    request(dict(action='arm', armed=False, connection_id=cid))
    agv_clock.now += 1.51
    state = request()[1]
    assert len(command_writes(state, 'stop')) == 2
    assert send(request, 'query_status')[0] == 400
    assert len(command_writes(request()[1], 'query_status')) == 1


@pytest.mark.parametrize(('command', 'fields'), [('forward', {}), ('set_speed', {'value': 500})])
def test_manual_motion_refuses_missing_status_and_stops_retrying_when_stale(session, agv_clock, command, fields):
    _, request = session
    cid = open_simulator(request)
    request(dict(action='arm', armed=True, connection_id=cid))
    assert send(request, command, cid, **fields)[0] == 400
    assert send(request, 'query_status')[1]['status'] is not None
    request(dict(action='silence', silent=True, connection_id=cid))
    assert send(request, command, cid, **fields)[0] == 200
    agv_clock.now += agv.STALE_AFTER
    assert len(command_writes(request()[1], command)) == 1


@pytest.mark.parametrize('rotate', ['rotate_clockwise', 'rotate_counterclockwise'])
def test_rotation_is_not_repeated_after_a_lost_ack(session, agv_clock, rotate):
    _, request = session
    cid = open_simulator(request)
    assert send(request, 'query_status')[1]['status'] is not None
    request(dict(action='arm', armed=True, connection_id=cid))
    request(dict(action='silence', silent=True, connection_id=cid))
    assert send(request, rotate, cid)[0] == 200
    agv_clock.now += 2
    state = request()[1]
    assert len(command_writes(state, rotate)) == 1
    assert state['results'][rotate]['state'] == 'no reply'


def test_expired_earlier_command_ack_cannot_confirm_stop(session, agv_clock, monkeypatch):
    class LateMotionAck(agv.Simulator):
        def write(self, raw):
            frame = agv.parse_frame(raw)
            if frame.command == 0x23:
                if frame.data[0] == 0x7D:
                    self.acknowledgements.append(agv_clock.now + 1.7)
                return len(raw)
            return super().write(raw)

    monkeypatch.setattr(agv, 'Simulator', LateMotionAck)
    _, request = session
    cid = open_simulator(request)
    assert send(request, 'query_status')[1]['status'] is not None
    request(dict(action='arm', armed=True, connection_id=cid))
    assert send(request, 'set_speed', cid, value=500)[0] == 200
    agv_clock.now += .3
    state = send(request, 'stop')[1]
    halt = state.get('urgent_command', 'stop')
    agv_clock.now += 1.41
    state = request()[1]
    assert 'acknowledged_at' not in state['results'][halt]
    agv_clock.now += .1
    assert len(command_writes(request()[1], halt)) == 2


def test_earlier_query_parked_reply_cannot_confirm_stop(agv_clock, half_duplex):
    link = agv.Connection(SimpleNamespace(lock=RLock(), closing=False, active=None,
                                          storage_error=None, execution_uncertain=False))
    try:
        link.submit(dict(action='connect'))
        with link.lock:
            link.transport.stopped = False
            link.transport._report()
            link._receive()
            link.transport.delay = 1.15
            link.transport.stopped = True
            link._send('query_status', None, background=True)
        started = agv_clock.now
        state = link.submit(dict(action='send', command='stop'))
        # Model the earlier query reply surviving the urgent write.
        link.transport.pending_status = started + 1.15
        halt = state.get('urgent_command', 'stop')
        agv_clock.now += .21
        state = link.capability()
        assert state['status']['stopped'] is True
        assert state['results'][halt]['state'] == agv.SENT
    finally:
        link.close()


def test_recipe_receive_failure_attempts_halt_before_close(agv_clock):
    link = agv.Connection(SimpleNamespace(lock=RLock(), closing=False, active=None,
                                          storage_error=None, execution_uncertain=False))
    link.submit(dict(action='connect'))
    transport = link.transport
    writes = []

    def failed_read(size):
        raise OSError('receive-side test failure')

    def observe_write(raw):
        writes.append(raw)
        return len(raw)

    transport.write = observe_write
    try:
        with link.recipe_link(stop_generation=link.stop_generation):
            transport.read = failed_read
            with pytest.raises(ValueError, match='Link lost'):
                link.recipe_feedback()
            assert writes == [agv.command_frame('pause', None, agv.DEFAULT_PREFIX).encode()]
            assert link.recipe_stop() is True
            assert len(writes) == 1
            state = link.capability()
            errors = [entry['decode'].get('error') for entry in state['log']]
            assert 'receive-side test failure' in errors
            assert command_writes(state, 'pause')
    finally:
        link.close()


@pytest.mark.parametrize('enable_at', [0, 6])
def test_background_polls_find_two_second_timed_reports(agv_clock, half_duplex, enable_at):
    link = agv.Connection(SimpleNamespace(lock=RLock(), closing=False, active=None,
                                          storage_error=None, execution_uncertain=False))
    try:
        link.submit(dict(action='connect'))
        reports = 0
        for step in range(1800):
            agv_clock.now = 10 + step / 100
            with link.lock:
                if step >= (enable_at + 2) * 100 and step % 200 == 0:
                    link.transport._report()
                    reports += 1
                link._receive()
            link._poll_status(link.transport)
        assert reports >= 5
        assert link.capability()['link_quality']['timed_report_interval_s'] == pytest.approx(2)
        queries = [at for cmd, at in link.transport.writes if cmd == 0x26]
        if enable_at:
            assert queries and min(queries) < 10 + enable_at
            assert all(at < 10 + enable_at + 5 for at in queries)
        else:
            assert not queries
    finally:
        link.close()


def test_unknown_mode_software_stop_tries_both_wire_commands(session, agv_clock):
    _, request = session
    open_simulator(request)
    transport = request.connection.transport
    transport.stopped = False
    first = send(request, 'stop')[1]
    assert first['urgent_command'] == 'stop'
    assert command_writes(first, 'stop') and not transport.stopped
    agv_clock.now += 1.51
    second = request()[1]
    assert second['results']['stop']['wire_command'] == 'pause'
    assert command_writes(second, 'pause') and transport.stopped
    agv_clock.now += 1.51
    final = request()[1]
    assert final['status']['actual_speed_m_h'] == 0
    assert final['results']['stop']['confirmed_stopped'] is True


@pytest.mark.parametrize(('mode', 'stations'), [(0, 3), (2, 1)])
def test_station_call_retry_rechecks_mode_and_learned_range(session, agv_clock, mode, stations):
    _, request = session
    cid = open_simulator(request)
    send(request, 'query_status')
    request(dict(action='arm', armed=True, connection_id=cid))
    simulator = request.connection.transport
    simulator.loss_fraction = 1
    assert send(request, 'call_station', cid, value=2)[0] == 200
    simulator.loss_fraction = 0
    simulator.mode, simulator.stations = mode, stations
    simulator._report()
    request()
    agv_clock.now += 2
    state = request()[1]
    assert len(command_writes(state, 'call_station')) == 1
    assert state['results']['call_station']['state'] == 'not checked'


def test_recipe_handoff_cannot_displace_pending_halt(agv_clock):
    link = agv.Connection(SimpleNamespace(lock=RLock(), closing=False, active=None,
                                          storage_error=None, execution_uncertain=False))
    try:
        link.submit(dict(action='connect'))
        link.transport.loss_fraction = 1
        link.submit(dict(action='send', command='pause'))
        with link.recipe_link(stop_generation=link.stop_generation):
            assert link.recipe_call(2, lambda *args: None) is None
            agv_clock.now += 1.5
            state = link.capability()
            assert len(command_writes(state, 'pause')) == 2
            assert not command_writes(state, 'call_station')
    finally:
        link.close()


def test_pause_flag_does_not_confirm_stop_at_nonzero_speed(session, agv_clock):
    _, request = session
    open_simulator(request)
    transport = request.connection.transport
    transport.stopped = False
    transport._report()
    request()
    send(request, 'pause')
    transport.stopped = False  # Simulate the brief period before braking completes.
    transport._report()
    state = request()[1]
    assert state['status']['actual_speed_m_h'] > 0
    assert state['results']['pause']['confirmed_stopped'] is False
    transport.stopped = True
    transport._report()
    state = request()[1]
    assert state['status']['actual_speed_m_h'] == 0
    assert state['results']['pause']['confirmed_stopped'] is True


def test_stale_mode_stop_reselects_after_fresh_mode_change(session, agv_clock):
    _, request = session
    open_simulator(request, agv_mode=0)
    send(request, 'query_status')
    transport = request.connection.transport
    transport.mode, transport.stopped = 2, False
    agv_clock.now += agv.STALE_AFTER + .1
    send(request, 'stop')
    transport._report()
    request()
    agv_clock.now += 1.51
    state = request()[1]
    assert command_writes(state, 'pause')
    assert transport.stopped


@pytest.mark.parametrize(('field', 'value'), [
    ('station_arrival', False), ('actual_speed_m_h', 100),
    ('obstacle_stop', True), ('fault_code', 1),
])
def test_station_arrival_and_cache_require_safe_arrival_evidence(session, agv_clock, field, value):
    _, request = session
    cid = open_simulator(request)
    send(request, 'query_status')
    request(dict(action='arm', armed=True, connection_id=cid))
    assert send(request, 'call_station', cid, value=2)[0] == 200
    link = request.connection
    with link.lock:
        previous = link.last_confirmed_station
        status = dict(link.status, current_station=2, target_station=2, stopped=True,
                      actual_speed_m_h=0, station_arrival=True, obstacle_stop=False, fault_code=0)
        status[field] = value
        link._status_received(status, b'')
        assert link.results['call_station']['state'] != 'arrived'
        assert link.last_confirmed_station == previous


def test_disconnect_refuses_pending_halt_and_keeps_retry_on_link(session, agv_clock):
    _, request = session
    cid = open_simulator(request, agv_mode=0)
    send(request, 'query_status')
    request(dict(action='silence', silent=True, connection_id=cid))
    send(request, 'stop')
    code, state = request(dict(action='disconnect', connection_id=cid))
    assert code == 400 and state['error']
    agv_clock.now += 1.51
    state = request()[1]
    assert state['connected'] and len(command_writes(state, 'stop')) == 2


def test_timed_report_does_not_clear_pre_stop_query_ambiguity(agv_clock, half_duplex):
    link = agv.Connection(SimpleNamespace(lock=RLock(), closing=False, active=None,
                                          storage_error=None, execution_uncertain=False))
    try:
        link.submit(dict(action='connect'))
        with link.lock:
            link.transport.stopped = False
            link.transport._report()
            link._receive()
            link.transport.delay = 1.15
            link.transport.stopped = True
            link._send('query_status', None, background=True)
            link.transport.stopped = False
            link.transport._report()
            link._receive()
        started = agv_clock.now
        state = link.submit(dict(action='send', command='stop'))
        halt = state.get('urgent_command', 'stop')
        link.transport.pending_status = started + 1.15
        link.transport.stopped = True
        agv_clock.now += 1.16
        state = link.capability()
        assert not state['results'][halt].get('confirmed_stopped', False)
    finally:
        link.close()


def test_blocked_pose_retry_does_not_block_halt_retry(session, monkeypatch):
    from anodizing_web import travel_pose
    from time import sleep
    owner, request = session
    cid = open_simulator(request, agv_mode=0)
    send(request, 'query_status')
    request(dict(action='arm', armed=True, connection_id=cid))
    request(dict(action='silence', silent=True, connection_id=cid))
    assert send(request, 'forward', cid)[0] == 200
    owner.robot_configured = True
    entered, release = Event(), Event()

    def blocked_pose(owner):
        entered.set()
        release.wait(4)
        return ''

    monkeypatch.setattr(travel_pose, 'motion_block', blocked_pose)
    try:
        assert entered.wait(3)
        assert send(request, 'stop')[0] == 200
        sleep(1.7)
        state = request.connection.commissioning()
        assert len(command_writes(state, 'stop')) >= 2
    finally:
        release.set()


def test_second_ack_for_retried_speed_cannot_acknowledge_halt(session, agv_clock):
    _, request = session
    cid = open_simulator(request, agv_mode=0)
    send(request, 'query_status')
    request(dict(action='arm', armed=True, connection_id=cid))
    request(dict(action='silence', silent=True, connection_id=cid))
    send(request, 'set_speed', cid, value=500)
    agv_clock.now += 2
    state = request()[1]
    assert len(command_writes(state, 'set_speed')) == 2
    link = request.connection
    with link.lock:
        link._acknowledge({})
    agv_clock.now += 1.51
    request()  # Expire the acknowledged record before the halt.
    state = send(request, 'stop')[1]
    halt = state['urgent_command']
    with link.lock:
        link._acknowledge({})
    agv_clock.now += 1.51
    state = request()[1]
    assert 'ack_elapsed_s' not in state['results'][halt]
    assert len(command_writes(state, 'stop')) + len(command_writes(state, 'pause')) >= 2


def test_pre_halt_parked_reply_with_old_pause_flag_needs_corroboration(agv_clock, half_duplex):
    link = agv.Connection(SimpleNamespace(lock=RLock(), closing=False, active=None,
                                          storage_error=None, execution_uncertain=False))
    try:
        link.submit(dict(action='connect'))
        with link.lock:
            link.transport.stopped = True
            link.transport.paused = True
            link.transport._report()
            link._receive()
            link.transport.delay = 1.15
            link._send('query_status', None, background=True)
            link.transport.stopped = False
            link.transport.paused = False
            link.transport._report()
            link._receive()
        started = agv_clock.now
        state = link.submit(dict(action='send', command='stop'))
        halt = state['urgent_command']
        assert halt == 'pause'
        link.transport.pending_status = started + 1.15
        link.transport.stopped = True
        link.transport.paused = True
        agv_clock.now += 1.16
        state = link.capability()
        assert state['status']['paused'] is True
        assert not state['results'][halt].get('confirmed_stopped', False)
        with link.lock:
            link.transport._report()
            link._receive()
        assert link.capability()['results'][halt].get('confirmed_stopped') is True
    finally:
        link.close()
