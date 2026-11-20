import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest import mock

from anodizing_web import calibration
from anodizing_web.jobs import JobOwner
from ur_handeye import collision, ui_jobs

EXAMPLE = Path(collision.__file__).parent / 'web/collision-scene.example.json'


class Runner:
    instances = []
    def __init__(self):
        self.active = False
        self.phase = 0
        self.answers = []
        self.stopped = False
        Runner.instances.append(self)
    def start(self, argv):
        self.argv = argv
        self.active = True
    def drain(self):
        if self.phase == 0:
            self.phase = 1
            yield {'type':'request', 'kind':'motion', 'id':1, 'summary':'Probe phase: return after each axis.'}
        elif self.phase == 2:
            self.phase = 3
            yield {'type':'request', 'kind':'motion', 'id':2, 'summary':'Updated view plan.'}
        elif self.phase == 4:
            self.phase = 5
            self.active = False
            yield from self.cleanup()
            yield {'type':'done', 'code':130 if self.stopped else 0}
    def cleanup(self):
        yield {'type': 'calibration_cleanup', 'errors': []}
    def answer(self, request_id, value):
        self.answers.append((request_id, value))
        self.phase = 2 if request_id == 1 and value else 4
    def stop(self):
        self.stopped = True
        self.active = False
        self.phase = 4


class AutoBridgeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.scene = self.root/'scene.json'
        self.scene.write_bytes(EXAMPLE.read_bytes())
        (self.root/'board.json').write_text('{}')
        self.body = {'action':'collect','agv_off':True,'session':'new-session','manifest':'board.json',
                     'board':'calibration_stage2','robot_ip':'fake','serial':'fake',
                     'collision_scene':'scene.json','views':14,'tilt_deg':25,'roll_deg':30}
        Runner.instances.clear()

    def wait(self, owner, job_id, state):
        for _ in range(400):
            job = owner.get(job_id)
            if job['state'] == state:
                return job
            if job['state'] in ('failed', 'interrupted'):
                self.fail(str(job))
            time.sleep(.01)
        self.fail('Job did not reach ' + state)

    def submit(self, owner, job_id, body):
        return owner.submit(job_id,'calibration',body,
            lambda jid:calibration.run(owner,self.root,body,jid,False,False))

    def test_default_off_and_invalid_scenes_refuse_before_worker(self):
        owner = JobOwner(self.root)
        with mock.patch.object(ui_jobs,'JobRunner',side_effect=AssertionError('worker created')):
            with self.assertRaisesRegex(ValueError,'disabled'):
                self.submit(owner,'default-off-01',self.body)
            enabled = JobOwner(self.root,allow_auto_calibration=True)
            for scene in ('scene.json','missing.json','../outside'):
                with self.subTest(scene=scene), self.assertRaises((ValueError,OSError)):
                    self.submit(enabled,'invalid-scene-'+str(len(scene)),dict(self.body,collision_scene=scene))
        self.assertEqual(Runner.instances,[])
        self.assertEqual(list((self.root/'.jobs').glob('*.scene.json')),[])

    def test_model_mismatch_and_missing_dependency_refuse_before_worker(self):
        import sys
        data=json.loads(self.scene.read_text())
        data['model_sha256']='wrong'
        self.scene.write_text(json.dumps(data))
        owner=JobOwner(self.root,allow_auto_calibration=True)
        with mock.patch.object(ui_jobs,'JobRunner',side_effect=AssertionError('worker created')):
            with self.assertRaisesRegex(ValueError,'different robot model'):
                self.submit(owner,'wrong-model-01',self.body)
            # Synthetic test-only scene; these flags are not physical confirmation.
            data=json.loads(EXAMPLE.read_text())
            data.update(confirmed=True,environment_complete=True)
            data['obstacles']=[{'name':'test_box','dimensions_m':[.1,.1,.1],
                                'pose':{'xyz_m':[2,2,2],'rpy_rad':[0,0,0]}}]
            self.scene.write_text(json.dumps(data))
            with mock.patch.dict(sys.modules,{'ur_handeye.collision_backend':None}):
                with self.assertRaisesRegex(ValueError,'collision dependencies'):
                    self.submit(owner,'missing-dep-01',self.body)

    def test_scene_changed_before_worker_is_refused(self):
        owner = JobOwner(self.root,allow_auto_calibration=True)
        from ur_handeye import ui
        original=ui.build_command
        def change(*args,**kwargs):
            result=original(*args,**kwargs)
            self.scene.write_text(self.scene.read_text()+' ')
            return result
        with mock.patch.object(collision.Scene,'require_motion_ready'), \
             mock.patch.object(ui,'build_command',side_effect=change), \
             mock.patch.object(ui_jobs,'JobRunner',side_effect=AssertionError('worker created')):
            self.submit(owner,'scene-changed-01',self.body)
            for _ in range(400):
                job=owner.get('scene-changed-01')
                if job['state']=='failed': break
                time.sleep(.01)
            self.assertEqual(job['state'],'failed')
            self.assertIn('scene changed',job['error'])

    def test_automatic_phases_are_scoped_and_replies_are_not_replayed(self):
        owner=JobOwner(self.root,allow_auto_calibration=True)
        with mock.patch.object(collision.Scene,'require_motion_ready'), mock.patch.object(ui_jobs,'JobRunner',Runner):
            self.submit(owner,'auto-collect-01',self.body)
            job=self.wait(owner,'auto-collect-01','waiting')
            snapshot=Path(job['auto_scene']['snapshot'])
            self.assertEqual(snapshot.read_bytes(),self.scene.read_bytes())
            self.assertEqual(snapshot.stat().st_mode & 0o777,0o400)
            argv=Runner.instances[-1].argv
            self.assertEqual(argv[0],'collect')
            self.assertEqual(argv[argv.index('--collision-scene')+1],str(snapshot))
            self.assertEqual(argv[argv.index('--views')+1],'14')
            self.assertEqual(float(argv[argv.index('--tilt-deg')+1]),25)
            self.assertEqual(float(argv[argv.index('--roll-deg')+1]),30)
            self.assertNotIn('--yes',argv)
            self.assertNotIn('--freedrive',argv)
            with self.assertRaisesRegex(ValueError,'stale'):
                owner.answer('auto-collect-01',2,True)
            owner.answer('auto-collect-01',1,True)
            for _ in range(400):
                job=owner.get('auto-collect-01')
                if job['pending'] and job['pending']['id']==2: break
                time.sleep(.01)
            self.assertEqual(job['pending']['id'],2)
            with self.assertRaisesRegex(ValueError,'stale'):
                owner.answer('auto-collect-01',1,True)
            duplicate=self.submit(owner,'auto-collect-01',self.body)
            self.assertEqual(duplicate['id'],job['id'])
            owner.answer('auto-collect-01',2,False)
            self.wait(owner,'auto-collect-01','done')
            self.assertEqual(Runner.instances[-1].answers,[(1,True),(2,False)])

    def test_source_change_refuses_approval_and_stops_worker(self):
        owner=JobOwner(self.root,allow_auto_calibration=True)
        with mock.patch.object(collision.Scene,'require_motion_ready'), mock.patch.object(ui_jobs,'JobRunner',Runner):
            self.submit(owner,'auto-changed-02',self.body)
            self.wait(owner,'auto-changed-02','waiting')
            self.scene.write_text(self.scene.read_text()+' ')
            with self.assertRaisesRegex(ValueError,'scene changed'):
                owner.answer('auto-changed-02',1,True)
            self.assertEqual(Runner.instances[-1].answers,[])
            self.assertFalse(Runner.instances[-1].active)
            for _ in range(400):
                job=owner.get('auto-changed-02')
                if job['state']=='failed': break
                time.sleep(.01)
            self.assertEqual(job['state'],'failed')

    def test_plan_and_replay_command_shapes_and_plan_cannot_approve(self):
        from ur_handeye import dataset
        owner=JobOwner(self.root,allow_auto_calibration=True)
        with mock.patch.object(collision.Scene,'require_motion_ready'), mock.patch.object(ui_jobs,'JobRunner',Runner):
            plan=dict(self.body,action='plan')
            self.submit(owner,'auto-plan-0001',plan)
            self.wait(owner,'auto-plan-0001','waiting')
            self.assertIn('--dry-run',Runner.instances[-1].argv)
            with self.assertRaisesRegex(ValueError,'disabled'):
                owner.answer('auto-plan-0001',1,True)
            owner.answer('auto-plan-0001',1,False)
            self.wait(owner,'auto-plan-0001','done')
            fit=self.root/'fit'; fit.mkdir(); (fit/'session.json').write_text('{}'); (fit/'samples.jsonl').write_text('')
            (self.root/'calibration.json').write_text('{}')
            replay=dict(self.body,action='replay',session='fit',verification='fresh-verify',calibration='calibration.json')
            with mock.patch.object(dataset,'load_result',return_value={'type':'eye_in_hand','verdict':'good','session':str(fit)}):
                self.submit(owner,'auto-replay-01',replay)
                self.wait(owner,'auto-replay-01','waiting')
                argv=Runner.instances[-1].argv
                self.assertEqual(argv[argv.index('--mode')+1],'replay')
                self.assertEqual(argv[argv.index('--replay')+1],str(fit))
                self.assertEqual(argv[argv.index('--output')+1],str(self.root/'fresh-verify'))
                self.assertEqual(argv[argv.index('--intrinsics')+1],str(self.root/'calibration.json'))
                owner.answer('auto-replay-01',1,False)
                self.wait(owner,'auto-replay-01','done')

    def test_existing_session_is_preserved_before_worker(self):
        session=self.root/'old-session'; session.mkdir()
        (session/'samples.jsonl').write_text('preserve this data')
        owner=JobOwner(self.root,allow_auto_calibration=True)
        body=dict(self.body,session='old-session')
        with mock.patch.object(collision.Scene,'require_motion_ready'), \
             mock.patch.object(ui_jobs,'JobRunner',side_effect=AssertionError('worker created')):
            self.submit(owner,'existing-path-01',body)
            for _ in range(400):
                job=owner.get('existing-path-01')
                if job['state']=='failed': break
                time.sleep(.01)
            self.assertEqual(job['state'],'failed')
            self.assertIn('new automatic collection',job['error'])
            self.assertEqual((session/'samples.jsonl').read_text(),'preserve this data')

    def test_auto_job_rejects_manual_continue(self):
        owner=JobOwner(self.root,allow_auto_calibration=True)
        gate=threading.Event()
        with mock.patch.object(collision.Scene,'require_motion_ready'):
            owner.submit('unexpected-prompt-01','calibration',self.body,lambda _:gate.wait(5))
        self.addCleanup(gate.set)
        owner.runner=Runner()
        owner.event('unexpected-prompt-01',{'type':'request','kind':'manual','id':1,'summary':'unexpected'})
        with self.assertRaisesRegex(ValueError,'cannot accept manual'):
            owner.answer('unexpected-prompt-01',1,'')
        gate.set()

    def test_offline_smoke_refuses_auto_enabled_service(self):
        import io
        import runpy
        page=b'<meta name="module-token" content="test_token">'
        status=json.dumps({'app_id':'anodizing_modules_v1','allow_camera':False,
                           'allow_teaching':False,'allow_manual':False,
                           'allow_auto_calibration':True}).encode()
        smoke=Path(__file__).parents[2]/'tools/smoke.py'
        with mock.patch('urllib.request.urlopen',side_effect=[io.BytesIO(page),io.BytesIO(status)]):
            with self.assertRaisesRegex(SystemExit,'default offline mode'):
                runpy.run_path(str(smoke),run_name='__main__')

    def test_manual_job_motion_prompt_never_accepts_true(self):
        owner=JobOwner(self.root,allow_auto_calibration=True)
        gate=threading.Event()
        owner.submit('manual-motion-01','calibration',{'action':'manual'},lambda _:gate.wait(5))
        self.addCleanup(gate.set)
        owner.runner=Runner()
        owner.event('manual-motion-01',{'type':'request','kind':'motion','id':1,'summary':'no'})
        with self.assertRaisesRegex(ValueError,'disabled'):
            owner.answer('manual-motion-01',1,True)
        gate.set()

    def test_other_module_motion_prompt_never_accepts_true(self):
        owner=JobOwner(self.root,allow_auto_calibration=True)
        gate=threading.Event()
        owner.submit('other-module-01','teaching',{},lambda _:gate.wait(5))
        self.addCleanup(gate.set)
        owner.runner=Runner()
        owner.event('other-module-01',{'type':'request','kind':'motion','id':1,'summary':'no'})
        with self.assertRaisesRegex(ValueError,'disabled'):
            owner.answer('other-module-01',1,True)
        gate.set()
