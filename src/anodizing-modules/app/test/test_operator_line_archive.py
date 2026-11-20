"""Synthetic legacy evidence. Historical labels never feed test admission."""
import json

import pytest

from anodizing_web import operator_line_archive as archive
from test_operator import request, service

CONTEXT = dict(mode='MOCK', api_address='fixture')
AT = '2026-01-01T00:00:04+00:00'
LATER = '2026-01-01T00:00:06+00:00'
IDENTITIES = [[f'Fixture{number}', f'M{number}', 'Bool', 'W', 1] for number in range(1, 7)]


def source(path):
    path.mkdir()
    human = [dict(identity=identity, kind='human', value='pass', at=AT,
                  observed_at=AT, source='Fixture observation', write_sent=False) for identity in IDENTITIES]
    manual = dict(human[2], kind='api', value='failed', at=LATER)
    latest = dict(human[4], value='fail', at=LATER)
    (path / 'operator-results.json').write_text(json.dumps(
        dict(format='operator-results-v1', context=CONTEXT, records=[*human, manual, latest])))
    (path / 'operator-results-history.jsonl').write_text(json.dumps(dict(human[0], context=CONTEXT)) + '\n')
    checked = [dict(tag_name=ident[0], identity=ident, context=CONTEXT, api_status=result, checked_at=stamp)
               for ident, result, stamp in [(IDENTITIES[0], 'checked', AT), (IDENTITIES[1], 'failed', LATER),
                                            (IDENTITIES[5], 'failed', '2026-01-01T00:00:01+00:00')]]
    record = checked[0]
    record['eligible'] = True
    for phase, value in [('one', 1), ('zero', 0)]:
        baseline = '2026-01-01T00:00:01+00:00'
        record[phase] = dict(receipt_valid=True, accepted_at=baseline, baseline_utc=baseline,
                             observed=dict(fresh=True, Remote=1, Local=0, command_value=value, supplier_utc=AT))
    checked.append(dict(checked[0], context=dict(CONTEXT, api_address='another-fixture'), api_status='failed'))
    (path / 'api-checks.json').write_text(json.dumps(dict(records=checked)))
    (path / 'fixture-report.md').write_bytes(b'# Fixture report\r\n')
    return [p.name for p in path.iterdir()]


def test_labels_context_and_original_report_are_preserved(tmp_path):
    originals = tmp_path / 'originals'
    selected = source(originals)
    data = tmp_path / 'data'
    data.mkdir()
    digest = archive.import_archive(data, originals, selected)
    assert archive.import_archive(data, originals, list(reversed(selected))) == digest
    summary, = archive.archives(data)
    assert summary['history_events'] == 1
    assert summary['api_counts'] == {'Pass': 3, 'Fail': 2, 'Unchecked': 1}
    assert summary['human_counts'] == {'Pass': 5, 'Fail': 1, 'Not checked': 0}
    rows = {row['identity'][0]: row for row in summary['rows']}
    assert rows['Fixture1']['api_source'] == 'Automated'
    assert rows['Fixture2']['api'] == 'Fail'
    assert rows['Fixture3']['api_source'] == 'Manual'
    assert rows['Fixture4']['api_source'] == 'Human backfill'
    assert rows['Fixture6']['api_source'] == 'Human backfill'
    index = summary['files'].index('fixture-report.md')
    with archive.original(data, digest, str(index)) as stream:
        assert stream.read() == b'# Fixture report\r\n'
    assert (data / 'line-tests/imports' / digest).stat().st_mode & 0o077 == 0


@pytest.mark.parametrize('case', ['parent', 'symlink', 'malformed', 'oversized'])
def test_local_import_rejects_unsafe_files(tmp_path, case, monkeypatch):
    originals = tmp_path / 'originals'
    selected = source(originals)
    if case == 'parent':
        selected.append('../fixture-report.md')
    if case == 'symlink':
        (originals / 'linked.md').symlink_to(originals / 'fixture-report.md')
        selected.append('linked.md')
    if case == 'malformed':
        (originals / 'operator-results-history.jsonl').write_text('[]\n')
    if case == 'oversized':
        monkeypatch.setattr(archive, 'FILE_LIMIT', 5)
    with pytest.raises((OSError, ValueError)):
        archive.import_archive(tmp_path, originals, selected)
    assert archive.archives(tmp_path) == []


def test_archive_routes_serve_originals_as_authenticated_attachments(tmp_path, service):
    originals = tmp_path / 'originals'
    selected = source(originals)
    service.root = tmp_path
    digest = archive.import_archive(tmp_path, originals, selected)
    prefix = '/api/tests/line/archives'
    summary = json.loads(request(service, prefix)[2])['archives'][0]
    assert 'rows' not in summary
    assert request(service, f'{prefix}/{digest}')[0] == 200
    index = summary['files'].index('fixture-report.md')
    path = f'{prefix}/{digest}/{index}'
    assert request(service, path, token='wrong')[0] == 403
    status, headers, body = request(service, path)
    assert status == 200 and headers['Content-Disposition'] == 'attachment; filename="fixture-report.md"'
    assert body == b'# Fixture report\r\n'
    assert request(service, f'{prefix}/{digest}/999')[0] == 404
