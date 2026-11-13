import json
import math
from pathlib import Path
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest import mock

from anodizing_web import auto_scene, calibration, preview_pose, scene_save
from ur_handeye import collision, primary


class PreviewBackendTests(unittest.TestCase):
    def state(self, timestamp, joints=None):
        return SimpleNamespace(timestamp_us=timestamp, received_at=time.monotonic(),
                               joints=joints if joints is not None else (0, .1, -.2, .3, -.4, .5))

    def test_pose_is_fresh_read_only_and_closed(self):
        monitor = mock.Mock()
        first, second = self.state(100), self.state(200)
        monitor.snapshot.side_effect = [first, second]
        with mock.patch.object(primary, 'PrimaryMonitor', return_value=monitor) as factory:
            result = preview_pose.read_pose('192.168.12.248')
        factory.assert_called_once_with('192.168.12.248')
        self.assertEqual(monitor.snapshot.call_args_list[1], mock.call(after=first.received_at, timeout=1))
        monitor.close.assert_called_once()
        self.assertEqual(result['joints_rad'], list(second.joints))
        self.assertEqual(result['source'], 'primary_read_only')
        self.assertEqual(result['robot_timestamp_s'], .0002)

    def test_bad_ip_never_connects(self):
        with mock.patch.object(primary, 'PrimaryMonitor') as factory:
            for host in ('', 'http://robot', '192.168.12.999', None):
                with self.assertRaises(ValueError):
                    preview_pose.read_pose(host)
        factory.assert_not_called()

    def test_bad_or_repeated_feedback_is_refused_and_closed(self):
        for stamp, joints in [(100, [0] * 6), (200, [math.nan] * 6), (200, [0] * 5)]:
            monitor = mock.Mock()
            monitor.snapshot.side_effect = [self.state(100), self.state(stamp, joints)]
            with mock.patch.object(primary, 'PrimaryMonitor', return_value=monitor):
                with self.assertRaises(ValueError):
                    preview_pose.read_pose('192.168.12.248')
            monitor.close.assert_called_once()

    def test_pose_opt_in_is_required_before_feedback(self):
        with mock.patch.object(preview_pose, 'read_pose') as reader:
            with self.assertRaises(PermissionError):
                calibration.run(None, None, {'action': 'pose', 'robot_ip': '192.168.12.248'}, 'test', False)
        reader.assert_not_called()

    def test_feedback_error_closes_monitor(self):
        monitor = mock.Mock()
        monitor.snapshot.side_effect = TimeoutError('No fresh state')
        with mock.patch.object(primary, 'PrimaryMonitor', return_value=monitor):
            with self.assertRaises(TimeoutError):
                preview_pose.read_pose('192.168.12.248')
        monitor.close.assert_called_once()

    def scene(self):
        return json.loads((Path(collision.__file__).parent / 'web/collision-scene.example.json').read_text())

    def test_draft_save_preserves_flags_and_never_overwrites(self):
        with tempfile.TemporaryDirectory() as temp:
            data = self.scene()
            result = scene_save.save_scene(Path(temp), 'scene-test-01', data)
            saved = Path(result['path'])
            self.assertEqual(json.loads(saved.read_text()), data)
            self.assertFalse(result['confirmed'])
            self.assertFalse(result['environment_complete'])
            self.assertEqual(result['obstacles_count'], 0)
            before = saved.read_bytes()
            with self.assertRaises(FileExistsError):
                scene_save.save_scene(Path(temp), 'scene-test-01', data)
            self.assertEqual(saved.read_bytes(), before)
            with self.assertRaises(collision.CollisionError):
                collision.load_scene(saved).require_motion_ready()

    def test_invalid_scene_or_identifier_creates_no_file(self):
        with tempfile.TemporaryDirectory() as temp:
            for identifier, data in [('../escape', self.scene()), ('test', None),
                                     ('test', dict(self.scene(), model_sha256='wrong'))]:
                with self.assertRaises(ValueError):
                    scene_save.save_scene(Path(temp), identifier, data)
            self.assertEqual(list(Path(temp).iterdir()), [])

    def test_changed_selected_model_is_refused(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            model = root / 'model.json'
            model.write_text('{}')
            with self.assertRaisesRegex(ValueError, 'model changed'):
                scene_save.save_scene(root, 'scene-test', self.scene(),
                                      {'source': str(model), 'sha256': 'wrong'})

    def test_saved_scene_read_preserves_geometry_and_flags(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            data = self.scene()
            result = scene_save.save_scene(root, 'scene-read-test', data)
            loaded = scene_save.read_scene(root, result['path'])
            self.assertEqual(loaded['scene'], data)
            self.assertEqual(loaded['model_sha256'], result['model_sha256'])
            self.assertFalse(loaded['confirmed'])

    def test_scene_read_refuses_outside_paths_and_other_json(self):
        with tempfile.TemporaryDirectory() as temp, tempfile.TemporaryDirectory() as other:
            root = Path(temp)
            outside = Path(other) / 'scene.json'
            outside.write_text(json.dumps(self.scene()))
            with self.assertRaises(ValueError):
                scene_save.read_scene(root, str(outside))
            invalid = root / 'not-scene.json'
            invalid.write_text('{}')
            with self.assertRaises(ValueError):
                scene_save.read_scene(root, str(invalid))

    def calibration_result(self):
        camera = [[0, -1, 0, .1], [1, 0, 0, .02], [0, 0, 1, .03], [0, 0, 0, 1]]
        return {'schema_version': 1, 'type': 'eye_in_hand', 'verdict': 'poor',
                'camera_in_flange': {'matrix': camera},
                'camera_in_tcp': {'matrix': camera, 'tcp_offset': [0] * 6},
                'board_in_base': {'matrix': [[1, 0, 0, 0], [0, 1, 0, 0],
                                             [0, 0, 1, 0], [0, 0, 0, 1]]},
                'intrinsics': {'width': 1280, 'height': 720, 'fx': 600, 'fy': 600,
                               'ppx': 640, 'ppy': 360, 'model': 'none', 'coeffs': [0] * 5},
                'camera': {'simulated': True, 'device': {'serial_number': 'test-camera'}}}

    def test_camera_visual_reads_calibration_without_hardware_or_motion_permission(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); path = root / 'calibration.json'
            data = self.calibration_result(); path.write_text(json.dumps(data))
            before = path.read_bytes()
            with mock.patch.object(primary, 'PrimaryMonitor') as factory:
                result = calibration.run(None, root, {'action': 'preview-calibration',
                                          'path': 'calibration.json'}, 'visual', False)
            factory.assert_not_called()
            self.assertEqual(result['camera_in_flange'], data['camera_in_flange']['matrix'])
            self.assertEqual(result['verdict'], 'poor')
            self.assertTrue(result['simulated'])
            self.assertEqual(result['camera_serial'], 'test-camera')
            self.assertEqual(path.read_bytes(), before)
            self.assertEqual(list(root.iterdir()), [path])

    def test_camera_visual_rejects_outside_and_invalid_calibration(self):
        with tempfile.TemporaryDirectory() as temp, tempfile.TemporaryDirectory() as other:
            root = Path(temp); path = Path(other) / 'calibration.json'
            path.write_text(json.dumps(self.calibration_result()))
            with self.assertRaises(ValueError):
                calibration.run(None, root, {'action': 'preview-calibration',
                                 'path': str(path)}, 'visual', False)
            invalid = root / 'calibration.json'
            for update in ({'camera_in_flange': {'matrix': [[0] * 4] * 4}},
                           {'type': 'eye_to_hand'}):
                invalid.write_text(json.dumps(dict(self.calibration_result(), **update)))
                with self.assertRaises(ValueError):
                    calibration.run(None, root, {'action': 'preview-calibration',
                                     'path': str(invalid)}, 'visual', False)

    def test_camera_visual_rejects_oversize_file(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); path = root / 'calibration.json'
            path.write_bytes(b' ' * 2_000_001)
            with self.assertRaisesRegex(ValueError, '2 MB'):
                calibration.run(None, root, {'action': 'preview-calibration',
                                 'path': str(path)}, 'visual', False)

    def test_supervised_scope_survives_save_and_plan_snapshot_without_claiming_cell_coverage(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); (root / '.jobs').mkdir()
            data = self.scene()
            data.update(collision_scope='supervised_self', confirmed=True)
            saved = scene_save.save_scene(root, 'supervised', data)
            prepared = auto_scene.prepare(root, 'supervised-plan', saved['path'])
            self.assertEqual(prepared['collision_scope'], 'supervised_self')
            snapshot = json.loads(Path(prepared['snapshot']).read_text())
            self.assertFalse(snapshot['environment_complete'])
            self.assertEqual(snapshot['obstacles'], [])
            self.assertEqual(snapshot['attached'], data['attached'])
            auto_scene.check(root, prepared)
            Path(saved['path']).write_text(json.dumps(dict(data, collision_scope='cell')))
            with self.assertRaisesRegex(ValueError, 'changed'):
                auto_scene.check(root, prepared)


if __name__ == '__main__':
    unittest.main()
