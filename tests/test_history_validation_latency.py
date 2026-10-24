"""Bound work on overlapping provenance while preserving live revocation."""
import asyncio
from collections import Counter
from copy import deepcopy
import json
import sqlite3
from types import SimpleNamespace

import pytest

from mikubot.chat import ChatService
from mikubot.db import Store
from mikubot.settings import DEFAULTS


@pytest.fixture
def service(tmp_path):
    store = Store(tmp_path)
    config = deepcopy(DEFAULTS)
    chat = ChatService(store, SimpleNamespace(get=lambda: deepcopy(config)), object())
    try: yield store, chat
    finally:
        asyncio.run(chat.close()); store.close()


def test_history_batches_overlapping_memory_versions_and_rechecks_edits(service, monkeypatch):
    store, chat = service
    facts = [store.create_memory({'content': f'事实{n}'}) for n in range(160)]
    versions = {item['id']: item['version'] for item in facts}
    for n in range(20):
        store.add_message('web:default', 'owner', 'user', f'问题{n}')
        store.add_message('web:default', 'assistant', 'assistant', f'回答{n}', memory_versions=versions)
    original = store.visible_memories
    calls = []
    def projection(*args, **kwargs):
        calls.append(set(kwargs.get('record_ids') or []))
        return original(*args, **kwargs)
    monkeypatch.setattr(store, 'visible_memories', projection)
    monkeypatch.setattr(store, 'memory_accessible', lambda *a, **k: pytest.fail('per-memory projection'))
    assert len(chat._history('web:default', limit=96, user_id='owner')) == 40
    assert calls == [set(versions)]
    assert not chat._changed(versions, {}, 'web:default', 'owner')
    store.update_memory(facts[0]['id'], {'content': '更正后的事实'})
    assert chat._changed(versions, {}, 'web:default', 'owner')
    assert all(item['role'] == 'user' for item in chat._history('web:default', limit=96, user_id='owner'))


def test_live_guard_observes_external_change_without_stale_projection(service):
    store, chat = service
    fact = store.create_memory({'content': '事实'})
    versions = {fact['id']: fact['version']}
    assert not chat._changed(versions, {}, 'web:default', 'owner')
    with sqlite3.connect(store.path) as other:
        other.execute("UPDATE memories SET status='deleted',data=json_set(data,'$.status','deleted') WHERE id=?", (fact['id'],))
    assert chat._changed(versions, {}, 'web:default', 'owner')


def test_source_guard_checks_existence_scope_and_withdrawal_without_loading_json(service, monkeypatch):
    store, chat = service
    source = store.add_message('web:default', 'owner', 'user', '来源')
    other = store.add_message('web:other', 'owner', 'user', '别的会话')
    monkeypatch.setattr(store, 'get_message', lambda *a: pytest.fail('full message decoding in hot guard'))
    assert not chat._sources_changed('web:default', [source['id'], source['id']])
    assert chat._sources_changed('web:default', [other['id']])
    assert chat._sources_changed('web:default', ['missing'])
    item = store.create_memory({'content': '撤回', 'blocked_source_ids': [source['id']]})
    assert chat._sources_changed('web:default', [source['id']])


def test_manifest_reads_each_shared_ancestor_once_and_does_not_cache_across_calls(service, monkeypatch):
    store, chat = service
    fact = store.create_memory({'content': '事实'})
    history = []
    for n in range(22):
        history.append(store.add_message('web:default', 'owner', 'user', f'问题{n}'))
        history.append(store.add_message('web:default', 'assistant', 'assistant', f'回答{n}',
            context_source_ids=[row['id'] for row in history], memory_versions={fact['id']: 1}))
    original = store.get_message
    reads = Counter()
    def get(key): reads[key] += 1; return original(key)
    monkeypatch.setattr(store, 'get_message', get)
    valid, sources, versions, _ = chat.context._manifest('web:default', 'owner', history)
    assert len(valid) == len(history) and len(sources) == len(history) and versions == {fact['id']: 1}
    assert all(count == 1 for count in reads.values())
    store.update_memory(fact['id'], {'content': '已修改'})
    valid, sources, _, _ = chat.context._manifest('web:default', 'owner', history)
    assert all(item['role'] == 'user' for item in valid)
    assert all(item['role'] == 'user' for item in sources.values())


def test_manifest_rejects_cycles_and_tampered_original_without_leaking_partial_closure(service):
    store, chat = service
    source = store.add_message('web:default', 'owner', 'user', '原始问题')
    reply = store.add_message('web:default', 'assistant', 'assistant', '回答', context_source_ids=[source['id']])
    forged = {**reply, 'content': '被改写的内容'}
    assert chat.context._manifest('web:default', 'owner', [forged])[0:2] == ([], {})
    with store._lock, store._conn:
        store._conn.execute("UPDATE messages SET data=json_set(data,'$.metadata.context_source_ids',json(?)) WHERE id=?",
                            (json.dumps([reply['id']]), reply['id']))
    current = store.get_message(reply['id'])
    assert chat.context._manifest('web:default', 'owner', [current])[0:2] == ([], {})
