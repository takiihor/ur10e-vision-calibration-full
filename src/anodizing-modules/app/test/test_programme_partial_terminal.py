"""A truncated but syntactically complete result cannot advance to another physical job."""
from copy import deepcopy

import pytest

from anodizing_web.executor import Executor, FakeDevices
from anodizing_web.programme import Programme
from test_programme_review import advance, start, steps


@pytest.mark.parametrize("defect", ["station_hold", "dip_rows", "paired_prefix", "wrong_step"])
def test_partial_terminal_requires_operator_check_even_after_good_reply(tmp_path, defect):
    roles = ("etch", "rinse") if defect == "paired_prefix" else ("seal",)
    devices = FakeDevices(setpoints={f"tank-{i}": 30 for i in range(len(roles))})
    executor = Executor(tmp_path / "ipc", devices)
    submitted = {}
    damaged = []
    corrupt = True

    def exchange(operation, body=None):
        if operation == "submit":
            submitted[body["job_id"]] = body["type"]
        reply = deepcopy(executor.dispatch(operation, body))
        job = reply.get("job") if operation == "status" else reply if operation == "submit" else None
        target = "station_visit" if defect == "station_hold" else "dip"
        if (corrupt and job and job["outcome"] == "succeeded"
                and submitted.get(job["job_id"]) == target):
            damaged.append(job["job_id"])
            if defect == "station_hold":
                job["result"].pop("hold")
            elif defect == "wrong_step":
                job["result"]["steps"][0]["id"] = "another-step"
            else:
                job["result"]["steps"] = job["result"]["steps"][:-1]
        return reply

    try:
        programme = Programme(tmp_path / "katana", exchange, steps(*roles))
        start(programme)
        run = advance(programme, lambda r: bool(damaged))
        original_id = damaged[0]
        assert run["pending"] and run["pending"]["job_id"] == original_id
        assert run["state"] == "operator_check"
        assert run["operator_check"]["job_id"] == original_id
        assert run["cursor"] == 0
        before = dict(submitted)
        corrupt = False
        for _ in range(3):
            programme.tick()
        assert programme.status()["run"]["state"] == "operator_check"
        assert submitted == before
    finally:
        executor.close()


@pytest.mark.parametrize('choice', ['resume', 'abort'])
def test_operator_check_records_physical_choice_after_lost_submit(tmp_path, choice):
    devices = FakeDevices(setpoints={'tank-0': 30})
    executor = Executor(tmp_path / 'ipc', devices)
    submitted = []

    def exchange(operation, body=None):
        if operation == 'submit':
            submitted.append(body['job_id'])
            if len(submitted) == 1:
                raise OSError('Lost first outbound submit request.')
        return executor.dispatch(operation, body)

    try:
        root = tmp_path / 'katana'
        programme = Programme(root, exchange, steps('seal'))
        opened = start(programme)
        held = advance(programme, lambda run: run['state'] == 'operator_check')
        assert held['operator_check']['job_id'] == submitted[0]
        assert not devices.effects and not executor.status()['armed']
        with pytest.raises(ValueError, match='Physically check'):
            programme.decide(opened['id'], choice, programme.review()['approval'], operator='Inspector',
                             basket=opened['basket'], location='input', confirmed=False, step=1)
        approval = programme.review()['approval']
        decided = programme.decide(opened['id'], choice, approval, operator='Inspector',
                                   basket=opened['basket'], location='input', confirmed=True, step=1)
        assert decided['operator_choices'][-1]['operator'] == 'Inspector'
        assert decided['operator_choices'][-1]['applied'] is True
        assert not devices.effects and not executor.status()['armed']
        if choice == 'resume':
            finished = advance(programme, lambda run: run['state'] == 'succeeded')
            assert finished['steps'][0]['dwell_s'] >= 30
            assert len(submitted) == len(set(submitted)) == 3
        else:
            assert decided['state'] == 'aborted' and decided['inspection']
            assert len(submitted) == 1 and not devices.effects
        retained = Programme(root, exchange, steps('seal')).status()['run']
        assert retained['operator_choices'][-1]['choice'] == choice
    finally:
        executor.close()


def test_resume_earlier_step_keeps_each_measured_attempt(tmp_path):
    devices = FakeDevices(setpoints={'tank-0': 30, 'tank-1': 30})
    executor = Executor(tmp_path / 'ipc', devices)
    try:
        root = tmp_path / 'katana'
        programme = Programme(root, executor.dispatch, steps('ultrasonic', 'seal'))
        opened = start(programme)
        parked = advance(programme, lambda run: run['cursor'] == 1)
        programme.stop()
        advance(programme, lambda run: not run['stop_pending'] and executor.status()['operator_check'])
        programme.decide(opened['id'], 'resume', programme.review()['approval'], operator='Inspector',
                         basket=opened['basket'], location='park', confirmed=True, step=1)
        finished = advance(programme, lambda run: run['state'] == 'succeeded')
        assert finished['cursor'] == 2
        original = finished['steps'][0]
        assert dict(original, air_s=None) == parked['steps'][0]
        assert original['air_s'] == pytest.approx(finished['steps'][1]['placed_s'] - original['lift_s'])
        assert [row['id'] for row in finished['steps']] == ['step-0', 'step-0', 'step-1']
        assert len({row['job_id'] for row in finished['steps']}) == 3
        assert Programme(root, executor.dispatch, steps('ultrasonic', 'seal')).status()['run'] == finished
    finally:
        executor.close()


def test_unreachable_hold_operation_falls_back_to_priority_stop(tmp_path):
    devices = FakeDevices(setpoints={'tank-0': 30})
    executor = Executor(tmp_path / 'ipc', devices)
    operations = []

    def exchange(operation, body=None):
        operations.append(operation)
        if operation in ('submit', 'hold'):
            raise OSError('Simulated loss of physical control request')
        return executor.dispatch(operation, body)

    try:
        programme = Programme(tmp_path / 'katana', exchange, steps('seal'))
        start(programme)
        held = advance(programme, lambda run: run['state'] == 'operator_check')
        assert held['pending']['job_id'] == held['operator_check']['job_id']
        assert 'hold' in operations and 'stop' in operations
        assert not executor.status()['armed'] and not devices.effects
    finally:
        executor.close()
