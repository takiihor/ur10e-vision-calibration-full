"""Offline checks for the read-only direct RS485 commissioning link."""
import json
from threading import RLock
from types import SimpleNamespace

import pytest

from anodizing_web import agv
from agv_examples import EXAMPLE_PREFIX, EXAMPLE_REPLY_PREFIX, EXAMPLE_ROUTE, EXAMPLE_VEHICLE_ID, REPORT


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


@pytest.mark.parametrize('mode', ['wired', 'serial', 'simulator'])
def test_profile_locks_arming_commands_and_recipe_in_every_mode(wired_link, mode):
    link, opened = wired_link
    body = {'action': 'connect', 'mode': mode}
    if mode == 'serial':
        body['port'] = '/dev/example-wire'
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
