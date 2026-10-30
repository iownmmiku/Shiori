"""Authored local chat scenarios and mock transports; no model or QQ credentials."""
import asyncio
from copy import deepcopy
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from qunyu.db import Store
from shiori.onebot import OneBotBridge
from shiori.official import OfficialBridge
from shiori.output import OutputLimitError, split_output
from shiori.provider import Provider, ProviderError
from shiori.settings import DEFAULTS
from shiori.social import SocialEngine


@pytest.fixture
def anyio_backend():
    return 'asyncio'


class Settings:
    def __init__(self):
        self.config = deepcopy(DEFAULTS)
        self.config['model'].update(base_url='https://test.invalid/v1', model='mock')
        self.config['onebot'].update(chat_policy="whitelist", enabled=True, owner_ids=['101'], allowed_groups=['202'])
        self.config['output'].update(interval_ms=0)
        self.config['identity'].update(respond_to_at=False, respond_to_name=False)

    def get(self):
        return deepcopy(self.config)


class MockPlanner:
    def __init__(self):
        self.calls = []
        self.error = None

    async def complete(self, messages, **kwargs):
        self.calls.append((messages, kwargs))
        if self.error:
            raise self.error
        assert kwargs['before_send']()
        payload = json.loads(messages[-1]['content'])
        return {'content': json.dumps({'action': 'reply', 'target_message_id': payload['current_message_id'],
            'target_user_id': payload['current_user_id'], 'addressed_to_bot': False,
            'intent': '补充电子音乐资料', 'reason': '有人分享且可以具体补充', 'needed_info': ['音乐资料']})}


@pytest.fixture
def setup(tmp_path):
    store, settings, planner = Store(tmp_path), Settings(), MockPlanner()
    store.upsert_scope({'id': 'qq:group:202', 'cooldown_seconds': 0, 'merge_window_ms': 0})
    social = SocialEngine(store, settings, planner)
    yield store, settings, planner, social
    store.close()


def message(store, text='今天重听了电子音乐专辑', scope='qq:group:202', user='303', **metadata):
    return store.add_message(scope, user, 'user', text, **metadata)


@pytest.mark.anyio
@pytest.mark.parametrize('field,metadata,text', [
    ('trigger_at', {'at_bot': True}, '今天重听了电子音乐专辑'),
    ('trigger_reply', {'reply_to_bot': True}, '刚才说的电子音乐很有意思'),
    ('trigger_name', {}, '初音未来的电子音乐很有意思'),
])
async def test_disabled_mandatory_reply_preserves_mention_signals_for_planner(setup, field, metadata, text):
    store, _, planner, social = setup
    scope = store.upsert_scope({'id': 'qq:group:202', field: False, 'proactive_reply': True})
    result = await social.plan(scope, message(store, text, **metadata))
    expected_signal = field != 'trigger_reply'
    assert result['reply'] and result['explicit'] is expected_signal
    payload = json.loads(planner.calls[0][0][-1]['content'])
    assert payload['signals'][{'trigger_at': 'at_bot', 'trigger_reply': 'reply_to_bot', 'trigger_name': 'named'}[field]] is expected_signal


@pytest.mark.anyio
async def test_modes_have_distinct_semantic_policy_and_followup_window_is_effective(setup):
    store, _, planner, social = setup
    scope = store.get_scope('qq:group:202')
    await social.plan(scope, message(store))
    balanced = json.loads(planner.calls[-1][0][-1]['content'])['participation']
    social.note_reply(scope['id'], '303')
    scope = store.upsert_scope({'id': scope['id'], 'activity': 'active', 'cooldown_seconds': 600,
                               'followup_seconds': 90, 'planner_min_interval_ms': 0})
    result = await social.plan(scope, message(store, '刚才电子音乐专辑的合成器部分也很好听'))
    active = json.loads(planner.calls[-1][0][-1]['content'])
    assert result['reply'] and active['signals']['followup']
    assert balanced['policy'] != active['participation']['policy']
    scope = store.upsert_scope({'id': scope['id'], 'followup_seconds': 0})
    count = len(planner.calls)
    assert not (await social.plan(scope, message(store)))['reply']
    assert len(planner.calls) == count


@pytest.mark.anyio
async def test_proactive_planner_rate_drops_new_decisions_but_direct_trigger_bypasses_it(setup):
    store, _, planner, social = setup
    scope = store.get_scope('qq:group:202')
    assert (await social.plan(scope, message(store)))['reply']
    assert '间隔' in (await social.plan(scope, message(store)))['reason']
    assert len(planner.calls) == 1
    assert (await social.plan(scope, message(store, '初音未来，帮我想想电子音乐')))['reply']
    assert len(planner.calls) == 2


@pytest.mark.anyio
async def test_local_command_does_not_depend_on_broken_planner_but_still_requires_trigger(setup):
    store, _, planner, social = setup
    planner.error = RuntimeError('broken')
    social.local_command_fn = lambda scope_id, text: '记住' in text
    scope = store.get_scope('qq:group:202')
    assert (await social.plan(scope, message(store, '初音未来，记住我喜欢音乐')))['reply']
    assert not planner.calls
    scope = store.upsert_scope({'id': scope['id'], 'trigger_name': False, 'proactive_reply': False})
    assert not (await social.plan(scope, message(store, '记住我喜欢音乐')))['reply']
    assert not planner.calls


@pytest.mark.anyio
async def test_planner_sees_bounded_knowledge_and_rechecks_its_dependencies(setup):
    store, _, planner, social = setup
    overview = {'memories': [{'id': f'm{i}', 'content': '记忆资料' * 300} for i in range(20)],
                'persona_knowledge': [{'id': f'k{i}', 'title': '音乐', 'text': '网络资料' * 300} for i in range(20)],
                'memory_versions': {'m0': 1}, 'knowledge_refs': {'k0': 'fingerprint'}}
    async def knowledge(*args):
        return overview
    social.knowledge_fn = knowledge
    current = [True]
    social.knowledge_current_fn = lambda scope, user, data: current[0]
    scope = store.get_scope('qq:group:202')
    assert (await social.plan(scope, message(store, '初音未来，聊聊音乐')))['reply']
    payload = json.loads(planner.calls[-1][0][-1]['content'])
    index = payload['available_knowledge']
    assert len(json.dumps(index, ensure_ascii=False)) <= 2500
    assert len(index['memories']) <= 4 and len(index['persona_knowledge']) <= 3
    assert 'memory_versions' not in index
    current[0] = False
    result = await social.plan(scope, message(store, '初音未来，接着聊音乐'))
    assert not result['reply'] and not result.get('error_feedback')
    assert len(planner.calls) == 1  # Guard fails before dispatch.


@pytest.mark.anyio
async def test_expired_queued_planner_is_not_dispatched_and_direct_user_gets_feedback(setup, monkeypatch):
    import shiori.social as social_module
    store, _, _, social = setup
    scope = store.upsert_scope({'id': 'qq:group:202', 'planner_max_age_ms': 1000})
    clock = [100.0]
    monkeypatch.setattr(social_module, 'time', SimpleNamespace(monotonic=lambda: clock[0]))
    dispatched = []
    class Queued:
        async def complete(self, messages, **kwargs):
            clock[0] += 2
            if not kwargs['before_send']():
                raise PermissionError('expired queued work')
            dispatched.append(messages)
            return {'content': '{}'}
    social.provider = Queued()
    result = await social.plan(scope, message(store, '初音未来，聊聊音乐'))
    assert not dispatched and not result['reply'] and result['error_feedback']


@pytest.mark.anyio
async def test_saved_plan_dependencies_can_invalidate_reply_before_chat_generation(setup):
    store, settings, _, social = setup
    current = [True]
    async def knowledge(*args):
        return {'memory_versions': {'music-memory': 2}, 'knowledge_refs': {}, 'persona_id': 'default'}
    social.knowledge_fn = knowledge
    social.knowledge_current_fn = lambda *args: current[0]
    original = social.plan
    async def invalidate_after_plan(*args, **kwargs):
        result = await original(*args, **kwargs)
        assert result['knowledge_dependencies']['memory_versions'] == {'music-memory': 2}
        current[0] = False
        return result
    social.plan = invalidate_after_plan
    chat = Chat(store, social)
    chat.respond = AsyncMock(side_effect=AssertionError('stale plan must not generate a reply'))
    bridge = OneBotBridge(store, settings, chat)
    bridge.send = AsyncMock(return_value=True)
    await bridge._flow.handle('qq:group:202', '303', '初音未来，聊聊音乐', [], {})
    assert not chat.respond.called and not bridge.send.called
    await bridge._flow.close()


class Chat:
    def __init__(self, store, social):
        self.store, self.social = store, social
        self.error = None
        self.reply = '这是一段模拟回复。'

    async def observe(self, scope_id, user_id, text, images=None, metadata=None):
        return self.store.add_message(scope_id, user_id, 'user', text, **metadata)

    async def respond(self, scope_id, user_id, text, **kwargs):
        if self.error:
            raise self.error
        record = self.store.add_message(scope_id, 'assistant', 'assistant', self.reply,
                                       source_ids=kwargs['metadata']['source_ids'])
        return {'reply': self.reply, 'message_id': record['id']}


@pytest.mark.anyio
@pytest.mark.parametrize('kind', ['planner', 'budget', 'queue', 'protected'])
async def test_direct_qq_failure_has_one_safe_feedback_without_model_fallback(setup, kind):
    store, settings, planner, social = setup
    chat = Chat(store, social)
    bridge = OneBotBridge(store, settings, chat)
    bridge.send = AsyncMock(return_value=True)
    if kind == 'planner':
        planner.error = RuntimeError('https://secret.invalid private-api-key')
    else:
        store.upsert_scope({'id': 'qq:group:202', 'plan_enabled': False, 'trigger_name': True})
        if kind == 'budget':
            chat.error = ProviderError('Token 预算不足 private-api-key')
        elif kind == 'queue':
            bridge._flow._scope_jobs['qq:group:202'] = 8
        else:
            settings.config['output']['platform_max_chars'] = 80
            chat.reply = '```python\n' + 'print(1)\n' * 30 + '```'
    await bridge._flow.handle('qq:group:202', '303', '初音未来，你好', [], {})
    assert bridge.send.await_count == 1
    sent = bridge.send.call_args.args[1]
    assert len(sent) < 100 and 'private-api-key' not in sent and 'secret.invalid' not in sent
    assert bridge.send.call_args.kwargs['metadata']['failure_feedback']
    if kind == 'queue':
        assert not planner.calls
    if kind == 'protected':
        assert any(x['content'] == chat.reply for x in store.list_messages('qq:group:202'))
    await bridge._flow.close()


@pytest.mark.anyio
async def test_private_always_reply_model_failure_has_feedback_and_no_planner_cost(setup):
    store, settings, planner, social = setup
    store.upsert_scope({'id': 'qq:private:101', 'always_reply': True})
    chat = Chat(store, social)
    chat.error = RuntimeError('connection broken')
    bridge = OneBotBridge(store, settings, chat)
    bridge.send = AsyncMock(return_value=True)
    await bridge._flow.handle('qq:private:101', '101', '你好', [], {}, direct=True)
    assert bridge.send.await_count == 1 and not planner.calls
    await bridge._flow.close()


@pytest.mark.anyio
async def test_unsolicited_failure_or_revoked_request_never_pushes_error_to_group(setup):
    store, settings, planner, social = setup
    planner.error = RuntimeError('broken')
    chat = Chat(store, social)
    bridge = OneBotBridge(store, settings, chat)
    bridge.send = AsyncMock(return_value=True)
    await bridge._flow.handle('qq:group:202', '303', '分享电子音乐', [], {})
    assert not bridge.send.called
    async def revoked(*args, **kwargs):
        settings.config['onebot']['allowed_groups'] = []
        raise RuntimeError('broken')
    planner.complete = revoked
    await bridge._flow.handle('qq:group:202', '303', '初音未来，你好', [], {})
    assert not bridge.send.called
    await bridge._flow.close()


def test_hard_transport_cap_wins_over_disabled_soft_split_and_max_parts():
    text = '音乐。' * 1000
    cfg = {'output': {'enabled': False, 'platform_max_chars': 80, 'max_parts': 1}}
    parts = split_output(text, cfg, {'id': 'qq:group:202'})
    assert ''.join(parts) == text and len(parts) > 1 and max(map(len, parts)) <= 80
    cfg['onebot'] = {'max_message_chars': 120}
    assert max(map(len, split_output(text, cfg, {'id': 'qq:group:202'}))) == 120
    assert max(map(len, split_output(text, cfg, {'id': 'qqofficial:group:gid'}))) == 80
    with pytest.raises(OutputLimitError, match='原文已保留'):
        split_output('https://example.test/' + 'a' * 200, cfg, {'id': 'qq:group:202'})


@pytest.mark.anyio
@pytest.mark.parametrize('official', [False, True])
async def test_adapter_enforces_hard_limit_for_direct_reminders_without_flow(setup, official):
    store, settings, _, social = setup
    settings.config['output']['platform_max_chars'] = 80
    chat = Chat(store, social)
    if official:
        settings.config['onebot']['adapter'] = 'official'
        settings.config['qq_official'].update(chat_policy="whitelist", enabled=True, allowed_groups=['gid'])
        bridge = OfficialBridge(store, settings, chat)
        bridge._running = bridge._connected = True
        api = SimpleNamespace(post_group_message=AsyncMock(return_value={'id': 'sent'}))
        bridge._client = SimpleNamespace(api=api)
        assert await bridge._send('qqofficial:group:gid', '文字' * 100, msg_id='source')
        calls = api.post_group_message.call_args_list
        assert max(len(x.kwargs['content']) for x in calls) <= 80
        assert [x.kwargs['msg_seq'] for x in calls] == [1, 2, 3]
    else:
        bridge = OneBotBridge(store, settings, chat)
        sent = []
        class Socket:
            async def send_json(self, request):
                sent.append(request)
                bridge._pending[request['echo']][1].set_result({'status': 'ok', 'retcode': 0, 'data': {'message_id': len(sent)}})
        bridge._socket = Socket()
        assert await bridge.send('qq:group:202', '文字' * 100)
        assert max(len(x['params']['message'][0]['data']['text']) for x in sent) <= 80
        assert ''.join(x['params']['message'][0]['data']['text'] for x in sent) == '文字' * 100


@pytest.mark.anyio
async def test_foreground_overtakes_queued_background_on_same_provider(setup, monkeypatch):
    store, settings, _, _ = setup
    provider = Provider(settings, store)
    entered, release = asyncio.Event(), asyncio.Event()
    dispatches = []
    original = httpx.AsyncClient
    async def handler(request):
        text = json.loads(request.content)['messages'][0]['content']
        dispatches.append(text)
        if text == 'background-first':
            entered.set()
            await release.wait()
        return httpx.Response(200, json={'choices': [{'message': {'content': 'ok'}}],
                                       'usage': {'prompt_tokens': 5, 'completion_tokens': 5}})
    monkeypatch.setattr(httpx, 'AsyncClient', lambda *a, **kw: original(*a, transport=httpx.MockTransport(handler), **kw))
    first = asyncio.create_task(provider.complete([{'role': 'user', 'content': 'background-first'}], purpose='knowledge'))
    await entered.wait()
    second = asyncio.create_task(provider.complete([{'role': 'user', 'content': 'background-second'}], purpose='memory_extract'))
    await asyncio.sleep(0)
    foreground = asyncio.create_task(provider.complete([{'role': 'user', 'content': 'foreground'}]))
    await asyncio.sleep(0)
    assert provider.status()['scheduler']['foreground_queued'] == 1
    release.set()
    await asyncio.gather(first, second, foreground)
    assert dispatches == ['background-first', 'foreground', 'background-second']
    assert provider.status()['reserved']['requests'] == 0


@pytest.mark.anyio
async def test_planner_quota_counts_actual_daily_usage_and_pending_reservations(setup):
    store, settings, _, _ = setup
    settings.config['planner'] = {'min_interval_ms': 0, 'daily_calls': 1, 'daily_tokens': 100, 'max_age_ms': 15000}
    provider = Provider(settings, store)
    cfg = provider.resolve('planner')
    key, _ = await provider._reserve(cfg, 'qq:group:202', 'planner', 10, 10)
    with pytest.raises(ProviderError, match='次数'):
        await provider._reserve(cfg, 'qq:group:202', 'planner', 10, 10)
    await provider._settle(key, {'scope_id': 'qq:group:202', 'purpose': 'planner', 'model': 'mock',
                               'input_tokens': 5, 'output_tokens': 5, 'cost': 0})
    assert provider.planner_totals('qq:group:202') == {'calls': 1, 'tokens': 10}
    with pytest.raises(ProviderError, match='次数'):
        await provider._reserve(cfg, 'qq:group:202', 'planner', 10, 10)
    settings.config['planner']['daily_calls'] = 0
    settings.config['planner']['daily_tokens'] = 15
    with pytest.raises(ProviderError, match='Token'):
        await provider._reserve(cfg, 'qq:group:202', 'planner', 10, 10)
    # Another isolated scope has its own Planner allowance, while global budget still applies.
    other, _ = await provider._reserve(cfg, 'qq:private:101', 'planner', 5, 5)
    await provider._settle(other)
