import hashlib
import json
from pathlib import Path
import zipfile

import pytest

from scripts.package_portable import package
from scripts.prepare_installer import prepare
from shiori import installer, update_runtime


def source_tree(tmp_path):
    source = tmp_path / 'program'
    source.mkdir()
    (source / 'shiori.exe').write_bytes(b'fixture program')
    (source / '中文说明.md').write_text('fixture documentation', 'utf-8')
    return source


@pytest.mark.parametrize('version', ['0.7.0', '0.2.1-beta'])
def test_built_zip_is_accepted_by_real_installer(tmp_path, monkeypatch, version):
    source = source_tree(tmp_path)
    archive = tmp_path / 'portable.zip'
    assert package(source, archive) == 2
    with zipfile.ZipFile(archive) as z:
        assert all('\\' not in x.orig_filename for x in z.infolist())
    payload = tmp_path / 'installer_payload'
    manifest = prepare(archive, payload, version)
    assert manifest['version'] == version
    assert json.loads((payload / 'manifest.json').read_text('utf-8')) == manifest
    monkeypatch.setattr(installer, 'is_instance_running', lambda _: False)
    monkeypatch.setattr(installer, '_acquire_application_guard', lambda _: lambda: None)
    target = tmp_path / 'installed'
    result = installer.install_payload(payload / 'payload.zip', manifest, target,
                                      desktop_shortcut=False, start_menu_shortcut=False)
    assert result['version'] == version
    assert (target / '中文说明.md').read_text('utf-8') == 'fixture documentation'
    record = json.loads((target / installer.INSTALL_RECORD).read_text('utf-8'))
    assert record['version'] == version
    assert record['files']


def test_source_update_accepts_exact_beta_archive_and_manifest_version(tmp_path):
    version = '0.2.1-beta'
    content = b"__version__ = '0.2.1-beta'\n"
    relative = 'shiori/__init__.py'
    manifest = {'version': version, 'files': {
        relative: hashlib.sha256(content).hexdigest(),
    }}
    archive = tmp_path / f'shiori-{version}-source.zip'
    with zipfile.ZipFile(archive, 'w') as output:
        output.writestr(f'shiori-{version}-source/{relative}', content)
        output.writestr(f'shiori-{version}-source/SOURCE_MANIFEST.json', json.dumps(manifest))
    stage = tmp_path / 'stage'
    stage.mkdir()
    files = update_runtime.stage_archive(archive, stage, 'source', version)
    assert (stage / relative).read_bytes() == content
    assert json.loads((stage / 'SOURCE_MANIFEST.json').read_text('utf-8')) == manifest
    assert files[relative] == manifest['files'][relative]
    assert set(files) == {relative, 'SOURCE_MANIFEST.json'}


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
