"""ROS-free, conservative box checks for stopped, unblended linear joint moves.

Link boxes enclose the supplied UR collision meshes. Separation at an interval
midpoint must exceed a bound on every point's motion over that interval.
An unresolved interval is refused. Clear endpoints alone never approve a path.
"""
import hashlib
import json
from pathlib import Path

import numpy as np


class CollisionError(ValueError):
    """The model is incomplete, or the proposed path cannot be proved clear."""


def vector(value, size=3, bound=10):
    if (not isinstance(value, list) or len(value) != size
            or any(type(v) not in (int, float) for v in value)):
        raise CollisionError('Enter a finite number for each coordinate.')
    a = np.asarray(value, dtype=float)
    if not np.isfinite(a).all() or (np.abs(a) > bound).any():
        raise CollisionError('A coordinate exceeds the allowed range.')
    return a


def transform(pose):
    if not isinstance(pose, dict) or set(pose) != {'xyz_m', 'rpy_rad'}:
        raise CollisionError('The pose must contain position and roll, pitch and yaw.')
    x, y, z = vector(pose['xyz_m'])
    r, p, yaw = vector(pose['rpy_rad'], bound=2 * np.pi)
    cr, sr, cp, sp, cy, sy = np.cos(r), np.sin(r), np.cos(p), np.sin(p), np.cos(yaw), np.sin(yaw)
    return np.array([[cy*cp, cy*sp*sr-sy*cr, cy*sp*cr+sy*sr, x],
                     [sy*cp, sy*sp*sr+cy*cr, sy*sp*cr-cy*sr, y],
                     [-sp, cp*sr, cp*cr, z], [0, 0, 0, 1.]])


def separation(a, b):
    """Largest separating-axis gap. Positive values are distance lower bounds."""
    ca, ra, ha = a
    cb, rb, hb = b
    axes = np.vstack((ra.T, rb.T, np.cross(ra.T[:, None], rb.T).reshape(-1, 3)))
    norms = np.linalg.norm(axes, axis=1)
    axes = axes[norms > 1e-10] / norms[norms > 1e-10, None]
    gaps = np.abs(axes @ (cb - ca)) - np.abs(axes @ ra) @ ha - np.abs(axes @ rb) @ hb
    return float(np.max(gaps))


def load_scene(path, model_path=None):
    path = Path(path).expanduser()
    if path.stat().st_size > 100_000:
        raise CollisionError('The scene file exceeds 100 kB.')
    try:
        return Scene(json.loads(path.read_text()), model_path=model_path)
    except (ValueError, TypeError, KeyError) as exc:
        if isinstance(exc, CollisionError):
            raise
        raise CollisionError('The collision scene file is not valid.') from exc


def load_model(model_path):
    bundled = Path(__file__).parent / 'web/ur10e-model.json'
    try:
        with (Path(model_path).expanduser() if model_path is not None else bundled).open('rb') as stream:
            raw = stream.read(2_000_001)
        if len(raw) > 2_000_000:
            raise CollisionError('The robot model file exceeds 2 MB.')
        model = json.loads(raw)
        json.dumps(model, allow_nan=False)
        if model_path is not None:
            nominal = json.loads(bundled.read_bytes())
            if (not isinstance(model, dict) or set(model) - set(nominal) - {'factory_calibration'}
                    or any(key not in model for key in nominal)
                    or type(model['version']) is not int or model['version'] != nominal['version']
                    or model['robot'] != nominal['robot'] or model['frame'] != nominal['frame']
                    or type(model['nominal']) is not bool or not isinstance(model['source'], str)
                    or ('factory_calibration' in model and not isinstance(model['factory_calibration'], dict))
                    or json.dumps(model['bodies'], sort_keys=True) != json.dumps(nominal['bodies'], sort_keys=True)
                    or json.dumps(model['tool0'], sort_keys=True) != json.dumps(nominal['tool0'], sort_keys=True)
                    or not isinstance(model['joints'], list) or len(model['joints']) != 6):
                raise CollisionError('The robot model does not preserve the trusted UR10e geometry.')
            for joint, reference in zip(model['joints'], nominal['joints'], strict=True):
                if (not isinstance(joint, dict) or set(joint) != {'name', 'xyz_m', 'rpy_rad'}
                        or joint['name'] != reference['name']):
                    raise CollisionError('The robot model joint names or order are invalid.')
                position = vector(joint['xyz_m'], bound=1)
                vector(joint['rpy_rad'], bound=2 * np.pi)
                actual, expected = transform({k: joint[k] for k in ('xyz_m', 'rpy_rad')}), transform(
                    {k: reference[k] for k in ('xyz_m', 'rpy_rad')})
                angle = np.arccos(np.clip((np.trace(actual[:3, :3].T @ expected[:3, :3]) - 1) / 2, -1, 1))
                if np.linalg.norm(position - expected[:3, 3]) > .1 or angle > .2:
                    raise CollisionError('The robot model joint origin is outside UR10e bounds.')
        return model, hashlib.sha256(raw).hexdigest()
    except CollisionError:
        raise
    except (OSError, UnicodeError, ValueError, TypeError, KeyError, OverflowError, RecursionError) as exc:
        raise CollisionError('The robot model file is unavailable or invalid.') from exc


class Scene:
    def __init__(self, data, model_path=None):
        required = {'version', 'type', 'frame', 'confirmed', 'environment_complete',
                    'clearance_m', 'model_uncertainty_m', 'attached', 'obstacles', 'notes', 'model_sha256'}
        if (not isinstance(data, dict) or set(data) - {'collision_scope'} != required or type(data['version']) is not int
                or data['version'] != 1 or data['type'] != 'ur10e_collision_scene' or data['frame'] != 'base'
                or type(data['confirmed']) is not bool or type(data['environment_complete']) is not bool
                or not isinstance(data['notes'], str) or len(data['notes']) > 2000):
            raise CollisionError('This file does not match version 1 of the collision scene format.')
        for key in ('clearance_m', 'model_uncertainty_m'):
            if type(data[key]) not in (int, float) or not np.isfinite(data[key]) or not .0001 <= data[key] <= .1:
                raise CollisionError('Set clearance and model uncertainty from 0.1 to 100 mm.')
        if (not isinstance(data['attached'], list) or len(data['attached']) != 3
                or not isinstance(data['obstacles'], list) or len(data['obstacles']) > 40):
            raise CollisionError('The scene must include the tool, camera and mount. Use at most 40 obstacles.')
        self.collision_scope = data.get('collision_scope', 'cell')
        if self.collision_scope not in ('cell', 'supervised_self'):
            raise CollisionError('Select cell or supervised_self collision scope.')
        if self.collision_scope == 'supervised_self' and (data['environment_complete'] or data['obstacles']):
            raise CollisionError('Supervised self checks cannot declare cell coverage or contain cell obstacles.')
        self.model, self.model_hash = load_model(model_path)
        if data['model_sha256'] != self.model_hash:
            raise CollisionError('The scene uses a different robot model. Check the model before motion.')
        self.data = json.loads(json.dumps(data, allow_nan=False))
        self.scene_hash = hashlib.sha256(json.dumps(self.data, sort_keys=True).encode()).hexdigest()
        self.origins = [transform({k: j[k] for k in ('xyz_m', 'rpy_rad')}) for j in self.model['joints']]
        self.tool0 = transform(self.model['tool0'])
        self.bodies = []
        for b in self.model['bodies']:
            self._add(b['name'], b['depth'], b['dimensions_m'], transform(b['pose']))
        names = set()
        for b in self.data['attached'] + self.data['obstacles']:
            if (not isinstance(b, dict) or set(b) != {'name', 'dimensions_m', 'pose'}
                    or not isinstance(b['name'], str) or not b['name'].isascii()
                    or not b['name'].replace('_', '').isalnum() or len(b['name']) > 40 or b['name'] in names):
                raise CollisionError('Use a unique short name for each box. Use letters, numbers and underscores.')
            names.add(b['name'])
            depth = 6 if b in self.data['attached'] else -1
            if depth == -1 and b['name'] in {x['name'] for x in self.model['bodies']} | {'tool', 'camera', 'mount'}:
                raise CollisionError('An obstacle name conflicts with a robot part.')
            local = transform(b['pose'])
            self._add(b['name'], depth, b['dimensions_m'], self.tool0 @ local if depth == 6 else local)
        if {b['name'] for b in self.data['attached']} != {'tool', 'camera', 'mount'}:
            raise CollisionError('The attached boxes must be named tool, camera and mount.')
        self.pairs = []
        for i, a in enumerate(self.bodies):
            for j in range(i + 1, len(self.bodies)):
                b = self.bodies[j]
                attached_a, attached_b = a['name'] in {'tool', 'camera', 'mount'}, b['name'] in {'tool', 'camera', 'mount'}
                if a['depth'] == b['depth'] == -1 or attached_a and attached_b:
                    continue
                if not attached_a and not attached_b and a['depth'] >= 0 and b['depth'] >= 0 and abs(a['depth'] - b['depth']) <= 1:
                    continue  # Adjacent arm links meet at their joint.
                if 'wrist_3_link' in {a['name'], b['name']} and {a['name'], b['name']} & {'tool', 'mount'}:
                    continue  # Fixed mounting contacts. Checks against wrist 1 and wrist 2 remain active.
                self.pairs.append((i, j))
        try:
            from .collision_backend import hull_gap
            self.hull_gap = hull_gap
        except ImportError:
            self.hull_gap = None

    def _add(self, name, depth, dims, pose):
        dimensions = vector(dims)
        if (dimensions <= 0).any():
            raise CollisionError('Each box dimension must be greater than zero.')
        radii = np.zeros(6)
        extent = np.linalg.norm(pose[:3, 3]) + np.linalg.norm(dimensions / 2)
        for k in range(depth):
            radii[k] = extent + sum(np.linalg.norm(t[:3, 3]) for t in self.origins[k + 1:depth])
        self.bodies.append({'name': name, 'depth': depth, 'half': dimensions / 2, 'pose': pose, 'radii': radii})

    def require_motion_ready(self):
        if not self.data['confirmed']:
            raise CollisionError('Confirm the physical tool, camera, mount and robot model before automatic motion.')
        if self.collision_scope == 'cell' and (not self.data['environment_complete'] or not self.data['obstacles']):
            raise CollisionError('Add the measured cell obstacles before automatic motion. Confirm the cell coverage.')
        if self.hull_gap is None:
            raise CollisionError('Install the collision dependencies before automatic motion.')

    def frames(self, joints):
        q = vector(joints, size=6, bound=4 * np.pi)
        frames = [np.eye(4)]
        for origin, angle in zip(self.origins, q, strict=True):
            c, s = np.cos(angle), np.sin(angle)
            rz = np.array([[c, -s, 0, 0], [s, c, 0, 0], [0, 0, 1, 0], [0, 0, 0, 1]])
            frames.append(frames[-1] @ origin @ rz)
        return frames

    def flange(self, joints):
        return self.frames(joints)[-1] @ self.tool0

    def boxes(self, joints):
        frames = self.frames(joints)
        result = []
        for b in self.bodies:
            t = b['pose'] if b['depth'] == -1 else frames[b['depth']] @ b['pose']
            result.append((t[:3, 3], t[:3, :3], b['half']))
        return result

    def body_points(self, index, frames, boxes):
        """Hull vertices in base, using the caller's already evaluated frames/boxes."""
        body = self.bodies[index]
        if index < len(self.model['bodies']):
            pose = frames[body['depth']]
            vertices = np.array(self.model['bodies'][index]['vertices'])
            return vertices @ pose[:3, :3].T + pose[:3, 3]
        centre, rotation, half = boxes[index]
        corners = np.array([[x, y, z] for x in (-1, 1) for y in (-1, 1) for z in (-1, 1)])
        return (corners * half) @ rotation.T + centre

    def check_segment(self, start, target, *, max_depth=16, max_nodes=4096):
        start, target = vector(start, size=6, bound=4*np.pi), vector(target, size=6, bound=4*np.pi)
        result = {'status': 'clear_in_model', 'scene_sha256': self.scene_hash, 'model_sha256': self.model_hash,
                  'collision_scope': self.collision_scope,
                  'start_joints': start.tolist(), 'target_joints': target.tolist(), 'nodes': 0,
                  'clearance_m': self.data['clearance_m'], 'model_uncertainty_m': self.data['model_uncertainty_m']}
        if self.hull_gap is None:
            return {**result, 'status': 'unavailable', 'reason': 'Install the collision dependencies before path checks.'}
        required = self.data['clearance_m'] + 2 * self.data['model_uncertainty_m']
        # A bound includes joint interpolation error and endpoint tracking tolerance.
        tracking = np.deg2rad(.2)
        radii = np.array([b['radii'] for b in self.bodies])
        stack = [(start, target, 0)]
        while stack:
            lo, hi, depth = stack.pop()
            mid = (lo + hi) / 2
            result['nodes'] += 1
            boxes = self.boxes(mid.tolist())
            frames = self.frames(mid.tolist())
            uncertain = None
            for i, j in self.pairs:
                common = max(0, min(self.bodies[i]['depth'], self.bodies[j]['depth']))
                movement = (radii[i, common:] + radii[j, common:]) @ (np.abs(hi - lo)[common:] / 2 + tracking)
                gap = separation(boxes[i], boxes[j])
                if gap <= required + movement:
                    gap = self.hull_gap(self.body_points(i, frames, boxes), self.body_points(j, frames, boxes))
                    if gap is None:
                        return {**result, 'status': 'unresolved', 'pair': [self.bodies[i]['name'], self.bodies[j]['name']]}
                if gap <= required:
                    return {**result, 'status': 'collision', 'pair': [self.bodies[i]['name'], self.bodies[j]['name']],
                            'sample_joints': mid.tolist(), 'separating_gap_m': gap}
                if gap <= required + movement:
                    uncertain = [self.bodies[i]['name'], self.bodies[j]['name']]
            if uncertain:
                if depth >= max_depth or result['nodes'] >= max_nodes:
                    return {**result, 'status': 'unresolved', 'pair': uncertain,
                            'sample_joints': mid.tolist()}
                stack.extend([(mid, hi, depth+1), (lo, mid, depth+1)])
        return result

    def require_clear(self, start, target):
        self.require_motion_ready()
        result = self.check_segment(start, target)
        if result['status'] != 'clear_in_model':
            pair = ' / '.join(result.get('pair', []))
            raise CollisionError(f"The path check failed: {result['status']}. Check {pair}.")
        return result


def plan_segments(metadata):
    """Reconstruct the planned joint moves, including intermediate and final returns."""
    start = vector(metadata['start_joints'], size=6, bound=4*np.pi).tolist()
    current = start
    segments = []
    for target in metadata['plan']['targets']:
        q = vector(target['joints'], size=6, bound=4*np.pi).tolist()
        if target.get('via_start', False):
            segments.append(('via_start', current, start))
            current = start
        label = target['label']
        if not isinstance(label, str) or len(label) > 80:
            raise CollisionError('A plan label is not valid.')
        segments.append((label, current, q))
        current = q
    segments.append(('return_start', current, start))
    return segments


def check_plan(scene, segments, *, check_segment=None):
    return {'version': 1, 'type': 'ur10e_checked_joint_plan', 'scene': scene.data,
            'collision_scope': scene.data.get('collision_scope', 'cell'),
            'model_sha256': scene.model_hash,
            'segments': [{'label': label, **(check_segment or scene.check_segment)(a, b)} for label, a, b in segments],
            'scene_confirmed': scene.data['confirmed'] and scene.data['environment_complete'] and bool(scene.data['obstacles'])}


def main():
    import argparse
    parser = argparse.ArgumentParser(description='Check a saved joint plan without robot or camera access.')
    parser.add_argument('--scene', required=True)
    parser.add_argument('--robot-model', help='Robot model JSON matching the scene hash')
    parser.add_argument('--plan', required=True, help='Saved calibration session.json with joint targets')
    parser.add_argument('--output', required=True, help='New checked-plan JSON file')
    args = parser.parse_args()
    scene = load_scene(args.scene, model_path=args.robot_model)
    path = Path(args.plan)
    if path.stat().st_size > 1_000_000:
        raise CollisionError('The plan file exceeds 1 MB.')
    result = check_plan(scene, plan_segments(json.loads(path.read_text())))
    with Path(args.output).open('x') as stream:
        json.dump(result, stream, indent=2, allow_nan=False)
        stream.write('\n')
    refused = sum(s['status'] != 'clear_in_model' for s in result['segments'])
    print(f"Checked {len(result['segments'])} joint paths. Refused {refused} paths.")
    print('This command does not connect to a robot or camera.')
    return int(refused > 0)


if __name__ == '__main__':
    raise SystemExit(main())
