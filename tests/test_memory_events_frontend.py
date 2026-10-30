"""Exercise structured event authoring and canonical-source UI boundaries."""
from pathlib import Path
import shutil
import subprocess

import pytest


def test_memory_event_frontend():
    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js unavailable")
    result = subprocess.run(
        [node, str(Path(__file__).with_name("frontend_memory_events.cjs"))],
        capture_output=True, text=True, encoding="utf-8", timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
