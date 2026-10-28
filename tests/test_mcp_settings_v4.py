"""MCP configuration boundaries, credential handling and backup compatibility."""
import io
import json
import zipfile

import pytest
from fastapi.testclient import TestClient

from shiori.api import create_app
from shiori.settings import Settings
from auth_helpers import password_login


def server(server_id='notes', **changes):
    return dict(id=server_id, name='个人笔记', url='https://mcp.example.com/mcp',
                token='fixture-mcp-secret-' + server_id, enabled=True,
                allowed_tools=['notes.add'], **changes)


def test_mcp_tokens_masked_encrypted_and_preserved_by_id(tmp_path):
    settings = Settings(tmp_path)
    first, second = server('notes'), server('calendar')
    public = settings.update({'mcp_servers': [first, second]})
    assert all('token' not in item and item['token_set'] for item in public['mcp_servers'])
    disk = settings.path.read_text('utf-8')
    assert first['token'] not in disk and second['token'] not in disk
    saved = json.loads(disk)
    assert saved['mcp_servers'][0]['token'].startswith(('dpapi:', 'local:'))
    reloaded = Settings(tmp_path)
    assert [item['token'] for item in reloaded.get()['mcp_servers']] == [first['token'], second['token']]

    reordered = list(reversed(reloaded.public()['mcp_servers']))
    reordered[0]['token'] = ''
    reordered[0]['name'] = '日程服务'
    reloaded.update({'mcp_servers': reordered})
    by_id = {item['id']: item for item in reloaded.get()['mcp_servers']}
    assert by_id['notes']['token'] == first['token']
    assert by_id['calendar']['token'] == second['token']
    assert by_id['calendar']['name'] == '日程服务'

    reordered[0]['clear_token'] = True
    reloaded.update({'mcp_servers': reordered})
    assert reloaded.get()['mcp_servers'][0]['token'] == ''
    assert not reloaded.public()['mcp_servers'][0]['token_set']
    assert reloaded.get()['mcp_servers'][1]['token'] == first['token']


def test_mcp_list_replace_cannot_resurrect_removed_credential(tmp_path):
    settings = Settings(tmp_path)
    original = server()
    settings.update({'mcp_servers': [original]})
    settings.update({'mcp_servers': []})
    original.pop('token')
    settings.update({'mcp_servers': [original]})
    assert settings.get()['mcp_servers'][0]['token'] == ''
    assert not settings.public()['mcp_servers'][0]['token_set']


@pytest.mark.parametrize('url', [
    '', 'file:///C:/notes', 'ws://localhost:3000/mcp',
    'http://mcp.example.com/mcp', 'https://alice:password@example.com/mcp',
    'https://example.com/mcp?token=secret', 'https://example.com/mcp#secret',
    'https://192.168.1.3/mcp', 'https://10.0.0.1/mcp',
    'https://169.254.169.254/mcp', 'https://example.com:99999/mcp',
    'https://example.com:0/mcp', 'https://foo.local/mcp',
    'https://foo.internal/mcp', 'https://example.com/\\mcp',
])
def test_mcp_bad_addresses_rejected_without_mutation(tmp_path, url):
    settings = Settings(tmp_path)
    before = settings.get()
    item = server()
    item['url'] = url
    with pytest.raises(ValueError):
        settings.update({'mcp_servers': [item]})
    assert settings.get() == before
    assert not settings.path.exists()


@pytest.mark.parametrize('url', ['http://localhost:3000/mcp', 'http://127.0.0.1:3000/mcp',
                                  'http://[::1]:3000/mcp', 'https://mcp.example.com/mcp'])
def test_mcp_supported_addresses_and_allowlist(tmp_path, url):
    settings = Settings(tmp_path)
    item = server()
    item.update(url=url, allowed_tools=['notes.add', 'notes.list', 'notes.add'])
    settings.update({'mcp_servers': [item]})
    assert settings.get()['mcp_servers'][0]['allowed_tools'] == ['notes.add', 'notes.list']


@pytest.mark.parametrize('changes', [
    {'allowed_tools': 'notes.add'}, {'allowed_tools': ['']}, {'allowed_tools': [12]},
    {'allowed_tools': ['x' * 129]}, {'allowed_tools': [f'tool-{i}' for i in range(51)]},
    {'enabled': 'true'}, {'id': 'bad/id'},
])
def test_mcp_invalid_fields_are_atomic(tmp_path, changes):
    settings = Settings(tmp_path)
    settings.update({'mcp_servers': [server()]})
    before = settings.get()
    disk = settings.path.read_bytes()
    item = server()
    item.update(changes)
    with pytest.raises(ValueError):
        settings.update({'mcp_servers': [item]})
    assert settings.get() == before
    assert settings.path.read_bytes() == disk


def test_mcp_count_and_duplicate_ids_rejected(tmp_path):
    settings = Settings(tmp_path)
    with pytest.raises(ValueError):
        settings.update({'mcp_servers': [server(str(i)) for i in range(9)]})
    with pytest.raises(ValueError):
        settings.update({'mcp_servers': [server(), server()]})
    assert settings.get()['mcp_servers'] == []


def backup_with_settings(content, config):
    original = zipfile.ZipFile(io.BytesIO(content))
    result = io.BytesIO()
    with zipfile.ZipFile(result, 'w') as archive:
        archive.writestr('qunyu.db', original.read('qunyu.db'))
        archive.writestr('settings.json', json.dumps(config))
    return result.getvalue()


def test_mcp_api_backup_hides_secret_and_restore_clears_it(tmp_path):
    app = create_app(tmp_path, start_workers=False)
    with TestClient(app) as client:
        password_login(client, app)
        item = server()
        response = client.put('/api/settings', json={'mcp_servers': [item]})
        assert response.status_code == 200, response.text
        for endpoint in ('/api/settings', '/api/bootstrap', '/api/mcp/status'):
            response = client.get(endpoint)
            assert response.status_code == 200, response.text
            assert item['token'] not in response.text
        backup = client.get('/api/backup').content
        archive = zipfile.ZipFile(io.BytesIO(backup))
        assert item['token'] not in archive.read('settings.json').decode()
        response = client.post('/api/backup/restore', files={'file': ('backup.zip', backup, 'application/zip')})
        assert response.status_code == 200, response.text
        restored = app.state.settings.get()['mcp_servers']
        assert len(restored) == 1 and restored[0]['token'] == '' and not restored[0]['enabled']
        assert app.state.settings.get()['paused']


def test_pre_mcp_backup_restore_drops_current_servers(tmp_path):
    app = create_app(tmp_path, start_workers=False)
    with TestClient(app) as client:
        password_login(client, app)
        old_backup = backup_with_settings(client.get('/api/backup').content,
                                         {'model': {'model': 'legacy'}, 'paused': False})
        assert client.put('/api/settings', json={'mcp_servers': [server()]}).status_code == 200
        assert app.state.settings.get()['mcp_servers']
        response = client.post('/api/backup/restore', files={'file': ('old.zip', old_backup, 'application/zip')})
        assert response.status_code == 200, response.text
        assert app.state.settings.get()['mcp_servers'] == []
        assert app.state.settings.get()['paused']


def test_invalid_mcp_in_backup_preserves_live_settings_and_data(tmp_path):
    app = create_app(tmp_path, start_workers=False)
    with TestClient(app) as client:
        password_login(client, app)
        assert client.put('/api/settings', json={'mcp_servers': [server()]}).status_code == 200
        invalid = server()
        invalid['url'] = 'https://alice:password@example.com/mcp'
        backup = backup_with_settings(client.get('/api/backup').content, {'mcp_servers': [invalid]})
        memory = client.post('/api/memories', json={'content': '恢复失败须保留的记忆'}).json()
        before = app.state.settings.get()
        response = client.post('/api/backup/restore', files={'file': ('bad.zip', backup, 'application/zip')})
        assert response.status_code == 400
        assert app.state.settings.get() == before
        assert app.state.store.get_memory(memory['id'])['content'] == '恢复失败须保留的记忆'
