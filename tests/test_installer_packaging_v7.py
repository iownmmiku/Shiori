import json
from pathlib import Path
import zipfile

import pytest

from scripts.package_portable import package
from scripts.prepare_installer import prepare
from shiori import installer


def source_tree(tmp_path):
    source = tmp_path / 'program'
    source.mkdir()
    (source / 'shiori.exe').write_bytes(b'fixture program')
    (source / '中文说明.md').write_text('fixture documentation', 'utf-8')
    return source


def test_built_zip_is_accepted_by_real_installer(tmp_path, monkeypatch):
    source = source_tree(tmp_path)
    archive = tmp_path / 'portable.zip'
    assert package(source, archive) == 2
    with zipfile.ZipFile(archive) as z:
        assert all('\\' not in x.orig_filename for x in z.infolist())
    payload = tmp_path / 'installer_payload'
    manifest = prepare(archive, payload, '0.7.0')
    monkeypatch.setattr(installer, 'is_instance_running', lambda _: False)
    monkeypatch.setattr(installer, '_acquire_application_guard', lambda _: lambda: None)
    target = tmp_path / 'installed'
    result = installer.install_payload(payload / 'payload.zip', manifest, target,
                                      desktop_shortcut=False, start_menu_shortcut=False)
    assert result['version'] == '0.7.0'
    assert (target / '中文说明.md').read_text('utf-8') == 'fixture documentation'
    assert json.loads((target / installer.INSTALL_RECORD).read_text('utf-8'))['files']


def test_prepare_checks_original_zip_name_before_windows_normalization(tmp_path):
    archive = tmp_path / 'windows.zip'
    with zipfile.ZipFile(archive, 'w') as z:
        z.writestr('shiori/shiori.exe', b'fixture')
    archive.write_bytes(archive.read_bytes().replace(b'shiori/shiori.exe', b'shiori\\shiori.exe'))
    with pytest.raises(ValueError):
        prepare(archive, tmp_path / 'payload', '0.7.0')
    assert not (tmp_path / 'payload/payload.zip').exists()


@pytest.mark.parametrize('relative', ['data/admin.token', '.qa-private/result.json'])
def test_packaging_refuses_user_and_test_data(tmp_path, relative):
    source = source_tree(tmp_path)
    secret = source / relative
    secret.parent.mkdir(parents=True)
    secret.write_bytes(b'never ship')
    archive = tmp_path / 'portable.zip'
    with pytest.raises(ValueError):
        package(source, archive)
    assert not archive.exists()
