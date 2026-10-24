"""Run shipped browser chat logic against offline draft/attachment fixtures."""
from pathlib import Path
import shutil
import subprocess

import pytest


def test_drafts_and_shared_attachment_references():
    node = shutil.which('node')
    if not node:
        pytest.skip('Node.js unavailable')
    result = subprocess.run([node, str(Path(__file__).with_name('frontend_chat_drafts.cjs'))],
                            capture_output=True, text=True, encoding='utf-8', timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
