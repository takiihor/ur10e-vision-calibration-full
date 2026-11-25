"""Site defaults are discovered or supplied; none are compiled into the service."""
import json
import math
from pathlib import Path
from types import SimpleNamespace
import tempfile
import threading
import unittest
from unittest import mock

from anodizing_web import robot_control, site_profile, teaching


class SiteProfileTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def owner(self):
        return SimpleNamespace(root=self.root, lock=threading.RLock(),
            allow_manual_pick_place=True, robot_model={'sha256': 'fake'},
            active=None, mcu_pending=None, execution_uncertain=False,
            storage_error=None, closing=False)

    def test_no_built_in_robot_or_site_defaults(self):
        defaults = robot_control.capability(self.owner())['defaults']
        self.assertEqual(defaults['robot_ip'], '')
        self.assertIsNone(defaults['tcp'])
        self.assertEqual(defaults['collision_scene'], '')
        self.assertIsNone(defaults['joint_bounds_rad'])
        self.assertIsNone(defaults['workspace_min_m'])
        self.assertNotIn('tank01', json.dumps(defaults))

    def test_nonfinite_profile_numbers_are_rejected(self):
        for value in (float('nan'), float('inf'), -float('inf'), True, 10**1000):
            with self.subTest(value=str(value)), self.assertRaises(ValueError):
                site_profile.finite_numbers([value, 1], 2)

    def test_nonfinite_bounds_do_not_reach_capability_json(self):
        for value in (float('nan'), float('inf'), -float('inf')):
            with self.subTest(value=value):
                profile = {'robot_control': {
                    'workspace_min_m': [value, -1, 0], 'workspace_max_m': [1, 1, 1],
                    'joint_bounds_rad': [[value, 1]] * 6,
                    'joint_bounds_source': 'fixture'}}
                (self.root / 'site-profile.json').write_text(json.dumps(profile))
                defaults = robot_control.capability(self.owner())['defaults']
                self.assertIsNone(defaults['workspace_min_m'])
                self.assertIsNone(defaults['joint_bounds_rad'])
                json.dumps(defaults, allow_nan=False)

    def test_available_calibrations_require_an_explicit_selection(self):
        for name in ('a-old', 'z-new'):
            path = self.root / 'calibrations' / name / 'calibration.json'
            path.parent.mkdir(parents=True)
            path.write_text(json.dumps({'robot': {'host': name}}))
        with mock.patch('ur_pick_place.vision.handeye.load_handeye',
                        return_value=SimpleNamespace(simulated=False, tcp_offset=[0, 0, .2, 0, 0, 0])):
            found = teaching.defaults(self.root)
        self.assertEqual(len(found['calibrations']), 2)
        self.assertEqual(found['settings']['calibration'], '')
        self.assertEqual(found['settings']['robot_ip'], '')
        self.assertEqual(found['settings']['tcp'], [0.0] * 6)

    def test_profile_prefills_reviewed_values_and_rejects_outside_paths(self):
        (self.root / 'collision').mkdir()
        (self.root / 'collision/scene.json').write_text('{}')
        profile = {'robot_control': {
            'robot_ip': 'controller.example', 'tcp': [0, 0, .1, 0, 0, 0],
            'collision_scene': 'collision/scene.json',
            'workspace_min_m': [-1, -1, .01], 'workspace_max_m': [1, 1, 1.2],
            'joint_bounds_rad': [[-math.pi, math.pi]] * 6,
            'joint_bounds_source': 'reviewed fixture', 'joint_speed_deg_s': 2}}
        (self.root / 'site-profile.json').write_text(json.dumps(profile))
        defaults = robot_control.capability(self.owner())['defaults']
        self.assertEqual(defaults['robot_ip'], 'controller.example')
        self.assertEqual(defaults['tcp'], [0, 0, .1, 0, 0, 0])
        self.assertEqual(defaults['collision_scene'], str(self.root / 'collision/scene.json'))
        self.assertEqual(defaults['joint_bounds_source'], 'reviewed fixture')
        self.assertEqual(defaults['joint_speed_deg_s'], 2)
        profile['robot_control']['collision_scene'] = '../outside.json'
        (self.root / 'site-profile.json').write_text(json.dumps(profile))
        defaults = robot_control.capability(self.owner())['defaults']
        self.assertEqual(defaults['collision_scene'], '')

    def test_teaching_defaults_discover_boards_and_use_a_new_project_path(self):
        found = teaching.defaults(self.root)
        self.assertEqual(found['picking_boards'], [])
        self.assertEqual(found['settings']['target']['board'], '')
        self.assertEqual(found['settings']['project'].split('/')[0], 'teaching')
        self.assertNotEqual(found['settings']['project'], 'teaching/site-project-example')
        board = Path(__file__).parents[2] / 'data/boards/stage2_charuco_realsense_80mm/board_manifest.json'
        (self.root / 'boards/example').mkdir(parents=True)
        (self.root / 'boards/example/board_manifest.json').write_bytes(board.read_bytes())
        found = teaching.defaults(self.root)
        self.assertTrue(found['picking_boards'])
        self.assertIn(found['settings']['target']['board'],
                      [item['target']['board'] for item in found['picking_boards']])


if __name__ == '__main__':
    unittest.main()
