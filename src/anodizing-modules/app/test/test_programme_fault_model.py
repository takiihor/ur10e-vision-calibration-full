"""Phase 1 controller fault boundaries with fake IPC and persisted journals."""
from copy import deepcopy
import json

import pytest

from anodizing_web.executor import Executor, FakeDevices
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


@pytest.mark.parametrize('contradiction', ['dwell', 'plc', 'matching-copies', 'same-fabricated-plc'])
def test_contradictory_terminal_cannot_erase_measured_dwell(tmp_path, contradiction):
    executor = Executor(tmp_path / 'ipc', FakeDevices(setpoints={'tank-0': 30}))
    injected = []
    measured = []

    def exchange(operation, body=None):
        reply = deepcopy(executor.dispatch(operation, body))
        job = reply.get('job') if operation == 'status' else reply if operation == 'submit' else None
        if job and job.get('outcome') == 'succeeded' and job['result'].get('steps'):
            measured[:] = deepcopy(programme.run['steps'])
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
        assert run['steps'] == measured
        assert run['state'] == 'operator_check'
    finally:
        executor.close()


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
        parked = advance(programme, lambda run: run['final_hold'] == 'park')
        if kind == 'air':
            exposure = executor.open_air
            assert exposure
            devices.plc.clock += 7
            executor._update_open_air()
            alarm = executor.status()['quality_alarm']
            assert alarm['measured_air_s'] >= 7
        else:
            assert parked['steps'][0]['dwell_s'] >= 40
        completed = advance(programme, lambda run: run['state'] == 'succeeded')
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


def test_stop_delivers_before_waiting_for_tick_lock(tmp_path):
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
        with programme.tick_lock:
            worker.start()
            assert forwarded.wait(1), 'Stop delivery waited for the dispatch lock'
    finally:
        worker.join()


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
        advance(programme, lambda run: run['final_hold'] == 'park')
        programme.stop()
        stopped = advance(programme, lambda run: run['state'] == 'operator_check' and run['stop']['confirmed'])
        assert not stopped['steps'][0]['inspection']
        devices.plc.clock += 7
        executor._update_open_air()
        programme.tick()
        current = programme.status()['run']
        assert current['inspection'] and current['steps'][0]['open_air_alarm_s'] >= 7
        assert current['state'] == 'operator_check'
        assert current['steps'][0]['air_s'] is None
        assert devices.effects[-1] == ('lift', 'park')
    finally:
        executor.close()


@pytest.mark.parametrize('fault', ['identity', 'intent', 'steps', 'lift', 'sequence'])
def test_invalid_checkpoint_holds_without_replacing_good_evidence(tmp_path, fault):
    executor = Executor(tmp_path / 'ipc', FakeDevices(setpoints={'tank-0': 30, 'tank-1': 30}))
    injected = []

    def exchange(operation, body=None):
        reply = deepcopy(executor.dispatch(operation, body))
        job = reply.get('job') if operation == 'status' and body else reply if operation == 'submit' else None
        pending = programme.run and programme.run['pending']
        if not injected and job and pending and pending['type'] == 'dip':
            checkpoint = job['checkpoint']
            if fault == 'steps' and checkpoint:
                injected.append((job['job_id'], deepcopy(programme.run['checkpoint'])))
                job.update(state='running', outcome=None, result={},
                           checkpoint=dict(boot=checkpoint['boot'], sequence=0, confirmed=[], intent=None))
            elif fault != 'steps' and job.get('outcome') == 'succeeded':
                placement = next(entry for entry in checkpoint['confirmed'] if entry['effect'] == 'place')
                injected.append((job['job_id'], deepcopy(programme.run['checkpoint'])))
                if fault == 'identity':
                    placement['value']['basket'] = 'another basket'
                elif fault == 'intent':
                    checkpoint['intent'] = dict(effect='place', value=placement['value'])
                elif fault == 'sequence':
                    checkpoint['sequence'] += 100
                else:
                    lift = next(entry for entry in checkpoint['confirmed'] if entry['effect'] == 'lift')
                    lift['evidence']['location'] = 'input'
                    job.update(state='running', outcome=None, result={})
        return reply

    try:
        programme = Programme(tmp_path / 'katana', exchange, steps('ultrasonic', 'seal'))
        start(programme)
        held = advance(programme, lambda _: injected)
        assert held['state'] == 'operator_check'
        assert held['pending']['job_id'] == injected[0][0]
        assert held['checkpoint'] == injected[0][1]
    finally:
        executor.close()


@pytest.mark.parametrize('fault', [None, 'missing', 'contradiction'])
def test_abort_retains_original_job_after_lost_final_reply(tmp_path, fault):
    from test_executor_phase1 import terminal

    executor = Executor(tmp_path / 'ipc', FakeDevices(setpoints={'tank-0': 30}))
    lost = []

    def exchange(operation, body=None):
        reply = executor.dispatch(operation, body)
        if operation == 'submit' and body['type'] == 'dip':
            lost.append(terminal(executor, body['job_id']))
            raise OSError('Lost successful final dip submit reply')
        if operation == 'status' and body and lost:
            if fault == 'missing':
                reply['job'] = None
            elif fault == 'contradiction':
                reply['job']['checkpoint']['sequence'] += 100
        return reply

    try:
        root = tmp_path / 'katana'
        programme = Programme(root, exchange, steps('seal'))
        opened = start(programme)
        advance(programme, lambda run: run['state'] == 'operator_check' and executor.status()['operator_check'])
        decided = programme.decide(opened['id'], 'abort', programme.review()['approval'], operator='Inspector',
                                   basket=opened['basket'], location='output', confirmed=True)
        assert decided['state'] == 'aborted' and decided['cursor'] == 0
        assert executor.status()['operator_check'] is None
        if fault:
            assert decided['steps'] == [] and decided['operator_choices'][-1]['evidence_note']
            fault = None
        restarted = Programme(root, exchange, steps('seal'))
        retained = restarted.retained_run(opened['id'])
        assert len(lost) == 1 and lost[0]['outcome'] == 'succeeded'
        assert retained['steps'][0]['plc'] == lost[0]['result']['steps'][0]['plc']
        assert retained['jobs'][-1]['job'] == lost[0]
        assert not retained['operator_choices'][-1].get('evidence_note')
        assert restarted.status()['run'] == retained
    finally:
        executor.close()


def test_failed_stop_attempt_can_be_covered_by_a_newer_settled_stop(tmp_path, monkeypatch):
    devices = FakeDevices(setpoints={'tank-0': 30})
    original_stop = devices.stop
    monkeypatch.setattr(devices, 'stop', lambda: False)
    executor = Executor(tmp_path / 'ipc', devices)
    try:
        programme = Programme(tmp_path / 'katana', executor.dispatch, steps('seal'))
        start(programme)
        first = programme.stop()['stop_id']
        first_done, _ = executor.stop_tickets[first]
        assert first_done.wait(1)
        monkeypatch.setattr(devices, 'stop', original_stop)
        executor.stop({'stop_id': 'other-stop'})
        held = advance(programme, lambda run: not run['stop_pending'] and executor.status()['operator_check'])
        assert held['stop']['stop_id'] == first
        assert held['stop']['halt_attempted'] is False
        assert executor.status()['stop']['stop_id'] == 'other-stop'
    finally:
        executor.close()


def test_active_history_write_error_enters_releasable_check(tmp_path, monkeypatch):
    import anodizing_web.programme as module

    root = tmp_path / 'katana'
    executor = Executor(tmp_path / 'ipc', FakeDevices(setpoints={'tank-0': 30}))
    atomic_json = module.atomic_json
    try:
        programme = Programme(root, executor.dispatch, steps('seal'))
        started = start(programme)

        def fail_once(path, value, **options):
            if path == programme.path:
                monkeypatch.setattr(module, 'atomic_json', atomic_json)
                raise OSError('Transient programme write failure')
            return atomic_json(path, value, **options)

        monkeypatch.setattr(module, 'atomic_json', fail_once)
        with pytest.raises(OSError, match='Transient programme write failure'):
            programme.stop()
        held = advance(programme, lambda run: run['state'] == 'operator_check'
                       and not run['stop_pending'] and executor.status()['operator_check'])
        assert not held['operator_check']['technician']
        assert programme.review()['block'] is None
        decided = programme.decide(started['id'], 'abort', programme.review()['approval'], operator='Inspector',
                                   basket=started['basket'], location='input', confirmed=True)
        assert decided['state'] == 'aborted'
    finally:
        executor.close()


def test_standalone_stop_is_durable_while_first_delivery_is_blocked(tmp_path):
    from threading import Event, Thread

    blocked, release = Event(), Event()
    link = RecordedLink()
    observed = []
    intent = tmp_path / 'programme-stop.json'

    def exchange(operation, body=None):
        if operation == 'stop':
            observed.append(body)
            blocked.set()
            assert release.wait(5)
            return dict(received=False, confirmed=False)
        return link(operation, body)

    programme = Programme(tmp_path, exchange, steps('seal'))
    started = start(programme)
    def request_stop():
        try:
            programme.stop()
        except ValueError:
            pass  # The restarted instance has replaced the active controller's files.

    worker = Thread(target=request_stop)
    try:
        worker.start()
        assert blocked.wait(1)
        from time import monotonic, sleep

        deadline = monotonic() + 1
        while not intent.is_file() and monotonic() < deadline:
            sleep(.005)
        published = json.loads(intent.read_text())
        assert published['stop_id'] == observed[0]['stop_id']
        assert published['confirmed'] is False
        received = []

        def restarted_exchange(operation, body=None):
            received.append((operation, body))
            return dict(received=False, confirmed=False)

        restarted = Programme(tmp_path, restarted_exchange, steps('seal'))
        restarted.tick()
        assert received[0][0] == 'stop'
        assert received[0][1]['stop_id'] == published['stop_id']
        assert received[0][1]['run_id'] == started['id']
    finally:
        release.set()
        worker.join()


@pytest.mark.parametrize('reboot', [False, True])
def test_parked_resume_preserves_outgoing_air_after_a_hold(tmp_path, reboot):
    snapshot = tmp_path / 'devices.json'
    setpoints = {'tank-0': 30, 'tank-1': 30}
    devices = FakeDevices(setpoints=setpoints, state_path=snapshot)
    executor = Executor(tmp_path / 'ipc', devices)
    try:
        programme = Programme(tmp_path / 'katana', executor.dispatch, steps('ultrasonic', 'seal'))
        recipe = programme.save(programme.status()['recipe']['id'], {'step-0': {'air': 2}})
        opened = programme.start(recipe['id'], 'basket', programme.review()['approval'])
        advance(programme, lambda run: run['cursor'] == 1)
        programme.stop()
        advance(programme, lambda run: not run['stop_pending'] and executor.status()['operator_check'])
        if reboot:
            executor.close()
            devices = FakeDevices(setpoints=setpoints, state_path=snapshot)
            executor = Executor(tmp_path / 'ipc', devices)
            programme.request = executor.dispatch
            programme.next_pulse = 0
            advance(programme, lambda run: not run['stop_pending'] and programme.review()['approval'])
        devices.plc.clock += 10
        programme.decide(opened['id'], 'resume', programme.review()['approval'], operator='Inspector',
                         basket=opened['basket'], location='park', confirmed=True, step=2)
        finished = advance(programme, lambda run: run['state'] != 'running')
        first, second = finished['steps']
        assert finished['state'] == 'succeeded' and first['inspection'] and finished['inspection']
        if reboot:
            assert first['air_s'] is None and second['incoming_air_s'] is None
        else:
            assert first['air_s'] == second['incoming_air_s'] >= 10
    finally:
        executor.close()


def test_next_basket_can_resume_after_previous_aborted_at_park(tmp_path):
    executor = Executor(tmp_path / 'ipc', FakeDevices(setpoints={'tank-0': 30, 'tank-1': 30}))
    try:
        programme = Programme(tmp_path / 'katana', executor.dispatch, steps('ultrasonic', 'seal'))
        previous = start(programme)
        advance(programme, lambda run: run['cursor'] == 1)
        programme.stop()
        advance(programme, lambda run: not run['stop_pending'] and executor.status()['operator_check'])
        aborted = programme.decide(previous['id'], 'abort', programme.review()['approval'], operator='Inspector',
                                   basket=previous['basket'], location='park', confirmed=True)
        assert aborted['state'] == 'aborted' and executor.devices.robot.basket == previous['basket']
        current = programme.start(programme.status()['recipe']['id'], 'next-basket', programme.review()['approval'])
        held = advance(programme, lambda run: run['state'] == 'operator_check' and executor.status()['operator_check'])
        assert held['operator_check']['basket'] == previous['basket']
        programme.decide(current['id'], 'resume', programme.review()['approval'], operator='Inspector',
                         basket=current['basket'], location='input', confirmed=True, step=1)
        assert advance(programme, lambda run: run['state'] != 'running')['state'] == 'succeeded'
        assert programme.retained_run(previous['id']) == aborted
    finally:
        executor.close()


def test_late_stop_reply_cannot_reopen_settled_ticket(tmp_path):
    from threading import Event, Thread

    executor = Executor(tmp_path / 'ipc', FakeDevices(setpoints={'tank-0': 30}))
    blocked, release = Event(), Event()

    def exchange(operation, body=None):
        if operation == 'stop' and not blocked.is_set():
            blocked.set()
            assert release.wait(5)
            return {'received': False, 'confirmed': False}
        return executor.dispatch(operation, body)

    programme = Programme(tmp_path / 'katana', exchange, steps('seal'))
    start(programme)
    first = Thread(target=programme.stop)
    try:
        first.start()
        assert blocked.wait(1)
        programme.stop()
        advance(programme, lambda run: not run['stop_pending'] and executor.status()['operator_check'])
        release.set()
        first.join()
        assert not programme.status()['run']['stop_pending']
    finally:
        release.set()
        first.join()
        executor.close()


@pytest.mark.parametrize('outcome', ['failed', 'unknown', 'stopped'])
def test_operator_abort_retains_failed_original_even_without_checkpoint(tmp_path, outcome):
    executor = Executor(tmp_path / 'ipc', FakeDevices(setpoints={'tank-0': 30}))
    captured = {}

    def exchange(operation, body=None):
        if operation == 'submit':
            captured.update(job_id=body['job_id'],
                            state={'failed': 'failed', 'unknown': 'interrupted', 'stopped': 'stopped'}[outcome],
                            outcome=outcome, result={}, checkpoint=None, error='No physical effect verified.')
            raise OSError('Lost job reply.')
        if operation == 'status' and body and body.get('job_id') == captured.get('job_id'):
            return dict(executor.status(), job=deepcopy(captured))
        return executor.dispatch(operation, body)

    try:
        programme = Programme(tmp_path / 'katana', exchange, steps('seal'))
        opened = start(programme)
        advance(programme, lambda run: run['state'] == 'operator_check' and executor.status()['operator_check'])
        decided = programme.decide(opened['id'], 'abort', programme.review()['approval'], operator='Inspector',
                                   basket=opened['basket'], location='input', confirmed=True)
        assert decided['state'] == 'aborted' and decided['steps'] == []
        assert [entry['job'] for entry in decided['jobs']] == [captured]
    finally:
        executor.close()


def test_standalone_stop_reaches_ipc_while_its_witness_write_is_blocked(tmp_path, monkeypatch):
    from threading import Event, Thread
    import anodizing_web.programme as module

    writing, release, delivered = Event(), Event(), Event()
    original = module.atomic_json

    def exchange(operation, body=None):
        assert operation == 'stop'
        delivered.set()
        return None

    programme = Programme(tmp_path, exchange, steps('seal'))

    def stalled_write(path, value, **options):
        if path == programme.stop_path:
            writing.set()
            assert release.wait(5)
        return original(path, value, **options)

    monkeypatch.setattr(module, 'atomic_json', stalled_write)
    thread = Thread(target=programme.stop)
    try:
        thread.start()
        assert writing.wait(1)
        assert delivered.wait(1), 'Stop waited for its own journal'
    finally:
        release.set()
        thread.join()


def test_new_programme_stop_never_uses_operator_request_identity(tmp_path):
    from anodizing_web.server import Handler, OperatorActions
    from types import SimpleNamespace

    sent = []

    def exchange(operation, body=None):
        assert operation == 'stop'
        sent.append(body['stop_id'])
        return None

    actions = OperatorActions(tmp_path, lambda *args, **kwargs: None)
    actions.execute('programme', {'action': 'start', 'request_id': 'used-start', 'review_cursor': actions.cursor},
                    lambda: {'id': 'prior-run'}, None, required=True)
    programme = Programme(tmp_path, exchange, steps('seal'))
    handler = SimpleNamespace(server=SimpleNamespace(programme=programme, programme_uses_executor=False))
    stop_requests = [{}, {'request_id': 'tab-1'}, {'request_id': 'tab-1'}, {'request_id': 'used-start'}]
    for fields in stop_requests:
        body = {'action': 'stop', **fields}
        actions.execute('programme', body, lambda: Handler._programme_action(handler, body), None, stop=True)
    assert len(set(sent)) == len(sent) == len(stop_requests)
    assert all(len(ident) == 32 for ident in sent)
    programme.tick()
    assert sent[-1] == sent[-2]


def test_stop_receipt_write_failure_keeps_dispatch_worker_alive(tmp_path, monkeypatch):
    from threading import Event
    import anodizing_web.programme as module

    executor = Executor(tmp_path / 'ipc', FakeDevices(setpoints={'tank-0': 30}))
    programme = Programme(tmp_path / 'katana', executor.dispatch, steps('seal'))
    atomic_json = module.atomic_json
    failed, saved = Event(), Event()

    def fail_once(path, value, **options):
        confirmed = path == programme.path and (programme.run.get('stop') or {}).get('confirmed')
        if confirmed and not failed.is_set():
            failed.set()
            raise OSError('Failed to persist confirmed Stop')
        original_result = atomic_json(path, value, **options)
        if confirmed:
            saved.set()
        return original_result

    try:
        start(programme)
        programme.stop()
        monkeypatch.setattr(module, 'atomic_json', fail_once)
        programme.start_service()
        assert failed.wait(2)
        assert saved.wait(2), 'Worker did not retry the confirmed Stop receipt'
        assert programme.worker.is_alive()
    finally:
        programme.close()
        executor.close()


def test_shared_programme_uses_link_stop_acceptance(tmp_path):
    def exchange(operation, body=None):
        assert operation == 'stop', 'A second read cannot qualify the same Stop on an earlier boot.'
        return dict(stop_id=body['stop_id'], generation=1, boot='newest', received=True, confirmed=True)

    programme = Programme(tmp_path, exchange, steps('seal'), pulse=lambda: {})
    assert programme.stop()['confirmed']
