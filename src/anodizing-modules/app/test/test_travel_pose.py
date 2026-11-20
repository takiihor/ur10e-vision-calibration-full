"""Travel pose teaching and live checks with read-only RTDE-shaped feedback."""
import json
import math
from types import SimpleNamespace

import pytest

from anodizing_web import agv, travel_pose
from anodizing_web.jobs import JobOwner


@pytest.fixture
def robot(tmp_path, monkeypatch):
    (tmp_path/'site-profile.json').write_text(json.dumps({'robot_control':{'robot_ip':'192.0.2.1'}}))
    owner = JobOwner(tmp_path, robot_read_access=True)
    owner.agv = agv.Connection(owner)
    feedback = SimpleNamespace(joints=[0.1]*6, velocity=[0.0]*6, closed=0, stamp=0)
    def timestamp():
        feedback.stamp += .025
        return feedback.stamp
    def disconnect():
        feedback.closed += 1
    receive = SimpleNamespace(getTimestamp=timestamp, getActualQ=lambda:feedback.joints,
        getActualQd=lambda:feedback.velocity, isConnected=lambda:True, getSafetyMode=lambda:1,
        disconnect=disconnect)
    def open_receive(host):
        assert host == '192.0.2.1'
        return receive
    monkeypatch.setattr(travel_pose.Runtime,'receive',open_receive)
    yield owner, feedback
    owner.agv.close()


def test_save_binds_live_robot_joints_confirmation_and_timestamp(robot):
    owner, feedback = robot
    with pytest.raises(ValueError,match='Confirm'):
        travel_pose.save(owner, {'joints_rad':[7]*6})
    assert travel_pose.load(owner.root) is None and feedback.closed == 0
    pose = travel_pose.save(owner, {'confirmed':True,'joints_rad':[7]*6})
    assert travel_pose.load(owner.root) == pose
    assert pose['joints_rad'] == [.1]*6
    assert pose['saved_at'].endswith('+00:00')
    assert pose['confirmation'] == travel_pose.CONFIRMATION
    assert feedback.closed == 1


def test_live_check_checks_every_joint_and_requires_stillness(robot):
    owner, feedback = robot
    travel_pose.save(owner, {'confirmed':True})
    feedback.joints[-1] += math.radians(1.99)
    assert travel_pose.state(owner)['at_pose'] is True
    feedback.joints[-1] += math.radians(.02)
    assert travel_pose.state(owner)['at_pose'] is False
    feedback.joints[-1] = .1
    feedback.velocity[0] = .01
    assert travel_pose.state(owner)['at_pose'] is False
    with pytest.raises(ValueError,match='stop'):
        travel_pose.save(owner, {'confirmed':True})
    assert travel_pose.load(owner.root)['joints_rad'] == [.1]*6


def test_robot_job_blocks_manual_move_but_travel_pose_allows_armed_move(robot):
    owner, _ = robot
    connection = owner.agv
    connection_id = connection.submit({'action':'connect', 'agv_mode':0})['connection_id']
    connection.submit(dict(action='arm',armed=True,connection_id=connection_id))
    connection.submit(dict(action='send',command='query_status',connection_id=connection_id))
    body = dict(action='send',command='forward',connection_id=connection_id,stop_generation=connection.stop_generation)
    with pytest.raises(ValueError,match='Save the AGV travel pose'):
        connection.submit(body)
    travel_pose.save(owner, {'confirmed':True})
    assert connection.submit(body)['results']['forward']['state'] == 'sent'
    owner.active = 'another-job'
    with pytest.raises(ValueError,match='robot job is running'):
        connection.submit(body)
