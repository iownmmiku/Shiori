"""Compatibility boundaries for the local Shiori rename."""
import hashlib
import importlib
import json
from types import SimpleNamespace
import zipfile

import pytest

from shiori import __version__, data_paths, installer, updates, update_runtime
from shiori.access import AccessManager


@pytest.mark.parametrize('legacy', ['mikubot', 'qunyu'])
def test_old_imports_keep_canonical_class_identity(legacy):
    for name, attribute in [('provider', 'ProviderError'), ('db', 'Store'), ('settings', 'Settings')]:
        old = importlib.import_module(legacy + '.' + name)
        canonical = importlib.import_module('shiori.' + name)
        assert old is canonical
        assert getattr(old, attribute) is getattr(canonical, attribute)
        assert canonical.__spec__.name == 'shiori.' + name


@pytest.mark.parametrize('brand', ['mikubot', 'qunyu'])
def test_existing_independent_data_is_reused_without_copy(tmp_path, monkeypatch, brand):
    new = tmp_path/'shiori/data'
    old = tmp_path/brand/'data'
    old.mkdir(parents=True)
    (old/'qunyu.db').write_bytes(b'existing memory')
    monkeypatch.setattr(data_paths, 'default_data_dir', lambda: new)
    assert data_paths.resolve_data_dir(base=tmp_path/'program') == old
    assert not new.exists()
    assert (old/'qunyu.db').read_bytes() == b'existing memory'
    new.mkdir(parents=True)
    (new/'settings.json').write_text('{}')
    assert data_paths.resolve_data_dir(base=tmp_path/'program') == new


def test_legacy_local_identity_requires_existing_signature():
    access = AccessManager(SimpleNamespace(token='fixture-local-secret', get=lambda: {}))
    old = access.identity('local', 'mikubot')
    assert access.is_admin(old)
    assert access.is_admin(access.identity('local', 'shiori'))
    assert not access.is_admin(dict(old, account_id='shiori'))
    assert not access.is_admin(dict(old, proof='forged'))


def test_rename_does_not_restore_deleted_default_persona(tmp_path):
    from shiori.db import Store
    store = Store(tmp_path)
    with store._conn:
        store._conn.execute("DELETE FROM personas WHERE id='default'")
        store._conn.execute("UPDATE events SET kind='mikubot_persona_seed_v1' WHERE kind='shiori_persona_seed_v1'")
    store.close()
    reopened = Store(tmp_path)
    try:
        assert not any(item['id'] == 'default' for item in reopened.list_personas())
    finally:
        reopened.close()


@pytest.mark.parametrize('brand', ['shiori', 'mikubot'])
def test_published_asset_names_remain_downloadable(tmp_path, brand):
    name = f'{brand}-{__version__}-source.zip'
    release = updates.normalize_release({'id': 12, 'tag_name': 'v'+__version__, 'assets': [{
        'id': 13, 'name': name, 'state': 'uploaded', 'size': 100,
        'browser_download_url': updates._asset_url('v'+__version__, name),
        'digest': 'sha256:'+'a'*64}]})
    assert release['assets']['source']['name'] == name
    manager = updates.UpdateManager(tmp_path)
    record = {'id': 'a'*32, 'tag': 'v'+__version__, 'kind': 'source', 'name': name}
    assert manager._file_path(record).name == name
    with pytest.raises(ValueError): manager._file_path(dict(record, name='../'+name))


@pytest.mark.parametrize('brand', ['shiori', 'mikubot'])
def test_versioned_source_archive_accepts_original_root(tmp_path, brand):
    archive, stage = tmp_path/'source.zip', tmp_path/'stage'
    stage.mkdir()
    files = {brand+'/__main__.py': b'# fixture', 'requirements-server.txt': b'# fixture'}
    manifest = {'version': __version__, 'files': {name: hashlib.sha256(value).hexdigest() for name, value in files.items()}}
    with zipfile.ZipFile(archive, 'w') as out:
        for name, value in {**files, 'SOURCE_MANIFEST.json': json.dumps(manifest).encode()}.items():
            out.writestr(f'{brand}-{__version__}-source/'+name, value)
    result = update_runtime.stage_archive(archive, stage, 'source', __version__)
    assert brand+'/__main__.py' in result


def test_legacy_install_marker_is_used_for_upgrade(tmp_path):
    target = tmp_path/'previous-install'
    target.mkdir()
    old = {'schema': 1, 'version': __version__, 'files': {'mikubot.exe': 'a'*64}}
    (target/'.mikubot-install.json').write_text(json.dumps(old))
    assert installer._old_record(target) == old
    assert installer.gui_install_target(target) == target


def test_update_legacy_ownership_removes_owned_files_and_rollback_restores(tmp_path):
    target, stage, data = tmp_path/'program', tmp_path/'stage', tmp_path/'data'
    for directory in (target, stage, data): directory.mkdir()
    (target/'mikubot.exe').write_bytes(b'old')
    (stage/'shiori.exe').write_bytes(b'new')
    marker = target/'.mikubot-install.json'
    marker.write_text(json.dumps({'files': {'mikubot.exe': update_runtime.digest(target/'mikubot.exe')}}))
    previous = marker.read_bytes()
    backup = tmp_path/'backup'
    journal = update_runtime.replace_program(stage, target, {'shiori.exe': update_runtime.digest(stage/'shiori.exe')}, backup, data)
    assert not (target/'mikubot.exe').exists()
    update_runtime.rollback_program(target, backup, journal, data)
    assert (target/'mikubot.exe').read_bytes() == b'old'
    assert marker.read_bytes() == previous
    assert not (target/'shiori.exe').exists()


def test_mixed_archive_roots_are_rejected(tmp_path):
    archive, stage = tmp_path/'mixed.zip', tmp_path/'stage'
    stage.mkdir()
    with zipfile.ZipFile(archive, 'w') as out:
        out.writestr('shiori/shiori.exe', b'new')
        out.writestr('mikubot/other.txt', b'unexpected')
    with pytest.raises(ValueError): update_runtime.stage_archive(archive, stage, 'portable', __version__)
