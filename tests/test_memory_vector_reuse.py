"""A recall reuses decoded vectors instead of reading and checking every stored one again."""
from copy import deepcopy
import json

from shiori.db import Store
from shiori.memory import MemoryEngine, _digest
from shiori.settings import DEFAULTS
import shiori.memory as memory


def engine_with(tmp_path, count, *, cache_mb=1):
    store, cfg = Store(tmp_path), deepcopy(DEFAULTS)
    cfg['model'].update(base_url='https://fixture.invalid/v1', model='chat')
    cfg['memory'].update(semantic_enabled=True, embedding_model='fixture', vector_cache_mb=cache_mb)
    template = store.create_memory({'scope_id': 'web:default', 'user_id': 'owner', 'content': '模板', 'status': 'active'})
    columns = ('id', 'scope_id', 'user_id', 'kind', 'status', 'visibility', 'valid_from',
               'valid_to', 'version', 'created_at', 'updated_at')
    records = [dict(template, id=f'vec-{i:03d}', content=f'记忆 {i}') for i in range(count)]
    with store._lock, store._conn:
        store._conn.executemany('INSERT INTO memories(' + ','.join(columns) + ',data) VALUES ('
            + ','.join('?' for _ in range(12)) + ')',
            [tuple(record.get(key) for key in columns) + (json.dumps(record),) for record in records])
    engine = MemoryEngine(store, cfg, None)
    signature = engine._signature(cfg, 2)
    connection = engine._connection()
    with connection:
        # Vector i points mostly along x, so the best matches for [1, 0] are the last ones.
        connection.executemany('INSERT INTO vectors VALUES (?,?,?,?)',
            [(signature, _digest(str(i)), 2, json.dumps([i + 1., 1.])) for i in range(count)])
        connection.executemany('INSERT INTO vector_refs VALUES (?,?,?,?,?,?)',
            [(record['id'], 1, 0, 'fixture-hash', signature, _digest(str(i))) for i, record in enumerate(records)])
    return store, cfg, engine, signature, records


def scan(engine, cfg, signature):
    return engine._cached_candidates(signature, 2, [1., 0.], 'web:default', 'owner', cfg, None, engine._generation)


def test_a_second_recall_reads_no_vector_text(tmp_path, monkeypatch):
    store, cfg, engine, signature, records = engine_with(tmp_path, 200)
    loads, digest, touched = memory.json.loads, memory._digest, []

    def parsed(raw, *args, **kwargs):
        if isinstance(raw, str) and raw.startswith('[') and not raw.startswith('[['):   # not a page of references
            touched.append('parse')
        return loads(raw, *args, **kwargs)

    def hashed(text):
        if text.startswith('['):
            touched.append('hash')      # the old code checked every stored text on each scan
        return digest(text)

    monkeypatch.setattr(memory.json, 'loads', parsed)
    monkeypatch.setattr(memory, '_digest', hashed)
    statements = []
    engine._connection().set_trace_callback(statements.append)
    try:
        first = scan(engine, cfg, signature)
        assert first == [record['id'] for record in reversed(records[-64:])]
        assert touched.count('parse') == 200
        touched.clear()
        statements.clear()
        assert scan(engine, cfg, signature) == first
        assert touched == []
        assert not [sql for sql in statements if 'vector FROM' in sql or 'v.vector' in sql]
    finally:
        engine.close()
        store.close()


def test_a_scan_fetches_one_row_per_page_not_one_per_reference(tmp_path):
    """Every fetched SQLite row hands the GIL to other threads. On the server a scan of ~1800
    references in 64-row pages took ~0.5 s for ~30 ms of work while the event loop was busy."""
    store, cfg, engine, signature, records = engine_with(tmp_path, 300)
    index, main = [], []
    engine._connection().set_trace_callback(index.append)
    store._conn.set_trace_callback(main.append)
    try:
        assert scan(engine, cfg, signature) == [record['id'] for record in reversed(records[-64:])]
        pages = [sql for sql in index if 'FROM vector_refs' in sql]
        versions = [sql for sql in main if 'json_group_object(m.id' in sql]
        assert len(pages) == 2 and len(versions) == 1       # one page, then the empty read that ends the scan
    finally:
        engine._connection().set_trace_callback(None)
        store._conn.set_trace_callback(None)
        engine.close()
        store.close()


def test_a_vector_of_another_dimension_is_skipped(tmp_path):
    store, cfg, engine, signature, records = engine_with(tmp_path, 3)
    try:
        with engine._index_lock:
            connection = engine._connection()
            with connection:
                connection.execute('UPDATE vectors SET dimension=3, vector=? WHERE content_hash=?',
                                   (json.dumps([500., 1., 0.]), _digest('0')))
        assert scan(engine, cfg, signature) == ['vec-002', 'vec-001']
    finally:
        engine.close()
        store.close()


def test_a_rewritten_vector_is_not_served_from_the_old_decoded_copy(tmp_path):
    store, cfg, engine, signature, records = engine_with(tmp_path, 3)
    try:
        assert scan(engine, cfg, signature)[0] == 'vec-002'
        with engine._index_lock:
            connection = engine._connection()
            with connection:
                engine._store_vector(connection, signature, _digest('0'), 2, [100., 1.])
        assert scan(engine, cfg, signature)[0] == 'vec-000'
    finally:
        engine.close()
        store.close()


def test_decoded_vectors_take_half_the_room_of_doubles(tmp_path):
    store, cfg, engine, signature, _ = engine_with(tmp_path, 1)
    try:
        vector = engine._decoded_vector(signature, 'fixture', json.dumps([1.] + [0.] * 1023), 1024)
        assert vector.typecode == 'f' and vector.itemsize == 4 and vector[0] == 1.
    finally:
        engine.close()
        store.close()


def test_the_similarity_matches_a_plain_sum():
    left, right = [0.6, 0.8, 0.0], [0.8, 0.6, 0.0]
    assert abs(memory._dot(left, right) - sum(a * b for a, b in zip(left, right))) < 1e-12
