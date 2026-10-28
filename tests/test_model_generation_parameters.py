"""Regression checks for PR #8 review findings: budgets, custom params, migration."""
import copy
import json
import math

import httpx
import pytest

from qunyu.db import Store
from shiori.provider import Provider, ProviderError, estimate_tokens
from shiori.settings import (CUSTOM_PARAM_RESERVED, CUSTOM_PARAM_MAX_ABS,
                              CUSTOM_PARAM_MAX_STRING, DEFAULTS, MAX_OUTPUT_TOKENS,
                              PROVIDER_DEFAULTS, Settings)


def model_patch(**values):
    return dict(base_url='https://api.example.test/v1', model='test-model', **values)


@pytest.fixture
def settings(tmp_path):
    return Settings(tmp_path)


@pytest.fixture
def anyio_backend():
    return 'asyncio'


@pytest.mark.parametrize('key', sorted(CUSTOM_PARAM_RESERVED))
def test_custom_params_cannot_override_request_or_output_budget_fields(settings, key):
    with pytest.raises(ValueError, match='核心字段'):
        settings.update({'model': model_patch(custom_params={key: 'value'})})
    assert settings.get()['model']['custom_params'] == {}


def test_custom_params_rejects_non_finite_nested_oversized_and_oversized_blob(settings):
    bad_values = [
        {'value': float('nan')},
        {'value': float('inf')},
        {'value': 1e309},
        {'value': {'nested': 1}},
        {'value': [1, 2]},
        {'value': CUSTOM_PARAM_MAX_ABS + 1},
        {'value': 'x' * (CUSTOM_PARAM_MAX_STRING + 1)},
        {'bad-key!': 'x'},
        {f'k{i}': i for i in range(51)},
        {f'k{i}': 'x' * CUSTOM_PARAM_MAX_STRING for i in range(5)},
    ]
    for custom_params in bad_values:
        with pytest.raises(ValueError):
            settings.update({'model': model_patch(custom_params=custom_params)})
    assert settings.get()['model']['custom_params'] == {}


def test_custom_params_accepts_normal_provider_specific_scalars(settings):
    params = {'seed': 42, 'service_tier': 'flex', 'stream_include_usage': False,
              'nullable': None, 'ratio': 0.25, 'provider.vendor-flag': -7}
    updated = settings.update({'model': model_patch(custom_params=params)})
    assert updated['model']['custom_params'] == params
    assert json.dumps(settings.public(), ensure_ascii=False, allow_nan=False)


@pytest.mark.parametrize('value', [-1, MAX_OUTPUT_TOKENS + 1, 1.5, True, '8192', None])
def test_max_tokens_must_be_integer_inside_supported_range(settings, value):
    with pytest.raises(ValueError, match='最大 Tokens'):
        settings.update({'model': model_patch(max_tokens=value)})


@pytest.mark.parametrize('value', [0, 1, 8192, MAX_OUTPUT_TOKENS])
def test_max_tokens_zero_means_no_extra_ceiling(settings, value):
    assert settings.update({'model': model_patch(max_tokens=value)})['model']['max_tokens'] == value


def test_old_configuration_loads_without_silent_output_ceiling(tmp_path):
    old = {'model': {'base_url': 'https://old.example.test/v1', 'model': 'old-default'},
           'providers': [{'id': 'old-model', 'name': 'Old', 'base_url': 'https://old.example.test/v1',
                          'model': 'old-model'}]}
    (tmp_path / 'settings.json').write_text(json.dumps(old), encoding='utf-8')
    loaded = Settings(tmp_path)
    config = loaded.get()
    assert config['model']['max_tokens'] == 0
    assert config['providers'][0]['max_tokens'] == 0
    assert config['model']['custom_params'] == {}
    assert config['providers'][0]['custom_params'] == {}


def test_provider_configuration_cannot_smuggle_reserved_custom_params(settings):
    with pytest.raises(ValueError, match='核心字段'):
        settings.update({'providers': [dict(PROVIDER_DEFAULTS, id='smuggle', name='Smuggle',
                                            base_url='https://api.example.test/v1', model='test-model',
                                            custom_params={'n': 3, 'max_completion_tokens': 1000000})]})
    assert settings.get()['providers'] == []


@pytest.fixture
def provider_setup(tmp_path, monkeypatch):
    store = Store(tmp_path / 'provider-params')
    cfg = copy.deepcopy(DEFAULTS)
    cfg['model'].update(base_url='https://default.example.test/v1', model='default-model', stream_mode='off')
    cfg['budget']['daily_tokens'] = 0
    cfg['budget']['daily_cost'] = 0
    calls, reserved_at_send = [], []

    class InMemorySettings:
        def get(self):
            return copy.deepcopy(cfg)

    provider = Provider(InMemorySettings(), store)
    original = httpx.AsyncClient

    def transport(handler):
        def factory(*args, **kwargs):
            return original(*args, transport=httpx.MockTransport(handler), **kwargs)
        monkeypatch.setattr(httpx, 'AsyncClient', factory)

    try:
        yield cfg, store, provider, transport, calls, reserved_at_send
    finally:
        store.close()


def reply_payload(content='ok'):
    return {'choices': [{'message': {'role': 'assistant', 'content': content}}],
            'usage': {'prompt_tokens': 5, 'completion_tokens': 2}}


@pytest.mark.anyio
@pytest.mark.parametrize('configured,purpose,explicit,expected', [
    (0, 'chat', None, 1000),
    (128, 'summary', None, 128),
    (16000, 'summary', None, 1000),
    (0, 'distillation', 12000, 12000),
    (0, 'distillation', 16000, 16000),
    (8192, 'chat', None, 8192),
    (16000, 'chat', None, 16000),
    (16000, 'distillation', 12000, 12000),
    (8192, 'distillation', 12000, 8192),
])
async def test_output_ceiling_precedence_is_shared_by_reservation_and_request(
        provider_setup, configured, purpose, explicit, expected):
    cfg, _, provider, transport, calls, reserved_at_send = provider_setup
    cfg['model']['max_tokens'] = configured
    messages = [{'role': 'user', 'content': 'hello'}]

    def handler(request):
        calls.append(json.loads(request.content))
        reserved_at_send.append(provider.status()['reserved']['tokens'])
        return httpx.Response(200, json=reply_payload())

    transport(handler)
    await provider.complete(messages, purpose=purpose, max_tokens=explicit)
    assert calls[-1]['max_tokens'] == expected
    assert reserved_at_send[-1] == estimate_tokens(messages) + expected
    assert provider.status()['reserved']['requests'] == 0


@pytest.mark.anyio
async def test_request_payload_never_lets_custom_params_override_core_fields(provider_setup):
    cfg, _, provider, transport, calls, _ = provider_setup
    cfg['model'].update(custom_params={
        'seed': 7, 'service_tier': 'flex', 'n': 3, 'max_completion_tokens': 1000000,
        'tools': [{'type': 'function'}], 'tool_choice': 'none', 'stream': True,
        'temperature': 0.1, 'top_p': 0.2, 'reasoning_effort': 'max',
    }, temperature=0.7, top_p=1.0)
    transport(lambda request: (calls.append(json.loads(request.content)), httpx.Response(200, json=reply_payload()))[1])
    await provider.complete([{'role': 'user', 'content': 'hello'}])
    payload = calls[-1]
    assert payload['seed'] == 7 and payload['service_tier'] == 'flex'
    assert payload['temperature'] == 0.7 and payload['top_p'] == 1.0
    assert payload['max_tokens'] == 1000
    for blocked in ('n', 'max_completion_tokens', 'tools', 'tool_choice', 'stream', 'reasoning_effort'):
        assert blocked not in payload


@pytest.mark.anyio
async def test_invalid_custom_params_fail_before_reservation_or_dispatch(provider_setup):
    _, _, provider, transport, calls, _ = provider_setup
    for custom_params in ({'value': float('nan')}, {'value': float('inf')},
                          {'value': object()}, {'value': 'x' * (CUSTOM_PARAM_MAX_STRING + 1)}):
        cfg, _, _, _, _, _ = provider_setup
        cfg['model']['custom_params'] = custom_params
        transport(lambda request: pytest.fail('invalid custom params must not be sent'))
        with pytest.raises(ProviderError, match='自定义参数'):
            await provider.complete([{'role': 'user', 'content': 'hello'}], config=cfg['model'])
        assert calls == []
        assert provider.status()['reserved']['requests'] == 0


@pytest.mark.anyio
async def test_unserializable_request_is_rejected_before_reservation(provider_setup):
    _, _, provider, transport, calls, _ = provider_setup
    transport(lambda request: pytest.fail('unserializable request must not be sent'))
    with pytest.raises(ProviderError, match='序列化'):
        await provider.complete([{'role': 'user', 'content': object()}])
    assert calls == []
    assert provider.status()['reserved']['requests'] == 0
