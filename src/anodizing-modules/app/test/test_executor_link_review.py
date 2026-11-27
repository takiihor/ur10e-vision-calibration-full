"""Exercise the Katana adapter against the IPC's real loopback job API."""
from contextlib import contextmanager
import json
import secrets
from threading import Event, Thread
import time
from urllib.request import Request, urlopen
from uuid import uuid4

import pytest

from anodizing_web.executor import Executor, FakeDevices
from anodizing_web.executor_http import Client, control_listener
from anodizing_web.server import Server


TOKEN = secrets.token_urlsafe(32)


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


def test_manual_visit_with_lost_reply_stays_in_operator_check_across_katana_restart(tmp_path):
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
            state = wait_for(lambda: call('agv'), lambda observed: observed.get('operator_check'))
            assert state['manual_visit']['id'] == submissions[0]
            assert state['operator_check']['job_id'] == submissions[0]
            assert devices.effects in ([], [('station', 2)])
            assert len(submissions) == 1


def test_fake_ipc_and_katana_require_operator_check_after_dip_partition(tmp_path):
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
                wait_for(lambda: client('status', {'job_id': pending})['job'], lambda job: job['outcome'])
                held = wait_for(lambda: call('programme')['run'], lambda current: current['state'] == 'operator_check')
                assert held['pending']['job_id'] == pending
                assert held['operator_check']['job_id'] == pending

            available = True
            with operator(tmp_path / 'katana', partition, [step]) as call:
                wait_for(lambda: call('agv'), lambda state: state.get('executor_session'))
                recovered = call('programme')['run']
                assert recovered['state'] == 'operator_check' and recovered['id'] == run['id']
                assert recovered['pending']['job_id'] == pending
                assert recovered['operator_check']['job_id'] == pending
                assert len([effect for effect, _ in devices.effects if effect == 'place']) == 1
                assert not client('status')['armed']
        finally:
            release.set()


def test_stop_intent_becomes_durable_while_transport_is_blocked(tmp_path):
    entered, release = Event(), Event()
    katana = tmp_path / 'katana'
    stop_id = uuid4().hex
    with remote(tmp_path) as (client, _, _):
        def block_first_stop(operation, body=None):
            if operation == 'stop' and not entered.is_set():
                entered.set()
                assert release.wait(5)
            return client(operation, body)

        with operator(katana, block_first_stop) as call:
            arm(call)
            worker = Thread(target=lambda: call('agv', {'action': 'send', 'command': 'stop',
                                                        'request_id': stop_id}))
            worker.start()
            try:
                assert entered.wait(3)
                witness = katana / 'executor-stop-witness.json'
                intent = wait_for(lambda: json.loads(witness.read_text()) if witness.is_file() else None)
                assert intent['id'] == stop_id
                assert not intent['report']['confirmed']
            finally:
                release.set()
                worker.join(5)


def test_stop_confirmation_with_an_active_job_is_rejected(tmp_path):
    from anodizing_web.executor_http import _validate_reply

    with remote(tmp_path) as (client, _, _), operator(tmp_path / 'katana', client) as call:
        arm(call)
        call('agv', {'action': 'send', 'command': 'stop'})
        stopped = wait_for(lambda: client('status'), lambda status: (status['stop'] or {}).get('confirmed'))
        disconnected = dict(stopped, device=dict(stopped['device'], connected=False, simulated=False))
        missing_active = dict(stopped)
        del missing_active['active_job_id']
        for contradiction in (dict(stopped, active_job_id='still-active'), missing_active, disconnected):
            with pytest.raises(ValueError, match='Stop'):
                _validate_reply('status', contradiction)


def test_wire_log_detects_unlinked_path(tmp_path):
    from anodizing_web.executor_http import WireLog

    path = tmp_path / 'wire.jsonl'
    trace = WireLog(path)
    try:
        trace('tx', b'first')
        path.unlink()
        with pytest.raises(OSError, match='wire.jsonl'):
            trace('tx', b'second')
    finally:
        trace.close()



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



@pytest.mark.parametrize(('protocol', 'direct'), [(1, True), (1, False), (2, True)])
def test_controller_reads_only_legacy_status_without_granting_authority(tmp_path, protocol, direct):
    class EarlierExecutor(Executor):
        def health(self):
            return dict(super().health(), protocol=protocol)

        def status(self, job_id=None):
            status = super().status(job_id)
            del status['operator_check']
            return dict(status, armed=True, lease_valid=True, session='previous-controller',
                        job={'job_id': 'legacy-job'}, active_job_id='legacy-job')

    executor = EarlierExecutor(tmp_path / 'ipc', FakeDevices())
    try:
        executor.devices.connect()
        with running(control_listener(0, executor, TOKEN)) as port:
            client = Client(port, TOKEN)
            if protocol == 2:
                with pytest.raises(OSError, match='Invalid IPC executor response'):
                    client('status')
                return
            def callable_client(operation, body=None):
                return executor.dispatch(operation, body or {})

            with operator(tmp_path / 'katana', client if direct else callable_client) as call:
                state = wait_for(lambda: call('agv'), lambda status: status.get('executor_reachable'))
                assert state['connected']
                assert state['executor_health']['protocol'] == 1
                assert state['executor_session'] is None
                assert state['executor_job'] is None
                assert 'read-only' in state['motion_block']
                assert not state['armed']
                assert executor.session is None
    finally:
        executor.close()


@pytest.mark.parametrize('journal', ['manual', 'operator'])
def test_manual_write_failure_halts_and_operator_can_release(tmp_path, monkeypatch, journal):
    from urllib.error import HTTPError
    from anodizing_web import executor_link, server

    module = executor_link if journal == 'manual' else server
    save = module.atomic_json
    failures = []

    def fail_first_manual_write(path, value, **kwargs):
        matched = (path.name == 'executor-manual-active.json' if journal == 'manual' else
                   path.parent.name == 'executor-operator-actions' and not failures and not value.get('result'))
        if armed and matched and not failures:
            failures.append(path)
            raise OSError('simulated transient failure')
        return save(path, value, **kwargs)

    armed = False
    monkeypatch.setattr(module, 'atomic_json', fail_first_manual_write)
    with remote(tmp_path) as (client, _, devices), operator(tmp_path / 'katana', client) as call:
        reviewed = arm(call)
        armed = True
        with pytest.raises(HTTPError):
            call('agv', dict(reviewed, action='send', command='call_station', value=2))
        held = wait_for(lambda: call('agv'), lambda state: state.get('operator_check'), timeout=3)
        check = held['operator_check']
        assert str(failures[0]) in check['reason'] and not check['technician']
        assert ('station', 2) not in devices.effects
        ready = wait_for(lambda: call('agv'), lambda state: not state['stop_unconfirmed']
                         and bool(client('status')['operator_check']))
        status = client('status')
        check = status['operator_check']
        assert not status['armed']
        cleared = call('agv', dict(reviewed, action='operator_check', choice='abort', operator='Inspector',
                                    location=check['location'] or 'station 1', basket=None,
                                    confirmed=True, job_id=check['job_id'],
                                    stop_generation=status['stop_generation']))
        assert cleared['operator_check'] is None


def test_manual_archive_history_loss_is_named_and_blocks_arm(tmp_path):
    katana = tmp_path / 'katana'
    with remote(tmp_path) as (client, _, _):
        with operator(katana, client) as call:
            reviewed = arm(call)
            archived = []
            for station in (2, 3):
                state = call('agv', dict(reviewed, action='send', command='call_station', value=station))
                job_id = state['manual_visit']['id']
                wait_for(lambda: call('agv'), lambda state: ((state['manual_visit'] or {}).get('result') or {})
                         .get('state') == 'done')
                call('agv', {'action': 'acknowledge_station', 'job_id': job_id})
                archived.append(job_id)
        missing = katana / 'executor-manual-history' / (archived[0] + '.json')
        missing.unlink()
        with operator(katana, client) as call:
            check = call('agv')['operator_check']
            assert check['technician'] and missing.name in check['reason']
            assert (missing.parent / (archived[1] + '.json')).is_file()


def test_manual_abort_keeps_intent_when_ipc_did_not_apply_decision(tmp_path):
    from urllib.error import HTTPError

    with remote(tmp_path) as (client, _, _):
        def contradict(operation, body=None):
            if operation == 'submit':
                client(operation, body)
                raise OSError('simulated lost submit response')
            if operation == 'operator_check':
                return client('status')
            return client(operation, body)

        with operator(tmp_path / 'katana', contradict) as call:
            reviewed = arm(call)
            with pytest.raises(HTTPError):
                call('agv', dict(reviewed, action='send', command='call_station', value=2))
            state = wait_for(lambda: call('agv'), lambda state: state.get('operator_check'))
            check = state['operator_check']
            with pytest.raises(HTTPError):
                call('agv', dict(reviewed, action='operator_check', choice='abort', operator='Inspector',
                                 confirmed=True, job_id=check['job_id'], basket=None,
                                 location=check['location'] or 'station 1', stop_generation=state['stop_generation']))
            assert call('agv')['manual_visit']['id'] == check['job_id']
            assert call('agv')['manual_visit_history'] is None


@pytest.mark.parametrize('failure', ['short_write', 'sync'])
def test_wire_log_retry_keeps_exactly_one_frame(tmp_path, monkeypatch, failure):
    from anodizing_web import executor_http

    path = tmp_path / 'wire.jsonl'
    trace = executor_http.WireLog(path)
    write, sync = executor_http.os.write, executor_http.os.fsync
    offset = []

    def short_write(fd, data):
        if fd == trace.fd:
            offset.append(len(data))
            if len(offset) == 1:
                return write(fd, data[:5])
            if len(offset) == 2:
                raise OSError('simulated write failure after prefix')
        return write(fd, data)

    def fail_sync(fd):
        if fd == trace.fd and not offset:
            offset.append(1)
            raise OSError('simulated lost fsync')
        return sync(fd)

    try:
        if failure == 'short_write':
            monkeypatch.setattr(executor_http.os, 'write', short_write)
        else:
            monkeypatch.setattr(executor_http.os, 'fsync', fail_sync)
        with pytest.raises(OSError, match='wire.jsonl'):
            trace('tx', b'one')
        trace('tx', b'one')
        trace('rx', b'two')
    finally:
        trace.close()
    assert [json.loads(line)['hex'] for line in path.read_text().splitlines()] == ['6f6e65', '74776f']


def test_interrupted_stop_publication_is_operator_releasable(tmp_path, monkeypatch):
    from urllib.error import HTTPError
    from anodizing_web import executor_link

    katana = tmp_path / 'katana'
    stop_id = uuid4().hex
    save = executor_link.atomic_json
    fail_receipt = False

    def fail_receipt_write(path, value, **kwargs):
        if fail_receipt and path.name == 'executor-stop.json':
            raise OSError('simulated interruption after Stop witness')
        return save(path, value, **kwargs)

    monkeypatch.setattr(executor_link, 'atomic_json', fail_receipt_write)
    with remote(tmp_path) as (client, _, _):
        with operator(katana, client) as call:
            arm(call)
            call('agv', {'action': 'send', 'command': 'stop'})
            wait_for(lambda: call('agv'), lambda state: not state['stop_unconfirmed'])
            fail_receipt = True
            call('agv', {'action': 'send', 'command': 'stop', 'request_id': stop_id})
            assert json.loads((katana / 'executor-stop-witness.json').read_text())['id'] == stop_id
            assert json.loads((katana / 'executor-stop.json').read_text())['id'] != stop_id
        fail_receipt = False
        allow_reply, ipc_held = Event(), Event()

        def delayed_hold(operation, body=None):
            reply = client(operation, body)
            if operation == 'hold':
                ipc_held.set()
                assert allow_reply.wait(5)
            return reply

        with operator(katana, delayed_hold) as call:
            try:
                assert ipc_held.wait(8)
                current = client('status')
                stale = call('agv')
                check = current['operator_check']
                assert stale['stop_generation'] < current['stop_generation']
                assert not check['technician'] and 'executor-stop.json' in check['reason']
                assert json.loads((katana / 'executor-stop.json').read_text())['id'] == stop_id
                decision = {'action': 'operator_check', 'executor_boot': stale['executor_boot'],
                            'executor_session': stale['executor_session'], 'choice': 'abort',
                            'confirmed': True, 'job_id': check['job_id'], 'basket': None,
                            'location': check['location'], 'operator': 'Inspector'}
                with pytest.raises(HTTPError) as refused:
                    call('agv', dict(decision, stop_generation=stale['stop_generation']))
                assert 'Review the current Stop generation' in refused.value.read().decode()
            finally:
                allow_reply.set()

            reviewed = wait_for(lambda: call('agv'), lambda state: not state['stop_unconfirmed']
                                and state['stop_generation'] == current['stop_generation']
                                and state.get('operator_check', {}).get('location') == check['location'])
            confirmed = call('agv', dict(decision, stop_generation=reviewed['stop_generation']))
            assert confirmed['operator_check'] is None


def test_manual_terminal_contradiction_survives_next_honest_poll(tmp_path):
    with remote(tmp_path) as (client, executor, _):
        status = executor.status
        contradicted = Event()

        def contradictory_status(job_id=None):
            state = status(job_id)
            job = state['job']
            if job_id and job and job['state'] == 'done' and not contradicted.is_set():
                contradicted.set()
                return dict(state, job=dict(job, outcome='failed'))
            return state

        executor.status = contradictory_status
        with operator(tmp_path / 'katana', client) as call:
            reviewed = arm(call)
            sent = call('agv', dict(reviewed, action='send', command='call_station', value=2))
            assert contradicted.wait(5)
            held = wait_for(lambda: call('agv'), lambda state: (state['manual_visit'] or {}).get('doubt')
                            and state.get('operator_check'))
            assert held['manual_visit']['id'] == sent['manual_visit']['id']
            assert 'outcomes conflict' in held['manual_visit']['doubt']
            assert held['operator_check']['job_id'] == sent['manual_visit']['id']
            assert not client('status')['armed']


def test_covering_stop_is_reopened_on_contradiction_but_not_on_later_arm(tmp_path):
    from threading import Lock

    from anodizing_web.executor_link import ExecutorLink

    with remote(tmp_path) as (client, executor, _):
        first = 'first-stop'
        second = 'covering-stop'
        executor.stop(dict(stop_id=first))
        executor.stop(dict(stop_id=second))
        receipt = wait_for(lambda: executor.stop(dict(stop_id=first)), lambda report: report['confirmed'])
        settled = client('status')
        assert settled['stop']['stop_id'] == second

        link = ExecutorLink.__new__(ExecutorLink)
        link.stop_latch = {}
        link.stop_lock = Lock()
        link.stop_intent = dict(id=first, report=receipt)
        link.cached = {}
        link._publish_stop = lambda intent: link.stop_latch.update(pending=intent)

        link._recheck_stop(settled)
        assert not link._pending_stop()

        contradictory = dict(settled, device=dict(settled['device'],
                             status=dict(settled['device']['status'], fault_code=1)))
        link._recheck_stop(contradictory)
        pending = link._pending_stop()
        assert pending['id'] == first and not pending['report']['confirmed']
        link.stop_latch.clear()

        # A reviewed Arm consumes the current Stop receipt.
        identity = dict(boot=executor.boot, session='replacement-controller', protocol=2)
        executor.heartbeat(dict(identity, sequence=1))
        executor.control('connect', identity)
        executor.control('arm', dict(identity, stop_generation=settled['stop_generation']))
        armed = client('status')
        assert armed['armed'] and armed['stop'] is None
        link._recheck_stop(armed)
        assert not link._pending_stop()


def test_manual_decision_requires_explicit_cleared_check_and_recovery():
    from anodizing_web.executor_link import ExecutorLink

    request = dict(boot='ipc-boot', session='reviewed-session', stop_generation=3)
    reply = dict(request, lease_valid=True, armed=False, recovery_required=False, operator_check=None)
    assert ExecutorLink._decision_applied(reply, request)
    missing_check = dict(reply)
    del missing_check['operator_check']
    missing_recovery = dict(reply)
    del missing_recovery['recovery_required']
    assert not ExecutorLink._decision_applied(missing_check, request)
    assert not ExecutorLink._decision_applied(missing_recovery, request)


def test_stop_reaches_executor_while_its_witness_write_is_stalled(tmp_path, monkeypatch):
    from anodizing_web import executor_link

    write_entered, release = Event(), Event()
    original = executor_link.atomic_json
    stop_id = uuid4().hex

    def stalled_write(path, value, **kwargs):
        if path.name == 'executor-stop-witness.json' and value['id'] == stop_id:
            write_entered.set()
            assert release.wait(5)
        return original(path, value, **kwargs)

    monkeypatch.setattr(executor_link, 'atomic_json', stalled_write)
    with remote(tmp_path) as (client, executor, _):
        with operator(tmp_path / 'katana', client) as call:
            arm(call)
            results = []
            worker = Thread(target=lambda: results.append(call(
                'agv', {'action': 'send', 'command': 'stop', 'request_id': stop_id})))
            worker.start()
            try:
                assert write_entered.wait(3)
                halted = wait_for(executor.status, lambda status: (status['stop'] or {}).get('stop_id') == stop_id,
                                  timeout=1)
                assert halted['armed'] is False
            finally:
                release.set()
                worker.join(5)
            assert results


def test_old_boot_stop_confirmation_cannot_clear_pending_stop(tmp_path):
    from types import SimpleNamespace
    from anodizing_web.executor_link import ExecutorLink

    root = tmp_path / 'katana'
    root.mkdir()
    local = SimpleNamespace(owner=SimpleNamespace(root=root), commissioning=lambda **kw: {},
                            record_event=lambda *args, **kwargs: None)
    delayed, release = Event(), Event()
    blocked = False

    with remote(tmp_path / 'old') as (old_client, old, _), remote(tmp_path / 'new') as (new_client, new, _):
        def transport(operation, body=None):
            if operation == 'status' and blocked:
                reply = old_client(operation, body)
                delayed.set()
                assert release.wait(5)
                return reply
            return old_client(operation, body)

        link = ExecutorLink(transport, local)
        wait_for(link.status, lambda state: state.get('executor_boot') == old.boot)
        link.close()
        stop_id = uuid4().hex
        wait_for(lambda: old_client('stop', {'stop_id': stop_id}), lambda report: report['confirmed'])
        blocked = True
        reports = []
        thread = Thread(target=lambda: reports.append(link.stop({'stop_id': stop_id})))
        thread.start()
        try:
            assert delayed.wait(2)
            link._apply(new_client('status'), meta=new.health())
            assert link.status()['executor_boot'] == new.boot
        finally:
            release.set()
            thread.join(5)
        assert reports and not reports[0]['confirmed']
        assert link.status()['stop_unconfirmed']
        link.client = new_client
        wait_for(lambda: link.stop({'stop_id': stop_id}), lambda receipt: receipt['confirmed'])
        assert new_client('status')['stop']['stop_id'] == stop_id
        assert not link.status()['stop_unconfirmed']
