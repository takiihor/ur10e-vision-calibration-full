"""Import explicitly selected legacy files and compute their historical labels."""
import argparse
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import tempfile

from .jobs import atomic_json
from .operator_line import IDENT, local_json, number, parse_catalog
from .operator_line_test import private_directory, sync_directory

FILE_LIMIT = 8_000_000
TOTAL_LIMIT = 32_000_000
RESULTS = 'operator-results.json'
CHECKS = 'api-checks.json'
HISTORY = 'operator-results-history.jsonl'
HASH = re.compile(r'[0-9a-f]{64}')
PART = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,119}')
LABELS = {'checked': 'Pass', 'failed': 'Fail', 'unchecked': 'Unchecked'}
HUMAN = {'pass': 'Pass', 'fail': 'Fail', 'unchecked': 'Not checked'}
MIN_TIME = datetime.min.replace(tzinfo=timezone.utc)


def checked_path(name):
    pieces = name.split('/')
    if (len(pieces) > 8 or not all(PART.fullmatch(piece) for piece in pieces)
            or Path(name).suffix.lower() not in ('.json', '.jsonl', '.md', '.txt', '.csv', '.html')):
        raise ValueError('Select relative report or evidence filenames.')
    return Path(name)


def read_file(root, name):
    path = root / checked_path(name)
    if path.resolve(strict=True) != path:
        raise ValueError('Select original regular files without symbolic links.')
    with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK), 'rb') as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise ValueError('Select a regular evidence file.')
        data = stream.read(FILE_LIMIT + 1)
    if len(data) > FILE_LIMIT:
        raise ValueError('Evidence file exceeds the size limit.')
    return data


def decode(raw):
    def invalid(_):
        raise ValueError('Evidence contains an invalid number.')
    return json.loads(raw, parse_constant=invalid)


def timestamp(value):
    try:
        stamp = datetime.fromisoformat(value)
        return stamp if stamp.tzinfo else MIN_TIME
    except (TypeError, ValueError):
        return MIN_TIME


def context(value):
    if (not isinstance(value, dict) or set(value) != {'mode', 'api_address'}
            or value['mode'] not in ('REAL', 'MOCK') or not isinstance(value['api_address'], str)
            or not 0 < len(value['api_address']) <= 256):
        raise ValueError('Archive context is missing or invalid.')
    return value


def identity(value):
    if (not isinstance(value, list) or len(value) != 5
            or not isinstance(value[0], str) or not IDENT.fullmatch(value[0])
            or not isinstance(value[1], str) or not re.fullmatch(r'[A-Za-z0-9_.:-]{1,40}', value[1])
            or not isinstance(value[2], str) or len(value[2]) > 20
            or value[3] not in ('R', 'W') or not number(value[4])):
        raise ValueError('Archive tag signature is invalid.')
    return tuple(value)


def original_signature(tag):
    return tuple(tag[key] for key in ('tag_name', 'device_address', 'data_type', 'read_write', 'multiplier'))


def complete(record):
    if record.get('eligible') is not True or record.get('failure'):
        return False
    for phase, value in [('one', 1), ('zero', 0)]:
        step = record.get(phase) or {}
        read = step.get('observed') or {}
        accepted, baseline = timestamp(step.get('accepted_at')), timestamp(step.get('baseline_utc'))
        if (step.get('receipt_valid') is not True or read.get('fresh') is not True
                or read.get('command_value') != value or read.get('Remote') != 1 or read.get('Local') != 0
                or min(accepted, baseline) == MIN_TIME or timestamp(read.get('supplier_utc')) <= max(accepted, baseline)
                or record.get('feedback_tag') and (value == 1 or record.get('feedback_clear_required'))
                and read.get('feedback_value') != value):
            return False
    return True


def provenance(row):
    if row is None:
        return None
    return {key: row[key] for key in ('at', 'observed_at', 'checked_at', 'started_at', 'source',
                                      'evidence_ref', 'physical_stop_verified') if key in row}


def summarize(files, tags):
    saved = decode(files[RESULTS]) if RESULTS in files else None
    operator_context = context(saved['context']) if saved else None
    decisions, checks, history_events, unmatched = {}, {}, 0, 0
    if RESULTS in files:
        if (not isinstance(saved, dict) or saved.get('format') != 'operator-results-v1'
                or not isinstance(saved.get('records'), list)):
            raise ValueError('Invalid saved operator results.')
        for row in saved['records']:
            signature = identity(row['identity'])
            kind = row['kind']
            allowed = HUMAN if kind == 'human' else {**LABELS, 'auto': ''} if kind == 'api' else {}
            if (row['value'] not in allowed or timestamp(row['at']) == MIN_TIME or signature[3] != 'W'
                    or row.get('write_sent') is not False or not isinstance(row.get('source'), str)
                    or row.get('observed_at') is not None and timestamp(row['observed_at']) == MIN_TIME):
                raise ValueError('Invalid archived operator judgment.')
            if tags is not None and signature not in tags:
                unmatched += 1
                continue
            key = (signature, kind)
            if key not in decisions or timestamp(row['at']) >= timestamp(decisions[key]['at']):
                decisions[key] = row
    if CHECKS in files:
        saved_checks = decode(files[CHECKS])
        if not isinstance(saved_checks.get('records'), list):
            raise ValueError('Invalid saved API checks.')
        for row in saved_checks['records']:
            signature = identity(row['identity'])
            row_context = context(row['context'])
            if row['tag_name'] != signature[0] or row['api_status'] not in LABELS or signature[3] != 'W':
                raise ValueError('Invalid archived API check.')
            if row_context != operator_context or tags is not None and signature not in tags:
                unmatched += 1
                continue
            checks[signature[0]] = row
    if HISTORY in files:
        for line in files[HISTORY].splitlines():
            row = decode(line)
            if not isinstance(row, dict):
                raise ValueError('Invalid archived history event.')
            context(row['context'])
            identity(row['identity'])
            history_events += 1

    signatures = {sig for sig, _ in decisions}
    signatures.update(tuple(row['identity']) for row in checks.values())
    if tags is not None and operator_context:
        signatures.update(sig for sig in tags if sig[3] == 'W')
    rows = []
    for sig in sorted(signatures):
        auto = checks.get(sig[0])
        human = decisions.get((sig, 'human'))
        manual = decisions.get((sig, 'api'))
        archived = auto
        valid = (auto is None or (tuple(auto['identity']) == sig and auto.get('identity_valid') is not False
                 and (tags is None or not auto.get('feedback_tag')
                      or tuple(auto.get('feedback_identity') or ()) in tags)))
        api = auto['api_status'] if auto and valid else 'unchecked'
        if api == 'checked' and not complete(auto):
            api = 'unchecked'
        previous = auto.get('previous_result') if auto and valid and api == auto['api_status'] == 'unchecked' else None
        if (previous and previous.get('context') == operator_context and previous.get('identity') == list(sig)
                and previous.get('feedback_tag') == auto.get('feedback_tag')
                and previous.get('feedback_identity') == auto.get('feedback_identity')
                and (previous.get('api_status') == 'failed'
                     or previous.get('api_status') == 'checked' and complete(previous))):
            auto, api = previous, previous['api_status']
        origin = 'Automated' if api != 'unchecked' else 'None'
        if (human and human['value'] == 'pass' and human.get('api_backfill_allowed') is not False
                and valid and api != 'checked'
                and not (api == 'failed' and timestamp(auto.get('checked_at') or auto.get('started_at'))
                         >= timestamp(human.get('observed_at') or human['at']))):
            api, origin, auto = 'checked', 'Human backfill', human
        if manual and manual['value'] != 'auto':
            api, origin, auto = manual['value'], 'Manual', manual
        rows.append(dict(identity=sig, api=LABELS[api], api_source=origin,
                         human=HUMAN[human['value']] if human else 'Not checked',
                         api_evidence=provenance(auto), human_evidence=provenance(human),
                         automated_evidence=provenance(archived)))
    api_counts = Counter(row['api'] for row in rows)
    human_counts = Counter(row['human'] for row in rows)
    return dict(context=operator_context, rows=rows, history_events=history_events, unmatched=unmatched,
                api_counts={label: api_counts[label] for label in LABELS.values()},
                human_counts={label: human_counts[label] for label in HUMAN.values()})


def import_archive(root, source, names, catalog=None):
    source, root = Path(source).resolve(strict=True), Path(root).resolve(strict=True)
    names = sorted(set(names))
    if not 1 <= len(names) <= 100:
        raise ValueError('Select up to 100 original files.')
    files, total = {}, 0
    for name in names:
        data = read_file(source, name)
        total += len(data)
        if total > TOTAL_LIMIT:
            raise ValueError('Selected evidence exceeds the archive size limit.')
        if name.endswith('.json'):
            decode(data)
        if name.endswith('.jsonl'):
            for line in data.splitlines():
                decode(line)
        files[name] = data
    tags = {original_signature(tag) for tag in parse_catalog(local_json(source, catalog)).values()} if catalog else None
    summary = summarize(files, tags)
    checksums = {name: hashlib.sha256(data).hexdigest() for name, data in files.items()}
    digest = hashlib.sha256(json.dumps([checksums, sorted(tags, key=str) if tags else None]).encode()).hexdigest()
    parent = root / 'line-tests'
    private_directory(parent)
    imports = parent / 'imports'
    private_directory(imports)
    sync_directory(root)
    sync_directory(parent)
    destination = imports / digest
    if destination.exists():
        read_manifest(imports, digest)
        return digest
    temporary = Path(tempfile.mkdtemp(prefix='.import-', dir=imports))
    try:
        for name, data in files.items():
            path = temporary / 'originals' / name
            path.parent.mkdir(parents=True, exist_ok=True)
            with os.fdopen(os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600), 'wb') as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
        atomic_json(temporary / 'manifest.json',
                    dict(id=digest, files=names, checksums=checksums, **summary))
        for path, _, _ in os.walk(temporary, topdown=False):
            sync_directory(path)
        try:
            temporary.rename(destination)
        except OSError:
            if not destination.is_dir():
                raise
            read_manifest(imports, digest)
        sync_directory(imports)
    finally:
        if temporary.exists():
            shutil.rmtree(temporary)
    return digest


def read_manifest(imports, digest):
    if not HASH.fullmatch(digest):
        raise ValueError('Archive unavailable.')
    if imports.resolve() != imports:
        raise ValueError('Archive unavailable.')
    archive = imports / digest
    if archive.resolve(strict=True) != archive:
        raise ValueError('Archive unavailable.')
    result = local_json(archive, 'manifest.json')
    if result.get('id') != digest or not isinstance(result.get('files'), list):
        raise ValueError('Archive unavailable.')
    return result


def archives(root, *, detail=True):
    imports = Path(root) / 'line-tests' / 'imports'
    if not imports.is_dir() or imports.resolve() != imports:
        return []
    result = []
    for path in sorted(imports.iterdir()):
        if HASH.fullmatch(path.name):
            manifest = read_manifest(imports, path.name)
            if not detail:
                manifest.pop('rows', None)
            result.append(manifest)
    return result


def original(root, digest, index):
    imports = Path(root) / 'line-tests' / 'imports'
    if not index.isdecimal() or len(index) > 3 or imports.resolve() != imports:
        raise ValueError('Original unavailable.')
    manifest = read_manifest(imports, digest)
    name = manifest['files'][int(index)]
    source = imports / digest / 'originals'
    path = source / checked_path(name)
    if path.resolve(strict=True) != path:
        raise ValueError('Original unavailable.')
    stream = os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK), 'rb')
    if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
        stream.close()
        raise ValueError('Original unavailable.')
    stream.original_name = path.name
    return stream


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data-dir', type=Path, required=True)
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--catalog', help='Relative file with the archived catalog for matching historical signatures')
    parser.add_argument('files', nargs='+')
    args = parser.parse_args()
    try:
        print(import_archive(args.data_dir, args.source, args.files, args.catalog))
    except (OSError, ValueError, KeyError, TypeError, AttributeError, RecursionError) as exc:
        parser.exit(2, f'Archive import failed ({type(exc).__name__}). Check the selected local files.\n')


if __name__ == '__main__':
    main()
