"""Inspect installed dependencies without enumerating or connecting to hardware."""
import importlib
import importlib.metadata
import importlib.util
import sys

required = ['numpy', 'cv2', 'PIL', 'reportlab', 'pypdf', 'packaging', 'ur_handeye.ui',
            'ur_pick_place.vision.teach_ui', 'charuco_generator.board', 'charuco_realsense.detect']
optional = ['scipy', 'pyrealsense2', 'rtde_receive']
failed = False
print('Python:', sys.version.split()[0])
for name in required + optional:
    try:
        module = importlib.import_module(name) if name not in ('pyrealsense2', 'rtde_receive') else None
        present = module is not None or importlib.util.find_spec(name) is not None
        version = getattr(module, '__version__', '') if module else ''
        print(f'{name}: {"available" if present else "missing"} {version}')
        if not present and name in required:
            failed = True
    except Exception as exc:
        print(f'{name}: unavailable ({exc})')
        if name in required:
            failed = True
try:
    import cv2
    for name in ('CharucoBoard', 'CharucoDetector', 'ArucoDetector'):
        if not hasattr(cv2.aruco, name):
            print('Missing OpenCV API:', name)
            failed = True
except Exception as exc:
    print('OpenCV API check failed:', exc)
    failed = True
from ur_handeye import cli
print('Read-only manual adaptation:', bool(getattr(cli, 'READ_ONLY_MANUAL_CAPTURE', False)))
if not getattr(cli, 'READ_ONLY_MANUAL_CAPTURE', False):
    failed = True
sys.exit(1 if failed else 0)
