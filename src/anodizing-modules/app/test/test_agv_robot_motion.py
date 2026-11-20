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
    if reporting:
        body.update(connection_id=link.id, stop_generation=link.stop_generation)
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
    link.transport.arrival = True
    link.settings['mode'] = 'serial'  # Fake serial reporting without opening a device.
    link.submit({'action': 'send', 'command': 'query_status', 'connection_id': connected['connection_id']})
    return link.transport


@pytest.mark.parametrize('count,station,allowed', [
    (0, 1, False), (1, 0, False), (1, 1, True), (1, 2, True), (1, 3, False),
    (255, 255, True), (255, 256, False),
])
def test_recipe_checks_the_learned_station_range(count, station, allowed):
    visit = StationVisit(None, None)
    visit.status = dict(mode=2, max_stations=count, set_speed_m_h=500)
    if allowed:
        visit.check_route([station], 500)
    else:
        with pytest.raises(ValueError, match=f'station {station} is not learned'):
            visit.check_route([station], 500)


def test_hold_attempts_agv_stop_and_releases_waiter_when_robot_stop_raises():
    link = Mock()
    robot_stop = Mock(side_effect=RuntimeError('robot stop failed'))
    visit = StationVisit(link, robot_stop)
    visit._sample = Mock(side_effect=ValueError('lost hold'))
    visit._watch()
    assert visit.ready.is_set()
    robot_stop.assert_called_once()
    link.recipe_stop.assert_called_once()


def test_watchdog_latches_cancel_and_halts_agv_while_robot_stop_blocks():
    robot_entered, release_robot, agv_halted = (threading.Event() for _ in range(3))
    link = Mock()
    link.recipe_stop.side_effect = lambda: agv_halted.set() or True

    def blocked_robot_stop():
        robot_entered.set()
        release_robot.wait(2)

    visit = StationVisit(link, blocked_robot_stop)
    visit._sample = Mock(side_effect=ValueError('lost hold'))
    thread = threading.Thread(target=visit._watch)
    thread.start()
    try:
        assert robot_entered.wait(1)
        assert agv_halted.wait(.5)
        assert visit.cancelled.is_set()
    finally:
        release_robot.set()
        thread.join(2)


def test_off_motion_is_interrupted_by_a_serial_report(tmp_path, monkeypatch):
    owner = JobOwner(tmp_path, allow_auto_calibration=True)
    owner.agv = link = agv.Connection(owner)
    running, stopped = threading.Event(), threading.Event()
    monkeypatch.setattr('anodizing_web.auto_scene.prepare', lambda *args: {'snapshot': 'fixture'})
    monkeypatch.setattr(owner, '_stop_for_agv', lambda _: stopped.set())

    def job(_):
        running.set()
        stopped.wait(2)

    try:
        owner.submit('late-serial-report', 'calibration', {'action':'collect','agv_off':True}, job)
        assert running.wait(1)
        connect_parked(link)
        assert stopped.wait(1)
        owner.shutdown()
        assert owner.get('late-serial-report')['state'] == 'failed'
        assert 'AGV is reporting' in owner.get('late-serial-report')['error']
    finally:
        stopped.set()
        owner.shutdown()
        link.close()


def test_stop_between_job_admission_and_recipe_handoff_prevents_work(tmp_path, monkeypatch):
    owner = JobOwner(tmp_path, allow_manual_pick_place=True, robot_model=object())
    owner.agv = link = agv.Connection(owner)
    admitted, handoff, worked = (threading.Event() for _ in range(3))
    run = owner._run

    def defer_handoff(*args):
        admitted.set()
        if handoff.wait(2):
            run(*args)

    monkeypatch.setattr('anodizing_web.auto_scene.prepare', lambda *args: {'snapshot': 'fixture'})
    monkeypatch.setattr(owner, '_run', defer_handoff)
    try:
        connect_parked(link)
        state = link.link_state()
        owner.submit('agv-stale-handoff', 'manual-pick-place',
                     dict(action='REPEAT', connection_id=link.id, stop_generation=state.get('stop_generation', 0)),
                     lambda _: worked.set())
        assert admitted.wait(1)
        link.submit({'action':'send', 'command':'stop'})
        handoff.set()
        owner.shutdown()
        assert not worked.is_set()
        assert 'cancelled' in owner.get('agv-stale-handoff')['error']
    finally:
        handoff.set()
        owner.shutdown()
        link.close()


def test_parked_recipe_approval_expires_after_stop():
    from anodizing_web.recipe_agv import admit

    link = Mock()
    link.motion_feedback.return_value = dict(mode='serial', status_age_s=.1, movement_pending=False,
        connection_id=10, stop_generation=2, status=dict(stopped=True, actual_speed_m_h=0, fault_code=0,
                                                        obstacle_stop=False))
    with pytest.raises(ValueError, match='approval expired'):
        admit(link, dict(connection_id=10, stop_generation=1), require_approval=True)
    assert admit(link, dict(connection_id=10, stop_generation=2), require_approval=True) == ('parked', 10, 2)


def test_obstacle_stopped_status_is_not_a_robot_hold(tmp_path):
    from anodizing_web.recipe_agv import motion_state

    owner = JobOwner(tmp_path)
    link = agv.Connection(owner)
    try:
        connect_parked(link)
        with link.lock:
            link.status = dict(link.status, obstacle_stop=True)
        assert not motion_state(link)['parked']
        with pytest.raises(ValueError, match='obstacle'):
            StationVisit(link, None)._check_hold(link.status, time.monotonic())
    finally:
        link.close()


def test_pick_place_station_hold_explains_standard_mode_refusal():
    status = dict(mode=0, fault_code=0, paused=False, stopped=True, actual_speed_m_h=0, current_station=1,
                  obstacle_stop=False)
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
    monkeypatch.setattr(agv, 'ACK_WINDOW', .10)


def assert_stopped(job, vehicle, violation):
    assert job['state'] == 'failed', job
    assert ('moved during robot work' if violation == 'moving' else 'status is stale') in job['error']
    assert job['agv_interruption']['agv_stop_sent'] is True
    assert vehicle.paused


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
            link.submit({'action':'send', 'command':'call_station', 'value':2, 'connection_id':connection_id,
                         'stop_generation':link.stop_generation})
        halt = link.submit({'action':'send', 'command':'stop'})
        assert halt['results'][halt['urgent_command']]['state'] == agv.SENT
    finally:
        release.set()
        owner.shutdown()
        link.close()


@pytest.mark.parametrize(('field', 'value'), [
    ('station_arrival', False), ('actual_speed_m_h', 100),
    ('obstacle_stop', True), ('fault_code', 1),
])
def test_capture_station_requires_safe_arrival_evidence(tmp_path, field, value):
    from anodizing_web.teaching import agv_capture

    owner = JobOwner(tmp_path)
    link = agv.Connection(owner)
    try:
        connect_parked(link)
        with link.lock:
            link.status = dict(link.status, **{field: value})
        result = agv_capture(link, 7)
        assert result['station'] == 7
        assert result['source'] == 'operator'
    finally:
        link.close()
