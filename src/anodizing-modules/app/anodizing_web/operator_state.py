"""Cache-only projection for operator views and recording."""
from datetime import datetime, timezone
from math import isfinite
from time import time

from . import agv, mcu, tool_power


def agv_state(sample, now=None):
    report, read_at, stamp, connected, mode, recipe, last_station, opened_at, error = sample[:9]
    now = sample[9] if now is None else now
    report = report or {}
    limit = 1000 * (agv.RECIPE_STALE_S if recipe else agv.STALE_AFTER)
    age = None if stamp is None else max(0, int(1000 * (now - stamp)))
    source = f'agv.{mode}.status' if mode else 'agv.status'
    station_mode = report.get('mode') == 2 and report.get('max_stations', 0) > 0

    def reported(key, name, value, unit=None):
        reason = ('AGV link lost. Use its e-stop if needed.' if error else 'Connect the AGV.') if not connected else (
            'No status report received.' if stamp is None else
            'Status expired. Check the radio and reconnect.' if age >= limit else
            'The report does not provide this value.' if value is None or
            (isinstance(value, float) and not isfinite(value)) else None)
        result = dict(id=f'agv.{key}', name=name, value=value if reason is None else None, unit=unit,
                      source=source, read_at=read_at, source_at=None,
                      freshness=dict(kind='max_age_ms', max_age_ms=limit),
                      age_ms=age, reason=reason,
                      quality='fresh' if reason is None else 'stale' if connected and age is not None and age >= limit else 'unknown')
        if reason and value is not None and (not isinstance(value, float) or isfinite(value)):
            result['last_read'] = dict(value=value, read_at=read_at)
        return result

    def event(key, name, value, at, unit=None, reason='No active AGV connection.'):
        return dict(id=f'agv.{key}', name=name, value=value, unit=unit, source='agv.connection',
                    read_at=at, source_at=None, freshness=dict(kind='on_event'), age_ms=None,
                    quality='unknown' if value is None else 'fresh', reason=reason if value is None else None)

    stopped = report.get('stopped')
    speed = report.get('actual_speed_m_h')
    moving = True if stopped is False or (speed is not None and speed != 0) else (
        False if stopped is True and speed == 0 else None)
    yield event('connected', 'AGV connected', connected, opened_at)
    yield event('transport', 'Transport', mode if connected else None, opened_at)
    yield event('last_confirmed_station', 'Last confirmed station',
                last_station[0] if connected and last_station else None,
                last_station[1] if connected and last_station else None, 'station',
                'No station arrival has been confirmed on this connection.')
    yield reported('moving', 'Moving', moving)
    yield reported('current_station', 'Current station', report.get('current_station') if station_mode else None, 'station')
    yield reported('target_station', 'Target station', report.get('target_station') if station_mode else None, 'station')
    for key, name, field, unit in (
        ('paused', 'Paused', 'paused', None),
        ('speed', 'Actual speed', 'actual_speed_m_h', 'm/h'),
        ('battery', 'Battery', 'battery_percent', '%'),
        ('fault', 'Fault', 'fault_code', None),
        ('obstacle_stop', 'Obstacle stop', 'obstacle_stop', None),
    ):
        yield reported(key, name, report.get(field), unit)


def project(sample, extra_variables, now):
    connection_id, sequence = sample[-2:]
    return dict(schema_version=1, generated_at=agv.timestamp(),
                agv=dict(connection_id=connection_id, sequence=sequence),
                variables=sorted((*agv_state(sample, now), *extra_variables), key=lambda variable: variable['id']))


def read(connection, extra_variables=()):
    sample = connection.state_sample()
    return project(sample, extra_variables, sample[9])


def operation_variables(owner):
    now = time()

    def value(key, name, data, job_id=None, at=None, *, unit=None, historical=False, row_id=None):
        stamp = age = None
        try:
            if type(at) in (int, float):
                stamp = datetime.fromtimestamp(at, timezone.utc).isoformat(timespec='milliseconds')
            elif isinstance(at, str) and datetime.fromisoformat(at).tzinfo is not None:
                stamp = at
            if historical and stamp is not None:
                age = max(0, int((now - datetime.fromisoformat(stamp).timestamp()) * 1000))
        except (ValueError, OverflowError, OSError):
            stamp = None
        return dict(id=key, name=name, value=data, unit=unit, source=key.split('.')[0],
                    read_at=stamp, source_at=None, freshness=dict(kind='on_event'), age_ms=age,
                    job_id=job_id, row_id=row_id or job_id,
                    quality='unknown' if data is None else 'historical' if historical else 'fresh',
                    reason='No recorded value.' if data is None else None)

    def result(job):
        return job.get('result') or {}

    def calibration_artifact(job):
        output = result(job)
        return output if output.get('calibration_sha256') and (
            job.get('calibration_action') == 'preview-calibration' or
            (output.get('result') or {}).get('type') in ('eye_in_hand', 'eye_to_hand')) else {}

    with owner.lock:
        active = owner.jobs.get(owner.active, {})
        manual = active.get('manual') or {}
        repeat = manual.get('repeat') or {}
        pending = active.get('pending') or {}
        phase = repeat.get('phase') or manual.get('stage') or pending.get('phase') or pending.get('kind')
        changed = active.get('manual_updated_at')
        jobs = owner.jobs.values()

        def latest(matches):
            return max((job for job in jobs if matches(job)), key=lambda job: job['created'], default={})

        calibration = latest(lambda job: job['module'] == 'calibration')
        fitted = latest(lambda job: job['module'] == 'calibration' and calibration_artifact(job))
        artifact = calibration_artifact(fitted)
        camera_job = latest(lambda job: job['module'] == 'calibration' and result(job).get('snapshot'))
        camera = result(camera_job).get('snapshot') or {}
        teaching = latest(lambda job: job['module'] == 'teaching' and result(job).get('project'))
        teacher = result(teaching).get('project')
        teacher = teacher if isinstance(teacher, dict) else result(teaching)
        snapshots = teacher.get('snapshots') or {}
        reference = snapshots.get('reference') or {}
        snapshot = max((item for item in snapshots.values() if item and item.get('acquired_at')),
                       key=lambda item: item['acquired_at'], default={})
        mcu_cap = mcu.capability(owner)
        power_cap = tool_power.capability(owner)
        mcu_outcome = mcu_cap['last_known'] or {}
        mcu_reply = latest(lambda job: (job.get('mcu') or {}).get('status') == 'replied')
        reply = mcu_reply.get('mcu') or {}
        power_outcome = power_cap['last_known'] or {}
        power_read = latest(lambda job: (job.get('tool_power') or {}).get('status') == 'observed')
        readback = power_read.get('tool_power') or {}
        return (
            value('job.active_id', 'Active job', owner.active, owner.active, active.get('created')),
            value('job.module', 'Active job module', active.get('module'), owner.active, active.get('created')),
            value('job.state', 'Active job state', active.get('state'), owner.active, changed),
            value('job.phase', 'Active phase', phase, owner.active, changed),
            value('job.transfers_done', 'Completed recipe transfers', repeat.get('completed_transfers'), owner.active, changed),
            value('job.execution_uncertain', 'Execution uncertain', owner.execution_uncertain),
            value('mcu.available', 'MCU available', mcu_cap['available']),
            value('mcu.pending', 'Pending MCU request', mcu_cap['pending'], mcu_cap['pending']),
            value('mcu.reply', 'Last MCU protocol reply', reply.get('rx_text'), mcu_reply.get('id'),
                  reply.get('observed_at'), historical=True),
            value('mcu.command_outcome', 'MCU command outcome', mcu_outcome.get('status'), mcu_outcome.get('job_id'),
                  mcu_outcome.get('observed_at') or owner.jobs.get(mcu_outcome.get('job_id'), {}).get('finished')),
            value('tool_power.available', 'Tool power readback available', power_cap['read_available']),
            value('tool_power.controller_tool_output_voltage', 'Last controller tool-port readback',
                  readback.get('controller_tool_output_voltage'), power_read.get('id'), readback.get('observed_at'),
                  unit='V', historical=True),
            value('tool_power.readback_outcome', 'Tool-port readback outcome', power_outcome.get('status'),
                  power_outcome.get('job_id'),
                  power_outcome.get('observed_at') or owner.jobs.get(power_outcome.get('job_id'), {}).get('finished')),
            value('calibration.selected_calibration', 'Last calibration artifact used or fitted', artifact.get('path'),
                  fitted.get('id'), fitted.get('finished'), historical=True),
            value('calibration.calibration_sha256', 'Calibration artifact SHA-256', artifact.get('calibration_sha256'),
                  fitted.get('id'), fitted.get('finished'), historical=True),
            value('calibration.status', 'Calibration job state', calibration.get('state'), calibration.get('id'),
                  calibration.get('finished')),
            value('calibration.snapshot_id', 'Last Calibration snapshot', camera.get('id'), camera_job.get('id'),
                  camera.get('acquired_at'), historical=True, row_id=camera.get('id')),
            value('teaching.selected_project', 'Last Teaching project', teacher.get('project'),
                  teaching.get('id'), teaching.get('finished'), historical=True),
            value('teaching.capture_id', 'Teaching reference capture', reference.get('id'), teaching.get('id'),
                  reference.get('acquired_at'), historical=True, row_id=reference.get('id')),
            value('teaching.taught', 'Teaching project taught', (teacher.get('flow') or {}).get('taught'),
                  teaching.get('id'), teaching.get('finished'), historical=True),
            value('teaching.tank', 'Teaching project tank', teacher.get('tank'),
                  teaching.get('id'), teaching.get('finished'), historical=True),
            value('teaching.snapshot_id', 'Last Teaching snapshot', snapshot.get('id'), teaching.get('id'),
                  snapshot.get('acquired_at'), historical=True, row_id=snapshot.get('id')),
        )


def server_state(server):
    sample = (getattr(server, 'executor_link', None) or server.agv).state_sample()
    scene = getattr(server, 'scene', None)
    owner = getattr(server, 'owner', None)
    variables = tuple(scene.variables(server.owner, server)) if scene else ()
    if owner is not None:
        variables += operation_variables(owner)
    line = getattr(server, 'line', None)
    if line is None:
        return project(sample, variables, agv.monotonic())
    snapshot = line.snapshot()
    if line.mode != 'disabled':
        variables = (item for item in variables if not item['id'].startswith('line.'))
    return dict(project(sample, (*variables, *snapshot['variables']), agv.monotonic()), line=snapshot['line'])
