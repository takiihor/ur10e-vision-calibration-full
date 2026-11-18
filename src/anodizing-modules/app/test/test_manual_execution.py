"""Offline real-manual worker checks; fake SDK/checker evidence is not physical validation."""
import copy
import importlib
import math
import threading
import time
from types import SimpleNamespace
import unittest

from ur_pick_place.manual import ManualSettings
from anodizing_web.manual_pick_place import vertical_intent


def executor_module(test):
    try:
        return importlib.import_module('anodizing_web.manual_execution')
    except ModuleNotFoundError as exc:
        if exc.name != 'anodizing_web.manual_execution':
            raise
        test.fail('The real manual Pick/Place executor has not been implemented.')


class ManualRouteTests(unittest.TestCase):
    def settings(self):
        return ManualSettings(robot_ip='fake', workspace_min_m=[-.9,-.9,.01],
            workspace_max_m=[.9,.9,1.2], tcp_speed_mm_s=10,
            joint_speed_rad_s=math.radians(3), joint_accel_rad_s2=.1)

    def test_real_saved_pick_below_workspace_is_refused_without_clamping(self):
        module=executor_module(self)
        route=vertical_intent([.4,.1,.007525,0,math.pi,0],117.056)
        original=copy.deepcopy(route)
        with self.assertRaisesRegex(ValueError, 'workspace|Workspace'):
            module.validate_route(route,self.settings())
        self.assertEqual(route,original)
        self.assertAlmostEqual(route['endpoint'][2]*1000,7.525)
        self.assertAlmostEqual(route['above'][2]*1000,124.581)

    def test_all_route_stages_must_fit_workspace_before_any_dispatch(self):
        module=executor_module(self)
        for endpoint,approach in (([.4,.1,1.15,0,math.pi,0],40),
                                  ([.4,.1,1.05,0,math.pi,0],200),
                                  ([.95,.1,.3,0,math.pi,0],100)):
            with self.subTest(endpoint=endpoint,approach=approach):
                route=vertical_intent(endpoint,approach)
                with self.assertRaisesRegex(ValueError,'workspace|Workspace'):
                    module.validate_route(route,self.settings())

    def test_safe_route_keeps_requested_values_and_full_hundred_mm_withdrawal(self):
        module=executor_module(self)
        route=vertical_intent([.4,.1,.3,0,math.pi,0],40)
        original=copy.deepcopy(route)
        module.validate_route(route,self.settings())
        self.assertEqual(route,original)
        self.assertAlmostEqual(route['above'][2],.34)
        self.assertAlmostEqual(route['withdrawn'][2],.4)

    def test_route_cannot_change_xy_or_orientation_between_vertical_endpoints(self):
        module=executor_module(self)
        for name,index in (('above',0),('above',3),('withdrawn',1),('withdrawn',5)):
            route=vertical_intent([.4,.1,.3,0,math.pi,0],100)
            route[name][index]+=.01
            with self.subTest(name=name,index=index),self.assertRaises(ValueError):
                module.validate_route(route,self.settings())


class OfflineRobot:
    """Deterministic RTDE-shaped fixture; q numerically encodes TCP for test wiring only."""
    def __init__(self, tcp, pose):
        self.tcp=list(tcp)
        self.q=list(pose)
        self.connected=True
        self.motion=None
        self.operation=self.changes=0
        self.duration=.06
        self.moves=[]
        self.stops=[]
        self.stop_fails=False
        self.cleanup_gate=None
        self.cleanup_entered=threading.Event()
        self.dispatch_hook=lambda kind,target:None

    def _update(self):
        if self.motion:
            start,target,began=self.motion
            fraction=min(1,(time.monotonic()-began)/self.duration)
            self.q=[a+(b-a)*fraction for a,b in zip(start,target)]
            if fraction>=1:
                self.motion=None
                self.changes+=1

    def isConnected(self):return self.connected
    def getSafetyMode(self):return 1
    def getTimestamp(self):return time.monotonic()
    def getTCPOffset(self):return list(self.tcp)
    def getActualQ(self):
        self._update();return list(self.q)
    def getActualTCPPose(self):return self.getActualQ()
    def getActualQd(self):
        self._update();return [0.0]*6 if not self.motion else [.01]*6
    def getActualTCPForce(self):return [0.0]*6
    def getForwardKinematics(self,q,tcp=None):
        from ur_pick_place.vision.transforms import pose_to_matrix,matrix_to_pose,invert
        if tcp is None:raise AssertionError('Explicit TCP is required in the fixture')
        return matrix_to_pose(pose_to_matrix(q)@invert(pose_to_matrix(self.tcp))@pose_to_matrix(tcp))
    def getInverseKinematics(self,pose,qnear=None):return list(pose)
    def getInverseKinematicsHasSolution(self,pose,qnear=None):return True
    def isJointsWithinSafetyLimits(self,q):return True
    def isPoseWithinSafetyLimits(self,pose):return True
    def isSteady(self):
        self._update();return self.motion is None
    def getAsyncOperationProgressEx(self):
        self._update()
        operation,changes,running=self.operation,self.changes,self.motion is not None
        return SimpleNamespace(operationId=lambda:operation,changeCount=lambda:changes,
                               isAsyncOperationRunning=lambda:running)
    def getAsyncOperationProgress(self):
        self._update();return 0 if self.motion else -1
    def _move(self,kind,target,speed,accel,asynchronous):
        self._update()
        if self.motion:raise AssertionError('Fixture received overlapping native movements')
        self.dispatch_hook(kind,list(target))
        self.moves.append((kind,list(target),speed,accel,asynchronous))
        self.operation=(self.operation+1)&127;self.changes+=1
        self.motion=(list(self.q),list(target),time.monotonic())
        return True
    def moveJ(self,*args):return self._move('moveJ',*args)
    def moveL(self,*args):return self._move('moveL',*args)
    def _stop(self,kind,*args):
        self.stops.append(kind)
        if self.stop_fails:raise RuntimeError('Injected stop failure')
        self._update();self.motion=None
        self.operation=(self.operation+1)&127;self.changes+=1
    def stopJ(self,*args):return self._stop('stopJ',*args)
    def stopL(self,*args):return self._stop('stopL',*args)
    def stopScript(self):
        if self.cleanup_gate:
            self.cleanup_entered.set()
            self.cleanup_gate.wait()
        self.motion=None
        return True
    def disconnect(self):self.connected=False


class ManualWorkerTests(unittest.TestCase):
    def prepare(self, operation='PICK', endpoint_z=.3):
        import tempfile
        import numpy as np
        from pathlib import Path
        from unittest import mock
        from ur_pick_place.poses import PoseStore
        from ur_pick_place.vision.config import VisionConfig
        from ur_pick_place.vision.grasp import configured_grasp
        from ur_pick_place.vision.observe import Observation
        from ur_pick_place.vision.targets import target_from_config
        from ur_pick_place.vision.transforms import pose_to_matrix, invert
        self.module=executor_module(self)
        temporary=tempfile.TemporaryDirectory();self.addCleanup(temporary.cleanup)
        self.root=Path(temporary.name)
        self.tcp=[0,0,.15,0,0,0]
        self.start=[.35,.08,.5,0,math.pi,0]
        self.endpoint=[.4,.1,endpoint_z,0,math.pi,0]
        self.robot=OfflineRobot(self.tcp,self.start)
        self.binding_ok=True
        self.check_hook=lambda:None
        self.vertical_status='clear_in_model'
        self.vertical_calls=[]
        self.rebind_calls=[]
        self.rebind_allowed=True
        self.inputs_ok=True
        self.alignment_ok=True
        self.joint_calls=[]
        def binding():
            if not self.binding_ok:raise ValueError('Selected scene changed')
        def segment(a,b):
            self.joint_calls.append((list(a),list(b)))
            return dict(status='clear_in_model',start_joints=list(a),target_joints=list(b),
                        scene_sha256='scene',model_sha256='factory')
        self.scene=SimpleNamespace(model={'nominal':False},model_hash='factory',scene_hash='scene',
            data={'model_uncertainty_m':.001,'attached':[{'name':'tool','dimensions_m':[.390,.080,.100]}]},flange=lambda q:pose_to_matrix(q)@invert(pose_to_matrix(self.tcp)),
            require_motion_ready=lambda:None,check_segment=segment)
        target_config={'kind':'aruco','marker_id':7,'dictionary':'DICT_5X5_100','marker_length_mm':30}
        target=target_from_config(target_config)
        role={'target':target_config,'observe_joints':self.start,'refine':{'enabled':False}}
        config=VisionConfig(handeye_calibration='fixture.json',pick=role,place=copy.deepcopy(role))
        model=configured_grasp({'marker_to_tcp':[0]*6,'reference_tcp_pose':self.endpoint},operation.lower())
        marker=pose_to_matrix(self.endpoint)
        self.observation=Observation(operation.lower(),target.describe(),marker,np.eye(4),np.eye(4),marker,
            {'joints_rad':list(self.start),'tcp_pose_m_rad':list(self.start),'tcp_offset':self.tcp},
            {'pixels_per_module':3.0},False,'calibration')
        def check_inputs():
            if not self.inputs_ok:raise ValueError('Manual teaching/grasp input changed; start a new operation.')
        def check_alignment():
            if not self.alignment_ok:raise ValueError('Current alignment changed; capture and align again.')
        self.context=SimpleNamespace(settings={'tcp':self.tcp,'robot_ip':'fake'},config=config,model=model,target=target,
            calibration=SimpleNamespace(sha256='calibration'),provenance={'fixture':'native data; fake robot/checker'},
            check_inputs=check_inputs,check_alignment=check_alignment,role=operation.lower(),approach_mm=40,
            joint_bounds_rad=[[-2*math.pi,2*math.pi] for _ in range(6)],
            joint_bounds_source='offline fixture limits')
        def vertical(scene,q0,tcp,target,workspace_min,workspace_max,binding):
            self.vertical_calls.append({'q0':list(q0),'target':list(target),'binding':copy.deepcopy(binding)})
            self.check_hook()
            return {'status':self.vertical_status,'reason':'explicit fake checker fixture',
                    'binding':dict(binding,workspace_min_m=workspace_min,workspace_max_m=workspace_max)}
        self.observe_mock=mock.patch.object(self.module,'observe_role',return_value=self.observation).start()
        self.addCleanup(mock.patch.stopall)
        mock.patch.object(self.module,'check_vertical',side_effect=vertical).start()
        def rebind(*args):
            self.rebind_calls.append(args)
            return self.rebind_allowed
        mock.patch.object(self.module,'rebind_vertical',side_effect=rebind,create=True).start()
        self.worker=self.module.ManualExecution(ManualRouteTests().settings(),PoseStore(self.root/'poses.json'),
            project=SimpleNamespace(settings={'tcp':self.tcp}),record={'id':'manual-job','validation_mode':'fixed_z'},
            guard=lambda:None,collision=self.scene,check_binding=binding,evidence=self.root/'execution.json',
            factory=lambda host:(self.robot,self.robot),operation=operation,context=self.context)
        self.addCleanup(self.worker.close)
        self.worker.submit('connect');self.wait_idle()

    def wait_for(self, predicate, timeout=4):
        deadline=time.monotonic()+timeout
        while time.monotonic()<deadline:
            state=self.worker.state()
            if predicate(state):return state
            time.sleep(.01)
        self.fail('Worker did not reach expected state: '+str(self.worker.state()))

    def wait_idle(self):
        return self.wait_for(lambda s:not s['pending'] and not s['moving'])

    def plan(self):
        self.worker.submit('move_tcp')
        state=self.wait_for(lambda s:s['manual']['stage']=='awaiting_plan' or bool(s['error']))
        self.assertFalse(state['error'],state['error'])
        self.assertEqual(self.robot.moves,[],'Fresh plan must be reviewed before any dispatch')
        return state['manual']['prompt']

    def at_hand_prompt(self, operation='PICK'):
        self.prepare(operation)
        self.worker.confirm(self.plan(),True)
        phase='grip' if operation=='PICK' else 'release'
        state=self.wait_for(lambda s:s['manual']['stage']=='awaiting_'+phase or bool(s['error']))
        self.assertFalse(state['error'],state['error'])
        return state['manual']['prompt']

    def test_pick_and_place_native_sequences_pause_then_use_two_vertical_moveL_legs(self):
        # Each operation gets a new worker/observation, never a cached synthetic target.
        for operation,phase,final in [('PICK','grip','operator_confirmed_held'),
                                      ('PLACE','release','operator_confirmed_released')]:
            with self.subTest(operation=operation):
                prompt=self.at_hand_prompt(operation)
                self.assertEqual(prompt['phase'],phase)
                self.assertEqual([m[0] for m in self.robot.moves],['moveJ','moveL'])
                self.assertEqual(self.observe_mock.call_count,1)
                before=copy.deepcopy(self.robot.moves)
                time.sleep(.08)
                self.assertEqual(self.robot.moves,before,'Manual prompt must hold')
                self.worker.confirm(prompt,True)
                state=self.wait_for(lambda s:s['manual']['stage']=='done' or bool(s['error']))
                self.assertFalse(state['error'],state['error'])
                self.assertEqual(state['manual']['item_state'],final)
                self.assertEqual([m[0] for m in self.robot.moves],['moveJ','moveL','moveL'])
                approach,withdrawal=self.robot.moves[1:]
                self.assertEqual(approach[1][:2]+approach[1][3:],withdrawal[1][:2]+withdrawal[1][3:])
                self.assertAlmostEqual(withdrawal[1][2]-approach[1][2],.1)
                self.assertEqual(approach[2],.01)
                self.assertEqual(len(self.vertical_calls),2)
                self.worker.close()

    def test_manual_reply_binds_job_phase_and_is_consumed_once(self):
        prompt=self.at_hand_prompt()
        for bad,value in [(dict(prompt,job_id='other'),True),(dict(prompt,phase='release'),True),(prompt,False)]:
            with self.assertRaises(ValueError):self.worker.confirm(bad,value)
        self.assertEqual(len(self.robot.moves),2)
        self.worker.confirm(prompt,True)
        with self.assertRaises(ValueError):self.worker.confirm(prompt,True)
        state=self.wait_for(lambda s:s['manual']['stage']=='done' or bool(s['error']))
        self.assertFalse(state['error'],state['error'])
        self.assertEqual(len(self.robot.moves),3)

    def test_stop_at_manual_boundary_makes_item_unknown_and_rejects_late_reply(self):
        prompt=self.at_hand_prompt()
        self.worker.stop()
        state=self.wait_for(lambda s:s['manual']['stage']=='interrupted')
        self.assertEqual(state['manual']['item_state'],'unknown')
        with self.assertRaises(ValueError):self.worker.confirm(prompt,True)
        time.sleep(.08)
        self.assertEqual([m[0] for m in self.robot.moves],['moveJ','moveL'])

    def test_unresolved_vertical_checker_cannot_fall_back_to_joint_descent(self):
        self.prepare();self.vertical_status='unresolved'
        self.worker.confirm(self.plan(),True)
        state=self.wait_for(lambda s:bool(s['error']))
        self.assertIn('unresolved',state['error'])
        self.assertEqual([m[0] for m in self.robot.moves],['moveJ'])
        self.assertEqual(state['manual']['item_state'],'unknown')

    def test_fresh_role_and_quality_gate_precedes_plan_or_any_movement(self):
        for field,value in [('timestamp',time.time()-61),('role','place'),('quality',{'pixels_per_module':2.99})]:
            with self.subTest(field=field):
                self.prepare();setattr(self.observation,field,value)
                self.worker.submit('move_tcp')
                state=self.wait_for(lambda s:bool(s['error']))
                self.assertEqual(self.robot.moves,[])
                self.assertIsNone(state['manual']['prompt'])
                self.worker.close()

    def test_plan_expiration_and_scene_change_do_not_dispatch(self):
        for changed in ('expired','scene'):
            with self.subTest(changed=changed):
                self.prepare();prompt=self.plan()
                if changed=='expired':self.observation.timestamp=time.time()-61
                else:self.binding_ok=False
                self.worker.confirm(prompt,True)
                self.wait_for(lambda s:bool(s['error']))
                self.assertEqual(self.robot.moves,[])
                self.worker.close()

    def test_saved_pick_outside_workspace_is_visible_and_refused_before_prompt(self):
        self.prepare(endpoint_z=.007525)
        self.context.approach_mm=117.056
        self.worker.submit('move_tcp')
        state=self.wait_for(lambda s:bool(s['error']))
        self.assertIn('workspace',state['error'])
        self.assertAlmostEqual(state['manual']['route']['endpoint'][2],.007525)
        self.assertAlmostEqual(state['manual']['route']['above'][2],.124581)
        self.assertEqual(self.robot.moves,[])

    def test_laptop_heartbeat_expiry_holds_at_boundary_without_replay(self):
        prompt=self.at_hand_prompt()
        self.worker._heartbeat_at=time.monotonic()-20
        time.sleep(.12)
        state=self.worker.state()
        self.assertFalse(state['error'],state['error'])
        self.assertEqual(state['manual']['prompt'],prompt)
        self.assertEqual([m[0] for m in self.robot.moves],['moveJ','moveL'])

    def test_stop_during_vertical_check_revokes_native_dispatch(self):
        self.prepare();self.check_hook=self.worker.stop
        self.worker.confirm(self.plan(),True)
        self.wait_for(lambda s:bool(s['error']))
        self.assertEqual([m[0] for m in self.robot.moves],['moveJ'])

    def test_feedback_jitter_uses_one_exact_vertical_start_for_binding_and_command(self):
        self.prepare()
        read=self.worker._read
        counter=[0]
        def varying_feedback():
            feedback=read()
            counter[0]+=1
            feedback['tcp_pose_m_rad'][0]+=counter[0]*1e-6
            feedback['tcp_pose_m_rad'][1]-=counter[0]*1e-6
            feedback['tcp_pose_m_rad'][5]+=counter[0]*1e-7
            return feedback
        self.worker._read=varying_feedback
        self.worker.confirm(self.plan(),True)
        state=self.wait_for(lambda s:s['manual']['stage']=='awaiting_grip' or bool(s['error']))
        self.assertFalse(state['error'],state['error'])
        approach=self.vertical_calls[0]
        start=approach['binding']['actual_start_tcp']
        self.assertEqual(approach['target'][:2]+approach['target'][3:],start[:2]+start[3:],
                         'moveL target must hold XY/orientation from its exact certificate start snapshot')
        self.worker.confirm(state['manual']['prompt'],True)
        state=self.wait_for(lambda s:s['manual']['stage']=='done' or bool(s['error']))
        self.assertFalse(state['error'],state['error'])
        withdrawal=self.vertical_calls[1]
        start=withdrawal['binding']['actual_start_tcp']
        self.assertEqual(withdrawal['target'][:2]+withdrawal['target'][3:],start[:2]+start[3:])
        self.assertAlmostEqual(withdrawal['target'][2]-start[2],.100)

    def test_feedback_change_during_dispatch_evidence_persistence_refuses_moveL(self):
        from unittest import mock
        self.prepare()
        original=self.module.atomic_json
        changed=[]
        def persist(path,value):
            result=original(path,value)
            if value.get('outcome')=='dispatching' and not changed:
                changed.append(True)
                self.robot.q[0]+=.001
            return result
        with mock.patch.object(self.module,'atomic_json',side_effect=persist):
            self.worker.confirm(self.plan(),True)
            state=self.wait_for(lambda s:bool(s['error']) or s['manual']['stage']=='awaiting_grip')
        self.assertEqual(changed,[True],'Regression must mutate feedback during final dispatch persistence')
        self.assertTrue(state['error'],'Changed current feedback after persistence must refuse dispatch')
        self.assertEqual([move[0] for move in self.robot.moves],['moveJ'])
        self.assertEqual(state['manual']['item_state'],'unknown')

    def test_final_cartesian_rebind_refusal_prevents_native_moveL(self):
        self.prepare()
        self.rebind_allowed=False
        self.worker.confirm(self.plan(),True)
        state=self.wait_for(lambda s:bool(s['error']))
        self.assertEqual([m[0] for m in self.robot.moves],['moveJ'])
        self.assertTrue(self.rebind_calls,'Revalidate the exact certificate and Scene immediately before dispatch')
        self.assertEqual(state['manual']['item_state'],'unknown')

    def test_teaching_or_grasp_change_while_manual_pause_cancels_without_movement(self):
        for phase in ('plan','grip'):
            with self.subTest(phase=phase):
                self.prepare()
                prompt=self.plan()
                if phase=='grip':
                    self.worker.confirm(prompt,True)
                    state=self.wait_for(lambda s:s['manual']['stage']=='awaiting_grip' or bool(s['error']))
                    self.assertFalse(state['error'],state['error'])
                    prompt=state['manual']['prompt']
                before=copy.deepcopy(self.robot.moves)
                self.inputs_ok=False
                state=self.wait_for(lambda s:s['manual']['stage']=='interrupted')
                self.assertIn('input changed',state['manual']['error'])
                self.assertEqual(self.robot.moves,before)
                self.assertEqual(state['manual']['item_state'],'unknown')
                with self.assertRaises(ValueError):self.worker.confirm(prompt,True)
                self.worker.close()

    def test_active_tcp_change_during_vertical_check_refuses_native_dispatch(self):
        self.prepare();self.check_hook=lambda:self.robot.tcp.__setitem__(2,.16)
        self.worker.confirm(self.plan(),True)
        state=self.wait_for(lambda s:bool(s['error']))
        self.assertIn('TCP',state['error'])
        self.assertEqual([m[0] for m in self.robot.moves],['moveJ'])


    def test_stop_latches_before_blocked_owner_persistence(self):
        self.prepare()
        self.robot.dispatch_hook=lambda kind,target:setattr(self.robot,'duration',3) if kind=='moveL' else None
        self.worker.confirm(self.plan(),True)
        self.wait_for(lambda s:len(self.robot.moves)==2)
        entered,release=threading.Event(),threading.Event()
        job={'id':'manual-job','auto_action':'PICK'}
        def save(record):
            entered.set();release.wait(5)
        owner=SimpleNamespace(lock=threading.RLock(),jobs={'manual-job':job},active='manual-job',
            runner=self.worker,_save=save,get=lambda job_id:dict(job))
        errors=[]
        def stop():
            try:self.module.control(owner,{'job_id':'manual-job','action':'stop'})
            except BaseException as error:errors.append(error)
        thread=threading.Thread(target=stop)
        thread.start()
        try:
            self.assertTrue(entered.wait(2),'test must block actual persistence')
            self.assertTrue(self.worker.cancelled.is_set(),'Stop must latch before any potentially blocked save')
            self.wait_for(lambda s:'stopL' in self.robot.stops,timeout=1)
        finally:
            release.set();thread.join(3)
        self.assertFalse(errors,errors)

    def stop_while_worker_waits_for_owner_lock(self, callback):
        owner,_,_=self.public_descent()
        self.robot.dispatch_hook=lambda kind,target:setattr(self.robot,'duration',30)
        status,raw=self.request('POST','/api/teaching',self.public_body)
        self.assertEqual(status,202,raw)
        job=self.wait_public(lambda j:j['state']=='waiting' and j.get('pending',{}).get('phase')=='plan')
        worker=owner.runner
        at_callback,enter_callback=threading.Event(),threading.Event()
        lock_wait,persisting,release_disk=threading.Event(),threading.Event(),threading.Event()
        native_stop=threading.Event()
        original=getattr(worker,callback) if callback=='guard' else self.context.check_alignment
        def gated_callback():
            if threading.current_thread() is worker._worker and worker._motion and worker._motion['mode']=='tcp':
                at_callback.set();enter_callback.wait(5)
            return original()
        if callback=='guard':worker.guard=gated_callback
        else:self.context.check_alignment=gated_callback
        lock=owner.lock
        class ObservedLock:
            def acquire(self,*args,**kwargs):
                if threading.current_thread() is worker._worker:lock_wait.set()
                return lock.acquire(*args,**kwargs)
            def release(self):lock.release()
            def __enter__(self):self.acquire();return self
            def __exit__(self,*args):self.release()
        owner.lock=ObservedLock()
        original_stop=self.robot._stop
        def stop_native(kind,*args):
            result=original_stop(kind,*args)
            if kind=='stopL':native_stop.set()
            return result
        self.robot._stop=stop_native
        self.assertEqual(self.request('POST','/api/manual-control',{'job_id':job['id'],'action':'confirm',
            'prompt':job['pending'],'value':True})[0],200)
        self.assertTrue(at_callback.wait(3),'Active SDK worker must enter the real '+callback+' callback')
        self.assertIsNotNone(self.robot.motion,'Regression must contend during active native fake motion')
        def save(record):persisting.set();release_disk.wait(5)
        owner._save=save
        def persist():
            with owner.lock:owner._save(owner.jobs[job['id']])
        disk=threading.Thread(target=persist)
        errors=[]
        def stop():
            try:self.module.control(owner,{'job_id':job['id'],'action':'stop'})
            except BaseException as exc:errors.append(exc)
        request=threading.Thread(target=stop)
        disk.start()
        try:
            self.assertTrue(persisting.wait(2),'Persistence must hold owner.lock')
            lock_wait.clear();enter_callback.set()
            self.assertTrue(lock_wait.wait(2),'Active SDK worker must attempt owner.lock before Stop')
            request.start()
            self.assertTrue(worker.cancelled.wait(1),'Stop must latch while persistence holds owner.lock')
            self.assertTrue(native_stop.wait(1),'Native stopL must run BEFORE persistence releases owner.lock')
            self.assertFalse(release_disk.is_set())
            self.assertIs(owner.runner,worker,'Keep ownership while cleanup remains blocked')
            self.assertEqual(owner.active,job['id'])
        finally:
            enter_callback.set();release_disk.set();disk.join(3)
            if request.ident is not None:request.join(3)
        self.assertFalse(errors,errors)
        self.wait_public(lambda j:j['state']=='failed')
        self.assertEqual([move[0] for move in self.robot.moves],['moveL'])

    def test_stop_reaches_native_while_worker_guard_waits_for_persisting_owner(self):
        self.stop_while_worker_waits_for_owner_lock('guard')

    def test_stop_reaches_native_while_alignment_waits_for_persisting_owner(self):
        self.stop_while_worker_waits_for_owner_lock('check_alignment')

    def test_movement_error_stops_native_motion_before_blocked_interruption_publish(self):
        self.prepare()
        self.robot.dispatch_hook=lambda kind,target:setattr(self.robot,'duration',3) if kind=='moveL' else None
        entered,release=threading.Event(),threading.Event()
        publish=self.worker._publish
        def blocked_publish(**values):
            if values.get('stage')=='interrupted':entered.set();release.wait(5)
            return publish(**values)
        self.worker._publish=blocked_publish
        wait=self.worker._wait_move
        def fail_during_approach():
            if self.worker._motion and self.worker._motion['mode']=='tcp':
                raise RuntimeError('Injected movement error before feedback completion')
            return wait()
        self.worker._wait_move=fail_during_approach
        self.worker.confirm(self.plan(),True)
        try:
            self.assertTrue(entered.wait(2),'test must block the interruption publication')
            self.assertIn('stopL',self.robot.stops,'Stop active native motion before reporting/persisting interruption')
        finally:release.set()
        self.wait_for(lambda s:bool(s['error']))
        self.assertEqual([m[0] for m in self.robot.moves],['moveJ','moveL'])

    def test_stop_during_native_vertical_movement_uses_stopL_and_never_withdraws(self):
        self.prepare();self.robot.duration=.4
        self.worker.confirm(self.plan(),True)
        self.wait_for(lambda s:len(self.robot.moves)==2)
        self.worker.stop()
        state=self.wait_for(lambda s:s['manual']['stage']=='interrupted' and not s['moving'])
        self.assertEqual(state['manual']['item_state'],'unknown')
        self.assertIn('stopL',self.robot.stops)
        self.assertEqual([m[0] for m in self.robot.moves],['moveJ','moveL'])

    def test_low_level_stop_failure_sets_uncertainty_instead_of_replaying(self):
        self.prepare();self.robot.duration=.4
        self.worker.confirm(self.plan(),True)
        self.wait_for(lambda s:len(self.robot.moves)==2)
        self.robot.stop_fails=True
        self.worker.stop()
        self.wait_for(lambda s:bool(s['error']))
        self.assertTrue(self.worker.cleanup_uncertain)
        self.assertEqual([m[0] for m in self.robot.moves],['moveJ','moveL'])


    def public_server(self):
        import json
        from unittest import mock
        from anodizing_web.server import Server
        from test_port import PortTests
        self.prepare()
        self.worker.close();self.robot.connected=True
        self.server=Server(0,self.root)
        self.server.allow_teaching=True
        self.addCleanup(self.server.server_close);self.addCleanup(self.server.shutdown)
        threading.Thread(target=self.server.serve_forever,daemon=True).start()
        self.request=lambda *a,**kw:PortTests.request(self,*a,**kw)
        owner=self.server.owner
        owner.allow_manual_pick_place=True
        owner.robot_model={'source':'fake-model','sha256':'factory'}
        binding={'snapshot':'fake-scene','sha256':'scene','model':{'snapshot':'fake-model','sha256':'factory'}}
        for target,kwargs in [('anodizing_web.auto_scene.prepare',{'return_value':binding}),
                              ('anodizing_web.auto_scene.check',{}),
                              ('ur_handeye.collision.load_scene',{'return_value':self.scene}),
                              ('anodizing_web.manual_execution.load_context',{'return_value':self.context}),
                              ('anodizing_web.manual_execution.native_backend',{'return_value':(self.robot,self.robot)})]:
            patch=mock.patch(target,**kwargs);patch.start();self.addCleanup(patch.stop)
        self.public_body={'request_id':'manual-real-request','action':'PICK','project':'fixture-project',
            'calibration':'fixture-calibration','pick_tag':'tank01','place_tag':'tank01',
            'reuse_taught_offset':True,'operator_path_approved':True,'tcp_confirmed':True,
            'held_envelope_confirmed':True,'initial_item_state':'operator_confirmed_released',
            'collision_scene':'reviewed-scene.json','workspace_min_m':[-.9,-.9,.01],
            'workspace_max_m':[.9,.9,1.2],'joint_speed_deg_s':3,'speed_mm_s':10,
            'joint_bounds_rad':[[-2*math.pi,2*math.pi] for _ in range(6)],
            'joint_bounds_source':'offline fixture limits'}
        return owner

    def wait_public(self,predicate,timeout=4):
        import json
        deadline=time.monotonic()+timeout
        while time.monotonic()<deadline:
            status,raw=self.request('GET','/api/jobs/'+self.public_body['request_id'])
            self.assertEqual(status,200,raw)
            job=json.loads(raw)
            if predicate(job):return job
            time.sleep(.01)
        self.fail('Public manual job did not reach expected state: '+str(job))

    def test_public_manual_operation_refuses_missing_or_different_held_envelope(self):
        import json
        owner=self.public_server()
        for index,attached in enumerate(([],[{'name':'tool','dimensions_m':[.200,.080,.100]}])):
            self.scene.data['attached']=attached
            self.public_body['request_id']='missing-held-envelope-'+str(index)
            status,raw=self.request('POST','/api/manual-pick-place',self.public_body)
            self.assertEqual(status,202,raw)
            job=self.wait_public(lambda j:j['state']=='failed')
            self.assertIn('390 x 80 x 100',job['error'])
            self.assertEqual(self.robot.moves,[])
            self.assertIsNone(owner.runner)

    def test_public_native_job_admission_exact_retry_and_restart_cannot_replay(self):
        import json
        from anodizing_web.jobs import JobOwner
        owner=self.public_server()
        status,raw=self.request('POST','/api/manual-pick-place',self.public_body)
        self.assertEqual(status,202,raw)
        # Expose the accepted native worker only through the existing owner runner.
        deadline=time.monotonic()+4
        while time.monotonic()<deadline:
            worker=owner.runner
            if worker and worker.state()['manual']['stage']=='awaiting_plan':break
            job=owner.get(self.public_body['request_id'])
            self.assertNotEqual(job['state'],'failed',job['error'])
            time.sleep(.01)
        else:self.fail('Public job did not publish native plan boundary')
        for phase in ('plan','grip'):
            deadline=time.monotonic()+4
            while time.monotonic()<deadline:
                state=worker.state()
                self.assertFalse(state['error'],state['error'])
                if state['manual']['stage']=='awaiting_'+phase:break
                time.sleep(.01)
            else:self.fail('Missing public '+phase+' prompt')
            prompt=state['manual']['prompt']
            reply={'job_id':self.public_body['request_id'],'action':'confirm','prompt':prompt,'value':True}
            self.assertEqual(self.request('POST','/api/manual-control',dict(reply,job_id='different-job'))[0],400)
            self.assertEqual(self.request('POST','/api/manual-control',reply)[0],200)
            self.assertEqual(self.request('POST','/api/manual-control',reply)[0],400)
        job=self.wait_public(lambda j:j['state'] not in ('running','waiting'))
        self.assertEqual(job['state'],'done',job['error'])
        self.assertEqual([m[0] for m in self.robot.moves],['moveJ','moveL','moveL'])
        # Each actual trajectory keeps its own final evidence; later legs cannot overwrite it.
        for leg in ('transit','approach','withdrawal'):
            path=owner.directory/(self.public_body['request_id']+'.'+leg+'.manual-path.json')
            self.assertTrue(path.is_file(),str(path))
            certificate=json.loads(path.read_text())
            self.assertEqual(certificate['outcome'],'completed')
            if leg!='transit':
                self.assertEqual(certificate['binding']['leg'],leg)
                self.assertEqual(certificate['binding']['job_id'],self.public_body['request_id'])
        self.assertFalse(self.robot.connected,'Native transport cleanup precedes ownership release')
        self.assertIsNone(owner.active)
        self.assertEqual(self.request('POST','/api/manual-pick-place',self.public_body)[0],202)
        changed=dict(self.public_body,action='PLACE')
        self.assertEqual(self.request('POST','/api/manual-pick-place',changed)[0],400)
        restarted=JobOwner(self.root)
        saved=restarted.submit(self.public_body['request_id'],'manual-pick-place',
            {k:v for k,v in self.public_body.items() if k!='request_id'},
            lambda _:self.fail('A saved exact request must never execute after restart'))
        self.assertEqual(saved['state'],'done')
        self.assertEqual(len(self.robot.moves),3)


    def prepare_descent(self, endpoint_z=.3, hover_z=.415433583):
        from ur_pick_place.poses import PoseStore
        self.prepare(endpoint_z=endpoint_z)
        self.worker.close();self.robot.connected=True
        self.robot.q=[.4,.1,hover_z,0,math.pi,0]
        self.context.settings['motion_model']='planar'
        self.context.alignment={'id':'exact-alignment-capture','status':'completed','validation_mode':'fixed_z',
            'validation_tcp_pose_m_rad':list(self.robot.q),
            'pick_tcp_pose_m_rad':[.4,.1,endpoint_z,0,math.pi,0]}
        self.worker=self.module.TeacherDescent(ManualRouteTests().settings(),PoseStore(self.root/'descent-poses.json'),
            project=SimpleNamespace(settings={'tcp':self.tcp}),record={'id':'descent-job','validation_mode':'fixed_z'},
            guard=lambda:None,collision=self.scene,check_binding=lambda:None,evidence=self.root/'descent.json',
            factory=lambda host:(self.robot,self.robot),operation='DESCEND',context=self.context)
        self.addCleanup(self.worker.close)
        self.worker.submit('connect');self.wait_idle()

    def test_teacher_descent_is_separate_plan_review_and_moveL_only_without_changing_planar_mode(self):
        self.prepare_descent()
        # A small accepted stopped-position deviation still becomes the actual line start.
        self.robot.q[0]+=.0002;self.robot.q[1]-=.0002
        start=list(self.robot.q)
        self.worker.confirm(self.plan(),True)
        state=self.wait_for(lambda s:s['manual']['stage']=='done' or bool(s['error']))
        self.assertFalse(state['error'],state['error'])
        self.assertEqual([m[0] for m in self.robot.moves],['moveL'])
        target=self.robot.moves[0][1]
        self.assertEqual(target[:2]+target[3:],start[:2]+start[3:])
        self.assertEqual(target[2],.3)
        self.assertEqual(self.context.settings['motion_model'],'planar')
        self.assertEqual(state['manual']['item_state'],'unknown')
        self.assertEqual(self.joint_calls,[])
        self.assertEqual(self.observe_mock.call_count,0,'Separate descent reuses exact completed alignment, not a new marker target')

    def test_current_teacher_taught_height_is_visible_but_below_workspace_refuses_without_clamp(self):
        self.prepare_descent(endpoint_z=.0078922268,hover_z=.115433583)
        original=copy.deepcopy(self.context.alignment)
        self.worker.submit('move_tcp')
        state=self.wait_for(lambda s:bool(s['error']))
        self.assertIn('workspace',state['error'].lower())
        self.assertAlmostEqual(state['manual']['route']['endpoint'][2],.0078922268)
        self.assertAlmostEqual(state['manual']['descent_mm'],107.5413562)
        self.assertEqual(self.context.alignment,original)
        self.assertEqual(self.robot.moves,[])
        self.assertIsNone(state['manual']['prompt'])

    def test_teacher_descent_rechecks_current_alignment_after_manual_plan_pause(self):
        self.prepare_descent()
        prompt=self.plan()
        self.alignment_ok=False
        self.worker.confirm(prompt,True)
        state=self.wait_for(lambda s:bool(s['error']))
        self.assertIn('alignment changed',state['error'])
        self.assertEqual(self.robot.moves,[])

    def test_teacher_descent_cannot_correct_shifted_alignment_with_a_joint_move(self):
        self.prepare_descent();self.robot.q[0]+=.005
        self.worker.submit('move_tcp')
        state=self.wait_for(lambda s:bool(s['error']))
        self.assertIn('alignment',state['error'])
        self.assertEqual(self.robot.moves,[])

    def public_descent(self):
        owner=self.public_server()
        self.robot.q=[.4,.1,.415433583,0,math.pi,0]
        self.context.provenance['project']=str(self.root/'fixture-project')
        grasp_path=str(self.root/'fixture-project/grasp_pick.json')
        self.context.hashes={grasp_path:'current-grasp'}
        aligned={'id':'aligned-capture','status':'completed','validation_mode':'fixed_z',
            'validation_tcp_pose_m_rad':list(self.robot.q),'pick_tcp_pose_m_rad':self.endpoint,
            'grasp_sha256':'current-grasp'}
        owner.jobs['alignment-job']={'id':'alignment-job','module':'teaching','auto_action':'validate_move',
            'state':'done','created':time.time(),'result':{'project':{'project':self.context.provenance['project'],
            'validation':aligned}},'error':None}
        current_project=copy.deepcopy(owner.jobs['alignment-job']['result']['project'])
        owner.teacher_service=SimpleNamespace(state=lambda:{'project':copy.deepcopy(current_project)},close=lambda:None)
        owner.teacher_job='alignment-job'
        alignment_token={'job_id':'alignment-job','capture_id':'aligned-capture','grasp_sha256':'current-grasp','project':self.context.provenance['project']}
        owner.current_alignment=copy.deepcopy(alignment_token)
        self.public_body.update(request_id='teacher-descent-request',action='vertical_descent',
            operator_vertical_approved=True,marker_unchanged=True,alignment_job_id='alignment-job',alignment_capture_id='aligned-capture')
        return owner,current_project,alignment_token

    def test_public_teacher_descent_requires_exact_completed_alignment_and_approval(self):
        owner,current_project,alignment_token=self.public_descent()
        for index,change in enumerate(({'operator_vertical_approved':False},
                                       {'alignment_capture_id':'wrong-capture'},
                                       {'alignment_job_id':'wrong-job'}, {'marker_unchanged':False})):
            invalid=dict(self.public_body,request_id='invalid-descent-'+str(index),**change)
            self.public_body['request_id']=invalid['request_id']
            status,raw=self.request('POST','/api/teaching',invalid)
            self.assertEqual(status,202,raw)
            job=self.wait_public(lambda j:j['state'] not in ('running','waiting'))
            self.assertEqual(job['state'],'failed')
            self.assertEqual(self.robot.moves,[])
            self.assertEqual(owner.current_alignment,alignment_token,'Invalid approvals must not consume current alignment')
        for index,change in enumerate(('current_capture','current_job','no_runtime_token')):
            if change=='current_capture':current_project['validation']['id']='newer-live-capture'
            elif change=='current_job':owner.teacher_job='different-live-alignment-job'
            else:owner.current_alignment=None
            self.public_body['request_id']='stale-live-alignment-'+str(index)
            status,raw=self.request('POST','/api/teaching',self.public_body)
            self.assertEqual(status,202,raw)
            job=self.wait_public(lambda j:j['state']=='failed')
            self.assertEqual(self.robot.moves,[])
            self.assertIsNotNone(owner.teacher_service,'Rejected requests must preserve current alignment eligibility')
            current_project['validation']['id']='aligned-capture'
            owner.teacher_job='alignment-job'
            owner.current_alignment=copy.deepcopy(alignment_token)
        self.public_body['request_id']='teacher-descent-request'
        status,raw=self.request('POST','/api/teaching',self.public_body)
        self.assertEqual(status,202,raw)
        deadline=time.monotonic()+4
        while time.monotonic()<deadline:
            worker=owner.runner
            if worker and worker.state()['manual']['stage']=='awaiting_plan':break
            self.assertNotEqual(owner.get(self.public_body['request_id'])['state'],'failed',
                                owner.get(self.public_body['request_id'])['error'])
            time.sleep(.01)
        else:self.fail('Teacher descent did not reach explicit plan boundary')
        self.assertEqual(self.robot.moves,[])
        reply={'job_id':self.public_body['request_id'],'action':'confirm',
               'prompt':worker.state()['manual']['prompt'],'value':True}
        self.assertEqual(self.request('POST','/api/manual-control',reply)[0],200)
        job=self.wait_public(lambda j:j['state'] not in ('running','waiting'))
        self.assertEqual(job['state'],'done',job['error'])
        self.assertEqual([m[0] for m in self.robot.moves],['moveL'])
        self.assertIsNone(owner.current_alignment,'One admitted descent consumes alignment eligibility')

    def test_public_descent_rechecks_native_capture_after_bound_plan_pause(self):
        owner,current_project,alignment_token=self.public_descent()
        status,raw=self.request('POST','/api/teaching',self.public_body)
        self.assertEqual(status,202,raw)
        job=self.wait_public(lambda j:j['state']=='waiting' and j.get('pending',{}).get('phase')=='plan')
        self.assertEqual(self.robot.moves,[])
        current_project['validation']['id']='newer-native-capture-after-plan'
        status,raw=self.request('POST','/api/manual-control',{'job_id':job['id'],'action':'confirm',
            'prompt':job['pending'],'value':True})
        self.assertIn(status,(200,400),raw)  # The worker may detect the change before the HTTP reply arrives.
        job=self.wait_public(lambda j:j['state']=='failed')
        self.assertIn('alignment',job['error'].lower())
        self.assertEqual(self.robot.moves,[])
        self.assertIsNone(owner.current_alignment,'An interrupted admitted descent cannot reuse alignment')


    def teacher_close_timeout(self, fail_storage=False):
        from unittest import mock
        from anodizing_web.jobs import JobOwner
        owner=self.public_server()
        release,checkpoint,finished=threading.Event(),threading.Event(),threading.Event()
        self.addCleanup(release.set)
        thread=threading.Thread(target=release.wait,daemon=True)
        thread.start()
        original_join=thread.join
        def close():
            if thread.is_alive():
                raise RuntimeError('Teacher worker did not stop')
        service=SimpleNamespace(worker_thread=thread,motion_thread=None,motion=None,close=close)
        owner.teacher_service=service
        original_save=owner._save
        def save(job):
            try:
                if fail_storage and job.get('execution_uncertain'):
                    raise OSError('Injected Teacher fence persistence failure')
                original_save(job)
            finally:
                if job.get('finished'):
                    finished.set();checkpoint.set()
        def controlled_join(timeout=None):
            if timeout is None:
                checkpoint.set()
                return original_join()
        with mock.patch.object(thread,'join',side_effect=controlled_join), \
             mock.patch.object(owner,'_save',side_effect=save):
            try:
                status,raw=self.request('POST','/api/manual-pick-place',self.public_body)
                self.assertEqual(status,202,raw)
                self.assertTrue(checkpoint.wait(3))
                with owner.lock:
                    self.assertTrue(thread.is_alive())
                    self.assertEqual(owner.active,self.public_body['request_id'])
                    self.assertTrue(owner.execution_uncertain)
                    self.assertTrue(owner.jobs[owner.active]['execution_uncertain'])
                if fail_storage:
                    self.assertIn('Injected Teacher fence persistence failure',owner.storage_error)
                self.assertEqual(self.robot.moves,[],'Manual RTDE work cannot start before Teacher cleanup')
                competitor=dict(self.public_body,request_id='competing-manual')
                self.assertEqual(self.request('POST','/api/manual-pick-place',competitor)[0],400)
            finally:
                release.set();original_join(3)
                finished.wait(3)
        with owner.lock:
            self.assertIsNone(owner.active)
        if not fail_storage:
            self.assertTrue(JobOwner(self.root).execution_uncertain)

    def test_teacher_close_timeout_retains_owner_until_teacher_worker_stops(self):
        self.teacher_close_timeout()

    def test_teacher_close_storage_failure_does_not_release_live_worker(self):
        self.teacher_close_timeout(fail_storage=True)

    def test_cleanup_timeout_retains_owner_while_sdk_worker_alive_and_restart_fence(self):
        from unittest import mock
        from anodizing_web.jobs import JobOwner
        owner=self.public_server()
        status,raw=self.request('POST','/api/manual-pick-place',self.public_body)
        self.assertEqual(status,202,raw)
        job_id=self.public_body['request_id']
        deadline=time.monotonic()+4
        while time.monotonic()<deadline:
            worker=owner.runner
            if worker and worker.state()['manual']['stage']=='awaiting_plan':break
            self.assertNotEqual(owner.get(job_id)['state'],'failed',owner.get(job_id)['error'])
            time.sleep(.01)
        else:self.fail('Manual plan was not ready')
        for phase in ('plan','grip'):
            deadline=time.monotonic()+4
            while time.monotonic()<deadline:
                state=worker.state()
                if state['manual']['stage']=='awaiting_'+phase:break
                self.assertFalse(state['error'],state['error']);time.sleep(.01)
            else:self.fail('Manual '+phase+' boundary missing')
            if phase=='grip':break
            worker.confirm(state['manual']['prompt'],True)
        release=threading.Event();self.addCleanup(release.set)
        self.robot.cleanup_gate=release
        checkpoint=threading.Event()
        original_join=worker._worker.join
        original_save=owner._save
        def save(job):
            original_save(job)
            if job.get('finished'):
                checkpoint.set()
        def controlled_join(timeout=None):
            if timeout is None:
                checkpoint.set()
                return original_join()
            # Simulate an expired bounded wait while the SDK gate remains closed.
        with mock.patch.object(worker._worker,'join',side_effect=controlled_join), \
             mock.patch.object(owner,'_save',side_effect=save):
            try:
                worker.confirm(state['manual']['prompt'],True)
                self.assertTrue(self.robot.cleanup_entered.wait(3),'Native cleanup must be attempted')
                self.assertTrue(checkpoint.wait(3),'Reach retained drain or terminal finalization')
                with owner.lock:
                    self.assertTrue(worker._worker.is_alive())
                    self.assertEqual(owner.active,job_id,'A live SDK worker retains the shared owner')
                    self.assertTrue(owner.execution_uncertain,'A close timeout must set the uncertainty fence')
                    import json
                    saved=json.loads((owner.directory/(job_id+'.json')).read_text())
                    self.assertTrue(saved['execution_uncertain'])
                competitor=dict(self.public_body,request_id='competing-manual')
                self.assertEqual(self.request('POST','/api/manual-pick-place',competitor)[0],400)
            finally:
                release.set();original_join(3)
        self.assertFalse(worker._worker.is_alive())
        job=self.wait_public(lambda j:bool(j.get('finished')))
        self.assertIsNone(owner.active)
        self.assertEqual(job['state'],'failed')
        self.assertTrue(job['execution_uncertain'])
        restarted=JobOwner(self.root)
        self.assertTrue(restarted.execution_uncertain)
        with self.assertRaisesRegex(ValueError,'uncertain|reconciliation'):
            restarted.submit('fresh-after-restart','manual-pick-place',{'action':'PICK'},lambda _:None)


class ManualLoaderTests(unittest.TestCase):
    def setUp(self):
        from test_manual_pick_place import ManualDataTests
        ManualDataTests.setUp(self)

    def test_native_loader_requires_four_pixel_reference_preserves_sources_and_binds_hashes(self):
        import json
        from unittest import mock
        from ur_pick_place.vision.handeye import HandEyeCalibration
        module=executor_module(self)
        # This test explicitly bypasses hardware qualification for a synthetic file fixture.
        # No SDK/camera exists; only loader quality, provenance and readonly behavior are tested.
        provenance=module.prepare(self.root,self.body)
        provenance['simulation_source']=False
        path=self.root/'project/observation.json'
        data=json.loads(path.read_text())
        with mock.patch.object(module,'prepare',return_value=provenance), \
             mock.patch.object(HandEyeCalibration,'require_hardware'):
            for value in (None,3.99,True):
                data['quality']['pixels_per_module']=value
                path.write_text(json.dumps(data))
                with self.subTest(value=value),self.assertRaisesRegex(ValueError,'4 pixels/module'):
                    module.load_context(self.root,dict(self.body,action='PICK'))
            data['quality']['pixels_per_module']=4.0
            path.write_text(json.dumps(data))
            before={str(p):p.read_bytes() for p in self.root.rglob('*') if p.is_file()}
            pick=module.load_context(self.root,dict(self.body,action='PICK'))
            place=module.load_context(self.root,dict(self.body,action='PLACE'))
            self.assertEqual(pick.model.role,'pick');self.assertEqual(place.model.role,'place')
            self.assertEqual(pick.model.marker_tcp.tolist(),place.model.marker_tcp.tolist())
            self.assertFalse(pick.config.pick.refine.enabled)
            self.assertGreaterEqual(pick.config.quality.min_pixels_per_module,3)
            self.assertEqual(before,{str(p):p.read_bytes() for p in self.root.rglob('*') if p.is_file()})
            pick.check_inputs();place.check_inputs()
            grasp=self.root/'project/grasp_pick.json'
            grasp.write_bytes(grasp.read_bytes()+b' ')
            with self.assertRaisesRegex(ValueError,'changed'):pick.check_inputs()
