"""Run both layout and shell interaction regressions in the pytest CI suite."""
from pathlib import Path
import shutil
import subprocess

import pytest


@pytest.mark.parametrize("script", [
    "frontend_sidebar_layout.cjs",
    "frontend_sidebar_interactions.cjs",
])
def test_sidebar_ui(script):
    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js unavailable")
    result = subprocess.run(
        [node, str(Path(__file__).with_name(script))],
        capture_output=True, text=True, encoding="utf-8", timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
