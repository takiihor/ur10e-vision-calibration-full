"""Explicit camera-only board observation. No robot interface is used."""
import base64


def board_preview(manifest, board, serial, *, enabled=False, camera_provider='realsense',
                  intrinsics=None, camera_fps=None):
    if enabled is not True:
        raise PermissionError('Camera capture is disabled. Enable read-only camera capture on the server.')
    if not isinstance(serial, str) or not serial.strip():
        raise ValueError('Select a camera serial number.')
    import cv2
    from ur_handeye.vision import BoardObserver, BoardTarget

    if camera_provider not in ('realsense', 'basler'):
        raise ValueError('Select RealSense or Basler as the camera provider.')
    if camera_provider == 'basler':
        from argparse import Namespace
        from ur_handeye.cli import _camera, _intrinsics
        args = Namespace(camera_provider=camera_provider, serial=serial.strip(), intrinsics=intrinsics,
                         width=None, height=None, fps=camera_fps, exposure_time_us=5000)
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
        return {'camera': camera.metadata, 'board': target.describe(),
                'observation': observation.to_record(), 'usable': usable,
                'reasons': reasons, 'image_jpeg_base64': encoded}
    finally:
        camera.close()
