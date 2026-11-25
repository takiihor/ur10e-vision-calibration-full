"""Offline regression tests for controller approval, recovery and Stop authority."""
from concurrent.futures import ThreadPoolExecutor
from http.client import HTTPResponse
from io import BytesIO
from threading import Event
from urllib.error import HTTPError

import pytest

from anodizing_web import executor_http
from anodizing_web.executor import Executor, FakeDevices
from anodizing_web.executor_http import Client
from test_executor_link_review import agv_ready, arm, operator, remote, wait_for
from test_programme_review import steps


def test_delayed_start_does_not_acquire_new_approval_after_stop(tmp_path):
    with remote(tmp_path) as (client, executor, devices), operator(
        tmp_path / 'katana', client, steps('seal')
    ) as call:
        agv_ready(call)
        programme = call('programme')
        status = client('status')
        approval = {key: status[key] for key in ('boot', 'session', 'stop_generation')}
        delayed = dict(action='start', revision=programme['recipe']['id'], basket='delayed', approval=approval)
        call('agv', dict(action='send', command='stop'))
        wait_for(lambda: call('agv'), lambda status: not status['stop_unconfirmed'])
        with pytest.raises(HTTPError, match='400'):
            call('programme', delayed)
        assert call('programme')['run'] is None
        assert devices.effects == []


@pytest.mark.parametrize('wire', [
    b'HTTP/1.1 200 OK\r\nContent-Length: 32\r\n\r\n{"status":',
    b'HTTP/1.1 200 OK\r\nContent-Length: 16\r\n\r\n{"job_types":[]}',
])
def test_client_reports_partial_or_invalid_reply_as_unavailable(monkeypatch, wire):
    class Response:
        def __init__(self, *args, **kwargs):
            pass

        def request(self, *args, **kwargs):
            pass

        def makefile(self, *args):
            return BytesIO(wire)

        def getresponse(self):
            result = HTTPResponse(self)
            result.begin()
            return result

        def close(self):
            pass

    monkeypatch.setattr(executor_http, 'HTTPConnection', Response)
    with pytest.raises(OSError, match='executor'):
        Client(1, 'offline-test')('health')


def test_stop_receipt_precedes_blocked_arm_device_call(tmp_path):
    waiting, release = Event(), Event()

    class Devices(FakeDevices):
        def arm(self):
            waiting.set()
            assert release.wait(5)
            super().arm()

    devices = Devices()
    executor = Executor(tmp_path, devices)
    envelope = dict(protocol=1, session='1' * 32, boot=executor.boot, stop_generation=0)
    executor.heartbeat(dict(envelope, sequence=1))
    executor.control('connect', envelope)
    try:
        with ThreadPoolExecutor() as pool:
            armed = pool.submit(executor.control, 'arm', envelope)
            try:
                assert waiting.wait(1)
                stop = pool.submit(executor.stop)
                assert stop.result(timeout=.5)['generation'] == 1
            finally:
                release.set()
            with pytest.raises(ValueError, match='cancel'):
                armed.result(timeout=1)
        assert not executor.status()['armed']
        assert not devices.armed
    finally:
        executor.close()


def test_commissioning_rejects_start_and_remains_connectable(tmp_path):
    class Commissioning(FakeDevices):
        simulated = False
        job_types = {'station_visit': 1}

    executor = Executor(tmp_path / 'ipc', Commissioning())
    try:
        with operator(tmp_path / 'katana', executor.dispatch, steps('seal')) as call:
            wait_for(lambda: call('agv'), lambda state: state.get('executor_health'))
            programme = call('programme')
            with pytest.raises(HTTPError, match='400'):
                call('programme', dict(action='start', revision=programme['recipe']['id'],
                                       basket='unsupported', approval=programme.get('approval')))
            assert call('programme')['run'] is None
            assert call('agv', dict(action='connect', mode='wired'))['connected']
    finally:
        executor.close()


def test_manual_reconciliation_keeps_unknown_outcome_and_releases_intent(tmp_path):
    with remote(tmp_path) as (client, executor, devices), operator(tmp_path / 'katana', client) as call:
        reviewed = arm(call)
        perform = devices.perform

        def missing_feedback(effect, value):
            perform(effect, value)
            raise ValueError('Lost simulated arrival evidence')

        devices.perform = missing_feedback
        call('agv', dict(reviewed, action='send', command='call_station', value=3))
        state = wait_for(lambda: call('agv'), lambda s: (s['manual_visit']['result'] or {}).get('outcome') == 'unknown')
        job = state['manual_visit']['id']
        state = call('agv', dict(action='reconcile_station', job_id=job, note='Verified simulated station hold'))
        assert state['manual_visit']['result']['outcome'] == 'unknown'
        assert state['manual_visit']['result']['reconciliation']['hold']['station'] == 3
        state = call('agv', dict(action='acknowledge_station', job_id=job))
        assert state['manual_visit'] is None
        assert client('status', dict(job_id=job))['job']['outcome'] == 'unknown'


def test_confirmed_stop_moves_to_history_after_arm(tmp_path):
    with remote(tmp_path) as (client, executor, devices), operator(tmp_path / 'katana', client) as call:
        arm(call)
        call('agv', dict(action='send', command='stop'))
        state = wait_for(lambda: call('agv'), lambda s: not s['stop_unconfirmed'])
        assert state['executor_stop']['confirmed']
        current = call('agv', dict(action='arm', armed=True, executor_boot=state['executor_boot'],
                                   executor_session=state['executor_session'],
                                   stop_generation=state['stop_generation']))
        assert current['armed']
        assert current['executor_stop'] is None
        assert current['executor_stop_history']['confirmed']


def test_partial_reply_does_not_retire_heartbeat_or_dispatch_workers(tmp_path, monkeypatch):
    from threading import current_thread

    from test_executor_link_review import running
    from anodizing_web.server import Server

    cut_heartbeat, cut_submit = Event(), Event()
    truncated = Event()
    connection = executor_http.HTTPConnection

    class DropReply(connection):
        def request(self, method, path, *args, **kwargs):
            self.path = path
            super().request(method, path, *args, **kwargs)

        def getresponse(self):
            reply = super().getresponse()
            name = current_thread().name
            drop_heartbeat = name == 'agv-executor-heartbeat' and self.path == '/v1/heartbeat'
            drop_submit = name == 'recipe-dispatch' and self.path == '/v1/submit'
            if (drop_heartbeat and cut_heartbeat.is_set()) or (drop_submit and cut_submit.is_set()):
                (cut_heartbeat if drop_heartbeat else cut_submit).clear()
                reply.close()

                class Truncated:
                    def makefile(self, *args):
                        return BytesIO(b'HTTP/1.1 200 OK\r\nContent-Length: 80\r\n\r\n{"job":')

                result = HTTPResponse(Truncated())
                result.begin()
                if drop_submit:
                    truncated.set()
                return result
            return reply

    with remote(tmp_path) as (client, executor, devices):
        monkeypatch.setattr(executor_http, 'HTTPConnection', DropReply)
        server = Server(0, tmp_path / 'katana', executor_client=client, programme_steps=steps('seal'))
        programme = server.programme
        with running(server):
            wait_for(server.executor_link.status, lambda status: status.get('executor_health'))
            cut_heartbeat.set()
            wait_for(lambda: server.executor_link.status().get('error') or not server.executor_link.worker.is_alive())
            assert server.executor_link.worker.is_alive()
            wait_for(lambda: programme.review().get('approval'))
            cut_submit.set()
            approval = programme.review()['approval']
            recipe = programme.status()['recipe']['id']
            programme.start(recipe, 'partial-reply', approval)
            assert truncated.wait(3)
            run = wait_for(lambda: programme.status()['run'], lambda r: r['jobs'] and not r['ack_pending'])
            assert programme.worker.is_alive()
            assert run['state'] == 'held'
            assert [value for effect, value in devices.effects if effect == 'station'] == [1]
            assert executor.status(run['jobs'][0]['job']['job_id'])['job']['acknowledged']


def test_missing_manual_visit_cannot_reuse_new_stop_generation(tmp_path):
    with remote(tmp_path) as (client, executor, devices):
        block = True

        def transport(operation, body=None):
            if block and operation == 'submit':
                raise OSError('Request lost before the IPC received it')
            return client(operation, body)

        with operator(tmp_path / 'katana', transport) as call:
            reviewed = arm(call)
            with pytest.raises(HTTPError, match='400'):
                call('agv', dict(reviewed, action='send', command='call_station', value=4))
            original = call('agv')['manual_visit']['id']
            call('agv', dict(action='send', command='stop'))
            state = wait_for(lambda: call('agv'), lambda s: not s['stop_unconfirmed'])
            fresh = dict(executor_boot=state['executor_boot'], executor_session=state.get('executor_session'),
                         stop_generation=state['stop_generation'])
            call('agv', dict(fresh, action='arm', armed=True))
            block = False
            with pytest.raises(HTTPError, match='400'):
                call('agv', dict(fresh, action='send', command='call_station', value=4))
            assert executor.status(original)['job'] is None
            result = call('agv', dict(action='reconcile_station', job_id=original,
                                     note='Verified simulated stationary hold.'))
            assert result['manual_visit']['result']['outcome'] == 'unknown'
            call('agv', dict(action='acknowledge_station', job_id=original))
            assert not devices.effects
            path = tmp_path / 'katana' / 'executor-manual-history' / f'{original}.json'
            assert path.exists()


def test_resume_uses_reviewed_generation_even_if_stop_was_not_from_programme(tmp_path):
    with remote(tmp_path) as (client, executor, devices):
        def hold_before_arm(operation, body=None):
            if operation == 'arm':
                raise OSError('Lost control link before arm')
            return client(operation, body)

        def approval():
            status = client('status')
            return {key: status[key] for key in ('boot', 'session', 'stop_generation')}

        with operator(tmp_path / 'katana', hold_before_arm, steps('seal')) as call:
            agv_ready(call)
            review = call('programme')
            run = call('programme', dict(action='start', revision=review['recipe']['id'],
                                         basket='hold-before-arm', approval=approval()))
            held = wait_for(lambda: call('programme')['run'], lambda run: run['state'] == 'held')
            assert held['id'] == run['id']
            delayed = dict(action='resume', job_id=run['id'], approval=approval())
            call('agv', dict(action='send', command='stop'))
            wait_for(lambda: call('agv'), lambda status: not status['stop_unconfirmed'])
            with pytest.raises(HTTPError, match='400'):
                call('programme', delayed)
            assert call('programme')['run']['state'] == 'held'
            assert not devices.effects


def test_lease_watcher_retries_a_failed_disarm_before_rearming(tmp_path):
    now = [0.]
    failed = Event()
    recover = Event()

    class Devices(FakeDevices):
        def disarm(self):
            if self.armed and not recover.is_set():
                failed.set()
                raise OSError('Temporary device disarm failure')
            super().disarm()

    devices = Devices()
    executor = Executor(tmp_path, devices, clock=lambda: now[0])
    envelope = dict(protocol=1, session='1' * 32, boot=executor.boot, stop_generation=0)
    try:
        executor.heartbeat(dict(envelope, sequence=1))
        executor.control('connect', envelope)
        assert executor.control('arm', envelope)['armed']
        now[0] = 4
        assert failed.wait(2)
        executor.heartbeat(dict(envelope, sequence=2))
        generation = executor.status()['stop_generation']
        with pytest.raises((OSError, ValueError), match='[Dd]isarm'):
            executor.control('arm', dict(envelope, stop_generation=generation))
        assert not executor.status()['armed']
        assert executor.watch.is_alive()
        recover.set()
        wait_for(lambda: not devices.armed)
        assert executor.control('arm', dict(envelope, stop_generation=generation))['armed']
    finally:
        recover.set()
        executor.close()


@pytest.mark.parametrize('damage', ['marker', 'job'])
def test_orphan_recovery_requires_durable_controller_identity(tmp_path, damage):
    root = tmp_path / 'katana'
    state_path = tmp_path / 'ipc' / '.fake-devices.json'
    with remote(tmp_path, state_path=state_path) as (client, executor, devices), operator(root, client) as call:
        reviewed = arm(call)
        state = call('agv', dict(reviewed, action='send', command='call_station', value=6))
        job_id = state['manual_visit']['id']
        wait_for(lambda: call('agv'), lambda state: (state['manual_visit']['result'] or {}).get('outcome') == 'succeeded')
        call('agv', dict(action='acknowledge_station', job_id=job_id))
    marker = tmp_path / 'ipc' / '.executor-active.json'
    if damage == 'marker':
        marker.write_bytes(b'{truncated')
    else:
        (tmp_path / 'ipc' / '.jobs' / f'{job_id}.json').unlink()
    with remote(tmp_path, state_path=state_path) as (client, executor, devices), operator(root, client) as call:
        wait_for(lambda: call('agv'), lambda state: state.get('executor_session'))
        recovery = dict(action='repair_executor_marker', note='Observed retained simulated stationary hold')
        with pytest.raises(HTTPError, match='400'):
            call('agv', dict(recovery, job_id='invented-job-id'))
        if damage == 'marker':
            assert marker.read_bytes() == b'{truncated'
        assert executor.recovery
        restored = call('agv', dict(recovery, job_id=job_id))
        evidence = restored['executor_recovery_evidence']
        assert evidence['job_id'] == job_id
        key = 'observation' if damage == 'marker' else 'reconciliation'
        assert evidence['outcome'] == ('succeeded' if damage == 'marker' else 'unknown')
        assert evidence[key]['hold']['station'] == 6
        assert executor.status(job_id)['job']['acknowledged']
        assert (root / 'executor-recovery-history' / f"{evidence['event_id']}.json").is_file()
        assert not executor.recovery
        assert not devices.effects


def test_mismatched_stop_receipt_leaves_original_intent_pending(tmp_path):
    with remote(tmp_path) as (client, executor, _):
        mismatched = True

        def transport(operation, body=None):
            if operation == 'stop' and mismatched:
                return dict(stop_id='different-stop-id', boot=executor.boot, generation=1,
                            received=True, halt_attempted=True, confirmed=True)
            return client(operation, body)

        with operator(tmp_path / 'katana', transport) as call:
            reviewed = arm(call)
            pending = call('agv', dict(action='send', command='stop'))
            assert pending['stop_unconfirmed']
            assert not pending['executor_stop']['confirmed']
            with pytest.raises(HTTPError, match='400'):
                call('agv', dict(reviewed, action='arm', armed=True))
            mismatched = False
            cleared = wait_for(lambda: call('agv'), lambda state: not state['stop_unconfirmed'])
            assert cleared['executor_stop']['confirmed']


def test_orphan_ack_retry_reobserves_original_hold_on_new_boot(tmp_path):
    import json

    root = tmp_path / 'katana'
    state_path = tmp_path / 'ipc' / '.fake-devices.json'
    with remote(tmp_path, state_path=state_path) as (client, _, _), operator(root, client) as call:
        request = dict(arm(call), action='send', command='call_station', value=8)
        job_id = call('agv', request)['manual_visit']['id']
        wait_for(lambda: call('agv'), lambda state: (state['manual_visit']['result'] or {}).get('outcome') == 'succeeded')
        call('agv', dict(action='acknowledge_station', job_id=job_id))

    (tmp_path / 'ipc' / '.executor-active.json').write_bytes(b'{')
    recovery = dict(action='repair_executor_marker', job_id=job_id, note='Verified stationary hold')
    with remote(tmp_path, state_path=state_path) as (client, executor, _):
        def interrupt_ack(operation, body=None):
            if operation == 'acknowledge':
                raise OSError('Lost transport before acknowledgement')
            return client(operation, body)

        with operator(root, interrupt_ack) as call:
            wait_for(lambda: call('agv'), lambda state: state.get('executor_session'))
            with pytest.raises(HTTPError, match='400'):
                call('agv', recovery)
            assert not executor.recovery
            saved = json.loads((root / 'executor-recovery.json').read_text())
            assert not saved['acknowledged']

    with remote(tmp_path, state_path=state_path) as (client, executor, devices), operator(root, client) as call:
        wait_for(lambda: call('agv'), lambda state: state.get('executor_session'))
        retrieved = call('agv', recovery)['executor_recovery_evidence']
        assert retrieved['acknowledged']
        assert retrieved['outcome'] == 'succeeded'
        assert retrieved['event_id'] == saved['event_id'] == retrieved['observation']['observation_id']
        assert retrieved['observation']['boot'] == executor.boot != saved['observation']['boot']
        assert retrieved['observation']['hold']['station'] == 8
        assert not devices.effects
