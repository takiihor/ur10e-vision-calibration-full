"""One supervised, finite commanded-transfer test on the native IPC worker."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import time
from types import SimpleNamespace

from ur_pick_place.config import number, vector
from ur_pick_place.vision.transforms import matrix_to_pose, pose_to_matrix
from .manual_execution import ManualExecution, load_context
from .manual_pick_place import ManualPhases
from .paths import inside, check_tree


def load_recipe(root, body, *, require_approval=True):
    count = body.get('transfers')
    if type(count) is not int or not 1 <= count <= 100:
        raise ValueError('Transfers must be an integer from 1 to 100. One transfer is one pickup and placement.')
    if require_approval and (body.get('automatic_test_approved') is not True or body.get('initial_item_state') != 'operator_confirmed_released'):
        raise ValueError('Approve this bounded automatic test with the rod at A and no held item.')
    settle = number(body.get('settle_s',1), 'Unverified settling time (seconds)', low=0, high=5)
    projects = {'A':body.get('project'), 'B':body.get('place_project') or body.get('project')}
    contexts = {}
    for endpoint, name in projects.items():
        project = check_tree(root, inside(root,name))
        saved = json.loads((project/'project.json').read_text())
        settings = saved['settings']
        board = settings['target'].get('board')
        for operation in ('PICK','PLACE'):
            request = dict(body, project=str(project), action=operation, pick_tag=board,place_tag=board,
                reuse_taught_offset=True,calibration=(body.get('calibration') if endpoint=='A' else None) or settings['calibration'],
                manifest=(body.get('manifest') if endpoint=='A' else None) or settings['target']['board_manifest'])
            contexts[endpoint,operation] = load_context(root,request)
    first = contexts['A','PICK']
    for context in contexts.values():
        if (context.settings['robot_ip'] != first.settings['robot_ip'] or
                context.calibration.sha256 != first.calibration.sha256 or
                context.settings['tcp'] != first.settings['tcp'] or
                context.config.camera != first.config.camera):
            raise ValueError('A and B must use the same robot, calibration, TCP and camera configuration.')
    checks = [context.check_inputs for context in contexts.values()]
    def check_inputs():
        for check in checks:
            check()
    for context in contexts.values():
        context.check_inputs = check_inputs
    selected = {key:value for key,value in body.items() if key not in ('request_id','action','automatic_test_approved','recipe_sha256')}
    inputs = {path:digest for context in contexts.values() for path,digest in context.hashes.items()}
    scene = inside(root,body.get('collision_scene'))
    inputs[str(scene)] = hashlib.sha256(scene.read_bytes()).hexdigest()
    recipe_sha256 = hashlib.sha256(json.dumps([selected,inputs],sort_keys=True,separators=(',',':'),allow_nan=False).encode()).hexdigest()
    if require_approval and body.get('recipe_sha256') != recipe_sha256:
        raise ValueError('Automatic recipe or saved inputs changed. Review and approve again.')
    summary = dict(recipe_sha256=recipe_sha256,inputs=inputs,transfers=count,A=str(first.provenance['project']), B=str(contexts['B','PICK'].provenance['project']),
        end='A' if count%2==0 else 'B', settle_s=settle,
        approach_mm={endpoint:contexts[endpoint,'PICK'].approach_mm for endpoint in ('A','B')},
        withdrawal_mm=100, grip='1 — Tight close', release='3 — Open',
        outcome='Commanded transfers only. MCU replies do not verify a held or released rod.')
    return SimpleNamespace(contexts=contexts,transfers=count,settle_s=settle,summary=summary,check_inputs=check_inputs)


class RepeatExecution(ManualExecution):
    def __init__(self,*args,recipe,owner,parent_job_id,**kwargs):
        self.recipe, self.owner, self.parent_job_id = recipe, owner, parent_job_id
        self.step = 'preflight'
        self.completed_transfers = 0
        super().__init__(*args,**kwargs)
        self.joint_bounds_rad = self.context.joint_bounds_rad
        self.joint_bounds_source = self.context.joint_bounds_source
        self.run_evidence = self.base_evidence
        self.manual['repeat'] = dict(recipe.summary,completed_transfers=0,phase='preflight')

    def _make_phases(self, initial, observation_id):
        item = self.manual['item_state']
        return ManualPhases(self.record['id'],self.operation,item,observation_id,automatic=True)

    def _stationary(self):
        self._tick()
        feedback = self._read()
        if (feedback['safety_mode'] not in (1,2) or self.control.isSteady() is not True or
                max(abs(v) for v in vector(self.receive.getActualQd(),6,'Joint velocity')) > .001 or
                self.control.getAsyncOperationProgressEx().isAsyncOperationRunning()):
            raise ValueError('Wait for a stationary normal/reduced robot before automatic MCU work.')

    def _command(self, command):
        from .mcu import run_automatic
        self._stationary()
        result = run_automatic(self.owner,self,self.step,command)
        self._stationary()
        self._publish(mcu=result)
        if command != '0':
            deadline = time.monotonic()+self.recipe.settle_s
            self._publish(stage='settling',settle_s=self.recipe.settle_s)
            while time.monotonic()<deadline:
                self._tick()
                feedback = self._read()
                if feedback['safety_mode'] not in (1,2) or self.control.isSteady() is not True:
                    raise ValueError('Robot changed state during the unverified settling delay.')
                self.cancelled.wait(min(.02,max(0,deadline-time.monotonic())))
        return result

    def _before_approach(self):
        if self.operation == 'PICK':
            self._publish(stage='opening_at_hover')
            self._command('3')
            self.phases.state['item_state'] = 'open_acknowledged_unverified'
            self._publish(item_state='open_acknowledged_unverified')

    def _pause(self, phase):
        self._tick()
        if phase == 'plan':
            self.plan_approved = True
            self._publish(stage='automatic_plan_checked',prompt=None)
        else:
            prompt = self.phases.snapshot()['prompt']
            self._publish(stage='commanding_'+phase,prompt=None)
            self._command('1' if phase=='grip' else '3')
            self.phases.acknowledge(prompt)

    def confirm(self, prompt, value):
        raise ValueError('Automatic repeat has no manual confirmation or resume.')

    def _dispatch_joint(self, q, target, after, generation, identity):
        if any(not low <= value <= high for point in (after['joints_rad'],q)
               for value,(low,high) in zip(point,self.joint_bounds_rad)):
            raise ValueError('Joint transit exceeds the reviewed operating bounds.')
        return super()._dispatch_joint(q,target,after,generation,identity)

    def _observe_transit(self, generation):
        self._tick()
        after = self._read()
        q = vector(self.context.config.role(self.context.role).observe_joints,6,'Saved observation joints')
        if any(not low <= value <= high for value,(low,high) in zip(q,self.context.joint_bounds_rad)):
            raise ValueError('Saved observation joints exceed the reviewed operating bounds.')
        if self.control.isJointsWithinSafetyLimits(q) is not True:
            raise ValueError('Saved observation joints exceed controller safety limits.')
        target = matrix_to_pose(self._flange(q) @ pose_to_matrix(self.context.settings['tcp']))
        self._workspace(after['tcp_pose_m_rad']); self._workspace(target)
        if self.control.isPoseWithinSafetyLimits(target) is not True:
            raise ValueError('Saved observation TCP exceeds controller safety limits.')
        self.evidence = self.base_evidence.with_name(self.record['id']+'.observation-transit.manual-path.json')
        self._publish(stage='moving_to_observation',observation_joints=q,observation_tcp_pose_m_rad=target)
        self._dispatch_joint(q,target,after,generation,{'repeat_job_id':self.parent_job_id,'step':self.step})
        self._wait_move()

    def _manual_sequence(self, generation):
        self._publish(stage='mcu_preflight',prompt=None,item_state='automatic_empty_declared')
        self._command('0')
        for index in range(self.recipe.transfers):
            source,destination = ('A','B') if index%2==0 else ('B','A')
            for operation,endpoint in (('PICK',source),('PLACE',destination)):
                self._tick()
                self.operation = operation
                self.context = self.recipe.contexts[endpoint,operation]
                self.context.joint_bounds_rad = self.joint_bounds_rad
                self.context.joint_bounds_source = self.joint_bounds_source
                self.project.settings = self.context.settings
                self.step = 't'+str(index+1).zfill(3)+'-'+operation.lower()
                self.record['id'] = self.parent_job_id+'-'+self.step
                self.base_evidence = self.run_evidence.with_name(self.record['id']+'.manual-path.json')
                self.plan_approved = False
                self.phases = None
                self._publish(stage='starting_operation',prompt=None,route=None,operation=operation,
                    repeat=dict(self.recipe.summary,completed_transfers=self.completed_transfers,
                        transfer=index+1,source=source,destination=destination,endpoint=endpoint,step=self.step))
                self._observe_transit(generation)
                super()._manual_sequence(generation)
            self.completed_transfers += 1
            self._publish(repeat=dict(self.manual['repeat'],completed_transfers=self.completed_transfers,
                commanded_rod_endpoint=destination))
        self._publish(stage='done',prompt=None,item_state='open_acknowledged_unverified',operation='REPEAT')
