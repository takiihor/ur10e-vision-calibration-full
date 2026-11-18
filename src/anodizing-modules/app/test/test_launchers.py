"""Launchers resolve the configured interpreter the same way the shell does."""
import os
from pathlib import Path
import subprocess
import tempfile
import unittest


class LauncherTests(unittest.TestCase):
    def test_interpreter_names_are_resolved_from_path(self):
        bundle = Path(__file__).parents[2]
        with tempfile.TemporaryDirectory() as tmp:
            interpreter = Path(tmp) / 'test-python'
            interpreter.write_text('#!/bin/sh\nprintf "%s\\n" "$@"\n')
            interpreter.chmod(0o755)
            env = dict(os.environ, PATH=tmp + os.pathsep + os.environ['PATH'],
                       ANODIZING_PYTHON='test-python')
            for name in ('test-all.bash', 'run-ipc.bash'):
                with self.subTest(launcher=name):
                    result = subprocess.run(['bash', str(bundle / 'tools' / name)],
                        env=env, capture_output=True, text=True, timeout=10)
                    self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                    self.assertIn('-m\n', result.stdout)
