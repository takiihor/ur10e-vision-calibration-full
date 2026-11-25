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
            run = wait_for(lambda: call('programme')['run'], lambda r: r['state'] == 'succeeded' and not r['ack_pending'])
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



@pytest.mark.parametrize('lock_name', ['lock', 'stop_lock'])
def test_link_delivers_before_cache_or_receipt_storage_is_available(tmp_path, lock_name):
    delivered = Event()

    with remote(tmp_path) as (client, _, _):
        def track(op, body=None):
            result = client(op, body)
            if op == 'stop':
                delivered.set()
            return result

        app = server.Server(0, tmp_path / 'katana', executor_client=track)
        try:
            link = app.executor_link
            wait_for(link.status, lambda state: state.get('executor_session'))
            with getattr(link, lock_name):
                worker = Thread(target=link.stop, args=({'stop_id': 'priority-stop'},))
                worker.start()
                assert delivered.wait(.8), f'Stop waited behind {lock_name}'
        finally:
            worker.join(5)
            app.server_close()



def test_lost_manual_refusal_preserves_reason_and_releases_unadmitted_visit(tmp_path):
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
            result = wait_for(lambda: call('agv'), lambda s: s.get('manual_visit_history'), timeout=3)
            history = result['manual_visit_history']
            assert result['manual_visit'] is None and history['result']['state'] == 'refused'
            assert history['result']['error'] and history['result']['job_id'] == history['id']
            assert not devices.effects



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
            assert not executor.status(job_id)['job']['acknowledged']
            corrupt = False
            wait_for(lambda: call('agv'), lambda s: ((s['manual_visit']['result'] or {}).get('result') or {})
                     .get('hold', {}).get('station') == 4)
            call('agv', dict(action='acknowledge_station', job_id=job_id))
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
