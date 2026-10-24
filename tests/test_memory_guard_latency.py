"""Offline guards for the streaming source-withdrawal check's hot path."""

from concurrent.futures import ThreadPoolExecutor
import json
import sqlite3
import time

import pytest

from mikubot.db import BLOCKED_SOURCE_CACHE_SCOPES, Store


@pytest.fixture
def store(tmp_path):
    item = Store(tmp_path)
    try:
        yield item
    finally:
        item.close()


def memory(store, *, scope='web:default', blocked=(), sources=()):
    return store.create_memory({'scope_id': scope, 'content': '记忆',
                                'source_ids': list(sources), 'blocked_source_ids': list(blocked)})


def replace_blocked(conn, record, blocked, *, table='memories'):
    conn.execute(f"UPDATE {table} SET data=json_set(data,'$.blocked_source_ids',json(?)) WHERE id=?",
                 (json.dumps(list(blocked)), record['id']))


def count_scans(store, monkeypatch):
    scans = []
    original = store._query_blocked_memory_sources

    def query(scope):
        scans.append(scope)
        return original(scope)

    monkeypatch.setattr(store, '_query_blocked_memory_sources', query)
    return scans


def test_hot_guard_reuses_only_relevant_scope_and_returns_copy(store, monkeypatch):
    first = memory(store, blocked=['source-a'])
    other = memory(store, scope='qq:group:elsewhere', blocked=['source-b'])
    scans = count_scans(store, monkeypatch)
    assert store.blocked_memory_sources('web:default') == {'source-a'}
    returned = store.blocked_memory_sources('web:default')
    returned.add('not-a-tombstone')
    store.add_message('web:default', 'owner', 'user', '不会修改撤回来源')
    store.record_usage({'input_tokens': 5})
    store.update_memory(other['id'], {'content': '另一个群的修改'})
    for _ in range(100):
        assert store.blocked_memory_sources('web:default') == {'source-a'}
    assert scans == ['web:default']
    store.update_memory(first['id'], {'blocked_source_ids': ['new-tombstone']})
    assert store.blocked_memory_sources('web:default') == {'source-a', 'new-tombstone'}
    assert scans == ['web:default', 'web:default']


def test_deletion_jargon_correction_and_raw_clear_are_immediate(store):
    item = memory(store, sources=['original-message'])
    assert store.blocked_memory_sources('web:default') == set()
    store.delete_memory(item['id'])
    assert store.blocked_memory_sources('web:default') == {'original-message'}
    jargon = store.create_jargon({'scope_id': 'web:default', 'term': '测试',
                                  'meaning': '测试语义', 'source_ids': ['jargon-message']})
    assert store.blocked_memory_sources('web:default') == {'original-message'}
    store.delete_jargon(jargon['id'])
    assert store.blocked_memory_sources('web:default') == {'original-message', 'jargon-message'}
    with store._lock, store._conn:
        store._conn.execute('DELETE FROM memories')
        store._conn.execute('DELETE FROM jargon')
    assert store.blocked_memory_sources('web:default') == set()


@pytest.mark.parametrize('table', ['memories', 'jargon'])
def test_direct_sql_scope_move_invalidates_both_origins(store, table):
    item = (memory(store, blocked=['moved']) if table == 'memories' else
            store.create_jargon({'scope_id': 'web:default', 'term': '测试', 'meaning': '含义',
                                  'blocked_source_ids': ['moved']}))
    assert store.blocked_memory_sources('web:default') == {'moved'}
    assert store.blocked_memory_sources('qq:group:target') == set()
    with store._lock, store._conn:
        store._conn.execute(f"UPDATE {table} SET scope_id=?,data=json_set(data,'$.scope_id',?) WHERE id=?",
                            ('qq:group:target', 'qq:group:target', item['id']))
    assert store.blocked_memory_sources('web:default') == set()
    assert store.blocked_memory_sources('qq:group:target') == {'moved'}


def test_transaction_reads_never_pollute_cache_after_rollback_revision_reuse(store):
    item = memory(store, blocked=['committed'])
    assert store.blocked_memory_sources('web:default') == {'committed'}
    with store._lock:
        store._conn.execute('BEGIN')
        replace_blocked(store._conn, item, ['rolled-back'])
        # Must see this connection's own write, rather than its older cache.
        assert store.blocked_memory_sources('web:default') == {'rolled-back'}
        store._conn.rollback()
        assert store.blocked_memory_sources('web:default') == {'committed'}
        # This commit reuses the rollback's counter; caching the uncommitted
        # result above would now incorrectly return "rolled-back".
        replace_blocked(store._conn, item, ['new-commit'])
        store._conn.commit()
    assert store.blocked_memory_sources('web:default') == {'new-commit'}


def test_first_guard_inside_rolled_back_transaction_has_no_tracking_side_effect(store):
    item = memory(store, blocked=['before'])
    with store._lock:
        store._conn.execute('BEGIN')
        replace_blocked(store._conn, item, ['temporary'])
        assert store.blocked_memory_sources('web:default') == {'temporary'}
        assert store._conn.in_transaction
        assert not store._blocked_source_cache
        store._conn.rollback()
    assert store.blocked_memory_sources('web:default') == {'before'}


def test_external_connection_commits_are_visible_without_ttl(store):
    item = memory(store, blocked=['before'])
    assert store.blocked_memory_sources('web:default') == {'before'}
    with sqlite3.connect(store.path) as external:
        replace_blocked(external, item, ['external'])
    assert store.blocked_memory_sources('web:default') == {'external'}
    with sqlite3.connect(store.path) as external:
        external.execute('DELETE FROM memories')
    assert store.blocked_memory_sources('web:default') == set()


def test_external_commit_during_cold_scan_does_not_publish_stale_result(store, monkeypatch):
    item = memory(store, blocked=['before'])
    original = store._query_blocked_memory_sources
    changed = False

    def query(scope):
        nonlocal changed
        result = original(scope)
        if not changed:
            changed = True
            with sqlite3.connect(store.path) as external:
                replace_blocked(external, item, ['during-scan'])
        return result

    monkeypatch.setattr(store, '_query_blocked_memory_sources', query)
    assert store.blocked_memory_sources('web:default') == {'during-scan'}
    assert store.blocked_memory_sources('web:default') == {'during-scan'}


def test_external_commit_during_hot_cache_lookup_is_rechecked(store):
    item = memory(store, blocked=['before'])
    assert store.blocked_memory_sources('web:default') == {'before'}
    changed = False

    def trace(sql):
        nonlocal changed
        if not changed and sql.startswith('SELECT revision FROM temp.blocked_source_revisions'):
            changed = True
            with sqlite3.connect(store.path) as external:
                replace_blocked(external, item, ['during-hit'])

    store._conn.set_trace_callback(trace)
    try:
        assert store.blocked_memory_sources('web:default') == {'during-hit'}
    finally:
        store._conn.set_trace_callback(None)
    assert changed


def test_lru_and_transaction_tracking_are_bounded(store, monkeypatch):
    scans = count_scans(store, monkeypatch)
    for i in range(BLOCKED_SOURCE_CACHE_SCOPES + 50):
        assert store.blocked_memory_sources(f'qq:group:{i}') == set()
    assert len(store._blocked_source_cache) == BLOCKED_SOURCE_CACHE_SCOPES
    assert store._conn.execute('SELECT COUNT(*) FROM temp.blocked_source_revisions').fetchone()[0] == BLOCKED_SOURCE_CACHE_SCOPES
    scans.clear()
    store.blocked_memory_sources('qq:group:0')
    store.blocked_memory_sources('qq:group:0')
    assert scans == ['qq:group:0']
    assert len(store._blocked_source_cache) == BLOCKED_SOURCE_CACHE_SCOPES


def test_backup_schema_is_unchanged_and_restore_reinstalls_tracking(store, tmp_path):
    item = memory(store, blocked=['snapshot'])
    assert store.blocked_memory_sources('web:default') == {'snapshot'}
    snapshot = store.backup(tmp_path / 'snapshot.db')
    with sqlite3.connect(snapshot) as saved:
        assert not saved.execute("SELECT name FROM sqlite_master WHERE name LIKE 'blocked_source_%'").fetchall()
    with store._lock, store._conn:
        replace_blocked(store._conn, item, ['later'])
    assert store.blocked_memory_sources('web:default') == {'later'}
    store.restore_from(snapshot)
    assert store.blocked_memory_sources('web:default') == {'snapshot'}
    with store._lock, store._conn:
        replace_blocked(store._conn, item, ['after-restore'])
    assert store.blocked_memory_sources('web:default') == {'after-restore'}


def test_parallel_threads_see_committed_tombstones(store):
    items = [memory(store, scope=f'qq:private:{i}', blocked=['original']) for i in range(6)]

    def update_and_check(index):
        item = items[index]
        scope = item['scope_id']
        assert store.blocked_memory_sources(scope) == {'original'}
        for generation in range(20):
            value = f'blocked-{index}-{generation}'
            with store._lock, store._conn:
                replace_blocked(store._conn, item, [value])
            assert store.blocked_memory_sources(scope) == {value}

    with ThreadPoolExecutor(max_workers=6) as pool:
        list(pool.map(update_and_check, range(6)))


@pytest.mark.parametrize('size', [8000, 14000])
def test_large_memory_hot_guard_avoids_repeated_json_scans(store, monkeypatch, size):
    template = memory(store)
    columns = ('scope_id', 'user_id', 'kind', 'status', 'visibility', 'valid_from',
               'valid_to', 'version', 'created_at', 'updated_at')
    rows = []
    for index in range(size):
        record = {**template, 'id': f'bulk-{index}', 'content': f'第{index}条短记忆'}
        rows.append((record['id'], *(record.get(key) for key in columns), json.dumps(record)))
    with store._lock, store._conn:
        store._conn.executemany(
            f"INSERT INTO memories(id,{','.join(columns)},data) VALUES ({','.join('?' for _ in range(12))})", rows)
    scans = count_scans(store, monkeypatch)
    started = time.perf_counter()
    for _ in range(20):
        assert store._query_blocked_memory_sources('web:default') == set()
    baseline = (time.perf_counter() - started) / 20
    assert scans == ['web:default'] * 20
    scans.clear()
    assert store.blocked_memory_sources('web:default') == set()
    scans.clear()
    started = time.perf_counter()
    for _ in range(200):
        assert store.blocked_memory_sources('web:default') == set()
    elapsed = time.perf_counter() - started
    assert not scans
    store.delete_memory(template['id'])
    store.update_memory('bulk-0', {'blocked_source_ids': ['withdrawn-now']})
    assert store.blocked_memory_sources('web:default') == {'withdrawn-now'}
    assert scans == ['web:default']
    print(f'{size} memories: original scan={baseline * 1000:.3f}ms/check; '
          f'cached={elapsed * 1000 / 200:.3f}ms/check; '
          f'200 hot guards={elapsed:.6f}s; full JSON scans=0; new tombstone immediate')
