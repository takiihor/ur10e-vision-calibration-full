"""Offline checks of the raw grant at the fake device boundary."""
from dataclasses import replace

import pytest

from anodizing_web import module_tokens
from anodizing_web.executor_http import control_handler
from anodizing_web.module_tokens import KeyStore
from test_executor_module_gateway import gateway


def test_revoked_raw_grant_refused_before_fake_send(gateway, tmp_path, monkeypatch):
    executor, _, _ = gateway
    store = KeyStore(tmp_path / 'keys.json')
    principal, _ = store.add('diagnostics', ['raw'])
    control_handler(executor, 'x' * 32, store)
    original = executor.devices.raw_exchange
    sent = []

    def revoke_before_send(name, value, authorize, **options):
        store.revoke(principal.name)
        def send_if_authorized():
            authorize()
            sent.append(name)
        send_if_authorized()
        return original(name, value, authorize, **options)

    monkeypatch.setattr(executor.devices, 'raw_exchange', revoke_before_send)
    with pytest.raises((ValueError, PermissionError)):
        executor.gateway('raw', {'command': 'query_status', 'args': {}},
                         principal=replace(principal, check_current=True))
    assert sent == []


@pytest.mark.parametrize('jump', ['forward', 'backward'])
def test_raw_read_checks_both_clocks_at_final_send(gateway, tmp_path, monkeypatch, jump):
    executor, _, _ = gateway
    wall, boot = [1_800_000_000.0], [100.0]
    monkeypatch.setattr(module_tokens, 'time', lambda: wall[0])
    monkeypatch.setattr(module_tokens, 'clock_gettime', lambda _: boot[0])
    store = KeyStore(tmp_path / 'keys.json')
    grant, bearer = store.add('diagnostics', ['read', 'raw'])
    control_handler(executor, 'x' * 32, store)
    principal = replace(store.authenticate(bearer), check_current=True)
    original = executor.devices.raw_exchange
    sent = []

    def cross_deadline(name, value, authorize, *, frame_authority):
        with frame_authority():
            if jump == 'forward':
                wall[0] = grant.raw_until
            else:
                wall[0] -= 3600
                boot[0] = grant.raw_deadline
            authorize(check_grant=False)
            sent.append(name)
            return original(name, value, authorize)

    monkeypatch.setattr(executor.devices, 'raw_exchange', cross_deadline)
    with pytest.raises(ValueError, match='raw scope has expired'):
        executor.gateway('raw', {'command': 'query_status', 'args': {}}, principal=principal)
    assert sent == []
    wall[0] = 1_800_000_000.0
    assert store.authenticate(bearer).scopes == {'read'}


@pytest.fixture
def track_gateway(tmp_path, monkeypatch, request):
    from time import time
    from anodizing_web.executor import Executor, FakeDevices
    from test_executor_phase1 import Clock
    from test_executor_module_gateway import module_confirmation

    monkeypatch.setenv('ANODIZING_MANUAL_TRAVEL_UNTIL', str(time() + 3600))
    monkeypatch.setenv('ANODIZING_MANUAL_TRAVEL_ISOLATED_TRACK', '1')
    monkeypatch.setenv('ANODIZING_MODULE_TRAVEL_ON_ATTESTED_TRACK', request.param)
    executor = Executor(tmp_path, FakeDevices(), clock=Clock(), gateway_enabled=True)
    identity = dict(boot=executor.boot, protocol=executor.health()['protocol'], session='katana-test')
    try:
        executor.gateway('heartbeat', dict(identity, sequence=1))
        executor.gateway('connect', identity)
        dev, _ = KeyStore(tmp_path / 'keys.json').add('diagnostics', ['raw', 'jobs', 'read'])
        claim = executor.gateway('claim', principal=dev)
        executor.gateway('arm', dict(identity, stop_generation=claim['stop_generation'],
                                     module_attestation=module_confirmation(executor, claim)))
        executor.devices.agv.station = 1
        executor.devices.last_confirmed_station = (1, '2026-11-27T00:00:00Z')
        executor.devices.agv.speed = 500
        yield executor, dev, dict(lease_id=claim['lease_id'], command='call_station', args={'value': 2})
    finally:
        executor.close()


@pytest.mark.parametrize('track_gateway', ['0', '1'], indirect=True)
def test_raw_isolated_track_requires_explicit_opt_in(track_gateway, request):
    executor, principal, body = track_gateway
    if request.node.callspec.params['track_gateway'] == '1':
        assert executor.gateway('raw', body, principal=principal)['decoded']['current_station'] == 2
        assert executor.devices.effects == [('station', 2)]
    else:
        with pytest.raises(ValueError):
            executor.gateway('raw', body, principal=principal)
        assert executor.devices.effects == []


@pytest.mark.parametrize('track_gateway', ['1'], indirect=True)
@pytest.mark.parametrize('condition', ['window', 'battery'])
def test_raw_opt_in_still_requires_window_and_battery(track_gateway, monkeypatch, condition):
    executor, principal, body = track_gateway
    if condition == 'window':
        executor.manual_travel_until = 0
    else:
        status = executor.devices.status
        def low_battery():
            state = status()
            state['status']['battery_percent'] = 0
            return state
        monkeypatch.setattr(executor.devices, 'status', low_battery)
    with pytest.raises(ValueError):
        executor.gateway('raw', body, principal=principal)
    assert executor.devices.effects == []


@pytest.mark.parametrize('track_gateway', ['1'], indirect=True)
@pytest.mark.parametrize('change', ['owner', 'lease', 'generation', 'arm'])
def test_raw_final_send_rechecks_authority(track_gateway, monkeypatch, change):
    executor, principal, body = track_gateway
    original = executor.devices.raw_motion
    reached = []
    def change_before_send(station, authorize, monitor, **options):
        reached.append(True)
        if change == 'owner':
            executor.gateway.ownership.owner = 'another-module'
        elif change == 'lease':
            executor.gateway.ownership.lease = None
        elif change == 'generation':
            executor.stop_generation = next(executor.generations)
        else:
            executor.module_attestation = None
        return original(station, authorize, monitor, **options)
    monkeypatch.setattr(executor.devices, 'raw_motion', change_before_send)
    with pytest.raises(ValueError):
        executor.gateway('raw', body, principal=principal)
    assert reached == [True]
    assert executor.devices.effects == []


@pytest.mark.parametrize('violation', ['speed', 'distance'])
def test_wired_raw_monitor_halts_without_waiting_for_registry(tmp_path, monkeypatch, violation):
    import json
    import time
    from threading import Event, Thread
    from types import SimpleNamespace
    from anodizing_web import agv
    from anodizing_web.executor import Executor
    from anodizing_web.executor_http import WiredDevices
    from test_executor_module_gateway import module_confirmation
    from test_executor_phase1 import Clock

    monkeypatch.setenv('ANODIZING_MANUAL_TRAVEL_UNTIL', str(time.time() + 3600))
    monkeypatch.setenv('ANODIZING_MANUAL_TRAVEL_ISOLATED_TRACK', '1')
    monkeypatch.setenv('ANODIZING_MODULE_TRAVEL_ON_ATTESTED_TRACK', '1')
    monkeypatch.setattr(agv, 'TIMED_REPORT_LISTEN_S', 0)
    monkeypatch.setattr(agv, 'RECIPE_POLL_S', .05)
    prefix = bytes.fromhex('42 19 0D')
    departed, inject, halted, release = (Event() for _ in range(4))

    class Wire(agv.Simulator):
        def __init__(self, **_):
            super().__init__(prefix)
            self.arrival = True
            self.speed = 500

        def write(self, raw):
            frame = agv.parse_frame(raw)
            if frame.command == 0x23 and frame.data[0] == 0x9D:
                result = super().write(prefix + raw) - len(prefix)
                self.arrives_at = None
                departed.set()
                return result
            if frame.command == 0x23 and frame.data[0] in (0x87, 0x5B):
                halted.set()
            return super().write(prefix + raw) - len(prefix)

        def _report(self):
            before = len(self.buffer)
            super()._report()
            if inject.is_set():
                frame = agv.parse_frame(self.buffer[before:])
                data = bytearray(frame.data)
                if violation == 'speed':
                    data[8:10] = (501).to_bytes(2, 'little', signed=True)
                else:
                    data[16:20] = (562).to_bytes(4, 'little')
                self.buffer = self.buffer[:before] + agv.Frame(frame.command, bytes(data), frame.prefix).encode()

    (tmp_path / 'site-profile.json').write_text(json.dumps({'agv': {'wired_port': '/dev/test-wire'}}))
    monkeypatch.setattr(agv, 'serial', SimpleNamespace(Serial=Wire))
    devices = WiredDevices(tmp_path, None)
    executor = Executor(tmp_path, devices, clock=Clock(), gateway_enabled=True)
    store = KeyStore(tmp_path / 'keys.json')
    principal, _ = store.add('diagnostics', ['raw', 'jobs', 'read'])
    control_handler(executor, 'x' * 32, store)
    principal = replace(principal, check_current=True)
    original = store.current_grant
    def blocked_grant(principal):
        if departed.is_set():
            release.wait(5)
        return original(principal)
    monkeypatch.setattr(store, 'current_grant', blocked_grant)
    identity = dict(boot=executor.boot, protocol=executor.health()['protocol'], session='katana-test')
    errors = []
    worker = None
    try:
        executor.gateway('heartbeat', dict(identity, sequence=1))
        executor.gateway('connect', identity)
        executor.gateway('status_read', identity)
        claim = executor.gateway('claim', principal=principal)
        executor.gateway('arm', dict(identity, stop_generation=claim['stop_generation'],
                                     module_attestation=module_confirmation(executor, claim)))
        body = dict(lease_id=claim['lease_id'], command='call_station', args={'value': 2})
        def travel():
            try:
                executor.gateway('raw', body, principal=principal)
            except Exception as exc:
                errors.append(exc)
        worker = Thread(target=travel)
        worker.start()
        assert departed.wait(3), errors
        inject.set()
        assert halted.wait(2), 'automatic halt waited for the registry'
        assert not release.is_set()
    finally:
        release.set()
        if worker is not None:
            worker.join(5)
        executor.close()
    assert not worker.is_alive()
