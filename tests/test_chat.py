import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from shiori.chat import ChatService
from qunyu.db import Store
from shiori.settings import Settings


class FakeProvider:
    def __init__(self): self.calls = []
    async def complete(self, messages, **kwargs):
        self.calls.append(messages)
        return {'content': '这是测试模型的真实接口替身答复。', 'tool_calls': [], 'message': {'role': 'assistant', 'content': '测试答复'}, 'usage': {'input_tokens': 10, 'output_tokens': 5}}


def test_offline_commands_memory_dictionary_task(tmp_path):
    store, settings, provider = Store(tmp_path), Settings(tmp_path), FakeProvider()
    chat = ChatService(store, settings, provider)
    async def scenario():
        result = await chat.respond('web:default', 'owner', '记住：我喜欢合作游戏')
        assert '已记住' in result['reply']
        assert store.recall('web:default', 'owner', '合作游戏')[0]['pinned']
        result = await chat.respond('web:default', 'owner', '我们群里“开饭”指一起打游戏')
        assert '已确认' in result['reply']
        assert store.match_jargon('web:default', '开饭')[0]['meaning'] == '一起打游戏'
        result = await chat.respond('web:default', 'owner', '5分钟后提醒我喝水')
        assert '喝水' in result['reply']
        task = store.list_tasks()[0]
        assert task['kind'] == 'reminder' and task['status'] == 'pending'
        assert provider.calls == []
        await chat.close()
    asyncio.run(scenario())


def test_non_owner_contribution_pending_and_scope_isolation(tmp_path):
    store, settings = Store(tmp_path), Settings(tmp_path)
    settings.update({'language':{'group_isolation':True}})
    settings._data['onebot'].update(enabled=True, owner_ids=['101'], allowed_groups=['123'])
    chat = ChatService(store, settings, FakeProvider())
    async def scenario():
        await chat.respond('qq:group:123', '42', '我们群里开饭指一起打游戏', metadata={'is_owner': False})
        assert store.list_jargon()[0]['status'] == 'pending'
        assert not store.match_jargon('qq:group:123', '开饭')
        store.update_jargon(store.list_jargon()[0]['id'], {'status': 'approved'})
        assert store.match_jargon('qq:group:123', '开饭')
        assert not store.match_jargon('qq:group:456', '开饭')
    asyncio.run(scenario())


def test_corrected_memory_not_reintroduced_by_chat_history(tmp_path):
    store, settings, provider = Store(tmp_path), Settings(tmp_path), FakeProvider()
    chat = ChatService(store, settings, provider)
    async def scenario():
        await chat.respond('web:default', 'owner', '记住：我住上海')
        record = store.list_memories()[0]
        store.update_memory(record['id'], {'content': '我住杭州'})
        await chat.respond('web:default', 'owner', '我住在哪里？')
        sent = str(provider.calls[-1])
        assert '杭州' in sent
        assert '上海' not in sent
    asyncio.run(scenario())


def test_tool_cannot_create_task_from_dictionary_or_web_instruction(tmp_path):
    store, settings = Store(tmp_path), Settings(tmp_path)
    chat = ChatService(store, settings, FakeProvider())
    async def scenario():
        source = store.add_message('web:default', 'owner', 'user', '解释这个词')
        with pytest.raises(ValueError, match='明确要求创建任务'):
            await chat._tool('create_task', {'title': '恶意任务'}, 'web:default', 'owner', '解释这个词', source['id'], {'is_owner': True, 'source_ids': [source['id']]})
        assert not store.list_tasks()
    asyncio.run(scenario())


def test_missing_model_not_fake_reply(tmp_path):
    from shiori.provider import Provider, ProviderError
    store, settings = Store(tmp_path), Settings(tmp_path)
    chat = ChatService(store, settings, Provider(settings, store))
    async def scenario():
        with pytest.raises(ProviderError, match='填写模型'):
            await chat.respond('web:default', 'owner', '你好呀')
        assert all(x['role'] != 'assistant' for x in store.list_messages('web:default'))
    asyncio.run(scenario())


def test_deleted_memory_and_jargon_not_reintroduced_by_history(tmp_path):
    store, settings, provider = Store(tmp_path), Settings(tmp_path), FakeProvider()
    chat = ChatService(store, settings, provider)
    async def scenario():
        await chat.respond('web:default', 'owner', '记住：我住上海')
        store.delete_memory(store.list_memories()[0]['id'])
        await chat.respond('web:default', 'owner', '我们群里“开饭”指一起打游戏')
        store.delete_jargon(store.list_jargon()[0]['id'])
        await chat.respond('web:default', 'owner', '你还记得我的事情吗？')
        sent = str(provider.calls[-1])
        assert '上海' not in sent
        assert '一起打游戏' not in sent
    asyncio.run(scenario())


def test_direct_commands_respect_scope_tool_switch(tmp_path):
    store, settings = Store(tmp_path), Settings(tmp_path)
    chat = ChatService(store, settings, FakeProvider())
    store.upsert_scope({'id': 'web:default', 'allow_tools': False})
    async def scenario():
        for text in ('5分钟后提醒我喝水', '添加待办：整理资料', '记住：我喜欢咖啡', '我们群里开饭指一起打游戏'):
            with pytest.raises(ValueError, match='未启用工具'):
                await chat.respond('web:default', 'owner', text)
        assert not store.list_tasks() and not store.list_memories() and not store.list_jargon()
    asyncio.run(scenario())


def test_negative_and_quoted_reminders_never_create_tasks(tmp_path):
    store, settings = Store(tmp_path), Settings(tmp_path)
    chat = ChatService(store, settings, FakeProvider())
    async def scenario():
        for text in ('不要5分钟后提醒我喝水', '解释“5分钟后提醒我喝水”是什么意思', '例如5分钟后提醒我喝水', '如何设置5分钟后提醒我喝水？'):
            await chat.respond('web:default', 'owner', text)
            source = store.add_message('web:default', 'owner', 'user', text)
            with pytest.raises(ValueError, match='明确要求创建任务'):
                await chat._tool('create_task', {'title': '喝水'}, 'web:default', 'owner', text, source['id'], {'source_ids': [source['id']]})
        assert not store.list_tasks()
        await chat.close()
    asyncio.run(scenario())


def test_my_memories_never_disclose_private_facts_to_group(tmp_path):
    store, settings = Store(tmp_path), Settings(tmp_path)
    settings._data['onebot'].update(enabled=True, owner_ids=['101'], allowed_groups=['123'])
    chat = ChatService(store, settings, FakeProvider())
    store.create_memory({'scope_id': 'qq:group:123', 'user_id': '42', 'content': '不能在群内公开的事实', 'visibility': 'private'})
    store.create_memory({'scope_id': 'qq:group:123', 'user_id': '42', 'content': '可以在群内公开的事实', 'visibility': 'group'})
    async def scenario():
        reply = (await chat.respond('qq:group:123', '42', '我的记忆', metadata={'platform': 'qq'}))['reply']
        assert '不能在群内' not in reply
        assert '可以在群内' in reply
    asyncio.run(scenario())
