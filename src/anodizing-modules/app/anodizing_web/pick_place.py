"""Compute synthetic PICK and PLACE targets from saved calibration and teaching."""
import json
import math
import time
from pathlib import Path

from .jobs import atomic_json
from .paths import check_tree, inside


def run(root, body, job_id):
    if body.get('mode', 'simulation') != 'simulation':
        raise ValueError('This endpoint supports simulation only; use the separate manual module.')
    import numpy as np
    from ur_pick_place.vision.config import Role
    from ur_pick_place.vision.grasp import load_taught, target_pose
    from ur_pick_place.vision.handeye import load_handeye
    from ur_pick_place.vision.observe import Observation
    from ur_pick_place.vision.robot_state import same_tcp
    from ur_pick_place.vision.targets import target_from_config
    from ur_pick_place.vision.teaching import digest
    from ur_pick_place.vision.transforms import invert, matrix_to_pose, pose_to_matrix

    operation = body.get('operation')
    if operation not in ('PICK', 'PLACE'):
        raise ValueError('Choose PICK or PLACE.')
    project = check_tree(root, inside(root, body.get('project')))
    calibration_path = inside(root, body.get('calibration'))
    saved = json.loads((project / 'project.json').read_text())
    if saved.get('type') != 'marker_pick_project' or saved.get('schema_version') != 1 or not saved['flow']['taught']:
        raise ValueError('Choose a completed teaching project.')
    settings = saved['settings']
    calibration = load_handeye(calibration_path)
    if saved['inputs']['calibration'] != calibration.sha256:
        raise ValueError('Calibration does not match this teaching project.')
    target_spec = dict(settings['target'])
    for key in ('board_manifest', 'aruco_manifest'):
        if target_spec.get(key):
            target_spec[key] = str(inside(root, body.get('manifest') or target_spec[key]))
            if saved['inputs'].get('manifest') != digest(target_spec[key]):
                raise ValueError('Marker manifest changed since teaching.')
    target = target_from_config(target_spec)
    model = load_taught(project / 'grasp_pick.json')
    old = Observation.from_json(json.loads((project / 'observation.json').read_text()))
    if (old.calibration_sha256 != calibration.sha256 or
            not same_tcp(settings['tcp'], calibration.tcp_offset) or
            not same_tcp(old.robot['tcp_offset'], calibration.tcp_offset)):
        raise ValueError('Teaching observation or TCP does not match calibration.')
    shift = body.get('shift_mm', [0, 0])
    if not isinstance(shift, list) or len(shift) != 2 or any(type(v) not in (float, int) or not math.isfinite(v) or abs(v) > 100 for v in shift):
        raise ValueError('Enter two finite simulated XY shifts within 100 mm.')
    state_path = project / 'simulated_state.json'
    state = json.loads(state_path.read_text()) if state_path.exists() else {'held': False}
    if operation == 'PICK' and state.get('held') is not False:
        raise ValueError('Simulation already holds an item.')
    if operation == 'PLACE' and state.get('held') is not True:
        raise ValueError('Run simulated PICK before PLACE.')
    if operation == 'PLACE' and state.get('calibration_sha256') != calibration.sha256:
        raise ValueError('The held simulation belongs to a different calibration.')
    if operation == 'PLACE' and state.get('grasp_sha256') != model.sha256:
        raise ValueError('The held simulation belongs to a different taught grasp.')
    base_marker = model.reference_marker.copy()
    base_marker[:2, 3] += np.asarray(shift, float) / 1000
    camera_target = invert(old.base_camera) @ base_marker
    fresh = Observation(role='pick', target=old.target, camera_target=camera_target,
        base_flange=old.base_flange, base_camera=old.base_camera, base_target=base_marker,
        robot=old.robot, quality=old.quality, refined=old.refined, calibration_sha256=calibration.sha256,
        notes=['Synthetic acquisition for this request.'])
    role = Role(target=target_spec, observe_joints=old.robot['joints_rad'],
                motion_model=settings.get('motion_model', 'full'), limits=settings.get('limits', {}))
    pose, report = target_pose(model, fresh, role, calibration_sha256=calibration.sha256,
                               tcp_offset=old.robot['tcp_offset'], target=target)
    record = {'simulation': True, 'operation': operation, 'job_id': job_id, 'created': time.time(),
              'calibration_sha256': calibration.sha256, 'grasp_sha256': model.sha256,
              'observation': fresh.to_json(), 'target_tcp_pose_m_rad': matrix_to_pose(pose),
              'validation': report, 'outcome': 'simulated only; no grip, release or motion occurred'}
    result_dir = project / 'simulated_results'
    result_dir.mkdir(exist_ok=True)
    result_path = result_dir / (job_id + '.json')
    if result_path.exists():
        raise ValueError('Result file already exists.')
    atomic_json(result_path, record)
    atomic_json(state_path, {'held': operation == 'PICK', 'calibration_sha256': calibration.sha256,
                             'grasp_sha256': model.sha256, 'last_job': job_id})
    return {'simulation': True, 'operation': operation, 'result_path': str(result_path),
            'target_tcp_pose_m_rad': record['target_tcp_pose_m_rad'], 'held_simulated': operation == 'PICK',
            'observation_time': fresh.timestamp, 'outcome': record['outcome']}
