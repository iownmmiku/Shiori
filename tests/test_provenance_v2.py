"""Regression cases for transitive provenance; providers never perform I/O."""
import asyncio
from copy import deepcopy
import json

import pytest

from shiori.chat import ChatService
from qunyu.db import Store
from shiori.settings import Settings


@pytest.fixture
def anyio_backend():
    return 'asyncio'


def response(content='', tool_calls=None):
    message = {'role': 'assistant', 'content': content}
    if tool_calls:
        message['tool_calls'] = tool_calls
    return {'content': content, 'tool_calls': tool_calls or [], 'message': message,
            'usage': {'input_tokens': 1, 'output_tokens': 1}}


def remember_call(id):
    return {'id': id, 'type': 'function', 'function': {'name': 'remember',
            'arguments': json.dumps({'content': '我喜欢柠檬茶', 'kind': 'preference'}, ensure_ascii=False)}}


class FakeProvider:
    def __init__(self):
        self.calls = []
        self.handler = None

    async def complete(self, messages, **kwargs):
        self.calls.append({'messages': deepcopy(messages), 'kwargs': deepcopy(kwargs)})
        if self.handler:
            return await self.handler(messages, kwargs, len(self.calls))
        return response('没有引用旧信息的安全回复')


@pytest.fixture
async def runtime(tmp_path):
    store, settings, provider = Store(tmp_path / 'data'), Settings(tmp_path / 'data'), FakeProvider()
    config = settings.get()
    # In-memory overrides avoid persisting any provider or connector credentials.
    config['onebot'].update(chat_policy="whitelist", enabled=True, adapter='napcat', owner_ids=['101'], allowed_groups=['202'])
    settings.get = lambda: deepcopy(config)
    store.upsert_scope({'id': 'web:default', 'auto_memory': False})
    chat = ChatService(store, settings, provider)
    yield store, config, chat, provider
    await chat.close()
    store.close()


@pytest.mark.anyio
async def test_correction_of_transitive_fact_invalidates_inflight_derived_reply(runtime, monkeypatch):
    store, _, chat, provider = runtime
    fact = store.create_memory({'scope_id': 'web:default', 'user_id': 'owner', 'kind': 'fact', 'content': '旧地址上海'})
    derived = store.create_memory({'scope_id': 'web:default', 'user_id': 'owner', 'kind': 'event',
        'content': 'OLD_D_SHANGHAI', 'dependencies': {fact['id']: fact['version']}})

    async def only_derived(scope_id, user_id, query,needed_info=None):
        return [x for x in store.recall(scope_id, user_id, '', 50) if x['id'] == derived['id']]

    monkeypatch.setattr(chat, '_recall_context', only_derived)
    entered, release = asyncio.Event(), asyncio.Event()

    async def model(messages, kwargs, call):
        if call == 1:
            assert 'OLD_D_SHANGHAI' in str(messages)
            entered.set()
            await release.wait()
            return response('OLD_D_SHANGHAI')
        assert 'OLD_D_SHANGHAI' not in str(messages)
        return response('刷新后的安全回复')

    provider.handler = model
    pending = asyncio.create_task(chat.respond('web:default', 'owner', '请根据情景回答'))
    await asyncio.wait_for(entered.wait(), 2)
    store.update_memory(fact['id'], {'content': '新地址杭州'})
    release.set()
    try:
        result = await pending
    except ValueError:
        result = None  # Rejecting a stale response is also a valid outcome.
    if result:
        assert 'OLD_D_SHANGHAI' not in result['reply']
        assert len(provider.calls) >= 2
    assert not [x for x in store.list_messages('web:default') if x['role'] == 'assistant' and 'OLD_D_SHANGHAI' in x['content']]


@pytest.mark.anyio
async def test_history_only_paraphrase_inherits_fact_provenance(runtime, monkeypatch):
    store, _, chat, provider = runtime
    fact = store.create_memory({'scope_id': 'web:default', 'user_id': 'owner', 'kind': 'fact', 'content': 'HISTORY_PRIVATE_FACT_MARKER'})

    async def first_question_only(scope_id, user_id, query,needed_info=None):
        return [store.get_memory(fact['id'])] if query == '甲问题请参考档案' else []

    monkeypatch.setattr(chat, '_recall_context', first_question_only)

    async def model(messages, kwargs, call):
        if call == 1:
            return response('A引用 HISTORY_PRIVATE_FACT_MARKER')
        assert 'A引用 HISTORY_PRIVATE_FACT_MARKER' in str(messages)
        return response('B仅根据上一条回答转述 HISTORY_PRIVATE_FACT_MARKER')

    provider.handler = model
    answer_a = await chat.respond('web:default', 'owner', '甲问题请参考档案')
    answer_b = await chat.respond('web:default', 'owner', '乙问题请换个说法')
    assert answer_b['memories'] == []  # B obtained the fact only through history.
    store.delete_memory(fact['id'])
    remaining_ids = {x['id'] for x in chat._history('web:default', limit=50)}
    assert answer_a['message_id'] not in remaining_ids
    assert answer_b['message_id'] not in remaining_ids


@pytest.mark.anyio
async def test_model_remember_cannot_recreate_same_source_after_admin_deletion(runtime):
    store, _, chat, provider = runtime
    deleted = []

    async def model(messages, kwargs, call):
        if call == 1:
            return response(tool_calls=[remember_call('remember-first')])
        if call == 2:
            first = json.loads(messages[-1]['content'])
            deleted.append(first['id'])
            store.delete_memory(first['id'])
            return response(tool_calls=[remember_call('remember-replayed')])
        return response('工具流程结束')

    provider.handler = model
    try:
        await chat.respond('web:default', 'owner', '帮我记下我的饮食偏好：我喜欢柠檬茶')
    except ValueError:
        pass  # An in-flight delete may invalidate the entire response.
    assert deleted and store.get_memory(deleted[0])['status'] == 'deleted'
    assert not [x for x in store.list_memories(scope_id='web:default', status='active') if '柠檬茶' in x['content']]


@pytest.mark.anyio
async def test_direct_jargon_explanation_keeps_versions_and_exits_history_on_delete(runtime):
    store, _, chat, provider = runtime
    term = store.create_jargon({'scope_id': 'web:default', 'term': '开饭', 'meaning': 'DICT_GAME_EXPLANATION', 'status': 'approved'})
    result = await chat.respond('web:default', 'owner', '解释黑话：开饭')
    saved = store.get_message(result['message_id'])
    assert saved['metadata'].get('jargon_versions', {}).get(term['id']) == term['version']
    assert not provider.calls
    store.delete_jargon(term['id'])
    assert result['message_id'] not in {x['id'] for x in chat._history('web:default', limit=50)}


@pytest.mark.anyio
@pytest.mark.parametrize('revocation', ['scope_disabled', 'qq_allowlist_removed'])
async def test_extraction_discards_pending_candidates_when_authority_changes(runtime, revocation):
    store, config, chat, provider = runtime
    scope_id, user_id = 'qq:group:202', '303'
    store.upsert_scope({'id': scope_id, 'auto_memory': True, 'enabled': True})
    messages = [store.add_message(scope_id, user_id, 'user', f'我长期喜欢合作游戏，这是真实偏好第{i}条') for i in range(4)]
    entered, release = asyncio.Event(), asyncio.Event()

    async def model(context, kwargs, call):
        assert kwargs['purpose'] == 'memory_extract'
        entered.set()
        await release.wait()
        return response(json.dumps([{'content': '我长期喜欢合作游戏', 'kind': 'preference', 'subject': user_id,
            'property': 'preferred_game', 'source_ids': [x['id'] for x in messages], 'confidence': .95}], ensure_ascii=False))

    provider.handler = model
    pending = asyncio.create_task(chat._extract(scope_id, user_id))
    await asyncio.wait_for(entered.wait(), 2)
    if revocation == 'scope_disabled':
        store.upsert_scope({'id': scope_id, 'enabled': False})
    else:
        config['onebot']['allowed_groups'] = []
    release.set()
    await pending
    assert not store.list_memories(scope_id=scope_id)
