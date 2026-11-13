"""Compatibility adapter retaining pick/place vision errors."""
from charuco_realsense.basler import BaslerCamera as _BaslerCamera
from .errors import VisionError


class BaslerCamera(_BaslerCamera):
    error_type = VisionError
