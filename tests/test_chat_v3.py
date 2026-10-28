"""Connector planning generations also guard real ChatService tool execution."""
import asyncio
from copy import deepcopy
import json
from unittest.mock import AsyncMock

import pytest

from shiori.chat import ChatService
from qunyu.db import Store
from shiori.settings import DEFAULTS


@pytest.fixture
def anyio_backend():
    return 'asyncio'


class Settings:
    def __init__(self):
        self.config = deepcopy(DEFAULTS)
        self.config['onebot'].update(enabled=True, adapter='napcat', owner_ids=['101'], allowed_groups=['202'])

    def get(self):
        return deepcopy(self.config)


def response(content='', calls=None):
    message = {'role': 'assistant', 'content': content}
    if calls:
        message['tool_calls'] = calls
    return {'content': content, 'tool_calls': calls or [], 'message': message, 'usage': {'input_tokens': 1, 'output_tokens': 1}}


def create_task():
    return {'id': 'create-1', 'type': 'function', 'function': {'name': 'create_task',
        'arguments': json.dumps({'title': '整理书桌'}, ensure_ascii=False)}}


class Provider:
    def __init__(self, block_at):
        self.block_at, self.calls = block_at, 0
        self.entered, self.release = asyncio.Event(), asyncio.Event()

    async def complete(self, messages, **kwargs):
        self.calls += 1
        if self.calls == self.block_at:
            self.entered.set()
            await self.release.wait()
        return response(calls=[create_task()]) if self.calls == 1 else response('旧请求的结果')


@pytest.mark.anyio
@pytest.mark.parametrize('interrupt', [False, True])
@pytest.mark.parametrize('block_at', [1, 2])
async def test_new_request_blocks_unstarted_tools_and_preserves_completed_actions(tmp_path, monkeypatch, interrupt, block_at):
    store, settings, provider = Store(tmp_path), Settings(), Provider(block_at)
    scope_id, user_id = 'qq:group:202', '101'
    scope = store.upsert_scope({'id': scope_id, 'auto_memory': False, 'allow_tools': True})
    chat = ChatService(store, settings, provider)
    # Exercise model tool orchestration rather than deterministic direct commands.
    monkeypatch.setattr(chat, '_direct', AsyncMock(return_value=None))
    first = await chat.observe(scope_id, user_id, '请创建待办整理书桌，并分析如何完成', metadata={'is_owner': True})
    plan = await chat.social.plan(scope, first, direct=True)
    metadata = {'is_owner': True, 'existing_message_id': first['id'], 'merged_message_ids': [first['id']],
                'source_ids': [first['id']], 'request_generation': plan['generation']}
    pending = asyncio.create_task(chat.respond(scope_id, user_id, first['content'], metadata=metadata))
    try:
        await asyncio.wait_for(provider.entered.wait(), 2)
        assert len(store.list_tasks(scope_id=scope_id)) == (1 if block_at == 2 else 0)
        new = await chat.observe(scope_id, user_id, '不用回答了' if interrupt else '群屿，改问火锅菜单', metadata={'is_owner': True})
        decision = await chat.social.plan(scope, new, direct=not interrupt)
        assert decision['action'] == ('skip' if interrupt else 'reply')
        assert not chat.social.request_current(scope_id, user_id, plan['generation'])
        provider.release.set()
        result = await pending
        assert result['interrupted'] and result['reply'] == ''
        tasks = store.list_tasks(scope_id=scope_id)
        if block_at == 1:
            assert not tasks and result['actions'] == []
        else:
            assert len(tasks) == 1 and tasks[0]['status'] == 'pending'
            assert len(result['actions']) == 1 and result['actions'][0]['ok']
            assert result['actions'][0]['result']['id'] == tasks[0]['id']
        assert not [x for x in store.list_messages(scope_id) if x['role'] == 'assistant']
    finally:
        provider.release.set()
        if not pending.done():
            await pending
        await chat.close()
        store.close()
