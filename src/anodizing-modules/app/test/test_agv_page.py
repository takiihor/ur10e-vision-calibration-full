"""Run the AGV page JavaScript with offline browser and API fixtures."""
from pathlib import Path
import shutil
import subprocess

import pytest


@pytest.mark.skipif(not shutil.which('node'), reason='Node is unavailable')
@pytest.mark.parametrize('scenario', ['reload', 'blocked', 'single-port', 'ambiguous', 'rejected', 'escape-stop'])
def test_agv_page(scenario):
    fixture = Path(__file__).with_name('agv_page.cjs')
    result = subprocess.run(['node', str(fixture), scenario], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stdout + result.stderr
