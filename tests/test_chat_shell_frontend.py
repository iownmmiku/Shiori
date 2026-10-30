"""Exercise the shipped shell's local Chat / QQ navigation boundaries."""
from pathlib import Path
import shutil
import subprocess

import pytest


def test_chat_qq_shell_navigation():
    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js unavailable")
    result = subprocess.run(
        [node, str(Path(__file__).with_name("frontend_chat_shell.cjs"))],
        capture_output=True, text=True, encoding="utf-8", timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
