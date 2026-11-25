"""Regression checks for the Katana basket journal and operator actions."""
from threading import Event, Thread
import time
from copy import deepcopy

import pytest

from anodizing_web.executor import Executor, FakeDevices
from anodizing_web.programme import Programme


from test_executor_phase1 import FAKE_TANK_SETPOINTS

def steps(*roles):
    return [{'id': f'step-{index}', 'tank': f'tank-{index}', 'role': role,
             'station': index + 1, 'setpoint': 30} for index, role in enumerate(roles)]


def start(programme):
    return programme.start(programme.status()['recipe']['id'], 'basket-one', programme.review()['approval'])


def advance(programme, predicate):
    until = time.monotonic() + 5
    while time.monotonic() < until:
        programme.tick()
        run = programme.status()['run']
        if predicate(run):
            return run
        time.sleep(.005)
    pytest.fail(f'Programme did not reach expected boundary: {run}')


def test_stop_reaches_executor_while_basket_journal_is_busy(tmp_path):
    sent = Event()

    def request(operation, _body=None):
        if operation == 'health':
            return RecordedLink()('health')
        if operation == 'status':
            return {'boot': 'original', 'stop_generation': 0}
        assert operation == 'stop'
        sent.set()
        return {'received': True, 'confirmed': True}

    programme = Programme(tmp_path, request, steps('seal'))
    start(programme)
    thread = Thread(target=programme.stop)
    try:
        with programme.lock:
            thread.start()
            assert sent.wait(1), 'Stop waited for the occupied journal lock'
    finally:
        thread.join()


def test_stop_preserves_inspection_until_explicit_disposition(tmp_path):
    devices = FakeDevices(setpoints=FAKE_TANK_SETPOINTS)
    devices.plc.dwell_offset = 61
    executor = Executor(tmp_path / 'ipc', devices)
    try:
        programme = Programme(tmp_path / 'katana', executor.dispatch, steps('ultrasonic'))
        started = start(programme)
        inspected = advance(programme, lambda run: run['state'] == 'inspection')
        assert inspected['steps'][0]['inspection']
        assert programme.stop()['state'] == 'inspection'
        with pytest.raises(ValueError, match='disposition'):
            programme.start(started['recipe']['id'], 'basket-two', programme.review()['approval'])
    finally:
        executor.close()


def test_new_basket_waits_for_previous_terminal_acknowledgement(tmp_path):
    executor = Executor(tmp_path / 'ipc', FakeDevices(setpoints=FAKE_TANK_SETPOINTS))
    try:
        programme = Programme(tmp_path / 'katana', executor.dispatch, steps('seal'))
        started = start(programme)
        completed = advance(programme, lambda run: run['state'] == 'succeeded')
        assert completed['ack_pending']
        with pytest.raises(ValueError, match='outstanding'):
            programme.start(started['recipe']['id'], 'basket-two', programme.review()['approval'])
        assert programme.stop()['state'] == 'succeeded'
        advance(programme, lambda run: run['ack_pending'] is None)
        started_again = programme.start(started['recipe']['id'], 'basket-two', programme.review()['approval'])
        assert started_again['id'] != started['id']
    finally:
        executor.close()


def test_resume_after_last_dip_lease_loss_closes_without_another_job(tmp_path):
    devices = FakeDevices(setpoints=FAKE_TANK_SETPOINTS)
    entered, release = Event(), Event()
    now = [10]
    perform = devices.perform

    def pause_dwell(effect, value):
        if effect == 'dwell':
            entered.set()
            assert release.wait(4)
        return perform(effect, value)

    devices.perform = pause_dwell
    executor = Executor(tmp_path / 'ipc', devices, clock=lambda: now[0])
    try:
        programme = Programme(tmp_path / 'katana', executor.dispatch, steps('seal'))
        started = start(programme)
        advance(programme, lambda run: run['pending'] and run['pending']['type'] == 'dip')
        assert entered.wait(4)
        now[0] += 3
        until = time.monotonic() + 1
        while not executor.loss.is_set() and time.monotonic() < until:
            time.sleep(.005)
        assert executor.loss.is_set()
        release.set()
        held = advance(programme, lambda run: run['state'] == 'held' and run['cursor'] == 1)
        assert held['final_hold'] == 'output'
        programme.next_pulse = 0
        programme.resume(started['id'], programme.review()['approval'])
        completed = advance(programme, lambda run: run['state'] == 'succeeded' and not run['ack_pending'])
        assert completed['steps'] == held['steps']
        assert len(completed['jobs']) == 2
        assert not executor.status()['armed']
    finally:
        release.set()
        executor.close()


def recovery_evidence(body):
    hold = dict(location='input', station=1, gripper='released', basket=body['basket'], run_id=body['run_id'],
                stopped=True, equipment_on=False)
    return dict(hold=hold, disposition=body['disposition'], boot=body['boot'],
                unload=dict(hold, basket=None, run_id=None),
                observation_id=body['observation_id'], observed_utc=time.time(), observed_mono_s=time.monotonic())


class RecordedLink:
    def __init__(self):
        self.boot = 'original'
        self.job = None
        self.submits = []
        self.reconciliations = []

    def __call__(self, operation, body=None):
        if operation == 'health':
            return {'boot': self.boot, 'protocol': 1, 'motion_enabled': True, 'simulated': True,
                    'job_types': {'station_visit': 1, 'dip': 1}, 'heartbeat_s': 1, 'lease_s': 3}
        if operation == 'heartbeat':
            return {'lease_valid': True, 'boot': self.boot}
        if operation in ('status', 'arm'):
            return {'boot': self.boot, 'stop_generation': 0, 'device': {'connected': True},
                    'armed': True, 'job': self.job, 'recovery_required': False}
        if operation == 'submit':
            self.submits.append(body)
            checkpoint = {'sequence': 3, 'boot': self.boot,
                          'intent': {'effect': 'station', 'value': 1}, 'confirmed': []}
            self.job = {'job_id': body['job_id'], 'state': 'running', 'checkpoint': checkpoint}
            return self.job
        if operation == 'reconcile':
            assert body['missing'] and body['job_id'] == self.submits[0]['job_id']
            self.reconciliations.append(body)
            self.job = {'job_id': body['job_id'], 'state': 'interrupted', 'outcome': 'unknown',
                        'checkpoint': None, 'result': {'hold': {'location': 'input'}}, 'error': None,
                        'reconciliation': recovery_evidence(body)}
            return self.job
        if operation == 'acknowledge':
            return {'acknowledged': True, 'job_id': body['job_id']}
        raise AssertionError(operation)


def test_running_checkpoint_remains_visible_when_link_disappears(tmp_path):
    link = RecordedLink()
    programme = Programme(tmp_path, link, steps('seal'))
    start(programme)
    pending = advance(programme, lambda run: run['pending'])
    assert pending.get('checkpoint') == link.job['checkpoint']
    assert Programme(tmp_path, link, steps('seal')).status()['run']['checkpoint'] == link.job['checkpoint']


def test_missing_job_after_ipc_reboot_gets_disposition_and_never_replays(tmp_path):
    link = RecordedLink()
    programme = Programme(tmp_path, link, steps('seal'))
    started = start(programme)
    pending = advance(programme, lambda run: run['pending'])['pending']
    link.boot, link.job = 'replacement', None
    programme.next_pulse = 0
    held = advance(programme, lambda run: run['state'] == 'recovery required' and run['pending_missing'])
    assert held['pending'] == pending
    with pytest.raises(ValueError):
        programme.resume(started['id'], programme.review()['approval'])
    recovered = programme.disposition(started['id'], 'Basket verified at input. End this run.')
    assert recovered['state'] == 'stopped'
    assert recovered['pending'] is None
    assert recovered['jobs'][-1]['job']['job_id'] == pending['job_id']
    assert len(link.reconciliations) == len(link.submits) == 1


def test_single_limit_edit_preserves_inheritance_and_the_running_snapshot(tmp_path):
    programme = Programme(tmp_path, RecordedLink(), steps('rinse', 'ultrasonic'))
    recipe = programme.status()['recipe']
    assert recipe['steps'][0]['limits']['air'] == 60
    edited = programme.save(recipe['id'], {'step-0': {'min': 29}})
    started = programme.start(edited['id'], 'basket-one', programme.review()['approval'])
    successor = programme.save(edited['id'], {'step-1': {'air': 12}})
    assert successor['steps'][0]['limits']['air'] == 12
    assert successor['steps'][0]['air_source'] == 'step-1'
    pinned = Programme(tmp_path, RecordedLink(), steps('rinse', 'ultrasonic')).status()['run']
    assert pinned['recipe'] == started['recipe']
    assert pinned['recipe']['steps'][0]['limits']['air'] == 60


def test_finalizing_with_bounds_requires_terminal_handling_and_cycle_evidence(tmp_path):
    programme = Programme(tmp_path, None, steps('seal'))
    recipe = programme.status()['recipe']
    bounded = programme.save(recipe['id'], {'step-0': {'min': 30, 'overstay': 1, 'air': None}})
    with pytest.raises(ValueError, match='terminal handling and timing evidence'):
        programme.finalize(bounded['id'])
    review = {'terminal_handling': 'Released at output with no next immersion.',
              'timing_evidence': 'Reviewed the cycle, done and equipment-off feedback.'}
    final = programme.finalize(bounded['id'], review)
    assert final['state'] == 'FINAL'
    assert final['review']['terminal_handling'] == review['terminal_handling']
    assert final['review']['timing_evidence'] == review['timing_evidence']
    assert final['review']['overstay_basis'] == {'step-0': 'setpoint'}


def test_missing_acknowledgement_preserves_the_completed_record(tmp_path):
    executor = Executor(tmp_path / 'ipc', FakeDevices(setpoints=FAKE_TANK_SETPOINTS))
    try:
        programme = Programme(tmp_path / 'katana', executor.dispatch, steps('seal'))
        started = start(programme)
        completed = advance(programme, lambda run: run['state'] == 'succeeded')
        assert completed['ack_pending']
        replacement = RecordedLink()
        replacement.submits.append({'job_id': completed['ack_pending']})
        replacement.boot = 'replacement'
        programme.request = replacement
        programme.next_pulse = 0
        held = advance(programme, lambda run: run.get('ack_missing'))
        assert held['state'] == 'succeeded'
        with pytest.raises(ValueError, match='outstanding'):
            programme.start(started['recipe']['id'], 'another-basket', programme.review()['approval'])
        finished = programme.disposition(started['id'], 'Verified output after the IPC lost its result.')
        assert finished['state'] == 'succeeded'
        assert finished['steps'] == completed['steps']
        advance(programme, lambda run: not run['ack_pending'])
        assert len(replacement.reconciliations) == 1
    finally:
        executor.close()


def test_reboot_between_jobs_requires_new_observation_at_last_confirmed_id(tmp_path):
    executor = Executor(tmp_path / 'ipc', FakeDevices(setpoints=FAKE_TANK_SETPOINTS))
    try:
        root = tmp_path / 'katana'
        programme = Programme(root, executor.dispatch, steps('seal'))
        started = start(programme)
        advance(programme, lambda run: run['ack_pending'] is not None)
        programme = Programme(root, executor.dispatch, steps('seal'))
        executor.deadline = None
        advance(programme, lambda run: run['ack_pending'] is None)
        original = programme.status()['run']['jobs'][-1]['job']
        observed = []

        def replacement(operation, body=None):
            if operation == 'reconcile':
                assert body.get('observed_only') and body['job_id'] == original['job_id']
                observed.append(body)
                return dict(original, observation=recovery_evidence(body))
            link = RecordedLink()
            link.boot, link.job = 'replacement', original
            return link(operation, body)

        programme.request = replacement
        programme.next_pulse = 0
        advance(programme, lambda run: run['state'] == 'recovery required')
        assert programme.disposition(started['id'], 'Verify the retained input hold.')['state'] == 'stopped'
        assert len(observed) == 1
    finally:
        executor.close()


@pytest.mark.parametrize('fault', ['wrong-job', 'old-boot', 'wrong-token', 'missing-time', 'wrong-key'])
def test_disposition_waits_for_matching_recovery_evidence(tmp_path, fault):
    devices = FakeDevices(setpoints=FAKE_TANK_SETPOINTS)
    devices.plc.dwell_offset = 10
    executor = Executor(tmp_path / 'ipc', devices)
    try:
        programme = Programme(tmp_path / 'katana', executor.dispatch, steps('ultrasonic'))
        recipe = programme.save(programme.status()['recipe']['id'], {'step-0': {'overstay': 3}})
        started = programme.start(recipe['id'], 'basket-one', programme.review()['approval'])
        advance(programme, lambda run: run['state'] == 'inspection' and not run['ack_pending'])

        def partial(operation, body=None):
            result = deepcopy(executor.dispatch(operation, body))
            if operation == 'reconcile':
                if fault == 'wrong-job':
                    result['job_id'] = 'different-job'
                elif fault == 'wrong-key':
                    result['observation'] = result.pop('reconciliation')
                else:
                    evidence = result['reconciliation']
                    if fault == 'old-boot':
                        evidence['boot'] = 'old-boot'
                    elif fault == 'wrong-token':
                        evidence['observation_id'] = 'different-token'
                    else:
                        evidence.pop('observed_mono_s')
            return result

        programme.request = partial
        with pytest.raises(OSError, match='recovery evidence'):
            programme.disposition(started['id'], 'Verified fake equipment and output hold.')
        assert programme.status()['run']['state'] == 'inspection'
        assert programme.status()['run']['disposition_intent']
        programme.request = executor.dispatch
        assert programme.disposition(started['id'], 'Verified fake equipment and output hold.')['state'] == 'stopped'
    finally:
        executor.close()
