import runpy
import unittest
from pathlib import Path


verify = runpy.run_path(str(Path(__file__).parents[1] / 'scripts' / 'verify.py'))
check_trace = verify['check_trace']

BIND = '2 bind(3, {sa_family=AF_INET, sin_port=htons(8871), sin_addr=inet_addr("127.0.0.1")}, 16) = 0\n'
ACCEPT = '2 accept4(3, {sa_family=AF_INET, sin_port=htons(42424), sin_addr=inet_addr("127.0.0.1")}, [16], SOCK_CLOEXEC) = 4\n'


class SocketTraceTest(unittest.TestCase):
    def test_records_loopback_http_listener_and_client(self):
        self.assertEqual(check_trace(BIND + ACCEPT), 1)

    def test_requires_positive_evidence(self):
        with self.assertRaises(RuntimeError):
            check_trace(BIND)

    def test_rejects_any_inet_connect_even_if_blocked(self):
        for host, port in [('192.168.12.248', 30004), ('127.0.0.1', 29999), ('127.0.0.1', 8871)]:
            with self.subTest(host=host, port=port), self.assertRaises(RuntimeError):
                check_trace(BIND + ACCEPT +
                    f'2 connect(8, {{sa_family=AF_INET, sin_port=htons({port}), '
                    f'sin_addr=inet_addr("{host}")}}, 16) = -1 ENETUNREACH\n')
