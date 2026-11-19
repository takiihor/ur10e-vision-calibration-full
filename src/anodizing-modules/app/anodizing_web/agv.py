"""XBOT AGV600 frames, simulator and a serialized, operator-owned test link."""

from collections import deque
from contextlib import nullcontext
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
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
SENT = 'sent (protocol has no ACK)'
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
    'set_speed': {'min': 500, 'max': 4000, 'default': 1257, 'label': 'Speed (m/h)'},  # XBOT manual p. 2.
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


def decode(frame):
    if frame.command == 0x21:
        # The two-byte reply was observed on AGV600 2026-11-19.
        if len(frame.data) == 2 or (len(frame.data) == 3 and frame.data[0] == 0x7D):
            return {'read_speed_m_h': int.from_bytes(frame.data[-2:], 'little')}
    if frame.command == 0x01 and not frame.data:
        return {'command': '01', 'data': '', 'description': 'unidentified frame 01 (not in the XBOT protocol)'}
    if frame.command != 0x25:
        return {'command': f'{frame.command:02X}', 'data': hex_bytes(frame.data)}
    inner = frame.encode()[len(frame.prefix):]
    values = {'header': 'A5 5A', 'length': len(inner), 'command': '25'}
    for offset, (key, size) in STATUS_FIELDS.items():
        if offset + size <= len(inner) - 1:
            value = int.from_bytes(inner[offset:offset + size], 'little')
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
        self.target = 1
        self.paused = False
        self.command_stop = False
        self.leading_end = 0

    @property
    def in_waiting(self):
        now = monotonic()
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
            if operation == 0x7D:
                self.speed = low + 256 * high
            elif operation == 0x87:
                self.stopped = self.command_stop = low == 4
                self.paused = False
                if low in (1, 2):
                    self.leading_end = {1: 1, 2: 255}[low]
            elif operation == 0x9D:
                self.target = low
                self.stopped = self.paused = self.command_stop = False
            elif operation == 0x5B:
                self.paused = self.stopped = low == 1
                self.command_stop = False
        elif not self.silent and frame.command == 0x26:
            speed = self.speed.to_bytes(2, 'little')
            data = (bytes((self.prefix[1], 86)) + speed
                    + bytes((self.stopped, 0, 0, self.command_stop))
                    + (b'\0\0' if self.stopped else speed)
                    + bytes((1, self.target, 0, 8, 2, 0))
                    + bytes(4) + bytes((self.paused, self.leading_end, 0xCC, 0)))
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
        return 'Service is closing.'
    if owner.storage_error:
        return 'Job storage failed.'
    if owner.execution_uncertain:
        return 'Execution is uncertain. Check the physical state.'
    if owner.active:
        return 'Another job owns the service.'
    return ''


def expected_status(name, value):
    return {
        'set_speed': {'set_speed_m_h': value},
        'forward': {'stopped': False, 'leading_end': 1},
        'reverse': {'stopped': False, 'leading_end': 255},
        'stop': {'stopped': True, 'command_stop': True}, 'pause': {'paused': True, 'stopped': True},
        'resume': {'paused': False, 'stopped': False},
        'call_station': {'target_station': value},
        'attachment_raise': {'lift_state': 1}, 'attachment_lower': {'lift_state': 2},
        'attachment_stop': {'roller_speed': 0},
    }.get(name)


class Connection:
    """One transport per server. Owner lock fences motion at the actual write."""

    def __init__(self, owner):
        self.owner = owner
        self.lock = Lock()
        self.send_lock = Lock()
        self.transport = None
        self.id = self.sequence = 0
        self.connected_at = None
        self.settings = None
        self.armed = False
        self.status = self.status_at = self.status_hex = None
        self.results = {}
        self.pending = {}
        self.reading = None
        self.recent_tx = {}
        # shortcut: Keep the latest 200 server events; add durable capture if tests need unattended logging.
        self.log = deque(maxlen=200)
        self.log_id = 0
        self.error = None

    def _record(self, direction, raw, decoded):
        self.log_id += 1
        self.log.append({'id': self.log_id, 'timestamp': datetime.now(timezone.utc).isoformat(timespec='milliseconds'),
                         'direction': direction, 'hex': hex_bytes(raw), 'decode': decoded})

    def _expire(self):
        now = monotonic()
        for name, (_, _, sent_at) in list(self.pending.items()):
            if now - sent_at >= STALE_AFTER:
                self.results[name]['state'] = 'not confirmed'
                del self.pending[name]

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
            self._record('RX', raw, decoded)
            self._expire()
            if frame.command == 0x25:
                self.status, self.status_hex, self.status_at = decoded, hex_bytes(raw), monotonic()
                for name, (expected, before, _) in list(self.pending.items()):
                    if expected is not None and all(decoded.get(key) == value for key, value in expected.items()) and (
                            any(key in before and before[key] != value for key, value in expected.items())):
                        self.results[name]['state'] = 'confirmed by status'
                        del self.pending[name]
                if self.reading == 'query_status':
                    self._finish_read('reply: status')
            elif self.reading == 'read_speed' and 'read_speed_m_h' in decoded:
                self._finish_read(f"reply: {decoded['read_speed_m_h']} m/h")

    def _finish_read(self, state):
        self.results[self.reading]['state'] = state
        self.reading = None

    def _snapshot(self):
        self._expire()
        self.sequence += 1
        now = monotonic()
        age = None if self.status_at is None else now - self.status_at
        stale = self.transport is not None and now - (self.status_at if age is not None else self.connected_at) >= STALE_AFTER
        return dict(connected=self.transport is not None, connection_id=self.id, sequence=self.sequence, settings=self.settings,
                    armed=self.armed, motion_block=motion_block(self.owner), status=self.status,
                    status_hex=self.status_hex, status_age_s=age,
                    stale=stale,
                    results=deepcopy(self.results), log=list(self.log), error=self.error,
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
            transport = Simulator(prefix)
        elif mode == 'serial':
            if serial is None:
                raise ValueError('Serial connection requires pyserial.')
            port = body.get('port')
            if not isinstance(port, str) or port not in {item['device'] for item in serial_ports()}:
                raise ValueError('Select a listed serial port.')
            # XBOT RS485 protocol p. 1. The IPC/adapter must handle RS485 direction switching.
            transport = serial.Serial(port=port, baudrate=9600, bytesize=8, parity='N',
                                      stopbits=1, timeout=0, write_timeout=WRITE_TIMEOUT, exclusive=True)
        else:
            raise ValueError('Select Simulator or Serial port.')
        self.transport = transport
        self.splitter = Splitter(prefix)
        self.id += 1
        self.connected_at = monotonic()
        self.settings = dict(mode=mode, port=port, address=hex_bytes(prefix[:2]), channel=hex_bytes(prefix[2:]))
        self.status = self.status_at = self.status_hex = self.error = None
        self.armed = False
        self.results.clear()
        self.pending.clear()
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
        for name in self.pending:
            self.results[name]['state'] = 'not confirmed'
        self.pending.clear()
        if transport is not None:
            transport.close()

    def close(self):
        with self.lock:
            try:
                self._close()
            except OSError as exc:
                self.error = str(exc)

    def _transport_error(self, exc):
        self.error = f'AGV transport error: {exc} Check the link before reconnecting.'
        try:
            self._close()
        except OSError as close_error:
            self.error += f' Close failed: {close_error}'

    def _send(self, name, value):
        frame = command_frame(name, value, self.splitter.prefix)
        try:
            self._receive()
        except OSError as exc:
            if name not in URGENT:
                raise
            self._record('RX', b'', {'error': str(exc)})
        raw = frame.encode()
        if self.transport.write(raw) != len(raw):
            raise OSError('Incomplete write. The command may have reached the AGV.')
        sent_at = monotonic()
        self.recent_tx = {wire: at for wire, at in self.recent_tx.items() if sent_at - at <= 4 * REPLY_TIMEOUT}
        self.recent_tx[raw] = sent_at
        detail = {'command': name, 'label': COMMANDS[name][0]}
        if value is not None:
            detail['value'] = value
        self._record('TX', raw, detail)
        if name in READS:
            self.reading = name
            self.results[name] = {'state': 'waiting for reply'}
            return sent_at

        result = {'state': SENT}
        before = self.status if self.status is not None else {}
        maximum = before.get('max_stations')
        if name == 'call_station' and maximum is not None and value > maximum:
            result['warning'] = f'Station {value} is above the last reported maximum ({maximum}).'
        if name != 'set_speed':
            for pending in list(self.pending):
                if pending not in ('set_speed', name):
                    self.results[pending]['state'] = 'not confirmed'
                    del self.pending[pending]
        self.results[name] = result
        self.pending[name] = (expected_status(name, value), before, sent_at)

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
                                raise ValueError('Tick Armed for this AGV connection first.')
                        elif name in READS and body.get('connection_id') is not None:
                            self._check_connection(body['connection_id'])
                        sent_at = self._send(name, body.get('value') if name not in URGENT else None)
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
