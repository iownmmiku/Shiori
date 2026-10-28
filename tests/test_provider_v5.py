import asyncio
import copy
import json

import httpx
import pytest

from qunyu.db import Store
from shiori.provider import Provider, ProviderError, estimate_tokens
from shiori.settings import DEFAULTS, PROVIDER_DEFAULTS, Settings


@pytest.fixture
def anyio_backend():
    return 'asyncio'


@pytest.fixture
def setup(tmp_path, monkeypatch):
    store = Store(tmp_path / 'provider-v5')
    cfg = copy.deepcopy(DEFAULTS)
    cfg['model'].update(base_url='https://default.example/v1', model='default-model')
    cfg['providers'] = [dict(PROVIDER_DEFAULTS, id=key, name=key, base_url=f'https://{key}.example/v1', model=key)
                        for key in ('a', 'b')]
    cfg['routing'].update(chat='a', summary='b', planner='b', embedding='b')
    cfg['memory']['semantic_enabled'] = True

    class InMemorySettings:
        def get(self):
            return copy.deepcopy(cfg)

    original = httpx.AsyncClient

    def transport(handler):
        monkeypatch.setattr(httpx, 'AsyncClient', lambda *args, **kwargs:
                            original(*args, transport=httpx.MockTransport(handler), **kwargs))

    yield cfg, store, Provider(InMemorySettings(), store), transport
    store.close()


def reply(*, usage=True, content='模拟答复'):
    data = {'choices': [{'message': {'role': 'assistant', 'content': content}}]}
    if usage:
        data['usage'] = {'prompt_tokens': 5, 'completion_tokens': 16}
    return httpx.Response(200, json=data)


@pytest.mark.anyio
@pytest.mark.parametrize('route', ['chat', 'planner', 'summary'])
async def test_input_fee_alone_can_block_request_with_free_output(setup, route):
    cfg, store, provider, transport = setup
    cfg['budget']['daily_cost'] = 1
    for item in cfg['providers']:
        item.update(input_price=1000000, output_price=0)
    transport(lambda request: pytest.fail('input cost already exceeds the budget'))
    with pytest.raises(ProviderError, match='费用预算'):
        await provider.complete([{'role': 'user', 'content': '说明' * 500}], purpose=route)
    assert store.list_usage() == [] and provider.status()['reserved']['requests'] == 0


@pytest.mark.anyio
@pytest.mark.parametrize('channel', ['schema', 'arguments'])
async def test_large_tool_schema_and_arguments_are_budgeted_before_http(setup, channel):
    cfg, store, provider, transport = setup
    cfg['budget']['daily_tokens'] = 100
    messages, tools = [{'role': 'user', 'content': 'hi'}], None
    if channel == 'schema':
        tools = [{'type': 'function', 'function': {'name': 'test', 'parameters': {
            'type': 'object', 'properties': {'value': {'enum': ['x' * 10000]}}}}}]
    else:
        messages.append({'role': 'assistant', 'content': '', 'tool_calls': [{'id': 'c1', 'type': 'function',
            'function': {'name': 'test', 'arguments': json.dumps({'value': 'x' * 10000})}}]})
    transport(lambda request: pytest.fail('tool text must be included in budget'))
    with pytest.raises(ProviderError, match='预算'):
        await provider.complete(messages, tools=tools)
    assert store.list_usage() == []


@pytest.mark.anyio
async def test_missing_usage_estimates_actual_schema_and_returned_tool_arguments(setup):
    cfg, store, provider, transport = setup
    tools = [{'type': 'function', 'function': {'name': 'test', 'description': '资料' * 300,
              'parameters': {'type': 'object', 'properties': {'v': {'type': 'string'}}}}}]
    message = {'role': 'assistant', 'content': '', 'tool_calls': [{'id': 'tool-1', 'type': 'function',
               'function': {'name': 'test', 'arguments': json.dumps({'v': 'a' * 300})}}]}
    transport(lambda request: httpx.Response(200, json={'choices': [{'message': message}]}))
    messages = [{'role': 'user', 'content': 'hi'}]
    result = await provider.complete(messages, tools=tools)
    assert result['usage']['estimated']
    assert result['usage']['input_tokens'] == estimate_tokens(messages, tools) > 300
    assert result['usage']['output_tokens'] == estimate_tokens([message]) > 150
    assert provider.status()['reserved']['requests'] == 0


@pytest.mark.anyio
async def test_tool_400_degradation_reestimates_only_actual_second_payload(setup):
    cfg, store, provider, transport = setup
    seen = []

    def handler(request):
        body = json.loads(request.content)
        seen.append(body)
        return httpx.Response(400) if 'tools' in body else reply(usage=False)

    transport(handler)
    messages = [{'role': 'user', 'content': 'hi'}]
    tools = [{'type': 'function', 'function': {'name': 'test', 'description': 'x' * 3000}}]
    result = await provider.complete(messages, tools=tools)
    assert len(seen) == 2 and 'tools' not in seen[-1]
    assert result['usage']['input_tokens'] == estimate_tokens(messages)
    assert provider.status()['reserved']['requests'] == 0


@pytest.mark.anyio
async def test_guard_is_rechecked_before_tool_degradation_second_send(setup):
    cfg, store, provider, transport = setup
    allowed, seen = [True], []

    def handler(request):
        seen.append(request)
        allowed[0] = False
        return httpx.Response(400)

    transport(handler)
    with pytest.raises(ProviderError, match='权限'):
        await provider.complete([{'role': 'user', 'content': 'hi'}], tools=[{'type': 'function'}], before_send=lambda: allowed[0])
    assert len(seen) == 1 and store.list_usage() == []
    assert provider.status()['reserved']['requests'] == 0


@pytest.mark.anyio
async def test_unrelated_providers_progress_concurrently(setup):
    cfg, store, provider, transport = setup
    entered, release = asyncio.Event(), asyncio.Event()

    async def handler(request):
        if request.url.host == 'a.example':
            entered.set()
            await release.wait()
        return reply()

    transport(handler)
    first = asyncio.create_task(provider.complete([{'role': 'user', 'content': 'hi'}]))
    await asyncio.wait_for(entered.wait(), 2)
    second = await asyncio.wait_for(provider.complete([{'role': 'user', 'content': 'hi'}], purpose='summary'), 2)
    assert not first.done() and second['usage']['provider_id'] == 'b'
    assert provider.status()['providers'][1]['busy']
    release.set()
    await first
    assert provider.status()['reserved']['requests'] == 0 and len(store.list_usage()) == 2


@pytest.mark.anyio
async def test_same_provider_waiter_guard_prevents_stale_send(setup):
    cfg, store, provider, transport = setup
    entered, release, seen, allowed = asyncio.Event(), asyncio.Event(), [], [True]

    async def handler(request):
        seen.append(request)
        entered.set()
        await release.wait()
        return reply()

    transport(handler)
    first = asyncio.create_task(provider.complete([{'role': 'user', 'content': 'hi'}]))
    await asyncio.wait_for(entered.wait(), 2)
    second = asyncio.create_task(provider.complete([{'role': 'user', 'content': 'stale'}], before_send=lambda: allowed[0]))
    await asyncio.sleep(0)
    assert len(seen) == 1
    allowed[0] = False
    release.set()
    await first
    with pytest.raises(ProviderError, match='权限'):
        await second
    assert len(seen) == 1 and provider.status()['reserved']['requests'] == 0


@pytest.mark.anyio
async def test_concurrent_global_reservations_cannot_double_spend_token_budget(setup):
    cfg, store, provider, transport = setup
    cfg['budget']['daily_tokens'] = 100
    entered, release, seen = asyncio.Event(), asyncio.Event(), []

    async def handler(request):
        seen.append(request.url.host)
        if request.url.host == 'a.example':
            entered.set()
            await release.wait()
        return reply()

    transport(handler)
    messages = [{'role': 'user', 'content': 'hi'}]
    first = asyncio.create_task(provider.complete(messages, max_tokens=90))
    await asyncio.wait_for(entered.wait(), 2)
    reserved = provider.status()['reserved']['tokens']
    assert reserved == estimate_tokens(messages) + 90
    with pytest.raises(ProviderError, match='预算'):
        await provider.complete(messages, purpose='summary', max_tokens=90)
    assert seen == ['a.example']
    release.set()
    await first
    await provider.complete(messages, purpose='summary', max_tokens=90)
    assert store.usage_totals()['tokens'] <= 100 and provider.status()['reserved']['requests'] == 0


@pytest.mark.anyio
async def test_concurrent_fee_reservations_include_input_when_output_is_free(setup):
    cfg, store, provider, transport = setup
    cfg['budget'].update(daily_tokens=0, daily_cost=1)
    for item in cfg['providers']:
        item.update(input_price=100000, output_price=0)
    entered, release, seen = asyncio.Event(), asyncio.Event(), []

    async def handler(request):
        seen.append(request.url.host)
        entered.set()
        await release.wait()
        return httpx.Response(200, json={'choices': [{'message': {'content': 'ok'}}],
                                       'usage': {'prompt_tokens': 10, 'completion_tokens': 1}})

    transport(handler)
    messages = [{'role': 'user', 'content': 'x' * 12}]
    first = asyncio.create_task(provider.complete(messages, max_tokens=16))
    await asyncio.wait_for(entered.wait(), 2)
    assert provider.status()['reserved']['cost'] == pytest.approx(1)
    with pytest.raises(ProviderError, match='费用预算'):
        await provider.complete(messages, purpose='summary', max_tokens=16)
    assert seen == ['a.example']
    release.set()
    await first
    assert store.usage_totals()['cost'] == pytest.approx(1)


@pytest.mark.anyio
async def test_scope_reservations_apply_across_routes_but_not_other_scopes(setup):
    cfg, store, provider, transport = setup
    cfg['budget']['daily_tokens'] = 0
    store.upsert_scope({'id': 'web:default', 'daily_tokens': 100})
    entered, release, seen = asyncio.Event(), asyncio.Event(), []

    async def handler(request):
        seen.append(request.url.host)
        if request.url.host == 'a.example':
            entered.set()
            await release.wait()
        return reply()

    transport(handler)
    messages = [{'role': 'user', 'content': 'hi'}]
    first = asyncio.create_task(provider.complete(messages, max_tokens=90))
    await asyncio.wait_for(entered.wait(), 2)
    with pytest.raises(ProviderError, match='会话'):
        await provider.complete(messages, purpose='summary', max_tokens=90)
    await asyncio.wait_for(provider.complete(messages, purpose='summary', scope_id='web:other', max_tokens=90), 2)
    release.set()
    await first
    assert seen == ['a.example', 'b.example']


@pytest.mark.anyio
@pytest.mark.parametrize('first_kind', ['chat', 'embedding'])
async def test_embedding_and_chat_share_one_atomic_budget(setup, first_kind):
    cfg, store, provider, transport = setup
    cfg['budget']['daily_tokens'] = 100
    entered, release, seen = asyncio.Event(), asyncio.Event(), []

    async def handler(request):
        seen.append(request.url.path)
        entered.set()
        await release.wait()
        if request.url.path.endswith('embeddings'):
            return httpx.Response(200, json={'data': [{'index': 0, 'embedding': [1., 0.]}], 'usage': {'prompt_tokens': 90}})
        return reply()

    transport(handler)
    messages = [{'role': 'user', 'content': 'hi'}]
    if first_kind == 'chat':
        first = asyncio.create_task(provider.complete(messages, max_tokens=90))
        await asyncio.wait_for(entered.wait(), 2)
        with pytest.raises(ProviderError, match='预算'):
            await provider.embed(['x' * 20])
    else:
        first = asyncio.create_task(provider.embed(['x' * 180]))
        await asyncio.wait_for(entered.wait(), 2)
        with pytest.raises(ProviderError, match='预算'):
            await provider.complete(messages, max_tokens=90)
    assert len(seen) == 1
    release.set()
    await first
    assert provider.status()['reserved']['requests'] == 0


@pytest.mark.anyio
async def test_known_http_failure_releases_reservation_before_fallback(setup):
    cfg, store, provider, transport = setup
    cfg['budget']['daily_tokens'] = 100
    cfg['routing']['fallback'] = ['b']
    seen = []

    def handler(request):
        seen.append(request.url.host)
        return httpx.Response(503) if request.url.host == 'a.example' else reply()

    transport(handler)
    result = await provider.complete([{'role': 'user', 'content': 'hi'}], max_tokens=90)
    assert seen == ['a.example', 'a.example', 'b.example']
    assert result['usage']['provider_id'] == 'b'
    assert len(store.list_usage()) == 1 and provider.status()['reserved']['requests'] == 0


@pytest.mark.anyio
@pytest.mark.parametrize('failure', ['timeout', 'cancel', 'invalid_response'])
async def test_unknown_dispatch_keeps_explicit_usage_estimate_and_does_not_fallback(setup, failure):
    cfg, store, provider, transport = setup
    cfg['routing']['fallback'] = ['b']
    entered, release, seen = asyncio.Event(), asyncio.Event(), []

    async def handler(request):
        seen.append(request.url.host)
        if failure == 'timeout':
            raise httpx.ReadTimeout('remote-secret-body', request=request)
        if failure == 'invalid_response':
            return httpx.Response(200, json={'choices': 'not a valid response'})
        entered.set()
        await release.wait()
        return reply()

    transport(handler)
    if failure == 'cancel':
        task = asyncio.create_task(provider.complete([{'role': 'user', 'content': 'hi'}], max_tokens=90))
        await asyncio.wait_for(entered.wait(), 2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    else:
        with pytest.raises(ProviderError):
            await provider.complete([{'role': 'user', 'content': 'hi'}], max_tokens=90)
    assert seen == ['a.example']
    usage = store.list_usage()
    assert len(usage) == 1 and usage[0]['unconfirmed'] and usage[0]['estimated']
    assert usage[0]['input_tokens'] + usage[0]['output_tokens'] == 95
    assert 'remote-secret-body' not in json.dumps(usage) + json.dumps(provider.status())
    assert provider.status()['reserved']['requests'] == 0


@pytest.mark.anyio
async def test_removed_model_configuration_cancels_waiter_before_send(setup):
    cfg, store, provider, transport = setup
    entered, release, seen = asyncio.Event(), asyncio.Event(), []

    async def handler(request):
        seen.append(request.url.host)
        entered.set()
        await release.wait()
        return reply()

    transport(handler)
    messages = [{'role': 'user', 'content': 'hi'}]
    first = asyncio.create_task(provider.complete(messages))
    await asyncio.wait_for(entered.wait(), 2)
    second = asyncio.create_task(provider.complete(messages))
    await asyncio.sleep(0)
    cfg['providers'][0]['api_key'] = 'changed-authorization'
    release.set()
    with pytest.raises(ProviderError, match='响应已收到'):
        await first
    with pytest.raises(ProviderError, match='配置'):
        await second
    assert seen == ['a.example']


@pytest.mark.parametrize('raw', [b'{broken-json', b'[]', b'{"model":[]}', b'{"budget":{"daily_cost":"oops"}}',
                                b'{"providers":"broken"}', b'{"paused":"false"}', b'{"model":{"api_key":"unknown-encrypted-key"}}', b'\xff\x00'])
def test_corrupt_settings_preserve_bytes_and_start_paused_without_connections(tmp_path, raw):
    path = tmp_path / 'settings.json'
    path.write_bytes(raw)
    settings = Settings(tmp_path)
    cfg = settings.get()
    assert cfg['paused'] and not cfg['onebot']['enabled'] and not cfg['qq_official']['enabled']
    assert cfg['mcp_servers'] == cfg['providers'] == []
    assert not cfg['exploration']['enabled'] and not cfg['model']['api_key']
    assert path.read_bytes() == raw and settings.recovery_path.read_bytes() == raw
    assert '备份恢复' in settings.warning
    again = Settings(tmp_path)
    assert again.recovery_path == settings.recovery_path
    assert len(list(tmp_path.glob('settings.corrupt-*.json'))) == 1
    settings.update({'identity': {'name': 'shiori'}})
    assert json.loads(path.read_text('utf-8'))['identity']['name'] == 'shiori'
    assert settings.recovery_path.read_bytes() == raw
    assert Settings(tmp_path).get()['paused']


def test_unreadable_original_cannot_be_overwritten_without_preserving_it(tmp_path, monkeypatch):
    raw = b'{corrupt-and-sensitive'
    path = tmp_path / 'settings.json'
    path.write_bytes(raw)

    def unavailable(self):
        raise OSError('cannot create backup')

    monkeypatch.setattr(Settings, '_preserve_original', unavailable)
    settings = Settings(tmp_path)
    assert settings.get()['paused'] and '保持不动' in settings.warning
    with pytest.raises(OSError):
        settings.update({'identity': {'name': 'new'}})
    assert path.read_bytes() == raw and settings.get()['identity']['name'] == DEFAULTS['identity']['name']


def test_legacy_partial_settings_load_with_defaults_and_no_recovery(tmp_path):
    (tmp_path / 'settings.json').write_text(json.dumps({'model': {'base_url': 'https://local.example/v1', 'model': 'old-model'},
                                                     'budget': {'daily_tokens': 500}}), 'utf-8')
    settings = Settings(tmp_path)
    assert settings.warning == '' and settings.recovery_path is None
    assert settings.get()['model']['model'] == 'old-model'
    assert settings.get()['routing']['planner'] == '' and not settings.get()['paused']


def test_planner_route_status_and_price_validation(tmp_path):
    settings = Settings(tmp_path)
    settings.update({'model': {'base_url': 'https://default.example/v1', 'model': 'default', 'input_price': 1},
                     'providers': [dict(id='planner', name='规划模型', base_url='https://planner.example/v1', model='planner')],
                     'routing': {'planner': 'planner'}})
    with pytest.raises(ValueError, match='单价'):
        settings.update({'budget': {'daily_cost': 1}})
    providers = settings.public()['providers']
    providers[0]['input_price'] = 1
    settings.update({'providers': providers, 'budget': {'daily_cost': 1}})
    store = Store(tmp_path)
    try:
        assert Provider(settings, store).status()['routes']['planner']['provider_id'] == 'planner'
    finally:
        store.close()


@pytest.mark.anyio
async def test_persona_knowledge_usage_does_not_create_a_conversation(setup):
    cfg, store, provider, transport = setup
    scopes = copy.deepcopy(store.list_scopes())
    transport(lambda request: reply())
    await provider.complete([{'role': 'user', 'content': '查阅兴趣资料'}], purpose='knowledge', scope_id='persona:default')
    assert store.list_scopes() == scopes
    assert store.list_usage()[0]['scope_id'] == 'persona:default'
    assert store.usage_totals('persona:default')['tokens'] == 21
    assert store.usage_totals('web:default')['tokens'] == 0
