"""Read a fresh pose snapshot through the existing receive-only Primary monitor."""
from datetime import datetime, timedelta, timezone
import ipaddress
import math
import time


def read_pose(host):
    if not isinstance(host, str):
        raise ValueError('Enter the robot IP address.')
    try:
        host = str(ipaddress.ip_address(host.strip()))
    except ValueError as exc:
        raise ValueError('Enter the robot IP address.') from exc
    from ur_handeye.primary import PrimaryMonitor

    monitor = PrimaryMonitor(host)
    try:
        first = monitor.snapshot(timeout=1)
        state = monitor.snapshot(after=first.received_at, timeout=1)
        joints = list(state.joints)
        if len(joints) != 6 or any(not math.isfinite(q) or abs(q) > 4 * math.pi for q in joints):
            raise ValueError('Robot feedback must contain six finite joint angles within the preview limits.')
        if state.timestamp_us <= first.timestamp_us:
            raise ValueError('Robot feedback did not advance. Read the pose again.')
        age = max(0, time.monotonic() - state.received_at)
        received = datetime.now(timezone.utc) - timedelta(seconds=age)
        return {'joints_rad': joints, 'received_at_utc': received.isoformat(),
                'robot_timestamp_s': state.timestamp_us / 1_000_000,
                'robot_ip': host, 'source': 'primary_read_only'}
    finally:
        monitor.close()
