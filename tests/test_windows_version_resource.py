"""Validate the exact resource format consumed by the Windows EXE builder."""
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

from scripts.write_windows_version import render_version_info, write_version_info


ROOT = Path(__file__).resolve().parents[1]


def test_build_script_is_readable_by_windows_powershell():
    script = ROOT / 'scripts/build.ps1'
    # Windows PowerShell 5.1 otherwise reads the Chinese strings as ANSI.
    assert script.read_bytes().startswith(b'\xef\xbb\xbf')
    if sys.platform != 'win32':
        return
    shell = shutil.which('powershell.exe')
    assert shell, 'Windows PowerShell is required to validate the Windows builder'
    path = str(script).replace("'", "''")
    command = (
        "$tokens = $null; $issues = $null; "
        "[void][System.Management.Automation.Language.Parser]::ParseFile("
        f"'{path}', [ref]$tokens, [ref]$issues); "
        "if ($issues.Count) { $issues | ForEach-Object { $_.Message }; exit 1 }"
    )
    result = subprocess.run([shell, '-NoProfile', '-NonInteractive', '-Command', command],
                            capture_output=True, timeout=20)
    assert result.returncode == 0, (result.stdout + result.stderr).decode('utf-8', errors='replace')


@pytest.mark.parametrize('version, numeric, flags', [
    ('0.1.0', (0, 1, 0), 0),
    ('0.2.1-beta', (0, 2, 1), 2),
    ('0.2.1-rc.1', (0, 2, 1), 2),
])
@pytest.mark.parametrize('kind, description', [
    ('app', 'Shiori Desktop'),
    ('installer', 'Shiori Setup'),
])
def test_pyinstaller_parses_public_release_resource(tmp_path, kind, description, version, numeric, flags):
    versioninfo = pytest.importorskip('PyInstaller.utils.win32.versioninfo')
    destination = tmp_path / kind / 'version.txt'
    write_version_info(destination, version, kind)
    info = versioninfo.load_version_info_from_text_file(destination)
    filename = 'shiori.exe' if kind == 'app' else f'shiori-{version}-Windows-Setup.exe'
    assert info.ffi.fileVersionMS == info.ffi.productVersionMS == numeric[0] << 16 | numeric[1]
    assert info.ffi.fileVersionLS == info.ffi.productVersionLS == numeric[2] << 16
    assert info.ffi.fileFlags == flags
    strings = {item.name: item.val for item in info.kids[0].kids[0].kids}
    assert strings == {
        'FileDescription': description,
        'FileVersion': version,
        'InternalName': filename[:-4],
        'OriginalFilename': filename,
        'ProductName': 'Shiori',
        'ProductVersion': version,
    }
    assert info.kids[1].kids[0].name == 'Translation'
    assert info.kids[1].kids[0].kids == [1033, 1200]
    # Exercise serialization too, rather than accepting a parse-only stub.
    raw = info.toRaw()
    assert raw and 'shiori'.encode('utf-16le') in raw


@pytest.mark.parametrize('version, kind', [
    ('0.1', 'app'), ('0.1.0-../preview', 'app'), ('0.1.0\n', 'app'),
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
