"""R3 IPC boundary regressions with only fake tank and AGV evidence."""
import json
import multiprocessing
import os
from pathlib import Path
import time

import pytest

from anodizing_web.executor import Executor, FakeDevices
from anodizing_web.executor_http import Client, control_listener

from test_executor_phase1 import Clock, FAKE_TANK_SETPOINTS, pair, running, session, terminal, visit, wait_for


def lease(executor):
    identity = dict(boot=executor.boot, protocol=1, session='r3-controller')
    executor.heartbeat(dict(identity, sequence=1))
    executor.control('connect', identity)
    return identity


def dip(identity, *, destination='park', paired=False):
    request = pair(identity)
    if not paired:
        step = request['params']['steps'][0]
        step['role'] = 'ultrasonic'
        request['params']['steps'] = [step]
    request['params']['hold'] = destination
    return request


def plant(executor, request):
    params = request['params']
    executor.devices.agv.station = params['steps'][0]['station']
    executor.devices.robot.basket = params['basket']
    executor.devices.run_id = params['run_id']


@pytest.mark.parametrize('location,equipment', [('tank', True), ('park', False)])
def test_manual_travel_requires_fresh_clear_plant(tmp_path, location, equipment):
    devices = FakeDevices()
    executor = Executor(tmp_path, devices)
    try:
        identity = session(executor)
        devices.robot.location = location
        devices.robot.basket = 'retained-basket'
        devices.run_id = 'retained-run'
        devices.plc.equipment_on = equipment
        request = visit(identity)
        with pytest.raises(ValueError):
            executor.submit(request)
        job = executor.status(request['job_id'])['job']
        assert job['state'] == 'refused' and job['error']
        assert devices.effects == []
    finally:
        executor.close()


def test_deleted_older_accepted_id_is_fenced_after_later_visit(tmp_path):
    devices = FakeDevices(state_path=tmp_path / '.fake-devices.json')
    executor = Executor(tmp_path, devices)
    try:
        identity = session(executor)
        for request in (visit(identity), visit(identity, 'later-visit', 3)):
            executor.submit(request)
            terminal(executor, request['job_id'])
            executor.acknowledge(request)
    finally:
        executor.close()
    (tmp_path / '.jobs' / 'station-visit-1.json').unlink()
    restarted = Executor(tmp_path, FakeDevices(state_path=devices.state_path))
    try:
        identity = lease(restarted)
        assert 'station-visit-1' in restarted.status()['recovery_ids']
        with pytest.raises(ValueError):
            restarted.submit(visit(identity))
        assert restarted.devices.effects == []
    finally:
        restarted.close()


@pytest.mark.parametrize('replacement', ['blank', 'park', 'output'])
def test_missing_result_cannot_assign_a_foreign_basket_hold(tmp_path, replacement):
    devices = FakeDevices()
    if replacement != 'blank':
        devices.robot.location = replacement
        devices.robot.basket = 'other-basket'
        devices.run_id = 'other-run'
        devices.recovery_input = dict(observed_utc=time.time(), source='fake station sensor',
                                      disposition=f'Observed a different run at {replacement}.')
    executor = Executor(tmp_path, devices)
    try:
        identity = lease(executor)
        request = dict(identity, job_id='missing-dip', missing=True, disposition='Observed a stopped plant.',
                       basket='original-basket', run_id='original-run', observation_id='r3-observation')
        with pytest.raises(ValueError, match='unaccounted|independent'):
            executor.reconcile(request)
        assert executor.status('missing-dip')['job'] is None
        if replacement == 'blank':
            with pytest.raises(ValueError, match='Independent'):
                executor.reconcile(dict(request, allow_unaccounted=True))
        else:
            result = executor.reconcile(dict(request, allow_unaccounted=True))
            assert result['reconciliation']['unaccounted']
            assert result['reconciliation']['hold']['basket'] == 'other-basket'
            assert 'unload' not in result['reconciliation']
            assert devices.robot.basket == 'other-basket'
    finally:
        executor.close()


def exit_after_confirmed_lift(root, paired):
    devices = FakeDevices(setpoints=FAKE_TANK_SETPOINTS, state_path=root / '.fake-devices.json')
    executor = Executor(root, devices)
    checkpoint = executor._checkpoint

    def checkpoint_and_exit(ident, data, uncertain=False):
        checkpoint(ident, data, uncertain)
        if not data['intent'] and data['confirmed'][-1]['effect'] == 'lift':
            os._exit(75)

    executor._checkpoint = checkpoint_and_exit
    request = dip(session(executor), destination='output', paired=paired)
    plant(executor, request)
    executor.submit(request)
    terminal(executor, request['job_id'])


@pytest.mark.parametrize('paired', [False, True])
def test_process_exit_after_confirmed_lift_retains_dwell(tmp_path, paired):
    child = multiprocessing.get_context('spawn').Process(target=exit_after_confirmed_lift,
                                                          args=(tmp_path, paired))
    child.start()
    child.join(10)
    if child.is_alive():
        child.kill()
        child.join()
    assert child.exitcode == 75
    executor = Executor(tmp_path, FakeDevices(state_path=tmp_path / '.fake-devices.json'))
    try:
        job = executor.status(pair({})['job_id'])['job']
        assert job['outcome'] == 'unknown'
        assert job['result']['steps'][0]['id'] == 'step-2'
        assert job['result']['steps'][0]['dwell_s'] >= 30
        assert job['checkpoint']['confirmed'][-1]['effect'] == 'lift'
        assert executor.status()['recovery_required']
    finally:
        executor.close()


def exit_before_confirmed_lift(root):
    devices = FakeDevices(setpoints=FAKE_TANK_SETPOINTS, state_path=root / '.fake-devices.json')
    perform = devices.perform

    def execute_then_exit(effect, value):
        result = perform(effect, value)
        if effect == 'lift':
            os._exit(76)
        return result

    devices.perform = execute_then_exit
    executor = Executor(root, devices)
    request = dip(session(executor), paired=True)
    plant(executor, request)
    executor.submit(request)
    terminal(executor, request['job_id'])


def test_process_exit_before_lift_confirmation_preserves_uncertain_transfer(tmp_path):
    child = multiprocessing.get_context('spawn').Process(target=exit_before_confirmed_lift, args=(tmp_path,))
    child.start()
    child.join(10)
    if child.is_alive():
        child.kill()
        child.join()
    assert child.exitcode == 76
    executor = Executor(tmp_path, FakeDevices(state_path=tmp_path / '.fake-devices.json'))
    try:
        ident = pair({})['job_id']
        status = executor.status(ident)
        assert status['job']['outcome'] == 'unknown'
        assert status['job']['checkpoint']['intent'] == {'effect': 'lift', 'value': 'transfer'}
        assert not status['job']['result'].get('steps')
        assert status['recovery_required']
        assert executor.devices.robot.location == 'transfer'
        assert executor.devices.robot.gripper == 'held'
        assert executor.devices.effects == []
    finally:
        executor.close()


@pytest.mark.parametrize('fixture', ['pair', 'final'])
def test_legacy_confirmed_checkpoint_recovers_only_measured_rows(tmp_path, fixture):
    path = Path(__file__).parent / 'fixtures' / f'executor-068199c-interrupted-{fixture}.json'
    saved = json.loads(path.read_text())
    ident = saved['id']
    directory = tmp_path / '.jobs'
    directory.mkdir()
    record_path = directory / f'{ident}.json'
    record_path.write_bytes(path.read_bytes())
    (tmp_path / '.executor-active.json').write_text(json.dumps({'job_id': ident}))
    executor = Executor(tmp_path, FakeDevices())
    try:
        job = executor.status(ident)['job']
        row, = job['result']['steps']
        effects = saved['checkpoint']['confirmed']
        assert row['placed_s'] == effects[0]['evidence']['start_s']
        assert row['lift_s'] == effects[2]['evidence']['lift_s']
        assert row['dwell_s'] == row['lift_s'] - row['placed_s']
        assert job['checkpoint']['boot'] == saved['checkpoint']['boot']
        assert job['outcome'] == 'unknown'
        assert executor.status()['recovery_required']
        persisted = json.loads(record_path.read_text())
        assert persisted['checkpoint']['steps'] == [row]
        assert persisted['checkpoint']['confirmed'] == effects
    finally:
        executor.close()


def test_inspection_disposition_verifies_unload_before_next_input(tmp_path):
    clock = Clock()
    devices = FakeDevices(clock=clock, setpoints=FAKE_TANK_SETPOINTS)
    devices.plc.dwell_offset = 5
    executor = Executor(tmp_path, devices)
    try:
        request = dip(session(executor))
        params = request['params']
        params['steps'][0]['limits']['overstay'] = 1
        plant(executor, request)
        executor.submit(request)
        job = terminal(executor, request['job_id'])
        assert job['outcome'] == 'succeeded'
        disposition = {key: request[key] for key in ('boot', 'protocol', 'session', 'job_id')}
        disposition.update(basket=params['basket'], run_id=params['run_id'], release_basket=True,
                           observation_id='release-r3', disposition='Unload inspected basket from park.')
        evidence = executor.reconcile(disposition)['reconciliation']
        assert evidence['hold']['location'] == 'park'
        assert evidence['unload']['location'] == 'input'
        assert evidence['unload']['basket'] is None
        assert executor.reconcile(disposition)['reconciliation'] == evidence
        assert devices.robot.basket is None
        identity = {key: request[key] for key in ('boot', 'protocol', 'session')}
        identity['stop_generation'] = executor.status()['stop_generation']
        executor.control('arm', identity)
        next_input = visit(identity, 'next-basket')
        next_input['params'].update(basket='new-basket', run_id='new-run', load_input=True)
        executor.submit(next_input)
        assert terminal(executor, next_input['job_id'])['outcome'] == 'succeeded'
        assert devices.robot.basket == 'new-basket'
    finally:
        executor.close()


def test_dip_cannot_succeed_when_actual_hold_differs(tmp_path):
    devices = FakeDevices(setpoints=FAKE_TANK_SETPOINTS)

    def wrong_hold(_):
        devices.robot.location = 'park'

    devices.robot.lift = wrong_hold
    executor = Executor(tmp_path, devices)
    try:
        request = dip(session(executor), destination='output')
        plant(executor, request)
        executor.submit(request)
        result = terminal(executor, request['job_id'])
        assert result['outcome'] == 'unknown'
        assert executor.status()['recovery_required']
    finally:
        executor.close()


def test_late_first_anodize_read_uses_placement_timer(tmp_path):
    clock = Clock()
    devices = FakeDevices(clock=clock, setpoints=FAKE_TANK_SETPOINTS)
    perform = devices.perform

    def late_poll(effect, value):
        if effect == 'dwell' and value['role'] == 'anodize':
            clock.now += 39
        return perform(effect, value)

    devices.perform = late_poll
    executor = Executor(tmp_path, devices)
    try:
        request = dip(session(executor), paired=True)
        request['params']['steps'][0].update(role='anodize', limits=dict(min=None, overstay=3, air=None))
        plant(executor, request)
        executor.submit(request)
        result = terminal(executor, request['job_id'])
        row = result['result']['steps'][0]
        assert row['plc']['done_s'] - row['placed_s'] == 30
        assert row['dwell_s'] == 39
        assert row['inspection']
        assert result['outcome'] == 'succeeded'
    finally:
        executor.close()


def test_open_air_exposure_alarms_locally_without_next_job(tmp_path):
    clock = Clock()
    devices = FakeDevices(clock=clock, setpoints=FAKE_TANK_SETPOINTS)
    executor = Executor(tmp_path, devices, clock=clock)
    try:
        request = dip(session(executor))
        request['params']['steps'][0]['limits']['air'] = 2
        plant(executor, request)
        executor.submit(request)
        assert terminal(executor, request['job_id'])['outcome'] == 'succeeded'
        clock.now += 120

        def expired_state():
            return executor.status(request['job_id'])

        state = wait_for(expired_state, lambda state: not executor.armed)
        assert state['quality_alarm']['job_id'] == request['job_id']
        assert state['quality_alarm']['open_air']
        row = state['job']['result']['steps'][0]
        assert row['inspection'] and row['air_s'] is None
    finally:
        executor.close()


def test_disposition_of_later_station_closes_same_basket_open_air_after_reboot(tmp_path):
    clock = Clock()
    state_path = tmp_path / '.fake-devices.json'
    devices = FakeDevices(clock=clock, setpoints=FAKE_TANK_SETPOINTS, state_path=state_path)
    executor = Executor(tmp_path, devices, clock=clock)
    try:
        request = dip(session(executor))
        params = request['params']
        params['steps'][0]['limits']['air'] = 2
        plant(executor, request)
        executor.submit(request)
        terminal(executor, request['job_id'])
        later = visit({key: request[key] for key in ('boot', 'protocol', 'session', 'stop_generation')},
                      'later-station', 4)
        later['params'].update(basket=params['basket'], run_id=params['run_id'], load_input=False)
        executor.submit(later)
        terminal(executor, later['job_id'])
    finally:
        executor.close()
    restarted = Executor(tmp_path, FakeDevices(state_path=state_path))
    try:
        identity = lease(restarted)
        assert restarted.status()['quality_alarm']['job_id'] == request['job_id']
        evidence = restarted.reconcile(dict(identity, job_id=later['job_id'], observed_only=True,
                                            basket=params['basket'], run_id=params['run_id'], release_basket=True,
                                            observation_id='r3-closure', disposition='Unload basket from park.'))
        assert evidence['observation']['unload']['basket'] is None
        assert restarted.status()['quality_alarm'] is None
        result = restarted.status(request['job_id'])['job']['result']
        assert result['steps'][0]['inspection'] and result['steps'][0]['air_s'] is None
        assert result['open_air_disposition']['job_id'] == later['job_id']
    finally:
        restarted.close()
    final = Executor(tmp_path, FakeDevices(state_path=state_path))
    try:
        assert final.status()['quality_alarm'] is None
    finally:
        final.close()


@pytest.mark.parametrize('ledger', ['.executor-accepted.json', '.executor-refused.json'])
def test_corrupt_ledger_fences_until_verified_bytes_restored(tmp_path, ledger):
    state_path = tmp_path / '.fake-devices.json'
    devices = FakeDevices(state_path=state_path)
    executor = Executor(tmp_path, devices)
    try:
        identity = session(executor)
        request = visit(identity)
        executor.submit(request)
        terminal(executor, request['job_id'])
        executor.acknowledge(request)
        devices.fresh = False
        with pytest.raises(ValueError, match='stale'):
            executor.submit(visit(identity, 'refused-station'))
    finally:
        executor.close()
    path = tmp_path / ledger
    retained = path.read_bytes()
    path.write_text('{corrupt')
    restarted = Executor(tmp_path, FakeDevices(state_path=state_path))
    try:
        identity = lease(restarted)
        status = restarted.status()
        assert ledger in status['recovery_ids']
        assert 'call a technician' in status['evidence_faults'][ledger]
        with pytest.raises(ValueError, match='technician'):
            restarted.control('arm', dict(identity, stop_generation=0))
        assert path.read_text() == '{corrupt'
    finally:
        restarted.close()
    path.write_bytes(retained)
    recovered = Executor(tmp_path, FakeDevices(state_path=state_path))
    try:
        identity = session(recovered)
        assert recovered.submit(visit(identity))['acknowledged']
        with pytest.raises(ValueError, match='stale'):
            recovered.submit(visit(identity, 'refused-station'))
        assert recovered.devices.effects == []
    finally:
        recovered.close()


def test_missing_latest_job_and_corrupt_marker_require_technician(tmp_path):
    state_path = tmp_path / '.fake-devices.json'
    executor = Executor(tmp_path, FakeDevices(state_path=state_path))
    try:
        identity = session(executor)
        for request in (visit(identity), visit(identity, 'later-visit', 3)):
            executor.submit(request)
            terminal(executor, request['job_id'])
            executor.acknowledge(request)
    finally:
        executor.close()
    marker = tmp_path / '.executor-active.json'
    marker.write_text('{broken')
    missing = tmp_path / '.jobs' / 'later-visit.json'
    missing.unlink()
    restarted = Executor(tmp_path, FakeDevices(state_path=state_path))
    try:
        identity = lease(restarted)
        status = restarted.status()
        assert status['recovery_required']
        assert set(status['evidence_faults']) == {marker.name, '.jobs/later-visit.json'}
        for ident in ('station-visit-1', 'later-visit'):
            with pytest.raises(ValueError, match='technician'):
                restarted.reconcile(dict(identity, job_id=ident, observed_only=ident != 'later-visit',
                                         missing=ident == 'later-visit', repair_active_marker=True,
                                         disposition='Observed stopped station.'))
        assert marker.read_text() == '{broken'
        assert not missing.exists()
        assert restarted.devices.effects == []
    finally:
        restarted.close()


def test_http_admission_refusal_distinguishes_absent_acceptance(tmp_path):
    devices = FakeDevices(setpoints={})
    executor = Executor(tmp_path, devices)
    try:
        request = dip(session(executor))
        token = 'r3-private-executor-test-token-value'
        listener = control_listener(0, executor, token)
        with running(listener):
            client = Client(listener.server_port, token)
            with pytest.raises(ValueError, match='PLC tank setpoint') as refused:
                client('submit', request)
            assert getattr(refused.value, 'job_id', None) == request['job_id']
            job = client('status', {'job_id': request['job_id']})['job']
            assert job['state'] == 'refused' and job['error'] == str(refused.value)
            plant(executor, request)
            devices.plc.setpoints.update(FAKE_TANK_SETPOINTS)
            with pytest.raises(ValueError, match='PLC tank setpoint'):
                client('submit', request)
        assert devices.effects == []
    finally:
        executor.close()


def test_client_refuses_acknowledgement_for_a_different_id(tmp_path):
    executor = Executor(tmp_path, FakeDevices())
    token = 'r3-private-executor-test-token-value'
    listener = control_listener(0, executor, token)
    executor.acknowledge = lambda _: {'acknowledged': True, 'job_id': 'different-job'}
    try:
        with running(listener):
            client = Client(listener.server_port, token)
            with pytest.raises(OSError, match='Invalid IPC executor response'):
                client('acknowledge', {'job_id': 'original-job'})
    finally:
        executor.close()
