"""Derived-vector reuse must save calls without retaining authority or work."""
import asyncio
from copy import deepcopy
import json

import pytest

from shiori.db import Store
from shiori.memory import MemoryEngine, _digest
from shiori.settings import DEFAULTS
from shiori.timing import TurnTiming


@pytest.fixture
def anyio_backend():
    return 'asyncio'


def config(**options):
    cfg = deepcopy(DEFAULTS)
    cfg['model'].update(base_url='https://fixture.invalid/v1', model='chat')
    cfg['memory'].update(semantic_enabled=True, embedding_model='fixture',
        background_indexing=True, query_cache_ttl_seconds=30, query_cache_entries=64,
        vector_cache_mb=1)
    cfg['memory'].update(options)
    return cfg


def setup(tmp_path, embed, **options):
    store, cfg = Store(tmp_path), config(**options)
    store.create_memory({'scope_id': 'web:default', 'user_id': 'owner',
                         'content': '咖啡饮料偏好', 'status': 'active'})
    engine = MemoryEngine(store, cfg, embed, foreground_busy=lambda: True)
    return store, cfg, engine


@pytest.mark.anyio
@pytest.mark.parametrize('setting', ['query_cache_ttl_seconds', 'query_cache_entries'])
async def test_disabling_query_cache_keeps_fresh_provider_queries(tmp_path, setting):
    calls = []

    async def embed(texts, **kwargs):
        calls.append(tuple(texts))
        return [[1., 0.] for _ in texts]

    store, _, engine = setup(tmp_path, embed, **{setting: 0})
    try:
        for _ in range(2):
            result = await engine.inspect('web:default', 'owner', '咖啡饮料')
            assert result['items'] and not result['semantic']['query_cache_hit']
        assert len(calls) == 2
        assert engine._query_cache_bytes == 0
    finally:
        await engine.aclose()
        store.close()


@pytest.mark.anyio
async def test_query_cache_expires_and_is_bounded_by_entries(tmp_path):
    calls = []

    async def embed(texts, **kwargs):
        calls.append(tuple(texts))
        return [[1., 0.] for _ in texts]

    store, _, engine = setup(tmp_path, embed, query_cache_ttl_seconds=1, query_cache_entries=2)
    try:
        await engine.inspect('web:default', 'owner', '咖啡饮料')
        cached = await engine.inspect('web:default', 'owner', '咖啡饮料')
        assert cached['semantic']['query_cache_hit'] and len(calls) == 1
        await asyncio.sleep(1.05)
        expired = await engine.inspect('web:default', 'owner', '咖啡饮料')
        assert not expired['semantic']['query_cache_hit'] and len(calls) == 2
        for query in ('喜欢饮料', '饮料偏好', '咖啡习惯'):
            await engine.inspect('web:default', 'owner', query)
        assert len(engine._query_cache) <= 2
        assert 0 < engine._query_cache_bytes <= 4 * 1024 * 1024
        engine.clear_index()
        assert engine._query_cache_bytes == 0 and not engine._query_cache
    finally:
        await engine.aclose()
        store.close()


@pytest.mark.anyio
async def test_live_cache_disable_releases_old_entries_and_reenable_starts_fresh(tmp_path):
    calls = []

    async def embed(texts, **kwargs):
        calls.append(tuple(texts))
        return [[1., 0.] for _ in texts]

    store, cfg, engine = setup(tmp_path, embed)
    try:
        await engine.inspect('web:default', 'owner', '咖啡饮料')
        assert len(calls) == 1 and engine._query_cache_bytes > 0
        cfg['memory']['query_cache_ttl_seconds'] = 0
        await engine.inspect('web:default', 'owner', '咖啡饮料')
        assert len(calls) == 2 and not engine._query_cache and engine._query_cache_bytes == 0
        cfg['memory']['query_cache_ttl_seconds'] = 30
        result = await engine.inspect('web:default', 'owner', '咖啡饮料')
        assert len(calls) == 3 and not result['semantic']['query_cache_hit']
    finally:
        await engine.aclose()
        store.close()


@pytest.mark.anyio
async def test_distinct_queries_do_not_hold_the_scoring_lock_across_embedding(tmp_path):
    entered, release = set(), asyncio.Event()

    async def embed(texts, **kwargs):
        entered.add(texts[0])
        await release.wait()
        return [[1., 0.] for _ in texts]

    store, _, engine = setup(tmp_path, embed)
    tasks = []
    try:
        tasks = [asyncio.create_task(engine.inspect('web:default', 'owner', query))
                 for query in ('咖啡饮料', '饮料偏好')]
        for _ in range(100):
            if len(entered) == 2:
                break
            await asyncio.sleep(.01)
        assert entered == {'咖啡饮料', '饮料偏好'}
        assert not engine._semantic_lock.locked()
        release.set()
        assert all(result['items'] for result in await asyncio.gather(*tasks))
    finally:
        release.set()
        await asyncio.gather(*tasks, return_exceptions=True)
        await engine.aclose()
        store.close()


@pytest.mark.anyio
async def test_close_drains_query_jobs_and_clears_all_derived_cache(tmp_path):
    entered = asyncio.Event()

    async def embed(texts, **kwargs):
        entered.set()
        await asyncio.Event().wait()

    store, _, engine = setup(tmp_path, embed)
    query = asyncio.create_task(engine.inspect('web:default', 'owner', '咖啡饮料'))
    try:
        await asyncio.wait_for(entered.wait(), 1)
        await asyncio.wait_for(engine.aclose(), 1)
        result = await asyncio.gather(query, return_exceptions=True)
        assert isinstance(result[0], asyncio.CancelledError)
        assert not engine._query_jobs and not engine._query_cache and not engine._decoded_vectors
        assert engine._query_cache_bytes == engine._decoded_vector_bytes == 0
    finally:
        query.cancel()
        await asyncio.gather(query, return_exceptions=True)
        await engine.aclose()
        store.close()


def test_decoded_vectors_reuse_compact_numbers_but_recheck_changed_raw_values(tmp_path, monkeypatch):
    store, _, engine = setup(tmp_path, None)
    import shiori.memory as memory
    decode, parses = memory.json.loads, []

    def counted(raw, *args, **kwargs):
        if raw in ('[1.0,0.0]', '[0.0,1.0]'):
            parses.append(raw)
        return decode(raw, *args, **kwargs)

    monkeypatch.setattr(memory.json, 'loads', counted)
    try:
        first = engine._decoded_vector('signature', 'content', '[1.0,0.0]', 2)
        assert engine._decoded_vector('signature', 'content', '[1.0,0.0]', 2) is first
        changed = engine._decoded_vector('signature', 'content', '[0.0,1.0]', 2)
        assert list(changed) == [0., 1.] and len(parses) == 2
        with pytest.raises((ValueError, TypeError)):
            engine._decoded_vector('signature', 'content', '[NaN,0]', 2)
        assert not engine._decoded_vectors
    finally:
        engine.close()
        store.close()


def test_decoded_cache_limit_and_zero_setting_never_retain_a_full_index(tmp_path):
    store, _, engine = setup(tmp_path, None)
    try:
        raw = json.dumps([1.] + [0.] * 511)
        engine._decoded_vector_limit = 9000
        for i in range(100):
            engine._decoded_vector('fixture', str(i), raw, 512)
        assert engine._decoded_vector_bytes <= 9000 and len(engine._decoded_vectors) < 4
        engine._decoded_vector_limit = 0
        engine._decoded_vectors.clear()
        engine._decoded_vector_bytes = 0
        engine._decoded_vector('fixture', 'zero', raw, 512)
        assert not engine._decoded_vectors and engine._decoded_vector_bytes == 0
    finally:
        engine.close()
        store.close()


def test_large_vector_scan_and_permission_batches_have_bounded_sql_work(tmp_path):
    """Prevent per-page whole-index sorting and repeated full-scope filtering."""
    store, cfg, engine = setup(tmp_path, None)
    count = 5000
    template = store.list_memories('web:default')[0]
    columns = ('id','scope_id','user_id','kind','status','visibility','valid_from',
               'valid_to','version','created_at','updated_at')
    records = [dict(template, id='scan-fixture-' + str(i).zfill(5),
                    content='synthetic vector record ' + str(i)) for i in range(count)]
    with store._lock, store._conn:
        store._conn.executemany('INSERT INTO memories(' + ','.join(columns) + ',data) VALUES ('
            + ','.join('?' for _ in range(12)) + ')',
            [tuple(record.get(key) for key in columns) + (json.dumps(record),) for record in records])
    signature = engine._signature(cfg, 2)
    connection = engine._connection()
    with connection:
        connection.executemany('INSERT INTO vectors VALUES (?,?,?,?)',
            [(signature, _digest(str(i)), 2, json.dumps([i + 1., 1.])) for i in range(count)])
        connection.executemany('INSERT INTO vector_refs VALUES (?,?,?,?,?,?)',
            [(record['id'],1,0,'synthetic-hash',signature,_digest(str(i)))
             for i, record in enumerate(records)])
    steps = {'main': 0, 'vector': 0}

    def bound(name):
        def progress():
            steps[name] += 1000
            return int(steps[name] > 2_000_000)
        return progress

    store._conn.set_progress_handler(bound('main'), 1000)
    connection.set_progress_handler(bound('vector'), 1000)
    try:
        selected = engine._cached_candidates(signature, 2, [1., 0.],
            'web:default', 'owner', cfg, None, engine._generation)
        assert selected == [record['id'] for record in reversed(records[-64:])]
        assert steps['main'] < 2_000_000 and steps['vector'] < 2_000_000
    finally:
        store._conn.set_progress_handler(None, 0)
        connection.set_progress_handler(None, 0)
        engine.close()
        store.close()


@pytest.mark.anyio
async def test_recall_phase_measurements_belong_to_the_owner_turn(tmp_path):
    async def embed(texts, **kwargs):
        await asyncio.sleep(.02)
        return [[1., 0.] for _ in texts]

    store, _, engine = setup(tmp_path, embed)
    trace = TurnTiming()
    try:
        with trace.activate():
            result = await engine.inspect('web:default', 'owner', '咖啡饮料')
        assert result['items']
        stats = trace.snapshot()
        for name in ('memory_lexical_rank_ms', 'memory_read_queue_ms', 'memory_read_worker_ms',
                     'memory_query_embedding_ms', 'memory_semantic_lock_ms', 'memory_vector_scan_ms',
                     'memory_vector_scores_ms', 'memory_use_stats_ms', 'memory_current_results_ms',
                     'memory_related_ms', 'memory_final_projection_ms'):
            assert name in stats and stats[name] >= 0
        assert stats['memory_query_embedding_ms'] >= 15
        before = trace.snapshot()
        await asyncio.sleep(.02)
        assert {k: v for k, v in before.items() if k != 'total_ms'} == {k: v for k, v in trace.snapshot().items() if k != 'total_ms'}
    finally:
        await engine.aclose()
        store.close()
