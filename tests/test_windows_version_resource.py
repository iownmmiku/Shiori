"""Validate the exact resource format consumed by the Windows EXE builder."""
from pathlib import Path
import subprocess
import sys

import pytest

from scripts.write_windows_version import render_version_info, write_version_info


ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize('kind, filename, description', [
    ('app', 'shiori.exe', 'Shiori Desktop'),
    ('installer', 'shiori-0.1.0-Windows-Setup.exe', 'Shiori Setup'),
])
def test_pyinstaller_parses_public_release_resource(tmp_path, kind, filename, description):
    versioninfo = pytest.importorskip('PyInstaller.utils.win32.versioninfo')
    destination = tmp_path / kind / 'version.txt'
    write_version_info(destination, '0.1.0', kind)
    info = versioninfo.load_version_info_from_text_file(destination)
    assert info.ffi.fileVersionMS == info.ffi.productVersionMS == 1
    assert info.ffi.fileVersionLS == info.ffi.productVersionLS == 0
    strings = {item.name: item.val for item in info.kids[0].kids[0].kids}
    assert strings == {
        'FileDescription': description,
        'FileVersion': '0.1.0',
        'InternalName': filename[:-4],
        'OriginalFilename': filename,
        'ProductName': 'Shiori',
        'ProductVersion': '0.1.0',
    }
    assert info.kids[1].kids[0].name == 'Translation'
    assert info.kids[1].kids[0].kids == [1033, 1200]
    # Exercise serialization too, rather than accepting a parse-only stub.
    raw = info.toRaw()
    assert raw and 'shiori'.encode('utf-16le') in raw


@pytest.mark.parametrize('version, kind', [
    ('0.1', 'app'), ('0.1.0-preview', 'app'), ('0.1.0\n', 'app'),
    ('0.1.65536', 'app'), ('0.1.0', 'unknown'),
])
def test_invalid_resource_inputs_are_rejected_before_any_output(tmp_path, version, kind):
    destination = tmp_path / 'not-created' / 'version.txt'
    with pytest.raises(ValueError):
        write_version_info(destination, version, kind)
    assert not destination.parent.exists()


def test_version_resource_cli_writes_the_installer_identity(tmp_path):
    target = tmp_path / '含空格的 版本资源' / 'installer.txt'
    result = subprocess.run([sys.executable, str(ROOT / 'scripts/write_windows_version.py'), str(target),
                             '--version', '0.1.0', '--kind', 'installer'], capture_output=True, timeout=10)
    assert result.returncode == 0, result.stderr.decode('utf-8', errors='replace')
    assert target.read_text('utf-8') == render_version_info('0.1.0', 'installer')


def test_both_windows_build_commands_attach_their_own_version_resource():
    build = (ROOT / 'scripts/build.ps1').read_text('utf-8-sig')
    app = next(line for line in build.splitlines() if '-m PyInstaller ' in line and '--onedir ' in line)
    installer = next(line for line in build.splitlines() if '-m PyInstaller ' in line and '--onefile ' in line)
    assert '--version-file $taskAppVersionFile' in app
    assert '--version-file $taskInstallerVersionFile' in installer
    assert 'scripts/write_windows_version.py $taskAppVersionFile --version $taskVersion --kind app' in build
    assert 'scripts/write_windows_version.py $taskInstallerVersionFile --version $taskVersion --kind installer' in build
