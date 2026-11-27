"""Katana browser enrollment and actuator admission against fake IPC devices."""
from http.client import HTTPConnection
from http.cookies import SimpleCookie
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
            for path in ('/', '/operator/', '/operator-key', '/agv', '/calibration', '/teaching', '/pick-place',
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


def test_enrollment_persists_and_key_rotation_requires_enrollment_again(tmp_path, key_file):
    root = tmp_path / 'katana'
    app = server.Server(0, root, operator_key_file=key_file)
    with running(app) as port:
        endpoint = '/operator-key'
        origin = {'Origin': f'http://127.0.0.1:{port}'}
        assert request(port, endpoint, {'key': OPERATOR_KEY}, form=True,
                       headers={'Origin': 'http://invalid.example'})[0] == 403
        status, headers, body = request(port, endpoint, {'key': TOKEN}, form=True, headers=origin)
        assert status == 403
        assert 'Set-Cookie' not in headers
        assert TOKEN.encode() not in body
        status, headers, body = request(port, endpoint, {'key': OPERATOR_KEY}, form=True, headers=origin)
        assert status == 303
        assert headers['Location'] == '/operator/'
        assert OPERATOR_KEY.encode() not in body
        assert OPERATOR_KEY not in headers['Set-Cookie']
        cookies = SimpleCookie(headers['Set-Cookie'])
        cookie = next(iter(cookies.values()))
        assert cookie['httponly']
        assert cookie['samesite'] == 'Strict'
        assert cookie['path'] == '/'
        assert not cookie['domain']
        assert int(cookie['max-age']) > 0
        browser = {'Cookie': f'{cookie.key}={cookie.value}'}
        assert json.loads(request(port, '/api/operator-auth', headers=browser)[2])['authorized']
        assert request(port, '/api/agv', {'action': 'connect'}, headers=browser)[0] == 200
        assert request(port, '/api/agv', {'action': 'connect'},
                       headers={**browser, 'Origin': 'http://invalid.example'})[0] == 403

    with running(server.Server(0, root, operator_key_file=key_file)) as port:
        assert json.loads(request(port, '/api/operator-auth', headers=browser)[2])['authorized']
    key_file.write_text(OPERATOR_KEY + '-rotated\n')
    with running(server.Server(0, root, operator_key_file=key_file)) as port:
        assert not json.loads(request(port, '/api/operator-auth', headers=browser)[2])['authorized']


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
