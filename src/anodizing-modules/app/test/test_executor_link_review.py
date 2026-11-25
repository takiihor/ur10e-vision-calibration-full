"""Exercise the Katana adapter against the IPC's real loopback job API."""
from contextlib import contextmanager
import json
from threading import Event, Thread, current_thread
import time
from urllib.request import Request, urlopen
from uuid import uuid4

import pytest

from anodizing_web.executor import Executor, FakeDevices
from anodizing_web.executor_http import Client, control_listener
from anodizing_web.server import Server


TOKEN = 'review-test-token-only-not-for-installation'


def wait_for(read, predicate=bool, *, timeout=10):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = read()
        if predicate(value):
            return value
        time.sleep(.02)
    pytest.fail(f'Timed out waiting for {value!r}')


@contextmanager
def running(server):
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_port
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


@contextmanager
def remote(tmp_path, *, state_path=None):
    from test_executor_phase1 import FAKE_TANK_SETPOINTS
    devices = FakeDevices(setpoints=dict(FAKE_TANK_SETPOINTS, **{'seal-tank': 30}), state_path=state_path)
    executor = Executor(tmp_path / 'ipc', devices)
    try:
        with running(control_listener(0, executor, TOKEN)) as port:
            yield Client(port, TOKEN), executor, devices
    finally:
        executor.close()


@contextmanager
def operator(root, client, steps=None):
    server = Server(0, root, executor_client=client, programme_steps=steps)
    with running(server) as port:
        def call(path, body=None, *, raw=False):
            if body and not raw and path in ('agv', 'programme') and body.get('command') != 'query_status':
                body = dict(body)
                if 'review_cursor' not in body:
                    body['review_cursor'] = call(path).get('review_cursor')
                body.setdefault('request_id', uuid4().hex)
            request = Request(f'http://127.0.0.1:{port}/api/{path}',
                              data=None if body is None else json.dumps(body).encode(),
                              headers={'X-Module-Token': server.token, 'Content-Type': 'application/json'})
            with urlopen(request, timeout=5) as reply:
                return json.load(reply)
        yield call


def agv_ready(call):
    return wait_for(lambda: call('agv'), lambda state: state.get('executor_health') and not state.get('motion_block'))


def arm(call):
    agv_ready(call)
    state = call('agv', {'action': 'connect', 'mode': 'wired'})
    reviewed = dict(connection_id=state['connection_id'], stop_generation=state['stop_generation'],
                    executor_boot=state.get('executor_boot'), executor_session=state.get('executor_session'))
    assert call('agv', dict(reviewed, action='arm', armed=True))['armed']
    return reviewed


def test_stop_survives_katana_restart_and_is_delivered_before_next_heartbeat(tmp_path):
    with remote(tmp_path) as (client, executor, _):
        calls = []
        available = True

        def partition(operation, body=None):
            calls.append((operation, body))
            if not available:
                raise OSError('review test partition')
            return client(operation, body)

        with operator(tmp_path / 'katana', partition) as call:
            arm(call)
            available = False
            stopped = call('agv', {'action': 'send', 'command': 'stop'})
            assert stopped['stop_unconfirmed']
            assert not stopped['executor_stop']['confirmed']

        with operator(tmp_path / 'katana', partition) as call:
            assert call('agv')['stop_unconfirmed'], 'Restart lost the operator Stop intent.'
            calls.clear()
            available = True
            wait_for(lambda: call('agv'), lambda state: not state['stop_unconfirmed'])
            operations = [operation for operation, _ in calls]
            assert operations[0] == 'stop'
            assert not executor.status()['armed']


def test_configured_recipe_runs_all_twelve_steps_at_remote_ipc(tmp_path):
    roles = ['ultrasonic', 'rinse', 'etch', 'rinse', 'desmut', 'rinse',
             'anodize', 'rinse', 'rinse', 'rinse', 'rinse', 'seal']
    steps = [dict(id=f'step-{i}', tank=f'tank-{i}', role=role, station=i+1, setpoint=30)
             for i, role in enumerate(roles)]
    with remote(tmp_path) as (client, executor, devices):
        sessions = set()

        def track(operation, body=None):
            if operation == 'heartbeat':
                sessions.add(body['session'])
            return client(operation, body)

        with operator(tmp_path / 'katana', track, steps) as call:
            capability = agv_ready(call)
            assert capability['executor_link'] and capability['ports'] == []
            assert capability['executor_boot'] == executor.health()['boot']
            revision = call('programme')['recipe']['id']
            call('programme', {'action': 'start', 'basket': 'basket-http', 'revision': revision,
                               'approval': call('programme')['approval']})
            record = wait_for(lambda: call('programme')['run'],
                              lambda run: run['state'] == 'succeeded', timeout=16)
            assert [row['id'] for row in record['steps']] == [step['id'] for step in steps]
            assert [value for effect, value in devices.effects if effect == 'place'] == [
                step['id'] for step in steps
            ], 'The configured IPC did not execute the reported dips.'
            assert len(sessions) == 1
            assert all(executor.status(entry['job']['job_id'])['job'] for entry in record['jobs'])


def test_manual_visit_retry_after_lost_http_reply_and_katana_restart_reuses_id(tmp_path):
    with remote(tmp_path) as (client, executor, devices):
        submissions = []

        def lose_reply(operation, body=None):
            result = client(operation, body)
            if operation == 'submit':
                submissions.append(body['job_id'])
                if len(submissions) == 1:
                    raise OSError('lost acceptance reply after IPC received request')
            return result

        with operator(tmp_path / 'katana', lose_reply) as call:
            reviewed = arm(call)
            request = dict(reviewed, action='send', command='call_station', value=2, request_id='first-click',
                           review_cursor=call('agv')['review_cursor'])
            # A transport error may surface, but the station visit remains identifiable.
            try:
                call('agv', request)
            except OSError:
                pass
            assert submissions
            wait_for(lambda: executor.status(submissions[0])['job']['outcome'])

        with operator(tmp_path / 'katana', lose_reply) as call:
            def recovered():
                return (call('agv').get('manual_visit') or {}).get('result')
            try:
                result = wait_for(recovered)
            except pytest.fail.Exception:
                pytest.fail('Restart lost the accepted manual visit and its durable ID.')
            assert result['job_id'] == submissions[0]
            assert result['outcome'] == 'succeeded'
            repeated = call('agv', request)
            assert repeated['operator_action']['job_id'] == submissions[0]
            assert devices.effects == [('station', 2)]
            assert len(submissions) == 1


def test_fake_ipc_finishes_admitted_dip_during_partition_then_katana_recovers_it(tmp_path):
    from threading import Event

    step = dict(id='last-seal', tank='seal-tank', role='seal', station=1, setpoint=30)
    entered, release = Event(), Event()
    available = True
    with remote(tmp_path) as (client, executor, devices):
        perform = devices.perform

        def finish_at_boundary(effect, value):
            if effect == 'dwell':
                entered.set()
                assert release.wait(8), 'Test never released the fake PLC.'
            return perform(effect, value)

        def partition(operation, body=None):
            if not available:
                raise OSError('review test partition')
            return client(operation, body)

        devices.perform = finish_at_boundary
        try:
            with operator(tmp_path / 'katana', partition, [step]) as call:
                agv_ready(call)
                revision = call('programme')['recipe']['id']
                run = call('programme', {'action': 'start', 'basket': 'recovered-basket', 'revision': revision,
                                     'approval': call('programme')['approval']})
                assert entered.wait(5), 'The configured executor never started the dip.'
                pending = call('programme')['run']['pending']['job_id']
                generation = client('status')['stop_generation']
                available = False
                wait_for(lambda: client('status'), lambda state: state['stop_generation'] > generation)
                release.set()
                result = wait_for(lambda: client('status', {'job_id': pending})['job'], lambda job: job['outcome'])
                assert result['outcome'] == 'held'
                assert result['result']['hold']['location'] == 'output'
                wait_for(lambda: call('programme')['run'], lambda current: current['state'] == 'held')

            available = True
            with operator(tmp_path / 'katana', partition, [step]) as call:
                agv_ready(call)
                recovered = wait_for(lambda: call('programme')['run'],
                                     lambda current: current['steps'] and not current['ack_pending'])
                assert recovered['state'] == 'held' and recovered['id'] == run['id']
                call('programme', {'action': 'resume', 'job_id': run['id'], 'approval': call('programme')['approval']})
                done = wait_for(lambda: call('programme')['run'], lambda current: current['state'] == 'succeeded')
                assert len(done['steps']) == 1
                assert [entry['job']['job_id'] for entry in done['jobs'] if entry['type'] == 'dip'] == [pending]
                assert [value for effect, value in devices.effects if effect == 'place'] == ['last-seal']
                assert executor.status(pending)['job']['acknowledged']
        finally:
            release.set()


def test_direct_stop_reaches_ipc_while_katana_intent_sync_waits(tmp_path, monkeypatch):
    from anodizing_web import executor_link

    syncing, release, sent = Event(), Event(), Event()
    save = executor_link.atomic_json

    def slow_journal(path, value, **kwargs):
        if path.name == 'executor-stop.json' and not syncing.is_set():
            syncing.set()
            assert release.wait(8)
        return save(path, value, **kwargs)

    monkeypatch.setattr(executor_link, 'atomic_json', slow_journal)
    with remote(tmp_path) as (client, _, _):
        def track(operation, body=None):
            if operation == 'stop' and current_thread().name == 'agv-executor-heartbeat':
                raise OSError('Hold the retry worker so the direct Stop is tested.')
            result = client(operation, body)
            if operation == 'stop':
                sent.set()
            return result

        with operator(tmp_path / 'katana', track) as call:
            arm(call)
            replies = []
            worker = Thread(target=lambda: replies.append(call('agv', {'action': 'send', 'command': 'stop'})))
            worker.start()
            try:
                assert syncing.wait(3)
                assert sent.wait(1), 'Katana waited for journal sync before delivering Stop.'
            finally:
                release.set()
                worker.join(5)
            assert replies and replies[0]['executor_stop']['received']


def test_remote_recording_captures_connected_and_disconnected_context(tmp_path):
    with remote(tmp_path) as (client, executor, _), operator(tmp_path / 'katana', client) as call:
        agv_ready(call)
        info = call('records', {'action': 'start', 'variables': ['agv.connected'], 'rate_hz': 1})
        call('records', {'action': 'stop', 'id': info['id']})
        disconnected = json.loads((tmp_path / 'katana' / 'records' / info['id'] / 'manifest.json').read_text())
        assert disconnected['agv_at_start'] is None
        assert disconnected['agv_transport'] is None
        state = call('agv', {'action': 'connect', 'mode': 'wired'})
        info = call('records', {'action': 'start', 'variables': ['agv.connected'], 'rate_hz': 1})
        call('records', {'action': 'stop', 'id': info['id']})
        manifest = json.loads((tmp_path / 'katana' / 'records' / info['id'] / 'manifest.json').read_text())
        assert manifest['agv_transport'] == 'wired'
        assert manifest['agv_at_start']['settings'] == state['settings']
        assert manifest['agv_at_start']['connection_id'] == state['connection_id']
        assert manifest['agv_at_start']['executor_boot'] == executor.boot


def test_recording_during_partition_labels_the_last_remote_connection(tmp_path):
    available = True
    with remote(tmp_path) as (client, _, _):
        def partition(operation, body=None):
            if not available:
                raise OSError('Lost executor link')
            return client(operation, body)

        with operator(tmp_path / 'katana', partition) as call:
            agv_ready(call)
            connected = call('agv', dict(action='connect', mode='wired'))
            available = False
            wait_for(lambda: call('agv'), lambda state: state.get('error') and state['stale'])
            record = call('records', dict(action='start', variables=['agv.connected'], rate_hz=1))
            call('records', dict(action='stop', id=record['id']))
            manifest = tmp_path / 'katana' / 'records' / record['id'] / 'manifest.json'
            context = json.loads(manifest.read_text())['agv_at_start']
            assert context['connection_id'] == connected['connection_id']
            assert context['executor_reachable'] is False
            assert context['observed_utc'] > 0 and context['age_s'] > 0
