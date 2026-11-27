"""Katana-only XBOT AGV2 settings over Bluetooth."""
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
BLOCKS = {0x6B: 0x34, 0x86: 0x34, 0xA7: 0x34}
WRITABLE = {'8C', '90', 'A7'}
FIRST = ('6F', '72', '86', '8C', '90', '9E', '6E', 'A7')
FIELDS = json.loads(Path(__file__).with_name('agv-ble-fields.json').read_text())
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

    def status(self, blocked=None):
        values = self.values
        return dict(connected=self.connected, busy=self.busy, configured=bool(self.identity),
                    blocked=blocked, active=self.session is not None,
                    fields=[dict(field=field, name=FIELDS[field][0], meaning=FIELDS[field][1],
                                 raw=values[field].hex(' ') if field in values else None,
                                 value=int.from_bytes(values[field], 'little') if field in values else None,
                                 writable=field in WRITABLE)
                            for field in (*FIRST, *(key for key in FIELDS if key not in FIRST))],
                    undo=self.undo)

    def _control_block(self):
        try:
            return self.control_state()
        except (OSError, ValueError):
            return 'Could not verify that the executor is disconnected and disarmed.'

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

    async def _read(self, field):
        size = BLOCKS[field]
        prefix = bytes((0x55, 0xAA, size + 2, 0x21, 1, field))
        self.incoming.clear()
        self.data_ready.clear()
        await self._send(frame(1, field, bytes((size,))))
        deadline = self.loop.time() + 3
        while True:
            if self.audit_failed:
                raise ValueError('The private Bluetooth byte log is unavailable.')
            cursor = self.incoming.find(prefix)
            while cursor >= 0 and len(self.incoming) - cursor >= size + 8:
                reply = self.incoming[cursor:cursor + size + 8]
                if reply[-2:] == checksum(reply[:-2]):
                    return {f'{field + pos // 2:02X}': bytes(reply[6 + pos:8 + pos])
                            for pos in range(0, size, 2)}
                cursor = self.incoming.find(prefix, cursor + 1)
            self.data_ready.clear()
            try:
                await asyncio.wait_for(self.data_ready.wait(), max(0, deadline - self.loop.time()))
            except TimeoutError as exc:
                raise ValueError(f'No valid Bluetooth settings reply for block {field:02X}.') from exc

    async def _read_all(self):
        values = {}
        for start in BLOCKS:
            values.update(await self._read(start))
        self.values = values

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
                    or type(old) is not int or type(value) is not int or old not in (0, 1) or value not in (0, 1)
                    or set(saved['fields']) != set(FIELDS)
                    or saved['fields'][field] != old.to_bytes(2, 'little').hex(' ')):
                raise ValueError()
        except (OSError, ValueError, TypeError, KeyError) as exc:
            raise ValueError('The Bluetooth undo snapshot is unreadable.') from exc
        return dict(field=field, old=value, value=old)

    def _write(self, body):
        field, old, value = (body.get(key) for key in ('field', 'old', 'value'))
        undoing = body['action'] == 'undo'
        if body.get('confirmed') is not True:
            raise ValueError('Confirm the old and new setting values before writing.')
        if (not isinstance(field, str) or field not in WRITABLE or type(old) is not int or type(value) is not int
                or old not in (0, 1) or value not in (0, 1) or old == value):
            raise ValueError('Choose a supported setting and distinct values of 0 or 1.')
        if undoing and self.undo != dict(field=field, old=old, value=value):
            raise ValueError('Review the latest Bluetooth write before undoing.')
        if blocked := self._control_block():
            raise ValueError(blocked)
        self._run(self._read_all())
        if self.values[field] != old.to_bytes(2, 'little'):
            raise ValueError('The previous value changed. Refresh settings and confirm again.')
        if value == 1 and field == '90' and self.values['8C'] != b'\x01\x00':
            raise ValueError('Enable multi-station mode before enabling its remote.')
        if value == 0 and field == '8C' and self.values['90'] != b'\x00\x00':
            raise ValueError('Disable the multi-station remote before disabling multi-station mode.')
        if self.expires <= monotonic():
            raise ValueError('The Bluetooth page session expired. Reconnect before writing.')
        snapshot_id = uuid4().hex
        self._save(f'snapshot-{snapshot_id}.json', dict(device=self.fingerprint, field=field, old=old, value=value,
                   fields={key: data.hex(' ') for key, data in self.values.items()}))
        if blocked := self._control_block():
            raise ValueError(blocked)
        def before_send():
            if self.expires <= monotonic():
                raise ValueError('The Bluetooth page session expired. Reconnect before writing.')
            if not undoing:
                self._save('undo.json', dict(device=self.fingerprint, snapshot=snapshot_id))
                self.undo = dict(field=field, old=value, value=old)
            self.values = {key: data for key, data in self.values.items() if key != field}

        index = int(field, 16)
        payload = value.to_bytes(2, 'little')
        self._run(self._send(frame(3, index, payload), before_send))
        start = next(start for start, size in BLOCKS.items() if start <= index < start + size // 2)
        try:
            verified = self._run(self._read(start))
        except ValueError as exc:
            raise ValueError('Write not confirmed by read-back. Read settings again before retrying.') from exc
        self.values = dict(self.values, **verified)
        if verified[field] != payload:
            raise ValueError('Write not confirmed by read-back. Review the current setting.')
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
            try:
                if action == 'connect':
                    self._connect(session)
                elif action == 'disconnect':
                    self._disconnect()
                else:
                    self._renew()
                    if action == 'read':
                        self._run(self._read_all())
                    elif action in ('write', 'undo'):
                        self._write(body)
                    elif action != 'heartbeat':
                        raise ValueError('Choose a Bluetooth settings action.')
            finally:
                self.busy = False
            return self.status()
