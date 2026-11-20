"""One bounded, server-owned recording of cached state and native events."""
from collections import Counter, deque
import csv
import json
import os
from pathlib import Path
import re
import stat
from threading import Condition, Lock, Thread
from time import monotonic
from uuid import uuid4

from .agv import timestamp
from .jobs import atomic_json

MAX_SECONDS = 2 * 60 * 60
MAX_BYTES = 50 * 1024 * 1024
TERMINAL_BYTES = 1024
MAX_ROW = 1024 * 1024
MAX_EVENTS = 1024
RATES = (.2, 1, 5)
FORMATS = {
    'json': ('json', 'application/json'),
    'csv': ('csv', 'text/csv; charset=utf-8'),
    'summary': ('md', 'text/markdown; charset=utf-8'),
}
STORAGE_ERROR = 'Recording storage unavailable. Check space and permissions in the service data directory.'
REASONS = {
    'stopped': 'Stopped by operator',
    'shutdown': 'Service shut down',
    'interrupted': 'Interrupted by service restart',
    'size_limit': '50 MiB limit reached',
    'time_limit': '2 hour limit reached',
    'storage_error': 'Storage error',
    'event_overflow': 'Event buffer filled',
    'source_error': 'State or event data could not be recorded',
}
RECORD_ID = re.compile(r'[a-f0-9]{32}')


def encode(row):
    return json.dumps(row, ensure_ascii=True, allow_nan=False, separators=(',', ':'))


def terminal(reason):
    return dict(type='terminal', timestamp=timestamp(), reason=reason)


def open_record(path, flags):
    fd = os.open(path, flags | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    if not stat.S_ISREG(os.fstat(fd).st_mode):
        os.close(fd)
        raise OSError('Recording file must be a regular file.')
    return os.fdopen(fd, 'rb') if flags == os.O_RDONLY else os.fdopen(fd, 'r+b', buffering=0)


def check_directory(path):
    if not stat.S_ISDIR(path.lstat().st_mode):
        raise OSError('Recording directory must be a real directory.')


def line_sources(manifest):
    labels = {'line.simulator': 'Simulation', 'line.supplier': 'Supplier API', 'line.disabled': 'Disabled'}
    return sorted({labels[item['source']] for item in manifest['variables'] if item.get('source') in labels})


def summary(manifest, outcome=None, stopping=False):
    return {
        **{key: manifest[key] for key in ('id', 'started_at', 'rate_hz', 'agv_transport', 'scene_simulation')},
        'variable_count': len(manifest['variables']),
        'line_sources': line_sources(manifest),
        'status': 'finished' if outcome else 'stopping' if stopping else 'recording',
        'outcome': outcome,
    }


class Recording:
    def __init__(self, manifest, stream, remaining, read_state):
        self.manifest = manifest
        self.stream = stream
        self.remaining = remaining
        self.ids = {item['id'] for item in manifest['variables']}
        self.read_state = read_state
        self.started = monotonic()
        self.period = 1 / manifest['rate_hz']
        self.tick = 0
        self.next_sample = self.started
        self.condition = Condition()
        self.events = deque()
        self.stop_reason = self.outcome = None
        self.thread = Thread(target=self.run, name='operator-recorder', daemon=True)

    def info(self):
        with self.condition:
            return summary(self.manifest, self.outcome, self.stop_reason is not None)

    def enqueue(self, row):
        # Native producers only acquire this queue lock. The worker releases it before I/O or state reads.
        with self.condition:
            if self.stop_reason is not None:
                return
            if len(self.events) == MAX_EVENTS:
                self.stop_reason = 'event_overflow'
            else:
                self.events.append(row)
            self.condition.notify()

    def stop(self, reason):
        with self.condition:
            if self.stop_reason is None:
                self.stop_reason = reason
            self.condition.notify()

    def append(self, row):
        data = (encode(row) + '\n').encode()
        reserve = 0 if row['type'] == 'terminal' else TERMINAL_BYTES
        if len(data) + reserve > self.remaining:
            return False
        if len(data) > MAX_ROW:
            raise ValueError('Recording row too large.')
        if self.stream.write(data) != len(data):
            raise OSError('Short recording write.')
        self.remaining -= len(data)
        return True

    def run(self):
        deadline = self.started + MAX_SECONDS
        offset = 0
        reason = 'source_error'
        try:
            while True:
                with self.condition:
                    now = monotonic()
                    if now >= deadline:
                        reason = 'time_limit'
                        break
                    sample = self.stop_reason is None and now >= self.next_sample
                    if sample:
                        # Skip missed periods after a stall. Repeated reads retain their source time.
                        self.tick = max(self.tick + 1, int((now - self.started) * self.manifest['rate_hz']) + 1)
                        self.next_sample = self.started + self.tick * self.period
                        if self.next_sample <= now:
                            self.tick += 1
                            self.next_sample = self.started + self.tick * self.period
                    elif self.events:
                        row = self.events.popleft()
                    elif self.stop_reason is not None:
                        reason = self.stop_reason
                        break
                    else:
                        self.condition.wait(min(self.next_sample, deadline) - now)
                        continue
                if sample:
                    state = self.read_state()
                    current = {v['id']: v for v in state['variables'] if v['id'] in self.ids}
                    values = [
                        current[item['id']] if item['id'] in current else
                        dict(item, value=None, read_at=None, source_at=None, age_ms=None,
                             quality='unknown', reason='Variable unavailable.')
                        for item in self.manifest['variables']
                    ]
                    row = dict(type='sample', timestamp=state['generated_at'], agv=state.get('agv'), variables=values)
                if not self.append(row):
                    reason = 'size_limit'
                    break
                offset = self.stream.tell()
        except OSError:
            reason = 'storage_error'
        except (TypeError, ValueError, KeyError):
            reason = 'source_error'
        finally:
            outcome = terminal(reason)
            with self.condition:
                self.stop_reason = reason
                self.events.clear()
            try:
                # Discard only bytes of a failed write, keeping every complete earlier row.
                if self.stream.tell() != offset:
                    self.stream.truncate(offset)
                    self.stream.seek(offset)
                if not self.append(outcome):
                    raise OSError('No space for recording outcome.')
                os.fsync(self.stream.fileno())
            except OSError:
                outcome = terminal('storage_error')
            finally:
                try:
                    self.stream.close()
                except OSError:
                    outcome = terminal('storage_error')
                with self.condition:
                    self.outcome = outcome


class Recorder:
    def __init__(self, root, read_state, *, agv_context=None, scene_simulation=False):
        self.directory = Path(root) / 'records'
        self.read_state = read_state
        self.agv_context = agv_context
        self.scene_simulation = scene_simulation
        self.lock = Lock()
        self.active = None
        self.history = {}
        self.closing = False
        self.error = None
        try:
            self.directory.mkdir(mode=0o700, exist_ok=True)
            check_directory(self.directory)
            for path in self.directory.iterdir():
                if RECORD_ID.fullmatch(path.name):
                    try:
                        self.history[path.name] = self.recover(path)
                    except (OSError, ValueError, TypeError, KeyError):
                        self.error = 'Some recordings could not be read. Check recording storage.'
        except OSError:
            self.error = STORAGE_ERROR

    def recover(self, path):
        check_directory(path)
        with open_record(path / 'manifest.json', os.O_RDONLY) as stream:
            manifest = json.loads(stream.read(MAX_ROW))
        if manifest['id'] != path.name or manifest['schema_version'] != 1:
            raise ValueError('Invalid recording manifest.')
        with open_record(path / 'rows.jsonl', os.O_RDWR) as stream:
            size = stream.seek(0, os.SEEK_END)
            start = max(0, size - MAX_ROW - TERMINAL_BYTES)
            stream.seek(start)
            tail = stream.read()
            end = tail.rfind(b'\n') + 1
            if not end and start:
                raise ValueError('Invalid recording tail.')
            last = tail[:end].rstrip(b'\n').rsplit(b'\n', 1)[-1]
            outcome = json.loads(last) if last else None
            if outcome and outcome.get('type') == 'terminal':
                return summary(manifest, outcome)
            outcome = terminal('interrupted')
            data = (encode(outcome) + '\n').encode()
            if end + start + len(data) + (path / 'manifest.json').stat().st_size <= MAX_BYTES:
                stream.seek(start + end)
                stream.truncate()
                if stream.write(data) != len(data):
                    raise OSError('Short interruption write.')
                os.fsync(stream.fileno())
        return summary(manifest, outcome)

    def catalog(self):
        with self.lock:
            latest = self.active.info() if self.active else None
            records = [*self.history.values(), *([latest] if latest else [])]
            records.sort(key=lambda item: item['started_at'], reverse=True)
            return dict(active=latest if latest and latest['outcome'] is None else None,
                        records=records, error=self.error)

    def start(self, settings):
        rate = settings.get('rate_hz')
        if type(rate) not in (float, int) or rate not in RATES:
            raise ValueError('Choose a sample rate of 0.2, 1 or 5 Hz.')
        ids = settings.get('variables')
        if not isinstance(ids, list) or not ids or any(not isinstance(v, str) for v in ids):
            raise ValueError('Choose at least one available variable.')
        chosen = set(ids)
        state = self.read_state()
        variables = [
            {key: v.get(key) for key in ('id', 'name', 'unit', 'source', 'freshness')}
            for v in state['variables'] if v['id'] in chosen
        ]
        if len(variables) != len(ids):
            raise ValueError('Choose distinct available variables and try again.')
        metadata = {}
        for key, limit in (('site', 100), ('operator', 100), ('devices', 200), ('notes', 2000)):
            value = settings.get(key, '')
            if not isinstance(value, str) or len(value) > limit:
                raise ValueError(f'Check the {key} field length.')
            metadata[key] = value.strip()
        context = self.agv_context() if self.agv_context else None
        transport = context['settings']['mode'] if context else next(
            (v['value'] for v in state['variables'] if v['id'] == 'agv.transport'), None)
        with self.lock:
            if self.closing or (self.active and self.active.info()['outcome'] is None):
                raise ValueError('Wait for the active recording to finish.')
            ident = uuid4().hex
            manifest = dict(schema_version=1, id=ident, started_at=timestamp(), rate_hz=rate,
                            variables=variables, agv_transport=transport, scene_simulation=self.scene_simulation,
                            agv_at_start=context, limits={'seconds': MAX_SECONDS, 'bytes': MAX_BYTES}, **metadata)
            remaining = MAX_BYTES - len(encode(manifest).encode())
            if remaining <= TERMINAL_BYTES:
                raise ValueError('The selected recording manifest is too large.')
            path = self.directory / ident
            stream = None
            try:
                check_directory(self.directory)
                path.mkdir(mode=0o700)
                stream = open_record(path / 'rows.jsonl', os.O_RDWR | os.O_CREAT | os.O_EXCL)
                atomic_json(path / 'manifest.json', manifest)
            except OSError:
                if stream is not None:
                    stream.close()
                raise ValueError(STORAGE_ERROR) from None
            if self.active:
                self.history[self.active.manifest['id']] = self.active.info()
            self.active = Recording(manifest, stream, remaining, self.read_state)
            self.active.thread.start()
            return self.active.info()

    def event(self, source, name, **fields):
        session = self.active
        if session is not None:
            if 'timestamp' not in fields:
                fields['timestamp'] = timestamp()
            session.enqueue(dict(type='event', source=source, event=name, **fields))

    def stop(self, ident):
        with self.lock:
            if not self.active or self.active.manifest['id'] != ident:
                raise ValueError('This recording is no longer active. Refresh the list.')
            self.active.stop('stopped')
            return self.active.info()

    def close(self):
        with self.lock:
            self.closing = True
            session = self.active
            if session:
                session.stop('shutdown')
        if session:
            session.thread.join()

    def export(self, ident, format, output):
        if not RECORD_ID.fullmatch(ident) or format not in FORMATS:
            raise KeyError(ident)
        with self.lock:
            item = (self.active.info() if self.active and self.active.manifest['id'] == ident
                    else self.history.get(ident))
            if item is None:
                raise KeyError(ident)
            if item['outcome'] is None:
                raise ValueError('Finish the recording before downloading.')
        path = self.directory / ident
        check_directory(self.directory)
        check_directory(path)
        with open_record(path / 'manifest.json', os.O_RDONLY) as manifest_file:
            manifest = json.loads(manifest_file.read(MAX_ROW))
        with open_record(path / 'rows.jsonl', os.O_RDONLY) as stream:
            def rows():
                while line := stream.readline(MAX_ROW):
                    if not line.endswith(b'\n'):
                        if stream.read(1):
                            raise OSError('Recording row exceeds the read limit.')
                        break
                    yield json.loads(line)

            if format == 'json':
                output.write('{"manifest":' + encode(manifest) + ',"outcome":' + encode(item['outcome']) + ',"rows":[')
                for index, row in enumerate(rows()):
                    if index:
                        output.write(',')
                    output.write(encode(row))
                output.write(']}\n')
            elif format == 'csv':
                write_csv(rows(), output)
            else:
                write_summary(manifest, item['outcome'], rows(), output)


def write_csv(rows, output):
    fields = ('sample_at', 'id', 'name', 'value', 'unit', 'source', 'read_at', 'source_at', 'row_id', 'quality', 'age_ms', 'reason')
    writer = csv.DictWriter(output, fields, extrasaction='ignore')
    writer.writeheader()
    for row in rows:
        if row['type'] == 'sample':
            for variable in row['variables']:
                value = variable['value']
                writer.writerow(dict(variable, sample_at=row['timestamp'],
                                     value=encode(value) if value is not None else ''))


def write_summary(manifest, outcome, rows, output):
    counts = Counter()
    transports = set()
    for row in rows:
        counts[row['type']] += 1
        if row['type'] == 'event' and row['source'] == 'agv' and row.get('transport'):
            transports.add(row['transport'])
    ident = manifest['id']
    label = REASONS[outcome['reason']]
    start_transport = (manifest['agv_transport'] or 'disconnected').capitalize()
    observed = ', '.join(sorted(transports)) or 'None'
    output.write('| Recording | Outcome | Start (UTC) | End (UTC) | Rate | Samples | Events |\n'
                 '| --- | --- | --- | --- | --- | --- | --- |\n'
                 f"| {ident} | {label} | {manifest['started_at']} | {outcome['timestamp']} | "
                 f"{manifest['rate_hz']} Hz | {counts['sample']} | {counts['event']} |\n\n"
                 f'AGV transport at start: {start_transport}\n\nAGV event transports: {observed}\n\n'
                 f"Scene simulation: {'Enabled' if manifest['scene_simulation'] else 'Disabled'}\n\n")
    if sources := line_sources(manifest):
        output.write('Line sources: ' + ', '.join(sources) + '\n\n')
    for key in ('site', 'operator', 'devices', 'notes'):
        if value := manifest[key]:
            # Keep operator-entered text literal even when a Markdown viewer renders the report.
            escaped = re.sub(r'([\x21-\x2f\x3a-\x40\x5b-\x60\x7b-\x7e])', r'\\\1', value)
            output.write(f'{key.capitalize()}\n\n' + '\n'.join('> ' + line for line in escaped.splitlines()) + '\n\n')
    output.write(f'Full evidence: [JSON](record-{ident}.json) · [CSV samples](record-{ident}.csv)\n')
