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
        state = wait_for(lambda: call('programme'), lambda review: review['approval'])
        body = dict(action='start', basket='reused-label', revision=state['recipe']['id'], approval=state['approval'])
        first = action(call, 'programme', state, 'first', **body)
        wait_for(lambda: call('programme')['run'], lambda r: r['state'] == 'succeeded')
        second = action(call, 'programme', call('programme'), 'second', **body)
        wait_for(lambda: call('programme')['run'], lambda r: r['state'] == 'succeeded')
        assert second['id'] != first['id']
        assert action(call, 'programme', state, 'first', **body)['id'] == first['id']
        with pytest.raises(HTTPError, match='400'):
            action(call, 'programme', state, 'delayed', **body)
        assert [value for effect, value in devices.effects if effect == 'place'] == ['seal', 'seal']


def test_programme_operator_check_allows_status_and_connect_while_fencing_manual_motion(tmp_path):
    katana = tmp_path / 'katana'
    fixture = steps('ultrasonic', 'seal')
    plant = tmp_path / 'ipc' / '.fake-devices.json'
    with remote(tmp_path, state_path=plant) as (client, _, _):
        programme = Programme(katana / 'programme', client, fixture)
        start(programme)
        advance(programme, lambda r: r['cursor'] == 1)
    with remote(tmp_path, state_path=plant) as (client, _, devices), operator(katana, client, fixture) as call:
        wait_for(lambda: call('programme')['run'], lambda r: r['state'] == 'operator_check')
        state = call('agv', {'action': 'connect', 'mode': 'wired'})
        assert state['connected']
        call('agv', {'action': 'send', 'command': 'query_status'})
        with pytest.raises(HTTPError, match='400'):
            call('agv', dict(action='arm', armed=True, executor_boot=state['executor_boot'],
                             executor_session=state['executor_session'], stop_generation=state['stop_generation']))
        with pytest.raises(HTTPError, match='400') as refused:
            visit(call, call('agv'), 7, 'blocked')
        assert 'Calibration module' in json.load(refused.value)['error']
        assert not devices.effects


def test_unacknowledged_success_is_retained_if_the_ipc_result_disappears(tmp_path):
    katana = tmp_path / 'katana'
    plant = tmp_path / 'ipc' / '.fake-devices.json'
    with remote(tmp_path, state_path=plant) as (client, _, _), operator(katana, client) as call:
        arm(call)
        job = visit(call, call('agv'), 3, 'visit')['manual_visit']['id']
        wait_for(lambda: call('agv')['manual_visit']['result'], lambda r: r and r['outcome'] == 'succeeded')
    path = sorted((tmp_path / 'ipc' / '.executor-journal').glob('*.json'))[-1]
    original = path.read_bytes()
    path.unlink()
    with remote(tmp_path, state_path=plant) as (client, _, devices), operator(katana, client) as call:
        state = wait_for(lambda: call('agv'), lambda status: status.get('evidence_faults'))
        assert state['manual_visit']['result']['outcome'] == 'succeeded'
        assert 'call a technician' in state['operator_check']['reason'].lower()
        with pytest.raises(HTTPError):
            call('agv', dict(action='arm', armed=True, executor_boot=state['executor_boot'],
                             executor_session=state['executor_session'], stop_generation=state['stop_generation']))
        assert not path.exists() and devices.effects == []

    path.write_bytes(original)
    with remote(tmp_path, state_path=plant) as (client, executor, devices), operator(katana, client) as call:
        wait_for(lambda: call('agv').get('executor_session'))
        ack_visit(call, job)
        assert call('agv')['manual_visit_history']['id'] == job
        assert executor.status(job)['job']['outcome'] == 'succeeded'
        assert devices.effects == []


def test_definitive_refusal_preserves_setpoint_detail_for_operator_check(tmp_path):
    executor = Executor(tmp_path / 'ipc', FakeDevices(setpoints={'tank-0': 29}))
    try:
        programme = Programme(tmp_path / 'katana', executor.dispatch, steps('ultrasonic'))
        start(programme)
        result = advance(programme, lambda r: r['state'] == 'operator_check')
        assert 'setpoint' in result['operator_check']['reason'].lower()
        refused = executor.status(result['operator_check']['job_id'])['job']
        assert refused['state'] == 'refused' and refused['error'] == result['reason']
        assert not any(effect == 'place' for effect, _ in executor.devices.effects)
        executor.devices.plc.setpoints['tank-0'] = 30
        programme.decide(result['id'], 'resume', programme.review()['approval'], operator='Inspector',
                         basket=result['basket'], location='input', confirmed=True, step=1)
        assert advance(programme, lambda r: r['state'] == 'succeeded')['final_hold'] == 'output'
    finally:
        executor.close()


def test_operator_check_preserves_foreign_observation_and_known_hold(tmp_path):
    plant = tmp_path / 'ipc' / '.fake-devices.json'
    executor = Executor(tmp_path / 'ipc', FakeDevices(state_path=plant, setpoints={'tank-0': 30, 'tank-1': 30}))
    programme = Programme(tmp_path / 'katana', executor.dispatch, steps('ultrasonic', 'seal'))
    start(programme)
    confirmed = advance(programme, lambda r: r['cursor'] == 1)
    executor.close()
    snapshot = json.loads(plant.read_text())
    snapshot.update(location='output', basket='another-basket', run_id='another-run')
    plant.write_text(json.dumps(snapshot))
    executor = Executor(tmp_path / 'ipc', FakeDevices(state_path=plant, setpoints={'tank-0': 30, 'tank-1': 30}))
    try:
        programme.request = executor.dispatch
        programme.next_pulse = 0
        held = advance(programme, lambda r: (r.get('operator_check') or {}).get('basket') == 'another-basket')
        assert held['operator_check']['observed_location'] == 'output'
        assert held['final_hold'] == 'park' and held['steps'] == confirmed['steps']
        with pytest.raises(ValueError, match='original basket'):
            programme.decide(held['id'], 'resume', programme.review()['approval'], operator='Inspector',
                             basket='another-basket', location='park', confirmed=True, step=2)
        aborted = programme.decide(held['id'], 'abort', programme.review()['approval'], operator='Inspector',
                                   basket='another-basket', location='output', confirmed=True)
        assert aborted['state'] == 'aborted' and aborted['inspection']
        assert aborted['steps'] == confirmed['steps']
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


def test_missing_terminal_entry_keeps_programme_and_measurement_fenced(tmp_path):
    plant = tmp_path / 'ipc' / '.fake-devices.json'
    fixture = steps('ultrasonic', 'seal')
    with remote(tmp_path, state_path=plant) as (client, _, _):
        programme = Programme(tmp_path / 'katana' / 'programme', client, fixture)
        start(programme)
        settled = advance(programme, lambda run: run['cursor'] == 1)
        assert len(settled['steps']) == 1
    path = sorted((tmp_path / 'ipc' / '.executor-journal').glob('*.json'))[-1]
    path.unlink()

    with remote(tmp_path, state_path=plant) as (client, _, devices):
        programme = Programme(tmp_path / 'katana' / 'programme', client, fixture)
        held = advance(programme, lambda run: run['operator_check']['technician'])
        assert 'call a technician' in held['operator_check']['reason'].lower()
        with pytest.raises(ValueError, match='call a technician'):
            programme.decide(settled['id'], 'abort', programme.review()['approval'], operator='Inspector',
                             basket=settled['basket'], location='park', confirmed=True)
        assert programme.status()['run']['steps'] == settled['steps']
        assert not path.exists() and devices.effects == []


def test_corrupt_journal_tip_fences_arm_and_preserves_both_retained_visits(tmp_path):
    katana = tmp_path / 'katana'
    plant = tmp_path / 'ipc' / '.fake-devices.json'
    with remote(tmp_path, state_path=plant) as (client, _, _), operator(katana, client) as call:
        arm(call)
        old = visit(call, call('agv'), 2, 'old')['manual_visit']['id']
        ack_visit(call, old)
        latest = visit(call, call('agv'), 3, 'latest')['manual_visit']['id']
        ack_visit(call, latest)
    marker = tmp_path / 'ipc' / '.executor-journal-tip.json'
    original = marker.read_bytes()
    marker.write_text('{')

    with remote(tmp_path, state_path=plant) as (client, executor, devices), operator(katana, client) as call:
        state = wait_for(lambda: call('agv'), lambda s: s.get('executor_session') and s.get('recovery_required'))
        assert state['operator_check']['technician']
        assert marker.name in state['operator_check']['reason']
        assert 'call a technician' in state['operator_check']['reason'].lower()
        with pytest.raises(HTTPError, match='400'):
            call('agv', dict(action='arm', armed=True, executor_boot=state['executor_boot'],
                             executor_session=state['executor_session'], stop_generation=state['stop_generation']))
        assert marker.read_text() == '{' and devices.effects == []
        assert not (katana / 'executor-recovery.json').exists()

    marker.write_bytes(original)
    with remote(tmp_path, state_path=plant) as (client, executor, devices), operator(katana, client) as call:
        wait_for(lambda: call('agv'), lambda s: s.get('executor_session') and not s['recovery_required'])
        assert executor.status(old)['job']['outcome'] == 'succeeded'
        assert executor.status(latest)['job']['outcome'] == 'succeeded'
        assert devices.effects == []


def test_failed_final_hold_preserves_previous_measured_step_across_restart(tmp_path):
    root = tmp_path / 'katana'
    device = FakeDevices(setpoints={'tank-0': 30, 'tank-1': 30})
    executor = Executor(tmp_path / 'ipc', device)
    perform = device.perform

    def lose_final_observation(effect, value):
        result = perform(effect, value)
        if effect == 'lift' and value == 'output':
            device.hold_available = False
        return result

    device.perform = lose_final_observation
    recipe = steps('ultrasonic', 'seal')
    try:
        programme = Programme(root, executor.dispatch, recipe)
        start(programme)
        previous = advance(programme, lambda run: run['cursor'] == 1)
        held = advance(programme, lambda run: run['state'] == 'operator_check')
        assert held['cursor'] == 1
        assert held['operator_check']['job_id'] == held['pending']['job_id']
        assert held['steps'][0]['id'] == previous['steps'][0]['id']
        assert held['steps'][0]['dwell_s'] == previous['steps'][0]['dwell_s']
        assert held['final_hold'] == 'park'
        restored = Programme(root, executor.dispatch, recipe).status()['run']
        assert restored['steps'] == held['steps'] and restored['pending'] == held['pending']
    finally:
        executor.close()
