"""Phase 1 contract checks use only local fake devices and clocks."""
from contextlib import contextmanager
import json
import time
from threading import Event, Thread
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

from anodizing_web.executor import Executor, FakeDevices
from anodizing_web.executor_http import display_listener
from anodizing_web.programme import Programme


class Clock:
    now = 10

    def __call__(self):
        return self.now


def wait_for(read, predicate):
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        result = read()
        if predicate(result):
            return result
        time.sleep(.01)
    pytest.fail(f'Timed out waiting for {result!r}')


def session(executor, name='controller-one'):
    meta = executor.health()
    identity = dict(session=name, boot=meta['boot'], protocol=meta['protocol'])
    executor.heartbeat(dict(identity, sequence=1))
    executor.control('connect', identity)
    executor.control('arm', dict(identity, stop_generation=executor.status()['stop_generation']))
    return dict(identity, stop_generation=executor.status()['stop_generation'])


def visit(lease, job_id='station-visit-1', station=2):
    return dict(lease, job_id=job_id, type='station_visit', version=1,
                params={'station': station})


def terminal(executor, job_id):
    return wait_for(lambda: executor.status(job_id)['job'],
                    lambda job: job['state'] not in ('running', 'accepted'))


def test_lost_acceptance_and_terminal_reply_cannot_repeat_effect(tmp_path):
    devices = FakeDevices()
    executor = Executor(tmp_path, devices)
    try:
        lease = session(executor)
        request = visit(lease)
        executor.submit(request)
        executor.submit(request)
        done = terminal(executor, request['job_id'])
        assert done['outcome'] == 'succeeded'
        assert executor.submit(request) == done
        with pytest.raises(ValueError, match='different request'):
            executor.submit(visit(lease, station=3))
        assert devices.effects == [('station', 2)]
    finally:
        executor.close()


def test_wifi_loss_finishes_admitted_leg_and_fences_next(tmp_path):
    clock, entered, release = Clock(), Event(), Event()
    devices = FakeDevices()
    perform = devices.perform

    def slow(effect, value):
        entered.set()
        assert release.wait(4)
        return perform(effect, value)

    devices.perform = slow
    executor = Executor(tmp_path, devices, clock=clock)
    try:
        lease = session(executor)
        executor.submit(visit(lease))
        assert entered.wait(4)
        clock.now += 3
        assert not executor.heartbeat(dict(lease, sequence=1))['lease_valid']
        release.set()
        assert terminal(executor, 'station-visit-1')['outcome'] == 'held'
        assert not executor.status()['armed']
        executor.heartbeat(dict(lease, sequence=2))
        with pytest.raises(ValueError):
            executor.submit(visit(lease, 'station-delayed'))
        assert devices.effects == [('station', 2)]
    finally:
        release.set()
        executor.close()


@pytest.mark.parametrize('fault,message', [('reserve_s', 'reserve'), ('pose', 'pose'), ('fresh', 'feedback')])
def test_local_interlocks_refuse_before_any_effect(tmp_path, fault, message):
    devices = FakeDevices()
    executor = Executor(tmp_path, devices)
    try:
        lease = session(executor)
        setattr(devices, fault, False)
        with pytest.raises(ValueError, match=message):
            executor.submit(visit(lease))
        assert devices.effects == []
    finally:
        executor.close()


def test_executor_restart_fences_unknown_effect(tmp_path):
    jobs = tmp_path / '.jobs'
    jobs.mkdir()
    (jobs / 'station-interrupted.json').write_text(json.dumps({
        'id': 'station-interrupted', 'module': 'executor', 'state': 'running',
        'fingerprint': 'saved', 'execution_uncertain': True,
        'checkpoint': {'intent': {'effect': 'station', 'value': 2}, 'confirmed': []},
    }))
    devices = FakeDevices()
    devices.restore(has_history=False)
    executor = Executor(tmp_path, devices)
    try:
        job = executor.status('station-interrupted')['job']
        assert job['outcome'] == 'unknown'
        assert job['checkpoint']['intent']['value'] == 2
        identity = {'boot': executor.health()['boot'], 'protocol': 1, 'session': 'new-controller'}
        executor.heartbeat(dict(identity, sequence=1))
        executor.control('connect', identity)
        with pytest.raises(ValueError, match='Reconcile'):
            executor.control('arm', dict(identity, stop_generation=executor.status()['stop_generation']))
        executor.reconcile(dict(identity, job_id=job['job_id'], disposition='Verified parked at the input.'))
        assert not executor.status()['armed']
    finally:
        executor.close()


@contextmanager
def running(server):
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f'http://127.0.0.1:{server.server_port}'
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_display_has_only_cached_reads(tmp_path):
    devices = FakeDevices()
    executor = Executor(tmp_path, devices)
    try:
        with running(display_listener(0, executor.status)) as base:
            assert 'AGV status' in urlopen(base).read().decode()
            state = json.load(urlopen(base + '/status'))
            assert state['boot'] == executor.health()['boot']
            for method, path in [('POST', '/status'), ('POST', '/v1/stop'), ('GET', '/v1/health')]:
                with pytest.raises(HTTPError) as err:
                    urlopen(Request(base + path, data=b'{}' if method == 'POST' else None, method=method))
                assert err.value.code in (404, 405)
        assert devices.effects == []
    finally:
        executor.close()


FAKE_TANK_SETPOINTS = {f'tank-{index}': 30 for index in range(12)}


def recipe():
    roles = ['ultrasonic', 'rinse', 'etch', 'rinse', 'desmut', 'rinse',
             'anodize', 'rinse', 'rinse', 'rinse', 'rinse', 'seal']
    return [{'id': f'step-{i}', 'tank': f'tank-{i}', 'role': role,
             'station': i + 1, 'setpoint': 30} for i, role in enumerate(roles)]


class Link:
    available = True

    def __init__(self, executor):
        self.executor = executor

    def __call__(self, operation, body=None):
        if not self.available:
            raise OSError('simulated Wi-Fi loss')
        return self.executor.dispatch(operation, body)


def test_full_programme_and_record_recovery(tmp_path):
    devices = FakeDevices(setpoints=FAKE_TANK_SETPOINTS)
    executor = Executor(tmp_path / 'executor', devices)
    root = tmp_path / 'controller'
    programme = Programme(root, Link(executor), recipe())
    try:
        draft = programme.status()['recipe']
        first = draft['steps'][0]
        revision = programme.save(draft['id'], {first['id']: {'min': 29, 'overstay': 3, 'air': 9}})
        with pytest.raises(ValueError, match='Resolve'):
            programme.finalize(revision['id'])
        run = programme.start(revision['id'], 'basket-001', programme.review()['approval'])
        programme.save(revision['id'], {first['id']: {'min': 28, 'overstay': 3, 'air': 8}})
        assert run['recipe']['steps'][0]['limits']['min'] == 29
        complete = wait_for(lambda: (programme.tick(), programme.status()['run'])[1],
                            lambda record: record['state'] == 'succeeded')
        assert len(complete['steps']) == 12
        assert [step['id'] for step in complete['steps']] == [step['id'] for step in recipe()]
        assert sum(entry['type'] == 'dip' for entry in complete['jobs']) == 9
        assert all(step['plc']['elapsed_s'] == 30 and step['dwell_s'] >= 30 for step in complete['steps'])
        assert sum(effect == 'place' for effect, _ in devices.effects) == 12
        recovered = Programme(root, Link(executor), recipe()).status()['run']
        assert recovered == complete
    finally:
        executor.close()


def test_unreachable_stop_survives_controller_restart(tmp_path):
    executor = Executor(tmp_path / 'executor', FakeDevices())
    root, link = tmp_path / 'controller', Link(executor)
    try:
        programme = Programme(root, link, recipe())
        run = programme.start(programme.status()['recipe']['id'], 'basket-002', programme.review()['approval'])
        link.available = False
        assert programme.stop()['stop_pending']
        recovered = Programme(root, link, recipe())
        assert recovered.status()['run']['id'] == run['id']
        assert recovered.status()['run']['stop_pending']
        link.available = True
        stopped = wait_for(lambda: (recovered.tick(), recovered.status()['run'])[1],
                           lambda item: not item['stop_pending'])
        assert stopped['state'] == 'stopped'
    finally:
        executor.close()


def test_commissioning_arm_disarm_logs_raw_bytes_and_blocks_travel(tmp_path, monkeypatch):
    from anodizing_web import agv
    from anodizing_web.executor_http import WiredDevices
    from agv_examples import REPORT
    from types import SimpleNamespace

    raw_log, wire = [], []

    class FakeWire:
        def __init__(self, **settings):
            self.buffer = b''
            assert settings['exclusive']

        @property
        def in_waiting(self):
            return len(self.buffer)

        def read(self, size):
            raw, self.buffer = self.buffer[:size], self.buffer[size:]
            return raw

        def write(self, raw):
            wire.append(raw)
            self.buffer += REPORT
            return len(raw)

        def close(self):
            pass

    (tmp_path / 'site-profile.json').write_text(json.dumps({'agv': {'wired_port': '/dev/test-wire'}}))
    monkeypatch.setattr(agv, 'serial', SimpleNamespace(Serial=FakeWire))
    monkeypatch.setattr(agv, 'TIMED_REPORT_LISTEN_S', 0)
    devices = WiredDevices(tmp_path, lambda direction, raw: raw_log.append((direction, raw)))
    executor = Executor(tmp_path, devices)
    try:
        meta = executor.health()
        identity = dict(session='controller-one', boot=meta['boot'], protocol=meta['protocol'])
        executor.heartbeat(dict(identity, sequence=1))
        executor.control('connect', identity)
        with pytest.raises(ValueError, match='fresh'):
            executor.control('arm', dict(identity, stop_generation=executor.status()['stop_generation']))
        executor.control('status_read', identity)
        assert executor.control('arm', dict(identity, stop_generation=executor.status()['stop_generation']))['armed']
        with pytest.raises(ValueError, match='live travel'):
            executor.submit(visit(dict(identity, stop_generation=0)))
        assert not executor.control('disarm', identity)['armed']
        assert not executor.control('disconnect', identity)['device']['connected']
        assert wire == [agv.command_frame('query_status', None, b'').encode()]
        assert raw_log == [('TX', wire[0]), ('RX', REPORT)]
    finally:
        executor.close()


def pair(lease):
    from anodizing_web.programme import draft
    return dict(lease, job_id='paired-dip-1', type='dip', version=1,
                params={'steps': draft(recipe())['steps'][2:4], 'hold': 'park',
                        'source': 'input', 'expected_basket': 'released',
                        'run_id': 'test-run-1', 'basket': 'test-basket',
                        'recipe_revision': 'test-revision-1'})


@pytest.mark.parametrize('boundary', ['dwell', 'lift'])
def test_wifi_loss_during_dwell_or_transfer_finishes_pair(tmp_path, boundary):
    clock, entered, release = Clock(), Event(), Event()
    devices = FakeDevices(setpoints=FAKE_TANK_SETPOINTS)
    perform = devices.perform

    def slow(effect, value):
        if effect == boundary and not entered.is_set():
            entered.set()
            assert release.wait(4)
        return perform(effect, value)

    devices.perform = slow
    executor = Executor(tmp_path, devices, clock=clock)
    try:
        lease = session(executor)
        request = pair(lease)
        devices.agv.station = request['params']['steps'][0]['station']
        executor.submit(request)
        assert entered.wait(4)
        clock.now += 3
        assert not executor.heartbeat(dict(lease, sequence=1))['lease_valid']
        release.set()
        done = terminal(executor, 'paired-dip-1')
        assert done['outcome'] == 'held'
        assert [step['id'] for step in done['result']['steps']] == ['step-2', 'step-3']
        assert done['result']['hold']['location'] == 'park'
        assert not executor.status()['armed']
        assert [effect for effect, _ in devices.effects].count('place') == 2
    finally:
        release.set()
        executor.close()


@pytest.mark.parametrize('fault', ['alarm', 'setpoint', 'stale_done'])
def test_plc_admission_and_cycle_refuse_unqualified_lift(tmp_path, fault):
    devices = FakeDevices(setpoints=FAKE_TANK_SETPOINTS)
    executor = Executor(tmp_path, devices)
    try:
        lease = session(executor)
        if fault == 'stale_done':
            devices.plc.stale_done = True
            request = pair(lease)
            devices.agv.station = request['params']['steps'][0]['station']
            executor.submit(request)
            done = terminal(executor, 'paired-dip-1')
            assert done['outcome'] == 'unknown'
            assert 'stale' in done['result']['reason']
            assert devices.effects == [('place', 'step-2')]
        else:
            if fault == 'alarm':
                devices.plc.alarm = True
            else:
                devices.plc.setpoints = {'tank-2': 0, 'tank-3': 31}
            with pytest.raises(ValueError, match='alarm|setpoint'):
                executor.submit(pair(lease))
            assert devices.effects == []
    finally:
        executor.close()


def test_limit_breach_records_measured_dwell_and_finishes_programme(tmp_path):
    devices = FakeDevices(setpoints=FAKE_TANK_SETPOINTS)
    devices.plc.dwell_offset = 70
    executor = Executor(tmp_path / 'executor', devices)
    try:
        programme = Programme(tmp_path / 'controller', Link(executor), recipe())
        revision = programme.status()['recipe']['id']
        run = programme.start(revision, 'inspect-001', programme.review()['approval'])
        completed = wait_for(lambda: (programme.tick(), programme.status()['run'])[1],
                             lambda record: record['state'] == 'succeeded' and not record['ack_pending'])
        assert completed['id'] == run['id'] and completed['inspection']
        assert completed['steps'][0]['inspection']
        assert completed['steps'][0]['plc']['elapsed_s'] == 100 and completed['steps'][0]['dwell_s'] >= 100
        assert any(entry['job']['result'].get('alarm') for entry in completed['jobs'])
        assert completed['final_hold'] == 'output'
        assert devices.effects[-1] == ('lift', 'output')
        assert programme.start(revision, 'inspect-002', programme.review()['approval'])['id'] != run['id']
    finally:
        executor.close()


def test_katana_restart_recovers_intent_before_advancing(tmp_path):
    clock, entered, release = Clock(), Event(), Event()
    devices = FakeDevices(setpoints=FAKE_TANK_SETPOINTS)
    perform = devices.perform

    def slow(effect, value):
        if not entered.is_set():
            entered.set()
            assert release.wait(4)
        return perform(effect, value)

    devices.perform = slow
    executor = Executor(tmp_path / 'executor', devices, clock=clock)
    try:
        root, link = tmp_path / 'controller', Link(executor)
        first = Programme(root, link, recipe())
        first.start(first.status()['recipe']['id'], 'restart-basket', first.review()['approval'])
        first.tick()
        assert entered.wait(4)
        ident = first.status()['run']['pending']['job_id']
        first.close()
        recovered = Programme(root, link, recipe())
        recovered.tick()
        assert recovered.status()['run']['pending']['job_id'] == ident
        clock.now += 3
        executor.status()
        executor.heartbeat(dict(session=first.session, boot=executor.boot, protocol=1, sequence=1))
        release.set()
        assert terminal(executor, ident)['outcome'] == 'held'
        recovered.tick()
        state = recovered.status()['run']
        assert state['state'] == 'held' and state['cursor'] == 0
        assert state['jobs'][0]['job']['job_id'] == ident
        assert state['pending'] is None
        assert devices.effects == [('station', 1)]
        recovered.resume(state['id'], recovered.review()['approval'])
        completed = wait_for(lambda: (recovered.tick(), recovered.status()['run'])[1],
                             lambda record: record['state'] == 'succeeded')
        assert len(completed['steps']) == 12
        assert devices.effects.count(('station', 1)) == 1
    finally:
        release.set()
        executor.close()


def test_incompatible_protocol_is_readable_and_cannot_renew_lease(tmp_path):
    executor = Executor(tmp_path, FakeDevices())
    try:
        with pytest.raises(ValueError, match='Incompatible'):
            executor.heartbeat(dict(boot=executor.boot, session='controller-one', protocol=2, sequence=1))
        assert executor.status()['boot'] == executor.boot
        assert not executor.status()['lease_valid']
    finally:
        executor.close()


def test_acceptance_persists_payload_before_the_first_effect(tmp_path):
    devices, entered, release = FakeDevices(), Event(), Event()
    perform = devices.perform

    def slow(effect, value):
        entered.set()
        assert release.wait(4)
        return perform(effect, value)

    devices.perform = slow
    executor = Executor(tmp_path, devices)
    try:
        request = visit(session(executor))
        executor.submit(request)
        assert entered.wait(4)
        record = json.loads((tmp_path / '.jobs' / 'station-visit-1.json').read_text())
        assert record['executor_payload'] == {k: request[k] for k in ('type', 'version', 'params')}
        assert record['checkpoint']['intent'] == {'effect': 'station', 'value': 2}
        assert record['checkpoint']['confirmed'] == []
    finally:
        release.set()
        executor.close()


@pytest.mark.parametrize('clear_at_write', [True, False])
def test_qualified_fake_wire_uses_station_visit_watchdog(tmp_path, monkeypatch, clear_at_write):
    from anodizing_web import agv
    from anodizing_web.executor_http import WiredDevices
    from types import SimpleNamespace

    frames = []
    prefix = bytes.fromhex('42 19 0D')
    tank_clear = True
    call_station = agv.Connection.recipe_call

    def recipe_call(link, *args, **kwargs):
        nonlocal tank_clear
        tank_clear = clear_at_write
        return call_station(link, *args, **kwargs)

    class FakeWire(agv.Simulator):
        def __init__(self, **_):
            super().__init__(prefix)
            self.arrival = True

        def write(self, raw):
            frames.append(agv.parse_frame(raw))
            return super().write(prefix + raw) - len(prefix)

    (tmp_path / 'site-profile.json').write_text(json.dumps({'agv': {'wired_port': '/dev/test-wire'}}))
    monkeypatch.setattr(agv, 'serial', SimpleNamespace(Serial=FakeWire))
    monkeypatch.setattr(agv, 'TIMED_REPORT_LISTEN_S', 0)
    monkeypatch.setattr(agv, 'RECIPE_POLL_S', .2)
    monkeypatch.setattr(agv.Connection, 'recipe_call', recipe_call)
    executor = Executor(tmp_path, WiredDevices(tmp_path, None, pose=time.monotonic, reserve=lambda: True,
                                             tank_clear=lambda: time.monotonic() if tank_clear else None))
    try:
        identity = dict(session='controller-one', boot=executor.boot, protocol=1)
        executor.heartbeat(dict(identity, sequence=1))
        executor.control('connect', identity)
        executor.control('status_read', identity)
        executor.control('arm', dict(identity, stop_generation=executor.status()['stop_generation']))
        executor.submit(visit(dict(identity, stop_generation=0)))
        done = terminal(executor, 'station-visit-1')
        moves = sum(f.command == 0x23 and f.data[0] == 0x9D for f in frames)
        if clear_at_write:
            assert done['outcome'] in ('succeeded', 'held')
            assert done['result']['hold']['station'] == 2
            assert moves == 1
        else:
            assert done['outcome'] == 'unknown'
            assert 'equipment-off' in done['result']['reason']
            assert moves == 0
    finally:
        executor.close()


def test_ipc_process_loss_after_effect_recovers_without_replaying(tmp_path):
    import os
    import select
    import signal

    device_state = tmp_path / '.fake-devices.json'
    read_end, write_end = os.pipe()
    pid = os.fork()
    if pid == 0:
        try:
            os.close(read_end)
            devices = FakeDevices(state_path=device_state)
            perform = devices.perform

            def lose_power(effect, value):
                result = perform(effect, value)
                (tmp_path / 'fake-device-evidence').write_text(json.dumps(devices.effects))
                os.write(write_end, b'1')
                Event().wait()
                return result

            devices.perform = lose_power
            executor = Executor(tmp_path, devices)
            executor.submit(visit(session(executor)))
            Event().wait()
        finally:
            os._exit(1)
    os.close(write_end)
    try:
        assert select.select([read_end], [], [], 5)[0] and os.read(read_end, 1) == b'1'
    finally:
        os.close(read_end)
        os.kill(pid, signal.SIGKILL)
        _, code = os.waitpid(pid, 0)
    assert os.waitstatus_to_exitcode(code) == -signal.SIGKILL
    assert json.loads((tmp_path / 'fake-device-evidence').read_text()) == [['station', 2]]
    new_devices = FakeDevices(state_path=device_state)
    recovered = Executor(tmp_path, new_devices)
    try:
        job = recovered.status('station-visit-1')['job']
        assert job['outcome'] == 'unknown'
        assert job['checkpoint']['intent'] == {'effect': 'station', 'value': 2}
        assert recovered.status()['recovery_required']
        replay = dict(session='new-controller', boot=recovered.boot, protocol=1, stop_generation=0)
        recovered.heartbeat(dict(replay, sequence=1))
        assert recovered.submit(visit(replay))['outcome'] == 'unknown'
        with pytest.raises(ValueError, match='Reconcile'):
            recovered.submit(visit(replay, 'station-visit-2'))
        assert new_devices.effects == []
        reconciled = recovered.reconcile(dict(replay, job_id='station-visit-1',
                                              disposition='Verified retained station after IPC crash.'))
        assert reconciled['reconciliation']['hold']['station'] == 2
        assert reconciled['reconciliation']['boot'] == recovered.boot
        assert not recovered.status()['recovery_required']
    finally:
        recovered.close()


def test_recovery_disposition_keeps_uncertain_basket_out_of_dispatch(tmp_path):
    devices = FakeDevices(setpoints=FAKE_TANK_SETPOINTS)
    devices.plc.stale_done = True
    executor = Executor(tmp_path / 'executor', devices)
    try:
        programme = Programme(tmp_path / 'controller', Link(executor), recipe())
        revision = programme.status()['recipe']['id']
        run = programme.start(revision, 'recover-001', programme.review()['approval'])
        held = wait_for(lambda: (programme.tick(), programme.status()['run'])[1],
                        lambda record: record['state'] == 'recovery required')
        assert held['cursor'] == 0
        assert executor.status()['recovery_required']
        with pytest.raises(ValueError, match='Safe hold'):
            programme.disposition(run['id'], 'Basket location uncertain.')
        devices.robot.location = 'input'
        devices.plc.equipment_on = False
        assert programme.disposition(run['id'], 'Verified basket in input hold.')['state'] == 'stopped'
        assert not executor.status()['recovery_required']
        assert devices.effects.count(('place', 'step-0')) == 1
    finally:
        executor.close()
