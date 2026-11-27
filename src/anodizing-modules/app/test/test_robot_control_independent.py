"""Real owner and fake native device; no physical robot validation."""
import importlib.util
import json
import time
import unittest
from unittest import mock
import test_manual_execution

class RobotControlIndependent(unittest.TestCase):
    def test_step_requires_exact_plan_approval_before_one_dispatch(self):
        h=test_manual_execution.ManualWorkerTests();owner=h.public_server();self.addCleanup(h.doCleanups)
        body={'request_id':'robot-step-once','action':'STEP','agv_off':True,'axis':'x','increment':5,
              'robot_ip':'fake','tcp':h.tcp,'tcp_confirmed':True,'operator_path_approved':True,
              'collision_scene':'reviewed-scene.json','workspace_min_m':[-.82,-.84,.025],
              'workspace_max_m':[.82,.84,1.22],'joint_bounds_rad':[[-6.28,6.28] for _ in range(6)],
              'joint_bounds_source':'offline fixture limits','joint_speed_deg_s':3}
        if importlib.util.find_spec('anodizing_web.robot_control'):
            patch=mock.patch('anodizing_web.robot_control.native_backend',return_value=(h.robot,h.robot));patch.start();self.addCleanup(patch.stop)
        status,raw=h.request('POST','/api/robot-control',body)
        self.assertEqual(status,202,raw)
        deadline=time.monotonic()+5
        while time.monotonic()<deadline:
            worker=owner.runner
            if worker:
                state=worker.state()
                if state.get('manual',{}).get('stage')=='awaiting_plan':break
                self.assertFalse(state.get('error'),state.get('error'))
            time.sleep(.01)
        else:self.fail('No plan approval boundary')
        self.assertEqual(h.robot.moves,[])
        prompt=state['manual']['prompt']
        reply={'job_id':body['request_id'],'action':'confirm','prompt':prompt,'value':True}
        self.assertEqual(h.request('POST','/api/manual-control',reply)[0],200)
        self.assertEqual(h.request('POST','/api/manual-control',reply)[0],400)
        deadline=time.monotonic()+5
        while time.monotonic()<deadline and owner.active is not None:time.sleep(.01)
        self.assertIsNone(owner.active)
        self.assertEqual(len(h.robot.moves),1)
        self.assertEqual(h.robot.moves[0][0],'moveJ')
        self.assertAlmostEqual(h.robot.moves[0][1][0],h.start[0]+.005)
        self.assertEqual(h.request('POST','/api/robot-control',body)[0],202)
        self.assertEqual(len(h.robot.moves),1)

    def test_base_xyz_and_local_rotation_use_correct_frames(self):
        import math
        import numpy as np
        from anodizing_web.robot_control import step_target
        from ur_pick_place.vision.transforms import pose_to_matrix
        pose=[.31,-.14,.55,.45,-.27,.32]
        start=pose_to_matrix(pose)
        for axis,i in [('x',0),('y',1),('z',2)]:
            target=step_target(pose,axis,5)
            expected=start.copy();expected[i,3]+=.005
            np.testing.assert_allclose(pose_to_matrix(target),expected,atol=1e-10)
        angle=math.radians(5)
        for axis,i in [('rx',0),('ry',1),('rz',2)]:
            rot=np.eye(4);j=(i+1)%3;k=(i+2)%3
            rot[j,j]=rot[k,k]=math.cos(angle);rot[j,k]=-math.sin(angle);rot[k,j]=math.sin(angle)
            target=step_target(pose,axis,5)
            np.testing.assert_allclose(pose_to_matrix(target),start@rot,atol=1e-10)
            wrong=list(pose);wrong[i+3]+=angle
            self.assertFalse(np.allclose(pose_to_matrix(target),pose_to_matrix(wrong),atol=1e-6))

    def make_step_fixture(self, request_id):
        h=test_manual_execution.ManualWorkerTests();owner=h.public_server();self.addCleanup(h.doCleanups)
        patch=mock.patch('anodizing_web.robot_control.native_backend',return_value=(h.robot,h.robot));patch.start();self.addCleanup(patch.stop)
        h.public_body={'request_id':request_id,'action':'STEP','agv_off':True,'axis':'x','increment':5,'robot_ip':'fake','tcp':h.tcp,
            'collision_scene':'reviewed-scene.json','workspace_min_m':[-.82,-.84,.025],'workspace_max_m':[.82,.84,1.22],
            'joint_bounds_rad':[[-6.28,6.28] for _ in range(6)],'joint_bounds_source':'fixture','joint_speed_deg_s':3}
        return h,owner

    def wait_prompt(self,h,owner):
        deadline=time.monotonic()+5
        while time.monotonic()<deadline:
            worker=owner.runner
            if worker:
                state=worker.state()
                if state.get('manual',{}).get('stage')=='awaiting_plan':return state['manual']['prompt']
                self.assertFalse(state.get('error'),state.get('error'))
            time.sleep(.01)
        self.fail('Step plan prompt missing')

    def test_invalid_step_values_rejected_without_hardware(self):
        from anodizing_web.robot_control import step_target
        for axis,value in [('q',1),('x',2),('rx',10),('x',True),('x',float('nan')),('z',0)]:
            with self.subTest(axis=axis,value=value),self.assertRaises(ValueError):step_target([0]*6,axis,value)

    def test_stop_at_plan_and_changed_duplicate_cannot_dispatch(self):
        h,owner=self.make_step_fixture('robot-step-stop')
        self.assertEqual(h.request('POST','/api/robot-control',h.public_body)[0],202)
        prompt=self.wait_prompt(h,owner)
        different=dict(h.public_body,increment=10)
        self.assertGreaterEqual(h.request('POST','/api/robot-control',different)[0],400)
        self.assertEqual(h.request('POST','/api/manual-control',{'job_id':h.public_body['request_id'],'action':'stop'})[0],200)
        self.assertGreaterEqual(h.request('POST','/api/manual-control',{'job_id':h.public_body['request_id'],'action':'confirm','prompt':prompt,'value':True})[0],400)
        deadline=time.monotonic()+5
        while time.monotonic()<deadline and owner.active is not None:time.sleep(.01)
        self.assertIsNone(owner.active);self.assertEqual(h.robot.moves,[])

    def check_preflight_refusal(self,mode):
        h,owner=self.make_step_fixture('robot-step-'+mode)
        if mode=='ik':h.robot.getInverseKinematicsHasSolution=lambda *args:False
        else:h.robot.tcp=[0,0,.2,0,0,0]
        self.assertEqual(h.request('POST','/api/robot-control',h.public_body)[0],202)
        job=h.wait_public(lambda j:j['state'] not in ('running','waiting'))
        self.assertEqual(job['state'],'failed');self.assertEqual(h.robot.moves,[])

    def test_ik_rejection_prevents_dispatch(self):self.check_preflight_refusal('ik')
    def test_tcp_mismatch_prevents_dispatch(self):self.check_preflight_refusal('tcp')

    def test_start_drift_after_approval_refuses_dispatch(self):
        h,owner=self.make_step_fixture('robot-step-drift')
        self.assertEqual(h.request('POST','/api/robot-control',h.public_body)[0],202)
        prompt=self.wait_prompt(h,owner)
        h.robot.q[0]+=.01
        self.assertEqual(h.request('POST','/api/manual-control',{'job_id':h.public_body['request_id'],'action':'confirm','prompt':prompt,'value':True})[0],200)
        job=h.wait_public(lambda j:j['state'] not in ('running','waiting'))
        self.assertEqual(job['state'],'failed');self.assertEqual(h.robot.moves,[])

    def test_feedback_drift_during_dispatch_certificate_prevents_move(self):
        from anodizing_web import teacher_motion
        h,owner=self.make_step_fixture('robot-step-fsync-drift')
        original=teacher_motion.atomic_json; changed=[]
        def persist(path,value):
            result=original(path,value)
            if value.get('outcome')=='dispatching' and not changed:
                changed.append(True);h.robot.q[0]+=.01
            return result
        with mock.patch.object(teacher_motion,'atomic_json',side_effect=persist):
            self.assertEqual(h.request('POST','/api/robot-control',h.public_body)[0],202)
            prompt=self.wait_prompt(h,owner)
            self.assertEqual(h.request('POST','/api/manual-control',{'job_id':h.public_body['request_id'],'action':'confirm','prompt':prompt,'value':True})[0],200)
            job=h.wait_public(lambda j:j['state'] not in ('running','waiting'))
        self.assertEqual(changed,[True]);self.assertEqual(job['state'],'failed');self.assertEqual(h.robot.moves,[])

    def test_stop_after_dispatch_reaches_native_and_stop_failure_fences(self):
        h,owner=self.make_step_fixture('robot-step-stop-fails');h.robot.duration=2;h.robot.stop_fails=True
        self.assertEqual(h.request('POST','/api/robot-control',h.public_body)[0],202)
        prompt=self.wait_prompt(h,owner)
        self.assertEqual(h.request('POST','/api/manual-control',{'job_id':h.public_body['request_id'],'action':'confirm','prompt':prompt,'value':True})[0],200)
        deadline=time.monotonic()+5
        while time.monotonic()<deadline and not h.robot.moves:time.sleep(.01)
        self.assertEqual(len(h.robot.moves),1)
        h.request('POST','/api/manual-control',{'job_id':h.public_body['request_id'],'action':'stop'})
        deadline=time.monotonic()+6
        while time.monotonic()<deadline and not owner.execution_uncertain:time.sleep(.01)
        self.assertIn('stopJ',h.robot.stops);self.assertTrue(owner.execution_uncertain)

    def test_process_death_after_dispatch_intent_fences_restart(self):
        import subprocess,sys,shutil
        from pathlib import Path
        from anodizing_web.jobs import JobOwner
        script='''import os,time
from test_robot_control_independent import RobotControlIndependent
case=RobotControlIndependent();h,owner=case.make_step_fixture('crash-step')
print(str(h.root),flush=True)
h.robot.dispatch_hook=lambda kind,target:os._exit(73)
assert h.request('POST','/api/robot-control',h.public_body)[0]==202
prompt=case.wait_prompt(h,owner)
assert h.request('POST','/api/manual-control',{'job_id':'crash-step','action':'confirm','prompt':prompt,'value':True})[0]==200
time.sleep(6)
os._exit(74)
'''
        import os
        env=dict(os.environ,PYTHONPATH=os.pathsep.join(
            [str(Path(__file__).parent),os.environ.get('PYTHONPATH','')]).rstrip(os.pathsep))
        result=subprocess.run([sys.executable,'-c',script],capture_output=True,text=True,timeout=15,env=env)
        self.assertEqual(result.returncode,73,result.stderr)
        root=Path(result.stdout.strip().splitlines()[0]);self.addCleanup(shutil.rmtree,root,True)
        recovered=JobOwner(root)
        self.assertTrue(recovered.execution_uncertain)
        paths=list((root/'.jobs').glob('*.manual-path.json'));self.assertTrue(paths)
        self.assertEqual(json.loads(paths[0].read_text())['outcome'],'dispatching')
        with self.assertRaisesRegex(ValueError,'(?i)uncertain'):
            recovered.submit('another-step','robot-control',{'action':'STEP'},lambda _:self.fail('must not replay'))

    def test_owner_active_storage_and_uncertainty_refuse_new_step(self):
        h,owner=self.make_step_fixture('robot-step-blocked')
        for key,value in [('active','other-fake-job'),('storage_error','fake-storage-error'),('execution_uncertain',True)]:
            with self.subTest(blocker=key):
                original=getattr(owner,key);setattr(owner,key,value)
                try:self.assertEqual(h.request('POST','/api/robot-control',h.public_body)[0],400)
                finally:setattr(owner,key,original)
                self.assertEqual(h.robot.moves,[])

    def test_workspace_and_operating_bounds_prevent_dispatch(self):
        h,owner=self.make_step_fixture('robot-step-bounds')
        h.public_body['workspace_max_m']=[.2,.9,1.2]
        self.assertEqual(h.request('POST','/api/robot-control',h.public_body)[0],202)
        job=h.wait_public(lambda j:j['state'] not in ('running','waiting'))
        self.assertEqual(job['state'],'failed');self.assertEqual(h.robot.moves,[])

    def test_blocked_joint_path_never_dispatches(self):
        h,owner=self.make_step_fixture('robot-step-blocked-path')
        h.scene.check_segment=lambda a,b:dict(status='collision',start_joints=list(a),target_joints=list(b),scene_sha256='scene',model_sha256='factory',pair=['arm','fixture'])
        self.assertEqual(h.request('POST','/api/robot-control',h.public_body)[0],202)
        prompt=self.wait_prompt(h,owner)
        self.assertEqual(h.request('POST','/api/manual-control',{'job_id':h.public_body['request_id'],'action':'confirm','prompt':prompt,'value':True})[0],200)
        job=h.wait_public(lambda j:j['state'] not in ('running','waiting'))
        self.assertEqual(job['state'],'failed');self.assertEqual(h.robot.moves,[])
