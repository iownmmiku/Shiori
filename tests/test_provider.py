import asyncio

import httpx
import pytest

from qunyu.db import Store
from shiori.provider import Provider, ProviderError, endpoint
from shiori.settings import Settings


def test_provider_http_contract_fallback_usage_and_no_secret_leak(tmp_path, monkeypatch):
    store, settings = Store(tmp_path), Settings(tmp_path)
    settings.update({'model': {'base_url': 'https://fixture.example/v1', 'model': 'fixture-model', 'api_key': 'fixture-key-not-real', 'input_price': 2, 'output_price': 8}})
    seen = []
    def handler(request):
        import json
        body = json.loads(request.content)
        seen.append(body)
        assert request.headers['authorization'] == 'Bearer fixture-key-not-real'
        if body.get('tools'):
            return httpx.Response(400, json={'error': 'tools unsupported'})
        return httpx.Response(200, json={'choices': [{'message': {'role': 'assistant', 'content': '接口已正确调用'}}], 'usage': {'prompt_tokens': 50, 'completion_tokens': 10}})
    original = httpx.AsyncClient
    monkeypatch.setattr(httpx, 'AsyncClient', lambda *a, **kw: original(*a, transport=httpx.MockTransport(handler), **kw))
    provider = Provider(settings, store)
    result = asyncio.run(provider.complete([{'role': 'user', 'content': '你好'}], tools=[{'type': 'function'}]))
    assert result['content'] == '接口已正确调用'
    assert len(seen) == 2
    assert store.stats()['input_tokens'] == 50
    assert not result['usage']['estimated']
    assert 'fixture-key-not-real' not in str(store.list_events()) + str(store.list_usage())


def test_model_budget_prevents_call(tmp_path, monkeypatch):
    store, settings = Store(tmp_path), Settings(tmp_path)
    settings.update({'model': {'base_url': 'https://fixture.example/v1', 'model': 'fixture'}, 'budget': {'daily_tokens': 10}})
    store.record_usage({'input_tokens': 10, 'output_tokens': 1, 'cost': 0, 'model': 'fixture'})
    with pytest.raises(ProviderError, match='预算'):
        asyncio.run(Provider(settings, store).complete([{'role': 'user', 'content': '你好'}]))


def test_endpoint_validation():
    assert endpoint('https://service.example/v1/chat/completions', 'models') == 'https://service.example/v1/models'
    for bad in ('ftp://example.com', 'https://key@example.com/v1', 'https://example.com/v1?key=secret'):
        with pytest.raises(ProviderError): endpoint(bad)
