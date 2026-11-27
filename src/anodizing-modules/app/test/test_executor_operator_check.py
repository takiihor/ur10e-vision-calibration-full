import time
import secrets
"""Operator checks exercise only fake hardware."""
from test_executor_phase1 import session, terminal, visit

from anodizing_web.executor import PROTOCOL, Executor, FakeDevices


def test_job_id_remains_consumed_and_restart_holds(tmp_path):
    executor = Executor(tmp_path, FakeDevices())
    identity = session(executor)
    request = visit(identity)
    request['params'].update(run_id='test-basket-run', basket='A')
    executor.submit(request)
    terminal(executor, request['job_id'])
    executor.close()

    restarted = Executor(tmp_path, FakeDevices())
    try:
        status = restarted.status(request['job_id'])
        assert status['operator_check']['job_id'] == request['job_id']
        assert not status['armed']
        assert restarted.submit(dict(request, boot=restarted.boot)) == status['job']
        assert restarted.devices.effects == []
    finally:
        restarted.close()


def test_missing_durable_journal_fences_and_keeps_evidence(tmp_path):
    import pytest

    executor = Executor(tmp_path, FakeDevices())
    request = visit(session(executor))
    executor.submit(request)
    terminal(executor, request['job_id'])
    executor.close()

    path = tmp_path / '.executor-journal' / '000000000000.json'
    path.unlink()
    restarted = Executor(tmp_path, FakeDevices())
    try:
        status = restarted.status()
        assert status['operator_check']['technician']
        assert '.executor-journal/000000000000.json' in status['operator_check']['reason']
        identity = lease(restarted)
        with pytest.raises(ValueError, match='technician'):
            restarted.control('arm', dict(identity, stop_generation=status['stop_generation']))
        assert not path.exists()
        assert restarted.devices.effects == []
    finally:
        restarted.close()


def test_operator_choice_fences_a_delayed_job_and_records_abort(tmp_path):
    import json
    import pytest

    executor = Executor(tmp_path, FakeDevices())
    try:
        reviewed = session(executor)
        old_job = visit(reviewed)
        status = executor.hold(dict(reviewed, reason='No clear dispatch reply', job_id=old_job['job_id']))
        current = dict(reviewed, stop_generation=status['stop_generation'])
        with pytest.raises(ValueError, match='Stop generation'):
            executor.operator_check(dict(reviewed, choice='abort', operator='reviewer', confirmed=True,
                                         basket=None, location='input', job_id=old_job['job_id']))
        result = executor.operator_check(dict(current, choice='abort', operator='reviewer', confirmed=True,
                                              basket=None, location='input', job_id=old_job['job_id']))
        assert not result['armed'] and result['operator_check'] is None
        executor.control('arm', current)
        with pytest.raises(ValueError, match='generation'):
            executor.submit(old_job)
        assert executor.devices.effects == []
        choices = [json.loads(path.read_text())['choice'] for path in executor.journal.glob('*.json')
                   if 'choice' in json.loads(path.read_text())]
        assert choices[0]['operator'] == 'reviewer' and choices[0]['choice'] == 'abort'
    finally:
        executor.close()


def test_shared_stop_id_is_confirmed_over_real_control_http(tmp_path):
    from types import SimpleNamespace

    from anodizing_web.executor_http import Client, control_listener
    from anodizing_web.executor_link import ExecutorLink
    from test_executor_phase1 import running, wait_for

    remote = Executor(tmp_path / 'ipc', FakeDevices())
    local = SimpleNamespace(owner=SimpleNamespace(root=tmp_path), commissioning=lambda **_: {})
    token = secrets.token_urlsafe(32)
    try:
        listener = control_listener(0, remote, token)
        with running(listener):
            client = Client(listener.server_port, token)
            stale = True

            def transport(operation, body=None):
                if stale and operation == 'stop':
                    return dict(stop_id=body['stop_id'], boot='old-executor-boot', generation=1,
                                received=True, halt_attempted=True, confirmed=True)
                return client(operation, body)

            link = ExecutorLink(transport, local)
            try:
                wait_for(lambda: link.identity, bool)
                stop = {'stop_id': 'programme-shared-stop'}
                assert not link.stop(stop)['confirmed']
                assert link.status()['stop_unconfirmed']
                stale = False
                receipt = wait_for(lambda: link.stop(stop), lambda report: report['confirmed'])
                assert receipt['stop_id'] == stop['stop_id']
                assert receipt['boot'] == remote.boot
                assert receipt == remote.status()['stop']
            finally:
                link.close()
    finally:
        remote.close()


def lease(executor):
    identity = dict(session='operator-restart', boot=executor.boot, protocol=PROTOCOL)
    executor.heartbeat(dict(identity, sequence=1))
    executor.control('connect', identity)
    return identity


def test_wire_halt_precedes_release_of_blocked_station_trace(tmp_path, monkeypatch):
    monkeypatch.setenv('ANODIZING_MANUAL_TRAVEL_UNTIL', str(__import__('time').time() + 3600))
    import json
    import time
    from threading import Event
    from types import SimpleNamespace

    from anodizing_web import agv
    from anodizing_web.executor_http import WiredDevices

    logged, release, halted = Event(), Event(), Event()
    prefix = bytes.fromhex('42 19 0D')
    trace_frames, wire_frames = [], []

    def trace(direction, raw):
        if direction == 'TX':
            frame = agv.parse_frame(raw)
            if frame.command == 0x23 and frame.data[0] == 0x9D:
                logged.set()
                assert release.wait(5)
            trace_frames.append(raw)

    class Wire(agv.Simulator):
        def __init__(self, **_):
            super().__init__(prefix)
            self.arrival = True

        def write(self, raw):
            frame = agv.parse_frame(raw)
            wire_frames.append(raw)
            if frame.command == 0x23 and frame.data[0] in (0x87, 0x5B):
                halted.set()
            return super().write(prefix + raw) - len(prefix)

    (tmp_path / 'site-profile.json').write_text(json.dumps({'agv': {'wired_port': '/dev/test-wire'}}))
    monkeypatch.setattr(agv, 'serial', SimpleNamespace(Serial=Wire))
    monkeypatch.setattr(agv, 'TIMED_REPORT_LISTEN_S', 0)
    monkeypatch.setattr(agv, 'RECIPE_POLL_S', .2)
    devices = WiredDevices(tmp_path, trace, pose=time.monotonic, reserve=lambda: True, tank_clear=time.monotonic)
    executor = Executor(tmp_path, devices)
    try:
        identity = lease(executor)
        executor.control('status_read', identity)
        request = visit(dict(identity, stop_generation=0))
        request['attestation'] = dict(id=request['job_id'], station=2, confirmed_at=time.time())
        executor.control('arm', request)
        executor.submit(request)
        assert logged.wait(4)
        executor.stop({'stop_id': 'wire-priority-regression'})
        assert halted.wait(.3)
    finally:
        release.set()
        executor.close()
    assert trace_frames == wire_frames


def test_halt_writes_during_blocked_read(tmp_path, monkeypatch):
    import json
    import time
    from threading import Event, Thread
    from types import SimpleNamespace

    from anodizing_web import agv
    from anodizing_web.executor_http import WiredDevices

    entered, release, halted = Event(), Event(), Event()
    prefix = bytes.fromhex('42 19 0D')
    trigger = Event()

    class Wire(agv.Simulator):
        def __init__(self, **_):
            super().__init__(prefix)

        def read(self, size):
            if trigger.is_set() and not entered.is_set():
                entered.set()
                assert release.wait(5)
            return super().read(size)

        def write(self, raw):
            frame = agv.parse_frame(raw)
            if frame.command == 0x23 and frame.data[0] in (0x87, 0x5B):
                halted.set()
            return super().write(prefix + raw) - len(prefix)

    (tmp_path / 'site-profile.json').write_text(json.dumps({'agv': {'wired_port': '/dev/test-wire'}}))
    monkeypatch.setattr(agv, 'serial', SimpleNamespace(Serial=Wire))
    monkeypatch.setattr(agv, 'TIMED_REPORT_LISTEN_S', 0)
    devices = WiredDevices(tmp_path, None, pose=time.monotonic, reserve=lambda: True, tank_clear=time.monotonic)
    executor = Executor(tmp_path, devices)
    reader = None
    try:
        devices.connect()
        devices.read()
        trigger.set()
        reader = Thread(target=devices.read, daemon=True)
        reader.start()
        assert entered.wait(3)
        executor.stop({'stop_id': 'blocked-read-probe'})
        assert halted.wait(.4)
    finally:
        release.set()
        if reader:
            reader.join(3)
        executor.close()


def test_stop_during_pending_snapshot_prevents_visit(tmp_path):
    from threading import Event

    entered, release, halted = Event(), Event(), Event()
    devices = FakeDevices()
    persist, stop = devices._persist, devices.stop

    def blocked(*, pending=False):
        if pending:
            entered.set()
            assert release.wait(5)
        return persist(pending=pending)

    def stopping():
        result = stop()
        halted.set()
        return result

    devices._persist = blocked
    devices.stop = stopping
    executor = Executor(tmp_path, devices)
    try:
        identity = session(executor)
        request = visit(identity)
        executor.submit(request)
        assert entered.wait(3)
        executor.stop({'stop_id': 'persist-stall'})
        assert halted.wait(2)
        release.set()
        assert terminal(executor, request['job_id'])['outcome'] == 'unknown'
        assert not devices.effects
    finally:
        release.set()
        executor.close()


def test_busy_refusal_consumes_the_id(tmp_path):
    from threading import Event

    import pytest

    devices = FakeDevices()
    entered, release = Event(), Event()
    perform = devices.perform

    def slow(effect, value):
        if effect == 'station' and value == 2:
            entered.set()
            assert release.wait(5)
        return perform(effect, value)

    devices.perform = slow
    executor = Executor(tmp_path, devices)
    try:
        identity = session(executor)
        first = visit(identity, 'station-job-a', 2)
        busy = visit(identity, 'station-job-b', 3)
        executor.submit(first)
        assert entered.wait(2)
        with pytest.raises(ValueError, match='busy'):
            executor.submit(busy)
        refusal = executor.status(busy['job_id'])['job']
        release.set()
        assert terminal(executor, first['job_id'])['outcome'] == 'succeeded'
        assert refusal['state'] == 'refused'
        assert executor.submit(busy) == refusal
        assert devices.effects == [('station', 2)]
    finally:
        release.set()
        executor.close()


def test_missing_fake_snapshot_fences_before_next_visit(tmp_path):
    import pytest

    path = tmp_path / '.fake-devices.json'
    devices = FakeDevices(state_path=path)
    executor = Executor(tmp_path, devices)
    try:
        identity = session(executor)
        executor.submit(visit(identity, 'first-job', 2))
        assert terminal(executor, 'first-job')['outcome'] == 'succeeded'
        path.unlink()
        with pytest.raises(ValueError, match=r'\.fake-devices.json'):
            executor.submit(visit(identity, 'second-job', 3))
        assert not path.exists()
        assert devices.effects == [('station', 2)]
        assert executor.status()['operator_check']['technician']
    finally:
        executor.close()


def test_manual_confirmation_uses_settled_station(tmp_path):
    from threading import Event

    import pytest
    from test_executor_phase1 import wait_for

    devices = FakeDevices()
    entered, release = Event(), Event()
    physical_visit = devices.agv.visit

    def settle_at_destination(station):
        entered.set()
        assert release.wait(5)
        return physical_visit(station)

    devices.agv.visit = settle_at_destination
    executor = Executor(tmp_path, devices)
    try:
        reviewed = session(executor)
        request = visit(reviewed)
        executor.submit(request)
        assert entered.wait(2)
        executor.hold(dict(reviewed, reason='Lost station result', manual=True))
        release.set()
        terminal(executor, request['job_id'])
        status = wait_for(executor.status, lambda state: state['stop']['confirmed'])
        assert status['operator_check']['location'] == 'station 2'
        assert status['operator_check']['earlier_location'] == 'station 0'
        decision = dict(reviewed, stop_generation=status['stop_generation'], choice='abort',
                        operator='reviewer', confirmed=True, basket=None, job_id=request['job_id'])
        with pytest.raises(ValueError, match='station'):
            executor.operator_check(dict(decision, location='station 0'))
        assert executor.operator_check(dict(decision, location='station 2'))['operator_check'] is None
    finally:
        release.set()
        executor.close()


def test_transient_event_tip_write_opens_releasable_check(tmp_path, monkeypatch):
    import pytest
    from test_executor_phase1 import wait_for

    from anodizing_web import executor as ipc

    executor = Executor(tmp_path, FakeDevices())
    write = ipc.atomic_json
    failed = False

    def fail_after_publication(path, body, **kwargs):
        nonlocal failed
        write(path, body, **kwargs)
        if path == executor.journal_tip and body['count'] == 1 and not failed:
            failed = True
            raise OSError('transient fsync failure')

    monkeypatch.setattr(ipc, 'atomic_json', fail_after_publication)
    try:
        reviewed = session(executor)
        request = visit(reviewed)
        with pytest.raises(OSError, match='transient fsync'):
            executor.submit(request)
        status = wait_for(executor.status, lambda state: bool((state.get('stop') or {}).get('confirmed')))
        assert failed
        assert not status['operator_check']['technician']
        assert '.executor-journal-tip.json' in status['operator_check']['reason']
        assert executor.devices.effects == []
        decision = dict(reviewed, stop_generation=status['stop_generation'], choice='abort',
                        operator='reviewer', confirmed=True, basket=None, location=status['operator_check']['location'],
                        job_id=request['job_id'])
        assert executor.operator_check(decision)['operator_check'] is None
    finally:
        executor.close()


def test_aborted_park_survives_lease_expiry_and_allows_reviewed_input(tmp_path):
    import pytest
    from test_executor_phase1 import Clock, FAKE_TANK_SETPOINTS
    from test_executor_r3_ipc import dip, plant

    clock = Clock()
    devices = FakeDevices(clock=clock, setpoints=FAKE_TANK_SETPOINTS)
    executor = Executor(tmp_path, devices, clock=clock)
    try:
        reviewed = session(executor)
        first = dip(reviewed)
        plant(executor, first)
        executor.submit(first)
        assert terminal(executor, first['job_id'])['outcome'] == 'succeeded'
        identity = first['params']
        held = executor.hold(dict(reviewed, reason='Abort parked basket'))
        executor.operator_check(dict(reviewed, stop_generation=held['stop_generation'], choice='abort',
                                     operator='reviewer', confirmed=True, location='park',
                                     job_id=first['job_id'], run_id=identity['run_id'], basket=identity['basket']))
        assert devices.hold()['location'] == 'park'
        clock.now += executor.lease_s + 1
        newer = dict(reviewed, session='next-session')
        executor.heartbeat(dict(newer, sequence=1))
        status = executor.status()
        assert status['operator_check'] is None
        newer['stop_generation'] = status['stop_generation']
        executor.control('arm', newer)
        new_visit = visit(newer, 'next-basket-visit')
        new_visit['params'].update(basket='B', run_id='new-run', load_input=True)
        with pytest.raises(ValueError, match='available input'):
            executor.submit(new_visit)
        status = executor.status()
        assert status['operator_check']['basket'] == identity['basket']
        decision = dict(newer, stop_generation=status['stop_generation'], choice='resume', step=1,
                        operator='reviewer', confirmed=True, basket='B', location='input',
                        run_id='new-run', job_id=new_visit['job_id'])
        assert executor.operator_check(decision)['operator_check'] is None
        assert devices.hold()['location'] == 'input'
        assert devices.hold()['basket'] == 'B'
    finally:
        executor.close()


def test_resume_after_ipc_restart_marks_unmeasured_exposure_for_inspection(tmp_path):
    from test_executor_phase1 import Clock, FAKE_TANK_SETPOINTS
    from test_executor_r3_ipc import dip, plant

    clock = Clock()
    path = tmp_path / '.fake-devices.json'
    devices = FakeDevices(clock=clock, setpoints=FAKE_TANK_SETPOINTS, state_path=path)
    executor = Executor(tmp_path, devices, clock=clock)
    try:
        first = dip(session(executor))
        first['params']['steps'][0]['limits']['air'] = 2
        plant(executor, first)
        executor.submit(first)
        assert terminal(executor, first['job_id'])['outcome'] == 'succeeded'
    finally:
        executor.close()

    clock.now += 10
    devices = FakeDevices(clock=clock, setpoints=FAKE_TANK_SETPOINTS, state_path=path)
    executor = Executor(tmp_path, devices, clock=clock)
    try:
        reviewed = lease(executor)
        status = executor.status()
        reviewed['stop_generation'] = status['stop_generation']
        params = first['params']
        decision = dict(reviewed, choice='resume', step=2, operator='reviewer', confirmed=True,
                        basket=params['basket'], location='park', run_id=params['run_id'], job_id=first['job_id'])
        assert executor.operator_check(decision)['operator_check'] is None
        executor.control('arm', reviewed)
        next_step = dict(params['steps'][0], id='next-step')
        request = dict(reviewed, job_id='resumed-dip', type='dip', version=1,
                       params=dict(params, steps=[next_step], source='park', hold='output'))
        executor.submit(request)
        second = terminal(executor, request['job_id'])
        previous = executor.status(first['job_id'])
        assert second['outcome'] == 'succeeded'
        assert second['result']['steps'][0]['incoming_air_s'] is None
        assert second['result']['steps'][0]['inspection']
        assert previous['job']['result']['steps'][0]['air_s'] is None
        assert previous['job']['result']['steps'][0]['inspection']
        assert previous['quality_alarm']['measured_air_s'] is None
    finally:
        executor.close()


def test_transient_wire_write_retries_and_leaves_operator_check(tmp_path, monkeypatch):
    import json
    from threading import Event
    from time import monotonic
    from types import SimpleNamespace

    import pytest
    from anodizing_web import agv
    from anodizing_web.executor_http import WiredDevices
    from test_executor_phase1 import wait_for

    prefix = bytes.fromhex('42 19 0D')
    fail, unblock = Event(), Event()
    retried = []

    class Wire(agv.Simulator):
        def __init__(self, **_):
            super().__init__(prefix)
            self.arrival = True

        def write(self, raw):
            return super().write(prefix + raw) - len(prefix)

    def trace(direction, raw):
        if fail.is_set() and not unblock.is_set():
            retried.append((direction, raw))
            raise OSError('temporary wire flush failure')

    (tmp_path / 'site-profile.json').write_text(json.dumps({'agv': {'wired_port': '/dev/test-wire'}}))
    monkeypatch.setattr(agv, 'serial', SimpleNamespace(Serial=Wire))
    monkeypatch.setattr(agv, 'TIMED_REPORT_LISTEN_S', 0)
    devices = WiredDevices(tmp_path, trace, pose=monotonic, reserve=lambda: True, tank_clear=monotonic)
    executor = Executor(tmp_path, devices)
    try:
        identity = lease(executor)
        fail.set()
        executor.control('status_read', identity)
        status = wait_for(executor.status, lambda state: state['operator_check'])
        assert 'agv-wire.jsonl' in status['operator_check']['reason']
        assert not status['operator_check']['technician']
        decision = dict(identity, stop_generation=status['stop_generation'], choice='abort',
                        operator='reviewer', confirmed=True, basket=None, location='station 1')
        with pytest.raises(ValueError, match='finish writing'):
            executor.operator_check(decision)
        unblock.set()
        wait_for(lambda: devices.journal_write_pending, lambda pending: not pending)
        wait_for(executor.status, lambda state: state['stop']['confirmed'])
        assert executor.operator_check(decision)['operator_check'] is None
        assert devices.journal_error is None
        assert len(retried) >= 1
    finally:
        unblock.set()
        executor.close()


def test_stop_corroboration_requires_post_halt_feedback_from_covering_ticket():
    from anodizing_web.executor_http import stop_corroborated

    earlier = dict(stop_id='earlier', boot='ipc-boot', generation=1,
                   received=True, halt_attempted=False, confirmed=True)
    current = dict(earlier, stop_id='later', generation=2, halt_attempted=True, halt_monotonic=10.0)
    device = dict(connected=True, stale=False, status_age_s=.2, status_monotonic=9.9,
                  status=dict(stopped=True, actual_speed_m_h=0, obstacle_stop=False, fault_code=0))
    status = dict(stop=current, boot='ipc-boot', stop_generation=2, active_job_id=None,
                  armed=False, job=None, device=device)
    assert not stop_corroborated(status, current)
    device['status_monotonic'] = 10.1
    assert stop_corroborated(status, current)
    assert stop_corroborated(status, earlier)
    current['halt_attempted'] = False
    assert not stop_corroborated(status, earlier)
    assert not stop_corroborated(status, current)


def test_wired_abort_between_stations_uses_observed_location(tmp_path):
    import pytest
    from anodizing_web.executor_http import WiredDevices

    devices = WiredDevices(tmp_path, None)
    feedback = dict(current_station=2, target_station=3, stopped=True, actual_speed_m_h=0,
                    station_arrival=False, obstacle_stop=False, fault_code=0, mode=2, max_stations=3)
    devices.status = lambda: dict(connected=True, status_age_s=.01, stale=False, status=feedback)
    executor = Executor(tmp_path, devices)
    try:
        identity = dict(boot=executor.boot, session='review-controller', protocol=PROTOCOL)
        executor.heartbeat(dict(identity, sequence=1))
        held = executor.hold(dict(identity, manual=True, idle=True, reason='Interrupted station leg',
                                  stop_generation=executor.generation))
        choice = dict(identity, choice='abort', operator='reviewer', basket=None, confirmed=True,
                      stop_generation=held['stop_generation'])
        assert held['operator_check']['location'] == 'between stations 2 and 3'
        with pytest.raises(ValueError, match='conflicts'):
            executor.operator_check(dict(choice, location='station 2'))
        with pytest.raises(ValueError, match='Arrival'):
            devices.hold()
        cleared = executor.operator_check(dict(choice, location='between stations 2 and 3'))
        assert cleared['operator_check'] is None

        feedback.pop('target_station')
        held = executor.hold(dict(identity, manual=True, idle=True, reason='Location to confirm',
                                  stop_generation=executor.generation))
        assert held['operator_check']['location'] == 'between stations'
        cleared = executor.operator_check(dict(choice, location='between stations 2 and 3',
                                               stop_generation=held['stop_generation']))
        assert cleared['operator_check'] is None
    finally:
        executor.close()


def test_status_only_wired_feedback_cannot_confirm_unsent_halt(tmp_path):
    from time import monotonic

    import pytest

    from anodizing_web.executor_http import StopEvidenceError, WiredDevices, _validate_reply
    from test_executor_phase1 import wait_for

    devices = WiredDevices(tmp_path, None)
    executor = Executor(tmp_path, devices)
    try:
        receipt = executor.stop({'stop_id': 'status-only-stop'})
        wait_for(lambda: not executor.stop_workers, bool)
        with devices.link.lock:
            devices.link.transport = object()
            devices.link.settings = {'mode': 'wired'}
            devices.link.status_at = monotonic()
            devices.link.status = dict(stopped=True, actual_speed_m_h=0, obstacle_stop=False, fault_code=0)
        executor._confirm_stop()
        status = executor.status()
        assert status['stop'] == receipt
        assert not receipt['halt_attempted'] and not receipt['confirmed']
        assert 'halt_monotonic' not in receipt
        _validate_reply('status', status)
        status['stop'].update(confirmed=True, halt_monotonic=status['device']['status_monotonic'] - .1)
        with pytest.raises(StopEvidenceError):
            _validate_reply('status', status)
    finally:
        devices.link.transport = None
        executor.close()
