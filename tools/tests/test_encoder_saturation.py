"""Raw saturation excludes edge measurements without discarding a whole view."""
from contextlib import redirect_stdout
import io
from pathlib import Path
import sys
import tempfile
import unittest

import cv2
import numpy as np

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from tools import encoder_intrinsics as encoder
from tools.tests.test_encoder_growth import rendered_network


def lattice_image(dark=False):
    image = np.full((480, 640), 30, np.uint8)
    for row in range(9):
        for col in range(12):
            if (col + row) % 2 == 0:
                x, y = 40 + 45*col, 35 + 45*row
                cv2.rectangle(image, (x-14, y-14), (x+14, y+14), 220, -1)
    image = cv2.GaussianBlur(image, (5, 5), .7)
    return 255-image if dark else image


class EncoderSaturationTests(unittest.TestCase):
    def test_smoothing_support_is_guarded_without_excessive_exclusion(self):
        # Right edge x=234, search to x=242, sigma0.6 support reaches x=245.
        for x, removed in [(244, 1), (246, 0)]:
            with self.subTest(saturated_x=x):
                image = lattice_image()
                image[215, x] = 255
                stats = {}
                corners, _ = encoder.detect_lattice(image, diagnostics=stats)
                self.assertEqual(stats['saturated_edge_squares_removed'], removed)
                self.assertEqual(len(corners), 54-removed)

    def test_predicted_dark_square_with_saturated_edge_is_removed(self):
        image, _, _, _, _ = rendered_network()
        baseline, _ = encoder.detect_lattice(image)
        target = np.array([695.064111, 98.710037])
        self.assertLess(np.linalg.norm(encoder.square_centres(baseline)-target, axis=1).min(), .1)
        # This dark square belongs to the grown network outside the bright seed.
        image[119, 696] = 255
        stats = {}
        corners, _ = encoder.detect_lattice(image, diagnostics=stats)
        self.assertEqual(stats['saturated_edge_squares_removed'], 1)
        self.assertEqual(len(corners), len(baseline)-1)
        self.assertGreater(np.linalg.norm(encoder.square_centres(corners)-target, axis=1).min(), 20)

    def test_public_detector_removes_only_square_with_saturated_edge(self):
        for dark in (False, True):
            with self.subTest(dark=dark):
                clean = lattice_image(dark)
                baseline, baseline_indices = encoder.detect_lattice(clean)
                self.assertEqual(len(baseline), 54)
                baseline_centres = encoder.square_centres(baseline)
                image = clean.copy()
                # One native pixel in the middle of the target's right-edge window.
                image[215, 237 if dark else 234] = 255
                corners, indices = encoder.detect_lattice(image)
                centres = encoder.square_centres(corners)
                self.assertEqual(len(corners), 53, 'Saturated edge still enters calibration')
                self.assertGreater(np.linalg.norm(centres-[220, 215], axis=1).min(), 20)
                distances = np.linalg.norm(centres[:, None]-baseline_centres[None, :], axis=2)
                matched = distances.argmin(axis=1)
                truth = np.array([(40+45*i, 35+45*j) for j in range(9) for i in range(12)
                                  if (i+j) % 2 == 0])
                # Seed and grown edges can use different windows; compare to known geometry.
                errors = np.linalg.norm(centres[:, None]-truth[None, :], axis=2)
                self.assertLess(errors.min(axis=1).max(), .5)
                # Removing a measurement must not alter any surviving integer identity.
                np.testing.assert_array_equal(
                    np.sum((indices[:, None]-indices[None, :])**2, axis=2),
                    np.sum((baseline_indices[matched, None]-baseline_indices[matched][None, :])**2, axis=2))
                stats = {}
                checked, _ = encoder.detect_lattice(image, diagnostics=stats)
                self.assertEqual(len(checked), 53)
                self.assertEqual(stats['squares_before_saturation_filter'], 54)
                self.assertEqual(stats['saturated_edge_squares_removed'], 1)

    def test_native_saturation_only_interior_or_remote_does_not_remove_square(self):
        for dark in (False, True):
            for location, value in [((220, 215), 255), ((5, 5), 255), ((234, 215), 254)]:
                with self.subTest(dark=dark, location=location, value=value):
                    image = lattice_image(dark)
                    image[location[1], location[0]] = value
                    stats = {}
                    corners, _ = encoder.detect_lattice(image, diagnostics=stats)
                    self.assertEqual(len(corners), 54)
                    self.assertEqual(stats['saturated_edge_squares_removed'], 0)
                    self.assertEqual(stats['squares_before_saturation_filter'], 54)

    def test_cli_observations_keeps_frame_with_unsaturated_squares(self):
        image = lattice_image()
        image[215, 234] = 255
        with tempfile.TemporaryDirectory() as folder, redirect_stdout(io.StringIO()):
            path = Path(folder)/'glare.png'
            self.assertTrue(cv2.imwrite(str(path), image))
            views = encoder.observations(folder, image.shape[::-1], .00168)
        self.assertEqual(len(views), 1)
        self.assertEqual(len(views[0][1]), 53)
        self.assertEqual(len(views[0][2]), 53)


if __name__ == '__main__':
    unittest.main()
