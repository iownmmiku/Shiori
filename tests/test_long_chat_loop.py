"""A long private chat must not stall the event loop around each reply (server, 2026-11-17).

In 21 hours the server's loop stood still 46 times for 1-3 s, most often at
trivial reads (the 12 chat records, one chat, today's usage). 33 of them came
right after a reply in one private chat whose replies each cite about 1,430
earlier messages: the delivery receipt, built in a worker thread, held the
store lock while reading 45 MB of those rows and then spent seconds decoding
and re-encoding them, and the loop's small reads waited for the lock and for
the interpreter. The largest stalls were the reply's own history checks, run
on the loop. The sticker check decoded the chat's last 200 messages on the loop
to find one id.
"""
import asyncio
import gc
import json
import sqlite3
import threading
from copy import deepcopy
from datetime import datetime, timedelta, timezone

import pytest

from shiori import delivery
from shiori.chat import ChatService
from shiori.context import ContextEngine
from shiori.db import Store
from shiori.delivery import DeliveryLedger, _delivery_fingerprint
from shiori.settings import DEFAULTS, resolve_model

SCOPE, USER = 'web:default', 'owner'


@pytest.fixture
def anyio_backend():
    return 'asyncio'


def long_chat(store, rounds, *, start=0):
    """Each reply cites the whole conversation before it, as on the server."""
    ids = [m['id'] for m in store.list_messages(SCOPE, limit=1000)]
    replies = []
    for index in range(start, start + rounds):
        asked = store.add_message(SCOPE, USER, 'user', f'问题 {index}')
        ids.append(asked['id'])
        reply = store.add_message(SCOPE, 'assistant', 'assistant', f'回答 {index}',
                                  source_ids=[asked['id']], context_source_ids=list(ids))
        ids.append(reply['id'])
        replies.append((asked, reply))
    return replies


def batch(asked, reply):
    return {'entries': [{'message': asked}], 'reply_message': reply, 'logical_message_id': reply['id'],
            'identity': {'name': 'Shiori'}, 'dependencies': reply['metadata']}


def fresh_fingerprints(store, asked, reply):
    """What the receipt held before the change: every row decoded and fingerprinted again."""
    keys = DeliveryLedger._snapshot_ids({'source_ids': [asked['id']], 'assistant_message_id': reply['id'],
                                         'dependencies': reply['metadata'],
                                         'context_source_ids': reply['metadata']['context_source_ids']})
    originals = {asked['id']: asked, reply['id']: reply}
    for row in store.ordered_messages(keys, scope_id=SCOPE):
        originals.setdefault(row['id'], row)
    return {key: _delivery_fingerprint(row) for key, row in originals.items() if key in keys}


@pytest.fixture
def store(tmp_path):
    value = Store(tmp_path)
    value.upsert_scope({'id': SCOPE})
    yield value
    value.close()


def test_a_receipt_decodes_only_the_rows_it_has_not_seen_and_matches_a_fresh_one(store, monkeypatch):
    replies = long_chat(store, 40)
    decoded = []
    original = delivery._delivery_fingerprint
    monkeypatch.setattr(delivery, '_delivery_fingerprint', lambda row: decoded.append(row['id']) or original(row))
    ledger = DeliveryLedger(store)
    first = ledger.create(SCOPE, USER, ['好的'], {}, batch(*replies[-1]))
    assert list(first['message_fingerprints'].items()) == list(fresh_fingerprints(store, *replies[-1]).items())
    # The next reply cites everything the last one did plus one more round.
    decoded.clear()
    asked, reply = long_chat(store, 1, start=40)[0]
    second = ledger.create(SCOPE, USER, ['好的'], {}, batch(asked, reply))
    assert list(second['message_fingerprints'].items()) == list(fresh_fingerprints(store, asked, reply).items())
    assert len(second['message_fingerprints']) == 82 and len(decoded) <= 4   # not 84


def test_an_edited_or_deleted_source_is_fingerprinted_again(store):
    replies = long_chat(store, 20)
    asked, reply = replies[-1]
    ledger = DeliveryLedger(store)
    before = ledger.create(SCOPE, USER, ['好的'], {}, batch(asked, reply))['message_fingerprints']
    edited, deleted = replies[3][0]['id'], replies[5][1]['id']
    with sqlite3.connect(store.path) as connection:   # another connection, as an admin tool would
        connection.execute("UPDATE messages SET data=json_set(data,'$.content','改过') WHERE id=?", (edited,))
        connection.execute('DELETE FROM messages WHERE id=?', (deleted,))
    after = ledger.create(SCOPE, USER, ['好的'], {}, batch(asked, reply))['message_fingerprints']
    assert after[edited] != before[edited]
    assert after[edited] == _delivery_fingerprint(store.get_message(edited))
    assert deleted in before and deleted not in after
    assert list(after.items()) == list(fresh_fingerprints(store, asked, reply).items())


def test_sources_are_read_in_short_lock_holds_and_still_in_chat_order(store):
    messages = [store.add_message(SCOPE, USER, 'user', f'消息 {n}') for n in range(250)]
    with store._lock, store._conn:          # equal timestamps: the insertion order decides
        store._conn.execute("UPDATE messages SET created_at='2026-11-17T05:00:00.000000Z' WHERE rowid % 3 = 0")
    expected = [m['id'] for m in store.list_messages(SCOPE, limit=1000)]
    statements = []
    store._conn.set_trace_callback(statements.append)
    try:
        found = store.ordered_messages([m['id'] for m in reversed(messages)], scope_id=SCOPE)
    finally:
        store._conn.set_trace_callback(None)
    assert [m['id'] for m in found] == expected
    reads = [sql for sql in statements if sql.startswith('SELECT created_at,rowid,id,data FROM messages')]
    assert len(reads) == 3                  # one store-lock hold per 100 ids
    plan = ' | '.join(row[3] for row in store._conn.execute('EXPLAIN QUERY PLAN ' + reads[0]))
    assert 'TEMP B-TREE' not in plan and 'SCAN messages' not in plan, plan


def test_a_provenance_walk_is_freed_on_return_not_by_the_garbage_collector(store):
    # The walk's recursive helper referenced itself, and through it every row
    # it had loaded: about 150 MB per walk in the server's long chat, freed
    # only by full collections that paused every thread for 0.5-0.8 s.
    long_chat(store, 30)
    for n in range(20):
        store.create_memory({'scope_id': SCOPE, 'user_id': USER, 'content': f'喜欢第 {n} 座山'})
    context = ContextEngine(store, {'context': {'enabled': False}}, None)
    history = store.list_messages(SCOPE, limit=24)
    gc.collect()
    gc.disable()
    try:
        assert len(context.reference(SCOPE, USER, history)['messages']) == 24
        after_walk = gc.collect()
        assert len(store.visible_memories(SCOPE, USER)) == 20
        after_projection = gc.collect()
    finally:
        gc.enable()
    assert after_walk < 20 and after_projection < 20, (after_walk, after_projection)


def test_the_sticker_check_finds_the_newest_member_line_without_decoding_any(store, monkeypatch):
    def newest_by_decoding(within):
        return next((m['id'] for m in reversed(store.list_messages(SCOPE, limit=within)) if m.get('role') == 'user'), None)
    member = store.add_message(SCOPE, USER, 'user', '最后一句')
    for n in range(5):
        store.add_message(SCOPE, 'assistant', 'assistant', f'回复 {n}', source_ids=[member['id']])
    expected = {within: newest_by_decoding(within) for within in (3, 200)}
    assert expected == {3: None, 200: member['id']}
    decoded = []
    real = json.loads
    monkeypatch.setattr(json, 'loads', lambda *a, **k: decoded.append(1) or real(*a, **k))
    assert {within: store.newest_user_message_id(SCOPE, within=within) for within in (3, 200)} == expected
    assert decoded == []


class Settings:
    def __init__(self):
        self.data = deepcopy(DEFAULTS)
        self.data['model'].update(base_url='https://fixture.invalid/v1', model='fixture')

    def get(self):
        return deepcopy(self.data)


class Model:
    def __init__(self, settings):
        self.settings, self.prompts = settings, []

    def resolve(self, purpose):
        return resolve_model(self.settings.get(), purpose)

    async def complete(self, messages, **kwargs):
        if kwargs.get('purpose', 'chat') == 'chat':
            self.prompts.append('\n'.join(str(m['content']) for m in messages))
        return {'content': '记得。', 'tool_calls': [], 'message': {'role': 'assistant', 'content': '记得。'}, 'usage': {}}


@pytest.mark.anyio
async def test_a_reply_checks_its_history_off_the_event_loop(tmp_path, monkeypatch):
    store, settings = Store(tmp_path), Settings()
    store.upsert_scope({'id': SCOPE, 'auto_memory': False, 'allow_tools': False})
    model = Model(settings)
    chat = ChatService(store, settings, model)
    yesterday = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    old = store.add_message(SCOPE, USER, 'user', '昨天说好周日去爬山', sent_at=yesterday)
    store.add_message(SCOPE, 'assistant', 'assistant', '好，周日见', source_ids=[old['id']], sent_at=yesterday)
    long_chat(store, 30)
    on_loop = {}
    loop_thread = threading.current_thread()

    def watch(owner, name):
        real = getattr(owner, name)

        def call(*args, **kwargs):
            on_loop.setdefault(name, set()).add(threading.current_thread() is loop_thread)
            return real(*args, **kwargs)
        monkeypatch.setattr(owner, name, call)
    watch(chat, '_history')
    watch(chat, '_context')
    watch(chat.context, 'reference')
    try:
        result = await asyncio.wait_for(chat.respond(SCOPE, USER, '昨天聊了什么'), 30)
        assert on_loop == {'_history': {False}, '_context': {False}, 'reference': {False}}
        # Same reply as before: the recalled lines still reach the model and its provenance.
        assert '【历史消息（时间检索）】' in model.prompts[-1] and '昨天说好周日去爬山' in model.prompts[-1]
        assert old['id'] in store.get_message(result['message_id'])['metadata']['context_source_ids']
    finally:
        await chat.close()
        store.close()
