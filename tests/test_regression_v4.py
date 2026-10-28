"""Independent regression coverage for the tool boundary after model awaits."""
import asyncio
from copy import deepcopy
import json
from unittest.mock import AsyncMock

import pytest
import httpx

from shiori.chat import ChatService
from qunyu.db import Store
from shiori.settings import DEFAULTS
from shiori.chat import TOOLS
from shiori.provider import Provider, ProviderError


@pytest.fixture
def anyio_backend():
    return 'asyncio'


class MutableSettings:
    def __init__(self):
        self.config = deepcopy(DEFAULTS)
        self.config['onebot'].update(chat_policy="whitelist", enabled=True, adapter='napcat', owner_ids=['101'], allowed_groups=['202'])
        self.config['qq_official'].update(chat_policy="whitelist", enabled=True, allowed_groups=['303'], allowed_users=['101'])

    def get(self):
        return deepcopy(self.config)


def response(content='', calls=None):
    message = {'role': 'assistant', 'content': content}
    if calls:
        message['tool_calls'] = calls
    return {'content': content, 'tool_calls': calls or [], 'message': message,
            'usage': {'input_tokens': 1, 'output_tokens': 1}}


def task_call(arguments=None):
    return {'id': 'create-1', 'type': 'function', 'function': {'name': 'create_task',
            'arguments': json.dumps(arguments or {'title': '整理书桌'}, ensure_ascii=False)}}


class BlockingProvider:
    def __init__(self, arguments=None):
        self.entered, self.release = asyncio.Event(), asyncio.Event()
        self.calls = 0
        self.arguments = arguments

    async def complete(self, messages, **kwargs):
        self.calls += 1
        if self.calls == 1:
            self.entered.set()
            await self.release.wait()
            return response(calls=[task_call(self.arguments)])
        return response('处理完毕')


@pytest.mark.anyio
@pytest.mark.parametrize('change', ['pause', 'disable_scope', 'tools_off', 'revoke_group',
    'disable_connector', 'switch_adapter', 'clear_request', 'delete_context_memory', 'revoke_official'])
async def test_tool_cannot_start_after_authority_or_source_changes(tmp_path, monkeypatch, change):
    store, settings, provider = Store(tmp_path), MutableSettings(), BlockingProvider()
    scope_id = 'qqofficial:group:303' if change == 'revoke_official' else 'qq:group:202'
    if change == 'revoke_official':
        settings.config['onebot']['adapter'] = 'official'
    store.upsert_scope({'id': scope_id, 'auto_memory': False, 'allow_tools': True})
    memory = store.create_memory({'scope_id': scope_id, 'user_id': '101', 'content': '整理书桌需要分层整理', 'visibility': 'group'})
    chat = ChatService(store, settings, provider)
    monkeypatch.setattr(chat, '_direct', AsyncMock(return_value=None))
    first = await chat.observe(scope_id, '101', '请创建待办整理书桌，并分析如何完成', metadata={'is_owner': True})
    plan = await chat.social.plan(store.get_scope(scope_id), first, direct=True)
    metadata = {'is_owner': True, 'existing_message_id': first['id'], 'merged_message_ids': [first['id']],
                'request_generation': plan['generation']}
    pending = asyncio.create_task(chat.respond(scope_id, '101', first['content'], metadata=metadata))
    try:
        await asyncio.wait_for(provider.entered.wait(), 2)
        if change == 'pause': settings.config['paused'] = True
        elif change == 'disable_scope': store.upsert_scope({'id': scope_id, 'enabled': False})
        elif change == 'tools_off': store.upsert_scope({'id': scope_id, 'allow_tools': False})
        elif change == 'revoke_group': settings.config['onebot']['allowed_groups'] = []
        elif change == 'disable_connector': settings.config['onebot']['enabled'] = False
        elif change == 'switch_adapter': settings.config['onebot']['adapter'] = 'official'
        elif change == 'clear_request': store.clear_messages(scope_id)
        elif change == 'delete_context_memory': store.delete_memory(memory['id'])
        elif change == 'revoke_official': settings.config['qq_official']['allowed_groups'] = []
        provider.release.set()
        try:
            await pending
        except (ValueError, KeyError):
            pass
        assert not store.list_tasks(scope_id=scope_id), f'{change}: a stale or revoked tool produced a side effect'
    finally:
        provider.release.set()
        if not pending.done():
            await pending
        await chat.close()
        store.close()


@pytest.mark.anyio
async def test_provider_tool_parameter_downgrade_cannot_return_executable_calls(tmp_path, monkeypatch):
    """The actual HTTP request without a tool catalogue cannot authorize a call."""
    store, settings = Store(tmp_path), MutableSettings()
    settings.config['model'].update(base_url='https://model.example/v1', model='test-model')
    seen = []

    def handler(request):
        body = json.loads(request.content)
        seen.append(body)
        if len(seen) == 1:
            return httpx.Response(400, json={'error': 'tools parameter unsupported'})
        return httpx.Response(200, json={'choices': [{'message': {
            'role': 'assistant', 'content': '', 'tool_calls': [task_call()]}}],
            'usage': {'prompt_tokens': 1, 'completion_tokens': 1}})

    original_client = httpx.AsyncClient
    monkeypatch.setattr(httpx, 'AsyncClient', lambda *args, **kwargs:
        original_client(*args, transport=httpx.MockTransport(handler), **kwargs))
    provider = Provider(settings, store)
    try:
        with pytest.raises(ProviderError, match='工具'):
            await provider.complete([{'role': 'user', 'content': '创建待办整理书桌'}], tools=TOOLS)
        assert len(seen) == 2 and 'tools' in seen[0] and 'tools' not in seen[1]
    finally:
        store.close()


@pytest.mark.anyio
@pytest.mark.parametrize('arguments', [
    {'title': ['整理书桌']},
    {'title': '整理书桌', 'unexpected': 'model supplied extra property'},
])
async def test_builtin_tool_schema_rejects_wrong_types_and_unknown_fields(tmp_path, monkeypatch, arguments):
    store, settings, provider = Store(tmp_path), MutableSettings(), BlockingProvider(arguments)
    store.upsert_scope({'id': 'web:default', 'auto_memory': False})
    chat = ChatService(store, settings, provider)
    monkeypatch.setattr(chat, '_direct', AsyncMock(return_value=None))
    pending = asyncio.create_task(chat.respond('web:default', 'owner', '请创建待办整理书桌，并分析如何完成'))
    try:
        await asyncio.wait_for(provider.entered.wait(), 2)
        provider.release.set()
        result = await pending
        assert not store.list_tasks(), 'invalid schema was coerced into a task mutation'
        assert result['actions'] and not result['actions'][0]['ok']
    finally:
        provider.release.set()
        if not pending.done():
            await pending
        await chat.close()
        store.close()
