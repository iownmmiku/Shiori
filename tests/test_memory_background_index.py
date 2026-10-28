import asyncio
import copy
import json

import pytest

from shiori.db import Store
from shiori.memory import MemoryEngine, _documents, _record_hash, _digest
from shiori.settings import DEFAULTS


@pytest.fixture
def anyio_backend():
    return 'asyncio'


def config():
    result = copy.deepcopy(DEFAULTS)
    result['memory'].update(semantic_enabled=True, embedding_model='embedding-test')
    result['model'].update(base_url='https://fixture.invalid/v1', model='chat')
    return result


def fact(store, content, **fields):
    return store.create_memory({'scope_id': 'web:default', 'user_id': 'owner', 'content': content, **fields})


@pytest.mark.anyio
async def test_cold_recall_returns_without_waiting_for_document_embeddings(tmp_path):
    store = Store(tmp_path)
    target = fact(store, '拿铁是我喜欢的饮料')
    entered, release = asyncio.Event(), asyncio.Event()
    calls = []
    async def embed(texts, *, background=False, **kwargs):
        calls.append((texts, background))
        if background:
            entered.set()
            await release.wait()
        return [[1., 0.] for _ in texts]
    engine = MemoryEngine(store, config(), embed)
    try:
        result = await asyncio.wait_for(engine.inspect('web:default', 'owner', '拿铁'), 1)
        assert result['items'][0]['id'] == target['id']
        assert result['semantic']['queued_chunks'] == 1
        assert result['semantic']['embedded_chunks'] == 0
        await entered.wait()
        # Repeated foreground reads cannot multiply the same pending chunk.
        await engine.inspect('web:default', 'owner', '拿铁')
        assert engine.indexing_status()['pending_chunks'] == 1
        release.set()
        await engine.wait_for_index()
        result = await engine.inspect('web:default', 'owner', '最爱什么饮品')
        assert result['items'][0]['id'] == target['id'] and result['mode'] == 'hybrid'
        assert len([call for call in calls if call[1]]) == 1
    finally:
        release.set()
        await engine.aclose()
        store.close()


@pytest.mark.anyio
@pytest.mark.parametrize('operation', ['delete', 'pause', 'clear', 'edit'])
async def test_revocation_before_background_dispatch_prevents_sending_source(tmp_path, operation):
    store = Store(tmp_path)
    target = fact(store, '只能在授权期间检索')
    cfg, calls = config(), []
    async def embed(texts, **kwargs):
        calls.append(texts)
        return [[1., 0.] for _ in texts]
    engine = MemoryEngine(store, cfg, embed)
    try:
        await engine.inspect('web:default', 'owner', '授权')
        if operation == 'delete': store.delete_memory(target['id'])
        elif operation == 'pause': cfg['paused'] = True
        elif operation == 'clear': engine.clear_index()
        else: store.update_memory(target['id'], {'content': '修正后的内容'})
        await asyncio.sleep(.15)
        assert len(calls) == 1
        assert not engine.indexing_status()['pending_chunks']
    finally:
        await engine.aclose()
        store.close()


@pytest.mark.anyio
async def test_background_queue_is_bounded_and_shutdown_cancels_active_request(tmp_path):
    store = Store(tmp_path)
    entered = asyncio.Event()
    async def embed(texts, *, background=False, **kwargs):
        if background:
            entered.set()
            await asyncio.sleep(30)
        return [[1., 0.] for _ in texts]
    engine = MemoryEngine(store, config(), embed)
    try:
        for index in range(12):
            target = fact(store, f'项目{index}的独立内容')
            await engine.inspect('web:default', 'owner', str(index))
        assert engine.indexing_status()['queued_batches'] <= 8
        assert engine.indexing_status()['pending_chunks'] <= 9 * 12
        await entered.wait()
        await asyncio.wait_for(engine.aclose(), 1)
        assert not engine.indexing_status()['pending_chunks']
        assert not engine.indexing_status()['running']
    finally:
        await engine.aclose()
        store.close()


@pytest.mark.anyio
async def test_queued_background_request_rechecks_authority_before_http(tmp_path):
    store = Store(tmp_path)
    target = fact(store, '咖啡的私密偏好')
    entered, release = asyncio.Event(), asyncio.Event()
    sent = []
    async def embed(texts, *, background=False, before_send=None, **kwargs):
        if background:
            entered.set()
            await release.wait()
        if before_send and not before_send():
            raise PermissionError('revoked')
        sent.extend(texts)
        return [[1., 0.] for _ in texts]
    engine = MemoryEngine(store, config(), embed)
    try:
        assert (await engine.recall('web:default', 'owner', '咖啡'))[0]['id'] == target['id']
        await entered.wait()
        store.delete_memory(target['id'])
        release.set()
        await engine.wait_for_index()
        assert sent == ['咖啡']
        assert await engine.recall('web:default', 'owner', '咖啡') == []
    finally:
        release.set()
        await engine.aclose()
        store.close()


@pytest.mark.anyio
async def test_semantic_cached_old_fact_survives_sql_candidate_limit(tmp_path):
    store = Store(tmp_path)
    target = fact(store, '拿铁最好喝', importance=0)
    async def embed(texts, **kwargs):
        return [[1., 0.] if '拿铁' in text or '喜爱的饮品' in text else [0., 1.] for text in texts]
    engine = MemoryEngine(store, config(), embed)
    try:
        await engine.recall('web:default', 'owner', '拿铁')
        await engine.wait_for_index()
        for number in range(250):
            fact(store, f'不同的工作记录{number}', importance=1)
        assert target['id'] not in store.memory_candidate_ids('web:default', 'owner', '喜爱的饮品')
        result = await engine.recall('web:default', 'owner', '喜爱的饮品')
        assert result[0]['id'] == target['id']
    finally:
        await engine.aclose()
        store.close()


@pytest.mark.anyio
async def test_document_indexing_does_not_compete_with_foreground_reply(tmp_path):
    store = Store(tmp_path)
    fact(store, '喜欢音乐')
    busy, calls = [True], []
    async def embed(texts, *, background=False, **kwargs):
        calls.append(background)
        return [[1., 0.] for _ in texts]
    engine = MemoryEngine(store, config(), embed, foreground_busy=lambda: busy[0])
    try:
        await engine.recall('web:default', 'owner', '音乐')
        await asyncio.sleep(.2)
        assert calls == [False]
        busy[0] = False
        await asyncio.wait_for(engine.wait_for_index(), 1)
        assert calls == [False, True]
    finally:
        await engine.aclose()
        store.close()


def cache(engine, records):
    signature = engine._signature(engine._config(), 2)
    connection = engine._connection()
    with connection:
        for doc in _documents(records):
            digest = _digest(doc.embedding_text)
            connection.execute('INSERT OR REPLACE INTO vectors VALUES (?,?,?,?)', (signature, digest, 2, json.dumps([1., 0.])))
            connection.execute('INSERT OR REPLACE INTO vector_refs VALUES (?,?,?,?,?,?)',
                (doc.record['id'],doc.record['version'],doc.index,_record_hash(doc.record),signature,digest))
    return signature


def test_old_prune_cannot_recreate_index_after_clear(tmp_path, monkeypatch):
    store = Store(tmp_path)
    record = fact(store, '清理测试')
    engine = MemoryEngine(store, config())
    cache(engine, [record])
    original = store.get_memory
    def read_then_clear(identifier):
        result = original(identifier)
        engine.clear_index()
        return result
    monkeypatch.setattr(store, 'get_memory', read_then_clear)
    try:
        engine._prune_index()
        assert not engine.index_path.exists() and engine._index is None
    finally:
        engine.close()
        store.close()


def test_old_cache_batch_cannot_recreate_index_after_clear(tmp_path, monkeypatch):
    import shiori.memory as memory_module
    store = Store(tmp_path)
    records = [fact(store, f'缓存片段{number}') for number in range(70)]
    engine = MemoryEngine(store, config())
    signature = cache(engine, records)
    docs, generation = _documents(records), engine._generation
    original = memory_module._vectors
    def decode_then_clear(*args, **kwargs):
        result = original(*args, **kwargs)
        if engine._generation == generation:
            engine.clear_index()
        return result
    monkeypatch.setattr(memory_module, '_vectors', decode_then_clear)
    try:
        with pytest.raises(PermissionError):
            engine._cached_scores(docs, signature, 2, [1., 0.], set(), None, generation)
        assert not engine.index_path.exists() and engine._index is None
    finally:
        engine.close()
        store.close()


@pytest.mark.anyio
async def test_stale_high_scoring_vectors_cannot_crowd_out_valid_old_fact(tmp_path):
    store = Store(tmp_path)
    target = fact(store, '拿铁是珍贵旧记忆', importance=0)
    stale = [fact(store, f'过期向量{number}', importance=1) for number in range(70)]
    async def embed(texts, **kwargs):
        return [[1., 0.] for _ in texts]
    engine = MemoryEngine(store, config(), embed)
    try:
        cache(engine, [target, *stale])
        for record in stale:
            store.update_memory(record['id'], {'content': '现已更正，与旧向量无关'})
        for number in range(250):
            fact(store, f'无关近期记录{number}', importance=1)
        assert target['id'] not in store.memory_candidate_ids('web:default', 'owner', '最爱哪种饮品')
        result = await engine.recall('web:default', 'owner', '最爱哪种饮品')
        assert result and result[0]['id'] == target['id']
    finally:
        await engine.aclose()
        store.close()
