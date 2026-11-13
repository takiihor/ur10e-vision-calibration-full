"""Site defaults are discovered or supplied; none are compiled into the service."""
import json
import math
from pathlib import Path
from types import SimpleNamespace
import tempfile
import threading
import unittest

from anodizing_web import robot_control, teaching


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
        self.assertNotEqual(found['settings']['project'], 'teaching/tank01-new')
        board = Path(__file__).parents[2] / 'data/boards/stage2_charuco_realsense_80mm/board_manifest.json'
        (self.root / 'boards/example').mkdir(parents=True)
        (self.root / 'boards/example/board_manifest.json').write_bytes(board.read_bytes())
        found = teaching.defaults(self.root)
        self.assertTrue(found['picking_boards'])
        self.assertIn(found['settings']['target']['board'],
                      [item['target']['board'] for item in found['picking_boards']])


if __name__ == '__main__':
    unittest.main()
