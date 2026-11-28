from dataclasses import replace
from email.message import Message
from io import BytesIO
import json
from secrets import token_urlsafe
from threading import Event, Thread
from types import SimpleNamespace

import pytest

from anodizing_web import agv
from anodizing_web.executor import Executor, FakeDevices
from anodizing_web.executor_http import control_handler
from anodizing_web.job_gateway import GatewayForbidden
from anodizing_web.module_tokens import KeyStore

from test_executor_module_gateway import gateway, module_confirmation
from test_executor_phase1 import visit, wait_for

SIMULATOR_SPEED = agv.SIMULATOR_SPEED


def test_raw_expiry_only_removes_raw_access(tmp_path, monkeypatch):
    from anodizing_web import module_tokens

    now = 1_800_000_000
    monkeypatch.setattr(module_tokens, 'time', lambda: now)
    monkeypatch.setattr(module_tokens, 'clock_gettime', lambda _: now)
    store = KeyStore(tmp_path / 'keys.json')
    _, key = store.add('diagnostics', ['read'])
    principal = store.grant('diagnostics', ['raw'], expires=1)
    assert principal.raw_until == now + 3600
    assert store.list() == [principal]
    assert store.authenticate(key).scopes == {'read', 'raw'}
    now += 3600
    assert store.authenticate(key).scopes == {'read'}
    with pytest.raises(ValueError, match='24'):
        store.grant('diagnostics', ['raw'], expires=25)


def post(handler_class, key, path, body, *, after_read=None, server=None):
    encoded = json.dumps(body).encode()
    handler = handler_class.__new__(handler_class)
    headers = Message()
    headers['Authorization'] = 'Bearer ' + key
    headers['Content-Type'] = 'application/json'
    headers['Content-Length'] = str(len(encoded))
    handler.headers, handler.path = headers, path
    handler.server, handler.client_address = server or SimpleNamespace(), ('127.0.0.1', 12345)
    handler.allowed_host = lambda: True
    responses = []
    handler.send = lambda status, value: responses.append((status, value))

    class Body(BytesIO):
        def read(self, size=-1):
            result = super().read(size)
            if after_read is not None:
                after_read()
            return result

    handler.rfile = Body(encoded)
    handler.handle_request('POST')
    [response] = responses
    return response


def arm_module(executor, identity, claim):
    return executor.gateway('arm', dict(identity, stop_generation=claim['stop_generation'],
                                        module_attestation=module_confirmation(executor, claim)))


def test_raw_http_reads_require_raw_and_no_claim(gateway, tmp_path):
    executor, _, _ = gateway
    keys = KeyStore(tmp_path / 'keys.json')
    _, reader = keys.add('normal-client', ['read', 'jobs'])
    _, dev = keys.add('diagnostics', ['raw'])
    operator = token_urlsafe(32)
    handler_class = control_handler(executor, operator, keys)
    read = dict(command='read_speed', args={})
    for key in (reader, operator):
        assert post(handler_class, key, '/v1/raw', read)[0] == 403
    code, result = post(handler_class, dev, '/v1/raw', read)
    assert code == 200
    assert result['decoded']['read_speed_m_h'] == SIMULATOR_SPEED
    assert bytes.fromhex(result['reply_hex']).startswith(b'\xa5\x5a')
    assert post(handler_class, dev, '/v1/raw', dict(command='arbitrary_register', args={}))[0] == 409
    [activity] = executor.gateway('status')['recent_raw']
    assert activity['client_name'] == 'diagnostics'
    assert activity['token_id'] == keys.list()[0].token_id
    [journal] = executor.journal.glob('*.json')
    wire = json.loads(journal.read_text())['raw_activity']['wire']
    assert {row['direction'] for row in wire} == {'TX', 'RX'}
    assert all(row['token_id'] == activity['token_id'] and row['utc'] for row in wire)


def test_speed_snapshot_readback_and_exact_undo(gateway, tmp_path):
    executor, _, _ = gateway
    keys = KeyStore(tmp_path / 'keys.json')
    dev, _ = keys.add('diagnostics', ['raw', 'jobs', 'read'])
    claimed = executor.gateway('claim', principal=dev)
    lease = dict(lease_id=claimed['lease_id'])
    result = executor.gateway('raw', dict(lease, command='set_speed', args={'value': 500}), principal=dev)
    assert result['snapshot']['before'] == SIMULATOR_SPEED
    assert result['snapshot']['after'] == result['decoded']['read_speed_m_h'] == 500
    with pytest.raises(ValueError, match='increase'):
        executor.gateway('raw', dict(lease, command='set_speed', args={'value': 501}), principal=dev)
    undo = executor.gateway('raw/undo', dict(lease, snapshot_id=result['snapshot']['id']), principal=dev)
    assert undo['decoded']['read_speed_m_h'] == SIMULATOR_SPEED
    executor.gateway('release', lease, principal=dev)
    assert 'speed' in executor.gateway('status')['operator_check']['reason']


@pytest.mark.parametrize(('command', 'args', 'reason'), [
    ('forward', {}, 'bounded distance'),
    ('auto_start', {}, 'automatic restart'),
    ('obstacle_sensing', {}, 'obstacle'),
    ('bumper', {}, 'bumper'),
    ('e_stop', {}, 'emergency-stop'),
    ('speed_limit', {}, 'speed limits'),
    ('set_speed', {'value': 499}, '500'),
    ('set_speed', {'value': 4001}, '4000'),
    ('call_station', {'value': True}, 'integer'),
    ('call_station', {'value': 256}, '255'),
    ('query_status', {'bytes': '00'}, 'arguments'),
])
def test_raw_wire_allowlist_rejects_before_transmit(gateway, tmp_path, command, args, reason):
    executor, _, _ = gateway
    dev, _ = KeyStore(tmp_path / 'keys.json').add('diagnostics', ['raw'])
    with pytest.raises(ValueError, match=reason):
        executor.gateway('raw', dict(command=command, args=args), principal=dev)
    assert executor.gateway('status')['recent_raw'] == []


def test_raw_motion_requires_lease_arm_and_caps(gateway, tmp_path):
    executor, identity, _ = gateway
    dev, _ = KeyStore(tmp_path / 'keys.json').add('diagnostics', ['raw', 'jobs', 'read'])
    other, _ = KeyStore(tmp_path / 'other.json').add('other', ['raw', 'jobs'])
    claim = executor.gateway('claim', principal=dev)
    lease = {'lease_id': claim['lease_id']}
    request = dict(lease, command='call_station', args={'value': 2})
    with pytest.raises(ValueError, match='lease'):
        executor.gateway('raw', request, principal=other)
    with pytest.raises(ValueError, match='Arm'):
        executor.gateway('raw', request, principal=dev)
    arm_module(executor, identity, claim)
    with pytest.raises(ValueError, match='Disarm'):
        executor.gateway('raw', dict(lease, command='set_speed', args={'value': 500}), principal=dev)

    devices = executor.devices
    devices.agv.station = 1
    devices.last_confirmed_station = (1, agv.timestamp())
    with pytest.raises(ValueError, match='speed cap'):
        executor.gateway('raw', request, principal=dev)
    devices.agv.speed = 500
    with pytest.raises(ValueError, match='adjacent'):
        executor.gateway('raw', dict(request, args={'value': 3}), principal=dev)
    executor.isolated_track = True
    with pytest.raises(ValueError, match='isolated track'):
        executor.gateway('raw', request, principal=dev)
    executor.isolated_track = False
    devices.fresh = False
    with pytest.raises(ValueError, match='fresh'):
        executor.gateway('raw', request, principal=dev)
    devices.fresh = True
    assert executor.gateway('raw', request, principal=dev)['decoded']['current_station'] == 2
    assert devices.effects == [('station', 2)]


def test_read_refuses_recipe_stop_and_operator_check(gateway, tmp_path):
    executor, _, _ = gateway
    dev, _ = KeyStore(tmp_path / 'keys.json').add('diagnostics', ['raw'])
    request = dict(command='query_status', args={})
    executor.gateway.change_owner('main-app-loop')
    with pytest.raises(ValueError, match='owns control'):
        executor.gateway('raw', request, principal=dev)
    executor.gateway.change_owner(None)
    executor.devices.run_id = 'fake-retained-run'
    with pytest.raises(ValueError, match='active job or recipe'):
        executor.gateway('raw', request, principal=dev)
    executor.devices.run_id = None
    executor.stop_result = {'confirmed': False, 'halt_attempted': False}
    with pytest.raises(ValueError, match='Stop or Operator check'):
        executor.gateway('raw', request, principal=dev)
    executor.stop_result = None
    executor._hold('Review the vehicle.', facts={'idle': True, 'manual': True})
    with pytest.raises(ValueError, match='Operator check'):
        executor.gateway('raw', request, principal=dev)


def test_normal_job_reads_status_after_unowned_raw_read(gateway, tmp_path, monkeypatch):
    executor, identity, _ = gateway
    dev, _ = KeyStore(tmp_path / 'keys.json').add('diagnostics', ['raw', 'jobs'])
    executor.gateway('raw', dict(command='query_status', args={}), principal=dev)
    reads = []
    original = executor.devices.read

    def read():
        reads.append(None)
        return original()

    monkeypatch.setattr(executor.devices, 'read', read)
    claim = executor.gateway('claim', principal=dev)
    armed = arm_module(executor, identity, claim)
    envelope = visit(dict(lease_id=claim['lease_id'], stop_generation=armed['stop_generation']))
    executor.gateway('submit', envelope, principal=dev)
    wait_for(lambda: executor.gateway('status', {'job_id': envelope['job_id']})['job'],
             lambda job: job is not None and job['state'] == 'done')
    assert len(reads) == 1


def test_unverified_speed_readback_retains_snapshot(gateway, tmp_path, monkeypatch):
    executor, _, _ = gateway
    dev, _ = KeyStore(tmp_path / 'keys.json').add('diagnostics', ['raw', 'jobs'])
    lease = executor.gateway('claim', principal=dev)['lease_id']
    exchange = executor.devices.raw_exchange

    def unchanged(name, value, authorize, **options):
        if name == 'set_speed':
            assert any('raw_snapshot' in json.loads(path.read_text()) for path in executor.journal.glob('*.json'))
            return None
        return exchange(name, value, authorize, **options)

    monkeypatch.setattr(executor.devices, 'raw_exchange', unchanged)
    with pytest.raises(ValueError, match='read-back'):
        executor.gateway('raw', dict(lease_id=lease, command='set_speed', args={'value': 500}), principal=dev)
    status = executor.gateway('status')
    assert status['raw_settings'][0]['outcome'] == 'unverified'
    assert 'snapshot ID' in status['operator_check']['reason']


def test_snapshot_and_operator_check_survive_restart(tmp_path):
    dev, _ = KeyStore(tmp_path / 'keys.json').add('diagnostics', ['raw', 'jobs'])
    executor = Executor(tmp_path, FakeDevices.configured(tmp_path), gateway_enabled=True)
    try:
        executor.devices.connect()
        claim = executor.gateway('claim', principal=dev)
        body = dict(lease_id=claim['lease_id'], command='set_speed', args={'value': 500})
        snapshot = executor.gateway('raw', body, principal=dev)['snapshot']
    finally:
        executor.close()
    restarted = Executor(tmp_path, FakeDevices.configured(tmp_path), gateway_enabled=True)
    try:
        status = restarted.gateway('status')
        assert not status['armed']
        assert status['raw_settings'][0]['id'] == snapshot['id']
        assert status['raw_settings'][0]['outcome'] == 'verified'
        assert snapshot['id'] in status['operator_check']['reason']
        assert restarted.devices.agv.speed == 500
    finally:
        restarted.close()


def test_raw_expiry_releases_the_raw_lease(gateway, tmp_path, monkeypatch):
    from anodizing_web import module_tokens

    executor, _, _ = gateway
    dev, _ = KeyStore(tmp_path / 'keys.json').add('diagnostics', ['raw', 'jobs'])
    executor.gateway('claim', principal=dev)
    executor.gateway('raw', {'command': 'query_status', 'args': {}}, principal=dev)
    monkeypatch.setattr(module_tokens, 'clock_gettime', lambda _: dev.raw_deadline)
    state = wait_for(lambda: executor.gateway('status'), lambda value: value['owner'] is None)
    assert state['stop']['received']
    assert 'scope expired' in state['operator_check']['reason']


@pytest.mark.parametrize('path,body', [
    ('/v1/raw', {'command': 'query_status', 'args': {}}),
    ('/v1/raw/undo', {'lease_id': 'test-lease', 'snapshot_id': 'test-snapshot'}),
])
def test_revoked_grant_after_raw_body_is_refused(gateway, tmp_path, path, body):
    executor, _, _ = gateway
    keys = KeyStore(tmp_path / 'keys.json')
    dev, bearer = keys.add('diagnostics', ['raw'])
    handler_class = control_handler(executor, token_urlsafe(32), keys)
    before = list(executor.journal.glob('*.json'))
    code, _ = post(handler_class, bearer, path, body, after_read=lambda: keys.revoke(dev.name))
    assert code == 403
    assert list(executor.journal.glob('*.json')) == before


def test_raw_grant_is_rechecked_at_admission(gateway, tmp_path):
    executor, _, _ = gateway
    keys = KeyStore(tmp_path / 'keys.json')
    dev, _ = keys.add('diagnostics', ['raw'])
    control_handler(executor, token_urlsafe(32), keys)
    keys.revoke(dev.name)
    with pytest.raises(GatewayForbidden, match='grant changed'):
        executor.gateway('raw', {'command': 'query_status', 'args': {}},
                         principal=replace(dev, check_current=True))
    assert executor.gateway('status')['recent_raw'] == []


@pytest.mark.parametrize('field', ['owner', 'lease_id', 'boot'])
def test_raw_motion_refuses_confirmation_bound_elsewhere(gateway, tmp_path, field):
    executor, identity, _ = gateway
    dev, _ = KeyStore(tmp_path / 'keys.json').add('diagnostics', ['raw', 'jobs'])
    claim = executor.gateway('claim', principal=dev)
    arm_module(executor, identity, claim)
    executor.devices.agv.station = 1
    executor.devices.last_confirmed_station = (1, agv.timestamp())
    executor.devices.agv.speed = 500
    record, deadline = executor.module_attestation
    executor.module_attestation = dict(record, **{field: token_urlsafe(18)}), deadline
    request = dict(lease_id=claim['lease_id'], command='call_station', args={'value': 2})
    with pytest.raises(ValueError, match='owner or lease changed'):
        executor.gateway('raw', request, principal=dev)
    assert executor.devices.effects == []


def test_raw_mutations_without_read_return_minimal_receipts(gateway, tmp_path):
    executor, _, _ = gateway
    dev, _ = KeyStore(tmp_path / 'keys.json').add('diagnostics', ['raw', 'jobs'])
    lease_id = executor.gateway('claim', principal=dev)['lease_id']
    request = dict(lease_id=lease_id, command='set_speed', args={'value': 500})
    receipt = executor.gateway('raw', request, principal=dev)
    assert set(receipt) == {'id', 'verified', 'snapshot'}
    assert receipt['verified'] is True
    assert receipt['snapshot'] == {'id': receipt['id']}
    undo = executor.gateway('raw/undo', dict(lease_id=lease_id, snapshot_id=receipt['id']), principal=dev)
    assert set(undo) == {'id', 'verified', 'snapshot'}
    assert undo['verified'] is True
    assert undo['snapshot'] == {'id': undo['id']}
    assert executor.devices.agv.speed == SIMULATOR_SPEED


def test_raw_refusal_records_contain_only_validated_metadata(gateway, tmp_path):
    executor, _, _ = gateway
    dev, _ = KeyStore(tmp_path / 'keys.json').add('diagnostics', ['raw', 'jobs'])
    lease_id = executor.gateway('claim', principal=dev)['lease_id']
    invalid = dict(lease_id=lease_id, command='set_speed', args={'value': 'synthetic-unvalidated'})
    before = set(executor.journal.glob('*.json'))
    with pytest.raises(ValueError):
        executor.gateway('raw', invalid, principal=dev)
    assert set(executor.journal.glob('*.json')) == before

    valid = dict(lease_id=lease_id, command='call_station', args={'value': 2})
    with pytest.raises(ValueError, match='Arm'):
        executor.gateway('raw', valid, principal=dev)
    [refusal] = [path.read_text() for path in set(executor.journal.glob('*.json')) - before]
    assert executor._digest(valid) in refusal
    assert dev.name in refusal and dev.token_id in refusal
    assert 'call_station' in refusal and 'Arm' in refusal
    assert lease_id not in refusal
    assert '"args"' not in refusal


def test_limited_raw_traffic_keeps_operator_stop_available(gateway, tmp_path):
    executor, _, _ = gateway
    keys = KeyStore(tmp_path / 'keys.json')
    _, bearer = keys.add('diagnostics', ['raw'])
    operator = token_urlsafe(32)
    handler_class = control_handler(executor, operator, keys)
    server = SimpleNamespace(key_limited=lambda _: True, reject_key=lambda _: 429)
    raw = dict(command='stop', args={})
    assert post(handler_class, bearer, '/v1/raw', raw, server=server)[0] == 429
    status, receipt = post(handler_class, operator, '/v1/stop', {}, server=server)
    assert status == 200
    assert receipt['received'] is True


def test_raw_attestation_rechecked_at_dispatch(gateway, tmp_path, monkeypatch):
    executor, identity, clock = gateway
    dev, _ = KeyStore(tmp_path / 'keys.json').add('diagnostics', ['raw', 'jobs'])
    claim = executor.gateway('claim', principal=dev)
    arm_module(executor, identity, claim)
    executor.devices.agv.station = 1
    executor.devices.last_confirmed_station = (1, agv.timestamp())
    executor.devices.agv.speed = 500
    dispatch = executor.devices.raw_motion

    def expiry_before_dispatch(station, authorize, monitor, **options):
        with executor.guard:
            record, _ = executor.module_attestation
            executor.module_attestation = record, clock()
            authorize(dispatch=True)
            return dispatch(station, authorize, monitor, **options)

    monkeypatch.setattr(executor.devices, 'raw_motion', expiry_before_dispatch)
    body = dict(lease_id=claim['lease_id'], command='call_station', args={'value': 2})
    with pytest.raises(ValueError, match='expir'):
        executor.gateway('raw', body, principal=dev)
    assert executor.devices.effects == []


def test_operator_stop_halts_while_raw_read_is_pending(gateway, tmp_path, monkeypatch):
    executor, _, _ = gateway
    keys = KeyStore(tmp_path / 'keys.json')
    _, bearer = keys.add('diagnostics', ['raw'])
    operator = token_urlsafe(32)
    handler_class = control_handler(executor, operator, keys)
    entered, release, halted = Event(), Event(), Event()
    raw_exchange, stop = executor.devices.raw_exchange, executor.devices.stop
    replies = []

    def blocked_read(name, value, authorize, **options):
        entered.set()
        assert release.wait(3)
        return raw_exchange(name, value, authorize, **options)

    def observe_halt():
        result = stop()
        halted.set()
        return result

    monkeypatch.setattr(executor.devices, 'raw_exchange', blocked_read)
    monkeypatch.setattr(executor.devices, 'stop', observe_halt)
    worker = Thread(target=lambda: replies.append(post(handler_class, bearer, '/v1/raw',
                                                       dict(command='query_status', args={}))))
    worker.start()
    try:
        assert entered.wait(1)
        status, receipt = post(handler_class, operator, '/v1/stop', {})
        assert status == 200 and receipt['received'] is True
        assert halted.wait(1)
    finally:
        release.set()
        worker.join(3)
    assert [reply[0] for reply in replies] == [409]
