"""Scene contracts use a tiny, synthetic GLB and never open a device."""
from hashlib import sha256
import json
import struct
from threading import RLock, Thread
from types import SimpleNamespace

import pytest

from anodizing_web import server
from test_operator import request, service


def glb(document):
    raw = json.dumps(document).encode()
    raw += b' ' * (-len(raw) % 4)
    return struct.pack('<4sIIII', b'glTF', 2, 20 + len(raw), len(raw), 0x4E4F534A) + raw


@pytest.fixture
def scene_profile(tmp_path):
    drawing = glb({'asset': {'version': '2.0'}, 'scenes': [{}], 'scene': 0})
    (tmp_path / 'synthetic.glb').write_bytes(drawing)
    profile = {
        'drawing': {'path': 'synthetic.glb', 'sha256': sha256(drawing).hexdigest()},
        'track': {'start_m': [2, 0, 1], 'end_m': [2, 0, 5], 'stations_m': {'1': 0, '2': 4}},
        'robot_mount': {'xyz_m': [0, .5, 0], 'rpy_rad': [0, 0, 0]},
        'tanks': [{'id': 'Tank01', 'anchor_m': [2, 1, 1], 'status_key': 'FixtureStatus'}],
        'scripted_line': {'FixtureStatus': 'Occupied'},
    }
    (tmp_path / 'site-profile.json').write_text(json.dumps({'scene': profile}))
    return profile


def test_scene_serves_only_configured_versioned_glb(service, tmp_path, scene_profile):
    from anodizing_web.operator_scene import Scene

    service.scene = Scene(tmp_path, simulate=True)
    status, _, raw = request(service, '/api/scene')
    assert status == 200
    config = json.loads(raw)
    service.owner = service.agv.owner
    service.owner.runner = service.owner.teacher_service = service.owner.teacher_job = None
    service.model_hash = 'a' * 64
    service.model_data = {'nominal': True}
    service.check_model = lambda: None
    variables = json.loads(request(service, '/api/state')[2])['variables']
    assert any(item['id'] == 'robot.joints' and item['source'] == 'robot.simulator' for item in variables)
    assert config['track'] == scene_profile['track']
    assert config['robot_mount'] == scene_profile['robot_mount']
    assert config['tanks'] == [{'id': 'Tank01', 'label': 'Tank 1', 'anchor_m': [2, 1, 1],
                               'status_id': 'line.FixtureStatus'}]
    assert 'synthetic.glb' not in raw.decode()
    url = config['drawing']['url']
    status, headers, raw = request(service, url)
    assert (status, headers['Content-Type'], raw) == (200, 'model/gltf-binary', (tmp_path / 'synthetic.glb').read_bytes())
    assert 'immutable' in headers['Cache-Control']
    assert request(service, url, token='wrong')[0] == 403
    assert request(service, url + 'x')[0] == 404
    assert request(service, '/operator/synthetic.glb')[0] == 404


@pytest.mark.parametrize('damage', ['unbound', 'external', 'distance', 'station', 'mount', 'duplicate'])
def test_bad_configuration_never_guesses_placement(tmp_path, scene_profile, damage):
    from anodizing_web.operator_scene import Scene

    if damage == 'unbound':
        scene_profile['drawing']['sha256'] = '0' * 64
    if damage == 'external':
        content = glb({'asset': {'version': '2.0'}, 'buffers': [{'uri': 'other.bin'}]})
        (tmp_path / 'synthetic.glb').write_bytes(content)
        scene_profile['drawing']['sha256'] = sha256(content).hexdigest()
    if damage == 'distance':
        scene_profile['track']['stations_m']['2'] = 4.01
    if damage == 'station':
        scene_profile['track']['stations_m']['256'] = 3
    if damage == 'mount':
        scene_profile['robot_mount']['xyz_m'][1] = float('inf')
    if damage == 'duplicate':
        scene_profile['tanks'] *= 2
    (tmp_path / 'site-profile.json').write_text(json.dumps({'scene': scene_profile}))
    config = Scene(tmp_path).config
    key = {'unbound': 'drawing', 'external': 'drawing', 'distance': 'track', 'station': 'track',
           'mount': 'robot_mount', 'duplicate': 'tanks'}[damage]
    assert not config[key]


def test_scene_reuses_scripted_values_and_yields_to_owner(tmp_path, scene_profile):
    from anodizing_web.operator_scene import Scene

    scene = Scene(tmp_path, simulate=True)
    owner = SimpleNamespace(lock=RLock(), active=None, runner=None, teacher_service=None, teacher_job=None,
                            execution_uncertain=False, closing=False)
    model = SimpleNamespace(model_hash='a' * 64, model_data={'nominal': True}, check_model=lambda: None)
    def variable(name, now):
        return next(item for item in scene.variables(owner, model, now) if item['id'] == name)

    a = variable('robot.joints', 10.01)
    b = variable('robot.joints', 10.15)
    assert a['value'] == b['value'] and a['read_at'] == b['read_at'] and b['age_ms'] > a['age_ms']
    assert len(a['value']) == 6 and a['value'] != variable('robot.joints', 10.21)['value']
    assert a['source'] == 'robot.simulator' and a['model_sha256'] == model.model_hash
    assert variable('line.FixtureStatus', 10.15)['value'] == 'Occupied'
    owner.active = 'test-job'
    assert variable('robot.joints', 10.21)['value'] is None
    owner.runner = SimpleNamespace(cached_joint_sample=lambda: ([0, 1, 2, 3, 4, 5], 10.1, False))
    assert variable('robot.joints', 10.21)['value'] == [0, 1, 2, 3, 4, 5]
    assert variable('robot.joints', 11.21)['quality'] == 'stale'
    owner.active = None
    assert variable('robot.joints', 11.21)['source'] == 'robot.simulator'
    assert next(v for v in Scene(tmp_path).variables(owner, model, 11.21) if v['id'] == 'line.FixtureStatus')['value'] is None


def test_scene_model_mismatch_withholds_joint_values(tmp_path, scene_profile):
    from anodizing_web.operator_scene import Scene

    def changed():
        raise ValueError('changed')

    owner = SimpleNamespace(lock=RLock(), active=None, execution_uncertain=False, closing=False)
    model = SimpleNamespace(model_hash='a' * 64, model_data={'nominal': True}, check_model=changed)
    state = {item['id']: item for item in Scene(tmp_path, simulate=True).variables(owner, model, 1.1)}
    assert state['robot.joints']['value'] is None
    assert state['robot.model']['value'] is None


def test_owner_lock_contention_returns_unknown_without_waiting(tmp_path, scene_profile):
    from anodizing_web.operator_scene import Scene

    owner = SimpleNamespace(lock=RLock())
    scene = Scene(tmp_path, simulate=True)
    results = []
    with owner.lock:
        thread = Thread(target=lambda: results.append(scene.robot_sample(owner, 4.0)), daemon=True)
        thread.start()
        thread.join(.5)
        assert not thread.is_alive()
        assert results == [(None, 'Robot ownership is changing.')]


def test_native_job_feedback_is_copied_from_the_existing_cache():
    from anodizing_web.teacher_motion import TeacherJointMotion

    worker = TeacherJointMotion.__new__(TeacherJointMotion)
    worker._lock = RLock()
    worker._connected = True
    worker._fresh_at = 10.0
    worker._feedback = {'joints_rad': [0, 1, 2, 3, 4, 5], 'received_at': 10.1}
    worker.demo = False

    q, stamp, simulated = worker.cached_joint_sample()
    assert stamp == 10.0 and simulated is False
    assert q == worker._feedback['joints_rad'] and q is not worker._feedback['joints_rad']
    worker._connected = False
    assert worker.cached_joint_sample() is None
