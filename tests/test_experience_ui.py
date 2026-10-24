"""Exercise the dependency-free UI's trust boundaries and API interactions."""
from pathlib import Path
import shutil
import subprocess
import pytest


def test_experience_ui_safety_and_api_interactions():
    node = shutil.which('node')
    if not node:
        pytest.skip('Optional frontend regression requires Node.js')
    result = subprocess.run([node,str(Path(__file__).with_name('frontend_experience.cjs'))],capture_output=True,text=True,encoding='utf-8',timeout=20)
    assert result.returncode == 0, result.stdout + result.stderr


def test_plugin_and_statistics_page_dispatch_and_metrics():
    node = shutil.which('node')
    if not node: pytest.skip('Optional frontend regression requires Node.js')
    result = subprocess.run([node,str(Path(__file__).with_name('frontend_plugins_statistics.cjs'))],capture_output=True,text=True,encoding='utf-8',timeout=20)
    assert result.returncode == 0, result.stdout + result.stderr


def test_model_output_configuration_and_stored_message_boundaries():
    node = shutil.which('node')
    if not node: pytest.skip('Optional frontend regression requires Node.js')
    result = subprocess.run([node,str(Path(__file__).with_name('frontend_model_output.cjs'))],capture_output=True,text=True,encoding='utf-8',timeout=20)
    assert result.returncode == 0, result.stdout + result.stderr


def test_password_ui_bootstrap_login_and_recovery_boundaries():
    node = shutil.which('node')
    if not node:
        pytest.skip('Optional frontend regression requires Node.js')
    result = subprocess.run([node,str(Path(__file__).with_name('frontend_auth.cjs'))],capture_output=True,text=True,encoding='utf-8',timeout=20)
    assert result.returncode == 0, result.stdout + result.stderr


def test_model_catalog_and_runtime_log_interactions():
    node = shutil.which('node')
    if not node:
        pytest.skip('Optional frontend regression requires Node.js')
    result = subprocess.run([node,str(Path(__file__).with_name('frontend_workspace_pages.cjs'))],capture_output=True,text=True,encoding='utf-8',timeout=20)
    assert result.returncode == 0, result.stdout + result.stderr


def test_github_update_selection_and_install_boundaries():
    node = shutil.which('node')
    if not node: pytest.skip('Optional frontend regression requires Node.js')
    result = subprocess.run([node,str(Path(__file__).with_name('frontend_updates.cjs'))],capture_output=True,text=True,encoding='utf-8',timeout=20)
    assert result.returncode == 0, result.stdout + result.stderr


def test_bot_configuration_navigation_inheritance_and_admission():
    node = shutil.which('node')
    if not node: pytest.skip('Optional frontend regression requires Node.js')
    result = subprocess.run([node, str(Path(__file__).with_name('frontend_bot_config.cjs'))],
                            capture_output=True, text=True, encoding='utf-8', timeout=20)
    assert result.returncode == 0, result.stdout + result.stderr


def test_modal_notifications_and_bounded_memory_page_interactions():
    node = shutil.which('node')
    if not node: pytest.skip('Optional frontend regression requires Node.js')
    result = subprocess.run([node, str(Path(__file__).with_name('frontend_memory_scale.cjs'))],
                            capture_output=True, text=True, encoding='utf-8', timeout=20)
    assert result.returncode == 0, result.stdout + result.stderr


def test_memory_archive_default_range_and_culture_independence():
    node = shutil.which('node')
    if not node: pytest.skip('Optional frontend regression requires Node.js')
    result = subprocess.run([node, str(Path(__file__).with_name('frontend_memory_scope.cjs'))],
                            capture_output=True, text=True, encoding='utf-8', timeout=20)
    assert result.returncode == 0, result.stdout + result.stderr


def test_sticker_configuration_library_and_editing_interactions():
    node = shutil.which('node')
    if not node: pytest.skip('Optional frontend regression requires Node.js')
    result = subprocess.run([node, str(Path(__file__).with_name('frontend_stickers.cjs'))],
                            capture_output=True, text=True, encoding='utf-8', timeout=20)
    assert result.returncode == 0, result.stdout + result.stderr
