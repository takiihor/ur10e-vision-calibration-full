"""Stage 1 contracts exercised without a socket or device."""
from email.parser import BytesParser
from io import BytesIO
import json
import re
from threading import RLock
from types import SimpleNamespace

import pytest

from anodizing_web import agv, server


def request(service, path, token='secret', host='127.0.0.1:8870', origin=None, method='GET', etag=None, body=None):
    handler = server.Handler.__new__(server.Handler)
    handler.server = service
    handler.client_address = ('127.0.0.1', 1234)
    extra = f'Origin: {origin}\r\n' if origin is not None else ''
    if etag is not None:
        extra += f'If-None-Match: {etag}\r\n'
    payload = b'' if body is None else json.dumps(body).encode()
    if payload:
        extra += f'Content-Type: application/json\r\nContent-Length: {len(payload)}\r\n'
    handler.rfile = BytesIO(f'{method} {path} HTTP/1.1\r\nHost: {host}\r\n'
                            f'X-Module-Token: {token}\r\n{extra}\r\n'.encode() + payload)
    handler.wfile = BytesIO()
    handler.handle_one_request()
    head, body = handler.wfile.getvalue().split(b'\r\n\r\n', 1)
    status, headers = head.split(b'\r\n', 1)
    return int(status.split()[1]), BytesParser().parsebytes(headers), body


@pytest.fixture
def service():
    owner = SimpleNamespace(lock=RLock(), active=None, closing=False,
                            storage_error=None, execution_uncertain=False)
    instance = SimpleNamespace(agv=agv.Connection(owner), token='secret',
                               hosts={'127.0.0.1:8870', 'localhost:8870'}, asset_cache={})
    instance.cached_asset = lambda path: server.Server.cached_asset(instance, path)
    return instance


def test_operator_entry_refreshes_nonce_and_uses_a_static_allowlist(service, tmp_path, monkeypatch):
    monkeypatch.setattr(server, 'OPERATOR_DIST', tmp_path, raising=False)
    (tmp_path / 'assets').mkdir()
    (tmp_path / 'assets' / 'main-a1.js').write_text('entry')
    (tmp_path / 'assets' / 'private.js').write_text('private')
    (tmp_path / 'asset-allowlist.json').write_text('["assets/main-a1.js"]')
    (tmp_path / 'index.html').write_text('<meta name="module-token" content="__TOKEN__">'
                                        '<meta name="style-nonce" content="__STYLE_NONCE__">')
    code, headers, body = request(service, '/operator/')
    assert code == 200 and headers['Cache-Control'] == 'no-store'
    assert b'content="secret"' in body
    nonce = re.search(r"'nonce-([^']+)'", headers['Content-Security-Policy'])[1]
    assert nonce.encode() in body and "script-src 'self';" in headers['Content-Security-Policy']
    assert "frame-ancestors 'none'" in headers['Content-Security-Policy']
    service.token = 'restarted'
    _, later, body = request(service, '/operator/')
    assert nonce not in later['Content-Security-Policy'] and b'content="restarted"' in body
    assert request(service, '/operator/assets/main-a1.js')[1]['Content-Type'].startswith('text/javascript')
    for path in ('/operator/assets/private.js', '/operator/asset-allowlist.json', '/operator/index.html',
                 '/operator/../agv'):
        assert request(service, path)[0] == 404


def test_state_is_authenticated_read_only_and_copies_acquisition_time(service, monkeypatch):
    link = service.agv
    clock = [10.0]
    monkeypatch.setattr(agv, 'monotonic', lambda: clock[0])
    monkeypatch.setattr(agv, 'serial_ports', lambda: pytest.fail('enumerated ports'))
    monkeypatch.setattr(link, '_receive', lambda: pytest.fail('read device'))
    monkeypatch.setattr(link, '_send', lambda *a, **k: pytest.fail('wrote device'))
    monkeypatch.setattr(link, '_service_resend', lambda: pytest.fail('retries run from read'))
    link.transport = object()
    link.settings = {'mode': 'simulator'}
    link.connected_at = 9.0
    link._status_received({'mode': 2, 'max_stations': 1, 'current_station': 2,
                           'stopped': True, 'actual_speed_m_h': 0, 'station_arrival': True,
                           'fault_code': 0, 'obstacle_stop': False}, b'')
    stamp = link.status_iso

    def station(key='current_station'):
        status, headers, payload = request(service, '/api/state')
        assert status == 200 and headers['Cache-Control'] == 'no-store'
        state = json.loads(payload)
        assert state['schema_version'] == 1
        variables = state['variables']
        assert [v['id'] for v in variables] == sorted(v['id'] for v in variables)
        return next(v for v in variables if v['id'] == f'agv.{key}')

    assert (station()['quality'], station()['value'], station()['read_at']) == ('fresh', 2, stamp)
    confirmed = station('last_confirmed_station')
    assert (confirmed['quality'], confirmed['value'], confirmed['read_at']) == ('fresh', 2, stamp)
    assert request(service, '/api/agv?cached=1')[0] == 200
    link.recipe_active = True
    clock[0] += 3
    assert station()['quality'] == 'stale' and station()['value'] is None
    link.recipe_active = False
    assert station()['quality'] == 'fresh'
    clock[0] += 2
    assert station()['read_at'] == stamp and station()['age_ms'] == 5000
    assert station()['quality'] == 'stale'
    assert request(service, '/api/state', token='old')[0] == 403
    assert request(service, '/api/state', origin='http://localhost:8870')[0] == 403
    assert request(service, '/operator/', host='example.org')[0] == 403
    assert request(service, '/api/state', method='POST')[0] == 400


def test_urgent_send_for_an_old_connection_refuses_before_touching_transport(service, monkeypatch):
    link = service.agv
    link.id = 2
    link.transport = object()
    monkeypatch.setattr(link, '_send', lambda *a, **k: pytest.fail('wrote to a different AGV link'))
    with pytest.raises(ValueError, match='connection changed'):
        link.submit({'action': 'send', 'command': 'stop', 'connection_id': 1})


def test_home_projection_does_not_report_stopped_at_nonzero_speed():
    from anodizing_web.operator_state import agv_state
    report = {'mode': 2, 'max_stations': 1, 'stopped': True, 'actual_speed_m_h': 500}
    sample = (report, 'now', 10.0, True, 'simulator', False, None, 'now', None, 10.1)
    moving = next(item for item in agv_state(sample) if item['id'] == 'agv.moving')
    assert moving['value'] is True
