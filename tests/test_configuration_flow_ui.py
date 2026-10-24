"""Exercise the input controller, not a second copy of its debounce algorithm."""
from pathlib import Path
import shutil
import subprocess

import pytest


def test_local_message_collection_controller():
    node = shutil.which('node')
    if not node:
        pytest.skip('Node.js is optional for frontend checks')
    result = subprocess.run([node, str(Path(__file__).with_name('frontend_message_debounce.cjs'))],
                            capture_output=True, text=True, encoding='utf-8', timeout=20)
    assert result.returncode == 0, result.stdout + result.stderr


def test_nested_bot_model_configuration_preserves_other_routes():
    node = shutil.which('node')
    if not node:
        pytest.skip('Node.js is optional for frontend checks')
    result = subprocess.run([node, str(Path(__file__).with_name('frontend_bot_models.cjs'))],
                            capture_output=True, text=True, encoding='utf-8', timeout=20)
    assert result.returncode == 0, result.stdout + result.stderr
