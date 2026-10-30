"""The interrupted-update UI keeps authentication and chat maintenance intact."""
from fastapi.testclient import TestClient
import pytest

from auth_helpers import FIXTURE_PASSWORD, password_login
from shiori import __version__, update_recovery, update_runtime
from shiori.api import create_app


IDENTIFIER = 'e' * 32


def recovery_app(tmp_path, monkeypatch, *, mode):
    data, target = tmp_path / 'data', tmp_path / 'program'
    first = create_app(data, start_workers=False, allow_remote=True)
    with TestClient(first, base_url='http://127.0.0.1:8765', client=('127.0.0.1', 41001)) as client:
        if mode == 'password':
            password_login(client, first)
            first.state.settings.update({'webui': {'remote_enabled': True}})
        elif mode == 'local_only':
            assert client.post('/api/auth/bootstrap', json={'token': first.state.settings.token}).status_code == 200
            assert client.post('/api/auth/setup', json={'mode': 'local_only'}).status_code == 200
    target.mkdir()
    (target / 'shiori.exe').write_bytes(b'incomplete fixture program')
    work = data / 'updates' / IDENTIFIER
    backup = work / 'program-backup'
    backup.mkdir(parents=True)
    (backup / 'shiori.exe').write_bytes(b'old fixture program')
    update_runtime.atomic_json(backup / 'journal.json', {'schema': 2, 'changed': ['shiori.exe'], 'created': [],
        'files': {}, 'backup_hashes': {'shiori.exe': update_runtime.digest(backup / 'shiori.exe')}})
    files = update_runtime.backup_data(data, work / 'data-backup')
    update_runtime.atomic_json(work / 'data-backup-manifest.json', {'schema': 1, 'files': files})
    update_runtime.atomic_json(work / 'switch.json', {'target': str(target), 'data': str(data),
        'download_id': IDENTIFIER, 'version': __version__, 'sha256': 'f' * 64,
        'parent_pid': 42, 'port': 8765, 'launch': ['fixture-never-launched'], 'kind': 'portable'})
    update_runtime.atomic_json(data / 'updates' / 'state.json', {'id': IDENTIFIER, 'tag': 'v' + __version__,
        'kind': 'portable', 'name': f'shiori-{__version__}-Windows-x64.zip', 'status': 'installing'})
    update_runtime.atomic_json(data / 'updates' / 'install-result.json', {'download_id': IDENTIFIER,
        'status': 'replacing', 'lease_version': 1, 'at': '2025-01-01T00:00:00+00:00'})
    monkeypatch.setattr(update_recovery, 'application_base', lambda: target)
    app = create_app(data, start_workers=False, allow_remote=True)
    app.state.shutdown = lambda: pytest.fail('these authentication tests must not shut down the app')
    monkeypatch.setattr(app.state.updates, 'launch_recovery', lambda *_: pytest.fail('unauthorized recovery must not launch a worker'))
    return app


def assert_recovery_without_chat(client, app):
    response = client.get('/api/bootstrap')
    assert response.status_code == 200, response.text
    boot = response.json()
    assert boot['update_recovery']['can_rollback'] is True
    assert boot['settings'] == {} and boot['scopes'] == []
    status = client.get('/api/updates')
    assert status.status_code == 200 and status.json()['recovery']['can_rollback']
    before = app.state.store.list_messages('web:default')
    response = client.post('/api/chat', json={'scope_id': 'web:default', 'text': 'must stay blocked'})
    assert response.status_code == 503 and response.json()['code'] == 'update_in_progress'
    assert app.state.store.list_messages('web:default') == before


def test_local_only_restart_can_bootstrap_recovery_but_not_chat(tmp_path, monkeypatch):
    app = recovery_app(tmp_path, monkeypatch, mode='local_only')
    with TestClient(app, base_url='http://127.0.0.1:8765', client=('127.0.0.1', 41001)) as client:
        assert app.state.updates.startup_pending()
        # Local-only mode intentionally trusts a loopback request, even after
        # restart. Recovery must preserve that mode without opening it remotely.
        assert client.get('/api/bootstrap').status_code == 200
        assert client.post('/api/auth/bootstrap', json={'token': 'wrong'}).status_code == 403
        assert client.post('/api/auth/bootstrap', json={'token': app.state.settings.token}).status_code == 200
        assert client.get('/api/auth/status').json()['authenticated']
        assert_recovery_without_chat(client, app)
        remote = TestClient(app, base_url='https://192.168.1.2:8765', client=('192.168.1.10', 41002))
        try:
            assert remote.get('/api/bootstrap').status_code == 403
            assert remote.post('/api/updates/recover', json={'download_id': IDENTIFIER}).status_code == 403
            assert remote.post('/api/auth/bootstrap', json={'token': app.state.settings.token}).status_code == 403
        finally:
            remote.close()


def test_password_restart_still_requires_password_before_recovery(tmp_path, monkeypatch):
    app = recovery_app(tmp_path, monkeypatch, mode='password')
    with TestClient(app, base_url='http://127.0.0.1:8765', client=('127.0.0.1', 41001)) as client:
        assert client.post('/api/auth/bootstrap', json={'token': app.state.settings.token}).status_code == 200
        assert client.get('/api/bootstrap').status_code == 401
        assert client.post('/api/auth/login', json={'password': 'wrong-password'}).status_code == 401
        assert client.post('/api/auth/login', json={'password': FIXTURE_PASSWORD}).status_code == 200
        assert_recovery_without_chat(client, app)


def test_first_password_can_be_set_during_recovery_only_with_local_proof(tmp_path, monkeypatch):
    app = recovery_app(tmp_path, monkeypatch, mode='unset')
    with TestClient(app, base_url='http://127.0.0.1:8765', client=('127.0.0.1', 41001)) as client:
        assert client.post('/api/auth/setup', json={'password': FIXTURE_PASSWORD}).status_code == 403
        assert client.post('/api/session', json={'token': app.state.settings.token}).status_code == 200
        assert client.post('/api/auth/setup', json={'password': FIXTURE_PASSWORD}).status_code == 200
        assert_recovery_without_chat(client, app)


def test_remote_recovery_needs_login_and_local_remote_update_authorization(tmp_path, monkeypatch):
    app = recovery_app(tmp_path, monkeypatch, mode='password')
    with TestClient(app, base_url='http://127.0.0.1:8765', client=('127.0.0.1', 41001)):
        remote = TestClient(app, base_url='https://192.168.1.2:8765', client=('192.168.1.10', 41002))
        try:
            assert remote.post('/api/updates/recover', json={'download_id': IDENTIFIER}).status_code == 401
            assert remote.post('/api/auth/bootstrap', json={'token': app.state.settings.token}).status_code == 403
            assert remote.post('/api/auth/login', json={'password': FIXTURE_PASSWORD}).status_code == 200
            assert_recovery_without_chat(remote, app)
            assert remote.get('/api/updates').json()['switch_allowed'] is False
            response = remote.post('/api/updates/recover', json={'download_id': IDENTIFIER})
            assert response.status_code == 403
            assert '本机授权' in response.json()['detail']
            assert remote.post('/api/updates/remote-policy', json={'enabled': True}).status_code == 403
            assert not app.state.updates._launching
        finally:
            remote.close()
