"""Execute the web-search UI contract without a browser, keys or network calls."""
from pathlib import Path
import shutil
import subprocess

import pytest


def test_web_search_settings_and_safe_results():
    node = shutil.which('node')
    if not node:
        pytest.skip('Node.js is required for the frontend contract')
    result = subprocess.run(
        [node, str(Path(__file__).with_name('frontend_web_search.cjs'))],
        capture_output=True, text=True, encoding='utf-8', timeout=20,
    )
    assert result.returncode == 0, result.stdout + result.stderr
