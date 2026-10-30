"""Focused offline regressions for long Chat history and unsaved configuration."""
from pathlib import Path
import shutil
import subprocess

import pytest


@pytest.mark.parametrize('script', ['frontend_form_drafts.cjs', 'frontend_chat_history.cjs', 'frontend_chat_harness.cjs'])
def test_frontend_workflow(script):
    node = shutil.which('node')
    if not node:
        pytest.skip('Node.js unavailable')
    result = subprocess.run([node, str(Path(__file__).with_name(script))], capture_output=True,
                            text=True, encoding='utf-8', timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
