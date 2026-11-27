#!/usr/bin/env python3
"""Verify offline vendor baseline archives and every recorded bundle delta."""
import hashlib
import json
from pathlib import Path
import sys
import tarfile
import tempfile

HERE = Path(__file__).resolve().parent
PACKAGE = HERE.parent
VENDOR = PACKAGE / 'src' / 'anodizing-modules' / 'vendor'


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def tree(root):
    result = {}
    for path in sorted(root.rglob('*')):
        if not path.is_file():
            continue
        relative = path.relative_to(root)
        if (any(part in ('__pycache__', '.pytest_cache', '.venv') for part in relative.parts)
                or path.suffix == '.pyc'):
            continue
        result[str(relative)] = digest(path)
    return result


def fail(message):
    print('FAIL:', message, file=sys.stderr)
    raise SystemExit(1)


def main():
    record = json.loads((HERE / 'BASELINES.json').read_text())
    if len(sys.argv) > 2:
        fail('usage: verify.py [TRUSTED_BASELINES_JSON]')
    receipt = Path(sys.argv[1]) if len(sys.argv) == 2 else HERE / 'private' / 'BASELINES.json'
    if not receipt.is_file():
        fail('supply the trusted private baseline record with its archives')
    baselines = json.loads(receipt.read_text())['baselines']
    if baselines.keys() != record['vendors'].keys():
        fail('private record and public mapping have different vendors')
    with tempfile.TemporaryDirectory() as temporary:
        for name, baseline in baselines.items():
            archive = receipt.parent / baseline['archive']
            if not archive.is_file():
                fail(f'missing archive {archive}')
            actual = digest(archive)
            if actual != baseline['sha256']:
                fail(f'{archive.name}: sha256 {actual} != {baseline["sha256"]}')
            target = Path(temporary) / name
            with tarfile.open(archive, 'r:gz') as bundle:
                bundle.extractall(temporary, filter='data')
            if not target.is_dir():
                fail(f'{archive.name}: missing top-level {name}/')
            baseline_files = tree(target)
            bundle_files = tree(VENDOR / name)
            vendor = record['vendors'][name]
            added = sorted(set(bundle_files) - set(baseline_files))
            deleted = sorted(set(baseline_files) - set(bundle_files))
            changed = sorted(key for key in baseline_files.keys() & bundle_files.keys()
                             if baseline_files[key] != bundle_files[key])
            unchanged = sorted((baseline_files.keys() & bundle_files.keys()) - set(changed))
            expected_added = [item['path'] for item in vendor['added']]
            expected_changed = [item['path'] for item in vendor['changed']]
            if added != expected_added or changed != expected_changed or unchanged != vendor['unchanged']:
                fail(f'{name}: bundle deltas differ from BASELINES.json')
            for item in vendor['added']:
                if bundle_files[item['path']] != item['sha256']:
                    fail(f'{name}: added file hash changed: {item["path"]}')
            for item in vendor['changed']:
                if bundle_files[item['path']] != item['bundle_sha256']:
                    fail(f'{name}: changed file hash differs: {item["path"]}')
            print(f'{name}: {archive.name} verified; '
                  f'{len(added)} added, {len(changed)} changed, {len(deleted)} deleted')
    print('Provenance verified.')


if __name__ == '__main__':
    main()
