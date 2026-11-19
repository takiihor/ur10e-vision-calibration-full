"""Protocol vectors from XBOT RS485 protocol pp. 1, 4–6, and SSCOM presets."""
from pathlib import Path

import pytest

from anodizing_web import agv


PREFIX = bytes.fromhex('00 01 04')
REPORT = bytes.fromhex(
    'A5 5A 1F 25 01 19 DC 05 01 00 01 00 00 00 02 02 00 01 02 00 15 19 00 00 00 00 DF 00 00 2C 80'
)


@pytest.mark.parametrize(('name', 'value', 'hex_frame'), [
    ('call_station', 1, '00 01 04 A5 5A 08 23 9D 01 00 C8'),
    ('set_speed', 1257, '00 01 04 A5 5A 08 23 7D E9 04 94'),
    ('query_status', None, '00 01 04 A5 5A 06 26 FF 2A'),
    ('resume', None, '00 01 04 A5 5A 08 23 5B 02 00 87'),
])
def test_vendor_commands_round_trip(name, value, hex_frame):
    wire = bytes.fromhex(hex_frame)
    assert agv.command_frame(name, value, PREFIX).encode() == wire
    assert agv.parse_frame(wire).encode() == wire


@pytest.fixture
def captured_frames():
    capture = Path(__file__).with_name('fixtures') / 'agv600-protocol-vectors.txt'
    return tuple(bytes.fromhex(line) for line in capture.read_text().splitlines())


@pytest.fixture
def captured_reads(captured_frames):
    status, speed, unidentified = captured_frames
    # Split the synthetic stream inside the radio prefix and frame header.
    return status + speed[:1], speed[1:] + unidentified[:1], unidentified[1:]


def test_splitter_preserves_captured_reply_prefixes_across_reads(captured_frames, captured_reads):
    splitter = agv.Splitter(PREFIX)
    for chunk, frame in zip(captured_reads, captured_frames, strict=True):
        assert splitter.feed(chunk) == [(frame, None)]


def test_splitter_recovers_the_reply_prefix_after_bad_length_and_checksum(captured_frames):
    status, speed, unidentified = captured_frames
    corrupt_length = status[:5] + b'\xff' + status[6:]
    # The incorrect length consumes the beginning of the next frame.
    corrupt_checksum = speed[:5] + b'\x0d' + speed[6:]
    frames = agv.Splitter(PREFIX).feed(corrupt_length + status + corrupt_checksum + speed + unidentified)
    assert len(frames) == 5
    assert frames[0][0] == corrupt_length and 'length' in frames[0][1]
    assert frames[2][1] and 'checksum' in frames[2][1]
    assert frames[1::2] == [(status, None), (speed, None)]
    assert frames[4] == (unidentified, None)


def test_splitter_uses_the_configured_reply_channel(captured_frames):
    _, speed, _ = captured_frames
    reply = b'\x00\xff\x07' + speed[3:]
    assert agv.Splitter(bytes.fromhex('00 02 07')).feed(reply) == [(reply, None)]


def test_captured_status_omits_unavailable_attachment_fields(captured_frames):
    wire, _, _ = captured_frames
    status = agv.decode(agv.parse_frame(wire))
    assert status['length'] == 29
    assert status['vehicle_id'] == 1 and status['battery_percent'] == 75
    assert status['set_speed_m_h'] == 1500
    assert status['obstacle_input'] == 0xCC
    assert not {'attachment_active', 'roller_speed'} & status.keys()
    assert status['checksum'] == '43'


def test_captured_short_speed_reply_decodes_as_1500_m_per_hour(captured_frames):
    _, wire, _ = captured_frames
    assert agv.decode(agv.parse_frame(wire)) == {'read_speed_m_h': 1500}


def test_vendor_status_decodes_every_offset():
    assert agv.decode(agv.parse_frame(REPORT)) == {
        'header': 'A5 5A', 'length': 31, 'command': '25', 'vehicle_id': 1, 'battery_percent': 25,
        'set_speed_m_h': 1500, 'stopped': True, 'obstacle_stop': False, 'station_arrival': True,
        'command_stop': False, 'actual_speed_m_h': 0, 'current_station': 2, 'target_station': 2,
        'fault_code': 0, 'max_stations': 1, 'mode': 2, 'customer_sensors': 0,
        'position_units': 6421, 'paused': False, 'leading_end': 0, 'obstacle_input': 223,
        'roller_center_blocked': False, 'roller_left_blocked': False, 'roller_right_blocked': False,
        'lift_state': 0, 'attachment_active': False, 'roller_speed': 44, 'checksum': '80',
    }


def test_short_report_and_partial_multibyte_field_omit_unavailable_values():
    frame = agv.Frame(0x25, REPORT[4:21])
    status = agv.decode(agv.parse_frame(frame.encode()))
    assert status['customer_sensors'] == 0
    assert not {'position_units', 'paused', 'leading_end', 'lift_state'} & status.keys()
    assert status['checksum'] == f'{frame.encode()[-1]:02X}'


def test_speed_reply_decodes_as_1257_m_per_hour():
    frame = agv.parse_frame(bytes.fromhex('00 01 04 A5 5A 08 21 7D E9 04 92'))
    assert agv.decode(frame) == {'read_speed_m_h': 1257}


@pytest.mark.parametrize(('index', 'value', 'error'), [(0, 0, 'header'), (2, 0, 'length'), (-1, 0, 'checksum')])
def test_bad_frame_reports_why(index, value, error):
    frame = bytearray(REPORT)
    frame[index] = value
    with pytest.raises(ValueError, match=error):
        agv.parse_frame(bytes(frame))


@pytest.mark.parametrize(('name', 'value'), [
    ('set_speed', 499), ('set_speed', 4001), ('set_speed', 1257.0),
    ('call_station', 0), ('call_station', 256), ('call_station', True),
])
def test_rejects_out_of_range_and_non_integer_arguments(name, value):
    with pytest.raises(ValueError):
        agv.command_frame(name, value, PREFIX)


def test_splitter_handles_garbage_fragmentation_and_both_reply_forms():
    splitter = agv.Splitter(PREFIX)
    assert splitter.feed(b'garbage' + PREFIX[:1]) == []
    assert splitter.feed(PREFIX[1:] + REPORT[:13]) == []
    frames = splitter.feed(REPORT[13:] + b'xyz' + REPORT)
    assert frames == [(PREFIX + REPORT, None), (REPORT, None)]


def test_splitter_recovers_after_bad_length_and_checksum():
    splitter = agv.Splitter(PREFIX)
    corrupt_length = REPORT[:2] + b'\xff' + REPORT[3:]
    corrupt_checksum = REPORT[:-1] + b'\x00'
    frames = splitter.feed(corrupt_length + REPORT + corrupt_checksum + REPORT)
    assert [(raw, error is None) for raw, error in frames] == [
        (corrupt_length, False), (REPORT, True), (corrupt_checksum, False), (REPORT, True)
    ]


def test_splitter_preserves_configured_prefix_containing_header_bytes():
    prefix = bytes.fromhex('A5 5A 04')
    splitter = agv.Splitter(prefix)
    assert splitter.feed(prefix + REPORT[:2]) == []
    assert splitter.feed(REPORT[2:]) == [(prefix + REPORT, None)]


def test_all_nonempty_sscom_presets():
    presets = Path(__file__).with_name('fixtures') / 'agv-sscom51-presets.txt'
    lines = presets.read_text().splitlines()
    assert len(lines) == 30
    for line in lines:
        preset, data = line.split(',')
        wire = bytes.fromhex(data)
        if preset.split('=')[0] in ('N2', 'N16'):
            with pytest.raises(ValueError, match='length'):
                agv.parse_frame(wire)
        else:
            assert agv.parse_frame(wire).encode() == wire, preset


def test_unknown_stop_flag_stays_raw_instead_of_indicating_stopped():
    data = bytearray(REPORT[4:-1])
    data[4] = 2
    data[24] = 2
    status = agv.decode(agv.Frame(0x25, bytes(data)))
    assert status['stopped'] == 2
    assert status['attachment_active'] is True


@pytest.mark.parametrize(('actual', 'command_stop', 'paused'), [
    (-750, False, False), (0, True, False), (0, False, True),
])
def test_synthetic_status_encodes_reverse_and_stop_flags(actual, command_stop, paused):
    data = bytearray(REPORT[4:-1])
    data[4] = actual == 0
    data[7] = command_stop
    data[8:10] = actual.to_bytes(2, 'little', signed=True)
    data[20] = paused
    wire = agv.Frame(0x25, data, PREFIX).encode()
    status = agv.decode(agv.parse_frame(wire))
    assert status['actual_speed_m_h'] == actual
    assert status['command_stop'] is command_stop
    assert status['paused'] is paused


def test_synthetic_ack_is_not_status_and_records_elapsed_time(monkeypatch):
    now = 0
    monkeypatch.setattr(agv, 'monotonic', lambda: now)
    connection = agv.Connection(None)
    transport = agv.Simulator(PREFIX)
    connection.transport = transport
    connection.splitter = agv.Splitter(PREFIX)
    connection._send('set_speed', 800)
    now = 1
    connection._receive()
    ack = connection.log[-1]['decode']
    assert ack['acknowledgement'] is True and ack['vehicle_id'] == 1
    assert 'stopped' not in ack and connection.status is None
    assert ack['acknowledged_command'] == 'set_speed'
    assert ack['ack_elapsed_s'] == pytest.approx(1)
