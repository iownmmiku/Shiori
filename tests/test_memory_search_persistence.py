"""Persisted lexical candidates retain tokens but never trust stale sources."""
import json
import sqlite3
import threading
import hashlib

import pytest

from shiori.db import Store
from shiori.memory_search import MemorySearchIndex


def fact(store, text):
    return store.create_memory({'scope_id': 'web:default', 'user_id': 'owner', 'content': text})


def recall(store, text='persistent-cobalt'):
    return store.memory_candidate_ids('web:default', 'owner', text, 8)


def ready(store):
    worker = store._memory_search_thread
    if worker is not None:
        worker.join(20)
    assert store.memory_search_status()['initialized']


def test_restart_reuses_all_existing_tokens_and_checks_source_changes(tmp_path, monkeypatch):
    store = Store(tmp_path)
    originals = [fact(store, f'persistent-cobalt-{number}') for number in range(100)]
    recall(store)
    ready(store)
    store.close()
    original_replace = MemorySearchIndex._replace
    changed = []
    monkeypatch.setattr(MemorySearchIndex, '_replace', lambda self, row: (changed.append(row['id']), original_replace(self, row))[1])
    store = Store(tmp_path)
    try:
        assert recall(store)
        ready(store)
        assert changed == []
        store.update_memory(originals[0]['id'], {'content': 'new unique-amber'})
        assert originals[0]['id'] in recall(store, 'unique-amber')
        assert changed == [originals[0]['id']]
    finally:
        store.close()


@pytest.mark.parametrize('external', [False, True])
def test_raw_sql_updates_and_deletes_are_detected_without_public_version_change(tmp_path, external):
    store = Store(tmp_path)
    target = fact(store, 'persistent-cobalt')
    recall(store)
    conn = sqlite3.connect(store.path) if external else store._conn
    try:
        updated = dict(target, content='raw-unique-amber')
        with conn:
            conn.execute('UPDATE memories SET data=? WHERE id=?', (json.dumps(updated), target['id']))
        assert target['id'] in recall(store, 'raw-unique-amber')
        with conn:
            conn.execute('DELETE FROM memories WHERE id=?', (target['id'],))
        assert target['id'] not in recall(store)
    finally:
        if external:
            conn.close()
        store.close()


def test_cold_open_revalidates_offline_edits_instead_of_trusting_id_version(tmp_path, monkeypatch):
    store = Store(tmp_path)
    target = fact(store, 'persistent-cobalt')
    recall(store)
    store.close()
    with sqlite3.connect(store.path) as connection:
        updated = dict(target, content='offline-unique-amber')
        connection.execute('UPDATE memories SET data=? WHERE id=?', (json.dumps(updated), target['id']))
    store = Store(tmp_path)
    try:
        assert target['id'] in recall(store, 'offline-unique-amber')
        assert store._memory_search.initialized
    finally:
        store.close()


def test_rollback_after_raw_sql_search_cannot_leave_persisted_false_candidates(tmp_path):
    store = Store(tmp_path)
    target = fact(store, 'persistent-cobalt')
    recall(store)
    try:
        with pytest.raises(RuntimeError), store._conn:
            store._conn.execute("UPDATE memories SET status='deleted',data=json_set(data,'$.status','deleted') WHERE id=?", (target['id'],))
            assert target['id'] not in recall(store)
            raise RuntimeError('rollback')
        assert target['id'] in recall(store)
    finally:
        store.close()


def test_lost_fts_table_after_restart_rebuilds_terms(tmp_path):
    store = Store(tmp_path)
    target = fact(store, 'persistent-cobalt')
    recall(store)
    mode = store.memory_search_status()['mode']
    store.close()
    if mode != 'fts5':
        pytest.skip('FTS5 unavailable')
    with sqlite3.connect(tmp_path / 'memory_search.db') as connection:
        connection.execute('DROP TABLE memory_fts')
    store = Store(tmp_path)
    try:
        assert target['id'] in recall(store)
    finally:
        store.close()


def large_store(store):
    template = fact(store, 'ordinary-context')
    with store._lock, store._conn:
        for number in range(512):
            store._save_memory(dict(template, id=f'bulk-{number}', content=f'ordinary-context {number}'))


def pause_after_first_batch(store, monkeypatch):
    """Gate the worker's inter-batch yield, after it releases Store's lock."""
    arrived, release = threading.Event(), threading.Event()
    original_wait = store._memory_search_stop.wait
    def wait(timeout=None):
        if not arrived.is_set():
            arrived.set()
            assert release.wait(10)
        return original_wait(timeout)
    monkeypatch.setattr(store._memory_search_stop, 'wait', wait)
    return arrived, release


def test_first_large_recall_uses_current_sql_while_background_batches_build(tmp_path, monkeypatch):
    store = Store(tmp_path)
    early = fact(store, 'early-rare-target')
    hidden = fact(store, 'hidden-rare-target')
    large_store(store)
    late = fact(store, 'late-rare-target')
    arrived, release = pause_after_first_batch(store, monkeypatch)
    original_replace = MemorySearchIndex._replace
    threads = []
    def replace(index, row):
        threads.append(threading.current_thread().name)
        assert threading.current_thread() is not threading.main_thread()
        return original_replace(index, row)
    monkeypatch.setattr(MemorySearchIndex, '_replace', replace)
    try:
        assert late['id'] in recall(store, 'late-rare-target')
        assert arrived.wait(5)
        assert store.memory_search_status()['building']
        assert not store.memory_search_status()['initialized']
        # Writes and reads proceed while the initial index is still incomplete.
        store.update_memory(early['id'], {'content': 'changed-cobalt-rare'})
        store.delete_memory(late['id'])
        assert early['id'] in recall(store, 'changed-cobalt-rare')
        assert late['id'] not in recall(store, 'late-rare-target')
        # An external permission edit must be enforced immediately and must
        # invalidate any already-built candidate metadata before publication.
        with sqlite3.connect(store.path) as connection:
            changed = dict(hidden, user_id='other')
            connection.execute('UPDATE memories SET user_id=?,data=? WHERE id=?',
                               ('other', json.dumps(changed), hidden['id']))
        assert hidden['id'] not in recall(store, 'hidden-rare-target')
        release.set()
        ready(store)
        assert threads and set(threads) == {'shiori-memory-index'}
        assert early['id'] in recall(store, 'changed-cobalt-rare')
        assert hidden['id'] not in recall(store, 'hidden-rare-target')
        assert late['id'] not in recall(store, 'late-rare-target')
        with store._lock:
            raw = store._conn.execute('SELECT data FROM memories WHERE id=?', (early['id'],)).fetchone()[0]
            saved = store._memory_search.connection.execute('SELECT fingerprint FROM source_state WHERE id=?', (early['id'],)).fetchone()[0]
        assert saved == hashlib.sha256(raw.encode()).hexdigest()
    finally:
        release.set()
        store.close()


def test_restore_and_transaction_rollback_during_background_build(tmp_path, monkeypatch):
    store = Store(tmp_path / 'live')
    original = fact(store, 'snapshot-rare-target')
    snapshot = store.backup(tmp_path / 'snapshot.db')
    large_store(store)
    transient = fact(store, 'transient-rare-target')
    arrived, release = pause_after_first_batch(store, monkeypatch)
    try:
        recall(store)
        assert arrived.wait(5)
        with pytest.raises(RuntimeError), store._lock, store._conn:
            store._conn.execute("UPDATE memories SET status='deleted',data=json_set(data,'$.status','deleted') WHERE id=?", (original['id'],))
            assert original['id'] not in recall(store, 'snapshot-rare-target')
            raise RuntimeError('rollback')
        assert original['id'] in recall(store, 'snapshot-rare-target')
        store.restore_from(snapshot)
        assert transient['id'] not in recall(store, 'transient-rare-target')
        release.set()
        ready(store)
        assert original['id'] in recall(store, 'snapshot-rare-target')
        assert transient['id'] not in recall(store, 'transient-rare-target')
        assert store._memory_search.connection.execute('SELECT COUNT(*) FROM documents').fetchone()[0] == 1
    finally:
        release.set()
        store.close()


def test_small_restored_database_removes_large_old_index_in_background(tmp_path, monkeypatch):
    store = Store(tmp_path / 'live')
    original = fact(store, 'snapshot-rare-target')
    snapshot = store.backup(tmp_path / 'snapshot.db')
    large_store(store)
    recall(store)
    ready(store)
    index = store._memory_search
    original_remove, calls = index._remove, []
    def remove(identifier):
        calls.append(threading.current_thread().name)
        assert threading.current_thread() is not threading.main_thread()
        return original_remove(identifier)
    monkeypatch.setattr(index, '_remove', remove)
    try:
        store.restore_from(snapshot)
        assert original['id'] in recall(store, 'snapshot-rare-target')
        ready(store)
        assert calls and set(calls) == {'shiori-memory-index'}
        assert index.connection.execute('SELECT COUNT(*) FROM documents').fetchone()[0] == 1
    finally:
        store.close()


def test_shutdown_stops_pending_index_worker_without_touching_source_records(tmp_path):
    store = Store(tmp_path)
    large_store(store)
    late = fact(store, 'late-rare-target')
    assert late['id'] in recall(store, 'late-rare-target')
    worker = store._memory_search_thread
    index = store._memory_search
    store.close()
    assert worker is None or not worker.is_alive()
    assert index.connection is None
    reopened = Store(tmp_path)
    try:
        assert reopened.get_memory(late['id'])['content'] == 'late-rare-target'
        assert late['id'] in recall(reopened, 'late-rare-target')
        ready(reopened)
    finally:
        reopened.close()
