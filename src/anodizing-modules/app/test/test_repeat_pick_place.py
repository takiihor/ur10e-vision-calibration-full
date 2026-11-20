"""Finite IPC repeat tests with fake robot, camera/checker and MCU only."""
import copy
import json
import threading
import time
from unittest import mock
import unittest
from types import SimpleNamespace

import test_manual_execution as manual_tests
from anodizing_web import agv
from test_agv_robot_motion import connect_parked


class RepeatTests(unittest.TestCase):
    prepare = manual_tests.ManualWorkerTests.prepare
    wait_for = manual_tests.ManualWorkerTests.wait_for
    wait_idle = manual_tests.ManualWorkerTests.wait_idle
    public_server = manual_tests.ManualWorkerTests.public_server
    wait_public = manual_tests.ManualWorkerTests.wait_public

    def setup_repeat(self, transfers=2):
        self.addCleanup(mock.patch.stopall)
        # Import before public_server patches the manual executor's module attributes.
        from anodizing_web import repeat_pick_place
        owner = self.public_server()
        self.context.settings.update(camera_provider='fake')
        self.context.hashes = {}
        self.context.provenance['project'] = str(self.root/'A')
        self.context.config.pick.observe_joints = list(self.start)
        self.context.config.place.observe_joints = list(self.start)
        self.contexts = {}
        for name in ('A','B'):
            for operation in ('PICK','PLACE'):
                ctx = copy.deepcopy(self.context)
                ctx.role = operation.lower()
                from dataclasses import replace
                ctx.model = replace(ctx.model, role=ctx.role)
                ctx.provenance['project'] = str(self.root/name)
                ctx.settings['calibration'] = 'fixture-calibration'
                self.contexts[name,operation] = ctx
        self.link = self.server.agv
        self.vehicle = connect_parked(self.link)
        self.vehicle.speed = 500
        self.recipe = SimpleNamespace(contexts=self.contexts, transfers=transfers, settle_s=0,
            stations={'A':2,'B':2}, agv_timeout_s=120, agv_speed_cap_m_h=1000,
            agv_approved=False, travel_pose=None,
            summary={'transfers':transfers,'A':'A','B':'B','end':'A' if transfers%2==0 else 'B',
                'approach_mm':{'A':40,'B':40},'settle_s':0,'recipe_sha256':'fixture-reviewed-recipe'}, check_inputs=lambda:None)
        mock.patch.object(repeat_pick_place,'load_recipe',return_value=self.recipe).start()
        from anodizing_web import manual_execution
        def observe(ctx,*args):
            obs = copy.deepcopy(self.observation)
            obs.role = ctx.role
            obs.robot['joints_rad'] = list(self.robot.q)
            obs.robot['tcp_pose_m_rad'] = list(self.robot.q)
            obs.timestamp = time.time()
            self.observed.append((ctx.provenance['project'],ctx.role))
            return obs
        self.observed=[]
        mock.patch.object(manual_execution,'observe_role',side_effect=observe).start()
        self.commands=[]
        self.command_states=[]
        def command(owner,worker,step,command):
            self.commands.append((step,command))
            self.command_states.append(worker.state()['manual']['item_state'])
            worker._tick()
            return {'command':command,'status':'replied','rx_text':{'0':'FSR1:0/FSR2:0','1':'Tight','3':'Open'}[command]}
        mock.patch('anodizing_web.mcu.run_automatic',side_effect=command).start()
        self.public_body.update(action='REPEAT',agv_off=False,project='A',place_project='B',transfers=transfers,
            connection_id=self.link.id,stop_generation=self.link.stop_generation,
            automatic_test_approved=True,settle_s=0)
        return owner

    def test_two_transfers_are_one_ipc_job_without_operator_confirmations(self):
        owner=self.setup_repeat()
        status,raw=self.request('POST','/api/manual-pick-place',self.public_body)
        self.assertEqual(status,202,raw)
        job=self.wait_public(lambda j:j['state'] not in ('running','waiting'))
        self.assertEqual(job['state'],'done',job['error'])
        self.assertEqual([command for _,command in self.commands],['0','3','1','3','3','1','3'])
        self.assertEqual(self.command_states[:2],['automatic_empty_declared','automatic_empty_declared'])
        self.assertEqual([(p.rsplit('/',1)[-1],role) for p,role in self.observed],
            [('A','pick'),('B','place'),('B','pick'),('A','place')])
        self.assertEqual(job['manual']['repeat']['completed_transfers'],2)
        self.assertEqual(job['manual']['item_state'],'open_acknowledged_unverified')
        self.assertNotIn('operator_confirmed',json.dumps(job['manual']))
        self.assertIsNone(owner.active)
        self.assertEqual([m[0] for m in self.robot.moves],['moveJ','moveJ','moveL','moveL']*4)
        self.assertEqual(len(list(owner.directory.glob('*.withdrawal.manual-path.json'))),4)

    def test_off_recipe_runs_without_a_station_or_agv_commands(self):
        self.setup_repeat(transfers=1)
        self.link.close()
        self.recipe.stations={'A':None,'B':None}
        self.public_body['agv_off']=True
        job=self.run_repeat()
        self.assertEqual(job['state'],'done',job['error'])
        self.assertEqual(job['agv_motion'],'off')
        record=job['manual']['repeat']['records'][0]
        self.assertEqual(record['agv_legs'],[])
        self.assertIsNone(record['agv_transport'])

    def configure_travel(self, *, drive=True):
        owner=self.setup_repeat()
        owner.allow_agv_in_recipe=drive
        self.recipe.stations={'A':2,'B':3}
        self.recipe.agv_approved=True
        self.recipe.travel_pose={'robot_ip':self.context.settings['robot_ip'],
            'joints_rad':[.32,.1,.45,0,0,0]}
        self.public_body['agv_manual_confirmed']=True
        self.calls=[]
        self.agv_stops=[]
        self.on_call=lambda:None
        write=self.vehicle.write
        def send(raw):
            frame=agv.parse_frame(raw)
            result=write(raw)
            if frame.command==0x23 and frame.data[0]==0x9D:
                self.calls.append(frame.data[1])
                self.vehicle.acknowledgements[-1]=time.monotonic()+.10
                self.vehicle.arrives_at=time.monotonic()+.35
                self.on_call()
            if frame.command==0x23 and (frame.data[:2] in (bytes((0x87,4)), bytes((0x5B,1)))):
                self.agv_stops.append(time.monotonic())
            return result
        self.vehicle.write=send
        return owner

    def run_repeat(self):
        status,raw=self.request('POST','/api/manual-pick-place',self.public_body)
        self.assertEqual(status,202,raw)
        return self.wait_public(lambda j:j['state'] not in ('running','waiting'),timeout=9)

    def test_two_transfers_move_agv_out_and_back(self):
        self.configure_travel()
        job=self.run_repeat()
        self.assertEqual(job['state'],'done',job['error'])
        self.assertEqual(self.calls,[3,2])
        self.assertEqual(self.vehicle.station,2)
        repeat=job['manual']['repeat']
        self.assertEqual([(t['source_station'],t['destination_station']) for t in repeat['records']],[(2,3),(3,2)])
        legs=[leg for transfer in repeat['records'] for leg in transfer['agv_legs']]
        self.assertEqual([leg['to_station'] for leg in legs],[3,2])
        for leg in legs:
            self.assertLess(0,leg['ack_s'])
            self.assertLess(leg['ack_s'],leg['arrival_s'])
        self.assertEqual(self.agv_stops,[])

    def assert_agv_abort(self, text, *, stop=True, robot_stop=False, alarm=True):
        job=self.run_repeat()
        self.assertEqual(job['state'],'failed',job)
        self.assertIn(text,job['error'])
        self.assertEqual(job['manual']['item_state'],'unknown')
        self.assertIn(text,job['manual']['repeat']['abort_reason'])
        self.assertEqual(bool(self.agv_stops),stop)
        self.assertEqual(bool(self.robot.stops),robot_stop)
        self.assertEqual(bool(job['manual'].get('agv_alarm')),alarm)

    def test_no_ack_stops_agv_even_if_it_arrives(self):
        self.configure_travel()
        self.on_call=self.vehicle.acknowledgements.clear
        self.assert_agv_abort('acknowledgement')

    def test_stale_status_during_travel(self):
        self.configure_travel()
        # Leave the response to the station command enabled, but lose status replies.
        originally=self.vehicle.write
        def send(raw):
            if self.calls and not self.agv_stops and agv.parse_frame(raw).command==0x26:
                return len(raw)
            return originally(raw)
        self.vehicle.write=send
        self.assert_agv_abort('status is stale')

    def test_stale_status_during_robot_motion(self):
        self.configure_travel()
        def begin(*args):
            if not self.robot.moves:
                self.vehicle.silent=True
                self.robot.duration=4.5
        self.robot.dispatch_hook=begin
        self.assert_agv_abort('status is stale',robot_stop=True)

    def test_agv_auto_restart_stops_robot(self):
        self.configure_travel()
        def begin(*args):
            if not self.robot.moves:
                self.vehicle.stopped=False
                self.robot.duration=1.5
        self.robot.dispatch_hook=begin
        self.assert_agv_abort('moved during robot work',robot_stop=True)

    def test_robot_leaves_travel_pose(self):
        self.configure_travel()
        self.on_call=lambda:self.robot.q.__setitem__(0,self.robot.q[0]+.1)
        self.assert_agv_abort('left the travel pose')

    def test_fault_during_travel(self):
        self.configure_travel()
        write=self.vehicle.write
        def report_fault(raw):
            result=write(raw)
            if self.calls and agv.parse_frame(raw).command==0x26:
                reply=agv.parse_frame(self.vehicle.buffer,offset=3)
                data=bytearray(reply.data)
                data[12]=17
                self.vehicle.buffer=agv.Frame(0x25,bytes(data),reply.prefix).encode()
            return result
        self.vehicle.write=report_fault
        self.assert_agv_abort('fault 17')

    def test_persistent_obstacle_stops_agv(self):
        self.configure_travel()
        self.on_call=lambda:setattr(self.vehicle,'arrives_at',None)
        write=self.vehicle.write
        def report_obstacle(raw):
            result=write(raw)
            if self.calls and agv.parse_frame(raw).command==0x26:
                reply=agv.parse_frame(self.vehicle.buffer,offset=3)
                data=bytearray(reply.data)
                data[5]=1
                self.vehicle.buffer=agv.Frame(0x25,bytes(data),reply.prefix).encode()
            return result
        self.vehicle.write=report_obstacle
        with mock.patch('anodizing_web.recipe_agv.OBSTACLE_S', .25):
            self.assert_agv_abort('obstacle stop exceeded')

    def test_endpoint_requires_a_fresh_arrival_at_requested_station(self):
        visit, report, now = self.travel_report()
        visit.status = dict(report, stopped=True, actual_speed_m_h=0, station_arrival=True, obstacle_stop=False)
        visit.status_at = now
        visit.station = 2
        with self.assertRaisesRegex(ValueError, 'Park the AGV at station 3'):
            visit.require_endpoint(3)
        visit.require_endpoint(2)

    def travel_report(self, **changes):
        from anodizing_web.recipe_agv import StationVisit

        visit = StationVisit(None, None)
        now = time.monotonic()
        visit.sent = visit.pose_at = now - .1
        visit.travel_deadline = now + 2
        visit.speed_cap = 1000
        visit.first_travel_report = True
        visit.leg = {'to_station': 3}
        report = dict(mode=2, target_station=3, set_speed_m_h=500, fault_code=0, paused=False,
                      current_station=2, max_stations=3, stopped=False, actual_speed_m_h=500)
        report.update(changes)
        return visit, report, now

    def test_changed_target_during_travel_aborts(self):
        visit, report, now = self.travel_report(target_station=1)
        visit._check_travel(report, now, [(report, now)], .05, now)
        with self.assertRaisesRegex(ValueError, 'reported target station 1'):
            visit._check_travel(report, now + .01, [(report, now + .01)], .05, now + .01)

    def test_changed_mode_during_travel_aborts(self):
        visit, report, now = self.travel_report(mode=0)
        with self.assertRaisesRegex(ValueError, 'station-call mode'):
            visit._check_travel(report, now, [(report, now)], .05, now)

    def test_speed_increase_during_travel_aborts(self):
        visit, report, now = self.travel_report(set_speed_m_h=1200)
        with self.assertRaisesRegex(ValueError, 'above the recipe cap'):
            visit._check_travel(report, now, [(report, now)], .05, now)

    def test_repeat_keeps_interruption_record_when_both_stop_attempts_raise(self):
        from anodizing_web.repeat_pick_place import RepeatExecution

        worker = RepeatExecution.__new__(RepeatExecution)
        worker.station_visit = None
        worker._repeat_transfers = mock.Mock(side_effect=ValueError('original interruption'))
        worker._stop_motion = mock.Mock(side_effect=RuntimeError('native Stop failed'))
        worker.manual = {'repeat': {}}
        worker.transfers = worker.agv_legs = []
        visit = worker.station_visit = mock.Mock(error=None, alarm=None, stop_sent=False, leg=None, station=2)
        worker.recipe = SimpleNamespace(stations={'A': 2, 'B': 2})
        worker.drive_agv = False
        worker._tick = worker._publish = mock.Mock()
        visit.abort.side_effect = OSError('radio Stop failed')
        with self.assertRaisesRegex(ValueError, 'original interruption'):
            worker._manual_sequence(1)
        record = worker._interruption_details()
        self.assertEqual(record['repeat']['abort_reason'], 'original interruption')
        self.assertEqual(record['robot_stop_error'], 'native Stop failed')
        self.assertEqual(record['agv_stop_error'], 'radio Stop failed')

    def test_travel_times_out(self):
        self.configure_travel()
        self.recipe.agv_timeout_s=1
        self.on_call=lambda:setattr(self.vehicle,'arrives_at',None)
        self.assert_agv_abort('travel timed out')

    def test_speed_over_cap_refuses_call(self):
        self.configure_travel()
        self.vehicle.speed=1200
        self.assert_agv_abort('above the recipe cap',stop=False,alarm=False)
        self.assertEqual(self.calls,[])

    def test_flag_off_checks_only_and_refuses_cross_station_run(self):
        self.configure_travel(drive=False)
        self.assert_agv_abort('check-only',stop=False,alarm=False)
        self.assertEqual(self.calls,[])
        self.assertEqual(self.robot.moves,[])

    def test_confirmation_is_required_even_with_startup_flag(self):
        self.configure_travel()
        self.recipe.agv_approved=False
        self.assert_agv_abort('Confirm the Manual mode',stop=False,alarm=False)
        self.assertEqual(self.calls,[])
        self.assertEqual(self.robot.moves,[])

    def test_flag_off_refuses_wrong_initial_station_even_for_one_station(self):
        self.configure_travel(drive=False)
        self.recipe.stations={'A':3,'B':3}
        self.assert_agv_abort('check-only',stop=False,alarm=False)
        self.assertEqual(self.calls,[])
        self.assertEqual(self.robot.moves,[])

    def test_same_station_never_calls_agv_even_with_drive_approval(self):
        self.configure_travel()
        self.recipe.stations={'A':3,'B':3}
        self.assert_agv_abort('Both endpoints use station 3',stop=False,alarm=False)
        self.assertEqual(self.calls,[])
        self.assertEqual(self.robot.moves,[])

    def test_recipe_refuses_simulator_at_real_execution_boundary(self):
        from anodizing_web.recipe_agv import require_serial
        with self.assertRaisesRegex(ValueError,'serial AGV'):
            require_serial('simulator')

    def test_mcu_failure_ends_entire_run_before_next_motion(self):
        self.setup_repeat()
        def fail(owner,worker,step,command):
            self.commands.append((step,command))
            if command=='1':raise ValueError('MCU outcome unknown')
            return {'command':command,'status':'replied'}
        with mock.patch('anodizing_web.mcu.run_automatic',side_effect=fail):
            status,raw=self.request('POST','/api/manual-pick-place',self.public_body)
            self.assertEqual(status,202,raw)
            job=self.wait_public(lambda j:j['state'] not in ('running','waiting'))
        self.assertEqual(job['state'],'failed')
        self.assertEqual(job['manual']['item_state'],'unknown')
        self.assertEqual([m[0] for m in self.robot.moves],['moveJ','moveJ','moveL'])
        self.assertEqual([c for _,c in self.commands],['0','3','1'])

    def test_stop_preflight_has_no_motion_and_exact_request_cannot_restart(self):
        from anodizing_web.jobs import JobOwner
        owner=self.setup_repeat()
        entered=threading.Event();release=threading.Event();self.addCleanup(release.set)
        def hold(owner,worker,step,command):
            self.commands.append((step,command));entered.set();release.wait(2);worker._tick()
        with mock.patch('anodizing_web.mcu.run_automatic',side_effect=hold):
            self.assertEqual(self.request('POST','/api/manual-pick-place',self.public_body)[0],202)
            self.assertTrue(entered.wait(2))
            response=self.request('POST','/api/manual-control',dict(job_id=self.public_body['request_id'],action='stop'))
            self.assertEqual(response[0],200,response)
            release.set()
            job=self.wait_public(lambda j:j['state'] not in ('running','waiting'))
        self.assertEqual(job['state'],'failed')
        self.assertEqual(job['manual']['item_state'],'unknown')
        self.assertEqual(self.robot.moves,[])
        self.assertEqual(self.request('POST','/api/manual-pick-place',self.public_body)[0],202)
        restarted=JobOwner(self.root)
        self.assertEqual(restarted.get(job['id'])['state'],'failed')
        self.assertEqual(self.commands,[('preflight','0')])

    def test_bad_observation_stops_after_checked_observation_transit(self):
        self.setup_repeat();self.observation.quality['pixels_per_module']=2
        self.assertEqual(self.request('POST','/api/manual-pick-place',self.public_body)[0],202)
        job=self.wait_public(lambda j:j['state'] not in ('running','waiting'))
        self.assertEqual(job['state'],'failed');self.assertIn('3 pixels/module',job['error'])
        self.assertEqual([m[0] for m in self.robot.moves],['moveJ'])
        self.assertEqual([c for _,c in self.commands],['0'])

    def test_vertical_refusal_never_uses_joint_descent_or_continues(self):
        self.setup_repeat();self.vertical_status='unresolved'
        self.assertEqual(self.request('POST','/api/manual-pick-place',self.public_body)[0],202)
        job=self.wait_public(lambda j:j['state'] not in ('running','waiting'))
        self.assertEqual(job['state'],'failed');self.assertIn('refused',job['error'])
        self.assertEqual([m[0] for m in self.robot.moves],['moveJ','moveJ'])
        self.assertEqual([c for _,c in self.commands],['0','3'])

    def test_observation_joint_bounds_refuse_before_movement(self):
        self.setup_repeat();self.contexts['A','PICK'].config.pick.observe_joints=[7]*6
        self.assertEqual(self.request('POST','/api/manual-pick-place',self.public_body)[0],202)
        job=self.wait_public(lambda j:j['state'] not in ('running','waiting'))
        self.assertEqual(job['state'],'failed');self.assertIn('operating bounds',job['error'])
        self.assertEqual(self.robot.moves,[])

    def test_other_endpoint_edit_during_joint_dispatch_persistence_prevents_motion(self):
        from anodizing_web import teacher_motion
        self.setup_repeat()
        path=self.root/'other-project.json';path.write_text('original')
        def check_inputs():
            if path.read_text()!='original':raise ValueError('Other endpoint input changed')
        self.recipe.check_inputs=check_inputs
        for context in self.contexts.values():context.check_inputs=check_inputs
        atomic=teacher_motion.atomic_json
        def persist(file,value):
            atomic(file,value)
            if str(file).endswith('.observation-transit.manual-path.json') and value.get('outcome')=='dispatching':
                path.write_text('changed')
        with mock.patch.object(teacher_motion,'atomic_json',side_effect=persist):
            self.assertEqual(self.request('POST','/api/manual-pick-place',self.public_body)[0],202)
            job=self.wait_public(lambda j:j['state'] not in ('running','waiting'))
        self.assertEqual(job['state'],'failed')
        self.assertIn('Other endpoint input changed',job['error'])
        self.assertEqual(self.robot.moves,[])


if __name__=='__main__':unittest.main()

class AutomaticMCUTests(unittest.TestCase):
    def setUp(self):
        import tempfile
        from pathlib import Path
        from anodizing_web.jobs import JobOwner
        from anodizing_web.tool_power import _project
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);(self.root/'project').mkdir()
        (self.root/'project/project.json').write_text(json.dumps(dict(schema_version=1,type='marker_pick_project',
            settings={'robot_ip':'192.168.12.248'},flow={})))
        project,host,digest=_project(self.root,'project')
        self.owner=JobOwner(self.root,allow_manual_pick_place=True)
        self.parent='p'*80
        self.owner.active=self.parent
        self.owner.jobs[self.parent]=dict(id=self.parent,auto_action='REPEAT')
        self.steady=True
        def stationary():
            if not self.steady:raise ValueError('Robot moved before byte')
        self.worker=SimpleNamespace(parent_job_id=self.parent,step='t001-pick',cancelled=threading.Event(),
            _shutdown=threading.Event(),_stationary=stationary,
            context=SimpleNamespace(provenance={'project':str(project)},settings={'robot_ip':host},
                hashes={str(project/'project.json'):digest},check_inputs=lambda:None))
        self.owner.runner=self.worker

    def test_long_parent_actuator_unknown_survives_real_jobowner_reload(self):
        from anodizing_web import mcu
        from anodizing_web.jobs import JobOwner
        from test_mcu import FakeSocket
        sock=FakeSocket(b'bad reply')
        with mock.patch.object(mcu.socket,'create_connection',return_value=sock):
            with self.assertRaises(ValueError):mcu.run_automatic(self.owner,self.worker,'t001-pick','1')
        child=[job for job in self.owner.jobs.values() if job.get('parent_job')==self.parent][0]
        self.assertLessEqual(len(child['id']),80)
        restarted=JobOwner(self.root)
        self.assertTrue(restarted.execution_uncertain)
        self.assertIn(child['id'],restarted.jobs)
        self.assertEqual(sock.sent,[b'1'])

    def test_stationary_rechecked_after_dispatch_persistence(self):
        from anodizing_web import mcu
        from test_mcu import FakeSocket
        save=self.owner._save
        def persist(job):
            save(job)
            if job.get('mcu',{}).get('status')=='dispatching':self.steady=False
        sock=FakeSocket(b'Tight')
        with mock.patch.object(self.owner,'_save',side_effect=persist),mock.patch.object(mcu.socket,'create_connection',return_value=sock):
            with self.assertRaisesRegex(ValueError,'moved'):mcu.run_automatic(self.owner,self.worker,'t001-pick','1')
        self.assertEqual(sock.sent,[])
        self.assertFalse(self.owner.execution_uncertain)

class RecipeTests(unittest.TestCase):
    """Native target/hash loader with synthetic fixtures; hardware qualification explicitly bypassed."""
    def setUp(self):
        from pathlib import Path
        import test_manual_pick_place as manual_data
        from anodizing_web.teaching import simulated
        manual_data.ManualDataTests.setUp(self)
        simulated(self.root,{'project':'B','calibration':'cal.json',
            'target':{'kind':'charuco','board':'tank02','board_manifest':'board.json'}})
        for name in ('project','B'):
            path=self.root/name/'observation.json';data=json.loads(path.read_text())
            data['quality']['pixels_per_module']=4.0;path.write_text(json.dumps(data))
        (self.root/'scene.json').write_text('{}')
        from anodizing_web import manual_execution, manual_pick_place
        from ur_pick_place.vision.handeye import HandEyeCalibration
        prepare=manual_pick_place.prepare
        def fixture_prepare(*args):
            result=prepare(*args);result['simulation_source']=False;return result
        self.addCleanup(mock.patch.stopall)
        mock.patch.object(manual_execution,'prepare',side_effect=fixture_prepare).start()
        mock.patch.object(HandEyeCalibration,'require_hardware').start()
        self.body.update(place_project='B',action='REPEAT',transfers=2,settle_s=1,
            collision_scene='scene.json',station_a=2,initial_item_state='operator_confirmed_released')

    def load(self,body=None,approved=False):
        from anodizing_web.repeat_pick_place import load_recipe
        return load_recipe(self.root,body or self.body,require_approval=approved)

    def test_distinct_projects_keep_native_target_models_and_readonly_sources(self):
        before={str(p):p.read_bytes() for p in self.root.rglob('*') if p.is_file()}
        recipe=self.load()
        for name,board in (('A','tank01'),('B','tank02')):
            for operation in ('PICK','PLACE'):
                ctx=recipe.contexts[name,operation]
                self.assertEqual(ctx.model.target['board'],board)
                self.assertEqual(ctx.target.describe()['board'],board)
                self.assertEqual(ctx.model.role,operation.lower())
        self.assertEqual(before,{str(p):p.read_bytes() for p in self.root.rglob('*') if p.is_file()})
        same=self.load(dict(self.body,place_project=''))
        self.assertEqual(same.summary['A'],same.summary['B'])
        self.assertEqual(same.contexts['B','PLACE'].target.describe()['board'],'tank01')

    def test_review_hash_requires_approval_and_invalidates_for_settings_scene_or_source_edit(self):
        recipe=self.load();body=dict(self.body,automatic_test_approved=True,recipe_sha256=recipe.summary['recipe_sha256'])
        self.load(body,approved=True)
        for change in ({'transfers':3},{'settle_s':2},{'automatic_test_approved':False}):
            with self.subTest(change=change),self.assertRaises(ValueError):self.load(dict(body,**change),approved=True)
        scene=self.root/'scene.json';scene.write_text('{"changed":true}')
        with self.assertRaisesRegex(ValueError,'changed'):self.load(body,approved=True)
        path=self.root/'B/grasp_pick.json';path.write_bytes(path.read_bytes()+b' ')
        with self.assertRaisesRegex(ValueError,'changed'):recipe.check_inputs()

    def test_stations_are_reviewed_and_change_the_hash(self):
        same=self.load()
        other=self.load(dict(self.body,station_b=3))
        self.assertEqual(same.summary['stations'],{'A':2,'B':2})
        self.assertEqual(other.summary['stations'],{'A':2,'B':3})
        self.assertNotEqual(same.summary['recipe_sha256'],other.summary['recipe_sha256'])
        for invalid in (0,256,True,'3'):
            with self.subTest(invalid=invalid),self.assertRaisesRegex(ValueError,'station'):
                self.load(dict(self.body,station_b=invalid))

    def test_off_mode_binds_hash_allows_missing_stations_and_refuses_station_change(self):
        parked = self.load()
        off = self.load(dict(self.body, agv_off=True))
        self.assertEqual(off.summary['agv_motion'], 'off')
        self.assertNotEqual(parked.summary['recipe_sha256'], off.summary['recipe_sha256'])
        no_stations = self.load(dict(self.body, agv_off=True, station_a=None))
        self.assertEqual(no_stations.stations, {'A': None, 'B': None})
        self.assertIsNone(no_stations.travel_pose)
        with self.assertRaisesRegex(ValueError, 'same station'):
            self.load(dict(self.body, agv_off=True, station_b=3))

    def test_counts_are_bounded_integers_and_incompatible_projects_refuse(self):
        for count in (0,101,True,1.5,'2',None):
            with self.subTest(count=count),self.assertRaisesRegex(ValueError,'integer'):self.load(dict(self.body,transfers=count))
        path=self.root/'B/project.json';original=path.read_bytes()
        for key,value in (('robot_ip','different'),('tcp',[0]*6),('camera_provider','different')):
            data=json.loads(original);data['settings'][key]=value;path.write_text(json.dumps(data))
            with self.subTest(key=key),self.assertRaises(ValueError):self.load()
            path.write_bytes(original)
        data=json.loads(original);data['inputs']['calibration']='different';path.write_text(json.dumps(data))
        with self.assertRaises(ValueError):self.load()


class RepeatCopyTests(unittest.TestCase):
    def test_disconnect_copy_does_not_claim_automatic_work_will_pause(self):
        from pathlib import Path
        text=(Path(__file__).parents[1]/'anodizing_web/web/manual-job.js').read_text()
        self.assertNotIn('holds at the next manual boundary',text)
        self.assertIn('IPC may continue accepted work',text)
        self.assertIn('pendant Stop',text)
