import copy
import json
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from ur_handeye.collision import CollisionError, Scene, check_plan, plan_segments, separation
from ur_handeye.collision_backend import hull_gap
from ur_handeye.robot import MotionLimits, RobotError, SimulatedRobot, URRobot, ur10e_forward

EXAMPLE = Path(__file__).parents[1] / 'src/ur_handeye/web/collision-scene.example.json'


def example():
    return json.loads(EXAMPLE.read_text())


def obstacle(name, centre, dimensions=(.02, .02, .02)):
    return {'name': name, 'dimensions_m': list(dimensions),
            'pose': {'xyz_m': list(centre), 'rpy_rad': [0, 0, 0]}}


def test_nominal_model_matches_independent_dh_forward_kinematics():
    scene = Scene(example())
    rng = np.random.default_rng(108)
    for q in rng.uniform(-3, 3, (30, 6)):
        np.testing.assert_allclose(scene.flange(q.tolist()), ur10e_forward(q), atol=2e-8)


def test_support_gap_is_conservative_and_handles_containment():
    points = np.array([[x, y, z] for x in [-.5, .5] for y in [-.5, .5] for z in [-.5, .5]])
    assert hull_gap(points, points + [2, 0, 0]) == pytest.approx(1 - 2e-6)
    assert hull_gap(points, points * .3) <= 0
    assert hull_gap(points, points + [.3, .3, .3]) <= 0


def test_clear_comparison_path_and_midpath_collision_with_clear_endpoints():
    scene = Scene(example())
    centre = np.array(SimulatedRobot.HOME)
    a, b = centre.copy(), centre.copy()
    a[0] -= .4
    b[0] += .4
    assert scene.check_segment(a.tolist(), b.tolist())['status'] == 'clear_in_model'
    body = next(x for x in scene.bodies if x['name'] == 'tool')
    point = (scene.frames(centre.tolist())[-1] @ body['pose'])[:3, 3]
    data = example()
    data['obstacles'] = [obstacle('block', point)]
    blocked = Scene(data)
    assert blocked.check_segment(a.tolist(), a.tolist())['status'] == 'clear_in_model'
    assert blocked.check_segment(b.tolist(), b.tolist())['status'] == 'clear_in_model'
    result = blocked.check_segment(a.tolist(), b.tolist())
    assert result['status'] == 'collision'
    assert 'block' in result['pair']


def test_supervised_self_requires_attached_confirmation_and_keeps_all_self_pairs():
    data = example()
    reference = Scene(data)
    data['collision_scope'] = 'supervised_self'
    scene = Scene(data)
    assert scene.pairs == reference.pairs
    with pytest.raises(CollisionError, match='Confirm the physical'):
        scene.require_motion_ready()
    data['confirmed'] = True
    scene = Scene(data)
    scene.require_motion_ready()
    q = list(SimulatedRobot.HOME)
    result = scene.check_segment(q, q)
    assert result['status'] == 'clear_in_model' and result['collision_scope'] == 'supervised_self'
    scene.hull_gap = None
    with pytest.raises(CollisionError, match='dependencies'):
        scene.require_motion_ready()


@pytest.mark.parametrize('update', [
    {'collision_scope': 'unknown'}, {'collision_scope': True},
    {'collision_scope': 'supervised_self', 'environment_complete': True},
    {'collision_scope': 'supervised_self', 'obstacles': [obstacle('block', [1, 0, 0])]},
])
def test_collision_scope_cannot_claim_unchecked_cell_coverage(update):
    data = example()
    data.update(update)
    with pytest.raises(CollisionError):
        Scene(data)


def test_supervised_self_still_refuses_tool_arm_collision():
    data = example()
    data.update(collision_scope='supervised_self', confirmed=True)
    q = list(SimulatedRobot.HOME)
    scene = Scene(data)
    arm = scene.bodies[3]
    point = (scene.frames(q)[3] @ arm['pose'])[:3, 3]
    tool = next(x for x in data['attached'] if x['name'] == 'tool')
    tool['pose']['xyz_m'] = (np.linalg.inv(scene.flange(q)) @ np.append(point, 1))[:3].tolist()
    result = Scene(data).check_segment(q, q)
    assert result['status'] == 'collision'
    assert 'tool' in result['pair']


def test_camera_and_mount_are_checked_against_other_arm_links():
    q = list(SimulatedRobot.HOME)
    for name in ('camera', 'mount', 'tool'):
        data = example()
        scene = Scene(data)
        link = scene.frames(q)[3]
        arm = scene.bodies[3]
        point = (link @ arm['pose'])[:3, 3]
        body = next(x for x in data['attached'] if x['name'] == name)
        body['pose']['xyz_m'] = (np.linalg.inv(scene.flange(q)) @ np.append(point, 1))[:3].tolist()
        result = Scene(data).check_segment(q, q)
        assert result['status'] == 'collision' and name in result['pair']


def test_limits_missing_solver_and_uncertain_intervals_refuse_paths(monkeypatch):
    scene = Scene(example())
    q = list(SimulatedRobot.HOME)
    scene.hull_gap = None
    assert scene.check_segment(q, q)['status'] == 'unavailable'
    scene = Scene(example())
    end = q.copy()
    end[0] += .8
    assert scene.check_segment(q, end, max_depth=0)['status'] == 'unresolved'
    scene.hull_gap = lambda *args: None
    assert scene.check_segment(q, q)['status'] == 'unresolved'


@pytest.mark.parametrize('change', [
    lambda d: d.update(confirmed='yes'), lambda d: d.update(version=True),
    lambda d: d.update(clearance_m=float('nan')), lambda d: d.update(model_sha256='wrong'),
    lambda d: d['attached'][0].update(dimensions_m=[0, .1, .1]),
    lambda d: d['attached'][1].update(name='tool'), lambda d: d.update(extra='ignored'),
])
def test_scene_validation_rejects_invalid_or_incompatible_input(change):
    data = example()
    change(data)
    with pytest.raises(CollisionError):
        Scene(data)


def test_draft_and_incomplete_cell_refuse_automatic_motion():
    data = example()
    with pytest.raises(CollisionError, match='Confirm the physical'):
        Scene(data).require_motion_ready()
    data['confirmed'] = True
    data['environment_complete'] = True
    with pytest.raises(CollisionError, match='measured cell'):
        Scene(data).require_motion_ready()


def test_scene_edits_do_not_mutate_an_existing_checker():
    data = example()
    scene = Scene(data)
    before = scene.scene_hash
    data['attached'][0]['dimensions_m'][0] = 2
    assert scene.data['attached'][0]['dimensions_m'][0] == .39
    assert Scene(data).scene_hash != before


def test_return_and_via_start_are_part_of_saved_plan():
    start, first, second = [0.] * 6, [.1] * 6, [.2] * 6
    segments = plan_segments({'start_joints': start, 'plan': {'targets': [
        {'label': 'first', 'joints': first}, {'label': 'second', 'joints': second, 'via_start': True}]}})
    assert segments == [('first', start, first), ('via_start', first, start),
                        ('second', start, second), ('return_start', second, start)]


def test_return_refusal_is_recorded_before_any_collector_motion(tmp_path):
    from ur_handeye.collect import Collector, CollectError
    from ur_handeye.poses import View
    records = []
    robot = SimulatedRobot()
    q, flange = robot.joints(), robot.flange_pose()
    target = list(q)
    target[0] += .1
    target_flange = robot.validate_joints(target)
    class Checker:
        data, model_hash = {'confirmed': False, 'environment_complete': False, 'obstacles': []}, 'test'
        def check_segment(self, a, b):
            blocked = np.allclose(a, target) and np.allclose(b, q)
            return {'status': 'collision' if blocked else 'clear_in_model', 'pair': ['tool', 'board']}
    robot.collision = Checker()
    collector = Collector(robot, None, SimpleNamespace(directory=tmp_path, update_metadata=lambda **v: records.append(v)))
    planned, skipped = collector.plan([View('view', target_flange)], flange, q)
    assert not planned and any(label == 'view' and 'collision' in reason for label, reason in skipped)
    assert robot.moves == 0 and records
    evidence = json.loads((tmp_path / 'collision-candidates-001.json').read_text())
    assert any(segment['status'] == 'collision' for segment in evidence['segments'])


def test_cli_draft_refusal_precedes_hardware_or_camera_access(monkeypatch):
    from ur_handeye import cli
    def forbidden(*args, **kwargs):
        pytest.fail('Hardware access occurred before draft refusal')
    monkeypatch.setattr(cli, '_camera', forbidden)
    monkeypatch.setattr('ur_handeye.robot.URRobot', forbidden)
    with pytest.raises(CollisionError, match='Confirm the physical'):
        cli.cmd_collect(SimpleNamespace(mode='auto', dry_run=False, collision_scene=str(EXAMPLE)))
    args = cli.parser().parse_args(['collect', '--robot-ip', '192.0.2.1'])
    assert args.tilt_deg == 25 and args.roll_deg == 30


def test_runtime_refusal_sends_no_move_command():
    robot = URRobot.__new__(URRobot)
    robot.limits = MotionLimits()
    robot.collision = Scene(example())
    robot.control = SimpleNamespace(isSteady=lambda: True)
    robot._require_control = lambda: robot.control
    robot.check_ready = lambda: None
    robot.validate_joints = lambda q: None
    robot.joints = lambda: list(SimulatedRobot.HOME)
    with pytest.raises(RobotError, match='Confirm the physical'):
        robot.move_joints(list(SimulatedRobot.HOME))


def test_motion_bound_encloses_points_between_joint_waypoints():
    scene = Scene(example())
    rng = np.random.default_rng(74)
    for _ in range(20):
        centre = np.array(SimulatedRobot.HOME) + rng.uniform(-.2, .2, 6)
        delta = rng.uniform(-.15, .15, 6)
        mid_frames, end_frames = scene.frames(centre.tolist()), scene.frames((centre+delta).tolist())
        for common in (0, 2, 4):
            mid, end = np.linalg.inv(mid_frames[common]), np.linalg.inv(end_frames[common])
            for body in scene.bodies:
                if body['depth'] < common:
                    continue
                # Test the farthest local box corners independently of the separating-plane solver.
                corners = np.array([[x,y,z,1] for x in (-body['half'][0],body['half'][0])
                                     for y in (-body['half'][1],body['half'][1])
                                     for z in (-body['half'][2],body['half'][2])])
                a = corners @ (mid @ mid_frames[body['depth']] @ body['pose']).T
                b = corners @ (end @ end_frames[body['depth']] @ body['pose']).T
                observed = np.linalg.norm(a[:,:3]-b[:,:3],axis=1).max()
                bound = body['radii'][common:] @ np.abs(delta[common:])
                assert observed <= bound + 1e-10


def test_runtime_rejects_nonfinite_fk_before_move():
    robot = URRobot.__new__(URRobot)
    robot.limits = MotionLimits()
    robot.collision = SimpleNamespace(require_clear=lambda a, b: None, flange=lambda q: np.eye(4),
                                      data={'model_uncertainty_m': .003})
    robot.control = SimpleNamespace(isSteady=lambda: True, moveJ=lambda *args: pytest.fail('moveJ called'))
    robot._require_control = lambda: robot.control
    robot.check_ready = lambda: None
    robot.validate_joints = lambda q: None
    robot.joints = lambda: list(SimulatedRobot.HOME)
    robot._flange_at = lambda q: np.full((4, 4), np.nan)
    with pytest.raises(RobotError, match='invalid kinematics'):
        robot.move_joints(list(SimulatedRobot.HOME))


def test_missing_solver_refuses_before_hardware(monkeypatch):
    from ur_handeye import cli
    data = example()
    data.update(confirmed=True, environment_complete=True, obstacles=[obstacle('table', [0, 0, -1])])
    scene = Scene(data)
    scene.hull_gap = None
    monkeypatch.setattr('ur_handeye.collision.load_scene', lambda path: scene)
    monkeypatch.setattr('ur_handeye.robot.URRobot', lambda *a, **k: pytest.fail('hardware access'))
    monkeypatch.setattr(cli, '_camera', lambda *a, **k: pytest.fail('camera access'))
    with pytest.raises(CollisionError, match='dependencies'):
        cli.cmd_collect(SimpleNamespace(mode='auto', dry_run=False, collision_scene='unused'))


def test_runtime_rejects_model_mismatch_before_move():
    robot = URRobot.__new__(URRobot)
    robot.limits = MotionLimits()
    robot.collision = SimpleNamespace(require_clear=lambda a, b: None, flange=lambda q: np.eye(4),
                                      data={'model_uncertainty_m': .003})
    robot.control = SimpleNamespace(isSteady=lambda: True, moveJ=lambda *args: pytest.fail('moveJ called'))
    robot._require_control = lambda: robot.control
    robot.check_ready = lambda: None
    robot.validate_joints = lambda q: None
    robot.joints = lambda: list(SimulatedRobot.HOME)
    robot._flange_at = lambda q: np.array([[1.,0,0,.01],[0,1,0,0],[0,0,1,0],[0,0,0,1]])
    with pytest.raises(RobotError, match='kinematics differ'):
        robot.move_joints(list(SimulatedRobot.HOME))
