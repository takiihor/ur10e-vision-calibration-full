"""Camera adapters. Images keep their native pixels; calibration supplies the model."""
import time
import math

import cv2
import numpy as np

from .camera import CameraError, Frame, StreamConfig
from .calibration import opencv_calibration


class BaslerCamera:
    """Mono8 acquisition for the a2A2448-23gmBAS through pypylon.

    Software-trigger each image so a post-move capture cannot reuse buffered images.
    Basler does not supply calibrated lens intrinsics; the observer uses its file.
    """

    error_type = CameraError

    def __init__(self, config, intrinsics=None):
        StreamConfig(config.width, config.height, config.fps)
        if config.exposure_time_us is not None and (not math.isfinite(config.exposure_time_us)
                                                   or not 0 < config.exposure_time_us <= 1000000):
            raise self.error_type('Basler exposure must be finite and between 0 and 1000000 microseconds')
        gain_db = getattr(config, 'gain_db', 0.0)
        if gain_db is None:
            gain_db = 0.0
        if type(gain_db) not in (int, float) or not math.isfinite(gain_db) or not 0 <= gain_db <= 48:
            raise self.error_type('Basler gain must be finite and between 0 and 48 dB')
        if intrinsics is not None:
            opencv_calibration(intrinsics, (config.width, config.height))
            if intrinsics.get("camera_serial") != config.serial:
                raise self.error_type("Basler intrinsics must belong to the selected camera serial")
        self.intrinsics = intrinsics

        try:
            from pypylon import genicam, pylon
        except ImportError as exc:
            raise self.error_type('Basler capture needs pypylon: pip install "pypylon>=4,<5"') from exc
        self._pylon, self._camera = pylon, None
        self._frame_number = 0
        try:
            factory = pylon.TlFactory.GetInstance()
            devices = factory.EnumerateDevices()
            matches = [d for d in devices if config.serial is None or d.GetSerialNumber() == config.serial]
            if len(matches) != 1:
                raise self.error_type('Select a Basler camera serial; expected exactly one matching camera')
            self.serial = matches[0].GetSerialNumber()
            camera = self._camera = pylon.InstantCamera(factory.CreateDevice(matches[0]))
            camera.Open()
            # Reset geometry that changes the intrinsics before selecting the profile.
            for name, value in (('OffsetX', 0), ('OffsetY', 0), ('BinningHorizontal', 1),
                                ('BinningVertical', 1), ('DecimationHorizontal', 1), ('DecimationVertical', 1)):
                try:
                    node = camera.GetNodeMap().GetNode(name)
                except genicam.LogicalErrorException:
                    continue  # Not every model supports binning/decimation.
                if genicam.IsWritable(node):
                    node.SetValue(value)
                elif genicam.IsReadable(node) and node.GetValue() != value:
                    raise self.error_type(f'Basler {name} must be {value} for the calibrated profile')
            camera.Width.Value, camera.Height.Value = config.width, config.height
            if (camera.Width.Value, camera.Height.Value) != (config.width, config.height):
                raise self.error_type('Basler readback differs from the calibrated image profile')
            self.metadata = {'provider': 'basler', 'device': {'serial_number': self.serial},
                             'stream': {'width': config.width, 'height': config.height,
                                        'fps': config.fps, 'format': 'Mono8'},
                             'exposure_time_us': config.exposure_time_us,
                             'requested_gain_db': float(gain_db)}
            camera.PixelFormat.Value = 'Mono8'
            camera.GainAuto.Value = 'Off'
            camera.Gain.Value = gain_db
            camera.ExposureAuto.Value = 'Off'
            if config.exposure_time_us is not None:
                camera.ExposureTime.Value = config.exposure_time_us
            camera.AcquisitionFrameRateEnable.Value = True
            camera.AcquisitionFrameRate.Value = config.fps
            self.metadata['stream']['readback_fps'] = float(camera.AcquisitionFrameRate.Value)
            self.metadata['requested_exposure_time_us'] = config.exposure_time_us
            self.metadata['exposure_time_us'] = float(camera.ExposureTime.Value)
            self.metadata['gain_db'] = float(camera.Gain.Value)
            camera.AcquisitionMode.Value = 'Continuous'
            camera.TriggerSelector.Value = 'FrameStart'
            camera.TriggerMode.Value = 'On'
            camera.TriggerSource.Value = 'Software'
            camera.StartGrabbing(pylon.GrabStrategy_OneByOne)
        except Exception:
            self.close()
            raise

    def read(self):
        pylon, camera = self._pylon, self._camera
        camera.WaitForFrameTriggerReady(5000, pylon.TimeoutHandling_ThrowException)
        camera.ExecuteSoftwareTrigger()
        result = camera.RetrieveResult(5000, pylon.TimeoutHandling_ThrowException)
        try:
            if not result.GrabSucceeded():
                raise self.error_type(f'Basler image grab failed: {result.GetErrorDescription()}')
            self._frame_number += 1
            block = int(result.GetBlockID())
            if block in (0, 2**64 - 1):
                block = self._frame_number  # Some transports/emulators do not supply block IDs.
            image = np.ascontiguousarray(result.Array).copy()
            if image.shape != (self.metadata['stream']['height'], self.metadata['stream']['width']):
                raise self.error_type('Basler image dimensions differ from the calibrated profile')
            return Frame(bgr=cv2.cvtColor(image, cv2.COLOR_GRAY2BGR), intrinsics=self.intrinsics,
                         frame_number=block, timestamp_ms=time.monotonic() * 1000,
                         timestamp_domain='host_monotonic')
        finally:
            result.Release()

    def lock_exposure(self):
        self._camera.ExposureAuto.Value = 'Off'
        self._camera.GainAuto.Value = 'Off'

    def close(self):
        if self._camera is not None:
            try:
                if self._camera.IsGrabbing():
                    self._camera.StopGrabbing()
            finally:
                self._camera.Close()
                self._camera = None

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()
