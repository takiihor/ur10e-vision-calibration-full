"""Basler routing tests use fakes only; never enumerate a camera."""
import unittest
from unittest import mock

DEFAULT_BASLER_CLI_FPS = 20


class BaslerIndependent(unittest.TestCase):
    def test_basler_missing_own_intrinsics_fails_before_sdk_or_observation(self):
        from anodizing_web import camera
        with mock.patch.dict('sys.modules', {'pypylon':None,'pyrealsense2':None}):
            with self.assertRaisesRegex(ValueError,'(?i)intrinsics'):
                camera.board_preview('fake-manifest','fake-board','basler-fixture',enabled=True,
                    camera_provider='basler',intrinsics=None,camera_fps=17)

    def test_cli_rejects_missing_simulated_and_wrong_serial_before_camera(self):
        from types import SimpleNamespace
        from ur_handeye import cli
        base=dict(camera_provider='basler',serial='basler-fixture',width=1280,height=960,
                  fps=17,lock_exposure=False,intrinsics=None,exposure_time_us=3200)
        for source,model in [(None,None),('simulated.json',{'simulated':True,'camera_serial':'basler-fixture'}),
                             ('other.json',{'simulated':False,'camera_serial':'another-camera'})]:
            with self.subTest(source=source):
                args=SimpleNamespace(**dict(base,intrinsics=source))
                with mock.patch.object(cli,'load_intrinsics',return_value=model),mock.patch.dict('sys.modules',{'pypylon':None,'pyrealsense2':None}):
                    with self.assertRaisesRegex(Exception,'(?i)(intrinsics|serial|simulated)'):
                        cli._camera(args)

    def test_default_realsense_selection_keeps_existing_profile(self):
        from types import ModuleType,SimpleNamespace
        from ur_handeye import cli
        created=[]
        class FakeCamera:
            def __init__(self,serial,config):created.append((serial,config));self.reads=0
            def read(self):self.reads+=1
            def close(self):pass
        class FakeConfig:
            def __init__(self,width,height,fps):self.width,self.height,self.fps=width,height,fps
        module=ModuleType('charuco_realsense.camera');module.RealsenseCamera=FakeCamera;module.StreamConfig=FakeConfig
        args=SimpleNamespace(serial='rs-fixture',width=1280,height=720,fps=30,lock_exposure=False,intrinsics=None)
        with mock.patch.dict('sys.modules',{'charuco_realsense.camera':module,'pypylon':None}):
            camera=cli._camera(args)
        self.assertEqual(camera.reads,15)
        self.assertEqual((created[0][0],created[0][1].width,created[0][1].height,created[0][1].fps),('rs-fixture',1280,720,30))

    def test_teaching_rejects_camera_provider_mismatch(self):
        from types import SimpleNamespace
        from tempfile import TemporaryDirectory
        from ur_pick_place.vision import teaching
        calibration=SimpleNamespace(require_hardware=lambda:None,tcp_offset=[0]*6,camera_serial='camera-1',intrinsics={'width':1280,'height':960},sha256='fixture')
        target=SimpleNamespace(kind='aruco')
        with TemporaryDirectory() as d, mock.patch.object(teaching,'load_handeye',return_value=calibration),mock.patch.object(teaching,'target_from_config',return_value=target),mock.patch.object(teaching,'read_json',return_value={'camera':{'provider':'basler','stream':{'fps':17},'exposure_time_us':3200}}):
            settings={'project':d,'calibration':d+'/cal.json','robot_ip':'fake','tcp':[0]*6,
                      'target':{'kind':'aruco','marker_id':7,'dictionary':'DICT_5X5_100','marker_length_mm':30},
                      'motion_model':'planar','approach_mm':100,'camera_provider':'realsense'}
            with self.assertRaisesRegex(ValueError,'(?i)(provider|camera)'):
                teaching.TeachingProject(settings)

    def test_teaching_propagates_recorded_basler_exposure(self):
        from types import SimpleNamespace
        from tempfile import TemporaryDirectory
        from ur_pick_place.vision import teaching
        calibration=SimpleNamespace(require_hardware=lambda:None,tcp_offset=[0]*6,camera_serial='camera-1',intrinsics={'width':1280,'height':960},sha256='fixture')
        target=SimpleNamespace(kind='aruco')
        with TemporaryDirectory() as d, mock.patch.object(teaching,'load_handeye',return_value=calibration),mock.patch.object(teaching,'target_from_config',return_value=target),mock.patch.object(teaching,'read_json',return_value={'camera':{'provider':'basler','stream':{'fps':17},'exposure_time_us':3200}}):
            settings={'project':d,'calibration':d+'/cal.json','robot_ip':'fake','tcp':[0]*6,
                      'target':{'kind':'aruco','marker_id':7,'dictionary':'DICT_5X5_100','marker_length_mm':30},
                      'motion_model':'planar','approach_mm':100,'camera_provider':'basler'}
            project=teaching.TeachingProject(settings)
            self.assertEqual(project.config.camera.exposure_time_us,3200)

    def test_intrinsics_provider_mismatch_rejected_in_both_directions(self):
        from types import SimpleNamespace
        from ur_handeye import cli
        for selected,recorded in [('realsense','basler'),('basler','realsense')]:
            args=SimpleNamespace(camera_provider=selected,serial='same-serial',intrinsics='fake.json',width=None,height=None)
            model={'camera_provider':recorded,'camera_serial':'same-serial','width':640,'height':480,'simulated':False}
            with self.subTest(selected=selected),mock.patch.object(cli,'load_intrinsics',return_value=model):
                with self.assertRaisesRegex(Exception,'(?i)(provider|basler|realsense|intrinsics)'):
                    cli._intrinsics(args)

    def test_fake_basler_trigger_frame_actual_readback_and_cleanup(self):
        import numpy as np
        from types import ModuleType,SimpleNamespace
        from charuco_realsense.basler import BaslerCamera
        class Node:
            def __init__(self,override=None):self._value=None;self.override=override
            @property
            def Value(self):return self._value
            @Value.setter
            def Value(self,v):self._value=self.override if self.override is not None else v
        class Result:
            Array=np.zeros((3,4),dtype=np.uint8)
            released=False
            def GrabSucceeded(self):return True
            def GetBlockID(self):return 41
            def Release(self):self.released=True
        class FakeCamera:
            def __init__(self):
                for n in ('Width','Height','PixelFormat','GainAuto','Gain','ExposureAuto','AcquisitionFrameRateEnable','AcquisitionMode','TriggerSelector','TriggerMode','TriggerSource'):setattr(self,n,Node())
                self.ExposureTime=Node(3198.0);self.AcquisitionFrameRate=Node(16.8);self.result=Result();self.events=[]
            def Open(self):self.events.append('open')
            def GetNodeMap(self):return self
            def GetNode(self,name):raise LookupError(name)
            def StartGrabbing(self,*a):self.events.append('start')
            def WaitForFrameTriggerReady(self,*a):self.events.append('ready')
            def ExecuteSoftwareTrigger(self):self.events.append('trigger')
            def RetrieveResult(self,*a):return self.result
            def IsGrabbing(self):return True
            def StopGrabbing(self):self.events.append('stop')
            def Close(self):self.events.append('close')
        cam=FakeCamera();device=SimpleNamespace(GetSerialNumber=lambda:'fake-basler')
        factory=SimpleNamespace(EnumerateDevices=lambda:[device],CreateDevice=lambda d:d)
        pylon=SimpleNamespace(TlFactory=SimpleNamespace(GetInstance=lambda:factory),InstantCamera=lambda d:cam,GrabStrategy_OneByOne=1,TimeoutHandling_ThrowException=1)
        genicam=SimpleNamespace(LogicalErrorException=LookupError,IsWritable=lambda n:True,IsReadable=lambda n:True)
        module=ModuleType('pypylon');module.pylon=pylon;module.genicam=genicam
        config=SimpleNamespace(serial='fake-basler',width=4,height=3,fps=17,exposure_time_us=3200)
        import builtins
        guarded_import=builtins.__import__
        def fake_import(name,*args,**kwargs):
            return module if name=='pypylon' else guarded_import(name,*args,**kwargs)
        with mock.patch('builtins.__import__',side_effect=fake_import):
            with BaslerCamera(config) as adapter:frame=adapter.read();meta=adapter.metadata
        self.assertEqual(frame.bgr.shape,(3,4,3));self.assertEqual(frame.timestamp_domain,'host_monotonic')
        self.assertTrue(cam.result.released);self.assertEqual(cam.events,['open','start','ready','trigger','stop','close'])
        self.assertEqual(meta['provider'],'basler');self.assertEqual(meta['device']['serial_number'],'fake-basler')
        self.assertEqual((meta['stream']['width'],meta['stream']['height'],meta['stream']['fps']),(4,3,17))
        self.assertEqual(meta['exposure_time_us'],3198.0);self.assertEqual(meta['stream']['readback_fps'],16.8)

    def test_cli_valid_basler_intrinsics_selects_own_profile_without_realsense(self):
        from types import SimpleNamespace
        from ur_handeye import cli
        from charuco_realsense import basler
        model={'camera_provider':'basler','camera_serial':'basler-1','width':1280,'height':960,'simulated':False}
        args=SimpleNamespace(camera_provider='basler',serial='basler-1',intrinsics='own.json',width=None,height=None,
                             fps=None,exposure_time_us=3200,lock_exposure=False)
        sentinel=object()
        with mock.patch.object(cli,'load_intrinsics',return_value=model),mock.patch.object(basler,'BaslerCamera',return_value=sentinel) as factory, mock.patch.dict('sys.modules',{'pyrealsense2':None}):
            self.assertIs(cli._camera(args),sentinel)
        config=factory.call_args.args[0]
        self.assertEqual((config.serial,config.width,config.height,config.fps,config.exposure_time_us),('basler-1',1280,960,DEFAULT_BASLER_CLI_FPS,3200))
        self.assertEqual(factory.call_args.kwargs['intrinsics'],model)
