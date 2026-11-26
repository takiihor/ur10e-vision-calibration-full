"""Offline BLE settings checks with a protocol-speaking fake device."""
import json
import secrets

import pytest

from anodizing_web.agv_ble import Bluetooth


TOKEN = secrets.token_urlsafe(24)


class FakeBluetooth:
    def __init__(self):
        self.words = {i: 0 for start in (0x6B, 0x86, 0xA7) for i in range(start, start + 26)}
        self.frames = []
        self.closed = False
        self.mismatch = False
        self.before_write = lambda: None

    async def open(self, identity, on_bytes):
        self.on_bytes = on_bytes

    async def send(self, wire):
        self.frames.append(bytes(wire))
        frame = bytes(byte ^ 0x15 for byte in wire)
        index = frame[5]
        if frame[4] == 1:
            size = frame[6]
            payload = b''.join(self.words[i].to_bytes(2, 'little') for i in range(index, index + size // 2))
            reply = bytes((0x55, 0xAA, size + 2, 0x21, 1, index)) + payload
            reply += (~sum(reply[2:]) & 0xffff).to_bytes(2, 'little')
            self.on_bytes(bytes(byte ^ 0x15 for byte in reply))
        elif frame[4] == 3:
            self.before_write()
            if not self.mismatch:
                self.words[index] = int.from_bytes(frame[6:8], 'little')

    async def close(self):
        self.closed = True


@pytest.fixture
def bluetooth(tmp_path):
    fake = FakeBluetooth()
    state = {'blocked': None}
    directory = tmp_path / 'bluetooth'
    link = Bluetooth({'name': 'example'}, directory, lambda: state['blocked'], transport_factory=lambda: fake)
    yield link, fake, state, directory
    link.close()


def request(link, action, **values):
    return link.submit({'action': action, 'session': TOKEN, **values})


def test_confirmed_write_requires_full_readback_and_undo(bluetooth):
    link, fake, _, directory = bluetooth
    request(link, 'connect')
    request(link, 'read')

    def verify_snapshot():
        snapshots = list(directory.glob('snapshot-*.json'))
        assert len(snapshots) == 1
        snapshot = json.loads(snapshots[0].read_text())
        assert len(snapshot['fields']) == 78
        assert snapshot['fields']['A7'].lower() == '00 00'

    fake.before_write = verify_snapshot
    assert len(link.status()['fields']) == 78
    with pytest.raises((ValueError, RuntimeError)):
        request(link, 'write', field='A7', old=0, value=1, confirmed=False)
    assert fake.words[0xA7] == 0
    request(link, 'write', field='A7', old=0, value=1, confirmed=True)
    assert fake.words[0xA7] == 1
    log = [json.loads(line) for line in (directory / 'bytes.jsonl').read_text().splitlines()]
    assert {entry['direction'] for entry in log} == {'tx', 'rx'}
    assert all(bytes.fromhex(entry['hex']) for entry in log)
    assert {bytes.fromhex(entry['hex']) for entry in log if entry['direction'] == 'tx'} >= set(fake.frames)
    assert link.status()['undo'] == {'field': 'A7', 'old': 1, 'value': 0}
    fake.before_write = lambda: None
    request(link, 'undo', field='A7', old=1, value=0, confirmed=True)
    assert fake.words[0xA7] == 0


def test_mismatched_readback_is_reported(bluetooth):
    link, fake, _, _ = bluetooth
    request(link, 'connect')
    request(link, 'read')
    fake.mismatch = True
    with pytest.raises((ValueError, RuntimeError), match='(?i)read.back|confirm'):
        request(link, 'write', field='A7', old=0, value=1, confirmed=True)
    assert fake.words[0xA7] == 0


def test_close_disconnects_device(bluetooth):
    link, fake, _, _ = bluetooth
    request(link, 'connect')
    request(link, 'disconnect')
    assert fake.closed
    assert not link.status()['connected']


@pytest.mark.parametrize('reason', ['Executor is armed', 'Executor is connected'])
def test_executor_control_blocks_writes(bluetooth, reason):
    link, fake, state, _ = bluetooth
    request(link, 'connect')
    request(link, 'read')
    state['blocked'] = reason
    with pytest.raises((ValueError, RuntimeError), match='(?i)executor'):
        request(link, 'write', field='A7', old=0, value=1, confirmed=True)
    assert fake.words[0xA7] == 0


def test_undo_survives_disconnect_and_new_session(bluetooth):
    link, fake, state, directory = bluetooth
    request(link, 'connect')
    request(link, 'read')
    request(link, 'write', field='A7', old=0, value=1, confirmed=True)
    request(link, 'disconnect')
    link.close()
    link = Bluetooth({'name': 'example'}, directory, lambda: state['blocked'], transport_factory=lambda: fake)
    other = secrets.token_urlsafe(24)
    link.submit({'action': 'connect', 'session': other})
    link.submit({'action': 'read', 'session': other})
    assert link.status()['undo'] == {'field': 'A7', 'old': 1, 'value': 0}
    link.submit({'action': 'undo', 'session': other, 'field': 'A7', 'old': 1, 'value': 0, 'confirmed': True})
    assert fake.words[0xA7] == 0
    link.close()


def test_executor_arm_is_blocked_until_bluetooth_disconnects(bluetooth):
    from threading import RLock
    from anodizing_web.executor_link import ExecutorLink
    link, _, _, _ = bluetooth
    executor = object.__new__(ExecutorLink)
    executor.bluetooth = link
    executor.stop_latch = {}
    executor.journal_fault = None
    executor.manual = None
    executor.manual_lock = RLock()
    executor.storage_check = None
    executor._verify_manual_history = lambda: None
    calls = []
    executor.client = lambda operation, body: calls.append((operation, body)) or {'armed': True}
    request(link, 'connect')
    with pytest.raises(ValueError, match='(?i)bluetooth'):
        executor.request('arm', {'armed': True})
    assert calls == []
    request(link, 'disconnect')
    assert executor.request('arm', {'armed': True})['armed'] is True
    assert calls == [('arm', {'armed': True})]
