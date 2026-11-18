"""Offline checks for the encoder target; no camera or robot required."""
import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import cv2
import numpy as np

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'src/anodizing-modules/vendor/ur10e-handeye/src'))
from tools import encoder_intrinsics as encoder
from ur_handeye.intrinsics import load_intrinsics


class EncoderTargetTests(unittest.TestCase):
    def fit_session(self, root, device, *, duplicate=False):
        train, check = root / 'train', root / 'check'
        train.mkdir()
        check.mkdir()
        (root / 'capture-profile.json').write_text(json.dumps({'device': device}))
        views = []
        for directory, count in ((train, 12), (check, 5)):
            split = []
            for index in range(count):
                path = directory / f'{index}.png'
                contents = f'{directory.name}-{index}'
                if duplicate and directory == check and index == 0:
                    contents = 'train-0'
                path.write_bytes(contents.encode())
                split.append((path, None, None))
            views.append(split)
        self.output = root / 'intrinsics.json'
        args = ['encoder_intrinsics.py', '--images', str(train), '--check', str(check),
                '--pitch-mm', '1.7', '--serial', '22222222', '--output', str(self.output)]
        fit = dict(camera_serial='22222222', fx=1., fy=1., ppx=1., ppy=1.,
                   estimation={'reprojection_rmse_px': 0., 'warnings': []})
        fit['estimation']['comparison'] = json.loads(json.dumps(fit))
        with patch.object(sys, 'argv', args), \
             patch.object(encoder, 'observations', side_effect=views), \
             patch.object(encoder, 'fit_calibration', return_value=fit), \
             contextlib.redirect_stdout(io.StringIO()):
            return encoder.main()

    def test_fit_rejects_a_serial_that_disagrees_with_capture(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(ValueError, r'serial.*11111111.*22222222'):
                self.fit_session(Path(tmp), {'serial_number': '11111111'})
            self.assertFalse(self.output.exists())

    def test_fit_records_when_capture_has_no_serial(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(self.fit_session(Path(tmp), {}), 0)
            fit = json.loads(self.output.read_text())
            for model in (fit, fit['estimation']['comparison']):
                self.assertEqual(model['camera_serial'], '22222222')
                self.assertIs(model['estimation']['camera_serial_recorded_in_capture'], False)

    def test_fit_rejects_byte_identical_training_and_check_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(ValueError, r'[Tt]raining.*check.*same content'):
                self.fit_session(Path(tmp), {'serial_number': '22222222'}, duplicate=True)
            self.assertFalse(self.output.exists())

    def test_serial_is_required_before_reading_images(self):
        with tempfile.TemporaryDirectory() as tmp:
            args = ['encoder_intrinsics.py', '--images', tmp, '--check', tmp,
                    '--pitch-mm', '1.7', '--output', str(Path(tmp) / 'intrinsics.json')]
            stderr = io.StringIO()
            with patch.object(sys, 'argv', args), \
                 patch.object(encoder, 'observations', side_effect=AssertionError('Images read without --serial')) as observations, \
                 contextlib.redirect_stderr(stderr), self.assertRaises(SystemExit) as error:
                encoder.main()
            self.assertEqual(error.exception.code, 2)
            self.assertIn('--serial', stderr.getvalue())
            observations.assert_not_called()
            self.assertEqual(list(Path(tmp).iterdir()), [])

    def test_diagonal_intersection_is_projective_centre(self):
        corners = np.array([[[0, 0], [10, 0], [8, 6], [2, 6]]], dtype=np.float32)
        np.testing.assert_allclose(encoder.square_centres(corners), [[5, 3.75]], atol=1e-5)

    def test_detection_finds_blurred_alternating_plain_squares(self):
        # Alternate cells contain plain squares; the other cells contain small code-like marks.
        image = np.full((480, 640), 30, np.uint8)
        expected = []
        for j in range(9):
            for i in range(12):
                x, y = 40 + 45*i, 35 + 45*j
                if (i+j) % 2 == 0:
                    cv2.rectangle(image, (x-14, y-14), (x+14, y+14), 220, -1)
                    expected.append((x, y))
                else:
                    cv2.rectangle(image, (x-6, y-6), (x+6, y+6), 160, 2)
        image = cv2.GaussianBlur(image, (5, 5), 1.0)
        noise = np.random.default_rng(37).normal(0, 1.0, image.shape)
        image = np.clip(image.astype(float) + noise, 0, 255).astype(np.uint8)
        centres = encoder.square_centres(encoder.detect_squares(image))
        self.assertEqual(len(centres), len(expected))
        distance = np.linalg.norm(centres[:, None, :] - np.asarray(expected)[None, :, :], axis=2)
        self.assertLess(distance.min(axis=0).max(), 0.5)

    def test_mixed_polarity_plain_squares_share_lattice(self):
        image = np.full((480, 760), 30, np.uint8)
        image[:, 358:] = 220
        expected, cells = [], []
        for j in range(9):
            for i in range(14):
                x, y = 65 + 45*i, 55 + 45*j
                dark = x >= 358
                if (i+j) % 2 == 0:
                    width = 18 if dark else 14
                    cv2.rectangle(image, (x-width, y-width), (x+width, y+width), 30 if dark else 220, -1)
                    expected.append((x, y))
                    cells.append((i, j))
                else:
                    cv2.rectangle(image, (x-7, y-7), (x+7, y+7), 60 if dark else 160, 2)
        image = cv2.GaussianBlur(image, (5, 5), 1)
        centres = encoder.square_centres(encoder.detect_squares(image))
        distances = np.linalg.norm(centres[:, None] - np.asarray(expected)[None, :], axis=2)
        self.assertEqual(len(centres), len(expected))
        self.assertLess(distances.min(axis=0).max(), .5)
        matched = np.asarray(cells)[distances.argmin(axis=1)]
        indices = np.asarray(encoder.index_lattice(centres, image.shape[::-1]))
        np.testing.assert_array_equal(np.sum((indices[:, None] - indices[None, :]) ** 2, axis=2),
                                      np.sum((matched[:, None] - matched[None, :]) ** 2, axis=2) / 2)

    def test_sharp_real_crop_does_not_detect_coded_cell(self):
        image = cv2.imread(str(ROOT / 'tools/tests/fixtures/encoder_code_crop.png'), cv2.IMREAD_GRAYSCALE)
        centres = encoder.square_centres(encoder.detect_squares(image))
        # Marked code cell in original frame at (2266, 66), crop origin (2191, 0).
        self.assertTrue(not len(centres) or np.linalg.norm(centres - [75, 66], axis=1).min() > 8)

    def test_detection_at_target_widths_and_rotations(self):
        for width, angle in [(10, 0), (20, 45), (40, 90)]:
            with self.subTest(width=width, angle=angle):
                image = np.full((600, 600), 30, np.uint8)
                expected = []
                pitch = width * 2
                for j in range(-3, 4):
                    for i in range(-3, 4):
                        x, y = 300 + i*pitch, 300 + j*pitch
                        if (i+j) % 2 == 0:
                            cv2.rectangle(image, (x-width//2, y-width//2),
                                          (x+width//2-1, y+width//2-1), 220, -1)
                            expected.append((x-.5, y-.5))
                transform = cv2.getRotationMatrix2D((300, 300), angle, 1)
                image = cv2.warpAffine(image, transform, (600, 600), borderValue=30)
                image = cv2.GaussianBlur(image, (5, 5), .8)
                rng = np.random.default_rng(123)
                image = np.clip(image.astype(float) + rng.normal(0, 1, image.shape), 0, 255).astype(np.uint8)
                expected = cv2.transform(np.array([expected], np.float32), transform)[0]
                centres = encoder.square_centres(encoder.detect_squares(image))
                self.assertEqual(len(centres), len(expected))
                distances = np.linalg.norm(centres[:, None] - expected[None, :], axis=2)
                self.assertLess(distances.min(axis=0).max(), .5)

    def test_index_rotated_lattice_with_connected_holes(self):
        cells = np.array([(i, j) for j in range(8) for i in range(10)
                          if (i+j) % 2 == 0 and (i, j) not in {(4, 4), (5, 5), (0, 0)}])
        angle = np.deg2rad(61)
        rotation = np.array([[np.cos(angle), -np.sin(angle)], [np.sin(angle), np.cos(angle)]])
        points = cells @ rotation.T * 30 + [360, 210]
        order = np.random.default_rng(4).permutation(len(points))
        indices = np.asarray(encoder.index_lattice(points[order], (800, 600)))
        self.assertEqual(indices.shape, (len(points), 2))
        self.assertTrue(np.issubdtype(indices.dtype, np.integer))
        actual = np.sum((indices[:, None] - indices[None, :]) ** 2, axis=2)
        truth = np.sum((cells[order, None] - cells[order][None, :]) ** 2, axis=2) / 2
        np.testing.assert_array_equal(actual, truth)

    def test_indexes_dense_full_sensor_lattice(self):
        points = np.array([(20.0*x + 200, 20.0*y + 300)
                           for y in range(60) for x in range(100)])
        indices = encoder.index_lattice(points, (2448, 2048))
        self.assertEqual(np.asarray(indices).shape, (6000, 2))
        self.assertEqual(len(np.unique(indices, axis=0)), 6000)

    def test_index_bridges_a_dark_band(self):
        cells = np.array([(x, y) for y in range(8) for x in range(16) if x not in (7, 8)])
        points = cells * 20.0 + 100
        indices = np.asarray(encoder.index_lattice(points, (600, 400)))
        np.testing.assert_array_equal(np.sum((indices[:, None] - indices[None, :])**2, axis=2),
                                      np.sum((cells[:, None] - cells[None, :])**2, axis=2))

    def test_index_rejects_shifted_region_across_dark_band(self):
        points = np.array([(20.0*x + (10 if x > 8 else 0), 20.0*y)
                           for y in range(8) for x in range(16) if x not in (7, 8)]) + 100
        with self.assertRaises(ValueError):
            encoder.index_lattice(points, (600, 400))

    def test_real_dark_crop_plain_squares_and_indexing(self):
        fixture = ROOT / 'tools/tests/fixtures'
        image = cv2.imread(str(fixture / 'encoder_diagnostic_crop.png'), cv2.IMREAD_GRAYSCALE)
        labels = json.loads((fixture / 'encoder_diagnostic_crop_labels.json').read_text())['points']
        expected = np.array([p['centre_px'] for p in labels])
        cells = np.array([p['cell'] for p in labels])
        centres = encoder.square_centres(encoder.detect_squares(image))
        distances = np.linalg.norm(centres[:, None] - expected[None, :], axis=2)
        nearest = distances.argmin(axis=1)
        self.assertTrue(np.all(distances.min(axis=1) < 8), 'False positive or unlabelled plain square')
        self.assertEqual(len(np.unique(nearest)), len(nearest), 'Duplicate detections')
        bright = {i for i, p in enumerate(labels) if p['polarity'] == 'bright'}
        self.assertGreaterEqual(len(bright.intersection(nearest)), int(np.ceil(.9 * len(bright))))
        indices = np.asarray(encoder.index_lattice(centres, image.shape[::-1]))
        truth = cells[nearest]
        np.testing.assert_array_equal(np.sum((indices[:, None] - indices[None, :])**2, axis=2),
                                      np.sum((truth[:, None] - truth[None, :])**2, axis=2) / 2)

    def test_real_boundary_crop_both_polarities_keep_phase(self):
        fixture = ROOT / 'tools/tests/fixtures'
        image = cv2.imread(str(fixture / 'encoder_diagnostic_boundary.png'), cv2.IMREAD_GRAYSCALE)
        labels = json.loads((fixture / 'encoder_diagnostic_boundary_labels.json').read_text())['points']
        expected = np.array([p['centre_px'] for p in labels])
        cells = np.array([p['cell'] for p in labels])
        centres = encoder.square_centres(encoder.detect_squares(image))
        distances = np.linalg.norm(centres[:, None] - expected[None, :], axis=2)
        nearest = distances.argmin(axis=1)
        self.assertTrue(np.all(distances.min(axis=1) < 9), 'Coded cell or unlabelled detection')
        self.assertEqual(len(np.unique(nearest)), len(nearest))
        for polarity in ('bright', 'dark'):
            labelled = {i for i, p in enumerate(labels) if p['polarity'] == polarity}
            self.assertGreaterEqual(len(labelled.intersection(nearest)), int(np.ceil(.8 * len(labelled))))
        indices = np.asarray(encoder.index_lattice(centres, image.shape[::-1]))
        truth = cells[nearest]
        np.testing.assert_array_equal(np.sum((indices[:, None] - indices[None, :])**2, axis=2),
                                      np.sum((truth[:, None] - truth[None, :])**2, axis=2) / 2)

    def test_index_rejects_duplicate_or_broken_points(self):
        points = np.array([(x*30., y*30.) for y in range(6) for x in range(6)]) + 100
        for broken in (np.vstack((points, points[0])), points + np.eye(len(points), 2)*8):
            with self.subTest(broken=broken[-1]), self.assertRaises(ValueError):
                encoder.index_lattice(broken, (400, 400))

    def test_rendered_squares_recover_intrinsics(self):
        size = (640, 512)
        matrix = np.array([[800., 0, 318.], [0, 795., 258.], [0, 0, 1.]])
        distortion = np.array([-.08, .025, .0005, -.0003, 0.])
        pitch = .0017
        cells = np.array([(i, j) for j in range(-35, 36) for i in range(-35, 36)
                          if (i+j) % 2 == 0], np.float32)
        centres3 = np.column_stack((cells*pitch, np.zeros(len(cells)))).astype(np.float32)
        offsets = np.array([[-.0006, -.0006, 0], [.0006, -.0006, 0],
                            [.0006, .0006, 0], [-.0006, .0006, 0]], np.float32)
        fractions = np.arange(16, dtype=np.float32)[:, None] / 16
        edge_offsets = np.concatenate([start + fractions * (end - start)
                                       for start, end in zip(offsets, np.roll(offsets, -1, axis=0))])
        polygons3 = (centres3[:, None, :] + edge_offsets).reshape(-1, 3)
        rng = np.random.default_rng(81)
        views = []
        for n in range(25):
            rotation = np.array([.18*np.sin(n*1.7), .18*np.cos(n*1.1), n*np.pi/48])
            translation = np.array([.008*np.sin(n), .008*np.cos(n*.7), .026 + .009*(n % 8)])
            corners, _ = cv2.projectPoints(polygons3, rotation, translation, matrix, distortion)
            corners = corners.reshape(-1, len(edge_offsets), 2)
            inside = ((corners[:, :, 0].min(axis=1) > 8) & (corners[:, :, 0].max(axis=1) < size[0]-8) &
                      (corners[:, :, 1].min(axis=1) > 8) & (corners[:, :, 1].max(axis=1) < size[1]-8))
            image = np.full((size[1]*8, size[0]*8), 30, np.uint8)
            for poly in corners[inside]:
                cv2.fillPoly(image, [np.round(poly*8 + 3.5).astype(np.int32)], 220)
            image = cv2.resize(image, size, interpolation=cv2.INTER_AREA)
            image = cv2.GaussianBlur(image, (5, 5), .8)
            image = np.clip(image.astype(float) + rng.normal(0, 1, image.shape), 0, 255).astype(np.uint8)
            corners, indices = encoder.detect_lattice(image)
            found = encoder.square_centres(corners)
            objects = np.column_stack((indices * (pitch * np.sqrt(2)), np.zeros(len(indices)))).astype(np.float32)
            views.append((Path(f'rendered-{n}.png'), objects, np.asarray(found, np.float32).reshape(-1, 1, 2)))
        result = encoder.fit_calibration(views, [], size, 'rendered', pitch)
        for key, expected in [('fx', 800.), ('fy', 795.), ('ppx', 318.), ('ppy', 258.)]:
            self.assertLess(abs(result[key] - expected) / expected, .002, key)
        self.assertLess(result['estimation']['reprojection_rmse_px'], .1)

    def test_fit_accepts_zero_principal_point(self):
        objects = np.array([(x*.01, y*.01, 0) for y in range(4) for x in range(4)], np.float32)
        matrix = np.array([[100., 0, 0], [0, 100., 0], [0, 0, 1.]])
        rotation, translation = np.zeros(3), np.array([0., 0., 1.])
        pixels, _ = cv2.projectPoints(objects, rotation, translation, matrix, np.zeros(5))
        views = [(Path(f'zero-{n}.png'), objects, pixels) for n in range(12)]
        fake = (0., matrix, np.zeros((1, 5)), [rotation]*12, [translation]*12)
        with patch.object(encoder.cv2, 'calibrateCamera', return_value=fake):
            result = encoder.fit_calibration(views, [], (640, 480), 'synthetic', .01)
        self.assertEqual(result['ppx'], 0)

    def test_corner_warnings_cover_individual_training_and_check_images(self):
        objects = np.array([(x*.01, y*.01, 0) for y in range(4) for x in range(4)], np.float32)
        matrix = np.array([[100., 0, 0], [0, 100., 0], [0, 0, 1.]])
        rotation, translation = np.zeros(3), np.array([0., 0., 1.])
        corners = [(1, 1), (561, 1), (1, 421), (561, 421)]
        def view(name, corner):
            pixels = np.array([[[corner[0] + x, corner[1] + y]]
                               for y in range(4) for x in range(4)], np.float32)
            return Path(name), objects, pixels
        training = [view(f'train-{n}.png', corners[n % 4]) for n in range(12)]
        checks = [view('check.png', corners[0])]
        fake = (0., matrix, np.zeros((1, 5)), [rotation]*12, [translation]*12)
        with patch.object(encoder.cv2, 'calibrateCamera', return_value=fake), \
             patch.object(encoder.cv2, 'solvePnP', return_value=(True, rotation, translation)):
            estimation = encoder.fit_calibration(training, checks, (640, 480), 'synthetic', .01)['estimation']
        self.assertEqual(estimation['dataset_coverage']['training']['empty_corners'], [])
        warnings = ' '.join(estimation['warnings']).lower()
        self.assertIn('corner', warnings)
        self.assertIn('training', warnings)
        self.assertIn('check', warnings)

    def test_calibration_recovers_known_intrinsics_and_loads(self):
        size = (1280, 1024)
        matrix = np.array([[1600., 0, 638.], [0, 1590., 514.], [0, 0, 1.]])
        distortion = np.array([-0.08, 0.025, 0.0005, -0.0003, 0.0])
        cells = np.array([(i, j) for j in range(-12, 13) for i in range(-15, 16)
                          if (i+j) % 2 == 0], np.float32)
        objects = np.column_stack((cells * .0017, np.zeros(len(cells)))).astype(np.float32)
        views = []
        for n in range(30):
            rotation = np.array([.18*np.sin(n*1.7), .18*np.cos(n*1.1), n*np.pi/48])
            translation = np.array([.004*np.sin(n), .003*np.cos(n*.7), .11 + .001*(n % 5)])
            projected, _ = cv2.projectPoints(objects, rotation, translation, matrix, distortion)
            visible = ((projected[:, 0, 0] > 15) & (projected[:, 0, 0] < size[0]-15) &
                       (projected[:, 0, 1] > 15) & (projected[:, 0, 1] < size[1]-15))
            views.append((Path(f'view-{n}.png'), objects[visible], projected[visible].astype(np.float32)))
        training, checks = views[:25], views[25:]
        result = encoder.fit_calibration(training, checks, size, 'synthetic', .0017)
        without_checks = encoder.fit_calibration(training, [], size, 'synthetic', .0017)
        for key in ('fx', 'fy', 'ppx', 'ppy'):
            self.assertEqual(result[key], without_checks[key])
        def check_coverage(coverage, pixels):
            bins = {(min(int(x * 8 / size[0]), 7), min(int(y * 8 / size[1]), 7))
                    for x, y in np.asarray(pixels).reshape(-1, 2)}
            corners = [('top_left', (0, 0)), ('top_right', (7, 0)),
                       ('bottom_left', (0, 7)), ('bottom_right', (7, 7))]
            self.assertEqual(coverage['cols'], 8)
            self.assertEqual(coverage['rows'], 8)
            self.assertEqual(coverage['occupied_cells'], len(bins))
            self.assertEqual(coverage['fraction'], len(bins) / 64)
            self.assertEqual(coverage['empty_corners'], [name for name, cell in corners if cell not in bins])

        for model in (result, result['estimation']['comparison']):
            estimation = model['estimation']
            self.assertEqual(len(estimation['fresh_checks']), 5)
            for item, view in zip(estimation['per_view'] + estimation['fresh_checks'], views):
                check_coverage(item['image_coverage'], view[2])
            for key, group in [('training', training), ('fresh_checks', checks)]:
                check_coverage(estimation['dataset_coverage'][key], np.concatenate([v[2] for v in group]))
            self.assertTrue(any('corner' in warning.lower() for warning in estimation['warnings']))
            self.assertIn('image_coverage_fraction', estimation)
            self.assertEqual(sum(c['count'] for c in estimation['residual_map']['cells']),
                             sum(len(v[1]) for v in training))
            self.assertEqual(sum(c['count'] for c in estimation['check_residual_map']['cells']),
                             sum(len(v[1]) for v in checks))
        with self.assertRaises(ValueError):
            encoder.fit_calibration(training, [training[0]], size, 'synthetic', .0017)
        for key, expected in [('fx', 1600.), ('fy', 1590.), ('ppx', 638.), ('ppy', 514.)]:
            self.assertLess(abs(result[key] - expected) / expected, .002, key)
        self.assertLess(result['estimation']['reprojection_rmse_px'], .1)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'intrinsics.json'
            path.write_text(json.dumps(result))
            loaded = load_intrinsics(path)
        self.assertEqual(loaded['camera_serial'], 'synthetic')
        self.assertAlmostEqual(loaded['fx'], result['fx'])


if __name__ == '__main__':
    unittest.main()
