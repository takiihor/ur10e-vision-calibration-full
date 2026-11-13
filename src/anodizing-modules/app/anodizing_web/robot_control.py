"""One approved endpoint increment through the existing checked joint worker."""
from copy import deepcopy
import math
from types import SimpleNamespace
import uuid

from ur_pick_place.config import vector
from ur_pick_place.vision.transforms import pose_to_matrix, matrix_to_pose
from ur_pick_place.vision.validation import pose_error

from .manual_execution import ManualExecution
from .site_profile import finite_numbers, section
from .teacher_motion import native_backend


def step_target(pose_m_rad, axis, increment):
    pose = vector(pose_m_rad, 6, 'Stopped TCP pose')
    if axis not in ('x', 'y', 'z', 'rx', 'ry', 'rz'):
        raise ValueError('Select X, Y, Z or a tool rotation axis.')
    allowed = (1, 5, 10) if len(axis) == 1 else (1, 2, 5)
    if type(increment) not in (int, float) or abs(increment) not in allowed:
        raise ValueError('Select a signed 1/5/10 mm step or 1/2/5 degree tool rotation.')
    if len(axis) == 1:
        pose['xyz'.index(axis)] += increment / 1000
        return pose
    delta = [0.0] * 6
    delta[3 + 'xyz'.index(axis[1])] = math.radians(increment)
    target = pose_to_matrix(pose)
    target[:3, :3] = target[:3, :3] @ pose_to_matrix(delta)[:3, :3]
    return matrix_to_pose(target)


def load_context(root, body):
    # Validate without connecting or opening teaching, calibration or camera data.
    step_target([0.0] * 6, body.get('axis'), body.get('increment'))
    tcp = vector(body.get('tcp'), 6, 'Reviewed active TCP (metres/radians)')
    if not isinstance(body.get('robot_ip'), str) or not body['robot_ip'].strip():
        raise ValueError('Enter the reviewed robot address.')
    return SimpleNamespace(settings={'robot_ip':body['robot_ip'], 'tcp':tcp},
        axis=body['axis'], increment=body['increment'], check_inputs=lambda:None)


class StepExecution(ManualExecution):
    def _dispatch_joint(self, q, target, after, generation, identity):
        guard = self.guard
        def dispatch_guard():
            guard()
            latest = self._read()
            self._stationary(latest)
            error = pose_error(latest['tcp_pose_m_rad'], after['tcp_pose_m_rad'])
            if error['position_mm'] > .2 or error['rotation_deg'] > .02:
                raise ValueError('Approved starting TCP pose changed; prepare a new step.')
        self.guard = dispatch_guard
        try:
            return super()._dispatch_joint(q, target, after, generation, identity)
        finally:
            self.guard = guard

    def _manual_sequence(self, generation):
        self._tick()
        before = self._read()
        if self.cancelled.wait(.25):
            self._tick()
        after = self._read()
        self._stationary(after)
        if max(abs(a-b) for a,b in zip(before['joints_rad'], after['joints_rad'])) > 2e-4:
            raise ValueError('Robot moved while preparing the step. Start a new request.')
        target = step_target(after['tcp_pose_m_rad'], self.context.axis, self.context.increment)
        self._workspace(after['tcp_pose_m_rad']); self._workspace(target)
        if (self.control.getInverseKinematicsHasSolution(target, after['joints_rad']) is not True
                or self.control.isPoseWithinSafetyLimits(target) is not True):
            raise ValueError('The requested endpoint is unreachable or outside controller safety limits.')
        q = vector(self.control.getInverseKinematics(target, after['joints_rad']), 6, 'Step IK joints')
        self._bounds(after['joints_rad']); self._bounds(q)
        if max(abs(a-b) for a,b in zip(after['joints_rad'], q)) > math.radians(75):
            raise ValueError('Step IK changes a joint by more than 75 degrees; request refused.')
        if self.control.isJointsWithinSafetyLimits(q) is not True:
            raise ValueError('The step joints are outside controller safety limits.')
        error = pose_error(matrix_to_pose(self._flange(q) @ pose_to_matrix(self.project.settings['tcp'])), target)
        if error['position_mm'] > 1 or error['rotation_deg'] > .1:
            raise ValueError('Step IK does not reach the requested endpoint.')
        plan = dict(axis=self.context.axis, increment=self.context.increment,
            start_tcp_pose_m_rad=after['tcp_pose_m_rad'], target_tcp_pose_m_rad=target,
            start_joints_rad=after['joints_rad'], target_joints_rad=q,
            tcp=self.project.settings['tcp'], joint_bounds_rad=self.context.joint_bounds_rad,
            joint_bounds_source=self.context.joint_bounds_source,
            workspace_min_m=self.settings.workspace_min_m, workspace_max_m=self.settings.workspace_max_m,
            joint_speed_deg_s=math.degrees(self.settings.joint_speed_rad_s),
            scene_sha256=self.collision.scene_hash, model_sha256=self.collision.model_hash)
        prompt = {'job_id':self.record['id'], 'phase':'plan', 'id':uuid.uuid4().hex}
        self.reply.clear()
        self._publish(stage='awaiting_plan', prompt=prompt, robot_step=deepcopy(plan))
        while not self.reply.wait(.05):
            self._tick(); self._stationary(self._read())
        self._tick()
        if not self.plan_approved:
            raise ValueError('Approve this exact step plan before movement.')
        self._stationary(self._read())
        self._publish(stage='checking_path_or_moving', prompt=None)
        self._dispatch_joint(q, target, after, generation, {'robot_step':plan})
        self._wait_move()
        actual = self._read()
        error = pose_error(actual['tcp_pose_m_rad'], target)
        if error['position_mm'] > 1 or error['rotation_deg'] > .1:
            raise ValueError('Robot stopped away from the requested step endpoint.')
        self._publish(stage='done', prompt=None, actual_tcp_pose_m_rad=actual['tcp_pose_m_rad'])

    def _bounds(self, joints):
        if any(not lo <= q <= hi for q,(lo,hi) in zip(joints,self.context.joint_bounds_rad)):
            raise ValueError('Step start or target is outside the reviewed operating joint bounds.')

    def _stationary(self, feedback):
        if (feedback['safety_mode'] not in (1,2) or self.control.isSteady() is not True
                or max(abs(v) for v in vector(self.receive.getActualQd(),6,'Joint velocities')) > .001
                or self.control.getAsyncOperationProgressEx().isAsyncOperationRunning()):
            raise ValueError('Wait for a stationary robot in normal or reduced safety mode.')


def run(owner, root, body, job_id):
    from .manual_execution import run as execute
    if body.get('action') != 'STEP':
        raise ValueError('Choose one finite robot STEP.')
    return execute(owner, root, body, job_id)


def capability(owner):
    """Cached configuration only. This never opens a robot or camera connection."""
    with owner.lock:
        reason = ('Enable reviewed --allow-manual-pick-place and select the factory robot model.'
                  if not owner.allow_manual_pick_place or not owner.robot_model else
                  'Another operation owns hardware.' if owner.active or owner.mcu_pending else
                  'Execution or job storage is uncertain.' if owner.execution_uncertain or owner.storage_error else
                  'The service is closing.' if owner.closing else '')
    # No site identity is built in. Only an operator-supplied site-profile.json
    # and an explicitly named saved teaching project may prefill reviewed values.
    defaults = dict(robot_ip='', tcp=None, collision_scene='', workspace_min_m=None,
        workspace_max_m=None, joint_bounds_rad=None, joint_bounds_source='', joint_speed_deg_s=3)
    profile = section(owner.root, 'robot_control')
    try:
        project_name = profile.get('teaching_project')
        if isinstance(project_name, str) and project_name.strip():
            from .paths import inside
            saved = inside(owner.root, project_name) / 'project.json'
            if saved.is_symlink() or saved.stat().st_size > 100_000:
                raise ValueError('Saved defaults are unavailable.')
            from pathlib import Path
            import json
            settings = json.loads(Path(saved).read_text())['settings']
            defaults.update(robot_ip=str(settings['robot_ip']),
                            tcp=vector(settings['tcp'], 6, 'Saved TCP'))
    except (OSError, ValueError, KeyError, TypeError):
        pass
    try:
        if isinstance(profile.get('robot_ip'), str):
            defaults['robot_ip'] = profile['robot_ip'].strip()
        if profile.get('tcp') is not None:
            defaults['tcp'] = vector(profile['tcp'], 6, 'Site profile TCP')
        if isinstance(profile.get('collision_scene'), str) and profile['collision_scene'].strip():
            from .paths import inside
            defaults['collision_scene'] = str(inside(owner.root, profile['collision_scene']))
        if profile.get('workspace_min_m') is not None and profile.get('workspace_max_m') is not None:
            low = finite_numbers(profile['workspace_min_m'], 3)
            high = finite_numbers(profile['workspace_max_m'], 3)
            if any(a >= b for a, b in zip(low, high)):
                raise ValueError('Site profile workspace bounds must be ordered.')
            defaults.update(workspace_min_m=low, workspace_max_m=high)
        if profile.get('joint_bounds_rad') is not None:
            bounds = profile['joint_bounds_rad']
            if not isinstance(bounds, list) or len(bounds) != 6:
                raise ValueError('Site profile joint bounds need six pairs.')
            checked = []
            for pair in bounds:
                low, high = finite_numbers(pair, 2)
                if low >= high:
                    raise ValueError('Site profile joint bounds must be ordered.')
                checked.append([low, high])
            source = profile.get('joint_bounds_source')
            if not isinstance(source, str) or not source.strip():
                raise ValueError('Site profile joint bounds need a reviewed source.')
            defaults.update(joint_bounds_rad=checked, joint_bounds_source=source.strip())
        speed = profile.get('joint_speed_deg_s')
        if type(speed) in (int, float) and math.isfinite(speed) and 1 <= speed <= 6:
            defaults['joint_speed_deg_s'] = speed
    except (OSError, ValueError, KeyError, TypeError):
        pass
    return {'enabled':bool(owner.allow_manual_pick_place and owner.robot_model), 'available':not reason,
        'detail':reason or 'Ready to prepare one finite checked joint step.', 'defaults':defaults}
