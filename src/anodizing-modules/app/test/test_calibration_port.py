"""Calibration cancellation and output identity, using offline workers and data."""
import json
from pathlib import Path
from threading import Event, Thread
import time

import pytest

from anodizing_web import calibration, teaching
from anodizing_web.jobs import JobOwner


def test_stop_before_calibration_worker_handoff(tmp_path):
    owner = JobOwner(tmp_path)
    entered, release = Event(), Event()
    job_id = 'calibration-manual'

    def dispatch(ident):
        entered.set()
        assert release.wait(3)
        calibration.check_stop(owner, ident)
        pytest.fail('A stopped capture must not start.')

    owner.submit(job_id, 'calibration', {'action': 'manual'}, dispatch)
    try:
        assert entered.wait(3)
        assert owner.status()['stop_targets'] == [{'kind': 'calibration', 'job_id': job_id}]
        with pytest.raises(ValueError, match='active'):
            owner.stop_calibration('an-old-job')
        assert owner.stop_calibration(job_id)['stop_requested']
        assert owner.stop_calibration(job_id)['stop_requested']
        assert owner.get(job_id)['stop_requested'] and owner.get(job_id)['pending'] is None
        with pytest.raises(ValueError, match='no longer active|stopping'):
            owner.answer(job_id, 1, '')
        with pytest.raises(ValueError, match='owns the service'):
            owner.submit('other-capture', 'calibration', {'action': 'manual'}, dispatch)
    finally:
        release.set()
        deadline = time.monotonic() + 3
        while owner.status()['active'] and time.monotonic() < deadline:
            time.sleep(.01)
    assert owner.get(job_id)['state'] == 'interrupted'
    restarted = JobOwner(tmp_path)
    assert restarted.get(job_id)['stop_requested']
    assert not restarted.execution_uncertain
    with pytest.raises(ValueError, match='active'):
        owner.stop_calibration(job_id)


@pytest.mark.parametrize('target', ['calibration', 'teacher'])
def test_stop_reaches_worker_while_event_save_is_stalled(tmp_path, target, monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import Mock

    owner = JobOwner(tmp_path, allow_teacher_motion=True, robot_model={'test': True})
    blocked, release, stopped = Event(), Event(), Event()
    save = owner._save

    def stall(job):
        if job['events'] and 'finished' not in job:
            blocked.set()
            assert release.wait(5)
        save(job)

    worker = SimpleNamespace(stop=Mock(side_effect=stopped.set))
    job_id = 'active-' + target

    def run(ident):
        if target == 'teacher':
            owner.teacher_service, owner.teacher_job = worker, ident
        else:
            owner.runner = worker
        owner.event(ident, {'type': 'log', 'message': 'Writing evidence'})

    monkeypatch.setattr(owner, '_save', stall)
    if target == 'teacher':
        monkeypatch.setattr('anodizing_web.auto_scene.prepare', lambda *args: None)
        owner.submit(job_id, 'teaching', {'action': 'validate_move', 'agv_off': True}, run)
        stop = lambda ident: teaching.control(owner, ident, 'stop')
    else:
        owner.submit(job_id, 'calibration', {'action': 'manual'}, run)
        stop = owner.stop_calibration

    errors = []

    def request_stop():
        try:
            stop(job_id)
        except Exception as exc:
            errors.append(exc)

    thread = Thread(target=request_stop, daemon=True)
    try:
        assert blocked.wait(2)
        thread.start()
        assert stopped.wait(.25), 'The native Stop was queued behind the evidence write'
    finally:
        release.set()
        if thread.ident is not None:
            thread.join(timeout=2)
    assert not thread.is_alive()
    assert not errors
    worker.stop.assert_called_once()


@pytest.mark.parametrize(('approved', 'cleanup_errors', 'expected_state'), [
    (False, [], 'interrupted'), (True, [], 'interrupted'), (True, ['native halt failed'], 'failed'), (True, None, 'failed'),
])
def test_automatic_stop_records_the_native_cleanup_outcome(approved, cleanup_errors, expected_state, monkeypatch):
    from test_auto_bridge import AutoBridgeTests, Runner, collision, ui_jobs

    class CleanupRunner(Runner):
        def cleanup(self):
            if cleanup_errors is not None:
                yield {'type': 'calibration_cleanup', 'errors': cleanup_errors}

    harness = AutoBridgeTests()
    harness.setUp()
    owner = JobOwner(harness.root, allow_auto_calibration=True)
    job_id = 'automatic-capture'
    monkeypatch.setattr(collision.Scene, 'require_motion_ready', lambda _: None)
    monkeypatch.setattr(ui_jobs, 'JobRunner', CleanupRunner)
    try:
        harness.submit(owner, job_id, harness.body)
        harness.wait(owner, job_id, 'waiting')
        if approved:
            owner.answer(job_id, 1, True)
            harness.wait(owner, job_id, 'waiting')
        owner.stop_calibration(job_id)
        deadline = time.monotonic() + 3
        while owner.status()['active'] and time.monotonic() < deadline:
            time.sleep(.01)
        saved = JobOwner(harness.root)
        assert saved.get(job_id)['state'] == expected_state
        assert saved.execution_uncertain == (expected_state == 'failed')
        if saved.execution_uncertain:
            assert (cleanup_errors or ['without confirming cleanup'])[0] in saved.get(job_id)['error']
            with pytest.raises(ValueError, match='Physical reconciliation'):
                saved.submit('different-capture', 'calibration', {'action': 'manual'}, lambda _: None)
    finally:
        owner.shutdown()
        harness.doCleanups()


def test_calibration_stop_prevents_motion_reply_during_approval_save(monkeypatch):
    from test_auto_bridge import AutoBridgeTests, Runner, collision, ui_jobs

    harness = AutoBridgeTests()
    harness.setUp()
    owner = JobOwner(harness.root, allow_auto_calibration=True)
    job_id = 'automatic-capture'
    blocked, release = Event(), Event()
    save = owner._save
    stopped_reply_errors = []

    def stall_approval(job):
        if job.get('calibration_motion_approved') and not blocked.is_set():
            blocked.set()
            assert release.wait(5)
        save(job)

    def approve():
        try:
            owner.answer(job_id, 1, True)
        except ValueError as exc:
            stopped_reply_errors.append(str(exc))

    monkeypatch.setattr(collision.Scene, 'require_motion_ready', lambda _: None)
    monkeypatch.setattr(ui_jobs, 'JobRunner', Runner)
    monkeypatch.setattr(owner, '_save', stall_approval)
    approval = Thread(target=approve, daemon=True)
    try:
        harness.submit(owner, job_id, harness.body)
        harness.wait(owner, job_id, 'waiting')
        approval.start()
        assert blocked.wait(2)
        owner.stop_calibration(job_id)
        assert Runner.instances[-1].stopped
        assert not Runner.instances[-1].answers
    finally:
        release.set()
        if approval.ident is not None:
            approval.join(timeout=2)
        owner.shutdown()
        harness.doCleanups()
    assert stopped_reply_errors == ['This operator request is no longer active.']
    assert not Runner.instances[-1].answers


def test_verify_rejects_recorded_fit_session_independent_of_setup(tmp_path):
    actual_fit = tmp_path / 'actual-fit'
    actual_fit.mkdir()
    (actual_fit / 'session.json').write_text('{}')
    (actual_fit / 'samples.jsonl').write_text('')
    (tmp_path / 'unrelated').mkdir()
    (tmp_path / 'board.json').write_text('{}')
    fixture = Path(__file__).parent / 'fixtures' / 'simulated_calibration.json'
    artifact = json.loads(fixture.read_text())
    artifact.update(session=str(actual_fit), board_in_base={'matrix': [[int(i == j) for j in range(4)] for i in range(4)]})
    path = tmp_path / 'fit.json'
    path.write_text(json.dumps(artifact))
    body = {'action': 'verify', 'session': 'unrelated', 'verification': actual_fit.name,
            'calibration': path.name, 'manifest': 'board.json'}
    with pytest.raises(ValueError, match='fresh verification.*fitting session'):
        calibration.run(JobOwner(tmp_path), tmp_path, body, 'verify-fit', False)
    assert not list(actual_fit.glob('verification-*.json'))


def test_handoff_checks_selected_result_and_its_bytes(tmp_path):
    from ur_handeye.dataset import load_result

    path = tmp_path / 'calibration.json'
    fixture = Path(__file__).parent / 'fixtures' / 'simulated_calibration.json'
    source = json.loads(fixture.read_text())
    source['verdict'] = 'acceptable'
    source['board_in_base'] = {'matrix': [[int(i == j) for j in range(4)] for i in range(4)]}
    path.write_text(json.dumps(source))
    saved = {'id': 'calibration-fit', 'module': 'calibration', 'state': 'done',
             'result': {'path': str(path), 'result': load_result(path),
                        'calibration_sha256': calibration.artifact_sha256(path)}}
    handoff = calibration.teaching_handoff(tmp_path, saved, path.name)
    assert handoff['simulation'] and handoff['calibration'] == str(path)
    assert handoff['calibration_sha256'] == saved['result']['calibration_sha256']
    with pytest.raises(ValueError, match='selected'):
        calibration.teaching_handoff(tmp_path, saved, 'different.json')
    path.write_text(json.dumps(source, indent=2))
    with pytest.raises(ValueError, match='changed'):
        calibration.teaching_handoff(tmp_path, saved, path.name)
