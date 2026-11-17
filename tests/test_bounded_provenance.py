"""A reply stores its direct sources; its closure is walked, not copied (server, 2026-11-17).

In the long private chat on the server every reply stored the whole conversation
before it: 1,430 message IDs and 717 memory versions per reply (103 KB), one
round more than the reply before. Checking a reply re-read all of them, so the
chat slowed down a little with every turn, and each delivery receipt kept
another copy with fingerprints (about 250 KB, 172 MB of the events table).
The rule stays the same: a change anywhere upstream still drops every reply
that depended on it, and the delivery guard still checks the whole closure.
"""
import asyncio
import json
from copy import deepcopy

import pytest

from shiori.chat import ChatService
from shiori.db import Store
from shiori.delivery import DeliveryLedger, release_finished_snapshots, release_outbox_history
from shiori.settings import Settings

SCOPE, USER = 'web:default', 'owner'


@pytest.fixture
def anyio_backend():
    return 'asyncio'


def response(content):
    return {'content': content, 'tool_calls': [], 'message': {'role': 'assistant', 'content': content},
            'usage': {'input_tokens': 1, 'output_tokens': 1}}


class Provider:
    async def complete(self, messages, **kwargs):
        return response('好的')


@pytest.fixture
async def runtime(tmp_path):
    store, settings = Store(tmp_path / 'data'), Settings(tmp_path / 'data')
    config = settings.get()
    settings.get = lambda: deepcopy(config)
    store.upsert_scope({'id': SCOPE, 'auto_memory': False, 'allow_tools': False})
    chat = ChatService(store, settings, Provider())
    yield store, chat
    await chat.close()
    store.close()


async def talk(chat, rounds, start=0):
    return [await chat.respond(SCOPE, USER, f'问题 {index}') for index in range(start, start + rounds)]


@pytest.mark.anyio
async def test_a_long_chat_stores_direct_sources_and_still_reaches_the_whole_closure(runtime):
    store, chat = runtime
    results = await talk(chat, 36)
    stored = [store.get_message(result['message_id']) for result in results]
    assert all(record['metadata']['provenance'] == 'direct' for record in stored)
    widths = [len(record['metadata']['context_source_ids']) for record in stored]
    # The history window (24 rounds) caps what a reply stores; the closure keeps growing.
    assert widths[-1] == widths[-6] <= 2 * 24 + 1
    closures = [len(result['provenance']['context_source_ids']) for result in results]
    assert closures[-1] == closures[-6] + 10 and closures[-1] >= 2 * 35
    assert len(json.dumps(stored[-1], ensure_ascii=False)) < 6000
    # Walking the stored sources reaches exactly the closure the reply was checked against.
    last = stored[-1]
    walked = set(chat.context.reference(SCOPE, USER, [last])['source_ids'])
    assert walked == set(results[-1]['provenance']['context_source_ids']) | {last['id']}


@pytest.mark.anyio
async def test_removing_a_memory_used_early_still_drops_every_later_reply(runtime, monkeypatch):
    store, chat = runtime
    fact = store.create_memory({'scope_id': SCOPE, 'user_id': USER, 'kind': 'fact', 'content': '住在杭州'})

    async def first_only(scope_id, user_id, query, needed_info=None):
        return [store.get_memory(fact['id'])] if query == '问题 0' else []
    monkeypatch.setattr(chat, '_recall_context', first_only)
    results = await talk(chat, 30)
    first, later = results[0]['message_id'], [result['message_id'] for result in results[1:]]
    assert fact['id'] in store.get_message(first)['metadata']['memory_versions']
    # Later replies only reach the fact through history, so they no longer store it...
    assert all(fact['id'] not in store.get_message(key)['metadata']['memory_versions'] for key in later)
    history = {record['id'] for record in chat._history(SCOPE, limit=400, user_id=USER)}
    assert {first, *later} <= history
    store.delete_memory(fact['id'])
    # ...and still fall with it, in the history filter and in the context walk.
    history = {record['id'] for record in chat._history(SCOPE, limit=400, user_id=USER)}
    assert not ({first, *later} & history)
    replies = [store.get_message(key) for key in later[-3:]]
    assert chat.context.reference(SCOPE, USER, replies)['messages'] == []


@pytest.mark.anyio
async def test_the_delivery_guard_and_outbox_check_the_whole_closure(runtime):
    store, chat = runtime
    *_, result = await talk(chat, 30)     # longer than the 24-round history window
    reply = store.get_message(result['message_id'])
    dependencies = dict(reply['metadata'], **result['provenance'])   # as the QQ flow builds them
    closure = set(dependencies['context_source_ids'])
    assert set(reply['metadata']['context_source_ids']) < closure
    early = store.list_messages(SCOPE, limit=400)[0]
    assert early['id'] in closure and early['id'] not in reply['metadata']['context_source_ids']
    asked = store.get_message(reply['metadata']['source_ids'][0])
    batch = {'entries': [{'message': asked}], 'reply_message': reply, 'logical_message_id': reply['id'],
             'identity': {'name': 'Shiori'}, 'dependencies': dependencies}
    record = DeliveryLedger(store).create(SCOPE, USER, ['第一段', '第二段'], {}, batch)
    assert set(record['message_fingerprints']) == closure | {reply['id'], asked['id']}
    with store._lock, store._conn:
        store._conn.execute('DELETE FROM messages WHERE id=?', (early['id'],))
    assert chat._sources_changed(SCOPE, dependencies['context_source_ids'])


@pytest.mark.anyio
async def test_a_qq_reply_is_guarded_and_receipted_against_its_closure(tmp_path, monkeypatch):
    from shiori.onebot import OneBotBridge
    from shiori.settings import DEFAULTS

    class Config:
        def __init__(self):
            self.config = deepcopy(DEFAULTS)
            self.config['onebot'].update(enabled=True, chat_policy='blacklist')
            self.config['output'].update(mode='model', interval_ms=0)
            self.config['stickers']['enabled'] = False

        def get(self):
            return deepcopy(self.config)

    scope = 'qq:private:303'
    store = Store(tmp_path)
    store.upsert_scope({'id': scope, 'auto_memory': False, 'merge_window_ms': 0})
    chat = ChatService(store, Config(), Provider())
    bridge = OneBotBridge(store, Config(), chat)
    checked = []
    real = chat._sources_changed
    monkeypatch.setattr(chat, '_sources_changed', lambda scope_id, ids: checked.append(len(set(ids))) or real(scope_id, ids))

    class Socket:
        def __init__(self):
            self.sent = 0

        async def send_json(self, action):
            self.sent += 1
            bridge._pending[action['echo']][1].set_result({'status': 'ok', 'retcode': 0, 'data': {'message_id': self.sent}})
    bridge._socket = Socket()
    try:
        for index in range(30):          # longer than the 24-round history window
            await asyncio.wait_for(bridge._flow.handle(scope, '303', f'问题 {index}', [], {}, direct=True), 10)
        receipt = DeliveryLedger(store).list(scope)[0]
        reply = store.get_message(receipt['assistant_message_id'])
        stored = len(reply['metadata']['context_source_ids'])
        # Every earlier message (59) plus this request: far more than the reply stores.
        assert receipt['snapshot_released']['sources'] >= 59 > stored + 2
        assert max(checked) >= 59
    finally:
        await bridge.stop()
        await chat.close()
        store.close()


def test_a_finished_outbox_record_is_stored_without_its_snapshot(tmp_path):
    store = Store(tmp_path)
    try:
        asked = store.add_message(SCOPE, USER, 'user', '你好')
        reply = store.add_message(SCOPE, 'assistant', 'assistant', '在', source_ids=[asked['id']],
                                  context_source_ids=[asked['id']], memory_versions={})
        batch = {'entries': [{'message': asked}], 'reply_message': reply, 'logical_message_id': reply['id'],
                 'identity': {'name': 'Shiori'}, 'dependencies': dict(reply['metadata'])}
        ledger = DeliveryLedger(store)
        record = ledger.create(SCOPE, USER, ['一', '二'], {}, batch)
        record['parts'][0]['status'] = 'confirmed'
        ledger.save(record)
        kept = ledger.get(record['id'])
        assert kept['message_fingerprints'] and ledger.has_snapshot(kept)   # a part may still be resumed
        record['parts'][1]['status'] = 'confirmed'
        ledger.save(record)
        stored = ledger.get(record['id'])
        assert 'message_fingerprints' not in stored and 'context_source_ids' not in stored
        assert 'context_source_ids' not in stored['dependencies'] and 'memory_versions' not in stored['dependencies']
        assert stored['snapshot_released'] == {'sources': 2} and not ledger.has_snapshot(stored)
        assert [part['status'] for part in stored['parts']] == ['confirmed', 'confirmed']
        assert record['message_fingerprints']                                # the caller's copy is untouched
    finally:
        store.close()


@pytest.mark.anyio
async def test_older_finished_records_are_released_in_batches_and_pending_ones_kept(tmp_path):
    store = Store(tmp_path)
    try:
        def legacy(key, created_at, statuses):
            details = {'id': key, 'scope_id': SCOPE, 'user_id': USER, 'parts': [{'text': 'x', 'status': s} for s in statuses],
                       'source_ids': ['a'], 'assistant_message_id': 'b', 'context_source_ids': ['a', 'c'],
                       'dependencies': {'context_source_ids': ['a', 'c'], 'memory_versions': {'m': 1}, 'persona_id': 'default'},
                       'snapshot_version': 1, 'message_fingerprints': {'a': '1', 'b': '2', 'c': '3'}}
            with store._lock, store._conn:
                store._write('events', {'id': key, 'kind': 'qq_outbox', 'message': 'QQ 分段发送记录',
                                        'created_at': created_at, 'details': details}, ('kind', 'created_at'))
        legacy('outbox-1', '2026-11-01T00:00:00.000001Z', ['confirmed', 'confirmed'])
        legacy('outbox-2', '2026-11-01T00:00:00.000002Z', ['confirmed', 'pending'])
        legacy('outbox-3', '2026-11-01T00:00:00.000003Z', ['unknown'])
        legacy('outbox-4', '2026-11-01T00:00:00.000004Z', ['sending'])
        assert release_finished_snapshots(store, '', limit=2) == (1, '2026-11-01T00:00:00.000002Z', 2)
        assert release_finished_snapshots(store, '2026-11-01T00:00:00.000002Z', limit=2)[:2] == (1, '2026-11-01T00:00:00.000004Z')
        ledger = DeliveryLedger(store)
        # A pending part may still be resumed; a part caught mid-send is left as it was.
        for key in ('outbox-2', 'outbox-4'):
            assert ledger.get(key)['message_fingerprints'] == {'a': '1', 'b': '2', 'c': '3'}
        for key in ('outbox-1', 'outbox-3'):
            record = ledger.get(key)
            assert 'message_fingerprints' not in record and record['dependencies'] == {'persona_id': 'default'}
        with store._lock:
            times = dict(store._conn.execute("SELECT id,created_at FROM events WHERE kind='qq_outbox'").fetchall())
        assert times['outbox-1'] == '2026-11-01T00:00:00.000001Z'
        assert await release_outbox_history(store) == 0                    # nothing left to release
    finally:
        store.close()
