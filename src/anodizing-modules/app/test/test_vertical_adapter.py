"""Offline checker and adapter integration using the bundled nominal scene."""
import json
import math
from pathlib import Path
import unittest
from ur_handeye import collision
from ur_handeye.collision import Scene, transform
from ur_pick_place.vision.transforms import matrix_to_pose
from anodizing_web.manual_execution import check_vertical, rebind_vertical

class VerticalAdapterTests(unittest.TestCase):
    def test_nominal_line_and_final_mutable_binding(self):
        # The bundled nominal example is the only scene shipped with the package.
        example=Path(collision.__file__).parent/'web/collision-scene.example.json'
        data=json.loads(example.read_text())
        data.update(confirmed=True,collision_scope='supervised_self')  # Offline fixture confirmation only.
        scene=Scene(data)
        q=[0.,-1.9,-1.6,-1.2,1.5708,0.]
        tcp=matrix_to_pose(transform({'xyz_m':[.035,-.02,.11],'rpy_rad':[.2,-.3,.1]}))
        from ur_pick_place.vision.transforms import pose_to_matrix
        start=matrix_to_pose(scene.flange(q)@pose_to_matrix(tcp))
        target=list(start);target[2]+=.100
        binding={'joint_bounds_rad':[[-2*math.pi,2*math.pi] for _ in range(6)],
            'joint_bounds_source':'explicit offline fixture bounds; not controller limits',
            'actual_start_tcp':start,'requested_target_tcp':target,'command':'moveL','unblended':True}
        low=[-2.,-2.,-2.];high=[2.,2.,2.]
        certificate=check_vertical(scene,q,tcp,target,low,high,binding)
        self.assertEqual(certificate['status'],'clear_in_model',certificate.get('reason'))
        self.assertAlmostEqual(certificate['line']['delta_z_m'],.100)
        self.assertGreater(len(certificate['balls']),1)
        self.assertTrue(rebind_vertical(scene,q,tcp,target,low,high,binding,certificate))
        scene.data['model_uncertainty_m']+=.001
        self.assertFalse(rebind_vertical(scene,q,tcp,target,low,high,binding,certificate))
