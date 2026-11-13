"""Explicit one-byte MCU requests through the configured robot tool forwarder."""
from copy import deepcopy
import hashlib
import json
import re
import socket
import threading
import time

from .jobs import ID
from .tool_power import _project

COMMANDS = {'0': b'0', '1': b'1', '2': b'2', '3': b'3'}
EXPECTED = {'1': 'Tight', '2': 'Loose', '3': 'Open'}
READ_TIMEOUT, WRITE_TIMEOUT, QUIET, MAX_REPLY = 2.0, .5, .1, 128


def parse_reply(command, raw):
    if not raw or len(raw) > MAX_REPLY:
        raise ValueError('MCU reply is empty or exceeds 128 bytes.')
    text = raw.decode('ascii').strip('\x00\r\n ')
    if command == '0':
        match = re.search(r'FSR\s*1\s*:\s*(-?\d+)\s*/\s*FSR\s*2\s*:\s*(-?\d+)', text, re.IGNORECASE)
        if not match:
            raise ValueError('MCU reply must contain FSR1:n/FSR2:n.')
        return {'fsr': [int(match[1]), int(match[2])]}
    if text.casefold() != EXPECTED[command].casefold():
        raise ValueError('MCU reply does not match '+EXPECTED[command]+'.')
    return {}


def _parent(owner):
    job = owner.jobs.get(owner.active, {})
    manual = job.get('manual', {})
    prompt = manual.get('prompt')
    runner = owner.runner
    if (job.get('module') != 'manual-pick-place' or job.get('auto_action') not in ('PICK', 'PLACE') or
            job.get('state') != 'waiting' or not prompt or job.get('pending') != prompt or
            manual.get('stage') != 'awaiting_'+str(prompt.get('phase')) or
            prompt.get('phase') not in ('grip', 'release') or job.get('manual_cancelled') or
            not runner or runner.cancelled.is_set()):
        return None
    live = runner.state().get('manual', {})
    if live.get('prompt') != prompt or live.get('stage') != manual['stage'] or live.get('operation') != job['auto_action']:
        return None
    return {'job_id': job['id'], 'prompt': deepcopy(prompt)}


def _check(owner, body, project, host, digest):
    if owner.closing or owner.storage_error or owner.execution_uncertain:
        raise ValueError('IPC closing, storage or uncertain execution blocks MCU commands.')
    if body.get('parent'):
        if _parent(owner) != body['parent']:
            raise ValueError('The grip/release prompt changed or stopped; no MCU command sent.')
        context = owner.runner.context
        if (str(project) != str(context.provenance['project']) or host != context.settings['robot_ip'] or
                context.hashes.get(str(project/'project.json')) != digest):
            raise ValueError('MCU project and robot must match the active Pick/Place operation.')
    elif owner.active and owner.active != body['request_id']:
        raise ValueError('Another job owns the service.')


def _record(owner, job, evidence, uncertain):
    with owner.lock:
        job.update(mcu=dict(evidence), execution_uncertain=uncertain)
        try:
            owner._save(job)
        except (OSError, ValueError, TypeError) as exc:
            owner.storage_error = str(exc)
            raise


def _drain(sock):
    """Discard queued pre-command data, matching Anobot's input-buffer flush."""
    deadline = time.monotonic()+READ_TIMEOUT
    count = 0
    while time.monotonic() < deadline:
        sock.settimeout(min(QUIET, max(.001, deadline-time.monotonic())))
        try:
            chunk = sock.recv(MAX_REPLY+1)
        except socket.timeout:
            return
        if not chunk:
            raise ConnectionError('MCU forwarder closed before command dispatch.')
        count += len(chunk)
        if count > MAX_REPLY:
            raise ValueError('MCU forwarder has excessive queued data; command not sent.')
    raise TimeoutError('MCU input did not become quiet; command not sent.')


def _receive(sock, evidence):
    raw = bytearray()
    deadline = time.monotonic()+READ_TIMEOUT
    while time.monotonic() < deadline and len(raw) <= MAX_REPLY:
        sock.settimeout(min(QUIET if raw else READ_TIMEOUT, max(.001, deadline-time.monotonic())))
        try:
            chunk = sock.recv(MAX_REPLY+1-len(raw))
        except socket.timeout:
            break
        if not chunk:
            break
        raw.extend(chunk)
        evidence.update(rx_text=bytes(raw).decode('ascii', errors='replace'), rx_hex=bytes(raw).hex(' '))
    return bytes(raw)


def _run(owner, body, project, host, digest, dispatch_guard=None):
    job = owner.jobs[body['request_id']]
    evidence = dict(project=str(project), project_sha256=digest, robot_host=host, port=54321,
        command=body['command'], tx_hex=COMMANDS[body['command']].hex(), rx_text='', rx_hex='',
        status='preparing', parent=body.get('parent'), observed_at=None)
    dispatched = False
    actuator = body['command'] != '0'
    try:
        _record(owner, job, evidence, False)
        with socket.create_connection((host, 54321), timeout=WRITE_TIMEOUT) as sock:
            _drain(sock)
            with owner.lock:
                (dispatch_guard() if dispatch_guard else _check(owner, body, project, host, digest))
                evidence['status'] = 'dispatching'
                _record(owner, job, evidence, actuator)
                # Recheck after persistence: slow fsync cannot hide a changed project or Stop.
                if _project(owner.root, body['project']) != (project, host, digest):
                    raise ValueError('Saved MCU project changed during dispatch persistence.')
                (dispatch_guard() if dispatch_guard else _check(owner, body, project, host, digest))
                # The durable flag survives restart. mcu_pending fences live admission until completion.
                sock.settimeout(WRITE_TIMEOUT)
                dispatched = True
                sock.sendall(COMMANDS[body['command']])
            raw = _receive(sock, evidence)
            evidence.update(parse_reply(body['command'], raw))
            evidence.update(status='replied', observed_at=time.time())
            _record(owner, job, evidence, False)
            return dict(evidence)
    except Exception as exc:
        evidence.update(status='unknown' if dispatched else 'not_sent', error=str(exc))
        if dispatched and actuator:
            with owner.lock:
                owner.execution_uncertain = True
        _record(owner, job, evidence, dispatched and actuator)
        raise ValueError(str(exc)+' No automatic resend. Inspect this request and the physical item state.') from exc
    finally:
        if not body.get('parent'):
            with owner.lock:
                owner.mcu_pending = None


def _child(owner, body, project, host, digest):
    """Finish only the MCU child. The paused native parent keeps its ownership."""
    job = owner.jobs[body['request_id']]
    try:
        result = _run(owner, body, project, host, digest)
        with owner.lock:
            job.update(state='done', result=result)
    except Exception as exc:
        with owner.lock:
            job.update(state='failed', error=str(exc))
    finally:
        with owner.lock:
            job['finished'] = time.time()
            try:
                owner._save(job)
            except (OSError, ValueError, TypeError) as exc:
                owner.storage_error = str(exc)
            owner.mcu_pending = None


def submit(owner, body):
    """Use only after the existing authenticated HTTP boundary checks."""
    if (not isinstance(body, dict) or set(body) not in (
            {'project', 'command', 'request_id'}, {'project', 'command', 'request_id', 'parent'}) or
            not isinstance(body.get('project'), str) or
            not isinstance(body.get('command'), str) or body['command'] not in COMMANDS):
        raise ValueError('MCU accepts a saved project, command 0/1/2/3, request_id and optional parent prompt.')
    body = dict(body)
    body.setdefault('parent', None)
    if body['parent'] is not None and (not isinstance(body['parent'], dict) or
            set(body['parent']) != {'job_id', 'prompt'} or not isinstance(body['parent']['prompt'], dict)):
        raise ValueError('Supply the exact current grip/release parent prompt.')
    request_id = body['request_id']
    if not isinstance(request_id, str) or not ID.fullmatch(request_id):
        raise ValueError('Supply a valid MCU request ID.')
    if not owner.allow_manual_pick_place:
        raise ValueError('MCU commands require reviewed --allow-manual-pick-place on IPC.')
    payload = {key: value for key, value in body.items() if key != 'request_id'}
    fingerprint = hashlib.sha256(json.dumps(['mcu', payload], sort_keys=True, separators=(',', ':'), allow_nan=False).encode()).hexdigest()
    with owner.lock:
        old = owner.jobs.get(request_id)
        if old:
            if old['fingerprint'] != fingerprint:
                raise ValueError('Request ID was already used for a different request.')
            return owner.get(request_id)
        if owner.mcu_pending:
            raise ValueError('An MCU request is still pending. Inspect it before another command.')
        project, host, digest = _project(owner.root, body['project'])
        _check(owner, body, project, host, digest)
        if body['parent']:
            job = dict(id=request_id, module='mcu', fingerprint=fingerprint, state='running', created=time.time(),
                events=[], pending=None, result=None, error=None, parent_job=body['parent']['job_id'])
            owner._save(job)
            owner.jobs[request_id] = job
            owner.mcu_pending = request_id
            threading.Thread(target=_child, args=(owner, body, project, host, digest), daemon=True).start()
        else:
            job = owner.submit(request_id, 'mcu', payload, lambda _: _run(owner, body, project, host, digest))
            owner.mcu_pending = request_id
        return dict(job)


def capability(owner):
    """Cache only. GET does not connect to the robot or change tool configuration."""
    with owner.lock:
        jobs = [job for job in owner.jobs.values() if job.get('module') == 'mcu' and job.get('mcu')]
        latest = max(jobs, key=lambda job: job['created']) if jobs else None
        parent = _parent(owner)
        reason = ('Enable reviewed --allow-manual-pick-place on IPC.' if not owner.allow_manual_pick_place else
            'Job storage failed.' if owner.storage_error else 'Service is closing.' if owner.closing else
            'Execution is uncertain. Check the physical state.' if owner.execution_uncertain else
            'An MCU request is pending.' if owner.mcu_pending else
            'Another operation owns the service. Wait for grip/release confirmation.' if owner.active and not parent else '')
        return dict(available=not bool(reason), detail=reason, cached_only=True, parent=parent,
            confirmation_detail=('MCU request pending. Wait for its reply.' if owner.mcu_pending else
                'Execution is uncertain. Check the physical state.' if owner.execution_uncertain else
                'Job storage failed.' if owner.storage_error else ''),
            pending=owner.mcu_pending, last_known=dict(latest['mcu'], job_id=latest['id'], job_state=latest['state']) if latest else None)


def run_automatic(owner, worker, step, command):
    """Private synchronous child of one approved repeat; HTTP cannot supply this authority."""
    project, host, digest = _project(owner.root, worker.context.provenance['project'])
    request_id = 'auto-mcu-'+hashlib.sha256(json.dumps([worker.parent_job_id,step,command]).encode()).hexdigest()
    body = dict(request_id=request_id, project=str(project), command=command,
        parent={'job_id':worker.parent_job_id,'automatic_step':step})
    def guard():
        job = owner.jobs.get(worker.parent_job_id, {})
        if (owner.active != worker.parent_job_id or owner.runner is not worker or
                job.get('auto_action') != 'REPEAT' or job.get('manual_cancelled') or
                owner.closing or owner.storage_error or owner.execution_uncertain or
                worker.cancelled.is_set() or worker._shutdown.is_set() or worker.step != step or
                str(project) != str(worker.context.provenance['project']) or
                worker.context.hashes.get(str(project/'project.json')) != digest or
                worker.context.settings['robot_ip'] != host):
            raise ValueError('Automatic MCU authority changed; no new command.')
        worker._stationary()
    with owner.lock:
        guard()
        if owner.mcu_pending or request_id in owner.jobs:
            raise ValueError('Automatic MCU request is pending or already recorded; no replay.')
        job = dict(id=request_id,module='mcu',fingerprint=hashlib.sha256(json.dumps(body,sort_keys=True).encode()).hexdigest(),
            state='running',created=time.time(),events=[],pending=None,result=None,error=None,parent_job=worker.parent_job_id)
        owner._save(job)
        owner.jobs[request_id] = job
        owner.mcu_pending = request_id
    try:
        result = _run(owner,body,project,host,digest,guard)
        with owner.lock:
            job.update(state='done',result=result)
        return result
    except Exception as exc:
        with owner.lock:
            job.update(state='failed',error=str(exc))
        raise
    finally:
        with owner.lock:
            job['finished'] = time.time()
            try:
                owner._save(job)
            except (OSError,ValueError,TypeError) as exc:
                owner.storage_error = str(exc)
            if owner.mcu_pending == request_id:
                owner.mcu_pending = None
