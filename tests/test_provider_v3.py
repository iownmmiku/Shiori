import asyncio
import io
import json
import zipfile

import httpx
import pytest
from fastapi.testclient import TestClient

from shiori.api import create_app
from qunyu.db import Store
from shiori.provider import Provider, ProviderError
from shiori.settings import Settings, resolve_model


def pool():
    return [dict(id='smart', name='聊天', base_url='https://smart.example/v1', model='smart-v1', api_key='smart-test-key'),
            dict(id='small', name='整理', base_url='https://small.example/v1', model='small-v1', api_key='small-test-key'),
            dict(id='vectors', name='向量', base_url='https://vectors.example/v1', model='embed-v1', api_key='embed-test-key', input_price=2)]


def prepare(tmp_path, monkeypatch, handler, *, without_keys=False):
    store, settings = Store(tmp_path), Settings(tmp_path)
    providers = [dict(item, api_key='') for item in pool()] if without_keys else pool()
    settings.update({'providers': providers, 'routing': {'chat': 'smart', 'memory': 'small', 'summary': 'small', 'embedding': 'vectors'}})
    original = httpx.AsyncClient
    monkeypatch.setattr(httpx, 'AsyncClient', lambda *a, **kw: original(*a, transport=httpx.MockTransport(handler), **kw))
    return store, settings, Provider(settings, store)


def response():
    return httpx.Response(200, json={'choices': [{'message': {'role': 'assistant', 'content': '真实模拟响应'}}], 'usage': {'prompt_tokens': 20, 'completion_tokens': 10}})


def test_task_routes_and_nested_keys_roundtrip(tmp_path, monkeypatch):
    seen = []
    def handler(request):
        seen.append((request.url.host, json.loads(request.content)['model'], request.headers['authorization']))
        return response()
    store, settings, provider = prepare(tmp_path, monkeypatch, handler)
    async def run():
        for purpose in ('chat', 'jargon_learning', 'expression_learning', 'context_digest'):
            await provider.complete([{'role': 'user', 'content': '资料'}], purpose=purpose)
    asyncio.run(run())
    assert [x[0] for x in seen] == ['smart.example', 'small.example', 'small.example', 'small.example']
    assert [x['provider_id'] for x in store.list_usage()] == ['small', 'small', 'small', 'smart']
    assert 'smart-test-key' not in settings.path.read_text('utf-8')
    assert Settings(tmp_path).get()['providers'][0]['api_key'] == 'smart-test-key'
    public = settings.public()
    assert all('api_key' not in x for x in public['providers'])
    settings.update({'providers': public['providers']})
    assert settings.get()['providers'][0]['api_key'] == 'smart-test-key'
    public['providers'][0]['clear_api_key'] = True
    settings.update({'providers': public['providers']})
    assert settings.get()['providers'][0]['api_key'] == ''
    assert 'small-test-key' not in json.dumps(provider.status())
    store.close()


def test_explicit_fallback_and_circuit(tmp_path, monkeypatch):
    seen = []
    def handler(request):
        seen.append(request.url.host)
        return httpx.Response(503) if request.url.host == 'smart.example' else response()
    store, settings, provider = prepare(tmp_path, monkeypatch, handler)
    settings.update({'routing': {'fallback': ['small']}})
    async def run():
        for _ in range(3):
            result = await provider.complete([{'role': 'user', 'content': '测试'}])
            assert result['usage']['provider_id'] == 'small'
    asyncio.run(run())
    assert seen.count('smart.example') == 4  # Two failed requests, each with a bounded retry.
    assert seen.count('small.example') == 3
    assert next(x for x in provider.status()['providers'] if x['id'] == 'smart')['circuit_open']
    store.close()


@pytest.mark.parametrize('failure', [401, 403, 404, 'read_timeout'])
def test_auth_config_and_unknown_completion_never_fallback(tmp_path, monkeypatch, failure):
    seen = []
    def handler(request):
        seen.append(request.url.host)
        if failure == 'read_timeout':
            raise httpx.ReadTimeout('unknown completion', request=request)
        return httpx.Response(failure)
    store, settings, provider = prepare(tmp_path, monkeypatch, handler)
    settings.update({'routing': {'fallback': ['small']}})
    with pytest.raises(ProviderError):
        asyncio.run(provider.complete([{'role': 'user', 'content': '测试'}]))
    assert seen == ['smart.example']
    assert not provider.status()['providers'][1]['circuit_open']
    store.close()


def test_embedding_route_uses_own_model_secret_price_and_cache_identity(tmp_path, monkeypatch):
    seen = []
    def handler(request):
        seen.append(request)
        return httpx.Response(200, json={'data': [{'index': 0, 'embedding': [1.0, 0.2]}], 'usage': {'prompt_tokens': 100}})
    store, settings, provider = prepare(tmp_path, monkeypatch, handler)
    settings.update({'memory': {'semantic_enabled': True}})
    assert asyncio.run(provider.embed(['向量资料'])) == [[1.0, 0.2]]
    assert seen[0].url.host == 'vectors.example'
    assert json.loads(seen[0].content)['model'] == 'embed-v1'
    assert seen[0].headers['authorization'] == 'Bearer embed-test-key'
    assert store.list_usage()[0]['cost'] == pytest.approx(.0002)
    from shiori.memory import MemoryEngine
    memory = MemoryEngine(store, settings)
    old_signature = memory._signature(settings.get(), 2)
    items = settings.public()['providers']
    items[-1]['model'] = 'embed-v2'
    settings.update({'providers': items})
    assert memory._signature(settings.get(), 2) != old_signature
    memory.close()
    store.close()


def test_route_validation_and_budget_stay_local(tmp_path, monkeypatch):
    store, settings, provider = prepare(tmp_path, monkeypatch, lambda request: pytest.fail('no external call expected'))
    for patch in ({'routing': {'chat': 'missing'}}, {'providers': [pool()[0], pool()[0]]}, {'context': {'keep_recent': 60}}, {'providers': [dict(pool()[0], input_price=float('nan'))]}):
        with pytest.raises(ValueError):
            settings.update(patch)
    settings.update({'routing': {'fallback': ['small']}, 'budget': {'daily_tokens': 1}})
    with pytest.raises(ProviderError, match='预算'):
        asyncio.run(provider.complete([{'role': 'user', 'content': '测试'}]))
    store.close()


def test_pool_bootstrap_diagnostics_expression_and_secretless_restore(tmp_path):
    app = create_app(tmp_path, start_workers=False)
    with TestClient(app, headers={'Authorization': 'Bearer ' + app.state.settings.token}) as client:
        assert client.put('/api/settings', json={'providers': pool(), 'routing': {'chat': 'smart'}}).status_code == 200
        bootstrap = client.get('/api/bootstrap').json()
        assert not bootstrap['setup_required']
        assert bootstrap['capabilities']['model_pool']
        assert client.get('/api/diagnostics').status_code == 200
        assert client.get('/api/context/status').json()['covered_count'] == 0
        expression = client.post('/api/jargon', json={'entry_type': 'expression', 'term': '稍等我捋一捋', 'meaning': '梳理讨论时使用'}).json()
        assert expression['status'] == 'pending' and not expression['use_allowed']
        assert len(client.get('/api/expressions').json()) == 1
        assert client.get('/api/jargon').json() == []
        archive_bytes = client.get('/api/backup').content
        with zipfile.ZipFile(io.BytesIO(archive_bytes)) as archive:
            raw = archive.read('settings.json')
            assert b'smart-test-key' not in raw
            cfg = json.loads(raw)
            assert all('api_key' not in item for item in cfg['providers'])
        result = client.post('/api/backup/restore', files={'file': ('backup.zip', archive_bytes, 'application/zip')})
        assert result.status_code == 200, result.text
        assert app.state.settings.get()['paused']
        assert all(not item['api_key'] for item in app.state.settings.get()['providers'])
        assert resolve_model(app.state.settings.get())['model'] == 'smart-v1'


def _unsolicited_task_response():
    calls = [{'id': 'audit-tool-call', 'type': 'function', 'function': {
        'name': 'create_task', 'arguments': json.dumps({'title': '不应创建的测试待办'})}}]
    return httpx.Response(200, json={
        'choices': [{'message': {'role': 'assistant', 'content': '', 'tool_calls': calls}}],
        'usage': {'prompt_tokens': 20, 'completion_tokens': 10},
    })


def test_text_only_fallback_cannot_execute_unsolicited_tools(tmp_path, monkeypatch):
    from shiori.chat import ChatService

    seen = []
    def handler(request):
        seen.append((request.url.host, json.loads(request.content)))
        return httpx.Response(503) if request.url.host == 'smart.example' else _unsolicited_task_response()

    store, settings, provider = prepare(tmp_path, monkeypatch, handler, without_keys=True)
    providers = settings.public()['providers']
    providers[1]['tools'] = False
    settings.update({'providers': providers, 'routing': {'fallback': ['small']}})

    async def run():
        chat = ChatService(store, settings, provider)
        try:
            with pytest.raises(ProviderError):
                # This phrasing requests a task without taking the deterministic command path.
                await chat.respond('web:default', 'owner', '帮我创建一个待办，标题买牛奶')
        finally:
            await chat.close()

    asyncio.run(run())
    fallback_requests = [payload for host, payload in seen if host == 'small.example']
    assert fallback_requests and all('tools' not in payload for payload in fallback_requests)
    assert store.list_tasks(scope_id='web:default') == []
    store.close()


def test_disabling_model_tools_during_response_prevents_execution(tmp_path, monkeypatch):
    from shiori.chat import ChatService

    seen = []
    async def handler(request):
        seen.append(json.loads(request.content))
        assert 'tools' in seen[-1]
        await asyncio.sleep(0)
        providers = settings.public()['providers']
        providers[0]['tools'] = False
        settings.update({'providers': providers})
        return _unsolicited_task_response()

    store, settings, provider = prepare(tmp_path, monkeypatch, handler, without_keys=True)

    async def run():
        chat = ChatService(store, settings, provider)
        try:
            with pytest.raises(ProviderError):
                await chat.respond('web:default', 'owner', '帮我创建一个待办，标题买牛奶')
        finally:
            await chat.close()

    asyncio.run(run())
    assert len(seen) == 1
    assert not settings.get()['providers'][0]['tools']
    assert store.list_tasks(scope_id='web:default') == []
    store.close()


def test_removing_actual_provider_during_response_prevents_execution(tmp_path, monkeypatch):
    from shiori.chat import ChatService

    async def handler(request):
        assert 'tools' in json.loads(request.content)
        await asyncio.sleep(0)
        providers = [item for item in settings.public()['providers'] if item['id'] != 'smart']
        settings.update({'providers': providers, 'routing': {'chat': ''}})
        return _unsolicited_task_response()

    store, settings, provider = prepare(tmp_path, monkeypatch, handler, without_keys=True)

    async def run():
        chat = ChatService(store, settings, provider)
        try:
            with pytest.raises(ProviderError):
                await chat.respond('web:default', 'owner', '帮我创建一个待办，标题买牛奶')
        finally:
            await chat.close()

    asyncio.run(run())
    assert all(item['id'] != 'smart' for item in settings.get()['providers'])
    assert store.list_tasks(scope_id='web:default') == []
    store.close()
