"""Issue and check IPC module keys without storing the bearer secrets."""
import argparse
from contextlib import contextmanager
from dataclasses import dataclass
import fcntl
from hashlib import sha256
from hmac import compare_digest
import json
import os
from pathlib import Path
import re
from secrets import token_urlsafe
import stat

from .jobs import atomic_json
from .job_client import calibration_key

SERVICE_OWNERS = {'agv-page', 'calibration', 'main-app-loop'}
SCOPES = {'read', 'jobs'}
NAME = re.compile(r'[a-z][a-z0-9_-]{0,63}')
KEY = re.compile(r'([A-Za-z0-9_-]{16})\.[A-Za-z0-9_-]{43}')
DIGEST = re.compile(r'[a-f0-9]{64}')


@dataclass(frozen=True)
class ModulePrincipal:
    name: str
    token_id: str
    scopes: frozenset[str]


@dataclass(frozen=True)
class ServicePrincipal:
    name: str
    token_id: str
    scopes: frozenset[str]


class KeyStore:
    def __init__(self, path=None):
        self.path = Path(path) if path is not None else None
        self.service_digests = None

    @staticmethod
    def service_credentials(token):
        if not isinstance(token, str) or not token.isascii() or len(token) < 32:
            raise ValueError('Provide a private executor service token of at least 32 ASCII characters.')
        if not token.startswith('sha256:'):
            if any(not 32 < ord(char) < 127 for char in token):
                raise ValueError('The executor service token must be one printable ASCII value.')
            return sha256(token.encode()).hexdigest(), sha256(calibration_key(token).encode()).hexdigest()
        lines = token.splitlines()
        operator = lines[0].removeprefix('sha256:')
        calibration = lines[1].removeprefix('calibration-sha256:') if len(lines) == 2 else None
        if (len(lines) > 2 or not DIGEST.fullmatch(operator) or calibration is not None and (
                not lines[1].startswith('calibration-sha256:') or not DIGEST.fullmatch(calibration)
                or calibration == operator)):
            raise ValueError('Provide distinct executor service SHA-256 digests with 64 lowercase hexadecimal characters.')
        return operator, calibration

    def configure_service_keys(self, token):
        self.service_digests = self.service_credentials(token)

    def authenticate_client(self, key, *, allow_modules):
        operator, calibration = self.service_digests
        digest = sha256(key.encode()).hexdigest()
        if compare_digest(digest, operator):
            return ServicePrincipal('agv-page', operator[:16], frozenset({'operator'}))
        if calibration is not None and compare_digest(digest, calibration):
            return ServicePrincipal('calibration', calibration[:16], frozenset(SCOPES))
        return self.authenticate(key) if allow_modules else None

    @staticmethod
    def _open_private(path, flags=os.O_RDONLY):
        fd = os.open(path, flags | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077:
            os.close(fd)
            raise ValueError('The module key store and its lock must be private regular files.')
        return fd

    def _read(self):
        try:
            fd = self._open_private(self.path)
        except FileNotFoundError:
            return {}
        with os.fdopen(fd, encoding='utf-8') as file:
            records = json.load(file)
        if not isinstance(records, dict):
            raise ValueError('Invalid module key registry.')
        seen = set()
        for name, record in records.items():
            if (not NAME.fullmatch(name) or name in SERVICE_OWNERS or not isinstance(record, dict)
                    or set(record) != {'token_id', 'scopes', 'digest'}
                    or not isinstance(record['token_id'], str)
                    or not re.fullmatch(r'[A-Za-z0-9_-]{16}', record['token_id'])
                    or record['token_id'] in seen or not isinstance(record['scopes'], list)
                    or not record['scopes'] or any(scope not in SCOPES for scope in record['scopes'])
                    or not isinstance(record['digest'], str) or not DIGEST.fullmatch(record['digest'])):
                raise ValueError('Invalid module key registry.')
            seen.add(record['token_id'])
        return records

    @staticmethod
    def _principal(name, record):
        return ModulePrincipal(name, record['token_id'], frozenset(record['scopes']))

    def authenticate(self, key):
        match = KEY.fullmatch(key)
        if not match:
            return None
        try:
            for name, record in self._read().items():
                if (record['token_id'] == match[1]
                        and compare_digest(record['digest'], sha256(key.encode()).hexdigest())):
                    return self._principal(name, record)
        except (OSError, ValueError, TypeError, RecursionError):
            return None
        return None

    @contextmanager
    def _update(self):
        fd = self._open_private(self.path.with_suffix('.lock'), os.O_CREAT | os.O_RDWR)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            records = self._read()
            yield records
            atomic_json(self.path, records, sync_directory=True)
        finally:
            os.close(fd)

    def add(self, name, scopes):
        if not NAME.fullmatch(name) or name in SERVICE_OWNERS:
            raise ValueError('Choose a module name starting with a lowercase letter, using up to 64 lowercase letters, digits, hyphens or underscores. Service owner names are reserved.')
        scopes = sorted(set(scopes))
        if not scopes or any(scope not in SCOPES for scope in scopes):
            raise ValueError('Choose one or both scopes: read, jobs.')
        with self._update() as records:
            if name in records:
                raise ValueError('A key already exists for this module. Revoke it before issuing another key.')
            token_id = token_urlsafe(12)
            while any(record['token_id'] == token_id for record in records.values()):
                token_id = token_urlsafe(12)
            key = f'{token_id}.{token_urlsafe(32)}'
            record = dict(token_id=token_id, scopes=scopes, digest=sha256(key.encode()).hexdigest())
            records[name] = record
        return self._principal(name, record), key

    def list(self):
        return [self._principal(name, record) for name, record in sorted(self._read().items())]

    def revoke(self, name, *, token_id=None):
        with self._update() as records:
            record = records.get(name)
            if record is None or (token_id is not None and token_id != record['token_id']):
                raise ValueError('No matching module key exists.')
            del records[name]


def main():
    parser = argparse.ArgumentParser(description='Manage IPC module keys on the executor host.')
    parser.add_argument('--data-dir', type=Path, required=True)
    actions = parser.add_subparsers(dest='action', required=True)
    issue = actions.add_parser('add')
    issue.add_argument('name')
    issue.add_argument('--scope', action='append', choices=sorted(SCOPES), required=True)
    issue.add_argument('--output-dir', type=Path, required=True)
    actions.add_parser('list')
    actions.add_parser('revoke').add_argument('name')
    args = parser.parse_args()
    store = KeyStore(args.data_dir / 'module-keys.json')
    try:
        if args.action == 'list':
            for record in store.list():
                print(record.name, record.token_id, ','.join(sorted(record.scopes)), sep='\t')
        elif args.action == 'revoke':
            store.revoke(args.name)
        else:
            certificate = (args.data_dir / 'module-api-cert.pem').read_bytes()
            args.output_dir.mkdir(mode=0o700)
            cert_path = args.output_dir / 'ipc-cert.pem'
            key_path = args.output_dir / 'module.key'
            record = None
            try:
                cert_path.write_bytes(certificate)
                record, key = store.add(args.name, args.scope)
                fd = os.open(key_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(fd, 'w', encoding='ascii') as file:
                    file.write(key + '\n')
                    file.flush()
                    os.fsync(file.fileno())
            except (OSError, ValueError):
                key_path.unlink(missing_ok=True)
                cert_path.unlink(missing_ok=True)
                args.output_dir.rmdir()
                if record is not None:
                    store.revoke(record.name, token_id=record.token_id)
                raise
            print(f'{record.name} ({record.token_id})\nKey file: {key_path}\nPinned certificate: {cert_path}')
    except (OSError, ValueError, TypeError) as exc:
        parser.exit(1, f'{exc}\n')


if __name__ == '__main__':
    main()
