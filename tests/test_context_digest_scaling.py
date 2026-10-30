"""Cumulative digests validate each current source once, with no cross-call trust."""
from collections import Counter
import sqlite3

import pytest

from shiori.context import ContextEngine
from shiori.db import Store, message_fingerprint
import shiori.db as database


@pytest.fixture
def env(tmp_path):
    store = Store(tmp_path)
    store.upsert_scope({'id': 'web:default', 'context_compress': True})
    context = ContextEngine(store, {'context': {'enabled': True}}, None)
    try:
        yield store, context
    finally:
        store.close()


def digest(store, sources, dependencies=None, **fields):
    return store.create_memory({'kind': 'event', 'content': 'Cumulative conversation summary',
        'property': 'conversation_digest', 'derived': True,
        'source_ids': [row['id'] for row in sources],
        'source_fingerprints': {row['id']: message_fingerprint(row) for row in sources},
        'covered_message_ids': [row['id'] for row in sources], 'dependencies': dependencies or {}, **fields})


def test_cumulative_source_reads_and_hashes_are_linear_in_unique_messages(env, monkeypatch):
    store, context = env
    sources, dependencies = [], {}
    for number in range(35):
        sources.extend(store.add_message('web:default', 'owner', 'user', f'source {number}:{i}') for i in range(12))
        latest = digest(store, sources, dict(dependencies))
        dependencies[latest['id']] = latest['version']
    original = database.message_fingerprint
    fingerprints, batches = Counter(), []
    def fingerprint(row):
        fingerprints[row['id']] += 1
        return original(row)
    monkeypatch.setattr(database, 'message_fingerprint', fingerprint)
    original_list = store._list
    def listed(table, *args, **kwargs):
        result = original_list(table, *args, **kwargs)
        if table == 'messages':
            batches.append(len(result))
        return result
    monkeypatch.setattr(store, '_list', listed)
    monkeypatch.setattr(store, 'memory_property_records', lambda *a, **k: pytest.fail('full summary list'))
    assert context._latest_summary('web:default', 'owner')['id'] == latest['id']
    assert set(fingerprints) == {row['id'] for row in sources}
    assert max(fingerprints.values()) == 1
    assert sum(batches) == len(sources) and max(batches) <= 400


@pytest.mark.parametrize('mutation', ['edit', 'delete', 'withdraw', 'dependency', 'permission'])
def test_latest_summary_rechecks_sources_dependencies_and_permissions(env, mutation):
    store, context = env
    first_source = store.add_message('web:default', 'owner', 'user', 'first')
    second_source = store.add_message('web:default', 'owner', 'user', 'second')
    first = digest(store, [first_source])
    fact = store.create_memory({'content': 'a dependency'})
    latest = digest(store, [first_source, second_source], {first['id']: 1, fact['id']: 1})
    assert context._latest_summary('web:default', 'owner')['id'] == latest['id']
    if mutation in ('edit', 'delete'):
        with sqlite3.connect(store.path) as connection:
            if mutation == 'edit':
                connection.execute("UPDATE messages SET data=json_set(data,'$.content','changed') WHERE id=?", (second_source['id'],))
            else:
                connection.execute('DELETE FROM messages WHERE id=?', (second_source['id'],))
    elif mutation == 'withdraw':
        store.create_memory({'content': 'withdrawn source', 'blocked_source_ids': [second_source['id']]})
    elif mutation == 'dependency':
        store.update_memory(fact['id'], {'content': 'corrected dependency'})
    else:
        store.update_memory(latest['id'], {'user_id': 'another-user'})
    assert context._latest_summary('web:default', 'owner')['id'] == first['id']


def test_newest_candidates_page_past_invalid_summaries_and_timestamp_ties(env):
    store, context = env
    source = store.add_message('web:default', 'owner', 'user', 'source')
    first = digest(store, [source])
    for _ in range(18):
        digest(store, [source], {'missing-fact': 1})
    assert context._latest_summary('web:default', 'owner')['id'] == first['id']
    store.upsert_scope({'id': 'web:default', 'enabled': False})
    assert context._latest_summary('web:default', 'owner') is None


def test_summary_guard_projects_only_its_dependencies(env, monkeypatch):
    store, context = env
    fact = store.create_memory({'content': 'relevant'})
    for number in range(100):
        store.create_memory({'content': f'unrelated {number}'})
    source = store.add_message('web:default', 'owner', 'user', 'source', memory_versions={fact['id']: 1})
    original = store.visible_memories
    def bounded(*args, **kwargs):
        assert set(kwargs['record_ids']) <= {fact['id']}
        return original(*args, **kwargs)
    monkeypatch.setattr(store, 'visible_memories', bounded)
    assert context._unchanged('web:default', 'owner', [source],
        {source['id']: message_fingerprint(source)}, {fact['id']: 1}, {}, False)
