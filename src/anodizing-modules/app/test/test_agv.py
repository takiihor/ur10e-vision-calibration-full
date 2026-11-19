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
