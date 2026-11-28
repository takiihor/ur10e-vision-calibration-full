"""Offline BLE settings checks with a protocol-speaking fake device."""
import json
import secrets

import pytest

from anodizing_web.agv_ble import Bluetooth


TOKEN = secrets.token_urlsafe(24)
CLEAR = bytes.fromhex('40 BF 17 07 30 15 D3 EA')
NEW_SETTINGS = [
    ('5E', 0, 1, '51'),
    ('73', 0, 1, '6B'),
    ('77', 0, 1, '6B'),
    ('86', 0, 1, '86'),
    ('8A', 0, 23, '86'),
    ('8D', 0, 2, '86'),
    ('9E', 0, 1, '86'),
]


class FakeBluetooth:
    def __init__(self):
        self.words = {i: 0 for start, count in ((5, 20), (0x51, 26), (0x6B, 26), (0x86, 26), (0xA7, 26))
                      for i in range(start, start + count)}
        self.words[0x18] = 0x2802
        self.words[0x6D] = 1
        self.words[0x9A] = 7
        self.records = {0x21: [], 0x23: []}
        self.clear_takes_effect = True
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
        operation = frame[4]
        if operation == 1:
            size = frame[6]
            payload = b''.join(self.words[i].to_bytes(2, 'little') for i in range(index, index + size // 2))
            self.reply(operation, index, payload)
        elif operation in self.records:
            self.reply(operation, index, b''.join(self.records[operation][index:index + frame[6]]))
        elif operation == 0x25:
            assert wire == CLEAR
            self.before_write()
            if self.clear_takes_effect:
                self.words[0x55] = self.words[0x56] = 0
                self.records = {0x21: [], 0x23: []}
        elif operation == 3:
            self.before_write()
            if not self.mismatch:
                self.words[index] = int.from_bytes(frame[6:8], 'little')

    def reply(self, operation, index, payload):
        reply = bytes((0x55, 0xAA, len(payload) + 2, 0x21, operation, index)) + payload
        reply += (~sum(reply[2:]) & 0xffff).to_bytes(2, 'little')
        wire = bytes(byte ^ 0x15 for byte in reply)
        for offset in range(0, len(wire), 20):
            self.on_bytes(wire[offset:offset + 20])

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
        assert len(snapshot['fields']) == 124
        assert snapshot['fields']['A7'].lower() == '00 00'

    fake.before_write = verify_snapshot
    assert len(link.status()['fields']) == 124
    with pytest.raises((ValueError, RuntimeError)):
        request(link, 'write', field='A7', old=0, value=1, confirmed=False)
    assert fake.words[0xA7] == 0
    request(link, 'write', field='A7', old=0, value=1, confirmed=True)
    assert fake.words[0xA7] == 1
    log = [json.loads(line) for line in (directory / 'bytes.jsonl').read_text().splitlines()]
    assert {entry['direction'] for entry in log} == {'tx', 'rx'}
    assert all(bytes.fromhex(entry['hex']) for entry in log)
    assert [bytes.fromhex(entry['hex']) for entry in log if entry['direction'] == 'tx'] == fake.frames
    assert link.status()['undo'] == {'field': 'A7', 'old': 1, 'value': 0}
    fake.before_write = lambda: None
    request(link, 'undo', field='A7', old=1, value=0, confirmed=True)
    assert fake.words[0xA7] == 0


def test_read_retries_silent_a7_block_once(bluetooth):
    link, fake, _, _ = bluetooth
    fake.words[0xA7] = 42
    send = fake.send
    silent = True

    async def send_with_silent_a7(wire):
        nonlocal silent
        frame = bytes(byte ^ 0x15 for byte in wire)
        if frame[4:7] == bytes.fromhex('01 A7 34') and silent:
            silent = False
            fake.frames.append(bytes(wire))
            return
        await send(wire)

    fake.send = send_with_silent_a7
    request(link, 'connect')
    result = request(link, 'read')
    frames = [bytes(byte ^ 0x15 for byte in frame) for frame in fake.frames]
    assert [frame[4:7] for frame in frames] == [
        bytes.fromhex(block) for block in ('01 05 28', '01 51 34', '01 6B 34', '01 86 34', '01 A7 34', '01 A7 34')
    ]
    assert next(row for row in result['fields'] if row['field'] == 'A7')['value'] == 42


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


@pytest.mark.parametrize('field,old,value,block', NEW_SETTINGS)
def test_new_setting_is_written_read_back_and_restored(bluetooth, field, old, value, block):
    link, fake, _, _ = bluetooth
    request(link, 'connect')
    result = request(link, 'write', field=field, old=old, value=value, confirmed=True)
    index = int(field, 16)
    assert fake.words[index] == value
    assert next(row for row in result['fields'] if row['field'] == field)['value'] == value
    plain = [bytes(byte ^ 0x15 for byte in frame) for frame in fake.frames]
    assert plain[-2][4:8] == bytes((3, index)) + value.to_bytes(2, 'little')
    assert plain[-1][4:6] == bytes((1, int(block, 16)))
    result = request(link, 'undo', **result['undo'], confirmed=True)
    assert fake.words[index] == old
    assert result['undo'] is None


def test_every_writable_field_declares_range_readback_and_undo(bluetooth):
    link, _, _, _ = bluetooth
    rows = {row['field']: row for row in link.status()['fields']}
    known = {field: block for field, _, _, block in NEW_SETTINGS} | {'8C': '86', '90': '86', 'A7': 'A7'}
    assert {field for field, row in rows.items() if row['writable']} == set(known)
    for field, readback in known.items():
        row = rows[field]
        assert 0 <= row['minimum'] < row['maximum'] < 2**16
        assert row['readback'] == readback
        assert row['undoable'] is True
    for field in ('6C', '6D', '6E', '88', '8B', '91', '96'):
        assert rows[field]['read_only_reason']


@pytest.mark.parametrize('field,old,value', [('6E', 0, 10), ('6C', 0, 1), ('8A', 0, -1), ('8D', 0, 3)])
def test_rejects_read_only_or_out_of_range_settings(bluetooth, field, old, value):
    link, fake, _, _ = bluetooth
    request(link, 'connect')
    with pytest.raises(ValueError, match=f'Field {field}'):
        request(link, 'write', field=field, old=old, value=value, confirmed=True)
    assert not any((frame[4] ^ 0x15) == 3 for frame in fake.frames)


def test_existing_78_word_undo_snapshot_remains_usable(bluetooth):
    link, fake, _, directory = bluetooth
    request(link, 'connect')
    request(link, 'write', field='A7', old=0, value=1, confirmed=True)
    snapshot, = directory.glob('snapshot-*.json')
    saved = json.loads(snapshot.read_text())
    saved['fields'] = {key: value for key, value in saved['fields'].items() if int(key, 16) >= 0x6B}
    assert len(saved['fields']) == 78
    snapshot.write_text(json.dumps(saved))
    request(link, 'disconnect')
    session = 'b' * 32
    result = link.submit({'action': 'connect', 'session': session})
    link.submit({'action': 'undo', 'session': session, **result['undo'], 'confirmed': True})
    assert fake.words[0xA7] == 0


def test_reads_device_words_counts_and_chunked_station_rows(bluetooth):
    link, fake, _, _ = bluetooth
    fake.words[0x55], fake.words[0x56] = 5, 1
    row = bytes.fromhex('01 00 00 00 03 00 04 10 00 00 00 00 00 00 00 00')
    fake.records = {0x21: [row] * 5, 0x23: [row]}
    request(link, 'connect')
    result = request(link, 'read')
    assert {item['field']: item['value'] for item in result['fields'] if item['field'] in ('18', '55', '56')} == {
        '18': 0x2802, '55': 5, '56': 1,
    }
    assert result['route']['counts'] == [5, 1]
    assert result['route']['records'] == {'outbound': [row.hex(' ')] * 5, 'return': [row.hex(' ')]}
    requests = [bytes(byte ^ 0x15 for byte in frame) for frame in fake.frames]
    assert [tuple(item[4:7]) for item in requests if item[4] in (0x21, 0x23)] == [
        (0x21, 0, 4), (0x21, 4, 1), (0x23, 0, 1)
    ]


def test_route_clear_reviews_counts_then_clears_once_and_verifies(bluetooth):
    link, fake, _, directory = bluetooth
    fake.words[0x55], fake.words[0x56] = 2, 1
    request(link, 'connect')
    with pytest.raises(ValueError, match='(?i)review'):
        request(link, 'clear', token='unreviewed', confirmed=True)
    reviewed = request(link, 'review-clear')['clear_review']
    assert reviewed['before'] == [2, 1]
    assert CLEAR not in fake.frames
    result = request(link, 'clear', token=reviewed['token'], confirmed=True)
    assert result['clear_result'] == {'before': [2, 1], 'after': [0, 0]}
    assert result['route']['counts'] == [0, 0]
    with pytest.raises(ValueError, match='(?i)review'):
        request(link, 'clear', token=reviewed['token'], confirmed=True)
    assert fake.frames.count(CLEAR) == 1
    log = [json.loads(line) for line in (directory / 'bytes.jsonl').read_text().splitlines()]
    assert [bytes.fromhex(entry['hex']) for entry in log if entry['direction'] == 'tx'] == fake.frames


@pytest.mark.parametrize('change,match', [
    ('counts', 'Counts changed'),
    ('profile', 'profile'),
    ('executor', 'executor'),
])
def test_clear_refuses_changed_conditions(bluetooth, change, match):
    link, fake, state, _ = bluetooth
    fake.words[0x55] = 1
    request(link, 'connect')
    reviewed = request(link, 'review-clear')['clear_review']
    if change == 'counts':
        fake.words[0x55] = 3
    elif change == 'profile':
        fake.words[0x18] = 0x8000
    else:
        state['blocked'] = 'Disconnect and disarm the executor.'
    with pytest.raises(ValueError, match=match):
        request(link, 'clear', token=reviewed['token'], confirmed=True)
    assert CLEAR not in fake.frames


def test_clear_records_nonzero_readback_without_retrying(bluetooth):
    link, fake, _, _ = bluetooth
    fake.words[0x55] = 1
    fake.clear_takes_effect = False
    request(link, 'connect')
    reviewed = request(link, 'review-clear')['clear_review']
    with pytest.raises(ValueError, match='(?i)clear.*unconfirmed'):
        request(link, 'clear', token=reviewed['token'], confirmed=True)
    assert link.status()['clear_result'] == {'before': [1, 0], 'after': [1, 0]}
    with pytest.raises(ValueError, match='(?i)review'):
        request(link, 'clear', token=reviewed['token'], confirmed=True)
    assert fake.frames.count(CLEAR) == 1


def test_invalid_route_counts_cannot_be_reviewed_for_clear(bluetooth):
    link, fake, _, _ = bluetooth
    fake.words[0x55] = 0xFFFF
    request(link, 'connect')
    with pytest.raises(ValueError, match='Outbound 65535'):
        request(link, 'review-clear')
    assert CLEAR not in fake.frames
