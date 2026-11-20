"""Stage 4 contracts using synthetic supplier shapes, fake time and no hardware."""
from datetime import datetime, timedelta, timezone
from io import StringIO
import json
from threading import Event, Thread
from types import SimpleNamespace

import pytest

from anodizing_web.operator_line import Line, ReadClient, parse_catalog
from anodizing_web.operator_records import write_csv
from test_operator import request, service


def tag(name, address, kind='Bool', access='R', **fields):
    return dict(tag_name=name, device_address=address, data_type=kind,
                read_write=access, description='Synthetic signal', multiplier=1, **fields)


TAGS = [
    tag('Remote', 'M1'), tag('Local', 'M2'), tag('Alarm01', 'M3'),
    tag('Tank21_HaveBasket', 'M4'), tag('Tank22_DoorOpened', 'M5'),
    tag('Tank21_WaterFlow', 'D6', 'Word'), tag('Tank22_WaterFlow', 'D6', 'Word'),
    tag('Tank21_TempPV', 'D7', 'Float'), tag('Tank21A_Command', 'M8', access='W'),
]
EPOCH = datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc)


class Fake:
    def __init__(self):
        self.sec = 0
        self.calls = []
        self.row = {}
        self.tags = TAGS

    def request(self, operation):
        self.calls.append(operation)
        return 200, {'tags': self.tags} if operation == 'list' else self.row

    def poll(self, line, ident, *, timestamp=None, **change):
        self.row = {'id': ident, 'timestamp': self.at().isoformat() if timestamp is None else timestamp,
                    'tags_ok': len(self.tags), 'tags_failed': 0,
                    **{tag['tag_name']: 0 for tag in self.tags},
                    'Tank21_HaveBasket': 1, 'Tank21_TempPV': 4.25, **change}
        line.step()
        return line.snapshot()

    def at(self):
        return EPOCH + timedelta(seconds=self.sec)


def line_source(fake, **settings):
    return Line('live', fake, clock=lambda: fake.sec, wall=fake.at,
                **{'clock_zone': 'UTC', 'clock_offset_s': 0, **settings})


def item(state, tag):
    return next(variable for variable in state['variables'] if variable['id'] == 'line.' + tag)


def test_progressing_rows_have_one_identity_and_repeat_never_renews_age():
    fake = Fake()
    line = line_source(fake)
    assert item(fake.poll(line, 1), 'Remote')['quality'] == 'unknown'
    fake.sec = 1
    fresh = item(fake.poll(line, 2), 'Tank21_TempPV')
    assert fresh['quality'] == 'fresh' and fresh['value'] == 4.25
    assert fresh['unit'] == '°C' and fresh['row_id'] == 2
    assert fresh['source_at'] == fake.at().isoformat() == fresh['read_at']
    fake.sec = 3
    fake.row['Tank21_TempPV'] = 99
    line.step()
    repeat = item(line.snapshot(), 'Tank21_TempPV')
    assert repeat['value'] == 4.25 and repeat['age_ms'] == 2000 and repeat['read_at'] == fresh['read_at']
    fake.sec = 4
    line.step()
    state = line.snapshot()
    assert item(state, 'api_reachable')['value'] is True
    expired = item(state, 'Tank21_TempPV')
    assert expired['quality'] == 'stale' and expired['value'] is None
    assert expired['last_read']['value'] == 4.25 and expired['read_at'] == fresh['read_at']
    assert item(fake.poll(line, 3), 'Remote')['quality'] == 'unknown'
    fake.sec = 5
    assert item(fake.poll(line, 4), 'Remote')['quality'] == 'fresh'


def test_partial_rows_keep_good_values_and_report_null_missing_and_invalid():
    fake = Fake()
    line = line_source(fake)
    fake.poll(line, 1)
    fake.sec = 1
    fake.poll(line, 2, Tank21_TempPV=None, Remote=2, tags_failed=3, tags_ok=6)
    del fake.row['Local']
    fake.sec = 2
    fake.row.update(id=3, timestamp=fake.at().isoformat())
    line.step()
    state = line.snapshot()
    assert item(state, 'Tank21_HaveBasket')['value'] == 1
    for name in ('Tank21_TempPV', 'Remote', 'Local'):
        assert item(state, name)['value'] is None and item(state, name)['quality'] == 'unknown'
        assert item(state, name)['reason']
    assert (state['line']['quality'], state['line']['tags_ok'], state['line']['tags_failed']) == ('partial', 6, 3)


@pytest.mark.parametrize('reported', [None, (99, 1), (7, 1), (7, 0), (7, None)])
def test_partial_row_qualifies_reported_counts_against_catalog_and_values(reported):
    fake = Fake()
    line = line_source(fake, tanks={'Tank21': {'have_basket': 'Tank21_HaveBasket'}})
    fake.poll(line, 1)
    fake.sec = 1
    fake.row.update(id=2, timestamp=fake.at().isoformat(), Tank21_TempPV=None, Remote=2)
    if reported is None:
        del fake.row['tags_ok'], fake.row['tags_failed']
    else:
        fake.row.update(zip(('tags_ok', 'tags_failed'), reported))
    line.step()
    state = line.snapshot()
    assert state['line']['quality'] == 'partial'
    assert (state['line']['tags_ok'], state['line']['tags_failed']) == (reported or (None, None))
    basket = item(state, 'Tank21_HaveBasket')
    assert basket['value'] == (1 if reported is None else None)
    assert basket['quality'] == ('fresh' if reported is None else 'unknown')


@pytest.mark.parametrize('timestamp', ['2026-01-01T00:00:02+00:00', '2025-12-31T23:59:59+00:00', 'bad'])
def test_untrusted_future_backwards_and_malformed_times_require_two_progressions(timestamp):
    fake = Fake()
    line = line_source(fake)
    fake.poll(line, 1)
    fake.sec = 1
    fake.poll(line, 2)
    assert item(fake.poll(line, 3, timestamp=timestamp), 'Remote')['quality'] == 'unknown'
    fake.sec = 3
    assert item(fake.poll(line, 4), 'Remote')['quality'] == 'unknown'
    fake.sec = 4
    assert item(fake.poll(line, 5), 'Remote')['quality'] == 'fresh'


def test_clock_configuration_handles_naive_offset_and_rejects_ambiguous_local_time():
    fake = Fake()
    unconfirmed = line_source(fake, clock_offset_s=None)
    for ident in (1, 2):
        fake.sec += 1
        assert item(fake.poll(unconfirmed, ident), 'Remote')['quality'] == 'unknown'
    line = line_source(fake, clock_offset_s=-1)
    for ident in (1, 2):
        fake.sec += 1
        raw = (fake.at() + timedelta(seconds=1)).replace(tzinfo=None).isoformat()
        state = fake.poll(line, ident, timestamp=raw)
    assert item(state, 'Remote')['quality'] == 'fresh'
    assert item(state, 'Remote')['source_at'] == raw
    assert state['line']['source_at'] == raw
    assert state['line']['clock_offset_ms'] == -1000
    assert state['line']['corrected_source_at'] == fake.at().isoformat()
    ambiguous = line_source(fake, clock_zone='America/New_York')
    state = fake.poll(ambiguous, 1, timestamp='2025-11-02T01:30:00')
    assert 'ambiguous' in item(state, 'Remote')['reason'].lower()


def test_bad_http_and_reused_row_id_cannot_revalidate_live_values():
    fake = Fake()
    line = line_source(fake)
    fake.poll(line, 1)
    fake.sec = 1
    fake.poll(line, 2)
    assert item(line.snapshot(), 'Remote')['quality'] == 'fresh'
    original = fake.request
    fake.request = lambda operation: (503, None)
    line.step()
    assert item(line.snapshot(), 'api_reachable')['value'] is False
    assert item(line.snapshot(), 'Remote')['quality'] == 'unknown'
    fake.request = original
    line.step()
    assert item(line.snapshot(), 'Remote')['quality'] == 'unknown'
    fake.sec = 2
    assert item(fake.poll(line, 2), 'Remote')['quality'] == 'unknown'
    fake.sec = 3
    assert item(fake.poll(line, 3), 'Remote')['quality'] == 'unknown'
    fake.sec = 4
    assert item(fake.poll(line, 4), 'Remote')['quality'] == 'fresh'


def test_diagnostics_report_only_the_row_received_on_each_attempt():
    fake = Fake()
    line = line_source(fake)
    fake.poll(line, 1)
    fake.sec = 1
    fake.poll(line, 2)
    original = fake.request
    fake.request = lambda operation: (503, None)
    line.step()
    unavailable = line.log()[-1]
    assert unavailable['http_status'] == 503
    assert all(unavailable[key] is None for key in ('row_id', 'source_at', 'tags_ok', 'tags_failed'))
    fake.request = original
    fake.poll(line, 5, timestamp='2026-01-01T00:00:03+00:00', tags_ok=2, tags_failed=7)
    rejected = line.log()[-1]
    assert rejected['row_id'] == 5
    assert rejected['source_at'] == fake.row['timestamp']
    assert (rejected['tags_ok'], rejected['tags_failed']) == (2, 7)
    assert 'future' in rejected['outcome']


def test_catalog_alias_and_shared_addresses_do_not_claim_independent_tank_values():
    fake = Fake()
    expected = parse_catalog({'tags': [*TAGS, tag('Removed', 'D11')]})
    line = line_source(fake, expected=expected, tanks={
        'Tank21': {'basket': 'Tank21_HaveBasket', 'door_opened': 'Tank22_DoorOpened',
                   'flow': 'Tank21_WaterFlow', 'command': 'Tank21A_Command'},
        'Tank22': {'door_opened': 'Tank22_DoorOpened', 'flow': 'Tank22_WaterFlow'},
    })
    fake.poll(line, 1)
    catalog = line.catalog()
    assert catalog['tanks'] == {'Tank21': {'basket': 'Tank21_HaveBasket'}, 'Tank22': {}}
    assert {'shared_address', 'duplicate_binding', 'invalid_binding', 'catalog_drift'} <= {
        note['code'] for note in catalog['warnings']}
    assert 'Tank21A' in {tag['group'] for tag in catalog['tags']}
    assert line.queue_refresh() == line.queue_refresh()
    line.step()
    assert fake.calls == ['list', 'read', 'list', 'read']
    assert line.snapshot()['line']['refresh']['outcome'] == 'done'


def test_unique_physical_alias_keeps_original_signal_and_clears_its_source_tank():
    fake = Fake()
    line = line_source(fake, tanks={'Tank21': {'door_opened': 'Tank22_DoorOpened'}, 'Tank22': {}})
    fake.poll(line, 1)
    catalog = line.catalog()
    assert catalog['tanks'] == {'Tank21': {'door_opened': 'Tank22_DoorOpened'}, 'Tank22': {}}
    assert any(note['code'] == 'alias' for note in catalog['warnings'])


def test_catalog_and_state_are_cache_only_during_stalled_single_worker(service):
    fake = Fake()
    service.owner = SimpleNamespace()
    line = service.line = line_source(fake)
    fake.poll(line, 1)
    entered, release = Event(), Event()

    def stalled(operation):
        assert operation == 'read'
        entered.set()
        assert release.wait(2)
        return 200, fake.row

    fake.request = stalled
    poll = Thread(target=line.step)
    poll.start()
    try:
        assert entered.wait(1)
        endpoint = '/api/line/catalog'
        assert request(service, endpoint, token='old')[0] == 403
        assert request(service, endpoint)[0] == 200
        assert request(service, '/api/state')[0] == 200
        path = '/api/line/refresh'
        assert request(service, path, method='POST', body={'action': 'refresh'}, origin='http://localhost:8870')[0] == 403
        first = request(service, path, method='POST', body={'action': 'refresh'})
        second = request(service, path, method='POST', body={'action': 'refresh'})
        assert first[0] == second[0] == 202 and first[2] == second[2]
        assert request(service, '/api/line/write', method='POST', body={'value': 1})[0] == 404
    finally:
        release.set()
        poll.join()


def test_read_client_only_uses_configured_origin_and_never_follows_redirect(monkeypatch):
    key = 'fixture-only-secret'
    seen = []

    class Connection:
        def __init__(self, host, port, timeout):
            seen.append((host, port, timeout))

        def request(self, method, path, headers):
            seen.append((method, path, headers))

        def getresponse(self):
            return SimpleNamespace(status=302, getheader=lambda name: 'https://elsewhere.invalid')

        def close(self):
            pass

    monkeypatch.setattr('anodizing_web.operator_line.HTTPConnection', Connection)
    client = ReadClient('http://line.invalid:8181', key)
    assert client.request('read') == (302, None)
    assert seen == [('line.invalid', 8181, 2), ('GET', '/api/v1/tags/read', {'X-API-Key': key})]
    with pytest.raises(KeyError):
        client.request('write')
    with pytest.raises(ValueError):
        ReadClient('https://name@line.invalid', key)


def test_partial_metadata_validation_and_recorder_export_provenance():
    with pytest.raises(ValueError):
        parse_catalog({'tags': [TAGS[0], TAGS[0]]})
    parsed = parse_catalog({'tags': [tag('Numeric', 'D1', None, low_limit=None, high_limit=10)]})
    assert parsed['Numeric']['maximum'] == 10 and parsed['Numeric']['minimum'] is None
    fake = Fake()
    line = line_source(fake)
    fake.poll(line, 1)
    fake.sec = 1
    value = item(fake.poll(line, 2), 'Remote')
    output = StringIO()
    write_csv([dict(type='sample', timestamp='sample', variables=[value])], output)
    assert 'row_id' in output.getvalue() and value['source_at'] in output.getvalue()
    assert 'api_key' not in json.dumps(line.catalog()).lower()


def test_unidentified_failed_tags_cannot_be_displayed_as_live():
    fake = Fake()
    line = line_source(fake)
    fake.poll(line, 1)
    fake.sec = 1
    state = fake.poll(line, 2, tags_failed=1, tags_ok=len(TAGS) - 1)
    assert state['line']['quality'] == 'partial'
    assert state['line']['tags_failed'] == 1
    assert all(variable['value'] is None for variable in state['variables'][2:])
    assert 'cannot be identified' in item(state, 'Remote')['reason']


def test_changed_catalog_cannot_apply_the_cached_row_to_the_new_tag_identity():
    fake = Fake()
    line = line_source(fake)
    fake.poll(line, 1)
    fake.sec = 1
    fake.poll(line, 2)
    fake.tags = [tag('Remote', 'M9')]
    line.queue_refresh()
    line.step()
    state = line.snapshot()
    assert state['line']['catalog_revision'] == 2
    assert item(state, 'Remote')['value'] is None and state['line']['row_id'] is None
    fake.sec = 2
    assert item(fake.poll(line, 3), 'Remote')['quality'] == 'unknown'
    fake.sec = 3
    assert item(fake.poll(line, 4), 'Remote')['quality'] == 'fresh'


def test_supplied_credentials_are_redacted_even_in_escaped_supplier_payloads(monkeypatch):
    from io import BytesIO
    key = 'fixture-"echoed"-secret'
    raw = json.dumps({'unexpected': key}).encode()
    response = BytesIO(raw)
    response.status = 200
    connection = SimpleNamespace(request=lambda *args, **kwargs: None,
                                 getresponse=lambda: response, close=lambda: None)
    monkeypatch.setattr('anodizing_web.operator_line.HTTPConnection', lambda *a, **k: connection)
    assert ReadClient('http://line.invalid', key).request('list') == (200, None)
