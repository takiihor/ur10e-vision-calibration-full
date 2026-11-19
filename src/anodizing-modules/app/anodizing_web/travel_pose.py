"""Operator-taught AGV travel joints and read-only robot feedback."""
import json
import math
from pathlib import Path
import time

from ur_pick_place.config import vector
from ur_pick_place.vision.cli import Runtime

from .agv import timestamp
from .jobs import atomic_json
from .paths import check_tree, inside
from .site_profile import section

CONFIRMATION = 'Folded inside the AGV footprint, with and without a rod'
MISSING = 'Save the AGV travel pose on Teaching before AGV movement.'
OUTSIDE = 'Robot is outside the saved AGV travel pose or moving. Fold the arm and wait for it to stop.'
UNAVAILABLE = 'Live robot feedback is unavailable. Check the robot connection and try again.'


def path(root):
    return Path(root) / 'agv-travel-pose.json'


def load(root):
    artifact = path(root)
    if artifact.is_symlink():
        raise ValueError('Review the saved AGV travel pose. A symbolic link is not allowed.')
    try:
        pose = json.loads(artifact.read_text())
    except FileNotFoundError:
        return None
    if (not isinstance(pose, dict) or pose.get('confirmation') != CONFIRMATION
            or not isinstance(pose.get('saved_at'), str) or not pose['saved_at']
            or not isinstance(pose.get('robot_ip'), str) or not pose['robot_ip']):
        raise ValueError('Save a new confirmed AGV travel pose on Teaching.')
    vector(pose.get('joints_rad'), 6, 'AGV travel joints')
    return pose


def at_pose(joints, pose):
    return all(abs(a-b) <= math.radians(2) for a,b in zip(joints, pose['joints_rad'], strict=True))


def _robot_ip(root, project=None):
    profile = section(root, 'robot_control')
    name = project or profile.get('teaching_project')
    configured = profile.get('robot_ip') or ''
    if name:
        directory = check_tree(root, inside(root, name))
        selected = json.loads((directory/'project.json').read_text())['settings']['robot_ip']
        if configured and selected != configured:
            raise ValueError('Select a teaching project for the configured robot.')
        configured = selected
    if not isinstance(configured, str) or not configured.strip():
        raise ValueError('Select a saved teaching project or configure the robot address in the site profile.')
    return configured.strip()


def _read_stopped(robot_ip):
    receive = None
    try:
        receive = Runtime.receive(robot_ip)
        previous = receive.getTimestamp()
        before = vector(receive.getActualQ(), 6, 'Actual robot joints')
        moving = max(abs(v) for v in vector(receive.getActualQd(), 6, 'Actual joint velocities')) > .001
        deadline = time.monotonic() + .25
        while time.monotonic() < deadline:
            time.sleep(.025)
            stamp = receive.getTimestamp()
            if not receive.isConnected() or receive.getSafetyMode() not in (1, 2) or not math.isfinite(stamp):
                break
            if stamp > previous:
                joints = vector(receive.getActualQ(), 6, 'Actual robot joints')
                stopped = not moving and max(abs(v) for v in vector(
                    receive.getActualQd(), 6, 'Actual joint velocities')) <= .001
                return joints, stopped and max(abs(a-b) for a,b in zip(before,joints)) <= .0002
    except (ImportError, OSError, RuntimeError, ValueError, TypeError) as exc:
        raise ValueError(UNAVAILABLE) from exc
    finally:
        if receive is not None:
            try:
                receive.disconnect()
            except (OSError, RuntimeError) as exc:
                raise ValueError(UNAVAILABLE) from exc
    raise ValueError(UNAVAILABLE)


def _check(owner, pose):
    profile = section(owner.root, 'robot_control')
    if (profile.get('robot_ip') or profile.get('teaching_project')) and _robot_ip(owner.root) != pose['robot_ip']:
        raise ValueError('The saved travel pose belongs to another robot. Save the pose for the configured robot.')
    joints, stopped = _read_stopped(pose['robot_ip'])
    return stopped and at_pose(joints, pose)


def motion_block(owner):
    try:
        pose = load(owner.root)
        return MISSING if pose is None else '' if _check(owner, pose) else OUTSIDE
    except (OSError, ValueError, KeyError, TypeError) as exc:
        return str(exc)


def state(owner):
    result = dict(robot_configured=owner.robot_configured, allow_agv_in_recipe=owner.allow_agv_in_recipe,
                  pose=None, at_pose=None, detail='Robot access is disabled.', agv=owner.agv.link_state())
    if not owner.robot_configured:
        return result
    try:
        pose = load(owner.root)
        result['pose'] = pose
        if pose is None:
            result['detail'] = MISSING
        elif owner.lock.acquire(blocking=False):
            try:
                if owner.active or owner.mcu_pending or owner.closing:
                    result['detail'] = 'A job owns the robot. Follow Current operation for live checks.'
                else:
                    result['at_pose'] = _check(owner, pose)
                    result['detail'] = 'Robot at travel pose and still.' if result['at_pose'] else OUTSIDE
            finally:
                owner.lock.release()
        else:
            result['detail'] = 'Waiting for the current robot check.'
    except (OSError, ValueError, TypeError, KeyError) as exc:
        result['detail'] = str(exc)
    return result


def save(owner, body):
    if not owner.robot_configured:
        raise ValueError('Start the service with reviewed robot access before saving a travel pose.')
    if body.get('confirmed') is not True:
        raise ValueError('Confirm the arm fits within the AGV footprint with and without a rod.')
    with owner.lock:
        if owner.active or owner.mcu_pending or owner.closing or owner.execution_uncertain or owner.storage_error:
            raise ValueError('Wait until the robot and service are ready before saving the travel pose.')
        robot_ip = _robot_ip(owner.root, body.get('project'))
        joints, stopped = _read_stopped(robot_ip)
        if not stopped:
            raise ValueError('Wait for the robot to stop before saving the travel pose.')
        pose = dict(robot_ip=robot_ip, joints_rad=joints, saved_at=timestamp(), confirmation=CONFIRMATION)
        atomic_json(path(owner.root), pose)
        return pose
