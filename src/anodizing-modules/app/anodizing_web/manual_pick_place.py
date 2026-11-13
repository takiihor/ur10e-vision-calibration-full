"""Role preparation and explicit manual execution; simulation stays separate."""
from copy import deepcopy
from dataclasses import replace
import json
import math
import uuid

from .paths import check_tree, inside

BLOCKER = 'cartesian_arm_sweep_unavailable'


def capability(owner=None):
    if owner is not None and owner.allow_manual_pick_place and owner.robot_model and not owner.execution_uncertain:
        return {'hardware_available':True,'path_check':{'status':'required_per_leg'},
                'detail':'Every moveL requires a matching continuous-branch certificate; refusals never run simulation.',
                'collision_scope':'supervised_self; external cell objects are not modelled','item_state':'unknown'}
    return {'hardware_available':False,
            'path_check':{'status':'disabled','reason':'reviewed_manual_service_required'},
            'detail':('Device execution is uncertain; reconciliation is required.' if owner is not None and owner.execution_uncertain
                      else 'Reviewed IPC manual movement requires --allow-manual-pick-place and the selected factory robot model.'),
            'collision_scope':'supervised_self; external cell objects are not modelled','item_state':'unknown'}


def vertical_intent(endpoint, approach_mm):
    """Describe endpoints only. This function does not check any swept path."""
    if (not isinstance(endpoint, (list, tuple)) or len(endpoint) != 6 or
            any(type(v) not in (int, float) or not math.isfinite(v) for v in endpoint)):
        raise ValueError('Endpoint must contain six finite metres/radians values.')
    if type(approach_mm) not in (int, float) or not math.isfinite(approach_mm) or approach_mm <= 0:
        raise ValueError('Approach height must be positive finite millimetres.')
    above, withdrawn = list(endpoint), list(endpoint)
    above[2] += approach_mm / 1000
    withdrawn[2] += .100
    return {'certified': False, 'trajectory': 'fixed_orientation_base_z',
            'endpoint': list(endpoint), 'above': above, 'withdrawn': withdrawn,
            'withdrawal_mm': 100, 'alignment': 'XY and orientation above endpoint',
            'approach': 'straight base -Z; hold XY and orientation',
            'withdrawal': 'straight base +Z; hold XY and orientation'}


def prepare(root, body):
    from ur_pick_place.vision.errors import VisionError
    try:
        return _prepare(root, body)
    except (VisionError, KeyError, TypeError) as exc:
        raise ValueError(f'Manual preparation refused: {exc}') from exc


def _prepare(root, body):
    from ur_pick_place.vision.config import Role
    from ur_pick_place.vision.grasp import load_taught, target_pose
    from ur_pick_place.vision.handeye import load_handeye
    from ur_pick_place.vision.observe import Observation
    from ur_pick_place.vision.robot_state import same_tcp
    from ur_pick_place.vision.targets import target_from_config
    from ur_pick_place.vision.teaching import digest
    from ur_pick_place.vision.transforms import matrix_to_pose

    if body.get('reuse_taught_offset') is not True:
        raise ValueError('This shared-offset test requires both role tags to match the project board and explicit reuse.')
    project = check_tree(root, inside(root, body.get('project')))
    saved = json.loads((project / 'project.json').read_text())
    if (saved.get('type') != 'marker_pick_project' or saved.get('schema_version') != 1 or
            saved['flow'].get('taught') is not True):
        raise ValueError('Choose a completed teaching project.')
    settings = saved['settings']
    calibration = load_handeye(inside(root, body.get('calibration')))
    if saved['inputs']['calibration'] != calibration.sha256:
        raise ValueError('Calibration does not match this teaching project.')
    target_spec = dict(settings['target'])
    if (target_spec.get('kind') != 'charuco' or not target_spec.get('board') or
            body.get('pick_tag') != target_spec['board'] or body.get('place_tag') != target_spec['board']):
        raise ValueError('Both role tags must match this project’s taught Charuco board.')
    manifest = inside(root, body.get('manifest') or target_spec['board_manifest'])
    if saved['inputs'].get('manifest') != digest(manifest):
        raise ValueError('Marker manifest changed since teaching.')
    target_spec['board_manifest'] = str(manifest)
    target = target_from_config(target_spec)
    model = load_taught(project / 'grasp_pick.json')
    old = Observation.from_json(json.loads((project / 'observation.json').read_text()))
    if model.role != 'pick' or old.role != 'pick':
        raise ValueError('The original grasp and teaching observation must have the pick role.')
    if (old.calibration_sha256 != calibration.sha256 or
            not same_tcp(settings['tcp'], calibration.tcp_offset) or
            not same_tcp(old.robot['tcp_offset'], calibration.tcp_offset)):
        raise ValueError('Teaching observation or TCP does not match calibration.')
    roles = {}
    for name in ('pick', 'place'):
        # Explicit dummy reuse creates role-specific copies, never rewrites original teaching.
        derived_model, reference = replace(model, role=name), replace(old, role=name)
        role = Role(target=target_spec, observe_joints=old.robot['joints_rad'],
                    motion_model=settings.get('motion_model', 'full'), limits=settings.get('limits', {}))
        pose, report = target_pose(derived_model, reference, role, calibration_sha256=calibration.sha256,
                                  tcp_offset=calibration.tcp_offset, target=target)
        roles[name] = {'role': name, 'tag': body[name + '_tag'], 'target': deepcopy(target.describe()),
            'marker_to_tcp': model.marker_tcp.tolist(), 'source_role': model.role,
            'source_grasp': str(project / 'grasp_pick.json'), 'source_grasp_sha256': model.sha256,
            'independently_taught': name == 'pick',
            'reuse': 'original_teaching' if name == 'pick' else 'explicit_dummy_test_shared_offset',
            'taught_reference_validation': report,
            'taught_reference_route_intent': vertical_intent(matrix_to_pose(pose), settings['approach_mm'])}
    result = capability()
    result.update({'project': str(project), 'roles': roles,
        'calibration_sha256': calibration.sha256, 'tcp_offset': list(calibration.tcp_offset),
        'manifest_sha256': digest(manifest),
        'simulation_source': bool(saved.get('simulation') or calibration.simulated),
        'quality_floor': {'pose_pixels_per_module': 3, 'reference_pixels_per_module': 4},
        'observation_requirement': 'New stationary quality-gated observation before each operation; '
                                   'saved teaching is reference data only.',
        'outcome': 'Read-only preparation. No fresh observation, motion, grip or release occurred.'})
    return result


def run(root, body, job_id, owner=None):
    action = body.get('action')
    if action in ('PICK', 'PLACE', 'REPEAT'):
        if owner is None:
            raise ValueError('Hardware execution requires the shared IPC JobOwner.')
        from .manual_execution import run as execute
        return execute(owner,root,body,job_id)
    if action == 'review_repeat':
        from .repeat_pick_place import load_recipe
        return load_recipe(root,body,require_approval=False).summary
    if action != 'prepare':
        raise ValueError('Choose read-only prepare, PICK or PLACE.')
    return prepare(root, body)


class ManualPhases:
    """Pure offline sequencing rules, not an execution authority or persisted worker.

    A worker must supply verified observations/motion completions. No HTTP route
    accepts these events. There is intentionally no restore/resume constructor.
    """
    def __init__(self, job_id, operation, item_state, observation_id, *, automatic=False):
        allowed = {'PICK': ('no_confirmed_item', 'operator_confirmed_released'),
                   'PLACE': ('operator_confirmed_held',)}
        self.automatic = automatic
        if automatic:
            allowed = {'PICK': ('automatic_empty_declared', 'open_acknowledged_unverified'),
                       'PLACE': ('close_acknowledged_unverified',)}
        if operation not in allowed or item_state not in allowed[operation]:
            raise ValueError('Operation requires the matching operator-confirmed item state.')
        if not isinstance(job_id, str) or not job_id or not isinstance(observation_id, str) or not observation_id:
            raise ValueError('Bind the phase record to a job and fresh observation identity.')
        self.state = {'job_id': job_id, 'role': operation.lower(), 'observation_id': observation_id,
                      'stage': 'observing', 'item_state': item_state, 'prompt': None, 'error': None}

    def snapshot(self):
        return deepcopy(self.state)

    def advance(self, event):
        next_stage = {('observing', 'observation_accepted'): 'aligning',
                      ('aligning', 'alignment_complete'): 'approaching',
                      ('approaching', 'approach_complete'): 'prompt',
                      ('withdrawing', 'withdrawal_complete'): 'done'}.get((self.state['stage'], event))
        if next_stage is None:
            raise ValueError('Completion does not match the current manual phase.')
        if next_stage == 'prompt':
            phase = 'grip' if self.state['role'] == 'pick' else 'release'
            self.state['prompt'] = {key: self.state[key] for key in ('job_id', 'role', 'observation_id')}
            self.state['prompt'].update(phase=phase, id=uuid.uuid4().hex)
            next_stage = 'awaiting_' + phase
        self.state['stage'] = next_stage

    def confirm(self, prompt, value):
        if (self.automatic or value is not True or self.state['prompt'] is None or prompt != self.state['prompt'] or
                self.state['stage'] not in ('awaiting_grip', 'awaiting_release')):
            raise ValueError('Confirmation does not match the active job and manual phase.')
        self.state.update(stage='withdrawing', prompt=None,
            item_state='operator_confirmed_held' if self.state['role'] == 'pick' else 'operator_confirmed_released')

    def acknowledge(self, prompt):
        if (not self.automatic or prompt != self.state['prompt'] or
                self.state['stage'] not in ('awaiting_grip', 'awaiting_release')):
            raise ValueError('Automatic ACK does not match this phase.')
        self.state.update(stage='withdrawing', prompt=None,
            item_state='close_acknowledged_unverified' if self.state['role']=='pick' else 'open_acknowledged_unverified')

    def interrupt(self, reason):
        self.state.update(stage='interrupted', item_state='unknown', prompt=None, error=str(reason))
