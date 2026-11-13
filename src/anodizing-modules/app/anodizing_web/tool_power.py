"""Explicit controller tool-port supply jobs. No gripper or motion/control SDK."""
import hashlib
import ipaddress
import json
import socket
import struct
import time

from .paths import check_tree, inside

MAX_FRAME = 1024 * 1024
READ_TIMEOUT = 3.0
SEND_TIMEOUT = 2.0
SCRIPTS = {
    'on': b'sec tool_power():\n  set_tool_voltage(24)\nend\n',
    'off': b'sec tool_power():\n  set_tool_voltage(0)\nend\n',
}
RELEASED = ('no_confirmed_item', 'operator_confirmed_released')


def decode_state(message):
    """Return timestamp and toolOutputVoltage from the same complete RobotState."""
    if not 5 <= len(message) <= MAX_FRAME or struct.unpack_from('!I', message)[0] != len(message):
        raise ValueError('Invalid Primary frame length.')
    if message[4] != 16:
        return None
    parts = {}
    offset = 5
    while offset < len(message):
        if len(message) - offset < 5:
            raise ValueError('Truncated Primary subpackage.')
        length, kind = struct.unpack_from('!IB', message, offset)
        if length < 5 or offset + length > len(message):
            raise ValueError('Invalid Primary subpackage length.')
        if kind in (0, 2):
            if kind in parts:
                raise ValueError('Duplicate Primary state subpackage.')
            parts[kind] = message[offset + 5:offset + length]
        offset += length
    if 0 not in parts or 2 not in parts:
        return None
    if len(parts[0]) < 8 or len(parts[2]) < 32:
        raise ValueError('Truncated Primary timestamp or ToolData.')
    timestamp = struct.unpack_from('!Q', parts[0])[0]
    voltage = parts[2][22]  # toolVoltage48V is a different field; never use it as supply status.
    if voltage not in (0, 12, 24):
        raise ValueError('Unknown controller tool output voltage.')
    return timestamp, voltage


def _remaining(deadline):
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError('Controller tool-port voltage readback deadline expired.')
    return remaining


def _exact(sock, count, deadline):
    data = bytearray()
    while len(data) < count:
        sock.settimeout(_remaining(deadline))
        chunk = sock.recv(count - len(data))
        if not chunk:
            raise ConnectionError('Controller tool-port voltage stream closed or truncated.')
        data.extend(chunk)
    return bytes(data)


def read_voltage(host, *, expected=None):
    """New read-only connection; discard initial state and require controller progress."""
    deadline = time.monotonic() + READ_TIMEOUT
    baseline = None
    with socket.create_connection((host, 30011), timeout=_remaining(deadline)) as sock:
        while True:
            header = _exact(sock, 4, deadline)
            length = struct.unpack('!I', header)[0]
            if not 5 <= length <= MAX_FRAME:
                raise ValueError('Invalid or oversized Primary frame.')
            sample = decode_state(header + _exact(sock, length - 4, deadline))
            if sample is None:
                continue
            timestamp, voltage = sample
            if baseline is None:
                baseline = timestamp
                continue
            if timestamp < baseline:
                raise ValueError('Controller timestamp moved backwards; voltage is unknown.')
            if timestamp == baseline:
                continue
            baseline = timestamp
            if expected is None or voltage == expected:
                return {'controller_tool_output_voltage': voltage,
                        'controller_timestamp_us': timestamp, 'observed_at': time.time(),
                        'source': 'controller_primary_tool_data', 'read_only_port': 30011,
                        'status': 'observed'}


def _project(root, selected):
    project = check_tree(root, inside(root, selected))
    path = inside(root, str(project / 'project.json'))
    if path.stat().st_size > MAX_FRAME:
        raise ValueError('Saved project is too large.')
    raw = path.read_bytes()
    saved = json.loads(raw)
    if (not isinstance(saved, dict) or saved.get('schema_version') != 1 or
            saved.get('type') != 'marker_pick_project' or saved.get('simulation') or
            not isinstance(saved.get('settings'), dict) or not isinstance(saved.get('flow'), dict)):
        raise ValueError('Select a saved native, non-synthetic teaching project.')
    host = saved['settings'].get('robot_ip')
    try:
        if not isinstance(host, str):
            raise ValueError('Saved robot endpoint must be a string.')
        address = ipaddress.IPv4Address(host)
    except (ValueError, TypeError, ipaddress.AddressValueError) as exc:
        raise ValueError('Saved project must select a numeric robot IPv4 endpoint.') from exc
    if address.is_loopback or address.is_unspecified or address.is_multicast or int(address) == 0xffffffff:
        raise ValueError('Saved project robot endpoint must be a unicast robot address.')
    return project, str(address), hashlib.sha256(raw).hexdigest()


def _record(owner, job, evidence, *, uncertain, change_fence=False):
    with owner.lock:
        job.update(tool_power=dict(evidence), execution_uncertain=uncertain)
        try:
            owner._save(job)
        except (OSError, ValueError, TypeError) as exc:
            owner.storage_error = str(exc)
            raise
        if change_fence:
            owner.execution_uncertain = uncertain


def _run(owner, body, job_id):
    with owner.lock:
        job = owner.jobs[job_id]
        if owner.active != job_id or job['module'] != 'tool-power':
            raise ValueError('Tool power requires the active shared IPC job.')
        if owner.storage_error or (owner.execution_uncertain and body['action'] != 'read') or owner.closing:
            raise ValueError('IPC storage, uncertain execution or closing fence blocks tool power.')
    project, host, digest = _project(owner.root, body['project'])
    evidence = {'project': str(project), 'project_sha256': digest, 'robot_host': host,
                'action': body['action'], 'operator_item_state': body['item_state'],
                'controller_tool_output_voltage': None, 'observed_at': None,
                'status': 'reading' if body['action'] == 'read' else 'preparing'}
    command = body['action'] != 'read'
    dispatched = False
    _record(owner, job, evidence, uncertain=False)
    try:
        if command:
            deadline = time.monotonic() + SEND_TIMEOUT
            with socket.create_connection((host, 30002), timeout=_remaining(deadline)) as sock:
                sock.settimeout(_remaining(deadline))
                # Persist the electrical fence only at the send boundary, after connection succeeds.
                evidence['status'] = 'dispatching'
                _record(owner, job, evidence, uncertain=True, change_fence=True)
                dispatched = True
                sock.sendall(SCRIPTS[body['action']])
            evidence['command_sent_at'] = time.time()
        # No cached/pre-command reading is eligible. Sending is not execution acknowledgement.
        result = read_voltage(host, expected={'on': 24, 'off': 0}.get(body['action']))
        evidence.update(result)
        evidence['requested_voltage'] = {'on': 24, 'off': 0}.get(body['action'])
        _record(owner, job, evidence, uncertain=False, change_fence=dispatched)
        return dict(evidence)
    except Exception as exc:
        status = 'unknown' if dispatched or not command else 'not_sent'
        evidence.update(status=status, controller_tool_output_voltage=None, observed_at=None, error=str(exc))
        _record(owner, job, evidence, uncertain=dispatched, change_fence=dispatched)
        detail = 'Electrical command not sent' if status == 'not_sent' else 'Controller tool-port voltage unknown'
        raise ValueError(f'{detail}: {exc}. No automatic resend or rollback.') from exc


def submit(owner, body):
    """Call only after the server's existing Host/Origin/token/body checks."""
    if not isinstance(body, dict) or set(body) != {'project', 'action', 'request_id', 'item_state'}:
        raise ValueError('Tool power accepts only project, action, request_id and item_state.')
    body = dict(body)
    action, item = body['action'], body['item_state']
    if action not in ('on', 'off', 'read'):
        raise ValueError('Choose tool power on, off or read.')
    if action == 'off' and item not in RELEASED:
        raise ValueError('Power off requires operator-selected Released / no held item.')
    if item not in (*RELEASED, 'operator_confirmed_held', 'unknown'):
        raise ValueError('Select a valid current operator item state.')
    if not owner.allow_manual_pick_place:
        raise ValueError('Tool power requires reviewed --allow-manual-pick-place on IPC.')
    request_id = body.pop('request_id')
    # Only this receive-only operation may use the owner's existing verify admission.
    admission = dict(body, action='verify') if action == 'read' else body
    return owner.submit(request_id, 'tool-power', admission, lambda job_id: _run(owner, body, job_id))


def capability(owner):
    """Cached evidence only; opening a page or GET never opens a controller connection."""
    with owner.lock:
        jobs = [job for job in owner.jobs.values() if job['module'] == 'tool-power' and job.get('tool_power')]
        latest = max(jobs, key=lambda job: job['created']) if jobs else None
        last = dict(latest['tool_power'], job_id=latest['id'], job_state=latest['state']) if latest else None
        reason = ('Enable reviewed --allow-manual-pick-place on IPC.' if not owner.allow_manual_pick_place else
                  'Job storage failed.' if owner.storage_error else
                  'Execution is uncertain; physical reconciliation is required.' if owner.execution_uncertain else
                  'Another job owns the service.' if owner.active else
                  'Service is closing.' if owner.closing else '')
        read_available = bool(owner.allow_manual_pick_place and not owner.active and not owner.storage_error and not owner.closing)
        return {'available': not bool(reason), 'write_available': not bool(reason),
                'read_available': read_available, 'detail': reason,
                'label': 'Controller tool-port voltage', 'last_known': last,
                'cached_only': True, 'supply_voltage': 24}
