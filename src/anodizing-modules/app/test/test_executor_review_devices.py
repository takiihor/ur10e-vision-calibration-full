"""Regression checks for executor device evidence, quality limits, and wired recovery."""
import json
from threading import RLock
from types import SimpleNamespace
from urllib.request import urlopen

import pytest

from anodizing_web import agv, operator_state
from anodizing_web.executor import Executor, FakeDevices
from anodizing_web.executor_http import display_listener
from anodizing_web.executor_link import ExecutorLink

from agv_examples import EXAMPLE_PREFIX, EXAMPLE_ROUTE, REPORT
from test_executor_phase1 import FAKE_TANK_SETPOINTS, Clock, pair, running, terminal


def arm(executor):
    identity = dict(boot=executor.boot, protocol=1, session='review-controller')
    executor.heartbeat(dict(identity, sequence=1))
    executor.control('connect', identity)
    identity['stop_generation'] = executor.status()['stop_generation']
    executor.control('arm', identity)
    return identity


@pytest.mark.parametrize('mismatch', ['station', 'source', 'gripper', 'basket'])
def test_dip_refuses_unverified_handoff_before_placement(tmp_path, mismatch):
    devices = FakeDevices(setpoints=FAKE_TANK_SETPOINTS)
    executor = Executor(tmp_path, devices)
    try:
        request = pair(arm(executor))
        devices.agv.station = request['params']['steps'][0]['station']
        if mismatch == 'station':
            devices.agv.station += 1
        elif mismatch == 'source':
            devices.robot.location = 'output'
        elif mismatch == 'gripper':
            devices.robot.gripper = 'held'
        else:
            devices.robot.basket = 'other-basket'
        try:
            executor.submit(request)
        except ValueError:
            pass
        else:
            job = terminal(executor, request['job_id'])
            assert job['outcome'] == 'failed'
        assert devices.effects == []
    finally:
        executor.close()


@pytest.mark.parametrize(('flag', 'value'), [
    ('done', False), ('equipment_on', True), ('basket_present', False), ('cycle', -1),
])
def test_anodize_rejects_unqualified_plc_observation_before_lift(tmp_path, flag, value):
    devices = FakeDevices(setpoints=FAKE_TANK_SETPOINTS)
    original = devices.plc.finish

    def unsuitable(step, baseline):
        return dict(original(step, baseline), **{flag: value})

    devices.plc.finish = unsuitable
    executor = Executor(tmp_path, devices)
    try:
        request = pair(arm(executor))
        request['params']['steps'][0]['role'] = 'anodize'
        devices.agv.station = request['params']['steps'][0]['station']
        executor.submit(request)
        job = terminal(executor, request['job_id'])
        assert job['outcome'] == 'unknown'
        assert 'PLC' in job['result']['reason']
        assert devices.effects == [('place', 'step-2')]
    finally:
        executor.close()


@pytest.mark.parametrize('breach', ['understay', 'post-done overstay', 'paired air', 'cross-job air'])
def test_quality_breach_latches_at_immersion_or_dwell_but_finishes_safe_hold(tmp_path, breach):
    devices = FakeDevices(setpoints=FAKE_TANK_SETPOINTS)
    perform = devices.perform
    clock = Clock()
    # Fake time advances explicitly in addition to the configured timer/travel budgets.
    devices.clock = clock
    devices.started = clock()
    during_dwell = []

    def with_delay(effect, value):
        if breach == 'post-done overstay' and effect == 'lift':
            clock.now += 2
        if breach in ('paired air', 'cross-job air') and effect == 'place' and value['id'] == 'step-3':
            clock.now += 8
        if effect == 'dwell' and value['id'] == 'step-3':
            during_dwell.append(executor.status().get('quality_alarm'))
        return perform(effect, value)

    devices.perform = with_delay
    executor = Executor(tmp_path, devices)
    try:
        request = pair(arm(executor))
        steps = request['params']['steps']
        devices.agv.station = steps[0]['station']
        if breach == 'understay':
            devices.plc.dwell_offset = -10
        elif breach == 'post-done overstay':
            steps[0]['role'] = 'anodize'
            steps[0]['limits']['overstay'] = 1
        elif breach == 'paired air':
            steps[0]['limits']['air'] = 6
        elif breach == 'cross-job air':
            request['params']['steps'] = [steps[-1]]
            devices.agv.station = steps[-1]['station']
            request['params']['previous_lift'] = {'lift_s': 0, 'air_limit': 6}
        executor.submit(request)
        job = terminal(executor, request['job_id'])
        assert job['outcome'] == 'held'
        assert job['result']['hold']['location'] == 'park'
        assert not executor.status()['armed']
        if breach in ('paired air', 'cross-job air'):
            assert during_dwell and during_dwell[0]['reason'] == 'Quality limit exceeded'
        with running(display_listener(0, executor.status)) as display:
            state = json.load(urlopen(display + '/status'))
            assert state['quality_alarm']['reason'] == 'Quality limit exceeded'
    finally:
        executor.close()


def test_anodize_distinguishes_a_delayed_plc_transition_from_a_late_poll(tmp_path):
    clock = Clock()
    devices = FakeDevices(clock=clock, setpoints=FAKE_TANK_SETPOINTS)
    finish = devices.plc.finish

    def delayed(step, baseline):
        if step['role'] != 'anodize':
            return finish(step, baseline)
        clock.now += 4
        done = finish(step, baseline)
        clock.now += 9
        return done

    devices.plc.finish = delayed
    executor = Executor(tmp_path, devices)
    try:
        request = pair(arm(executor))
        anodize = request['params']['steps'][0]
        anodize['role'] = 'anodize'
        anodize['limits']['overstay'] = 3
        devices.agv.station = anodize['station']
        executor.submit(request)
        job = terminal(executor, request['job_id'])
        record = job['result']['steps'][0]
        assert record['plc']['done_s'] - record['placed_s'] == 34
        assert record['plc']['sample_s'] - record['plc']['done_s'] == 9
        assert record['dwell_s'] == 43
        assert job['outcome'] == 'held'
    finally:
        executor.close()


def test_interrupted_pair_retains_confirmed_first_dwell(tmp_path):
    devices = FakeDevices(setpoints=FAKE_TANK_SETPOINTS)
    perform = devices.perform

    def interrupt(effect, value):
        if effect == 'station':
            raise ValueError('Receiving station became unavailable after the first lift.')
        return perform(effect, value)

    devices.perform = interrupt
    executor = Executor(tmp_path, devices)
    try:
        request = pair(arm(executor))
        devices.agv.station = request['params']['steps'][0]['station']
        executor.submit(request)
        job = terminal(executor, request['job_id'])
        assert job['outcome'] == 'unknown'
        assert job['result']['steps'][0]['id'] == 'step-2'
        assert job['result']['steps'][0]['dwell_s'] >= 30
    finally:
        executor.close()


@pytest.fixture
def qualified_wire(tmp_path, monkeypatch):
    (tmp_path / 'site-profile.json').write_text(json.dumps({'agv': {
        'wired_port': '/dev/review-fake', **EXAMPLE_ROUTE}}))
    owner = SimpleNamespace(root=tmp_path, lock=RLock(), closing=False, active=None,
                            storage_error=None, execution_uncertain=False)

    class FakeWire:
        def __init__(self, **_):
            self.writes = []
            self.transport_closed = False

        in_waiting = 0

        def read(self, size):
            return b''

        def write(self, raw):
            self.writes.append(raw)
            return len(raw)

        def close(self):
            self.transport_closed = True

    monkeypatch.setattr(agv, 'serial', SimpleNamespace(Serial=FakeWire))
    link = agv.Connection(owner, commissioning_arm=True, wired_station_jobs=True)
    monkeypatch.setattr(link, '_start_worker', lambda **_: None)
    link.submit({'action': 'connect', 'mode': 'wired'})
    assert link.splitter.prefix == EXAMPLE_PREFIX
    transport = link.transport
    try:
        with link.recipe_link(stop_generation=link.stop_generation):
            yield link, transport
    finally:
        link.close()


def test_wired_receive_failure_writes_bare_recovery_halt_and_closes(qualified_wire):
    link, transport = qualified_wire
    with link.lock:
        link._transport_error(agv.ReceiveError('test receive failure'))
    assert transport.writes == [agv.command_frame('pause', None, b'').encode()]
    assert transport.transport_closed and link.transport is None


def test_wired_urgent_retry_writes_bare_frame_with_radio_receive_profile(qualified_wire, monkeypatch):
    link, transport = qualified_wire
    monkeypatch.setattr(link, '_due', lambda _: True)
    monkeypatch.setattr(link, '_start_worker', lambda **_: None)
    with link.lock:
        link._send('pause', None, requested_halt='stop')
        assert link._retry(link.resend)
    assert transport.writes == [
        agv.command_frame('pause', None, b'').encode(),
        agv.command_frame('stop', None, b'').encode(),
    ]


def test_current_station_without_verified_arrival_is_not_displayed_as_confirmed():
    adapter = object.__new__(ExecutorLink)
    adapter.status = lambda: dict(connected=True, sequence=1, connection_id=1,
                                  status_time='fixture sample time', status_age_s=0,
                                  status=dict(current_station=3, station_arrival=False, mode=2,
                                              max_stations=20))
    variables = {value['id']: value for value in operator_state.read(adapter)['variables']}
    assert variables['agv.current_station']['value'] == 3
    assert variables['agv.last_confirmed_station']['value'] is None


def test_wired_export_preserves_verified_arrival_timestamp(qualified_wire):
    link, _ = qualified_wire
    decoded = agv.decode(agv.parse_frame(REPORT))
    decoded.update(current_station=3, station_arrival=True, stopped=True,
                   obstacle_stop=False, actual_speed_m_h=0, fault_code=0, mode=2)
    with link.lock:
        link._status_received(decoded, REPORT)
    state = link.commissioning()
    assert state['last_confirmed_station'] == (3, state['status_time'])


def test_remote_mode_fences_native_robot_paths_even_when_opted_in(tmp_path):
    from anodizing_web.server import Server

    devices = FakeDevices(setpoints=FAKE_TANK_SETPOINTS)
    remote = Executor(tmp_path / 'remote', devices)
    try:
        server = Server(0, tmp_path / 'orchestrator', executor_client=remote.dispatch,
                        allow_manual_pick_place=True, allow_agv_in_recipe=True,
                        allow_teacher_motion=True, allow_auto_calibration=True)
        try:
            assert not server.owner.allow_manual_pick_place
            assert not server.owner.allow_agv_in_recipe
            assert not server.owner.allow_teacher_motion
            assert not server.owner.allow_auto_calibration
        finally:
            server.server_close()
    finally:
        remote.close()


def test_elapsed_hold_and_station_travel_are_recorded_at_next_dip(tmp_path):
    clock = Clock()
    devices = FakeDevices(setpoints=FAKE_TANK_SETPOINTS)
    devices.clock, devices.started = clock, clock()
    executor = Executor(tmp_path, devices)
    try:
        identity = arm(executor)
        request = pair(identity)
        first, second = request['params']['steps']
        first['role'] = 'ultrasonic'
        first['limits']['air'] = 6
        devices.agv.station = first['station']
        request['params']['steps'] = [first]
        executor.submit(request)
        lift = terminal(executor, request['job_id'])['result']['steps'][0]['lift_s']

        clock.now += 5
        executor.submit(dict(identity, job_id='next-station', type='station_visit', version=1,
                             params={'station': second['station']}))
        assert terminal(executor, 'next-station')['outcome'] == 'succeeded'
        next_dip = dict(request, job_id='next-dip', params=dict(
            request['params'], steps=[second], source='park',
            previous_lift=dict(lift_s=lift, air_limit=first['limits']['air']),
        ))
        executor.submit(next_dip)
        job = terminal(executor, 'next-dip')
        assert job['outcome'] == 'held'
        assert job['result']['steps'][0]['incoming_air_s'] == 7
        assert job['result']['hold']['location'] == 'park'
    finally:
        executor.close()


def test_legacy_pick_place_stop_renders_executor_receipt():
    from pathlib import Path
    import shutil
    import subprocess
    import anodizing_web

    node = shutil.which('node')
    if node is None:
        pytest.skip('Node is unavailable')
    source = Path(anodizing_web.__file__).parent / 'web' / 'manual-pick-place.js'
    result = subprocess.run([node, '-e', """
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const fields = new Map();
const field = id => {
  if (!fields.has(id)) fields.set(id, {
    id, value: '', dataset: {}, checkValidity: () => true, setAttribute() {},
    addEventListener(type, callback) { this[type] = callback; }
  });
  return fields.get(id);
};
const status = {
  executor_link: true, connected: true, results: {},
  executor_stop: {received: true, halt_attempted: true, confirmed: false}
};
vm.runInNewContext(fs.readFileSync(process.argv[1], 'utf8'), {
  document: {getElementById: field, addEventListener() {}, querySelector: () => ({content: 'test'})},
  window: {}, setTimeout() {},
  fetch: async (route, options) => ({
    ok: true,
    json: async () => {
      if (route === '/api/agv') {
        assert.equal(JSON.parse(options.body).command, 'stop');
        return status;
      }
      if (route === '/api/agv-link') return status;
      return {};
    }
  })
});
field('pick-agv-stop').click().then(() => {
  assert.equal(field('pick-agv-stop-outcome').dataset.state, 'waiting');
  assert.match(field('pick-agv-stop-outcome').textContent, /Halt attempted.*Stop unconfirmed/);
}).catch(error => { process.exitCode = 1; console.error(error); });
""", str(source)], capture_output=True, text=True, timeout=5)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize(('interlock', 'reason'), [
    ('hold_available', 'receiving hold'), ('reserve_s', 'reserve'),
])
def test_fault_after_first_lift_fences_transfer_and_keeps_the_confirmed_dwell(tmp_path, interlock, reason):
    state_path = tmp_path / '.fake-devices.json'
    devices = FakeDevices(setpoints=FAKE_TANK_SETPOINTS, state_path=state_path)
    perform = devices.perform

    def lose_interlock(effect, value):
        evidence = perform(effect, value)
        if (effect, value) == ('lift', 'transfer'):
            setattr(devices, interlock, 0 if interlock == 'reserve_s' else False)
            if interlock == 'reserve_s':
                devices._persist()
        return evidence

    devices.perform = lose_interlock
    executor = Executor(tmp_path, devices)
    try:
        request = pair(arm(executor))
        devices.agv.station = request['params']['steps'][0]['station']
        executor.submit(request)
        result = terminal(executor, request['job_id'])['result']
        assert result['outcome'] == 'unknown'
        assert reason in result['reason']
        assert devices.effects == [('place', 'step-2'), ('lift', 'transfer')]
        assert [step['id'] for step in result['steps']] == ['step-2']
        status = executor.status()
        assert status['recovery_required'] and not status['armed']
        if interlock == 'reserve_s':
            checkpoint = executor.status(request['job_id'])['job']['checkpoint']
            assert checkpoint['reserve']['usable_s'] == 0
            assert checkpoint['reserve']['required_s'] > 0
    finally:
        executor.close()
    if interlock == 'reserve_s':
        replacement = Executor(tmp_path, FakeDevices(setpoints=FAKE_TANK_SETPOINTS, state_path=state_path))
        try:
            status = replacement.status(request['job_id'])
            assert status['device']['reserve']['usable_s'] == 0
            assert status['recovery_required']
            assert [step['id'] for step in status['job']['result']['steps']] == ['step-2']
            with pytest.raises(ValueError, match='[Ss]afe hold|reserve'):
                replacement.devices.observe_recovery_hold()
        finally:
            replacement.close()


@pytest.mark.parametrize(('reserve_s', 'fresh'), [(65, True), (None, True), (float('nan'), True), (100, False)])
def test_numeric_reserve_refuses_paired_dip_before_placement(tmp_path, reserve_s, fresh):
    devices = FakeDevices(setpoints=FAKE_TANK_SETPOINTS)
    executor = Executor(tmp_path, devices)
    try:
        request = pair(arm(executor))
        devices.agv.station = request['params']['steps'][0]['station']
        devices.reserve_s, devices.reserve_fresh = reserve_s, fresh
        with pytest.raises(ValueError, match='reserve'):
            executor.submit(request)
        assert devices.effects == []
    finally:
        executor.close()


def test_numeric_reserve_admits_short_job_and_records_evidence(tmp_path):
    devices = FakeDevices()
    executor = Executor(tmp_path, devices)
    try:
        identity = arm(executor)
        devices.reserve_s = 65
        executor.submit(dict(identity, job_id='numeric-reserve-station', type='station_visit',
                             version=1, params={'station': 1}))
        job = terminal(executor, 'numeric-reserve-station')
        assert job['outcome'] == 'succeeded'
        assert job['checkpoint']['confirmed'][0]['evidence']['reserve']['usable_s'] == 65
        assert job['checkpoint']['confirmed'][0]['evidence']['reserve']['required_s'] == 11
        assert executor.status()['device']['reserve']['usable_s'] == 64
    finally:
        executor.close()
