"""Execute the real interest UI helpers using authored status/API substitutes."""
from pathlib import Path
import shutil
import subprocess

import pytest


def test_interest_diagnostics_rendering_and_persistence():
    node = shutil.which('node')
    if not node:
        pytest.skip('Node.js is required for the interest UI contract')
    result = subprocess.run(
        [node, str(Path(__file__).with_name('frontend_interest_diagnostics.cjs'))],
        capture_output=True, text=True, encoding='utf-8', timeout=20,
    )
    assert result.returncode == 0, result.stdout + result.stderr
