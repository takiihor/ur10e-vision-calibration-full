"""Katana browser enrollment and actuator admission against fake IPC devices."""
from http.client import HTTPConnection
import json
import secrets
from urllib.parse import urlencode

import pytest

from anodizing_web import server
from test_executor_link_review import TOKEN, remote, running
from test_programme_review import steps


OPERATOR_KEY = secrets.token_urlsafe(32)


@pytest.fixture
def key_file(tmp_path):
    path = tmp_path / 'operator.key'
    path.write_text(OPERATOR_KEY + '\n')
    path.chmod(0o600)
    return path


def request(port, path, body=None, *, headers=None, form=False):
    connection = HTTPConnection('127.0.0.1', port, timeout=5)
    headers = dict(headers or {})
    headers.setdefault('Host', f'dev.localhost:{port}')
    if body is not None:
        headers['Content-Type'] = 'application/x-www-form-urlencoded' if form else 'application/json'
        body = urlencode(body) if form else json.dumps(body)
    try:
        connection.request('POST' if body is not None else 'GET', path, body=body, headers=headers)
        response = connection.getresponse()
        return response.status, dict(response.getheaders()), response.read()
    finally:
        connection.close()


def test_viewer_pages_and_api_do_not_serve_keys(tmp_path, key_file):
    with remote(tmp_path) as (client, _, _):
        app = server.Server(0, tmp_path / 'katana', executor_client=client, operator_key_file=key_file)
        with running(app) as port:
            for path in ('/', '/dev/', '/operator-key', '/agv', '/calibration', '/teaching', '/pick-place',
                         '/preview/tool-envelope.html', '/api/status', '/api/state', '/api/agv'):
                status, headers, body = request(port, path)
                assert status == 200, path
                for value in (OPERATOR_KEY, TOKEN, app.token):
                    assert value.encode() not in body, path
                assert 'Set-Cookie' not in headers, path
            assert json.loads(request(port, '/api/operator-auth')[2]) == {'required': True, 'authorized': False}


def test_viewer_cannot_write_but_can_stop(tmp_path, key_file):
    with remote(tmp_path) as (client, executor, _):
        app = server.Server(0, tmp_path / 'katana', executor_client=client, operator_key_file=key_file,
                            programme_steps=steps('seal'))
        with running(app) as port:
            for path, body in (
                ('/api/agv', {'action': 'arm', 'armed': True}),
                ('/api/agv', {'action': 'arm', 'armed': False}),
                ('/api/agv', {'action': 'send', 'command': 'call_station'}),
                ('/api/agv', {'action': 'send', 'command': 'query_status'}),
                ('/api/agv-bluetooth', {'action': 'write'}),
                ('/api/agv-travel-pose', {'confirmed': True}),
                ('/api/programme', {'action': 'start'}),
                ('/api/records', {'action': 'stop', 'id': 'anything'}),
            ):
                assert request(port, path, body)[0] == 403, (path, body)
            assert request(port, '/api/agv', {'action': 'arm', 'armed': True},
                           headers={'Authorization': f'Bearer {TOKEN}', 'X-Module-Token': TOKEN})[0] == 403
            assert request(port, '/api/agv', {'action': 'arm', 'armed': True},
                           headers={'X-Module-Token': app.token})[0] == 403
            stop = {'action': 'send', 'command': 'stop', 'request_id': 'auth-priority-stop'}
            assert request(port, '/api/agv', stop, headers={'Origin': 'http://invalid.example'})[0] == 403
            status, _, body = request(port, '/api/agv', stop)
            assert status == 200
            assert json.loads(body)['executor_stop']['stop_id'] == stop['request_id']
            assert executor.status()['stop']['stop_id'] == stop['request_id']


def test_enrollment_is_origin_bound_and_key_rotation_requires_enrollment_again(tmp_path, key_file):
    root = tmp_path / 'katana'
    app = server.Server(0, root, operator_key_file=key_file)
    with running(app) as port:
        endpoint = '/operator-key'
        origin = {'Origin': f'http://dev.localhost:{port}'}
        assert request(port, endpoint, {'key': OPERATOR_KEY}, form=True,
                       headers={'Origin': 'http://invalid.example'})[0] == 403
        status, headers, body = request(port, endpoint, {'key': TOKEN}, form=True, headers=origin)
        assert status == 403
        assert 'Set-Cookie' not in headers
        assert TOKEN.encode() not in body
        status, headers, body = request(port, endpoint, {'key': OPERATOR_KEY}, form=True, headers=origin)
        assert status == 200
        assert 'Set-Cookie' not in headers
        assert OPERATOR_KEY.encode() not in body
        credential = json.loads(body)['enrollment']
        browser = {'X-Operator-Enrollment': credential}
        assert json.loads(request(port, '/api/operator-auth', headers=browser)[2])['authorized']
        assert request(port, '/api/agv', {'action': 'connect'}, headers=browser)[0] == 200
        assert request(port, '/api/agv', {'action': 'connect'},
                       headers={**browser, 'Origin': 'http://invalid.example'})[0] == 403
        old_cookie = {'Cookie': f'anodizing_operator={credential}'}
        assert not json.loads(request(port, '/api/operator-auth', headers=old_cookie)[2])['authorized']
    # Even an explicitly copied credential is unusable at a different origin.
    with running(server.Server(port, root, operator_key_file=key_file)) as restarted_port:
        assert json.loads(request(restarted_port, '/api/operator-auth', headers=browser)[2])['authorized']
    with running(server.Server(0, tmp_path / 'other', operator_key_file=key_file)) as other_port:
        assert other_port != port
        assert not json.loads(request(other_port, '/api/operator-auth', headers=browser)[2])['authorized']
    key_file.write_text(OPERATOR_KEY + '-rotated\n')
    with running(server.Server(port, root, operator_key_file=key_file)) as restarted_port:
        assert not json.loads(request(restarted_port, '/api/operator-auth', headers=browser)[2])['authorized']


def test_key_file_cannot_be_public_or_inside_application_data_root(tmp_path, key_file):
    root = tmp_path / 'katana'
    root.mkdir()
    inside_root = root / 'operator.key'
    inside_root.write_bytes(key_file.read_bytes())
    inside_root.chmod(0o600)
    with pytest.raises(ValueError, match='operator key file'):
        server.Server(0, root, operator_key_file=inside_root)
    key_file.chmod(0o644)
    with pytest.raises(ValueError, match='operator key file'):
        server.Server(0, root, operator_key_file=key_file)


def test_keyed_operator_service_requires_dedicated_hostname(tmp_path, key_file):
    app = server.Server(0, tmp_path / 'katana', operator_key_file=key_file)
    with running(app) as port:
        assert request(port, '/operator-key', headers={'Host': f'127.0.0.1:{port}'})[0] == 403
        status, _, page = request(port, '/operator-key', headers={'Host': f'dev.localhost:{port}'})
        assert status == 200
        assert b'href="/dev/"' in page


def test_operator_enrollment_expires_on_server(tmp_path, key_file, monkeypatch):
    import time

    now = time.time()
    app = server.Server(0, tmp_path / 'katana', operator_key_file=key_file)
    with running(app) as port:
        host = f'dev.localhost:{port}'
        headers = {'Host': host, 'Origin': f'http://{host}'}
        status, _, response = request(port, '/operator-key', {'key': OPERATOR_KEY}, form=True, headers=headers)
        assert status == 200
        browser = {'Host': host, 'X-Operator-Enrollment': json.loads(response)['enrollment']}
        assert json.loads(request(port, '/api/operator-auth', headers=browser)[2])['authorized']
        monkeypatch.setattr(time, 'time', lambda: now + 12 * 60 * 60 + 2)
        assert not json.loads(request(port, '/api/operator-auth', headers=browser)[2])['authorized']


@pytest.mark.parametrize('path', ['/', '/agv', '/operator/'])
def test_legacy_hosts_show_move_notice_but_do_not_allow_enrollment_or_api(tmp_path, key_file, path):
    app = server.Server(0, tmp_path / 'katana', operator_key_file=key_file)
    with running(app) as port:
        legacy = {'Host': f'127.0.0.1:{port}'}
        status, _, body = request(port, path, headers=legacy)
        assert status == 200
        assert b'dev.localhost' in body
        assert request(port, '/operator-key', headers=legacy)[0] == 403
        assert request(port, '/api/operator-auth', headers=legacy)[0] == 403


def test_old_app_redirect_discards_query_and_preserves_browser_fragment(tmp_path, key_file):
    app = server.Server(0, tmp_path / 'katana', operator_key_file=key_file)
    with running(app) as port:
        legacy = {'Host': f'operator.localhost:{port}', 'Cookie': 'secret=cookie',
                  'X-Operator-Enrollment': 'secret-enrollment'}
        status, headers, body = request(port, '/operator/?key=secret-query', headers=legacy)
        assert status in (301, 302, 303, 307, 308)
        # With no fragment in Location, browsers retain the original #/ route.
        assert headers['Location'] == f'http://dev.localhost:{port}/dev/'
        assert headers['Referrer-Policy'] == 'no-referrer'
        assert b'secret' not in body
        assert 'Set-Cookie' not in headers


def test_old_origin_cannot_enroll_or_use_api_and_old_hmac_is_rejected(tmp_path, key_file):
    import hmac
    import time

    app = server.Server(0, tmp_path / 'katana', operator_key_file=key_file)
    with running(app) as port:
        old_host = f'operator.localhost:{port}'
        new_host = f'dev.localhost:{port}'
        old = {'Host': old_host, 'Origin': f'http://{old_host}'}
        assert request(port, '/operator-key', {'key': OPERATOR_KEY}, form=True, headers=old)[0] == 403
        assert request(port, '/api/operator-auth', headers=old)[0] == 403
        assert request(port, '/api/operator-auth', {}, headers=old)[0] == 403
        assert request(port, '/operator-key', {'key': OPERATOR_KEY}, form=True,
                       headers={'Host': new_host, 'Origin': f'http://{old_host}'})[0] == 403
        value = f'{int(time.time()) + 3600}.{secrets.token_hex(16)}'
        signature = hmac.digest(OPERATOR_KEY.encode(),
            f'anodizing operator enrollment http://{old_host} {value}'.encode(), 'sha256').hex()
        browser = {'Host': new_host, 'X-Operator-Enrollment': f'{value}.{signature}'}
        assert not json.loads(request(port, '/api/operator-auth', headers=browser)[2])['authorized']
