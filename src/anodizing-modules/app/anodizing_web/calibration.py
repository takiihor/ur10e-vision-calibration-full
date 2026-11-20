"""Calibration jobs with explicit camera and automatic-control opt-ins."""
from hashlib import sha256
import json
from pathlib import Path
import time

from .paths import check_tree, inside


def defaults(root):
    from .site_profile import section
    fields = ('session', 'manifest', 'board', 'serial', 'robot_ip', 'camera_provider', 'camera_fps',
              'camera_exposure_us', 'camera_gain_db', 'intrinsics', 'calibration', 'verification',
              'collision_scene', 'views', 'tilt_deg', 'roll_deg')
    return {key: value for key, value in section(root, 'calibration').items()
            if key in fields and type(value) in (str, int, float)}


def check_stop(owner, job_id):
    if owner.stop_requested(job_id):
        raise ValueError('Calibration Stop requested.')


def artifact_sha256(path):
    with path.open('rb') as stream:
        content = stream.read(2_000_001)
    if len(content) > 2_000_000:
        raise ValueError('The calibration file exceeds 2 MB.')
    return sha256(content).hexdigest()


def teaching_handoff(root, job, selection):
    from ur_handeye.dataset import load_result
    from ur_handeye.ui import CalibrationUI

    fitted = job.get('result') or {}
    result = fitted.get('result') or {}
    if (job['module'] != 'calibration' or job['state'] != 'done' or result.get('type') != 'eye_in_hand'
            or result.get('verdict') not in ('good', 'acceptable')):
        raise ValueError('Select a completed usable calibration.')
    path = inside(root, selection, required=False)
    if str(path) != fitted.get('path'):
        raise ValueError('The selected path differs from the fitted calibration.')
    digest = artifact_sha256(path)
    if digest != fitted.get('calibration_sha256', digest) or load_result(path) != result:
        raise ValueError('The fitted calibration changed. Reopen or fit the session before teaching.')
    camera = result.get('camera') or {}
    robot = result.get('robot') or {}
    return {'calibration': str(path), 'calibration_sha256': digest, 'tcp': result['camera_in_tcp']['tcp_offset'],
            'robot_ip': robot.get('host', '') if robot.get('host') != 'simulated' else '',
            'serial': (camera.get('device') or {}).get('serial_number', ''),
            'camera_provider': camera.get('provider', 'realsense'),
            'camera_fps': (camera.get('stream') or {}).get('fps'),
            'camera_exposure_time_us': camera.get('exposure_time_us'),
            'simulation': CalibrationUI.is_simulated(result)}


def run(owner, root, body, job_id, allow_camera, allow_manual=False):
    action = body.get('action')
    if action == 'pose':
        if not allow_manual:
            raise PermissionError('Start the service with --allow-manual to read robot feedback.')
        from .preview_pose import read_pose
        return read_pose(body.get('robot_ip'))
    if action == 'scene':
        from .scene_save import save_scene
        return save_scene(root, job_id, body.get('scene'), owner.robot_model)
    if action == 'scene-read':
        from .scene_save import read_scene
        return read_scene(root, body.get('path'), owner.robot_model)
    if action == 'preview-calibration':
        from ur_handeye.dataset import load_result
        path = inside(root, body.get('path'))
        if path.stat().st_size > 2_000_000:
            raise ValueError('The calibration file exceeds 2 MB.')
        result = load_result(path)
        if result['type'] != 'eye_in_hand':
            raise ValueError('Select an eye-in-hand calibration for the wrist camera visual.')
        camera = result.get('camera') if isinstance(result.get('camera'), dict) else {}
        robot = result.get('robot') if isinstance(result.get('robot'), dict) else {}
        device = camera.get('device') if isinstance(camera.get('device'), dict) else {}
        serial = device.get('serial_number')
        return {'path': str(path), 'calibration_sha256': artifact_sha256(path),
                'camera_in_flange': result['camera_in_flange']['matrix'],
                'verdict': result['verdict'],
                'simulated': bool(result.get('simulation_truth') or camera.get('simulated')
                                  or robot.get('host') == 'simulated'),
                'camera_serial': serial if isinstance(serial, str) else None}
    if action in ('camera', 'camera-preview'):
        if not allow_camera:
            raise PermissionError('Start the service with --allow-camera to capture a board view.')
        from .camera import board_preview, camera_preview
        manifest = inside(root, body.get('manifest'))
        camera_options = {}
        if body.get('camera_exposure_us') is not None:
            camera_options['exposure_time_us'] = body['camera_exposure_us']
        if body.get('camera_gain_db') is not None:
            camera_options['gain_db'] = body['camera_gain_db']
        common = dict(enabled=True, camera_provider=body.get('camera_provider', 'realsense'),
                      camera_fps=body.get('camera_fps'), **camera_options)
        if action == 'camera-preview':
            return camera_preview(str(manifest), body.get('board', 'calibration_stage2'),
                                  body.get('serial', ''), **common)
        return board_preview(str(manifest), body.get('board', 'calibration_stage2'),
                             body.get('serial', ''), **common,
                             intrinsics=str(inside(root, body['intrinsics'])) if body.get('intrinsics') else None)
    if action == 'manual' and not allow_manual:
        raise PermissionError('Start the service with --allow-manual to collect pendant-positioned views.')
    if action not in ('simulate', 'solve', 'intrinsics', 'calibrate', 'verify', 'manual', 'plan', 'collect', 'replay'):
        raise ValueError('Unknown calibration action.')
    from ur_handeye.ui import build_command
    from ur_handeye.ui_jobs import JobRunner
    from ur_handeye.poses import PlanOptions

    automatic = action in ('plan', 'collect', 'replay')
    if automatic and (not owner.allow_auto_calibration or not owner.jobs[job_id].get('auto_scene')):
        raise PermissionError('Automatic calibration is disabled or has no accepted scene.')
    session = inside(root, body.get('session'), required=action not in ('simulate', 'manual', 'plan', 'collect'))
    session = check_tree(root, session)
    manifest = inside(root, body.get('manifest'))
    intrinsic = inside(root, body['intrinsics']) if action in ('solve', 'manual', 'plan', 'collect') and body.get('intrinsics') else None
    verification = check_tree(root, inside(root, body.get('verification'), required=action == 'verify')) if action in ('verify', 'replay') else None
    if action == 'collect' and session.exists():
        raise ValueError('Choose a new automatic collection session folder.')
    if action == 'replay' and verification.exists():
        raise ValueError('Choose a new verification session folder.')
    calibration = inside(root, body.get('calibration')) if action in ('verify', 'replay') else (
        inside(root, body['calibration']) if action == 'manual' and body.get('calibration') else None)
    if action == 'verify':
        from ur_handeye.dataset import load_result

        fitting = load_result(calibration).get('session')
        if isinstance(fitting, str) and fitting and (Path(root) / Path(fitting).expanduser()).resolve() == verification:
            raise ValueError('Choose fresh verification observations separate from the fitting session.')
    for directory in (session, verification):
        if directory and (directory / 'session.json').is_file():
            metadata = json.loads((directory / 'session.json').read_text())
            recorded = metadata.get('board', {}).get('manifest')
            if recorded:
                existing = Path(recorded)
                if existing.exists():
                    inside(root, str(existing))
            for entry in directory.rglob('*'):
                if entry.is_symlink():
                    inside(root, str(entry))
    settings = {'session': str(session), 'manifest': str(manifest),
                'intrinsics': str(intrinsic) if intrinsic else '', 'calibration_type': 'eye_in_hand',
                'camera_provider': body.get('camera_provider', 'realsense'),
                'camera_fps': body.get('camera_fps'),
                'camera_exposure_us': body.get('camera_exposure_us'),
                'camera_gain_db': body.get('camera_gain_db')}
    if action in ('manual', 'replay'):
        if action == 'replay' or 'fit_session' in body:
            fitting = session if action == 'replay' else check_tree(root, inside(root, body['fit_session']))
            if not calibration:
                raise ValueError('Select the fitted calibration for verification capture.')
            if fitting == (verification if action == 'replay' else session):
                raise ValueError('Choose a fresh verification session separate from the fitting session.')
            from ur_handeye.dataset import load_result
            from ur_handeye.ui import CalibrationUI
            result = load_result(calibration)
            if result.get('verdict') == 'poor' or result.get('type') != 'eye_in_hand' or CalibrationUI.is_simulated(result):
                raise ValueError('Select a usable real eye-in-hand calibration for verification capture.')
            if result.get('session') and inside(root, result['session']) != fitting:
                raise ValueError('The calibration does not match the fitting session.')
    if action == 'manual':
        from ur_handeye import cli
        if not getattr(cli, 'READ_ONLY_MANUAL_CAPTURE', False):
            raise ValueError('The read-only manual capture package is not active.')
        verification = session
    if action == 'manual' or automatic:
        robot_ip, serial = body.get('robot_ip'), body.get('serial')
        if not isinstance(robot_ip, str) or not robot_ip.strip() or any(c.isspace() for c in robot_ip):
            raise ValueError('Enter the robot IP or hostname for feedback.')
        if not isinstance(serial, str) or not serial.strip():
            raise ValueError('Enter the camera serial number.')
        settings.update(robot_ip=robot_ip.strip(), serial=serial.strip(), board=body.get('board', ''),
                        speed='0.1', views=str(body.get('views', 14)),
                        tilt_deg=str(body.get('tilt_deg', 25)), roll_deg=str(body.get('roll_deg', 30)),
                        collision_scene=owner.jobs[job_id]['auto_scene']['snapshot'] if automatic else '',
                        robot_model=owner.jobs[job_id]['auto_scene'].get('model', {}).get('snapshot', '') if automatic else '')
    argv = build_command(action, settings, calibration=str(calibration) if calibration else None,
                         verification=str(verification) if verification else None)
    if action == 'simulate':
        views = int(body.get('views', 14))
        tilt, roll = float(body.get('tilt_deg', 25)), float(body.get('roll_deg', 30))
        PlanOptions(tilt_deg=tilt, roll_deg=roll, max_views=views)
        if views > 14:
            raise ValueError('Choose 6 to 14 views.')
        board = body.get('board', 'calibration_stage2')
        if not isinstance(board, str) or len(board) > 100:
            raise ValueError('Invalid board name.')
        argv += ['--manifest', str(manifest), '--board', board, '--views', str(views),
                 '--tilt-deg', str(tilt), '--roll-deg', str(roll)]
    if body.get('collision_scene') and not automatic:
        from ur_handeye.collision import load_scene
        scene = inside(root, body['collision_scene'])
        load_scene(scene)
        owner.event(job_id, {'type': 'log', 'message': 'Collision scene loaded. Motion remains disabled.'})
    with owner.lock:
        if automatic:
            from .auto_scene import check
            check(root, owner.jobs[job_id]['auto_scene'])
        with owner.agv_start_lock:
            check_stop(owner, job_id)
            if action in ('collect', 'replay'):
                owner.check_agv()
            runner = JobRunner()
            owner.runner = runner
            runner.start(argv)
            if action in ('collect', 'replay'):
                owner.motion_job = job_id
    last_result = None
    exit_code = None
    worker_error = None
    cleanup_errors = None
    try:
        while exit_code is None:
            with owner.lock:
                for event in runner.drain():
                    if event.get('type') == 'calibration_cleanup':
                        cleanup_errors = event['errors']
                    if event.get('type') == 'error':
                        worker_error = event['message']
                    owner.event(job_id, event)
                    if event.get('type') == 'result':
                        path = inside(root, event['path'])
                        last_result = {'path': str(path), 'result': event['result'],
                                       'calibration_sha256': artifact_sha256(path)}
                    if event.get('type') == 'done':
                        exit_code = event['code']
            if exit_code is None:
                time.sleep(.05)
    finally:
        try:
            if runner.active:
                runner.stop()
                deadline = time.monotonic() + 5
                while runner.active:
                    list(runner.drain())
                    if runner.active and time.monotonic() > deadline:
                        runner.process.kill()
                        deadline = float('inf')
                    time.sleep(.05)
        finally:
            if automatic:
                with owner.lock:
                    job = owner.jobs[job_id]
                    if cleanup_errors or (cleanup_errors is None and job.get('calibration_motion_approved')):
                        owner.execution_uncertain = job['execution_uncertain'] = True
    if cleanup_errors:
        detail = ' '.join(dict.fromkeys(filter(None, (worker_error, *cleanup_errors))))
        raise ValueError(f'Calibration cleanup failed. {detail}')
    if cleanup_errors is None and owner.jobs.get(job_id, {}).get('execution_uncertain'):
        raise ValueError(worker_error or 'Calibration worker exited without confirming cleanup after motion approval.')
    if exit_code != 0:
        raise ValueError(worker_error or f'Calibration worker failed with exit code {exit_code}. Review the job log.')
    output = argv[argv.index('--output') + 1] if '--output' in argv else None
    completed_session = output if action in ('plan', 'collect', 'manual', 'replay') and output else session
    return last_result or {'message': 'Calibration worker finished.', 'session': str(completed_session)}
