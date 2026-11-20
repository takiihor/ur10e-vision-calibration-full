"""One process owns accepted jobs and operator replies, independent of HTTP clients."""
from contextlib import nullcontext
import hashlib
import json
import os
from pathlib import Path
import re
import threading
import tempfile
import time

ID = re.compile(r'^[A-Za-z0-9_-]{8,80}$')
CALIBRATION_STOP_ACTIONS = ('manual', 'collect', 'replay')


class JobStop:
    def __init__(self, kind, job_id):
        self.kind, self.job_id = kind, job_id
        self.requested = threading.Event()
        self.lock = threading.Lock()


def atomic_json(path, value):
    path = Path(path)
    fd, temp = tempfile.mkstemp(prefix='.job-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            stream.write(json.dumps(value, allow_nan=False, separators=(',', ':')))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


class JobOwner:
    record_event = None

    def __init__(self, root, *, allow_auto_calibration=False, robot_model=None, allow_teacher_motion=False, allow_manual_pick_place=False,
                 allow_agv_in_recipe=False, robot_read_access=False):
        self.directory = Path(root) / '.jobs'
        if self.directory.is_symlink():
            raise ValueError('Job directory must not be a symbolic link.')
        self.directory.mkdir(parents=True, exist_ok=True)
        self.root = Path(root).resolve()
        self.allow_auto_calibration = allow_auto_calibration
        self.allow_teacher_motion = allow_teacher_motion
        self.allow_manual_pick_place = allow_manual_pick_place
        self.allow_agv_in_recipe = allow_agv_in_recipe
        self.robot_configured = (robot_read_access or allow_auto_calibration
                                 or allow_teacher_motion or allow_manual_pick_place)
        self.agv = None
        self.agv_visit = None
        self.agv_start_lock = threading.Lock()
        self.execution_uncertain = False
        self.robot_model = robot_model
        self.current_alignment = None  # Runtime-only; restart never restores permission to descend.
        self.motion_job = None
        self.lock = threading.RLock()
        self.jobs = {}
        self.active = None
        self.line_test_fence = False
        self.mcu_pending = None
        self.runner = None
        self.storage_error = None
        self.closing = False
        self.teacher_service = None
        self.active_stop = None
        self.teacher_job = self.teacher_project = self.teacher_settings = None
        for path in self.directory.glob('*.json'):
            try:
                if path.is_symlink():
                    continue
                job = json.loads(path.read_text())
                if not ID.fullmatch(job['id']) or path.stem != job['id']:
                    continue
                if job['state'] in ('running', 'waiting'):
                    job.update(state='interrupted', pending=None, error='Service restarted. Review the saved artifacts.')
                    if job.get('calibration_motion_approved'):
                        job['execution_uncertain'] = True
                    if job.get('manual'):
                        manual = job['manual']
                        manual.update(stage='interrupted',prompt=None,item_state='unknown')
                        if manual.get('agv_transport'):
                            from .recipe_agv import ALARM
                            manual.update(agv_alarm=ALARM, agv_stop_sent=False)
                            manual['repeat']['abort_reason'] = job['error']
                            job['error'] += ' ' + ALARM
                    atomic_json(path, job)
                self.jobs[job['id']] = job
                self.execution_uncertain |= bool(job.get('execution_uncertain'))
            except (ValueError, KeyError, OSError, TypeError):
                continue

        for path in list(self.directory.glob('*.teacher-path.json')) + list(self.directory.glob('*.manual-path.json')):
            try:
                outcome = json.loads(path.read_text()).get('outcome')
                self.execution_uncertain |= outcome in ('dispatching', 'execution_uncertain')
            except (OSError, ValueError, TypeError):
                self.execution_uncertain = True

    def _save(self, job):
        atomic_json(self.directory / (job['id'] + '.json'), job)

    def drain_workers(self, job_id, workers):
        workers = [worker for worker in workers if worker and worker.is_alive()]
        if not workers:
            return
        with self.lock:
            self.execution_uncertain = True
            self.jobs[job_id]['execution_uncertain'] = True
            try:
                self._save(self.jobs[job_id])
            except Exception as exc:
                self.storage_error = str(exc)
        # A timed-out SDK worker retains ownership until it actually stops.
        for worker in workers:
            worker.join()

    def cancel_unconfirmed(self, request_id):
        if not isinstance(request_id, str) or not ID.fullmatch(request_id):
            raise ValueError('Invalid request ID.')
        with self.lock:
            old = self.jobs.get(request_id)
            if old is not None:
                if old['module'] not in ('manual-pick-place', 'robot-control'):
                    raise ValueError('Choose a Pick/place or robot-step request ID.')
                return self.get(request_id)
            now = time.time()
            job = {'id': request_id, 'module': 'manual-pick-place', 'fingerprint': None, 'state': 'interrupted',
                   'created': now, 'finished': now, 'pending': None, 'result': None, 'events': [],
                   'cancelled_before_acceptance': True, 'error': 'Request cancelled before acceptance.'}
            self._save(job)
            self.jobs[request_id] = job
            return dict(job)

    def submit(self, request_id, module, payload, function):
        if not isinstance(request_id, str) or not ID.fullmatch(request_id):
            raise ValueError('Request ID must contain 8 to 80 letters, numbers, underscores or hyphens.')
        fingerprint = hashlib.sha256(json.dumps([module, payload], sort_keys=True, separators=(',', ':'),
                                                  allow_nan=False).encode()).hexdigest()
        with self.lock:
            if self.closing:
                raise ValueError('The service is closing.')
            old = self.jobs.get(request_id)
            if old:
                if old.get('cancelled_before_acceptance'):
                    raise ValueError('Request ID was cancelled before acceptance.')
                if old['fingerprint'] != fingerprint:
                    raise ValueError('Request ID was already used for a different request.')
                return dict(old)
            if self.line_test_fence:
                raise ValueError('A line test owns the service. Wait for confirmed restore.')
            if self.mcu_pending:
                raise ValueError('An MCU request is still pending. Wait for its outcome.')
            if self.storage_error:
                raise ValueError('Job storage failed. Restart only after checking the IPC data directory.')
            if self.execution_uncertain and payload.get('action') not in ('simulate', 'configure', 'review_validation', 'solve', 'calibrate', 'intrinsics', 'verify'):
                raise ValueError('Device execution is uncertain. Physical reconciliation is required before another hardware job.')
            if self.active:
                raise ValueError('Another job owns the service. Wait for it to finish.')
            scene = None
            teacher_move = module == 'teaching' and payload.get('action') in ('validate_move', 'return_observation')
            manual_move = ((module == 'manual-pick-place' and payload.get('action') in ('PICK','PLACE','REPEAT'))
                           or (module == 'robot-control' and payload.get('action') == 'STEP')
                           or (module == 'teaching' and payload.get('action') == 'vertical_descent'))
            if manual_move and (not self.allow_manual_pick_place or not self.robot_model):
                raise ValueError('Manual movement requires reviewed --allow-manual-pick-place and a factory robot model.')
            if teacher_move and not self.allow_teacher_motion:
                raise ValueError('Start the service with --allow-teacher-motion after review.')
            if teacher_move and not self.robot_model:
                raise ValueError('Teacher movement requires the selected factory robot model.')
            auto_motion = module == 'calibration' and payload.get('action') in ('collect', 'replay')
            auto_plan = module == 'calibration' and payload.get('action') == 'plan'
            if (auto_motion or auto_plan) and not self.allow_auto_calibration:
                raise ValueError('Automatic calibration is disabled on this service.')
            actuator = module == 'mcu' and payload.get('command') in ('1', '2', '3')
            if manual_move or teacher_move or auto_motion or actuator:
                from .recipe_agv import admit, StationVisit
                repeat = module == 'manual-pick-place' and payload.get('action') == 'REPEAT'
                mode, connection_id, stop_generation = admit(self.agv, payload, require_approval=repeat)
                visit = StationVisit(self.agv, lambda: self._stop_for_agv(request_id),
                                     strict_station=repeat, connection_id=connection_id,
                                     stop_generation=stop_generation, switched_off=mode == 'off')
            else:
                mode, visit = None, None
            if manual_move or teacher_move or auto_motion or auto_plan:
                from .auto_scene import prepare
                scene = prepare(self.root, request_id, payload.get('collision_scene'), self.robot_model) if self.robot_model else prepare(self.root, request_id, payload.get('collision_scene'))
            job = {'id': request_id, 'module': module, 'fingerprint': fingerprint,
                   'state': 'running', 'created': time.time(), 'events': [], 'pending': None,
                   'result': None, 'error': None}
            if module == 'calibration':
                job['calibration_action'] = payload.get('action')
            if mode:
                job['agv_motion'] = mode
            if scene:
                job['auto_scene'] = scene
                job['auto_action'] = payload['action']
            try:
                self._save(job)
            except BaseException:
                if scene:
                    Path(scene['snapshot']).unlink()
                    if scene.get('model'):
                        Path(scene['model']['snapshot']).unlink()
                raise
            if ((scene and payload.get('action') != 'vertical_descent') or
                    (module == 'teaching' and payload.get('action') in ('configure','capture','teach','locate','capture_validation'))):
                self.current_alignment = None
            self.jobs[request_id] = job
            self.active = request_id
            with self.agv_start_lock:
                if teacher_move:
                    self.active_stop = JobStop('teacher', request_id)
                elif job.get('calibration_action') in CALIBRATION_STOP_ACTIONS:
                    self.active_stop = JobStop('calibration', request_id)
            self.agv_visit = visit
            if self.record_event is not None:
                self.record_event('job', 'accepted', job_id=request_id, request_id=request_id,
                                  module=module, state=job['state'])
            threading.Thread(target=self._run, args=(job, function), daemon=True).start()
            return dict(job)

    def check_agv(self):
        visit = self.agv_visit
        if visit and visit.error:
            raise ValueError(visit.error)

    def _stop_for_agv(self, job_id):
        # Stop must bypass owner persistence, even if a job record is being written.
        with self.agv_start_lock:
            if self.motion_job == job_id and self.runner:
                self.runner.stop()
            elif self.teacher_job == job_id and self.teacher_service:
                self.teacher_service.stop()

    def _request_stop(self, handle):
        with handle.lock:
            if self.active_stop is not handle:
                return False
            handle.requested.set()
        # The latch belongs to this job even if handoff or retirement is in progress.
        with self.agv_start_lock:
            if self.active_stop is handle:
                if handle.kind == 'calibration' and self.runner:
                    self.runner.stop()
                elif handle.kind == 'teacher' and self.teacher_job == handle.job_id and self.teacher_service:
                    self.teacher_service.stop()
        return True

    def _retire_stop(self, job):
        with self.agv_start_lock:
            handle = self.active_stop
            if handle and handle.job_id == job['id']:
                with handle.lock:
                    self.active_stop = None
                    if handle.kind == 'calibration' and handle.requested.is_set():
                        job['stop_requested'] = True

    def _run(self, job, function):
        visit = self.agv_visit
        try:
            with visit.watch() if visit else nullcontext():
                if visit:
                    visit.ready.wait()
                    self.check_agv()
                result = function(job['id'])
                self.check_agv()
            self.check_agv()
            with self.lock:
                self._retire_stop(job)
                if job.get('stop_requested'):
                    raise ValueError('Calibration Stop requested.')
                job.update(state='done', result=result, pending=None)
                if job.get('auto_action') == 'validate_move':
                    project = (result or {}).get('project') or {}
                    capture = project.get('validation') or {}
                    if capture.get('status') == 'completed' and capture.get('validation_mode') == 'fixed_z':
                        self.current_alignment = {'job_id':job['id'],'capture_id':capture['id'],
                            'grasp_sha256':capture['grasp_sha256'],'project':str(project['project'])}
        except Exception as exc:
            with self.lock:
                self._retire_stop(job)
                job.update(state='failed', error=str(exc), pending=None)
                if job.get('stop_requested') and not job.get('execution_uncertain') and not (visit and visit.error):
                    job.update(state='interrupted', error=f'Calibration ended after Stop was requested. {exc} Review the saved artifacts and check the machine.')
                if visit and visit.error:
                    manual = job.get('manual')
                    repeat = (manual or {}).get('repeat')
                    reason = (repeat or {}).get('abort_reason') or visit.error
                    job['error'] = ' '.join(filter(None, (job['error'] if job.get('execution_uncertain') else None,
                                                          reason, visit.alarm)))
                    job['agv_interruption'] = dict(reason=reason, alarm=visit.alarm, agv_stop_sent=visit.stop_sent,
                                                   stop_error=visit.stop_error, robot_stop_error=visit.robot_stop_error)
                    if manual:
                        manual.update(stage='interrupted', prompt=None, item_state='unknown',
                                      agv_alarm=visit.alarm, agv_stop_sent=visit.stop_sent)
                        if repeat:
                            repeat.setdefault('abort_reason', reason)
        finally:
            with self.lock:
                self._retire_stop(job)
                job['finished'] = time.time()
                saved = False
                try:
                    self._save(job)
                    saved = True
                except (OSError, TypeError, ValueError) as exc:
                    self.storage_error = str(exc)
                if self.record_event is not None:
                    self.record_event('job', 'outcome', job_id=job['id'], request_id=job['id'],
                                      module=job['module'], state=job['state'], audit_saved=saved)
                with self.agv_start_lock:
                    self.runner = None
                    self.motion_job = None
                self.agv_visit = None
                if job['module'] == 'mcu' and self.mcu_pending == job['id']:
                    self.mcu_pending = None
                self.active = None

    def event(self, job_id, event):
        with self.lock:
            job = self.jobs[job_id]
            job['events'].append(event)
            # Keep the browser response bounded; final artifacts remain in their session.
            job['events'] = job['events'][-200:]
            if event.get('type') == 'request' and not self.stop_requested(job_id):
                job.update(state='waiting', pending=event)
            self._save(job)

    def stop_requested(self, job_id):
        handle = self.active_stop
        return bool(handle and handle.job_id == job_id and handle.requested.is_set())

    def stop_calibration(self, job_id):
        handle = self.active_stop
        if not handle or handle.kind != 'calibration' or handle.job_id != job_id or not self._request_stop(handle):
            raise ValueError('This calibration capture is no longer active.')
        return {'job_id': job_id, 'stop_requested': True}

    def stop_teacher(self, job_id):
        handle = self.active_stop
        if handle and handle.kind == 'teacher':
            if handle.job_id != job_id or not self._request_stop(handle):
                raise ValueError('This Teacher control belongs to an old session.')
            return
        with self.agv_start_lock:
            if (self.active_stop and self.active_stop.kind == 'teacher') or not self.teacher_service or self.teacher_job != job_id:
                raise ValueError('This Teacher control belongs to an old session.')
            self.teacher_service.stop()

    def answer(self, job_id, request_id, value):
        with self.lock:
            job = self.jobs.get(job_id)
            if not job or self.active != job_id or not self.runner or not job['pending'] or self.stop_requested(job_id):
                raise ValueError('This operator request is no longer active.')
            if type(request_id) is not int or job['pending'].get('id') != request_id:
                raise ValueError('This operator request is stale.')
            kind = job['pending'].get('kind')
            if kind not in ('motion', 'manual') or (kind == 'motion' and type(value) is not bool) or (kind == 'manual' and value not in ('', 'q')):
                raise ValueError('Invalid operator answer.')
            if kind == 'manual' and job.get('auto_action') and value != 'q':
                raise ValueError('Automatic calibration cannot accept manual capture replies.')
            approves_motion = kind == 'motion' and value is True
            if approves_motion:
                self.check_agv()
                if not self.allow_auto_calibration or job['module'] != 'calibration' or self.motion_job != job_id or not job.get('auto_scene'):
                    raise ValueError('Robot motion is disabled for this job.')
                # A restart after approval may have lost the worker's cleanup report.
                job['calibration_motion_approved'] = True
                self._save(job)
                from .auto_scene import check
                try:
                    check(self.root, job['auto_scene'])
                except ValueError:
                    with self.agv_start_lock:
                        self.runner.stop()
                    raise
            with self.agv_start_lock:
                if self.stop_requested(job_id):
                    raise ValueError('This operator request is no longer active.')
                if approves_motion:
                    self.check_agv()
                self.runner.answer(request_id, value)
            job.update(state='running', pending=None)
            self._save(job)
            return dict(job)

    def shutdown(self):
        # Do not release ownership until every accepted worker has cleaned up.
        with self.lock:
            self.closing = True
        while True:
            with self.lock:
                if not self.active and not self.mcu_pending:
                    service = self.teacher_service
                    if service:
                        service.close()
                        self.teacher_service = None
                    return
                if self.active and self.teacher_service and self.jobs[self.active]['module'] == 'teaching':
                    self.teacher_service.stop()
                runner = self.runner
                if runner:
                    try:
                        runner.stop()
                    except OSError:
                        pass
            time.sleep(.05)

    def get(self, job_id):
        with self.lock:
            if job_id not in self.jobs:
                raise ValueError('Job not found.')
            job = json.loads(json.dumps(self.jobs[job_id], allow_nan=False))
            if job.get('module') == 'calibration' and self.stop_requested(job_id):
                job.update(stop_requested=True, pending=None, state='running')
            return job

    def status(self):
        with self.lock:
            stop_targets = []
            handle = self.active_stop
            teacher_id = handle.job_id if handle and handle.kind == 'teacher' else self.teacher_job
            if teacher_id is not None:
                stop_targets.append({'kind': 'teacher', 'job_id': teacher_id})
            manual = self.jobs.get(self.active, {})
            if manual.get('auto_action') in ('PICK', 'PLACE', 'REPEAT', 'vertical_descent', 'STEP'):
                stop_targets.append({'kind': 'manual', 'job_id': self.active})
            if handle and handle.kind == 'calibration':
                stop_targets.append({'kind': 'calibration', 'job_id': handle.job_id})
            return {'active': self.active, 'stop_targets': stop_targets, 'mcu_pending': self.mcu_pending, 'storage_error': self.storage_error, 'closing': self.closing,
                    'execution_uncertain': self.execution_uncertain,
                    'recovery': 'Review physical state, native worker cleanup and saved job evidence before a reviewed service restart; no automatic motion replay or clear button.' if self.execution_uncertain else None, 'jobs': [
                {k: v for k, v in job.items() if k in ('id', 'module', 'state', 'created', 'finished', 'error', 'calibration_action')}
                for job in sorted(self.jobs.values(), key=lambda j: j['created'], reverse=True)[:100]]}
