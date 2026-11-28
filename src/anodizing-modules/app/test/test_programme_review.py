"""Regression checks for the Katana basket journal and operator actions."""
from threading import Event, Thread
import time

import pytest

from anodizing_web.executor import PROTOCOL, Executor, FakeDevices
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


def test_stop_preserves_inspection_flag_on_completed_basket(tmp_path):
    devices = FakeDevices(setpoints=FAKE_TANK_SETPOINTS)
    devices.plc.dwell_offset = 61
    executor = Executor(tmp_path / 'ipc', devices)
    try:
        programme = Programme(tmp_path / 'katana', executor.dispatch, steps('ultrasonic'))
        started = start(programme)
        inspected = advance(programme, lambda run: run['state'] == 'succeeded')
        assert inspected['steps'][0]['inspection']
        stopped = programme.stop()
        assert stopped['state'] == 'succeeded'
        assert stopped['stop']['received']
        advance(programme, lambda run: run['stop']['confirmed'])
        assert programme.retained_run(started['id'])['steps'] == inspected['steps']
        assert start(programme)['id'] != started['id']
    finally:
        executor.close()


class RecordedLink:
    def __init__(self):
        self.boot = 'original'
        self.job = None
        self.session = None

    def __call__(self, operation, body=None):
        if operation == 'health':
            return {'boot': self.boot, 'protocol': PROTOCOL, 'motion_enabled': True, 'simulated': True,
                    'job_types': {'station_visit': 1, 'dip': 1}, 'heartbeat_s': 1, 'lease_s': 3}
        if operation == 'heartbeat':
            self.session = body['session']
            return {'lease_valid': True, 'boot': self.boot}
        if operation in ('status', 'arm'):
            return {'boot': self.boot, 'stop_generation': 0, 'device': {'connected': True},
                    'armed': True, 'job': self.job, 'lease_valid': True, 'session': self.session}
        if operation == 'submit':
            checkpoint = {'sequence': 1, 'boot': self.boot,
                          'intent': {'effect': 'load_input', 'value': body['params']}, 'confirmed': [], 'steps': []}
            self.job = {'job_id': body['job_id'], 'state': 'running', 'checkpoint': checkpoint, 'result': {}, 'outcome': None}
            return self.job
        raise AssertionError(operation)


def test_running_checkpoint_remains_visible_when_link_disappears(tmp_path):
    link = RecordedLink()
    programme = Programme(tmp_path, link, steps('seal'))
    start(programme)
    pending = advance(programme, lambda run: run['pending'])
    assert pending.get('checkpoint') == link.job['checkpoint']
    restarted = Programme(tmp_path, link, steps('seal')).status()['run']
    assert restarted['checkpoint'] == link.job['checkpoint']
    assert restarted['pending'] == pending['pending']
    assert restarted['state'] == 'operator_check'


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


def test_gateway_calibration_requires_arm_from_the_agv_page(tmp_path):
    class Gateway(RecordedLink):
        def __init__(self):
            super().__init__()
            self.armed_for = None

        def __call__(self, operation, body=None):
            assert operation != 'arm', 'Calibration may not Arm the gateway'
            status = super().__call__(operation, body)
            if operation == 'status':
                status.update(owner='calibration', armed_for=self.armed_for, armed=self.armed_for is not None)
            return status

    link = Gateway()
    programme = Programme(tmp_path, link, steps('seal'), operator_arm=True)
    assert 'Arm for the Calibration module' in programme.review()['block']
    link.armed_for = 'calibration'
    start(programme)
    assert advance(programme, lambda run: run['pending'])['state'] == 'running'


def test_calibration_expiry_polls_running_job_without_stopping_it(tmp_path):
    class Gateway(RecordedLink):
        def __init__(self):
            super().__init__()
            self.expired = False
            self.calls = []

        def __call__(self, operation, body=None):
            self.calls.append(operation)
            assert operation not in ('arm', 'stop', 'hold', 'operator_check')
            response = super().__call__(operation, body)
            if operation == 'status':
                response.update(owner='calibration', armed_for=None if self.expired else 'calibration',
                                armed=not self.expired, module_attestation_expired=self.expired)
            return response

    link = Gateway()
    programme = Programme(tmp_path, link, steps('seal'), operator_arm=True)
    start(programme)
    pending = advance(programme, lambda run: run['pending'])['pending']
    link.expired = True
    programme.tick()
    run = programme.status()['run']
    assert run['state'] == 'running'
    assert run['pending'] == pending
    assert run.get('operator_check') is None
    assert link.calls.count('submit') == 1
