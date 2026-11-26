from threading import Event, Thread

import pytest

from anodizing_web import executor_http


@pytest.mark.parametrize('https', (False, True))
def test_stop_witness_follows_http_body_and_precedes_reply(monkeypatch, https):
    sending, allow_send, release_reply, written = (Event() for _ in range(4))
    calls = []

    class Connection:
        def __init__(self, *args, **kwargs):
            pass

        def request(self, method, path, payload, headers):
            assert (method, path) == ('POST', '/v1/stop')
            sending.set()
            assert allow_send.wait(2)
            calls.append('sent')

        def getresponse(self):
            assert release_reply.wait(2)
            raise OSError('lost response')

        def close(self):
            pass

    def persist():
        calls.append('persisted')
        written.set()

    if https:
        monkeypatch.setattr(executor_http, 'client_context', lambda _: object(), raising=False)
        monkeypatch.setattr(executor_http, 'HTTPSConnection', Connection, raising=False)
    else:
        monkeypatch.setattr(executor_http, 'HTTPConnection', Connection)
    client = executor_http.Client(1234, 'fixture', certificate='fixture.pem') if https else executor_http.Client(1234, 'fixture')

    def deliver():
        try:
            executor_http.deliver_priority_stop(client, {'stop_id': 'stop-test'}, persist)
        except OSError:
            calls.append('lost reply')

    assert callable(getattr(executor_http, 'deliver_priority_stop', None))
    worker = Thread(target=deliver)
    worker.start()
    try:
        assert sending.wait(1)
        assert not written.is_set()
        allow_send.set()
        assert written.wait(1)
        assert calls == ['sent', 'persisted']
    finally:
        allow_send.set()
        release_reply.set()
        worker.join(3)
    assert calls == ['sent', 'persisted', 'lost reply']


def test_operator_and_calibration_clients_use_distinct_scoped_keys():
    from anodizing_web.executor_http import CalibrationModule, OperatorApp
    from anodizing_web.job_client import Client

    app = OperatorApp(18875, 'fixture-service-key-only')
    calibration = app.for_owner('calibration')
    assert isinstance(app, Client)
    assert isinstance(calibration, CalibrationModule)
    assert app.owner_id == 'agv-page'
    assert app.scopes == {'operator'}
    assert calibration.owner_id == 'calibration'
    assert calibration.scopes == {'read', 'jobs'}
    assert calibration.key != app.key
    with pytest.raises(ValueError, match='operator app key'):
        calibration('arm')
