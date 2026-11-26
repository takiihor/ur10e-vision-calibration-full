from http.server import BaseHTTPRequestHandler
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
    assert config.accepts('198.51.100.19')
    assert not config.accepts('203.0.113.19')
    assert not config.accepts('192.0.2.19')
    with pytest.raises(ValueError, match='separate'):
        network('198.51.100.8', '0.0.0.0/0')
    with pytest.raises(ValueError, match='unicast'):
        network('0.0.0.0', '0.0.0.0/32')


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
