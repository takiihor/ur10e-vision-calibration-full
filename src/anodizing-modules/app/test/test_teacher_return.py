"""Checked return uses real owner/native worker with fake SDK and Scene."""
import copy
import json
import math
import time
import unittest
from unittest import mock

from test_teacher_joint import JointTeacherTests, JointTeacherIntegrationTests
from test_port import PortTests
from ur_pick_place.vision.teaching import digest
from ur_pick_place.vision.observe import Observation
from ur_pick_place.vision.transforms import pose_to_matrix


class ReturnTests(unittest.TestCase):
    setUp = JointTeacherTests.setUp
    wait_idle = JointTeacherTests.wait_idle
    public_server = JointTeacherIntegrationTests.public_server
    intent_hook = JointTeacherTests.intent_hook

    def prepare(self, status='clear_in_model'):
        JointTeacherTests.prepare(self,status)
        self.motion.close()
        self.robot.connected = True
        # A valid saved reference, independent of the validation marker/target.
        import numpy as np
        self.project.observation = Observation(role='pick',target=self.project.target.describe(),
            camera_target=np.eye(4),base_flange=np.eye(4),base_camera=np.eye(4),base_target=np.eye(4),
            robot={'joints_rad':list(self.target),'tcp_offset':self.project.settings['tcp']},
            quality={},refined=False,calibration_sha256=self.project.calibration.sha256)
        self.observation_path = self.project.directory/'observation.json'
        self.observation_path.write_text(json.dumps(self.project.observation.to_json()))
        self.observation_sha = digest(self.observation_path)
        self.public_server()
        self.body = dict(self.return_body,action='return_observation',project='project',
            request_id='checked-return-001',collision_scene='selected-scene.json')
        self.project.validation = None  # Return needs no successful marker validation.

    def submit(self,body=None,hook=None):
        body=self.body if body is None else body
        status,raw=self.request('POST','/api/teaching',body)
        self.assertEqual(status,202,raw)
        deadline=time.monotonic()+5
        while time.monotonic()<deadline:
            _,raw=self.request('GET','/api/jobs/'+body['request_id']);job=json.loads(raw)
            if job['state'] not in ('running','waiting'):return job
            if hook:hook()
            self.request('POST','/api/teacher-control',{'job_id':body['request_id'],'action':'heartbeat'})
            time.sleep(.02)
        self.fail('Return job did not finish')

    def test_return_reuses_exact_checked_segment_and_deduplicates(self):
        self.prepare()
        with mock.patch.object(self.robot,'getInverseKinematics',side_effect=AssertionError('Return must use saved joints')):
            job=self.submit()
        self.assertEqual(job['state'],'done',job['error'])
        result=job['result']['return_result']
        self.assertEqual(result['status'],'completed')
        self.assertEqual(result['observation_sha256'],self.observation_sha)
        self.assertEqual(result['actual_joints_rad'],self.target)
        self.assertEqual(self.checked,[(self.start,self.target)])
        self.assertEqual(len(self.joint_calls),1)
        self.assertEqual(self.joint_calls[0],(self.target,math.radians(3),.1,True))
        self.assertEqual(self.robot.moves,[])
        certificate=result['path_certificate']
        self.assertEqual(certificate['observation_sha256'],self.observation_sha)
        self.assertEqual(certificate['motion_kind'],'return_observation')
        self.assertEqual(certificate['outcome'],'completed')
        self.assertEqual(certificate['trajectory'],'joint_linear_unblended')
        self.assertAlmostEqual(result['joint_speed_deg_s'],3)
        self.assertFalse(self.robot.connected)
        self.assertIsNone(self.server.owner.active)
        self.assertEqual(self.request('POST','/api/teaching',self.body)[0],202)
        self.assertEqual(len(self.joint_calls),1)
        self.assertEqual(self.request('POST','/api/teaching',dict(self.body,joint_speed_deg_s=4))[0],400)
        self.assertEqual(digest(self.observation_path),self.observation_sha)

    def test_unresolved_return_never_moves(self):
        self.prepare('unresolved')
        job=self.submit()
        self.assertEqual(job['state'],'failed')
        self.assertIn('unresolved',job['error'])
        self.assertEqual(self.joint_calls,[])

    def test_return_requires_independent_path_approval(self):
        self.prepare()
        job=self.submit(dict(self.body,path_confirmed=False))
        self.assertEqual(job['state'],'failed')
        self.assertIn('Confirm',job['error'])
        self.assertEqual(self.joint_calls,[])

    def test_observation_change_during_intent_revokes_dispatch(self):
        self.prepare()
        with self.intent_hook(lambda:self.observation_path.write_text('{}')):
            job=self.submit()
        self.assertEqual(job['state'],'failed')
        self.assertIn('observation changed',job['error'].lower())
        self.assertEqual(self.joint_calls,[])

    def test_stop_during_return_intent_revokes_dispatch(self):
        self.prepare()
        with self.intent_hook(self.service.stop):job=self.submit()
        self.assertEqual(job['state'],'failed')
        self.assertEqual(self.joint_calls,[])

    def test_wrong_model_return_refuses(self):
        self.prepare()
        self.model_error=.005
        job=self.submit()
        self.assertEqual(job['state'],'failed')
        self.assertIn('model',job['error'])
        self.assertEqual(self.joint_calls,[])

    def test_changed_saved_joints_cannot_rebind_cached_target(self):
        self.prepare()
        raw=json.loads(self.observation_path.read_text())
        raw['robot']['joints_rad'][0]+=.02
        self.observation_path.write_text(json.dumps(raw))
        job=self.submit()
        self.assertEqual(job['state'],'failed')
        self.assertIn('observation',job['error'].lower())
        self.assertEqual(self.joint_calls,[])

    def test_stop_during_return_check_revokes_dispatch(self):
        self.prepare()
        self.during_check=self.service.stop
        job=self.submit()
        self.assertEqual(job['state'],'failed')
        self.assertEqual(self.joint_calls,[])
        self.assertIn('stop',job['error'].lower())

    def test_changed_tcp_and_model_refuse(self):
        self.prepare()
        self.robot.tcp=[0,0,.3,0,0,0]
        job=self.submit()
        self.assertEqual(job['state'],'failed')
        self.assertIn('TCP',job['error'])
        self.assertEqual(self.joint_calls,[])

    def test_return_failed_stop_fences_restart(self):
        from anodizing_web.jobs import JobOwner
        self.prepare()
        self.robot._duration=3
        self.stop_fails=True
        def stop_after_dispatch():
            if self.joint_calls:self.service.stop()
        job=self.submit(hook=stop_after_dispatch)
        self.assertEqual(job['state'],'failed')
        self.assertEqual(len(self.joint_calls),1)
        self.assertTrue(self.stop_calls)
        self.assertTrue(self.server.owner.execution_uncertain)
        self.assertTrue(job['execution_uncertain'])
        restarted=JobOwner(self.root)
        with self.assertRaisesRegex(ValueError,'uncertain|reconciliation'):
            restarted.submit('no-replay-return','teaching',{'action':'return_observation'},lambda _:None)

    def test_return_optin_model_and_scene_are_required_at_admission(self):
        self.prepare()
        self.server.owner.allow_teacher_motion=False
        self.assertEqual(self.request('POST','/api/teaching',self.body)[0],400)
        self.server.owner.allow_teacher_motion=True
        self.server.owner.robot_model=None
        self.assertEqual(self.request('POST','/api/teaching',self.body)[0],400)
        self.assertEqual(self.joint_calls,[])

    def test_ui_exposes_separate_return_approval_and_speed(self):
        self.prepare()
        _,raw=self.request('GET','/teaching');page=raw.decode()
        self.assertIn('observation-return-confirmed',page)
        self.assertNotIn('Return is disabled here',page)
        self.assertIn('value="3"',page)
        _,raw=self.request('GET','/teacher.js');script=raw.decode()
        self.assertIn("'validation/return':'return_observation'",script)
        self.assertNotIn("$('observation-return').disabled=true",script)
