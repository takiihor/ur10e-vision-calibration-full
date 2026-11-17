"""Capture validation and lossless file handling without camera hardware."""
import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import cv2
import numpy as np

from tools import capture_encoder_intrinsics as capture


class CaptureTests(unittest.TestCase):
    def test_serial_is_required_before_opening_camera(self):
        with tempfile.TemporaryDirectory() as tmp:
            args = ['--capture-dir', tmp, '--split', 'train', '--pitch-mm', '1.7']
            stderr = io.StringIO()
            with patch.object(capture, 'BaslerCamera', side_effect=AssertionError('Camera opened without --serial')) as camera, \
                 patch.object(capture.cv2, 'destroyAllWindows'), \
                 contextlib.redirect_stderr(stderr), self.assertRaises(SystemExit) as error:
                capture.main(args)
            self.assertEqual(error.exception.code, 2)
            self.assertIn('--serial', stderr.getvalue())
            camera.assert_not_called()
            self.assertEqual(list(Path(tmp).iterdir()), [])

    def test_invalid_arguments_do_not_open_camera(self):
        with tempfile.TemporaryDirectory() as tmp:
            common = ['--capture-dir', tmp, '--split', 'train', '--pitch-mm', '1.7', '--serial', '12345']
            invalid = [(['--exposure-us', value]) for value in ['0', '-1', '20001', 'nan', 'inf']]
            invalid += [(['--pitch-mm', value]) for value in ['0', '-1', 'nan', 'inf']]
            invalid += [['--serial', 'bad'], ['--serial', '']]
            with patch.object(capture, 'BaslerCamera') as camera:
                for extra in invalid:
                    with self.subTest(extra=extra), contextlib.redirect_stderr(io.StringIO()), self.assertRaises((ValueError, SystemExit)):
                        capture.main(common + extra)
                    camera.assert_not_called()

    def test_saves_raw_mono8_and_never_overwrites(self):
        image = np.arange(60, dtype=np.uint8).reshape(6, 10)
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            (folder / '001.png').write_bytes(b'existing')
            capture.save_frame(folder, image)
            self.assertEqual((folder / '001.png').read_bytes(), b'existing')
            np.testing.assert_array_equal(cv2.imread(str(folder / '002.png'), cv2.IMREAD_UNCHANGED), image)

    def test_fake_camera_review_and_save(self):
        image = np.arange(600, dtype=np.uint8).reshape(20, 30)
        camera = MagicMock()
        camera.metadata = {'serial': '12345'}
        context = MagicMock()
        context.__enter__.return_value = camera
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            args = ['--capture-dir', tmp, '--split', 'train', '--pitch-mm', '1.7',
                    '--serial', '12345', '--count', '1']
            with patch.object(capture, 'BaslerCamera', return_value=context), \
                 patch.object(capture, 'camera_config', return_value={}), \
                 patch.object(capture, 'read_gray', return_value=image), \
                 patch.object(capture, 'detect_lattice', return_value=(np.zeros((0, 4, 2)), np.zeros((0, 2)))), \
                 patch.object(capture.cv2, 'namedWindow'), \
                 patch.object(capture.cv2, 'imshow'), \
                 patch.object(capture.cv2, 'waitKey', side_effect=[32, 13]), \
                 patch.object(capture.cv2, 'getWindowProperty', return_value=1), \
                 patch.object(capture.cv2, 'destroyAllWindows') as destroy:
                self.assertEqual(capture.main(args), 0)
            np.testing.assert_array_equal(cv2.imread(str(folder / 'train/001.png'), cv2.IMREAD_UNCHANGED), image)
            profile = json.loads((folder / 'capture-profile.json').read_text())
            self.assertEqual(profile['serial'], '12345')
            self.assertEqual(profile['nominal_cell_pitch_mm'], 1.7)
            context.__exit__.assert_called_once()
            destroy.assert_called_once()

    def test_rejects_changed_capture_profile_without_writing_split(self):
        camera = MagicMock()
        camera.metadata = {'serial': '12345'}
        context = MagicMock()
        context.__enter__.return_value = camera
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            (folder / 'capture-profile.json').write_text(json.dumps({'serial': 'different'}))
            args = ['--capture-dir', tmp, '--split', 'check', '--pitch-mm', '1.7', '--serial', '12345']
            with patch.object(capture, 'BaslerCamera', return_value=context), \
                 patch.object(capture, 'camera_config', return_value={}), \
                 patch.object(capture.cv2, 'destroyAllWindows'):
                with self.assertRaises(ValueError):
                    capture.main(args)
            self.assertFalse((folder / 'check').exists())
            self.assertEqual(json.loads((folder / 'capture-profile.json').read_text()), {'serial': 'different'})
            context.__exit__.assert_called_once()

    def test_encode_failure_leaves_no_partial_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(capture.cv2, 'imencode', return_value=(False, None)):
                with self.assertRaises((ValueError, RuntimeError, OSError)):
                    capture.save_frame(Path(tmp), np.zeros((10, 10), np.uint8))
            self.assertEqual(list(Path(tmp).iterdir()), [])


if __name__ == '__main__':
    unittest.main()
