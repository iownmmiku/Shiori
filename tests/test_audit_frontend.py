from pathlib import Path
import shutil
import subprocess

import pytest


def test_backup_and_canonical_evidence_ui():
    node = shutil.which('node')
    if not node: pytest.skip('Node.js unavailable')
    result = subprocess.run([node, str(Path(__file__).with_name('frontend_backup_evidence.cjs'))],
                            capture_output=True, text=True, encoding='utf-8', timeout=25)
    assert result.returncode == 0, result.stdout + result.stderr
