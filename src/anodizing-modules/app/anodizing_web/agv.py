"""XBOT AGV600 frames, simulator and a serialized, operator-owned test link."""

from collections import deque
from contextlib import contextmanager, nullcontext
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
import errno
from itertools import pairwise
import random
import re
from statistics import median
from threading import Event, Lock, Thread
from time import monotonic, sleep

try:
    import serial
    from serial.tools import list_ports
except ImportError:
    serial = list_ports = None


HEADER = b'\xa5\x5a'
DEFAULT_PREFIX = b'\x00\x01\x04'
REPLY_TIMEOUT = 1.5
STATUS_REPLY_EXPECTED = .95  # Observed query replies took .83–.88 s.
WRITE_TIMEOUT = .5
STALE_AFTER = 5
TIMED_REPORT_LISTEN_S = 3.1  # Vendor timed reports are 1–3 seconds apart.
PAGE_POLL_S = 1.2
RECIPE_STALE_S = 3
RECIPE_POLL_S = .9
MOTION_POLL_S = .5
UNIDENTIFIED_INTERVAL = 11.22
REPORT_GAP = (.100, .150)  # Observed post-report transmit window.
ACK_WINDOW = .75
FALLBACK_INTERVAL = 1.5
LATE_ACK_GRACE = .3
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
REPEATABLE = {'set_speed', 'forward', 'reverse', 'call_station', 'attachment_stop'}
READS = {'query_status', 'read_speed'}
STATION_CALL_REQUIRED = ('Station call needs the station-call function, set in the XBOT app. '
                         'In standard mode, use Forward/Reverse.')


def station_limit(learned_legs):
    # XBOT V2.0 manual lines 630–645 numbers the start as 1. Android 3.5.3
    # DrawLine includes that start in its count + 1 points. Treat RS485 byte 17 the same way.
    # RS485 protocol lines 142–149 encode the target in one byte.
    return min(learned_legs + 1, ARGUMENTS['call_station']['max']) if learned_legs else 0


def arrived_station(status):
    station = status.get('current_station')
    if (type(station) is int and status.get('mode') == 2
            and 1 <= station <= station_limit(status.get('max_stations', 0))
            and status.get('station_arrival') is True and status.get('stopped') is True
            and status.get('actual_speed_m_h') == 0 and status.get('fault_code') == 0
            and status.get('obstacle_stop') is False):
        return station
    return None


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
    def __init__(self, prefix, *, loss_fraction=0, seed=0):
        if not 0 <= loss_fraction <= 1:
            raise ValueError('Simulator loss fraction must be from 0 to 1.')
        self.prefix = prefix
        self.reply_prefix = reply_prefix(prefix)
        self.loss_fraction = loss_fraction
        self.random = random.Random(seed)
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
        self.mode = 2
        self.next_report = monotonic() + 1

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
        if not self.silent and self.mode == 0 and now >= self.next_report:
            self._report()
            self.next_report = now + 1
        return len(self.buffer)

    def read(self, size):
        data, self.buffer = self.buffer[:size], self.buffer[size:]
        return data

    def write(self, raw):
        frame = parse_frame(raw, offset=3)
        if self.loss_fraction and self.random.random() < self.loss_fraction:
            return len(raw)
        if frame.command == 0x23:
            operation, low, high = frame.data
            if ((self.mode != 2 and operation in (0x5B, 0x9D))
                    or (self.mode == 2 and operation == 0x87 and low == 4)):
                return len(raw)
            if operation == 0x9D and (high or self.stations < 2 or not 1 <= low <= self.stations):
                return len(raw)
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
                self.arrives_at = monotonic() + 3
            elif operation == 0x5B:
                resume_station = (self.paused and self.station != self.target
                                  and self.stations >= 2 and 1 <= self.target <= self.stations)
                self.paused = self.stopped = low == 1
                self.arrives_at = monotonic() + 3 if low == 2 and resume_station else None
                self.command_stop = False
        elif not self.silent and frame.command == 0x26:
            self._report()
        elif not self.silent and frame.command == 0x21:
            self.buffer += Frame(0x21, self.speed.to_bytes(2, 'little'), self.reply_prefix).encode()
        return len(raw)

    def _report(self):
        speed = self.speed.to_bytes(2, 'little')
        actual = 0 if self.stopped else -self.speed if self.leading_end == 255 else self.speed
        learned_legs = max(0, self.stations - 1)
        data = (bytes((self.prefix[1], 86)) + speed
                + bytes((self.stopped, 0, self.arrival, self.command_stop))
                + actual.to_bytes(2, 'little', signed=True)
                + bytes((self.station, self.target, 0, learned_legs, self.mode, 0))
                + bytes(4) + bytes((self.paused, 0 if self.stopped else self.leading_end, 0xCC, 0)))
        self.buffer += Frame(0x25, data, self.reply_prefix).encode()

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
    if getattr(owner, 'line_test_fence', False):
        return 'A line test owns the service. Wait for confirmed restore.'
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
    earlier_query: bool = False
    pre_halt_query: bool = False
    halt_sample_seen: bool = False


class ReceiveError(OSError):
    pass


@dataclass
class Resend:
    name: str
    raw: bytes
    sent_at: float
    recipe_check: object = None
    tries: int = 1

    @property
    def interval(self):
        return FALLBACK_INTERVAL if self.name in URGENT else 2

    @property
    def reply_wait(self):
        return 1 if self.name in URGENT else self.interval

    @property
    def limit(self):
        if self.name in URGENT:
            return 5
        return 4 if self.name in REPEATABLE else 1


class Connection:
    """One transport per server. Owner lock fences motion at the actual write."""

    record_event = None

    def __init__(self, owner):
        self.owner = owner
        self.recipe_active = False
        self.recipe_statuses = deque(maxlen=64)
        self.motion_poll_at = 0
        self.status_iso = None
        self.last_confirmed_station = None
        self.stop_generation = 0
        self.teaching_generation = 0
        self.urgent_waiters = 0
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
        self.unmatched_ack = False
        self.unanswered_query = False
        self.resend = self.retry_worker = None
        self.manual_retry_worker = None
        self.last_write_at = float('-inf')
        self.link_quality = dict.fromkeys(('sent', 'acknowledged', 'resent', 'no_reply'), 0)
        self.passive_reports = deque(maxlen=4)
        self.listen_until = 0
        self.timed_report_interval = None
        self.query_at = None
        self.events = deque(maxlen=200)
        self.event_id = 0
        self.stale = False
        self.reading = None
        self.recent_tx = {}
        # shortcut: Keep the latest 200 server events; add durable capture if tests need unattended logging.
        self.log = deque(maxlen=200)
        self.log_id = 0
        self.error = None

    def _capture(self, event, **detail):
        if self.record_event is not None:
            self.record_event('agv', event, connection_id=self.id,
                              transport=(self.settings or {}).get('mode'), **detail)

    def _record(self, direction, raw, decoded, *, tx_id=None, **audit):
        self.log_id += 1
        entry = {'id': self.log_id, 'timestamp': timestamp(),
                 'direction': direction, 'hex': hex_bytes(raw), 'decode': decoded}
        self.log.append(entry)
        self._capture('frame', **entry, tx_id=(self.log_id if tx_id is None and direction == 'TX' else tx_id),
                      **audit)
        return entry

    def _event(self, field, before, after, **detail):
        self.event_id += 1
        entry = dict(id=self.event_id, timestamp=timestamp(), field=field, before=before, after=after, **detail)
        self.events.append(entry)
        self._capture(field, **entry)

    def _check_stale(self, now):
        stale = self.transport is not None and self.connected_at is not None and now - (
            self.status_at if self.status_at is not None else self.connected_at) >= STALE_AFTER
        if stale != self.stale:
            self._event('link_stale', self.stale, stale, disconnected=self.transport is None)
            self.stale = stale

    def _finish_confirmation(self, name, state):
        pending = self.pending.pop(name)
        result = self.results[name]
        result.update(state=state, completed_at=timestamp(), elapsed_s=monotonic() - pending.sent_at)
        halt = {key: result[key] for key in ('requested_command', 'wire_command') if key in result}
        self._capture('command_outcome', command=name, tx_id=result['tx_id'], state=state,
                      timestamp=result['completed_at'], **halt)
        if self.resend is not None and self.resend.name == name:
            self.resend = None

    def _cancel_resend(self, *, keep_urgent=False):
        if self.resend is not None and not (keep_urgent and self.resend.name in URGENT):
            self._finish_confirmation(self.resend.name, 'not checked')

    def _expire(self):
        now = monotonic()
        self._check_stale(now)
        active_tx = None
        for name, pending in list(self.pending.items()):
            remaining = max(0, STALE_AFTER - (now - pending.sent_at))
            result = self.results[name]
            if self.resend is not None and self.resend.name == name:
                retry = self.resend
                interval = retry.interval
                if name in URGENT and self._timed_reports_on_time(now):
                    interval = self.timed_report_interval
                result['remaining_s'] = max(0, retry.sent_at + interval * (retry.limit - retry.tries)
                                            + retry.reply_wait - now)
                active_tx = result['tx_id']
                continue
            result['remaining_s'] = remaining
            if remaining == 0 and not pending.travelling:
                if pending.status_seen:
                    self._finish_confirmation(name, 'not confirmed')
                else:
                    result['state'] = 'waiting for status'
        if any(now > expires_at and tx != active_tx and self._ack_outstanding(result)
               for tx, (_, _, result, expires_at) in self.awaiting_ack.items()):
            self.unmatched_ack = True
        self.awaiting_ack = {tx: item for tx, item in self.awaiting_ack.items()
                             if now <= item[3] or tx == active_tx}

    @staticmethod
    def _ack_outstanding(result):
        # A single short ACK cannot account for multiple writes of the same command.
        return result['tries'] > 1 or 'ack_elapsed_s' not in result

    def _acknowledge(self, decoded):
        # The ACK carries only vehicle ID. The vendor specifies no maximum delay.
        retry = self.resend
        if retry is not None and retry.name in URGENT:
            result = self.results[retry.name]
            if len(self.awaiting_ack) > 1 or self.unmatched_ack or result['ambiguous_ack']:
                result['unattributed_ack'] = True
                return
        if len(self.awaiting_ack) == 1:
            name, sent_at, result, expires_at = next(iter(self.awaiting_ack.values()))
            if ('ack_elapsed_s' in result or self.results.get(name) is not result
                    or result['state'] == 'not checked'
                    or (name in URGENT and (self.unmatched_ack or result.get('ambiguous_ack')))):
                return
            now = monotonic()
            elapsed = now - sent_at
            result.update(ack_elapsed_s=elapsed, acknowledged_at=timestamp())
            if result['state'] == 'no reply':
                result.update(state='acknowledged late', completed_at=result['acknowledged_at'],
                              elapsed_s=result['elapsed_s'] + now - (expires_at - LATE_ACK_GRACE))
                self.link_quality['no_reply'] -= 1
            if name == 'call_station' and self.recipe_active and self.recipe_call_at is not None:
                result['recipe_ack_elapsed_s'] = sent_at - self.recipe_call_at + elapsed
            decoded.update(acknowledged_command=name, ack_elapsed_s=elapsed)
            self._capture('command_outcome', command=name, tx_id=result['tx_id'],
                          state='acknowledged late' if result['state'] == 'acknowledged late' else 'acknowledged',
                          timestamp=result['acknowledged_at'])
            self.link_quality['acknowledged'] += 1
            if (self.resend is not None and self.resend.name == name
                    and not result.get('mode_unverified')
                    and (result.get('requested_command') != 'stop'
                         or self._wire_halt(self._fresh_mode()) == result['wire_command'])):
                self.resend = None

    def _passive_report(self, now):
        # A query reply and a timed report have the same wire format.
        if self.query_at is not None and now - self.query_at <= REPLY_TIMEOUT:
            if not self.unanswered_query and not self._fresh_timed_reports(now):
                # Two reports for one query indicate a timed stream may have started.
                self.listen_until = now + TIMED_REPORT_LISTEN_S
            return
        self.passive_reports.append(now)
        self.listen_until = now + TIMED_REPORT_LISTEN_S
        if len(self.passive_reports) == self.passive_reports.maxlen:
            gaps = [end - start for start, end in pairwise(self.passive_reports)]
            interval = median(gaps)
            if interval >= .2 and all(abs(gap - interval) <= .3 * interval for gap in gaps):
                self.timed_report_interval = interval

    def _fresh_timed_reports(self, now):
        return (self.timed_report_interval is not None
                and now - self.passive_reports[-1] < 2.5 * self.timed_report_interval)

    def _timed_reports_on_time(self, now):
        return (self.timed_report_interval is not None
                and now - self.passive_reports[-1] < 1.5 * self.timed_report_interval)

    def _send_slot(self, *, retry=False, urgent=False):
        now = monotonic()
        if not urgent and (self.reading is not None or self._urgent_pending(now)):
            return False
        if not self._timed_reports_on_time(now):
            interval = FALLBACK_INTERVAL if retry or self._fresh_timed_reports(now) else ACK_WINDOW
            return now >= self.last_write_at + interval
        report_at = self.passive_reports[-1]
        return (now >= self.last_write_at + ACK_WINDOW
                and report_at + REPORT_GAP[0] <= now <= report_at + REPORT_GAP[1])

    def _status_reply_in_flight(self, now):
        return (self.reading == 'query_status' and not self._fresh_timed_reports(now)
                and now < self.read_sent_at + STATUS_REPLY_EXPECTED)

    def _urgent_pending(self, now):
        return (self.urgent_waiters or (self.resend is not None and self.resend.name in URGENT)
                or any(name in URGENT and 'ack_elapsed_s' not in result and now < expires_at
                       for name, _, result, expires_at in self.awaiting_ack.values()))

    def _wait_send_slot(self, transport, deadline):
        while True:
            with self.lock:
                if self.transport is not transport:
                    raise ValueError('The AGV connection changed. Refresh before sending another command.')
                if transport is None:
                    return True
                try:
                    self._receive()
                except OSError as exc:
                    self._transport_error(exc)
                    raise ValueError(self.error) from exc
                if self._send_slot():
                    return True
            if monotonic() >= deadline:
                return False
            sleep(.01)

    def _status_received(self, decoded, raw):
        previous = self.status or {}
        for field in EVENT_FIELDS:
            before = previous.get(field, False if field == 'obstacle_stop' else 0 if field == 'fault_code' else None)
            if field in decoded and before is not None and decoded[field] != before:
                self._event(field, before, decoded[field])
        now = monotonic()
        station = arrived_station(decoded)
        if (self.status_at is None or now - self.status_at >= RECIPE_STALE_S
                or station != arrived_station(previous)):
            self.teaching_generation += 1
        self.status, self.status_hex, self.status_at = decoded, hex_bytes(raw), now
        self._passive_report(self.status_at)
        self.status_iso = timestamp()
        if station is not None:
            self.last_confirmed_station = (station, self.status_iso)
        if self.recipe_active:
            self.recipe_statuses.append((decoded, self.status_at))
        self._check_stale(self.status_at)
        self.unanswered_query = False
        for name, pending in list(self.pending.items()):
            pending.status_seen = True
            if name == 'call_station':
                value = self.results[name]['value']
                learned = decoded.get('mode') == 2 and 1 <= value <= station_limit(decoded.get('max_stations', 0))
                if station == value:
                    self._finish_confirmation(name, 'arrived')
                else:
                    pending.travelling = (learned and decoded.get('target_station') == value
                                          and decoded.get('stopped') is False)
                    self.results[name]['state'] = 'travelling' if pending.travelling else SENT
                    if pending.travelling and self.resend is not None and self.resend.name == name:
                        self.resend = None
            elif name in URGENT:
                if pending.earlier_query:
                    pending.earlier_query = False
                    continue
                stopped = decoded.get('stopped') is True and decoded.get('actual_speed_m_h') == 0
                result = self.results[name]
                wire = result['wire_command']
                if result['requested_command'] == 'stop' and self._wire_halt(decoded.get('mode')) != wire:
                    pending.halt_sample_seen = False
                    continue
                flag = 'command_stop' if wire == 'stop' else 'paused'
                flag_active = decoded.get(flag) is True
                was_stopped = pending.before.get('stopped') is True and pending.before.get('actual_speed_m_h') == 0
                changed = (stopped and not was_stopped) or (flag_active and pending.before.get(flag) is not True)
                acknowledged = 'ack_elapsed_s' in result
                # Even a completed query may leave a pre-halt status in flight. Require
                # command-specific evidence and corroboration before accepting it.
                corroborated = not pending.pre_halt_query or (flag_active and (acknowledged or pending.halt_sample_seen))
                matches = stopped and (changed or acknowledged
                                       or (self.query_at is not None and self.query_at > pending.sent_at))
                if matches and corroborated:
                    result['confirmed_stopped'] = True
                    self._finish_confirmation(name, 'confirmed by status')
                    continue
                elif flag_active and changed:
                    result.update(confirmed_stopped=False, state='confirmed by status')
                pending.halt_sample_seen = matches and flag_active
            elif pending.expected and all(decoded.get(key) == value for key, value in pending.expected.items()) and (
                    not pending.before or any(key in pending.before and pending.before[key] != value
                                              for key, value in pending.expected.items())):
                self._finish_confirmation(name, 'confirmed by status')
        self._expire()

    def _receive(self):
        try:
            chunk = self.transport.read(min(4096, self.transport.in_waiting))
        except OSError as exc:
            raise ReceiveError(str(exc)) from exc
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
                    self._finish_read('status received')
            else:
                self._record('RX', raw, decoded)
                if self.reading == 'read_speed' and 'read_speed_m_h' in decoded:
                    self._finish_read(f"reply: {decoded['read_speed_m_h']} m/h")
        if self.reading is not None and monotonic() - self.read_sent_at >= REPLY_TIMEOUT:
            self._finish_read('no reply')

    def _finish_read(self, state):
        result = self.results[self.reading]
        result.update(state=state, elapsed_s=monotonic() - self.read_sent_at, completed_at=timestamp())
        self._capture('command_outcome', command=self.reading, tx_id=result['tx_id'], state=state,
                      timestamp=result['completed_at'])
        if state == 'no reply':
            self.link_quality['no_reply'] += 1
        self.reading = None

    def _snapshot(self):
        self._expire()
        self.sequence += 1
        now = monotonic()
        age = None if self.status_at is None else now - self.status_at
        if self.reading is not None:
            self.results[self.reading]['remaining_s'] = max(0, REPLY_TIMEOUT - (now - self.read_sent_at))
        robot_configured = getattr(self.owner, 'robot_configured', False)
        return dict(robot_interlock=robot_configured, recipe_active=self.recipe_active, connected=self.transport is not None, connection_id=self.id, sequence=self.sequence, settings=self.settings,
                    connection_started_at=self.connected_iso, stop_generation=self.stop_generation,
                    armed=self.armed, halt_pending=bool(self._urgent_pending(now)),
                    motion_block=motion_block(self.owner), status=self.status,
                    status_hex=self.status_hex, status_age_s=age, status_time=self.status_iso,
                    connection_age_s=None if self.connected_at is None else now - self.connected_at,
                    stale=self.stale, events=list(self.events),
                    link_quality=dict(self.link_quality, timed_report_interval_s=(
                        self.timed_report_interval if self._fresh_timed_reports(now) else None)),
                    results=deepcopy(self.results), replaced=deepcopy(list(self.replaced)),
                    log=list(self.log), error=self.error,
                    log_truncated=self.log_id > self.log.maxlen,
                    silent=isinstance(self.transport, Simulator) and self.transport.silent)

    def state_sample(self):
        """Copy the acquired sample without servicing the link."""
        with self.lock:
            return (deepcopy(self.status), self.status_iso, self.status_at, self.transport is not None,
                    (self.settings or {}).get('mode'), self.recipe_active, self.last_confirmed_station,
                    self.connected_iso, self.error, monotonic(), self.id, self.sequence)

    @contextmanager
    def recording_context(self):
        with self.lock:
            yield (dict(connection_id=self.id, settings=dict(self.settings))
                   if self.transport is not None and self.settings else None)

    def commissioning(self, *, ports=False):
        if ports:
            try:
                devices, error = serial_ports(), None
            except OSError as exc:
                devices, error = [], str(exc)
        with self.lock:
            snapshot = self._snapshot()
        if ports:
            snapshot.update(serial_available=serial is not None, ports=devices, port_error=error, commands=[
                dict(name=name, label=label, payload=payload, argument=ARGUMENTS.get(name),
                     motion=name not in URGENT and name not in READS)
                for name, (label, payload) in COMMANDS.items()
            ])
        return snapshot

    def link_state(self):
        with self.lock:
            now = monotonic()
            last = self.status_at if self.status_at is not None else self.connected_at
            return {'connected': self.transport is not None, 'connection_id': self.id,
                    'stop_generation': self.stop_generation,
                    'mode': self.settings['mode'] if self.transport is not None else None,
                    'stale': last is not None and now-last >= RECIPE_STALE_S,
                    'status_age_s': None if self.status_at is None else now-self.status_at,
                    'status': self.status, 'results': {name: dict(self.results[name])
                                                       for name in URGENT if name in self.results}}

    def cached_motion_feedback(self):
        with self.lock:
            return self._motion_sample()

    def _motion_sample(self):
        if self.transport is None:
            return {}
        return dict(status=self.status, status_time=self.status_iso, mode=self.settings['mode'],
                    connection_id=self.id, stop_generation=self.stop_generation,
                    status_age_s=None if self.status_at is None else monotonic() - self.status_at,
                    teaching_generation=self.teaching_generation, status_monotonic=self.status_at,
                    movement_pending=any(name not in URGENT and name != 'set_speed' for name in self.pending))

    def motion_feedback(self, *, poll=False):
        # A robot page may request status without taking over an AGV page read.
        acquired = poll and self.send_lock.acquire(blocking=False)
        try:
            with self.lock:
                if self.transport is None:
                    return {}
                now = monotonic()
                try:
                    self._receive()
                    self._expire()
                    if acquired and not self.recipe_active:
                        if (self.reading is None and self.resend is None and now >= self.motion_poll_at
                                and not self._urgent_pending(now)
                                and not self._fresh_timed_reports(now)
                                and (self.status_at is None or now-self.status_at >= MOTION_POLL_S)
                                and self._send_slot()):
                            self.motion_poll_at = self._send('query_status', None, background=True) + MOTION_POLL_S
                            self._receive()
                except OSError as exc:
                    self._transport_error(exc)
                    return {}
                return self._motion_sample()
        finally:
            if acquired:
                self.send_lock.release()

    def capability(self):
        self._service_resend()
        with self.lock:
            if self.transport is not None:
                try:
                    self._receive()
                except OSError as exc:
                    self._transport_error(exc)
        return self.commissioning(ports=True)

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
            agv_mode, loss, seed = body.get('agv_mode', 2), body.get('loss_fraction', 0), body.get('seed', 0)
            if type(agv_mode) is not int or agv_mode not in (0, 2):
                raise ValueError('Select simulator AGV mode 0 (standard) or 2 (station calling).')
            if type(loss) not in (int, float) or not 0 <= loss <= 1 or type(seed) is not int:
                raise ValueError('Use a simulator loss fraction from 0 to 1 and a whole number seed.')
            transport = Simulator(prefix, loss_fraction=loss, seed=seed) if loss else Simulator(prefix)
            transport.stations = stations
            transport.mode = agv_mode
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
        self.listen_until = self.connected_at + TIMED_REPORT_LISTEN_S
        self.settings = dict(mode=mode, port=port, address=hex_bytes(prefix[:2]), channel=hex_bytes(prefix[2:]))
        self.status = self.status_at = self.status_hex = self.status_iso = self.error = None
        self.last_confirmed_station = None
        self.motion_poll_at = 0
        self.armed = False
        self.results.clear()
        self.pending.clear()
        self.replaced.clear()
        self.awaiting_ack.clear()
        self.unmatched_ack = self.unanswered_query = False
        self.last_write_at = float('-inf')
        self.link_quality = dict.fromkeys(self.link_quality, 0)
        self.passive_reports.clear()
        self.timed_report_interval = self.query_at = None
        self.events.clear()
        self.event_id = 0
        self.stale = False
        self.recent_tx.clear()
        self.log.clear()
        self.log_id = 0
        self._capture('connected', settings=dict(self.settings), timestamp=self.connected_iso)
        self._start_worker(receive=True)

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
        self.retry_worker = None
        if transport is not None:
            self._capture('disconnected')
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
        if isinstance(exc, ReceiveError) and self.recipe_active and self.transport is self.recipe_transport:
            self.stop_generation += 1
            name = self._halt_command(recipe=True)
            try:
                self._write(name, command_frame(name, None, self.splitter.prefix).encode(), None, 1,
                            requested_halt='stop')
            except OSError as write_error:
                self._record('Link', b'', {'error': str(write_error), 'command': name})
            else:
                self.recipe_halt_sent = True
        try:
            self._close()
        except OSError as close_error:
            self._record('Link', b'', {'error': str(close_error)})

    def _write(self, name, raw, value, attempt, background=False, *, tx_id=None, requested_halt=None):
        if name not in READS and name not in URGENT and name != 'set_speed':
            self.teaching_generation += 1
        try:
            written = self.transport.write(raw)
        finally:
            self.last_write_at = monotonic()
        if written != len(raw):
            raise OSError('Incomplete write. The command may have reached the AGV.')
        sent_at = self.last_write_at
        self.recent_tx = {wire: at for wire, at in self.recent_tx.items() if sent_at - at <= 4 * REPLY_TIMEOUT}
        self.recent_tx[raw] = sent_at
        detail = {'command': name, 'label': COMMANDS[name][0], 'try': attempt}
        if value is not None:
            detail['value'] = value
        if background:
            detail['background'] = True
        self.link_quality['sent'] += 1
        self.link_quality['resent'] += attempt > 1
        halt = dict(requested_command=requested_halt or name, wire_command=name) if name in URGENT else {}
        return sent_at, self._record('TX', raw, detail, tx_id=tx_id, **halt)

    def _send(self, name, value, *, background=False, prewrite=None, recipe_check=None,
              status_hold_s=None, requested_halt=None, mode_unverified=False):
        frame = command_frame(name, value, self.splitter.prefix)
        if name in URGENT:
            self._cancel_resend()
        else:
            self._receive()
        before = self.status or {}
        if prewrite:
            prewrite()
        if name not in READS and name not in URGENT:
            self._check_motion_status(name, value)
        raw = frame.encode()
        sent_at, entry = self._write(name, raw, value, 1, background, requested_halt=requested_halt)
        result = {'tx_id': entry['id'], 'sent_at': entry['timestamp'], 'value': value, 'tries': 1}
        if name in READS:
            if name == 'query_status':
                self.query_at = sent_at
                self.unanswered_query = True
            self.reading, self.read_sent_at = name, sent_at
            self.results[name] = dict(result, state='waiting for reply', background=background)
            return sent_at

        result['state'] = SENT
        if status_hold_s is not None:
            result['status_hold_s'] = status_hold_s
        if name in URGENT:
            result.update(wire_command=name, mode_unverified=mode_unverified,
                          requested_command=requested_halt or name,
                          ambiguous_ack=any(self._ack_outstanding(candidate)
                                            for _, _, candidate, _ in self.awaiting_ack.values()))
        if name != 'set_speed':
            for pending in list(self.pending):
                if pending not in ('set_speed', name):
                    self._finish_confirmation(pending, 'not checked')
        if name in self.pending:
            self._finish_confirmation(name, 'not checked')
            self.replaced.append((name, self.results[name]))
        elif name in URGENT and name in self.results and self.results[name]['state'] == 'not checked':
            self.replaced.append((name, self.results[name]))
        self._cancel_resend()
        self.results[name] = result
        self.awaiting_ack[entry['id']] = (name, sent_at, result, sent_at + REPLY_TIMEOUT)
        self.pending[name] = Confirmation(expected_status(name, value), before, sent_at,
                                          earlier_query=name in URGENT and self.unanswered_query,
                                          pre_halt_query=name in URGENT and self.query_at is not None)
        self.resend = Resend(name, raw, sent_at, recipe_check)
        if recipe_check is None and self.settings is not None and self.retry_worker is None:
            self._start_worker()
        return sent_at

    def _fresh_mode(self):
        if self.status_at is not None and monotonic() - self.status_at < STALE_AFTER:
            return self.status.get('mode')
        return None

    @staticmethod
    def _wire_halt(mode):
        return 'pause' if mode == 2 else 'stop' if mode in (0, 1) else None

    def _halt_command(self, *, recipe=False):
        return self._wire_halt(self._fresh_mode()) or ('pause' if recipe else 'stop')

    def _send_urgent(self, name, *, recipe=False, connection_id=None):
        with self.lock:
            if connection_id is not None:
                self._check_connection(connection_id)
            transport = self.transport
            if recipe:
                if not self.recipe_active:
                    return False
                if transport is not self.recipe_transport:
                    return self.recipe_halt_sent
            if transport is None:
                raise ValueError('Connect an AGV transport first.')
            self.stop_generation += 1
            self._cancel_resend(keep_urgent=True)
            self.urgent_waiters += 1
        held_at = None
        try:
            while True:
                with self.lock:
                    if self.transport is not transport:
                        if recipe:
                            return (self.recipe_active and transport is self.recipe_transport
                                    and self.recipe_halt_sent)
                        raise ValueError('The AGV connection changed. Refresh before sending another command.')
                    try:
                        self._receive()
                    except ReceiveError as exc:
                        self._record('RX', b'', {'error': str(exc)})
                        waiting = False
                    else:
                        waiting = self._status_reply_in_flight(monotonic())
                    now = monotonic()
                    if not waiting:
                        halt = self._halt_command(recipe=recipe) if name == 'stop' else name
                        retry = self.resend
                        if (retry is not None and retry.name in URGENT
                                and self.results[retry.name]['requested_command'] == name):
                            halt = retry.name
                            result = self.results[halt]
                            self._capture('halt_request', requested_command=name, wire_command=result['wire_command'],
                                          tx_id=result['tx_id'], joined=True)
                        else:
                            try:
                                self._send(halt, None, status_hold_s=0 if held_at is None else now - held_at,
                                           requested_halt=name,
                                           mode_unverified=name == 'stop' and self._wire_halt(self._fresh_mode()) is None)
                            except OSError as exc:
                                self._transport_error(exc)
                                if recipe:
                                    return False
                                raise ValueError(self.error) from exc
                        if recipe:
                            self.recipe_halt_sent = True
                            return True
                        return dict(self._snapshot(), urgent_command=halt)
                    if held_at is None:
                        held_at = now
                sleep(.01)
        finally:
            with self.lock:
                self.urgent_waiters -= 1

    def _start_worker(self, *, receive=False):
        self.retry_worker = Thread(target=self._retry_loop, args=(self.transport, receive),
                                   name='agv-link', daemon=True)
        self.retry_worker.start()

    def _retry_loop(self, transport, receive):
        tick = Event()
        while True:
            tick.wait(.020)
            with self.lock:
                if self.transport is not transport:
                    return
                if not receive:
                    self._expire()
                    if self.resend is None and not self.awaiting_ack:
                        self.retry_worker = None
                        return
                try:
                    self._receive()
                except OSError as exc:
                    self._transport_error(exc)
                    return
            self._service_resend()
            if receive:
                self._poll_status(transport)

    def _poll_status(self, transport):
        if not self.send_lock.acquire(blocking=False):
            return
        try:
            with self.lock:
                if (self.transport is not transport or self.owner.closing or self.recipe_active
                        or self.resend is not None or self.reading is not None):
                    return
                now = monotonic()
                if self._urgent_pending(now):
                    return
                if now < self.listen_until or self._fresh_timed_reports(now) or not self._send_slot():
                    return
                if self.query_at is not None and now - self.query_at < PAGE_POLL_S:
                    return
                try:
                    self._send('query_status', None, background=True)
                except OSError as exc:
                    self._transport_error(exc)
        finally:
            self.send_lock.release()

    def _due(self, retry):
        if retry is None:
            return False
        now = monotonic()
        if retry.tries == retry.limit:
            return now >= retry.sent_at + retry.reply_wait
        if retry.name in URGENT:
            return not self._status_reply_in_flight(now) and self._send_slot(retry=True, urgent=True)
        return now >= retry.sent_at + retry.interval

    def _retry(self, retry):
        if self.resend is not retry or not self._due(retry):
            return False
        name = retry.name
        result = self.results[name]
        if retry.tries == retry.limit:
            if 'ack_elapsed_s' in result:
                self.resend = None
                return False
            self.link_quality['no_reply'] += 1
            if name not in URGENT and retry.limit == 1:
                result['warning'] = 'Delivery is uncertain. Check the AGV before trying again.'
            self._finish_confirmation(name, 'no reply')
            self.awaiting_ack[result['tx_id']] = (name, retry.sent_at, result, monotonic() + LATE_ACK_GRACE)
            return False
        if name in URGENT and retry.tries >= 2 and (
                result.get('unattributed_ack') or (result['mode_unverified'] and not result.get('mode_checked')
                                                   and 'ack_elapsed_s' in result)
        ) and self.reading is None and not self._fresh_timed_reports(monotonic()):
            # After a retry had its reply window, ask whether the halt took effect.
            result.pop('unattributed_ack', None)
            result['mode_checked'] = True
            self._send('query_status', None, background=True)
            return False
        if name not in URGENT and not self._send_slot(retry=True):
            return False
        if retry.recipe_check is not None:
            try:
                retry.recipe_check(self.status, self.status_at, self.recipe_statuses)
            except ValueError:
                self._cancel_resend()
                raise
        if name not in URGENT:
            try:
                self._check_motion_status(name, result['value'])
            except ValueError as exc:
                result['warning'] = str(exc)
                self._cancel_resend()
                if retry.recipe_check is not None:
                    raise
                return False
        attempt = retry.tries + 1
        wire_name, raw = name, retry.raw
        if name in URGENT and result['requested_command'] == 'stop':
            selected = self._wire_halt(self._fresh_mode())
            wire_name = selected or ('pause' if result['wire_command'] == 'stop' else 'stop')
            if selected is None or wire_name != result['wire_command']:
                # A previous wire command may still have a short ACK in flight.
                result['mode_unverified'] = True
            if wire_name != result['wire_command']:
                self.pending[name].halt_sample_seen = False
            result['wire_command'] = wire_name
            raw = command_frame(wire_name, None, self.splitter.prefix).encode()
        retry.sent_at, entry = self._write(
            wire_name, raw, result['value'], attempt, tx_id=result['tx_id'],
            requested_halt=result.get('requested_command'))
        retry.tries = result['tries'] = attempt
        result['last_sent_at'] = entry['timestamp']
        self.awaiting_ack[result['tx_id']] = (name, retry.sent_at, result, retry.sent_at + REPLY_TIMEOUT)
        return True

    def _service_resend(self):
        with self.lock:
            retry = self.resend
            if retry is None or retry.recipe_check is not None:
                return
            if retry.name not in URGENT and not self._due(retry):
                return
            try:
                self._receive()
                if self.resend is not retry:
                    return
                if retry.name in URGENT or retry.tries == retry.limit:
                    self._retry(retry)
                    return
                if not self._send_slot(retry=True):
                    return
            except OSError as exc:
                self._transport_error(exc)
                return
            if getattr(self.owner, 'robot_configured', False):
                if self.manual_retry_worker is None:
                    self.manual_retry_worker = Thread(target=self._retry_ordinary, args=(retry, True),
                                                      name='agv-pose-check', daemon=True)
                    self.manual_retry_worker.start()
                return
        self._retry_ordinary(retry)

    def _retry_ordinary(self, retry, background=False):
        try:
            if not self.send_lock.acquire(blocking=False):
                return
            try:
                if not self.owner.lock.acquire(blocking=False):
                    return
                try:
                    self._retry_manual(retry)
                finally:
                    self.owner.lock.release()
            finally:
                self.send_lock.release()
        finally:
            if background:
                with self.lock:
                    self.manual_retry_worker = None

    def _retry_manual(self, retry):
        with self.lock:
            if self.resend is not retry:
                return
        reason = motion_block(self.owner)
        if not reason and getattr(self.owner, 'robot_configured', False):
            from . import travel_pose
            reason = travel_pose.motion_block(self.owner)
        with self.lock:
            if self.resend is not retry:
                return
            if reason or not self.armed or self.recipe_active:
                if reason:
                    self.results[retry.name]['warning'] = reason
                self._cancel_resend()
                return
            try:
                self._receive()
                self._retry(retry)
            except OSError as exc:
                self._transport_error(exc)

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
        if action == 'connect' and hasattr(self.owner, 'line_test_fence'):
            with self.owner.lock:
                if getattr(self.owner, 'line_test_fence', False):
                    raise ValueError('A line test owns the service. Wait for confirmed restore.')
                return self._submit_locked(body, action, name, False)
        if action != 'send':
            return self._submit_locked(body, action, name, False)
        if name in URGENT:
            return self._send_urgent(name, connection_id=body.get('connection_id'))
        motion = name not in READS
        with self.lock:
            if motion:
                self._check_manual_motion(body)
            transport = self.transport
            if self._urgent_pending(monotonic()) and transport is not None:
                try:
                    self._receive()
                except ReceiveError as exc:
                    self._transport_error(exc)
                    raise ValueError(self.error) from exc
            if self._urgent_pending(monotonic()):
                if name == 'query_status' and body.get('background') is True:
                    return self._snapshot()
                raise ValueError('AGV Stop or Pause is awaiting delivery. Check the AGV before trying again.')
        with self.send_lock:
            deadline = monotonic() + 4
            while self._wait_send_slot(transport, deadline):
                snapshot = self._submit(body, action, name, motion)
                if snapshot is not None:
                    return snapshot
                if monotonic() >= deadline:
                    break
            raise ValueError('Could not find a gap in the AGV reports. Check the radio and try again.')

    def _submit(self, body, action, name, motion):
        with self.owner.lock if motion else nullcontext():
            if motion and getattr(self.owner, 'robot_configured', False):
                from . import travel_pose
                with self.lock:
                    self._check_manual_motion(body)
                reason = travel_pose.motion_block(self.owner)
                if reason:
                    raise ValueError(reason)
            return self._submit_locked(body, action, name, motion)

    def _check_stop_generation(self, approved):
        if type(approved) is not int or approved != self.stop_generation:
            raise ValueError('Stop, Pause or disarming cancelled this movement. Refresh and review before trying again.')

    def _check_motion_status(self, name, value):
        status = self.status or {}
        if (self.status_at is None or monotonic() - self.status_at >= STALE_AFTER
                or status.get('mode') not in (0, 2) or type(status.get('stopped')) is not bool
                or status.get('fault_code') != 0 or status.get('obstacle_stop') is not False):
            raise ValueError('AGV movement needs fresh vehicle status with no fault or obstacle.')
        if name == 'call_station':
            if status['mode'] != 2:
                raise ValueError(STATION_CALL_REQUIRED)
            maximum = station_limit(status.get('max_stations', 0))
            if maximum == 0:
                raise ValueError('AGV station learning is unverified. Learn the route before calling a station.')
            if type(value) is int and value > maximum:
                raise ValueError(f'Choose a learned station from 1 to {maximum}.')
        if name == 'resume' and status['mode'] != 2:
            raise ValueError('Resume needs the station-call function. In standard mode, use Forward/Reverse.')

    def _check_manual_motion(self, body):
        if self.transport is None:
            raise ValueError('Connect an AGV transport first.')
        self._check_connection(body.get('connection_id'))
        reason = motion_block(self.owner)
        if reason:
            raise ValueError(reason)
        if not self.armed:
            raise ValueError('Tick Armed first.')
        self._check_stop_generation(body.get('stop_generation'))

    def _submit_locked(self, body, action, name, motion):
        with self.lock:
            if action == 'connect':
                self._connect(body)
            elif self.transport is None:
                raise ValueError('Connect an AGV transport first.')
            else:
                try:
                    if action == 'send':
                        background = name == 'query_status' and body.get('background') is True
                        if motion:
                            self._check_manual_motion(body)
                        else:
                            if self.recipe_active:
                                raise ValueError('The recipe is checking AGV status. Follow Current operation.')
                            if body.get('connection_id') is not None:
                                self._check_connection(body['connection_id'])
                            if background and self._urgent_pending(monotonic()):
                                return self._snapshot()
                        if not self._send_slot():
                            return None
                        sent_at = self._send(name, body.get('value'), background=background)
                    else:
                        self._check_connection(body.get('connection_id'))
                        if action == 'disconnect':
                            if self.recipe_active:
                                raise ValueError('Stop the active recipe before disconnecting the AGV.')
                            self._receive()
                            if self._urgent_pending(monotonic()):
                                raise ValueError('AGV Stop or Pause is awaiting delivery. Wait before disconnecting.')
                            self._close()
                        elif action == 'arm' and type(body.get('armed')) is bool:
                            self.armed = body['armed']
                            if not self.armed:
                                self.stop_generation += 1
                                self._cancel_resend(keep_urgent=True)
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

    @contextmanager
    def recipe_link(self, connection_id=None, *, stop_generation):
        # Wait for a foreground read. Recipe feedback drains an in-flight background read.
        with self.send_lock, self.lock:
            if self.transport is None or self.recipe_active:
                raise ValueError('Connect the AGV on the AGV page before starting the recipe.')
            if connection_id is not None:
                self._check_connection(connection_id)
            self._check_stop_generation(stop_generation)
            self.recipe_active = True
            self.recipe_generation = stop_generation
            self.recipe_halt_sent = False
            self._cancel_resend(keep_urgent=True)
            self.armed = False
            self.recipe_transport = self.transport
            self.recipe_statuses.clear()
            self.recipe_next_poll = self.recipe_ack_window = 0
            self.recipe_call_at = None
            self.recipe_call_waiting = False
        try:
            yield self.settings['mode']
        finally:
            with self.lock:
                if self.resend is not None and self.resend.recipe_check is not None:
                    self._cancel_resend()
                self.recipe_active = False
                self.recipe_statuses.clear()

    def _recipe_check_link(self):
        if self.transport is not self.recipe_transport:
            raise ValueError('AGV link lost. Check the radio and reconnect on the AGV page.')
        if self.stop_generation != self.recipe_generation:
            raise ValueError('AGV Stop or Pause cancelled the recipe. Check the line and start a new reviewed run.')

    def recipe_feedback(self):
        with self.lock:
            self._recipe_check_link()
            try:
                self._receive()
                now = monotonic()
                ack = self.results.get('call_station', {}).get('recipe_ack_elapsed_s')
                retry = self.resend
                if (self._due(retry) and retry.recipe_check is not None
                        and (self._send_slot(retry=True) or retry.tries == retry.limit)):
                    if self.reading is not None:
                        self._finish_read('not checked')
                    if self._retry(retry):
                        self.recipe_ack_window = now + REPLY_TIMEOUT
                # The matched ACK and status can take about a second on the
                # radio. Leave room for them on its half-duplex link.
                if (self.reading is None and not self.recipe_call_waiting and now >= self.recipe_next_poll
                        and not self._fresh_timed_reports(now)
                        and not self._urgent_pending(now)
                        and (ack is not None or now >= self.recipe_ack_window) and self._send_slot()):
                    sent = self._send('query_status', None, background=True)
                    self.recipe_next_poll = sent + RECIPE_POLL_S
                    self._receive()
            except OSError as exc:
                self._transport_error(exc)
                raise ValueError(self.error) from exc
            updates = tuple(self.recipe_statuses)
            self.recipe_statuses.clear()
            return (self.status, self.status_at, updates,
                    self.results.get('call_station', {}).get('recipe_ack_elapsed_s'))

    def recipe_call(self, station, validate, *, retry_check=None):
        with self.lock:
            self._recipe_check_link()
            try:
                self._receive()
            except ReceiveError as exc:
                self._transport_error(exc)
                raise ValueError(self.error) from exc
            if self.reading is not None or self._urgent_pending(monotonic()):
                self.recipe_call_waiting = True
                return None
            self.recipe_call_waiting = True
            transport = self.transport
        if not self._wait_send_slot(transport, monotonic() + .125):
            return None
        with self.lock:
            self._recipe_check_link()
            if not self._send_slot():
                return None
            self.recipe_call_waiting = False

            def prewrite():
                validate(self.status, self.status_at, self.recipe_statuses)
                if self.resend is not None:
                    raise ValueError('An earlier AGV command is awaiting acknowledgement. Check the AGV and try again.')
            # Leave the transport open on a failed write so the urgent abort can
            # attempt Stop even if some or all of the call reached the AGV.
            sent = self._send('call_station', station, prewrite=prewrite, recipe_check=retry_check or validate)
            self.recipe_call_at = sent
            self.recipe_ack_window = sent + REPLY_TIMEOUT
            return sent

    def recipe_stop(self):
        return self._send_urgent('stop', recipe=True)
