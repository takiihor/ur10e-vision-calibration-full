import os
import runpy
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


verify = runpy.run_path(str(Path(__file__).parents[1] / 'scripts' / 'verify.py'))
check_trace = verify['check_trace']
launcher = Path(__file__).parents[1] / 'scripts' / 'verify'

BIND = '2 bind(3, {sa_family=AF_INET, sin_port=htons(8871), sin_addr=inet_addr("127.0.0.1")}, 16) = 0\n'
ACCEPT = '2 accept4(3, {sa_family=AF_INET, sin_port=htons(42424), sin_addr=inet_addr("127.0.0.1")}, [16], SOCK_CLOEXEC) = 4\n'


class EvidenceNameTest(unittest.TestCase):
    def test_json_route_keeps_its_documented_filename(self):
        api = verify['api']
        with tempfile.TemporaryDirectory() as tmp, patch.dict(api.__globals__, {
                'EVIDENCE': Path(tmp), 'request': lambda path, token: b'{}'}):
            api('/preview/collision-scene.example.json', 'fixture')
            api('/api/status', 'fixture')
            self.assertEqual(sorted(p.name for p in Path(tmp).iterdir()),
                             ['collision-scene.example.json', 'status.json'])


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


class InterpreterSelectionTest(unittest.TestCase):
    def setUp(self):
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        self.root = Path(scratch.name)
        self.launcher = self.root / launcher.relative_to(launcher.parents[4])
        self.launcher.parent.mkdir(parents=True)
        shutil.copy2(launcher, self.launcher)
        subprocess.run(['git', 'init', '-q', str(self.root)], check=True)
        subprocess.run(['git', '-C', str(self.root), '-c', 'user.name=Verifier',
                        '-c', 'user.email=verifier@example.test', 'commit', '-qm', 'Fixture',
                        '--allow-empty'], check=True)
        bwrap = self.root / 'bwrap'
        bwrap.write_text('#!/bin/sh\nprintf "%s\\n" "$@"\n')
        bwrap.chmod(0o755)
        self.env = dict(os.environ, PATH=f'{self.root}:{os.environ["PATH"]}')
        self.env.pop('ANODIZING_PYTHON', None)
        self.env.pop('ANODIZING_VENV', None)

    def run_launcher(self):
        return subprocess.run([self.launcher, 'doctor', self.root], env=self.env,
                              capture_output=True, text=True)

    def test_uses_explicit_interpreter(self):
        python = self.root / 'custom-python'
        python.symlink_to(sys.executable)
        self.env['ANODIZING_PYTHON'] = str(python)
        result = self.run_launcher()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(f'--\n{python}\n-I\n-B\n', result.stdout)

    def test_uses_existing_launcher_venv(self):
        default = self.root / '.venv'
        custom = self.root / 'custom venv'
        subprocess.run([sys.executable, '-m', 'venv', '--without-pip', default, custom], check=True)
        for venv in (default, custom):
            with self.subTest(venv=venv.name):
                if venv == custom:
                    self.env['ANODIZING_VENV'] = str(custom)
                result = self.run_launcher()
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn(f'--\n{venv / "bin/python"}\n-I\n-B\n', result.stdout)

    def test_mounts_runtime_at_the_alias_used_by_the_venv(self):
        venv = self.root / '.venv'
        subprocess.run([sys.executable, '-m', 'venv', '--without-pip', venv], check=True)
        base = Path(sys.base_prefix)
        alias = self.root / 'runtime alias'
        alias.symlink_to(base, target_is_directory=True)
        python = venv / 'bin/python'
        python.unlink()
        python.symlink_to(alias / Path(sys._base_executable).resolve().relative_to(base))
        result = self.run_launcher()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(f'--ro-bind\n{base}\n{alias}\n', result.stdout)

    def test_explains_how_to_supply_a_missing_interpreter(self):
        result = self.run_launcher()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('Set ANODIZING_PYTHON', result.stderr)

    def test_rejects_an_interpreter_that_fails_the_version_check(self):
        self.env['ANODIZING_PYTHON'] = '/bin/false'
        result = self.run_launcher()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('requires CPython 3.12', result.stderr)
