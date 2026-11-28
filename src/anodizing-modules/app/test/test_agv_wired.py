"""Offline checks for the read-only direct RS485 commissioning link."""
import json
from threading import RLock
from types import SimpleNamespace

import pytest

from anodizing_web import agv
from agv_examples import EXAMPLE_PREFIX, EXAMPLE_REPLY_PREFIX, EXAMPLE_ROUTE, EXAMPLE_STATUS, EXAMPLE_VEHICLE_ID, REPORT


@pytest.fixture
def wired_link(tmp_path, monkeypatch):
    (tmp_path / 'site-profile.json').write_text(json.dumps({'agv': {'wired_port': '/dev/example-wire'}}))
    owner = SimpleNamespace(root=tmp_path, lock=RLock(), closing=False, active=None,
                            storage_error=None, execution_uncertain=False)
    opened = []

    class FakeSerial:
        def __init__(self, **settings):
            self.settings, self.writes, self.buffer = settings, [], b''
            opened.append(self)

        @property
        def in_waiting(self):
            return len(self.buffer)

        def read(self, size):
            result, self.buffer = self.buffer[:size], self.buffer[size:]
            return result

        def write(self, raw):
            self.writes.append(raw)
            return len(raw)

        def close(self):
            pass

    monkeypatch.setattr(agv, 'serial', SimpleNamespace(Serial=FakeSerial))
    monkeypatch.setattr(agv, 'serial_ports', lambda: [{'device': '/dev/example-wire', 'description': 'fake'}])
    link = agv.Connection(owner)
    try:
        yield link, opened
    finally:
        link.close()


def test_profile_wired_port_is_published_before_connect(wired_link):
    link, _ = wired_link
    assert link.commissioning()['wired_port'] == '/dev/example-wire'


@pytest.mark.parametrize(('mode', 'prefix'), [('wired', b''), ('serial', EXAMPLE_PREFIX)])
def test_serial_status_query_uses_transport_framing(wired_link, mode, prefix):
    link, opened = wired_link
    body = {'action': 'connect', 'mode': mode, **EXAMPLE_ROUTE}
    if mode == 'serial':
        body['port'] = '/dev/example-wire'
    state = link.submit(body)
    assert state['settings']['port'] == '/dev/example-wire'
    assert opened[0].settings['baudrate'] == 9600
    with link.lock:
        link._send('query_status', None)
    assert opened[0].writes[-1] == agv.command_frame('query_status', None, prefix).encode()


@pytest.mark.parametrize('prefix', [b'', EXAMPLE_REPLY_PREFIX])
def test_wired_accepts_bare_and_broadcast_prefixed_status(wired_link, prefix):
    link, opened = wired_link
    link.submit({'action': 'connect', 'mode': 'wired', **EXAMPLE_ROUTE})
    opened[0].buffer = prefix + REPORT
    assert link.capability()['status']['vehicle_id'] == EXAMPLE_VEHICLE_ID


def test_dedicated_wired_link_without_radio_route_locks_to_first_complete_status(wired_link):
    link, opened = wired_link
    response = link.submit({'action': 'connect', 'mode': 'wired'})
    assert response['settings']['address'] == response['settings']['channel'] == ''
    device = opened[0]
    device.buffer = agv.Frame(0x25, bytes((EXAMPLE_VEHICLE_ID + 1,))).encode()
    first = link.capability()
    assert first['status'] is None
    assert 'vehicle ID' in first['log'][-1]['decode']['error']
    other_vehicle = bytes((EXAMPLE_VEHICLE_ID + 1,)) + EXAMPLE_STATUS[1:]
    device.buffer = agv.Frame(0x25, other_vehicle[:14]).encode()
    assert link.capability()['status'] is None
    device.buffer = agv.Frame(0x25, EXAMPLE_STATUS[:17]).encode()
    assert link.capability()['status']['vehicle_id'] == EXAMPLE_VEHICLE_ID
    device.buffer = agv.Frame(0x25, other_vehicle).encode()
    other = link.capability()
    assert other['status']['vehicle_id'] == EXAMPLE_VEHICLE_ID
    assert 'vehicle ID' in other['log'][-1]['decode']['error']


def test_raw_wired_exchange_encodes_and_checks_speed_readback(wired_link, monkeypatch):
    link, opened = wired_link
    link.submit({'action': 'connect', 'mode': 'wired'})
    serial = opened[0]
    speed = 1000
    data = bytearray(EXAMPLE_STATUS)
    data[2:4] = speed.to_bytes(2, 'little')
    data[4] = 1
    data[5] = 0
    data[8:10] = b'\x00\x00'
    data[12] = 0
    serial.buffer = agv.Frame(0x25, data).encode()
    link.capability()
    original_write = serial.write

    def reply(raw):
        nonlocal speed
        frame = agv.parse_frame(raw)
        assert frame.prefix == b''
        if frame.command == 0x21:
            serial.buffer += agv.Frame(0x21, bytes((0x7d,)) + speed.to_bytes(2, 'little')).encode()
        elif frame.command == 0x23:
            assert frame.data[0] == 0x7d
            speed = int.from_bytes(frame.data[1:], 'little')
        return original_write(raw)

    monkeypatch.setattr(serial, 'write', reply)
    assert link.raw_exchange('read_speed', None, lambda: None)['decoded']['read_speed_m_h'] == 1000
    with pytest.raises(ValueError, match='changed before write'):
        link.raw_exchange('set_speed', 500, lambda: None, expected=(EXAMPLE_VEHICLE_ID, 999))
    link.raw_exchange('set_speed', 500, lambda: None, expected=(EXAMPLE_VEHICLE_ID, 1000))
    result = link.raw_exchange('read_speed', None, lambda: None)
    assert result['decoded'] == {'read_speed_m_h': 500}
    assert bytes.fromhex(result['reply_hex']) == agv.Frame(0x21, bytes.fromhex('7D F4 01')).encode()
    assert [agv.parse_frame(frame).command for frame in serial.writes].count(0x23) == 1


@pytest.mark.parametrize('mode', ['wired', 'serial', 'simulator'])
def test_profile_locks_arming_commands_and_recipe_in_every_mode(wired_link, mode):
    link, opened = wired_link
    body = {'action': 'connect', 'mode': mode}
    if mode == 'serial':
        body.update(port='/dev/example-wire', **EXAMPLE_ROUTE)
    state = link.submit(body)
    cid = state['connection_id']
    with pytest.raises(ValueError):
        link.submit({'action': 'arm', 'armed': True, 'connection_id': cid})
    assert not link.commissioning()['armed']
    for name in ('forward', 'stop', 'pause', 'set_speed'):
        with pytest.raises(ValueError):
            link.submit({'action': 'send', 'command': name, 'value': 500,
                         'connection_id': cid, 'stop_generation': link.stop_generation})
    with pytest.raises(ValueError):
        with link.recipe_link(connection_id=cid, stop_generation=link.stop_generation):
            pass
    if opened:
        assert opened[0].writes == []


def test_recipe_retry_rechecks_expiry_after_waiting_for_frame_lock(wired_link, monkeypatch):
    from threading import Event, Thread

    link, opened = wired_link
    link.submit({'action': 'connect', 'mode': 'wired', **EXAMPLE_ROUTE})
    link.wired_station_jobs = True
    link.recipe_active = True
    checked, expired, attempting = Event(), Event(), Event()
    errors = []

    def check(*_):
        checked.set()
        if expired.is_set():
            raise ValueError('confirmation expired')

    raw = agv.command_frame('call_station', 2, link._tx_prefix()).encode()
    retry = agv.Resend('call_station', raw, 0, recipe_check=check)
    link.resend = retry
    link.results['call_station'] = {'value': 2, 'tx_id': 'fixture-retry'}
    link.pending['call_station'] = agv.Confirmation(None, {}, 0)
    monkeypatch.setattr(link, '_due', lambda _: True)
    monkeypatch.setattr(link, '_send_slot', lambda **_: True)
    monkeypatch.setattr(link, '_check_motion_status', lambda *_: None)
    before = len(opened[0].writes)
    original_write = link._write
    def signal_write(*args, **kwargs):
        attempting.set()
        return original_write(*args, **kwargs)
    monkeypatch.setattr(link, '_write', signal_write)

    def run():
        try:
            link._retry(retry)
        except ValueError as exc:
            errors.append(str(exc))

    with link.frame_lock:
        worker = Thread(target=run)
        worker.start()
        assert attempting.wait(1)
        expired.set()
    worker.join(1)
    assert not worker.is_alive()
    assert errors == ['confirmation expired']
    assert len(opened[0].writes) == before
