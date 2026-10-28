"""The search quota index must not invalidate backups from earlier builds."""
import sqlite3

import pytest

from shiori.db import Store


def test_restore_backup_without_new_search_index_preserves_data_and_quota(tmp_path):
    store = Store(tmp_path / 'live')
    try:
        record = store.create_memory({'content': '旧备份中的共同经历'})
        attempt = store.add_event('search_attempt', '网页搜索尝试', provider='public')
        original_connection = store._conn
        backup = store.backup(tmp_path / 'legacy.db')
        with sqlite3.connect(backup) as conn:
            conn.execute('DROP INDEX events_kind_time')
        assert store.restore_from(backup, validate_only=True)['valid']
        store.create_memory({'content': '恢复时应移除的记录'})
        store.restore_from(backup)
        assert store._conn is original_connection
        assert [item['id'] for item in store.list_memories()] == [record['id']]
        assert any(item['id'] == attempt['id'] for item in store.list_events())
        assert store._conn.execute('PRAGMA quick_check').fetchone()[0] == 'ok'
        assert [row[2] for row in store._conn.execute('PRAGMA index_info(events_kind_time)')] == ['kind', 'created_at']
    finally:
        store.close()


def test_restore_rejects_an_untrusted_definition_of_search_index(tmp_path):
    store = Store(tmp_path / 'live')
    try:
        original = store.create_memory({'content': '保留本机数据'})
        backup = store.backup(tmp_path / 'modified.db')
        with sqlite3.connect(backup) as conn:
            conn.execute('DROP INDEX events_kind_time')
            conn.execute('CREATE INDEX events_kind_time ON events(created_at)')
        with pytest.raises(ValueError, match='结构不兼容'):
            store.restore_from(backup)
        assert store.get_memory(original['id'])['content'] == '保留本机数据'
    finally:
        store.close()
