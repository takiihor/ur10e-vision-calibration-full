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

    def test_basler_exposure_and_gain_are_forwarded(self):
        import ur_handeye.cli as cli
        import ur_handeye.vision as vision
        camera = mock.Mock(metadata={'provider': 'basler'})
        observation = mock.Mock(image_bgr=None, pixels=[])
        observation.usable.return_value = (False, ['no_pose'])
        observation.to_record.return_value = {}
        with mock.patch.object(cli, '_intrinsics'), \
             mock.patch.object(cli, '_camera', return_value=camera) as factory, \
             mock.patch.object(vision, 'BoardTarget'), \
             mock.patch.object(vision, 'BoardObserver') as observer:
            observer.return_value.observe.return_value = observation
            result = board_preview('manifest', 'tank01', '42007093', enabled=True,
                                   camera_provider='basler', intrinsics='intrinsics.json',
                                   exposure_time_us=50000, gain_db=12.5)
        args = factory.call_args.args[0]
        self.assertEqual(args.exposure_time_us, 50000)
        self.assertEqual(args.gain, 12.5)
        self.assertEqual(result['camera']['provider'], 'basler')
        camera.close.assert_called_once_with()

    def test_basler_exposure_and_gain_ranges_are_checked(self):
        for kwargs, message in (({'exposure_time_us': 0}, 'exposure'),
                                ({'gain_db': 49}, 'gain')):
            with self.assertRaisesRegex(ValueError, message):
                board_preview('manifest', 'tank01', '42007093', enabled=True,
                              camera_provider='basler', intrinsics='intrinsics.json', **kwargs)

    def test_camera_preview_needs_no_intrinsics_and_returns_annotated_image(self):
        import numpy as np
        import charuco_realsense.basler as basler_module
        import charuco_realsense.detect as detect_module
        import ur_handeye.vision as vision
        from anodizing_web.camera import camera_preview
        frame = mock.Mock(bgr=np.zeros((12, 16, 3), dtype=np.uint8), intrinsics=None)
        camera = mock.Mock(metadata={'provider': 'basler'})
        camera.read.return_value = frame
        detection = mock.Mock(
            marker_ids=(17,),
            marker_corners=np.array([[[1, 1], [5, 1], [5, 5], [1, 5]]], dtype=np.float32),
            charuco_ids=(0, 1, 2, 3),
            charuco_corners=np.array([[[2, 2]], [[6, 2]], [[2, 6]], [[6, 6]]], dtype=np.float32))
        with mock.patch.object(basler_module, 'BaslerCamera', return_value=camera) as factory, \
             mock.patch.object(detect_module, 'CharucoDetector') as detector, \
             mock.patch.object(vision, 'BoardTarget') as target:
            detector.return_value.detect.return_value = detection
            target.return_value.describe.return_value = {'name': 'tank01'}
            target.return_value.board = mock.Mock()
            target.return_value.border_bits = 1
            result = camera_preview('manifest', 'tank01', '42007093', enabled=True,
                                    exposure_time_us=50000, gain_db=10)
        config = factory.call_args.args[0]
        self.assertEqual((config.width, config.height, config.exposure_time_us, config.gain_db),
                         (2448, 2048, 50000, 10))
        self.assertTrue(result['preview_only'])
        self.assertEqual(result['charuco_count'], 4)
        self.assertTrue(result['usable'])
        self.assertTrue(result['image_jpeg_base64'])
        camera.close.assert_called_once_with()
