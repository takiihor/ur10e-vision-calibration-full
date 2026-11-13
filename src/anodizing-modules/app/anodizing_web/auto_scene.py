"""Bind an automatic calibration job to its validated collision scene."""
import hashlib
import os
from pathlib import Path

from .paths import inside


def prepare(root, job_id, value, robot_model=None):
    from ur_handeye.collision import load_scene
    source = inside(root, value)
    if not source.is_file() or source.stat().st_size > 100_000:
        raise ValueError('Select a collision scene file of at most 100 kB.')
    with source.open('rb') as stream:
        raw = stream.read(100_001)
    if len(raw) > 100_000:
        raise ValueError('The collision scene file exceeds 100 kB.')
    snapshot = Path(root) / '.jobs' / (job_id + '.scene.json')
    fd = os.open(snapshot, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o400)
    model_snapshot = None
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        model = None
        if robot_model:
            model_source = inside(root, robot_model['source'])
            with model_source.open('rb') as stream:
                model_raw = stream.read(2_000_001)
            if len(model_raw) > 2_000_000 or hashlib.sha256(model_raw).hexdigest() != robot_model['sha256']:
                raise ValueError('The selected robot model changed. Restart the service after review.')
            model_target = Path(root) / '.jobs' / (job_id + '.model.json')
            model_fd = os.open(model_target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o400)
            model_snapshot = model_target
            with os.fdopen(model_fd, 'wb') as stream:
                stream.write(model_raw)
                stream.flush()
                os.fsync(stream.fileno())
            model = {'source': str(model_source), 'snapshot': str(model_snapshot), 'sha256': robot_model['sha256']}
            loaded = load_scene(snapshot, model_path=model_snapshot)
        else:
            loaded = load_scene(snapshot)
        loaded.require_motion_ready()
        result = {'source': str(source), 'snapshot': str(snapshot), 'sha256': hashlib.sha256(raw).hexdigest(),
                  'collision_scope': loaded.collision_scope}
        if model:
            result['model'] = model
        return result
    except BaseException:
        if model_snapshot and model_snapshot.exists():
            model_snapshot.unlink()
        snapshot.unlink()
        raise


def check(root, scene):
    source = inside(root, scene['source'])
    snapshot = Path(scene['snapshot'])
    if snapshot.parent != Path(root) / '.jobs' or snapshot.is_symlink():
        raise ValueError('The accepted collision scene snapshot is invalid.')
    try:
        for path in (source, snapshot):
            with path.open('rb') as stream:
                raw = stream.read(100_001)
            if len(raw) > 100_000 or hashlib.sha256(raw).hexdigest() != scene['sha256']:
                raise ValueError('The collision scene changed after this job was accepted.')
        if scene.get('model'):
            model = scene['model']
            model_source = inside(root, model['source'])
            model_snapshot = Path(model['snapshot'])
            if model_snapshot.parent != Path(root) / '.jobs' or model_snapshot.is_symlink():
                raise ValueError('The accepted robot model snapshot is invalid.')
            for path in (model_source, model_snapshot):
                with path.open('rb') as stream:
                    raw = stream.read(2_000_001)
                if len(raw) > 2_000_000 or hashlib.sha256(raw).hexdigest() != model['sha256']:
                    raise ValueError('The robot model changed after this job was accepted.')
    except OSError as exc:
        raise ValueError('The collision scene or robot model is unavailable. Stop this job.') from exc
