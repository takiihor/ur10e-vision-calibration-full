"""Selected-model integration checks. The local factory draft is an external fixture."""
import hashlib
import http.client
import json
from pathlib import Path
import shutil
import tempfile
import threading
import time
import unittest
from unittest import mock

from anodizing_web import calibration
from anodizing_web.jobs import JobOwner
from anodizing_web.server import Server
from ur_handeye import collision, ui_jobs
from test_auto_bridge import Runner

DRAFT = Path(__file__).resolve().parents[3] / 'ROBOT_MODEL_FACTORY_DRAFT.json'
EXAMPLE = Path(collision.__file__).parent / 'web/collision-scene.example.json'


@unittest.skipUnless(DRAFT.is_file(), 'Local factory-model draft is unavailable')
class SelectedModelBridgeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.model = self.root / 'factory.json'
        shutil.copyfile(DRAFT, self.model)
        self.digest = hashlib.sha256(self.model.read_bytes()).hexdigest()
        self.assertEqual(self.digest,'4e64c90f62e55ce90e440ecdafbdb64684c080f7a5f655e3a479a1e8b1e60fea')
        self.scene = self.root / 'scene.json'
        data = json.loads(EXAMPLE.read_text())
        data['model_sha256'] = self.digest
        self.scene.write_text(json.dumps(data))
        (self.root / 'board.json').write_text('{}')
        self.body = {'action':'collect','agv_off':True,'session':'new-session','manifest':'board.json',
                     'board':'calibration_stage2','robot_ip':'fake','serial':'fake',
                     'collision_scene':'scene.json','views':14,'tilt_deg':25,'roll_deg':30}
        self.server = Server(0,self.root,allow_auto_calibration=True,robot_model='factory.json')
        self.thread = threading.Thread(target=self.server.serve_forever,daemon=True)
        self.thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        Runner.instances.clear()

    def get(self,path):
        connection=http.client.HTTPConnection('127.0.0.1',self.server.server_address[1],timeout=10)
        connection.request('GET',path,headers={'X-Module-Token':self.server.token})
        response=connection.getresponse(); value=response.read(); status=response.status
        connection.close()
        return status,value

    def wait(self,job_id,state):
        for _ in range(400):
            job=self.server.owner.get(job_id)
            if job['state']==state:return job
            if job['state'] in ('failed','interrupted'):self.fail(str(job))
            time.sleep(.01)
        self.fail('Job did not reach '+state)

    def submit(self,job_id,body=None):
        body=body or self.body
        return self.server.owner.submit(job_id,'calibration',body,
            lambda jid:calibration.run(self.server.owner,self.root,body,jid,False,False))

    def test_preview_json_js_and_status_share_model_and_hash(self):
        status,raw=self.get('/preview/ur10e-model.json')
        self.assertEqual(status,200)
        self.assertEqual(raw,self.model.read_bytes())
        self.assertEqual(hashlib.sha256(raw).hexdigest(),self.digest)
        status,js=self.get('/preview/ur10e-model.js')
        self.assertEqual(status,200)
        lines=js.decode().splitlines()
        displayed=json.loads(lines[0].removeprefix('const UR10E_MODEL = ').removesuffix(';'))
        default=json.loads(lines[2].removeprefix('const DEFAULT_COLLISION_SCENE = ').removesuffix(';'))
        self.assertEqual(displayed,json.loads(raw))
        self.assertIn(self.digest,lines[1])
        self.assertEqual(default['model_sha256'],self.digest)
        self.assertFalse(default['confirmed']);self.assertFalse(default['environment_complete'])
        self.assertEqual(default['obstacles'],[])
        status,preview=self.get('/preview/robot-preview.js')
        self.assertEqual(status,200)
        self.assertNotIn(b'NOMINAL MODEL',preview)
        self.assertNotIn(b'UR10e nominal collision hulls',preview)
        status,raw=self.get('/api/status'); model=json.loads(raw)['robot_model']
        self.assertEqual(model['sha256'],self.digest)
        self.assertFalse(model['nominal']);self.assertTrue(model['valid'])

    def test_accepted_job_snapshots_model_and_uses_native_model_argument(self):
        with mock.patch.object(collision.Scene,'require_motion_ready'),mock.patch.object(ui_jobs,'JobRunner',Runner):
            self.submit('factory-job-01')
            job=self.wait('factory-job-01','waiting')
            model=job['auto_scene']['model']; snapshot=Path(model['snapshot'])
            self.assertEqual(snapshot.read_bytes(),self.model.read_bytes())
            self.assertEqual(snapshot.stat().st_mode & 0o777,0o400)
            self.assertEqual(model['sha256'],self.digest)
            argv=Runner.instances[-1].argv
            self.assertEqual(argv[argv.index('--robot-model')+1],str(snapshot))
            self.assertEqual(argv[argv.index('--collision-scene')+1],job['auto_scene']['snapshot'])
            self.server.owner.answer('factory-job-01',1,False)
            self.wait('factory-job-01','done')

    def test_changed_model_refuses_new_job_and_preview(self):
        self.model.write_bytes(self.model.read_bytes()+b' ')
        with mock.patch.object(ui_jobs,'JobRunner',side_effect=AssertionError('worker created')):
            with self.assertRaisesRegex(ValueError,'model changed'):
                self.submit('changed-model-01')
        self.assertEqual(self.get('/preview/ur10e-model.js')[0],409)
        self.assertFalse(json.loads(self.get('/api/status')[1])['robot_model']['valid'])

    def test_model_change_while_awaiting_approval_stops_worker(self):
        with mock.patch.object(collision.Scene,'require_motion_ready'),mock.patch.object(ui_jobs,'JobRunner',Runner):
            self.submit('changed-after-01')
            self.wait('changed-after-01','waiting')
            self.model.write_bytes(self.model.read_bytes()+b' ')
            with self.assertRaisesRegex(ValueError,'model changed'):
                self.server.owner.answer('changed-after-01',1,True)
            self.assertFalse(Runner.instances[-1].active)
            self.assertEqual(Runner.instances[-1].answers,[])

    def test_model_change_before_worker_is_refused(self):
        from ur_handeye import ui
        original=ui.build_command
        def change(*args,**kwargs):
            argv=original(*args,**kwargs)
            self.model.write_bytes(self.model.read_bytes()+b' ')
            return argv
        with mock.patch.object(collision.Scene,'require_motion_ready'), \
             mock.patch.object(ui,'build_command',side_effect=change), \
             mock.patch.object(ui_jobs,'JobRunner',side_effect=AssertionError('worker created')):
            self.submit('changed-before-worker')
            for _ in range(400):
                job=self.server.owner.get('changed-before-worker')
                if job['state']=='failed':break
                time.sleep(.01)
            self.assertEqual(job['state'],'failed')
            self.assertIn('model changed',job['error'])

    def test_model_snapshot_change_refuses_approval(self):
        with mock.patch.object(collision.Scene,'require_motion_ready'),mock.patch.object(ui_jobs,'JobRunner',Runner):
            self.submit('changed-snapshot-01')
            job=self.wait('changed-snapshot-01','waiting')
            snapshot=Path(job['auto_scene']['model']['snapshot'])
            snapshot.chmod(0o600)
            snapshot.write_bytes(snapshot.read_bytes()+b' ')
            with self.assertRaisesRegex(ValueError,'model changed'):
                self.server.owner.answer('changed-snapshot-01',1,True)
            self.assertFalse(Runner.instances[-1].active)
            self.assertEqual(Runner.instances[-1].answers,[])

    def test_wrong_scene_hash_refuses_before_worker(self):
        self.scene.write_bytes(EXAMPLE.read_bytes())
        with mock.patch.object(ui_jobs,'JobRunner',side_effect=AssertionError('worker created')):
            with self.assertRaisesRegex(ValueError,'different robot model'):
                self.submit('wrong-scene-01')

    def test_changed_hull_refuses_before_worker(self):
        model=json.loads(self.model.read_text())
        model['bodies'][0]['vertices'][0][0] += .01
        bad=self.root/'bad-model.json';bad.write_text(json.dumps(model))
        digest=hashlib.sha256(bad.read_bytes()).hexdigest()
        scene=json.loads(self.scene.read_text());scene['model_sha256']=digest
        self.scene.write_text(json.dumps(scene))
        owner=JobOwner(self.root,allow_auto_calibration=True,robot_model={'source':str(bad),'sha256':digest})
        with mock.patch.object(ui_jobs,'JobRunner',side_effect=AssertionError('worker created')):
            with self.assertRaises(ValueError):
                owner.submit('bad-geometry-01','calibration',self.body,lambda _:None)


class ModelPathTests(unittest.TestCase):
    def test_external_model_path_is_refused(self):
        with tempfile.TemporaryDirectory() as root, tempfile.TemporaryDirectory() as other:
            model=Path(other)/'model.json'
            model.write_text('{}')
            with self.assertRaisesRegex(ValueError,'outside'):
                Server(0,Path(root),robot_model=str(model))

    def test_default_nominal_preview_hash_matches_model(self):
        with tempfile.TemporaryDirectory() as root:
            server=Server(0,Path(root))
            thread=threading.Thread(target=server.serve_forever,daemon=True)
            thread.start()
            try:
                connection=http.client.HTTPConnection('127.0.0.1',server.server_address[1],timeout=10)
                connection.request('GET','/preview/ur10e-model.json')
                response=connection.getresponse();model=response.read()
                self.assertEqual(response.status,200)
                connection.close()
                connection=http.client.HTTPConnection('127.0.0.1',server.server_address[1],timeout=10)
                connection.request('GET','/preview/ur10e-model.js')
                response=connection.getresponse();script=response.read()
                self.assertEqual(response.status,200)
                connection.close()
                self.assertTrue(json.loads(model)['nominal'])
                self.assertIn(hashlib.sha256(model).hexdigest().encode(),script)
            finally:
                server.shutdown();server.server_close()
