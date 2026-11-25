"""Offline regressions for independently observed phase-one evidence."""
import pytest
from anodizing_web.executor import Executor, FakeDevices
from anodizing_web.programme import Programme
from test_executor_phase1 import pair, session, terminal
from test_programme_review import advance, steps


def test_rebooted_fake_cannot_invent_a_safe_recovery_hold(tmp_path):
    devices = FakeDevices(setpoints={'tank-0': 30})
    devices.plc.stale_done = True
    executor = Executor(tmp_path / 'ipc', devices)
    programme = Programme(tmp_path / 'katana', executor.dispatch, steps('ultrasonic'))
    run = programme.start(programme.status()['recipe']['id'], 'basket', programme.review()['approval'])
    advance(programme, lambda r: r['state'] == 'recovery required' and not r['ack_pending'])
    assert devices.robot.location == 'tank-0' and devices.plc.equipment_on
    executor.close()
    replacement = Executor(tmp_path / 'ipc', FakeDevices())
    try:
        programme.request = replacement.dispatch
        programme.next_pulse = 0
        with pytest.raises(ValueError, match='[Ss]afe hold|observ'):
            programme.disposition(run['id'], 'Verify independently before release.')
        assert replacement.status()['recovery_required']
    finally:
        replacement.close()


def test_delayed_plc_sample_does_not_hide_immersion_time(tmp_path):
    clock = [0.0]
    devices = FakeDevices(clock=lambda: clock[0], setpoints={'tank-0': 30})
    perform = devices.perform
    def delayed(effect, value):
        if effect == 'dwell':
            clock[0] += 39
        return perform(effect, value)
    devices.perform = delayed
    executor = Executor(tmp_path / 'ipc', devices)
    try:
        programme = Programme(tmp_path / 'katana', executor.dispatch, steps('ultrasonic'))
        recipe = programme.save(programme.status()['recipe']['id'], {'step-0': {'overstay': 3}})
        programme.start(recipe['id'], 'basket', programme.review()['approval'])
        run = advance(programme, lambda r: r['state'] != 'running')
        record = run['steps'][0]
        assert record['dwell_s'] == pytest.approx(record['lift_s'] - record['placed_s'])
        assert record['dwell_s'] == pytest.approx(39)
        assert run['state'] == 'succeeded' and run['final_hold'] == 'output'
        assert run['inspection'] and record['inspection'] and run['jobs'][-1]['job']['result']['alarm']
    finally:
        executor.close()


@pytest.mark.parametrize('second', ['basket-two', 'basket-one'])
def test_second_basket_is_loaded_at_input(tmp_path, second):
    devices = FakeDevices(setpoints={'tank-0': 30, 'tank-1': 30})
    executor = Executor(tmp_path / 'ipc', devices)
    try:
        programme = Programme(tmp_path / 'katana', executor.dispatch, steps('seal'))
        revision = programme.status()['recipe']['id']
        for basket in ('basket-one', second):
            programme.start(revision, basket, programme.review()['approval'])
            run = advance(programme, lambda r: r['state'] != 'running' and not r['ack_pending'])
            assert run['state'] == 'succeeded'
            place = next(c for j in run['jobs'] for c in j['job']['checkpoint']['confirmed'] if c['effect'] == 'place')
            assert place['value']['source'] == 'input'
    finally:
        executor.close()


def test_fresh_between_job_observation_is_saved_on_basket(tmp_path):
    devices = FakeDevices(setpoints={'tank-0': 30, 'tank-1': 30})
    executor = Executor(tmp_path / 'ipc', devices)
    programme = Programme(tmp_path / 'katana', executor.dispatch, steps('ultrasonic', 'seal'))
    run = programme.start(programme.status()['recipe']['id'], 'basket', programme.review()['approval'])
    advance(programme, lambda r: r['cursor'] == 1 and r['ack_pending'])
    executor.close()
    devices.robot.location = 'output'
    executor = Executor(tmp_path / 'ipc', devices)
    try:
        programme.request = executor.dispatch
        programme.next_pulse = 0
        held = advance(programme, lambda r: r['state'] == 'recovery required' and not r['ack_pending'])
        recovered = programme.disposition(run['id'], 'Verified retained basket at output.')
        job_id = held['jobs'][-1]['job']['job_id']
        observation = executor.status(job_id)['job']['observation']
        assert observation['hold']['location'] == 'output'
        assert recovered['final_hold'] == 'output'
        assert recovered['recovery_observation'] == dict(observation, job_id=job_id)
    finally:
        executor.close()


@pytest.mark.parametrize('settings', [None, {}, {'tank-2': 0, 'tank-3': 30}, {'tank-2': 31, 'tank-3': 30}])
def test_plc_admission_requires_independent_positive_matching_settings(tmp_path, settings):
    devices = FakeDevices()
    executor = Executor(tmp_path, devices)
    try:
        lease = session(executor)
        devices.plc.setpoints = settings
        with pytest.raises(ValueError, match='setpoint'):
            executor.submit(pair(lease))
        assert devices.effects == []
    finally:
        executor.close()


def test_torn_marker_and_missing_job_keep_named_fences_even_at_a_safe_hold(tmp_path):
    marker = tmp_path / '.executor-active.json'
    devices = FakeDevices()
    ident = 'lost-job-1'
    seed = Executor(tmp_path, devices)
    try:
        identity = session(seed)
        seed.submit(dict(identity, job_id=ident, type='station_visit', version=1, params={'station': 1}))
        terminal(seed, ident)
    finally:
        seed.close()
    (tmp_path / '.jobs' / f'{ident}.json').unlink()
    marker.write_bytes(b'{')
    executor = Executor(tmp_path, devices)
    try:
        identity = dict(protocol=1, session='controller-one', boot=executor.boot)
        executor.heartbeat(dict(identity, sequence=1))
        assert executor.status()['recovery_required']
        devices.hold_available = False
        request = dict(identity, job_id=ident, missing=True, repair_active_marker=True,
                       disposition='Verified basket at input and equipment off.')
        effects = list(devices.effects)
        for available in (False, True):
            devices.hold_available = available
            with pytest.raises(ValueError, match='call a technician'):
                executor.reconcile(request)
            status = executor.status()
            assert status['recovery_required']
            assert set(status['evidence_faults']) == {marker.name, f'.jobs/{ident}.json'}
            assert marker.read_bytes() == b'{' and not (tmp_path / '.jobs' / f'{ident}.json').exists()
            assert devices.effects == effects
    finally:
        executor.close()


def test_retained_device_observation_releases_recovery_with_basket_identity(tmp_path):
    devices = FakeDevices(setpoints={'tank-0': 30})
    devices.plc.stale_done = True
    executor = Executor(tmp_path / 'ipc', devices)
    programme = Programme(tmp_path / 'katana', executor.dispatch, steps('ultrasonic'))
    run = programme.start(programme.status()['recipe']['id'], 'basket', programme.review()['approval'])
    held = advance(programme, lambda r: r['state'] == 'recovery required' and not r['ack_pending'])
    executor.close()
    devices.robot.location = 'input'
    devices.plc.equipment_on = False
    replacement = Executor(tmp_path / 'ipc', devices)
    try:
        programme.request = replacement.dispatch
        programme.next_pulse = 0
        result = programme.disposition(run['id'], 'Verified basket at input and equipment off.')
        resolution = replacement.status(held['recovery_job'])['job']['reconciliation']
        assert result['state'] == 'stopped'
        assert not replacement.status()['recovery_required']
        assert resolution['hold']['basket'] == 'basket'
        assert resolution['hold']['equipment_on'] is False
    finally:
        replacement.close()


def test_plc_timer_uses_independently_configured_positive_setting(tmp_path):
    devices = FakeDevices()
    devices.plc.setpoints = {'tank-2': 30, 'tank-3': 30}
    executor = Executor(tmp_path, devices)
    try:
        lease = session(executor)
        request = pair(lease)
        devices.agv.station = request['params']['steps'][0]['station']
        executor.submit(request)
        from test_executor_phase1 import terminal
        result = terminal(executor, request['job_id'])
        assert result['outcome'] == 'succeeded'
        assert [step['plc']['elapsed_s'] for step in result['result']['steps']] == [30, 30]
    finally:
        executor.close()


def test_crash_during_effect_cannot_trust_previous_safe_device_snapshot(tmp_path):
    import json
    root = tmp_path / 'ipc'
    devices = FakeDevices(setpoints={'tank-0': 30}, state_path=root / '.fake-devices.json')
    executor = Executor(root, devices)
    def crash_during_visit(station):
        devices.agv.station = station
        raise RuntimeError('simulated crash during effect')
    devices.agv.visit = crash_during_visit
    with pytest.raises(RuntimeError, match='simulated crash'):
        devices.perform('station', 2)
    executor.close()
    (root / '.jobs' / 'lost-dip.json').write_text(json.dumps({
        'id': 'lost-dip', 'module': 'executor', 'state': 'running', 'fingerprint': 'saved',
        'execution_uncertain': True,
        'checkpoint': {'intent': {'effect': 'place', 'value': {'tank': 'tank-0'}}, 'confirmed': []},
    }))
    (root / '.executor-active.json').write_text(json.dumps({'job_id': 'lost-dip'}))
    replacement = Executor(root, FakeDevices(setpoints={'tank-0': 30}, state_path=root / '.fake-devices.json'))
    try:
        identity = dict(protocol=1, session='controller-one', boot=replacement.boot)
        replacement.heartbeat(dict(identity, sequence=1))
        with pytest.raises(ValueError, match='[Ss]afe hold|observ'):
            replacement.reconcile(dict(identity, job_id='lost-dip', disposition='Inspect before releasing.'))
        assert replacement.status()['recovery_required']
    finally:
        replacement.close()


def test_same_disposition_after_reboot_observes_current_hold(tmp_path):
    devices = FakeDevices(setpoints={'tank-0': 30})
    executor = Executor(tmp_path / 'ipc', devices)
    programme = Programme(tmp_path / 'katana', executor.dispatch, steps('seal'))
    programme.start(programme.status()['recipe']['id'], 'basket', programme.review()['approval'])
    completed = advance(programme, lambda r: r['state'] == 'succeeded' and not r['ack_pending'])
    job_id = completed['jobs'][-1]['job']['job_id']
    identity = dict(protocol=1, session='review-one', boot=executor.boot)
    # The programme owns the prior lease; restart gives this reviewer a fresh one.
    executor.close()
    executor = Executor(tmp_path / 'ipc', devices)
    try:
        identity['boot'] = executor.boot
        executor.heartbeat(dict(identity, sequence=1))
        request = dict(identity, job_id=job_id, observed_only=True, disposition='Verify safe hold.')
        first = executor.reconcile(request)['observation']
        assert first['hold']['location'] == 'output'
        executor.close()
        devices.robot.location = 'input'
        executor = Executor(tmp_path / 'ipc', devices)
        identity['boot'] = executor.boot
        executor.heartbeat(dict(identity, sequence=1))
        second = executor.reconcile(dict(request, boot=executor.boot))['observation']
        assert second['boot'] == executor.boot
        assert second['hold']['location'] == 'input'
    finally:
        executor.close()


def test_occupied_input_cannot_be_replaced_by_new_basket(tmp_path):
    devices = FakeDevices(setpoints={'tank-0': 30})
    executor = Executor(tmp_path / 'ipc', devices)
    try:
        devices.robot.basket = 'existing-basket'
        programme = Programme(tmp_path / 'katana', executor.dispatch, steps('seal'))
        programme.start(programme.status()['recipe']['id'], 'different-basket', programme.review()['approval'])
        run = advance(programme, lambda r: r['state'] != 'running' and not r['ack_pending'])
        assert run['state'] != 'succeeded'
        assert devices.robot.basket == 'existing-basket'
        assert not any(effect == 'place' for effect, _ in devices.effects)
    finally:
        executor.close()
