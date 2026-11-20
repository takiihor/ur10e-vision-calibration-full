"""MCU protocol and persisted shared ownership; every socket is fake."""
import json
from pathlib import Path
import socket
import tempfile
import time
import threading
from types import SimpleNamespace
import unittest
from unittest import mock

from anodizing_web.jobs import JobOwner
from anodizing_web import agv, mcu
from test_agv_robot_motion import connect_parked


class FakeSocket:
    def __init__(self, response=b'Open', *, before=b'', send_error=None, on_send=None):
        self.response, self.before = response, before
        self.send_error, self.on_send = send_error, on_send
        self.sent = []
        self.closed = False

    def __enter__(self): return self
    def __exit__(self, *args): self.closed = True
    def settimeout(self, value): assert 0 < value <= 2

    def recv(self, count):
        name = 'response' if self.sent else 'before'
        data = getattr(self, name)
        if not data: raise socket.timeout()
        chunk, rest = data[:min(3, count)], data[min(3, count):]
        setattr(self, name, rest)
        return chunk

    def sendall(self, data):
        if self.on_send: self.on_send()
        self.sent.append(data)
        if self.send_error: raise self.send_error


class MCUTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root/'project').mkdir()
        (self.root/'project/project.json').write_text(json.dumps(dict(schema_version=1,
            type='marker_pick_project', settings={'robot_ip':'192.168.12.248'}, flow={})))
        self.owner = JobOwner(self.root, allow_manual_pick_place=True)

    def submit(self, command='3', request_id='mcu-request-123'):
        return mcu.submit(self.owner, dict(project='project', command=command, request_id=request_id, agv_off=True))

    def finish(self, request_id='mcu-request-123'):
        deadline = time.monotonic()+2
        while self.owner.active and time.monotonic() < deadline: time.sleep(.005)
        self.assertIsNone(self.owner.active)
        return self.owner.get(request_id)

    def test_each_fixed_ascii_command_and_reply_has_durable_intent(self):
        for index, (command, response) in enumerate((('0', b'FSR 1: -20 / FSR 2: 300\r\n'),
                ('1', b'tIGHT\x00'), ('2', b' Loose '), ('3', b'Open'))):
            with self.subTest(command=command):
                request_id = 'mcu-command-'+str(index)
                def persisted():
                    saved = json.loads((self.root/'.jobs'/f'{request_id}.json').read_text())
                    self.assertEqual(saved['mcu']['status'], 'dispatching')
                    self.assertEqual(saved['execution_uncertain'],command!='0')
                sock = FakeSocket(response, before=b'old stale response', on_send=persisted)
                with mock.patch.object(mcu.socket, 'create_connection', return_value=sock) as connect:
                    self.submit(command, request_id)
                    job = self.finish(request_id)
                self.assertEqual(job['state'], 'done', job['error'])
                self.assertEqual(sock.sent, [command.encode('ascii')])
                self.assertEqual(connect.call_args.args[0], ('192.168.12.248',54321))
                self.assertTrue(sock.closed)
                self.assertEqual(job['mcu']['rx_hex'], response.hex(' '))
                self.assertNotIn('item_state', job['mcu'])
                if command == '0': self.assertEqual(job['mcu']['fsr'], [-20,300])
                self.assertFalse(self.owner.execution_uncertain)

    def test_malformed_empty_oversized_and_wrong_replies_leave_uncertainty(self):
        for index, response in enumerate((b'',b'Open extra',b'Loose',b'\xffOpen',b'Open'+b' '*125)):
            with self.subTest(response=response):
                self.owner = JobOwner(self.root, allow_manual_pick_place=True)
                self.owner.execution_uncertain = False
                sock = FakeSocket(response)
                request_id = 'mcu-badreply-'+str(index)
                with mock.patch.object(mcu.socket,'create_connection',return_value=sock):
                    self.submit('3',request_id); job=self.finish(request_id)
                self.assertEqual(job['state'],'failed')
                self.assertEqual(job['mcu']['status'],'unknown')
                self.assertEqual(job['mcu']['rx_hex'],response[:129].hex(' '))
                self.assertTrue(self.owner.execution_uncertain)
                self.assertTrue(JobOwner(self.root).execution_uncertain)

    def test_status_parser_and_motion_ack_are_distinct(self):
        self.assertEqual(mcu.parse_reply('0',b'FSR1:12/FSR2:-4'), {'fsr':[12,-4]})
        for response in (b'Open',b'FSR1:x/FSR2:2',b'FSR1:1'):
            with self.assertRaises(ValueError): mcu.parse_reply('0',response)

    def test_moving_agv_blocks_standalone_actuation_and_allows_sensor_read(self):
        self.owner.agv = link = agv.Connection(self.owner)
        self.addCleanup(link.close)
        connect_parked(link)
        with link.lock:
            link.status = dict(link.status, stopped=False, actual_speed_m_h=500)
        sock = FakeSocket(b'FSR1:1/FSR2:2')
        with mock.patch.object(mcu.socket, 'create_connection', return_value=sock):
            with self.assertRaisesRegex(ValueError, 'AGV state unknown'):
                mcu.submit(self.owner, dict(project='project', command='3', request_id='mcu-actuation'))
            self.submit('0', 'mcu-sensor-read')
            self.assertEqual(self.finish('mcu-sensor-read')['state'], 'done')
        self.assertEqual(sock.sent, [b'0'])

    def test_serial_report_before_dispatch_cancels_declared_off_actuation(self):
        self.owner.agv = link = agv.Connection(self.owner)
        self.addCleanup(link.close)
        sock = FakeSocket()
        with (mock.patch.object(mcu.socket, 'create_connection', return_value=sock),
              mock.patch.object(mcu, '_drain', side_effect=lambda _: connect_parked(link))):
            self.submit()
            job = self.finish()
        self.assertEqual(job['state'], 'failed')
        self.assertEqual(job['mcu']['status'], 'not_sent')
        self.assertIn('AGV is reporting', job['error'])
        self.assertEqual(sock.sent, [])

    def test_missing_status_reply_does_not_create_actuator_uncertainty_or_replay(self):
        sock=FakeSocket(b'')
        with mock.patch.object(mcu.socket,'create_connection',return_value=sock) as connect:
            self.submit('0');job=self.finish();self.submit('0')
            self.assertEqual(connect.call_count,1)
        self.assertEqual(job['state'],'failed');self.assertEqual(job['mcu']['status'],'unknown')
        self.assertFalse(self.owner.execution_uncertain)
        self.assertFalse(JobOwner(self.root).execution_uncertain)
        with mock.patch.object(mcu.socket,'create_connection',return_value=FakeSocket(b'FSR1:1/FSR2:2')):
            self.submit('0','status-explicit-new');self.assertEqual(self.finish('status-explicit-new')['state'],'done')

    def test_connection_failure_is_not_sent_and_does_not_fence(self):
        with mock.patch.object(mcu.socket,'create_connection',side_effect=OSError('refused')):
            self.submit(); job=self.finish()
        self.assertEqual(job['mcu']['status'],'not_sent')
        self.assertFalse(self.owner.execution_uncertain)

    def test_send_failure_is_unknown_and_never_replayed(self):
        sock=FakeSocket(send_error=OSError('partial send'))
        with mock.patch.object(mcu.socket,'create_connection',return_value=sock) as connect:
            self.submit();job=self.finish();self.submit()
            self.assertEqual(connect.call_count,1)
        self.assertTrue(job['execution_uncertain'])
        restarted=JobOwner(self.root,allow_manual_pick_place=True)
        with mock.patch.object(mcu.socket,'create_connection',side_effect=AssertionError('replay')):
            old=mcu.submit(restarted,dict(project='project',command='3',request_id=job['id'],agv_off=True))
        self.assertEqual(old['state'],'failed')

    def test_validation_and_all_owner_fences_prevent_hardware(self):
        with mock.patch.object(mcu.socket,'create_connection',side_effect=AssertionError('hardware')):
            for command in ('4',3,'1\n',None,[],{}):
                with self.subTest(command=command), self.assertRaises(ValueError): self.submit(command)
            for field,value in (('allow_manual_pick_place',False),('execution_uncertain',True),
                    ('active','other-job'),('storage_error','disk full'),('closing',True)):
                old=getattr(self.owner,field);setattr(self.owner,field,value)
                try:
                    for command in ('0','1','2','3'):
                        with self.subTest(field=field,command=command), self.assertRaises(ValueError): self.submit(command)
                finally: setattr(self.owner,field,old)
            with self.assertRaises(ValueError): mcu.submit(self.owner,dict(project='project',command='3',request_id='valid-123',host='other'))

    def test_cached_get_never_connects_or_claims_sensor_state(self):
        with mock.patch.object(mcu.socket,'create_connection',side_effect=AssertionError('GET hardware')):
            state=mcu.capability(self.owner)
        self.assertTrue(state['available']);self.assertTrue(state['cached_only'])
        self.assertIsNone(state['last_known'])

    def test_storage_failure_before_dispatch_never_sends(self):
        sock=FakeSocket()
        original=self.owner._save
        def fail_dispatch(job):
            if job.get('mcu',{}).get('status')=='dispatching': raise OSError('disk full')
            original(job)
        with mock.patch.object(self.owner,'_save',side_effect=fail_dispatch), mock.patch.object(mcu.socket,'create_connection',return_value=sock):
            self.submit();job=self.finish()
        self.assertEqual(sock.sent,[])
        self.assertIn('disk full',self.owner.storage_error)
        self.assertEqual(job['state'],'failed')

    def parent(self):
        project, host, digest=mcu._project(self.root,'project')
        prompt=dict(id='grip-prompt',phase='grip',job_id='parent-pick')
        job=dict(id='parent-pick',module='manual-pick-place',auto_action='PICK',created=time.time(),
            state='waiting',pending=prompt,manual=dict(stage='awaiting_grip',prompt=prompt,operation='PICK',item_state='unknown'))
        self.owner.jobs[job['id']]=job;self.owner.active=job['id']
        self.owner.runner=SimpleNamespace(cancelled=threading.Event(),context=SimpleNamespace(
            provenance={'project':str(project)},hashes={str(project/'project.json'):digest},settings={'robot_ip':host}))
        self.owner.runner.state=lambda:{'manual':job['manual']}
        self.owner.motion_job=job['id']
        return {'job_id':job['id'],'prompt':prompt}

    def child(self,parent,command='1',request_id='child-request-123'):
        return mcu.submit(self.owner,dict(project='project',command=command,request_id=request_id,parent=parent))

    def wait_child(self,request_id='child-request-123'):
        deadline=time.monotonic()+2
        while self.owner.mcu_pending and time.monotonic()<deadline:time.sleep(.005)
        self.assertIsNone(self.owner.mcu_pending)
        return self.owner.get(request_id)

    def test_child_preserves_parent_prompt_runner_and_requires_separate_confirmation(self):
        parent=self.parent();runner=self.owner.runner
        with mock.patch.object(mcu.socket,'create_connection',return_value=FakeSocket(b'Tight')):
            self.child(parent);job=self.wait_child()
        self.assertEqual(job['state'],'done',job['error'])
        self.assertEqual(self.owner.active,'parent-pick');self.assertIs(self.owner.runner,runner)
        self.assertEqual(self.owner.motion_job,'parent-pick')
        self.assertEqual(self.owner.jobs['parent-pick']['pending'],parent['prompt'])
        self.assertEqual(self.owner.jobs['parent-pick']['manual']['item_state'],'unknown')
        self.owner.active=None;self.owner.runner=None
        with mock.patch.object(mcu.socket,'create_connection',side_effect=AssertionError('duplicate replay')):
            self.assertEqual(self.child(parent)['state'],'done')
            with self.assertRaises(ValueError):self.child(parent,command='3')

    def test_stop_before_dispatch_prevents_byte(self):
        parent=self.parent()
        def cancel_before_send():
            self.owner.runner.cancelled.set()
            return FakeSocket(b'Tight')
        with mock.patch.object(mcu.socket,'create_connection',side_effect=cancel_before_send):
            self.child(parent);job=self.wait_child()
        self.assertEqual(job['mcu']['status'],'not_sent')
        self.assertFalse(self.owner.execution_uncertain)

    def test_pending_child_blocks_new_owner_after_parent_cleanup_and_ack_keeps_other_fence(self):
        parent=self.parent();entered=threading.Event();release=threading.Event()
        class SlowSocket(FakeSocket):
            def recv(sock,count):
                if sock.sent and not entered.is_set():
                    entered.set();release.wait(1)
                return super().recv(count)
        try:
            with mock.patch.object(mcu.socket,'create_connection',return_value=SlowSocket(b'Tight')):
                self.child(parent);self.assertTrue(entered.wait(1))
                self.owner.active=None;self.owner.runner=None;self.owner.motion_job=None
                with self.assertRaises(ValueError):self.owner.submit('other-job-123','other',{'action':'verify'},lambda _:None)
                self.owner.execution_uncertain=True
                release.set();job=self.wait_child()
        finally:release.set()
        self.assertEqual(job['state'],'done',job['error'])
        self.assertTrue(self.owner.execution_uncertain)
        self.assertIsNone(self.owner.active);self.assertIsNone(self.owner.runner)

    def test_wrong_parent_phase_prompt_project_and_cancelled_are_rejected(self):
        parent=self.parent()
        with mock.patch.object(mcu.socket,'create_connection',side_effect=AssertionError('hardware')):
            with self.assertRaises(ValueError):self.submit()
            with self.assertRaises(ValueError):self.child(dict(parent,prompt=dict(parent['prompt'],id='stale')))
            self.owner.jobs['parent-pick']['manual']['stage']='awaiting_plan'
            with self.assertRaises(ValueError):self.child(parent)
            self.owner.jobs['parent-pick']['manual']['stage']='awaiting_grip'
            self.owner.runner.context.settings['robot_ip']='192.168.12.249'
            with self.assertRaises(ValueError):self.child(parent)

    def test_consumed_live_prompt_blocks_stale_persisted_wait(self):
        parent=self.parent()
        self.owner.runner.state=lambda:{'manual':dict(self.owner.jobs['parent-pick']['manual'],prompt=None)}
        with mock.patch.object(mcu.socket,'create_connection',side_effect=AssertionError('hardware')):
            with self.assertRaises(ValueError):self.child(parent)

    def test_project_changed_during_dispatch_persistence_prevents_byte(self):
        sock=FakeSocket();original=self.owner._save
        def edit_project(job):
            original(job)
            if job.get('mcu',{}).get('status')=='dispatching':
                path=self.root/'project/project.json'
                path.write_text(path.read_text()+' ')
        with mock.patch.object(self.owner,'_save',side_effect=edit_project),mock.patch.object(mcu.socket,'create_connection',return_value=sock):
            self.submit();job=self.finish()
        self.assertEqual(sock.sent,[])
        self.assertEqual(job['mcu']['status'],'not_sent')


if __name__=='__main__': unittest.main()
