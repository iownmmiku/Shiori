"""Source connections, per-session capabilities and stale model responses."""
import json
import time

import httpx
import pytest
from fastapi.testclient import TestClient

from auth_helpers import password_login
from shiori.api import create_app
from shiori.computer import TOOLS
from shiori.provider import ProviderError


@pytest.fixture
def anyio_backend():
    return 'asyncio'


@pytest.fixture
def selection(tmp_path):
    app = create_app(tmp_path / 'selection', start_workers=False)
    with TestClient(app) as client:
        password_login(client, app)
        source = app.state.model_catalog.save_source({'name': '离线选模源', 'base_url': 'https://selection.example/v1',
                                                       'api_key': 'selection-fixture-key'})
        models = app.state.model_catalog.add_models({'provider_id': source['id'], 'models': [
            {'model': 'global-model', 'tools': False, 'vision': False},
            {'model': 'session-model', 'tools': True, 'vision': True}]})['added']
        global_id, selected_id = models[0]['id'], models[1]['id']
        app.state.model_catalog.update_routing({'chat': global_id})
        app.state.store.upsert_scope({'id': 'web:default', 'model_id': selected_id})
        yield client, app, source['id'], global_id, selected_id, tmp_path / 'workspace'


def reply(content='offline response', tool_calls=None):
    message = {'role': 'assistant', 'content': content}
    if tool_calls:
        message['tool_calls'] = tool_calls
    return httpx.Response(200, json={'choices': [{'message': message}],
                                    'usage': {'prompt_tokens': 3, 'completion_tokens': 4}})


def model_result(content='', calls=None):
    message = {'role': 'assistant', 'content': content}
    if calls:
        message['tool_calls'] = calls
    return {'content': content, 'tool_calls': calls or [], 'message': message, 'usage': {}}


def mount_transport(monkeypatch, handler):
    original = httpx.AsyncClient
    monkeypatch.setattr(httpx, 'AsyncClient', lambda *a, **kw: original(*a, transport=httpx.MockTransport(handler), **kw))


def test_connection_test_resolves_saved_model_source_url_and_key(selection, monkeypatch):
    client, app, source_id, global_id, selected_id, root = selection
    observed = []
    def handler(request):
        observed.append((str(request.url), request.headers.get('authorization'), json.loads(request.content)['model']))
        return reply('连接成功')
    mount_transport(monkeypatch, handler)
    result = client.post('/api/model/test', json={'provider_id': selected_id})
    assert result.status_code == 200, result.text
    assert result.json()['ok'] and observed == [('https://selection.example/v1/chat/completions',
                                                 'Bearer selection-fixture-key', 'session-model')]
    app.state.model_catalog.save_source({'enabled': False}, source_id)
    assert client.post('/api/model/test', json={'provider_id': selected_id}).status_code == 400
    assert len(observed) == 1


def test_bootstrap_with_new_source_route_is_available_and_contains_no_secret(selection):
    client, app, source_id, global_id, selected_id, root = selection
    # Bootstrap receives public/masked settings, while capability resolution
    # must still work for routes whose connection lives in provider_sources.
    response = client.get('/api/bootstrap')
    assert response.status_code == 200, response.text
    assert response.json()['capabilities']['text']
    assert 'selection-fixture-key' not in response.text


def test_session_vision_enabled_overrides_nonvision_global_model(selection, monkeypatch):
    client, app, *_ = selection
    observed = []
    async def complete(messages, **kwargs):
        observed.append(messages)
        return model_result('已按本会话视觉模型处理')
    monkeypatch.setattr(app.state.provider, 'complete', complete)
    image = 'data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAusB9Y9Zl1sAAAAASUVORK5CYII='
    result = client.post('/api/chat', json={'text': '看看这张图', 'images': [image]})
    assert result.status_code == 200, result.text
    assert observed and any(isinstance(item.get('content'), list) for item in observed[0])


def test_session_vision_disabled_rejects_images_even_when_global_supports_them(selection, monkeypatch):
    client, app, _, global_id, selected_id, _ = selection
    app.state.model_catalog.update_model(global_id, {'vision': True})
    app.state.model_catalog.update_model(selected_id, {'vision': False})
    called = []
    async def complete(*args, **kwargs):
        called.append(True)
        return model_result('must not reach model')
    monkeypatch.setattr(app.state.provider, 'complete', complete)
    result = client.post('/api/chat', json={'text': '查看图片', 'images': ['data:image/png;base64,eA==']})
    assert result.status_code == 400 and not called


def test_session_tools_enabled_mount_and_execute_despite_global_tools_disabled(selection, monkeypatch):
    client, app, *_ = selection
    calls = []
    async def complete(messages, **kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            assert 'calculate' in {item['function']['name'] for item in kwargs['tools']}
            return model_result(calls=[{'id': 'isolated-calculation', 'type': 'function', 'function': {
                'name': 'calculate', 'arguments': json.dumps({'expression': '2+2'})}}])
        return model_result('计算结果是4')
    monkeypatch.setattr(app.state.provider, 'complete', complete)
    result = client.post('/api/chat', json={'text': '我需要一个计算工具的演示'})
    assert result.status_code == 200, result.text
    assert len(calls) == 2
    assert result.json()['actions'][0]['tool'] == 'calculate' and result.json()['actions'][0]['ok']


def test_session_tools_disabled_do_not_mount_tools_even_with_global_tools_enabled(selection, monkeypatch):
    client, app, _, global_id, selected_id, _ = selection
    app.state.model_catalog.update_model(global_id, {'tools': True})
    app.state.model_catalog.update_model(selected_id, {'tools': False})
    observed = []
    async def complete(messages, **kwargs):
        observed.append(kwargs.get('tools'))
        return model_result('当前会话使用文字答复')
    monkeypatch.setattr(app.state.provider, 'complete', complete)
    result = client.post('/api/chat', json={'text': '介绍一下当前对话'})
    assert result.status_code == 200 and observed == [None]


@pytest.mark.anyio
@pytest.mark.parametrize('change', ['disable_source', 'change_key', 'change_url', 'disable_model', 'change_session_model'])
async def test_inflight_response_is_rejected_when_source_or_selected_model_changes(selection, monkeypatch, change):
    _, app, source_id, global_id, selected_id, _ = selection
    seen = []
    def handler(request):
        seen.append(True)
        if change == 'disable_source':
            app.state.model_catalog.save_source({'enabled': False}, source_id)
        elif change == 'change_key':
            app.state.model_catalog.save_source({'api_key': 'rotated-offline-key'}, source_id)
        elif change == 'change_url':
            app.state.model_catalog.save_source({'base_url': 'https://changed.example/v1'}, source_id)
        elif change == 'disable_model':
            app.state.model_catalog.update_model(selected_id, {'enabled': False})
        else:
            app.state.store.upsert_scope({'id': 'web:default', 'model_id': global_id})
        return reply('', [{'id': 'stale-call', 'type': 'function', 'function': {
            'name': 'write_file', 'arguments': json.dumps({'path': 'must-not-execute.txt', 'content': 'stale'})}}])
    mount_transport(monkeypatch, handler)
    with pytest.raises(ProviderError, match='响应已收到'):
        await app.state.provider.complete([{'role': 'user', 'content': 'isolated stale response'}], tools=TOOLS,
                                          purpose='computer', scope_id='web:default')
    # The actual response was received and its usage remains recorded, although
    # its tool calls were refused. No fake zero-cost/unsent result is reported.
    usage = app.state.store.list_usage()
    assert len(seen) == 1 and len(usage) == 1
    assert usage[0]['model'] == 'session-model' and usage[0]['input_tokens'] == 3
    assert not app.state.provider.status()['reserved']['requests']


def test_source_disabled_during_automatic_computer_response_never_writes_file(selection, monkeypatch):
    client, app, source_id, _, _, workspace = selection
    app.state.settings.update({'computer': {'enabled': True, 'runtime': 'local', 'workspace': str(workspace)}})
    dispatched = []
    def handler(request):
        dispatched.append(True)
        app.state.model_catalog.save_source({'enabled': False}, source_id)
        return reply('', [{'id': 'stale-auto-write', 'type': 'function', 'function': {
            'name': 'write_file', 'arguments': json.dumps({'path': 'must-not-write.txt', 'content': 'stale response'})}}])
    mount_transport(monkeypatch, handler)
    result = client.post('/api/computer/jobs', json={'text': '请在本机生成隔离验收文件'})
    assert result.status_code == 200, result.text
    job_id = result.json()['id']
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        job = client.get('/api/jobs/' + job_id).json()
        if job['status'] in ('completed', 'failed', 'cancelled', 'interrupted'):
            break
        time.sleep(.01)
    assert len(dispatched) == 1 and job['status'] == 'failed', job
    assert not (workspace / 'must-not-write.txt').exists()
    assert not any(event['type'] in ('authorized', 'tool_result') for event in job['events'])
    assert len(app.state.store.list_usage()) == 1
