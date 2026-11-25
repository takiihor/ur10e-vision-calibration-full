"""Phase 1 controller fault boundaries with fake IPC and persisted journals."""
from copy import deepcopy
import json

import pytest

from anodizing_web.executor import AdmissionRefused, Executor, FakeDevices
from anodizing_web.executor_http import Client, control_listener
from test_executor_phase1 import running
from anodizing_web.programme import Programme
from test_programme_review import RecordedLink, advance, start, steps


@pytest.mark.parametrize('damage', ['missing', 'unreadable', 'missing-run'])
def test_retained_run_receipt_fences_controller_history(tmp_path, damage):
    root = tmp_path / 'programme'
    receipts = tmp_path / 'executor-operator-actions'
    receipts.mkdir()
    programme = Programme(root, RecordedLink(), steps('seal'))
    basket = start(programme)
    receipt = receipts / 'start.json'
    receipt.write_text(json.dumps({'signature': 'receipt', 'result': {'run_id': basket['id']}}))
    if damage == 'missing':
        programme.path.unlink()
        expected = None
    elif damage == 'unreadable':
        expected = '{'
        programme.path.write_text(expected)
    else:
        data = json.loads(programme.path.read_text())
        data.update(run=None, runs={})
        expected = json.dumps(data)
        programme.path.write_text(expected)
    restarted = Programme(root, RecordedLink(), steps('seal'))
    assert 'call a technician' in restarted.review()['block']
    assert str(programme.path) in restarted.review()['block']
    assert restarted.control_owned()
    with pytest.raises(ValueError, match='call a technician'):
        start(restarted)
    assert (programme.path.read_text() if programme.path.exists() else None) == expected


def test_lost_refusal_closes_first_job_without_plant_inference(tmp_path):
    link = RecordedLink()

    def exchange(operation, body=None):
        if operation == 'submit':
            link.submits.append(body)
            link.job = dict(job_id=body['job_id'], state='refused', outcome='failed',
                            error='Insufficient reserve.', result={}, checkpoint=None)
            raise OSError('Response lost')
        return link(operation, body)

    programme = Programme(tmp_path, exchange, steps('seal'))
    start(programme)
    programme.tick()
    programme.tick()
    run = programme.status()['run']
    assert run['state'] == 'failed'
    assert run['refusal'] == {'job_id': link.submits[0]['job_id'], 'reason': 'Insufficient reserve.'}
    assert run['pending'] is None and not run['failed_hold']
    assert len(link.submits) == 1
    assert start(programme)['id'] != run['id']


def test_matching_terminal_result_is_reacknowledged_when_it_returns(tmp_path):
    executor = Executor(tmp_path / 'ipc', FakeDevices(setpoints={'tank-0': 30}))
    hidden = set()

    def exchange(operation, body=None):
        reply = deepcopy(executor.dispatch(operation, body))
        if operation == 'status' and (body or {}).get('job_id') in hidden:
            reply['job'] = None
        return reply

    try:
        programme = Programme(tmp_path / 'programme', exchange, steps('seal'))
        start(programme)
        completed = advance(programme, lambda run: run['state'] == 'succeeded')
        hidden.add(completed['ack_pending'])
        programme.tick()
        assert programme.status()['run']['ack_missing']
        hidden.clear()
        programme.tick()
        restored = programme.status()['run']
        assert not restored['ack_pending'] and not restored['ack_missing']
        assert restored['steps'] == completed['steps']
    finally:
        executor.close()


@pytest.mark.parametrize('contradiction', ['dwell', 'plc', 'matching-copies', 'same-fabricated-plc'])
def test_contradictory_terminal_cannot_erase_measured_dwell(tmp_path, contradiction):
    executor = Executor(tmp_path / 'ipc', FakeDevices(setpoints={'tank-0': 30}))
    injected = []

    def exchange(operation, body=None):
        reply = deepcopy(executor.dispatch(operation, body))
        job = reply.get('job') if operation == 'status' else reply if operation == 'submit' else None
        if job and job.get('outcome') == 'succeeded' and job['result'].get('steps'):
            row = job['result']['steps'][0]
            if contradiction == 'dwell':
                row['dwell_s'] = 0
            elif contradiction == 'plc':
                row['plc'] = {'cycle': 0}
            elif contradiction in ('matching-copies', 'same-fabricated-plc'):
                row['plc']['cycle'] += 1
                if contradiction == 'same-fabricated-plc':
                    job['checkpoint']['steps'][0]['plc']['cycle'] = row['plc']['cycle']
            injected.append(job['job_id'])
        return reply

    try:
        programme = Programme(tmp_path / 'programme', exchange, steps('seal'))
        start(programme)
        run = advance(programme, lambda _: bool(injected))
        assert run['pending']['job_id'] == injected[0]
        assert run['steps'] == []
        assert run['invalid_result']
        assert not executor.status(injected[0])['job']['acknowledged']
    finally:
        executor.close()


def test_unaccounted_disposition_now_matching_basket_requires_unload_confirmation(tmp_path):
    link = RecordedLink()
    programme = Programme(tmp_path, link, steps('seal'))
    basket = start(programme)
    advance(programme, lambda run: run['pending'])
    link.job = None
    advance(programme, lambda run: run['pending_missing'])
    with pytest.raises(ValueError, match='unload'):
        programme.disposition(basket['id'], 'Previously observed foreign basket.', allow_unaccounted=True)
    held = programme.status()['run']
    assert held['state'] == 'recovery required'
    assert held['pending']
    assert held['disposition_intent'] is None


@pytest.mark.parametrize('kind', ['dwell', 'air'])
def test_limit_alarm_flags_measured_value_and_programme_continues(tmp_path, kind):
    devices = FakeDevices(setpoints={'tank-0': 30, 'tank-1': 30})
    if kind == 'dwell':
        devices.plc.dwell_offset = 10
    executor = Executor(tmp_path / 'ipc', devices)
    try:
        programme = Programme(tmp_path / 'programme', executor.dispatch, steps('ultrasonic', 'seal'))
        recipe = programme.save(programme.status()['recipe']['id'], {'step-0': {'overstay': 3, 'air': 2}})
        programme.start(recipe['id'], 'alarm-basket', programme.review()['approval'])
        parked = advance(programme, lambda run: run['final_hold'] == 'park' and not run['ack_pending'])
        if kind == 'air':
            exposure = executor.open_air
            assert exposure
            devices.plc.clock += 7
            executor._update_open_air()
            alarm = executor.status()['quality_alarm']
            assert alarm['measured_air_s'] >= 7
        else:
            assert parked['steps'][0]['dwell_s'] >= 40
        completed = advance(programme, lambda run: run['state'] == 'succeeded' and not run['ack_pending'])
        first = completed['steps'][0]
        assert first['inspection']
        if kind == 'air':
            assert first['open_air_alarm_s'] >= 7
            assert first['air_s'] >= first['open_air_alarm_s']
        else:
            assert first['dwell_s'] >= 40
        assert len(completed['steps']) == 2
    finally:
        executor.close()


def test_stop_delivers_before_waiting_for_stop_record_lock(tmp_path):
    from threading import Event, Thread

    forwarded = Event()
    link = RecordedLink()

    def exchange(operation, body=None):
        if operation == 'stop':
            forwarded.set()
            return {'received': True, 'confirmed': True}
        return link(operation, body)

    programme = Programme(tmp_path, exchange, steps('seal'))
    start(programme)
    worker = Thread(target=programme.stop)
    try:
        with programme.stop_lock:
            worker.start()
            assert forwarded.wait(1), 'Stop delivery waited for the record lock'
    finally:
        worker.join()


def test_lost_later_http_refusal_keeps_verified_input_until_disposition(tmp_path):
    token = 'private-fake-ipc-token-for-controller-test'
    devices = FakeDevices(setpoints={'tank-0': 31})
    executor = Executor(tmp_path / 'ipc', devices)
    try:
        with running(control_listener(0, executor, token)) as port:
            client = Client(int(port.rsplit(':', 1)[-1]), token)
            refused = []

            def exchange(operation, body=None):
                try:
                    return client(operation, body)
                except AdmissionRefused as exc:
                    refused.append((body['job_id'], str(exc)))
                    raise OSError('Refusal response lost') from exc

            programme = Programme(tmp_path / 'programme', exchange, steps('seal'))
            basket = start(programme)
            failed = advance(programme, lambda run: run['state'] == 'failed')
            assert len(refused) == 1
            assert failed['pending'] is None and failed['failed_hold']
            assert failed['refusal'] == dict(zip(('job_id', 'reason'), refused[0]))
            assert failed['final_hold'] == 'input'
            assert not [effect for effect in devices.effects if effect[0] == 'place']
            assert client('status', {'job_id': refused[0][0]})['job']['state'] == 'refused'
            stopped = programme.disposition(basket['id'], 'Release the verified basket at input.')
            assert stopped['state'] == 'stopped'
            assert stopped['recovery_observation']['unload']['basket'] is None
            assert executor.devices.robot.basket is None
    finally:
        executor.close()


def test_retained_witness_fences_truncated_measured_rows_after_restart(tmp_path):
    executor = Executor(tmp_path / 'ipc', FakeDevices(setpoints={'tank-0': 30}))
    try:
        root = tmp_path / 'programme'
        programme = Programme(root, executor.dispatch, steps('seal'))
        start(programme)
        completed = advance(programme, lambda run: run['state'] == 'succeeded')
        data = json.loads(programme.path.read_text())
        stored = data['runs'][completed['id']]
        stored['steps'].clear()
        stored['cursor'] = 0
        damaged = json.dumps(data)
        programme.path.write_text(damaged)
        restarted = Programme(root, executor.dispatch, steps('seal'))
        assert 'call a technician' in (restarted.review()['block'] or '')
        assert programme.path.read_text() == damaged
    finally:
        executor.close()


@pytest.mark.parametrize('phase,revised', [('programme.json', False), ('settled witness', True)])
def test_interrupted_history_publish_keeps_last_atomic_record(tmp_path, monkeypatch, phase, revised):
    import anodizing_web.programme as module

    link = RecordedLink()
    programme = Programme(tmp_path, link, steps('seal'))
    original = programme.status()['recipe']
    atomic_json = module.atomic_json
    inject = True

    def interrupted(path, value, **options):
        if inject and (path.name == phase or (
                phase == 'settled witness' and path.name == 'programme-history.json'
                and 'previous' not in value)):
            raise OSError('Simulated interruption at atomic replacement')
        return atomic_json(path, value, **options)

    monkeypatch.setattr(module, 'atomic_json', interrupted)
    with pytest.raises(OSError, match='interruption'):
        programme.save(original['id'], {'step-0': {'min': 29}})
    inject = False
    restarted = Programme(tmp_path, link, steps('seal'))
    assert restarted.review()['block'] is None
    recipe = restarted.status()['recipe']
    assert (recipe['id'] != original['id']) is revised
    assert recipe['steps'][0]['limits']['min'] == (29 if revised else 30)


def test_parked_stopped_basket_records_measured_air_alarm(tmp_path):
    devices = FakeDevices(setpoints={'tank-0': 30, 'tank-1': 30})
    executor = Executor(tmp_path / 'ipc', devices)
    try:
        programme = Programme(tmp_path / 'programme', executor.dispatch, steps('ultrasonic', 'seal'))
        draft = programme.status()['recipe']
        recipe = programme.save(draft['id'], {'step-0': {'air': 2}})
        programme.start(recipe['id'], 'alarm-basket', programme.review()['approval'])
        advance(programme, lambda run: run['final_hold'] == 'park' and not run['ack_pending'])
        programme.stop()
        stopped = advance(programme, lambda run: run['state'] == 'recovery required' and run['stop']['confirmed'])
        assert not stopped['steps'][0]['inspection']
        devices.plc.clock += 7
        executor._update_open_air()
        programme.tick()
        current = programme.status()['run']
        assert current['inspection'] and current['steps'][0]['open_air_alarm_s'] >= 7
        assert current['state'] == 'recovery required'
        assert current['steps'][0]['air_s'] is None
        assert devices.effects[-1] == ('lift', 'park')
    finally:
        executor.close()
