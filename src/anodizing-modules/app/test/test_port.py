import copy
import errno
import fcntl
import os
import socket
import http.client
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest import mock

from anodizing_web.jobs import JobOwner
from anodizing_web.server import Server
from anodizing_web.teaching import simulated
from anodizing_web.pick_place import run as simulated_task

FIXTURE = Path(__file__).parent / 'fixtures' / 'simulated_calibration.json'
TARGET = {'kind': 'aruco', 'dictionary': 'DICT_5X5_100', 'marker_id': 7, 'marker_length_mm': 30}


class PortTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / 'cal.json').write_bytes(FIXTURE.read_bytes())
        self.server = Server(0, self.root)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def test_static_assets_use_etag_without_losing_csp(self):
        def get(path, extra=None):
            connection = http.client.HTTPConnection('127.0.0.1', self.server.server_address[1], timeout=5)
            connection.request('GET', path, headers={'X-Module-Token': self.server.token, **(extra or {})})
            response = connection.getresponse()
            value = response.read()
            result = response.status, dict(response.getheaders()), value
            connection.close()
            return result
        status, headers, body = get('/style.css')
        self.assertEqual(status, 200)
        self.assertEqual(headers['Cache-Control'], 'no-cache')
        self.assertIn('ETag', headers)
        self.assertIn("frame-ancestors 'none'", headers['Content-Security-Policy'])
        status, headers, body = get('/style.css', {'If-None-Match': headers['ETag']})
        self.assertEqual(status, 304)
        self.assertEqual(body, b'')

    def request(self, method, path, data=None, headers=None):
        connection = http.client.HTTPConnection('127.0.0.1', self.server.server_address[1], timeout=5)
        request_headers = {'X-Module-Token': self.server.token}
        if headers:
            request_headers.update(headers)
        raw = None
        if data is not None:
            raw = json.dumps(data).encode()
            request_headers['Content-Type'] = 'application/json'
        connection.request(method, path, body=raw, headers=request_headers)
        response = connection.getresponse()
        content = response.read()
        connection.close()
        return response.status, content

    def wait_job(self, job_id):
        for _ in range(500):
            status, raw = self.request('GET', '/api/jobs/' + job_id)
            self.assertEqual(status, 200)
            job = json.loads(raw)
            if job['state'] not in ('running', 'waiting'):
                return job
            time.sleep(.01)
        self.fail('Job did not finish')

    def post_job(self, module, request_id, body):
        status, raw = self.request('POST', '/api/' + module, dict(body, request_id=request_id))
        self.assertEqual(status, 202, raw)
        return self.wait_job(request_id)

    def test_routes_host_origin_token_and_size(self):
        for path in ('/', '/calibration', '/teaching', '/pick-place', '/app.js', '/preview/tool-envelope.html', '/preview/robot-preview.js'):
            status, content = self.request('GET', path)
            self.assertEqual(status, 200)
            self.assertTrue(content)
        self.assertEqual(self.request('GET', '/', headers={'Host': 'attacker.example'})[0], 403)
        self.assertEqual(self.request('GET', '/api/status', headers={'X-Module-Token': 'wrong'})[0], 403)
        self.assertEqual(self.request('POST', '/api/teaching', {}, headers={'Origin': 'http://attacker.example'})[0], 403)
        self.assertEqual(self.request('POST', '/api/teaching', {'x': 'x' * 32768})[0], 400)
        self.assertEqual(self.request('POST', '/api/motion', {'request_id': 'motion-test'})[0], 404)

    def test_second_service_cannot_create_another_owner(self):
        with self.assertRaisesRegex(RuntimeError, 'already running'):
            Server(0, self.root / 'another-data-dir')

    def test_same_directory_lock_does_not_relabel_live_job(self):
        started, finish = threading.Event(), threading.Event()
        def task(_):
            started.set()
            finish.wait(5)
            return {'complete':True}
        self.server.owner.submit('live-lock-01', 'teaching', {}, task)
        self.assertTrue(started.wait(2))
        job_path = self.root / '.jobs/live-lock-01.json'
        original = job_path.read_bytes()
        alternate_lock = self.root / 'alternate-global-lock'
        alternate_lock.mkdir()
        # Bypass only the per-user lock so this specifically tests the data lock.
        with mock.patch('anodizing_web.server.tempfile.gettempdir', return_value=str(alternate_lock)):
            with self.assertRaisesRegex(RuntimeError, 'data directory'):
                Server(0, self.root)
        self.assertEqual(job_path.read_bytes(), original)
        self.assertEqual(json.loads(original)['state'], 'running')
        finish.set()
        self.assertEqual(self.wait_job('live-lock-01')['state'], 'done')

    def test_shutdown_holds_data_lock_until_worker_cleanup(self):
        started, finish = threading.Event(), threading.Event()
        def task(_):
            started.set()
            finish.wait(5)
            return {'complete':True}
        self.server.owner.submit('shutdown-lock-01', 'teaching', {}, task)
        self.assertTrue(started.wait(2))
        self.server.shutdown()
        closer = threading.Thread(target=self.server.server_close)
        closer.start()
        time.sleep(.1)
        self.assertTrue(closer.is_alive())
        fd = os.open(self.root/'.jobs/.owner.lock', os.O_RDWR)
        try:
            with self.assertRaises(BlockingIOError):
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            finish.set()
            closer.join(5)
            self.assertFalse(closer.is_alive())
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        finally:
            os.close(fd)

    def test_default_refuses_hardware_actions(self):
        from anodizing_web import camera
        with mock.patch.object(camera, 'board_preview') as capture:
            job = self.post_job('calibration', 'camera-default', {'action':'camera', 'manifest':'cal.json', 'serial':'x'})
            self.assertEqual(job['state'], 'failed')
            self.assertIn('--allow-camera', job['error'])
            capture.assert_not_called()
        job = self.post_job('calibration', 'manual-default', {'action':'manual'})
        self.assertEqual(job['state'], 'failed')
        self.assertIn('--allow-manual', job['error'])
        job = self.post_job('teaching', 'teach-default', {'action':'capture'})
        self.assertEqual(job['state'], 'failed')
        self.assertIn('--allow-teaching', job['error'])

    def test_opted_in_camera_and_teaching_use_existing_adapters(self):
        from anodizing_web import camera
        from ur_pick_place.vision import teach_ui
        self.server.allow_camera = True
        self.server.allow_teaching = True
        with mock.patch.object(camera, 'board_preview', return_value={'image_jpeg_base64':'fake'}) as capture:
            job = self.post_job('calibration', 'camera-allowed', {'action':'camera','manifest':'cal.json',
                'board':'calibration_stage2','serial':'123'})
        self.assertEqual(job['state'], 'done', job['error'])
        self.assertEqual(capture.call_args.kwargs, {'enabled':True, 'camera_provider':'realsense',
            'intrinsics':None, 'camera_fps':None})
        settings = {'project':'project','calibration':'cal.json','target':TARGET,'tcp':[0]*6,
                    'robot_ip':'fake','motion_model':'planar','approach_mm':100,'tcp_confirmed':True}
        fake = mock.Mock()
        fake.state.return_value = {'busy':None, 'error':'', 'project':{'project':str(self.root/'project')}}
        with mock.patch.object(teach_ui, 'TeachingService', return_value=fake):
            job = self.post_job('teaching', 'configure-readonly', {'action':'configure','settings':settings})
            self.assertEqual(job['state'], 'done', job['error'])
            self.assertEqual(fake.configure.call_args.args[0]['calibration'], str(self.root/'cal.json'))
            (self.root/'project').mkdir()
            (self.root/'project/project.json').write_text(json.dumps({'settings':settings}))
            job = self.post_job('teaching', 'capture-readonly', {'action':'capture','project':'project'})
            self.assertEqual(job['state'], 'done', job['error'])
            fake.submit.assert_called_once_with('capture', {'marker_unchanged':None})
        self.assertEqual(fake.close.call_count, 0)
        self.server.owner.shutdown()
        self.assertEqual(fake.close.call_count, 1)

    def test_simulated_handover_and_mismatch(self):
        teaching = {'action':'simulate', 'project':'project', 'calibration':'cal.json', 'target':TARGET}
        job = self.post_job('teaching', 'sim-teaching', teaching)
        self.assertEqual(job['state'], 'done', job['error'])
        base = {'project':'project', 'calibration':'cal.json', 'shift_mm':[0,0]}
        pick = self.post_job('pick-place', 'request-pick', dict(base, operation='PICK'))
        self.assertEqual(pick['state'], 'done', pick['error'])
        # Repeating the same request returns the stored result and cannot pick twice.
        duplicate = self.post_job('pick-place', 'request-pick', dict(base, operation='PICK'))
        self.assertEqual(duplicate['result'], pick['result'])
        status, _ = self.request('POST', '/api/pick-place', dict(base, operation='PLACE', request_id='request-pick'))
        self.assertEqual(status, 400)
        place = self.post_job('pick-place', 'request-place', dict(base, operation='PLACE', shift_mm=[10,-5]))
        self.assertEqual(place['state'], 'done', place['error'])
        self.assertGreater(place['result']['observation_time'], pick['result']['observation_time'])
        self.assertAlmostEqual(place['result']['target_tcp_pose_m_rad'][0] - pick['result']['target_tcp_pose_m_rad'][0], .01)
        self.assertAlmostEqual(place['result']['target_tcp_pose_m_rad'][1] - pick['result']['target_tcp_pose_m_rad'][1], -.005)
        wrong = json.loads(FIXTURE.read_text())
        wrong['intrinsics']['fx'] += 1
        (self.root / 'wrong.json').write_text(json.dumps(wrong))
        job = self.post_job('pick-place', 'wrong-calibration', dict(base, operation='PICK', calibration='wrong.json'))
        self.assertEqual(job['state'], 'failed')
        self.assertIn('does not match', job['error'])
        grasp = self.root / 'project' / 'grasp_pick.json'
        raw = json.loads(grasp.read_text()); raw['target']['marker_id'] += 1
        grasp.write_text(json.dumps(raw))
        job = self.post_job('pick-place', 'wrong-marker', dict(base, operation='PICK'))
        self.assertEqual(job['state'], 'failed')
        self.assertIn('target refused', job['error'])

    def test_selected_charuco_manifest_and_relocated_import(self):
        local = Path(__file__).resolve()
        candidates = [local.parents[2] / 'data/boards/stage2_charuco_realsense_80mm/board_manifest.json',
                      local.parents[3] / 'marker_assets/two_stage/stage2_charuco_realsense_80mm/board_manifest.json']
        source = next(path for path in candidates if path.is_file())
        (self.root/'board.json').write_bytes(source.read_bytes())
        target = {'kind':'charuco','board':'tank01','board_manifest':'board.json'}
        result = simulated(self.root, {'project':'charuco','calibration':'cal.json','target':target})
        self.assertTrue(result['simulation'])
        (self.root/'board.json').rename(self.root/'relocated.json')
        base = {'project':'charuco','calibration':'cal.json','manifest':'relocated.json','shift_mm':[0,0]}
        pick = simulated_task(self.root, dict(base, operation='PICK'), 'charuco-pick')
        place = simulated_task(self.root, dict(base, operation='PLACE', shift_mm=[30,0]), 'charuco-place')
        self.assertAlmostEqual(place['target_tcp_pose_m_rad'][0] - pick['target_tcp_pose_m_rad'][0], .03)
        simulated_task(self.root, dict(base, operation='PICK'), 'charuco-pick2')
        with (self.root/'relocated.json').open('a') as stream:
            stream.write(' ')
        with self.assertRaisesRegex(ValueError, 'manifest changed'):
            simulated_task(self.root, dict(base, operation='PLACE'), 'charuco-bad')

    def test_simulated_teaching_refuses_real_calibration_and_accepts_empty_directory(self):
        data = json.loads(FIXTURE.read_text())
        data.pop('simulation_truth', None)
        data['camera'].pop('simulated', None)
        (self.root / 'real.json').write_text(json.dumps(data))
        body = {'project':'new', 'calibration':'real.json', 'target':TARGET}
        with self.assertRaisesRegex(ValueError, 'requires a simulated calibration'):
            simulated(self.root, body)
        self.assertFalse((self.root / 'new').exists())
        (self.root / 'empty').mkdir()
        result = simulated(self.root, dict(body, project='empty', calibration='cal.json'))
        self.assertTrue(result['simulation'])
        self.assertTrue((self.root / 'empty/grasp_pick.json').is_file())

    def test_http_disconnect_does_not_cancel_job(self):
        from anodizing_web import calibration
        started, finish = threading.Event(), threading.Event()
        def work(owner, root, body, job_id, allow_camera, allow_manual):
            started.set()
            self.assertTrue(finish.wait(5))
            return {'finished': True}
        with mock.patch.object(calibration, 'run', side_effect=work):
            connection = http.client.HTTPConnection('127.0.0.1', self.server.server_address[1], timeout=5)
            payload = json.dumps({'request_id':'disconnect-001', 'action':'simulate'})
            connection.request('POST', '/api/calibration', payload,
                               {'X-Module-Token':self.server.token, 'Content-Type':'application/json'})
            connection.close()
            self.assertTrue(started.wait(3))
            status, _ = self.request('POST', '/api/teaching', {'request_id':'while-busy-01','action':'simulate'})
            self.assertEqual(status, 400)
            finish.set()
            job = self.wait_job('disconnect-001')
            self.assertEqual(job['state'], 'done')
            self.assertEqual(job['result'], {'finished':True})
            status, data = self.request('POST','/api/calibration',{'request_id':'disconnect-001','action':'simulate'})
            self.assertEqual(status, 202)
            self.assertEqual(json.loads(data)['result'], {'finished':True})

    def test_teaching_image_is_bounded_and_confined(self):
        (self.root / 'project').mkdir()
        (self.root / 'project/capture.jpg').write_bytes(b'fake-image')
        status, image = self.request('GET', '/api/teaching-image?project=project')
        self.assertEqual((status, image), (200, b'fake-image'))
        self.assertEqual(self.request('GET', '/api/teaching-image?project=..')[0], 404)
        (self.root / 'project/capture.jpg').unlink()
        (self.root / 'project/capture.jpg').symlink_to('/etc/passwd')
        self.assertEqual(self.request('GET', '/api/teaching-image?project=project')[0], 404)

    def test_paths_reject_escape_and_symlink(self):
        job = self.post_job('teaching', 'escape-path', {'action':'simulate', 'project':'../outside', 'calibration':'cal.json', 'target':TARGET})
        self.assertEqual(job['state'], 'failed')
        self.assertIn('outside', job['error'])
        (self.root / 'project').mkdir()
        (self.root / 'project' / 'link').symlink_to('/tmp')
        job = self.post_job('teaching', 'symlink-path', {'action':'simulate', 'project':'project', 'calibration':'cal.json', 'target':TARGET})
        self.assertEqual(job['state'], 'failed')
        self.assertIn('symbolic', job['error'])

    def test_busy_disconnect_reconnect_and_stale_answers(self):
        gate = threading.Event()
        calls = []
        def slow(job_id):
            calls.append(job_id)
            gate.wait(5)
            return {'finished':True}
        first = self.server.owner.submit('slow-job-01', 'calibration', {'action':'fake'}, slow)
        self.assertEqual(first['state'], 'running')
        with self.assertRaisesRegex(ValueError, 'owns'):
            self.server.owner.submit('other-job-01', 'teaching', {}, lambda _: None)
        same = self.server.owner.submit('slow-job-01', 'calibration', {'action':'fake'}, slow)
        self.assertEqual(same['id'], first['id'])
        class Runner:
            def answer(self, request_id, value):
                calls.append((request_id, value))
        self.server.owner.runner = Runner()
        self.server.owner.event('slow-job-01', {'type':'request','id':4,'kind':'manual','summary':'Capture?'})
        with self.assertRaisesRegex(ValueError, 'stale'):
            self.server.owner.answer('slow-job-01', 3, '')
        self.server.owner.answer('slow-job-01', 4, '')
        with self.assertRaisesRegex(ValueError, 'no longer'):
            self.server.owner.answer('slow-job-01', 4, '')
        # No client remains attached while the accepted function completes.
        gate.set()
        job = self.wait_job('slow-job-01')
        self.assertEqual(job['result'], {'finished':True})
        self.assertEqual(calls, ['slow-job-01', (4, '')])
        recovered = JobOwner(self.root).get('slow-job-01')
        self.assertEqual(recovered['result'], {'finished':True})


class StartupTests(unittest.TestCase):
    def test_occupied_port_preserves_error_and_releases_locks(self):
        with tempfile.TemporaryDirectory() as directory, socket.socket() as listener:
            listener.bind(('127.0.0.1', 0))
            listener.listen()
            with mock.patch('anodizing_web.server.tempfile.gettempdir', return_value=directory):
                with self.assertRaises(OSError) as error:
                    Server(listener.getsockname()[1], directory)
                self.assertEqual(error.exception.errno, errno.EADDRINUSE)
                server = Server(0, directory)
                server.server_close()


class ManualCollectionTests(unittest.TestCase):
    def test_eye_in_hand_manual_selects_receive_only_robot(self):
        from ur_handeye import cli, robot
        self.assertTrue(cli.READ_ONLY_MANUAL_CAPTURE)
        with tempfile.TemporaryDirectory() as tmp:
            args = cli.parser().parse_args(['collect','--robot-ip','fake','--manifest','fake.json',
                '--board','calibration_stage2','--output',str(Path(tmp)/'session'),
                '--mode','manual','--calibration-type','eye_in_hand'])
            fake_robot = mock.Mock()
            fake_robot.tcp_offset.return_value = [0.0] * 6
            fake_robot.limits = mock.Mock()
            fake_camera = mock.MagicMock()
            fake_camera.__enter__.return_value = fake_camera
            fake_camera.metadata = {'device': {'serial_number':'fake'}}
            collector = mock.Mock(usable=8)
            with mock.patch.object(robot, 'LocalCaptureRobot', return_value=fake_robot) as local, \
                 mock.patch.object(robot, 'URRobot', side_effect=AssertionError('control opened')), \
                 mock.patch.object(cli, '_camera', return_value=fake_camera), \
                 mock.patch.object(cli, 'BoardTarget') as board, \
                 mock.patch.object(cli, 'BoardObserver'), \
                 mock.patch.object(cli, 'Session') as session, \
                 mock.patch.object(cli, 'Collector', return_value=collector):
                board.return_value.describe.return_value = {'name':'calibration_stage2'}
                self.assertEqual(cli.cmd_collect(args, read_line=lambda _: 'q'), 0)
            local.assert_called_once()
            self.assertEqual(session.call_args.args[1]['pose_source'], 'primary_read_only')
            self.assertEqual(session.call_args.args[1]['calibration_type'], 'eye_in_hand')
            collector.run_manual.assert_called_once()
            fake_robot.close.assert_called_once()

    def test_manual_worker_prompt_survives_browser_absence(self):
        from anodizing_web.calibration import run
        import ur_handeye.ui_jobs
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'board.json').write_text('{}')
            owner = JobOwner(root)
            sent = []
            class FakeRunner:
                def __init__(self): self.phase = 0; self.active = True
                def start(self, argv):
                    sent.extend(argv)
                def drain(self):
                    if self.phase == 0:
                        self.phase = 1
                        yield {'type':'request', 'id':1, 'kind':'manual', 'summary':'Position the robot.'}
                    elif self.phase == 2:
                        self.phase = 3
                        self.active = False
                        yield {'type':'done', 'code':0}
                def answer(self, request_id, value):
                    self.phase = 2
            body = {'action':'manual','session':'new-session','manifest':'board.json',
                    'board':'calibration_stage2','robot_ip':'fake','serial':'fake','views':14}
            with mock.patch.object(ur_handeye.ui_jobs, 'JobRunner', FakeRunner):
                owner.submit('manual-wait-01','calibration',body,lambda jid:run(owner,root,body,jid,False,True))
                for _ in range(200):
                    job = owner.get('manual-wait-01')
                    if job['state'] == 'waiting': break
                    time.sleep(.01)
                self.assertEqual(job['pending']['id'], 1)
                self.assertIn('--mode', sent)
                self.assertIn('manual', sent)
                self.assertNotIn('--freedrive', sent)
                self.assertNotIn('--yes', sent)
                owner.answer('manual-wait-01', 1, 'q')
                for _ in range(200):
                    job = owner.get('manual-wait-01')
                    if job['state'] == 'done': break
                    time.sleep(.01)
                self.assertEqual(job['state'], 'done', job.get('error'))

    def test_manual_freedrive_refused_before_connection(self):
        from ur_handeye import cli, robot
        args = cli.parser().parse_args(['collect','--robot-ip','fake','--manifest','fake.json',
            '--board','calibration_stage2','--mode','manual','--freedrive'])
        with mock.patch.object(robot, 'LocalCaptureRobot') as local:
            with self.assertRaisesRegex(ValueError, 'freedrive'):
                cli.cmd_collect(args)
            local.assert_not_called()
