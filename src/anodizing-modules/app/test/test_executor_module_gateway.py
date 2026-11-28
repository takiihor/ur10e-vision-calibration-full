"""Client authority and key handling with simulated devices only."""
import json
import secrets
from hashlib import sha256
import sys
from threading import Event, Thread
from time import CLOCK_BOOTTIME, time
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

from anodizing_web import executor as executor_module
from anodizing_web.executor import MODULE_ATTESTATION_S, AdmissionRefused, Executor, FakeDevices
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


@pytest.fixture
def suspend_gateway(tmp_path, monkeypatch):
    boot_clock = Clock()
    clock_gettime = executor_module.time.clock_gettime
    monkeypatch.setattr(executor_module.time, 'clock_gettime',
                        lambda clock_id: boot_clock() if clock_id == CLOCK_BOOTTIME else clock_gettime(clock_id))
    monkeypatch.setenv('ANODIZING_HEARTBEAT_S', '1')
    monkeypatch.setenv('ANODIZING_LEASE_S', '12')
    executor = Executor(tmp_path, FakeDevices(), gateway_enabled=True)
    identity = dict(boot=executor.boot, protocol=executor.health()['protocol'], session='katana-test')
    try:
        executor.gateway('heartbeat', dict(identity, sequence=1))
        executor.gateway('connect', identity)
        yield executor, identity, boot_clock
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
    new_arm = executor.gateway('arm', dict(identity, stop_generation=claim['stop_generation'], module_attestation=module_confirmation(executor, claim)))
    assert new_arm['armed_for'] == 'station-script'
    with pytest.raises(ValueError, match='current owner'):
        executor.gateway('submit', visit(dict(identity, stop_generation=new_arm['stop_generation'])))


def test_module_arm_requires_operator_confirmation(gateway):
    executor, identity, _ = gateway
    claim = executor.gateway('claim', principal=principal())
    with pytest.raises(ValueError, match='confirm'):
        executor.gateway('arm', dict(identity, stop_generation=claim['stop_generation']))
    assert not executor.gateway('status')['armed']


def module_confirmation(executor, claim):
    return {'confirmed': True, 'operator': 'shift operator', 'browser_enrollment': 'a' * 32,
            'owner': claim['owner'], 'lease_id': executor.gateway('status')['module_lease_id']}


@pytest.mark.parametrize('field,value', [
    ('confirmed', False), ('operator', ''), ('browser_enrollment', ''),
    ('owner', 'another-module'), ('lease_id', 'another-lease'),
])
def test_module_arm_rejects_invalid_or_stale_confirmation(gateway, field, value):
    executor, identity, _ = gateway
    claim = executor.gateway('claim', principal=principal())
    confirmation = module_confirmation(executor, claim)
    confirmation[field] = value
    with pytest.raises(ValueError):
        executor.gateway('arm', dict(identity, stop_generation=claim['stop_generation'],
                                     module_attestation=confirmation))
    assert not executor.gateway('status')['armed']


def test_module_arm_records_confirmation_and_expiry(gateway):
    executor, identity, _ = gateway
    claim = executor.gateway('claim', principal=principal())
    before = time()
    armed = executor.gateway('arm', dict(identity, stop_generation=claim['stop_generation'],
                                         module_attestation=module_confirmation(executor, claim)))
    assert armed['armed_for'] == claim['owner']
    attestation = executor.gateway('status')['module_attestation']
    assert attestation['owner'] == claim['owner']
    assert before + 1800 <= attestation['expires_at'] <= time() + 1800


def test_module_confirmation_expires_while_leases_are_kept_alive(gateway):
    executor, identity, clock = gateway
    module = principal()
    claim = executor.gateway('claim', principal=module)
    executor.gateway('arm', dict(identity, stop_generation=claim['stop_generation'],
                                 module_attestation=module_confirmation(executor, claim)))
    for sequence in range(2, 202):
        clock.now += 9
        executor.gateway('heartbeat', dict(identity, sequence=sequence))
        executor.gateway('heartbeat', {'lease_id': claim['lease_id'], 'sequence': sequence}, principal=module)
    status = executor.gateway('status')
    assert status['module_attestation'] is None
    assert status['module_attestation_expired']
    with pytest.raises((AdmissionRefused, ValueError), match='expir'):
        executor.gateway('submit', visit(dict(lease_id=claim['lease_id'],
                                               stop_generation=status['stop_generation'])), principal=module)
    assert executor.devices.effects == []


def test_module_arm_confirmation_expires_across_suspend(suspend_gateway):
    executor, identity, boot_clock = suspend_gateway
    module = principal()
    claim = executor.gateway('claim', principal=module)
    executor.gateway('arm', dict(identity, stop_generation=claim['stop_generation'],
                                 module_attestation=module_confirmation(executor, claim)))
    started = boot_clock.now
    for sequence, elapsed in enumerate(range(9, MODULE_ATTESTATION_S, 9), start=2):
        boot_clock.now = started + elapsed
        executor.gateway('heartbeat', dict(identity, sequence=sequence))
        executor.gateway('heartbeat', dict(lease_id=claim['lease_id'], sequence=sequence), principal=module)

    boot_clock.now = started + MODULE_ATTESTATION_S - 1
    before_suspend = executor.gateway('status', principal=module)
    assert before_suspend['armed'] and before_suspend['module_attestation'] is not None

    boot_clock.now += 1  # Only boot time advances during suspend.
    after_resume = executor.gateway('status', principal=module)
    assert after_resume['lease_valid'] and executor.gateway('status')['lease_valid']
    assert after_resume['module_attestation_expired']
    assert after_resume['module_attestation'] is None
    assert not after_resume['armed']


@pytest.mark.parametrize('action', ['stop', 'disarm', 'release'])
def test_module_confirmation_clears_on_operator_action(gateway, action):
    executor, identity, _ = gateway
    claim = executor.gateway('claim', principal=principal())
    executor.gateway('arm', dict(identity, stop_generation=claim['stop_generation'],
                                 module_attestation=module_confirmation(executor, claim)))
    assert executor.gateway('status')['module_attestation'] is not None
    if action == 'release':
        executor.gateway('release', {'lease_id': claim['lease_id']}, principal=principal())
    else:
        executor.gateway(action, identity)
    assert executor.gateway('status')['module_attestation'] is None


def test_module_job_auto_admission_idempotency_and_own_records(gateway):
    executor, identity, _ = gateway
    module = principal()
    claim = executor.gateway('claim', principal=module)
    current = executor.gateway('arm', dict(identity, stop_generation=claim['stop_generation'], module_attestation=module_confirmation(executor, claim)))
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


def test_jobs_only_first_submission_and_retry_return_minimal_receipts(gateway):
    executor, identity, _ = gateway
    module = principal(scopes=('jobs',))
    claim = executor.gateway('claim', principal=module)
    armed = executor.gateway('arm', dict(identity, stop_generation=claim['stop_generation'],
                                         module_attestation=module_confirmation(executor, claim)))
    request = visit(dict(lease_id=claim['lease_id'], stop_generation=armed['stop_generation']))
    first = executor.gateway('submit', request, principal=module)
    wait_for(lambda: executor.status(request['job_id'])['job'], lambda job: job['state'] == 'done')
    receipt = executor.gateway('submit', request, principal=module)
    assert receipt['job_id'] == request['job_id']
    assert receipt['state'] == 'done'
    assert receipt['checkpoint'] is None
    assert receipt['result'] == {}
    assert 'params' not in json.dumps(receipt)
    assert first == dict(receipt, state=first['state'])


def test_refused_retry_stays_refused_and_never_records_unvalidated_parameters(gateway):
    executor, identity, _ = gateway
    module = principal(scopes=('jobs',))
    claim = executor.gateway('claim', principal=module)
    armed = executor.gateway('arm', dict(identity, stop_generation=claim['stop_generation'],
                                         module_attestation=module_confirmation(executor, claim)))
    executor.manual_travel_until = time() + 60
    request = visit(dict(lease_id=claim['lease_id'], stop_generation=armed['stop_generation']))
    request['params'] = {'unvalidated': 'synthetic-private-value'}
    with pytest.raises(AdmissionRefused) as first:
        executor.gateway('submit', request, principal=module)
    assert 'synthetic-private-value' not in json.dumps(executor.owner.jobs[request['job_id']])
    with pytest.raises(AdmissionRefused, match=str(first.value)):
        executor.gateway('submit', request, principal=module)


@pytest.mark.parametrize('station,reserve,reason', [(250, None, 'learned range'), (2, 100, 'battery')])
def test_module_station_checks_learned_range_and_battery_floor(gateway, station, reserve, reason):
    executor, identity, _ = gateway
    module = principal()
    claimed = executor.gateway('claim', principal=module)
    current = executor.gateway('arm', dict(identity, stop_generation=claimed['stop_generation'], module_attestation=module_confirmation(executor, claimed)))
    if reserve is not None:
        executor.devices.reserve_s = reserve
    with pytest.raises(AdmissionRefused, match=reason):
        executor.gateway('submit', visit(dict(lease_id=claimed['lease_id'], stop_generation=current['stop_generation']),
                                         station=station), principal=module)
    assert executor.devices.effects == []


@pytest.mark.parametrize('opt_in,case,refusal', [
    pytest.param(None, 'clear', 'isolated track', id='unset'),
    pytest.param('0', 'clear', 'isolated track', id='disabled'),
    pytest.param('yes', 'clear', 'isolated track', id='not-one'),
    pytest.param('1', 'clear', None, id='enabled-and-confirmed'),
    pytest.param('1', 'expired-window', 'window', id='expired-window'),
    pytest.param('1', 'battery-below-floor', 'battery', id='battery-below-floor'),
    pytest.param('1', 'battery-at-floor', 'battery', id='battery-at-floor'),
    pytest.param('1', 'no-confirmation', '(?i)confirm', id='no-confirmation'),
])
def test_isolated_track_module_motion_requires_opt_in_and_all_checks(tmp_path, monkeypatch, opt_in, case, refusal):
    class IsolatedTrackDouble(FakeDevices):
        simulated = False
        battery_percent = 80

        def configure_manual_travel(self, expires, battery_floor, isolated_track):
            assert isolated_track

        def status(self):
            state = super().status()
            if state['status'] is not None:
                state['status']['battery_percent'] = self.battery_percent
            return state

        def perform(self, effect, value, *, check_module_dispatch):
            check_module_dispatch()
            return super().perform(effect, value)

    monkeypatch.setenv('ANODIZING_MANUAL_TRAVEL_UNTIL', str(time() + 60))
    monkeypatch.setenv('ANODIZING_MANUAL_TRAVEL_ISOLATED_TRACK', '1')
    monkeypatch.setenv('ANODIZING_MANUAL_TRAVEL_MIN_BATTERY', '20')
    if opt_in is None:
        monkeypatch.delenv('ANODIZING_MODULE_TRAVEL_ON_ATTESTED_TRACK', raising=False)
    else:
        monkeypatch.setenv('ANODIZING_MODULE_TRAVEL_ON_ATTESTED_TRACK', opt_in)
    devices = IsolatedTrackDouble()
    executor = Executor(tmp_path, devices, gateway_enabled=True)
    try:
        identity = dict(boot=executor.boot, protocol=executor.health()['protocol'], session='katana-test')
        executor.gateway('heartbeat', dict(identity, sequence=1))
        executor.gateway('connect', identity)
        module = principal()
        claim = executor.gateway('claim', principal=module)
        if case != 'no-confirmation':
            executor.gateway('arm', dict(identity, stop_generation=claim['stop_generation'],
                                         module_attestation=module_confirmation(executor, claim)))
        if case == 'expired-window':
            executor.manual_travel_until = time() - 1
        elif case == 'battery-below-floor':
            devices.battery_percent = 19
        elif case == 'battery-at-floor':
            devices.battery_percent = 20
        job = visit(dict(lease_id=claim['lease_id'], stop_generation=claim['stop_generation']))
        if refusal:
            with pytest.raises(AdmissionRefused, match=refusal):
                executor.gateway('submit', job, principal=module)
            assert devices.effects == []
        else:
            executor.gateway('submit', job, principal=module)
            final = wait_for(lambda: executor.status(job['job_id'])['job'],
                             lambda entry: entry is not None and entry['state'] not in ('accepted', 'running', 'waiting'))
            assert final['outcome'] == 'succeeded'
            assert devices.effects == [('station', 2)]
    finally:
        executor.close()


@pytest.mark.parametrize('change', [
    pytest.param(lambda executor: setattr(executor, 'isolated_track', True), id='isolated-track'),
    pytest.param(lambda executor: setattr(executor.gateway.ownership, 'owner', 'next-owner'), id='owner'),
    pytest.param(lambda executor: setattr(executor.gateway.ownership.lease, 'id', 'next-lease'), id='lease'),
    pytest.param(lambda executor: setattr(executor.gateway.ownership.lease, 'deadline', executor.clock()), id='lease-expired'),
    pytest.param(lambda executor: setattr(executor, 'stop_generation', next(executor.generations)), id='stop-generation'),
])
def test_module_station_rechecks_current_motion_authority_at_send(tmp_path, monkeypatch, change):
    clock = Clock()
    sent = Event()

    class WiredStationDouble(FakeDevices):
        simulated = False
        manual_travel_qualified = True

        def configure_manual_travel(self, expires, battery_floor, isolated_track):
            assert not isolated_track

        def perform(self, effect, value, *, check_module_dispatch):
            with executor.guard:
                change(executor)
                check_module_dispatch()
                sent.set()
            return super().perform(effect, value)

    monkeypatch.setenv('ANODIZING_MANUAL_TRAVEL_UNTIL', str(time() + 60))
    monkeypatch.setenv('ANODIZING_MANUAL_TRAVEL_ISOLATED_TRACK', '0')
    monkeypatch.delenv('ANODIZING_MODULE_TRAVEL_ON_ATTESTED_TRACK', raising=False)
    executor = Executor(tmp_path, WiredStationDouble(), clock=clock, gateway_enabled=True)
    try:
        identity = dict(boot=executor.boot, protocol=executor.health()['protocol'], session='katana-test')
        executor.gateway('heartbeat', dict(identity, sequence=1))
        executor.gateway('connect', identity)
        module = principal()
        claim = executor.gateway('claim', principal=module)
        armed = executor.gateway('arm', dict(identity, stop_generation=claim['stop_generation'],
                                             module_attestation=module_confirmation(executor, claim)))
        job = visit(dict(lease_id=claim['lease_id'], stop_generation=armed['stop_generation']))
        executor.gateway('submit', job, principal=module)
        end = wait_for(lambda: executor.status(job['job_id'])['job'],
                       lambda result: result is not None and result['state'] not in ('accepted', 'running', 'waiting'))
        assert not sent.is_set()
        assert executor.devices.effects == []
        assert end['outcome'] != 'succeeded'
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


@pytest.mark.parametrize('owner', ['controller', 'module'])
def test_lease_expires_across_suspend(suspend_gateway, owner):
    executor, identity, boot_clock = suspend_gateway
    status_args = {}
    if owner == 'module':
        module = principal()
        claim = executor.gateway('claim', principal=module)
        lease_s = claim['lease_s']
        status_args['principal'] = module
    else:
        executor.gateway('claim', identity)
        lease_s = executor.health()['lease_s']

    boot_clock.now += lease_s - 1
    assert executor.gateway('status', **status_args)['lease_valid']
    boot_clock.now += 1
    assert not executor.gateway('status', **status_args)['lease_valid']


def test_module_read_status_includes_only_the_battery_estimate(gateway):
    executor, _, _ = gateway
    status = executor.devices.status
    executor.devices.status = lambda: dict(status(), battery_trend='in_use')
    state = executor.gateway('status', principal=principal(scopes=('read',)))
    assert state['device']['battery_trend'] == 'in_use'
    assert set(state['device']) == {'connected', 'stale', 'status', 'status_age_s', 'battery_trend'}


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
    reviewed['module_attestation'] = module_confirmation(executor, selected)
    assert executor.gateway('arm', reviewed)['armed_for'] == 'calibration'
    with pytest.raises(ValueError, match='AGV page'):
        executor.gateway('disarm', identity, owner='calibration')


def test_coincident_controller_and_module_expiry_stops_once(gateway):
    executor, identity, clock = gateway
    module = principal()
    claimed = executor.gateway('claim', principal=module)
    clock.now += 7
    executor.gateway('heartbeat', dict(identity, sequence=2))
    armed = executor.gateway('arm', dict(identity, stop_generation=claimed['stop_generation'], module_attestation=module_confirmation(executor, claimed)))
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
    armed = executor.gateway('arm', dict(identity, stop_generation=calibration['stop_generation'], module_attestation=module_confirmation(executor, calibration)))
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


def test_stop_coalesces_pending_halts_and_bounds_receipts(tmp_path):
    devices = FakeDevices()
    executor = Executor(tmp_path, devices, gateway_enabled=True)
    entered, release = Event(), Event()
    calls = []
    original_stop = devices.stop

    def slow_stop():
        calls.append(1)
        if len(calls) == 1:
            entered.set()
            assert release.wait(3)
        return original_stop()

    devices.stop = slow_stop
    try:
        first = executor.stop({'stop_id': 'first-stop'})
        assert entered.wait(1)
        executor.stop({'stop_id': 'second-stop'})
        latest = executor.stop({'stop_id': 'third-stop'})
        assert len(calls) == 1
        release.set()
        wait_for(lambda: not executor.stop_workers, bool)
        assert len(calls) == 2
        assert executor.status()['stop']['generation'] == latest['generation'] > first['generation']
        for index in range(200):
            executor.stop({'stop_id': f'receipt-{index}'})
        assert len(executor.stop_tickets) < 200
    finally:
        release.set()
        executor.close()


def test_heartbeat_stop_does_not_wait_for_blocked_journal_maintenance(gateway):
    executor, identity, clock = gateway
    module = principal()
    executor.gateway('claim', principal=module)
    entered, release, halted = Event(), Event(), Event()
    original_stop = executor.devices.stop

    def blocked_maintenance():
        entered.set()
        assert release.wait(3)

    def observe_halt():
        halted.set()
        return original_stop()

    executor._update_open_air = blocked_maintenance
    executor.devices.stop = observe_halt
    try:
        assert entered.wait(1)
        clock.now += 10
        assert halted.wait(1), 'Heartbeat expiry did not signal halt while maintenance was blocked.'
    finally:
        release.set()


def test_revoked_module_key_cannot_claim_after_request_body_arrives(gateway, tmp_path):
    from email.message import Message
    from io import BytesIO
    from types import SimpleNamespace
    from anodizing_web.executor_http import control_handler
    from anodizing_web.module_tokens import KeyStore

    executor, _, _ = gateway
    tokens = KeyStore(tmp_path / 'keys.json')
    record, key = tokens.add('sample', ['jobs'])
    handler_class = control_handler(executor, 'fixture-operator-key-' * 2, tokens)
    handler = handler_class.__new__(handler_class)
    headers = Message()
    headers['Authorization'] = 'Bearer ' + key
    headers['Content-Type'] = 'application/json'
    headers['Content-Length'] = '2'
    handler.headers, handler.path = headers, '/v1/claim'
    handler.server, handler.client_address = SimpleNamespace(), ('127.0.0.1', 12345)
    handler.allowed_host = lambda: True
    responses = []
    handler.send = lambda status, value: responses.append((status, value))

    class Body(BytesIO):
        def read(self, size=-1):
            result = super().read(size)
            tokens.revoke(record.name)
            return result

    handler.rfile = Body(b'{}')
    handler.handle_request('POST')
    assert responses[0][0] == 403
    assert executor.gateway('status')['owner'] is None


def test_module_stop_joins_pending_page_stop_without_changing_page_receipt(gateway):
    executor, _, _ = gateway
    entered, release = Event(), Event()
    original_stop = executor.devices.stop

    def blocked_stop():
        entered.set()
        assert release.wait(3)
        return original_stop()

    executor.devices.stop = blocked_stop
    try:
        page = executor.gateway('stop', {'stop_id': 'page-stop'})
        assert entered.wait(1)
        module = executor.gateway('stop', {'stop_id': 'module-stop'}, principal=principal())
        assert module['generation'] == page['generation']
        assert not module['confirmed']
        assert executor.status()['stop']['stop_id'] == 'page-stop'
        release.set()
        wait_for(lambda: executor.status()['stop'], lambda stop: stop['confirmed'])
    finally:
        release.set()


def test_short_module_name_confirmation_survives_journal_reload_but_not_restart_authority(tmp_path):
    executor = Executor(tmp_path, FakeDevices(), gateway_enabled=True)
    identity = dict(boot=executor.boot, protocol=executor.health()['protocol'], session='katana-test')
    executor.gateway('heartbeat', dict(identity, sequence=1))
    executor.gateway('connect', identity)
    claim = executor.gateway('claim', principal=principal('a'))
    executor.gateway('arm', dict(identity, stop_generation=claim['stop_generation'],
                                 module_attestation=module_confirmation(executor, claim)))
    executor.close()
    restarted = Executor(tmp_path, FakeDevices(), gateway_enabled=True)
    try:
        status = restarted.gateway('status')
        assert not status['armed']
        assert status['module_attestation'] is None
        assert not restarted.damaged
    finally:
        restarted.close()


def test_old_confirmation_cannot_arm_new_owner_or_lease(gateway):
    executor, identity, _ = gateway
    first_module = principal('first-module', 'first-key-id')
    first = executor.gateway('claim', principal=first_module)
    confirmation = module_confirmation(executor, first)
    executor.gateway('arm', dict(identity, stop_generation=first['stop_generation'], module_attestation=confirmation))
    executor.gateway('release', {'lease_id': first['lease_id']}, principal=first_module)
    second = executor.gateway('claim', principal=principal('second-module', 'second-key-id'))
    assert executor.gateway('status')['module_attestation'] is None
    with pytest.raises(ValueError, match='owner or lease changed'):
        executor.gateway('arm', dict(identity, stop_generation=second['stop_generation'],
                                     module_attestation=confirmation))
    assert not executor.gateway('status')['armed']


def test_operator_check_clears_module_confirmation(gateway):
    executor, identity, _ = gateway
    claim = executor.gateway('claim', principal=principal())
    executor.gateway('arm', dict(identity, stop_generation=claim['stop_generation'],
                                 module_attestation=module_confirmation(executor, claim)))
    executor.gateway('hold', dict(identity, stop_generation=claim['stop_generation'],
                                  manual=True, idle=True, reason='Fixture check'))
    status = executor.gateway('status')
    assert status['operator_check'] is not None
    assert status['module_attestation'] is None
    assert not status['armed']


def test_touchscreen_shows_current_authority_and_job_from_cached_status(gateway, monkeypatch):
    from anodizing_web import executor_http

    executor, identity, clock = gateway
    module = principal(token_id=secrets.token_urlsafe(12))
    claim = executor.gateway('claim', principal=module)
    envelope = dict(lease_id=claim['lease_id'], stop_generation=claim['stop_generation'])
    executor.gateway('heartbeat', dict(lease_id=claim['lease_id'], sequence=1), principal=module)
    executor.gateway('arm', dict(identity, stop_generation=claim['stop_generation'],
                                 module_attestation=module_confirmation(executor, claim)))
    monkeypatch.setattr(executor.devices, 'read', lambda: pytest.fail('Touchscreen requested fresh device I/O'))
    monkeypatch.setattr(executor_http, 'Loopback', lambda port, handler: handler)
    reader = executor_http.display_listener(0, lambda: executor.status(display=True))

    def touchscreen(method, path):
        request = reader.__new__(reader)
        request.allowed_host = lambda: True
        request.path = path
        replies = []
        request.send = lambda status, value, *args: replies.append((status, value))
        getattr(request, 'do_' + method)()
        [reply] = replies
        return reply

    finished = 'previous-visit'
    executor.gateway('submit', visit(envelope, finished), principal=module)
    wait_for(lambda: executor.status(finished)['job'], lambda job: job['state'] == 'done')
    wait_for(lambda: executor.active is None and executor.status()['active_job_id'] is None, bool)
    entered, release = Event(), Event()
    perform = executor.devices.perform

    def hold_fake_job(effect, value):
        entered.set()
        assert release.wait(3)
        return perform(effect, value)

    monkeypatch.setattr(executor.devices, 'perform', hold_fake_job)
    current = 'current-visit'
    executor.gateway('submit', visit(envelope, current, station=3), principal=module)
    try:
        assert entered.wait(1)
        clock.now += 1.25
        code, state = touchscreen('GET', '/status')
        assert code == 200
        assert state['owner'] == module.name
        assert state['lease_alive'] is True
        assert state['heartbeat_age_s'] == pytest.approx(1.25)
        assert state['arm_confirmation_remaining_s'] == pytest.approx(1798.75)
        assert state['job'] == dict(job_id=current, state='running', outcome=None)
        assert state['last_job'] == dict(job_id=finished, state='done', outcome='succeeded')
        assert state['stop_latched'] is False
        serialized = json.dumps(state)
        assert 'module_lease_id' not in state
        assert 'token_id' not in serialized and module.token_id not in serialized
        assert claim['lease_id'] not in serialized
        assert touchscreen('POST', '/v1/stop')[0] == 405
        assert touchscreen('GET', '/v1/health')[0] == 404
        executor.gateway('stop', {'stop_id': 'touchscreen-check'})
        _, stopped = touchscreen('GET', '/status')
        assert stopped['stop_latched'] is True
        assert stopped['arm_confirmation_remaining_s'] is None
    finally:
        release.set()
