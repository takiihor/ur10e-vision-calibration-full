"""Public Teacher session/admission tests. Native device operations are fakes."""
import copy
import json
from pathlib import Path
import threading
from types import SimpleNamespace
import unittest
from unittest import mock

from test_port import PortTests, TARGET


class FakeTeacher:
    """Retains a capture only until configure/stop, like the native service."""
    def __init__(self, root):
        self.root = root
        self.lock = threading.RLock()
        self.project = None
        self.value = {'version':'0.3.1','busy':None,'error':'','failed':None,'completed':None,'project':None}
        self.started, self.release, self.stopped = (threading.Event() for _ in range(3))
        self.block = False
        self.closed = 0
        self.captures = 0

    def configure(self, settings):
        directory = Path(settings['project'])
        directory.mkdir(exist_ok=True)
        raw = {k:v for k,v in settings.items() if k not in ('project','tcp_confirmed')}
        (directory/'project.json').write_text(json.dumps({'settings':raw}))
        self.project = SimpleNamespace(directory=directory, settings=raw)
        self.value['project'] = {'project':str(directory),'settings':raw,'flow':{'captured':True,'taught':True},'validation':None}
        return self.state()

    def state(self):
        return copy.deepcopy(self.value)

    def submit(self, action, body):
        self.value.update(busy=action, error='', completed=None)
        self.started.set()
        def finish():
            if self.block:
                self.release.wait(5)
            self.captures += 1
            self.value['project']['validation'] = {'id':'capture-'+str(self.captures),'status':'captured'}
            self.value.update(busy=None, completed=action)
        threading.Thread(target=finish,daemon=True).start()

    def stop(self):
        self.stopped.set()
        capture = self.value.get('project',{}).get('validation')
        if capture:
            capture['status'] = 'cancelled'

    def heartbeat(self):
        pass

    def close(self):
        self.closed += 1
        self.stop()


class TeacherSessionTests(unittest.TestCase):
    setUp = PortTests.setUp
    request = PortTests.request
    wait_job = PortTests.wait_job
    post_job = PortTests.post_job

    def configure(self, fake):
        from ur_pick_place.vision import teach_ui
        self.server.allow_teaching = True
        patch = mock.patch.object(teach_ui, 'TeachingService', return_value=fake)
        patch.start()
        self.addCleanup(patch.stop)
        settings = {'project':'project','calibration':'cal.json','target':TARGET,'tcp':[0]*6,
                    'robot_ip':'fake','motion_model':'planar','approach_mm':100,'tcp_confirmed':True}
        job = self.post_job('teaching','teacher-setup',{'action':'configure','settings':settings})
        self.assertEqual(job['state'],'done',job['error'])

    def test_validation_capture_remains_in_owner_session_and_stop_revokes_it(self):
        fake = FakeTeacher(self.root)
        self.configure(fake)
        job = self.post_job('teaching','teacher-capture',{'action':'capture_validation','project':'project'})
        self.assertEqual(job['state'],'done',job['error'])
        self.assertEqual(job['result']['project']['validation']['id'],'capture-1')
        status, raw = self.request('GET','/api/teacher-state?job=teacher-capture')
        self.assertEqual(status,200,raw)
        self.assertEqual(json.loads(raw)['project']['validation']['id'],'capture-1')
        self.assertEqual(fake.closed,0,'An ordinary job must not destroy the Teacher session')
        status,raw = self.request('POST','/api/teacher-control',{'job_id':'teacher-capture','action':'stop'})
        self.assertEqual(status,200,raw)
        status,raw = self.request('GET','/api/teacher-state?job=teacher-capture')
        self.assertEqual(json.loads(raw)['project']['validation']['status'],'cancelled')
        self.assertEqual(self.request('POST','/api/teacher-control',{'job_id':'teacher-setup','action':'stop'})[0],400)

    def test_stop_reaches_busy_native_worker_without_releasing_admission(self):
        fake = FakeTeacher(self.root)
        self.configure(fake)
        fake.block = True
        self.addCleanup(fake.release.set)
        status,raw = self.request('POST','/api/teaching',{'request_id':'teacher-busy','action':'capture_validation','project':'project'})
        self.assertEqual(status,202,raw)
        self.assertTrue(fake.started.wait(2),'The actual admitted worker must start')
        status,raw = self.request('POST','/api/teacher-control',{'job_id':'teacher-busy','action':'stop'})
        self.assertEqual(status,200,raw)
        self.assertTrue(fake.stopped.is_set())
        self.assertEqual(self.server.owner.active,'teacher-busy')
        self.assertEqual(self.request('POST','/api/teaching',{'request_id':'competitor-job','action':'locate','project':'project'})[0],400)
        self.assertEqual(self.request('POST','/api/teacher-control',{'job_id':'teacher-busy','action':'heartbeat'},headers={'X-Module-Token':'wrong'})[0],403)
        fake.release.set()
        self.assertEqual(self.wait_job('teacher-busy')['state'],'done')
        self.assertIsNone(self.server.owner.active)

    def test_teacher_motion_is_explicitly_disabled_without_opt_in(self):
        status, raw = self.request('POST','/api/teaching',{'request_id':'teacher-disabled','action':'validate_move','project':'project'})
        self.assertEqual(status,400,raw)
        self.assertIn('--allow-teacher-motion',json.loads(raw)['error'])
        self.assertIsNone(self.server.owner.active)


    def test_adapter_persistence_error_stops_and_drains_before_releasing_owner(self):
        fake = FakeTeacher(self.root)
        self.configure(fake)
        fake.block = True
        failed, closing = threading.Event(), threading.Event()
        original_close = fake.close
        def close():
            fake.stopped.set()
            closing.set()
            fake.release.wait(5)
            original_close()
        fake.close = close
        self.addCleanup(fake.release.set)
        original_save = self.server.owner._save
        def save(job):
            if job['events'] and not failed.is_set():
                failed.set()
                raise OSError('injected event persistence failure')
            original_save(job)
        with mock.patch.object(self.server.owner, '_save', side_effect=save):
            status, raw = self.request('POST','/api/teaching',{'request_id':'teacher-event-fault','action':'capture_validation','project':'project'})
            self.assertEqual(status,202,raw)
            self.assertTrue(failed.wait(2),'Failure must occur after native submit')
            self.assertTrue(closing.wait(1),'Adapter failure must start native stop/cleanup')
            self.assertTrue(fake.stopped.is_set())
            self.assertEqual(self.server.owner.active,'teacher-event-fault')
            self.assertEqual(self.request('POST','/api/teaching',{'request_id':'competitor-fault','action':'locate','project':'project'})[0],400)
            fake.release.set()
            job = self.wait_job('teacher-event-fault')
            self.assertEqual(job['state'],'failed')
            self.assertIn('persistence failure',job['error'])
            self.assertIn('persistence failure',self.server.owner.status()['storage_error'])
            self.assertIsNone(self.server.owner.active)
