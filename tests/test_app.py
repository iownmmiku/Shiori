import io
import json
import zipfile
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from shiori.api import create_app
from auth_helpers import password_login


@pytest.fixture
def client(tmp_path):
    app = create_app(tmp_path, start_workers=False)
    with TestClient(app) as c:
        yield c, app


def login(c, app):
    password_login(c, app)


def test_local_auth_csrf_and_redaction(client):
    c, app = client
    assert c.get('/api/bootstrap').status_code == 401
    assert c.post('/api/session', json={'token': 'wrong'}).status_code == 401
    login(c, app)
    assert c.get('/api/bootstrap').status_code == 200
    assert c.post('/api/memories', headers={'origin': 'https://evil.example'}, json={'content': 'x'}).status_code == 403
    secret = 'fixture-only-key-12345'
    response = c.put('/api/settings', json={'model': {'base_url': 'https://example.org/v1', 'model': 'fixture', 'api_key': secret}})
    assert response.status_code == 200
    assert secret not in response.text
    assert secret not in c.get('/api/bootstrap').text
    assert c.get('/api/bootstrap', headers={'host': 'evil.example'}).status_code == 403


def test_crud_and_offline_chat(client):
    c, app = client
    login(c, app)
    response = c.post('/api/chat', json={'scope_id': 'web:default', 'text': '记住：我喜欢合作游戏'})
    assert response.status_code == 200, response.text
    memory = c.get('/api/memories').json()[0]
    assert c.put('/api/memories/' + memory['id'], json={'content': '我喜欢策略游戏'}).status_code == 200
    assert app.state.store.recall('web:default', 'owner', '策略游戏')
    assert c.delete('/api/memories/' + memory['id']).status_code == 200
    assert not app.state.store.recall('web:default', 'owner', '策略游戏')
    assert c.post('/api/jargon', json={'term': '开饭', 'meaning': '约游戏'}).status_code == 200
    entry = c.get('/api/jargon').json()[0]
    assert c.put('/api/jargon/' + entry['id'], json={'status': 'disabled'}).status_code == 200
    assert not app.state.store.match_jargon('web:default', '开饭')
    task = c.post('/api/tasks', json={'title': '写发布说明', 'kind': 'todo'}).json()
    assert c.post('/api/tasks/' + task['id'] + '/cancel').json()['status'] == 'cancelled'
    assert c.post('/api/chat', json={'scope_id': 'qq:group:123', 'text': '你好'}).status_code == 400


def test_backup_restore_excludes_keys_and_keeps_data(client):
    c, app = client
    login(c, app)
    c.put('/api/settings', json={'model': {'api_key': 'fixture-secret-backup-test', 'base_url': 'https://example.com/v1', 'model': 'test'}})
    original = c.post('/api/memories', json={'content': '应恢复的记忆'}).json()
    backup = c.get('/api/backup')
    assert backup.status_code == 200
    archive = zipfile.ZipFile(io.BytesIO(backup.content))
    assert b'fixture-secret-backup-test' not in archive.read('settings.json')
    assert 'qunyu.db' in archive.namelist()
    c.delete('/api/memories/' + original['id'])
    response = c.post('/api/backup/restore', files={'file': ('backup.zip', backup.content, 'application/zip')})
    assert response.status_code == 200, response.text
    assert app.state.store.get_memory(original['id'])['status'] == 'active'
    assert app.state.settings.get()['paused']
    assert not app.state.settings.get()['model']['api_key']


def test_files_and_invalid_backup(client):
    c, app = client
    login(c, app)
    result = c.post('/api/files', data={'scope_id': 'web:default'}, files={'file': ('notes.md', '# 资料\n真实文件内容'.encode(), 'text/markdown')})
    assert result.status_code == 200, result.text
    assert '真实文件内容' in result.json()['memory']['content']
    assert c.post('/api/files', files={'file': ('bad.exe', b'not text', 'application/octet-stream')}).status_code == 400
    assert c.post('/api/backup/restore', files={'file': ('bad.zip', b'not zip', 'application/zip')}).status_code == 400


def altered_backup(c, config):
    original = zipfile.ZipFile(io.BytesIO(c.get('/api/backup').content))
    result = io.BytesIO()
    with zipfile.ZipFile(result, 'w') as archive:
        archive.writestr('qunyu.db', original.read('qunyu.db'))
        archive.writestr('settings.json', json.dumps(config))
    return result.getvalue()


def test_restore_invalid_settings_does_not_replace_live_database(client):
    c, app = client
    login(c, app)
    invalid = altered_backup(c, {'model': {'temperature': 'not-a-number'}})
    record = c.post('/api/memories', json={'content': '必须保留的最新记忆'}).json()
    before = app.state.settings.get()
    response = c.post('/api/backup/restore', files={'file': ('backup.zip', invalid, 'application/zip')})
    assert response.status_code == 400
    assert app.state.store.get_memory(record['id'])['content'] == '必须保留的最新记忆'
    assert app.state.settings.get() == before


def test_restore_write_failure_rolls_back_database_and_settings(client, monkeypatch):
    c, app = client
    login(c, app)
    backup = c.get('/api/backup').content
    record = c.post('/api/memories', json={'content': '恢复失败需要保留'}).json()
    before = app.state.settings.get()
    original_save, count = app.state.settings._save, 0
    def fail_once():
        nonlocal count
        count += 1
        if count == 2:
            raise OSError('模拟配置写入失败')
        original_save()
    monkeypatch.setattr(app.state.settings, '_save', fail_once)
    response = c.post('/api/backup/restore', files={'file': ('backup.zip', backup, 'application/zip')})
    assert response.status_code == 400, response.text
    assert app.state.store.get_memory(record['id'])['content'] == '恢复失败需要保留'
    assert app.state.settings.get() == before


def test_onebot_rejects_secrets_embedded_in_url(client):
    c, app = client
    login(c, app)
    for url in ('ws://user:password@127.0.0.1:3001', 'ws://127.0.0.1:3001?access_token=secret', 'ws://127.0.0.1:3001#secret'):
        response = c.put('/api/settings', json={'onebot': {'ws_url': url}})
        assert response.status_code == 400
        assert app.state.settings.get()['onebot']['ws_url'] == ''
    for url in ('https://user:password@example.com/v1', 'https://example.com/v1?key=secret'):
        assert c.put('/api/settings', json={'model': {'base_url': url}}).status_code == 400
        assert app.state.settings.get()['model']['base_url'] == ''


@pytest.mark.parametrize('adapter', ['napcat', 'snowluma'])
def test_onebot_reverse_websocket_roundtrip(client, adapter):
    from starlette.websockets import WebSocketDisconnect
    c, app = client
    login(c, app)
    token = 'qa-local-onebot-token'
    response = c.put('/api/settings', json={'onebot': {'enabled': True, 'adapter': adapter, 'owner_ids': ['42'], 'allowed_groups': ['123'], 'access_token': token}})
    assert response.status_code == 200
    with pytest.raises(WebSocketDisconnect):
        with c.websocket_connect('/onebot/v11/ws', headers={'authorization': 'Bearer wrong-token'}):
            pass
    with c.websocket_connect('/onebot/v11/ws', headers={'authorization': 'Bearer ' + token}) as socket:
        socket.send_json({'post_type': 'message', 'message_type': 'group', 'self_id': 999, 'message_id': 100,
                          'user_id': 42, 'group_id': 123, 'message': [{'type': 'at', 'data': {'qq': '999'}}, {'type': 'text', 'data': {'text': '/帮助'}}]})
        action = socket.receive_json()
        assert action['action'] == 'send_group_msg' and str(action['params']['group_id']) == '123'
        assert '可以直接聊天' in action['params']['message'][0]['data']['text']
        socket.send_json({'echo': action['echo'], 'status': 'ok', 'retcode': 0, 'data': {'message_id': 101}})
    assert app.state.store.list_messages('qq:group:123')
