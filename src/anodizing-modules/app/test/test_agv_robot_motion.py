"""Exercise the shared admission at every app motion entry point using in-process fakes."""
import threading
import time
from unittest.mock import Mock

import pytest

from anodizing_web import agv
from anodizing_web.jobs import JobOwner
from anodizing_web.recipe_agv import StationVisit


MOTIONS = [
    ('calibration', 'collect'), ('calibration', 'replay'),
    ('teaching', 'validate_move'), ('teaching', 'return_observation'),
    ('teaching', 'vertical_descent'), ('manual-pick-place', 'PICK'),
    ('manual-pick-place', 'PLACE'), ('manual-pick-place', 'REPEAT'),
    ('robot-control', 'STEP'),
]


@pytest.mark.parametrize('module,action', MOTIONS)
@pytest.mark.parametrize('reporting,off,accepted', [
    (False, False, False), (True, False, True), (False, True, True), (True, True, False),
])
def test_each_motion_requires_parked_or_explicitly_off(tmp_path, monkeypatch, module, action,
                                                        reporting, off, accepted):
    owner = JobOwner(tmp_path, allow_auto_calibration=True, robot_model=object(),
                     allow_teacher_motion=True, allow_manual_pick_place=True)
    owner.agv = link = agv.Connection(owner)
    if reporting:
        connect_parked(link)
    monkeypatch.setattr('anodizing_web.auto_scene.prepare', lambda *args: {'snapshot': 'fixture'})
    finished = threading.Event()
    body = {'action': action, 'agv_off': off}
    try:
        if accepted:
            job = owner.submit('offline-motion', module, body, lambda _: finished.set())
            assert job['agv_motion'] == ('off' if off else 'parked')
            assert finished.wait(2)
        else:
            with pytest.raises(ValueError, match='AGV is reporting.*Use AGV parked' if off else 'AGV state unknown'):
                owner.submit('offline-motion', module, body, lambda _: finished.set())
            assert not finished.is_set()
    finally:
        owner.shutdown()
        link.close()


def connect_parked(link):
    connected = link.submit({'action': 'connect', 'mode': 'simulator'})
    link.transport.station = link.transport.target = 2
    link.settings['mode'] = 'serial'  # Fake serial reporting without opening a device.
    link.submit({'action': 'send', 'command': 'query_status', 'connection_id': connected['connection_id']})
    return link.transport


def test_hold_attempts_agv_stop_and_releases_waiter_when_robot_stop_raises():
    link = Mock()
    robot_stop = Mock(side_effect=RuntimeError('robot stop failed'))
    visit = StationVisit(link, robot_stop)
    visit._sample = Mock(side_effect=ValueError('lost hold'))
    visit._watch()
    assert visit.ready.is_set()
    robot_stop.assert_called_once()
    link.recipe_stop.assert_called_once()


def test_pick_place_station_hold_explains_standard_mode_refusal():
    status = dict(mode=0, fault_code=0, paused=False, stopped=True, actual_speed_m_h=0, current_station=1)
    with pytest.raises(ValueError, match='Pick/place needs station-call mode to verify and call learned stations.*XBOT app'):
        StationVisit(None, None)._check_hold(status, time.monotonic())
    assert StationVisit(None, None, strict_station=False)._check_hold(status, time.monotonic()) == 1


def test_capture_station_prefers_fresh_serial_report_and_preserves_provenance(tmp_path):
    from anodizing_web.teaching import agv_capture

    owner = JobOwner(tmp_path)
    link = agv.Connection(owner)
    try:
        connect_parked(link)
        assert agv_capture(link, 7) == dict(station=2, source='agv_status',
                                             status_time=link.status_iso, transport='serial')
        link.settings['mode'] = 'simulator'
        assert agv_capture(link, 7) == dict(station=7, source='operator',
                                             status_time=link.status_iso, transport='simulator')
        with pytest.raises(ValueError, match='1 to 255'):
            agv_capture(link, 256)
    finally:
        link.close()
    assert agv_capture(link, 7) == dict(station=7, source='operator', status_time=None, transport=None)


def interrupt_vehicle(vehicle, violation):
    if violation == 'moving':
        vehicle.stopped = False
    else:
        vehicle.silent = True


def fast_agv(monkeypatch):
    monkeypatch.setattr(agv, 'RECIPE_POLL_S', .04)
    monkeypatch.setattr(agv, 'RECIPE_STALE_S', .65)


def assert_stopped(job, vehicle, violation):
    assert job['state'] == 'failed', job
    assert ('moved during robot work' if violation == 'moving' else 'status is stale') in job['error']
    assert job['agv_interruption']['agv_stop_sent'] is True
    assert vehicle.command_stop


@pytest.mark.parametrize('violation', ['moving', 'stale'])
def test_calibration_hold_stops_runner_during_approved_phase(violation, monkeypatch):
    from test_auto_bridge import AutoBridgeTests, Runner, ui_jobs, collision

    harness = AutoBridgeTests()
    harness.setUp()
    owner = JobOwner(harness.root, allow_auto_calibration=True)
    owner.agv = link = agv.Connection(owner)
    try:
        vehicle = connect_parked(link)
        fast_agv(monkeypatch)
        monkeypatch.setattr(collision.Scene, 'require_motion_ready', lambda _: None)
        monkeypatch.setattr(ui_jobs, 'JobRunner', Runner)
        body = dict(harness.body, agv_off=False)
        harness.submit(owner, 'agv-calibration', body)
        harness.wait(owner, 'agv-calibration', 'waiting')
        owner.answer('agv-calibration', 1, True)
        harness.wait(owner, 'agv-calibration', 'waiting')
        interrupt_vehicle(vehicle, violation)
        deadline = time.monotonic()+3
        while owner.active and time.monotonic() < deadline:
            time.sleep(.02)
        assert not owner.active
        assert Runner.instances[-1].stopped
        assert_stopped(owner.get('agv-calibration'), vehicle, violation)
    finally:
        owner.shutdown()
        link.close()
        harness.doCleanups()


@pytest.mark.parametrize('violation', ['moving', 'stale'])
def test_teaching_hold_stops_native_joint_worker(violation, monkeypatch):
    from test_teacher_joint import JointTeacherIntegrationTests

    harness = JointTeacherIntegrationTests()
    harness.setUp()
    try:
        harness.prepare()
        harness.motion.close()
        harness.robot.connected = True
        harness.public_server()
        vehicle = connect_parked(harness.server.agv)
        fast_agv(monkeypatch)
        harness.robot._duration = 5
        move = harness.robot.moveJ

        def dispatch(*args):
            result = move(*args)
            interrupt_vehicle(vehicle, violation)
            return result

        monkeypatch.setattr(harness.robot, 'moveJ', dispatch)
        _, job = harness.run_public_move(agv_off=False)
        assert_stopped(job, vehicle, violation)
        assert harness.stop_calls
    finally:
        harness.doCleanups()


@pytest.mark.parametrize('violation', ['moving', 'stale'])
@pytest.mark.parametrize('operation', ['PICK', 'PLACE'])
def test_single_hold_stops_native_motion_and_invalidates_item(violation, operation, monkeypatch):
    import json
    from test_manual_execution import ManualWorkerTests

    harness = ManualWorkerTests()
    try:
        harness.public_server(operation=operation)
        vehicle = connect_parked(harness.server.agv)
        fast_agv(monkeypatch)
        harness.robot.duration = 5
        harness.robot.dispatch_hook = lambda *args: interrupt_vehicle(vehicle, violation)
        harness.public_body.update(action=operation, agv_off=False, initial_item_state=
                                   'operator_confirmed_released' if operation == 'PICK' else 'operator_confirmed_held')
        status, raw = harness.request('POST', '/api/manual-pick-place', harness.public_body)
        assert status == 202, raw
        job = harness.wait_public(lambda job: job['state'] in ('waiting', 'failed'))
        assert job['state'] == 'waiting', job['error']
        reply = {'job_id': job['id'], 'action': 'confirm', 'prompt': job['manual']['prompt'], 'value': True}
        status, raw = harness.request('POST', '/api/manual-control', reply)
        assert status == 200, raw
        job = harness.wait_public(lambda job: job['state'] == 'failed')
        assert_stopped(job, vehicle, violation)
        assert job['manual']['item_state'] == 'unknown'
        assert harness.robot.stops == ['stopJ']
        assert json.loads((harness.server.owner.directory / (job['id']+'.json')).read_text())['agv_motion'] == 'parked'
    finally:
        harness.doCleanups()



def test_agv_page_blocks_motion_during_a_switched_off_job(tmp_path, monkeypatch):
    owner = JobOwner(tmp_path, allow_auto_calibration=True)
    owner.agv = link = agv.Connection(owner)
    entered, release = threading.Event(), threading.Event()
    monkeypatch.setattr('anodizing_web.auto_scene.prepare', lambda *args: {'snapshot': 'fixture'})

    def job(_):
        entered.set()
        release.wait(3)

    try:
        owner.submit('agv-off-job', 'calibration', {'action':'collect', 'agv_off':True}, job)
        assert entered.wait(2)
        connection_id = link.submit({'action':'connect', 'mode':'simulator'})['connection_id']
        link.submit({'action':'arm', 'armed':True, 'connection_id':connection_id})
        with pytest.raises(ValueError, match='A robot job is running'):
            link.submit({'action':'send', 'command':'call_station', 'value':2, 'connection_id':connection_id})
        assert link.submit({'action':'send', 'command':'stop'})['results']['stop']['state'] == agv.SENT
    finally:
        release.set()
        owner.shutdown()
        link.close()
