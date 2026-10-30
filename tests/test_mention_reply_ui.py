"""Run reply-mode edits through the shipped forms and save controllers."""
from pathlib import Path
import shutil
import subprocess

import pytest


def test_mention_reply_form_roundtrip():
    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js unavailable")
    result = subprocess.run(
        [node, str(Path(__file__).with_name("frontend_mention_reply.cjs"))],
        capture_output=True, text=True, encoding="utf-8", timeout=20,
    )
    assert result.returncode == 0, result.stdout + result.stderr
