"""Offline preparation/phase checks; no Cartesian or hardware certification."""
import copy
import json
from pathlib import Path
import tempfile
import threading
import unittest

from anodizing_web import pick_place
from anodizing_web.teaching import simulated

FIXTURE = Path(__file__).parent / 'fixtures/simulated_calibration.json'


class ManualDataTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root/'cal.json').write_bytes(FIXTURE.read_bytes())
        board = Path(__file__).parents[2]/'data/boards/stage2_charuco_realsense_80mm/board_manifest.json'
        (self.root/'board.json').write_bytes(board.read_bytes())
        simulated(self.root, {'project':'project', 'calibration':'cal.json',
            'target':{'kind':'charuco','board':'tank01','board_manifest':'board.json'}})
        self.body = {'project':'project','calibration':'cal.json','pick_tag':'tank01',
                     'place_tag':'tank01','reuse_taught_offset':True}

    def snapshot(self):
        return {str(p.relative_to(self.root)):p.read_bytes() for p in self.root.rglob('*') if p.is_file()}

    def test_explicit_hardware_mode_cannot_fall_through_to_simulation(self):
        before = self.snapshot()
        with self.assertRaisesRegex(ValueError, 'simulation'):
            pick_place.run(self.root, dict(self.body, operation='PICK', mode='hardware'), 'reject-live')
        self.assertEqual(self.snapshot(), before)

    def test_separate_roles_reuse_exact_native_offset_without_writing(self):
        from anodizing_web.manual_pick_place import prepare
        before = self.snapshot()
        result = prepare(self.root, self.body)
        pick, place = result['roles']['pick'], result['roles']['place']
        self.assertEqual((pick['role'],place['role']), ('pick','place'))
        self.assertEqual(pick['target'],place['target'])
        self.assertEqual(pick['marker_to_tcp'],place['marker_to_tcp'])
        self.assertEqual(pick['source_grasp_sha256'],place['source_grasp_sha256'])
        self.assertEqual(place['source_role'],'pick')
        self.assertFalse(place['independently_taught'])
        self.assertEqual(place['reuse'],'explicit_dummy_test_shared_offset')
        self.assertEqual(result['quality_floor'], {'pose_pixels_per_module':3,'reference_pixels_per_module':4})
        self.assertFalse(result['hardware_available'])
        self.assertTrue(result['simulation_source'])
        self.assertEqual(result['path_check'],{'status':'disabled','reason':'reviewed_manual_service_required'})
        self.assertEqual(self.snapshot(), before)
        place['marker_to_tcp'][0][0] += 1
        self.assertNotEqual(pick['marker_to_tcp'],place['marker_to_tcp'])

    def test_implicit_reuse_or_other_tag_is_refused(self):
        from anodizing_web.manual_pick_place import prepare
        for change in ({'reuse_taught_offset':False},{'reuse_taught_offset':1},
                       {'pick_tag':'tank02'},{'place_tag':'tank02'},{'place_tag':None}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                prepare(self.root,dict(self.body,**change))

    def test_provenance_changes_are_refused(self):
        from anodizing_web.manual_pick_place import prepare
        for name, change in (
            ('project.json', lambda d:d['inputs'].update(calibration='wrong')),
            ('grasp_pick.json', lambda d:d.update(role='place')),
            ('grasp_pick.json', lambda d:d.update(calibration_sha256='wrong')),
            ('observation.json', lambda d:d['robot'].update(tcp_offset=[0,0,.3,0,0,0])),
            ('observation.json', lambda d:d['target'].update(board='tank02'))):
            path = self.root/'project'/name
            original = path.read_bytes()
            data = json.loads(original); change(data); path.write_text(json.dumps(data))
            with self.subTest(name=name), self.assertRaises(ValueError):
                prepare(self.root,self.body)
            path.write_bytes(original)
        with (self.root/'board.json').open('a') as stream:
            stream.write(' ')
        with self.assertRaisesRegex(ValueError,'manifest'):
            prepare(self.root,self.body)

    def test_route_is_intent_only_and_both_vertical_legs_hold_xy_orientation(self):
        from anodizing_web.manual_pick_place import vertical_intent
        endpoint = [.4,.1,.3,3.141592653589793,0,0]
        result = vertical_intent(endpoint,40)
        self.assertFalse(result['certified'])
        self.assertEqual(result['trajectory'],'fixed_orientation_base_z')
        for name,height in (('above',.04),('withdrawn',.1)):
            self.assertEqual(result[name][:2]+result[name][3:],endpoint[:2]+endpoint[3:])
            self.assertAlmostEqual(result[name][2]-endpoint[2],height)
        self.assertEqual(result['withdrawal_mm'],100)
        self.assertEqual(result['endpoint'],endpoint)
        self.assertEqual(endpoint[2],.3)
        for bad in ([0]*5,[0,0,float('nan'),0,0,0],[0,0,True,0,0,0]):
            with self.assertRaises(ValueError):vertical_intent(bad,100)


class ManualPhaseTests(unittest.TestCase):
    def sequence(self,operation='PICK',item='no_confirmed_item'):
        from anodizing_web.manual_pick_place import ManualPhases
        return ManualPhases('manual-test-001',operation,item,'observation-001')

    def reach_prompt(self,sequence):
        for event in ('observation_accepted','alignment_complete','approach_complete'):
            sequence.advance(event)
        return sequence.snapshot()['prompt']

    def test_pick_and_place_pause_before_withdrawal_and_bind_confirmation(self):
        for operation,item,phase,outcome in (
            ('PICK','no_confirmed_item','grip','operator_confirmed_held'),
            ('PLACE','operator_confirmed_held','release','operator_confirmed_released')):
            with self.subTest(operation=operation):
                sequence=self.sequence(operation,item)
                prompt=self.reach_prompt(sequence)
                self.assertEqual(sequence.snapshot()['stage'],'awaiting_'+phase)
                self.assertEqual(prompt['job_id'],'manual-test-001')
                self.assertEqual(prompt['role'],operation.lower())
                self.assertEqual(prompt['observation_id'],'observation-001')
                with self.assertRaises(ValueError):sequence.advance('withdrawal_complete')
                for key in ('job_id','role','observation_id','phase','id'):
                    wrong=dict(prompt,**{key:'wrong'})
                    with self.subTest(key=key), self.assertRaises(ValueError):sequence.confirm(wrong,True)
                with self.assertRaises(ValueError):sequence.confirm(prompt,1)
                sequence.confirm(prompt,True)
                self.assertEqual(sequence.snapshot()['item_state'],outcome)
                self.assertEqual(sequence.snapshot()['stage'],'withdrawing')
                with self.assertRaises(ValueError):sequence.confirm(prompt,True)
                sequence.advance('withdrawal_complete')
                self.assertEqual(sequence.snapshot()['stage'],'done')
                self.assertEqual(sequence.snapshot()['item_state'],outcome)

    def test_invalid_order_interruption_and_reconnect_never_resume(self):
        for op,item in (('PLACE','no_confirmed_item'),('PICK','operator_confirmed_held'),
                        ('PICK','unknown'),('PLACE','unknown')):
            with self.subTest(op=op,item=item),self.assertRaises(ValueError):self.sequence(op,item)
        sequence=self.sequence()
        with self.assertRaises(ValueError):sequence.advance('alignment_complete')
        prompt=self.reach_prompt(sequence)
        sequence.interrupt('heartbeat lost')
        self.assertEqual(sequence.snapshot()['item_state'],'unknown')
        self.assertEqual(sequence.snapshot()['error'],'heartbeat lost')
        self.assertIsNone(sequence.snapshot()['prompt'])
        with self.assertRaises(ValueError):sequence.confirm(prompt,True)
        with self.assertRaises(ValueError):sequence.advance('withdrawal_complete')
        sequence.interrupt('refresh')
        self.assertEqual(sequence.snapshot()['item_state'],'unknown')

    def test_prompt_snapshot_cannot_mutate_internal_authority(self):
        sequence=self.sequence(); prompt=self.reach_prompt(sequence)
        prompt['job_id']='other-job'
        with self.assertRaises(ValueError):sequence.confirm(prompt,True)
        self.assertEqual(sequence.snapshot()['prompt']['job_id'],'manual-test-001')

    def test_interrupt_at_every_active_stage_is_unknown(self):
        for count in range(5):
            sequence=self.sequence()
            if count>=1:sequence.advance('observation_accepted')
            if count>=2:sequence.advance('alignment_complete')
            if count>=3:prompt=self.reach_existing_prompt(sequence)
            if count>=4:sequence.confirm(prompt,True)
            sequence.interrupt('Stop')
            self.assertEqual(sequence.snapshot()['item_state'],'unknown')
            self.assertEqual(sequence.snapshot()['stage'],'interrupted')

    def reach_existing_prompt(self,sequence):
        sequence.advance('approach_complete')
        return sequence.snapshot()['prompt']


class ManualHttpTests(unittest.TestCase):
    def setUp(self):
        from test_port import PortTests
        PortTests.setUp(self)
        self.request = PortTests.request.__get__(self)
        self.wait_job = PortTests.wait_job.__get__(self)

    def test_hardware_requests_require_reviewed_service_flag_before_admission(self):
        body={'request_id':'manual-live-001','action':'PICK','mode':'hardware'}
        for change in ({},{},{'action':'PLACE'}):
            status,raw=self.request('POST','/api/manual-pick-place',dict(body,**change))
            self.assertEqual(status,400,raw)
            self.assertIn('--allow-manual-pick-place',json.loads(raw)['error'])
            self.assertEqual(self.server.owner.jobs,{})
        self.assertIsNone(self.server.owner.runner)
        self.assertIsNone(self.server.owner.active)
        self.assertFalse((self.root/'project').exists())
        self.assertEqual(self.request('POST','/api/manual-pick-place',dict(body,request_id='unauth-001'),
            headers={'X-Module-Token':'wrong'})[0],403)

    def test_fresh_place_request_is_refused_before_devices(self):
        body={'request_id':'manual-place-001','action':'PLACE','mode':'hardware'}
        status,raw=self.request('POST','/api/manual-pick-place',body)
        self.assertEqual(status,400,raw)
        self.assertIn('--allow-manual-pick-place',json.loads(raw)['error'])
        self.assertEqual(self.server.owner.jobs,{})
        self.assertIsNone(self.server.owner.runner)
        self.assertIsNone(self.server.owner.active)
        self.assertFalse((self.root/'project').exists())

    def test_prepare_is_owned_readonly_and_rejected_while_owner_busy(self):
        board=Path(__file__).parents[2]/'data/boards/stage2_charuco_realsense_80mm/board_manifest.json'
        (self.root/'board.json').write_bytes(board.read_bytes())
        simulated(self.root, {'project':'project','calibration':'cal.json',
            'target':{'kind':'charuco','board':'tank01','board_manifest':'board.json'}})
        def data_snapshot():
            return {str(p.relative_to(self.root)):p.read_bytes()
                    for p in self.root.rglob('*') if p.is_file() and
                    p.relative_to(self.root).parts[0]!='.jobs'}
        before=data_snapshot()
        body={'request_id':'manual-prepare-001','action':'prepare','project':'project',
              'calibration':'cal.json','pick_tag':'tank01','place_tag':'tank01',
              'reuse_taught_offset':True}
        status,raw=self.request('POST','/api/manual-pick-place',body)
        self.assertEqual(status,202,raw)
        job=self.wait_job(body['request_id'])
        self.assertEqual(job['module'],'manual-pick-place')
        self.assertEqual(job['state'],'done',job['error'])
        self.assertEqual(set(job['result']['roles']),{'pick','place'})
        self.assertFalse(job['result']['hardware_available'])
        self.assertEqual(json.loads((self.root/'.jobs/manual-prepare-001.json').read_text()),job)
        self.assertEqual(data_snapshot(),before)
        self.assertIsNone(self.server.owner.runner)
        self.assertIsNone(self.server.owner.active)

        started,finish=threading.Event(),threading.Event()
        def blocking_task(_):
            started.set()
            if not finish.wait(5):
                raise RuntimeError('Offline busy-owner fixture timed out')
            return {'complete':True}
        self.server.owner.submit('offline-owner-001','teaching',{},blocking_task)
        try:
            self.assertTrue(started.wait(2))
            status,raw=self.request('POST','/api/manual-pick-place',
                                    dict(body,request_id='manual-busy-001'))
            self.assertEqual(status,400,raw)
            self.assertIn('Another job owns the service',json.loads(raw)['error'])
            self.assertEqual(self.server.owner.active,'offline-owner-001')
            self.assertNotIn('manual-busy-001',self.server.owner.jobs)
            self.assertFalse((self.root/'.jobs/manual-busy-001.json').exists())
            self.assertEqual(data_snapshot(),before)
        finally:
            finish.set()
            self.assertEqual(self.wait_job('offline-owner-001')['state'],'done')

    def test_browser_has_real_manual_roles_without_simulation_controls(self):
        status,raw=self.request('GET','/pick-place')
        self.assertEqual(status,200)
        page=raw.decode()
        for content in ('manual-pick-tag','manual-place-tag','manual-setup','manual-PICK','manual-PLACE'):
            self.assertIn(content,page)
        for obsolete in ('Simulate PICK','Simulate PLACE','SIMULATION','shift_x','shift_y'):
            self.assertNotIn(obsolete,page)
        status,raw=self.request('GET','/manual-pick-place.js')
        self.assertEqual(status,200)
        self.assertIn(b'hardware_available',raw)
        status,raw=self.request('GET','/api/manual-pick-place')
        self.assertEqual(status,200)
        self.assertFalse(json.loads(raw)['hardware_available'])
