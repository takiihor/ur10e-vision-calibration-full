"""Prediction/refinement regressions; image labels do not come from the detector."""
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

import cv2
import numpy as np

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from tools import encoder_intrinsics as encoder


def rendered_network(shift=False):
    """Render connected dark cells and curved edges with known barrel distortion."""
    size, margin, scale = (800, 640), 240, 3
    image = np.full(((size[1] + 2*margin)*scale,
                     (size[0] + 2*margin)*scale), 220, np.uint8)

    def point(p):
        return tuple(np.rint((np.asarray(p) + margin)*scale + (scale-1)/2).astype(int))

    def rect(a, b, colour, thickness=-1):
        cv2.rectangle(image, point(a), point(b), colour,
                      thickness*scale if thickness > 0 else thickness)

    def line(a, b, thickness):
        cv2.line(image, point(a), point(b), 30, thickness*scale)

    for x in range(-220, 1041, 40):
        line((x, -240), (x, 880), 2)
    for y in range(-220, 881, 40):
        line((-240, y), (1040, y), 2)
    rect((180, 140), (620, 500), 30)
    cells, base, dark, moved = [], [], [], []
    for j, y in enumerate(range(-200, 861, 40)):
        for i, x in enumerate(range(-200, 1041, 40)):
            centre = np.array([x, y], float)
            if shift and x >= 660:
                centre += [20, 0]
            central = 200 <= x <= 600 and 160 <= y <= 480
            colour = 220 if central else 30
            if (i+j) % 2 == 0:
                half = 12 if central else 17
                rect(centre-half, centre+half, colour)
                if not central:
                    # Join only the corners, leaving four measurable straight mid-edges.
                    for dx, dy in ((-1, -1), (1, -1), (1, 1), (-1, 1)):
                        line(centre+[dx*17, dy*17], centre+[dx*20, dy*20], 3)
                cells.append((i, j))
                base.append(centre)
                dark.append(not central)
                moved.append(shift and x >= 660)
            else:
                rect(centre-16, centre+16, colour, 2)
                rect(centre+[-10, -10], centre+[-3, -3], colour)
                rect(centre+[3, -10], centre+[10, -3], colour)
                rect(centre+[-5, 3], centre+[7, 10], colour)
    image = cv2.resize(image, (size[0]+2*margin, size[1]+2*margin),
                       interpolation=cv2.INTER_AREA)
    matrix = np.array([[650., 0, 400.], [0, 650., 320.], [0, 0, 1.]])
    distortion = np.array([-.22, .04, 0., 0., 0.])
    grid = np.stack(np.meshgrid(np.arange(size[0]), np.arange(size[1])), axis=-1).astype(np.float32)
    source = cv2.undistortPoints(grid.reshape(-1, 1, 2), matrix, distortion,
                                P=matrix).reshape(size[1], size[0], 2) + margin
    image = cv2.remap(image, source[:, :, 0], source[:, :, 1], cv2.INTER_LINEAR,
                      borderValue=220)
    image = cv2.GaussianBlur(image, (5, 5), .6)
    objects = np.column_stack(((np.asarray(base)-[400, 320])/650, np.ones(len(base))))
    expected = cv2.projectPoints(objects, np.zeros(3), np.zeros(3), matrix, distortion)[0][:, 0]
    return image, expected, np.asarray(cells), np.asarray(dark), np.asarray(moved)


class EncoderGrowthTests(unittest.TestCase):
    def test_seed_radius_clip_keeps_only_anchor_connected_patch(self):
        # The outer path connects a legitimate remote square before radius clipping,
        # but clipping removes its bridges. It must wait for prediction/refinement.
        core = [(x, y) for x in range(5) for y in range(5)] + [(-1, 0), (0, -1), (-1, 3)]
        cells = np.array(core + [(-6, 0), (-6, 3), (-5, 4), (-3, 6)])
        centres = 300 + 25*cells
        quads = centres[:, None] + np.array([[-7, -7], [7, -7], [7, 7], [-7, 7]])
        image = np.full((600, 600), 30, np.uint8)
        for quad in quads:
            cv2.fillConvexPoly(image, quad.astype(np.int32), 220)

        def candidates(gray, edge_windows=None):
            edge_windows.extend(quads)
            return quads.astype(float)

        with patch.object(encoder, 'detect_squares', side_effect=candidates), \
                patch.object(encoder, 'refine_predicted_square', return_value=None):
            corners, indices = encoder.detect_lattice(image)
        actual = encoder.square_centres(corners)
        expected = 300 + 25*np.array(core)
        self.assertEqual(len(actual), len(core))
        distances = np.linalg.norm(actual[:, None]-expected[None, :], axis=2)
        nearest = distances.argmin(axis=1)
        np.testing.assert_allclose(distances.min(axis=1), 0, atol=1e-9)
        self.assertEqual(len(np.unique(nearest)), len(core))
        np.testing.assert_array_equal(
            np.sum((indices[:, None]-indices[None, :])**2, axis=2),
            np.sum((np.array(core)[nearest, None]-np.array(core)[nearest][None, :])**2, axis=2))

    def test_supported_anchor_keeps_its_sparser_neighbours(self):
        image = np.full((400, 480), 30, np.uint8)
        truth = np.array([(120+40*x, 120+40*y) for y in range(3) for x in range(4)])
        for x, y in truth:
            cv2.rectangle(image, (x-10, y-10), (x+10, y+10), 220, -1)
        image = cv2.GaussianBlur(image, (5, 5), .6)
        corners, indices = encoder.detect_lattice(image)
        centres = encoder.square_centres(corners)
        self.assertEqual(len(centres), 12)
        matched = np.linalg.norm(centres[:, None]-truth[None, :], axis=2).argmin(axis=1)
        np.testing.assert_array_equal(
            np.sum((indices[:, None]-indices[None, :])**2, axis=2),
            np.sum((truth[matched, None]-truth[matched][None, :])**2, axis=2)/(40*40))

    def test_remote_plain_square_does_not_displace_dense_seed(self):
        image = np.full((420, 580), 30, np.uint8)
        for x, y in [(65, 72), *((260 + 32*i, 115 + 32*j) for i in range(7) for j in range(7))]:
            cv2.rectangle(image, (x-9, y-9), (x+9, y+9), 220, -1)
        image = cv2.GaussianBlur(image, (5, 5), .6)
        corners, indices = encoder.detect_lattice(image)
        centres = encoder.square_centres(corners)
        self.assertEqual(len(centres), 49)
        self.assertEqual(len(np.unique(indices, axis=0)), 49)
        self.assertTrue(np.all(centres[:, 0] >= 259))

    def test_refinement_uses_local_scale_when_perspective_differs_from_seed(self):
        image = np.full((128, 128), 220, np.float32)
        cv2.rectangle(image, (51, 51), (77, 77), 30, -1)
        levels = cv2.GaussianBlur(image, (0, 0), .6)
        # Local plain-lattice pitch is 37.48 px; the seed elsewhere was only 28 px.
        homography = np.array([[26.5, -26.5, 64.], [26.5, 26.5, 64.], [0., 0., 1.]])
        fitted = encoder.refine_predicted_square((levels, 255-levels), homography, np.array([0, 0]), 28.)
        self.assertIsNotNone(fitted, 'Valid 27 px dark square rejected by remote seed scale')
        np.testing.assert_allclose(encoder.square_centres([fitted]), [[64., 64.]], atol=.1)

    def assert_topology(self, indices, truth):
        np.testing.assert_array_equal(
            np.sum((indices[:, None]-indices[None, :])**2, axis=2),
            np.sum((truth[:, None]-truth[None, :])**2, axis=2)/2)

    def test_grows_connected_dark_network_through_barrel_distortion_to_corners(self):
        image, expected, cells, dark, _ = rendered_network()
        # The old contour detector sees only the 49 bright squares in the centre.
        corners, indices = encoder.detect_lattice(image)
        centres = encoder.square_centres(corners)
        distances = np.linalg.norm(centres[:, None]-expected[None, :], axis=2)
        nearest = distances.argmin(axis=1)
        # Curved, connected transition edges give up to 1.081 px with known outlines.
        self.assertTrue(np.all(distances.min(axis=1) < 1.25), 'Coded cell or false square accepted')
        self.assertEqual(len(np.unique(nearest)), len(nearest), 'Duplicate accepted cell')
        visible = ((expected > 25) & (expected < np.array(image.shape[::-1])-25)).all(axis=1)
        for polarity in (False, True):
            labels = set(np.flatnonzero(visible & (dark == polarity)))
            self.assertGreaterEqual(len(labels.intersection(nearest)), int(np.ceil(.9*len(labels))))
        self.assertEqual(encoder.image_coverage(centres, image.shape[::-1])['empty_corners'], [])
        self.assert_topology(np.asarray(indices), cells[nearest])

    def test_does_not_reindex_a_half_cell_shifted_region(self):
        image, expected, cells, _, moved = rendered_network(shift=True)
        corners, indices = encoder.detect_lattice(image)
        centres = encoder.square_centres(corners)
        distances = np.linalg.norm(centres[:, None]-expected[None, :], axis=2)
        nearest = distances.argmin(axis=1)
        self.assertGreaterEqual(len(centres), 40)
        self.assertTrue(np.all(distances.min(axis=1) < 1.25), 'Accepted a non-plain position')
        self.assertFalse(np.any(moved[nearest]), 'Disagreeing region was silently re-indexed')
        self.assert_topology(np.asarray(indices), cells[nearest])


if __name__ == '__main__':
    unittest.main()
