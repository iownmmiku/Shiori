import sqlite3

import pytest

from shiori.db import Store
from shiori.memory_search import MemorySearchIndex


@pytest.fixture
def store(tmp_path):
    value = Store(tmp_path / 'data')
    yield value
    value.close()


def fact(store, content, **values):
    return store.create_memory({'content': content, 'scope_id': 'web:default', 'user_id': 'owner', **values})


def bulk(store, template, count, *, prefix='new', content='今天讨论一些普通事务', updated_at='2026-10-20T00:00:00Z', **values):
    with store._lock, store._conn:
        for number in range(count):
            store._save_memory(dict(template, id=f'{prefix}-{number}', content=content,
                created_at=updated_at, updated_at=updated_at, **values))


def test_old_rare_memory_survives_thousands_of_newer_records_and_hot_reads_are_bounded(store, monkeypatch):
    template = fact(store, '普通事实')
    bulk(store, template, 3000, prefix='old', updated_at='2020-01-01T00:00:00Z')
    with store._lock, store._conn:
        store._save_memory(dict(template, id='ancient-target', content='保险柜的暗号是星际微分青铜鸢尾',
            updated_at='2010-01-01T00:00:00Z', created_at='2010-01-01T00:00:00Z'))
    bulk(store, template, 3000)
    assert 'ancient-target' in store.memory_candidate_ids('web:default', 'owner', '青铜鸢尾', limit=32)
    worker = store._memory_search_thread
    if worker is not None:
        worker.join(20)
    assert store.memory_search_status()['initialized']
    calls = []
    store._conn.set_trace_callback(calls.append)
    replace = store._memory_search._replace
    monkeypatch.setattr(store._memory_search, '_replace', lambda record: pytest.fail('hot recall decoded a memory body'))
    original_list = store._list
    def guarded(table, *args, **kwargs):
        assert table != 'memories', 'candidate query used the full rich-record projection'
        return original_list(table, *args, **kwargs)
    monkeypatch.setattr(store, '_list', guarded)
    try:
        ids = store.memory_candidate_ids('web:default', 'owner', '青铜鸢尾', limit=32)
        assert 'ancient-target' in ids and len(ids) <= 32
        assert not any('SELECT data FROM memories' in query for query in calls)
    finally:
        store._conn.set_trace_callback(None)
        monkeypatch.setattr(store._memory_search, '_replace', replace)


def test_dirty_updates_deletes_and_rollback_refresh_only_changed_ids(store, monkeypatch):
    wanted = fact(store, '旧口令 emerald-tulip')
    other = fact(store, '普通记录')
    store.memory_candidate_ids('web:default', 'owner', '')
    calls = []
    replace = store._memory_search._replace
    monkeypatch.setattr(store._memory_search, '_replace', lambda value: (calls.append(value['id']), replace(value))[1])
    store.update_memory(wanted['id'], {'content': '新口令 cobalt-orchid'})
    assert wanted['id'] in store.memory_candidate_ids('web:default', 'owner', 'cobalt', 8)
    assert calls == [wanted['id']]
    calls.clear()
    store.delete_memory(wanted['id'])
    assert wanted['id'] not in store.memory_candidate_ids('web:default', 'owner', 'cobalt', 8)
    # A deleted row is decoded only as the one dirty row, then excluded.
    assert calls == [wanted['id']]
    try:
        with store._lock, store._conn:
            old = store.get_memory(other['id'])
            store._write('memories', dict(old, status='deleted'),
                ('scope_id', 'user_id', 'kind', 'status', 'visibility', 'valid_from', 'valid_to', 'version', 'created_at', 'updated_at'))
            assert other['id'] not in store.memory_candidate_ids('web:default', 'owner', '', 8)
            raise RuntimeError('roll back')
    except RuntimeError:
        pass
    assert other['id'] in store.memory_candidate_ids('web:default', 'owner', '', 8)


def test_coarse_scope_visibility_consent_and_time_filters_apply_before_limit(store):
    for scope_id in ('web:a', 'web:b'):
        store.upsert_scope({'id': scope_id, 'memory_space': 'shared'})
    store.upsert_scope({'id': 'web:c'})
    own = fact(store, '银河桃子', scope_id='web:a', visibility='private')
    shared = fact(store, '银河桃子', scope_id='web:b', visibility='group', shareable=True)
    template = fact(store, '银河桃子', scope_id='web:a', user_id='stranger', pinned=True)
    bulk(store, template, 250, prefix='foreign-private', content='银河桃子', user_id='stranger')
    excluded = [template]
    excluded.append(fact(store, '银河桃子', scope_id='web:b', visibility='private', shareable=True, pinned=True))
    excluded.append(fact(store, '银河桃子', scope_id='web:b', visibility='group', shareable=False, pinned=True))
    excluded.append(fact(store, '银河桃子', scope_id='web:c', visibility='group', shareable=True, pinned=True))
    excluded.append(fact(store, '银河桃子', scope_id='web:a', valid_to='2000-01-01T00:00:00Z'))
    excluded.append(fact(store, '银河桃子', scope_id='web:a', valid_from='2999-01-01T00:00:00Z'))
    excluded.append(fact(store, '银河桃子', scope_id='web:a', status='pending'))
    ids = store.memory_candidate_ids('web:a', 'owner', '银河桃子', 8)
    assert set(ids) == {own['id'], shared['id']}
    assert store.filter_memory_candidate_ids('web:a', 'owner', [item['id'] for item in excluded] + ids) == ids
    # Scope and consent are fresh, not cached by the index.
    store.upsert_scope({'id': 'web:b', 'enabled': False})
    assert shared['id'] not in store.memory_candidate_ids('web:a', 'owner', '银河桃子', 8)
    store.upsert_scope({'id': 'web:b', 'enabled': True, 'memory_space': 'other'})
    assert shared['id'] not in store.filter_memory_candidate_ids('web:a', 'owner', [shared['id']])
    store.upsert_scope({'id': 'web:a', 'enabled': False})
    assert store.memory_candidate_ids('web:a', 'owner', '') == []


def test_group_never_collects_private_candidates_and_no_cross_space_digest(store):
    store.upsert_scope({'id': 'qq:group:a', 'memory_space': 'shared'})
    store.upsert_scope({'id': 'web:b', 'memory_space': 'shared'})
    private = fact(store, '群聊秘密', scope_id='qq:group:a', visibility='private')
    group = fact(store, '群聊秘密', scope_id='qq:group:a', visibility='group')
    digest = fact(store, '群聊秘密', scope_id='web:b', visibility='group', shareable=True, property='conversation_digest')
    assert store.memory_candidate_ids('qq:group:a', 'owner', '群聊秘密') == [group['id']]
    assert store.filter_memory_candidate_ids('qq:group:a', 'owner', [private['id'], digest['id'], group['id']]) == [group['id']]


def test_candidates_are_not_authorization_results_dependencies_still_checked(store):
    source = fact(store, '相同口令第一版')
    dependent = fact(store, '相同口令推导', dependencies={source['id']: 1})
    store.update_memory(source['id'], {'content': '相同口令第二版'})
    ids = store.memory_candidate_ids('web:default', 'owner', '口令')
    assert dependent['id'] in ids
    visible = store.visible_memories('web:default', 'owner', record_ids=ids)
    assert dependent['id'] not in {item['id'] for item in visible}


def test_candidate_versions_reject_stale_cache_metadata_without_loading_bodies(store, monkeypatch):
    target = fact(store, '旧版文字')
    private = fact(store, '他人的私人文字', user_id='stranger')
    assert store.memory_candidate_versions('web:default', 'owner', [target['id']]) == {target['id']: 1}
    store.update_memory(target['id'], {'content': '已更正的文字'})
    # Resolve scope metadata first so the assertion targets memory bodies.
    original_get = store._get
    def guarded_get(table, identifier):
        assert table != 'memories', 'version filtering decoded a memory body'
        return original_get(table, identifier)
    monkeypatch.setattr(store, '_get', guarded_get)
    queries = []
    store._conn.set_trace_callback(queries.append)
    try:
        current = store.memory_candidate_versions('web:default', 'owner', [private['id'], target['id'], 'missing'])
        assert current == {target['id']: 2}
        assert current[target['id']] != 1
        assert store.memory_candidate_versions('web:default', 'owner', [target['id']], scope_allowed=lambda origin: False) == {}
        assert not any('SELECT data FROM memories' in query for query in queries)
    finally:
        store._conn.set_trace_callback(None)
    monkeypatch.setattr(store, '_get', original_get)
    store.delete_memory(target['id'])
    assert store.memory_candidate_versions('web:default', 'owner', [target['id']]) == {}


def test_restore_invalidates_index_without_changing_main_schema(store, tmp_path):
    wanted = fact(store, '旧快照里的靛蓝鸢尾')
    original_schema = [tuple(row) for row in store._conn.execute('SELECT type,name,sql FROM sqlite_master ORDER BY type,name')]
    snapshot = store.backup(tmp_path / 'snapshot.db')
    store.memory_candidate_ids('web:default', 'owner', '靛蓝鸢尾')
    store.delete_memory(wanted['id'])
    newer = fact(store, '快照后的玛瑙海豚')
    assert wanted['id'] not in store.memory_candidate_ids('web:default', 'owner', '靛蓝鸢尾')
    store.restore_from(snapshot)
    assert wanted['id'] in store.memory_candidate_ids('web:default', 'owner', '靛蓝鸢尾')
    assert newer['id'] not in store.memory_candidate_ids('web:default', 'owner', '玛瑙海豚')
    assert original_schema == [tuple(row) for row in store._conn.execute('SELECT type,name,sql FROM sqlite_master ORDER BY type,name')]


def test_fts_unavailable_uses_persistent_inverted_index_without_hot_full_read(store, monkeypatch, caplog):
    def absent(self):
        raise sqlite3.OperationalError('no such module: fts5')
    monkeypatch.setattr(MemorySearchIndex, '_create_fts', absent)
    wanted = fact(store, '中文蒸汽齿轮和 singularityterm')
    assert wanted['id'] in store.memory_candidate_ids('web:default', 'owner', '蒸汽齿轮')
    assert store.memory_search_status()['mode'] == 'inverted'
    assert 'FTS5不可用' in caplog.text and store.memory_search_status()['warning']
    monkeypatch.setattr(store._memory_search, '_replace', lambda record: pytest.fail('fallback decoded all bodies again'))
    assert wanted['id'] in store.memory_candidate_ids('web:default', 'owner', 'singularityterm')


def test_corrupt_cache_rebuild_and_missing_fts_recovery_preserve_main_data(store, caplog):
    wanted = fact(store, '重建测试蓝铜矿')
    cache = store.data_dir / 'memory_search.db'
    cache.write_bytes(b'broken disposable cache')
    assert wanted['id'] in store.memory_candidate_ids('web:default', 'owner', '蓝铜矿')
    assert '重建' in caplog.text
    if store.memory_search_status()['mode'] == 'fts5':
        store._memory_search.connection.execute('DROP TABLE memory_fts')
        store._memory_search.connection.commit()
        assert wanted['id'] in store.memory_candidate_ids('web:default', 'owner', '蓝铜矿')
    assert store.get_memory(wanted['id'])['content'] == '重建测试蓝铜矿'


def test_unwritable_cache_uses_explicit_sql_fallback_and_remains_bounded(store, monkeypatch, caplog):
    wanted = fact(store, '单独的昆仑石')
    for number in range(8):
        fact(store, f'普通事实 {number}')
    def unavailable(*args, **kwargs):
        raise sqlite3.OperationalError('unable to open database file')
    monkeypatch.setattr('shiori.memory_search.MemorySearchIndex', unavailable)
    ids = store.memory_candidate_ids('web:default', 'owner', '昆仑石', 2)
    assert wanted['id'] in ids and len(ids) == 2
    assert store.memory_search_status()['mode'] == 'sql_fallback'
    assert '降级' in caplog.text


def test_closing_store_closes_derived_connection_and_reopen_rebuilds_from_main(store):
    wanted = fact(store, '持久紫水晶')
    store.memory_candidate_ids('web:default', 'owner', '紫水晶')
    connection = store._memory_search.connection
    root = store.data_dir
    store.close()
    with pytest.raises(sqlite3.ProgrammingError):
        connection.execute('SELECT 1')
    reopened = Store(root)
    try:
        assert wanted['id'] in reopened.memory_candidate_ids('web:default', 'owner', '紫水晶')
    finally:
        reopened.close()
