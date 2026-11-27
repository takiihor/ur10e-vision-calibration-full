"""One stopped, unblended joint test to a planar Teacher endpoint."""
import hashlib
import json
import math
import time
import numpy as np

from ur_pick_place.config import vector
from ur_pick_place.vision.validation import ObservationReturnMotion, ValidationMotion, pose_error
from ur_pick_place.vision.transforms import pose_to_matrix, matrix_to_pose, invert
from .jobs import atomic_json


class TeacherJointMotion(ValidationMotion):
    def __init__(self, *args, collision, check_binding, evidence, **kwargs):
        self.collision, self.check_binding, self.evidence = collision, check_binding, evidence
        self.path_certificate = None
        self.cleanup_uncertain = False
        self._stop_attempted = False
        super().__init__(*args, **kwargs)

    def cached_joint_sample(self):
        """Copy already acquired feedback without waiting for the native worker."""
        if not self._lock.acquire(blocking=False):
            return None
        try:
            if self._connected and self._feedback:
                received_at = min(self._fresh_at, self._feedback['received_at'])
                return list(self._feedback['joints_rad']), received_at, self.demo
            return None
        finally:
            self._lock.release()

    def _execute(self, command, payload, generation):
        if command in ('move_tcp', 'move_saved'):
            with self._lock:
                self._message = 'Preflight: checking capture and controller readiness'
        try:
            return super()._execute(command,payload,generation)
        except CleanupUncertain:
            self.cleanup_uncertain = True
            raise

    def _flange(self, q):
        # Use an explicit virtual TCP for forward kinematics.
        virtual = [0, 0, .001, 0, 0, 0]
        return pose_to_matrix(vector(self.control.getForwardKinematics(q, virtual), 6, 'FK pose')) @ invert(pose_to_matrix(virtual))

    def _dispatch_validation(self, target, after, generation):
        if self.record.get('validation_mode') != 'fixed_z':
            raise ValueError('Only the planar endpoint joint test is available.')
        self.collision.require_motion_ready()
        if self.collision.model['nominal']:
            raise ValueError('Select the factory robot model before Teacher movement.')
        self.check_binding()
        self.guard()
        if self.control.isSteady() is not True:
            raise ValueError('Wait until the robot stops before the path check.')
        start = list(after['joints_rad'])
        q = vector(self.control.getInverseKinematics(target, start), 6, 'IK joints')
        if max(abs(a-b) for a,b in zip(start,q)) > math.radians(75):
            raise ValueError('IK changes a joint by more than 75 degrees. Reposition and capture again.')
        if self.control.isJointsWithinSafetyLimits(q) is not True:
            raise ValueError('IK joints are outside controller safety limits.')
        error = pose_error(matrix_to_pose(self._flange(q) @ pose_to_matrix(self.project.settings['tcp'])), target)
        if error['position_mm'] > 1 or error['rotation_deg'] > .1:
            raise ValueError('IK does not reach the reviewed Teacher endpoint.')
        return self._dispatch_joint(q, target, after, generation, {'capture_id':self.record['id']})

    def _dispatch_joint(self, q, target, after, generation, identity):
        self.collision.require_motion_ready()
        if self.collision.model['nominal']:
            raise ValueError('Select the factory robot model before Teacher movement.')
        self.check_binding()
        self.guard()
        if self.control.isSteady() is not True:
            raise ValueError('Wait until the robot stops before the path check.')
        start = list(after['joints_rad'])
        with self._lock:
            self._message = 'Checking joint path: full arm, tool, camera and mount'
        certificate = self.collision.check_segment(start,q)
        self.path_certificate = dict(certificate, trajectory='joint_linear_unblended', **identity,
            target_tcp_pose_m_rad=list(target), joint_speed_rad_s=self.settings.joint_speed_rad_s,
            joint_accel_rad_s2=self.settings.joint_accel_rad_s2, height_contract='destination_only')
        atomic_json(self.evidence,self.path_certificate)
        if (certificate.get('start_joints') != start or certificate.get('target_joints') != q
                or certificate.get('scene_sha256') != self.collision.scene_hash
                or certificate.get('model_sha256') != self.collision.model_hash):
            raise ValueError('The joint path certificate does not match this request.')
        if certificate.get('status') != 'clear_in_model':
            pair = ' / '.join(certificate.get('pair', []))
            raise ValueError('Teacher path check failed: '+str(certificate.get('status'))+'. '+pair)
        for point in (start,((np.asarray(start)+q)/2).tolist(),q):
            actual,model = self._flange(point),self.collision.flange(point)
            if not np.isfinite(actual).all() or not np.isfinite(model).all():
                raise ValueError('The robot or collision model returned invalid kinematics.')
            delta = np.linalg.norm(actual[:3,3]-model[:3,3])
            angle = math.acos(float(np.clip((np.trace(actual[:3,:3].T @ model[:3,:3])-1)/2,-1,1)))
            if delta > self.collision.data['model_uncertainty_m'] or angle > math.radians(.1):
                raise ValueError('The robot kinematics differ from the collision model.')
        self.path_certificate['outcome'] = 'dispatching'
        atomic_json(self.evidence,self.path_certificate)
        try:
            before = self.control.getAsyncOperationProgressEx()
            if before.isAsyncOperationRunning():
                raise ValueError('Another controller operation is still active.')
            self.check_binding()
            latest = self._read()
            if latest['safety_mode'] not in (1,2) or self.control.isSteady() is not True:
                raise ValueError('Robot safety or stationary state changed during the path check.')
            if max(abs(a-b) for a,b in zip(start,latest['joints_rad'])) > 2e-4:
                raise ValueError('Starting joints changed during the path check. Capture again.')
            self.guard()
            with self._lock:
                if generation != self._generation or self._stop_requested.is_set() or self._shutdown.is_set():
                    raise ValueError('Teacher movement stopped before dispatch.')
                self._motion = {'mode':'joint','before':before,'operation':(before.operationId()+1)&127,
                                'target':q,'deadline':time.monotonic()+120}
                self._message = 'Moving: checked joint-space test; TCP height may vary'
        except BaseException:
            self.path_certificate['outcome'] = 'not_dispatched'
            atomic_json(self.evidence,self.path_certificate)
            raise
        try:
            if self.control.moveJ(q,self.settings.joint_speed_rad_s,self.settings.joint_accel_rad_s2,True) is not True:
                raise RuntimeError('Robot rejected the joint movement.')
        except BaseException:
            self.cleanup_uncertain = True
            raise

    def _poll(self):
        feedback = self._read()
        motion = self._motion
        if not motion:
            return
        self.guard()
        if time.monotonic()-self._heartbeat_at > 2 or time.monotonic() > motion['deadline']:
            raise RuntimeError('Teacher heartbeat lost or joint movement timed out.')
        if feedback['safety_mode'] not in (1,2):
            raise RuntimeError('Robot safety mode changed during movement.')
        status = self.control.getAsyncOperationProgressEx()
        if status.operationId() == motion['before'].operationId() and status.changeCount() == motion['before'].changeCount():
            return
        if status.operationId() != motion['operation']:
            self.cleanup_uncertain = True
            raise RuntimeError('Unexpected controller operation identity.')
        if status.isAsyncOperationRunning():
            return
        if self.control.isSteady() is not True:
            return
        velocity = vector(self.receive.getActualQd(),6,'Joint velocities')
        if max(abs(v) for v in velocity) > .001:
            return
        if max(abs(a-b) for a,b in zip(feedback['joints_rad'],motion['target'])) > math.radians(.2):
            raise RuntimeError('Robot stopped away from the checked joint target.')
        self.path_certificate.update(outcome='completed',actual_joints=list(feedback['joints_rad']))
        atomic_json(self.evidence,self.path_certificate)
        with self._lock:
            self._motion = None
            self._message = 'Movement complete'

    def _stop_motion(self):
        if not self._motion or not self.control:
            return
        if self._stop_attempted:
            raise RuntimeError('The previous Teacher stop remains unconfirmed.')
        self._stop_attempted = True
        self._message = 'Stopping joint movement'
        before = None
        try:
            try:
                before = self.control.getAsyncOperationProgressEx()
            finally:
                # Stop is attempted even when the pre-stop status read fails.
                self.control.stopJ(1.0,True)
            if before is None:
                raise RuntimeError('Cannot identify the stop operation.')
            deadline = time.monotonic()+3
            while time.monotonic()<deadline:
                feedback = self._read()
                status = self.control.getAsyncOperationProgressEx()
                velocity = vector(self.receive.getActualQd(),6,'Joint velocities')
                if (status.operationId() == ((before.operationId()+1)&127)
                        and status.changeCount() != before.changeCount() and not status.isAsyncOperationRunning()
                        and self.control.isSteady() is True and max(abs(v) for v in velocity)<=.001):
                    self.path_certificate.update(outcome='stopped',actual_joints=feedback['joints_rad'])
                    atomic_json(self.evidence,self.path_certificate)
                    with self._lock:
                        self._motion = None
                        self._message = 'Movement stopped'
                    return
                time.sleep(.02)
            raise RuntimeError('Joint stop confirmation timed out.')
        except BaseException:
            self.cleanup_uncertain = True
            if self.path_certificate:
                self.path_certificate['outcome'] = 'execution_uncertain'
                atomic_json(self.evidence,self.path_certificate)
            raise

    def close(self):
        try:
            super().close()
        except BaseException:
            # A live SDK worker still owns its acquired channels, even before dispatch.
            self.cleanup_uncertain = True
            raise

    def _disconnect(self):
        try:
            super()._disconnect()
        except BaseException:
            self.cleanup_uncertain = True
            raise


class TeacherReturnMotion(TeacherJointMotion, ObservationReturnMotion):
    def __init__(self, *args, project, record, **kwargs):
        from ur_pick_place.vision.observe import Observation
        from ur_pick_place.vision.targets import same_target
        from ur_pick_place.vision.robot_state import same_tcp
        raw = (project.directory / 'observation.json').read_bytes()
        if hashlib.sha256(raw).hexdigest() != record['observation_sha256']:
            raise ValueError('Saved observation changed; return request cancelled.')
        observation = Observation.from_json(json.loads(raw))
        if (observation.role != 'pick' or observation.calibration_sha256 != project.calibration.sha256
                or not same_target(observation.target, project.target)
                or not same_tcp(observation.robot['tcp_offset'], project.settings['tcp'])
                or vector(observation.robot['joints_rad'], 6, 'Saved observation joints') != record['target_joints_rad']):
            raise ValueError('Saved observation does not match this return request.')
        super().__init__(*args, project=project, record=record, **kwargs)

    def _dispatch_return(self, target, target_tcp, after, generation):
        return self._dispatch_joint(target, target_tcp, after, generation,
            {'motion_kind':'return_observation', 'observation_sha256':self.record['observation_sha256']})


class CleanupUncertain(RuntimeError):
    """An acquired native transport could not be confirmed closed."""


def native_backend(host):
    from rtde_control import RTDEControlInterface
    from rtde_receive import RTDEReceiveInterface
    from ur_pick_place.scheduling import preserve_caller_scheduler
    control = receive = None
    try:
        with preserve_caller_scheduler():
            control = RTDEControlInterface(host,125.0)
        with preserve_caller_scheduler():
            receive = RTDEReceiveInterface(host,125.0)
        return control,receive
    except BaseException as original:
        errors = []
        for cleanup in ([control.stopScript,control.disconnect] if control else []) + ([receive.disconnect] if receive else []):
            try:
                cleanup()
            except Exception as exc:
                errors.append(str(exc))
        if errors:
            raise CleanupUncertain(str(original)+'; partial connection cleanup failed: '+'; '.join(errors)) from original
        raise
