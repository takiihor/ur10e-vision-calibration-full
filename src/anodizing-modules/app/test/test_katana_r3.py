"""Controller-side R3 recovery and request-ordering regressions."""
import json
from threading import Event, Thread
from urllib.error import HTTPError

import pytest

from anodizing_web.executor import Executor, FakeDevices
from anodizing_web.programme import Programme

from test_executor_link_review import arm, operator, remote, wait_for
from test_programme_review import advance, start, steps


def action(call, route, state, request_id, **body):
    return call(route, dict(body, request_id=request_id, review_cursor=state.get('review_cursor', 'baseline')))


def visit(call, reviewed, station, request_id):
    return action(call, 'agv', reviewed, request_id, action='send', command='call_station',
                  connection_id=reviewed['connection_id'], executor_boot=reviewed['executor_boot'],
                  executor_session=reviewed['executor_session'], stop_generation=reviewed['stop_generation'],
                  value=station)


def ack_visit(call, job_id):
    wait_for(lambda: call('agv')['manual_visit']['result'], lambda r: r and r['outcome'])
    return call('agv', {'action': 'acknowledge_station', 'job_id': job_id})


def test_delayed_station_click_returns_original_after_newer_visit_and_restart(tmp_path):
    katana = tmp_path / 'katana'
    with remote(tmp_path) as (client, _, devices):
        with operator(katana, client) as call:
            arm(call)
            first = call('agv')
            job = visit(call, first, 2, 'first')['manual_visit']['id']
            ack_visit(call, job)
            second = visit(call, call('agv'), 3, 'second')['manual_visit']['id']
            ack_visit(call, second)
        with operator(katana, client) as call:
            replay = visit(call, first, 2, 'first')
            assert replay['operator_action']['job_id'] == job
            assert call('agv')['manual_visit'] is None
            assert devices.effects == [('station', 2), ('station', 3)]


def test_unseen_station_request_from_earlier_review_is_rejected(tmp_path):
    with remote(tmp_path) as (client, _, devices), operator(tmp_path / 'katana', client) as call:
        arm(call)
        old = call('agv')
        job = visit(call, old, 3, 'newer')['manual_visit']['id']
        ack_visit(call, job)
        with pytest.raises(HTTPError, match='400'):
            visit(call, old, 2, 'delayed')
        assert devices.effects == [('station', 3)]


def test_delayed_start_returns_original_run_and_an_unseen_old_start_cannot_run(tmp_path):
    step = dict(id='seal', tank='seal-tank', role='seal', station=1, setpoint=30)
    with remote(tmp_path) as (client, _, devices), operator(tmp_path / 'katana', client, [step]) as call:
        state = call('programme')
        body = dict(action='start', basket='reused-label', revision=state['recipe']['id'], approval=state['approval'])
        first = action(call, 'programme', state, 'first', **body)
        wait_for(lambda: call('programme')['run'], lambda r: r['state'] == 'succeeded' and not r['ack_pending'])
        second = action(call, 'programme', call('programme'), 'second', **body)
        wait_for(lambda: call('programme')['run'], lambda r: r['state'] == 'succeeded' and not r['ack_pending'])
        assert second['id'] != first['id']
        assert action(call, 'programme', state, 'first', **body)['id'] == first['id']
        with pytest.raises(HTTPError, match='400'):
            action(call, 'programme', state, 'delayed', **body)
        assert [value for effect, value in devices.effects if effect == 'place'] == ['seal', 'seal']


def test_programme_recovery_allows_status_and_connect_while_fencing_manual_motion(tmp_path):
    katana = tmp_path / 'katana'
    fixture = steps('ultrasonic', 'seal')
    plant = tmp_path / 'ipc' / '.fake-devices.json'
    with remote(tmp_path, state_path=plant) as (client, _, _):
        programme = Programme(katana / 'programme', client, fixture)
        start(programme)
        advance(programme, lambda r: r['cursor'] == 1 and r['ack_pending'])
    with remote(tmp_path, state_path=plant) as (client, _, devices), operator(katana, client, fixture) as call:
        wait_for(lambda: call('programme')['run'], lambda r: r['state'] == 'recovery required' and not r['ack_pending'])
        state = call('agv', {'action': 'connect', 'mode': 'wired'})
        assert state['connected']
        call('agv', {'action': 'send', 'command': 'query_status'})
        with pytest.raises(HTTPError, match='400'):
            call('agv', dict(action='arm', armed=True, executor_boot=state['executor_boot'],
                             executor_session=state['executor_session'], stop_generation=state['stop_generation']))
        with pytest.raises(HTTPError, match='400') as refused:
            visit(call, call('agv'), 7, 'blocked')
        assert 'programme' in json.load(refused.value)['error'].lower()
        assert not devices.effects


def test_unacknowledged_success_is_retained_if_the_ipc_result_disappears(tmp_path):
    katana = tmp_path / 'katana'
    plant = tmp_path / 'ipc' / '.fake-devices.json'
    with remote(tmp_path, state_path=plant) as (client, _, _), operator(katana, client) as call:
        arm(call)
        job = visit(call, call('agv'), 3, 'visit')['manual_visit']['id']
        wait_for(lambda: call('agv')['manual_visit']['result'], lambda r: r and r['outcome'] == 'succeeded')
    (tmp_path / 'ipc' / '.jobs' / f'{job}.json').unlink()
    with remote(tmp_path, state_path=plant) as (client, _, _), operator(katana, client) as call:
        wait_for(lambda: call('agv').get('executor_session'))
        with pytest.raises(HTTPError):
            call('agv', {'action': 'acknowledge_station', 'job_id': job})
        visit_record = call('agv')['manual_visit']
        assert visit_record['missing']
        assert visit_record['result']['outcome'] == 'succeeded'
        recovered = call('agv', dict(action='reconcile_station', job_id=job, note='Verified original hold.'))
        assert recovered['manual_visit']['result']['reconciliation']
        assert recovered['manual_visit']['prior_result']['outcome'] == 'succeeded'
        ack_visit(call, job)


def test_definitive_refusal_preserves_setpoint_detail_without_creating_recovery_intent(tmp_path):
    executor = Executor(tmp_path / 'ipc', FakeDevices(setpoints={'tank-0': 29}))
    try:
        programme = Programme(tmp_path / 'katana', executor.dispatch, steps('ultrasonic'))
        start(programme)
        result = advance(programme, lambda r: r['state'] in ('failed', 'recovery required'))
        assert result['state'] == 'failed'
        assert 'setpoint' in result['reason'].lower()
        assert result['pending'] is None
        assert executor.status(result['refusal']['job_id'])['job'] is None
        assert not any(effect == 'place' for effect, _ in executor.devices.effects)
        assert result['failed_hold']
        assert programme.disposition(result['id'], 'Release the basket at input.')['state'] == 'stopped'
        assert executor.devices.hold()['basket'] is None
        executor.devices.plc.setpoints['tank-0'] = 30
        start(programme)
        assert advance(programme, lambda r: r['state'] == 'succeeded')['final_hold'] == 'output'
    finally:
        executor.close()


def test_basket_recovery_checks_run_and_preserves_unaccounted_hold(tmp_path):
    plant = tmp_path / 'ipc' / '.fake-devices.json'
    executor = Executor(tmp_path / 'ipc', FakeDevices(state_path=plant, setpoints={'tank-0': 30, 'tank-1': 30}))
    programme = Programme(tmp_path / 'katana', executor.dispatch, steps('ultrasonic', 'seal'))
    start(programme)
    advance(programme, lambda r: r['cursor'] == 1 and r['ack_pending'])
    executor.close()
    snapshot = json.loads(plant.read_text())
    snapshot.update(location='output', basket='another-basket', run_id='another-run')
    plant.write_text(json.dumps(snapshot))
    executor = Executor(tmp_path / 'ipc', FakeDevices(state_path=plant, setpoints={'tank-0': 30, 'tank-1': 30}))
    try:
        programme.request = executor.dispatch
        programme.next_pulse = 0
        held = advance(programme, lambda r: r['state'] == 'recovery required' and not r['ack_pending'])
        with pytest.raises(ValueError, match='unaccounted'):
            programme.disposition(held['id'], 'Another basket is present.')
        assert programme.status()['run']['state'] == 'recovery required'
        released = programme.disposition(held['id'], 'Verified another basket.', allow_unaccounted=True)
        assert released['state'] == 'stopped'
        assert released['final_hold'] is None and released['recovery_observation']['unaccounted']
    finally:
        executor.close()


def test_motion_routes_require_reviewed_ids_even_in_direct_http_calls(tmp_path):
    step = dict(id='seal', tank='seal-tank', role='seal', station=1, setpoint=30)
    with remote(tmp_path) as (client, _, devices), operator(tmp_path / 'katana', client, [step]) as call:
        reviewed = arm(call)
        call('agv', dict(reviewed, action='arm', armed=False), raw=True)
        for command in ('query_status', 'stop'):
            with pytest.raises(HTTPError, match='400'):
                call('agv', dict(reviewed, action='arm', armed=True, command=command), raw=True)
        assert not call('agv')['armed']
        with pytest.raises(HTTPError, match='400'):
            call('agv', dict(reviewed, action='send', command='call_station', value=4), raw=True)
        recipe = call('programme')
        with pytest.raises(HTTPError, match='400'):
            call('programme', dict(action='start', basket='basket', revision=recipe['recipe']['id'],
                                   approval=recipe['approval']), raw=True)
        assert not devices.effects


def test_normal_request_cannot_pass_delayed_admission_and_stop_still_has_priority(tmp_path):
    entered, release, stop_forwarded, second_returned = Event(), Event(), Event(), Event()
    with remote(tmp_path) as (client, _, devices):
        def delay(operation, body=None):
            if operation == 'submit':
                entered.set()
                assert release.wait(4)
            if operation == 'stop':
                stop_forwarded.set()
            return client(operation, body)

        with operator(tmp_path / 'katana', delay) as call:
            arm(call)
            first_state = call('agv')
            expected_failures = []

            def send(state, station, key):
                try:
                    visit(call, state, station, key)
                except HTTPError as exc:
                    expected_failures.append(exc)
                finally:
                    if station == 4:
                        second_returned.set()

            first = Thread(target=send, args=(first_state, 3, 'slow'))
            first.start()
            assert entered.wait(2)
            second = Thread(target=send, args=(call('agv'), 4, 'second'))
            second.start()
            try:
                assert not second_returned.wait(.15)
                call('agv', {'action': 'send', 'command': 'stop'}, raw=True)
                assert stop_forwarded.wait(1)
            finally:
                release.set()
                first.join(4)
                second.join(4)
            assert not devices.effects
            assert len(expected_failures) == 2


def test_disposition_acknowledges_recreated_terminal_job_and_releases_next_basket(tmp_path):
    plant = tmp_path / 'ipc' / '.fake-devices.json'
    fixture = [dict(id='seal', tank='seal-tank', role='seal', station=1, setpoint=30)]
    with remote(tmp_path, state_path=plant) as (client, executor, devices):
        lift = devices.robot.lift
        devices.robot.lift = lambda destination: lift('park')
        programme = Programme(tmp_path / 'katana' / 'programme', client, fixture)
        start(programme)
        settled = advance(programme, lambda r: r['state'] == 'recovery required' and not r['ack_pending'])
        ident = settled['recovery_job']
        assert len(settled['steps']) == 1
    (tmp_path / 'ipc' / '.jobs' / f'{ident}.json').unlink()

    with remote(tmp_path, state_path=plant) as (client, executor, devices):
        programme = Programme(tmp_path / 'katana' / 'programme', client, fixture)
        released = programme.disposition(settled['id'], 'Verified park and unloaded the original basket.')
        assert released['recovery_observation']['unload']['basket'] is None
        assert released['steps'] == settled['steps']
        advance(programme, lambda r: not r['ack_pending'])
        assert executor.status(ident)['job']['acknowledged']
        start(programme)
        assert advance(programme, lambda r: r['state'] == 'succeeded')['final_hold'] == 'output'


def test_lost_first_orphan_reply_keeps_intent_and_refuses_older_valid_id(tmp_path):
    katana = tmp_path / 'katana'
    plant = tmp_path / 'ipc' / '.fake-devices.json'
    with remote(tmp_path, state_path=plant) as (client, _, _), operator(katana, client) as call:
        arm(call)
        old = visit(call, call('agv'), 2, 'old')['manual_visit']['id']
        ack_visit(call, old)
        latest = visit(call, call('agv'), 3, 'latest')['manual_visit']['id']
        ack_visit(call, latest)
    (tmp_path / 'ipc' / '.executor-active.json').write_text('{')
    note = 'Verified the stopped station.'
    with remote(tmp_path, state_path=plant) as (client, executor, _):
        def lost(operation, body=None):
            result = client(operation, body)
            if operation == 'reconcile':
                raise OSError('lost return after IPC committed')
            return result

        with operator(katana, lost) as call:
            wait_for(lambda: call('agv'), lambda s: s.get('executor_session') and s.get('recovery_required'))
            with pytest.raises(HTTPError, match='400'):
                call('agv', dict(action='repair_executor_marker', job_id=old, note=note))
            assert not (katana / 'executor-recovery.json').exists()
            with pytest.raises(HTTPError, match='400'):
                call('agv', dict(action='repair_executor_marker', job_id=latest, note=note))
            state = wait_for(lambda: call('agv'), lambda s: not s['recovery_required'])
            pending = state['executor_recovery_evidence']
            assert pending['disposition'] == note and not pending['acknowledged']
            assert executor.status(latest)['job']['observation']['observation_id'] == pending['event_id']
            with pytest.raises(HTTPError, match='400'):
                call('agv', dict(action='arm', armed=True, executor_boot=state['executor_boot'],
                                 executor_session=state['executor_session'], stop_generation=state['stop_generation']))

    with remote(tmp_path, state_path=plant) as (client, executor, _), operator(katana, client) as call:
        state = wait_for(lambda: call('agv'), lambda s: s.get('executor_session'))
        assert not state['executor_recovery_evidence']['acknowledged']
        fixed = call('agv', dict(action='repair_executor_marker', job_id=latest, note=note))
        assert fixed['executor_recovery_evidence']['acknowledged']
        assert fixed['executor_recovery_evidence']['event_id'] == pending['event_id']
        assert executor.status(latest)['job']['observation']['boot'] == executor.boot


def test_acknowledged_legacy_recovery_merges_previously_missing_confirmed_row(tmp_path):
    root = tmp_path / 'katana'
    executor = Executor(tmp_path / 'ipc', FakeDevices(setpoints={'tank-0': 30}))
    devices = executor.devices
    perform = devices.perform

    def lose_final_observation(effect, value):
        result = perform(effect, value)
        if effect == 'lift':
            devices.hold_available = False
        return result

    devices.perform = lose_final_observation
    try:
        programme = Programme(root, executor.dispatch, steps('seal'))
        start(programme)
        held = advance(programme, lambda run: run['state'] == 'recovery required' and not run['ack_pending'])
        assert held['steps'][0]['id'] == 'step-0'
        assert executor.status(held['recovery_job'])['job']['acknowledged']
        # Older Katana already acknowledged the original job without carrying its confirmed dwell across.
        journal = json.loads((root / 'programme.json').read_text())
        run = journal['runs'][held['id']]
        run['steps'] = []
        run['cursor'] = 0
        run['jobs'][-1]['job']['result'].pop('steps')
        (root / 'programme.json').write_text(json.dumps(journal))
        devices.hold_available = True
        executor.deadline = None
        programme = Programme(root, executor.dispatch, steps('seal'))
        recovered = programme.disposition(held['id'], 'Verified the selected basket at output.')
        assert recovered['steps'] == held['steps']
        assert recovered['cursor'] == 1
        assert Programme(root, executor.dispatch, steps('seal')).status()['run']['steps'] == held['steps']
    finally:
        executor.close()
