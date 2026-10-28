"""Moving group windows never withdraw real sources; real mutations still do."""
import asyncio
from copy import deepcopy
import json

import pytest

from shiori.chat import ChatService
from shiori.db import Store
from shiori.settings import DEFAULTS
from shiori.social import ConversationFlow


SCOPE = 'qq:group:202'


@pytest.fixture
def anyio_backend():
    return 'asyncio'


class Config:
    def __init__(self):
        self.value = deepcopy(DEFAULTS)
        self.value['onebot'].update(enabled=True, chat_policy='blacklist')
        self.value['model'].update(base_url='https://fixture.invalid/v1', model='fixture')
        self.value['output'].update(mode='model', interval_ms=0)
        self.value['stickers']['enabled'] = False

    def get(self):
        return deepcopy(self.value)


class Planner:
    def __init__(self):
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.calls = []

    async def complete(self, messages, **kwargs):
        assert kwargs['before_send']()
        payload = json.loads(messages[-1]['content'])
        self.calls.append(payload)
        self.entered.set()
        await self.release.wait()
        return {'content': json.dumps({'action': 'reply', 'target_message_id': payload['current_message_id'],
            'target_user_id': payload['current_user_id'], 'addressed_to_bot': True,
            'intent': 'Answer the current request', 'reason': 'A direct request', 'needed_info': []})}


@pytest.fixture
def setup(tmp_path):
    store, config, provider = Store(tmp_path), Config(), Planner()
    store.upsert_scope({'id': SCOPE, 'plan_enabled': True, 'auto_memory': False,
                        'merge_window_ms': 0, 'cooldown_seconds': 0})
    chat = ChatService(store, config, provider)
    chat.social.knowledge_fn = None
    yield store, config, provider, chat
    store.close()


def background(store, count):
    return [store.add_message(SCOPE, str(400 + index % 4), 'user', f'Ordinary group message {index}')
            for index in range(count)]


def remove_message(store, message):
    with store._lock, store._conn:
        store._conn.execute('DELETE FROM messages WHERE id=?', (message['id'],))


def edit_message(store, message):
    changed = dict(message, content='The canonical message has been edited')
    with store._lock, store._conn:
        store._conn.execute('UPDATE messages SET data=? WHERE id=?', (json.dumps(changed), message['id']))


@pytest.mark.anyio
@pytest.mark.parametrize('new_messages', [1, 220])
async def test_direct_planner_keeps_fixed_history_when_other_members_speak(setup, new_messages):
    store, _, provider, chat = setup
    old = background(store, 13)
    message = store.add_message(SCOPE, '303', 'user', 'miku, please answer this question', at_bot=True)
    task = asyncio.create_task(chat.social.plan(store.get_scope(SCOPE), message, direct=True))
    try:
        await asyncio.wait_for(provider.entered.wait(), 2)
        background(store, new_messages)
        assert old[0]['id'] not in {item['id'] for item in chat._history(SCOPE, limit=14, user_id='303')}
        provider.release.set()
        result = await asyncio.wait_for(task, 2)
        assert result['reply'] and result['semantic'] and result['target_message_id'] == message['id']
        assert store.get_message(old[0]['id']) == old[0]
    finally:
        provider.release.set()
        await chat.close()


@pytest.mark.anyio
@pytest.mark.parametrize('change', ['delete', 'edit', 'withdraw', 'memory', 'jargon', 'identity', 'revoke'])
async def test_fixed_planner_history_still_rejects_real_source_or_dependency_changes(setup, change):
    store, config, provider, chat = setup
    old = background(store, 12)
    memory = store.create_memory({'scope_id': SCOPE, 'user_id': '303', 'kind': 'fact',
        'content': 'The user enjoys music', 'visibility': 'group', 'source_ids': [old[0]['id']]})
    jargon = store.create_jargon({'scope_id': SCOPE, 'term': 'fixture phrase', 'meaning': 'A friendly greeting',
        'status': 'approved', 'use_allowed': True})
    store.add_message(SCOPE, 'assistant', 'assistant', 'A previous verified reply',
        source_ids=[old[0]['id']], memory_versions={memory['id']: memory['version']},
        jargon_versions={jargon['id']: jargon['version']})
    message = store.add_message(SCOPE, '303', 'user', 'miku, please answer this question', at_bot=True)
    assert len(chat._history(SCOPE, limit=14, user_id='303')) == 14
    task = asyncio.create_task(chat.social.plan(store.get_scope(SCOPE), message, direct=True))
    try:
        await asyncio.wait_for(provider.entered.wait(), 2)
        background(store, 2)  # Mutated sources also roll outside the prompt window.
        if change == 'delete':
            remove_message(store, old[0])
        elif change == 'edit':
            edit_message(store, old[0])
        elif change == 'withdraw':
            store.delete_memory(memory['id'])
            assert store.get_message(old[0]['id'])['content']
        elif change == 'memory':
            store.update_memory(memory['id'], {'importance': .9})
        elif change == 'jargon':
            store.update_jargon(jargon['id'], {'status': 'disabled'})
        elif change == 'identity':
            config.value['identity']['name'] = 'A different bot'
        else:
            config.value['onebot']['blocked_groups'] = ['202']
        provider.release.set()
        result = await asyncio.wait_for(task, 2)
        assert not result['reply']
    finally:
        provider.release.set()
        await chat.close()


@pytest.mark.anyio
@pytest.mark.parametrize('change', ['traffic', 'delete', 'edit', 'withdraw', 'memory', 'jargon', 'identity', 'revoke', 'reply_edit'])
async def test_multipart_reply_uses_fixed_sources_but_stops_after_real_mutation(setup, change):
    store, config, _, chat = setup
    store.upsert_scope({'id': SCOPE, 'plan_enabled': False})
    memory = store.create_memory({'scope_id': SCOPE, 'user_id': '303', 'kind': 'fact',
        'content': 'The user enjoys music', 'visibility': 'group'})
    jargon = store.create_jargon({'scope_id': SCOPE, 'term': 'fixture phrase', 'meaning': 'A friendly greeting',
        'status': 'approved', 'use_allowed': True})
    reply = 'First paragraph.\n\nSecond paragraph.\n\nThird paragraph.'
    original = {}

    async def respond(scope, user, text, images, metadata):
        original.update(store.get_message(metadata['existing_message_id']))
        message = store.add_message(scope, 'assistant', 'assistant', reply,
            source_ids=metadata['source_ids'], context_source_ids=metadata['source_ids'],
            memory_versions={memory['id']: memory['version']},
            jargon_versions={jargon['id']: jargon['version']})
        return {'reply': reply, 'message_id': message['id']}

    chat.respond = respond
    sent = []

    async def emit(scope, text, metadata):
        sent.append(text)
        if len(sent) == 1:
            background(store, 220)
            if change == 'delete':
                remove_message(store, original)
            elif change == 'edit':
                edit_message(store, original)
            elif change == 'withdraw':
                withdrawal = store.create_memory({'scope_id': SCOPE, 'user_id': '303', 'kind': 'fact',
                    'content': 'Withdraw this source', 'source_ids': [original['id']]})
                store.delete_memory(withdrawal['id'])
            elif change == 'memory':
                store.update_memory(memory['id'], {'importance': .9})
            elif change == 'jargon':
                store.update_jargon(jargon['id'], {'status': 'disabled'})
            elif change == 'identity':
                config.value['identity']['name'] = 'A different bot'
            elif change == 'revoke':
                config.value['onebot']['blocked_groups'] = ['202']
            elif change == 'reply_edit':
                edit_message(store, store.get_message(metadata['assistant_message_id']))
        return True

    flow = ConversationFlow(store, config, chat, emit, lambda *_: True)
    try:
        await flow.handle(SCOPE, '303', 'miku, please reply in three paragraphs', [], {'at_bot': True}, direct=True)
        expected = reply.split('\n\n') if change == 'traffic' else ['First paragraph.']
        assert sent == expected
        if change == 'traffic':
            assert store.get_message(original['id']) == original
        else:
            assert any(event['kind'] == 'qq_output' and event['details']['stopped_part'] == 2
                       for event in store.list_events())
    finally:
        await flow.close()
        await chat.close()


@pytest.mark.anyio
@pytest.mark.parametrize('first_result', ['reply', 'skip'])
async def test_slow_proactive_planner_coalesces_waiting_group_traffic(setup, first_result):
    store, config, provider, chat = setup
    config.value['planner'].update(group_batch_ms=1, group_batch_messages=2, min_interval_ms=0)
    sent = []
    original_complete = provider.complete

    async def complete(*args, **kwargs):
        response = await original_complete(*args, **kwargs)
        if len(provider.calls) == 1 and first_result == 'skip':
            value = json.loads(response['content'])
            value.update(action='skip', intent='')
            response['content'] = json.dumps(value)
        return response

    provider.complete = complete

    async def respond(scope, user, text, images, metadata):
        message = store.add_message(scope, 'assistant', 'assistant', text, source_ids=metadata['source_ids'])
        return {'reply': text, 'message_id': message['id']}

    async def emit(scope, text, metadata):
        sent.append(text)
        return True

    chat.respond = respond
    flow = ConversationFlow(store, config, chat, emit, lambda *_: True)
    try:
        await flow.handle(SCOPE, '303', 'Discussion topic initial question', [], {}, wait=False)
        await asyncio.wait_for(provider.entered.wait(), 2)
        for index in range(20):
            await flow.handle(SCOPE, str(400 + index % 3), f'Discussion topic followup {index}', [], {}, wait=False)
            await asyncio.sleep(.003)
        assert len(provider.calls) == 1
        assert len(flow._planner_batches) == 1
        assert len(flow._jobs) == 2
        provider.release.set()
        while flow._jobs:
            await asyncio.wait_for(asyncio.gather(*list(flow._jobs)), 2)
        assert len(provider.calls) == 2
        assert provider.calls[1]['current_message_text'] == 'Discussion topic followup 19'
        assert provider.calls[1]['current_user_id'] == str(400 + 19 % 3)
        assert sent[-1] == 'Discussion topic followup 19'
        assert not flow._planner_batches
    finally:
        provider.release.set()
        await flow.close()
        await chat.close()


@pytest.mark.anyio
async def test_direct_request_does_not_wait_for_optional_group_planner_lock(setup):
    store, config, provider, chat = setup
    config.value['planner'].update(group_batch_ms=1, group_batch_messages=2, min_interval_ms=0)

    async def emit(*_):
        return True

    flow = ConversationFlow(store, config, chat, emit, lambda *_: True)
    try:
        await flow.handle(SCOPE, '303', 'Discussion topic initial question', [], {}, wait=False)
        await asyncio.wait_for(provider.entered.wait(), 2)
        await flow.handle(SCOPE, '404', 'Discussion topic waiting passive message', [], {}, wait=False)
        await asyncio.sleep(.01)
        task = asyncio.create_task(flow.handle(SCOPE, '505', 'A direct question', [], {'at_bot': True}, direct=True, wait=False))
        for _ in range(100):
            if len(provider.calls) == 2:
                break
            await asyncio.sleep(.001)
        assert len(provider.calls) == 2
        assert provider.calls[1]['current_user_id'] == '505'
        assert provider.calls[1]['signals']['explicit'] is True
        provider.release.set()
        await asyncio.wait_for(task, 2)
    finally:
        provider.release.set()
        await flow.close()
        await chat.close()


@pytest.mark.anyio
async def test_merge_window_and_failure_feedback_resolve_old_sources_directly(setup):
    store, config, _, chat = setup
    store.upsert_scope({'id': SCOPE, 'plan_enabled': False, 'merge_window_ms': 50})
    sent = []

    async def respond(scope, user, text, images, metadata):
        background(store, 220)
        raise ValueError('Fixture model failure')

    async def emit(scope, text, metadata):
        sent.append((text, metadata))
        return True

    chat.respond = respond
    flow = ConversationFlow(store, config, chat, emit, lambda *_: True)
    try:
        task = asyncio.create_task(flow.handle(SCOPE, '303', 'miku, answer me', [], {'at_bot': True}, direct=True))
        for _ in range(100):
            if flow._batches:
                break
            await asyncio.sleep(.001)
        assert flow._batches
        background(store, 220)
        await asyncio.wait_for(task, 2)
        assert len(sent) == 1 and sent[0][1]['failure_feedback'] is True
    finally:
        await flow.close()
        await chat.close()
