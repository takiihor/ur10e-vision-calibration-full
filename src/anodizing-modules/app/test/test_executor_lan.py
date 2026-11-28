from http.server import BaseHTTPRequestHandler
import json
from pathlib import Path
import ssl
import subprocess
from threading import Thread
from urllib.request import urlopen

import pytest

from anodizing_web.executor_lan import GatewayNetwork, HttpsListener, client_context


def network(address='127.0.0.1', subnet='127.0.0.0/8'):
    return GatewayNetwork(address, subnet, '192.0.2.0/24', 18875)


def certificate(root: Path):
    root.mkdir()
    cert = root / 'module-api-cert.pem'
    key = root / 'module-api-key.pem'
    subprocess.run(
        ['openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-nodes', '-days', '1',
         '-keyout', str(key), '-out', str(cert), '-subj', '/CN=127.0.0.1',
         '-addext', 'subjectAltName=IP:127.0.0.1'],
        capture_output=True, check=True,
    )
    key.chmod(0o600)
    return cert


def test_module_listener_refuses_non_lan_and_robot_subnet():
    config = network('198.51.100.8', '198.51.100.0/24')
    assert config.accepts('198.51.100.8')
    assert config.accepts('198.51.100.19')
    assert not config.accepts('203.0.113.19')
    assert not config.accepts('192.0.2.19')
    with pytest.raises(ValueError, match='separate'):
        network('198.51.100.8', '0.0.0.0/0')
    with pytest.raises(ValueError, match='unicast'):
        network('0.0.0.0', '0.0.0.0/32')


def test_reserved_stop_cannot_use_the_ipc_address_as_katana():
    with pytest.raises(ValueError, match='different from the IPC address'):
        GatewayNetwork('198.51.100.8', '198.51.100.0/24', '192.0.2.0/24', 18875,
                       stop_port=18876, katana_cidr='198.51.100.8/32')


def test_failed_keys_are_throttled_and_logged(caplog, tmp_path):
    certificate(tmp_path / 'tls')
    with HttpsListener(network(), tmp_path / 'tls', BaseHTTPRequestHandler, port=0) as server:
        for _ in range(5):
            assert server.reject_key('127.0.0.1') == 403
        assert server.reject_key('127.0.0.1') == 429
        assert server.reject_key('127.0.0.2') == 403
        assert 'Module API key refused' in caplog.text


def test_https_requires_the_pinned_certificate(tmp_path):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(204)
            self.end_headers()

    cert = certificate(tmp_path / 'local')
    wrong = certificate(tmp_path / 'wrong')
    with HttpsListener(network(), tmp_path / 'local', Handler, port=0) as server:
        thread = Thread(target=server.serve_forever)
        thread.start()
        url = f'https://127.0.0.1:{server.server_port}/v1/status'
        try:
            with urlopen(url, context=client_context(cert), timeout=2) as reply:
                assert reply.status == 204
            with pytest.raises(OSError) as error:
                urlopen(url, context=client_context(wrong), timeout=2)
            assert isinstance(error.value.reason, ssl.SSLCertVerificationError)
        finally:
            server.shutdown()
            thread.join()


def test_reserved_https_stop_accepts_only_katana_source_and_operator_key(tmp_path, monkeypatch):
    from functools import partial
    from http.client import HTTPSConnection

    from anodizing_web import executor_http
    from anodizing_web.executor import Executor, FakeDevices
    from anodizing_web.module_tokens import calibration_key
    from test_executor_link_review import TOKEN, running

    tls = tmp_path / 'tls'
    cert = certificate(tls)
    (tls / 'site-profile.json').write_text(json.dumps({'line_network': {
        'ipc_address': '127.0.0.1', 'line_subnet': '127.0.0.0/8', 'ur10e_subnet': '192.0.2.0/24',
        'katana_cidr': '127.0.0.2/32', 'stop_api_port': 18876,
    }}))
    config = GatewayNetwork.configured(tls, simulated=True)
    assert config.stop_port == 18876
    executor = Executor(tmp_path / 'ipc', FakeDevices(), gateway_enabled=True)
    try:
        _, module_key = executor.gateway.keys.add('sample', ('read', 'jobs'))
        handler = executor_http.control_handler(executor, TOKEN, executor.gateway.keys)
        with running(HttpsListener(config, tls, handler, port=0)) as port:
            local = executor_http.Client(port, module_key, certificate=cert)
            assert local('health')['boot'] == executor.boot
            with pytest.raises(ValueError, match='not authorized'):
                executor_http.Client(port, 'wrong-key', certificate=cert)('health')

        monkeypatch.setattr(executor_http, 'HTTPSConnection',
                            partial(HTTPSConnection, source_address=('127.0.0.2', 0)))
        monkeypatch.setattr(config, 'stop_port', 0)
        with running(executor_http.stop_listener(config, tls, executor, TOKEN)) as port:
            operator = executor_http.OperatorApp(port, TOKEN, certificate=cert)
            assert operator('health')['boot'] == executor.boot
            for key in ('wrong-key', calibration_key(TOKEN), module_key):
                with pytest.raises(ValueError, match='not authorized'):
                    executor_http.Client(port, key, certificate=cert)('stop', {'stop_id': 'denied'})
            assert executor.status()['stop'] is None
            with pytest.raises(ValueError, match='Not found'):
                operator('status')
            ipc_peer = HTTPSConnection('127.0.0.1', port, timeout=2, context=client_context(cert),
                                        source_address=('127.0.0.1', 0))
            try:
                with pytest.raises(OSError):
                    ipc_peer.request('GET', '/v1/health', headers={'Authorization': 'Bearer ' + TOKEN})
            finally:
                ipc_peer.close()
            assert operator('stop', {'stop_id': 'reserved-stop'})['received'] is True
    finally:
        executor.close()


def test_stop_listener_opens_when_tower_light_worker_cannot_start(tmp_path, monkeypatch, caplog):
    from functools import partial
    from http.client import HTTPSConnection

    from anodizing_web import executor_http, tower_light
    from anodizing_web.executor import Executor, FakeDevices
    from test_executor_link_review import TOKEN, running

    class BrokenThread(Thread):
        def start(self):
            raise RuntimeError('Cannot start tower test thread.')

    def no_serial():
        pytest.fail('The tower tried to open serial during the startup failure test.')

    monkeypatch.setattr(tower_light, 'Thread', BrokenThread)
    monkeypatch.setattr(executor_http, 'HTTPSConnection',
                        partial(HTTPSConnection, source_address=('127.0.0.2', 0)))
    tls = tmp_path / 'tls'
    cert = certificate(tls)
    config = GatewayNetwork('127.0.0.1', '127.0.0.0/8', '192.0.2.0/24', 18875,
                            stop_port=18876, katana_cidr='127.0.0.2/32')
    config.stop_port = 0
    light = tower_light.TowerLight(no_serial, False)
    executor = Executor(tmp_path / 'ipc', FakeDevices(), gateway_enabled=True, tower=light)
    try:
        with running(executor_http.stop_listener(config, tls, executor, TOKEN)) as port:
            operator = executor_http.OperatorApp(port, TOKEN, certificate=cert)
            assert operator('health')['boot'] == executor.boot
            assert operator('stop', {'stop_id': 'tower-startup-failed'})['received'] is True
        assert executor.status(display=True)['tower_light'] == {'available': False, 'pattern': 'unavailable'}
        assert 'Cannot start tower test thread.' in caplog.text
    finally:
        executor.close()


def test_stop_survives_a_failed_https_connection_and_goes_first_on_recovery(tmp_path, monkeypatch):
    from http.client import HTTPSConnection
    from types import SimpleNamespace

    from anodizing_web import executor_http
    from anodizing_web.executor import Executor, FakeDevices
    from anodizing_web.executor_link import ExecutorLink
    from test_executor_link_review import TOKEN, running, wait_for

    tls = tmp_path / 'tls'
    cert = certificate(tls)
    executor = Executor(tmp_path / 'ipc', FakeDevices(), gateway_enabled=True)
    local_root = tmp_path / 'katana'
    local_root.mkdir()
    local = SimpleNamespace(owner=SimpleNamespace(root=local_root), commissioning=lambda **_: {})
    calls = []
    online = False

    class Connection(HTTPSConnection):
        def request(self, method, url, *args, **kwargs):
            if not online:
                raise TimeoutError('Fake HTTPS link loss.')
            calls.append(url)
            return super().request(method, url, *args, **kwargs)

    monkeypatch.setattr(executor_http, 'HTTPSConnection', Connection)
    try:
        handler = executor_http.control_handler(executor, TOKEN)
        with running(HttpsListener(network(), tls, handler, port=0)) as port:
            client = executor_http.Client(port, TOKEN, certificate=cert)
            link = ExecutorLink(client, local)
            try:
                assert link.stop({'stop_id': 'https-transport-test'})['received'] is False
                assert link.status()['stop_unconfirmed'] is True
            finally:
                link.close()
            link = ExecutorLink(client, local)
            try:
                assert link.status()['stop_unconfirmed'] is True
                online = True
                stop = wait_for(executor.status, lambda state: (state['stop'] or {}).get('stop_id') == 'https-transport-test')
                assert calls[0] == '/v1/stop'
                assert stop['armed'] is False
                wait_for(link.status, lambda state: not state['stop_unconfirmed'])
            finally:
                link.close()
    finally:
        executor.close()


def test_standard_library_module_client_claims_over_the_keyed_https_api(tmp_path):
    from anodizing_web.executor import Executor, FakeDevices
    from anodizing_web.executor_http import control_handler
    from anodizing_web.job_client import DevModule
    from test_executor_link_review import TOKEN, running

    tls = tmp_path / 'tls'
    cert = certificate(tls)
    executor = Executor(tmp_path / 'ipc', FakeDevices(), gateway_enabled=True)
    try:
        record, key = executor.gateway.keys.add('sample', ('read', 'jobs'))
        handler = control_handler(executor, TOKEN, executor.gateway.keys)
        with running(HttpsListener(network(), tls, handler, port=0)) as port:
            module = DevModule(f'https://127.0.0.1:{port}', key, cert, record.scopes)
            assert module.request('health')['gateway_enabled']
            claim = module.request('claim', {})
            assert module.owner_id == claim['owner'] == 'sample'
            assert module.request('status')['armed_for'] is None
            with pytest.raises(ValueError, match='operator app key'):
                module.request('arm', {})
            module.request('release', {'lease_id': claim['lease_id']})
            assert module.request('status')['owner'] is None
    finally:
        executor.close()


def test_module_client_requires_an_explicit_certificate():
    from anodizing_web.job_client import DevModule

    with pytest.raises(ValueError, match='certificate'):
        DevModule('https://127.0.0.1:12345', 'fixture-key', None, ('read',))


def test_module_client_refuses_redirects(monkeypatch):
    from urllib.request import HTTPRedirectHandler, Request
    from anodizing_web.job_client import DevModule

    monkeypatch.setattr(ssl, 'create_default_context', lambda **_: object())
    client = DevModule('https://127.0.0.1:12345', 'fixture-key', 'fixture.pem', ('read',))
    redirect = next(handler for handler in client.opener.handlers if isinstance(handler, HTTPRedirectHandler))
    with pytest.raises(ValueError, match='redirect'):
        redirect.redirect_request(Request('https://127.0.0.1:12345/v1/status'), None, 302, 'Found', {},
                                  'https://127.0.0.1:12346/v1/status')


def test_throttled_bad_keys_skip_registry_work_but_valid_stop_still_works(tmp_path):
    from email.message import Message
    from io import BytesIO
    from threading import Lock
    from anodizing_web.executor import Executor, FakeDevices
    from anodizing_web.executor_http import control_handler
    from anodizing_web.module_tokens import KeyStore

    executor = Executor(tmp_path / 'ipc', FakeDevices(), gateway_enabled=True)
    tokens = KeyStore(tmp_path / 'keys.json')
    _, module_key = tokens.add('sample', ['jobs'])
    reads = []
    read = tokens._read
    tokens._read = lambda: (reads.append(1), read())[1]
    operator_key = 'fixture-operator-key-' * 2
    handler_class = control_handler(executor, operator_key, tokens)
    listener = HttpsListener.__new__(HttpsListener)
    listener.failed_keys, listener.failed_key_window, listener.failed_key_lock = {}, -1, Lock()

    def request(key):
        handler = handler_class.__new__(handler_class)
        headers = Message()
        headers['Authorization'] = 'Bearer ' + key
        headers['Content-Type'] = 'application/json'
        headers['Content-Length'] = '2'
        handler.headers, handler.path, handler.rfile = headers, '/v1/stop', BytesIO(b'{}')
        handler.server, handler.client_address = listener, ('127.0.0.1', 12345)
        handler.allowed_host = lambda: True
        replies = []
        handler.send = lambda status, value: replies.append(status)
        handler.handle_request('POST')
        return replies[0]

    try:
        bad_key = 'a' * 16 + '.' + 'b' * 43
        assert [request(bad_key) for _ in range(6)] == [403] * 5 + [429]
        read_count = len(reads)
        assert request(bad_key) == 429
        assert len(reads) == read_count
        assert request(operator_key) == 200
        assert request(module_key) == 200
    finally:
        executor.close()


@pytest.mark.parametrize('surface', ['loopback-http', 'local-https', 'lan-https'])
def test_listener_bounds_ordinary_workers_and_reuses_released_capacity(tmp_path, monkeypatch, surface):
    from threading import Event
    from anodizing_web.executor_http import Loopback
    from test_executor_phase1 import wait_for

    entered, release = Event(), Event()
    class Request:
        def __init__(self):
            self.closed = Event()
        def shutdown(self, how):
            self.closed.set()
        def close(self):
            self.closed.set()

    monkeypatch.setattr(Loopback, 'connection_limit', 1, raising=False)
    monkeypatch.setattr(HttpsListener, 'connection_limit', 1)
    monkeypatch.setattr(HttpsListener, 'local_connection_limit', 1, raising=False)
    peer = ('127.0.0.2' if surface == 'lan-https' else '127.0.0.1', 1)
    if surface == 'loopback-http':
        server = Loopback(0, BaseHTTPRequestHandler)
    else:
        certificate(tmp_path / 'tls')
        server = HttpsListener(network(), tmp_path / 'tls', BaseHTTPRequestHandler, port=0)
    with server as listener:
        def handle(request, peer):
            entered.set()
            assert release.wait(3)
        listener.finish_request = handle
        first, second = Request(), Request()
        try:
            listener.process_request(first, peer)
            assert entered.wait(1)
            listener.process_request(second, peer)
            assert second.closed.wait(1)
            if surface == 'local-https':
                entered.clear()
                listener.process_request(Request(), ('127.0.0.2', 1))
                assert entered.wait(1), 'IPC-local requests consumed the line-LAN budget'
            release.set()
            wait_for(first.closed.is_set, bool)
            entered.clear()
            third = Request()
            listener.process_request(third, peer)
            assert entered.wait(1)
        finally:
            release.set()


def test_listener_deadline_shuts_down_a_request_even_if_handler_is_active(tmp_path):
    from threading import Event

    certificate(tmp_path / 'tls')
    entered, release, shutdown = Event(), Event(), Event()
    class Request:
        def shutdown(self, how):
            shutdown.set()
        def close(self):
            pass

    with HttpsListener(network(), tmp_path / 'tls', BaseHTTPRequestHandler, port=0) as listener:
        listener.request_s = .1
        def handle(request, peer):
            entered.set()
            assert release.wait(3)
        listener.finish_request = handle
        try:
            listener.process_request(Request(), ('127.0.0.1', 1))
            assert entered.wait(1)
            assert shutdown.wait(1)
        finally:
            release.set()


def test_reserved_stop_separates_pre_auth_and_authenticated_worker_budgets(tmp_path, monkeypatch):
    from threading import Event
    from http.server import ThreadingHTTPServer

    certificate(tmp_path / 'tls')
    monkeypatch.setattr(ThreadingHTTPServer, '__init__', lambda self, address, handler: setattr(self, 'server_port', 18876))
    monkeypatch.setattr(HttpsListener, 'connection_limit', 1)
    monkeypatch.setattr(HttpsListener, 'stop_connection_limit', 1, raising=False)
    listener = HttpsListener(network(), tmp_path / 'tls', BaseHTTPRequestHandler, port=18876, peer='127.0.0.1')
    listener.preauth_s = 2
    authenticated, pending, release, release_pending, denied = (Event() for _ in range(5))

    class Request:
        def __init__(self, kind):
            self.kind = kind
            self.closed = Event()
        def shutdown(self, how):
            self.closed.set()
        def close(self):
            self.closed.set()

    def handle(request, peer):
        if request.kind == 'authenticated':
            assert listener.admit_stop(request)
            authenticated.set()
            assert release.wait(3)
        elif request.kind == 'another authenticated':
            if not listener.admit_stop(request):
                denied.set()
        else:
            pending.set()
            assert release_pending.wait(3)

    listener.finish_request = handle
    first, second, third = Request('authenticated'), Request('pending'), Request('extra')
    try:
        listener.process_request(first, ('127.0.0.1', 1))
        assert authenticated.wait(1)
        listener.process_request(second, ('127.0.0.1', 2))
        assert pending.wait(1), 'authenticated Stop consumed the pre-auth budget'
        listener.process_request(third, ('127.0.0.1', 3))
        assert third.closed.wait(1), 'pre-auth budget was not bounded'
        release_pending.set()
        assert second.closed.wait(2)
        fourth = Request('another authenticated')
        listener.process_request(fourth, ('127.0.0.1', 4))
        assert denied.wait(1), 'authenticated Stop budget was not bounded'
    finally:
        release.set()
        release_pending.set()
        assert first.closed.wait(2)


def test_reserved_stop_pre_auth_deadline_is_short(tmp_path, monkeypatch):
    from threading import Event
    from http.server import ThreadingHTTPServer

    certificate(tmp_path / 'tls')
    monkeypatch.setattr(ThreadingHTTPServer, '__init__', lambda self, address, handler: setattr(self, 'server_port', 18876))
    listener = HttpsListener(network(), tmp_path / 'tls', BaseHTTPRequestHandler, port=18876, peer='127.0.0.1')
    entered, shutdown, release = Event(), Event(), Event()

    class Request:
        def shutdown(self, how):
            shutdown.set()
        def close(self):
            pass

    def handle(request, peer):
        entered.set()
        assert release.wait(3)

    listener.finish_request = handle
    try:
        listener.process_request(Request(), ('127.0.0.1', 1))
        assert entered.wait(1)
        assert shutdown.wait(1.5), 'pre-auth request exceeded the short deadline'
    finally:
        release.set()
