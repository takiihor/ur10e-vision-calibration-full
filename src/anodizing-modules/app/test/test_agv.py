"""Protocol checks with generated frames and invented inputs."""
import pytest

from anodizing_web import agv
from agv_examples import EXAMPLE_PREFIX as PREFIX, EXAMPLE_SPEED, EXAMPLE_VEHICLE_ID, REPORT, synthetic_replies


@pytest.mark.parametrize(('name', 'value', 'command', 'data'), [
    ('call_station', 17, 0x23, b'\x9d\x11\x00'),
    ('set_speed', EXAMPLE_SPEED, 0x23, b'\x7d\x39\x05'),
    ('query_status', None, 0x26, b'\xff'),
    ('resume', None, 0x23, b'\x5b\x02\x00'),
])
def test_commands_round_trip(name, value, command, data):
    wire = agv.command_frame(name, value, PREFIX).encode()
    assert agv.parse_frame(wire) == agv.Frame(command, data, PREFIX)


@pytest.fixture
def example_frames():
    return synthetic_replies(PREFIX)


@pytest.fixture
def fragmented_reads(example_frames):
    status, speed, unidentified = example_frames
    # Split the synthetic stream inside the radio prefix and frame header.
    return status + speed[:1], speed[1:] + unidentified[:1], unidentified[1:]


def test_splitter_preserves_synthetic_reply_prefixes_across_reads(example_frames, fragmented_reads):
    splitter = agv.Splitter(PREFIX)
    for chunk, frame in zip(fragmented_reads, example_frames, strict=True):
        assert splitter.feed(chunk) == [(frame, None)]


def test_splitter_recovers_the_reply_prefix_after_bad_length_and_checksum(example_frames):
    status, speed, unidentified = example_frames
    corrupt_length = status[:5] + b'\xff' + status[6:]
    # The incorrect length consumes the beginning of the next frame.
    corrupt_checksum = speed[:5] + b'\x0d' + speed[6:]
    frames = agv.Splitter(PREFIX).feed(corrupt_length + status + corrupt_checksum + speed + unidentified)
    assert len(frames) == 5
    assert frames[0][0] == corrupt_length and 'length' in frames[0][1]
    assert frames[2][1] and 'checksum' in frames[2][1]
    assert frames[1::2] == [(status, None), (speed, None)]
    assert frames[4] == (unidentified, None)


def test_splitter_uses_the_configured_reply_channel(example_frames):
    _, speed, _ = example_frames
    reply = b'\x00\xff\x07' + speed[3:]
    assert agv.Splitter(bytes.fromhex('00 02 07')).feed(reply) == [(reply, None)]


def test_synthetic_status_omits_unavailable_attachment_fields(example_frames):
    wire, _, _ = example_frames
    status = agv.decode(agv.parse_frame(wire))
    assert status['length'] == 29
    assert status['vehicle_id'] == EXAMPLE_VEHICLE_ID and status['battery_percent'] == 83
    assert status['set_speed_m_h'] == EXAMPLE_SPEED
    assert status['laser_blocked'] is True
    assert not {'attachment_active', 'roller_speed'} & status.keys()
    assert status['checksum'] == f'{wire[-1]:02X}'


def test_synthetic_short_speed_reply_decodes_at_example_speed(example_frames):
    _, wire, _ = example_frames
    assert agv.decode(agv.parse_frame(wire)) == {'read_speed_m_h': EXAMPLE_SPEED}


def test_synthetic_status_decodes_every_offset():
    assert agv.decode(agv.parse_frame(REPORT)) == {
        'header': 'A5 5A', 'length': 31, 'command': '25', 'vehicle_id': EXAMPLE_VEHICLE_ID, 'battery_percent': 83,
        'set_speed_m_h': EXAMPLE_SPEED, 'stopped': True, 'obstacle_stop': False, 'station_arrival': True,
        'command_stop': False, 'actual_speed_m_h': 0, 'current_station': 7, 'target_station': 9,
        'fault_code': 0, 'max_stations': 12, 'mode': 2, 'customer_sensors': 5,
        'position_units': 2057, 'paused': False, 'leading_end': 0, 'obstacle_input': 1,
        'laser_blocked': True, 'lift_state': 0, 'attachment_active': True, 'roller_speed': 24,
        'checksum': f'{REPORT[-1]:02X}',
    }


def test_short_report_and_partial_multibyte_field_omit_unavailable_values():
    frame = agv.Frame(0x25, REPORT[4:21])
    status = agv.decode(agv.parse_frame(frame.encode()))
    assert status['customer_sensors'] == 5
    assert not {'position_units', 'paused', 'leading_end', 'lift_state'} & status.keys()
    assert status['checksum'] == f'{frame.encode()[-1]:02X}'


def test_speed_reply_decodes_as_1257_m_per_hour():
    frame = agv.parse_frame(agv.Frame(0x21, b'\x7d' + (1257).to_bytes(2, 'little'), PREFIX).encode())
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
    prefix = bytes.fromhex('A5 5A 0D')
    splitter = agv.Splitter(prefix)
    assert splitter.feed(prefix + REPORT[:2]) == []
    assert splitter.feed(REPORT[2:]) == [(prefix + REPORT, None)]


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
    connection._send('pause', None)
    now = 1
    connection._receive()
    ack = connection.log[-1]['decode']
    assert ack['acknowledgement'] is True and ack['vehicle_id'] == EXAMPLE_VEHICLE_ID
    assert 'stopped' not in ack and connection.status is None
    assert ack['acknowledged_command'] == 'pause'
    assert ack['ack_elapsed_s'] == pytest.approx(1)



def test_seeded_simulator_drops_host_frames_before_processing_them():
    transport = agv.Simulator(PREFIX, loss_fraction=.5, seed=7)
    raw = agv.command_frame('read_speed', None, PREFIX).encode()
    received = []
    for _ in range(8):
        assert transport.write(raw) == len(raw)
        received.append(bool(transport.read(transport.in_waiting)))
    assert received == [False, False, True, False, True, False, False, True]


def test_status_timestamp_tracks_first_serial_bytes_across_a_delay(monkeypatch, tmp_path):
    from types import SimpleNamespace

    clock = 10.0
    chunk = REPORT[:8]
    monkeypatch.setattr(agv, 'monotonic', lambda: clock)

    def read(_):
        return chunk

    link = agv.Connection(SimpleNamespace(root=tmp_path))
    link.transport = SimpleNamespace(in_waiting=len(REPORT), read=read)
    link.splitter = agv.Splitter(b'')
    link.connected_at = clock

    link._receive()
    assert link.status is None
    clock = 11.0  # A halt may be sent while waiting for the rest of this frame.
    chunk = REPORT[8:] + REPORT
    consumed = []
    consume = link._status_received

    def delayed_consume(*args):
        nonlocal clock
        clock = 12.0  # Decoding and recording are not serial observations.
        consume(*args)
        consumed.append(link.status_at)

    monkeypatch.setattr(link, '_status_received', delayed_consume)
    link._receive()
    assert consumed == [10.0, 11.0]


@pytest.mark.parametrize(('levels', 'expected'), [
    ([25, 25, 26, 26, 27, 27, 28, 28, 29, 29, 30], 'charging'),
    ([30, 30, 29, 29, 28, 28, 27, 27, 26, 26, 25], 'in_use'),
    ([25] * 11, 'steady'),
    ([25, 26] * 5 + [26], 'steady'),
    ([25] * 10 + [35], 'steady'),
])
def test_battery_trend_uses_sustained_fresh_feedback(tmp_path, monkeypatch, levels, expected):
    from types import SimpleNamespace

    link = agv.Connection(SimpleNamespace(root=tmp_path, closing=False, storage_error=None, execution_uncertain=False, active=None))
    link.transport = SimpleNamespace()
    link.connected_at = 0
    monkeypatch.setattr(agv, 'monotonic', lambda: now)
    for now in range(0, 601, 2):
        link._status_received({'battery_percent': levels[now // 60]}, b'', now)
        if now == 60:
            assert link.commissioning()['battery_trend'] is None
    assert link.commissioning()['battery_trend'] == expected


@pytest.mark.parametrize('interruption', ['gap', 'link closed', 'invalid reading'])
def test_battery_trend_requires_new_history_after_feedback_loss(tmp_path, monkeypatch, interruption):
    from types import SimpleNamespace

    link = agv.Connection(SimpleNamespace(root=tmp_path, closing=False, storage_error=None, execution_uncertain=False, active=None))
    link.transport = SimpleNamespace(close=lambda: None)
    link.connected_at = 0
    monkeypatch.setattr(agv, 'monotonic', lambda: now)
    for now in range(0, 241, 2):
        link._status_received({'battery_percent': 30 - now // 60}, b'', now)
    assert link.commissioning()['battery_trend'] == 'in_use'

    if interruption == 'gap':
        now += agv.RECIPE_STALE_S
    elif interruption == 'link closed':
        link.close()
    else:
        now += 2
        link._status_received({'battery_percent': 255}, b'', now)
    assert link.commissioning()['battery_trend'] is None

    now += 2
    link.transport = SimpleNamespace()
    link._status_received({'battery_percent': 25}, b'', now)
    assert link.commissioning()['battery_trend'] is None


def test_battery_trend_ignores_changes_older_than_ten_minutes(tmp_path, monkeypatch):
    from types import SimpleNamespace

    link = agv.Connection(SimpleNamespace(root=tmp_path, closing=False, storage_error=None, execution_uncertain=False, active=None))
    link.transport = SimpleNamespace()
    link.connected_at = 0
    monkeypatch.setattr(agv, 'monotonic', lambda: now)
    for now in range(0, 801, 2):
        link._status_received({'battery_percent': 30 if now < 180 else 25}, b'', now)
    assert link.commissioning()['battery_trend'] == 'steady'
