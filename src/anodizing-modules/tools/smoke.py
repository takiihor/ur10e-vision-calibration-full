"""Run a local simulation-only smoke workflow against the loopback service."""
import json
import os
from pathlib import Path
import re
import time
import uuid
from urllib.request import Request, urlopen

root = Path(__file__).resolve().parents[1]
port = int(os.environ.get('ANODIZING_PORT', '8870'))
base = f'http://127.0.0.1:{port}'
with urlopen(base + '/', timeout=5) as response:
    page = response.read(100000).decode()
match = re.search(r'<meta name="module-token" content="([A-Za-z0-9_-]+)">', page)
if not match:
    raise SystemExit('The expected module page was not found.')
token = match.group(1)

def request(path, data=None):
    headers = {'X-Module-Token': token}
    payload = None
    if data is not None:
        headers['Content-Type'] = 'application/json'
        payload = json.dumps(data).encode()
    with urlopen(Request(base + path, data=payload, headers=headers), timeout=5) as response:
        return json.load(response)

status = request('/api/status')
if status.get('app_id') != 'anodizing_modules_v1' or any(status.get(k, True) for k in ('allow_camera','allow_teaching','allow_manual','allow_auto_calibration','allow_teacher_motion')):
    raise SystemExit('Smoke test requires this service in default offline mode.')
data_dir = Path(status['data_dir'])
name = 'smoke-' + uuid.uuid4().hex[:12]
folder = data_dir / 'smoke' / name
folder.mkdir(parents=True, exist_ok=False)
(folder / 'calibration.json').write_bytes((root / 'app/test/fixtures/simulated_calibration.json').read_bytes())
cal = str(folder / 'calibration.json')
project = str(folder / 'project')

def job(module, body):
    job_id = uuid.uuid4().hex
    request('/api/' + module, dict(body, request_id=job_id))
    for _ in range(600):
        result = request('/api/jobs/' + job_id)
        if result['state'] == 'done':
            return result['result']
        if result['state'] in ('failed', 'interrupted'):
            raise RuntimeError(result.get('error'))
        time.sleep(.1)
    raise TimeoutError('Job did not finish.')

teaching = job('teaching', {'action':'simulate','project':project,'calibration':cal,
    'target':{'kind':'aruco','dictionary':'DICT_5X5_100','marker_id':7,'marker_length_mm':30}})
pick = job('pick-place', {'operation':'PICK','project':project,'calibration':cal,'shift_mm':[0,0]})
place = job('pick-place', {'operation':'PLACE','project':project,'calibration':cal,'shift_mm':[10,-5]})
assert teaching['simulation'] and pick['held_simulated'] and not place['held_simulated']
assert abs(place['target_tcp_pose_m_rad'][0] - pick['target_tcp_pose_m_rad'][0] - .01) < 1e-9
assert abs(place['target_tcp_pose_m_rad'][1] - pick['target_tcp_pose_m_rad'][1] + .005) < 1e-9
print('PASS: simulated teaching, PICK, fresh shifted PLACE. Artifacts:', folder)
