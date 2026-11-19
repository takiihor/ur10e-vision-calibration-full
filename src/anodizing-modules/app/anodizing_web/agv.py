"""XBOT AGV600 frames, simulator and a serialized, operator-owned test link."""

from collections import deque
from contextlib import nullcontext
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
import errno
import re
from threading import Lock
from time import monotonic, sleep

try:
    import serial
    from serial.tools import list_ports
except ImportError:
    serial = list_ports = None


HEADER = b'\xa5\x5a'
DEFAULT_PREFIX = b'\x00\x01\x04'
REPLY_TIMEOUT = 1.5
WRITE_TIMEOUT = .5
STALE_AFTER = 5
UNIDENTIFIED_INTERVAL = 11.22
SENT = 'sent'
LINK_LOST = 'Link lost. The AGV may still be moving. Use its e-stop if needed.'
PORT_RETRY = 'Reseat the USB plug, reload this page, then select Connect.'
# XBOT RS485 protocol pp. 1, 4–5.
COMMANDS = {
    'read_speed': ('Read speed', '21 7D 02'),
    'set_speed': ('Set speed', '23 7D 00 00'),
    'forward': ('Forward', '23 87 01 00'),
    'reverse': ('Reverse', '23 87 02 00'),
    'stop': ('Stop', '23 87 04 00'),
    'call_station': ('Call station', '23 9D 00 00'),
    'pause': ('Pause', '23 5B 01 00'),
    'resume': ('Resume', '23 5B 02 00'),
    'rotate_clockwise': ('Rotate 180° clockwise', '23 68 01 00'),
    'rotate_counterclockwise': ('Rotate 180° counterclockwise', '23 68 02 00'),
    'attachment_raise': ('Roller forward / raise (only if fitted)', '23 69 01 00'),
    'attachment_lower': ('Roller reverse / lower (only if fitted)', '23 69 02 00'),
    'attachment_stop': ('Roller stop (only if fitted)', '23 69 03 00'),
    'query_status': ('Query status', '26 FF'),
}
ARGUMENTS = {
    'set_speed': {'min': 500, 'max': 4000, 'default': 500, 'label': 'Speed (m/h)'},  # XBOT manual p. 2.
    'call_station': {'min': 1, 'max': 255, 'default': 1, 'label': 'Station number'},
}
URGENT = {'stop', 'pause'}
READS = {'query_status', 'read_speed'}


def hex_bytes(raw):
    return raw.hex(' ').upper()


@dataclass(frozen=True)
class Frame:
    command: int
    data: bytes
    prefix: bytes = b''

    def encode(self):
        inner = HEADER + bytes((5 + len(self.data), self.command)) + self.data
        return self.prefix + inner + bytes((sum(inner) & 0xFF,))


def parse_frame(raw, *, offset=None):
    if offset is None:
        offsets = [start for start in (0, 3) if raw[start:start + 2] == HEADER]
        if not offsets:
            raise ValueError('Invalid frame header (expected A5 5A).')
        if len(offsets) == 2 and raw[2] != len(raw):
            offsets.reverse()
        offset = offsets[0]
    inner = raw[offset:]
    if inner[:2] != HEADER:
        raise ValueError('Invalid frame header (expected A5 5A).')
    if len(inner) < 5 or inner[2] != len(inner):
        raise ValueError('Invalid frame length (LEN does not match the bytes received).')
    if inner[-1] != sum(inner[:-1]) & 0xFF:
        raise ValueError('Invalid frame checksum (SUM does not match).')
    return Frame(inner[3], inner[4:-1], raw[:offset])


def command_frame(name, value, prefix):
    if not isinstance(name, str) or name not in COMMANDS:
        raise ValueError('Select a known AGV command.')
    payload = bytes.fromhex(COMMANDS[name][1])
    if name in ARGUMENTS:
        argument = ARGUMENTS[name]
        if type(value) is not int or not argument['min'] <= value <= argument['max']:
            raise ValueError(f"{argument['label']} must be an integer in {argument['min']}–{argument['max']}.")
        payload = payload[:2] + value.to_bytes(2, 'little')
    elif value is not None:
        raise ValueError('This AGV command takes no value.')
    return Frame(payload[0], payload[1:], prefix)


def reply_prefix(request_prefix):
    return b'\x00\xff' + request_prefix[2:]


class Splitter:
    """Keep at most one partial frame and recognize request and reply radio prefixes."""

    def __init__(self, prefix):
        self.prefix = prefix
        self.prefixed_headers = (prefix + HEADER, reply_prefix(prefix) + HEADER)
        self.buffer = b''

    def feed(self, chunk):
        self.buffer += chunk
        frames = []
        while self.buffer:
            start = self.buffer.find(HEADER)
            if start < 0:
                self.buffer = self.buffer[-4:]
                break
            prefixed = min((index for header in self.prefixed_headers
                            if (index := self.buffer.find(header)) >= 0), default=-1)
            if 0 <= prefixed <= start:
                self.buffer = self.buffer[prefixed:]
                offset = 3
            else:
                # A configured prefix may itself contain A5 5A and be split across reads.
                self.buffer = self.buffer[start:]
                if (self.prefix + HEADER).startswith(self.buffer):
                    break
                offset = 0
            if len(self.buffer) < offset + 4:
                break
            length, command = self.buffer[offset + 2:offset + 4]
            # The published status layout ends at byte 30 (XBOT protocol pp. 2, 5).
            if length < 5 or (command == 0x25 and length > 31):
                next_header = self.buffer.find(HEADER, offset + 2)
                end = next_header if next_header >= 0 else len(self.buffer)
                if next_header >= 3 and self.buffer.startswith(self.prefixed_headers, next_header - 3):
                    end -= 3
                frames.append((self.buffer[:end], 'Invalid frame length (LEN is out of range).'))
                self.buffer = self.buffer[end:]
                continue
            end = offset + length
            if len(self.buffer) < end:
                break
            raw = self.buffer[:end]
            try:
                parse_frame(raw, offset=offset)
                error = None
            except ValueError as exc:
                error = str(exc)
                next_header = raw.find(HEADER, offset + 2)
                if next_header >= 0:
                    end = next_header
                    if next_header >= 3 and raw.startswith(self.prefixed_headers, next_header - 3):
                        end -= 3
            frames.append((raw, error))
            self.buffer = self.buffer[end:]
        return frames


# Offsets start at the inner header (XBOT RS485 protocol pp. 2, 5–6).
STATUS_FIELDS = {
    4: ('vehicle_id', 1), 5: ('battery_percent', 1), 6: ('set_speed_m_h', 2),
    8: ('stopped', 1), 9: ('obstacle_stop', 1), 10: ('station_arrival', 1),
    11: ('command_stop', 1), 12: ('actual_speed_m_h', 2), 14: ('current_station', 1),
    15: ('target_station', 1), 16: ('fault_code', 1), 17: ('max_stations', 1),
    18: ('mode', 1), 19: ('customer_sensors', 1), 20: ('position_units', 4),
    24: ('paused', 1), 25: ('leading_end', 1), 26: ('obstacle_input', 1),
    27: ('lift_state', 1), 28: ('attachment_active', 1), 29: ('roller_speed', 1),
}
FLAGS = {'stopped', 'obstacle_stop', 'station_arrival', 'command_stop', 'paused'}


# XBOT user manual V2.0, printed pp. 16–17. The status field holds one byte.
FAULTS = {
    2: 'Communication failure', 9: 'CPU initialization error', 10: 'Bluetooth board communication fault',
    11: 'Left motor current sensor fault', 12: 'Left motor current sensor fault',
    13: 'Left motor current sensor fault', 14: 'Right motor current sensor fault',
    15: 'Right motor current sensor fault', 16: 'Right motor current sensor fault',
    17: 'Motor-to-controller communication error', 18: 'Controller-to-motor communication error',
    19: 'No data from the front magnetic guide', 21: 'No data from the rear magnetic guide',
    25: 'Controller received no Bluetooth panel data', 26: 'Drive voltage fault',
    29: 'CPU initialization error', 34: 'Gyroscope data fault', 35: 'Gyroscope initialization fault',
    36: 'Left motor Hall sensor fault', 37: 'Too many slowdown markers', 39: 'Right motor Hall sensor fault',
    40: 'Upgrade platform error', 61: 'Off track', 62: 'Too many stations', 63: 'Route learning error',
    64: 'Motor stalled', 65: 'Vehicle lifted off the ground', 66: 'Excessive swaying or rotation',
    67: 'Invalid gyroscope rotation rate', 68: 'Excessive rotation speed', 69: 'Excessive motor speed difference',
    70: 'Motors turning in opposite directions', 71: 'Motor Hall sensor fault', 72: 'Gyroscope data unavailable',
    73: 'Motor count error', 74: 'Motor mismatch', 75: 'Missing RFID card in sequence',
    77: 'Target station missing', 78: 'RFID card unreadable', 79: 'Servo drive Hall count error',
    80: 'Battery management system error',
    203: 'Left motor pre-zone error', 206: 'Left motor phase current out of range',
    209: 'Left motor overheating', 211: 'Left motor drive MOSFET overheating',
    216: 'Left motor braking resistor configuration error', 217: 'Left motor system error',
    219: 'Left motor electrical angle calculation error', 221: 'Left motor speed calculation error',
    241: 'Left motor overspeed',
}
EVENT_FIELDS = ('stopped', 'current_station', 'target_station', 'fault_code',
                'obstacle_stop', 'command_stop', 'paused')


def timestamp():
    return datetime.now(timezone.utc).isoformat(timespec='milliseconds')


def port_failure(exc):
    message = str(exc).lower()
    if exc.errno in (errno.EACCES, errno.EPERM) or 'permission denied' in message:
        return 'Serial permission denied. Log out and back in on the IPC, then select Connect.'
    return f'Serial port is busy or missing. {PORT_RETRY}'


def decode(frame):
    if frame.command == 0x21:
        # The two-byte reply was observed on AGV600 2026-11-19.
        if len(frame.data) == 2 or (len(frame.data) == 3 and frame.data[0] == 0x7D):
            return {'read_speed_m_h': int.from_bytes(frame.data[-2:], 'little')}
    if frame.command == 0x01 and not frame.data:
        return {'command': '01', 'data': '', 'description': 'unidentified frame 01 (not in the XBOT protocol)'}
    if frame.command != 0x25:
        return {'command': f'{frame.command:02X}', 'data': hex_bytes(frame.data)}
    # Short ACK observed on AGV600 2026-11-19, not in the XBOT protocol.
    if len(frame.data) == 1:
        return {'acknowledgement': True, 'vehicle_id': frame.data[0]}
    inner = frame.encode()[len(frame.prefix):]
    values = {'header': 'A5 5A', 'length': len(inner), 'command': '25'}
    for offset, (key, size) in STATUS_FIELDS.items():
        if offset + size <= len(inner) - 1:
            value = int.from_bytes(inner[offset:offset + size], 'little', signed=key == 'actual_speed_m_h')
            if (key in FLAGS and value in (0, 1)) or key == 'attachment_active':
                value = bool(value)
            values[key] = value
    eye = values.get('obstacle_input')
    if eye in (0, 1):
        values['laser_blocked'] = bool(eye)
    elif eye is not None and eye & 0xEC == 0xCC:
        for beam, bit in (('center', 4), ('left', 1), ('right', 0)):
            values[f'roller_{beam}_blocked'] = not bool(eye & (1 << bit))
    values['checksum'] = f'{inner[-1]:02X}'
    return values


class Simulator:
    def __init__(self, prefix):
        self.prefix = prefix
        self.reply_prefix = reply_prefix(prefix)
        self.silent = False
        self.buffer = b''
        self.next_unidentified = monotonic() + UNIDENTIFIED_INTERVAL
        self.speed = 1500
        self.stopped = True
        self.station = self.target = 1
        self.stations = 3
        self.arrival = False
        self.arrives_at = None
        self.acknowledgements = deque()
        self.paused = False
        self.command_stop = False
        self.leading_end = 0

    @property
    def in_waiting(self):
        now = monotonic()
        if self.arrives_at is not None and now >= self.arrives_at:
            self.station, self.stopped, self.arrival = self.target, True, True
            self.arrives_at = None
            self.leading_end = 0
        while self.acknowledgements and now >= self.acknowledgements[0]:
            self.acknowledgements.popleft()
            if not self.silent:
                self.buffer += Frame(0x25, self.prefix[1:2]).encode()
        if not self.silent and now >= self.next_unidentified:
            self.buffer += Frame(0x01, b'').encode()
            self.next_unidentified = now + UNIDENTIFIED_INTERVAL
        return len(self.buffer)

    def read(self, size):
        data, self.buffer = self.buffer[:size], self.buffer[size:]
        return data

    def write(self, raw):
        frame = parse_frame(raw, offset=3)
        if frame.command == 0x23:
            operation, low, high = frame.data
            if not self.silent:
                self.acknowledgements.append(monotonic() + 1)
            if operation == 0x7D:
                self.speed = low + 256 * high
            elif operation == 0x87:
                self.stopped = self.command_stop = low == 4
                self.paused = self.arrival = False
                self.arrives_at = None
                if low in (1, 2):
                    self.leading_end = {1: 1, 2: 255}[low]
            elif operation == 0x9D:
                self.leading_end = 255 if low < self.station else 1
                self.target = low
                self.stopped = self.paused = self.command_stop = self.arrival = False
                self.arrives_at = monotonic() + 3 if 1 <= low <= self.stations else None
            elif operation == 0x5B:
                resume_station = self.paused and self.station != self.target and 1 <= self.target <= self.stations
                self.paused = self.stopped = low == 1
                self.arrives_at = monotonic() + 3 if low == 2 and resume_station else None
                self.command_stop = False
        elif not self.silent and frame.command == 0x26:
            speed = self.speed.to_bytes(2, 'little')
            actual = 0 if self.stopped else -self.speed if self.leading_end == 255 else self.speed
            data = (bytes((self.prefix[1], 86)) + speed
                    + bytes((self.stopped, 0, self.arrival, self.command_stop))
                    + actual.to_bytes(2, 'little', signed=True)
                    + bytes((self.station, self.target, 0, self.stations, 2, 0))
                    + bytes(4) + bytes((self.paused, 0 if self.stopped else self.leading_end, 0xCC, 0)))
            self.buffer += Frame(0x25, data, self.reply_prefix).encode()
        elif not self.silent and frame.command == 0x21:
            self.buffer += Frame(0x21, self.speed.to_bytes(2, 'little'), self.reply_prefix).encode()
        return len(raw)

    def close(self):
        self.buffer = b''


def serial_ports():
    if list_ports is None:
        return []
    return [{'device': port.device, 'description': port.description}
            for port in sorted(list_ports.comports())]


def hex_input(value, count, label):
    pattern = r'\s*'.join([r'[0-9a-fA-F]{2}'] * count)
    if not isinstance(value, str) or re.fullmatch(pattern, value.strip()) is None:
        raise ValueError(f'{label} must contain {count} hex byte(s).')
    return bytes.fromhex(re.sub(r'\s', '', value))


def motion_block(owner):
    if owner.closing:
        return 'Service is closing. Reopen the page after the service restarts.'
    if owner.storage_error:
        return 'Job storage failed. Report the problem before moving.'
    if owner.execution_uncertain:
        return 'Robot state is uncertain. Check the robot before moving.'
    if owner.active:
        return 'A robot job is running. Wait for it to finish before sending movement.'
    return ''


def expected_status(name, value):
    return {
        'set_speed': {'set_speed_m_h': value},
        'forward': {'stopped': False, 'leading_end': 1},
        'reverse': {'stopped': False, 'leading_end': 255},
        'stop': {'stopped': True, 'command_stop': True}, 'pause': {'paused': True, 'stopped': True},
        'resume': {'paused': False, 'stopped': False},
        'attachment_raise': {'lift_state': 1}, 'attachment_lower': {'lift_state': 2},
        'attachment_stop': {'roller_speed': 0},
    }.get(name)


@dataclass
class Confirmation:
    expected: dict | None
    before: dict
    sent_at: float
    status_seen: bool = False
    travelling: bool = False


class Connection:
    """One transport per server. Owner lock fences motion at the actual write."""

    def __init__(self, owner):
        self.owner = owner
        self.lock = Lock()
        self.send_lock = Lock()
        self.transport = None
        self.id = self.sequence = 0
        self.connected_at = self.connected_iso = None
        self.settings = None
        self.armed = False
        self.status = self.status_at = self.status_hex = None
        self.results = {}
        self.pending = {}
        self.replaced = deque(maxlen=20)
        self.awaiting_ack = {}
        self.events = deque(maxlen=200)
        self.event_id = 0
        self.stale = False
        self.reading = None
        self.recent_tx = {}
        # shortcut: Keep the latest 200 server events; add durable capture if tests need unattended logging.
        self.log = deque(maxlen=200)
        self.log_id = 0
        self.error = None

    def _record(self, direction, raw, decoded):
        self.log_id += 1
        entry = {'id': self.log_id, 'timestamp': timestamp(),
                 'direction': direction, 'hex': hex_bytes(raw), 'decode': decoded}
        self.log.append(entry)
        return entry

    def _event(self, field, before, after, **detail):
        self.event_id += 1
        self.events.append(dict(id=self.event_id, timestamp=timestamp(), field=field, before=before, after=after,
                                **detail))

    def _check_stale(self, now):
        stale = self.transport is not None and self.connected_at is not None and now - (
            self.status_at if self.status_at is not None else self.connected_at) >= STALE_AFTER
        if stale != self.stale:
            self._event('link_stale', self.stale, stale, disconnected=self.transport is None)
            self.stale = stale

    def _finish_confirmation(self, name, state):
        pending = self.pending.pop(name)
        self.results[name].update(state=state, completed_at=timestamp(), elapsed_s=monotonic() - pending.sent_at)

    def _expire(self):
        now = monotonic()
        self._check_stale(now)
        for name, pending in list(self.pending.items()):
            remaining = max(0, STALE_AFTER - (now - pending.sent_at))
            result = self.results[name]
            result['remaining_s'] = remaining
            if remaining == 0 and not pending.travelling:
                if pending.status_seen:
                    self._finish_confirmation(name, 'not confirmed')
                else:
                    result['state'] = 'waiting for status'
        self.awaiting_ack = {tx: item for tx, item in self.awaiting_ack.items()
                             if now - item[1] <= 4 * REPLY_TIMEOUT}

    def _acknowledge(self, decoded):
        if self.awaiting_ack:
            _, (name, sent_at, result) = self.awaiting_ack.popitem()
            elapsed = monotonic() - sent_at
            result.update(ack_elapsed_s=elapsed, acknowledged_at=timestamp())
            decoded.update(acknowledged_command=name, ack_elapsed_s=elapsed)

    def _status_received(self, decoded, raw):
        previous = self.status or {}
        for field in EVENT_FIELDS:
            before = previous.get(field, False if field == 'obstacle_stop' else 0 if field == 'fault_code' else None)
            if field in decoded and before is not None and decoded[field] != before:
                self._event(field, before, decoded[field])
        self.status, self.status_hex, self.status_at = decoded, hex_bytes(raw), monotonic()
        self._check_stale(self.status_at)
        for name, pending in list(self.pending.items()):
            pending.status_seen = True
            if name == 'call_station':
                value = self.results[name]['value']
                learned = 1 <= value <= decoded.get('max_stations', 0)
                if learned and decoded.get('current_station') == value and decoded.get('stopped') is True:
                    self._finish_confirmation(name, 'arrived')
                else:
                    pending.travelling = (learned and decoded.get('target_station') == value
                                          and decoded.get('stopped') is False)
                    self.results[name]['state'] = 'travelling' if pending.travelling else SENT
            elif pending.expected and all(decoded.get(key) == value for key, value in pending.expected.items()) and (
                    not pending.before or any(key in pending.before and pending.before[key] != value
                                              for key, value in pending.expected.items())):
                self._finish_confirmation(name, 'confirmed by status')
        self._expire()

    def _receive(self):
        chunk = self.transport.read(min(4096, self.transport.in_waiting))
        for raw, error in self.splitter.feed(chunk):
            if error:
                self._record('RX', raw, {'error': error})
                continue
            sent_at = self.recent_tx.get(raw)
            if sent_at is not None and monotonic() - sent_at <= 4 * REPLY_TIMEOUT:
                self._record('RX', raw, {'description': 'echo of our transmission'})
                continue
            offset = 3 if raw.startswith(self.splitter.prefixed_headers) else 0
            frame = parse_frame(raw, offset=offset)
            decoded = decode(frame)
            if frame.command == 0x25 and decoded.get('vehicle_id') != self.splitter.prefix[1]:
                self._record('RX', raw, {'error': 'Missing or unexpected vehicle ID.', 'report': decoded})
                continue
            if frame.prefix:
                decoded['reply_prefix'] = hex_bytes(frame.prefix)
            self._expire()
            if decoded.get('acknowledgement'):
                self._acknowledge(decoded)
                self._record('RX', raw, decoded)
            elif frame.command == 0x25:
                if 'mode' not in decoded:
                    self._record('RX', raw, {'error': 'Incomplete AGV status.', 'report': decoded})
                    continue
                fault = decoded['fault_code']
                decoded['fault_page'] = 17 if fault >= 200 else 16
                decoded['fault_description'] = FAULTS.get(fault, 'Unknown fault') if fault else 'No fault'
                self._record('RX', raw, decoded)
                self._status_received(decoded, raw)
                if self.reading == 'query_status':
                    self._finish_read('reply: status')
            else:
                self._record('RX', raw, decoded)
                if self.reading == 'read_speed' and 'read_speed_m_h' in decoded:
                    self._finish_read(f"reply: {decoded['read_speed_m_h']} m/h")

    def _finish_read(self, state):
        result = self.results[self.reading]
        result.update(state=state, elapsed_s=monotonic() - self.read_sent_at, completed_at=timestamp())
        self.reading = None

    def _snapshot(self):
        self._expire()
        self.sequence += 1
        now = monotonic()
        age = None if self.status_at is None else now - self.status_at
        if self.reading is not None:
            self.results[self.reading]['remaining_s'] = max(0, REPLY_TIMEOUT - (now - self.read_sent_at))
        return dict(connected=self.transport is not None, connection_id=self.id, sequence=self.sequence, settings=self.settings,
                    connection_started_at=self.connected_iso,
                    armed=self.armed, motion_block=motion_block(self.owner), status=self.status,
                    status_hex=self.status_hex, status_age_s=age,
                    stale=self.stale, events=list(self.events),
                    results=deepcopy(self.results), replaced=deepcopy(list(self.replaced)),
                    log=list(self.log), error=self.error,
                    log_truncated=self.log_id > self.log.maxlen,
                    silent=isinstance(self.transport, Simulator) and self.transport.silent)

    def capability(self):
        try:
            ports = serial_ports()
            port_error = None
        except OSError as exc:
            ports, port_error = [], str(exc)
        with self.lock:
            if self.transport is not None:
                try:
                    self._receive()
                except OSError as exc:
                    self._transport_error(exc)
            return dict(self._snapshot(), serial_available=serial is not None, ports=ports,
                        port_error=port_error, commands=[
                            dict(name=name, label=label, payload=payload, argument=ARGUMENTS.get(name),
                                 motion=name not in URGENT and name not in READS)
                            for name, (label, payload) in COMMANDS.items()
                        ])

    def _connect(self, body):
        if self.transport is not None:
            raise ValueError('Disconnect the open AGV transport first.')
        if self.owner.closing:
            raise ValueError('Service is closing.')
        mode = body.get('mode', 'simulator')
        prefix = (hex_input(body.get('address', '00 01'), 2, 'Address')
                  + hex_input(body.get('channel', '04'), 1, 'Channel'))
        port = None
        if mode == 'simulator':
            stations = body.get('stations', 3)
            if type(stations) is not int or not 0 <= stations <= 255:
                raise ValueError('Simulator station count must be a whole number from 0 to 255.')
            transport = Simulator(prefix)
            transport.stations = stations
        elif mode == 'serial':
            if serial is None:
                raise ValueError('Serial connection requires pyserial.')
            port = body.get('port')
            if not isinstance(port, str) or port not in {item['device'] for item in serial_ports()}:
                raise ValueError(f'Serial port is busy or missing. {PORT_RETRY}')
            # XBOT RS485 protocol p. 1. The IPC/adapter must handle RS485 direction switching.
            try:
                transport = serial.Serial(port=port, baudrate=9600, bytesize=8, parity='N',
                                          stopbits=1, timeout=0, write_timeout=WRITE_TIMEOUT, exclusive=True)
            except OSError as exc:
                self._record('Link', b'', {'error': str(exc)})
                self.error = port_failure(exc)
                raise ValueError(self.error) from exc
        else:
            raise ValueError('Select Simulator or Serial port.')
        self.transport = transport
        self.splitter = Splitter(prefix)
        self.id += 1
        self.connected_at = monotonic()
        self.connected_iso = timestamp()
        self.settings = dict(mode=mode, port=port, address=hex_bytes(prefix[:2]), channel=hex_bytes(prefix[2:]))
        self.status = self.status_at = self.status_hex = self.error = None
        self.armed = False
        self.results.clear()
        self.pending.clear()
        self.replaced.clear()
        self.awaiting_ack.clear()
        self.events.clear()
        self.event_id = 0
        self.stale = False
        self.recent_tx.clear()
        self.log.clear()
        self.log_id = 0

    def _check_connection(self, connection_id):
        if type(connection_id) is not int or connection_id != self.id:
            raise ValueError('The AGV connection changed. Refresh before sending another command.')

    def _close(self):
        transport, self.transport = self.transport, None
        self.armed = False
        if self.reading is not None:
            self._finish_read('no reply')
        for name in list(self.pending):
            self._finish_confirmation(name, 'not checked')
        self.awaiting_ack.clear()
        if transport is not None:
            transport.close()

    def close(self):
        with self.lock:
            try:
                self._close()
            except OSError as exc:
                self.error = str(exc)

    def _transport_error(self, exc):
        self.error = LINK_LOST
        self._event('transport_error', False, True)
        self._record('Link', b'', {'error': str(exc)})
        try:
            self._close()
        except OSError as close_error:
            self._record('Link', b'', {'error': str(close_error)})

    def _send(self, name, value, *, background=False):
        frame = command_frame(name, value, self.splitter.prefix)
        try:
            self._receive()
        except OSError as exc:
            if name not in URGENT:
                raise
            self._record('RX', b'', {'error': str(exc)})
        before = self.status or {}
        maximum = before.get('max_stations')
        if name == 'call_station' and maximum and value > maximum:
            raise ValueError(f'Choose a learned station from 1 to {maximum}.')
        raw = frame.encode()
        if self.transport.write(raw) != len(raw):
            raise OSError('Incomplete write. The command may have reached the AGV.')
        sent_at = monotonic()
        self.recent_tx = {wire: at for wire, at in self.recent_tx.items() if sent_at - at <= 4 * REPLY_TIMEOUT}
        self.recent_tx[raw] = sent_at
        detail = {'command': name, 'label': COMMANDS[name][0]}
        if value is not None:
            detail['value'] = value
        if background:
            detail['background'] = True
        entry = self._record('TX', raw, detail)
        result = {'tx_id': entry['id'], 'sent_at': entry['timestamp'], 'value': value}
        if name in READS:
            self.reading, self.read_sent_at = name, sent_at
            self.results[name] = dict(result, state='waiting for reply', background=background)
            return sent_at

        result['state'] = SENT
        if name == 'call_station' and not maximum:
            result['warning'] = ('Station learning is unverified.' if maximum is None
                                 else f'Station {value} is above the last reported maximum (0).')
        if name != 'set_speed':
            for pending in list(self.pending):
                if pending not in ('set_speed', name):
                    self._finish_confirmation(pending, 'not checked')
        if name in self.pending:
            self._finish_confirmation(name, 'not checked')
            self.replaced.append((name, self.results[name]))
        self.results[name] = result
        self.awaiting_ack[entry['id']] = (name, sent_at, result)
        self.pending[name] = Confirmation(expected_status(name, value), before, sent_at)

    def _wait_for_reply(self, transport, deadline):
        while True:
            with self.lock:
                if self.transport is not transport or self.reading is None:
                    return self._snapshot()
                try:
                    self._receive()
                except OSError as exc:
                    self._transport_error(exc)
                    raise ValueError(self.error) from exc
                if self.reading is None:
                    return self._snapshot()
                remaining = deadline - monotonic()
                if remaining <= 0:
                    self._finish_read('no reply')
                    return self._snapshot()
            sleep(min(.01, remaining))

    def submit(self, body):
        action, name = body.get('action'), body.get('command')
        if action == 'send' and (not isinstance(name, str) or name not in COMMANDS):
            raise ValueError('Select a known AGV command.')
        # Preserve half-duplex pacing for ordinary writes while Stop and Pause bypass the reply wait.
        with self.send_lock if action == 'send' and name not in URGENT else nullcontext():
            return self._submit(body, action, name)

    def _submit(self, body, action, name):
        motion = action == 'send' and name not in URGENT and name not in READS
        with self.owner.lock if motion else nullcontext(), self.lock:
            if action == 'connect':
                self._connect(body)
            elif self.transport is None:
                raise ValueError('Connect an AGV transport first.')
            else:
                try:
                    if action == 'send':
                        if motion:
                            self._check_connection(body.get('connection_id'))
                            reason = motion_block(self.owner)
                            if reason:
                                raise ValueError(reason)
                            if not self.armed:
                                raise ValueError('Tick Armed first.')
                        elif name in READS and body.get('connection_id') is not None:
                            self._check_connection(body['connection_id'])
                        sent_at = self._send(name, body.get('value') if name not in URGENT else None,
                                                 background=name == 'query_status' and body.get('background') is True)
                    else:
                        self._check_connection(body.get('connection_id'))
                        if action == 'disconnect':
                            self._close()
                        elif action == 'arm' and type(body.get('armed')) is bool:
                            self.armed = body['armed']
                        elif action == 'silence' and isinstance(self.transport, Simulator) and type(body.get('silent')) is bool:
                            self.transport.silent = body['silent']
                        else:
                            raise ValueError('Invalid AGV connection action.')
                except OSError as exc:
                    self._transport_error(exc)
                    raise ValueError(self.error) from exc
            if action != 'send' or name not in READS:
                return self._snapshot()
            transport = self.transport
        return self._wait_for_reply(transport, sent_at + REPLY_TIMEOUT)
