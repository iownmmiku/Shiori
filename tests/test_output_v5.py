"""Explicit legacy-length compatibility; default model mode has separate tests."""
import asyncio
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from qunyu.db import Store
from shiori.chat import ChatService
from shiori.official import OfficialBridge
from shiori.onebot import OneBotBridge
from shiori.output import split_output as configured_split_output
from shiori.settings import DEFAULTS
from shiori.social import SocialEngine


def split_output(text, config=None, scope=None):
    config = deepcopy(config or {})
    config.setdefault('output', {}).setdefault('mode', 'length')
    return configured_split_output(text, config, scope)


@pytest.fixture
def anyio_backend():
    return 'asyncio'


@pytest.mark.parametrize('text', ['嗯', '好呀。', '只有一句就足够', '这有好几句。也不一定拆开。可以很自然。'])
def test_short_output_stays_one_message(text):
    assert split_output(text) == [text]


def test_output_length_preserves_all_text_and_has_variable_sentence_counts():
    text = ('这句话有很多字但不按一句一段去拆分。短句。好呀。' * 30) + '\n尾部'
    parts = split_output(text)
    assert ''.join(parts) == text
    assert len(parts) > 1 and any(part.count('。') > 1 for part in parts)
    assert len(parts) <= 8


@pytest.mark.parametrize('atom', ['https://example.test/' + 'a' * 500,
    '```python\n' + 'print("test")\n' * 50 + '```', '[很长的链接](https://example.test/' + 'a' * 500 + ')',
    '`' + 'some_call()' * 50 + '`'])
def test_protected_long_text_never_splits_in_middle(atom):
    text = '开头。' * 60 + atom + '\n结尾。' * 60
    parts = split_output(text, {'output': {'target_chars': 80, 'max_chars': 120, 'max_parts': 30}})
    assert ''.join(parts) == text
    assert any(atom in part for part in parts)


def test_unicode_clusters_and_flags_are_not_broken():
    cluster = '👩🏽\u200d💻'
    flag = '🇨🇳'
    accent = 'e\u0301'
    text = (cluster + flag + accent) * 50
    parts = split_output(text, {'output': {'target_chars': 23, 'max_chars': 29}})
    assert ''.join(parts) == text
    for part in parts:
        assert not part.startswith(('🏽', '\u200d', '\u0301', '🇳'))
        assert not part.endswith(('👩', '🏽', '\u200d', '🇨', 'e'))


def test_max_parts_merges_tail_without_dropping_content_and_disabled_stays_whole():
    text = '第一句。第二句很长。第三句。' * 100
    parts = split_output(text, {'output': {'target_chars': 20, 'max_chars': 30, 'max_parts': 3}})
    assert len(parts) == 3 and ''.join(parts) == text and len(parts[-1]) > 30
    assert split_output(text, {'output': {'enabled': False}}) == [text]
    assert split_output(text, {}, {'output_enabled': False}) == [text]


def test_scope_output_override_inherits_unset_global_fields():
    text = '第一句。第二句很长。第三句。' * 20
    global_config = {'output': {'target_chars': 80, 'max_chars': 120, 'max_parts': 8}}
    assert len(split_output(text, global_config, {'output': {'target_chars': 20, 'max_chars': 40, 'max_parts': 2}})) == 2
    assert split_output(text, global_config, {'output': {'enabled': False}}) == [text]
    assert split_output(text, global_config, {'output': {'target_chars': None}}) == split_output(text, global_config)


class Settings:
    def __init__(self):
        self.config = deepcopy(DEFAULTS)
        self.config['onebot'].update(chat_policy="whitelist", enabled=True, owner_ids=['101'], allowed_groups=['202'])
        self.config['output'].update(mode='length', target_chars=30, max_chars=40, interval_ms=0)

    def get(self):
        return deepcopy(self.config)


class Chat:
    def __init__(self, store, settings):
        self.store, self.social = store, SocialEngine(store, settings, None)

    async def observe(self, scope_id, user_id, text, images=None, metadata=None):
        return self.store.add_message(scope_id, user_id, 'user', text, **metadata)

    async def respond(self, *args, **kwargs):
        return {'reply': '第一段的内容。' * 6 + '第二段还有详细说明。' * 8, 'message_id': 'response'}


@pytest.fixture
def bridge_setup(tmp_path):
    store, settings = Store(tmp_path), Settings()
    store.upsert_scope({'id': 'qq:group:202', 'plan_enabled': False, 'merge_window_ms': 0})
    chat = Chat(store, settings)
    bridge = OneBotBridge(store, settings, chat)
    yield store, settings, chat, bridge
    store.close()


@pytest.mark.anyio
async def test_unknown_send_stops_remaining_parts_without_retry(bridge_setup):
    store, _, _, bridge = bridge_setup
    bridge.send = AsyncMock(side_effect=[True, False])
    await bridge._flow.handle('qq:group:202', '303', '初音未来你好', [], {}, direct=False)
    assert bridge.send.await_count == 2
    first, second = bridge.send.call_args_list
    assert first.kwargs['metadata']['output_part']['index'] == 1
    assert second.kwargs['metadata']['output_part']['index'] == 2
    events = store.list_events()
    assert any('剩余分段已停止' in x['message'] for x in events)


@pytest.mark.anyio
@pytest.mark.parametrize('change', ['permission', 'stop', 'source'])
async def test_between_parts_new_permission_or_source_change_stops_unsent_parts(bridge_setup, change):
    store, settings, chat, bridge = bridge_setup
    async def send(scope_id, text, **kwargs):
        if change == 'permission':
            settings.config['onebot']['allowed_groups'] = []
        elif change == 'stop':
            chat.social._generations[(scope_id, '303')] += 1
        else:
            store.clear_messages(scope_id)
        return True
    bridge.send = AsyncMock(side_effect=send)
    await bridge._flow.handle('qq:group:202', '303', '初音未来你好', [], {}, direct=False)
    assert bridge.send.await_count == 1


@pytest.mark.anyio
async def test_all_confirmed_parts_are_identifiable_as_bot_replies(bridge_setup):
    store, _, _, bridge = bridge_setup
    counter = 0
    class Socket:
        async def send_json(self, request):
            nonlocal counter
            counter += 1
            bridge._pending[request['echo']][1].set_result({'status': 'ok', 'retcode': 0, 'data': {'message_id': counter}})
    bridge._socket = Socket()
    await bridge._flow.handle('qq:group:202', '303', '初音未来你好', [], {})
    assert counter >= 2
    for index in range(1, counter + 1):
        assert bridge._reply_context('qq:group:202', str(index))['reply_to_bot']
    sent = [event for event in store.list_events() if event['kind'] == 'qq_sent']
    assert {event['details']['output_part']['index'] for event in sent} == set(range(1, counter + 1))


@pytest.mark.anyio
async def test_official_parts_use_unique_passive_sequence_and_store_receipts(tmp_path):
    store, settings = Store(tmp_path), Settings()
    settings.config['onebot']['adapter'] = 'official'
    settings.config['qq_official'].update(chat_policy="whitelist", enabled=True, allowed_groups=['gid'])
    store.upsert_scope({'id': 'qqofficial:group:gid', 'plan_enabled': False})
    chat = Chat(store, settings)
    bridge = OfficialBridge(store, settings, chat)
    bridge._running = bridge._connected = True
    counter = 0
    async def post(**kwargs):
        nonlocal counter
        counter += 1
        return {'id': str(counter)}
    api = SimpleNamespace(post_group_message=AsyncMock(side_effect=post))
    bridge._client = SimpleNamespace(api=api)
    await bridge._flow.handle('qqofficial:group:gid', 'uid', '初音未来你好', [], {'platform_message_id': 'source'})
    assert counter >= 2
    assert [x.kwargs['msg_seq'] for x in api.post_group_message.call_args_list] == list(range(1, counter + 1))
    assert all(x.kwargs['msg_id'] == 'source' for x in api.post_group_message.call_args_list)
    assert all(bridge._outgoing.get(str(index)) == 'qqofficial:group:gid' for index in range(1, counter + 1))
    # Restarting the connector retains proof for every segment of the same app.
    settings.config['qq_official']['app_id'] = 'same-app'
    store.add_event('qq_official_sent', '已确认', scope_id='qqofficial:group:gid', platform_message_id='previous-segment', official_app_id='same-app')
    restarted = OfficialBridge(store, settings, chat)
    restarted._running = restarted._connected = True
    restarted._client = SimpleNamespace(api=api)
    await restarted._handle_message('group', {'id': 'followup', 'group_openid': 'gid',
        'author': {'member_openid': 'uid'}, 'content': '再说一下',
        'message_reference': {'message_id': 'previous-segment'}}, direct=False)
    assert counter > len(bridge._outgoing)
    before = counter
    settings.config['qq_official']['app_id'] = 'different-app'
    other_app = OfficialBridge(store, settings, chat)
    other_app._running = other_app._connected = True
    other_app._client = SimpleNamespace(api=api)
    await other_app._handle_message('group', {'id': 'not-same-app', 'group_openid': 'gid',
        'author': {'member_openid': 'uid'}, 'content': '再说一下',
        'message_reference': {'message_id': 'previous-segment'}}, direct=False)
    assert counter == before
    store.close()


@pytest.mark.anyio
async def test_actual_recalled_memory_deleted_after_first_part_stops_remaining_output(tmp_path):
    store, settings = Store(tmp_path), Settings()
    store.upsert_scope({'id': 'qq:group:202', 'plan_enabled': False, 'merge_window_ms': 0})
    item = store.create_memory({'scope_id': 'qq:group:202', 'user_id': '303', 'kind': 'note',
                                'content': '这是用户保存的详细记忆内容。' * 20, 'status': 'active'})
    chat = ChatService(store, settings, None)
    bridge = OneBotBridge(store, settings, chat)
    async def send(scope_id, text, **kwargs):
        store.delete_memory(item['id'])
        return True
    bridge.send = AsyncMock(side_effect=send)
    await bridge._flow.handle('qq:group:202', '303', '我的记忆', [], {'at_bot': True}, direct=True)
    assert bridge.send.await_count == 1
    assert any('停止剩余分段' in x['message'] for x in store.list_events())
    await chat.close()
    store.close()


@pytest.mark.anyio
async def test_actual_jargon_deleted_after_first_part_stops_remaining_output(tmp_path):
    store, settings = Store(tmp_path), Settings()
    store.upsert_scope({'id': 'qq:group:202', 'plan_enabled': False, 'merge_window_ms': 0})
    item = store.create_jargon({'scope_id': 'qq:group:202', 'term': '开饭',
                                'meaning': '这个词在群内表示准备开始游戏活动。' * 15, 'status': 'approved'})
    chat = ChatService(store, settings, None)
    bridge = OneBotBridge(store, settings, chat)
    async def send(scope_id, text, **kwargs):
        store.delete_jargon(item['id'])
        return True
    bridge.send = AsyncMock(side_effect=send)
    await bridge._flow.handle('qq:group:202', '303', '解释黑话：开饭', [], {'at_bot': True}, direct=True)
    assert bridge.send.await_count == 1
    assert any('停止剩余分段' in x['message'] for x in store.list_events())
    await chat.close()
    store.close()


@pytest.mark.anyio
async def test_each_output_part_checks_knowledge_fingerprint_and_links_logical_reply(bridge_setup):
    store, _, chat, bridge = bridge_setup
    changed = False
    async def respond(scope_id, user_id, text, **kwargs):
        response = store.add_message(scope_id, 'assistant', 'assistant', '很长的知识回答。' * 40,
                                     source_ids=kwargs['metadata']['source_ids'], knowledge_refs={'entry': 'hash'}, persona_id='miku')
        return {'reply': response['content'], 'message_id': response['id']}
    chat.respond = respond
    chat._knowledge_changed = lambda persona, refs: changed if persona == 'miku' and refs == {'entry': 'hash'} else False
    async def send(scope_id, text, **kwargs):
        nonlocal changed
        changed = True
        return True
    bridge.send = AsyncMock(side_effect=send)
    await bridge._flow.handle('qq:group:202', '303', '初音未来你好', [], {})
    assert bridge.send.await_count == 1
    logical_id = bridge.send.call_args.kwargs['metadata']['assistant_message_id']
    assert store.get_message(logical_id)['role'] == 'assistant'


@pytest.mark.anyio
async def test_actual_persona_knowledge_deleted_after_first_part_stops_remaining_output(tmp_path):
    from datetime import datetime, timedelta, timezone
    store, settings = Store(tmp_path), Settings()
    settings.config['model'].update(base_url='https://test.invalid/v1', model='fake')
    store.upsert_scope({'id': 'qq:group:202', 'plan_enabled': False, 'merge_window_ms': 0})
    class Provider:
        async def complete(self, messages, **kwargs):
            assert '用于测试的音乐知识' in messages[1]['content']
            reply = '这是根据音乐知识整理的回答。' * 30
            return {'content': reply, 'message': {'role': 'assistant', 'content': reply}, 'tool_calls': [], 'usage': {}}
    chat = ChatService(store, settings, Provider())
    entry = {'id': 'knowledge-one', 'persona_id': 'default', 'status': 'active', 'interest': '音乐',
             'title': '音乐资料', 'text': '用于测试的音乐知识', 'url': 'https://example.test/topic',
             'retrieved_at': datetime.now(timezone.utc).isoformat(),
             'valid_until': (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()}
    chat.knowledge._state['items'].append(entry)
    bridge = OneBotBridge(store, settings, chat)
    async def send(scope_id, text, **kwargs):
        chat.knowledge.delete(entry['id'])
        return True
    bridge.send = AsyncMock(side_effect=send)
    await bridge._flow.handle('qq:group:202', '303', '音乐资料介绍一下', [], {'at_bot': True}, direct=True)
    assert bridge.send.await_count == 1
    assert any('停止剩余分段' in x['message'] for x in store.list_events())
    await chat.close()
    store.close()


@pytest.mark.anyio
async def test_concurrent_proactive_plans_recheck_delivery_cooldown_without_interleaving(bridge_setup):
    import json
    store, settings, chat, bridge = bridge_setup
    settings.config['model'].update(base_url='https://test.invalid/v1', model='fake')
    store.upsert_scope({'id': 'qq:group:202', 'plan_enabled': True, 'cooldown_seconds': 60})
    class Planner:
        async def complete(self, messages, **kwargs):
            payload = json.loads(messages[-1]['content'])
            return {'content': json.dumps({'action': 'reply', 'target_message_id': payload['current_message_id'],
                'target_user_id': payload['current_user_id'], 'addressed_to_bot': False, 'intent': '回应有价值的分享',
                'reason': '当前分享尚无人回应', 'needed_info': []})}
    chat.social.provider = Planner()
    entered, release = asyncio.Event(), asyncio.Event()
    sent = []
    async def send(scope_id, text, **kwargs):
        sent.append(kwargs['metadata']['source_ids'])
        if len(sent) == 1:
            entered.set()
            await release.wait()
        return True
    bridge.send = AsyncMock(side_effect=send)
    first = asyncio.create_task(bridge._flow.handle('qq:group:202', '303', '分享最近重听的音乐', [], {}))
    await entered.wait()
    second = asyncio.create_task(bridge._flow.handle('qq:group:202', '404', '分享另一张音乐专辑', [], {}))
    await asyncio.sleep(.005)
    release.set()
    await asyncio.gather(first, second)
    assert len(sent) >= 2 and len({tuple(ids) for ids in sent}) == 1
    assert len(store.list_messages('qq:group:202')) == 2
