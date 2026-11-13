import sys
from unittest import TestCase, mock
from anodizing_web.camera import board_preview


class CameraPreviewTest(TestCase):
    def test_default_does_not_load_or_open_camera(self):
        with mock.patch.dict(sys.modules, {'charuco_realsense.camera': None}):
            with self.assertRaises(PermissionError):
                board_preview('manifest', 'calibration_stage2', 'serial')

    def test_camera_closed_when_observation_fails(self):
        import charuco_realsense.camera as camera_module
        import ur_handeye.vision as vision
        camera = mock.Mock()
        with mock.patch.object(camera_module, 'RealsenseCamera', return_value=camera) as factory, \
             mock.patch.object(vision, 'BoardTarget'), \
             mock.patch.object(vision, 'BoardObserver') as observer:
            observer.return_value.observe.side_effect = RuntimeError('capture failed')
            with self.assertRaisesRegex(RuntimeError, 'capture failed'):
                board_preview('manifest', 'calibration_stage2', '123', enabled=True)
        factory.assert_called_once()
        camera.close.assert_called_once_with()

    def test_fake_capture_returns_image_and_releases_camera(self):
        import numpy as np
        import charuco_realsense.camera as camera_module
        import ur_handeye.vision as vision
        camera = mock.Mock(metadata={'device': {'serial_number': '123'}})
        target = mock.Mock()
        target.describe.return_value = {'name': 'calibration_stage2'}
        observation = mock.Mock(image_bgr=np.zeros((10, 20, 3), dtype=np.uint8), pixels=[])
        observation.usable.return_value = (False, ['no_pose'])
        observation.to_record.return_value = {'charuco_ids': []}
        with mock.patch.object(camera_module, 'RealsenseCamera', return_value=camera), \
             mock.patch.object(vision, 'BoardTarget', return_value=target), \
             mock.patch.object(vision, 'BoardObserver') as observer:
            observer.return_value.observe.return_value = observation
            result = board_preview('manifest', 'calibration_stage2', '123', enabled=True)
        self.assertEqual(result['camera']['device']['serial_number'], '123')
        self.assertEqual(result['board']['name'], 'calibration_stage2')
        self.assertFalse(result['usable'])
        self.assertEqual(result['reasons'], ['no_pose'])
        self.assertTrue(result['image_jpeg_base64'])
        observer.return_value.observe.assert_called_once_with(frames=5)
        camera.close.assert_called_once_with()

    def test_serial_required_before_open(self):
        with self.assertRaises(ValueError):
            board_preview('manifest', 'calibration_stage2', '', enabled=True)
