"""Phase 1 executor fault checks with fake hardware only."""
import json
import multiprocessing
import os
from threading import Event, Thread

import pytest

from anodizing_web.executor import AdmissionRefused, Executor, FakeDevices
from anodizing_web.executor_http import _validate_job

from test_executor_phase1 import FAKE_TANK_SETPOINTS, session, terminal, visit, wait_for
from test_executor_r3_ipc import dip, lease, plant


def test_refusal_is_available_by_id_after_restart(tmp_path):
    executor = Executor(tmp_path, FakeDevices())
    try:
        request = visit(session(executor))
        executor.devices.reserve_s = 0
        with pytest.raises(AdmissionRefused) as refused:
            executor.submit(request)
    finally:
        executor.close()
    restarted = Executor(tmp_path, FakeDevices())
    try:
        job = restarted.status(request['job_id'])['job']
        assert job is not None
        assert job['state'] == 'refused'
        assert job['error'] == str(refused.value)
        assert restarted.devices.effects == []
    finally:
        restarted.close()


def test_deleted_refusal_inventory_fences_after_restart(tmp_path):
    executor = Executor(tmp_path, FakeDevices())
    try:
        request = visit(session(executor))
        executor.devices.reserve_s = 0
        with pytest.raises(AdmissionRefused):
            executor.submit(request)
    finally:
        executor.close()
    refusal = tmp_path / '.executor-refused.json'
    refusal.unlink()
    restarted = Executor(tmp_path, FakeDevices())
    try:
        status = restarted.status()
        assert status['recovery_required']
        assert 'call a technician' in status['evidence_faults'][refusal.name]
        identity = lease(restarted)
        with pytest.raises(ValueError, match='technician'):
            restarted.control('arm', dict(identity, stop_generation=status['stop_generation']))
        assert not refusal.exists()
        assert restarted.devices.effects == []
    finally:
        restarted.close()


@pytest.mark.parametrize('damage', ['missing', 'malformed-result', 'contradictory-result'])
def test_damaged_accepted_job_stays_untouched_and_fenced(tmp_path, damage):
    executor = Executor(tmp_path, FakeDevices(setpoints=FAKE_TANK_SETPOINTS))
    try:
        request = dip(session(executor), destination='output')
        plant(executor, request)
        executor.submit(request)
        terminal(executor, request['job_id'])
    finally:
        executor.close()
    ident = request['job_id']
    path = tmp_path / '.jobs' / (ident + '.json')
    if damage == 'missing':
        path.unlink()
        original = None
    else:
        stored = json.loads(path.read_text())
        if damage == 'malformed-result':
            del stored['result']['steps'][0]['plc']
        else:
            stored['result']['steps'][0]['dwell_s'] = 0
        original = json.dumps(stored)
        path.write_text(original)
    restarted = Executor(tmp_path, FakeDevices())
    try:
        status = restarted.status(ident)
        assert status['recovery_required']
        assert 'call a technician' in status['evidence_faults'][str(path.relative_to(tmp_path))]
        identity = lease(restarted)
        with pytest.raises(ValueError, match='technician'):
            restarted.reconcile(dict(identity, job_id=ident, disposition='Observed plant', missing=original is None))
        assert (path.read_text() if path.exists() else None) == original
        assert restarted.devices.effects == []
    finally:
        restarted.close()


def test_lift_that_leaves_basket_immersed_keeps_open_dwell(tmp_path):
    devices = FakeDevices(setpoints=FAKE_TANK_SETPOINTS)
    devices.robot.lift = lambda destination: None
    executor = Executor(tmp_path, devices)
    try:
        request = dip(session(executor), destination='output')
        plant(executor, request)
        executor.submit(request)
        job = terminal(executor, request['job_id'])
        assert job['outcome'] == 'unknown'
        assert job['result']['steps'] == []
        assert job['checkpoint']['confirmed'][-1]['evidence']['location'].startswith('tank')
    finally:
        executor.close()


def test_validated_reply_requires_consistent_measured_dwell_and_plc(tmp_path):
    executor = Executor(tmp_path, FakeDevices(setpoints=FAKE_TANK_SETPOINTS))
    try:
        request = dip(session(executor), destination='output')
        plant(executor, request)
        executor.submit(request)
        job = terminal(executor, request['job_id'])
        _validate_job(job)
        job['result']['steps'][0]['dwell_s'] = 0
        job['result']['steps'][0]['plc'] = {'cycle': 0}
        with pytest.raises(ValueError):
            _validate_job(job)
    finally:
        executor.close()


def test_unaccounted_disposition_cannot_close_when_original_is_now_at_park(tmp_path):
    devices = FakeDevices(setpoints=FAKE_TANK_SETPOINTS)
    executor = Executor(tmp_path, devices)
    try:
        request = dip(session(executor))
        plant(executor, request)
        executor.submit(request)
        done = terminal(executor, request['job_id'])
        assert done['result']['hold']['location'] == 'park'
        with pytest.raises(ValueError, match='ordinary.*unload'):
            executor.reconcile(dict(request, disposition='Basket not accounted for', observed_only=True,
                                    allow_unaccounted=True, observation_id='observation', **{
                                        key: request['params'][key] for key in ('basket', 'run_id')}))
        assert executor.status(request['job_id'])['job']['observation'] is None
    finally:
        executor.close()


def test_stop_latches_and_halts_while_journal_and_admission_locks_are_held(tmp_path):
    devices = FakeDevices()
    halted = Event()
    stop = devices.stop

    def signal_stop():
        result = stop()
        halted.set()
        return result

    devices.stop = signal_stop
    executor = Executor(tmp_path, devices)
    reader = Thread(target=executor.stop, args=({'stop_id': 'urgent-stop'},), daemon=True)
    try:
        session(executor)
        with executor.admission, executor.owner.lock, executor.guard, executor.stop_lock:
            reader.start()
            assert halted.wait(1)
        reader.join(1)
        assert not reader.is_alive()
    finally:
        reader.join(2)
        executor.close()


def test_measured_dwell_breach_alarms_without_blocking_next_job(tmp_path):
    devices = FakeDevices(setpoints=FAKE_TANK_SETPOINTS)
    executor = Executor(tmp_path, devices)
    try:
        request = dip(session(executor))
        plant(executor, request)
        request['params']['steps'][0]['limits']['overstay'] = 0
        executor.submit(request)
        job = terminal(executor, request['job_id'])
        assert job['outcome'] == 'succeeded'
        assert job['result']['steps'][0]['inspection']
        assert executor.status()['quality_alarm']['job_id'] == request['job_id']
        assert executor.status()['armed']
    finally:
        executor.close()


def exit_during_admission(root, phase):
    devices = FakeDevices(state_path=root / '.fake-devices.json')
    executor = Executor(root, devices)
    request = visit(session(executor))
    write_marker = executor._write_marker
    save_job = executor.owner._save
    submit = executor.owner.submit

    def marker(*args, **kwargs):
        write_marker(*args, **kwargs)
        if phase == ('pending-marker' if kwargs.get('committing_id') else 'committed-marker'):
            os._exit(85)

    def job(*args):
        save_job(*args)
        if phase == 'job-file':
            os._exit(85)

    def job_admission(*args):
        if phase == 'acceptance-inventory':
            os._exit(85)
        return submit(*args)

    executor._write_marker = marker
    executor.owner._save = job
    executor.owner.submit = job_admission
    executor.submit(request)
    os._exit(86)


@pytest.mark.parametrize('phase', ['pending-marker', 'acceptance-inventory', 'job-file', 'committed-marker'])
def test_process_exit_at_admission_write_boundaries_has_no_effect(tmp_path, phase):
    child = multiprocessing.get_context('spawn').Process(target=exit_during_admission, args=(tmp_path, phase))
    child.start()
    child.join(8)
    if child.is_alive():
        child.kill()
        child.join()
    assert child.exitcode == 85
    restarted = Executor(tmp_path, FakeDevices(state_path=tmp_path / '.fake-devices.json'))
    try:
        snapshot = restarted.status(visit({})['job_id'])
        committed = phase == 'committed-marker'
        assert snapshot['job']['outcome'] == ('unknown' if committed else 'stopped')
        assert snapshot['recovery_required'] == committed
        assert not snapshot.get('evidence_faults')
        assert restarted.devices.agv.station == 0
        assert restarted.devices.effects == []
    finally:
        restarted.close()


def exit_during_refusal(root):
    import anodizing_web.executor as executor_module

    saved = executor_module.atomic_json
    devices = FakeDevices(state_path=root / '.fake-devices.json')
    executor = Executor(root, devices)
    request = visit(session(executor))
    devices.reserve_s = 0

    def write(path, data, **kwargs):
        saved(path, data, **kwargs)
        if path.name == '.executor-refused-proof.json' and 'previous' in data:
            os._exit(87)

    executor_module.atomic_json = write
    executor.submit(request)
    os._exit(88)


def test_process_exit_after_refusal_proof_preserves_the_original_refusal(tmp_path):
    child = multiprocessing.get_context('spawn').Process(target=exit_during_refusal, args=(tmp_path,))
    child.start()
    child.join(8)
    if child.is_alive():
        child.kill()
        child.join()
    assert child.exitcode == 87
    restarted = Executor(tmp_path, FakeDevices(state_path=tmp_path / '.fake-devices.json'))
    try:
        identity = session(restarted)
        request = visit(identity)
        snapshot = restarted.status(request['job_id'])
        assert not snapshot['recovery_required']
        refused = snapshot['job']
        assert refused['state'] == 'refused' and 'reserve' in refused['error'].lower()
        with pytest.raises(AdmissionRefused, match='reserve'):
            restarted.submit(request)
        assert restarted.devices.effects == []
    finally:
        restarted.close()


def test_active_checkpoint_never_rewrites_externally_damaged_job(tmp_path):
    devices = FakeDevices()
    entered, release = Event(), Event()
    perform = devices.perform

    def paused(effect, value):
        result = perform(effect, value)
        entered.set()
        assert release.wait(3)
        return result

    devices.perform = paused
    executor = Executor(tmp_path, devices)
    try:
        request = visit(session(executor))
        executor.submit(request)
        assert entered.wait(3)
        path = tmp_path / '.jobs' / (request['job_id'] + '.json')
        path.write_text('damaged job bytes')
        release.set()
        wait_for(lambda: executor.owner.active, lambda active: active is None)
        assert path.read_text() == 'damaged job bytes'
        assert executor.status(request['job_id'])['evidence_faults']
    finally:
        release.set()
        executor.close()


def test_wired_halt_is_attempted_during_blocked_station_interlock(tmp_path, monkeypatch):
    import time
    from types import SimpleNamespace
    from anodizing_web import agv
    from anodizing_web.executor_http import WiredDevices

    frames, entered, release, halt = [], Event(), Event(), Event()
    prefix = bytes.fromhex('42 19 0D')
    station_call = agv.Connection.recipe_call
    checking_arm = Event()

    def recipe_call(link, *args, **kwargs):
        checking_arm.set()
        return station_call(link, *args, **kwargs)

    def tank_clear():
        if checking_arm.is_set():
            entered.set()
            assert release.wait(3)
        return time.monotonic()

    class Wire(agv.Simulator):
        def __init__(self, **_):
            super().__init__(prefix)
            self.arrival = True

        def write(self, raw):
            frame = agv.parse_frame(raw)
            frames.append(frame)
            if frame.command == 0x23 and frame.data[0] in (0x87, 0x5B):
                halt.set()
            return super().write(prefix + raw) - len(prefix)

    (tmp_path / 'site-profile.json').write_text(json.dumps({'agv': {'wired_port': '/dev/test-wire'}}))
    monkeypatch.setattr(agv, 'serial', SimpleNamespace(Serial=Wire))
    monkeypatch.setattr(agv, 'TIMED_REPORT_LISTEN_S', 0)
    monkeypatch.setattr(agv, 'RECIPE_POLL_S', .2)
    monkeypatch.setattr(agv.Connection, 'recipe_call', recipe_call)
    devices = WiredDevices(tmp_path, None, pose=time.monotonic, reserve=lambda: True, tank_clear=tank_clear)
    executor = Executor(tmp_path, devices)
    try:
        identity = lease(executor)
        executor.control('status_read', identity)
        request = visit(dict(identity, stop_generation=0))
        executor.control('arm', request)
        executor.submit(request)
        assert entered.wait(3)
        receipt = executor.stop({'stop_id': 'priority-wired-stop'})
        assert receipt['received'] and not receipt['confirmed']
        assert halt.wait(1)
        assert not executor.stop({'stop_id': 'priority-wired-stop'})['confirmed']
        release.set()
        terminal(executor, request['job_id'])
        assert not any(frame.command == 0x23 and frame.data[0] == 0x9D for frame in frames)
    finally:
        release.set()
        executor.close()


def test_invalid_remote_events_reject_one_reply_without_breaking_status(tmp_path):
    from anodizing_web.executor_http import Client, control_listener
    from test_executor_phase1 import running

    executor = Executor(tmp_path, FakeDevices())
    try:
        dispatch = executor.dispatch

        def invalid_status(operation, body):
            status = dispatch(operation, body)
            if operation == 'status' and body.get('job_id') == 'invalid-events':
                status['device']['events'] = None
            return status

        executor.dispatch = invalid_status
        token = 'r5-private-test-executor-token-string'
        listener = control_listener(0, executor, token)
        with running(listener):
            client = Client(listener.server_port, token)
            with pytest.raises(OSError, match='Invalid IPC executor response'):
                client('status', {'job_id': 'invalid-events'})
            assert client('status', {'job_id': 'different-job-id'})['boot'] == executor.boot
    finally:
        executor.close()


def test_stop_during_recovery_observation_prevents_any_new_unload(tmp_path):
    devices = FakeDevices(setpoints=FAKE_TANK_SETPOINTS)
    entered, release = Event(), Event()
    observe = devices.observe_recovery_hold

    def paused_observation():
        hold = observe()
        entered.set()
        assert release.wait(3)
        return hold

    devices.observe_recovery_hold = paused_observation
    executor = Executor(tmp_path, devices)
    errors = []
    worker = None
    try:
        request = dip(session(executor))
        plant(executor, request)
        executor.submit(request)
        terminal(executor, request['job_id'])
        params = request['params']
        intent = dict(request, disposition='Unload original basket', observed_only=True, release_basket=True,
                      basket=params['basket'], run_id=params['run_id'], observation_id='r5-unload-check')

        def reconcile():
            try:
                executor.reconcile(intent)
            except ValueError as exc:
                errors.append(str(exc))

        worker = Thread(target=reconcile, daemon=True)
        worker.start()
        assert entered.wait(3)
        assert executor.stop({'stop_id': 'urgent-unload-stop'})['received']
        release.set()
        worker.join(3)
        assert not worker.is_alive()
        assert errors and 'Stop' in errors[0]
        assert devices.robot.basket == params['basket']
        assert devices.robot.location == 'park'
    finally:
        release.set()
        if worker is not None:
            worker.join(3)
        executor.close()


def test_fresh_stop_evidence_confirms_both_overlapping_receipts(tmp_path):
    devices = FakeDevices()
    executor = Executor(tmp_path, devices)
    evidence_ready = Event()
    evidence = devices.stop_evidence
    devices.stop_evidence = lambda requested: evidence_ready.is_set() and evidence(requested)
    try:
        session(executor)
        earlier = {'stop_id': 'earlier-stop'}
        newer = {'stop_id': 'newer-stop'}
        assert not executor.stop(earlier)['confirmed']
        assert not executor.stop(newer)['confirmed']
        evidence_ready.set()
        first = wait_for(lambda: executor.stop(newer), lambda receipt: receipt['confirmed'])
        retained = executor.stop(earlier)
        assert retained['confirmed']
        assert retained['generation'] < first['generation']
        assert executor.status()['stop']['stop_id'] == newer['stop_id']
    finally:
        executor.close()
