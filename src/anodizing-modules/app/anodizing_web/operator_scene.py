"""Read-only display geometry and scene samples. No hardware connection is opened."""
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import json
import math
import re
import struct
from time import monotonic

from .paths import inside
from .site_profile import finite_numbers, section

MAX_DRAWING_BYTES = 12_000_000
TANK_ID = re.compile(r'Tank(?:0[1-9]|[1-9][0-9])')
STATUS_KEY = re.compile(r'[A-Za-z][A-Za-z0-9_]{0,63}')
SHA256 = re.compile(r'[a-f0-9]{64}')
SCRIPTED_STATES = {'Occupied', 'Empty', 'Open', 'Closed', 'Active', 'Inactive'}


def read_drawing(root, drawing):
    path = inside(root, drawing['path'])
    if path.suffix.lower() != '.glb' or not path.is_file():
        raise ValueError('Select a GLB file.')
    with path.open('rb') as stream:
        data = stream.read(MAX_DRAWING_BYTES + 1)
    if not 20 <= len(data) <= MAX_DRAWING_BYTES or sha256(data).hexdigest() != drawing['sha256']:
        raise ValueError('Drawing size or identity differs.')
    magic, version, size, json_size, chunk = struct.unpack_from('<4sIIII', data)
    if (magic, version, size, chunk) != (b'glTF', 2, len(data), 0x4E4F534A) or json_size > size - 20:
        raise ValueError('Drawing must be a GLB 2.0.')
    document = json.loads(data[20:20 + json_size])
    if document['asset']['version'] != '2.0' or document.get('extensionsRequired'):
        raise ValueError('Use a self-contained drawing without external decoders.')
    if any('uri' in item for key in ('buffers', 'images') for item in document.get(key, [])):
        raise ValueError('Embed all drawing resources.')
    return data


def read_track(value):
    start = finite_numbers(value['start_m'], 3)
    end = finite_numbers(value['end_m'], 3)
    length = math.dist(start, end)
    stations = value['stations_m']
    if not math.isfinite(length) or start[::2] == end[::2] or not isinstance(stations, dict) or not stations:
        raise ValueError('Enter track endpoints and stations.')
    for station, distance in stations.items():
        if (not re.fullmatch(r'[1-9][0-9]{0,2}', station) or int(station) > 255
                or type(distance) not in (int, float) or not 0 <= distance <= length):
            raise ValueError('Enter a valid station and distance along the track.')
    if len(set(stations.values())) != len(stations):
        raise ValueError('Enter a distinct distance for each station.')
    return dict(start_m=start, end_m=end, stations_m=stations)


def read_mount(value):
    return {field: finite_numbers(value[field], 3) for field in ('xyz_m', 'rpy_rad')}


def read_tanks(items):
    tanks = {}
    for item in items:
        ident, key = item['id'], item.get('status_key')
        if not TANK_ID.fullmatch(ident) or ident in tanks or (key is not None and not STATUS_KEY.fullmatch(key)):
            raise ValueError('Enter unique tank IDs and valid status keys.')
        label = item.get('label', f'Tank {int(ident[4:])}')
        if not isinstance(label, str) or not 1 <= len(label.strip()) <= 60:
            raise ValueError('Enter a short tank label.')
        tanks[ident] = dict(id=ident, label=label.strip(), anchor_m=finite_numbers(item['anchor_m'], 3),
                            status_id=f'line.{key}' if key else None)
    return list(tanks.values())


def variable(ident, name, value, source, *, unit=None, age=None, read_at=None, limit=None, reason=None):
    stale = limit is not None and age is not None and age >= limit
    return dict(id=ident, name=name, value=None if stale else value, unit=unit, source=source,
                read_at=read_at, source_at=None, age_ms=age,
                freshness=dict(kind='max_age_ms', max_age_ms=limit) if limit else dict(kind='on_event'),
                quality='stale' if stale else 'unknown' if value is None else 'fresh',
                reason='Sample expired.' if stale else reason)


class Scene:
    def __init__(self, root, *, simulate=False):
        profile = section(root, 'scene')
        self.data = None
        self.simulate = simulate
        started = monotonic()
        self.start = math.floor(started * 5) / 5
        self.start_utc = datetime.now(timezone.utc) - timedelta(seconds=started - self.start)
        self.config = dict(drawing=None, track=None, robot_mount=None, tanks=[], simulation=simulate)
        try:
            drawing = profile['drawing']
            digest = drawing['sha256']
            if not SHA256.fullmatch(digest):
                raise ValueError('Enter a drawing SHA-256.')
            self.data = read_drawing(root, drawing)
            self.config['drawing'] = dict(sha256=digest, url=f'/api/scene/drawing/{digest}.glb')
        except (KeyError, TypeError, AttributeError, ValueError, OSError, RecursionError):
            pass
        for key, validator in (('track', read_track), ('robot_mount', read_mount), ('tanks', read_tanks)):
            try:
                self.config[key] = validator(profile[key])
            except (KeyError, TypeError, AttributeError, ValueError, OverflowError):
                pass
        if not self.data:
            self.config.update(track=None, robot_mount=None)
            for tank in self.config['tanks']:
                tank['anchor_m'] = None
        fixture = profile.get('scripted_line') if simulate else {}
        self.fixture = fixture if isinstance(fixture, dict) else {}

    def robot_sample(self, owner, now):
        if not owner.lock.acquire(blocking=False):
            return None, 'Robot ownership is changing.'
        try:
            if owner.closing or owner.execution_uncertain:
                return None, 'Robot state is uncertain.'
            if owner.active:
                provider = owner.runner
                if owner.teacher_job == owner.active and owner.teacher_service:
                    provider = owner.teacher_service.motion
                cached = getattr(provider, 'cached_joint_sample', None)
                return (cached() if cached else None), 'The active job has no available joint sample.'
            if not self.simulate:
                return None, 'Live idle robot feedback is disabled.'
            tick = math.floor(now * 5)
            q = [math.sin(tick / 20) * .35, -1.9, -1.6, -1.2, 1.57, 0.0]
            return (q, tick / 5, True), None
        finally:
            owner.lock.release()

    def variables(self, owner, model, now=None):
        try:
            model.check_model()
            identity = dict(sha256=model.model_hash, nominal=model.model_data.get('nominal') is True)
        except (OSError, ValueError):
            identity = None
        yield variable('robot.model', 'Robot model', identity, 'robot.model',
                       reason=None if identity else 'The selected robot model changed. Review it and restart the service.')
        now = monotonic() if now is None else now
        sample, reason = self.robot_sample(owner, now) if identity else (None, 'Robot model unavailable.')
        source, age, read_at, q = 'robot.joints', None, None, None
        if sample is not None:
            q, stamp, simulated = sample
            seconds = max(0, now - stamp)
            age = int(seconds * 1000)
            source = 'robot.simulator' if simulated else 'robot.job.sample'
            read_at = (self.start_utc + timedelta(seconds=stamp - self.start)).isoformat()
            try:
                finite_numbers(q, 6)
                if any(abs(angle) > 4 * math.pi for angle in q):
                    raise ValueError('Joint angle outside display range.')
            except ValueError:
                q, reason = None, 'The joint sample is invalid.'
        yield dict(variable('robot.joints', 'Robot joints', q, source, unit='rad', age=age,
                            read_at=read_at, limit=1000, reason=None if q is not None else reason),
                   model_sha256=identity['sha256'] if identity else None)
        status_ids = {tank['status_id'] for tank in self.config['tanks'] if tank['status_id']}
        for ident in status_ids:
            key = ident.removeprefix('line.')
            value = self.fixture.get(key)
            if isinstance(value, str) and value in SCRIPTED_STATES:
                stamp = math.floor(now - self.start) + self.start
                age = int(max(0, now - stamp) * 1000)
                read_at = (self.start_utc + timedelta(seconds=stamp - self.start)).isoformat()
                yield variable(ident, key, value, 'line.simulator', age=age, read_at=read_at, limit=3000)
            else:
                yield variable(ident, key, None, 'line.status', limit=3000, reason='Line status is unavailable.')
