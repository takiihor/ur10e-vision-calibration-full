"""Supervised station admission against the offline fake executor."""
import time

import pytest

from anodizing_web.executor import Executor, FakeDevices, check_manual_report
from test_executor_phase1 import session, visit


def test_manual_station_requires_fresh_confirmation(tmp_path, monkeypatch):
    monkeypatch.setenv('ANODIZING_MANUAL_TRAVEL_UNTIL', str(time.time() + 3600))
    devices = FakeDevices()
    executor = Executor(tmp_path, devices)
    try:
        lease = session(executor)
        with pytest.raises(ValueError) as refusal:
            executor.submit(visit(lease))
        assert 'I am at the e-stop, the arm is folded, and the path and tank equipment are clear.' in str(refusal.value)
        assert devices.effects == []
    finally:
        executor.close()


def test_expired_manual_travel_refuses_station(tmp_path, monkeypatch):
    monkeypatch.setenv('ANODIZING_MANUAL_TRAVEL_UNTIL', str(time.time() - 1))
    devices = FakeDevices()
    executor = Executor(tmp_path, devices)
    try:
        lease = session(executor)
        request = visit(lease)
        request['attestation'] = dict(id=request['job_id'], station=2, confirmed_at=time.time())
        with pytest.raises(ValueError, match='(?i)expir|disabled'):
            executor.submit(request)
        assert devices.effects == []
    finally:
        executor.close()


@pytest.mark.parametrize(('field', 'value'), [
    ('fault_code', 80), ('battery_percent', 19), ('max_stations', 0),
])
def test_station_refuses_bad_report(tmp_path, monkeypatch, field, value):
    monkeypatch.setenv('ANODIZING_MANUAL_TRAVEL_UNTIL', str(time.time() + 3600))
    devices = FakeDevices()
    executor = Executor(tmp_path, devices)
    try:
        lease = session(executor)
        status = devices.status
        def report():
            result = status()
            result['status'][field] = value
            return result
        devices.status = report
        request = visit(lease)
        request['attestation'] = dict(id=request['job_id'], station=2, confirmed_at=time.time())
        with pytest.raises(ValueError):
            executor.submit(request)
        assert devices.effects == []
    finally:
        executor.close()


def test_station_refuses_stale_confirmation(tmp_path, monkeypatch):
    monkeypatch.setenv('ANODIZING_MANUAL_TRAVEL_UNTIL', str(time.time() + 3600))
    devices = FakeDevices()
    executor = Executor(tmp_path, devices)
    try:
        lease = session(executor)
        request = visit(lease)
        request['attestation'] = dict(id=request['job_id'], station=2, confirmed_at=time.time() - 3600)
        with pytest.raises(ValueError, match='(?i)attest|confirm'):
            executor.submit(request)
        assert devices.effects == []
    finally:
        executor.close()


@pytest.mark.parametrize(('legs', 'station', 'allowed'), [
    (0, 1, False), (1, 1, True), (1, 2, True), (1, 3, False),
    (255, 255, True), (255, 256, False),
])
def test_manual_station_range(legs, station, allowed):
    report = dict(mode=2, max_stations=legs, fault_code=0, obstacle_stop=False, battery_percent=80)
    if allowed:
        check_manual_report(report, 0, 20, station)
    else:
        with pytest.raises(ValueError, match='Choose a station'):
            check_manual_report(report, 0, 20, station)


@pytest.mark.parametrize(('field', 'value'), [('fault_code', 80), ('battery_percent', 19)])
def test_arm_refuses_bad_report(tmp_path, monkeypatch, field, value):
    monkeypatch.setenv('ANODIZING_MANUAL_TRAVEL_UNTIL', str(time.time() + 3600))
    devices = FakeDevices()
    executor = Executor(tmp_path, devices)
    try:
        meta = executor.health()
        identity = dict(session='operator', boot=meta['boot'], protocol=meta['protocol'])
        executor.heartbeat(dict(identity, sequence=1))
        executor.control('connect', identity)
        status = devices.status
        def report():
            result = status()
            result['status'][field] = value
            return result
        devices.status = report
        with pytest.raises(ValueError):
            executor.control('arm', dict(identity, stop_generation=executor.status()['stop_generation']))
        assert not executor.status()['armed']
    finally:
        executor.close()


def test_confirmation_cannot_authorize_another_visit(tmp_path, monkeypatch):
    from test_executor_phase1 import terminal
    monkeypatch.setenv('ANODIZING_MANUAL_TRAVEL_UNTIL', str(time.time() + 3600))
    devices = FakeDevices()
    executor = Executor(tmp_path, devices)
    try:
        lease = session(executor)
        first = visit(lease, job_id='first-visit')
        first['attestation'] = dict(id='first-visit', station=2, confirmed_at=time.time())
        executor.submit(first)
        completed = terminal(executor, 'first-visit')
        assert completed['outcome'] == 'succeeded'
        assert executor.submit(first) == completed
        second = visit(lease, job_id='second-visit')
        second['attestation'] = first['attestation']
        with pytest.raises(ValueError):
            executor.submit(second)
        assert devices.effects == [('station', 2)]
    finally:
        executor.close()


def test_supervised_stop_requires_halt_attempt_even_after_expiry(tmp_path, monkeypatch):
    from test_executor_phase1 import wait_for
    monkeypatch.setenv('ANODIZING_MANUAL_TRAVEL_UNTIL', str(time.time() - 1))
    devices = FakeDevices()
    executor = Executor(tmp_path, devices)
    try:
        session(executor)
        devices.stop = lambda: False
        devices.stop_evidence = lambda requested: True
        executor.stop({'stop_id': 'failed-halt'})
        wait_for(lambda: executor.status(), lambda state: not executor.stop_workers)
        assert not executor.status()['stop']['confirmed']
    finally:
        executor.close()


def test_web_confirmation_is_journaled_and_cannot_be_reused(tmp_path, monkeypatch):
    from urllib.error import HTTPError
    from test_executor_link_review import arm, operator, remote, wait_for
    monkeypatch.setenv('ANODIZING_MANUAL_TRAVEL_UNTIL', str(time.time() + 3600))
    root = tmp_path / 'katana'
    with remote(tmp_path) as (client, _, devices), operator(root, client) as call:
        reviewed = arm(call)
        confirmation = dict(id='operator-confirmation', station=2, confirmed_at=time.time())
        request = dict(reviewed, action='send', command='call_station', value=2, attestation=confirmation)
        call('agv', request)
        state = wait_for(lambda: call('agv'), lambda s: (s.get('manual_visit') or {}).get('result', {}).get('outcome'))
        intent = state['manual_visit']
        assert intent['attestation'] == confirmation and intent['attested_at'] >= confirmation['confirmed_at']
        call('agv', dict(action='acknowledge_station', job_id=intent['id']))
        with pytest.raises(HTTPError):
            call('agv', request)
        assert devices.effects == [('station', 2)]


def test_stale_status_refuses_confirmed_station(tmp_path, monkeypatch):
    monkeypatch.setenv('ANODIZING_MANUAL_TRAVEL_UNTIL', str(time.time() + 3600))
    devices = FakeDevices()
    executor = Executor(tmp_path, devices)
    try:
        lease = session(executor)
        devices.fresh = False
        request = visit(lease)
        request['attestation'] = dict(id=request['job_id'], station=2, confirmed_at=time.time())
        with pytest.raises(ValueError):
            executor.submit(request)
        assert devices.effects == []
    finally:
        executor.close()


@pytest.mark.parametrize('kind', ['recipe', 'dip'])
def test_supervised_policy_refuses_recipe_and_dip(tmp_path, monkeypatch, kind):
    from test_executor_phase1 import pair
    monkeypatch.setenv('ANODIZING_MANUAL_TRAVEL_UNTIL', str(time.time() + 3600))
    devices = FakeDevices()
    executor = Executor(tmp_path, devices)
    try:
        lease = session(executor)
        request = pair(lease) if kind == 'dip' else visit(lease)
        if kind == 'recipe':
            request['params']['run_id'] = 'recipe-run'
        with pytest.raises(ValueError, match='(?i)recipe|programme|dip'):
            executor.submit(request)
        assert devices.effects == []
    finally:
        executor.close()


def test_wired_missing_providers_does_not_enable_manual_travel(tmp_path, monkeypatch):
    from anodizing_web.executor_http import WiredDevices
    monkeypatch.setenv('ANODIZING_MANUAL_TRAVEL_UNTIL', str(time.time() + 3600))
    executor = Executor(tmp_path, WiredDevices(tmp_path, None))
    try:
        health = executor.health()
        assert health['manual_travel_until'] is None
        assert health['manual_travel_missing_evidence']
        with pytest.raises(ValueError, match='(?i)interlock|evidence|live travel'):
            executor.devices.check('station_visit', {'station': 2})
    finally:
        executor.close()


def test_wired_lost_station_ack_requires_operator_check_without_resend(tmp_path, monkeypatch):
    import json
    from types import SimpleNamespace
    from anodizing_web import agv, recipe_agv
    from anodizing_web.executor import PROTOCOL
    from anodizing_web.executor_http import WiredDevices
    from test_executor_phase1 import Clock, terminal

    monkeypatch.setenv('ANODIZING_MANUAL_TRAVEL_UNTIL', str(time.time() + 3600))
    frames = []
    prefix = bytes.fromhex('42 19 0D')
    class FakeWire(agv.Simulator):
        def __init__(self, **_):
            super().__init__(prefix)
            self.arrival = True
        def write(self, raw):
            frame = agv.parse_frame(raw)
            frames.append(frame)
            if frame.command == 0x23 and frame.data[0] == 0x9D:
                return len(raw)  # The transmission has an unknown outcome and no reply.
            return super().write(prefix + raw) - len(prefix)

    (tmp_path / 'site-profile.json').write_text(json.dumps({'agv': {'wired_port': '/dev/test-wire'}}))
    monkeypatch.setattr(agv, 'serial', SimpleNamespace(Serial=FakeWire))
    monkeypatch.setattr(agv, 'TIMED_REPORT_LISTEN_S', 0)
    monkeypatch.setattr(agv, 'RECIPE_POLL_S', .05)
    monkeypatch.setattr(recipe_agv, 'ACK_S', 3)
    monkeypatch.setattr(agv.Resend, 'interval', property(lambda self: .2))
    devices = WiredDevices(tmp_path, None, pose=time.monotonic, reserve=lambda: True, tank_clear=time.monotonic)
    executor = Executor(tmp_path, devices, clock=Clock())
    try:
        identity = dict(session='operator', boot=executor.boot, protocol=PROTOCOL)
        executor.heartbeat(dict(identity, sequence=1))
        executor.control('connect', identity)
        executor.control('status_read', identity)
        executor.control('arm', dict(identity, stop_generation=0))
        request = visit(dict(identity, stop_generation=0))
        request['attestation'] = dict(id=request['job_id'], station=2, confirmed_at=time.time())
        executor.submit(request)
        done = terminal(executor, request['job_id'])
        assert done['outcome'] == 'unknown'
        assert executor.status()['operator_check']['job_id'] == request['job_id']
        moves = [frame for frame in frames if frame.command == 0x23 and frame.data[0] == 0x9D]
        assert len(moves) == 1
    finally:
        executor.close()


@pytest.mark.parametrize('remaining', [-1, 3600])
def test_katana_does_not_confirm_stop_without_halt_for_manual_transport(tmp_path, monkeypatch, remaining):
    from anodizing_web.executor_http import stop_corroborated
    from test_executor_phase1 import wait_for

    monkeypatch.setenv('ANODIZING_MANUAL_TRAVEL_UNTIL', str(time.time() + remaining))
    executor = Executor(tmp_path, FakeDevices())
    try:
        executor.stop({'stop_id': 'manual-stop-1'})
        state = wait_for(executor.status, lambda status: status['stop']['confirmed'])
        receipt = state['stop']
        assert stop_corroborated(state, receipt)
        receipt['halt_attempted'] = False
        assert not stop_corroborated(state, receipt)
    finally:
        executor.close()


@pytest.mark.parametrize('scenario', ['success', 'expire_before_send', 'expire_at_current_station'])
def test_isolated_track_allows_fake_wired_station_without_plant_providers(tmp_path, monkeypatch, scenario):
    import json
    from types import SimpleNamespace
    from anodizing_web import agv
    from anodizing_web.executor import PROTOCOL
    from anodizing_web.executor_http import WiredDevices
    from test_executor_phase1 import Clock, terminal

    monkeypatch.setenv('ANODIZING_MANUAL_TRAVEL_UNTIL', str(time.time() + 3600))
    monkeypatch.setenv('ANODIZING_MANUAL_TRAVEL_ISOLATED_TRACK', '1')
    prefix = bytes.fromhex('42 19 0D')
    frames = []
    call_station = agv.Connection.recipe_call
    def recipe_call(link, *args, **kwargs):
        if scenario == 'expire_before_send':
            devices.manual_travel_until = 0
        return call_station(link, *args, **kwargs)
    class FakeWire(agv.Simulator):
        def __init__(self, **_):
            super().__init__(prefix)
            self.arrival = True
            if scenario == 'expire_at_current_station':
                self.station = self.target = 2
        def write(self, raw):
            frames.append(agv.parse_frame(raw))
            return super().write(prefix + raw) - len(prefix)

    (tmp_path / 'site-profile.json').write_text(json.dumps({'agv': {'wired_port': '/dev/test-wire'}}))
    monkeypatch.setattr(agv, 'serial', SimpleNamespace(Serial=FakeWire))
    monkeypatch.setattr(agv, 'TIMED_REPORT_LISTEN_S', 0)
    monkeypatch.setattr(agv, 'RECIPE_POLL_S', .05)
    monkeypatch.setattr(agv.Connection, 'recipe_call', recipe_call)
    from anodizing_web.executor_http import StationVisit
    require_endpoint = StationVisit.require_endpoint
    def expire_at_endpoint(visit, *args, **kwargs):
        if scenario == 'expire_at_current_station':
            devices.manual_travel_until = 0
        return require_endpoint(visit, *args, **kwargs)
    monkeypatch.setattr(StationVisit, 'require_endpoint', expire_at_endpoint)
    devices = WiredDevices(tmp_path, None)
    executor = Executor(tmp_path, devices, clock=Clock())
    try:
        assert executor.health()['manual_travel_isolated_track'] is True
        identity = dict(session='operator', boot=executor.boot, protocol=PROTOCOL)
        executor.heartbeat(dict(identity, sequence=1))
        executor.control('connect', identity)
        executor.control('status_read', identity)
        executor.control('arm', dict(identity, stop_generation=0))
        request = visit(dict(identity, stop_generation=0))
        request['attestation'] = dict(id=request['job_id'], station=2, confirmed_at=time.time())
        executor.submit(request)
        done = terminal(executor, request['job_id'])
        moves = [frame for frame in frames if frame.command == 0x23 and frame.data[0] == 0x9D]
        assert len(moves) == (1 if scenario == 'success' else 0)
        assert done['outcome'] == ('succeeded' if scenario == 'success' else 'unknown')
    finally:
        executor.close()


def test_isolated_track_health_ends_at_window_expiry(tmp_path, monkeypatch):
    from anodizing_web.executor_http import WiredDevices
    monkeypatch.setenv('ANODIZING_MANUAL_TRAVEL_UNTIL', str(time.time() - 1))
    monkeypatch.setenv('ANODIZING_MANUAL_TRAVEL_ISOLATED_TRACK', '1')
    executor = Executor(tmp_path, WiredDevices(tmp_path, None))
    try:
        assert executor.health()['manual_travel_isolated_track'] is False
    finally:
        executor.close()
