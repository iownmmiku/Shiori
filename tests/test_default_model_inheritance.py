"""Catalog-only setup must support background work without changing old routes."""
from copy import deepcopy

import httpx
import pytest

from shiori.db import Store
from shiori.memory_lifecycle import MemoryLifecycle
from shiori.model_catalog import ModelCatalog
from shiori.provider import Provider, ProviderError
from shiori.settings import DEFAULTS, Settings, resolve_model


@pytest.fixture
def anyio_backend():
    return 'asyncio'


def catalog_only():
    config = deepcopy(DEFAULTS)
    config['provider_sources'] = [{'id': 'source', 'name': 'Fixture source', 'base_url': 'https://fixture.invalid/v1', 'api_key': '', 'enabled': True}]
    config['providers'] = [{'id': 'chat', 'name': 'Catalog chat', 'source_id': 'source',
                            'model': 'chat-model', 'enabled': True, 'tools': True}]
    config['routing']['chat'] = 'chat'
    return config


@pytest.mark.parametrize('purpose', ['planner', 'memory', 'gate', 'summary', 'memory_extract',
                                    'jargon_learning', 'expression_learning', 'context_digest', 'knowledge', 'sticker_select'])
def test_catalog_only_background_routes_inherit_chat(purpose):
    config = catalog_only()
    assert resolve_model(config, purpose)['id'] == 'chat'
    assert resolve_model(config, purpose)['base_url'] == 'https://fixture.invalid/v1'
    assert config['routing'].get(purpose, '') == ''


@pytest.mark.parametrize('purpose', ['planner', 'memory', 'gate', 'summary'])
def test_existing_inline_default_and_explicit_routes_stay_intact(purpose):
    config = catalog_only()
    config['model'].update(base_url='https://legacy.invalid/v1', model='legacy')
    assert resolve_model(config, purpose)['id'] == 'default'
    config['routing'][purpose] = 'chat'
    assert resolve_model(config, purpose)['id'] == 'chat'
    config['routing'][purpose] = 'deleted'
    assert resolve_model(config, purpose)['enabled'] is False
    assert resolve_model(config, purpose, model_id='default')['model'] == 'legacy'


def test_embedding_remains_independent_and_disabled_sources_do_not_fall_back():
    config = catalog_only()
    assert resolve_model(config, 'embedding')['model'] == ''
    config['provider_sources'][0]['enabled'] = False
    assert resolve_model(config, 'memory')['enabled'] is False
    assert resolve_model(config, 'memory')['model'] == ''
    config['provider_sources'][0]['enabled'] = True
    config['providers'][0]['enabled'] = False
    assert resolve_model(config, 'planner')['enabled'] is False


@pytest.mark.anyio
async def test_quick_catalog_setup_enables_real_background_dispatch(tmp_path, monkeypatch):
    settings, store = Settings(tmp_path), Store(tmp_path)
    provider = Provider(settings, store)
    manager = ModelCatalog(settings, provider, store)
    source = manager.save_source({'name': 'Local fixture', 'base_url': 'https://fixture.invalid/v1', 'api_key': ''})
    model = manager.add_models({'provider_id': source['id'], 'models': ['background-chat']})['added'][0]
    manager.update_routing({'chat': model['id']})
    from types import SimpleNamespace
    lifecycle = MemoryLifecycle(SimpleNamespace(store=store, settings=settings))
    seen = []

    def response(request):
        import json
        seen.append(json.loads(request.content)['model'])
        return httpx.Response(200, json={'choices': [{'message': {'content': '[]'}, 'finish_reason': 'stop'}],
                                         'usage': {'prompt_tokens': 5, 'completion_tokens': 1}})

    original = httpx.AsyncClient
    monkeypatch.setattr(httpx, 'AsyncClient', lambda *args, **kwargs: original(*args, transport=httpx.MockTransport(response), **kwargs))
    try:
        assert lifecycle.allowed()
        snapshot = manager.snapshot()
        assert snapshot['routes']['memory']['model_id'] == model['id']
        for purpose in ('memory_extract', 'planner', 'context_digest'):
            result = await provider.complete([{'role': 'user', 'content': 'Offline inheritance check'}], purpose=purpose)
            assert result['content'] == '[]'
        assert seen == ['background-chat'] * 3
        assert settings.get()['model']['model'] == ''
    finally:
        store.close()
