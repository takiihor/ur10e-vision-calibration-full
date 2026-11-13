"""Conditional model check of one fixed-orientation, exact base-Z TCP line.

Contraction balls enclose the continuous IK branch; IK proposals and joint
chords never certify it. No SDK, robot connection, command or file write.
See API.md and NUMERICS.md for the engineering numerical assumptions.
"""
from dataclasses import asdict, dataclass
import hashlib
import json
import math
import time

import numpy as np

from .collision import CollisionError, separation, vector


@dataclass(frozen=True)
class Limits:
    max_nodes: int = 4096
    max_depth: int = 24
    min_step_m: float = 1e-6
    max_time_s: float = 30.


@dataclass(frozen=True)
class Numerics:
    # These are assumed norm error envelopes, not machine epsilon or intervals.
    fk_error: float = 1e-10
    jacobian_error: float = 1e-10
    arithmetic_error: float = 1e-12
    bound_relative_error: float = 1e-12
    gap_error_m: float = 2e-6
    rotation_scale_m: float = .25


def _json(value):
    return json.dumps(value, sort_keys=True, allow_nan=False, separators=(',', ':'))


def _hash(value):
    return hashlib.sha256(_json(value).encode()).hexdigest()


def _rigid(value, name):
    t = np.asarray(value, dtype=float)
    if (t.shape != (4, 4) or not np.isfinite(t).all()
            or not np.array_equal(t[3], [0, 0, 0, 1])
            or np.linalg.norm(t[:3, :3].T @ t[:3, :3] - np.eye(3), 'fro') > 1e-12
            or abs(np.linalg.det(t[:3, :3]) - 1) > 1e-12
            or np.linalg.norm(t[:3, 3]) > 10):
        raise ValueError(name + ' must be a finite rigid transform within 10 m.')
    return t.copy()


def endpoint(scene, joints, tcp):
    return scene.flange(np.asarray(joints, dtype=float).tolist()) @ tcp


def evaluate(scene, joints, tcp, reference_rotation, ell):
    """f(q), Df(q) with a spatial angular Jacobian at the actual active TCP."""
    frames = scene.frames(np.asarray(joints, dtype=float).tolist())
    t = frames[-1] @ scene.tool0 @ tcp
    axes = np.array([(frames[i] @ scene.origins[i])[:3, 2] for i in range(6)]).T
    origins = np.array([(frames[i] @ scene.origins[i])[:3, 3] for i in range(6)]).T
    linear = np.cross(axes.T, (t[:3, 3, None] - origins).T).T
    s = reference_rotation.T @ t[:3, :3]
    angular = ell * ((np.trace(s)*np.eye(3) - s)/2) @ reference_rotation.T @ axes
    skew = (s-s.T)/2
    f = np.r_[t[:3, 3], ell*np.array([skew[2, 1], skew[0, 2], skew[1, 0]])]
    return f, np.vstack((linear, angular))


def derivative_bounds(scene, tcp, ell):
    """Analytic global bounds for the corrected revolute Scene chain (2 norms)."""
    lengths = [np.linalg.norm(o[:3, 3]) for o in scene.origins]
    tail = np.linalg.norm(scene.tool0[:3, 3]) + np.linalg.norm(tcp[:3, 3])
    t = np.array([sum(lengths[i+1:])+tail for i in range(6)])
    mixed = np.array([[math.hypot(t[max(i, j)], ell*(1.5+(j < i)))
                       for j in range(6)] for i in range(6)])
    return {'tcp_axis_distance_bounds_m': t.tolist(),
            'column_derivative_bounds': mixed.tolist(), 'M': float(np.linalg.norm(mixed, 'fro'))}


def model_ik(scene, target, tcp, seed, *, max_iterations=20, deadline=math.inf):
    """Bounded model Newton proposal; acceptance always needs ball containment."""
    q = np.asarray(seed, dtype=float).copy()
    desired = np.r_[target[:3, 3], np.zeros(3)]
    for _ in range(max_iterations):
        if time.monotonic() >= deadline:
            raise ValueError('IK time limit exhausted.')
        f, j = evaluate(scene, q, tcp, target[:3, :3], .25)
        residual = f-desired
        if np.linalg.norm(residual) < 1e-13:
            return q.tolist()
        step = np.linalg.solve(j, residual)
        if not np.isfinite(step).all() or np.linalg.norm(step) > .5:
            raise ValueError('IK proposal is unbounded or ill conditioned.')
        q -= step
    raise ValueError('IK iteration limit exhausted.')


def _collision_ball(scene, c, r, tracking, numerics, deadline):
    boxes, frames = scene.boxes(c.tolist()), scene.frames(c.tolist())
    required = scene.data['clearance_m'] + 2*scene.data['model_uncertainty_m']
    margin = math.inf
    for i, j in scene.pairs:
        if time.monotonic() >= deadline:
            return 'unresolved', {'reason': 'Time limit exhausted in collision check.'}
        a = scene.bodies[i]['radii'] + scene.bodies[j]['radii']
        a = a.copy()
        common = max(0, min(scene.bodies[i]['depth'], scene.bodies[j]['depth']))
        a[:common] = 0
        movement = np.linalg.norm(a)*r + a @ tracking
        movement = movement*(1+numerics.bound_relative_error)+numerics.arithmetic_error
        threshold = required+movement
        gap = separation(boxes[i], boxes[j])-numerics.gap_error_m
        if not np.isfinite(gap):
            return 'unresolved', {'reason': 'Nonfinite OBB gap.'}
        if gap <= threshold:
            gap = scene.hull_gap(scene.body_points(i, frames, boxes), scene.body_points(j, frames, boxes))
            if gap is None or not np.isfinite(gap):
                return 'unresolved', {'reason': 'Hull gap is unresolved.', 'pair': [scene.bodies[i]['name'], scene.bodies[j]['name']]}
            gap -= max(0., numerics.gap_error_m-2e-6)
            # Inherited hull backend has already subtracted its 2 micrometres.
        pair = [scene.bodies[i]['name'], scene.bodies[j]['name']]
        if gap <= required:
            return 'collision', {'reason': 'Center geometry lacks required clearance; exact branch contact is not asserted.',
                                 'pair': pair, 'center_joints': c.tolist(), 'separating_gap_m': float(gap)}
        margin = min(margin, gap-threshold)
    if not np.isfinite(margin):
        return 'unresolved', {'reason': 'No finite checked collision pairs.'}
    return ('clear_in_model' if margin > numerics.arithmetic_error else 'subdivide'), {'collision_margin_m': float(margin)}


def _snapshot(scene, start, tcp, target, joint_bounds, binding, command_start=None):
    model_start = endpoint(scene, start, tcp)
    line_start = model_start if command_start is None else command_start
    return {'line': {'type': 'fixed_orientation_base_z', 'start_joints': list(start),
                     'start_tcp': line_start.tolist(), 'model_start_tcp': model_start.tolist(),
                     'start_source': 'model_fk' if command_start is None else 'explicit_command_start',
                     'tcp': tcp.tolist(),
                     'target_tcp': target.tolist(),
                     'delta_z_m': float(target[2, 3]-line_start[2, 3])},
            'scene_sha256': scene.scene_hash, 'model_sha256': scene.model_hash,
            'scene_state_sha256': _hash(scene.data), 'model_state_sha256': _hash(scene.model),
            'chain_geometry_sha256': _hash({
                'origins': [o.tolist() for o in scene.origins], 'tool0': scene.tool0.tolist(),
                'bodies': [{k: (v.tolist() if isinstance(v, np.ndarray) else v)
                            for k, v in b.items()} for b in scene.bodies], 'pairs': scene.pairs}),
            'collision_scope': scene.collision_scope, 'model_nominal': scene.model['nominal'],
            'model_source': scene.model['source'],
            'factory_calibration': json.loads(_json(scene.model.get('factory_calibration'))),
            'binding': json.loads(_json(binding)), 'joint_bounds_rad': np.asarray(joint_bounds).tolist()}


def binding_matches(certificate, scene, start, tcp, target, *, joint_bounds, binding, command_start=None):
    """Compare inputs again before caller dispatch. This function never dispatches."""
    try:
        fresh = _snapshot(scene, start, _rigid(tcp, 'TCP'), _rigid(target, 'Target'), joint_bounds, binding,
                          None if command_start is None else _rigid(command_start, 'Command start'))
        return (certificate.get('status') == 'clear_in_model'
                and certificate.get('binding_sha256') == _hash(fresh)
                and all(certificate.get(k) == v for k, v in fresh.items()))
    except (ValueError, TypeError, KeyError, OverflowError):
        return False


def check_vertical(scene, start, tcp, target, *, joint_bounds, binding, ik=None,
                   limits=Limits(), numerics=Numerics(), command_start=None):
    """Return conditional engineering model certificate or explicit refusal.

    The caller owns stopped-start, connected calibration/TCP identity, controller
    FK checks, scene/payload coverage, workspace and exact native command binding.
    Time budget is cooperative around bounded NumPy/LP/callback operations.
    """
    began = time.monotonic()
    result = {'status': 'unresolved', 'nodes': 0, 'balls': [], 'elapsed_s': 0.}
    def finish(status, reason=None, **details):
        result.update(details, status=status, elapsed_s=time.monotonic()-began)
        if reason:
            result['reason'] = reason
        return result
    try:
        if (type(limits.max_nodes) is not int or not 1 <= limits.max_nodes <= 100000
                or type(limits.max_depth) is not int or not 0 <= limits.max_depth <= 50
                or not np.isfinite([limits.min_step_m, limits.max_time_s]).all()
                or not 0 < limits.min_step_m <= 2 or not 0 < limits.max_time_s <= 300):
            raise ValueError('Invalid subdivision/node/time limits.')
        default = asdict(Numerics()); provided = asdict(numerics)
        if (not np.isfinite(list(provided.values())).all()
                or any(provided[k] < default[k] for k in default if k != 'rotation_scale_m')
                or not .01 <= numerics.rotation_scale_m <= 1):
            raise ValueError('Numerical allowances cannot be reduced below the documented envelope.')
        scene.require_motion_ready()
        c = vector(np.asarray(start).tolist(), size=6, bound=4*math.pi)
        tcp, target = _rigid(tcp, 'TCP'), _rigid(target, 'Target')
        if np.linalg.norm(tcp[:3, 3]) > 1:
            raise ValueError('TCP offset exceeds the bounded 1 m numerical domain.')
        initial = endpoint(scene, c, tcp) if command_start is None else _rigid(command_start, 'Command start')
        if (not np.array_equal(initial[:2, 3], target[:2, 3])
                or not np.array_equal(initial[:3, :3], target[:3, :3])):
            raise ValueError('Target must preserve exact line-start XY and orientation.')
        delta = float(target[2, 3]-initial[2, 3]); length, direction = abs(delta), math.copysign(1, delta)
        if not 0 < length <= 2:
            raise ValueError('Vertical length must be nonzero and at most 2 m.')
        bounds = np.asarray(joint_bounds, dtype=float)
        if (bounds.shape != (6, 2) or not np.isfinite(bounds).all()
                or (bounds[:, 0] >= bounds[:, 1]).any() or (np.abs(bounds) > 4*math.pi).any()):
            raise ValueError('Supply six finite known joint bounds within the Scene domain.')
        if not isinstance(binding, dict):
            raise ValueError('Supply the caller operation/robot/command binding object.')
        snapshot = _snapshot(scene, c.tolist(), tcp, target, bounds, binding,
                             None if command_start is None else initial)
        result.update(snapshot, binding_sha256=_hash(snapshot), numeric_assumptions=provided,
                      limits=asdict(limits), tracking_tolerance_rad=[math.radians(.2)]*6,
                      hull_gap_allowance_m=numerics.gap_error_m,
                      controller_branch_assumption={
                          'total_componentwise_joint_tube_rad': math.radians(.2),
                          'meaning': 'Controller branch for this exact command remains within this total tube of the certified corrected-model IK branch; model mismatch and tracking share this budget.',
                          'qualification': 'Assumed, not established by finite controller FK samples. New Cartesian use of inherited tracking/model assumptions.'},
                      certificate_scope='engineering_model_check_under_stated_numeric_and_scene_assumptions')
        raw_bounds = derivative_bounds(scene, tcp, numerics.rotation_scale_m)
        m = raw_bounds['M']*(1+numerics.bound_relative_error)+numerics.arithmetic_error
        if not np.isfinite(m) or m <= 0:
            raise ValueError('No finite analytic derivative bound.')
        result['derivative_bounds'] = dict(raw_bounds, M=m)
        tracking = np.full(6, math.radians(.2))
        deadline = began+limits.max_time_s
        pos, step, depth, incoming_error = 0., length, 0, 0.
        reference = initial[:3, :3]
        ell, eps = numerics.rotation_scale_m, numerics.arithmetic_error
        def y(z):
            return np.r_[initial[:3, 3]+[0, 0, direction*z], np.zeros(3)]
        while pos < length:
            if time.monotonic() >= deadline:
                return finish('unresolved', 'Time limit exhausted.')
            if result['nodes'] >= limits.max_nodes:
                return finish('unresolved', 'Node limit exhausted.')
            result['nodes'] += 1
            end = min(length, pos+step)
            if end <= pos:
                return finish('unresolved', 'Interval progress is not representable.')
            f, b = evaluate(scene, c, tcp, reference, ell)
            if not np.isfinite(f).all() or not np.isfinite(b).all():
                return finish('unresolved', 'Nonfinite FK or analytic Jacobian.')
            if np.linalg.cond(b) > 1e8:
                return finish('unresolved', 'Jacobian is singular or poorly conditioned.')
            inverse = np.linalg.inv(b)
            cnorm = np.linalg.norm(inverse, 'fro')*(1+eps)+eps
            inverse_residual = (np.linalg.norm(np.eye(6)-inverse@b, 'fro')*(1+eps)
                                + eps*(1+cnorm*np.linalg.norm(b, 'fro')))
            def residual_bound(value):
                return float(np.linalg.norm(inverse@value)*(1+eps)
                             + cnorm*numerics.fk_error+eps*(1+cnorm*np.linalg.norm(value)))
            alpha = max(residual_bound(f-y(z)) for z in (pos, end))
            r = max(2.1*alpha, 2.1*incoming_error, 1e-8)
            gamma = (inverse_residual + cnorm*(numerics.jacobian_error+m*r))*(1+eps)+eps
            angle = math.acos(float(np.clip((np.trace(reference.T@endpoint(scene,c,tcp)[:3,:3])-1)/2, -1, 1)))
            limit_margin = float(np.min(np.r_[c-bounds[:, 0], bounds[:, 1]-c]-r-np.tile(tracking, 2)))
            finite = np.isfinite([cnorm,inverse_residual,alpha,r,gamma,angle,limit_margin]).all()
            contraction = (finite and gamma < 1-eps and alpha+gamma*r+eps < r
                           and incoming_error+eps < r
                           and angle+math.sqrt(6)*r+math.sqrt(6*numerics.fk_error/ell) < math.pi/2-eps)
            detail = {}
            if contraction and limit_margin > eps:
                if not result['balls']:
                    initial_error = residual_bound(f-y(0))/(1-gamma-eps)*(1+eps)+eps
                    if not np.isfinite(initial_error) or initial_error+eps >= np.min(tracking):
                        return finish('unresolved', 'Initial model root exceeds the total joint correspondence envelope.',
                                      initial_root_error_rad=float(initial_error))
                    result['initial_root_error_rad'] = float(initial_error)
                status, detail = _collision_ball(scene,c,r,tracking,numerics,deadline)
                if status in ('unresolved','collision'):
                    return finish(status, **detail)
                if status == 'clear_in_model':
                    target_end = initial.copy(); target_end[2, 3] += direction*end
                    seed = c.tolist()
                    if ik is None:
                        d = model_ik(scene,target_end,tcp,seed,deadline=deadline)
                    else:
                        try:
                            d = ik(target_end.copy(),list(seed))
                        except Exception as exc:
                            return finish('unresolved', 'IK callback failed: '+str(exc))
                    if time.monotonic() >= deadline:
                        return finish('unresolved', 'Time limit exhausted after IK.')
                    d = vector(np.asarray(d).tolist(),size=6,bound=4*math.pi)
                    distance = np.linalg.norm(d-c)*(1+eps)+eps
                    if distance+eps >= r:
                        return finish('unresolved', 'IK candidate is not strictly inside preceding ball.')
                    fd, _ = evaluate(scene,d,tcp,reference,ell)
                    numerator = residual_bound(fd-y(end))
                    error = (numerator/(1-gamma-eps))*(1+eps)+eps
                    if not np.isfinite(error) or error+eps >= r-distance:
                        return finish('unresolved', 'Recentering residual enclosure is not inside preceding ball.')
                    result['balls'].append({'z_interval_m': [direction*pos,direction*end],
                        'center_joints': c.tolist(), 'radius_rad': float(r),
                        'alpha': float(alpha), 'gamma': float(gamma),
                        'inverse_norm_bound': float(cnorm), 'inverse_residual_bound': float(inverse_residual),
                        'incoming_root_error_rad': float(incoming_error),
                        'outgoing_root_error_rad': float(error), 'orientation_cap_margin_rad': float(math.pi/2-angle-math.sqrt(6)*r),
                        'joint_limit_margin_rad': limit_margin, **detail})
                    c, incoming_error, pos = d, error, end
                    step, depth = min(step*2,length-pos), 0
                    continue
            if depth >= limits.max_depth or step/2 < limits.min_step_m:
                return finish('unresolved', 'Subdivision/minimum step exhausted.',
                              last_contraction={'alpha':float(alpha),'gamma':float(gamma),'radius_rad':float(r),
                                                'joint_limit_margin_rad':limit_margin}, **detail)
            step /= 2; depth += 1
        result['endpoint_joints'] = c.tolist()
        result['endpoint_root_error_rad'] = float(incoming_error)
        return finish('clear_in_model')
    except (CollisionError, ValueError, TypeError, KeyError, OverflowError, np.linalg.LinAlgError) as exc:
        return finish('unresolved', str(exc))
