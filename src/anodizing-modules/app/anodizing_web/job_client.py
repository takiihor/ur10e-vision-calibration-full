"""Client identity and the standard-library development module client."""
import hmac
import json
import ssl
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, HTTPSHandler, ProxyHandler, Request, build_opener


def calibration_key(operator_key):
    return hmac.digest(operator_key.encode(), b'anodizing calibration gateway key', 'sha256').hex()


class Client:
    owner_id = None
    scopes = frozenset()

    def __init__(self, key):
        self.key = key


class _RejectRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ValueError('The IPC module endpoint returned a redirect.')


class DevModule(Client):
    def __init__(self, url, key, certificate, scopes):
        super().__init__(key)
        endpoint = urlsplit(url)
        if (endpoint.scheme != 'https' or endpoint.hostname is None or endpoint.username is not None
                or endpoint.path not in ('', '/') or endpoint.query or endpoint.fragment):
            raise ValueError('Choose the IPC HTTPS base address.')
        self.url = url.rstrip('/')
        self.scopes = frozenset(scopes)
        if not certificate:
            raise ValueError('Provide the IPC certificate file.')
        context = ssl.create_default_context(cafile=certificate)
        self.opener = build_opener(ProxyHandler({}), HTTPSHandler(context=context), _RejectRedirect())

    def request(self, operation, body=None):
        route = operation.partition('?')[0]
        read = route in ('health', 'status', 'jobs')
        raw = route in ('raw', 'raw/undo')
        if not read and not raw and route not in ('claim', 'submit', 'release', 'heartbeat', 'stop'):
            raise ValueError('This operation requires the operator app key.')
        scope = 'raw' if raw else 'read' if read else 'jobs'
        if route != 'stop' and scope not in self.scopes:
            raise ValueError('The client lacks the scope for this operation.')
        if raw and body is None:
            raise ValueError('Raw commands require a POST body.')
        headers = {'Authorization': 'Bearer ' + self.key, 'Content-Type': 'application/json'}
        data = None if body is None else json.dumps(body, allow_nan=False).encode()
        request = Request(self.url + '/v1/' + operation, data, headers)
        try:
            with self.opener.open(request, timeout=45 if raw else 2) as response:
                reply = json.load(response)
        except HTTPError as exc:
            with exc:
                raise ValueError(json.load(exc)['error']) from None
        if route == 'claim':
            self.owner_id = reply['owner']
        return reply
