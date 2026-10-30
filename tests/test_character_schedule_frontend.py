"""Run the character-life controller's asynchronous and permission regressions."""
from pathlib import Path
import shutil
import subprocess

import pytest


def test_character_schedule_controller():
    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js unavailable")
    result = subprocess.run(
        [node, str(Path(__file__).with_name("frontend_character_schedule.cjs"))],
        capture_output=True, text=True, encoding="utf-8", timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
