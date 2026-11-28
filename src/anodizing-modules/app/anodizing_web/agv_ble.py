"""Katana-only XBOT AGV2 maintenance over Bluetooth."""
import asyncio
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
from threading import RLock, Thread, Timer
from time import monotonic
from uuid import uuid4

from .jobs import atomic_json

NUS = '6e40000{}-b5a3-f393-e0a9-e50e24dcca9e'
BLOCKS = {0x05: 0x28, 0x51: 0x34, 0x6B: 0x34, 0x86: 0x34, 0xA7: 0x34}
FIELD_BLOCKS = {f'{index:02X}': start for start, size in BLOCKS.items()
                for index in range(start, start + size // 2)}
LEGACY_FIELDS = {field for field, start in FIELD_BLOCKS.items() if start >= 0x6B}
WRITABLE = {
    '5E': (0, 1), '73': (0, 1), '77': (0, 1), '86': (0, 1), '8A': (0, 100),
    '8C': (0, 1), '8D': (0, 2), '90': (0, 1), '9E': (0, 1), 'A7': (0, 1),
}
FIRST = ('55', '56', '6F', '72', '86', '8C', '90', '9E', '6E', 'A7', 'B4', 'BB', '18')
FIELDS = json.loads(Path(__file__).with_name('agv-ble-fields.json').read_text())
READ_ONLY = {
    '6C': 'Changing the laser distance could weaken obstacle detection.',
    '6D': 'The effect of muting all sound on the obstacle warning is unverified.',
    '6E': 'Auto-start can move the AGV after a stop.',
    '74': 'The direction change is unverified on this controller profile.',
    '79': 'The unlock timer range and steps are unverified on this unit.',
    '87': 'The Bluetooth stop is unverified.',
    '88': 'This is a sensor status word. Keep obstacle sensing, bumpers and the e-stop enabled.',
    '8B': 'Shortening this duration could weaken the audible obstacle warning.',
    '91': 'Sensor limits and calibration are unverified.',
    '94': 'Changing chemistry could give an incorrect battery reserve reading.',
    '95': 'The voltage calibration scale is unverified.',
    '96': 'The route type choices are unverified on this unit.',
    '9F': 'Changing the vehicle number can break the configured wired link.',
}
SPEED_FIELDS = {'7D', '89', '8E', '8F'}
ROUTE_COUNTS = ('55', '56')
ROUTE_INDEX_LIMIT = 256
ROUTE_DIRECTIONS = (('outbound', 0x21), ('return', 0x23))
LEASE_SECONDS = 15
CONTROL_BLOCK = 'Disconnect the Bluetooth maintenance session before connecting or arming RS485.'
SESSION = re.compile(r'[A-Za-z0-9_-]{16,80}')


def frame(operation, field, data):
    plain = bytes((0x55, 0xAA, len(data) + 2, 0x12, operation, field)) + data
    return mask(plain + checksum(plain))


def mask(data):
    return bytes(value ^ 0x15 for value in data)


def checksum(data):
    return (~sum(data[2:]) & 0xffff).to_bytes(2, 'little')


class NoBluetoothReply(ValueError):
    pass


class BlueZ:
    client = None

    async def open(self, identity, on_bytes):
        try:
            from bleak import BleakClient, BleakScanner
        except ImportError as exc:
            raise ValueError('Bluetooth support is unavailable in the app environment.') from exc
        try:
            found = await BleakScanner.discover(timeout=5, return_adv=True)
        except Exception as exc:
            raise ValueError('Bluetooth is off or unavailable on Katana.') from exc
        matches = [
            device for device, advert in found.values()
            if (not identity.get('name') or identity['name'] in (advert.local_name, device.name))
            and (not identity.get('address') or identity['address'].casefold() == device.address.casefold())
        ]
        if len(matches) != 1:
            raise ValueError('AGV not found.' if not matches else 'More than one AGV matches the site profile. Set its address.')
        self.client = BleakClient(matches[0], timeout=8)
        try:
            await asyncio.wait_for(self.client.connect(), 9)
            writer = self.client.services.get_characteristic(NUS.format(2))
            reader = self.client.services.get_characteristic(NUS.format(3))
            if not writer or not reader or writer.service_uuid != NUS.format(1) or reader.service_uuid != NUS.format(1):
                raise ValueError('AGV Bluetooth settings service is unavailable.')
            if 'write-without-response' not in writer.properties or 'notify' not in reader.properties:
                raise ValueError('AGV Bluetooth characteristics do not support settings transport.')
            await asyncio.wait_for(self.client.start_notify(reader, lambda _, raw: on_bytes(bytes(raw))), 4)
        except Exception as exc:
            raise ValueError('Could not connect to the AGV Bluetooth settings service.') from exc
        return matches[0].address.casefold()

    @property
    def connected(self):
        return self.client is not None and self.client.is_connected

    async def send(self, wire):
        try:
            await asyncio.wait_for(self.client.write_gatt_char(NUS.format(2), wire, response=False), 4)
        except Exception as exc:
            raise ValueError('Bluetooth transmission failed. Check the AGV connection.') from exc

    async def close(self):
        if self.connected:
            await asyncio.wait_for(self.client.disconnect(), 5)


class Bluetooth:
    def __init__(self, identity, directory, control_state, transport_factory=None):
        self.identity = {key: identity[key].strip() for key in ('name', 'address')
                         if isinstance(identity.get(key), str) and identity[key].strip()}
        self.directory = Path(directory)
        self.control_state = control_state
        self.transport_factory = transport_factory or BlueZ
        self.gate = RLock()
        self.loop = self.worker = self.timer = None
        self.session = self.transport = self.fingerprint = None
        self.retired = {}
        self.connected = self.busy = False
        self.expires = 0
        self.values = {}
        self.undo = None
        self.clear_review = self.clear_result = None
        self.route_records = self.route_error = None
        self.audit_failed = False
        self.incoming = bytearray()

    @property
    def reason(self):
        return CONTROL_BLOCK if self.session is not None else None

    @contextmanager
    def wired_control(self):
        if self.reason:
            raise ValueError(self.reason)
        with self.gate:
            if self.reason:
                raise ValueError(self.reason)
            yield

    def _field_status(self, field, values):
        name, meaning = FIELDS.get(field, ('Unidentified word', 'Meaning unresolved on this profile.'))
        raw = values.get(field)
        value = int.from_bytes(raw, 'little') if raw is not None else None
        row = dict(field=field, name=name, meaning=meaning, raw=raw.hex(' ') if raw is not None else None,
                   value=value, writable=field in WRITABLE)
        if field in WRITABLE:
            row.update(minimum=WRITABLE[field][0], maximum=WRITABLE[field][1],
                       readback=f'{FIELD_BLOCKS[field]:02X}', undoable=True)
        else:
            row['read_only_reason'] = self._read_only_reason(field, value)
        return row

    @staticmethod
    def _read_only_reason(field, value):
        if field in SPEED_FIELDS:
            current = f'The current setpoint is {value} m/h. ' if value is not None else ''
            return current + 'Speed changes require an agreed way to undo a reduction without a speed increase.'
        if field in ('18', '55', '56', '6B', '9A', 'B4', 'BB'):
            return 'The controller reports this value.'
        return READ_ONLY.get(field, 'The meaning, permitted values or restore behavior is unverified on this unit.')

    @staticmethod
    def _counts(values):
        if all(field in values for field in ROUTE_COUNTS):
            return [int.from_bytes(values[field], 'little') for field in ROUTE_COUNTS]
        return None

    def status(self, blocked=None):
        values = self.values
        return dict(connected=self.connected, busy=self.busy, configured=bool(self.identity),
                    blocked=blocked, active=self.session is not None,
                    fields=[self._field_status(field, values)
                            for field in (*FIRST, *(key for key in FIELD_BLOCKS if key not in FIRST))],
                    route=dict(counts=self._counts(values), records=self.route_records, error=self.route_error),
                    clear_result=self.clear_result, undo=self.undo)

    def _control_block(self):
        try:
            return self.control_state()
        except (OSError, ValueError):
            return 'Executor unavailable. Restore its link and verify it is disconnected and disarmed before writing.'

    def _run(self, coroutine):
        return asyncio.run_coroutine_threadsafe(coroutine, self.loop).result()

    def _save(self, name, value):
        try:
            atomic_json(self.directory / name, value, sync_directory=True)
        except OSError as exc:
            raise ValueError('Could not save the private Bluetooth settings snapshot or undo record.') from exc

    def _record(self, direction, wire):
        try:
            descriptor = os.open(self.directory / 'bytes.jsonl',
                                 os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW, 0o600)
            with os.fdopen(descriptor, 'a', encoding='utf-8') as output:
                output.write(json.dumps(dict(at=datetime.now(timezone.utc).isoformat(),
                                             direction=direction, hex=wire.hex(' '))) + '\n')
                output.flush()
                os.fsync(output.fileno())
        except OSError as exc:
            self.audit_failed = True
            raise ValueError('Could not save the private Bluetooth byte log.') from exc

    def _receive(self, wire):
        try:
            self._record('rx', wire)
        except ValueError:
            pass
        finally:
            self.incoming.extend(mask(wire))
            del self.incoming[:-4096]
            self.data_ready.set()

    async def _open(self):
        self.data_ready = asyncio.Event()
        target = await self.transport.open(self.identity, self._receive)
        return hashlib.sha256(json.dumps([self.identity, target], sort_keys=True).encode()).hexdigest()

    async def _send(self, wire, before_send=None):
        if self.audit_failed:
            raise ValueError('The private Bluetooth byte log is unavailable.')
        self._record('tx', wire)
        if before_send is not None:
            before_send()
        await self.transport.send(wire)

    async def _exchange(self, operation, index, request, size):
        prefix = bytes((0x55, 0xAA, size + 2, 0x21, operation, index))
        self.incoming.clear()
        self.data_ready.clear()
        await self._send(frame(operation, index, request))
        deadline = self.loop.time() + 3
        while True:
            if self.audit_failed:
                raise ValueError('The private Bluetooth byte log is unavailable.')
            cursor = self.incoming.find(prefix)
            while cursor >= 0 and len(self.incoming) - cursor >= size + 8:
                reply = self.incoming[cursor:cursor + size + 8]
                if reply[-2:] == checksum(reply[:-2]):
                    return bytes(reply[6:-2])
                cursor = self.incoming.find(prefix, cursor + 1)
            self.data_ready.clear()
            try:
                await asyncio.wait_for(self.data_ready.wait(), max(0, deadline - self.loop.time()))
            except TimeoutError as exc:
                raise NoBluetoothReply(f'No valid Bluetooth reply for {operation:02X} {index:02X}. Check the link and read again.') from exc

    async def _read(self, start, size=None):
        size = BLOCKS[start] if size is None else size
        try:
            payload = await self._exchange(1, start, bytes((size,)), size)
        except NoBluetoothReply:
            if start != 0xA7:
                raise
            payload = await self._exchange(1, start, bytes((size,)), size)
        return {f'{start + offset // 2:02X}': payload[offset:offset + 2]
                for offset in range(0, size, 2)}

    async def _read_all(self):
        values = {}
        try:
            for start in BLOCKS:
                values.update(await self._read(start))
        except ValueError:
            self.values = {}
            self.route_records = self.route_error = None
            raise
        if self._counts(values) != self._counts(self.values):
            self.route_records = self.route_error = None
        self.values = values

    async def _read_route_records(self, counts):
        records = {}
        for (direction, operation), count in zip(ROUTE_DIRECTIONS, counts):
            if count > ROUTE_INDEX_LIMIT:
                raise ValueError(f'The {direction} count is {count}. The Bluetooth row index only covers 256 records.')
            rows = []
            for index in range(0, count, 4):
                length = min(4, count - index)
                payload = await self._exchange(operation, index, bytes((length,)), length * 16)
                rows.extend(payload[pos:pos + 16].hex(' ') for pos in range(0, len(payload), 16))
            records[direction] = rows
        return records

    async def _read_page(self):
        await self._read_all()
        self.route_records = self.route_error = None
        try:
            self.route_records = await asyncio.wait_for(self._read_route_records(self._counts(self.values)), 20)
        except (TimeoutError, ValueError) as exc:
            self.route_error = str(exc) or 'Reading the route took too long. Check the link and read again.'

    async def _read_counts(self):
        self.values = {key: data for key, data in self.values.items() if key not in ROUTE_COUNTS}
        readback = await self._read(0x55, 4)
        self.values = dict(self.values, **readback)
        return self._counts(readback)

    def _require_live_session(self):
        if self.expires <= monotonic():
            raise ValueError('The Bluetooth page session expired. Reconnect before writing.')

    def _check_clear(self):
        if blocked := self._control_block():
            raise ValueError(blocked)
        profile = self._run(self._read(0x05))
        self.values = dict(self.values, **profile)
        if int.from_bytes(profile['18'], 'little', signed=True) < 0:
            raise ValueError(f"Route clear is unavailable for controller profile 18 = {profile['18'].hex(' ')}. "
                             'Use the vendor procedure for this controller.')
        previous = self._counts(self.values)
        counts = self._run(self._read_counts())
        if counts != previous:
            self.route_records = self.route_error = None
        if max(counts) > ROUTE_INDEX_LIMIT:
            raise ValueError(f'Outbound {counts[0]} and return {counts[1]} exceed the supported route count. '
                             'Read settings again and check the route before clearing.')
        return counts

    def _review_clear(self):
        self.clear_review = None
        counts = self._check_clear()
        if counts == [0, 0]:
            raise ValueError('Outbound 0 and return 0. The stored route is already empty.')
        self.clear_review = dict(token=uuid4().hex, before=counts)
        return self.clear_review

    def _clear(self, body):
        review, self.clear_review = self.clear_review, None
        if review is None or body.get('token') != review['token'] or body.get('confirmed') is not True:
            raise ValueError('Review the current route counts, then confirm the clear.')
        before = self._check_clear()
        if before != review['before']:
            raise ValueError(f"Counts changed from {review['before']} to {before} (outbound, return). Review again.")
        if blocked := self._control_block():
            raise ValueError(blocked)
        self.clear_result = None

        def before_send():
            self._require_live_session()
            self.values = {key: data for key, data in self.values.items() if key not in ROUTE_COUNTS}
            self.route_records = self.route_error = None
            self.clear_result = dict(before=before, after=None)

        try:
            self._run(self._send(frame(0x25, 0, b''), before_send))
            after = self._run(self._read_counts())
        except ValueError as exc:
            if self.clear_result is None:
                raise
            raise ValueError('Route clear is unconfirmed. Read the counts again before taking any further action.') from exc
        self.clear_result = dict(before=before, after=after)
        if after != [0, 0]:
            raise ValueError(f'Route clear is unconfirmed. Read-back was outbound {after[0]}, return {after[1]}. '
                             'Check the route before taking any further action.')
        self.route_records = {'outbound': [], 'return': []}

    def _renew(self):
        if self.timer is not None:
            self.timer.cancel()
        self.expires = monotonic() + LEASE_SECONDS
        self.timer = Timer(LEASE_SECONDS, self._expire, args=(self.session, self.expires))
        self.timer.daemon = True
        self.timer.start()

    def _expire(self, session, expires):
        with self.gate:
            if session != self.session or expires != self.expires:
                return
            try:
                self._disconnect()
            except ValueError:
                self._renew()

    def _disconnect(self):
        if self.transport is not None:
            try:
                self._run(self.transport.close())
            except Exception as exc:
                raise ValueError('Bluetooth disconnect is unconfirmed. The RS485 controls remain blocked.') from exc
        if self.timer is not None:
            self.timer.cancel()
        self.connected = False
        self.values = {}
        self.clear_review = self.clear_result = None
        self.route_records = self.route_error = None
        self.fingerprint = self.undo = self.session = self.transport = None

    def close(self):
        with self.gate:
            try:
                self._disconnect()
            finally:
                if self.timer is not None:
                    self.timer.cancel()
                self.session = None
                if self.loop is not None:
                    self.loop.call_soon_threadsafe(self.loop.stop)
                    self.worker.join()
                    self.loop.close()
                    self.loop = self.worker = None

    def _connect(self, session):
        if not self.identity:
            raise ValueError('Configure the AGV Bluetooth name or address in the private site profile.')
        if self.session is not None:
            raise ValueError('A Bluetooth maintenance session is already open.')
        try:
            self.directory.mkdir(mode=0o700, exist_ok=True)
            if self.directory.is_symlink() or self.directory.stat().st_mode & 0o077:
                raise ValueError('The Bluetooth audit directory must be private.')
        except OSError as exc:
            raise ValueError('Could not open the private Bluetooth audit directory.') from exc
        if self.loop is None:
            self.loop = asyncio.new_event_loop()
            self.worker = Thread(target=self.loop.run_forever, daemon=True, name='agv-bluetooth')
            self.worker.start()
        self.session, self.transport = session, self.transport_factory()
        self._renew()
        self.audit_failed = False
        try:
            self.fingerprint = self._run(self._open())
            self.connected = True
            self.undo = self._load_undo()
            self._renew()
        except Exception:
            self._disconnect()
            raise

    def _load_undo(self):
        path = self.directory / 'undo.json'
        if path.is_symlink():
            raise ValueError('The Bluetooth undo record is unreadable.')
        try:
            record = json.loads(path.read_text())
        except FileNotFoundError:
            return None
        except (OSError, ValueError) as exc:
            raise ValueError('The Bluetooth undo record is unreadable.') from exc
        if record is None:
            return None
        if not isinstance(record, dict):
            raise ValueError('The Bluetooth undo record is unreadable.')
        if record.get('device') != self.fingerprint:
            return None
        try:
            ident = record['snapshot']
            if not isinstance(ident, str) or not re.fullmatch(r'[0-9a-f]{32}', ident):
                raise ValueError()
            snapshot = self.directory / f'snapshot-{ident}.json'
            if snapshot.is_symlink():
                raise ValueError()
            saved = json.loads(snapshot.read_text())
            field, old, value = saved['field'], saved['old'], saved['value']
            if (saved.get('device') != self.fingerprint or field not in WRITABLE
                    or not self._valid_change(field, old, value)
                    or set(saved['fields']) not in (LEGACY_FIELDS, FIELD_BLOCKS.keys())
                    or saved['fields'][field] != old.to_bytes(2, 'little').hex(' ')):
                raise ValueError()
        except (OSError, ValueError, TypeError, KeyError) as exc:
            raise ValueError('The Bluetooth undo snapshot is unreadable.') from exc
        return dict(field=field, old=value, value=old)

    @staticmethod
    def _valid_change(field, old, value):
        minimum, maximum = WRITABLE[field]
        return (type(old) is int and type(value) is int and old != value
                and minimum <= old <= maximum and minimum <= value <= maximum)

    def _write(self, body):
        field, old, value = (body.get(key) for key in ('field', 'old', 'value'))
        undoing = body['action'] == 'undo'
        if body.get('confirmed') is not True:
            raise ValueError('Confirm the old and new setting values before writing.')
        if not isinstance(field, str) or field not in WRITABLE:
            if isinstance(field, str) and field in FIELD_BLOCKS:
                raise ValueError(f'Field {field} is read-only. {self._read_only_reason(field, None)} Choose an editable setting.')
            raise ValueError('Choose an editable Bluetooth setting.')
        if not self._valid_change(field, old, value):
            minimum, maximum = WRITABLE[field]
            raise ValueError(f'Field {field} received {old!r} → {value!r}. Choose distinct whole numbers from '
                             f'{minimum} to {maximum} and review again.')
        if undoing and self.undo != dict(field=field, old=old, value=value):
            raise ValueError('Review the latest Bluetooth write before undoing.')
        if blocked := self._control_block():
            raise ValueError(blocked)
        self._run(self._read_all())
        current = int.from_bytes(self.values[field], 'little')
        if current != old:
            raise ValueError(f'Field {field} changed from {old} to {current}. Read settings and review again.')
        if value == 1 and field == '90' and self.values['8C'] != b'\x01\x00':
            raise ValueError('Enable multi-station mode before enabling its remote.')
        if value == 0 and field == '8C' and self.values['90'] != b'\x00\x00':
            raise ValueError('Disable the multi-station remote before disabling multi-station mode.')
        if field == '73' and self.values['86'] != b'\x00\x00':
            raise ValueError('Front/rear key swap requires double-button mode (86 = 0). Check button mode and review again.')
        if field == '8D' and self.values['6D'] == b'\x00\x00':
            raise ValueError('Master sound is off (6D = 0). The driving sound pattern cannot be changed in this mode.')
        if field in ('86', '9E') and int.from_bytes(self.values['18'], 'little', signed=True) < 0:
            raise ValueError(f"Field {field} is unavailable for controller profile 18 = {self.values['18'].hex(' ')}.")
        if field == '9E' and self.values['8C'] != b'\x00\x00':
            raise ValueError('Track memory requires standard mode (8C = 0). Check multi-station mode and review again.')
        self._require_live_session()
        self.clear_review = None
        snapshot_id = uuid4().hex
        self._save(f'snapshot-{snapshot_id}.json', dict(device=self.fingerprint, field=field, old=old, value=value,
                   fields={key: data.hex(' ') for key, data in self.values.items()}))
        if blocked := self._control_block():
            raise ValueError(blocked)
        def before_send():
            self._require_live_session()
            if not undoing:
                self._save('undo.json', dict(device=self.fingerprint, snapshot=snapshot_id))
                self.undo = dict(field=field, old=value, value=old)
            self.values = {key: data for key, data in self.values.items() if key != field}

        index = int(field, 16)
        payload = value.to_bytes(2, 'little')
        self._run(self._send(frame(3, index, payload), before_send))
        start = FIELD_BLOCKS[field]
        try:
            verified = self._run(self._read(start))
        except ValueError as exc:
            raise ValueError('Write not confirmed by read-back. Read settings again before retrying.') from exc
        self.values = dict(self.values, **verified)
        if verified[field] != payload:
            actual = int.from_bytes(verified[field], 'little')
            raise ValueError(f'Field {field} read-back was {actual}, expected {value}. Read settings and review again.')
        if undoing:
            self._save('undo.json', None)
            self.undo = None

    def submit(self, body):
        action, session = body.get('action'), body.get('session')
        if not isinstance(session, str) or not SESSION.fullmatch(session):
            raise ValueError('Open a Bluetooth maintenance page session first.')
        with self.gate:
            now = monotonic()
            if action in ('connect', 'disconnect'):
                self.retired = {key: expiry for key, expiry in self.retired.items() if expiry > now}
            if action == 'disconnect':
                if len(self.retired) < 128:
                    self.retired[session] = now + 60
                if session != self.session:
                    return self.status()
            if action == 'connect' and session in self.retired:
                raise ValueError('This Bluetooth page session already closed. Reload and connect again.')
            if action != 'connect' and session != self.session:
                raise ValueError('The Bluetooth page session expired. Connect again.')
            if action not in ('connect', 'disconnect') and not getattr(self.transport, 'connected', True):
                self._disconnect()
                raise ValueError('Bluetooth connection was lost. Connect again.')
            self.busy = action != 'heartbeat'
            extra = {}
            try:
                if action == 'connect':
                    self._connect(session)
                elif action == 'disconnect':
                    self._disconnect()
                else:
                    self._renew()
                    if action == 'read':
                        self._run(self._read_page())
                    elif action == 'review-clear':
                        extra['clear_review'] = self._review_clear()
                    elif action == 'clear':
                        self._clear(body)
                    elif action in ('write', 'undo'):
                        self._write(body)
                    elif action != 'heartbeat':
                        raise ValueError('Choose a Bluetooth settings action.')
            finally:
                self.busy = False
                if action not in ('connect', 'disconnect') and self.session is not None:
                    self._renew()
            return dict(self.status(), **extra)
