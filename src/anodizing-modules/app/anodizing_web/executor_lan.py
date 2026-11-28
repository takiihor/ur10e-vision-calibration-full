"""The keyed executor API's line-LAN HTTPS transport."""
from http.server import ThreadingHTTPServer
from ipaddress import IPv4Address, IPv4Interface, IPv4Network
import logging
import ssl
from socket import SHUT_RDWR
import stat
from threading import BoundedSemaphore, Lock, Timer
from time import monotonic

from .site_profile import section


LOG = logging.getLogger(__name__)


class GatewayNetwork:
    def __init__(self, address, subnet, robot_subnet, port, *, stop_port=None, katana_cidr=None):
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
        self.stop_port = stop_port
        self.katana_address = None
        if stop_port is not None:
            if type(stop_port) is not int or not 1 <= stop_port <= 65535 or stop_port == port:
                raise ValueError('Choose a separate fixed port for the Katana Stop API.')
            if not isinstance(katana_cidr, str):
                raise ValueError('Set Katana’s static /32 address for the Stop API.')
            katana = IPv4Interface(katana_cidr)
            if katana.network.prefixlen != 32 or katana.ip not in self.subnet:
                raise ValueError('Set Katana’s static /32 address inside the line subnet.')
            if katana.ip == self.address:
                raise ValueError('Choose a Katana Stop address different from the IPC address.')
            self.katana_address = str(katana.ip)

    @classmethod
    def configured(cls, root, *, simulated):
        profile = section(root, 'line_network')
        if 'line_subnet' not in profile:
            return None
        config = cls(profile.get('ipc_address'), profile['line_subnet'], profile.get('ur10e_subnet'),
                     profile.get('module_api_port', 18875), stop_port=profile.get('stop_api_port'),
                     katana_cidr=profile.get('katana_cidr'))
        if config.address.is_loopback and not simulated:
            raise ValueError('Choose the IPC line-LAN address for the live module API.')
        return config

    def accepts(self, source):
        return IPv4Address(source) in self.subnet


def client_context(certificate):
    return ssl.create_default_context(cafile=certificate)


class BoundedListener(ThreadingHTTPServer):
    daemon_threads = False
    connection_limit = 16
    request_s = 8

    def __init__(self, address, handler):
        self.connection_slots = BoundedSemaphore(self.connection_limit)
        self.request_lock = Lock()
        self.requests = {}
        super().__init__(address, handler)

    def _budget(self, peer):
        return self.connection_slots

    def _initial_timeout(self):
        return self.request_s

    def process_request(self, request, client_address):
        slots = self._budget(client_address)
        if not slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            slots.release()
            raise

    def process_request_thread(self, request, client_address):
        slots = self._budget(client_address)
        try:
            try:
                with self.request_lock:
                    self._request_deadline(request, self._initial_timeout(), slots)
            except RuntimeError:
                self.shutdown_request(request)
                return
            super().process_request_thread(request, client_address)
        finally:
            with self.request_lock:
                timer, slots = self.requests.pop(request, (None, slots))
                if timer:
                    timer.cancel()
                slots.release()

    def _request_deadline(self, request, seconds, slots):
        # A socket read timeout alone restarts after each received byte.
        def expire():
            with self.request_lock:
                if self.requests.get(request) == (timer, slots):
                    try:
                        request.shutdown(SHUT_RDWR)
                    except OSError:
                        pass
        timer = Timer(seconds, expire)
        timer.daemon = True
        timer.start()
        self.requests[request] = timer, slots


class HttpsListener(BoundedListener):
    scheme = 'https'
    local_connection_limit = 4
    stop_connection_limit = 4
    preauth_s = .5

    def __init__(self, network, root, handler, *, port=None, peer=None):
        self.network = network
        self.peer = peer
        self.local_slots = BoundedSemaphore(self.local_connection_limit)
        self.stop_slots = BoundedSemaphore(self.stop_connection_limit)
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

    def _budget(self, peer):
        if self.peer is None and peer[0] == str(self.network.address):
            return self.local_slots
        return super()._budget(peer)

    def _initial_timeout(self):
        return self.preauth_s if self.peer is not None else self.request_s

    def get_request(self):
        connection, peer = super().get_request()
        try:
            if not self.network.accepts(peer[0]) or (self.peer is not None and peer[0] != self.peer):
                raise PermissionError('The peer is outside the configured client addresses.')
            return self.tls.wrap_socket(connection, server_side=True, do_handshake_on_connect=False), peer
        except OSError:
            connection.close()
            raise

    def admit_stop(self, request):
        if self.peer is None:
            return True
        with self.request_lock:
            if not self.stop_slots.acquire(blocking=False):
                return False
            timer, slots = self.requests[request]
            try:
                self._request_deadline(request, self.request_s, self.stop_slots)
            except BaseException:
                self.stop_slots.release()
                raise
            timer.cancel()
            slots.release()
            return True

    def key_limited(self, source):
        with self.failed_key_lock:
            return (int(monotonic() // 60) == self.failed_key_window
                    and self.failed_keys.get(source, 0) >= 6)

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
