"""Fake time and a transport with no supplier or hardware methods."""
import json

import pytest

from anodizing_web.jobs import JobOwner
from anodizing_web.operator_line import Line, Simulator
from anodizing_web.operator_line_test import LineTest
from test_operator_line import Fake, tag
from test_operator import request, service


COMMAND = 'FixtureCommand'
FEEDBACK = 'FixtureFeedback'
TAGS = [tag('Remote', 'M1'), tag('Local', 'M2'), tag(COMMAND, 'M3', access='W'),
        tag(FEEDBACK, 'M4')]
CASE = dict(id='fixture', command=COMMAND, feedback=FEEDBACK, baseline=0, expected=1,
            clear=0, seconds=4, basis='Synthetic mapping', provisional=False)


class Rig:
    def __init__(self, root, *, enabled=True):
        self.time = Fake()
        self.sim = Simulator(dict(tags=TAGS, rows=[dict(Remote=1, Local=0, FixtureCommand=0, FixtureFeedback=0)],
                                  test_cases=[CASE]), wall=self.time.at)
        self.line = Line('simulation', self.sim, clock=lambda: self.time.sec, wall=self.time.at,
                         clock_zone='UTC', clock_offset_s=0)
        self.owner = JobOwner(root)
        self.test = LineTest(root, self.line, self.owner, enabled)
        self.writes = []
        write = self.sim.write

        def record(case, value):
            self.writes.append(value)
            return write(case, value)

        self.sim.write = record
        self.poll(2)

    def poll(self, n=1):
        for _ in range(n):
            self.time.sec += 1
            self.line.step()
            self.test.tick()

    def action(self, action, **body):
        return self.test.action(dict(action=action, **body))

    def prepare(self):
        session = self.action('arm')['session']
        approval = self.action('prepare', session=session, case='fixture', request_id='fixture-run')
        return dict(session=session, request_id='fixture-run', approval=approval['approval'])

    def start(self):
        body = self.prepare()
        self.action('start', **body)
        assert self.writes == []
        self.poll()
        assert self.writes == [1]
        return body


def test_matched_cycle_is_durable_before_dispatch_and_clears_on_later_read(tmp_path):
    rig = Rig(tmp_path)
    write = rig.sim.write
    def inspect(case, value):
        record = json.loads((tmp_path / 'line-tests/simulation/fixture-run.json').read_text())
        assert record['state'] in ('dispatching', 'clearing')
        assert record['baseline']['values'][COMMAND] == 0
        return write(case, value)
    rig.sim.write = inspect
    body = rig.start()
    assert rig.action('start', **body)['id'] == 'fixture-run'
    rig.poll()
    assert rig.writes == [1, 0] and rig.owner.line_test_fence
    rig.poll()
    run = rig.test.status()['history'][0]
    assert run['state'] == 'finished' and run['api'] == 'Pass'
    assert run['zero_readback']['id'] > run['zero_baseline']['id']
    assert not rig.owner.line_test_fence
    rig.action('judge', request_id='fixture-run', result='Fail', notes='Synthetic inspection')
    run = rig.test.status()['history'][0]
    assert run['human']['result'] == 'Fail' and run['api'] == 'Pass'


@pytest.mark.parametrize('gate', ['flag', 'arm', 'approval', 'expired', 'wrong-request', 'wrong-token',
                                  'remote', 'local', 'partial', 'stale', 'clock', 'metadata',
                                  'alias', 'command', 'feedback', 'owner', 'catalog'])
def test_refused_gate_sends_no_command(tmp_path, gate):
    rig = Rig(tmp_path, enabled=gate != 'flag')
    if gate == 'flag':
        with pytest.raises(ValueError):
            rig.action('arm')
    elif gate in ('arm', 'approval'):
        with pytest.raises(ValueError):
            rig.action('prepare' if gate == 'arm' else 'start', case='fixture', request_id='fixture-run')
    else:
        body = rig.prepare()
        if gate in ('expired', 'wrong-request', 'wrong-token'):
            if gate == 'expired':
                rig.time.sec += 31
            if gate == 'wrong-request':
                body['request_id'] = 'changed-run'
            if gate == 'wrong-token':
                body['approval'] = 'wrong'
            with pytest.raises(ValueError):
                rig.action('start', **body)
        else:
            rig.action('start', **body)
            row = rig.sim.rows[0]
            if gate in ('remote', 'local', 'partial', 'command', 'feedback'):
                field, value = {'remote': ('Remote', 0), 'local': ('Local', 1),
                                'partial': (FEEDBACK, None), 'command': (COMMAND, 1),
                                'feedback': (FEEDBACK, 1)}[gate]
                row[field] = value
            if gate in ('metadata', 'alias'):
                rig.sim.tags = [dict(item, device_address='M99' if gate == 'metadata' else 'M4')
                                if item['tag_name'] == COMMAND else item for item in TAGS]
            if gate == 'stale':
                old = rig.sim.request('read')
                rig.sim.request = lambda op: (200, {'tags': TAGS}) if op == 'list' else old
                rig.time.sec += 4
            if gate == 'clock':
                rig.line.offset = None
            if gate == 'owner':
                rig.owner.active = 'unrelated'
            if gate == 'catalog':
                read = rig.sim.request
                rig.sim.request = lambda op: (503, None) if op == 'list' else read(op)
            rig.poll()
            assert rig.test.status()['history'][0]['state'] == 'refused'
    assert not rig.writes


def test_stop_skips_observation_and_recovery_waits_for_both_mode_bits(tmp_path):
    rig = Rig(tmp_path)
    rig.start()
    rig.sim.rows[0]['Local'] = 1
    rig.time.sec += 1
    rig.line.step()
    rig.action('stop', request_id='fixture-run')
    rig.test.tick()
    assert rig.writes == [1]
    assert rig.test.status()['alarm']
    rig.sim.rows[0]['Local'] = 0
    rig.poll(2)
    assert rig.writes == [1, 0]
    assert rig.test.status()['history'][0]['state'] == 'finished'


def test_unknown_dispatch_order_fences_even_after_fresh_zero_and_restart(tmp_path):
    rig = Rig(tmp_path)
    write = rig.sim.write
    rig.sim.write = lambda case, value: (write(case, value)[0], value == 0)
    rig.action('start', **rig.prepare())
    rig.poll(3)
    assert rig.writes == [1, 0]
    assert rig.test.status()['alarm'] and rig.owner.line_test_fence
    restart = Rig(tmp_path)
    assert restart.test.status()['alarm'] and not restart.writes
    assert restart.test.status()['unresolved'] == [dict(id='fixture-run', command=COMMAND)]
    with pytest.raises(ValueError):
        restart.action('arm')
    with pytest.raises(ValueError):
        restart.owner.submit('conflict-job', 'test', {}, lambda _: None)


def test_unreadable_intent_keeps_alarm_and_fence_on_restart(tmp_path):
    rig = Rig(tmp_path)
    rig.start()
    (tmp_path / 'line-tests/simulation/fixture-run.json').write_bytes(b'{')
    restart = Rig(tmp_path)
    assert restart.test.status()['alarm'] and restart.owner.line_test_fence
    assert not restart.writes


@pytest.mark.parametrize('scenario', ['timeout', 'mismatched-one', 'mismatched-zero', 'disarm',
                                      'shutdown', 'expired-session', 'unreachable', 'zero-cache', 'tab-close'])
def test_clears_or_fences_after_possible_dispatch(tmp_path, scenario):
    rig = Rig(tmp_path)
    write, read = rig.sim.write, rig.sim.request

    def inject(case, value):
        receipt, ordered = write(case, value)
        if scenario == 'timeout' and value == 1:
            rig.sim.pending_feedback = None
        if (scenario, value) in [('mismatched-one', 1), ('mismatched-zero', 0)]:
            receipt['device_address'] = 'M99'
        return receipt, ordered

    rig.sim.write = inject
    body = rig.prepare()
    rig.action('start', **body)
    rig.poll()
    if scenario == 'tab-close':
        rig.sim.pending_feedback = None
        rig.test.lease_until = rig.time.sec
    elif scenario == 'shutdown':
        rig.test.closing = True
        rig.test.tick()
    elif scenario == 'disarm':
        rig.action('disarm', session=body['session'])
        rig.test.tick()
    elif scenario == 'expired-session':
        rig.test.session_until = rig.time.sec
        rig.test.tick()
    elif scenario == 'unreachable':
        rig.sim.request = lambda op: (503, None)
    elif scenario == 'zero-cache':
        rig.poll()
        old = read('read')
        same_time = rig.line.sample['source_at']
        rig.sim.request = lambda op: (200, dict(old[1], timestamp=same_time))
    if scenario == 'timeout':
        rig.action('heartbeat', session=body['session'])
    rig.poll(14)
    run = rig.test.status()['history'][0]
    if scenario in ('mismatched-zero', 'unreachable', 'zero-cache'):
        assert run['state'] == 'uncertain' and rig.owner.line_test_fence
    else:
        assert rig.writes == [1, 0] and run['state'] == 'finished'
        assert run['api'] == 'Fail' and not rig.owner.line_test_fence


def test_provisional_feedback_is_observed_without_api_pass(tmp_path):
    rig = Rig(tmp_path)
    rig.test.cases['fixture'] = dict(CASE, provisional=True)
    rig.start()
    rig.poll(3)
    assert rig.test.status()['history'][0]['api'] == 'Observed'


@pytest.mark.parametrize('after_dispatch', [False, True])
def test_storage_failure_fences_with_no_one_or_best_effort_zero(tmp_path, monkeypatch, after_dispatch):
    rig = Rig(tmp_path)
    body = rig.prepare()
    rig.action('start', **body)
    if after_dispatch:
        rig.poll()
    from anodizing_web import operator_line_test
    def storage_failure(*args):
        raise OSError('Synthetic disk failure')
    monkeypatch.setattr(operator_line_test, 'atomic_json', storage_failure)
    rig.poll(12)
    assert rig.writes == ([1, 0] if after_dispatch else [])
    assert rig.test.status()['alarm'] and rig.owner.line_test_fence


def test_routes_require_token_and_origin_and_export_bound_evidence(tmp_path, service):
    rig = Rig(tmp_path)
    service.line_tests = rig.test
    path = '/api/tests/line'
    assert request(service, path, token='wrong')[0] == 403
    assert request(service, path, origin='http://localhost:9999',
                   method='POST', body={'action': 'arm'})[0] == 403
    assert request(service, path, host='localhost:9999')[0] == 403
    rig.start()
    rig.poll(3)
    for format in ('json', 'csv', 'summary'):
        status, headers, body = request(service, f'{path}/run/fixture-run/{format}')
        assert status == 200 and 'attachment' in headers['Content-Disposition']
        assert b'Simulation' in body and b'Pass' in body
    assert request(service, f'{path}/run/missing-id/json')[0] == 404


def test_live_client_cannot_enable_writes_even_with_flag(tmp_path):
    driver = Line('live', object())
    test = LineTest(tmp_path, driver, JobOwner(tmp_path), True)
    assert not test.status()['enabled']
    with pytest.raises(ValueError, match='Simulator writes are disabled'):
        test.action({'action': 'arm'})


def test_cleanup_refreshes_metadata_before_any_zero(tmp_path):
    rig = Rig(tmp_path)
    rig.start()
    rig.sim.tags = [dict(item, device_address='M99') if item['tag_name'] == COMMAND else item for item in TAGS]
    rig.action('stop', request_id='fixture-run')
    rig.test.tick()
    assert rig.writes == [1]
    assert rig.test.status()['alarm']
    rig.sim.tags = TAGS
    rig.poll(4)
    assert rig.writes == [1, 0]
    assert rig.test.status()['latest']['state'] == 'finished'
