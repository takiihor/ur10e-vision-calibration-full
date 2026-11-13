"""Fake-only scheduling contracts: no real SDK or scheduler calls."""
import builtins
import os
from types import SimpleNamespace
import pytest
from ur_handeye.robot import RobotError, URRobot

@pytest.fixture
def sdk(monkeypatch):
    state = SimpleNamespace(policy=0, priority=0, starts=[], cleanup=[], fail=None,
                            restore_failure=False, enabled=True)
    def policy(tid):
        assert tid == 0
        return state.policy
    def param(tid):
        assert tid == 0
        return SimpleNamespace(sched_priority=state.priority)
    def restore(tid, policy, param):
        assert tid == 0
        if state.restore_failure:
            raise PermissionError('restore denied')
        state.policy, state.priority = policy, param.sched_priority
    for name, method in [('sched_getscheduler', policy), ('sched_getparam', param),
                         ('sched_setscheduler', restore)]:
        monkeypatch.setattr(os, name, method, raising=False)
    def construct(kind):
        def factory(host, frequency):
            assert (host, frequency) == ('fake-only.invalid', 125.0)
            state.starts.append((kind, state.policy, state.priority))
            if state.enabled:
                state.policy, state.priority = 1, 90
            if state.fail == kind:
                raise RuntimeError(kind + ' connection failed')
            return SimpleNamespace(
                disconnect=lambda: state.cleanup.append((kind, state.policy, state.priority)),
                stopScript=lambda: None)
        return factory
    fake_modules = {
        'rtde_receive': SimpleNamespace(RTDEReceiveInterface=construct('receive')),
        'rtde_control': SimpleNamespace(RTDEControlInterface=construct('control')),
    }
    original_import = builtins.__import__
    def import_fake_sdk(name, globals=None, locals=None, fromlist=(), level=0):
        if name in fake_modules:
            return fake_modules[name]
        return original_import(name, globals, locals, fromlist, level)
    monkeypatch.setattr(builtins, '__import__', import_fake_sdk)
    return state

@pytest.mark.parametrize('initial', [(0, 0), (1, 42)])
def test_each_constructor_restores_original_caller_scheduler(sdk, initial):
    sdk.policy, sdk.priority = initial
    robot = URRobot('fake-only.invalid')
    assert sdk.starts == [('receive', *initial), ('control', *initial)]
    assert (sdk.policy, sdk.priority) == initial
    robot.close()
    assert sdk.cleanup == [('control', *initial), ('receive', *initial)]

@pytest.mark.parametrize('failure', ['receive', 'control'])
def test_constructor_failure_restores_before_existing_cleanup(sdk, failure):
    sdk.fail = failure
    with pytest.raises(RobotError, match=failure + ' connection failed'):
        URRobot('fake-only.invalid')
    assert (sdk.policy, sdk.priority) == (0, 0)
    assert sdk.cleanup == ([('receive', 0, 0)] if failure == 'control' else [])

def test_receive_only_never_constructs_control(sdk):
    robot = URRobot('fake-only.invalid', control=False)
    assert sdk.starts == [('receive', 0, 0)]
    assert (sdk.policy, sdk.priority) == (0, 0)
    assert robot.control is None
    robot.close()

def test_restore_failure_fails_closed_and_cleans_acquired_interface(sdk):
    sdk.restore_failure = True
    with pytest.raises((RobotError, PermissionError), match='restore denied'):
        URRobot('fake-only.invalid')
    assert sdk.starts == [('receive', 0, 0)]
    assert len(sdk.cleanup) == 1
    assert sdk.cleanup[0][0] == 'receive'

def test_platform_without_scheduler_api_still_connects(sdk, monkeypatch):
    sdk.enabled = False
    for name in ('sched_getscheduler', 'sched_getparam', 'sched_setscheduler'):
        monkeypatch.delattr(os, name)
    robot = URRobot('fake-only.invalid')
    assert sdk.starts == [('receive', 0, 0), ('control', 0, 0)]
    robot.close()
