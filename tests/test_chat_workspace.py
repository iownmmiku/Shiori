"""Local organization must stay separate from platform delivery and memory."""
import asyncio
import json
import sqlite3
import threading

import httpx
import pytest
from fastapi.testclient import TestClient

from shiori.api import create_app
from shiori.chat_workspace import ChatWorkspace
from shiori.db import Store


@pytest.fixture
def workspace(tmp_path):
    store = Store(tmp_path / 'data')
    yield ChatWorkspace(store)
    store.close()


def test_project_conversation_crud_preserves_scope_permissions_and_history(workspace):
    store = workspace.store
    project = workspace.create_project({'name': '音乐', 'description': '单独整理对话'})
    conversation = workspace.create_conversation({'name': '曲目讨论', 'project_id': project['id']})
    scope_id = conversation['id']
    store.upsert_scope({'id': scope_id, 'enabled': False, 'memory_space': 'explicit-space',
                        'profile_id': 'custom-profile', 'profile_overrides': ['allow_tools'],
                        'allow_tools': False, 'settings': {'planner_daily_calls': 12, 'custom': {'keep': True}}})
    message = store.add_message(scope_id, 'owner', 'user', '保留正文')
    memory = store.create_memory({'scope_id': scope_id, 'content': '保留记忆'})
    before = store._get('scopes', scope_id)
    result = workspace.update_conversation(scope_id, {'name': '新标题', 'pinned': True, 'archived': True})
    assert result['pinned'] and result['archived'] and result['name'] == '新标题'
    after = store._get('scopes', scope_id)
    assert {key: value for key, value in after.items() if key not in ('name', 'chat_workspace')} == {
        key: value for key, value in before.items() if key not in ('name', 'chat_workspace')}
    assert workspace.update_project(project['id'], {'name': '作品', 'archived': True})['archived']
    assert workspace.delete_project(project['id']) == {'ok': True}
    result = next(row for row in workspace.snapshot()['conversations'] if row['id'] == scope_id)
    assert result['project_id'] is None and result['archived'] and result['pinned']
    assert store.get_message(message['id'])['content'] == '保留正文'
    assert store.get_memory(memory['id'])['content'] == '保留记忆'
    assert store._get('scopes', scope_id)['settings'] == before['settings']


def test_project_membership_does_not_share_memory_or_file_authority(workspace):
    project = workspace.create_project({'name': '只有分组'})
    first = workspace.create_conversation({'project_id': project['id']})
    second = workspace.create_conversation({'project_id': project['id']})
    for conversation in (first, second):
        scope = workspace.store._get('scopes', conversation['id'])
        assert scope['memory_space'] == '' and scope['settings'] == {}
        assert not any(key in scope for key in ('paths', 'roots', 'file_access', 'permissions'))
    workspace.store.create_memory({'scope_id': first['id'], 'content': '仅第一会话'})
    assert workspace.store.list_memories(scope_id=second['id']) == []
    workspace.update_conversation(first['id'], {'project_id': None})
    assert next(row for row in workspace.snapshot()['conversations'] if row['id'] == first['id'])['project_id'] is None


@pytest.mark.parametrize('method,args', [
    ('create_project', ({'name': ''},)),
    ('create_project', ({'name': 'a' * 101},)),
    ('create_project', ({'name': 'x', 'description': 'a' * 2001},)),
    ('create_project', ({'name': 'x', 'permissions': ['all']},)),
    ('create_project', ({'name': 'x\nspoof'},)),
    ('create_conversation', ({'name': 1},)),
    ('create_conversation', ({'project_id': []},)),
    ('create_conversation', ({'scope_id': 'qq:group:123'},)),
    ('update_conversation', ('web:default', {'pinned': 'true'})),
    ('update_conversation', ('web:default', {'archived': 1})),
    ('update_conversation', ('web:default', {'settings': {'enabled': False}})),
    ('update_conversation', ('web:default', {'memory_space': 'shared'})),
])
def test_invalid_or_authority_changing_fields_rejected_without_mutation(workspace, method, args):
    before = workspace.snapshot()
    with pytest.raises(ValueError):
        getattr(workspace, method)(*args)
    assert workspace.snapshot() == before


def test_workspace_excludes_qq_and_cannot_create_scope_through_patch_or_read(workspace):
    store = workspace.store
    for scope_id in ('qq:group:123', 'qq:private:456', 'qqofficial:group:789', 'qqofficial:private:abc'):
        store.upsert_scope({'id': scope_id})
        with pytest.raises(ValueError):
            workspace.update_conversation(scope_id, {'name': 'changed'})
    assert [row['id'] for row in workspace.snapshot()['conversations']] == ['web:default']
    with pytest.raises(KeyError):
        workspace.update_conversation('web:missing', {'name': 'must not exist'})
    with pytest.raises(KeyError):
        workspace.qq_messages('qq:group:missing')
    with pytest.raises(ValueError):
        workspace.qq_messages('web:default')
    assert len(store.list_scopes()) == 5
    with pytest.raises(KeyError):
        workspace.create_conversation({'project_id': 'missing'})
    assert len(store.list_scopes()) == 5


def test_qq_pagination_uses_stable_scope_local_cursors_at_same_timestamp(workspace):
    store = workspace.store
    scope = 'qq:group:123'
    source = []
    with store._lock, store._conn:
        store.upsert_scope({'id': scope, 'enabled': False})
        for index in range(9):
            record = store.add_message(scope, str(index), 'user', f'message {index}')
            record['created_at'] = '2026-01-01T00:00:00.000000Z'
            store._write('messages', record, ('scope_id', 'user_id', 'role', 'created_at'))
            source.append(record)
    foreign = store.add_message('qqofficial:private:other', 'other', 'user', 'foreign')
    store.add_message('web:default', 'owner', 'user', 'local private text')
    rows = workspace.qq_chats()
    assert {item['id'] for item in rows['items']} == {scope, 'qqofficial:private:other'}
    group = next(item for item in rows['items'] if item['id'] == scope)
    assert group['message_count'] == 9 and group['last_message']['id'] == source[-1]['id']
    assert group['last_message']['content'] == source[-1]['content']
    assert group['enabled'] is False and 'unread' not in group
    assert workspace.qq_chats(limit=1)['has_more']
    assert not workspace.qq_chats(limit=1, offset=1)['has_more']
    seen, before = [], None
    while True:
        page = workspace.qq_messages(scope, before=before, limit=3)
        seen = page['items'] + seen
        if not page['has_more']:
            break
        before = page['next_before']
    assert seen == source
    forward = workspace.qq_messages(scope, after=source[1]['id'], limit=3)
    assert forward['items'] == source[2:5] and forward['has_more']
    forward = workspace.qq_messages(scope, after=forward['next_after'], limit=10)
    assert forward['items'] == source[5:] and not forward['has_more']
    assert workspace.qq_messages(scope, after=source[-1]['id'])['items'] == []
    for kwargs in ({'before': foreign['id']}, {'after': foreign['id']}, {'before': 'missing'},
                   {'before': source[0]['id'], 'after': source[1]['id']}):
        with pytest.raises(ValueError):
            workspace.qq_messages(scope, **kwargs)
    assert store.get_scope(scope)['enabled'] is False


def test_original_markup_is_data_in_names_and_qq_messages(workspace):
    original = '<img src=x onerror=alert(1)><script>"&</script>'
    project = workspace.create_project({'name': original, 'description': original})
    conversation = workspace.create_conversation({'name': original, 'project_id': project['id']})
    assert workspace.snapshot()['projects'][0]['name'] == original
    assert next(row for row in workspace.snapshot()['conversations'] if row['id'] == conversation['id'])['name'] == original
    message = workspace.store.add_message('qq:private:xss', original, 'user', original,
                                         sender={'nickname': original}, output_part={'index': 1, 'total': 2})
    assert workspace.qq_messages('qq:private:xss')['items'] == [message]
    assert workspace.qq_chats()['items'][0]['last_message']['content'] == original
    long_message = workspace.store.add_message('qq:private:xss', 'sender', 'user', original * 30,
        images=['data:image/png;base64,' + 'X' * 20000], original_reply='Y' * 20000)
    preview = workspace.qq_chats()['items'][0]['last_message']
    assert preview['content'] == long_message['content'][:300]
    assert preview['metadata'] == {'image_count': 1}
    assert len(json.dumps(preview)) < 1500
    assert workspace.qq_messages('qq:private:xss')['items'][-1] == long_message


@pytest.mark.parametrize('include_messages', [True, False])
def test_backup_preserves_projects_and_membership(workspace, tmp_path, include_messages):
    project = workspace.create_project({'name': '持久项目'})
    conversation = workspace.create_conversation({'name': '持久对话', 'project_id': project['id']})
    workspace.update_conversation(conversation['id'], {'pinned': True, 'archived': True})
    workspace.store.add_message(conversation['id'], 'owner', 'user', '正文')
    expected = workspace.snapshot()
    snapshot = workspace.store.backup(tmp_path / 'snapshot.db', include_messages=include_messages)
    workspace.delete_project(project['id'])
    workspace.store.restore_from(snapshot)
    restored = workspace.snapshot()
    assert restored['projects'] == expected['projects']
    current = next(row for row in restored['conversations'] if row['id'] == conversation['id'])
    assert current['project_id'] == project['id'] and current['pinned'] and current['archived']
    assert len(workspace.store.list_messages(conversation['id'])) == int(include_messages)


def test_legacy_backup_without_projects_is_restored_into_live_service(workspace, tmp_path):
    snapshot = workspace.store.backup(tmp_path / 'legacy.db')
    with sqlite3.connect(snapshot) as source:
        source.execute('DROP TABLE chat_projects')
    workspace.create_project({'name': '清除本机后加项目'})
    workspace.create_conversation({'name': '清除本机后加对话'})
    assert workspace.store.restore_from(snapshot, validate_only=True) == {'valid': True}
    workspace.store.restore_from(snapshot)
    assert workspace.snapshot()['projects'] == []
    assert [row['id'] for row in workspace.snapshot()['conversations']] == ['web:default']
    assert workspace.create_project({'name': '恢复后可继续创建'})['id']


@pytest.mark.parametrize('malformed', ['schema', 'payload', 'id'])
def test_untrusted_project_backup_is_rejected(workspace, tmp_path, malformed):
    project = workspace.create_project({'name': '保留本机数据'})
    snapshot = workspace.store.backup(tmp_path / 'invalid.db')
    with sqlite3.connect(snapshot) as source:
        if malformed == 'schema':
            source.execute('DROP TABLE chat_projects')
            source.execute('CREATE TABLE chat_projects(id TEXT PRIMARY KEY,data TEXT)')
        else:
            source.execute('UPDATE chat_projects SET data=?',
                           (json.dumps([] if malformed == 'payload' else {**project, 'id': 'mismatch'}),))
    with pytest.raises(ValueError):
        workspace.store.restore_from(snapshot)
    assert workspace.snapshot()['projects'] == [project]


def test_authenticated_api_contract_and_chat_document_alias(tmp_path):
    app = create_app(tmp_path / 'http', start_workers=False)
    with TestClient(app) as client:
        paths = ('/api/chat/workspace', '/api/qq/chats', '/api/qq/chats/qq:group:1/messages')
        for path in paths:
            assert client.get(path).status_code == 401
        assert client.post('/api/chat/projects', json={'name': 'unauthenticated'}).status_code == 401
        assert client.get('/chat').status_code == 200
        assert client.get('/chat').text == client.get('/').text
        client.headers['Authorization'] = 'Bearer ' + app.state.settings.token
        response = client.post('/api/chat/projects', json={'name': 'API project'})
        assert response.status_code == 200, response.text
        project = response.json()
        response = client.post('/api/chat/conversations', json={'project_id': project['id']})
        assert response.status_code == 200, response.text
        conversation = response.json()
        assert conversation['id'].startswith('web:') and conversation['name'] == '新对话'
        app.state.store.upsert_scope({'id': conversation['id'], 'persona_id': '', 'model_id': 'chosen-model',
                                     'settings': {'debounce_enabled': False}})
        original_scope = app.state.store._get('scopes', conversation['id'])
        response = client.patch('/api/chat/conversations/' + conversation['id'], json={'name': 'changed', 'pinned': True})
        assert response.status_code == 200 and response.json()['pinned']
        saved_scope = app.state.store._get('scopes', conversation['id'])
        assert {key: value for key, value in saved_scope.items() if key not in ('name', 'chat_workspace')} == {
            key: value for key, value in original_scope.items() if key not in ('name', 'chat_workspace')}
        assert app.state.store.get_scope(conversation['id'])['model_id'] == 'chosen-model'
        assert app.state.store.get_scope(conversation['id'])['persona_id'] == ''
        response = client.patch('/api/chat/projects/' + project['id'], json={'description': 'updated', 'archived': True})
        assert response.status_code == 200 and response.json()['archived']
        assert client.patch('/api/chat/conversations/qq:group:1', json={'name': 'invalid'}).status_code == 400
        assert client.patch('/api/chat/conversations/web:missing', json={'name': 'invalid'}).status_code == 404
        assert client.post('/api/chat/projects', json={'name': 1}).status_code == 400
        assert client.post('/api/chat/conversations', json={'project_id': 'missing'}).status_code == 404
        assert client.get('/api/qq/chats?limit=501').status_code == 422
        assert client.get('/api/qq/chats/qq:group:1/messages?limit=0').status_code == 422
        assert client.delete('/api/chat/projects/' + project['id']).status_code == 200
        assert client.get('/api/chat/workspace').json()['projects'] == []
        assert client.get('/api/chat/workspace', headers={'Origin': 'https://evil.invalid'}).status_code == 403
        assert client.get('/api/qq/chats', headers={'Sec-Fetch-Site': 'cross-site'}).status_code == 403


def test_long_qq_history_cursor_seeks_do_not_scan_unrelated_history(workspace):
    store = workspace.store
    scope_id = 'qq:group:long-history'
    store.upsert_scope({'id': scope_id})
    with store._lock, store._conn:
        records = []
        for index in range(20000):
            created_at = f'2026-01-01T00:00:00.{index:06d}Z'
            record = {'id': f'history-{index}', 'scope_id': scope_id, 'user_id': 'user',
                      'role': 'user', 'content': str(index), 'created_at': created_at, 'metadata': {}}
            records.append((record['id'], scope_id, 'user', 'user', created_at, json.dumps(record)))
        store._conn.executemany('INSERT INTO messages VALUES(?,?,?,?,?,?)', records)
    instructions = []
    store._conn.set_progress_handler(lambda: instructions.append(True) or 0, 1000)
    try:
        assert workspace.qq_messages(scope_id, after='history-19999')['items'] == []
        assert workspace.qq_messages(scope_id, before='history-0')['items'] == []
        assert [row['id'] for row in workspace.qq_messages(scope_id, after='history-19997')['items']] == [
            'history-19998', 'history-19999']
        # A bound on VM work, not wall-clock timing: repeated near-edge polls
        # must seek the scope/time index even with tens of thousands of rows.
        assert len(instructions) < 5
    finally:
        store._conn.set_progress_handler(None, 0)


def test_cancelled_workspace_write_is_drained_before_backup_restore(tmp_path, monkeypatch):
    entered, release, finished, restoring = (threading.Event() for _ in range(4))
    app = create_app(tmp_path / 'cancelled-write', start_workers=False)
    with TestClient(app) as client:
        headers = {'Authorization': 'Bearer ' + app.state.settings.token}
        client.headers.update(headers)
        retained = client.post('/api/chat/projects', json={'name': '备份中项目'}).json()
        archive = client.get('/api/backup').content
        original_create = app.state.chat_workspace.create_project
        original_restore = app.state.store.restore_from

        def held_create(payload):
            entered.set()
            try:
                assert release.wait(10)
                return original_create(payload)
            finally:
                finished.set()

        def restore(candidate, *, validate_only=False):
            restoring.set()
            assert finished.is_set(), 'restore overtook a cancelled workspace mutation'
            return original_restore(candidate, validate_only=validate_only)

        monkeypatch.setattr(app.state.chat_workspace, 'create_project', held_create)
        monkeypatch.setattr(app.state.store, 'restore_from', restore)

        async def scenario():
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                         base_url='http://testserver', headers=headers) as http:
                request = asyncio.create_task(http.post('/api/chat/projects', json={'name': '恢复前未完成项目'}))
                restoration = None
                try:
                    assert await asyncio.to_thread(entered.wait, 5)
                    request.cancel()
                    await asyncio.sleep(.02)
                    request.cancel()
                    restoration = asyncio.create_task(http.post('/api/backup/restore',
                        files={'file': ('backup.zip', archive, 'application/zip')}))
                    await asyncio.sleep(.05)
                    assert not request.done(), 'HTTP cancellation abandoned the database worker'
                    assert not restoring.is_set(), 'restore did not wait for the active request'
                    busy = await http.get('/api/qq/chats')
                    assert busy.status_code == 503, busy.text
                    release.set()
                    with pytest.raises(asyncio.CancelledError):
                        await request
                    result = await restoration
                    assert result.status_code == 200, result.text
                    assert finished.is_set() and restoring.is_set()
                    snapshot = (await http.get('/api/chat/workspace')).json()
                    assert snapshot['projects'] == [retained]
                finally:
                    release.set()
                    await asyncio.gather(request, *([restoration] if restoration else []), return_exceptions=True)

        client.portal.call(scenario)
