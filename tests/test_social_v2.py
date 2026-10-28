import asyncio
import json
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from qunyu.db import Store
from shiori.onebot import OneBotBridge
from shiori.official import OfficialBridge
from shiori.settings import DEFAULTS
from shiori.social import SocialEngine


@pytest.fixture
def anyio_backend():
    return 'asyncio'


class Settings:
    def __init__(self):
        self.config = deepcopy(DEFAULTS)
        self.config['onebot'].update(chat_policy="whitelist", enabled=True, adapter='napcat', owner_ids=['101'], allowed_groups=['202'])
        self.config['model'].update(base_url='https://mock.invalid/v1', model='test')

    def get(self):
        return deepcopy(self.config)


class Provider:
    def __init__(self, reply=None):
        self.calls = []
        self.reply = reply
        self.entered = asyncio.Event()
        self.blocker = None

    async def complete(self, messages, **kwargs):
        self.calls.append((messages, kwargs))
        self.entered.set()
        if self.blocker:
            await self.blocker.wait()
        if kwargs['purpose'] == 'planner':
            payload = json.loads(messages[-1]['content'])
            choose = self.reply if isinstance(self.reply, dict) else {'reply': False}
            return {'content': json.dumps({'action': 'reply' if choose.get('reply') else 'skip',
                'target_message_id': payload['current_message_id'], 'target_user_id': payload['current_user_id'],
                'addressed_to_bot': False, 'intent': '回答当前问题' if choose.get('reply') else '',
                'reason': '模拟语义判断：当前不需要接话', 'needed_info': []})}
        payload = json.loads(messages[-1]['content'])
        item = next((x for x in payload if x['term'] == 'yyds'), payload[0])
        result = self.reply if self.reply is not None else [{'term': item['term'], 'meaning': '这个群表示非常认可', 'topic': '游戏', 'source_ids': [x['id'] for x in item['examples']], 'status': 'approved', 'use_allowed': True}]
        return {'content': json.dumps(result, ensure_ascii=False)}


class ObservingChat:
    def __init__(self, store, settings, provider):
        self.store, self.settings, self.provider = store, settings, provider
        self.social = SocialEngine(store, settings, provider)
        self.calls = []

    async def observe(self, scope_id, user_id, text, images=None, metadata=None):
        message = self.store.add_message(scope_id, user_id, 'user', text, **dict(metadata or {}, images=images or []))
        self.social.observe(scope_id, message)
        return message

    async def respond(self, scope_id, user_id, text, images=None, metadata=None):
        records = {x['id']: x for x in self.store.list_messages(scope_id, limit=200)}
        ids = metadata['source_ids']
        assert metadata['existing_message_id'] == ids[-1]
        assert all(records[id]['user_id'] == user_id and records[id]['scope_id'] == scope_id for id in ids)
        assert text == '\n'.join(records[id]['content'] for id in ids)
        self.calls.append((scope_id, user_id, text, images, metadata))
        message = self.store.add_message(scope_id, 'assistant', 'assistant', '测试回复', source_ids=ids)
        return {'reply': '测试回复', 'message_id': message['id']}


@pytest.fixture
def setup(tmp_path):
    store, settings, provider = Store(tmp_path), Settings(), Provider()
    store.upsert_scope({'id': 'qq:group:202', 'merge_window_ms': 15, 'cooldown_seconds': 0, 'plan_enabled': False})
    chat = ObservingChat(store, settings, provider)
    bridge = OneBotBridge(store, settings, chat)
    bridge.send = AsyncMock(return_value=True)
    yield store, settings, provider, chat, bridge
    store.close()


def event(id, text, *, user=303, group=202, mention=False, reply=None):
    segments = [{'type': 'text', 'data': {'text': text}}]
    if mention:
        segments.insert(0, {'type': 'at', 'data': {'qq': '999'}})
    if reply:
        segments.insert(0, {'type': 'reply', 'data': {'id': reply}})
    return {'post_type': 'message', 'message_type': 'group', 'self_id': 999, 'message_id': id,
            'group_id': group, 'user_id': user, 'sender': {'card': f'成员{user}'}, 'message': segments}


@pytest.mark.anyio
async def test_allowed_group_observation_is_free_and_never_duplicated(setup):
    store, _, provider, chat, bridge = setup
    await bridge.handle_event(event(1, '今天一起聊天'))
    await bridge.handle_event(event(2, '现在准备吃饭', user=404))
    await bridge.handle_event(event(1, '今天一起聊天'))
    await bridge.handle_event(event(3, '不能读取这个群', group=9999))
    raw = store.list_messages('qq:group:202')
    assert [x['user_id'] for x in raw] == ['303', '404']
    assert not chat.calls and not provider.calls and not bridge.send.called


@pytest.mark.anyio
async def test_names_reply_proof_and_followup_preserve_actor(setup):
    store, _, provider, chat, bridge = setup
    await bridge.handle_event(event(1, '初音，你怎么看'))
    await bridge.handle_event(event(2, '那为什么呢？'))
    await bridge.handle_event(event(3, '继续', user=404))
    bridge._outgoing['confirmed-own'] = 'qq:group:202'
    await bridge.handle_event(event(4, '这里再解释一下', user=404, reply='confirmed-own'))
    await bridge.handle_event(event(5, '别人的问题', reply='unknown-other'))
    assert len(chat.calls) == 2
    assert chat.calls[0][4]['sender_name'] == '成员303'
    assert chat.calls[-1][4]['reply_to_bot'] is True
    assert not provider.calls
    assert len([x for x in store.list_messages('qq:group:202') if x['role'] == 'user']) == 5


@pytest.mark.anyio
async def test_short_messages_merge_only_same_requester_with_real_source_ids(setup):
    store, _, _, chat, bridge = setup
    first = asyncio.create_task(bridge.handle_event(event(1, '初音未来，帮我', user=101)))
    await asyncio.sleep(.002)
    second = asyncio.create_task(bridge.handle_event(event(2, '5分钟后提醒我喝水', user=101)))
    stranger = asyncio.create_task(bridge.handle_event(event(3, '继续', user=303)))
    await asyncio.gather(first, second, stranger)
    assert len(chat.calls) == 1
    call = chat.calls[0]
    assert call[1] == '101' and call[2] == '初音未来，帮我\n5分钟后提醒我喝水'
    assert call[4]['is_owner'] is True and len(call[4]['source_ids']) == 2
    assert len([x for x in store.list_messages('qq:group:202') if x['role'] == 'user']) == 3


@pytest.mark.anyio
async def test_planner_is_explicitly_enabled_and_checks_statements_as_well_as_questions(setup):
    store, _, provider, chat, bridge = setup
    await bridge.handle_event(event(1, '正在路上'))
    assert not provider.calls
    store.upsert_scope({'id': 'qq:group:202', 'plan_enabled': True, 'cooldown_seconds': 0})
    await bridge.handle_event(event(2, '今天发现了一段有趣的旋律'))
    await bridge.handle_event(event(3, '明确向你提问', mention=True))
    assert len(provider.calls) == 2 and all(x[1]['purpose'] == 'planner' for x in provider.calls)
    assert not chat.calls  # An explicit mention can also receive a semantic skip.


def evidence(store, scope='qq:group:202'):
    return [store.add_message(scope, str(300 + i % 2), 'user', f'yyds 这个游戏建议不错 {i}') for i in range(3)]


@pytest.mark.anyio
async def test_learning_disabled_needs_no_provider_and_switches_are_independent(setup):
    store, _, provider, chat, _ = setup
    evidence(store)
    assert (await chat.social.analyze('qq:group:202'))['skipped']
    assert not provider.calls
    store.upsert_scope({'id': 'qq:group:202', 'dictionary_learn': True, 'dictionary_understand': False, 'dictionary_use': True})
    result = await chat.social.analyze('qq:group:202')
    assert len(result['candidates']) == 1
    candidate = result['candidates'][0]
    assert candidate['status'] == 'pending' and candidate['use_allowed'] is False
    assert candidate['evidence']['observations'] == len(candidate['source_ids']) == 3
    assert not chat.social.match_jargon('qq:group:202', 'yyds')
    store.update_jargon(candidate['id'], {'status': 'approved'})
    store.upsert_scope({'id': 'qq:group:202', 'dictionary_learn': False, 'dictionary_understand': True, 'dictionary_use': False})
    assert chat.social.match_jargon('qq:group:202', 'yyds')
    assert len(provider.calls) == 1


@pytest.mark.anyio
@pytest.mark.parametrize('change', ['pause', 'disable', 'stop_learning', 'clear_source'])
async def test_inflight_learning_is_discarded_on_revocation_or_source_clear(setup, change):
    store, settings, provider, chat, _ = setup
    store.upsert_scope({'id': 'qq:group:202', 'dictionary_learn': True})
    evidence(store)
    provider.blocker = asyncio.Event()
    task = asyncio.create_task(chat.social.analyze('qq:group:202'))
    await provider.entered.wait()
    if change == 'pause':
        settings.config['paused'] = True
    elif change == 'disable':
        store.upsert_scope({'id': 'qq:group:202', 'enabled': False})
    elif change == 'stop_learning':
        store.upsert_scope({'id': 'qq:group:202', 'dictionary_learn': False})
    else:
        store.clear_messages('qq:group:202')
    provider.blocker.set()
    await task
    assert not store.list_jargon(scope_id='qq:group:202')


@pytest.mark.anyio
async def test_explicit_explanation_one_source_is_pending_and_foreign_sources_rejected(setup):
    store, _, provider, chat, _ = setup
    store.upsert_scope({'id': 'qq:group:202', 'dictionary_learn': True})
    source = store.add_message('qq:group:202', '303', 'user', '我们群里“开饭”指一起打游戏')
    result = await chat.social.analyze('qq:group:202')
    assert result['candidates'][0]['source_ids'] == [source['id']]
    assert result['candidates'][0]['status'] == 'pending'
    store.delete_jargon(result['candidates'][0]['id'])
    assert not (await chat.social.analyze('qq:group:202'))['candidates']
    assert len(provider.calls) == 1  # deleted explanation cannot be rediscovered


@pytest.mark.anyio
async def test_contextual_multi_meaning_match_keeps_ambiguity_without_model(setup):
    store, _, provider, chat, _ = setup
    for meaning, topic, example in [('一起打游戏', '游戏 开黑', '今晚开饭玩游戏'), ('一起吃饭', '午餐 餐厅', '中午开饭去哪家餐厅')]:
        store.create_jargon({'scope_id': 'qq:group:202', 'term': '开饭', 'meaning': meaning, 'topic': topic, 'example': example, 'status': 'approved'})
    hits = chat.social.match_jargon('qq:group:202', '中午开饭去哪家餐厅')
    assert len(hits) == 2 and hits[0]['meaning'] == '一起吃饭'
    assert hits[0]['context_score'] > hits[1]['context_score']
    assert not chat.social.match_jargon('web:default', '开饭') and not provider.calls


@pytest.mark.anyio
@pytest.mark.parametrize('change', ['gate_off', 'quiet', 'clear'])
async def test_gate_result_cannot_outlive_its_setting_or_source(setup, change):
    store, _, provider, chat, bridge = setup
    provider.reply = {'reply': True}
    provider.blocker = asyncio.Event()
    store.upsert_scope({'id': 'qq:group:202', 'activity': 'active', 'plan_enabled': True})
    task = asyncio.create_task(bridge.handle_event(event(1, '大家觉得这个怎么办？')))
    await provider.entered.wait()
    if change == 'gate_off':
        store.upsert_scope({'id': 'qq:group:202', 'plan_enabled': False})
    elif change == 'quiet':
        store.upsert_scope({'id': 'qq:group:202', 'activity': 'quiet'})
    else:
        store.clear_messages('qq:group:202')
    provider.blocker.set()
    await task
    assert not chat.calls and not bridge.send.called


@pytest.mark.anyio
async def test_group_reply_queue_is_bounded_while_all_raw_messages_remain(setup):
    store, _, _, chat, bridge = setup
    store.upsert_scope({'id': 'qq:group:202', 'merge_window_ms': 100})
    tasks = [asyncio.create_task(bridge.handle_event(event(id, '明确提问', user=300 + id, mention=True), wait=False)) for id in range(15)]
    await asyncio.gather(*tasks)
    assert len([x for x in store.list_messages('qq:group:202') if x['role'] == 'user']) == 15
    assert bridge._flow._scope_jobs['qq:group:202'] == 8
    await bridge._flow.close()
    assert not chat.calls
    assert not bridge._flow._scope_jobs
    store.upsert_scope({'id': 'qq:group:202', 'merge_window_ms': 0})
    await bridge.handle_event(event(100, '重连之后仍可明确提问', mention=True))
    assert len(chat.calls) == 1


@pytest.mark.anyio
async def test_automatic_analysis_runs_per_batch_not_per_message(setup):
    store, _, provider, chat, _ = setup
    store.upsert_scope({'id': 'qq:group:202', 'dictionary_learn': True})
    for index in range(12):
        await chat.observe('qq:group:202', str(300 + index % 2), f'yyds 这波建议很不错 {index}')
    if chat.social._jobs:
        await asyncio.gather(*list(chat.social._jobs))
    assert len(provider.calls) == 1 and len(store.list_jargon(scope_id='qq:group:202', status='pending')) == 1
    for index in range(12, 24):
        await chat.observe('qq:group:202', str(300 + index % 2), f'yyds 还有很多讨论 {index}')
    assert len(provider.calls) == 1


@pytest.mark.anyio
async def test_model_cannot_cite_a_foreign_scope_or_fabricated_source(setup):
    store, _, provider, chat, _ = setup
    store.upsert_scope({'id': 'qq:group:202', 'dictionary_learn': True})
    evidence(store)
    foreign = store.add_message('qq:group:other', '303', 'user', 'yyds is unrelated private data')
    provider.reply = [{'term': 'yyds', 'meaning': '错误跨群来源', 'source_ids': [foreign['id'], 'fabricated-id']}]
    result = await chat.social.analyze('qq:group:202')
    assert result['candidates'] == [] and not store.list_jargon(scope_id='qq:group:202')


@pytest.mark.anyio
async def test_official_nonmention_events_observed_only_with_platform_allowlist(setup):
    store, settings, provider, chat, _ = setup
    settings.config['onebot']['adapter'] = 'official'
    settings.config['qq_official'].update(chat_policy="whitelist", enabled=True, app_id='app', allowed_groups=['gid'], allowed_users=['uid'])
    store.upsert_scope({'id': 'qqofficial:group:gid', 'merge_window_ms': 0, 'plan_enabled': False})
    bridge = OfficialBridge(store, settings, chat)
    bridge._running = bridge._connected = True
    bridge._client = SimpleNamespace(api=SimpleNamespace(post_group_message=AsyncMock(return_value={'id': 'sent'})))
    payload = {'t': 'GROUP_MESSAGE_CREATE', 'd': {'id': 'plain', 'group_openid': 'gid', 'author': {'member_openid': 'member'}, 'content': '普通旁听内容'}}
    await bridge.handle_event(payload)
    assert not chat.calls and len(store.list_messages('qqofficial:group:gid')) == 1
    payload['t'], payload['d']['id'], payload['d']['content'] = 'GROUP_AT_MESSAGE_CREATE', 'at', '<@bot> 帮忙解释一下'
    await bridge.handle_event(payload)
    assert len(chat.calls) == 1 and len([x for x in store.list_messages('qqofficial:group:gid') if x['role'] == 'user']) == 2
    assert not provider.calls
