"""Offline collision selection against deterministic fake robot and scene."""
import numpy as np
import pytest

from ur_handeye.collect import CollectError, Collector
from ur_handeye.poses import View
from ur_handeye.robot import MotionLimits


class Scene:
    data = {'confirmed': True, 'environment_complete': True, 'obstacles': ['fixture']}
    model_hash = 'fake-model'

    def __init__(self, blocked):
        self.blocked = blocked
        self.checked = []

    def check_segment(self, start, target, **kwargs):
        a, b = round(start[0], 3), round(target[0], 3)
        self.checked.append((a, b))
        blocked = self.blocked(a, b)
        return {'status': blocked if isinstance(blocked, str) else ('collision' if blocked else 'clear_in_model'),
                'pair': ['fixture', 'tool'] if blocked else [],
                'start_joints': list(start), 'target_joints': list(target)}


class Robot:
    limits = MotionLimits()

    def __init__(self, scene):
        self.collision = scene
        self.moves = []

    def plan(self, flange, q_start):
        return [float(flange[0, 3]), 0, 0, 0, 0, 0]

    def move_joints(self, joints):
        self.moves.append(joints)


class Session:
    def update_metadata(self, **kwargs):
        pass


def view(label, x):
    flange = np.eye(4)
    flange[0, 3] = x
    return View(label, flange)


def collector(blocked):
    scene = Scene(blocked)
    robot = Robot(scene)
    logs = []
    return Collector(robot, None, Session(), log=logs.append), scene, robot, logs


def test_blocked_endpoint_is_skipped_with_reason_and_feasible_view_remains():
    subject, scene, robot, logs = collector(lambda a, b: b == .1)
    planned, skipped = subject.plan([view('blocked', .1), view('clear', .2)], np.eye(4), [0.] * 6)
    assert [v.label for v in planned] == ['clear']
    assert any(label == 'blocked' and ('collision' in reason or 'fixture' in reason)
               for label, reason in skipped)
    assert robot.moves == []


def test_blocked_direct_transition_routes_both_checked_legs_through_start():
    subject, scene, robot, logs = collector(lambda a, b: (a, b) == (.1, .2))
    planned, skipped = subject.plan([view('first', .1), view('second', .2)], np.eye(4), [0.] * 6)
    assert [v.label for v in planned] == ['first', 'second']
    assert not skipped
    assert planned[1].via_start
    assert (.1, 0.) in scene.checked and (0., .2) in scene.checked
    assert robot.moves == []


def test_no_safe_return_is_refused_before_motion():
    subject, scene, robot, logs = collector(lambda a, b: (a, b) == (.1, 0.))
    try:
        planned, skipped = subject.plan([view('unsafe-return', .1)], np.eye(4), [0.] * 6)
    except CollectError as exc:
        assert 'collision' in str(exc) or 'fixture' in str(exc)
    else:
        assert not planned
        assert any(label == 'unsafe-return' and ('collision' in reason or 'fixture' in reason)
                   for label, reason in skipped)
    assert robot.moves == []


def auto_subject(monkeypatch, blocked=lambda a, b: False):
    from types import SimpleNamespace
    from ur_handeye.collect import CollectOptions
    from ur_handeye.transforms import matrix_to_pose

    subject, scene, robot, logs = collector(blocked)
    robot.current = [0.] * 6
    robot.check_ready = lambda: None
    robot.joints = lambda: list(robot.current)
    robot.flange_pose = lambda: np.eye(4)
    robot.plan = lambda flange, start: [*flange[:3, 3], 0., 0., 0.]
    def move(joints):
        robot.moves.append(list(joints))
        robot.current = list(joints)
    robot.move_joints = move
    subject.options = CollectOptions(bootstrap_after=0)
    subject.observer = SimpleNamespace(target=SimpleNamespace(corners=[], centre=np.zeros(3)))
    observation = SimpleNamespace(board_in_camera=np.eye(4))
    def capture(label):
        subject.usable += 1
        return {'status': 'ok', 'reasons': [], 'flange_pose': matrix_to_pose(robot.flange_pose())}, observation
    subject.capture = capture
    approvals = []
    subject.confirm = lambda summary: approvals.append(summary) or True
    return subject, robot, approvals


def test_blocked_mandatory_probe_refused_before_approval_or_motion(monkeypatch):
    subject, robot, approvals = auto_subject(monkeypatch, lambda a, b: b == .03)
    with pytest.raises(CollectError, match='[Pp]robe'):
        subject.run_auto()
    assert approvals == []
    assert robot.moves == []


@pytest.mark.parametrize('count', [2, 8])
def test_dry_run_refuses_insufficient_count_or_rotation_diversity(monkeypatch, count):
    import ur_handeye.collect as collect
    subject, robot, approvals = auto_subject(monkeypatch)
    monkeypatch.setattr(collect, 'plan_views', lambda *args: [view(f'view{i}', .01 * (i + 1))
                                                              for i in range(count)])
    with pytest.raises(CollectError, match='(?i)(few|insufficient|rotation|diversity|coverage)'):
        subject.run_auto(dry_run=True)
    assert approvals == []
    assert robot.moves == []


def test_bootstrap_refuses_remaining_views_that_lose_rotation_coverage(monkeypatch):
    import ur_handeye.collect as collect
    from ur_handeye.collect import CollectOptions
    from ur_handeye.transforms import axis_angle_rotation

    subject, robot, approvals = auto_subject(monkeypatch)
    subject.options = CollectOptions(bootstrap_after=5)
    initial = [view(f'view{i}', .01 * (i + 1)) for i in range(8)]
    for i, item in enumerate(initial[1:], 1):
        item.flange[:3, :3] = axis_angle_rotation([1, 0, 0] if i % 2 else [0, 1, 0],
                                                  .7 if i % 3 else -.7)
    updated = [view(f'view{i}', .01 * (i + 1)) for i in range(8)]
    calls = iter([initial, updated])
    monkeypatch.setattr(collect, 'plan_views', lambda *args: next(calls))
    monkeypatch.setattr(collect, 'estimate_camera_rotation', lambda *args: (np.eye(3), 0.))
    subject._interim = lambda *args: np.eye(4)
    def flange_pose():
        for item in initial:
            if abs(robot.current[0] - item.flange[0, 3]) < 1e-8 and robot.current[1:3] == [0., 0.]:
                return item.flange.copy()
        return np.eye(4)
    robot.flange_pose = flange_pose
    with pytest.raises(CollectError, match='(?i)(few|insufficient|rotation|diversity|coverage)'):
        subject.run_auto()
    assert len(approvals) == 2  # probe and initial views, no approval of the unsafe replan
    assert len(robot.moves) == 7  # three probe round trips and the first view only


@pytest.mark.parametrize('status', ['unresolved', 'node_budget_exceeded'])
def test_unresolved_endpoint_is_refused_and_recorded(status):
    subject, scene, robot, logs = collector(lambda a, b: status if a == b == .1 else False)
    planned, skipped = subject.plan([view('uncertain', .1), view('clear', .2)], np.eye(4), [0.] * 6)
    assert [v.label for v in planned] == ['clear']
    assert any(label == 'uncertain' and status in reason for label, reason in skipped)
    assert any(segment['status'] == status for evidence in subject.collision_candidates
               for segment in evidence['segments'])
    assert robot.moves == []


def test_certificates_are_directed_and_cached_only_within_one_plan():
    subject, scene, robot, logs = collector(lambda a, b: False)
    targets = [view('first', .1), view('second', .2)]
    subject.plan(targets, np.eye(4), [0.] * 6)
    first = list(scene.checked)
    assert len(first) == len(set(first))
    assert (0., .1) in first and (.1, 0.) in first
    assert any(segment['label'] == 'return_start' for segment in subject.collision_checks[-1]['segments'])
    subject.plan(targets, np.eye(4), [0.] * 6)
    assert scene.checked[len(first):] == first


def test_replay_preserves_recorded_targets_and_routes_blocked_direct_path_via_start(monkeypatch):
    subject, robot, approvals = auto_subject(monkeypatch, lambda a, b: (a, b) == (.1, .2))
    robot.validate_joints = lambda q: view('target', q[0]).flange
    targets = [[.1, 0, 0, 0, 0, 0], [.2, 0, 0, 0, 0, 0]]
    assert subject.run_replay(targets) == 2
    assert robot.moves == [targets[0], [0.] * 6, targets[1], [0.] * 6]
    assert len(approvals) == 1


def test_replay_refuses_blocked_hub_before_approval_or_motion(monkeypatch):
    subject, robot, approvals = auto_subject(monkeypatch, lambda a, b: (a, b) in {(.1, .2), (.1, 0.)})
    robot.validate_joints = lambda q: view('target', q[0]).flange
    targets = [[.1, 0, 0, 0, 0, 0], [.2, 0, 0, 0, 0, 0]]
    with pytest.raises(CollectError, match='(?i)(collision|path|fixture)'):
        subject.run_replay(targets)
    assert approvals == []
    assert robot.moves == []


def test_bootstrap_accepts_diverse_captured_poses_with_remaining_views(monkeypatch):
    import ur_handeye.collect as collect
    from ur_handeye.collect import CollectOptions
    from ur_handeye.transforms import axis_angle_rotation

    subject, robot, approvals = auto_subject(monkeypatch)
    subject.options = CollectOptions(bootstrap_after=6)
    initial = [view(f'view{i}', .1 + .01 * i) for i in range(8)]
    initial[0].flange[:3, :3] = axis_angle_rotation([1, 0, 0], .7)
    initial[1].flange[:3, :3] = axis_angle_rotation([0, 1, 0], .7)
    updated = [view(f'view{i}', .1 + .01 * i) for i in range(8)]
    calls = iter([initial, updated])
    monkeypatch.setattr(collect, 'plan_views', lambda *args: next(calls))
    monkeypatch.setattr(collect, 'estimate_camera_rotation', lambda *args: (np.eye(3), 0.))
    subject._interim = lambda *args: np.eye(4)
    def flange_pose():
        for item in initial:
            if abs(robot.current[0] - item.flange[0, 3]) < 1e-8 and robot.current[1:3] == [0., 0.]:
                return item.flange.copy()
        return np.eye(4)
    robot.flange_pose = flange_pose
    assert subject.run_auto() == 12
    assert len(approvals) == 3
    assert len(robot.moves) == 15
