"""Installing a source package from this machine: held to the same checks as a
GitHub download, then handed to the normal switch. Only for whoever is at the
machine."""
import hashlib
import io
import json
import zipfile

import pytest
from fastapi.testclient import TestClient

from auth_helpers import password_login
from shiori import __version__, updates
from shiori.api import create_app

NEXT = '99.0.0-test.1'


def package(version=NEXT, *, manifest=True, flat=False, extra_root=False):
    """A minimal source package as the build script lays it out."""
    files = {'shiori/__init__.py': f"__version__ = '{version}'\n".encode(), 'README.md': b'fixture\n'}
    buffer, prefix = io.BytesIO(), '' if flat else f'shiori-{version}/'
    with zipfile.ZipFile(buffer, 'w', zipfile.ZIP_DEFLATED) as archive:
        for name, body in files.items():
            archive.writestr(prefix + name, body)
        if manifest:
            archive.writestr(prefix + 'SOURCE_MANIFEST.json', json.dumps(
                {'version': version, 'files': {name: hashlib.sha256(body).hexdigest() for name, body in files.items()}}))
        if extra_root:
            archive.writestr('another-root/notes.txt', 'x')
    return buffer.getvalue()


def manager(tmp_path, **runtime):
    return updates.UpdateManager(tmp_path / 'data', runtime={
        'current_version': __version__, 'repository': updates.REPOSITORY, 'releases_url': updates.RELEASES_URL,
        'mode': 'source', 'default_kind': 'source', 'install_supported': False, 'switch_supported': True,
        'update_kind': 'source', 'service': '', **runtime})


def saved(tmp_path, body, name='package.zip'):
    path = tmp_path / name
    path.write_bytes(body)
    return path, hashlib.sha256(body).hexdigest()


def test_a_checked_package_is_left_ready_for_the_normal_switch(tmp_path):
    item, (path, digest) = manager(tmp_path), saved(tmp_path, package())
    snapshot = item.stage_local(path, digest.upper())
    download = item.download
    assert download['status'] == 'ready' and download['tag'] == 'v' + NEXT and download['kind'] == 'source' and download['local'] is True
    assert download['name'] == f'shiori-{NEXT}-source.zip' and download['total'] == path.stat().st_size
    assert 'sha256' not in snapshot['download'] and snapshot['download']['status'] == 'ready'
    # The switch re-reads the staged copy and refuses it once it has been touched.
    staged = item.verified_file(download['id'])
    assert staged.read_bytes() == path.read_bytes() and staged != path
    staged.write_bytes(staged.read_bytes() + b'x')
    with pytest.raises(ValueError):
        item.verified_file(download['id'])
    assert updates.UpdateManager(tmp_path / 'data', runtime=item.runtime).download['tag'] == 'v' + NEXT   # survives a restart


@pytest.mark.parametrize('case', ['wrong_hash', 'bad_hash', 'not_a_zip', 'no_manifest', 'flat', 'two_roots', 'older', 'packaged_app', 'empty'])
def test_anything_else_is_refused_and_nothing_is_staged(tmp_path, case):
    item, body, runtime = manager(tmp_path), package(), {}
    if case == 'not_a_zip':
        body = b'MZ this is not a zip archive'
    elif case == 'no_manifest':
        body = package(manifest=False)
    elif case == 'flat':
        body = package(flat=True)
    elif case == 'two_roots':
        body = package(extra_root=True)
    elif case == 'older':
        body = package('0.0.1')
    elif case == 'packaged_app':
        item = manager(tmp_path, update_kind='portable')
    elif case == 'empty':
        body = b''
    path, digest = saved(tmp_path, body)
    stated = {'wrong_hash': '0' * 64, 'bad_hash': 'abc'}.get(case, digest)
    with pytest.raises(ValueError):
        item.stage_local(path, stated)
    assert item.download.get('status') != 'ready'
    assert not [entry for entry in (tmp_path / 'data' / 'updates').glob('*/*.zip')] if (tmp_path / 'data' / 'updates').exists() else True


def test_the_message_says_how_to_number_a_test_build(tmp_path):
    item, (path, digest) = manager(tmp_path), saved(tmp_path, package('0.0.1'))
    with pytest.raises(ValueError, match='下一个版本号的预发布号'):
        item.stage_local(path, digest)
    path, digest = saved(tmp_path, package(manifest=False))
    with pytest.raises(ValueError, match='源码清单'):
        item.stage_local(path, digest)


def test_the_endpoint_is_for_the_machine_itself_and_hands_over_to_the_switch(tmp_path, monkeypatch):
    app = create_app(tmp_path / 'data', start_workers=False)
    body = package()
    digest = hashlib.sha256(body).hexdigest()
    upload = lambda client, sha=digest: client.post('/api/updates/local', data={'sha256': sha},
                                                     files={'file': ('shiori-test-source.zip', body, 'application/zip')})
    app.state.updates.runtime.update(switch_supported=True, update_kind='source')
    launched = []
    monkeypatch.setattr(app.state.updates, 'launch_replacement', lambda download_id: launched.append(download_id) or {'ok': True})
    with TestClient(app) as local:
        assert upload(local).status_code == 401                        # not signed in
        password_login(local, app)
        remote = TestClient(app, base_url='https://192.168.1.2:8765', client=('192.168.1.10', 1234), cookies=local.cookies)
        assert remote.post('/api/updates/local', data={'sha256': digest},
                           files={'file': ('x.zip', body, 'application/zip')}).status_code in (401, 403)
        app.state.shutdown = None
        assert upload(local).status_code in (400, 422)                 # cannot restart itself: refuse before touching anything
        stopped = []
        app.state.shutdown = lambda: stopped.append(True)
        assert upload(local, '0' * 64).status_code in (400, 422) and not launched
        response = upload(local)
        assert response.status_code == 200, response.text
        assert launched == [app.state.updates.download['id']] and response.json()['handoff'] == {'ok': True}
        assert response.json()['download']['tag'] == 'v' + NEXT
        assert not list((tmp_path / 'data' / 'updates').glob('local-*.part'))   # the upload spool is gone
