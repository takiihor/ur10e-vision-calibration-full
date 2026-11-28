"""Issue and check IPC module keys without storing the bearer secrets."""
import argparse
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
import fcntl
from functools import cached_property
from hashlib import sha256
from hmac import compare_digest
import json
from math import isfinite
import os
from pathlib import Path
import re
from secrets import token_urlsafe
import stat
from threading import Lock
from time import CLOCK_BOOTTIME, clock_gettime, time
from uuid import UUID

from .jobs import atomic_json
from .job_client import calibration_key

SERVICE_OWNERS = {'agv-page', 'calibration', 'main-app-loop'}
SCOPES = {'read', 'jobs'}
MODULE_SCOPES = SCOPES | {'raw'}
NAME = re.compile(r'[a-z][a-z0-9_-]{0,63}')
KEY = re.compile(r'([A-Za-z0-9_-]{16})\.[A-Za-z0-9_-]{43}')
DIGEST = re.compile(r'[a-f0-9]{64}')
KEY_FIELDS = {'token_id', 'scopes', 'digest'}
RAW_CLOCK_FIELDS = {'raw_boot', 'raw_deadline'}


def _boot_id():
    return str(UUID(Path('/proc/sys/kernel/random/boot_id').read_text().strip()))


@dataclass(frozen=True)
class ModulePrincipal:
    name: str
    token_id: str
    scopes: frozenset[str]
    raw_until: float | None = None
    raw_deadline: float | None = None
    check_current: bool = field(default=False, compare=False, repr=False)


@dataclass(frozen=True)
class ServicePrincipal:
    name: str
    token_id: str
    scopes: frozenset[str]


class KeyStore:
    def __init__(self, path=None):
        self.path = Path(path) if path is not None else None
        self.service_digests = None
        self.registry_lock = Lock()
        self.registry_cache = None
        self.raw_expiry_lock = Lock()
        self.expired_raw_grants = set()

    @cached_property
    def boot_id(self):
        return _boot_id()

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
                    or set(record) not in (KEY_FIELDS, KEY_FIELDS | {'raw_until'},
                                           KEY_FIELDS | {'raw_until'} | RAW_CLOCK_FIELDS)
                    or not isinstance(record['token_id'], str)
                    or not re.fullmatch(r'[A-Za-z0-9_-]{16}', record['token_id'])
                    or record['token_id'] in seen or not isinstance(record['scopes'], list)
                    or not record['scopes'] or any(not isinstance(scope, str) or scope not in MODULE_SCOPES
                                                   for scope in record['scopes'])
                    or not isinstance(record['digest'], str) or not DIGEST.fullmatch(record['digest'])):
                raise ValueError('Invalid module key registry.')
            expiry = record.get('raw_until')
            if ('raw' in record['scopes']) != ('raw_until' in record) or 'raw_until' in record and (
                    type(expiry) not in (int, float) or not 0 < expiry < 253402300799):
                raise ValueError('Invalid raw scope expiry.')
            if 'raw_deadline' in record and (
                    not isinstance(record['raw_boot'], str) or not record['raw_boot']
                    or type(record['raw_deadline']) not in (int, float)
                    or not 0 < record['raw_deadline'] < 253402300799):
                raise ValueError('Invalid raw scope clock.')
            seen.add(record['token_id'])
        return records

    @staticmethod
    def _principal(name, record):
        return ModulePrincipal(name, record['token_id'], frozenset(record['scopes']),
                               record.get('raw_until'), record.get('raw_deadline'))

    def _effective_principal(self, name, record):
        principal = self._principal(name, record)
        if 'raw' in principal.scopes and (
                record.get('raw_boot') != self.boot_id or self.raw_expired(principal)):
            scopes = principal.scopes - {'raw'}
            return ModulePrincipal(name, principal.token_id, scopes) if scopes else None
        return principal

    def raw_expired(self, principal):
        if principal.raw_until is None or principal.raw_deadline is None:
            return True
        grant = (principal.token_id, principal.raw_until, principal.raw_deadline)
        # The final send and motion watcher use this check without registry I/O.
        with self.raw_expiry_lock:
            if grant in self.expired_raw_grants:
                return True
            if time() >= principal.raw_until or clock_gettime(CLOCK_BOOTTIME) >= principal.raw_deadline:
                self.expired_raw_grants.add(grant)
                return True
        return False

    def _registry_stamp(self):
        try:
            info = self.path.lstat()
        except FileNotFoundError:
            return None
        return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns

    def authenticate(self, key):
        match = KEY.fullmatch(key)
        if not match or self.path is None:
            return None
        try:
            with self.registry_lock:
                stamp = self._registry_stamp()
                if self.registry_cache is None or self.registry_cache[0] != stamp:
                    records = self._read()
                    if self._registry_stamp() != stamp:
                        return None
                    indexed = {record['token_id']: (name, record) for name, record in records.items()}
                    self.registry_cache = stamp, indexed
                entry = self.registry_cache[1].get(match[1])
            if entry:
                name, record = entry
                if compare_digest(record['digest'], sha256(key.encode()).hexdigest()):
                    return self._effective_principal(name, record)
        except (OSError, ValueError, TypeError, RecursionError):
            return None
        return None

    def current_grant(self, principal):
        try:
            record = self._read().get(principal.name)
            return record is not None and self._effective_principal(principal.name, record) == principal
        except (OSError, ValueError, TypeError, RecursionError):
            return False

    @contextmanager
    def hold_grant(self, principal):
        # Serialize the frame with registry updates, before acquiring the wire frame lock.
        fd = self._open_private(self.path.with_suffix('.lock'), os.O_CREAT | os.O_RDWR)
        try:
            fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
            yield self.current_grant(principal)
        finally:
            os.close(fd)

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

    def _raw_clock(self, expires):
        hours = 8 if expires is None else expires
        if type(hours) not in (int, float) or not isfinite(hours) or hours <= 0:
            raise ValueError('Choose a positive finite raw expiry in hours.')
        now = time()
        if os.environ.get('ANODIZING_RAW_GRANTS_USE_TRAVEL_WINDOW') == '1':
            try:
                end = float(os.environ.get('ANODIZING_MANUAL_TRAVEL_UNTIL', 'nan'))
            except ValueError:
                end = float('nan')
            if not now < end < 253402300799:
                raise ValueError('Extended raw grants require an open supervised travel window.')
            until = min(now + hours * 3600, end)
        elif hours > 24:
            raise ValueError('Raw access expires after more than zero and at most 24 hours.')
        else:
            until = now + hours * 3600
        return dict(raw_until=until, raw_boot=self.boot_id,
                    raw_deadline=clock_gettime(CLOCK_BOOTTIME) + (until - now))

    def add(self, name, scopes, *, expires=None):
        if not NAME.fullmatch(name) or name in SERVICE_OWNERS:
            raise ValueError('Choose a module name starting with a lowercase letter, using up to 64 lowercase letters, digits, hyphens or underscores. Service owner names are reserved.')
        scopes = sorted(set(scopes))
        if not scopes or any(scope not in MODULE_SCOPES for scope in scopes):
            raise ValueError('Choose from the read, jobs and raw scopes.')
        if expires is not None and 'raw' not in scopes:
            raise ValueError('--expires applies to the raw scope.')
        raw_clock = self._raw_clock(expires) if 'raw' in scopes else {}
        with self._update() as records:
            if name in records:
                raise ValueError('A key already exists for this module. Revoke it before issuing another key.')
            token_id = token_urlsafe(12)
            while any(record['token_id'] == token_id for record in records.values()):
                token_id = token_urlsafe(12)
            key = f'{token_id}.{token_urlsafe(32)}'
            record = dict(token_id=token_id, scopes=scopes, digest=sha256(key.encode()).hexdigest())
            record.update(raw_clock)
            records[name] = record
        return self._principal(name, record), key

    def grant(self, name, scopes, *, expires=None):
        if set(scopes) != {'raw'}:
            raise ValueError('Use grant to add or renew the raw scope.')
        raw_clock = self._raw_clock(expires)
        with self._update() as records:
            if name not in records:
                raise ValueError('No matching module key exists.')
            record = records[name]
            record.update(scopes=sorted(set(record['scopes']) | {'raw'}), **raw_clock)
        return self._principal(name, record)

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
    issue.add_argument('--scope', action='append', choices=sorted(MODULE_SCOPES), required=True)
    issue.add_argument('--expires', type=float, metavar='HOURS')
    issue.add_argument('--output-dir', type=Path, required=True)
    grant = actions.add_parser('grant')
    grant.add_argument('name')
    grant.add_argument('--scope', action='append', choices=['raw'], required=True)
    grant.add_argument('--expires', type=float, metavar='HOURS')
    actions.add_parser('list')
    actions.add_parser('revoke').add_argument('name')
    args = parser.parse_args()
    store = KeyStore(args.data_dir / 'module-keys.json')
    try:
        if args.action == 'list':
            for record in store.list():
                fields = [record.name, record.token_id, ','.join(sorted(record.scopes))]
                if record.raw_until is not None:
                    fields.append(datetime.fromtimestamp(record.raw_until, UTC).isoformat())
                print(*fields, sep='\t')
        elif args.action == 'revoke':
            store.revoke(args.name)
        elif args.action == 'grant':
            record = store.grant(args.name, args.scope, expires=args.expires)
            print(f'{record.name} raw expiry: {datetime.fromtimestamp(record.raw_until, UTC).isoformat()}')
        else:
            certificate = (args.data_dir / 'module-api-cert.pem').read_bytes()
            args.output_dir.mkdir(mode=0o700)
            cert_path = args.output_dir / 'ipc-cert.pem'
            key_path = args.output_dir / 'module.key'
            record = None
            try:
                cert_path.write_bytes(certificate)
                record, key = store.add(args.name, args.scope, expires=args.expires)
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
