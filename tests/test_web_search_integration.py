"""Authenticated entry points share one configured search service."""
import asyncio
import io
import json
import zipfile

from fastapi.testclient import TestClient

from auth_helpers import password_login
from mikubot.api import create_app


def test_authenticated_search_routes_and_background_injection(tmp_path):
    app = create_app(tmp_path, start_workers=False)
    service = app.state.search
    assert app.state.chat.search is service
    assert app.state.chat.knowledge.search_fn.__self__ is service
    assert app.state.distillation.search_fn.__self__ is service
    calls = []

    async def fake_search(query):
        calls.append(query)
        return {'query': query, 'channel': 'tavily', 'results': [
            {'title': '官方资料', 'url': 'https://example.com/source', 'content': '来源摘要'}],
            'links': ['https://example.com/source'], 'text': '来源摘要'}

    service.search = fake_search
    with TestClient(app) as client:
        assert client.get('/api/search/status').status_code == 401
        assert client.post('/api/search/test', json={'query': '不应发送'}).status_code == 401
        password_login(client, app)
        assert client.get('/api/search/status').json()['provider'] == 'public'
        for path in ('/api/tools/search', '/api/search/test'):
            response = client.post(path, json={'query': '公开资料'})
            assert response.status_code == 200, response.text
            assert response.json()['links'] == ['https://example.com/source']
        assert client.post('/api/search/test', headers={'origin': 'https://evil.example'},
                           json={'query': '跨站不应发送'}).status_code == 403
        assert calls == ['公开资料', '公开资料']
        app.state.chat.reset_engines()
        assert app.state.chat.search is service
        assert app.state.chat.knowledge.search_fn is fake_search


def test_chat_search_tool_uses_configured_service_and_disable_hides_schema(tmp_path):
    app = create_app(tmp_path, start_workers=False)
    chat = app.state.chat
    scope = app.state.store.get_scope('web:default')
    source = app.state.store.add_message('web:default', 'owner', 'user', '搜索公开资料')
    calls = []

    async def fake_search(query):
        calls.append(query)
        return {'query': query, 'links': ['https://example.com/verified']}

    chat.search.search = fake_search
    try:
        result = asyncio.run(chat._tool('web_search', {'query': '公开资料'},
            'web:default', 'owner', '搜索公开资料', source['id'], {}))
        assert result['links'] == ['https://example.com/verified']
        assert calls == ['公开资料']
        app.state.settings.update({'search': {'enabled': False}})
        schemas = chat._request_tools(scope, 'owner', '搜索公开资料', {})
        assert all(item['function']['name'] != 'web_search' for item in schemas)
    finally:
        asyncio.run(chat.close())
        app.state.store.close()


def test_search_api_exposes_stable_safe_error_codes(tmp_path):
    app = create_app(tmp_path, start_workers=False)
    with TestClient(app) as client:
        password_login(client, app)
        response = client.post('/api/search/test', json={'query': ['invalid']})
        assert response.status_code == 400
        assert response.json()['code'] == 'invalid_query'
        client.put('/api/settings', json={'search': {'enabled': False}})
        response = client.post('/api/tools/search', json={'query': '短查询'})
        assert response.status_code == 400
        assert response.json()['code'] == 'disabled'
        assert 'Traceback' not in response.text


def _without_search_settings(raw):
    output = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(raw)) as original, zipfile.ZipFile(output, 'w') as target:
        for name in original.namelist():
            value = original.read(name)
            if name == 'settings.json':
                cfg = json.loads(value)
                cfg.pop('search', None)
                cfg.pop('search_providers', None)
                value = json.dumps(cfg).encode()
            target.writestr(name, value)
    return output.getvalue()


def test_search_keys_are_redacted_from_backup_logs_and_restore_clears_local_keys(tmp_path):
    app = create_app(tmp_path, start_workers=False)
    secret = 'fixture-search-key-not-for-backup'
    second = 'fixture-search-other-provider-key'
    with TestClient(app) as client:
        password_login(client, app)
        response = client.put('/api/settings', json={
            'search': {'provider': 'tavily'},
            'search_providers': [{'id': 'tavily', 'api_key': secret}]})
        assert response.status_code == 200, response.text
        assert secret not in response.text
        assert secret not in client.get('/api/bootstrap').text
        assert secret not in app.state.chat.tool_actions._redact('key=' + secret)
        (tmp_path / 'runtime.log').write_text('2099-01-01 10:00:00 INFO ' + secret + '\n', 'utf-8')
        assert secret not in client.get('/api/logs').text
        backup = client.get('/api/backup')
        with zipfile.ZipFile(io.BytesIO(backup.content)) as archive:
            assert secret not in archive.read('settings.json').decode()
        client.put('/api/settings', json={'search_providers': [
            {'id': 'deepseek', 'api_key': second}]})
        response = client.post('/api/backup/restore', files={
            'file': ('search-backup.zip', backup.content, 'application/zip')})
        assert response.status_code == 200, response.text
        assert not app.state.settings.get()['search']['enabled']
        assert all(not item['api_key'] for item in app.state.settings.get()['search_providers'])
        # Backups written before this feature must clear keys on this machine,
        # too, rather than retaining credentials through a merged empty list.
        client.put('/api/settings', json={'search_providers': [
            {'id': 'tavily', 'api_key': secret}]})
        old = _without_search_settings(backup.content)
        response = client.post('/api/backup/restore', files={
            'file': ('legacy-backup.zip', old, 'application/zip')})
        assert response.status_code == 200, response.text
        assert all(not item['api_key'] for item in app.state.settings.get()['search_providers'])
