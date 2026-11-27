"""R3 IPC boundary regressions with only fake tank and AGV evidence."""
import json
import secrets
import multiprocessing
import os
from pathlib import Path
import time

import pytest

from anodizing_web.executor import PROTOCOL, Executor, FakeDevices
from anodizing_web.executor_http import Client, control_listener

from test_executor_phase1 import Clock, FAKE_TANK_SETPOINTS, pair, running, session, terminal, visit, wait_for


def lease(executor):
    identity = dict(boot=executor.boot, protocol=PROTOCOL, session='r3-controller')
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
        at_park = terminal(executor, request['job_id'])
        assert at_park['outcome'] == 'succeeded'
        clock.now += 120

        def expired_state():
            return executor.status(request['job_id'])

        state = wait_for(expired_state, lambda state: state['quality_alarm'] and not executor.armed)
        assert state['quality_alarm']['job_id'] == request['job_id']
        assert state['quality_alarm']['open_air']
        assert state['quality_alarm']['measured_air_s'] == 120
        assert state['job']['checkpoint'] == at_park['checkpoint']
        row = state['job']['result']['steps'][0]
        assert row['inspection'] and row['air_s'] is None
    finally:
        executor.close()


def test_http_admission_refusal_distinguishes_absent_acceptance(tmp_path):
    devices = FakeDevices(setpoints={})
    executor = Executor(tmp_path, devices)
    try:
        request = dip(session(executor))
        token = secrets.token_urlsafe(32)
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
            assert client('submit', request) == job
        assert devices.effects == []
    finally:
        executor.close()
