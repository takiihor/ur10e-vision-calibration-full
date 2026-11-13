import hashlib
import json
from pathlib import Path
import numpy as np
import pytest

from ur_handeye import cli
from ur_handeye.collision import CollisionError, Scene, load_scene
from ur_handeye.transforms import invert, pose_to_matrix
from ur_handeye.ui import build_command

WEB = Path(__file__).parents[1] / 'src/ur_handeye/web'
BENCH = Path(__file__).resolve().parents[4]
FACTORY = BENCH / 'ROBOT_MODEL_FACTORY_DRAFT.json'
FACTORY_SCENE = BENCH / 'COLLISION_SCENE_FACTORY_DRAFT.json'
FACTORY_DATA = BENCH / 'FACTORY_KINEMATICS.json'
PREFLIGHT = BENCH.parent / 'split_web_modules_20261104/operator-20261105/read-only-robot.json'


def scene_data():
    return json.loads((WEB / 'collision-scene.example.json').read_text())


def write_model(tmp_path, model):
    path = tmp_path / 'model.json'
    path.write_text(json.dumps(model, allow_nan=True))
    data = scene_data()
    data['model_sha256'] = hashlib.sha256(path.read_bytes()).hexdigest()
    return path, data


def test_default_model_and_explicit_copy_are_identical(tmp_path):
    source = WEB / 'ur10e-model.json'
    path = tmp_path / 'nominal.json'
    path.write_bytes(source.read_bytes())
    default = Scene(scene_data())
    selected = Scene(scene_data(), model_path=path)
    assert default.model_hash == selected.model_hash == hashlib.sha256(source.read_bytes()).hexdigest()
    np.testing.assert_allclose(default.flange([.1] * 6), selected.flange([.1] * 6))


def test_factory_model_matches_two_distinct_saved_postures():
    if not all(path.is_file() for path in (FACTORY, FACTORY_SCENE, FACTORY_DATA, PREFLIGHT)):
        pytest.skip('local factory evidence is not included in the standalone package')
    scene = load_scene(FACTORY_SCENE, model_path=FACTORY)
    samples = [json.loads(PREFLIGHT.read_text())['samples'][0],
               json.loads(FACTORY_DATA.read_text())['pose_samples'][0]]
    assert np.linalg.norm(np.subtract(samples[0]['joints_rad'], samples[1]['joints_rad'])) > .1
    for sample in samples:
        flange = pose_to_matrix(sample['tcp_pose_m_rad']) @ invert(pose_to_matrix(sample['tcp_offset_m_rad']))
        np.testing.assert_allclose(scene.flange(sample['joints_rad']), flange, atol=2e-10, rtol=0)
    assert not scene.data['confirmed'] and not scene.data['environment_complete']
    with pytest.raises(CollisionError, match='Confirm the physical'):
        scene.require_motion_ready()


@pytest.mark.parametrize('change', [
    lambda m: m['bodies'][0]['vertices'][0].__setitem__(0, 0.123),
    lambda m: m['bodies'][1].update(depth=5),
    lambda m: m['bodies'][2].update(mesh_sha256='wrong'),
    lambda m: m['bodies'][3]['dimensions_m'].__setitem__(0, .01),
    lambda m: m['bodies'][4]['pose']['xyz_m'].__setitem__(0, .1),
    lambda m: m['tool0']['xyz_m'].__setitem__(0, .01),
    lambda m: m['joints'][0].update(name='other'),
    lambda m: m['joints'][0]['xyz_m'].__setitem__(0, 2),
    lambda m: m['joints'][0]['rpy_rad'].__setitem__(0, .3),
    lambda m: m['joints'][0]['xyz_m'].__setitem__(0, float('nan')),
    lambda m: m.update(factory_calibration={'value': float('inf')}),
    lambda m: m.update(frame='base_link'),
    lambda m: m.update(version=True),
])
def test_external_model_rejects_altered_geometry_or_invalid_origins(tmp_path, change):
    model = json.loads((WEB / 'ur10e-model.json').read_text())
    change(model)
    path, data = write_model(tmp_path, model)
    with pytest.raises(CollisionError):
        Scene(data, model_path=path)


def test_model_hash_and_file_errors_fail_closed(tmp_path):
    model = json.loads((WEB / 'ur10e-model.json').read_text())
    path, data = write_model(tmp_path, model)
    Scene(data, model_path=path)
    path.write_text(path.read_text() + ' ')
    with pytest.raises(CollisionError, match='different robot model'):
        Scene(data, model_path=path)
    path.write_text('{')
    with pytest.raises(CollisionError, match='invalid'):
        Scene(data, model_path=path)
    path.write_bytes(b' ' * 2_000_001)
    with pytest.raises(CollisionError, match='exceeds'):
        Scene(data, model_path=path)
    with pytest.raises(CollisionError, match='unavailable'):
        Scene(data, model_path=tmp_path / 'missing')


@pytest.mark.parametrize('mode', ['auto', 'replay'])
def test_native_collect_passes_model_before_hardware(monkeypatch, mode):
    captured = []
    def load(path, model_path=None):
        captured.append((path, model_path))
        raise CollisionError('stop before hardware')
    monkeypatch.setattr('ur_handeye.collision.load_scene', load)
    args = cli.parser().parse_args(['collect', '--robot-ip', 'unused', '--mode', mode,
                                    '--collision-scene', 'scene.json', '--robot-model', 'factory.json'])
    with pytest.raises(CollisionError, match='stop before hardware'):
        cli.cmd_collect(args)
    assert captured == [('scene.json', 'factory.json')]


def test_build_command_propagates_selected_model(tmp_path):
    model = json.loads((WEB / 'ur10e-model.json').read_text())
    path, data = write_model(tmp_path, model)
    scene = tmp_path / 'scene.json'
    scene.write_text(json.dumps(data))
    manifest = tmp_path / 'board.json'
    manifest.write_text('{}')
    settings = {'session': str(tmp_path / 'session'), 'manifest': str(manifest),
                'robot_ip': 'unused', 'board': 'calibration', 'serial': '', 'speed': '.1', 'views': '14',
                'collision_scene': str(scene), 'robot_model': str(path)}
    command = build_command('plan', settings)
    assert command[command.index('--robot-model') + 1] == str(path)
    assert command[command.index('--collision-scene') + 1] == str(scene)
    assert '--dry-run' in command
    settings['robot_model'] = str(tmp_path / 'missing')
    with pytest.raises(CollisionError, match='unavailable'):
        build_command('plan', settings)


def test_offline_collision_cli_passes_model(monkeypatch, tmp_path):
    from ur_handeye import collision
    captured = []
    def load(path, model_path=None):
        captured.append((path, model_path))
        raise CollisionError('stop before plan')
    monkeypatch.setattr(collision, 'load_scene', load)
    monkeypatch.setattr('sys.argv', ['collision', '--scene', 'scene.json', '--robot-model', 'factory.json',
                                     '--plan', 'plan.json', '--output', str(tmp_path / 'result.json')])
    with pytest.raises(CollisionError, match='stop before plan'):
        collision.main()
    assert captured == [('scene.json', 'factory.json')]
