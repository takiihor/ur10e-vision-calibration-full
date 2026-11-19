"""Connected image and progress contracts, using only native fake boundaries."""
import json
from pathlib import Path
import threading
import unittest
from test_port import PortTests
from test_teacher_validation import FakeTeacher, TeacherSessionTests
from test_teacher_joint import JointTeacherIntegrationTests


class TeacherImageTests(unittest.TestCase):
    setUp=PortTests.setUp
    request=PortTests.request
    wait_job=PortTests.wait_job
    post_job=PortTests.post_job
    configure=TeacherSessionTests.configure

    def test_validation_image_is_authenticated_and_bound_to_current_capture(self):
        fake=FakeTeacher(self.root)
        self.configure(fake)
        self.post_job('teaching','teacher-image-capture',{'action':'capture_validation','project':'project'})
        (self.root/'project/validation.jpg').write_bytes(b'validation-image')
        path='/api/teaching-image?project=project&kind=validation&capture_id=capture-1'
        status,raw=self.request('GET',path)
        self.assertEqual(status,200,raw)
        self.assertEqual(raw,b'validation-image')
        self.assertEqual(self.request('GET',path,headers={'X-Module-Token':'wrong'})[0],403)
        self.assertEqual(self.request('GET',path.replace('capture-1','stale-capture'))[0],404)
        self.assertEqual(self.request('GET',path.replace('project=project','project=..'))[0],404)


    def test_slow_validation_image_reply_does_not_block_owner_or_controls(self):
        from unittest import mock
        from anodizing_web.server import Handler
        fake=FakeTeacher(self.root)
        self.configure(fake)
        self.post_job('teaching','teacher-image-slow',{'action':'capture_validation','project':'project'})
        (self.root/'project/validation.jpg').write_bytes(b'validation-image')
        path='/api/teaching-image?project=project&kind=validation&capture_id=capture-1'
        entered,release,controls_done=threading.Event(),threading.Event(),threading.Event()
        image_result,control_results,errors=[],[],[]
        original=Handler.reply
        def slow_reply(handler,status,body,mime='application/json; charset=utf-8',**kwargs):
            if status==200 and mime=='image/jpeg' and handler.path==path:
                entered.set()
                if not release.wait(5):
                    raise AssertionError('Fixture did not release the image reply')
            return original(handler,status,body,mime,**kwargs)
        def image_request():
            try:
                image_result.append(self.request('GET',path))
            except Exception as exc:
                errors.append(exc)
        def controls():
            try:
                control_results.append(self.request('GET','/api/status')[0])
                for action in ('heartbeat','stop'):
                    control_results.append(self.request('POST','/api/teacher-control',
                        {'job_id':'teacher-image-slow','action':action})[0])
            except Exception as exc:
                errors.append(exc)
            finally:
                controls_done.set()
        image_thread=threading.Thread(target=image_request)
        control_thread=threading.Thread(target=controls)
        with mock.patch.object(Handler,'reply',new=slow_reply):
            image_thread.start()
            try:
                self.assertTrue(entered.wait(2),'Authenticated image reply must be in flight')
                owner_free=self.server.owner.lock.acquire(timeout=.2)
                if owner_free:
                    self.server.owner.lock.release()
                service_free=fake.lock.acquire(timeout=.2)
                if service_free:
                    fake.lock.release()
                control_thread.start()
                controls_before_release=controls_done.wait(1)
            finally:
                release.set()
                image_thread.join(3)
                if control_thread.ident is not None:
                    control_thread.join(3)
        self.assertTrue(owner_free,'Image transmission must not hold the owner lock')
        self.assertTrue(service_free,'Image transmission must not hold the native session lock')
        self.assertTrue(controls_before_release,'Status, heartbeat and Stop must answer while the image reply is blocked')
        self.assertEqual(control_results,[200,200,200])
        self.assertEqual(image_result,[(200,b'validation-image')])
        self.assertEqual(errors,[])


class TeacherProgressTests(unittest.TestCase):
    setUp=JointTeacherIntegrationTests.setUp
    prepare=JointTeacherIntegrationTests.prepare
    public_server=JointTeacherIntegrationTests.public_server
    wait_idle=JointTeacherIntegrationTests.wait_idle

    def test_actual_native_preflight_reports_its_stage_while_sdk_query_is_pending(self):
        self.prepare()
        self.motion.close()
        self.robot.connected=True
        self.public_server()
        entered,release=threading.Event(),threading.Event()
        self.addCleanup(release.set)
        original=self.robot.getInverseKinematics
        def pending(*args):
            entered.set()
            release.wait(4)
            return original(*args)
        self.robot.getInverseKinematics=pending
        status,raw=self.request('POST','/api/teaching',{'request_id':'native-stage-test','action':'validate_move','project':'project','agv_off':True,
            'capture_id':self.record['id'],'path_confirmed':True,'joint_speed_deg_s':3,'collision_scene':'selected-scene.json',
            'workspace_min_m':[-.9,-.9,.02],'workspace_max_m':[.9,.9,1.2]})
        self.assertEqual(status,202,raw)
        self.assertTrue(entered.wait(2))
        status,raw=self.request('GET','/api/teacher-state?job=native-stage-test')
        self.assertEqual(status,200,raw)
        state=json.loads(raw)
        self.assertEqual(state['busy'],'validate_move')
        self.assertIn('Preflight',state['motion']['message'])
        self.assertEqual(self.joint_calls,[])
        release.set()


class OfflineTeacherCapabilityTests(unittest.TestCase):
    def check_tool(self, name, expected, argv):
        import io
        import runpy
        import tempfile
        from unittest import mock
        status={'app_id':'anodizing_modules_v1','allow_camera':False,'allow_teaching':False,
                'allow_manual':False,'allow_auto_calibration':False,'allow_teacher_motion':True,
                'owner':{'active':None,'storage_error':None}}
        page=b'<meta name="module-token" content="fixture_token">'
        script=Path(__file__).resolve().parents[2]/'tools'/name
        with tempfile.TemporaryDirectory() as tmp:
            status['data_dir']=tmp
            defaults={'picking_boards':[{'target':{'board':'tank01','kind':'charuco'}}]}
            values=[io.BytesIO(page)]
            if name == 'benchmark.py':
                values.append(io.BytesIO(json.dumps(defaults).encode()))
            values += [io.BytesIO(json.dumps(status).encode()),AssertionError('Tool advanced beyond offline capability guard')]
            with mock.patch('urllib.request.urlopen',side_effect=values), mock.patch('sys.argv',[str(script),*argv,str(Path(tmp)/'unused-result.json')] if argv else [str(script)]):
                caught=None
                try:
                    if name == 'benchmark.py':
                        runpy.run_path(str(script),run_name='benchmark_fixture')['main']()
                    else:
                        runpy.run_path(str(script),run_name='__main__')
                except BaseException as exc:
                    caught=exc
            self.assertIsInstance(caught,expected,'Teacher opt-in must refuse before simulation/data work: '+repr(caught))
            self.assertIn('offline',str(caught))
            if name == 'smoke.py':
                self.assertEqual(list(Path(tmp).iterdir()),[],'Offline guard must run before creating simulation artifacts')
            # Benchmark intentionally preserves a partial report even on refusal.

    def test_smoke_refuses_teacher_motion_enabled_service(self):
        self.check_tool('smoke.py',SystemExit,[])

    def test_benchmark_refuses_teacher_motion_enabled_service(self):
        self.check_tool('benchmark.py',RuntimeError,['--port','18870','--source-id','fixture','--fixture','fake','--output'])
