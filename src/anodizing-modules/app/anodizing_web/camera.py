"""Explicit camera-only board observation. No robot interface is used."""
import base64


def _basler_options(exposure_time_us, gain_db):
    try:
        exposure = 5000.0 if exposure_time_us is None else float(exposure_time_us)
        gain = 0.0 if gain_db is None else float(gain_db)
    except (TypeError, ValueError) as exc:
        raise ValueError('Basler exposure and gain must be numbers.') from exc
    if not 0 < exposure <= 1_000_000:
        raise ValueError('Basler exposure must be between 1 and 1000000 microseconds.')
    if not 0 <= gain <= 48:
        raise ValueError('Basler gain must be between 0 and 48 dB.')
    return exposure, gain


def board_preview(manifest, board, serial, *, enabled=False, camera_provider='realsense',
                  intrinsics=None, camera_fps=None, exposure_time_us=None, gain_db=None):
    if enabled is not True:
        raise PermissionError('Camera capture is disabled. Enable read-only camera capture on the server.')
    if not isinstance(serial, str) or not serial.strip():
        raise ValueError('Select a camera serial number.')
    import cv2
    from ur_handeye.vision import BoardObserver, BoardTarget, snapshot_evidence

    if camera_provider not in ('realsense', 'basler'):
        raise ValueError('Select RealSense or Basler as the camera provider.')
    if camera_provider == 'basler':
        from argparse import Namespace
        from ur_handeye.cli import _camera, _intrinsics
        exposure, gain = _basler_options(exposure_time_us, gain_db)
        args = Namespace(camera_provider=camera_provider, serial=serial.strip(), intrinsics=intrinsics,
                         width=None, height=None, fps=camera_fps, exposure_time_us=exposure,
                         gain=gain, lock_exposure=False)
        _intrinsics(args)
        target = BoardTarget(manifest, board)
        camera = _camera(args)
    else:
        from charuco_realsense.camera import RealsenseCamera, StreamConfig
        target = BoardTarget(manifest, board)
        camera = RealsenseCamera(serial.strip(), StreamConfig(width=1280, height=720, fps=15 if camera_fps is None else camera_fps))
    try:
        observation = BoardObserver(camera, target).observe(frames=5)
        usable, reasons = observation.usable(0.3)
        image = observation.image_bgr.copy() if observation.image_bgr is not None else None
        encoded = None
        if image is not None:
            for x, y in observation.pixels:
                cv2.circle(image, (round(float(x)), round(float(y))), 5, (70, 210, 70), 2)
            height, width = image.shape[:2]
            if width > 800:
                image = cv2.resize(image, (800, round(height * 800 / width)))
            ok, jpeg = cv2.imencode('.jpg', image, [cv2.IMWRITE_JPEG_QUALITY, 85])
            if not ok:
                raise ValueError('Could not encode the camera image.')
            encoded = base64.b64encode(jpeg).decode('ascii')
        record = observation.to_record()
        metadata = camera.metadata
        return {'camera': metadata, 'board': target.describe(), 'observation': record, 'usable': usable,
                'snapshot': snapshot_evidence(observation.image_frame, metadata, 'Burst pose detected' if record.get('pose') else 'No pose') if encoded else None,
                'reasons': reasons, 'image_jpeg_base64': encoded}
    finally:
        camera.close()


def camera_preview(manifest, board, serial, *, enabled=False, camera_provider='basler',
                   exposure_time_us=None, gain_db=None, camera_fps=None):
    """Camera-only focus/exposure check. No intrinsics, pose or calibration claim."""
    if enabled is not True:
        raise PermissionError('Camera capture is disabled. Enable read-only camera capture on the server.')
    if not isinstance(serial, str) or not serial.strip():
        raise ValueError('Select a camera serial number.')
    if camera_provider not in ('realsense', 'basler'):
        raise ValueError('Select RealSense or Basler as the camera provider.')
    import cv2
    import numpy as np
    from charuco_realsense.detect import CharucoDetector
    from ur_handeye.vision import BoardTarget, frame_evidence, snapshot_evidence

    target = BoardTarget(manifest, board)
    detector = CharucoDetector(target.board, target.border_bits, refine=False)
    if camera_provider == 'basler':
        from types import SimpleNamespace
        from charuco_realsense.basler import BaslerCamera
        exposure, gain = _basler_options(exposure_time_us, gain_db)
        fps = 20 if not camera_fps or int(camera_fps) <= 0 else int(camera_fps)
        camera = BaslerCamera(SimpleNamespace(serial=serial.strip(), width=2448, height=2048,
                                              fps=fps, exposure_time_us=exposure, gain_db=gain))
    else:
        from charuco_realsense.camera import RealsenseCamera, StreamConfig
        fps = 15 if not camera_fps or int(camera_fps) <= 0 else int(camera_fps)
        camera = RealsenseCamera(serial.strip(), StreamConfig(width=1280, height=720, fps=fps))
    try:
        best = None
        for _ in range(3):
            frame = camera.read()
            evidence = frame_evidence(frame)
            gray = cv2.cvtColor(frame.bgr, cv2.COLOR_BGR2GRAY)
            detection = detector.detect(gray, frame.intrinsics)
            if best is None or len(detection.charuco_ids) > len(best[1].charuco_ids):
                best = (frame, detection, evidence)
        frame, detection, evidence = best
        image = frame.bgr.copy()
        if detection.marker_ids:
            cv2.aruco.drawDetectedMarkers(image, detection.marker_corners.reshape(-1, 1, 4, 2),
                                          np.asarray(detection.marker_ids))
        for index, point in zip(detection.charuco_ids, detection.charuco_corners.reshape(-1, 2)):
            cv2.circle(image, (round(float(point[0])), round(float(point[1]))), 6, (70, 210, 70), 2)
            cv2.putText(image, str(index), (round(float(point[0])) + 8, round(float(point[1])) - 8),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.7, (70, 210, 70), 2)
        height, width = image.shape[:2]
        if width > 800:
            image = cv2.resize(image, (800, round(height * 800 / width)))
        ok, jpeg = cv2.imencode('.jpg', image, [cv2.IMWRITE_JPEG_QUALITY, 85])
        if not ok:
            raise ValueError('Could not encode the camera image.')
        reasons = [] if detection.charuco_ids else (
            ['no_markers'] if not detection.marker_ids else ['no_charuco_corners'])
        metadata = camera.metadata
        return {'camera': metadata, 'board': target.describe(), 'preview_only': True,
                'snapshot': snapshot_evidence(evidence, metadata, 'No pose · camera preview'),
                'marker_ids': list(detection.marker_ids), 'charuco_ids': list(detection.charuco_ids),
                'charuco_count': len(detection.charuco_ids), 'usable': bool(detection.charuco_ids),
                'reasons': reasons, 'image_jpeg_base64': base64.b64encode(jpeg).decode('ascii')}
    finally:
        camera.close()
