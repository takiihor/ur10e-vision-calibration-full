"""Save a new validated scene without approving or starting a motion job."""
import hashlib
import json
import os
import re

from .paths import check_tree, inside


def _model_path(root, robot_model):
    model_path = None
    if robot_model:
        model_path = inside(root, robot_model['source'])
        with model_path.open('rb') as stream:
            model_raw = stream.read(2_000_001)
        if len(model_raw) > 2_000_000 or hashlib.sha256(model_raw).hexdigest() != robot_model['sha256']:
            raise ValueError('The selected robot model changed. Restart the service after review.')
    return model_path


def read_scene(root, value, robot_model=None):
    from ur_handeye.collision import Scene

    path = inside(root, value)
    with path.open('rb') as stream:
        raw = stream.read(100_001)
    if len(raw) > 100_000:
        raise ValueError('The scene file exceeds 100 kB.')
    data = json.loads(raw)
    scene = Scene(data, model_path=_model_path(root, robot_model))
    return {'path': str(path), 'scene': data, 'model_sha256': scene.model_hash,
            'confirmed': data['confirmed'], 'environment_complete': data['environment_complete'],
            'obstacles_count': len(data['obstacles'])}


def save_scene(root, job_id, data, robot_model=None):
    from ur_handeye.collision import Scene

    if not isinstance(job_id, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,96}', job_id):
        raise ValueError('Invalid scene request ID.')
    if not isinstance(data, dict):
        raise ValueError('Select a valid collision scene.')
    raw = (json.dumps(data, allow_nan=False, indent=2) + '\n').encode()
    if len(raw) > 100_000:
        raise ValueError('The scene file exceeds 100 kB.')
    scene = Scene(data, model_path=_model_path(root, robot_model))
    directory = check_tree(root, inside(root, 'collision', required=False))
    directory.mkdir(parents=True, exist_ok=True)
    path = inside(root, str(directory / f'scene-{job_id}.json'), required=False)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
    except BaseException:
        path.unlink()
        raise
    return {'path': str(path), 'model_sha256': scene.model_hash,
            'confirmed': data['confirmed'], 'environment_complete': data['environment_complete'],
            'obstacles_count': len(data['obstacles']), 'sha256': hashlib.sha256(raw).hexdigest()}
