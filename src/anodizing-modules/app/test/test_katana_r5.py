"""Katana routes, priority delivery and optional recording against the real IPC job client."""
import json
from threading import Event, Thread
from urllib.error import HTTPError

import pytest

from anodizing_web import server
from test_executor_link_review import arm, operator, remote, wait_for
from test_programme_review import steps


@pytest.mark.parametrize('route', ['agv', 'programme'])
def test_stop_delivered_during_unrelated_operator_journal_write(tmp_path, monkeypatch, route):
    journal, release, delivered = Event(), Event(), Event()
    original = server.atomic_json

    def stalled(path, value, **kwargs):
        if path.parent.name == 'executor-operator-actions' and set(value) == {'signature'}:
            journal.set()
            assert release.wait(5)
        return original(path, value, **kwargs)

    with remote(tmp_path) as (client, _, _):
        def track(op, body=None):
            result = client(op, body)
            if op == 'stop':
                delivered.set()
            return result

        with operator(tmp_path / 'katana', track, steps('seal')) as call:
            arm(call)
            monkeypatch.setattr(server, 'atomic_json', stalled)
            cursor = call('agv')['review_cursor']
            unrelated = Thread(target=call, args=('agv', dict(action='arm', armed=False,
                               review_cursor=cursor, request_id='unrelated')))
            unrelated.start()
            assert journal.wait(2)
            body = dict(action='stop') if route == 'programme' else dict(action='send', command='stop')
            worker = Thread(target=call, args=(route, dict(body, request_id='urgent-request', review_cursor=cursor)))
            worker.start()
            try:
                assert delivered.wait(.8), 'Stop waited for another operator journal'
            finally:
                release.set()
                unrelated.join(5)
                worker.join(5)


def test_history_query_returns_requested_measured_rows_while_current_stays_live(tmp_path):
    with remote(tmp_path) as (client, _, _), operator(tmp_path / 'katana', client, steps('seal')) as call:
        for basket in ('A', 'B'):
            state = wait_for(lambda: call('programme'), lambda s: s['approval'])
            call('programme', dict(action='start', revision=state['recipe']['id'], approval=state['approval'], basket=basket))
            run = wait_for(lambda: call('programme')['run'], lambda r: r['state'] == 'succeeded')
            if basket == 'A':
                first = run
        historical = call('programme?run_id=' + first['id'])
        assert historical['run']['id'] == first['id']
        assert historical['run']['steps'] == first['steps']
        assert call('programme')['run']['id'] == run['id']
        with pytest.raises(HTTPError) as error:
            call('programme?run_id=missing-run')
        assert error.value.code == 404


def test_remote_recording_forwards_each_frame_and_event_once_and_marks_gap(tmp_path):
    with remote(tmp_path) as (client, _, devices), operator(tmp_path / 'katana', client) as call:
        arm(call)
        info = call('records', dict(action='start', variables=['agv.connected'], rate_hz=1))
        status = devices.status
        frames = [dict(id=1, timestamp='measured', direction='RX', hex='FF', decode={})]
        events = [dict(id=1, timestamp='measured', field='stale', before=True, after=False)]
        devices.status = lambda: dict(status(), log=frames, events=events)
        for _ in range(2):
            call('agv', dict(action='send', command='query_status'))
        frames[:] = [dict(frames[0], id=5)]
        call('agv', dict(action='send', command='query_status'))
        call('records', dict(action='stop', id=info['id']))
        path = tmp_path / 'katana/records' / info['id'] / 'rows.jsonl'
        rows = wait_for(lambda: [json.loads(line) for line in path.read_text().splitlines()],
                        lambda rows: any(r['type'] == 'terminal' for r in rows))
        remote_events = [r for r in rows if r.get('source') == 'agv']
        assert [r['id'] for r in remote_events if r['event'] == 'frame'] == [1, 5]
        assert [r['id'] for r in remote_events if r['event'] == 'stale'] == [1]
        gaps = [r for r in remote_events if r['event'] == 'gap']
        assert len(gaps) == 1 and (gaps[0]['first_missing'], gaps[0]['last_missing']) == (2, 4)
        assert all(r['executor_boot'] and r['connection_id'] and r['transport'] == 'wired' for r in remote_events)



@pytest.mark.parametrize('lock_name', ['lock', 'stop_receipts'])
def test_link_delivers_before_cache_or_receipt_storage_is_available(tmp_path, lock_name):
    root = tmp_path / 'katana'
    with remote(tmp_path) as (client, executor, _):
        app = server.Server(0, root, executor_client=client)
        worker = Thread(target=app.executor_link.stop, args=({'stop_id': 'priority-stop'},))
        try:
            link = app.executor_link
            wait_for(link.status, lambda state: state.get('executor_session'))
            with getattr(link, lock_name):
                worker.start()
                wait_for(executor.status, lambda state: (state['stop'] or {}).get('stop_id') == 'priority-stop',
                         timeout=.8)
            worker.join(5)
            assert not worker.is_alive()
            intent = json.loads((root / 'executor-stop-witness.json').read_text())
            assert intent['id'] == 'priority-stop' and not intent['report']['confirmed']
        finally:
            app.server_close()



def test_lost_manual_refusal_requires_operator_check_without_motion(tmp_path):
    from anodizing_web.executor import AdmissionRefused
    with remote(tmp_path) as (client, _, devices):
        def lost(op, body=None):
            try:
                return client(op, body)
            except AdmissionRefused as exc:
                raise OSError('lost admission reply') from exc

        root = tmp_path / 'katana'
        with operator(root, lost) as call:
            reviewed = arm(call)
            devices.reserve_s = 0
            with pytest.raises(OSError):
                call('agv', dict(reviewed, action='send', command='call_station', value=2))
            result = wait_for(lambda: call('agv'), lambda s: s.get('operator_check'), timeout=3)
            check = result['operator_check']
            assert result['manual_visit']['id'] == check['job_id']
            assert 'reserve' in check['reason'].lower()
            assert not result['manual_visit_history'] and not devices.effects



@pytest.mark.parametrize('when', ['lookup', 'acknowledge'])
def test_manual_station_ack_requires_matching_observed_station(tmp_path, when):
    corrupt = when == 'lookup'
    seen = Event()
    with remote(tmp_path) as (client, executor, devices):
        def transport(op, body=None):
            reply = client(op, body)
            job = reply.get('job') if op == 'status' else reply if op == 'submit' else None
            if corrupt and job and job.get('outcome') == 'succeeded':
                job['result']['hold']['station'] = 9
                seen.set()
            return reply

        with operator(tmp_path / 'katana', transport) as call:
            reviewed = arm(call)
            try:
                call('agv', dict(reviewed, action='send', command='call_station', value=4))
            except HTTPError:
                pass
            job_id = call('agv')['manual_visit']['id']
            if when == 'acknowledge':
                wait_for(lambda: call('agv'), lambda s: (s['manual_visit']['result'] or {}).get('outcome'))
                corrupt = True
            else:
                assert seen.wait(3)
            with pytest.raises(HTTPError):
                call('agv', dict(action='acknowledge_station', job_id=job_id))
            check = wait_for(lambda: call('agv'), lambda s: s.get('operator_check'))
            assert check['manual_visit']['id'] == job_id
            assert check['manual_visit']['doubt']
            corrupt = False
            with pytest.raises(HTTPError):
                call('agv', dict(action='acknowledge_station', job_id=job_id))
            assert executor.status(job_id)['job']['result']['hold']['station'] == 4
            assert devices.effects == [('station', 4)]


def test_kiosk_projects_technician_diagnostic_from_cached_status():
    from urllib.request import urlopen
    from anodizing_web.executor_http import display_listener
    from test_executor_link_review import running

    faults = {'.jobs/missing.json': 'Missing .jobs/missing.json. Please call a technician.'}
    cached = dict(boot='ipc', armed=False, device={'connected': False}, recovery_required=True,
                  evidence_faults=faults)
    with running(display_listener(0, lambda: cached)) as port:
        with urlopen(f'http://127.0.0.1:{port}/status') as reply:
            assert json.load(reply).get('evidence_faults') == faults


def test_programme_operator_receipt_write_failure_fences_dispatch(tmp_path, monkeypatch):
    from anodizing_web.programme import Programme

    allow_tick = Event()
    tick = Programme.tick

    def delayed_tick(programme):
        if allow_tick.wait(5):
            tick(programme)

    monkeypatch.setattr(Programme, 'tick', delayed_tick)
    original = server.atomic_json
    failures = []

    def fail_result(path, value, **options):
        if not failures and path.parent.name == 'executor-operator-actions' and 'result' in value:
            failures.append(path)
            raise OSError('simulated operator receipt storage failure')
        return original(path, value, **options)

    with remote(tmp_path) as (client, executor, devices), operator(tmp_path / 'katana', client, steps('seal')) as call:
        state = wait_for(lambda: call('programme'), lambda review: review['approval'])
        monkeypatch.setattr(server, 'atomic_json', fail_result)
        with pytest.raises(HTTPError) as error:
            call('programme', dict(action='start', revision=state['recipe']['id'], basket='receipt-fault',
                                   approval=state['approval'], request_id='faulted-start',
                                   review_cursor=state['review_cursor']))
        explanation = json.load(error.value)['error']
        assert str(failures[0]) in explanation
        assert 'simulated operator receipt storage failure' in explanation
        try:
            check = wait_for(lambda: executor.status().get('operator_check'), timeout=4)
            run_id = call('programme')['run']['id']
            assert check['run_id'] == run_id and not check.get('manual')
            assert str(failures[0]) in check['reason']
        finally:
            allow_tick.set()
        hold = wait_for(lambda: call('programme'), lambda review: review['run']['state'] == 'operator_check'
                        and not review['run']['stop_pending'] and review['approval']
                        and executor.status().get('operator_check'))
        run = hold['run']
        assert not run['operator_check']['technician']
        with pytest.raises(HTTPError):
            call('agv', dict(action='arm', armed=True))
        with pytest.raises(HTTPError):
            call('programme', dict(action='abort', job_id=run['id'], basket=run['basket'], location='input',
                                   operator='Inspector', confirmed=False, approval=hold['approval']))
        assert not devices.effects and not executor.status()['armed']
        pending_receipt = json.loads(failures[0].read_text())
        assert set(pending_receipt) == {'signature'}

        hold = call('programme')
        try:
            aborted = call('programme', dict(action='abort', job_id=run['id'], basket=run['basket'], location='input',
                                            operator='Inspector', confirmed=True, approval=hold['approval']))
        except HTTPError as exc:
            pytest.fail(f'Physical Abort was refused: {exc.read().decode()} | IPC={executor.status()["operator_check"]!r}')
        assert aborted['state'] == 'aborted' and aborted['operator_choices'][-1]['applied']
        assert not devices.effects and not executor.status()['armed']
        assert executor.status()['operator_check'] is None
        assert json.loads(failures[0].read_text()) == pending_receipt


def test_missing_operator_receipt_keeps_retained_basket_and_blocks_start(tmp_path):
    root = tmp_path / 'katana'
    with remote(tmp_path) as (client, executor, devices), operator(root, client, steps('seal')) as call:
        state = wait_for(lambda: call('programme'), lambda review: review['approval'])
        call('programme', dict(action='start', revision=state['recipe']['id'], basket='saved',
                               approval=state['approval']))
        recorded = wait_for(lambda: call('programme')['run'], lambda run: run['state'] == 'succeeded')
        [receipt] = (root / 'executor-operator-actions').glob('*.json')
        receipt.unlink()
        current = call('programme')
        assert receipt.name in current['technician']
        assert 'call a technician' in current['technician']
        assert current['run']['steps'] == recorded['steps']
        assert current['approval'] is None
        assert [effect for effect, _ in devices.effects].count('place') == 1
