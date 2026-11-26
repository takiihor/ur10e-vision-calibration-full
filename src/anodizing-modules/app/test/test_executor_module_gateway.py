"""Client authority and key handling with simulated devices only."""
import json
import secrets
from hashlib import sha256
import sys
from threading import Event, Thread
from time import time
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

from anodizing_web.executor import AdmissionRefused, Executor, FakeDevices
from anodizing_web.executor_http import Client, control_listener

from test_executor_phase1 import Clock, visit, wait_for


@pytest.fixture
def gateway(tmp_path):
    clock = Clock()
    executor = Executor(tmp_path, FakeDevices(), clock=clock, gateway_enabled=True)
    identity = dict(boot=executor.boot, protocol=executor.health()['protocol'], session='katana-test')
    try:
        executor.gateway('heartbeat', dict(identity, sequence=1))
        executor.gateway('connect', identity)
        yield executor, identity, clock
    finally:
        executor.close()


def principal(name='station-script', token_id='test-token-id', scopes=('read', 'jobs')):
    from anodizing_web.module_tokens import ModulePrincipal

    return ModulePrincipal(name, token_id, frozenset(scopes))


def test_owner_handoff_disarms_and_fences_old_generation(gateway):
    executor, identity, _ = gateway
    page = executor.gateway('claim', identity)
    executor.gateway('arm', dict(identity, stop_generation=page['stop_generation']))
    with pytest.raises(ValueError, match='agv-page'):
        executor.gateway('claim', principal=principal())
    released = executor.gateway('release', identity)
    assert released['owner'] is None
    assert not released['armed'] and not executor.devices.status()['armed']
    claim = executor.gateway('claim', principal=principal())
    assert claim['owner'] == 'station-script' and not claim['armed']
    assert claim['stop_generation'] > page['stop_generation']
    new_arm = executor.gateway('arm', dict(identity, stop_generation=claim['stop_generation']))
    assert new_arm['armed_for'] == 'station-script'
    with pytest.raises(ValueError, match='current owner'):
        executor.gateway('submit', visit(dict(identity, stop_generation=new_arm['stop_generation'])))


def test_module_job_auto_admission_idempotency_and_own_records(gateway):
    executor, identity, _ = gateway
    module = principal()
    claim = executor.gateway('claim', principal=module)
    current = executor.gateway('arm', dict(identity, stop_generation=claim['stop_generation']))
    job = visit(dict(lease_id=claim['lease_id'], stop_generation=current['stop_generation']))
    executor.gateway('submit', job, principal=module)
    done = wait_for(lambda: executor.gateway('status', {'job_id': job['job_id']}, principal=module)['job'],
                    lambda result: result['state'] == 'done')
    assert done['client_name'] == module.name and done['token_id'] == module.token_id
    assert executor.gateway('submit', job, principal=module) == done
    other = principal('another-module', 'another-key-id')
    assert executor.gateway('jobs', principal=other) == {'jobs': []}
    assert executor.gateway('status', {'job_id': job['job_id']}, principal=other)['job'] is None
    assert executor.gateway('jobs')['jobs'] == [done]
    assert executor.gateway('status')['latest_manual_job_id'] is None
    assert executor.gateway('status')['recent_jobs'][0]['token_id'] == module.token_id
    assert executor.devices.effects == [('station', 2)]


@pytest.mark.parametrize('station,reserve,reason', [(250, None, 'learned range'), (2, 100, 'battery')])
def test_module_station_checks_learned_range_and_battery_floor(gateway, station, reserve, reason):
    executor, identity, _ = gateway
    module = principal()
    claimed = executor.gateway('claim', principal=module)
    current = executor.gateway('arm', dict(identity, stop_generation=claimed['stop_generation']))
    if reserve is not None:
        executor.devices.reserve_s = reserve
    with pytest.raises(AdmissionRefused, match=reason):
        executor.gateway('submit', visit(dict(lease_id=claimed['lease_id'], stop_generation=current['stop_generation']),
                                         station=station), principal=module)
    assert executor.devices.effects == []


def test_isolated_track_never_admits_a_module_without_local_confirmation(tmp_path, monkeypatch):
    class IsolatedTrackDouble(FakeDevices):
        simulated = False

        def configure_manual_travel(self, expires, battery_floor, isolated_track):
            assert isolated_track

    monkeypatch.setenv('ANODIZING_MANUAL_TRAVEL_UNTIL', str(time() + 60))
    monkeypatch.setenv('ANODIZING_MANUAL_TRAVEL_ISOLATED_TRACK', '1')
    devices = IsolatedTrackDouble()
    executor = Executor(tmp_path, devices, gateway_enabled=True)
    try:
        identity = dict(boot=executor.boot, protocol=executor.health()['protocol'], session='katana-test')
        executor.gateway('heartbeat', dict(identity, sequence=1))
        executor.gateway('connect', identity)
        module = principal()
        claim = executor.gateway('claim', principal=module)
        current = executor.gateway('arm', dict(identity, stop_generation=claim['stop_generation']))
        job = visit(dict(lease_id=claim['lease_id'], stop_generation=current['stop_generation']))
        with pytest.raises(AdmissionRefused, match='per-call confirmation'):
            executor.gateway('submit', job, principal=module)
        assert executor.gateway('status', {'job_id': job['job_id']}, principal=module)['job']['state'] == 'refused'
        assert devices.effects == []
    finally:
        executor.close()


def test_module_lease_expires_at_ten_seconds_even_when_idle(gateway):
    executor, _, clock = gateway
    module = principal()
    claim = executor.gateway('claim', principal=module)
    clock.now += 9
    assert executor.gateway('heartbeat', {'lease_id': claim['lease_id'], 'sequence': 0}, principal=module)['lease_valid']
    clock.now += 9
    assert executor.gateway('heartbeat', {'lease_id': claim['lease_id'], 'sequence': 0}, principal=module)['lease_valid']
    clock.now += 1
    result = wait_for(lambda: executor.gateway('status'), lambda result: result['owner'] is None)
    assert result['stop']['received'] and not result['armed']
    assert 'heartbeat' in result['operator_check']['reason']
    with pytest.raises(ValueError, match='Claim'):
        executor.gateway('heartbeat', {'lease_id': claim['lease_id'], 'sequence': 1}, principal=module)


def test_module_scopes_keep_controls_and_foreign_attestation_out(gateway):
    executor, _, _ = gateway
    reader = principal(scopes=('read',))
    assert executor.gateway('jobs', principal=reader) == {'jobs': []}
    with pytest.raises(ValueError, match='jobs scope'):
        executor.gateway('claim', principal=reader)
    writer = principal(scopes=('jobs',))
    with pytest.raises(ValueError, match='read scope'):
        executor.gateway('status', principal=writer)
    with pytest.raises(ValueError, match='operator credential'):
        executor.gateway('arm', principal=writer)
    with pytest.raises(ValueError, match='unsupported field'):
        executor.gateway('submit', {'attestation': {'ready': True}}, principal=writer)
    assert executor.gateway('stop', principal=reader)['received']


def test_calibration_owner_cannot_arm_or_disarm(gateway):
    executor, identity, _ = gateway
    selected = executor.gateway('claim', identity, owner='calibration')
    reviewed = dict(identity, stop_generation=selected['stop_generation'])
    with pytest.raises(ValueError, match='AGV page'):
        executor.gateway('arm', reviewed, owner='calibration')
    assert executor.gateway('arm', reviewed)['armed_for'] == 'calibration'
    with pytest.raises(ValueError, match='AGV page'):
        executor.gateway('disarm', identity, owner='calibration')


def test_coincident_controller_and_module_expiry_stops_once(gateway):
    executor, identity, clock = gateway
    module = principal()
    claimed = executor.gateway('claim', principal=module)
    clock.now += 7
    executor.gateway('heartbeat', dict(identity, sequence=2))
    armed = executor.gateway('arm', dict(identity, stop_generation=claimed['stop_generation']))
    entered, release = Event(), Event()
    perform = executor.devices.perform
    stops = []
    stop = executor.stop

    def blocked(effect, value):
        entered.set()
        assert release.wait(2)
        return perform(effect, value)

    def count_stops(body=None):
        stops.append(body)
        return stop(body)

    executor.devices.perform = blocked
    executor.stop = count_stops
    try:
        executor.gateway('submit', visit(dict(lease_id=claimed['lease_id'], stop_generation=armed['stop_generation'])),
                         principal=module)
        assert entered.wait(1)
        clock.now += 3
        state = wait_for(lambda: executor.gateway('status'), lambda state: state['owner'] is None)
        assert not state['lease_valid']
        assert 'module heartbeat' in state['operator_check']['reason']
        assert len(stops) == 1
    finally:
        release.set()


def test_module_key_store_holds_only_digests_and_revokes_without_restart(tmp_path):
    from anodizing_web.module_tokens import KeyStore

    path = tmp_path / 'keys.json'
    store = KeyStore(path)
    assert store.authenticate('no-module-key') is None
    record, key = store.add('inspection', ['read', 'jobs'])
    assert store.authenticate(key) == record
    assert store.authenticate(key + 'x') is None
    assert key not in path.read_text()
    assert not {'key', 'secret'} & set(json.loads(path.read_text())['inspection'])
    assert path.stat().st_mode & 0o777 == 0o600
    assert store.list() == [record]
    KeyStore(path).revoke(record.name)
    assert store.authenticate(key) is None


def test_admin_distributes_key_and_pin_without_printing_secret(tmp_path, monkeypatch, capsys):
    from anodizing_web.module_tokens import KeyStore, main

    certificate = b'IPC certificate fixture\n'
    (tmp_path / 'module-api-cert.pem').write_bytes(certificate)
    output = tmp_path / 'delivery'
    monkeypatch.setattr(sys, 'argv', ['anodizing-module-token', '--data-dir', str(tmp_path),
                                      'add', 'inspection', '--scope', 'read', '--output-dir', str(output)])
    main()
    key_file = output / 'module.key'
    key = key_file.read_text().strip()
    assert key_file.stat().st_mode & 0o777 == 0o600
    assert output.stat().st_mode & 0o777 == 0o700
    assert (output / 'ipc-cert.pem').read_bytes() == certificate
    tokens = KeyStore(tmp_path / 'module-keys.json')
    assert tokens.authenticate(key).scopes == {'read'}
    assert key not in capsys.readouterr().out


def test_http_key_scopes_operator_digest_and_revocation(gateway, tmp_path):
    from anodizing_web.job_client import calibration_key
    from anodizing_web.module_tokens import KeyStore

    executor, identity, _ = gateway
    token = secrets.token_urlsafe(32)
    tokens = KeyStore(tmp_path / 'keys.json')
    _, reader = tokens.add('reader', ['read'])
    calibration = calibration_key(token)
    hashes = f'sha256:{sha256(token.encode()).hexdigest()}\ncalibration-sha256:{sha256(calibration.encode()).hexdigest()}'
    with control_listener(0, executor, hashes, tokens) as server:
        worker = Thread(target=server.serve_forever, daemon=True)
        worker.start()
        try:
            def request(path, key=None, body=None):
                headers = {'Authorization': f'Bearer {key}'} if key else {}
                if body is not None:
                    headers['Content-Type'] = 'application/json'
                payload = None if body is None else json.dumps(body).encode()
                with urlopen(Request(f'http://127.0.0.1:{server.server_port}/v1/{path}', payload, headers),
                             timeout=1) as reply:
                    return json.load(reply)

            assert request('jobs', reader) == {'jobs': []}
            with pytest.raises(HTTPError) as bad_scope:
                request('claim', reader, {})
            assert bad_scope.value.code == 403
            with pytest.raises(HTTPError) as anonymous:
                request('stop', body={})
            assert anonymous.value.code == 403
            assert request('stop', reader, {})['received']
            def selected_claim(key, owner):
                headers = {'Authorization': f'Bearer {key}', 'Content-Type': 'application/json', 'X-Client-Owner': owner}
                url = f'http://127.0.0.1:{server.server_port}/v1/claim'
                with urlopen(Request(url, json.dumps(identity).encode(), headers), timeout=1) as reply:
                    return json.load(reply)

            with pytest.raises(HTTPError) as forged:
                selected_claim(token, 'calibration')
            assert forged.value.code == 403
            assert selected_claim(calibration, 'calibration')['owner'] == 'calibration'
            with pytest.raises(HTTPError) as bad_arm:
                request('arm', calibration, identity)
            assert bad_arm.value.code == 403
            stop_headers = {'Authorization': f'Bearer {token}', 'Content-Type': 'application/json',
                            'X-Client-Owner': 'calibration'}
            stop_url = f'http://127.0.0.1:{server.server_port}/v1/stop'
            with urlopen(Request(stop_url, b'{}', stop_headers), timeout=1) as reply:
                assert json.load(reply)['received']
            tokens.revoke('reader')
            with pytest.raises(HTTPError) as revoked:
                request('jobs', reader)
            assert revoked.value.code == 403
        finally:
            server.shutdown()
            worker.join()


def test_module_stop_ingress_does_not_wait_for_admission_or_device_halt(gateway):
    executor, _, _ = gateway
    received, halt, allow_halt = Event(), Event(), Event()
    stop = executor.devices.stop

    def blocked_halt():
        halt.set()
        assert allow_halt.wait(2)
        return stop()

    def request_stop():
        if executor.gateway('stop', principal=principal(scopes=('read',)))['received']:
            received.set()

    executor.devices.stop = blocked_halt
    with executor.admission, executor.guard:
        request = Thread(target=request_stop, daemon=True)
        request.start()
        try:
            assert halt.wait(1)
            assert received.wait(1)
        finally:
            allow_halt.set()
    request.join(1)


def test_module_stop_retry_does_not_share_the_operator_stop_ticket(gateway):
    executor, _, _ = gateway
    body = {'stop_id': 'shared-stop-id'}
    module = principal()
    first = executor.gateway('stop', body, principal=module)
    retry = executor.gateway('stop', body, principal=module)
    operator = executor.gateway('stop', body, owner='main-app-loop')
    assert first['stop_id'] == retry['stop_id'] == operator['stop_id'] == body['stop_id']
    assert first['generation'] == retry['generation'] < operator['generation']


def test_executor_uses_the_job_api_gateway_and_its_owned_state(gateway):
    from anodizing_web.job_gateway import JobAPI, JobGateway
    from anodizing_web.module_tokens import KeyStore

    executor, identity, _ = gateway
    api = executor.gateway
    assert isinstance(api, JobAPI) and isinstance(api, JobGateway)
    assert isinstance(api.keys, KeyStore)
    assert api.claim(identity)['owner'] == api.ownership.owner == 'agv-page'
    api('arm', dict(identity, stop_generation=executor.generation))
    assert api.ownership.armed_for == 'agv-page'
    api.stop()
    assert api.ownership.armed_for is None


def test_calibration_key_reads_its_own_jobs_and_retries_old_key_records(gateway):
    executor, identity, _ = gateway
    page = executor.gateway('claim', identity)
    armed = executor.gateway('arm', dict(identity, stop_generation=page['stop_generation']))
    manual = visit(dict(identity, stop_generation=armed['stop_generation']), 'manual-retry-test')
    executor.gateway('submit', manual)
    wait_for(lambda: executor.status(manual['job_id'])['job'], lambda job: job['state'] == 'done')
    wait_for(lambda: executor.active is None and executor.status()['active_job_id'] is None, bool)
    executor.gateway('release', identity)
    calibration = executor.gateway('claim', identity, owner='calibration')
    armed = executor.gateway('arm', dict(identity, stop_generation=calibration['stop_generation']))
    request = visit(dict(identity, stop_generation=armed['stop_generation']), 'calibration-retry-test')
    executor.gateway('submit', request, owner='calibration')
    recorded = wait_for(lambda: executor.status(request['job_id'])['job'], lambda job: job['state'] == 'done')

    token = secrets.token_urlsafe(32)
    with control_listener(0, executor, token) as server:
        worker = Thread(target=server.serve_forever, daemon=True)
        worker.start()
        try:
            client = Client(server.server_port, token).for_owner('calibration')
            assert client('status', {'job_id': manual['job_id']})['job'] is None
            assert client('jobs')['jobs'] == [recorded]
            assert client('submit', request) == recorded
            assert executor.devices.effects == [('station', 2), ('station', 2)]
        finally:
            server.shutdown()
            worker.join()
