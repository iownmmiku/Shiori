"""Independent cache, authority and cancellation checks using local fakes."""
import asyncio
from copy import deepcopy
from types import SimpleNamespace
import json

import httpx
import pytest

from shiori.db import Store
from shiori.chat import ChatService
from shiori.memory import MemoryEngine, SemanticBudget
from shiori.settings import DEFAULTS, PROVIDER_DEFAULTS, Settings
from test_planner_operational_diagnostics import (
    install_transport, message as planner_message, response as planner_response,
    setup_social, valid_plan,
)


@pytest.fixture
def anyio_backend():
    return 'asyncio'


def memory_config():
    cfg = deepcopy(DEFAULTS)
    cfg['model'].update(base_url='https://fixture.invalid/v1', model='fixture', api_key='fixture-key-one')
    cfg['memory'].update(semantic_enabled=True, embedding_model='embedding-fixture',
        background_indexing=True, query_cache_ttl_seconds=30, query_cache_entries=64,
        vector_cache_mb=1)
    return cfg


def fact(store, scope, user, content, *, visibility='private'):
    return store.create_memory({'scope_id': scope, 'user_id': user, 'content': content,
                                'status': 'active', 'visibility': visibility})


@pytest.mark.anyio
async def test_query_cache_never_shares_a_private_projection_between_members(tmp_path):
    store, cfg, calls = Store(tmp_path), memory_config(), []
    alice = fact(store, 'web:default', 'alice', '喜欢的饮料是咖啡，只有Alice知道')
    bob = fact(store, 'web:default', 'bob', '喜欢的饮料是茶，只有Bob知道')
    query = '喜欢的饮料'

    async def embed(texts, *, scope_id, before_send, **kwargs):
        assert before_send()
        calls.append((scope_id, tuple(texts)))
        return [[1., 0.] for _ in texts]

    engine = MemoryEngine(store, cfg, embed, foreground_busy=lambda: True)
    try:
        a = await engine.inspect('web:default', 'alice', query)
        b = await engine.inspect('web:default', 'bob', query)
        assert {item['id'] for item in a['items']} == {alice['id']}
        assert {item['id'] for item in b['items']} == {bob['id']}
        assert len([call for call in calls if call[1] == (query,)]) == 2
        warm = await engine.inspect('web:default', 'alice', query)
        assert {item['id'] for item in warm['items']} == {alice['id']}
        assert len([call for call in calls if call[1] == (query,)]) == 2
    finally:
        await engine.aclose()
        store.close()


@pytest.mark.anyio
@pytest.mark.parametrize('change', ['account', 'model', 'scope'])
async def test_query_cache_rechecks_account_model_and_conversation_boundary(tmp_path, change):
    store, cfg, calls = Store(tmp_path), memory_config(), []
    target = fact(store, 'web:default', 'owner', '喜欢的饮料是咖啡')
    query = '喜欢的饮料'

    async def embed(texts, *, scope_id, before_send, **kwargs):
        assert before_send()
        calls.append((scope_id, tuple(texts)))
        return [[1., 0.] for _ in texts]

    engine = MemoryEngine(store, cfg, embed, foreground_busy=lambda: True)
    try:
        first = await engine.inspect('web:default', 'owner', query)
        assert first['items'][0]['id'] == target['id']
        scope = 'web:default'
        if change == 'account':
            cfg['model']['api_key'] = 'fixture-key-two'
        elif change == 'model':
            cfg['memory']['embedding_model'] = 'different-embedding-fixture'
        else:
            scope = 'web:other'
            store.upsert_scope({'id': scope})
            fact(store, scope, 'owner', '喜欢的饮料是咖啡')
        second = await engine.inspect(scope, 'owner', query)
        assert second['items']
        assert len([call for call in calls if call[1] == (query,)]) == 2
    finally:
        await engine.aclose()
        store.close()


@pytest.mark.anyio
async def test_warm_query_cannot_restore_a_deleted_or_edited_memory(tmp_path):
    store, cfg, calls = Store(tmp_path), memory_config(), []
    old = fact(store, 'web:default', 'owner', '喜欢的饮料是咖啡')
    query = '喜欢的饮料'

    async def embed(texts, **kwargs):
        calls.append(tuple(texts))
        return [[1., 0.] for _ in texts]

    engine = MemoryEngine(store, cfg, embed, foreground_busy=lambda: True)
    try:
        assert (await engine.inspect('web:default', 'owner', query))['items'][0]['id'] == old['id']
        store.update_memory(old['id'], {'content': '喜欢的饮料现在是茶，旧偏好已纠正'})
        updated = await engine.inspect('web:default', 'owner', query)
        assert updated['items'] and all('现在是茶' in item['excerpt'] for item in updated['items'])
        store.delete_memory(old['id'])
        deleted = await engine.inspect('web:default', 'owner', query)
        assert not deleted['items']
        assert len([call for call in calls if call == (query,)]) == 1
    finally:
        await engine.aclose()
        store.close()


@pytest.mark.anyio
async def test_one_waiter_cancel_does_not_cancel_another_current_query(tmp_path):
    store, cfg = Store(tmp_path), memory_config()
    target = fact(store, 'web:default', 'owner', '喜欢的饮料是咖啡')
    query = '喜欢的饮料'
    entered, release, both_ranked = asyncio.Event(), asyncio.Event(), asyncio.Event()
    calls, ranked = [], 0

    async def embed(texts, *, before_send, **kwargs):
        assert before_send()
        calls.append(tuple(texts))
        entered.set()
        await release.wait()
        return [[1., 0.] for _ in texts]

    engine = MemoryEngine(store, cfg, embed, foreground_busy=lambda: True)
    original_rank = engine._rank_async

    async def rank(*args, **kwargs):
        nonlocal ranked
        result = await original_rank(*args, **kwargs)
        ranked += 1
        if ranked == 2:
            both_ranked.set()
        return result

    engine._rank_async = rank
    first = second = None
    try:
        first = asyncio.create_task(engine.inspect('web:default', 'owner', query))
        await asyncio.wait_for(entered.wait(), 2)
        second = asyncio.create_task(engine.inspect('web:default', 'owner', query))
        await asyncio.wait_for(both_ranked.wait(), 2)
        await asyncio.sleep(0)
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        release.set()
        result = await asyncio.wait_for(second, 2)
        assert result['items'][0]['id'] == target['id']
        assert calls == [(query,)]
    finally:
        release.set()
        for task in (first, second):
            if task and not task.done():
                task.cancel()
        await asyncio.gather(*(task for task in (first, second) if task), return_exceptions=True)
        await engine.aclose()
        store.close()


@pytest.mark.anyio
async def test_last_query_waiter_cancel_drains_request_and_allows_fresh_retry(tmp_path):
    store, cfg = Store(tmp_path), memory_config()
    fact(store, 'web:default', 'owner', '喜欢的饮料是咖啡')
    query = '喜欢的饮料'
    entered, upstream_cancelled = asyncio.Event(), asyncio.Event()
    calls = []

    async def embed(texts, **kwargs):
        calls.append(tuple(texts))
        if len(calls) == 1:
            entered.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                upstream_cancelled.set()
                raise
        return [[1., 0.] for _ in texts]

    engine = MemoryEngine(store, cfg, embed, foreground_busy=lambda: True)
    task = None
    try:
        task = asyncio.create_task(engine.inspect('web:default', 'owner', query))
        await asyncio.wait_for(entered.wait(), 2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await asyncio.wait_for(upstream_cancelled.wait(), 2)
        result = await asyncio.wait_for(engine.inspect('web:default', 'owner', query), 2)
        assert result['items']
        assert calls == [(query,), (query,)]
    finally:
        if task and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await engine.aclose()
        store.close()


@pytest.mark.anyio
async def test_shared_recall_timeout_drains_last_query_and_forbids_further_billing(tmp_path, monkeypatch):
    store, cfg = Store(tmp_path), memory_config()
    target = fact(store, 'web:default', 'owner', '喜欢的饮料是咖啡')
    entered, cancelled, calls = asyncio.Event(), asyncio.Event(), []

    async def embed(texts, *, before_send, **kwargs):
        assert before_send()
        calls.append(tuple(texts))
        entered.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            raise

    engine = MemoryEngine(store, cfg, embed, foreground_busy=lambda: True)
    loop = asyncio.get_running_loop()
    budget = SemanticBudget(loop.time() + 60)
    deadlines = []
    original_timeout_at = asyncio.timeout_at

    def controlled_timeout_at(when):
        assert when == budget.deadline
        timeout = original_timeout_at(None)
        deadlines.append(timeout)
        return timeout

    # The lexical read can outlast a short wall-clock budget before any embed
    # starts. Arm the real timeout only after the provider request is in flight.
    monkeypatch.setattr(asyncio, 'timeout_at', controlled_timeout_at)
    task = None
    try:
        task = asyncio.create_task(engine.inspect('web:default', 'owner', '喜欢的饮料',
                                                  semantic_deadline=budget))
        await asyncio.wait_for(entered.wait(), 2)
        assert len(deadlines) == 1
        deadlines[0].reschedule(loop.time())
        first = await asyncio.wait_for(task, 2)
        assert first['items'][0]['id'] == target['id']
        assert first['semantic']['error'] == 'TimeoutError'
        assert budget.exhausted
        assert cancelled.is_set()
        assert not engine._query_flights and not engine._query_jobs
        # Keep the nominal deadline in the future: the exhausted marker must
        # prevent another billed call even if the timer fired slightly early.
        assert loop.time() < budget.deadline
        next_query = await asyncio.wait_for(engine.inspect('web:default', 'owner', '咖啡',
                                                           semantic_deadline=budget), 2)
        assert next_query['items']
        assert calls == [('喜欢的饮料',)]
        assert len(deadlines) == 1
    finally:
        if task and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await engine.aclose()
        store.close()


@pytest.mark.anyio
async def test_permission_revocation_while_query_waits_never_publishes_old_projection(tmp_path):
    store, cfg = Store(tmp_path), memory_config()
    fact(store, 'web:default', 'owner', '喜欢的饮料是咖啡')
    entered, release, calls = asyncio.Event(), asyncio.Event(), []

    async def embed(texts, *, before_send, **kwargs):
        assert before_send()
        calls.append(tuple(texts))
        entered.set()
        await release.wait()
        return [[1., 0.] for _ in texts]

    engine = MemoryEngine(store, cfg, embed, foreground_busy=lambda: True)
    task = None
    try:
        task = asyncio.create_task(engine.inspect('web:default', 'owner', '喜欢的饮料'))
        await asyncio.wait_for(entered.wait(), 2)
        cfg['paused'] = True
        release.set()
        result = await asyncio.wait_for(task, 2)
        assert not result['items']
        assert calls == [('喜欢的饮料',)]
    finally:
        release.set()
        if task and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await engine.aclose()
        store.close()


@pytest.mark.anyio
@pytest.mark.parametrize('text,needed,expected_seconds', [
    ('今天想聊聊新番', None, 1),
    ('还记得昨天晚上我说了什么吗', None, 4),
    ('请回答一下这个问题', ['昨天晚上提到的喜好'], 4),
    ('请回答一下这个问题', ['普通背景资料'], 1),
])
async def test_reply_recall_keeps_deep_memory_budget_and_one_shared_allowance(tmp_path, text, needed, expected_seconds):
    store, cfg, seen = Store(tmp_path), memory_config(), []
    cfg['memory'].update(foreground_semantic_timeout_seconds=1, semantic_timeout_seconds=4)

    class Config:
        def get(self):
            return deepcopy(cfg)

    async def recall(scope, user, query, limit, *, semantic_deadline):
        seen.append((query, semantic_deadline, semantic_deadline.deadline - asyncio.get_running_loop().time()))
        await asyncio.sleep(0)
        return []

    chat = ChatService.__new__(ChatService)
    chat.store = store
    chat.settings = Config()
    chat.memory = SimpleNamespace(recall=recall)
    chat.social = SimpleNamespace(_allowed=lambda *_: True)
    try:
        assert await chat._recall_context('web:default', 'owner', text, needed_info=needed) == []
        assert seen
        assert all(item[1] is seen[0][1] for item in seen)
        # No per-query restart of the turn's allowance; this checks the real
        # orchestration call rather than a copy of its intent classification.
        assert all(expected_seconds - .5 <= item[2] <= expected_seconds + .05 for item in seen)
    finally:
        store.close()


@pytest.mark.parametrize('bad', [True, float('nan'), float('inf'), 10 ** 1000, '3'],
                         ids=['boolean', 'nan', 'infinite', 'huge-integer', 'text'])
def test_direct_memory_config_bad_budget_is_bounded_without_throwing(bad):
    config = {'foreground_semantic_timeout_seconds': bad, 'semantic_timeout_seconds': bad}
    assert ChatService._recall_budget_seconds(config, '普通聊天') == 3
    assert ChatService._recall_budget_seconds(config, '昨天说了什么') == 8


def test_new_memory_settings_roundtrip_and_reject_bad_limits_atomically(tmp_path):
    settings = Settings(tmp_path)
    patch = {'foreground_semantic_timeout_seconds': 1, 'query_cache_ttl_seconds': 300,
             'query_cache_entries': 0, 'vector_cache_mb': 0}
    settings.update({'memory': patch})
    reloaded = Settings(tmp_path)
    assert all(reloaded.get()['memory'][key] == value for key, value in patch.items())
    before = settings.get()
    limits = {'foreground_semantic_timeout_seconds': (1, 120),
              'query_cache_ttl_seconds': (0, 300), 'query_cache_entries': (0, 256),
              'vector_cache_mb': (0, 64)}
    for key, (low, high) in limits.items():
        for value in (True, float('nan'), float('inf'), low - 1, high + 1, '1'):
            with pytest.raises(ValueError):
                settings.update({'memory': {key: value}})
            assert settings.get() == before


@pytest.fixture
def planner_runtime(tmp_path):
    store, config, scope, provider, social = setup_social(tmp_path)
    config.value['planner'].update(structured_output='auto', format_retry_enabled=True, max_age_ms=5000)
    config.value['providers'] = [dict(PROVIDER_DEFAULTS, id='backup', name='Fixture backup',
        base_url='https://backup.fixture.invalid/v1', model='backup', stream_mode='off')]
    config.value['routing']['fallback'] = ['backup']
    yield store, config, scope, provider, social
    store.close()


@pytest.mark.anyio
@pytest.mark.parametrize('status', [400, 422])
async def test_auto_format_compatibility_is_one_bounded_fallback_then_cached(planner_runtime, monkeypatch, status):
    store, _, scope, provider, social = planner_runtime
    calls = []

    def handler(request):
        payload = json.loads(request.content)
        calls.append(payload)
        if len(calls) == 1:
            assert payload['response_format']['type'] == 'json_object'
            return httpx.Response(status, json={'error': {
                'message': 'response_format is not supported by this model',
                'type': 'invalid_request_error', 'param': 'response_format', 'code': 'unsupported_parameter'}})
        assert 'response_format' not in payload
        return planner_response(valid_plan(request))

    install_transport(monkeypatch, handler)
    first = await social.plan(scope, planner_message(store, scope), direct=True)
    assert first['reply'] and len(calls) == 2
    assert (await social.plan(scope, planner_message(store, scope), direct=True))['reply']
    assert len(calls) == 3
    assert not provider._reservations and provider._global_gate.active == 0


@pytest.mark.anyio
async def test_repeated_unsupported_format_error_cannot_retry_forever(planner_runtime, monkeypatch):
    store, _, scope, provider, social = planner_runtime
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(400, json={'error': {'message': 'response_format not supported',
                                                  'param': 'response_format'}})

    install_transport(monkeypatch, handler)
    result = await social.plan(scope, planner_message(store, scope), direct=True)
    assert not result['reply']
    assert len(calls) == 2
    assert not provider._reservations and provider._global_gate.active == 0


@pytest.mark.anyio
async def test_format_compatibility_rechecks_source_before_second_dispatch(planner_runtime, monkeypatch):
    store, config, scope, _, social = planner_runtime
    calls = []

    def handler(request):
        calls.append(request)
        config.value['paused'] = True
        return httpx.Response(400, json={'error': {'message': 'response_format not supported',
                                                  'param': 'response_format'}})

    install_transport(monkeypatch, handler)
    assert not (await social.plan(scope, planner_message(store, scope), direct=True))['reply']
    assert len(calls) == 1


@pytest.mark.anyio
@pytest.mark.parametrize('failure', ['content_filter', 'wrong_target', 'unrelated_bad_request'])
async def test_structured_planner_failures_cannot_become_a_backup_reply(planner_runtime, monkeypatch, failure):
    store, _, scope, provider, social = planner_runtime
    calls = []

    def handler(request):
        calls.append(request)
        assert request.url.host == 'fixture.invalid', 'Terminal failure must not dispatch to a backup'
        if failure == 'unrelated_bad_request':
            return httpx.Response(400, json={'error': {'message': 'Invalid input supplied'}})
        if failure == 'content_filter':
            return httpx.Response(200, json={'choices': [{'finish_reason': 'content_filter',
                'message': {'content': valid_plan(request)}}],
                'usage': {'prompt_tokens': 40, 'completion_tokens': 5}})
        content = json.loads(valid_plan(request))
        content['target_message_id'] = 'a-different-message'
        return planner_response(json.dumps(content))

    install_transport(monkeypatch, handler)
    result = await social.plan(scope, planner_message(store, scope), direct=True)
    assert not result['reply']
    assert len(calls) == 1
    assert not provider._reservations and provider._global_gate.active == 0


@pytest.mark.anyio
async def test_invalid_json_without_configured_backup_never_guesses_reply(planner_runtime, monkeypatch):
    store, config, scope, _, social = planner_runtime
    config.value['routing']['fallback'] = []
    calls = []

    def handler(request):
        calls.append(request)
        return planner_response('not a JSON decision, and not an authorization')

    install_transport(monkeypatch, handler)
    result = await social.plan(scope, planner_message(store, scope), direct=True)
    assert not result['reply'] and result['error_code'] == 'invalid_json'
    assert len(calls) == 1


@pytest.mark.anyio
async def test_partial_stream_cannot_enter_format_retry_or_become_a_decision(planner_runtime, monkeypatch):
    store, config, scope, provider, social = planner_runtime
    config.value['model'].update(stream_mode='on', timeout_fallback=False)
    calls = []

    class Partial(httpx.AsyncByteStream):
        async def __aiter__(self):
            payload = {'choices': [{'index': 0, 'delta': {'content': '{"action":"reply"'}}]}
            yield ('data: ' + json.dumps(payload) + '\n\n').encode()

    def handler(request):
        calls.append(request)
        assert request.url.host == 'fixture.invalid'
        return httpx.Response(200, headers={'content-type': 'text/event-stream'}, stream=Partial())

    install_transport(monkeypatch, handler)
    result = await social.plan(scope, planner_message(store, scope), direct=True)
    assert not result['reply'] and result['error_code'] == 'stream_incomplete'
    assert len(calls) == 1
    assert not provider._reservations and provider._global_gate.active == 0
    assert store.list_usage()[0]['unconfirmed']
