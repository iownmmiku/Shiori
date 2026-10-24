"""Collected transport messages never expand a real command's arguments."""
import asyncio
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json

import pytest

from mikubot.chat import ChatService
from mikubot.db import Store
from mikubot.provider import ProviderError
from mikubot.settings import DEFAULTS
from mikubot.social import ConversationFlow


class Config:
    def __init__(self):
        self.data = deepcopy(DEFAULTS)
        self.data['model'].update(base_url='https://fixture.invalid/v1', model='fixture', vision=True)
        self.data['onebot'].update(enabled=True, chat_policy='blacklist')
        self.data['debounce'].update(enabled=True, seconds=.1, max_wait_seconds=1)
        self.data['output']['enabled'] = False
        self.data['stickers']['enabled'] = False

    def get(self):
        return deepcopy(self.data)


class Model:
    def __init__(self):
        self.calls = []
        self.error = None
        self.next_tools = []

    async def complete(self, messages, **kwargs):
        self.calls.append(deepcopy(messages))
        assert kwargs['before_send']()
        if self.error:
            raise self.error
        tools, self.next_tools = self.next_tools, []
        return {'content': '' if tools else '今天聊聊音乐吧。', 'tool_calls': tools,
                'message': {'role': 'assistant', 'content': None if tools else '今天聊聊音乐吧。', 'tool_calls': tools}, 'usage': {}}


@pytest.fixture
def anyio_backend():
    return 'asyncio'


@pytest.fixture
def setup(tmp_path):
    store, settings, model = Store(tmp_path), Config(), Model()
    for scope in ('web:default', 'qq:private:303'):
        store.upsert_scope({'id': scope, 'auto_memory': False, 'plan_enabled': False, 'cooldown_seconds': 0})
    chat = ChatService(store, settings, model)
    yield store, settings, model, chat
    store.close()


async def collected(chat, store, texts, *, scope='web:default', user='owner', sent=None):
    originals = [store.add_message(scope, user, 'user', text, sent_at=sent[index] if sent else None)
                 for index, text in enumerate(texts)]
    result = await chat.respond(scope, user, '\n'.join(texts), metadata={'existing_message_id': originals[-1]['id'],
        'merged_message_ids': [item['id'] for item in originals]})
    return result, originals


@pytest.mark.anyio
@pytest.mark.parametrize('texts,expected', [
    (['记住：我喜欢咖啡', '今天聊什么'], '我喜欢咖啡'),
    (['今天聊什么', '记住：我喜欢咖啡'], '我喜欢咖啡'),
])
async def test_memory_command_owns_only_its_original_and_chat_has_one_model_call(setup, texts, expected):
    store, _, model, chat = setup
    try:
        result, originals = await collected(chat, store, texts)
        memories = store.list_memories('web:default')
        assert len(memories) == 1 and memories[0]['content'] == expected
        command = next(item for item in originals if item['content'].startswith('记住'))
        assert memories[0]['source_ids'] == [command['id']]
        assert len(model.calls) == 1 and model.calls[0][-1]['content'] == '今天聊什么'
        references = next(item['content'] for item in model.calls[0] if 'completed_commands' in str(item['content']) and '以下是当前允许' in str(item['content']))
        receipts = json.loads(references.split('\n', 1)[1])['completed_commands']
        assert receipts == [{'source_id': command['id'], 'reply': '已记住：我喜欢咖啡\n可在记忆页纠正或删除。'}]
        assert '已记住：我喜欢咖啡' in result['reply'] and '今天聊聊音乐吧。' in result['reply']
        assert store.get_message(result['message_id'])['metadata']['source_ids'] == [item['id'] for item in originals]
        assert len([item for item in store.list_messages('web:default') if item['role'] == 'assistant']) == 1
    finally:
        await chat.close()


@pytest.mark.anyio
async def test_todo_and_reminder_keep_their_own_text_sources_and_calendar(setup):
    store, _, model, chat = setup
    stamp = datetime.now(timezone.utc) - timedelta(minutes=1)
    try:
        _, originals = await collected(chat, store, ['添加待办：整理照片', '3分钟后提醒我喝水', '今天聊什么'],
            sent=[stamp.isoformat(), stamp.isoformat(), datetime.now(timezone.utc).isoformat()])
        tasks = store.list_tasks()
        assert len(tasks) == 2
        assert {task['title'] for task in tasks} == {'整理照片', '喝水'}
        reminder = next(task for task in tasks if task['kind'] == 'reminder')
        assert abs((datetime.fromisoformat(reminder['due_at']) - stamp - timedelta(minutes=3)).total_seconds()) < .01
        assert reminder['idempotency_key'] == originals[1]['id'] + ':reminder'
        assert len(model.calls) == 1
    finally:
        await chat.close()


@pytest.mark.anyio
async def test_empty_todo_header_accepts_only_an_adjacent_multiline_list(setup):
    store, _, model, chat = setup
    try:
        result, originals = await collected(chat, store, ['添加待办：', '检查资料\n准备发布'])
        assert {item['title'] for item in store.list_tasks()} == {'检查资料', '准备发布'}
        assert len(result['actions']) == 2 and not model.calls
        assert len([item for item in store.list_messages('web:default') if item['role'] == 'user']) == 2
        await chat.respond('web:default', 'owner', '添加待办：\n检查资料\n准备发布', metadata={
            'existing_message_id': originals[-1]['id'], 'merged_message_ids': [item['id'] for item in originals]})
        assert len(store.list_tasks()) == 2
    finally:
        await chat.close()


@pytest.mark.anyio
@pytest.mark.parametrize('following', ['今天聊什么', '你喜欢什么\n今天聊什么', '记住：我喜欢茶\n添加待办：整理照片'])
async def test_empty_todo_header_never_consumes_chat_or_other_commands(setup, following):
    store, _, _, chat = setup
    try:
        result, _ = await collected(chat, store, ['添加待办：', following])
        assert '还没有填好' in result['reply']
        assert not store.list_tasks()
    finally:
        await chat.close()


@pytest.mark.anyio
async def test_empty_todo_list_item_is_rejected_before_any_task_is_written(setup):
    store, _, _, chat = setup
    try:
        result = await chat.respond('web:default', 'owner', '添加待办：\n检查资料\n•')
        assert '本次未添加' in result['reply'] and not store.list_tasks()
    finally:
        await chat.close()


@pytest.mark.anyio
async def test_commands_alone_execute_in_order_without_a_model_or_recursive_lock(setup):
    store, _, model, chat = setup
    try:
        result, _ = await asyncio.wait_for(collected(chat, store, ['我的待办', '添加待办：整理照片', '我的待办']), 2)
        assert result['reply'].startswith('没有未完成的待办。')
        assert result['reply'].index('已添加待办') > result['reply'].index('没有未完成的待办')
        assert result['reply'].endswith('）') and len(store.list_tasks()) == 1
        assert model.calls == []
    finally:
        await chat.close()


@pytest.mark.anyio
async def test_provider_failure_keeps_command_acknowledgement_and_does_not_replay(setup):
    store, _, model, chat = setup
    model.error = ProviderError('fixture unavailable')
    try:
        result, _ = await collected(chat, store, ['添加待办：整理照片', '今天聊什么'])
        assert '已添加待办：整理照片' in result['reply'] and '不需要重复执行' in result['reply']
        assert len(store.list_tasks()) == 1 and len(model.calls) == 1
        assert result['actions'][0]['ok'] is True
    finally:
        await chat.close()


@pytest.mark.anyio
async def test_model_cannot_repeat_completed_command_using_chat_as_new_authority(setup):
    store, _, model, chat = setup
    model.next_tools = [{'id': 'bad', 'type': 'function', 'function': {'name': 'create_task',
        'arguments': json.dumps({'title': '错误重复任务'})}}]
    try:
        result, _ = await collected(chat, store, ['添加待办：整理照片', '今天聊什么'])
        assert [task['title'] for task in store.list_tasks()] == ['整理照片']
        assert len(result['actions']) == 2 and result['actions'][1]['ok'] is False
        assert '明确要求创建任务' in result['actions'][1]['error']
    finally:
        await chat.close()


@pytest.mark.anyio
async def test_remaining_model_tool_receipt_uses_only_its_own_original_source(setup):
    store, _, model, chat = setup
    model.next_tools = [{'id': 'second', 'type': 'function', 'function': {'name': 'remember',
        'arguments': json.dumps({'content': '我也喜欢茶'})}}]
    try:
        _, originals = await collected(chat, store, ['记住：我喜欢咖啡', '也请你记住我喜欢茶'])
        records = {item['content']: item for item in store.list_memories('web:default')}
        assert records['我喜欢咖啡']['source_ids'] == [originals[0]['id']]
        assert records['我也喜欢茶']['source_ids'] == [originals[1]['id']]
        assert len(model.calls) == 2
    finally:
        await chat.close()


@pytest.mark.anyio
async def test_qq_debounce_reuses_the_same_original_command_boundary(setup):
    store, settings, model, chat = setup
    sent = []
    async def emit(scope, text, metadata):
        sent.append(text)
        return True
    flow = ConversationFlow(store, settings, chat, emit, lambda *_: True)
    try:
        for text in ['记住：我喜欢咖啡', '今天聊什么']:
            await flow.handle('qq:private:303', '303', text, [], {}, direct=True, wait=False)
        while flow._jobs:
            await asyncio.wait_for(asyncio.gather(*list(flow._jobs)), 3)
        assert len(model.calls) == 1 and sent
        memories = store.list_memories('qq:private:303')
        assert len(memories) == 1 and memories[0]['content'] == '我喜欢咖啡'
        assert '已记住' in '\n'.join(sent) and '音乐' in '\n'.join(sent)
    finally:
        await flow.close()
        await chat.close()


@pytest.mark.anyio
async def test_forged_or_revoked_sources_cannot_reach_any_command(setup):
    store, _, _, chat = setup
    other = store.add_message('web:other', 'owner', 'user', '记住：不可写入')
    legitimate = store.add_message('web:default', 'owner', 'user', '今天聊什么')
    try:
        with pytest.raises(ValueError, match='同一会话'):
            await chat.respond('web:default', 'owner', '记住：不可写入\n今天聊什么', metadata={
                'existing_message_id': legitimate['id'], 'merged_message_ids': [other['id'], legitimate['id']]})
        assert store.list_memories() == []
        store.clear_messages('web:default')
        with pytest.raises(KeyError):
            await chat.respond('web:default', 'owner', '记住：不可写入\n今天聊什么', metadata={
                'existing_message_id': legitimate['id'], 'merged_message_ids': [other['id'], legitimate['id']]})
    finally:
        await chat.close()
