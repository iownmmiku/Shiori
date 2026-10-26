"""Execute the admission UI and save handler, including preserved legacy rules."""
from pathlib import Path
import shutil
import subprocess

import pytest


def test_admission_policy_rendering_and_submission():
    node = shutil.which('node')
    if not node:
        pytest.skip('Node.js is required for the frontend contract')
    result = subprocess.run([node, str(Path(__file__).with_name('frontend_admission_policy.cjs'))],
                            capture_output=True, text=True, encoding='utf-8', timeout=20)
    assert result.returncode == 0, result.stdout + result.stderr
