"""The IPC advertises the commissioning clocks used by both lease owners."""
import time

import pytest

from anodizing_web.executor import Executor, FakeDevices
from anodizing_web.programme import Programme
from test_executor_phase1 import Clock, session
from test_executor_link_review import operator, wait_for


def test_configured_lease_expires_on_the_ipc_monotonic_boundary(tmp_path, monkeypatch):
    monkeypatch.setenv('ANODIZING_HEARTBEAT_S', '0.2')
    monkeypatch.setenv('ANODIZING_LEASE_S', '0.8')
    clock = Clock()
    executor = Executor(tmp_path, FakeDevices(), clock=clock)
    try:
        session(executor)
        health = executor.health()
        assert (health['heartbeat_s'], health['lease_s']) == (.2, .8)
        clock.now += .79
        assert executor.status()['lease_valid']
        clock.now += .02
        assert not executor.status()['lease_valid']
    finally:
        executor.close()


@pytest.mark.parametrize('interval,expiry', [('0', '3'), ('nan', '3'), ('1', 'inf'), ('3', '3')])
def test_invalid_commissioning_clocks_refused_before_workers_start(tmp_path, monkeypatch, interval, expiry):
    monkeypatch.setenv('ANODIZING_HEARTBEAT_S', interval)
    monkeypatch.setenv('ANODIZING_LEASE_S', expiry)
    with pytest.raises(ValueError):
        executor = Executor(tmp_path, FakeDevices())
        executor.close()


def test_both_controller_paths_follow_the_advertised_interval(tmp_path):
    executor = Executor(tmp_path / 'ipc', FakeDevices())
    pulses = []

    def request(operation, body=None):
        if operation == 'heartbeat':
            pulses.append(time.monotonic())
        if operation == 'health':
            return dict(executor.health(), heartbeat_s=.05, lease_s=.4)
        return executor.dispatch(operation, body)

    try:
        with operator(tmp_path / 'katana', request):
            wait_for(lambda: len(pulses) >= 2)
            assert pulses[1] - pulses[0] < .4
        programme = Programme(tmp_path / 'offline', request, [
            dict(id='s', role='seal', tank='tank', station=1, setpoint=20)])
        programme.session = executor.session
        programme.sequence = executor.last_sequence
        start = time.monotonic()
        programme.pulse()
        assert start < programme.next_pulse <= time.monotonic() + .05
    finally:
        executor.close()
