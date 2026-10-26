import asyncio
import copy
import importlib.util
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from mikubot.chat import ChatService
from mikubot.db import Store
from mikubot.memory import MemoryEngine
from mikubot.settings import DEFAULTS, Settings


@pytest.fixture
def anyio_backend():
    return 'asyncio'


def config(**memory):
    value = copy.deepcopy(DEFAULTS)
    value['memory'].update(semantic_enabled=True, embedding_model='embed', background_indexing=False, **memory)
    value['model'].update(base_url='https://fixture.invalid/v1', model='chat')
    return value


def fact(store, content='喜欢咖啡', **fields):
    return store.create_memory({'scope_id':'web:default', 'user_id':'owner', 'content':content, **fields})


@pytest.mark.anyio
@pytest.mark.parametrize('slow_phase', ['query', 'documents', 'lock'])
async def test_semantic_deadline_includes_query_documents_and_engine_lock(tmp_path, slow_phase):
    store = Store(tmp_path)
    wanted = fact(store)
    calls = []
    async def embed(texts, **kwargs):
        calls.append(texts)
        if slow_phase == 'query' or slow_phase == 'documents' and len(calls) == 2:
            await asyncio.sleep(10)
        return [[1., 0.] for _ in texts]
    engine = MemoryEngine(store, config(), embed)
    # Build the lexical index before timing the shared online budget. The first
    # pass over a cold index is local CPU work whose duration depends on the
    # host; this deadline is meant to bound the online embedding phases.
    await engine._rank_async('web:default', 'owner', engine._config(), '咖啡')
    if slow_phase == 'lock':
        await engine._semantic_lock.acquire()
    try:
        budget = .6
        started = asyncio.get_running_loop().time()
        result = await engine.inspect('web:default', 'owner', '咖啡', semantic_deadline=started + budget)
        assert asyncio.get_running_loop().time() - started < 3
        assert result['mode'] == 'lexical' and result['items'][0]['id'] == wanted['id']
        assert result['semantic']['error'] == 'TimeoutError'
        assert len(calls) == {'query': 1, 'documents': 2, 'lock': 0}[slow_phase]
        if slow_phase != 'lock':
            assert not engine._semantic_lock.locked()
    finally:
        if slow_phase == 'lock':
            engine._semantic_lock.release()
        engine.close()
        store.close()


@pytest.mark.anyio
async def test_external_cancellation_is_not_converted_to_lexical_reply(tmp_path):
    store = Store(tmp_path)
    fact(store)
    entered = asyncio.Event()
    async def embed(texts, **kwargs):
        entered.set()
        await asyncio.sleep(10)
    engine = MemoryEngine(store, config(), embed)
    task = asyncio.create_task(engine.inspect('web:default', 'owner', '咖啡'))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not engine._semantic_lock.locked() and engine._pending_reads == 0
    engine.close()
    store.close()


@pytest.mark.anyio
async def test_reply_queries_share_one_semantic_deadline(tmp_path):
    store = Store(tmp_path)
    wanted = fact(store)
    calls = []
    async def embed(texts, **kwargs):
        calls.append(texts)
        await asyncio.sleep(10)
    cfg = config(semantic_timeout_seconds=.04)
    engine = MemoryEngine(store, cfg, embed)
    chat = object.__new__(ChatService)
    chat.store, chat.memory = store, engine
    chat.settings = SimpleNamespace(get=lambda: cfg)
    chat.social = SimpleNamespace(_allowed=lambda scope_id: True)
    try:
        # Build the lexical index before timing the shared online budget.
        await engine._rank_async('web:default', 'owner', cfg, '咖啡')
        result = await chat._recall_context('web:default', 'owner', '咖啡', ['咖啡偏好', '咖啡记忆', '咖啡习惯'])
        assert len(calls) == 1 and wanted['id'] in {item['id'] for item in result}
    finally:
        engine.close()
        store.close()


def test_resource_settings_validate_atomically_and_roundtrip(tmp_path):
    settings = Settings(tmp_path)
    patch = {'memory': {'term_cache_mb': 0, 'read_concurrency': 1, 'read_queue_limit': 4, 'semantic_timeout_seconds': 8},
             'scheduler': {'global_model_concurrency': 1, 'request_max_mb': 12, 'parsing_queue_limit': 2},
             'context': {'max_input_chars': 16000, 'max_history_messages': 16, 'max_images': 1},
             'model': {'stream_mode': 'on', 'first_token_timeout_seconds': 8, 'total_timeout_seconds': 60, 'timeout_fallback': True}}
    settings.update(patch)
    reloaded = Settings(tmp_path)
    for section, fields in patch.items():
        for key, value in fields.items():
            assert reloaded.get()[section][key] == value
    before = settings.get()
    for section, key, bad in [('memory','term_cache_mb',-1),('memory','read_concurrency',True),
                              ('memory','read_queue_limit',0),('memory','semantic_timeout_seconds',0),
                              ('scheduler','global_model_concurrency',17),('scheduler','request_max_mb',65),
                              ('context','max_input_chars',1999),('context','max_history_messages',0),
                              ('context','max_images',4),('model','stream_mode','invalid'),
                              ('model','timeout_fallback','yes'),('model','total_timeout_seconds',1801),
                              ('model','first_token_timeout_seconds',True)]:
        with pytest.raises(ValueError):
            settings.update({section: {key: bad}})
        assert settings.get() == before


def test_memory_resources_apply_at_idle_and_cache_can_be_disabled(tmp_path):
    store = Store(tmp_path)
    fact(store)
    cfg = config(term_cache_mb=0, read_concurrency=1, read_queue_limit=4)
    engine = MemoryEngine(store, cfg)
    asyncio.run(engine.recall_local('web:default', 'owner', '咖啡'))
    assert engine._read_capacity == 1 and engine._term_cache.bytes == 0
    cfg['memory'].update(term_cache_mb=1, read_concurrency=2)
    asyncio.run(engine.recall_local('web:default', 'owner', '咖啡'))
    assert engine._read_capacity == 2 and 0 < engine._term_cache.bytes <= 1024 * 1024
    cfg['memory']['term_cache_mb'] = 0
    asyncio.run(engine.recall_local('web:default', 'owner', '咖啡'))
    assert engine._term_cache.bytes == 0
    engine.close()
    store.close()


def test_organization_and_extraction_projection_do_not_load_all_bodies(tmp_path, monkeypatch):
    store = Store(tmp_path)
    wanted = fact(store)
    fact(store, '其他用户的事实', user_id='other')
    engine = MemoryEngine(store, copy.deepcopy(DEFAULTS))
    monkeypatch.setattr(store, 'list_memories', lambda *a, **kw: pytest.fail('unbounded memory read'))
    assert store.user_memory_versions('web:default', 'owner') == {wanted['id']: 1}
    assert [item['id'] for item in store.user_memory_records('web:default', 'owner', limit=1)] == [wanted['id']]
    assert engine.propose_organization('web:default', 'owner')['created'] == 0
    engine.close()
    store.close()


def test_chat_context_budget_preserves_request_and_system_and_rejects_oversize():
    chat = object.__new__(ChatService)
    chat.settings = SimpleNamespace(get=lambda: {'context': {'max_input_chars': 2000}})
    system, current = {'role': 'system', 'content': 'identity'}, {'role': 'user', 'content': 'latest request'}
    result = chat._input_budget([system, {'role': 'assistant', 'content': 'old' * 1000}, current])
    assert result == [system, current]
    with pytest.raises(ValueError, match='本次未调用模型'):
        chat._input_budget([system, {'role':'user', 'content':'x' * 2000}])


def test_resource_sampler_reads_only_selected_fields(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location('resource_sampler', Path(__file__).parents[1] / 'scripts/sample_resources.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    proc, cgroup = tmp_path / 'proc', tmp_path / 'cgroup'
    proc.mkdir()
    cgroup.mkdir()
    (proc / 'meminfo').write_text('MemTotal: 2000 kB\nMemAvailable: 1000 kB\n')
    (cgroup / 'memory.current').write_text('1234')
    (cgroup / 'memory.max').write_text('max')
    (cgroup / 'memory.events').write_text('oom 2\noom_kill 1\n')
    value = module.linux_sample(proc_root=proc, cgroup_root=cgroup)
    assert value['host_memory']['MemTotal'] == 2000 * 1024
    assert value['cgroup']['memory.current'] == 1234 and value['cgroup']['memory.max'] is None
    assert value['cgroup']['memory.events']['oom_kill'] == 1


def test_resource_controls_require_explicit_save():
    node = shutil.which('node')
    if not node:
        pytest.skip('Node unavailable')
    result = subprocess.run([node, str(Path(__file__).with_name('frontend_resources.cjs'))], capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stdout + result.stderr
