"""The keyed executor API's line-LAN HTTPS transport."""
from http.server import ThreadingHTTPServer
from ipaddress import IPv4Address, IPv4Network
import logging
import ssl
import stat
from threading import Lock
from time import monotonic

from .site_profile import section


LOG = logging.getLogger(__name__)


class GatewayNetwork:
    def __init__(self, address, subnet, robot_subnet, port):
        if any(not isinstance(value, str) for value in (address, subnet, robot_subnet)):
            raise ValueError('Set the IPC address, line subnet and UR10e subnet in the site profile.')
        self.address = IPv4Address(address)
        if self.address.is_unspecified or self.address.is_multicast or self.address == IPv4Address('255.255.255.255'):
            raise ValueError('Choose a unicast IPC line-LAN address.')
        self.subnet = IPv4Network(subnet)
        if self.address not in self.subnet or self.subnet.overlaps(IPv4Network(robot_subnet)):
            raise ValueError('Choose a line-LAN address and subnet separate from the UR10e subnet.')
        if type(port) is not int or not 1 <= port <= 65535:
            raise ValueError('Choose a fixed port for the module API.')
        self.port = port

    @classmethod
    def configured(cls, root, *, simulated):
        profile = section(root, 'line_network')
        if 'line_subnet' not in profile:
            return None
        config = cls(profile.get('ipc_address'), profile['line_subnet'], profile.get('ur10e_subnet'),
                     profile.get('module_api_port', 18875))
        if config.address.is_loopback and not simulated:
            raise ValueError('Choose the IPC line-LAN address for the live module API.')
        return config

    def accepts(self, source):
        return IPv4Address(source) in self.subnet


def client_context(certificate):
    return ssl.create_default_context(cafile=certificate)


class HttpsListener(ThreadingHTTPServer):
    scheme = 'https'
    daemon_threads = False

    def __init__(self, network, root, handler, *, port=None):
        self.network = network
        self.tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        self.tls.minimum_version = ssl.TLSVersion.TLSv1_2
        key = root / 'module-api-key.pem'
        info = key.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077:
            raise ValueError('The IPC TLS private key must be a regular file with mode 0600.')
        self.tls.load_cert_chain(root / 'module-api-cert.pem', key)
        self.failed_keys = {}
        self.failed_key_window = -1
        self.failed_key_lock = Lock()
        super().__init__((str(network.address), network.port if port is None else port), handler)
        self.hosts = {f'{network.address}:{self.server_port}'}

    def get_request(self):
        connection, peer = super().get_request()
        try:
            if not self.network.accepts(peer[0]):
                raise PermissionError('The peer is outside the configured line subnet.')
            return self.tls.wrap_socket(connection, server_side=True, do_handshake_on_connect=False), peer
        except OSError:
            connection.close()
            raise

    def reject_key(self, source):
        window = int(monotonic() // 60)
        with self.failed_key_lock:
            if window != self.failed_key_window:
                self.failed_key_window = window
                self.failed_keys.clear()
            attempts = self.failed_keys.get(source, 0) + 1
            if attempts <= 6:
                self.failed_keys[source] = attempts
        if attempts <= 5:
            LOG.warning('Module API key refused from %s', source)
            return 403
        if attempts == 6:
            LOG.warning('Module API key refusals rate-limited from %s', source)
        return 429
