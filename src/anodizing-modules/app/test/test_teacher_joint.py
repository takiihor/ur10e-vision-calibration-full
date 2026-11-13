"""Real native validation guards/worker with explicit fake robot/model boundaries."""
import json
import math
import time
from types import SimpleNamespace
import unittest

import numpy as np
from test_marker_validation import ValidationTests
from ur_pick_place.manual import ManualSettings
from ur_pick_place.poses import PoseStore
from ur_pick_place.vision.validation import check_capture
from ur_pick_place.vision.transforms import pose_to_matrix, matrix_to_pose, invert
from anodizing_web.teacher_motion import TeacherJointMotion


class JointTeacherTests(unittest.TestCase):
    setUp = ValidationTests.setUp

    def prepare(self, status='clear_in_model'):
        self.start = list(self.robot.joints)
        self.target = [q+.01 for q in self.start]
        self.joint_calls, self.stop_calls = [], []
        self.operation, self.changes, self.running = 0, 0, False
        self.stop_fails = False
        start_pose = list(self.robot.pose)
        end_pose = self.record['validation_tcp_pose_m_rad']
        self.model_error = 0
        def flange(q):
            f = (q[0]-self.start[0])/.01
            pose = [a+(b-a)*f for a,b in zip(start_pose,end_pose)]
            pose[0] += self.model_error
            return pose_to_matrix(pose) @ invert(pose_to_matrix(self.project.settings['tcp']))
        def actual_fk(q, tcp):
            old = self.model_error
            self.model_error = 0
            value = matrix_to_pose(flange(q) @ pose_to_matrix(tcp))
            self.model_error = old
            return value
        self.robot.getForwardKinematics = actual_fk
        self.robot.getInverseKinematics = lambda pose,q: list(self.target)
        self.robot.getActualTCPPose = lambda: actual_fk(self.robot.getActualQ(), self.project.settings['tcp'])
        self.robot.isSteady = lambda: self.robot._move is None
        self.robot.getActualQd = lambda: [0.0]*6
        def begin():
            self.operation=(self.operation+1)&127
            self.changes+=1
            self.running=True
        def move(q,speed,accel,asynchronous):
            self.joint_calls.append((list(q),speed,accel,asynchronous))
            begin()
            return self.robot._start('joints',q)
        self.robot.moveJ=move
        def stop(accel,asynchronous):
            self.stop_calls.append((accel,asynchronous))
            if self.stop_fails:
                raise RuntimeError('injected stop connection failure')
            begin()
            self.robot._move=None
            return None  # Installed stopJ returns no acceptance Boolean.
        self.robot.stopJ=stop
        def progress():
            self.robot._update()
            if self.running and self.robot._move is None:
                self.running=False
                self.changes+=1
            op,changes,running=self.operation,self.changes,self.running
            return SimpleNamespace(operationId=lambda:op,changeCount=lambda:changes,isAsyncOperationRunning=lambda:running)
        self.robot.getAsyncOperationProgressEx=progress
        self.checked=[]
        self.during_check=lambda:None
        def check(a,b):
            self.checked.append((list(a),list(b)))
            self.during_check()
            return {'status':status,'start_joints':list(a),'target_joints':list(b),'scene_sha256':'scene-hash','model_sha256':'model-hash'}
        self.scene=SimpleNamespace(model={'nominal':False},model_hash='model-hash',scene_hash='scene-hash',
            data={'model_uncertainty_m':.001},flange=flange,require_motion_ready=lambda:None,check_segment=check)
        self.binding_ok=True
        def binding():
            if not self.binding_ok:
                raise ValueError('The accepted scene changed')
        self.motion=TeacherJointMotion(ManualSettings(robot_ip='fake',joint_speed_rad_s=math.radians(3),joint_accel_rad_s2=.1),
            PoseStore(self.root/'waypoints.json'),project=self.project,record=self.record,
            guard=lambda:check_capture(self.project,self.record),factory=lambda host:(self.robot,self.robot),
            collision=self.scene,check_binding=binding,evidence=self.root/'checked-joint.json')
        self.addCleanup(self.motion.close)
        self.motion.submit('connect')
        self.wait_idle()

    def wait_idle(self):
        deadline=time.monotonic()+4
        while time.monotonic()<deadline:
            self.motion.heartbeat()
            state=self.motion.state()
            if not state['pending'] and not state['moving']:
                return state
            time.sleep(.01)
        self.fail('Native joint worker did not finish')

    def test_checked_joint_move_keeps_target_z_and_records_exact_path(self):
        self.prepare()
        self.motion.submit('move_tcp')
        state=self.wait_idle()
        self.assertFalse(state['error'],state['error'])
        self.assertEqual(state['message'],'Movement complete')
        self.assertEqual(len(self.joint_calls),1)
        self.assertEqual(self.joint_calls[0],(self.target,math.radians(3),.1,True))
        self.assertEqual(self.robot.moves,[],'Teacher must not dispatch moveL')
        self.assertEqual(self.checked,[(self.start,self.target)])
        self.assertAlmostEqual(self.robot.getActualTCPPose()[2],self.record['capture_robot']['tcp_pose_m_rad'][2])
        saved=json.loads((self.root/'checked-joint.json').read_text())
        self.assertEqual(saved['start_joints'],self.start)
        self.assertEqual(saved['target_joints'],self.target)
        self.assertEqual(saved['actual_joints'],self.target)
        self.assertEqual(saved['trajectory'],'joint_linear_unblended')

    def test_blocked_or_unresolved_segment_never_dispatches(self):
        self.prepare('unresolved')
        self.motion.submit('move_tcp')
        state=self.wait_idle()
        self.assertIn('unresolved',state['error'])
        self.assertEqual(self.joint_calls,[])

    def test_drift_during_check_refuses_before_dispatch(self):
        self.prepare()
        self.during_check=lambda:self.robot.joints.__setitem__(0,self.robot.joints[0]+.0003)
        self.motion.submit('move_tcp')
        state=self.wait_idle()
        self.assertIn('changed',state['error'])
        self.assertEqual(self.joint_calls,[])

    def test_wrong_model_refuses_before_dispatch(self):
        self.prepare()
        self.model_error=.005
        self.motion.submit('move_tcp')
        state=self.wait_idle()
        self.assertIn('model',state['error'])
        self.assertEqual(self.joint_calls,[])

    def test_scene_changed_during_check_refuses_before_dispatch(self):
        self.prepare()
        self.during_check=lambda:setattr(self,'binding_ok',False)
        self.motion.submit('move_tcp')
        state=self.wait_idle()
        self.assertIn('scene changed',state['error'])
        self.assertEqual(self.joint_calls,[])

    def test_stop_during_check_revokes_dispatch(self):
        self.prepare()
        self.during_check=self.motion.stop
        self.motion.submit('move_tcp')
        state=self.wait_idle()
        self.assertEqual(self.joint_calls,[])
        self.assertIn('stop',state['error'].lower())


    def intent_hook(self, hook):
        from unittest import mock
        from anodizing_web import teacher_motion
        original = teacher_motion.atomic_json
        def write(path, value):
            original(path,value)
            if value.get('outcome') == 'dispatching':
                hook()
        return mock.patch.object(teacher_motion,'atomic_json',side_effect=write)

    def test_stop_during_intent_write_refuses_before_dispatch(self):
        self.prepare()
        with self.intent_hook(self.motion.stop):
            self.motion.submit('move_tcp')
            state=self.wait_idle()
        self.assertEqual(self.joint_calls,[],'Saved intent cannot bypass a later Stop')
        self.assertIn('stop',state['error'].lower())

    def test_capture_expiring_during_intent_write_refuses_before_dispatch(self):
        self.prepare()
        with self.intent_hook(lambda:self.record.__setitem__('timestamp',time.time()-61)):
            self.motion.submit('move_tcp')
            state=self.wait_idle()
        self.assertEqual(self.joint_calls,[],'Saved intent cannot extend capture lifetime')
        self.assertIn('expired',state['error'])

    def test_scene_changed_during_intent_write_refuses_before_dispatch(self):
        self.prepare()
        with self.intent_hook(lambda:setattr(self,'binding_ok',False)):
            self.motion.submit('move_tcp')
            state=self.wait_idle()
        self.assertEqual(self.joint_calls,[],'Saved intent cannot authorize a changed scene')
        self.assertIn('scene changed',state['error'])


class JointTeacherIntegrationTests(JointTeacherTests):
    # Run only these two tests explicitly; inherited unit cases have prior evidence.
    def public_server(self):
        import threading
        from unittest import mock
        from anodizing_web.server import Server
        from anodizing_web.teaching import _identity, checked_settings
        from test_port import PortTests
        self.server=Server(0,self.root)
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        threading.Thread(target=self.server.serve_forever,daemon=True).start()
        self.request=lambda *a,**kw:PortTests.request(self,*a,**kw)
        self.server.allow_teaching=True
        owner=self.server.owner
        owner.allow_teacher_motion=True
        owner.robot_model={'source':'fake-model','sha256':'model-hash'}
        owner.teacher_service=self.service
        owner.teacher_project=str(self.project.directory)
        owner.teacher_settings=_identity(checked_settings(self.root,dict(self.project.settings,project=str(self.project.directory))))
        owner.teacher_job='previous-job'
        binding={'snapshot':'fake-scene','sha256':'scene-hash','model':{'snapshot':'fake-model','sha256':'model-hash'}}
        for name,kwargs in [('anodizing_web.auto_scene.prepare',{'return_value':binding}),
                            ('anodizing_web.auto_scene.check',{}),
                            ('ur_handeye.collision.load_scene',{'return_value':self.scene})]:
            patch=mock.patch(name,**kwargs);patch.start();self.addCleanup(patch.stop)
        return owner

    def run_public_move(self):
        import json
        body={'request_id':'joint-test-request','action':'validate_move','project':'project',
            'capture_id':self.record['id'],'path_confirmed':True,'joint_speed_deg_s':3,
            'collision_scene':'selected-scene.json','workspace_min_m':[-.9,-.9,.02],'workspace_max_m':[.9,.9,1.2]}
        status,raw=self.request('POST','/api/teaching',body)
        self.assertEqual(status,202,raw)
        deadline=time.monotonic()+4
        while time.monotonic()<deadline:
            _,raw=self.request('GET','/api/jobs/joint-test-request')
            job=json.loads(raw)
            if job['state'] not in ('running','waiting'):
                return body,job
            self.request('POST','/api/teacher-control',{'job_id':'joint-test-request','action':'heartbeat'})
            time.sleep(.02)
        self.fail('Public native motion job did not complete')

    def test_public_job_reuses_native_capture_and_dispatches_once(self):
        self.prepare()
        self.motion.close()  # The direct-class fixture is not the web worker.
        self.robot.connected=True
        self.robot._move=None
        self.public_server()
        body,job=self.run_public_move()
        self.assertEqual(job['state'],'done',job['error'])
        result=job['result']['project']['validation']
        self.assertEqual(result['id'],self.record['id'])
        self.assertEqual(result['status'],'completed')
        self.assertEqual(result['path_certificate']['target_joints'],self.target)
        self.assertEqual(len(self.joint_calls),1)
        status,raw=self.request('POST','/api/teaching',body)
        self.assertEqual(status,202,raw)
        self.assertEqual(len(self.joint_calls),1,'Retrying a request ID cannot dispatch again')
        self.assertIsNone(self.server.owner.active)
        self.assertFalse(self.robot.connected,'Admission ends only after native cleanup')

    def test_failed_stop_leaves_durable_fence_on_restart(self):
        from anodizing_web.jobs import JobOwner
        self.prepare()
        self.motion.close()
        self.robot.connected=True
        self.robot._duration=3
        self.stop_fails=True
        self.public_server()
        # Trigger native guard failure only after the actual SDK-shaped move starts.
        original_guard=self.service._motion_guard
        def guard(project,record):
            if self.joint_calls:
                raise ValueError('Injected browser heartbeat loss')
            original_guard(project,record)
        self.service._motion_guard=guard
        _,job=self.run_public_move()
        self.assertEqual(job['state'],'failed')
        self.assertEqual(len(self.joint_calls),1)
        self.assertTrue(self.stop_calls,'Native worker must attempt a stop')
        new_owner=JobOwner(self.root)
        with self.assertRaisesRegex(ValueError,'uncertain|reconciliation'):
            new_owner.submit('another-motion-job','teaching',{'action':'validate_move'},lambda _:None)


    def blocked_sdk_worker(self, stage):
        import threading
        from unittest import mock
        self.prepare()
        self.motion.close()
        self.robot.connected=True
        self.public_server()
        entered,release=threading.Event(),threading.Event()
        self.addCleanup(release.set)
        name='moveJ' if stage=='dispatch' else 'getInverseKinematics'
        original=getattr(self.robot,name)
        def blocked(*args):
            entered.set()
            release.wait(5)
            return original(*args)
        setattr(self.robot,name,blocked)
        body={'request_id':'blocked-native-job','action':'validate_move','project':'project',
            'capture_id':self.record['id'],'path_confirmed':True,'joint_speed_deg_s':3,
            'collision_scene':'selected-scene.json','workspace_min_m':[-.9,-.9,.02],'workspace_max_m':[.9,.9,1.2]}
        status,raw=self.request('POST','/api/teaching',body)
        self.assertEqual(status,202,raw)
        self.assertTrue(entered.wait(2),'The real RTDE-shaped worker must block in the selected SDK method')
        worker=self.service.motion._worker
        original_join=worker.join
        # Exercise native close timeout quickly; an unbounded drain join still waits for release.
        def quick_join(timeout=None):
            return original_join(None if timeout is None else .02)
        with mock.patch.object(worker,'join',side_effect=quick_join):
            self.request('POST','/api/teacher-control',{'job_id':'blocked-native-job','action':'stop'})
            deadline=time.monotonic()+2
            while time.monotonic()<deadline:
                if self.service.state()['busy'] is None or self.server.owner.execution_uncertain:
                    break
                time.sleep(.02)
            self.assertTrue(worker.is_alive())
            self.assertTrue(self.server.owner.active=='blocked-native-job' or self.server.owner.execution_uncertain,
                'A live native SDK worker must retain admission or an immediate hard fence')
            self.assertTrue(self.service.motion.cleanup_uncertain if self.service.motion else self.server.owner.execution_uncertain,
                'Close timeout must be an explicit uncertain outcome')
            self.assertEqual(self.request('POST','/api/teaching',{'request_id':'competing-native','action':'capture_validation','project':'project'})[0],400)
            release.set()
            worker.join(2)
            deadline=time.monotonic()+3
            while self.server.owner.active and time.monotonic()<deadline:
                time.sleep(.02)
            self.assertFalse(worker.is_alive())

    def test_blocked_dispatch_worker_cannot_release_ownership_on_close_timeout(self):
        self.blocked_sdk_worker('dispatch')

    def test_blocked_preflight_worker_cannot_release_ownership_on_close_timeout(self):
        self.blocked_sdk_worker('preflight')


    def test_joint_result_records_actual_joint_units_and_endpoint_contract(self):
        self.prepare()
        self.motion.close()
        self.robot.connected=True
        self.public_server()
        _,job=self.run_public_move()
        self.assertEqual(job['state'],'done',job['error'])
        record=job['result']['project']['validation']
        self.assertNotIn('speed_mm_s',record,'Dummy TCP speed must not be reported as the issued joint speed')
        self.assertEqual(record['trajectory'],'joint_linear_unblended')
        self.assertEqual(record['height_contract'],'destination_only')
        self.assertAlmostEqual(record['joint_speed_deg_s'],3)
        self.assertAlmostEqual(record['joint_accel_rad_s2'],.1)
