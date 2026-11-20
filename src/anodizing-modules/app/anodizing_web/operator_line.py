"""One read-only producer for the line views, Scene and Recorder."""
from collections import Counter, defaultdict, deque
from datetime import datetime, timedelta, timezone
from http.client import HTTPConnection, HTTPException, HTTPSConnection
import json
from math import isfinite
import os
import re
import stat
from threading import Event, Lock, Thread
from time import monotonic
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .operator_scene import TANK_ID, variable
from .paths import inside
from .site_profile import section

MAX_BYTES = 2_000_000
PERIOD_S = 1
LIMIT_MS = 3000
OPERATIONS = {'list': '/api/v1/tags/list', 'read': '/api/v1/tags/read'}
IDENT = re.compile(r'[A-Za-z][A-Za-z0-9_]{0,63}')
GROUP = re.compile(r'(Tank[0-9]{2}[A-Z]?)_')
ALARM = re.compile(r'Alarm[0-9]+')
POLL_TIME = re.compile(r'[0-9]{4}-[0-9]{2}-[0-9]{2}[T ][0-9]{2}:[0-9]{2}:[0-9]{2}'
                       r'(?:\.[0-9]{1,6})?(?:Z|[+-][0-9]{2}:[0-9]{2})?')
TYPES = {'Bool', 'Word', 'SignedWord', 'Float', None}
RESERVED = {'id', 'timestamp', 'poll_count', 'tags_ok', 'tags_failed', 'api_reachable', 'poll_age'}
SYSTEM = ('Remote', 'Local', 'MainInletStatus')
UNITS = {'TempPV': '°C', 'TempSV': '°C', 'TempAdj': '°C', 'AmperePV': 'A',
         'AmpereSV': 'A', 'VoltagePV': 'V', 'VoltageSV': 'V'}


def utc_now():
    return datetime.now(timezone.utc)


def number(value):
    return type(value) is int and abs(value) <= 2**53 - 1 or type(value) is float and isfinite(value)


def count(value):
    return type(value) is int and 0 <= value <= 2**53 - 1


def contains_secret(value, key):
    if isinstance(value, str):
        return key in value
    if isinstance(value, list):
        return any(contains_secret(item, key) for item in value)
    if isinstance(value, dict):
        return any(key in name or contains_secret(item, key) for name, item in value.items())
    return False


class ReadClient:
    def __init__(self, origin, key):
        parsed = urlsplit(origin)
        if (parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username is not None
                or parsed.path not in ('', '/') or parsed.query or parsed.fragment or not key
                or not key.isascii() or not key.isprintable()):
            raise ValueError('Invalid line origin or service credential.')
        self.connect = HTTPSConnection if parsed.scheme == 'https' else HTTPConnection
        self.host, self.port, self.key = parsed.hostname, parsed.port, key

    def request(self, operation):
        path = OPERATIONS[operation]
        connection = self.connect(self.host, self.port, timeout=2)
        status = None
        try:
            deadline = monotonic() + 2
            connection.request('GET', path, headers={'X-API-Key': self.key})
            response = connection.getresponse()
            status = response.status
            if status != 200:
                return status, None
            data = bytearray()
            while len(data) <= MAX_BYTES and monotonic() < deadline:
                chunk = response.read1(65536)
                if not chunk:
                    # Reject a credential echoed in a response, including JSON escapes.
                    value = json.loads(data.decode('utf-8'))
                    if not contains_secret(value, self.key):
                        return status, value
                    break
                data.extend(chunk)
        except (OSError, HTTPException, ValueError, RecursionError):
            pass
        finally:
            connection.close()
        return status, None


class Simulator:
    def __init__(self, fixture, wall=utc_now):
        self.fixture, self.wall = fixture, wall
        self.overrides = {}
        self.pending_feedback = None
        self.feedback_delay = fixture.get('feedback_delay_rows', 0)
        if type(self.feedback_delay) is not int or not 0 <= self.feedback_delay <= 15:
            raise ValueError('Invalid simulator feedback delay.')
        self.tags = fixture['tags']
        self.rows = fixture['rows']
        parse_catalog({'tags': self.tags})
        if not isinstance(self.rows, list) or not self.rows or any(not isinstance(row, dict) for row in self.rows):
            raise ValueError('Invalid line simulator rows.')
        self.sequence = 0

    def request(self, operation):
        if operation == 'list':
            return 200, {'tags': self.tags}
        values = self.rows[self.sequence % len(self.rows)]
        self.sequence += 1
        if self.pending_feedback is not None:
            due, name, value = self.pending_feedback
            if self.sequence >= due:
                self.overrides[name] = value
                self.pending_feedback = None
        return 200, {**values, **self.overrides, 'id': self.sequence, 'timestamp': self.wall().isoformat()}

    def write(self, case, value):
        # In-process dispatch finishes before it returns. There is no transport or delayed write.
        name, feedback = case['command'], case['feedback']
        self.overrides[name] = value
        self.pending_feedback = None
        if value:
            self.pending_feedback = (self.sequence + 1 + self.feedback_delay, feedback, case['expected'])
        elif 'clear' in case:
            self.overrides[feedback] = case['clear']
        else:
            self.overrides.pop(feedback, None)
        address = next(tag['device_address'] for tag in self.tags if tag['tag_name'] == name)
        return dict(written=True, tag_name=name, device_address=address, value=value), True


def local_json(root, path):
    fd = os.open(inside(root, path), os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
    with os.fdopen(fd, 'rb') as stream:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise ValueError('Select a regular line data file.')
        data = stream.read(MAX_BYTES + 1)
    if len(data) > MAX_BYTES:
        raise ValueError('Line data file is too large.')
    return json.loads(data)


def parse_catalog(payload):
    tags = payload.get('tags') if isinstance(payload, dict) else None
    if not isinstance(tags, list) or not 0 < len(tags) <= 1000:
        raise ValueError('Invalid catalog.')
    result = {}
    for item in tags:
        if not isinstance(item, dict):
            raise ValueError('Invalid catalog tag.')
        name, address, kind, access, description = (
            item.get(key) for key in ('tag_name', 'device_address', 'data_type', 'read_write', 'description'))
        multiplier = item.get('multiplier', 1)
        minimum, maximum = item.get('low_limit'), item.get('high_limit')
        suffix = name.partition('_')[2] if isinstance(name, str) else ''
        unit = item.get('unit', UNITS.get(suffix))
        if (not isinstance(name, str) or not IDENT.fullmatch(name) or name in result or name in RESERVED
                or not isinstance(address, str) or not re.fullmatch(r'[A-Za-z0-9_.:-]{1,40}', address)
                or kind is not None and (not isinstance(kind, str) or kind not in TYPES)
                or access not in ('R', 'W') or not isinstance(description, str) or len(description) > 500
                or not number(multiplier) or multiplier <= 0 or any(v is not None and not number(v) for v in (minimum, maximum))
                or (minimum is not None and maximum is not None and minimum > maximum)
                or unit is not None and (not isinstance(unit, str) or len(unit) > 20)):
            raise ValueError('Invalid catalog tag.')
        match = GROUP.match(name)
        group = match[1] if match else 'Alarms' if ALARM.fullmatch(name) else 'System'
        result[name] = dict(tag_name=name, device_address=address, data_type=kind, read_write=access,
                            description=description, multiplier=multiplier, minimum=minimum,
                            maximum=maximum, unit=unit, group=group)
    return result


def physical_tanks(tanks):
    if not isinstance(tanks, dict):
        raise ValueError('Invalid tank mapping.')
    for tank, signals in tanks.items():
        if (not TANK_ID.fullmatch(tank) or not isinstance(signals, dict)
                or any(not IDENT.fullmatch(role) or not isinstance(name, str) or not IDENT.fullmatch(name)
                       for role, name in signals.items())):
            raise ValueError('Invalid tank mapping.')
    return tanks


def catalog_view(tags, expected, tanks, revision):
    warnings = []

    def warn(code, message):
        warnings.append(dict(code=code, message=message))

    addresses = defaultdict(list)
    for tag in tags.values():
        addresses[tag['device_address'].upper()].append(tag['tag_name'])
    unsafe = set()
    for address, names in sorted(addresses.items()):
        if len(names) > 1:
            unsafe.update(names)
            warn('shared_address', f'{", ".join(sorted(names))} share address {address}. Confirm the mapping with the supplier.')
    if expected is not None:
        identity = ('device_address', 'data_type', 'read_write', 'multiplier')
        for name in sorted(tags.keys() | expected.keys()):
            live, reference = tags.get(name), expected.get(name)
            if live is None or reference is None:
                warn('catalog_drift', f'{name} was {"added to" if live else "removed from"} the supplier catalog. Review the local baseline.')
            else:
                changed = [key for key in live if live[key] != reference[key]]
                if changed:
                    warn('catalog_drift', f'{name} differs from the local baseline in {", ".join(changed)}. Review before relying on it.')
                    if set(changed).intersection(identity):
                        unsafe.add(name)
    bindings = Counter(name for signals in tanks.values() for name in signals.values())
    mapped = {}
    for tank, signals in tanks.items():
        mapped[tank] = {}
        for role, name in signals.items():
            tag = tags.get(name)
            if bindings[name] > 1:
                warn('duplicate_binding', f'{name} is assigned to multiple fields including {tank} {role.replace("_", " ")}. Review the physical tank mapping.')
            elif tag is None or tag['read_write'] != 'R' or name in unsafe or (
                    role == 'basket' and tag['data_type'] != 'Bool'):
                warn('invalid_binding', f'{name} is unavailable for {tank} {role.replace("_", " ")}. Review the physical tank mapping.')
            else:
                mapped[tank][role] = name
                if tag['group'] != tank:
                    warn('alias', f'{tank} {role.replace("_", " ")} uses reported tag {name}.')
    return dict(revision=revision, tags=[tags[name] for name in sorted(tags)], warnings=warnings, tanks=mapped)


class Line:
    def __init__(self, mode='disabled', client=None, *, clock=monotonic, wall=utc_now,
                 clock_zone=None, clock_offset_s=None, expected=None, tanks=None, setup_error=None):
        self.mode, self.client = mode, client
        self.clock, self.wall = clock, wall
        self.zone = ZoneInfo(clock_zone) if clock_zone else None
        self.offset = timedelta(seconds=clock_offset_s) if clock_offset_s is not None else None
        self.expected, self.tanks = expected, tanks or {}
        self.source = {'disabled': 'line.disabled', 'live': 'line.supplier', 'simulation': 'line.simulator'}[mode]
        self.lock = Lock()
        self.stop = Event()
        self.worker = None
        self.tests = None
        self.tags = {}
        self.view = dict(revision=0, tags=[], warnings=[], tanks={})
        self.diagnostics = deque(maxlen=100)
        self.sample = None
        self.identity = None
        self.progress = 0
        self.reason = setup_error or ('Line reads are disabled. Check the service setup.' if mode == 'disabled'
                                      else 'Waiting for line reads.')
        self.contact = (None, None, None)
        self.refresh = dict(id=0, outcome=None)

    @classmethod
    def configured(cls, root, mode):
        if mode == 'disabled':
            return cls()
        profile = section(root, 'line')
        try:
            tanks = physical_tanks(profile.get('tanks', {}))
            baseline = profile.get('baseline')
            expected = parse_catalog(local_json(root, baseline)) if baseline else None
            if mode == 'simulation':
                return cls(mode, Simulator(local_json(root, profile['simulation_fixture'])),
                           clock_zone='UTC', clock_offset_s=0, expected=expected, tanks=tanks)
            origin = profile['origin']
            offset, zone = profile.get('clock_offset_s'), profile.get('clock_zone')
            if offset is not None and not number(offset):
                raise ValueError('Invalid clock correction.')
            return cls(mode, ReadClient(origin, os.environ.get('PLC_API_KEY')), clock_zone=zone,
                       clock_offset_s=offset, expected=expected, tanks=tanks)
        except (OSError, KeyError, TypeError, ValueError, AttributeError, OverflowError, RecursionError, ZoneInfoNotFoundError):
            return cls(mode, setup_error='Line setup unavailable. Check the local configuration and restart the service.')

    def start(self):
        if self.client:
            self.worker = Thread(target=self.run, name='operator-line', daemon=True)
            self.worker.start()

    def close(self):
        if self.tests:
            self.tests.closing = True
            self.tests.wake.set()
        else:
            self.stop.set()
        if self.worker:
            self.worker.join(timeout=11 if self.tests and self.tests.enabled else 2.5)
        self.stop.set()

    def run(self):
        next_poll = self.clock()
        tests = self.tests
        while not self.stop.is_set():
            now = self.clock()
            if now >= next_poll:
                self.step()
                next_poll = now + PERIOD_S
            if tests:
                # A stop wakes the same worker between polls and cannot race a dispatch.
                tests.wake.clear()
                tests.tick()
                if tests.closing and not tests.active:
                    return
            (tests.wake if tests else self.stop).wait(max(0, next_poll - self.clock()))

    def queue_refresh(self):
        if not self.client:
            raise ValueError(self.reason)
        with self.lock:
            if self.refresh['outcome'] != 'pending':
                self.refresh = dict(id=self.refresh['id'] + 1, outcome='pending')
            return self.refresh['id']

    def catalog(self):
        with self.lock:
            return self.view

    def log(self):
        with self.lock:
            return list(self.diagnostics)

    def log_read(self, operation, stamp, status, **details):
        self.diagnostics.append(dict(operation=operation, at=stamp.isoformat(), http_status=status, **details))

    def refresh_catalog(self):
        with self.lock:
            requested = self.refresh if self.refresh['outcome'] == 'pending' else None
        status, payload = self.client.request('list')
        stamp = self.wall()
        try:
            tags = parse_catalog(payload) if status == 200 else None
        except ValueError:
            tags = None
        view = catalog_view(tags, self.expected, self.tanks, self.view['revision'] + 1) if tags else None
        with self.lock:
            self.log_read('Catalog', stamp, status, outcome='Replied' if tags else 'Catalog unavailable')
            if view is not None and tags != self.tags:
                # A row acquired under a different catalog cannot vouch for the new inventory.
                self.tags, self.view, self.sample, self.progress = tags, view, None, 0
                self.reason = 'Catalog changed. Waiting for two progressing PLC rows.'
            if requested:
                self.refresh = dict(id=requested['id'], outcome='done' if tags else 'failed')
        return tags is not None

    def step(self):
        with self.lock:
            needs_catalog = not self.tags or self.refresh['outcome'] == 'pending'
        if needs_catalog:
            self.refresh_catalog()
        if self.stop.is_set():
            return
        status, payload = self.client.request('read')
        now, stamp = self.clock(), self.wall()
        row = payload if status == 200 and isinstance(payload, dict) else {}
        row_id, source_at, ok, failed = (row.get(key) for key in ('id', 'timestamp', 'tags_ok', 'tags_failed'))
        # Parse outside the cache lock. Only this worker publishes samples.
        values, sample_time, error = {}, None, None
        if status != 200:
            error = 'Line API unavailable. Check the service connection and credentials.'
        elif not self.tags:
            error = 'Catalog unavailable. Refresh the catalog and check the service.'
        elif not count(row_id):
            error = 'PLC row invalid. Check the supplier response.'
        else:
            try:
                sample_time = self.poll_time(source_at)
            except (TypeError, OverflowError, ValueError) as exc:
                error = str(exc) if isinstance(exc, PollTimeError) else 'PLC time invalid. Check the supplier clock.'
            if sample_time is not None:
                reported_age = (stamp - sample_time).total_seconds()
                if reported_age < 0:
                    error = 'PLC time is in the future. Check the clock and measured offset.'
                values = {name: self.tag_value(tag, row.get(name)) for name, tag in self.tags.items()}
        with self.lock:
            self.contact = (status == 200, stamp.isoformat(), now)
            if error:
                self.progress, self.reason = 0, error
                outcome = error
            else:
                outcome = self.accept(row, values, sample_time, reported_age, now, stamp)
            self.log_read('Read', stamp, status, outcome=outcome, row_id=row_id if count(row_id) else None,
                          source_at=source_at if isinstance(source_at, str) and POLL_TIME.fullmatch(source_at) else None,
                          tags_ok=ok if count(ok) else None, tags_failed=failed if count(failed) else None)

    def poll_time(self, value):
        if self.zone is None or self.offset is None:
            raise PollTimeError('PLC clock is unverified. Confirm its timezone and measured offset.')
        if not isinstance(value, str) or not POLL_TIME.fullmatch(value):
            raise ValueError('Invalid timestamp.')
        stamp = datetime.fromisoformat(value)
        if stamp.tzinfo is None:
            candidates = {stamp.replace(tzinfo=self.zone, fold=fold).astimezone(timezone.utc) for fold in (0, 1)}
            valid = {instant for instant in candidates if instant.astimezone(self.zone).replace(tzinfo=None) == stamp}
            if len(valid) != 1:
                raise PollTimeError('PLC local time is ambiguous or invalid. Check the timezone and supplier clock.')
            stamp = valid.pop()
        return stamp.astimezone(timezone.utc) + self.offset

    @staticmethod
    def tag_value(tag, value):
        kind = tag['data_type']
        if kind == 'Bool':
            return int(value) if type(value) in (bool, int, float) and value in (0, 1) else None
        if kind in ('Word', 'SignedWord'):
            if not number(value) or kind == 'Word' and value < 0:
                return None
            return value
        return value if number(value) else None

    def accept(self, payload, values, sample_time, age, now, stamp):
        ident = (sample_time, payload['id'])
        previous = self.identity
        if ident == previous:
            return 'Repeated row'
        self.identity = ident
        if previous and (ident[0] <= previous[0] or ident[1] <= previous[1]):
            self.progress, self.reason = 0, 'PLC poll time or row ID went backwards or repeated inconsistently. Check the supplier clock and feed.'
            return self.reason
        if self.sample is None or now - self.sample['monotonic_at'] >= LIMIT_MS / 1000:
            self.progress = 0
        if age >= LIMIT_MS / 1000:
            self.progress = 0
        else:
            self.progress = min(2, self.progress + 1)
        failure_count = sum(value is None for value in values.values())
        ok_count, failed_count = payload.get('tags_ok'), payload.get('tags_failed')
        counts_supplied = 'tags_ok' in payload or 'tags_failed' in payload
        counts_match = (count(ok_count) and count(failed_count)
                        and (ok_count, failed_count) == (len(values) - failure_count, failure_count))
        unidentified = counts_supplied and not counts_match
        self.sample = dict(id=ident[1], source_at=payload['timestamp'], read_at=stamp.isoformat(),
                           corrected_source_at=sample_time.isoformat(),
                           monotonic_at=now - age, values={} if unidentified else values,
                           partial=failure_count > 0 or unidentified, unidentified=unidentified,
                           ok=ok_count if count(ok_count) else None, failed=failed_count if count(failed_count) else None)
        self.reason = None if self.progress == 2 else 'Waiting for two progressing PLC rows.'
        return self.reason or ('Partial row' if self.sample['partial'] else 'Replied')

    def snapshot(self):
        with self.lock:
            tags, sample, reason, contact, refresh, view = (
                self.tags, self.sample, self.reason, self.contact, self.refresh, self.view)
        now = self.clock()
        age = max(0, int(1000 * (now - sample['monotonic_at']))) if sample else None
        expired = age is not None and age >= LIMIT_MS
        expired_reason = 'PLC sample expired. Check the supplier polling feed.'
        contact_value, contact_at, contact_stamp = contact
        contact_age = max(0, int(1000 * (now - contact_stamp))) if contact_stamp is not None else None
        variables = [variable('line.api_reachable', 'Line API reachable', contact_value, self.source,
                              age=contact_age, read_at=contact_at, limit=LIMIT_MS,
                              reason=reason if contact_value is not True else None)]
        quality = 'stale' if expired else 'unknown' if reason else 'partial' if sample['partial'] else 'fresh'
        variables[0]['reason'] = ('Line API contact expired. Check the connection.' if variables[0]['quality'] == 'stale'
                                  else variables[0]['reason'])
        for name, label, unit, value in [('poll_age', 'PLC poll age', 'ms', age),
                                         *((name, name, tag['unit'], sample['values'].get(name) if sample else None)
                                           for name, tag in tags.items())]:
            issue = expired_reason if expired else reason
            if value is None and not issue:
                issue = ('Failed signals cannot be identified. Check the supplier response.' if sample['unidentified']
                         else 'PLC value missing, failed or invalid.')
            item = variable(f'line.{name}', label, value if not issue else None, self.source, unit=unit,
                            age=age, read_at=sample['read_at'] if sample else None, limit=LIMIT_MS, reason=issue)
            item['reason'] = issue
            if sample:
                item.update(source_at=sample['source_at'], row_id=sample['id'])
                if issue and value is not None and name != 'poll_age':
                    item['last_read'] = dict(value=value, read_at=item['read_at'], source_at=item['source_at'], row_id=item['row_id'])
            variables.append(item)
        return dict(variables=variables, line=dict(mode=self.mode, quality=quality, refresh=refresh,
                    catalog_revision=view['revision'], catalog_count=len(tags), tanks=view['tanks'],
                    row_id=sample['id'] if sample else None, source_at=sample['source_at'] if sample else None,
                    corrected_source_at=sample['corrected_source_at'] if sample else None,
                    clock_offset_ms=self.offset.total_seconds() * 1000 if self.zone is not None and self.offset is not None else None,
                    tags_ok=sample['ok'] if sample else None, tags_failed=sample['failed'] if sample else None,
                    reason=expired_reason if expired else reason, refresh_available=self.client is not None))


class PollTimeError(ValueError):
    """A fixed operator-facing clock diagnostic."""
