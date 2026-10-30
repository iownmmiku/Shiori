from pathlib import Path
import shutil
import subprocess

import pytest


def test_shipped_lazy_page_loader():
    node = shutil.which('node')
    if not node:
        pytest.skip('Node.js unavailable')
    result = subprocess.run([node, str(Path(__file__).with_name('frontend_lazy_assets.cjs'))],
                            capture_output=True, text=True, encoding='utf-8', timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
