import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest import mock
from urllib.parse import quote

from anodizing_web import calibration
from anodizing_web.jobs import JobOwner
import test_port


class ImmediateRunner:
    def __init__(self): self.active = False
    def start(self, argv): self.active = True
    def drain(self):
        if self.active:
            self.active = False
            yield {'type': 'done', 'code': 0}


class CandidatePaths(unittest.TestCase):
    def test_unused_future_inputs_do_not_block_manual_or_simulation(self):
        import ur_handeye.ui_jobs
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root/'board.json').write_text('{}')
            owner = JobOwner(root)
            owner.event = mock.Mock()
            body = {'session':'fresh','manifest':'board.json','verification':'future/missing',
                    'calibration':'missing.json','intrinsics':'missing.json','board':'calibration_stage2',
                    'robot_ip':'fake','serial':'fake','views':8}
            with mock.patch.object(ur_handeye.ui_jobs, 'JobRunner', ImmediateRunner):
                calibration.run(owner,root,dict(body,action='simulate'), 'unused-sim',False,True)
                manual = dict(body,action='manual',calibration='',intrinsics='')
                calibration.run(owner,root,manual,'unused-manual',False,True)

    def test_fresh_verification_and_confinement(self):
        import ur_handeye.ui_jobs
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp); (root/'board.json').write_text('{}'); (root/'cal.json').write_text('{}')
            fit=root/'fit'; fit.mkdir(); (fit/'session.json').write_text('{}'); (fit/'samples.jsonl').write_text('')
            other=root/'fresh'; other.mkdir(); (other/'session.json').write_text('{}'); (other/'samples.jsonl').write_text('')
            owner=JobOwner(root)
            owner.event = mock.Mock()
            body={'action':'verify','session':'fit','manifest':'board.json','calibration':'cal.json'}
            for value in ('missing', '../outside', 'fit'):
                with self.subTest(value=value), self.assertRaises(ValueError):
                    calibration.run(owner,root,dict(body,verification=value),'verify-invalid',False,True)
            with mock.patch.object(ur_handeye.ui_jobs,'JobRunner',ImmediateRunner):
                calibration.run(owner,root,dict(body,verification='fresh'),'verify-valid',False,True)
            manual={'action':'manual','session':'fit','fit_session':'fit','manifest':'board.json',
                    'calibration':'cal.json','robot_ip':'fake','serial':'fake'}
            with self.assertRaisesRegex(ValueError,'fresh verification'):
                calibration.run(owner,root,manual,'verify-same',False,True)
            with self.assertRaises(ValueError):
                calibration.run(owner,root,dict(manual,fit_session=''),'verify-empty',False,True)
            candidate=dict(manual,session='new-verify')
            with mock.patch('ur_handeye.dataset.load_result',return_value={'type':'eye_in_hand','verdict':'good','camera':{'simulated':True}}):
                with self.assertRaisesRegex(ValueError,'usable real'):
                    calibration.run(owner,root,candidate,'verify-simulated',False,True)
            with mock.patch('ur_handeye.dataset.load_result',return_value={'type':'eye_in_hand','verdict':'good','session':str(fit)}), \
                 mock.patch.object(ur_handeye.ui_jobs,'JobRunner',ImmediateRunner):
                calibration.run(owner,root,candidate,'verify-capture',False,True)


class CandidateTeacher(unittest.TestCase):
    setUp = test_port.PortTests.setUp
    request = test_port.PortTests.request
    wait_job = test_port.PortTests.wait_job

    def test_teacher_defaults_and_authenticated_artifacts(self):
        status,data=self.request('GET','/api/teacher-defaults')
        self.assertEqual(status,200)
        defaults=json.loads(data)
        self.assertEqual(defaults['settings']['target']['board'],'')
        self.assertEqual(defaults['settings']['project'].split('/')[0],'teaching')
        self.assertEqual(defaults['picking_boards'],[])
        self.assertEqual(defaults['settings']['motion_model'],'planar')
        self.assertEqual(self.request('GET','/api/teacher-defaults',headers={'X-Module-Token':'bad'})[0],403)
        project=self.root/'project'; project.mkdir()
        settings={'calibration':'cal.json','target':{'kind':'aruco','dictionary':'DICT_5X5_100','marker_id':7,'marker_length_mm':30}}
        import hashlib
        (project/'project.json').write_text(json.dumps({'settings':settings,'flow':{'taught':True,'exported':False},'inputs':{'calibration':hashlib.sha256((self.root/'cal.json').read_bytes()).hexdigest()}}))
        self.assertEqual(self.request('GET','/api/teacher-project?project=project')[0],200)
        self.assertEqual(self.request('GET','/api/teacher-project?project=..')[0],404)
        (project/'capture.jpg').write_bytes(b'fake-image')
        (project/'grasp_pick.json').write_text('{"test":true}')
        result={'project':{'project':str(project),'flow':{'taught':True,'exported':False}}}
        self.server.owner.submit('teacher-read-01','teaching',{},lambda _:result)
        self.wait_job('teacher-read-01')
        path='/api/teacher-file?job=teacher-read-01&name='
        self.assertEqual(self.request('GET',path+'grasp_pick.json')[0],200)
        self.assertEqual(self.request('GET',path+'grasp_pick.json',headers={'X-Module-Token':'bad'})[0],403)
        self.assertEqual(self.request('GET',path+'target.json')[0],404)
        (self.root/'cal.json').write_text('{}')
        self.assertEqual(self.request('GET',path+'grasp_pick.json')[0],404)
        self.assertEqual(self.request('GET',path+quote('../../etc/passwd'))[0],404)
        self.assertEqual(self.request('POST','/api/validation/move',{})[0],404)
        self.server.owner.submit('teacher-old-sim','teaching',{},lambda _:{'project':'project','simulation':True})
        self.wait_job('teacher-old-sim')
        self.assertEqual(self.request('GET','/api/teacher-file?job=teacher-old-sim&name=grasp_pick.json')[0],404)
