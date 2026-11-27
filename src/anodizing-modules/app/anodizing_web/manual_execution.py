"""Manual IPC sequence on the existing native SDK worker; no gripper controller."""
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import replace
import hashlib
import json
import math
from pathlib import Path
import threading
import time
from types import SimpleNamespace
import uuid

import numpy as np
from ur_pick_place.config import number, vector
from ur_pick_place.manual import ManualSettings
from ur_pick_place.poses import PoseStore
from ur_pick_place.vision.transforms import pose_to_matrix, matrix_to_pose, invert
from ur_pick_place.vision.validation import pose_error

from .jobs import atomic_json
from .manual_pick_place import ManualPhases, prepare, vertical_intent
from .paths import check_tree, inside
from .teacher_motion import TeacherJointMotion, native_backend


def validate_route(route, settings):
    endpoint = vector(route['endpoint'], 6, 'Manual endpoint')
    for name in ('endpoint', 'above', 'withdrawn'):
        pose = vector(route[name], 6, name)
        if pose[:2] + pose[3:] != endpoint[:2] + endpoint[3:]:
            raise ValueError('Vertical legs must hold endpoint XY and orientation.')
        if any(not a <= v <= b for a, v, b in zip(settings.workspace_min_m, pose[:3], settings.workspace_max_m)):
            raise ValueError(f'{name} Z {pose[2]*1000:.3f} mm / XYZ {pose[:3]} is outside workspace '
                             f'{settings.workspace_min_m} to {settings.workspace_max_m}; no clamping.')
    if route['above'][2] <= endpoint[2] or not math.isclose(route['withdrawn'][2]-endpoint[2], .1, abs_tol=1e-12):
        raise ValueError('Require a positive descent and the full straight +100 mm withdrawal.')


def load_context(root, body):
    from ur_pick_place.vision.config import VisionConfig
    from ur_pick_place.vision.grasp import load_taught
    from ur_pick_place.vision.handeye import load_handeye
    from ur_pick_place.vision.targets import target_from_config
    provenance = prepare(root, body)
    if provenance['simulation_source']:
        raise ValueError('Synthetic teaching/calibration cannot authorize hardware Pick/Place.')
    project = Path(provenance['project'])
    saved = json.loads((project/'project.json').read_text())
    settings = dict(saved['settings'])
    settings['calibration'] = str(inside(root, body['calibration']))
    settings['target'] = dict(settings['target'], board_manifest=str(inside(root, body.get('manifest') or settings['target']['board_manifest'])))
    paths = [project/name for name in ('project.json','grasp_pick.json','observation.json')]
    paths += [Path(settings['calibration']), Path(settings['target']['board_manifest'])]
    hashes = {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in paths}
    def check_inputs():
        for name, expected in hashes.items():
            path = inside(root, name)
            if hashlib.sha256(path.read_bytes()).hexdigest() != expected:
                raise ValueError('Manual teaching/calibration/marker input changed; start a new operation.')
    calibration = load_handeye(settings['calibration'])
    calibration.require_hardware()
    camera_record = json.loads(Path(settings['calibration']).read_text()).get('camera',{})
    stream = camera_record.get('stream',{})
    provider = settings.get('camera_provider','realsense')
    if camera_record.get('provider') and camera_record['provider'] != provider:
        raise ValueError('Selected camera provider differs from the calibration camera.')
    role = body['action'].lower()
    observed = json.loads((project/'observation.json').read_text())
    reference_ppm = observed.get('quality',{}).get('pixels_per_module')
    if type(reference_ppm) not in (int,float) or not math.isfinite(reference_ppm) or reference_ppm < 4:
        raise ValueError('Teaching reference needs at least 4 pixels/module; re-teach with a qualified reference.')
    role_config = {'target':settings['target'], 'observe_joints':observed['robot']['joints_rad'],
                   'motion_model':settings.get('motion_model','full'), 'limits':settings.get('limits',{}),
                   'refine':{'enabled':False}}
    config = VisionConfig(handeye_calibration=settings['calibration'], pick=role_config, place=deepcopy(role_config),
        camera={'provider':provider, 'serial':calibration.camera_serial,
                'width':calibration.intrinsics['width'], 'height':calibration.intrinsics['height'], 'fps':stream.get('fps',15),
                'exposure_time_us':camera_record.get('exposure_time_us'),
                'gain_db':camera_record.get('gain_db')},
        quality={'min_pixels_per_module':max(3., settings.get('min_pixels_per_module',3.))},
        robot={'active_tcp_offset':settings['tcp']}, base_dir=str(project))
    return SimpleNamespace(settings=settings, config=config, calibration=calibration,
        model=replace(load_taught(project/'grasp_pick.json'), role=role), target=target_from_config(settings['target']),
        provenance=provenance, check_inputs=check_inputs, role=role, approach_mm=settings['approach_mm'], hashes=hashes,
        agv_station=(saved.get('agv') or {}).get('station'))


def observe_role(context, control, receive, tick):
    from ur_pick_place.vision.observe import Observer, open_camera, check_camera_identity
    from ur_pick_place.vision.robot_state import ReceivePoseSource, same_tcp
    source = ReceivePoseSource(receive, context.calibration, context.settings['tcp'], context.config.robot)
    snapshot = source.snapshot
    def checked_snapshot():
        tick()
        if not same_tcp(control.getTCPOffset(), context.settings['tcp']):
            raise ValueError('Active TCP changed during manual observation.')
        return snapshot()
    source.snapshot = checked_snapshot
    camera = open_camera(context.config.camera)
    try:
        check_camera_identity(camera, context.calibration)
        return Observer(context.config, context.calibration, camera, source, tick=tick).look(context.role, allow_motion=False)
    finally:
        camera.close()


def rebind_vertical(scene, q0, tcp, target, workspace_min, workspace_max, binding, certificate):
    from ur_handeye.cartesian import binding_matches
    return binding_matches(certificate,scene,q0,pose_to_matrix(tcp),pose_to_matrix(target),
        joint_bounds=binding['joint_bounds_rad'],
        binding=dict(binding,workspace_min_m=workspace_min,workspace_max_m=workspace_max),
        command_start=pose_to_matrix(binding['actual_start_tcp']))


def check_vertical(scene, q0, tcp, target, workspace_min, workspace_max, binding):
    from ur_handeye.cartesian import check_vertical as checker
    return checker(scene,q0,pose_to_matrix(tcp),pose_to_matrix(target),
        joint_bounds=binding['joint_bounds_rad'],
        binding=dict(binding,workspace_min_m=workspace_min,workspace_max_m=workspace_max),
        command_start=pose_to_matrix(binding['actual_start_tcp']))


class ManualExecution(TeacherJointMotion):
    def __init__(self, *args, operation, context, emit=None, **kwargs):
        self.base_evidence = Path(kwargs['evidence'])
        self.operation, self.context = operation, context
        self.emit = emit or (lambda state: None)
        self.cancelled = threading.Event()
        self.reply = threading.Event()
        self.manual = {'stage':'connecting', 'prompt':None, 'item_state':'unknown', 'route':None,'operation':operation}
        self.phases = None
        self.plan_approved = False
        super().__init__(*args, **kwargs)

    def state(self):
        state = super().state()
        with self._lock:
            state['manual'] = deepcopy(self.manual)
        return state

    def stop(self):
        self.cancelled.set()
        self.reply.set()
        super().stop()

    def confirm(self, prompt, value):
        with self._lock:
            if (self.cancelled.is_set() or value is not True or not self.manual['prompt'] or prompt != self.manual['prompt']):
                raise ValueError('Reply does not match the current manual job and phase.')
            if prompt['phase'] == 'plan':
                self.plan_approved = True
                self.manual['prompt'] = None
            else:
                self.phases.confirm(prompt, value)
                self.manual.update(self.phases.snapshot())
            # Consume before waking the worker: duplicate replies can never move twice.
            self.reply.set()

    def _tick(self):
        if self.cancelled.is_set() or self._shutdown.is_set():
            raise ValueError('Manual operation stopped; no resume.')
        self.guard()
        self.check_binding()
        self.context.check_inputs()
        if self.operation == 'DESCEND':
            self.context.check_alignment()

    def _publish(self, **values):
        with self._lock:
            self.manual.update(values)
            state = deepcopy(self.manual)
        atomic_json(self.base_evidence.with_suffix('.manual-state.json'), state)
        self.emit(state)

    def _pause(self, phase):
        self.reply.clear()
        if phase == 'plan':
            prompt = {key:self.phases.state[key] for key in ('job_id','role','observation_id')}
            prompt.update(phase='plan', id=uuid.uuid4().hex)
        else:
            prompt = self.phases.snapshot()['prompt']
        self._publish(stage='awaiting_'+phase, prompt=prompt)
        while not self.reply.wait(.05):
            self._tick()
            feedback = self._read()
            if feedback['safety_mode'] not in (1,2) or self.control.isSteady() is not True:
                raise ValueError('Robot changed state while waiting for manual confirmation.')
        self._tick()
        self._publish(prompt=None)

    def _wait_move(self):
        while self._motion:
            self._tick()
            self._poll()
            time.sleep(.02)

    def _interruption_details(self):
        return {}

    def _execute(self, command, payload, generation):
        if command != 'move_tcp':
            return super()._execute(command,payload,generation)
        try:
            self._manual_sequence(generation)
        except BaseException as exc:
            # Native stop must precede filesystem or owner persistence.
            try:
                self._stop_motion()
            except BaseException as stop_error:
                self.cleanup_uncertain = True
                exc = RuntimeError(f'{exc}; stop failed: {stop_error}')
            if self.phases:
                self.phases.interrupt(exc)
            self._publish(stage='interrupted', item_state='unknown', prompt=None, error=str(exc),
                          **self._interruption_details())
            raise exc

    def _dispatch_validation(self, target, after, generation):
        self.evidence = self.base_evidence.with_name(self.record['id']+'.transit.manual-path.json')
        return super()._dispatch_validation(target,after,generation)

    def _make_phases(self, initial, observation_id):
        return ManualPhases(self.record['id'], self.operation, initial, observation_id)

    def _before_approach(self):
        pass

    def _manual_sequence(self, generation):
        from ur_pick_place.vision.grasp import target_pose
        self._tick()
        self._publish(stage='observing')
        observation = observe_role(self.context, self.control, self.receive, self._tick)
        if observation.role != self.operation.lower() or not 0 <= time.time()-observation.timestamp <= 60:
            raise ValueError('A fresh matching role observation is required before each operation.')
        ppm = observation.quality.get('pixels_per_module')
        if type(ppm) not in (int,float) or not math.isfinite(ppm) or ppm < 3:
            raise ValueError('Fresh pose observation needs at least 3 pixels/module.')
        pose, report = target_pose(self.context.model, observation, self.context.config.role(observation.role),
            calibration_sha256=self.context.calibration.sha256, tcp_offset=self.context.settings['tcp'], target=self.context.target)
        observation_id = uuid.uuid4().hex
        atomic_json(self.base_evidence.with_suffix('.observation.json'), observation.to_json())
        route = vertical_intent(matrix_to_pose(pose), self.context.approach_mm)
        self.record.update(timestamp=observation.timestamp, observation_id=observation_id)
        initial = self.record.get('initial_item_state', 'operator_confirmed_released' if self.operation=='PICK' else 'operator_confirmed_held')
        self.phases = self._make_phases(initial, observation_id)
        self._publish(**self.phases.snapshot(), route=route, validation=report,
            provenance=self.context.provenance, workspace_min_m=self.settings.workspace_min_m,
            workspace_max_m=self.settings.workspace_max_m, speed_mm_s=self.settings.tcp_speed_mm_s,
            joint_speed_deg_s=math.degrees(self.settings.joint_speed_rad_s))
        validate_route(route,self.settings)
        self._pause('plan')
        if not self.plan_approved or not 0 <= time.time()-observation.timestamp <= 60:
            raise ValueError('Fresh route approval expired; start a new capture/operation.')
        before = self._read()
        self._workspace(before['tcp_pose_m_rad'])
        if (max(abs(a-b) for a,b in zip(before['joints_rad'],observation.robot['joints_rad'])) > 2e-4 or
                pose_error(before['tcp_pose_m_rad'], observation.robot['tcp_pose_m_rad'])['position_mm'] > 1):
            raise ValueError('Robot moved since fresh observation; start a new operation.')
        self.phases.advance('observation_accepted')
        self._publish(stage='aligning', trajectory='checked_joint_transit_to_hover; TCP height may vary')
        self._dispatch_validation(route['above'],before,generation)
        self._wait_move()
        self.phases.advance('alignment_complete')
        self._publish(stage='approaching', trajectory='moveL fixed-orientation base -Z')
        actual_hover = self._read()['tcp_pose_m_rad']
        error = pose_error(actual_hover,route['above'])
        if error['position_mm'] > 1 or error['rotation_deg'] > .1:
            raise ValueError('Aligned hover is outside the reviewed 1 mm / 0.1 degree tolerance.')
        command_endpoint = list(actual_hover); command_endpoint[2] = route['endpoint'][2]
        self._publish(command_endpoint=command_endpoint,
            alignment_error=pose_error(actual_hover,route['above']),
            line_contract='Hold actual stopped hover XY/orientation; descend to reviewed taught Z without shortening.')
        self._before_approach()
        self._linear(command_endpoint, generation, 'approach')
        self._wait_move()
        self.phases.advance('approach_complete')
        self._pause('grip' if self.operation=='PICK' else 'release')
        self._publish(**self.phases.snapshot(), trajectory='moveL fixed-orientation base +Z 100 mm')
        # Full 100 mm from fresh actual stopped endpoint; never shorten a withdrawal.
        actual = self._read()['tcp_pose_m_rad']
        withdrawn = list(actual); withdrawn[2] += .100
        self._workspace(withdrawn)
        self._linear(withdrawn,generation,'withdrawal')
        self._wait_move()
        self.phases.advance('withdrawal_complete')
        self._publish(**self.phases.snapshot(), actual_tcp_pose_m_rad=self._read()['tcp_pose_m_rad'])

    def _linear(self,target,generation,leg):
        self._tick()
        after = self._read()
        reviewed_target = list(target)
        target = list(after['tcp_pose_m_rad'])
        target[2] = after['tcp_pose_m_rad'][2]+.100 if leg == 'withdrawal' else reviewed_target[2]
        self.evidence = self.base_evidence.with_name(self.record['id']+'.'+leg+'.manual-path.json')
        self._workspace(after['tcp_pose_m_rad']); self._workspace(target)
        if (self.control.isSteady() is not True or after['safety_mode'] not in (1,2)
                or max(abs(v) for v in vector(self.receive.getActualQd(),6,'Joint velocity')) > .001):
            raise ValueError('Wait for normal/reduced stationary robot before moveL.')
        if self.control.isPoseWithinSafetyLimits(target) is not True:
            raise ValueError('Vertical endpoint is outside controller safety limits.')
        expected = self.manual['route']['above' if leg=='approach' else 'endpoint']
        error = pose_error(after['tcp_pose_m_rad'],expected)
        if error['position_mm'] > 1 or error['rotation_deg'] > .1:
            raise ValueError('Robot did not reach the reviewed vertical start; no joint substitution.')
        binding = {'job_id':self.record['id'], 'role':self.operation.lower(), 'observation_id':self.record['observation_id'],
            'robot_host':self.settings.robot_ip, 'calibration_sha256':self.context.calibration.sha256,
            'joint_bounds_rad':self.context.joint_bounds_rad, 'joint_bounds_source':self.context.joint_bounds_source,
            'inputs':getattr(self.context,'hashes',{}), 'held_geometry':('no_grip_or_held_item_action' if self.operation=='DESCEND' else 'operator_confirmed_inside_selected_tool_box'),
            'leg':leg, 'speed_m_s':self.settings.tcp_speed_mm_s/1000, 'accel_m_s2':self.settings.tcp_accel_m_s2,
            'command':'moveL', 'unblended':True, 'actual_start_tcp':after['tcp_pose_m_rad'], 'requested_target_tcp':list(target)}
        self._publish(stage='checking_'+leg, command_start=after['tcp_pose_m_rad'],command_target=target,
            trajectory='moveL fixed-orientation base Z; whole-line conditional geometry check')
        certificate = check_vertical(self.collision, after['joints_rad'], self.context.settings['tcp'],target,
            self.settings.workspace_min_m,self.settings.workspace_max_m,binding)
        self.path_certificate = dict(certificate, outcome='not_dispatched')
        atomic_json(self.evidence,self.path_certificate)
        if (certificate.get('status') != 'clear_in_model' or certificate.get('binding') != dict(binding,
                workspace_min_m=self.settings.workspace_min_m,workspace_max_m=self.settings.workspace_max_m)):
            raise ValueError('Vertical path refused: '+str(certificate.get('status'))+'; '+str(certificate.get('reason','binding mismatch')))
        balls = certificate.get('balls',[])
        points = [after['joints_rad']]
        if balls:
            points.append(balls[len(balls)//2]['center_joints'])
        points.append(certificate.get('endpoint_joints',after['joints_rad']))
        self._publish(stage='checking_controller_'+leg,controller_sanity_samples=len(points),
            checked_geometry_balls=len(balls), geometry_elapsed_s=certificate.get('elapsed_s'))
        sanity_started = time.monotonic()
        for point in points:
            self._tick()
            actual, model = self._flange(point), self.collision.flange(point)
            if (not np.isfinite(actual).all() or not np.isfinite(model).all()
                    or np.linalg.norm(actual[:3,3]-model[:3,3]) > self.collision.data['model_uncertainty_m']
                    or pose_error(matrix_to_pose(actual),matrix_to_pose(model))['rotation_deg'] > .1
                    or self.control.isJointsWithinSafetyLimits(point) is not True):
                raise ValueError('Controller/model FK or controller joint admission differs; moveL refused.')
        self.path_certificate['controller_sanity_elapsed_s'] = time.monotonic()-sanity_started
        # FK samples detect mismatches; the certificate states the separate whole-route correspondence assumption.
        progress = self.control.getAsyncOperationProgressEx()
        self._tick()
        self._publish(stage='descending' if self.operation=='DESCEND' else ('approaching' if leg=='approach' else 'withdrawing'))
        self.path_certificate['outcome']='dispatching'
        atomic_json(self.evidence,self.path_certificate)
        self._tick()
        latest_progress = self.control.getAsyncOperationProgressEx()
        latest = self._read()
        drift = pose_error(latest['tcp_pose_m_rad'],after['tcp_pose_m_rad'])
        if (latest_progress.isAsyncOperationRunning() or latest_progress.operationId()!=progress.operationId()
                or latest_progress.changeCount()!=progress.changeCount()
                or self.control.isSteady() is not True or latest['safety_mode'] not in (1,2)
                or max(abs(v) for v in vector(self.receive.getActualQd(),6,'Joint velocity')) > .001
                or max(abs(a-b) for a,b in zip(after['joints_rad'],latest['joints_rad'])) > 2e-4
                or drift['position_mm'] > .1 or drift['rotation_deg'] > .01):
            self.path_certificate['outcome']='not_dispatched'; atomic_json(self.evidence,self.path_certificate)
            raise ValueError('Starting pose/controller state changed during Cartesian check.')
        self._tick()
        if not rebind_vertical(self.collision,after['joints_rad'],self.context.settings['tcp'],target,
                self.settings.workspace_min_m,self.settings.workspace_max_m,binding,certificate):
            self.path_certificate['outcome']='not_dispatched'
            atomic_json(self.evidence,self.path_certificate)
            raise ValueError('Cartesian certificate binding changed before dispatch.')
        with self._lock:
            if generation != self._generation or self.cancelled.is_set():
                raise ValueError('Manual movement stopped before dispatch.')
            self._motion={'mode':'tcp','before':latest_progress,'operation':(latest_progress.operationId()+1)&127,
                          'target':list(target),'deadline':time.monotonic()+120}
        try:
            if self.control.moveL(target,self.settings.tcp_speed_mm_s/1000,self.settings.tcp_accel_m_s2,True) is not True:
                raise RuntimeError('Controller rejected native moveL.')
        except BaseException:
            self.cleanup_uncertain=True
            raise

    def _poll(self):
        # Accepted IPC tasks survive laptop loss; the next manual prompt holds.
        self._heartbeat_at=time.monotonic()
        if not self._motion or self._motion['mode']=='joint':
            return super()._poll()
        feedback=self._read(); self._tick()
        motion=self._motion
        if feedback['safety_mode'] not in (1,2) or time.monotonic()>motion['deadline']:
            raise RuntimeError('Safety mode changed or vertical movement timed out.')
        status=self.control.getAsyncOperationProgressEx()
        if status.operationId()==motion['before'].operationId() and status.changeCount()==motion['before'].changeCount():
            return
        if status.operationId()!=motion['operation']:
            self.cleanup_uncertain=True
            raise RuntimeError('Unexpected native controller operation identity.')
        if status.isAsyncOperationRunning() or self.control.isSteady() is not True or max(abs(v) for v in vector(self.receive.getActualQd(),6,'joint velocity'))>.001:
            return
        error=pose_error(feedback['tcp_pose_m_rad'],motion['target'])
        if error['position_mm']>1 or error['rotation_deg']>.1:
            raise RuntimeError('Robot stopped away from checked vertical endpoint.')
        self.path_certificate.update(outcome='completed',actual_tcp_pose_m_rad=feedback['tcp_pose_m_rad'],actual_joints=feedback['joints_rad'])
        atomic_json(self.evidence,self.path_certificate)
        self._motion=None

    def _stop_motion(self):
        if not self._motion or self._motion['mode']=='joint':
            return super()._stop_motion()
        if self._stop_attempted:
            raise RuntimeError('Previous vertical stop remains unconfirmed.')
        self._stop_attempted=True
        before=None
        try:
            try:
                before=self.control.getAsyncOperationProgressEx()
            finally:
                self.control.stopL(1.,True)
            if before is None:
                raise RuntimeError('Cannot identify vertical stop operation.')
            deadline=time.monotonic()+3
            while time.monotonic()<deadline:
                feedback=self._read(); status=self.control.getAsyncOperationProgressEx()
                if (status.operationId()==((before.operationId()+1)&127) and status.changeCount()!=before.changeCount()
                        and not status.isAsyncOperationRunning() and self.control.isSteady() is True
                        and max(abs(v) for v in vector(self.receive.getActualQd(),6,'joint velocity'))<=.001):
                    self.path_certificate.update(outcome='stopped',actual_tcp_pose_m_rad=feedback['tcp_pose_m_rad'])
                    atomic_json(self.evidence,self.path_certificate)
                    self._motion=None
                    return
                time.sleep(.02)
            raise RuntimeError('Vertical stop confirmation timed out.')
        except BaseException:
            self.cleanup_uncertain=True
            if self.path_certificate:
                self.path_certificate['outcome']='execution_uncertain'; atomic_json(self.evidence,self.path_certificate)
            raise


class TeacherDescent(ManualExecution):
    """One separately approved descent after a completed planar alignment."""
    def _manual_sequence(self, generation):
        self._tick()
        before = self._read()
        aligned = self.context.alignment
        expected = aligned['validation_tcp_pose_m_rad']
        error = pose_error(before['tcp_pose_m_rad'], expected)
        if error['position_mm'] > 1 or error['rotation_deg'] > .1:
            raise ValueError('Current TCP differs from the completed alignment; align again.')
        # The requested line holds the fresh actual start XY/orientation and ends
        # at the current taught height. This is not a joint correction near the rod.
        endpoint = list(before['tcp_pose_m_rad'])
        endpoint[2] = aligned['pick_tcp_pose_m_rad'][2]
        if endpoint[2] >= before['tcp_pose_m_rad'][2]:
            raise ValueError('The taught height must be below the aligned TCP.')
        route = {'above':list(before['tcp_pose_m_rad']), 'endpoint':endpoint}
        self.record['observation_id'] = aligned['id']
        self._publish(stage='reviewing_descent', route=route, item_state='unknown',
            taught_pick_tcp_pose_m_rad=aligned['pick_tcp_pose_m_rad'],
            descent_mm=(before['tcp_pose_m_rad'][2]-endpoint[2])*1000,
            workspace_min_m=self.settings.workspace_min_m, workspace_max_m=self.settings.workspace_max_m,
            speed_mm_s=self.settings.tcp_speed_mm_s)
        self._workspace(before['tcp_pose_m_rad']); self._workspace(endpoint)
        self.phases = ManualPhases(self.record['id'],'PICK','no_confirmed_item',aligned['id'])
        self._pause('plan')
        self._publish(stage='descending', trajectory='moveL fixed-orientation base -Z to current taught height')
        self._linear(endpoint,generation,'approach')
        self._wait_move()
        self._publish(stage='done',prompt=None,item_state='unknown',actual_tcp_pose_m_rad=self._read()['tcp_pose_m_rad'])


def control(owner, body):
    job_id = body.get('job_id')
    if body.get('action') == 'cancel-unconfirmed':
        return owner.cancel_unconfirmed(job_id)
    # Latch before any owner lock or disk I/O, including a stalled fsync.
    runner = owner.runner
    if body.get('action') == 'stop' and owner.active == job_id and isinstance(runner,ManualExecution):
        runner.stop()
    with owner.lock:
        job = owner.jobs.get(job_id)
        if not job or owner.active != job_id or job.get('auto_action') not in ('PICK','PLACE','REPEAT','vertical_descent','STEP'):
            raise ValueError('This manual job is no longer active; no resume is available.')
        if body.get('action') == 'stop':
            job['manual_cancelled'] = True
            try:
                owner._save(job)
            except BaseException as exc:
                owner.storage_error = str(exc)
                owner.execution_uncertain = True
                job['execution_uncertain'] = True
                raise
        elif body.get('action') == 'confirm':
            owner.check_agv()
            if owner.mcu_pending or owner.execution_uncertain or owner.storage_error:
                raise ValueError('MCU work is pending or execution/storage is uncertain. Inspect the outcome before confirmation.')
            if not isinstance(owner.runner,ManualExecution):
                raise ValueError('No manual prompt is ready.')
            owner.runner.confirm(body.get('prompt'),body.get('value'))
        else:
            raise ValueError('Choose manual confirm or stop.')
        return owner.get(job_id)


def run(owner, root, body, job_id):
    worker = None
    @contextmanager
    def worker_owner_lock():
        # Slow persistence must not trap the SDK worker ahead of native Stop.
        lock = owner.lock
        while True:
            if worker and (worker.cancelled.is_set() or worker._shutdown.is_set()):
                raise ValueError('Manual operation stopped; no resume.')
            if lock.acquire(timeout=.02):
                break
        try:
            if worker and (worker.cancelled.is_set() or worker._shutdown.is_set()):
                raise ValueError('Manual operation stopped; no resume.')
            yield
        finally:
            lock.release()
    operation = body.get('action')
    descent = operation == 'vertical_descent'
    repeat = operation == 'REPEAT'
    step = operation == 'STEP'
    for field in (() if step else ('operator_path_approved','tcp_confirmed') + (() if descent else ('held_envelope_confirmed',))):
        if body.get(field) is not True:
            raise ValueError('Explicit operator confirmation required: '+field)
    if not owner.allow_manual_pick_place or not owner.robot_model:
        raise ValueError('Reviewed manual movement requires --allow-manual-pick-place and the factory robot model.')
    if operation not in ('PICK','PLACE','REPEAT','vertical_descent','STEP'):
        raise ValueError('Unknown manual operation.')
    if descent and (body.get('operator_vertical_approved') is not True or body.get('marker_unchanged') is not True):
        raise ValueError('Separately approve this vertical descent; the old alignment Run cannot descend.')
    context_body = dict(body,action='PICK' if descent else operation)
    if descent:
        board = body.get('pick_tag')
        try:
            project = check_tree(root, inside(root, body.get('project')))
            saved = json.loads((project/'project.json').read_text())
            saved_board = (saved.get('settings', {}).get('target') or {}).get('board')
            if isinstance(saved_board, str) and saved_board:
                board = saved_board
        except (OSError, ValueError, KeyError, TypeError):
            pass
        if isinstance(board, str) and board:
            context_body.update(pick_tag=board,place_tag=board,reuse_taught_offset=True)
    if step:
        from .robot_control import load_context as load_step, StepExecution, native_backend as step_backend
        context = load_step(root, body)
    elif repeat:
        from .repeat_pick_place import load_recipe, RepeatExecution
        recipe = load_recipe(root, body)
        context = recipe.contexts['A','PICK']
    else:
        context = load_context(root,context_body)
        visit = owner.agv_visit
        station = getattr(context, 'agv_station', None)
        if station is not None and visit and not visit.switched_off:
            visit.require_endpoint(station)
    if descent:
        with owner.lock:
            aligned_job = owner.jobs.get(body.get('alignment_job_id')) or {}
            aligned_project = (aligned_job.get('result') or {}).get('project') or {}
            aligned = aligned_project.get('validation') or {}
        if (aligned_job.get('module') != 'teaching' or aligned_job.get('auto_action') != 'validate_move'
                or aligned_job.get('state') != 'done' or aligned.get('status') != 'completed'
                or aligned.get('validation_mode') != 'fixed_z' or aligned.get('id') != body.get('alignment_capture_id')
                or str(aligned_project.get('project')) != str(context.provenance['project'])
                or aligned.get('grasp_sha256') != context.hashes[str(Path(context.provenance['project'])/'grasp_pick.json')]):
            raise ValueError('Select the completed alignment for the current taught grasp and capture.')
        expected_alignment = {'job_id':aligned_job['id'],'capture_id':aligned['id'],
            'grasp_sha256':aligned['grasp_sha256'],'project':str(context.provenance['project'])}
        alignment_service = owner.teacher_service
        if not alignment_service or owner.teacher_job != aligned_job['id']:
            raise ValueError('Alignment is no longer the current Teacher session; complete a new alignment.')
        def check_alignment():
            current = ((alignment_service.state().get('project') or {}).get('validation') or {})
            with worker_owner_lock():
                if (owner.current_alignment != expected_alignment or current.get('id') != aligned['id']
                        or current.get('status') != 'completed' or current.get('validation_mode') != 'fixed_z'
                        or current.get('grasp_sha256') != aligned['grasp_sha256']):
                    raise ValueError('Alignment is no longer current; capture and complete a new alignment. Confirm the dummy/rod stayed fixed.')
        check_alignment()
        context.check_alignment = check_alignment
        context.alignment = deepcopy(aligned)
    elif not repeat and not step:
        allowed = {'PICK':('no_confirmed_item','operator_confirmed_released'),'PLACE':('operator_confirmed_held',)}
        if body.get('initial_item_state') not in allowed[operation]:
            raise ValueError('Confirm the current held/released item state for this operation.')
    bounds = body.get('joint_bounds_rad')
    source = body.get('joint_bounds_source')
    if (not isinstance(bounds,list) or len(bounds)!=6 or not isinstance(source,str) or not source.strip()):
        raise ValueError('Supply the reviewed controller operating joint bounds and their source; Scene input bounds are not controller limits.')
    context.joint_bounds_rad = [vector(pair,2,'Operating joint bounds') for pair in bounds]
    if any(not -4*math.pi <= lo < hi <= 4*math.pi for lo,hi in context.joint_bounds_rad):
        raise ValueError('Operating joint bounds must be ordered within the Scene numerical domain.')
    context.joint_bounds_source = source.strip()
    from ur_handeye.collision import load_scene
    from .auto_scene import check
    binding = owner.jobs[job_id].get('auto_scene')
    if not binding or not binding.get('model'):
        raise ValueError('Select the reviewed collision scene and factory robot model.')
    check(root,binding)
    scene = load_scene(binding['snapshot'],model_path=binding['model']['snapshot'])
    scene.require_motion_ready()
    if not descent and not step:
        tools = [box for box in scene.data.get('attached',[]) if box.get('name')=='tool']
        if len(tools)!=1 or not np.allclose(sorted(tools[0].get('dimensions_m',[])),sorted([.390,.080,.100]),rtol=0,atol=1e-9):
            raise ValueError('Select the reviewed existing 390 x 80 x 100 mm attached tool box covering this demo item.')
    if scene.model['nominal']:
        raise ValueError('A factory robot model is required.')
    if body.get('workspace_min_m') is None or body.get('workspace_max_m') is None:
        raise ValueError('Enter reviewed workspace bounds; no workspace defaults are applied.')
    settings = ManualSettings(robot_ip=context.settings['robot_ip'],
        workspace_min_m=body['workspace_min_m'],
        workspace_max_m=body['workspace_max_m'],
        tcp_speed_mm_s=number(body.get('speed_mm_s',10),'Vertical speed (mm/s)',low=1,high=20),
        tcp_accel_m_s2=.05, joint_speed_rad_s=math.radians(number(body.get('joint_speed_deg_s',3),'Joint speed (degrees/s)',low=1,high=6)),
        joint_accel_rad_s2=.1)
    def guard():
        owner.check_agv()
        with worker_owner_lock():
            if (owner.closing or owner.active != job_id or owner.storage_error or owner.jobs[job_id].get('manual_cancelled')
                    or ((repeat or step) and owner.execution_uncertain)):
                raise ValueError('IPC ownership/storage changed; operation cancelled.')
            if repeat:
                recipe.check_inputs()
    def emit(state):
        with owner.lock:
            job = owner.jobs[job_id]
            previous = job.get('manual') or {}
            repeat_state = state.get('repeat') or {}
            previous_repeat = previous.get('repeat') or {}
            if (state.get('stage'), repeat_state.get('phase'), repeat_state.get('completed_transfers')) != (
                    previous.get('stage'), previous_repeat.get('phase'), previous_repeat.get('completed_transfers')):
                job['manual_updated_at'] = time.time()
            job.update(manual=state,state='waiting' if state.get('prompt') else 'running',pending=state.get('prompt'))
            owner._save(job)
    cleanup_error = None
    try:
        # A Teacher camera/control worker must finish before another SDK owner starts.
        if owner.teacher_service:
            try:
                owner.teacher_service.close()
            except BaseException:
                service = owner.teacher_service
                with owner.lock:
                    owner.execution_uncertain = True
                    owner.jobs[job_id]['execution_uncertain'] = True
                    try:
                        owner._save(owner.jobs[job_id])
                    except Exception as exc:
                        owner.storage_error = str(exc)
                workers = (getattr(service,'worker_thread',None),getattr(service,'motion_thread',None),
                           getattr(getattr(service,'motion',None),'_worker',None))
                for thread in workers:
                    if thread and thread.is_alive():
                        thread.join(timeout=12)
                owner.drain_workers(job_id, workers)
                raise
            owner.teacher_service = None
            owner.teacher_job = owner.teacher_project = owner.teacher_settings = None
        guard()
        record = {'id':job_id,'validation_mode':'fixed_z','initial_item_state':body.get('initial_item_state','unknown')}
        cls = TeacherDescent if descent else StepExecution if step else RepeatExecution if repeat else ManualExecution
        worker = cls(settings,PoseStore(owner.directory/(job_id+'.waypoints.json')),
            project=SimpleNamespace(settings=context.settings),record=record,guard=guard,
            collision=scene,check_binding=lambda:check(root,binding),evidence=owner.directory/(job_id+'.manual-path.json'),
            factory=step_backend if step else native_backend,operation='DESCEND' if descent else operation,context=context,emit=emit,
            **({'recipe':recipe,'owner':owner,'parent_job_id':job_id} if repeat else {}))
        with owner.lock:
            with owner.agv_start_lock:
                owner.check_agv()
                owner.runner = worker
                owner.motion_job = job_id
            emit(worker.state()['manual'])
        def wait():
            while True:
                try:
                    guard()
                except ValueError:
                    if not (repeat and worker.cancelled.is_set()):
                        raise
                    # The repeat worker owns native and AGV Stop and writes the
                    # specific interruption before it clears the active command.
                state = worker.state()
                if state['error']:
                    raise ValueError(state['error'])
                if not state['pending'] and not state['moving']:
                    if worker.cancelled.is_set():
                        raise ValueError('Manual operation stopped; no resume.')
                    return state
                time.sleep(.02)
        worker.submit('connect'); wait(); guard()
        worker.submit('move_tcp'); state = wait()
        if state['manual']['stage'] != 'done':
            raise ValueError('Manual operation did not finish; item state unknown.')
        return state
    finally:
        if worker:
            if descent:
                with owner.lock:
                    owner.current_alignment = None
            try:
                previous_error = worker.state()['error']
                worker.close()
                if worker.state()['error'] and worker.state()['error'] != previous_error:
                    cleanup_error = worker.state()['error']
            except BaseException as exc:
                cleanup_error = str(exc)
            if cleanup_error or worker.cleanup_uncertain:
                with owner.lock:
                    owner.execution_uncertain = True
                    owner.jobs[job_id].update(execution_uncertain=True,
                        manual=dict(worker.state()['manual'],stage='interrupted',prompt=None,item_state='unknown'))
                    try:
                        owner._save(owner.jobs[job_id])
                    except Exception as exc:
                        owner.storage_error = str(exc)
            # Ownership is retained even if close's finite join times out.
            if worker._worker.is_alive():
                worker._worker.join(timeout=10)
                if worker._worker.is_alive():
                    cleanup_error = cleanup_error or 'RTDE worker did not stop within 10 seconds.'
            owner.drain_workers(job_id, [worker._worker])
            if cleanup_error:
                raise RuntimeError('Manual cleanup failed: '+cleanup_error)
