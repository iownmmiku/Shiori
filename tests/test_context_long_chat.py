"""A long private chat, where every reply cites the whole conversation before it.

On the server the latest replies of one private chat cited about 700 earlier
messages each. Preparing a reply's context walked that provenance twice on the
event loop, holding up every other chat for 1-3 seconds per reply."""
import sqlite3
import threading
from collections import Counter

import pytest

from shiori.context import ContextEngine
from shiori.db import Store

SCOPE, USER = 'web:default', 'owner'


@pytest.fixture
def anyio_backend():
    return 'asyncio'


@pytest.fixture
def env(tmp_path):
    store = Store(tmp_path)
    store.upsert_scope({'id': SCOPE})
    ids = []
    for index in range(60):
        asked = store.add_message(SCOPE, USER, 'user', f'question {index}')
        ids.append(asked['id'])
        reply = store.add_message(SCOPE, 'assistant', 'assistant', f'answer {index}',
                                  source_ids=[asked['id']], context_source_ids=list(ids))
        ids.append(reply['id'])
    try:
        yield store, ContextEngine(store, {'context': {'enabled': False}}, None), ids
    finally:
        store.close()


@pytest.mark.anyio
async def test_a_long_chat_is_validated_off_the_event_loop_reading_each_message_once(env, monkeypatch):
    store, context, ids = env
    history = store.list_messages(SCOPE, limit=24)
    reads, on_loop = Counter(), set()
    original = store.get_message

    def counted(record_id):
        reads[record_id] += 1
        on_loop.add(threading.current_thread() is threading.main_thread())
        return original(record_id)
    monkeypatch.setattr(store, 'get_message', counted)
    result = await context.build(SCOPE, USER, history)
    assert [m['id'] for m in result['messages']] == [m['id'] for m in history]
    assert set(result['source_ids']) == set(ids)   # the whole conversation is its provenance
    assert on_loop == {False}                       # never on the event loop's thread
    assert set(reads) == set(ids) and max(reads.values()) == 1


@pytest.mark.anyio
@pytest.mark.parametrize('mutation', ['delete', 'withdraw'])
async def test_the_shared_walk_answers_exactly_like_a_fresh_one(env, mutation):
    store, context, ids = env
    history = store.list_messages(SCOPE, limit=24)
    removed = ids[40]   # an early question every later reply cited
    if mutation == 'delete':
        with sqlite3.connect(store.path) as connection:
            connection.execute('DELETE FROM messages WHERE id=?', (removed,))
    else:
        store.create_memory({'content': 'withdrawn source', 'blocked_source_ids': [removed]})
    built = await context.build(SCOPE, USER, history)
    fresh = context.reference(SCOPE, USER, history)   # validated without any shared cache
    for key in ('messages', 'source_ids', 'memory_versions', 'jargon_versions'):
        assert built[key] == fresh[key], key
    # Every reply in the window was built on the removed question; only the
    # member's own lines are left, and nothing from before them.
    assert [m['role'] for m in built['messages']] == ['user'] * 12
    assert removed not in built['source_ids'] and len(built['source_ids']) == 12
