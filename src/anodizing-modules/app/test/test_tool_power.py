"""Offline power protocol and shared-owner behavior; sockets are fake."""
import json
import os
import subprocess
import sys
from pathlib import Path
import struct
import tempfile
import threading
import time
import unittest
from unittest import mock

from anodizing_web.jobs import JobOwner


def package(kind, payload):
    return struct.pack('!IB', len(payload) + 5, kind) + payload


def state(timestamp=100, voltage=24, extra=b''):
    tool = struct.pack('!BBddfBffB', 0, 0, 0, 0, 48, voltage, .1, 30, 0)
    return package(16, package(0, struct.pack('!Q', timestamp)) + package(2, tool + extra))


class FakeSocket:
    def __init__(self, data=b'', *, fragment=3, send_error=None):
        self.data = data
        self.fragment = fragment
        self.sent = []
        self.timeouts = []
        self.closed = False
        self.send_error = send_error

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.closed = True

    def settimeout(self, timeout):
        assert 0 < timeout <= 3
        self.timeouts.append(timeout)

    def recv(self, count):
        count = min(count, self.fragment)
        chunk, self.data = self.data[:count], self.data[count:]
        return chunk

    def sendall(self, data):
        self.sent.append(data)
        if self.send_error:
            raise self.send_error


class ProtocolTests(unittest.TestCase):
    def test_tool_output_field_and_future_fields(self):
        from anodizing_web.tool_power import decode_state
        self.assertEqual(decode_state(state(123, 12, b'future')), (123, 12))
        self.assertEqual(decode_state(state(124, 0)), (124, 0))
        self.assertIsNone(decode_state(package(20, b'other message')))
        self.assertIsNone(decode_state(package(16, package(2, b'x' * 32))))

    def test_malformed_truncated_and_duplicate_fields_are_rejected(self):
        from anodizing_web.tool_power import decode_state
        malformed = [b'', state()[:-1], struct.pack('!IB', 4, 16),
                     package(16, b'x'), package(16, struct.pack('!IB', 4, 2)),
                     package(16, struct.pack('!IB', 1000, 2)),
                     package(16, package(0, b'123') + package(2, b'x'*32)),
                     package(16, package(0, struct.pack('!Q', 100)) + package(2, b'x'*31)),
                     package(16, package(0, struct.pack('!Q', 100))*2 + package(2, b'x'*32)),
                     state(voltage=25)]
        for message in malformed:
            with self.subTest(message=message), self.assertRaises(ValueError):
                decode_state(message)

    def test_fragmented_read_discards_initial_and_repeated_timestamps(self):
        from anodizing_web.tool_power import read_voltage
        sock = FakeSocket(state(100, 24) + state(100, 24) + state(101, 0) + state(102, 24))
        with mock.patch('anodizing_web.tool_power.socket.create_connection', return_value=sock) as connect:
            result = read_voltage('192.168.12.248', expected=24)
        self.assertEqual(result['controller_tool_output_voltage'], 24)
        self.assertEqual(result['controller_timestamp_us'], 102)
        self.assertEqual(result['source'], 'controller_primary_tool_data')
        self.assertEqual(connect.call_args.args[0], ('192.168.12.248', 30011))
        self.assertTrue(sock.closed)
        self.assertTrue(sock.timeouts)

    def test_stale_matching_initial_cannot_confirm_command(self):
        from anodizing_web.tool_power import read_voltage
        for data in (state(100,24), state(100,24)*2, state(100,24)+state(101,0),
                     state(100,0)+state(99,24)):
            with self.subTest(data=data), mock.patch('anodizing_web.tool_power.socket.create_connection', return_value=FakeSocket(data)):
                with self.assertRaises((OSError, ValueError, TimeoutError)):
                    read_voltage('192.168.12.248', expected=24)

    def test_truncated_oversized_and_invalid_frame_headers_are_bounded(self):
        from anodizing_web.tool_power import read_voltage, MAX_FRAME
        for data in (b'\x00', struct.pack('!I', MAX_FRAME+1), struct.pack('!I',4), state()[:-1]):
            sock = FakeSocket(data)
            with self.subTest(data=data[:10]), mock.patch('anodizing_web.tool_power.socket.create_connection', return_value=sock):
                with self.assertRaises((OSError, ValueError)):
                    read_voltage('192.168.12.248')
            self.assertTrue(sock.closed)

    def test_global_deadline_expires_even_if_stream_keeps_delivering(self):
        from anodizing_web.tool_power import read_voltage
        ticks = iter(i * .2 for i in range(100))
        with mock.patch('anodizing_web.tool_power.time.monotonic', side_effect=lambda:next(ticks)), mock.patch(
                'anodizing_web.tool_power.socket.create_connection', return_value=FakeSocket(state()*100, fragment=65536)):
            with self.assertRaises(TimeoutError):
                read_voltage('192.168.12.248')


class OwnerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root/'project').mkdir()
        self.project = {'schema_version':1, 'type':'marker_pick_project', 'settings':{'robot_ip':'192.168.12.248'}, 'flow':{}}
        self.save_project()
        self.owner = JobOwner(self.root, allow_manual_pick_place=True)
        self.body = {'project':'project', 'action':'on', 'item_state':'unknown', 'request_id':'tool-power-001'}

    def save_project(self):
        (self.root/'project/project.json').write_text(json.dumps(self.project))

    def wait(self, request_id='tool-power-001'):
        deadline = time.monotonic()+3
        while time.monotonic()<deadline:
            with self.owner.lock:
                if self.owner.active is None:
                    return self.owner.get(request_id)
            time.sleep(.002)
        self.fail('Offline worker did not finish')

    def run_power(self, *, action='on', voltage=24, data=None, send_error=None):
        from anodizing_web.tool_power import submit
        command = FakeSocket(send_error=send_error)
        reader = FakeSocket(data if data is not None else state(100,0)+state(101,voltage))
        sockets = [reader] if action=='read' else [command,reader]
        self.body.update(action=action)
        with mock.patch('anodizing_web.tool_power.socket.create_connection', side_effect=sockets) as connect:
            submit(self.owner, self.body)
            job=self.wait(self.body['request_id'])
        return job,command,reader,connect

    def test_explicit_on_is_only_fixed_secondary_command_and_keeps_teacher(self):
        teacher = mock.Mock()
        self.owner.teacher_service = teacher
        alignment = {'job_id':'old-alignment'}
        self.owner.current_alignment = alignment
        job,command,reader,connect=self.run_power()
        self.assertEqual(job['state'],'done',job['error'])
        self.assertEqual(command.sent,[b'sec tool_power():\n  set_tool_voltage(24)\nend\n'])
        self.assertEqual([c.args[0] for c in connect.call_args_list], [('192.168.12.248',30002),('192.168.12.248',30011)])
        self.assertEqual(job['result']['controller_tool_output_voltage'],24)
        self.assertFalse(self.owner.execution_uncertain)
        self.assertFalse(job['execution_uncertain'])
        self.assertTrue(command.closed and reader.closed)
        teacher.close.assert_not_called()
        self.assertIs(self.owner.current_alignment,alignment)

    def test_off_requires_explicit_operator_released_or_no_item(self):
        from anodizing_web.tool_power import submit
        for item in ('unknown','operator_confirmed_held',None,True,'released'):
            with self.subTest(item=item), self.assertRaisesRegex(ValueError,'Released|held'):
                submit(self.owner,dict(self.body,action='off',item_state=item))
        self.assertIsNone(self.owner.active)
        for i,item in enumerate(('operator_confirmed_released','no_confirmed_item')):
            self.body.update(request_id=f'tool-off-{i:03d}',item_state=item,action='off')
            with mock.patch('anodizing_web.tool_power.socket.create_connection', side_effect=[FakeSocket(),FakeSocket(state(100,24)+state(101,0))]) as connect:
                submit(self.owner,self.body)
                job=self.wait(self.body['request_id'])
            self.assertEqual(job['state'],'done',job['error'])
            self.assertEqual(job['result']['controller_tool_output_voltage'],0)

    def test_read_is_explicit_job_and_get_never_connects(self):
        from anodizing_web.tool_power import capability
        with mock.patch('anodizing_web.tool_power.socket.create_connection', side_effect=AssertionError('GET touched hardware')):
            self.assertIsNone(capability(self.owner)['last_known'])
        job,command,reader,connect=self.run_power(action='read',voltage=12)
        self.assertEqual(job['state'],'done',job['error'])
        self.assertEqual(connect.call_count,1)
        self.assertEqual(command.sent,[])
        with mock.patch('anodizing_web.tool_power.socket.create_connection', side_effect=AssertionError('GET touched hardware')):
            cached=capability(self.owner)
        self.assertEqual(cached['last_known']['controller_tool_output_voltage'],12)
        self.assertEqual(cached['last_known']['project'],str(self.root/'project'))

    def test_duplicate_and_restart_cannot_resend(self):
        from anodizing_web.tool_power import submit
        job,*_=self.run_power()
        self.owner=JobOwner(self.root,allow_manual_pick_place=True)
        with mock.patch('anodizing_web.tool_power.socket.create_connection', side_effect=AssertionError('duplicate sent')):
            duplicate=submit(self.owner,self.body)
            self.assertEqual(duplicate['id'],job['id'])
            with self.assertRaisesRegex(ValueError,'different request'):
                submit(self.owner,dict(self.body,action='read'))

    def test_all_owner_fences_and_waiting_jobs_block_power(self):
        from anodizing_web.tool_power import submit
        for field,value in (('active','plan-wait-001'),('execution_uncertain',True),('storage_error','disk full'),('closing',True),('allow_manual_pick_place',False)):
            with self.subTest(field=field), mock.patch('anodizing_web.tool_power.socket.create_connection', side_effect=AssertionError('fence bypass')):
                previous=getattr(self.owner,field)
                setattr(self.owner,field,value)
                with self.assertRaises(ValueError):submit(self.owner,self.body)
                setattr(self.owner,field,previous)
        release=threading.Event()
        self.owner.submit('plan-wait-001','calibration',{},lambda _:release.wait(2))
        try:
            self.owner.event('plan-wait-001',{'type':'request','kind':'motion','id':1})
            with self.assertRaisesRegex(ValueError,'Another job'):submit(self.owner,self.body)
        finally:release.set()
        self.wait('plan-wait-001')

    def test_partial_send_or_lost_readback_is_unknown_and_restart_fenced(self):
        from anodizing_web.tool_power import capability, submit
        for send_error in (None,TimeoutError('partial send')):
            with self.subTest(send_error=send_error):
                self.body['request_id']='uncertain-001' if send_error is None else 'uncertain-002'
                self.owner.execution_uncertain=False
                job,command,reader,connect=self.run_power(data=state(100,24),send_error=send_error)
                self.assertEqual(job['state'],'failed')
                self.assertIsNone(job['tool_power']['controller_tool_output_voltage'])
                self.assertTrue(job['execution_uncertain'])
                self.assertEqual(len(command.sent),1)
                self.assertTrue(self.owner.execution_uncertain)
                self.assertIsNone(capability(self.owner)['last_known']['controller_tool_output_voltage'])
                restarted=JobOwner(self.root,allow_manual_pick_place=True)
                self.assertTrue(restarted.execution_uncertain)
                with self.assertRaisesRegex(ValueError,'uncertain'):
                    submit(restarted,dict(self.body,request_id='next-tool-001'))

    def test_receive_only_read_survives_uncertainty_without_clearing_any_fence(self):
        from anodizing_web.tool_power import capability, submit
        self.owner.execution_uncertain = True
        self.owner.jobs['old-arm-job'] = {'id':'old-arm-job','module':'teaching','created':0,
                                        'state':'failed','execution_uncertain':True}
        self.body.update(action='read')
        with mock.patch('anodizing_web.tool_power.socket.create_connection',
                        return_value=FakeSocket(state(100,0)+state(101,24))) as connect:
            submit(self.owner,self.body)
            job=self.wait()
        self.assertEqual(job['state'],'done',job['error'])
        self.assertEqual([c.args[0] for c in connect.call_args_list],[('192.168.12.248',30011)])
        self.assertTrue(self.owner.execution_uncertain)
        self.assertTrue(self.owner.jobs['old-arm-job']['execution_uncertain'])
        self.assertTrue(capability(self.owner)['read_available'])
        self.assertFalse(capability(self.owner)['write_available'])
        with self.assertRaisesRegex(ValueError,'uncertain'):
            submit(self.owner,dict(self.body,action='on',request_id='next-write-001'))

    def test_process_death_at_send_preserves_fence_across_read_and_restart(self):
        from anodizing_web.tool_power import submit
        child = """
import os, sys, time
from pathlib import Path
from unittest import mock
from anodizing_web.jobs import JobOwner
from anodizing_web.tool_power import submit
class CrashSocket:
    def __enter__(self): return self
    def __exit__(self, *args): pass
    def settimeout(self, timeout): pass
    def sendall(self, data): os._exit(73)
owner = JobOwner(Path(sys.argv[1]), allow_manual_pick_place=True)
body = {'project':'project', 'action':'on', 'item_state':'unknown', 'request_id':'tool-power-001'}
with mock.patch('anodizing_web.tool_power.socket.create_connection', return_value=CrashSocket()):
    submit(owner, body)
    time.sleep(3)
os._exit(74)
"""
        process = subprocess.run([sys.executable, '-c', child, str(self.root)],
                                 env=os.environ.copy(), timeout=5)
        self.assertEqual(process.returncode, 73)
        self.owner = JobOwner(self.root, allow_manual_pick_place=True)
        old = self.owner.get(self.body['request_id'])
        self.assertEqual(old['state'], 'interrupted')
        self.assertTrue(old['execution_uncertain'])
        self.assertTrue(self.owner.execution_uncertain)
        read_body = dict(self.body, action='read', request_id='read-after-crash')
        with mock.patch('anodizing_web.tool_power.socket.create_connection',
                        return_value=FakeSocket(state(100, 0) + state(101, 24))) as connect:
            submit(self.owner, read_body)
            read_job = self.wait(read_body['request_id'])
        self.assertEqual(read_job['state'], 'done', read_job['error'])
        self.assertEqual([c.args[0] for c in connect.call_args_list], [('192.168.12.248', 30011)])
        self.owner = JobOwner(self.root, allow_manual_pick_place=True)
        self.assertTrue(self.owner.execution_uncertain)
        self.assertTrue(self.owner.get(self.body['request_id'])['execution_uncertain'])
        with mock.patch('anodizing_web.tool_power.socket.create_connection', side_effect=AssertionError('unexpected send')):
            with self.assertRaisesRegex(ValueError, 'uncertain'):
                submit(self.owner, dict(self.body, request_id='new-write-after-crash'))
            self.assertEqual(submit(self.owner, self.body)['id'], self.body['request_id'])

    def test_dispatch_intent_is_durable_before_electrical_send(self):
        from anodizing_web.tool_power import submit
        intents=[]
        class InspectSocket(FakeSocket):
            def sendall(inner,data):
                saved=json.loads((self.owner.directory/'tool-power-001.json').read_text())
                intents.append(saved['execution_uncertain'])
                self.assertEqual(saved['tool_power']['status'],'dispatching')
                super().sendall(data)
        with mock.patch('anodizing_web.tool_power.socket.create_connection',side_effect=[InspectSocket(),FakeSocket(state(100,0)+state(101,24))]):
            submit(self.owner,self.body);job=self.wait()
        self.assertEqual(intents,[True])
        self.assertEqual(job['state'],'done',job['error'])

    def test_connection_failure_before_send_never_fences_a_write(self):
        from anodizing_web.tool_power import submit
        for i, error in enumerate((ConnectionRefusedError('refused'), TimeoutError('connect timeout'))):
            with self.subTest(error=error):
                if i:
                    self.owner.execution_uncertain = False
                self.body['request_id'] = f'connect-failure-{i:03d}'
                def fail_connect(*args, **kwargs):
                    saved = json.loads((self.owner.directory / (self.body['request_id'] + '.json')).read_text())
                    self.assertEqual(saved['tool_power']['status'], 'preparing')
                    self.assertFalse(saved['execution_uncertain'])
                    raise error
                with mock.patch('anodizing_web.tool_power.socket.create_connection', side_effect=fail_connect) as connect:
                    submit(self.owner, self.body)
                    job = self.wait(self.body['request_id'])
                self.assertEqual(connect.call_count, 1)
                self.assertEqual(job['state'], 'failed')
                self.assertFalse(job['execution_uncertain'])
                self.assertFalse(self.owner.execution_uncertain)
                saved = json.loads((self.owner.directory / (self.body['request_id'] + '.json')).read_text())
                self.assertFalse(saved['execution_uncertain'])
                self.owner = JobOwner(self.root, allow_manual_pick_place=True)
                self.assertFalse(self.owner.execution_uncertain)
                with mock.patch('anodizing_web.tool_power.socket.create_connection', side_effect=[FakeSocket(), FakeSocket(state(100, 0) + state(101, 24))]):
                    next_id = f'after-connect-failure-{i:03d}'
                    submit(self.owner, dict(self.body, request_id=next_id))
                    self.assertEqual(self.wait(next_id)['state'], 'done')

    def test_storage_failure_before_send_has_no_electrical_effect(self):
        from anodizing_web.tool_power import submit
        original=self.owner._save
        def fail_intent(job):
            if job.get('execution_uncertain'):raise OSError('disk full')
            original(job)
        command = FakeSocket()
        with mock.patch.object(self.owner,'_save',side_effect=fail_intent), mock.patch('anodizing_web.tool_power.socket.create_connection', return_value=command):
            submit(self.owner,self.body);job=self.wait()
        self.assertEqual(job['state'],'failed')
        self.assertEqual(command.sent, [])
        self.assertTrue(self.owner.storage_error)

    def test_body_cannot_supply_script_port_or_robot_endpoint(self):
        from anodizing_web.tool_power import submit
        for change in ({'robot_ip':'127.0.0.1'},{'port':30001},{'script':'movej(...)'},{'action':'12v'}):
            with self.subTest(change=change), self.assertRaises(ValueError):submit(self.owner,dict(self.body,**change))

    def test_invalid_or_synthetic_saved_projects_never_connect(self):
        from anodizing_web.tool_power import submit
        for i,change in enumerate(({'simulation':True},{'type':'other'},{'schema_version':2},{'settings':{'robot_ip':'simulation'}},{'settings':{'robot_ip':'192.168.12.248:30002'}},{'settings':{'robot_ip':'127.0.0.1'}},{'settings':{'robot_ip':3232238840}})):
            self.project.update(change);self.save_project()
            self.body['request_id']=f'bad-project-{i:03d}'
            with mock.patch('anodizing_web.tool_power.socket.create_connection') as connect:
                submit(self.owner,self.body);job=self.wait(self.body['request_id'])
            self.assertEqual(job['state'],'failed')
            connect.assert_not_called()
            self.project={'schema_version':1,'type':'marker_pick_project','settings':{'robot_ip':'192.168.12.248'},'flow':{}}


if __name__=='__main__':unittest.main()
