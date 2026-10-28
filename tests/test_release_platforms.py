import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest


@pytest.mark.parametrize('module',['shiori','qunyu'])
def test_cli_help_with_legacy_redirected_console_encoding(module):
    result=subprocess.run([sys.executable,'-m',module,'--help'],
        env={**os.environ,'PYTHONIOENCODING':'cp1252'},capture_output=True,timeout=15)
    assert result.returncode==0,result.stderr.decode('utf-8',errors='replace')
    assert 'Shiori 本地聊天助手' in result.stdout.decode('utf-8')


def test_non_directory_ancestor_is_left_for_path_validation(monkeypatch):
    from shiori import filesystem
    def stat():raise NotADirectoryError('ordinary file blocks a path ancestor')
    monkeypatch.setattr(filesystem,'Path',lambda value:SimpleNamespace(lstat=stat))
    assert filesystem.is_link(Path('blocked/child')) is False
