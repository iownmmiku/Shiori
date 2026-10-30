"""Schedule extensions retain strict backup schemas and accept older snapshots."""
import json
import sqlite3
from types import SimpleNamespace

import pytest

from shiori.db import Store
from shiori.life_schedule import CharacterSchedule
from shiori.schedule_delivery import ScheduleDelivery
from test_life_schedule import Settings, Provider, prepare


def extended_store(root):
    store = Store(root)
    service = CharacterSchedule(store, Settings(), Provider(), root)
    flow = SimpleNamespace(store=store, chat=SimpleNamespace(character_schedule=service))
    queue = ScheduleDelivery(flow)
    return store, service, queue


@pytest.mark.parametrize('include_messages', [False, True])
def test_backup_chat_option_covers_materials_diaries_and_queue(tmp_path, include_messages):
    store, service, _ = extended_store(tmp_path / 'live')
    try:
        pid = prepare(service)
        scope = 'qq:private:source'
        store.upsert_scope({'id': scope, 'persona_id': pid})
        service.save_settings({'enabled': True, 'diary_chat_enabled': True, 'diary_scopes': [scope]})
        source = store.add_message(scope, 'user', 'user', 'raw private original')
        assert service.capture_message(scope, 'user', 'user', source['content'], source['id'])
        _, record = service._record(pid)
        with store._lock, store._conn:
            for day, kind, extra in [('2026-10-24', 'pure-before', {'material_count': 0}),
                                      ('2026-10-25', 'legacy-chat', {'material_count': 1}),
                                      ('2026-10-26', 'legacy-continuation', {'material_count': 0}),
                                      ('2026-10-27', 'pure-after', {'material_count': 0, 'source_fingerprints': {}}),
                                      ('2026-10-28', 'tracked-chat', {'material_count': 0, 'source_fingerprints': {source['id']: 'fingerprint'}})]:
                value = dict(id=kind, content=kind, date=day, **extra)
                store._conn.execute('INSERT INTO life_diaries VALUES(?,?,?,?,?,?)',
                                    (kind, pid, record['key'], day, scope, json.dumps(value)))
            store._conn.execute('INSERT INTO character_schedule_queue VALUES(?,?,?,?,?,?,?,?)',
                                ('queue', scope, 'key', 'pending', json.dumps([{'id': source['id']}]), 1, 2, ''))
        snapshot = tmp_path / 'snapshot.db'
        store.backup(snapshot, include_messages=include_messages)
        source_db = sqlite3.connect(snapshot)
        try:
            assert source_db.execute('SELECT COUNT(*) FROM messages').fetchone()[0] == int(include_messages)
            assert source_db.execute('SELECT COUNT(*) FROM life_materials').fetchone()[0] == int(include_messages)
            assert source_db.execute('SELECT COUNT(*) FROM character_schedule_queue').fetchone()[0] == int(include_messages)
            ids = {r[0] for r in source_db.execute('SELECT id FROM life_diaries')}
            assert ids == ({'pure-before', 'pure-after', 'legacy-chat', 'legacy-continuation', 'tracked-chat'}
                           if include_messages else {'pure-before', 'pure-after'})
        finally:
            source_db.close()
        assert store.restore_from(snapshot, validate_only=True) == {'valid': True}
        assert store.get_message(source['id'])['content'] == 'raw private original'
        assert store._conn.execute('SELECT COUNT(*) FROM life_diaries').fetchone()[0] == 5
    finally:
        store.close()


def test_current_schedule_backup_validates_and_retains_manual_plan(tmp_path):
    store, service, _ = extended_store(tmp_path / 'live')
    try:
        pid = prepare(service)
        snapshot = tmp_path / 'snapshot.db'
        store.backup(snapshot)
        assert store.restore_from(snapshot, validate_only=True) == {'valid': True}
        store.restore_from(snapshot)
        service.reload()
        assert service.state(pid, '2026-10-29')['day']['summary'] == '角色创作的一天'
    finally:
        store.close()


def test_pre_schedule_backup_recreates_only_trusted_optional_tables(tmp_path):
    old = Store(tmp_path / 'old')
    snapshot = tmp_path / 'old.db'
    old.backup(snapshot)
    old.close()
    store, service, queue = extended_store(tmp_path / 'live')
    try:
        store.restore_from(snapshot)
        service.reload()
        queue.reload(restored=True)
        assert queue.status() == []
        assert not service.get_settings()['enabled']
        pid = prepare(service)
        assert service.state(pid, '2026-10-29')['day']
    finally:
        store.close()


@pytest.mark.parametrize('malformed', ['schema', 'body', 'queue'])
def test_unknown_extension_schema_and_malformed_data_are_rejected(tmp_path, malformed):
    store, service, _ = extended_store(tmp_path / 'live')
    try:
        prepare(service)
        snapshot = tmp_path / 'bad.db'
        store.backup(snapshot)
        with sqlite3.connect(snapshot) as source:
            if malformed == 'schema':
                source.execute('DROP INDEX life_calls_day')
                source.execute('CREATE INDEX life_calls_day ON life_calls(persona_id)')
            elif malformed == 'body':
                source.execute("UPDATE life_personas SET data='[]'")
            else:
                source.execute('INSERT INTO character_schedule_queue VALUES(?,?,?,?,?,?,?,?)',
                               ('bad', 'qq:group:123', 'key', 'pending', json.dumps([{}] * 9), 1, 2, ''))
        with pytest.raises(ValueError):
            store.restore_from(snapshot, validate_only=True)
        assert service.state()['profile']
    finally:
        store.close()


def test_restored_pending_messages_require_explicit_retry(tmp_path):
    store, _, queue = extended_store(tmp_path)
    try:
        with store._lock, store._conn:
            store._conn.execute('INSERT INTO character_schedule_queue VALUES(?,?,?,?,?,?,?,?)',
                ('pending', 'qq:group:123', 'key', 'pending', json.dumps([{'id':'original'}]), 1, 2, ''))
        queue.reload(restored=True)
        assert queue.status()[0]['status'] == 'uncertain'
        assert '备份' in queue.status()[0]['reason']
    finally:
        store.close()


def test_queue_receipts_never_hide_new_pending_entries(tmp_path):
    store, _, queue = extended_store(tmp_path)
    try:
        with store._lock, store._conn:
            store._conn.executemany('INSERT INTO character_schedule_queue VALUES(?,?,?,?,?,?,?,?)',
                [(f'old-{n}', 'qq:group:123', 'key', 'submitted', '[]', n, n, '') for n in range(600)])
            store._conn.execute('INSERT INTO character_schedule_queue VALUES(?,?,?,?,?,?,?,?)',
                ('pending', 'qq:group:123', 'key', 'pending', json.dumps([{'id':'new'}]), 9999, 10000, ''))
        assert queue.status()[0]['id'] == 'pending'
        assert queue.clear_scope('qq:group:123')['changed'] == 1
        assert queue.status() == []
    finally:
        store.close()


def test_unresolved_history_never_hides_pending_controls_after_restore(tmp_path):
    store, _, queue = extended_store(tmp_path)
    try:
        with store._lock, store._conn:
            store._conn.executemany('INSERT INTO character_schedule_queue VALUES(?,?,?,?,?,?,?,?)',
                [(f'unknown-{n}', 'qq:group:123', 'key', 'uncertain', '[]', n, n, '') for n in range(501)])
            store._conn.execute('INSERT INTO character_schedule_queue VALUES(?,?,?,?,?,?,?,?)',
                ('new', 'qq:group:456', 'key', 'pending', json.dumps([{'id': 'new-source'}]), 9999, 10000, ''))
        assert any(row['id'] == 'new' for row in queue.status())
        assert queue.clear_scope('qq:group:456')['changed'] == 1
        assert not any(row['id'] == 'new' for row in queue.status())
    finally:
        store.close()
