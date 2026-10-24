"""Atomic model scheduling and queue/transport latency, using offline fixtures."""
import asyncio
import copy
from types import SimpleNamespace

import httpx
import pytest

from mikubot.db import Store
from mikubot.provider import Provider, ProviderError, ProviderTimeoutError
from mikubot.settings import DEFAULTS, Settings, resolve_model


@pytest.fixture
def anyio_backend():
    return 'asyncio'


@pytest.fixture
def provider():
    config = copy.deepcopy(DEFAULTS)
    return Provider(SimpleNamespace(get=lambda: config), SimpleNamespace())


async def settled():
    # Put queued tasks into acquire() without waiting for wall-clock time.
    await asyncio.sleep(0)
    await asyncio.sleep(0)


async def cleanup(tasks):
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)


def empty(provider):
    assert provider._global_gate.active == 0
    assert provider._background_gate.active == 0
    assert provider._scheduler.waiting == []
    assert provider._global_gate.queue == []
    assert provider._background_gate.queue == []
    assert all(g.active == 0 and not g.queue and not g.locked() for g in provider._provider_locks.values())


def start_worker(provider, key, name, entered, release, order, purpose='chat'):
    async def work():
        async with provider._provider_slot({'id': key}, purpose):
            order.append(name)
            entered.set()
            await release.wait()
    return asyncio.create_task(work())


@pytest.mark.anyio
async def test_waiting_same_model_does_not_occupy_global_capacity(provider):
    first, second, independent = asyncio.Event(), asyncio.Event(), asyncio.Event()
    release_a, release_b, order, tasks = asyncio.Event(), asyncio.Event(), [], []
    try:
        tasks.append(start_worker(provider, 'a', 'a1', first, release_a, order))
        await first.wait()
        tasks.append(start_worker(provider, 'a', 'a2', second, release_a, order))
        await settled()
        assert provider._global_gate.active == 1 and not second.is_set()
        tasks.append(start_worker(provider, 'b', 'b1', independent, release_b, order))
        await asyncio.wait_for(independent.wait(), 1)
        assert order == ['a1', 'b1'] and provider._global_gate.active == 2
        assert provider._provider_locks['a'].active == 1
        release_b.set()
        release_a.set()
        await asyncio.gather(*tasks)
        assert order == ['a1', 'b1', 'a2']
        empty(provider)
    finally:
        await cleanup(tasks)


@pytest.mark.anyio
async def test_queued_background_cannot_reserve_model_ahead_of_foreground(provider):
    provider.settings.get()['scheduler']['global_model_concurrency'] = 1
    active, bg, fg = asyncio.Event(), asyncio.Event(), asyncio.Event()
    release, order, tasks = asyncio.Event(), [], []
    try:
        tasks.append(start_worker(provider, 'a', 'active', active, release, order))
        await active.wait()
        tasks.append(start_worker(provider, 'b', 'background', bg, release, order, 'distillation'))
        await settled()
        assert provider._provider_locks['b'].active == 0
        tasks.append(start_worker(provider, 'b', 'foreground', fg, release, order))
        await settled()
        release.set()
        await asyncio.gather(*tasks)
        assert order == ['active', 'foreground', 'background']
        empty(provider)
    finally:
        await cleanup(tasks)


@pytest.mark.anyio
async def test_background_limit_does_not_hold_global_or_model_capacity(provider):
    active, bg, fg = asyncio.Event(), asyncio.Event(), asyncio.Event()
    release, order, tasks = asyncio.Event(), [], []
    try:
        tasks.append(start_worker(provider, 'a', 'background-a', active, release, order, 'memory_extract'))
        await active.wait()
        tasks.append(start_worker(provider, 'b', 'background-b', bg, release, order, 'sticker_select'))
        await settled()
        tasks.append(start_worker(provider, 'b', 'foreground-b', fg, release, order))
        await asyncio.wait_for(fg.wait(), 1)
        assert order == ['background-a', 'foreground-b']
        assert provider._background_gate.active == 1 and provider._global_gate.active == 2
        assert not bg.is_set()
        release.set()
        await asyncio.gather(*tasks)
        assert order == ['background-a', 'foreground-b', 'background-b']
        empty(provider)
    finally:
        await cleanup(tasks)


@pytest.mark.anyio
async def test_running_background_is_never_preempted(provider):
    active, foreground = asyncio.Event(), asyncio.Event()
    release, order, tasks = asyncio.Event(), [], []
    try:
        tasks.append(start_worker(provider, 'a', 'background', active, release, order, 'knowledge'))
        await active.wait()
        tasks.append(start_worker(provider, 'a', 'foreground', foreground, release, order))
        await settled()
        assert not foreground.is_set() and not tasks[0].done()
        release.set()
        await asyncio.gather(*tasks)
        assert order == ['background', 'foreground']
        empty(provider)
    finally:
        await cleanup(tasks)


@pytest.mark.anyio
async def test_same_priority_keeps_fifo_among_ready_requests(provider):
    provider.settings.get()['scheduler']['global_model_concurrency'] = 1
    active = await provider._scheduler.acquire('a', False)
    order, tasks, release = [], [], asyncio.Event()
    release.set()
    try:
        for key in ('b', 'c', 'd'):
            tasks.append(start_worker(provider, key, key, asyncio.Event(), release, order))
            await settled()
        provider._scheduler.release(active)
        await asyncio.gather(*tasks)
        assert order == ['b', 'c', 'd']
        empty(provider)
    finally:
        provider._scheduler.release(active)
        await cleanup(tasks)


@pytest.mark.anyio
async def test_capacity_increase_admits_queued_models_on_next_scheduling_event(provider):
    cfg = provider.settings.get()['scheduler']
    cfg['global_model_concurrency'] = 1
    active = await provider._scheduler.acquire('a', False)
    a2 = asyncio.create_task(provider._scheduler.acquire('a', False))
    b = asyncio.create_task(provider._scheduler.acquire('b', False))
    c = None
    try:
        await settled()
        cfg.update(global_model_concurrency=3, model_concurrency=2)
        # A new arrival refreshes limits without reserving a model prematurely.
        c = asyncio.create_task(provider._scheduler.acquire('c', False))
        await settled()
        assert a2.done() and b.done() and not c.done()
        assert provider._global_gate.active == 3 and provider._provider_locks['a'].active == 2
        provider._scheduler.release(active)
        await asyncio.wait_for(asyncio.shield(c), 1)
        for task in (a2, b, c):
            provider._scheduler.release(task.result())
        empty(provider)
    finally:
        provider._scheduler.release(active)
        await cleanup([a2, b, *([c] if c else [])])


@pytest.mark.anyio
async def test_lowered_capacity_does_not_cancel_active_requests_or_overgrant(provider):
    cfg = provider.settings.get()['scheduler']
    cfg.update(global_model_concurrency=3, model_concurrency=3)
    active = [await provider._scheduler.acquire('a', False) for _ in range(3)]
    cfg.update(global_model_concurrency=1, model_concurrency=1)
    queued = asyncio.create_task(provider._scheduler.acquire('a', False))
    try:
        await settled()
        assert provider._global_gate.active == 3 and not queued.done()
        for entry in active[:2]:
            provider._scheduler.release(entry)
            await settled()
            assert not queued.done()
        provider._scheduler.release(active[-1])
        next_entry = await asyncio.wait_for(asyncio.shield(queued), 1)
        assert provider._global_gate.active == 1
        provider._scheduler.release(next_entry)
        empty(provider)
    finally:
        for entry in active:
            provider._scheduler.release(entry)
        await cleanup([queued])


@pytest.mark.anyio
@pytest.mark.parametrize('limit', ['model', 'global', 'background'])
async def test_queue_limits_remain_bounded_before_dispatch(provider, limit):
    cfg = provider.settings.get()['scheduler']
    cfg.update(model_queue_limit=1, global_model_queue_limit=8, background_queue_limit=1)
    if limit == 'global':
        cfg.update(global_model_concurrency=1, global_model_queue_limit=1)
    background = limit == 'background'
    active = await provider._scheduler.acquire('a', background)
    next_key = 'a' if limit == 'model' else 'b'
    queued = asyncio.create_task(provider._scheduler.acquire(next_key, background))
    try:
        await settled()
        assert not queued.done()
        with pytest.raises(ProviderError, match='队列已满'):
            await provider._scheduler.acquire(next_key if limit == 'model' else 'c', background)
        assert len(provider._scheduler.waiting) == 1
        assert provider._global_gate.active == 1
    finally:
        await cleanup([queued])
        provider._scheduler.release(active)
        empty(provider)


@pytest.mark.anyio
async def test_repeated_waiter_cancellation_frees_all_queue_views(provider):
    active = await provider._scheduler.acquire('a', True)
    try:
        for _ in range(100):
            task = asyncio.create_task(provider._scheduler.acquire('a', True))
            await settled()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert not provider._scheduler.waiting
            assert not provider._global_gate.queue and not provider._background_gate.queue
            assert not provider._provider_locks['a'].queue
        assert provider._global_gate.active == 1 and provider._background_gate.active == 1
    finally:
        provider._scheduler.release(active)
        empty(provider)


@pytest.mark.anyio
async def test_cancellation_after_grant_before_resume_releases_all_capacity(provider):
    active = await provider._scheduler.acquire('a', True)
    task = asyncio.create_task(provider._scheduler.acquire('a', True))
    await settled()
    provider._scheduler.release(active)
    # Grant has happened synchronously, but acquire() has not resumed yet.
    assert provider._global_gate.active == 1
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    empty(provider)
    entry = await provider._scheduler.acquire('a', True)
    provider._scheduler.release(entry)
    empty(provider)


@pytest.mark.anyio
async def test_cancellation_while_running_releases_exactly_once(provider):
    entered, release, tasks = asyncio.Event(), asyncio.Event(), []
    try:
        task = start_worker(provider, 'a', 'active', entered, release, [], 'memory_extract')
        tasks.append(task)
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        empty(provider)
    finally:
        await cleanup(tasks)


@pytest.mark.anyio
@pytest.mark.parametrize('deadline', [False, True])
async def test_cancelled_or_expired_wait_is_in_turn_timing_without_capacity_leak(provider, monkeypatch, deadline):
    from mikubot import provider as module
    from mikubot.timing import TurnTiming
    clock, traces = [1.0], []
    monkeypatch.setattr(module, 'time', SimpleNamespace(monotonic=lambda: clock[0]))
    active = await provider._scheduler.acquire('a', False)
    async def queued():
        trace = TurnTiming()
        traces.append(trace)
        with trace.activate():
            async with asyncio.timeout(.02 if deadline else None):
                async with provider._provider_slot({'id': 'a'}):
                    pytest.fail('cancelled/expired waiter must not be dispatched')
    task = asyncio.create_task(queued())
    try:
        await settled()
        assert len(provider._scheduler.waiting) == 1
        clock[0] = 4.0
        if not deadline:
            task.cancel()
        with pytest.raises(TimeoutError if deadline else asyncio.CancelledError):
            await task
        assert traces[0].snapshot()['model_queue_ms'] == 3000
        assert provider._global_gate.active == 1
        assert not provider._scheduler.waiting and not provider._global_gate.queue
    finally:
        provider._scheduler.release(active)
        await cleanup([task])
        empty(provider)


@pytest.mark.anyio
@pytest.mark.parametrize('global_limit,model_limit', [(1, 1), (4, 2)])
async def test_mixed_workload_never_exceeds_any_resource_limit(provider, global_limit, model_limit):
    provider.settings.get()['scheduler'].update(global_model_concurrency=global_limit, model_concurrency=model_limit)
    seen = []
    async def work(index):
        key = f'model-{index % 4}'
        async with provider._provider_slot({'id': key}, 'memory_extract' if index % 3 == 0 else 'chat'):
            seen.append(index)
            assert 1 <= provider._global_gate.active <= global_limit
            assert 0 <= provider._background_gate.active <= 1
            assert all(g.active <= model_limit for g in provider._provider_locks.values())
            await asyncio.sleep(0)
    await asyncio.gather(*(work(index) for index in range(30)))
    assert len(seen) == 30
    empty(provider)


@pytest.mark.anyio
async def test_existing_status_fields_describe_all_waiters_without_reservations(provider):
    active = await provider._scheduler.acquire('a', True)
    background = asyncio.create_task(provider._scheduler.acquire('b', True))
    foreground = asyncio.create_task(provider._scheduler.acquire('a', False))
    try:
        await settled()
        status = provider.status()['scheduler']
        assert status == {'background_concurrency': 1, 'background_running': True,
            'global_concurrency': 2, 'global_running': 1, 'global_queued': 2,
            'background_queued': 1, 'foreground_queued': 1}
        assert provider._provider_locks['b'].active == 0
    finally:
        await cleanup([background, foreground])
        provider._scheduler.release(active)
        empty(provider)


@pytest.fixture
def actual_provider(tmp_path):
    store, settings = Store(tmp_path), Settings(tmp_path)
    settings.update({'model': {'base_url': 'https://fixture.invalid/v1', 'model': 'fixture', 'stream_mode': 'off'}})
    try:
        yield Provider(settings, store), settings, store
    finally:
        store.close()


def transport(monkeypatch, handler):
    original = httpx.AsyncClient
    monkeypatch.setattr(httpx, 'AsyncClient', lambda **kwargs: original(transport=httpx.MockTransport(handler), **kwargs))


@pytest.mark.anyio
@pytest.mark.parametrize('embedding', [False, True])
async def test_real_usage_separates_queue_wait_from_transport_latency(actual_provider, monkeypatch, embedding):
    from mikubot import provider as module
    provider, settings, store = actual_provider
    clock = [0.0]
    monkeypatch.setattr(module, 'time', SimpleNamespace(monotonic=lambda: clock[0]))
    def handler(request):
        clock[0] += .012
        data = {'data': [{'index': 0, 'embedding': [1.0, 2.0]}], 'usage': {'prompt_tokens': 5}} if embedding else {
            'choices': [{'message': {'content': 'ok'}}], 'usage': {'prompt_tokens': 5, 'completion_tokens': 2}}
        return httpx.Response(200, json=data)
    transport(monkeypatch, handler)
    cfg = None
    if embedding:
        settings.update({'providers': [{'id': 'embedding-fixture', 'name': '嵌入测试',
            'base_url': 'https://fixture.invalid/v1', 'model': 'embed', 'model_type': 'embedding'}]})
        cfg = resolve_model(settings.get(), model_id='embedding-fixture')
    active = await provider._scheduler.acquire(cfg['id'] if cfg else 'default', False)
    task = asyncio.create_task(provider.embed(['hello'], config=cfg) if embedding else provider.complete([{'role': 'user', 'content': 'hello'}]))
    try:
        await settled()
        assert not task.done()
        clock[0] = 7.0
        provider._scheduler.release(active)
        result = await task
        usage = store.list_usage()[0]
        assert usage['queue_wait_ms'] == 7000
        assert usage['latency_ms'] == 12
        if not embedding:
            assert result['usage']['queue_wait_ms'] == 7000
        empty(provider)
    finally:
        provider._scheduler.release(active)
        await cleanup([task])


@pytest.mark.anyio
async def test_uncertain_dispatched_usage_retains_queue_wait(actual_provider, monkeypatch):
    from mikubot import provider as module
    provider, _, store = actual_provider
    clock = [0.0]
    monkeypatch.setattr(module, 'time', SimpleNamespace(monotonic=lambda: clock[0]))
    def handler(request):
        raise httpx.ReadTimeout('fixture timeout', request=request)
    transport(monkeypatch, handler)
    active = await provider._scheduler.acquire('default', False)
    task = asyncio.create_task(provider.complete([{'role': 'user', 'content': 'hello'}]))
    try:
        await settled()
        clock[0] = 2.0
        provider._scheduler.release(active)
        with pytest.raises(ProviderTimeoutError):
            await task
        usage = store.list_usage()[0]
        assert usage['queue_wait_ms'] == 2000 and usage['unconfirmed']
        assert not provider._reservations
        empty(provider)
    finally:
        provider._scheduler.release(active)
        await cleanup([task])


@pytest.mark.anyio
async def test_guard_is_revalidated_after_queue_without_network_or_budget_reservation(actual_provider, monkeypatch):
    provider, _, store = actual_provider
    allowed = [True]
    transport(monkeypatch, lambda request: pytest.fail('stale request must not dispatch'))
    active = await provider._scheduler.acquire('default', False)
    task = asyncio.create_task(provider.complete([{'role': 'user', 'content': 'hello'}], before_send=lambda: allowed[0]))
    try:
        await settled()
        allowed[0] = False
        provider._scheduler.release(active)
        with pytest.raises(ProviderError, match='来源、权限或模型配置已变化'):
            await task
        assert store.list_usage() == [] and not provider._reservations
        empty(provider)
    finally:
        provider._scheduler.release(active)
        await cleanup([task])


@pytest.mark.anyio
async def test_cancellation_while_queued_creates_no_charge_or_reservation(actual_provider, monkeypatch):
    provider, _, store = actual_provider
    transport(monkeypatch, lambda request: pytest.fail('cancelled waiter must not dispatch'))
    active = await provider._scheduler.acquire('default', False)
    task = asyncio.create_task(provider.complete([{'role': 'user', 'content': 'hello'}]))
    try:
        await settled()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert store.list_usage() == [] and not provider._reservations
        assert provider._global_gate.active == 1 and not provider._global_gate.queue
    finally:
        provider._scheduler.release(active)
        await cleanup([task])
        empty(provider)
