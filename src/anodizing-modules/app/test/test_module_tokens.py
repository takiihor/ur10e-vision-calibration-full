"""Offline tests for raw-grant lifetime."""
import pytest

from anodizing_web import module_tokens
from anodizing_web.module_tokens import KeyStore


def test_forward_wall_clock_jump_expires_raw_and_rollback_cannot_restore_it(tmp_path, monkeypatch):
    wall = [1_800_000_000.0]
    boot = [100.0]
    monkeypatch.setattr(module_tokens, 'time', lambda: wall[0])
    monkeypatch.setattr(module_tokens, 'clock_gettime', lambda _: boot[0])
    store = KeyStore(tmp_path / 'keys.json')
    grant, bearer = store.add('diagnostics', ['read', 'jobs', 'raw'], expires=1)
    assert store.authenticate(bearer) == grant

    wall[0] = grant.raw_until
    assert store.authenticate(bearer).scopes == {'read', 'jobs'}
    wall[0] -= 1800
    assert store.authenticate(bearer).scopes == {'read', 'jobs'}
    assert not store.current_grant(grant)

    boot[0] += 1
    renewed = store.grant('diagnostics', ['raw'], expires=1)
    assert store.authenticate(bearer) == renewed


def test_raw_expiry_survives_wall_clock_rollback_and_process_restart(tmp_path, monkeypatch):
    wall = [1_800_000_000]
    boot_time = [100.0]
    monkeypatch.setattr(module_tokens, 'time', lambda: wall[0])
    monkeypatch.setattr(module_tokens, 'clock_gettime', lambda clock: boot_time[0], raising=False)
    monkeypatch.setattr(module_tokens, 'CLOCK_BOOTTIME', 7, raising=False)
    monkeypatch.setattr(module_tokens, '_boot_id', lambda: 'synthetic-boot', raising=False)
    path = tmp_path / 'keys.json'
    _, bearer = KeyStore(path).add('diagnostics', ['read', 'raw'], expires=1)
    assert KeyStore(path).authenticate(bearer).scopes == {'read', 'raw'}
    wall[0] -= 3600
    boot_time[0] += 3601
    assert KeyStore(path).authenticate(bearer).scopes == {'read'}
    wall[0] -= 3600
    assert KeyStore(path).authenticate(bearer).scopes == {'read'}


def test_raw_grant_fails_closed_after_boot_change(tmp_path, monkeypatch):
    boot = ['synthetic-boot-a']
    monkeypatch.setattr(module_tokens, '_boot_id', lambda: boot[0], raising=False)
    path = tmp_path / 'keys.json'
    _, bearer = KeyStore(path).add('diagnostics', ['read', 'raw'], expires=1)
    assert KeyStore(path).authenticate(bearer).scopes == {'read', 'raw'}
    boot[0] = 'synthetic-boot-b'
    assert KeyStore(path).authenticate(bearer).scopes == {'read'}


def test_owner_opt_in_caps_long_raw_grant_at_travel_window(tmp_path, monkeypatch):
    wall = 1_795_000_000
    end = wall + 30 * 3600
    boot_time = [100.0]
    monkeypatch.setattr(module_tokens, 'time', lambda: wall)
    monkeypatch.setattr(module_tokens, 'clock_gettime', lambda clock: boot_time[0])
    monkeypatch.setenv('ANODIZING_MANUAL_TRAVEL_UNTIL', str(end))
    monkeypatch.delenv('ANODIZING_RAW_GRANTS_USE_TRAVEL_WINDOW', raising=False)
    store = KeyStore(tmp_path / 'keys.json')
    original, key = store.add('diagnostics', ['read', 'raw'])
    assert original.raw_until == wall + 8 * 3600
    with pytest.raises(ValueError, match='24 hours'):
        store.grant('diagnostics', ['raw'], expires=30)

    monkeypatch.setenv('ANODIZING_RAW_GRANTS_USE_TRAVEL_WINDOW', '1')
    assert store.authenticate(key) == original
    renewed = store.grant('diagnostics', ['raw'], expires=31)
    assert renewed.raw_until == end
    assert renewed.raw_deadline == boot_time[0] + 30 * 3600
    assert renewed.token_id == original.token_id
    assert store.authenticate(key) == renewed
    boot_time[0] = renewed.raw_deadline
    assert store.authenticate(key).scopes == {'read'}


def test_travel_window_opt_in_refuses_new_grants_after_the_window(tmp_path, monkeypatch):
    monkeypatch.setenv('ANODIZING_RAW_GRANTS_USE_TRAVEL_WINDOW', '1')
    monkeypatch.setenv('ANODIZING_MANUAL_TRAVEL_UNTIL', '100')
    monkeypatch.setattr(module_tokens, 'time', lambda: 100)
    store = KeyStore(tmp_path / 'keys.json')
    with pytest.raises(ValueError, match='open supervised travel window'):
        store.add('diagnostics', ['read', 'raw'])
