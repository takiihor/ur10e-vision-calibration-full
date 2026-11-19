"""Read-only teaching adapter and a separately labelled synthetic artifact chain."""
import json
import time
from pathlib import Path

from .jobs import atomic_json
from .paths import check_tree, inside


def checked_settings(root, settings):
    if not isinstance(settings, dict):
        raise ValueError('Teaching settings must be an object.')
    settings = dict(settings)
    settings['project'] = str(check_tree(root, inside(root, settings.get('project'), required=False)))
    settings['calibration'] = str(inside(root, settings.get('calibration')))
    target = settings.get('target')
    if not isinstance(target, dict):
        raise ValueError('Select a marker target.')
    target = dict(target)
    for key in ('board_manifest', 'aruco_manifest'):
        if target.get(key):
            target[key] = str(inside(root, target[key]))
    settings['target'] = target
    return settings


def defaults(root):
    """Discover setup inputs under the allowed data directory. No device is opened."""
    from dataclasses import asdict
    from datetime import datetime
    from ur_pick_place.vision.config import Limits
    from ur_pick_place.vision.handeye import load_handeye
    from ur_pick_place.vision.teaching import picking_boards

    repo = Path(root).resolve()
    manifests = []
    for base in (repo / 'boards', repo / 'data' / 'boards'):
        if not base.is_dir():
            continue
        for path in sorted(base.rglob('board_manifest.json')):
            if not path.is_file() or path.is_symlink():
                continue
            try:
                manifests.append(str(inside(repo, str(path))))
            except ValueError:
                continue
    boards = picking_boards(manifests)
    picking = [b for b in boards if 'calibration' not in b['target']['board'].lower()]
    preferred = [b for b in picking if all(abs(a - 80) < 1e-9 for a in b['dimensions_mm'])] or picking
    target = dict(preferred[0]['target']) if preferred else {
        'kind': 'charuco', 'board': '', 'board_manifest': ''}

    calibrations = []
    seen = set()
    for path in sorted((repo / 'calibrations').glob('*/calibration.json')) + sorted(
            (repo / 'handeye').glob('*/calibration.json')):
        try:
            calibration = load_handeye(path)
            if calibration.simulated:
                continue
            resolved = str(inside(repo, str(path)))
            if resolved in seen:
                continue
            seen.add(resolved)
            raw = json.loads(path.read_text())
            calibrations.append({'path': resolved, 'tcp': calibration.tcp_offset,
                                 'robot_ip': raw.get('robot', {}).get('host', '')})
        except (OSError, ValueError, KeyError, TypeError):
            continue

    projects = sorted((repo / 'teaching').glob('*/project.json'),
                      key=lambda p: p.stat().st_mtime, reverse=True)
    project = {'project': str((repo / 'teaching' / datetime.now().strftime('%Y%m%d-%H%M%S')).relative_to(repo)),
               'calibration': '', 'robot_ip': '', 'tcp': [0.0] * 6,
               'camera_provider': 'realsense', 'refine_before_pick': False,
               'min_pixels_per_module': 5.0, 'target': target,
               'motion_model': 'planar', 'approach_mm': 100.0,
               'limits': asdict(Limits())}
    for path in projects:
        try:
            saved = json.loads(path.read_text())
            settings = saved['settings']
            saved_target = settings.get('target')
            if (saved.get('flow', {}).get('taught') is not True
                    or not isinstance(saved_target, dict)
                    or saved_target.get('board') not in [b['target']['board'] for b in preferred]):
                continue
            project.update({key: value for key, value in settings.items()
                            if key in ('calibration', 'robot_ip', 'tcp', 'camera_provider',
                                       'refine_before_pick', 'min_pixels_per_module', 'target',
                                       'motion_model', 'approach_mm')})
            project['project'] = str(path.parent.relative_to(repo))
            break
        except (OSError, ValueError, KeyError, TypeError):
            continue
    return {'calibrations': calibrations, 'manifests': manifests,
            'picking_boards': boards, 'settings': project}


def simulated(root, body):
    from ur_pick_place.vision.handeye import load_handeye
    from ur_pick_place.vision.grasp import teach
    from ur_pick_place.vision.config import Limits
    from ur_pick_place.vision.observe import Observation
    from ur_pick_place.vision.targets import target_from_config
    from ur_pick_place.vision.transforms import invert, pose_to_matrix, matrix_to_pose
    from ur_pick_place.vision.teaching import digest
    import numpy as np

    project = check_tree(root, inside(root, body.get('project'), required=False))
    calibration_path = inside(root, body.get('calibration'))
    if project.exists() and (not project.is_dir() or any(project.iterdir())):
        raise ValueError('Choose a new empty project directory. Existing data is preserved.')
    calibration = load_handeye(calibration_path)
    if not calibration.simulated:
        raise ValueError('Simulated teaching requires a simulated calibration.')
    target_spec = body.get('target')
    settings = checked_settings(root, {'project': str(project), 'calibration': str(calibration_path),
        'target': target_spec, 'robot_ip': 'simulation', 'tcp': calibration.tcp_offset,
        'motion_model': 'planar', 'approach_mm': 100})
    target = target_from_config(settings['target'])
    base_camera = calibration.camera_in_flange.copy()
    base_marker = pose_to_matrix([0.4, 0.1, 0.3, 0, 0, 0])
    observation = Observation(role='pick', target=target.describe(), camera_target=invert(base_camera) @ base_marker,
        base_flange=np.eye(4), base_camera=base_camera, base_target=base_marker,
        robot={'joints_rad': [0.0] * 6, 'tcp_offset': calibration.tcp_offset},
        quality={'position_spread_mm': 0.0, 'rotation_spread_deg': 0.0}, refined=False,
        calibration_sha256=calibration.sha256, notes=['Synthetic observation. No hardware was read.'])
    tcp = matrix_to_pose(base_marker @ pose_to_matrix([0, 0, 0.04, 3.141592653589793, 0, 0]))
    grasp = teach(observation, tcp, role='pick', calibration=calibration, limits=Limits())
    project.mkdir(parents=True, exist_ok=True)
    inputs = {'calibration': calibration.sha256}
    for key in ('board_manifest', 'aruco_manifest'):
        if settings['target'].get(key):
            inputs['manifest'] = digest(settings['target'][key])
    atomic_json(project / 'project.json', {'schema_version': 1, 'type': 'marker_pick_project',
        'simulation': True, 'settings': {k: v for k, v in settings.items() if k != 'project'},
        'inputs': inputs, 'flow': {'captured': True, 'taught': True, 'exported': False}})
    atomic_json(project / 'observation.json', observation.to_json())
    atomic_json(project / 'grasp_pick.json', grasp)
    return {'simulation': True, 'project': str(project), 'calibration_sha256': calibration.sha256,
            'files': ['project.json', 'observation.json', 'grasp_pick.json']}



def session_state(owner, job_id):
    with owner.lock:
        if not owner.teacher_service or owner.teacher_job != job_id:
            raise ValueError('This Teacher session is no longer active. Open the project and capture again.')
        state = owner.teacher_service.state()
        scene = (owner.jobs.get(job_id) or {}).get('auto_scene') or {}
        state['collision_scope'] = scene.get('collision_scope')
        state['descent_alignment'] = dict(owner.current_alignment) if owner.current_alignment else None
        state['execution_uncertain'] = owner.execution_uncertain
        state['motion_available'] = bool(owner.allow_teacher_motion and owner.robot_model and not owner.execution_uncertain)
        state['motion_unavailable_reason'] = ('Device execution is uncertain. Reconciliation is required.' if owner.execution_uncertain else 'Checked joint-space movement requires --allow-teacher-motion and the selected factory robot model.')
        return state


def control(owner, job_id, action):
    if action not in ('heartbeat', 'stop'):
        raise ValueError('Unknown Teacher control.')
    with owner.lock:
        if not owner.teacher_service or owner.teacher_job != job_id:
            raise ValueError('This Teacher control belongs to an old session.')
        service = owner.teacher_service
        if action == 'stop':
            service.stop()
        else:
            service.heartbeat()
    return {'ok': True}


def _identity(settings):
    return {k:v for k,v in settings.items() if k not in ('project', 'tcp_confirmed')}


def agv_capture(connection, station):
    from .recipe_agv import motion_state
    state = motion_state(connection)
    reported = state['teaching_station']
    if reported is not None:
        station = reported
    elif type(station) is not int or not 1 <= station <= 255:
        raise ValueError('AGV not reporting: enter the station this tank is at (1 to 255).')
    return dict(station=station, source='agv_status' if reported is not None else 'operator',
                status_time=state['status_time'], transport=state['transport'])


def run(owner, root, body, job_id, allow_teaching):
    action = body.get('action')
    if action == 'simulate':
        return simulated(root, body)
    if action == 'vertical_descent':
        if not allow_teaching:
            raise PermissionError('Read-only Teacher setup must also be enabled.')
        from .manual_execution import run as execute
        return execute(owner,root,body,job_id)
    if action in ('validate_move', 'return_observation') and not owner.allow_teacher_motion:
        raise PermissionError('Start the service with --allow-teacher-motion after reviewing the checked joint-space path.')
    if action not in ('configure', 'capture', 'teach', 'locate', 'capture_validation', 'review_validation', 'validate_move', 'return_observation'):
        raise ValueError('Unknown teaching action.')
    if not allow_teaching:
        raise PermissionError('Start the service with --allow-teaching to use read-only teaching.')
    from ur_pick_place.vision.teach_ui import TeachingService
    from ur_pick_place.vision.teaching import read_json
    if action == 'configure':
        settings = checked_settings(root, body.get('settings'))
    else:
        project = check_tree(root, inside(root, body.get('project')))
        saved = read_json(project / 'project.json')
        if saved.get('simulation'):
            raise ValueError('Synthetic projects cannot use hardware teaching.')
        settings = checked_settings(root, dict(saved['settings'], project=str(project), tcp_confirmed=True))
    service = owner.teacher_service
    if action == 'configure' or service is None or owner.teacher_project != settings['project']:
        if service:
            service.close()
        from .teacher_motion import native_backend
        service = TeachingService(Path(root), motion_factory=native_backend)
        try:
            service.configure(settings)
        except BaseException:
            service.close()
            raise
        with owner.lock:
            owner.teacher_service = service
            owner.teacher_project = settings['project']
            native = service.state().get('project') or {}
            owner.teacher_settings = _identity(checked_settings(root, dict(native.get('settings', settings), project=settings['project'])))
    elif _identity(settings) != owner.teacher_settings:
        raise ValueError('Teacher settings changed. Check setup and capture again.')
    with owner.lock:
        owner.teacher_job = job_id
    if action == 'configure':
        return service.state()
    started = False
    try:
        if action in ('capture_validation', 'validate_move') and settings.get('motion_model') != 'planar':
            raise ValueError('Only the planar-endpoint joint test is available. Full-pose validation is disabled.')
        if action == 'review_validation':
            return service.review_validation(body)
        if action in ('validate_move', 'return_observation'):
            from functools import partial
            from ur_handeye.collision import load_scene
            from .auto_scene import check
            from .teacher_motion import TeacherJointMotion, TeacherReturnMotion
            from ur_pick_place.config import number
            binding = owner.jobs[job_id].get('auto_scene')
            if not binding or not binding.get('model'):
                raise ValueError('Select a confirmed collision scene and factory robot model.')
            check(root, binding)
            scene = load_scene(binding['snapshot'], model_path=binding['model']['snapshot'])
            scene.require_motion_ready()
            if scene.model['nominal']:
                raise ValueError('Teacher movement requires the factory robot model.')
            speed = number(body.get('joint_speed_deg_s'), 'Joint test speed (degrees/s)', low=1, high=6)
            def check_binding():
                owner.check_agv()
                check(root, binding)

            motion_class = partial(TeacherReturnMotion if action == 'return_observation' else TeacherJointMotion,
                collision=scene, check_binding=check_binding,
                evidence=owner.directory/(job_id+'.teacher-path.json'))
            if action == 'return_observation':
                service.return_motion_class = motion_class
            else:
                service.validation_motion_class = motion_class
            started = True
            service.joint_validation = True
            if body.get('workspace_min_m') is None or body.get('workspace_max_m') is None:
                raise ValueError('Enter reviewed workspace bounds; no workspace defaults are applied.')
            with owner.agv_start_lock:
                owner.check_agv()
                if action == 'return_observation':
                    service.return_observation(dict(body, joint_speed_deg_s=speed))
                else:
                    service.move_validation(dict(body, speed_mm_s=10, joint_speed_deg_s=speed))
        else:
            if action == 'teach' and body.get('marker_unchanged') is not True:
                raise ValueError('Confirm that the marker and object stayed fixed.')
            options = {'marker_unchanged': body.get('marker_unchanged')}
            capture = {'agv': agv_capture(owner.agv, body.get('agv_station'))} if action == 'capture' else {}
            started = True
            service.submit(action, options, **capture)
        last_status = None
        while True:
            state = service.state()
            motion = state.get('motion') or {}
            status = motion.get('error') or motion.get('message') or state.get('error') or state.get('busy')
            if status and status != last_status:
                try:
                    owner.event(job_id, {'type':'log', 'message':str(status)})
                except Exception as exc:
                    owner.storage_error = 'Teacher event persistence failed: '+str(exc)
                    raise
                last_status = status
            if not state['busy']:
                result = (state.get('return_result') if action == 'return_observation'
                          else (state.get('project') or {}).get('validation')) or {}
                if result.get('execution_uncertain'):
                    with owner.lock:
                        owner.execution_uncertain = True
                        owner.jobs[job_id]['execution_uncertain'] = True
                        owner._save(owner.jobs[job_id])
                if state['error']:
                    raise ValueError(state['error'])
                return state
            time.sleep(.05)
    except BaseException as exc:
        if started:
            _abort_and_drain(owner, service, job_id, exc)
        raise


def _abort_and_drain(owner, service, job_id, cause):
    with owner.lock:
        owner.jobs[job_id]['error'] = str(cause)
    cleanup_error = None
    try:
        service.stop()
        service.close()
    except Exception as exc:
        cleanup_error = str(exc)
    def fence(reason):
        with owner.lock:
            owner.execution_uncertain = True
            owner.jobs[job_id]['execution_uncertain'] = True
            owner.jobs[job_id]['error'] += '; cleanup uncertain: '+reason
            try:
                owner._save(owner.jobs[job_id])
            except Exception as exc:
                owner.storage_error = str(exc)

    uncertain = cleanup_error is not None
    if uncertain:
        fence(cleanup_error)
    while True:
        # The supervisor can publish a native worker until it exits.
        with service.lock:
            motion = getattr(service, 'motion', None)
            workers = (getattr(service, 'worker_thread', None), getattr(service, 'motion_thread', None),
                       getattr(motion, '_worker', None))
            workers = [worker for worker in workers if worker and worker.is_alive()]
        if not uncertain and getattr(motion, 'cleanup_uncertain', False):
            fence('native worker still active')
            uncertain = True
        if not workers:
            break
        if not uncertain:
            for worker in workers:
                worker.join(timeout=12)
                if worker.is_alive():
                    fence('native worker still active')
                    uncertain = True
                    break
        owner.drain_workers(job_id, workers)
    with owner.lock:
        owner.teacher_service = None
        owner.teacher_job = owner.teacher_project = owner.teacher_settings = None
