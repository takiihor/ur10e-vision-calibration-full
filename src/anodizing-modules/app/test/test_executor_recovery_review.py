"""Regressions for the executor Stop latch and recovery journal."""
import json
from threading import Event
from time import time

import pytest

from anodizing_web.executor import Executor, FakeDevices
from anodizing_web.executor_http import WiredDevices
from test_executor_phase1 import Clock, wait_for


def identity(executor, **kwargs):
    return dict(protocol=executor.health()['protocol'], boot=executor.boot, session='test-session', **kwargs)


def authorize(executor):
    request = identity(executor, stop_generation=executor.status()['stop_generation'])
    executor.heartbeat(dict(request, sequence=1))
    executor.control('connect', request)
    return request


@pytest.mark.parametrize('expiry', [False, True], ids=['stop', 'expired-lease'])
def test_delayed_reviewed_arm_cannot_clear_a_newer_fence(tmp_path, expiry):
    clock = Clock()
    with_executor = Executor(tmp_path, FakeDevices(), clock=clock)
    try:
        reviewed = authorize(with_executor)
        if expiry:
            clock.now += 3
            assert with_executor.heartbeat(dict(reviewed, sequence=2))['lease_valid']
        else:
            with_executor.stop()
        with pytest.raises(ValueError, match='generation|expired|cancelled'):
            with_executor.control('arm', reviewed)
        assert not with_executor.status()['armed']
    finally:
        with_executor.close()


def test_receipt_during_dip_cannot_confirm_halt_or_clear_uncertainty(tmp_path):
    entered, release = Event(), Event()
    devices = FakeDevices(setpoints={'tank-a': 30})
    devices.agv.station = 1
    perform = devices.perform

    def blocked(effect, value):
        if effect == 'dwell':
            entered.set()
            assert release.wait(8)
        return perform(effect, value)

    devices.perform = blocked
    executor = Executor(tmp_path, devices)
    try:
        request = authorize(executor)
        executor.control('arm', request)
        executor.submit(dict(request, job_id='active-dip',
                             type='dip', version=1, params=dict(
                                 run_id='open-basket', basket='basket-a', recipe_revision='rev-a',
                                 source='input', expected_basket='released', hold='output',
                                 steps=[dict(id='dip', tank='tank-a', station=1, role='ultrasonic', setpoint=30,
                                             limits=dict(min=None, overstay=None, air=None))])))
        assert entered.wait(3)
        assert devices.plc.equipment_on
        receipt = executor.stop()
        assert receipt['received'] and not receipt['confirmed']
        with pytest.raises(ValueError):
            executor.control('arm', dict(request, stop_generation=executor.status()['stop_generation']))
        release.set()
        result = wait_for(lambda: executor.status('active-dip'),
                          lambda state: state['job']['outcome'] is not None)
        assert result['job']['outcome'] == 'unknown'
        assert result['recovery_required']
    finally:
        release.set()
        executor.close()


def test_disconnected_wired_sample_cannot_confirm_stop(tmp_path):
    clock = Clock()
    devices = WiredDevices(tmp_path, None)
    executor = Executor(tmp_path, devices, clock=clock)
    try:
        devices.link.status = dict(actual_speed_m_h=0, stopped=True)
        from time import monotonic
        devices.link.status_at = monotonic()
        assert not devices.status()['connected']
        assert not executor.stop()['confirmed']
    finally:
        executor.close()


def test_losing_active_record_leaves_independent_recovery_fence(tmp_path):
    entered, release = Event(), Event()
    devices = FakeDevices()
    devices.agv.station = 1
    perform = devices.perform

    def blocked(effect, value):
        result = perform(effect, value)
        entered.set()
        assert release.wait(8)
        return result

    devices.perform = blocked
    executor = Executor(tmp_path, devices)
    try:
        request = authorize(executor)
        executor.control('arm', request)
        executor.submit(dict(request, job_id='lost-record', type='station_visit', version=1, params={'station': 2}))
        assert entered.wait(3)
        (tmp_path / '.jobs' / 'lost-record.json').unlink()
        restarted = Executor(tmp_path, devices)
        try:
            assert restarted.status()['recovery_required']
            assert 'lost-record' in restarted.status()['recovery_ids']
        finally:
            release.set()
            restarted.close()
    finally:
        release.set()
        executor.close()


def test_reconcile_keeps_other_unreadable_records_fenced(tmp_path):
    jobs = tmp_path / '.jobs'
    jobs.mkdir()
    (jobs / 'unknown-visit.json').write_text(json.dumps(dict(
        id='unknown-visit', module='executor', state='interrupted', execution_uncertain=True)))
    (jobs / 'unreadable-visit.json').write_text('{')
    devices = FakeDevices()
    devices.restore(has_history=False)
    executor = Executor(tmp_path, devices)
    try:
        request = authorize(executor)
        executor.reconcile(dict(request, job_id='unknown-visit', disposition='Verified at input.'))
        assert executor.status()['recovery_required']
    finally:
        executor.close()


def test_missing_disposition_uses_fresh_retained_evidence_and_fences_replay(tmp_path):
    devices = FakeDevices()
    devices.agv.station = 7
    devices.recovery_input = dict(observed_utc=time(), source='fake fixture', disposition='Checked station 7.')
    executor = Executor(tmp_path, devices)
    try:
        request = dict(authorize(executor), job_id='missing-reply', missing=True,
                       disposition='Verified at station 7 and quarantined the basket.')
        devices.fresh = False
        with pytest.raises(ValueError):
            executor.reconcile(request)
        devices.fresh = True
        executor.reconcile(request)
        result = executor.status('missing-reply')['job']
        assert result['outcome'] == 'unknown'
        assert result['result']['reconciliation']['hold']['station'] == 7
        with pytest.raises(ValueError, match='different request|reconciled'):
            executor.submit(dict(request, type='station_visit', version=1, params={'station': 2}))
        assert devices.effects == []
    finally:
        executor.close()


def test_hold_at_lease_deadline_is_disarmed_before_watchdog_poll(tmp_path):
    clock = Clock()
    devices = FakeDevices()
    hold = devices.hold

    def hold_at_deadline():
        if devices.agv.station == 2:
            clock.now += 3
        return hold()

    devices.hold = hold_at_deadline
    executor = Executor(tmp_path, devices, clock=clock)
    try:
        request = authorize(executor)
        executor.control('arm', request)
        executor.submit(dict(request, job_id='lease-edge-job', type='station_visit', version=1,
                             params={'station': 2}))
        status = wait_for(lambda: executor.status('lease-edge-job'),
                          lambda state: state['job']['outcome'] is not None)
        assert status['job']['outcome'] == 'held'
        assert not status['armed']
    finally:
        executor.close()
