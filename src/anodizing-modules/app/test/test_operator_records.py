"""Offline recorder behavior through the worker, native event producers and HTTP."""
from concurrent.futures import ThreadPoolExecutor
import csv
from io import StringIO
import json
from threading import Event, RLock
from time import monotonic, sleep
from types import SimpleNamespace

import pytest

from anodizing_web import agv, operator_records, operator_state, server, tool_power
from anodizing_web.jobs import JobOwner
from test_operator import request
from test_agv_handler import open_simulator, send, session


def until(check):
    deadline = monotonic() + 4
    while not check():
        assert monotonic() < deadline, 'Recorder worker did not reach the expected state'
        sleep(.005)


def exported(records, ident, format='json'):
    stream = StringIO()
    records.export(ident, format, stream)
    return stream.getvalue()


def document(records, ident):
    return json.loads(exported(records, ident))


def test_recorder_selects_and_exports_cached_module_evidence(tmp_path, monkeypatch):
    owner = JobOwner(tmp_path, allow_manual_pick_place=True)
    link = agv.Connection(owner)

    def forbid(*args, **kwargs):
        pytest.fail('Recorder attempted device access.')

    monkeypatch.setattr(link, '_receive', forbid)
    monkeypatch.setattr(link, '_send', forbid)
    monkeypatch.setattr(tool_power.socket, 'create_connection', forbid)
    stamp = '2026-01-01T00:00:00.000+00:00'
    owner.active = 'recipe-example'
    owner.jobs = {
        'recipe-example': dict(id='recipe-example', module='manual-pick-place',
                               state='running', created=3, manual_updated_at=4,
                               manual={'stage': 'moving', 'repeat': {'phase': 'place', 'completed_transfers': 2}}),
        'mcu-example': dict(id='mcu-example', module='mcu', state='done', created=2, finished=3,
                            mcu={'rx_text': 'FSR1:1/FSR2:2', 'status': 'replied', 'observed_at': 1}),
        'mcu-later': dict(id='mcu-later', module='mcu', state='failed', created=3, finished=4,
                          mcu={'rx_text': 'partial', 'status': 'unknown', 'observed_at': None}),
        'power-example': dict(id='power-example', module='tool-power', state='done', created=1, finished=3,
                              tool_power={'controller_tool_output_voltage': 0, 'observed_at': 1, 'status': 'observed'}),
        'power-later': dict(id='power-later', module='tool-power', state='failed', created=3, finished=4,
                            tool_power={'controller_tool_output_voltage': None, 'observed_at': None, 'status': 'unknown'}),
        'fit-example': dict(id='fit-example', module='calibration', state='done', created=1, finished=2,
                            result={'path': 'fit.json', 'calibration_sha256': 'a' * 64,
                                    'result': {'type': 'eye_in_hand'}}),
        'preview-example': dict(id='preview-example', module='calibration', state='done', created=2, finished=3,
                                result={'snapshot': {'id': 'board-frame', 'acquired_at': stamp}}),
        'teach-example': dict(id='teach-example', module='teaching', state='done', created=1, finished=2,
                              result={'project': {'project': 'training-project', 'flow': {'taught': False},
                                                  'snapshots': {'reference': {'id': 'target-frame', 'acquired_at': stamp}}}}),
    }
    instance = SimpleNamespace(owner=owner, agv=link)
    records = operator_records.Recorder(tmp_path, lambda: operator_state.server_state(instance))
    expected = {
        'job.phase': ('place', 'recipe-example'),
        'job.transfers_done': (2, 'recipe-example'),
        'mcu.reply': ('FSR1:1/FSR2:2', 'mcu-example'),
        'mcu.command_outcome': ('unknown', 'mcu-later'),
        'tool_power.controller_tool_output_voltage': (0, 'power-example'),
        'tool_power.readback_outcome': ('unknown', 'power-later'),
        'calibration.calibration_sha256': ('a' * 64, 'fit-example'),
        'calibration.snapshot_id': ('board-frame', 'board-frame'),
        'teaching.capture_id': ('target-frame', 'target-frame'),
        'teaching.taught': (False, 'teach-example'),
        'teaching.tank': (None, 'teach-example'),
    }
    try:
        ident = records.start({'rate_hz': 5, 'variables': list(expected)})['id']
        until(lambda: (records.directory / ident / 'rows.jsonl').stat().st_size > 0)
        records.stop(ident)
    finally:
        records.close()
    saved = document(records, ident)
    assert {v['id'] for v in saved['manifest']['variables']} == expected.keys()
    variables = {v['id']: v for v in saved['rows'][0]['variables']}
    assert {key: (item['value'], item['row_id']) for key, item in variables.items()} == expected
    assert variables['calibration.snapshot_id']['job_id'] == 'preview-example'
    assert variables['calibration.snapshot_id']['read_at'] == stamp
    assert variables['teaching.capture_id']['read_at'] == stamp
    assert variables['tool_power.controller_tool_output_voltage']['quality'] == 'historical'
    assert variables['tool_power.controller_tool_output_voltage']['read_at'] == '1970-01-01T00:00:01.000+00:00'
    assert variables['teaching.tank']['quality'] == 'unknown'
    csv_rows = {row['id']: row for row in csv.DictReader(StringIO(exported(records, ident, 'csv')))}
    assert csv_rows['teaching.capture_id']['job_id'] == 'teach-example'


@pytest.fixture
def recording(tmp_path, monkeypatch):
    clock = [10.0]
    monkeypatch.setattr(operator_records, 'monotonic', lambda: clock[0])
    monkeypatch.setattr(agv, 'monotonic', lambda: clock[0])
    owner = SimpleNamespace(lock=RLock(), active=None)
    link = agv.Connection(owner)
    for name in ('_receive', '_write', 'capability', 'motion_feedback'):
        monkeypatch.setattr(link, name, lambda *args, **kwargs: pytest.fail('Recorder touched the AGV'))
    monkeypatch.setattr(agv, 'serial_ports', lambda: pytest.fail('Recorder enumerated ports'))
    records = operator_records.Recorder(tmp_path, lambda: operator_state.read(link))
    yield records, link, clock
    records.close()


@pytest.mark.parametrize('rate', [.2, 1, 5])
def test_worker_cadence_and_projected_quality_without_a_browser(recording, rate, tmp_path):
    records, link, clock = recording
    period = 1 / rate
    session = records.start({'rate_hz': rate, 'variables': ['agv.speed']})
    path = tmp_path / 'records' / session['id'] / 'rows.jsonl'

    def samples():
        return [row for line in path.read_bytes().splitlines(keepends=True) if line.endswith(b'\n')
                if (row := json.loads(line))['type'] == 'sample']

    until(lambda: len(samples()) == 1)
    assert samples()[0]['variables'][0]['quality'] == 'unknown'
    assert samples()[0]['variables'][0]['value'] is None
    link.transport = object()
    link.settings = {'mode': 'simulator'}
    link.connected_at = clock[0]
    with link.lock:
        link._status_received({'actual_speed_m_h': 500}, b'')
    stamp = link.status_iso
    clock[0] = 10 + period - .01
    with link.lock:
        link.status_at = clock[0]
    records.event('test', 'wake')
    sleep(.025)
    assert len(samples()) == 1
    clock[0] = 10 + period
    records.event('test', 'wake')
    until(lambda: len(samples()) == 2)
    assert samples()[1]['variables'][0]['value'] == 500
    clock[0] = 10 + period + 5
    records.event('test', 'wake')
    until(lambda: len(samples()) == 3)
    records.stop(session['id'])
    until(lambda: records.catalog()['active'] is None)
    stale = document(records, session['id'])['rows']
    stale = [row for row in stale if row['type'] == 'sample'][-1]['variables'][0]
    assert (stale['quality'], stale['value'], stale['read_at']) == ('stale', None, stamp)
    assert stale['age_ms'] >= 5000


def test_native_events_are_correlated_and_exports_keep_notes_and_audit(recording, tmp_path):
    records, link, _ = recording
    owner = JobOwner(tmp_path)
    owner.record_event = link.record_event = records.event
    session = records.start({'rate_hz': 1, 'variables': ['agv.speed'],
                             'operator': 'Fixture operator', 'notes': 'Synthetic test'})
    link.id = 3
    link.settings = {'mode': 'simulator'}
    with link.lock:
        tx = link._record('TX', b'\x25', {'command': 'query_status', 'try': 1})
        link._record('RX', b'\x26', {})
        link.results['query_status'] = {'tx_id': tx['id']}
        link.reading, link.read_sent_at = 'query_status', 9
        link._finish_read('status received')
        link._event('link_stale', False, True)
        link._event('link_stale', True, False)
    job = owner.submit('fixture-job', 'calibration', {}, lambda _: {'synthetic': True})
    until(lambda: owner.active is None)
    records.stop(session['id'])
    until(lambda: records.catalog()['active'] is None)
    data = document(records, session['id'])
    events = [row for row in data['rows'] if row['type'] == 'event']
    outcome = next(e for e in events if e['event'] == 'command_outcome')
    assert outcome['connection_id'] == 3 and outcome['tx_id'] == tx['id']
    assert {e['direction'] for e in events if e['event'] == 'frame'} == {'TX', 'RX'}
    assert [e['after'] for e in events if e['event'] == 'link_stale'] == [True, False]
    assert [e['state'] for e in events if e.get('job_id') == job['id']] == ['running', 'done']
    assert all(e['request_id'] == job['id'] for e in events if e.get('job_id') == job['id'])
    assert owner.get(job['id'])['result'] == {'synthetic': True}
    assert data['manifest']['notes'] == 'Synthetic test'
    summary = exported(records, session['id'], 'summary')
    assert summary.startswith('| Recording | Outcome |') and 'Synthetic test' in summary


def test_csv_reports_quality_and_original_timestamps(recording):
    records, link, _ = recording
    link.settings, link.transport = {'mode': 'simulator'}, object()
    with link.lock:
        link._status_received({'actual_speed_m_h': 500}, b'')
    session = records.start({'rate_hz': 1, 'variables': ['agv.speed']})
    directory = records.directory / session['id']
    until(lambda: (directory / 'rows.jsonl').stat().st_size > 0)
    records.stop(session['id'])
    until(lambda: records.catalog()['active'] is None)
    row, = csv.DictReader(StringIO(exported(records, session['id'], 'csv')))
    assert row['read_at'] == link.status_iso and row['sample_at']
    assert (row['id'], row['quality'], row['value'], row['unit']) == ('agv.speed', 'fresh', '500', 'm/h')


@pytest.mark.parametrize('limit,reason', [('MAX_SECONDS', 'time_limit'), ('MAX_BYTES', 'size_limit')])
def test_caps_reserve_a_terminal_row(recording, monkeypatch, limit, reason):
    records, _, clock = recording
    monkeypatch.setattr(operator_records, limit, 2 if limit == 'MAX_SECONDS' else 4096)
    session = records.start({'rate_hz': 5, 'variables': ['agv.speed']})
    ident = session['id']
    if reason == 'time_limit':
        clock[0] += 2
        records.event('test', 'wake')
    else:
        for _ in range(20):
            records.event('fixture', 'details', value='x' * 300)
    until(lambda: records.catalog()['active'] is None)
    rows = document(records, ident)['rows']
    assert rows[-1]['type'] == 'terminal' and rows[-1]['reason'] == reason
    assert sum(path.stat().st_size for path in (records.directory / ident).iterdir()) <= operator_records.MAX_BYTES


def test_append_failure_ends_visibly_and_keeps_complete_rows(recording, monkeypatch):
    records, _, clock = recording
    append = operator_records.Recording.append

    def short_write(self, row):
        if row['type'] == 'event':
            self.stream.write(b'{"type":')
            raise OSError('synthetic disk failure')
        return append(self, row)

    monkeypatch.setattr(operator_records.Recording, 'append', short_write)
    session = records.start({'rate_hz': 1, 'variables': ['agv.speed']})
    until(lambda: (records.directory / session['id'] / 'rows.jsonl').stat().st_size > 0)
    clock[0] += 1
    records.event('fixture', 'failure')
    until(lambda: records.catalog()['active'] is None)
    data = document(records, session['id'])
    assert data['outcome']['reason'] == 'storage_error'
    assert [row['type'] for row in data['rows']][0] == 'sample'
    assert data['rows'][-1]['reason'] == 'storage_error'


def test_restart_preserves_complete_rows_and_marks_partial_record_interrupted(recording, tmp_path):
    records, _, _ = recording
    session = records.start({'rate_hz': 1, 'variables': ['agv.speed']})
    records.stop(session['id'])
    until(lambda: records.catalog()['active'] is None)
    path = records.directory / session['id'] / 'rows.jsonl'
    complete = path.read_bytes().splitlines(keepends=True)[:-1]
    path.write_bytes(b''.join(complete) + b'{"incomplete":')
    restarted = operator_records.Recorder(tmp_path, records.read_state)
    data = document(restarted, session['id'])
    assert data['rows'][:-1] == [json.loads(row) for row in complete]
    assert data['rows'][-1]['reason'] == 'interrupted'
    assert restarted.catalog()['active'] is None


def test_one_recording_at_a_time_and_stop_is_bound_to_its_id(recording):
    records, _, _ = recording
    release = Event()
    settings = {'rate_hz': 1, 'variables': ['agv.speed']}

    def start():
        release.wait()
        try:
            return records.start(settings)
        except ValueError:
            return None

    with ThreadPoolExecutor(max_workers=2) as threads:
        attempts = [threads.submit(start) for _ in range(2)]
        release.set()
        session, = [result for attempt in attempts if (result := attempt.result())]
    with pytest.raises(ValueError):
        records.stop('0' * 32)
    with pytest.raises(ValueError):
        records.export(session['id'], 'json', StringIO())
    assert records.catalog()['active']['id'] == session['id']


@pytest.mark.parametrize('settings', [
    {'rate_hz': True, 'variables': ['agv.speed']},
    {'rate_hz': 2, 'variables': ['agv.speed']},
    {'rate_hz': 1, 'variables': ['agv.speed', 'agv.speed']},
    {'rate_hz': 1, 'variables': ['agv.unknown']},
    {'rate_hz': 1, 'variables': ['agv.speed'], 'notes': 'x' * 2001},
])
def test_invalid_manifests_are_refused(recording, settings):
    records, _, _ = recording
    with pytest.raises(ValueError):
        records.start(settings)
    assert not records.catalog()['records']


def test_records_http_keeps_loopback_token_and_download_policy(recording):
    records, link, _ = recording
    service = SimpleNamespace(records=records, agv=link, owner=link.owner, token='secret',
                              hosts={'127.0.0.1:8870'})
    assert request(service, '/api/records', token='old')[0] == 403
    assert request(service, '/api/records', origin='http://localhost:8870')[0] == 403
    assert request(service, '/api/records', host='site.invalid')[0] == 403
    assert request(service, '/api/records', method='POST', body={
        'action': 'start', 'rate_hz': 1, 'variables': ['agv.speed']}, token='old')[0] == 403
    status, _, payload = request(service, '/api/records', method='POST',
                                 body={'action': 'start', 'rate_hz': 1, 'variables': ['agv.speed']})
    assert status == 201
    ident = json.loads(payload)['id']
    assert json.loads(request(service, '/api/records')[2])['active']['id'] == ident
    assert request(service, f'/api/records/{ident}/json')[0] == 409
    assert request(service, '/api/records', method='POST', body={'action': 'stop', 'id': ident})[0] == 200
    until(lambda: records.catalog()['active'] is None)
    status, headers, payload = request(service, f'/api/records/{ident}/json')
    assert status == 200 and headers['Cache-Control'] == 'no-store'
    assert headers['Content-Disposition'] == f'attachment; filename="record-{ident}.json"'
    assert int(headers['Content-Length']) == len(payload)
    assert json.loads(payload)['outcome']['reason'] == 'stopped'
    assert request(service, '/api/records/' + '0' * 32 + '/json')[0] == 404


def test_a_stalled_disk_does_not_block_event_producers_and_overflow_ends_recording(recording, monkeypatch):
    records, link, _ = recording
    append = operator_records.Recording.append
    stalled, resume = Event(), Event()

    def stall_sample(self, row):
        if row['type'] == 'sample':
            stalled.set()
            assert resume.wait(4)
        return append(self, row)

    monkeypatch.setattr(operator_records.Recording, 'append', stall_sample)
    monkeypatch.setattr(operator_records, 'MAX_EVENTS', 2)
    link.record_event = records.event
    session = records.start({'rate_hz': 1, 'variables': ['agv.speed']})

    def frames():
        with link.lock:
            for i in range(3):
                link._record('RX', bytes([i]), {})

    try:
        assert stalled.wait(2)
        with ThreadPoolExecutor(max_workers=1) as thread:
            sent = thread.submit(frames)
            try:
                sent.result(timeout=1)
            finally:
                resume.set()
    finally:
        resume.set()
    until(lambda: records.catalog()['active'] is None)
    data = document(records, session['id'])
    assert data['outcome']['reason'] == 'event_overflow'
    assert len([row for row in data['rows'] if row['type'] == 'event']) == 2


def test_shutdown_finishes_and_restart_keeps_the_outcome(recording):
    records, _, _ = recording
    session = records.start({'rate_hz': 1, 'variables': ['agv.speed']})
    records.close()
    reopened = operator_records.Recorder(records.directory.parent, records.read_state)
    assert document(reopened, session['id'])['outcome']['reason'] == 'shutdown'


def test_symlink_storage_keeps_recorder_unavailable_without_writing_outside(tmp_path):
    external = tmp_path / 'external'
    external.mkdir()
    project = tmp_path / 'data'
    project.mkdir()
    (project / 'records').symlink_to(external, target_is_directory=True)
    state = {'variables': [{'id': 'agv.speed', 'name': 'Speed'}]}
    records = operator_records.Recorder(project, lambda: state)
    assert records.catalog()['error'] == operator_records.STORAGE_ERROR
    with pytest.raises(ValueError, match='Check space and permissions'):
        records.start({'rate_hz': 1, 'variables': ['agv.speed']})
    assert not list(external.iterdir())


@pytest.mark.parametrize('damage', ['row', 'earlier_row', 'manifest', 'variable'])
def test_recovery_lists_unreadable_sessions_and_preserves_their_bytes(recording, tmp_path, damage):
    records, _, _ = recording
    settings = {'rate_hz': 1, 'variables': ['agv.speed']}
    healthy = records.start(settings)['id']
    records.stop(healthy)
    until(lambda: records.catalog()['active'] is None)
    damaged = records.start(settings)['id']
    records.stop(damaged)
    until(lambda: records.catalog()['active'] is None)
    path = records.directory / damaged / ('rows.jsonl' if damage in ('row', 'earlier_row') else 'manifest.json')
    if damage == 'row':
        raw = b''.join(path.read_bytes().splitlines(keepends=True)[:-1]) + b'[1]\n'
    elif damage == 'earlier_row':
        raw = b'[1]\n' + path.read_bytes()
    elif damage == 'manifest':
        raw = b'[]'
    else:
        raw = json.dumps(dict(json.loads(path.read_bytes()), variables=[1])).encode()
    path.write_bytes(raw)

    restarted = operator_records.Recorder(tmp_path, records.read_state)
    by_id = {item['id']: item for item in restarted.catalog()['records']}
    assert by_id[damaged]['status'] == 'unreadable'
    assert by_id[healthy]['status'] == 'finished'
    assert restarted.catalog()['error']
    assert path.read_bytes() == raw
    with pytest.raises(ValueError, match='unreadable'):
        document(restarted, damaged)


def test_start_captures_native_halts_while_manifest_persistence_is_stalled(tmp_path, monkeypatch, session):
    _, request_agv = session
    link = request_agv.connection
    connection_id = open_simulator(request_agv)
    assert send(request_agv, 'query_status', connection_id)[0] == 200
    records = operator_records.Recorder(tmp_path, lambda: operator_state.read(link),
                                        agv_context=link.recording_context)
    link.record_event = records.event
    entered, resume = Event(), Event()
    atomic_json = operator_records.atomic_json

    def stall_manifest(path, manifest):
        entered.set()
        assert resume.wait(4)
        return atomic_json(path, manifest)

    monkeypatch.setattr(operator_records, 'atomic_json', stall_manifest)
    with ThreadPoolExecutor(max_workers=2) as pool:
        start = pool.submit(records.start, {'rate_hz': 1, 'variables': ['agv.speed']})
        try:
            assert entered.wait(2)
            delivery = pool.submit(send, request_agv, 'stop', connection_id)
            status, state = delivery.result(timeout=1)
            assert status == 200 and state['urgent_command'] == 'pause'
            tx_id = state['results']['pause']['tx_id']
        finally:
            resume.set()
        ident = start.result(timeout=2)['id']
    records.stop(ident)
    until(lambda: records.catalog()['active'] is None)
    data = document(records, ident)
    assert data['manifest']['agv_at_start']['connection_id'] == connection_id
    assert any(row.get('tx_id') == tx_id for row in data['rows'] if row.get('direction') == 'TX')


def test_recorded_halt_evidence_distinguishes_stop_pause_and_joined_requests(tmp_path, session):
    _, request_agv = session
    link = request_agv.connection
    connection_id = open_simulator(request_agv)
    assert send(request_agv, 'query_status', connection_id)[0] == 200
    records = operator_records.Recorder(tmp_path, lambda: operator_state.read(link))
    link.record_event = records.event
    ident = records.start({'rate_hz': 1, 'variables': ['agv.speed']})['id']
    status, stop = send(request_agv, 'stop', connection_id)
    assert status == 200
    stop_tx = stop['results']['pause']['tx_id']
    status, joined = send(request_agv, 'stop', connection_id)
    assert status == 200 and joined['results']['pause']['tx_id'] == stop_tx
    status, pause = send(request_agv, 'pause', connection_id)
    assert status == 200
    pause_tx = pause['results']['pause']['tx_id']
    assert stop_tx != pause_tx
    records.stop(ident)
    until(lambda: records.catalog()['active'] is None)
    rows = document(records, ident)['rows']
    for action, tx in [('stop', stop_tx), ('pause', pause_tx)]:
        frame, = [row for row in rows if row.get('direction') == 'TX' and row.get('tx_id') == tx]
        assert (frame['requested_command'], frame['wire_command'], frame['connection_id']) == (
            action, 'pause', connection_id)
    joined, = [row for row in rows if row.get('event') == 'halt_request' and row.get('joined')]
    assert (joined['requested_command'], joined['wire_command'], joined['connection_id'], joined['tx_id']) == (
        'stop', 'pause', connection_id, stop_tx)
    outcome = next(row for row in rows if row.get('event') == 'command_outcome' and row.get('tx_id') == stop_tx)
    assert (outcome['requested_command'], outcome['wire_command'], outcome['connection_id']) == (
        'stop', 'pause', connection_id)
