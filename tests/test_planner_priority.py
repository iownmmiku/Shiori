"""Queued replies precede planning, without changing scheduling limits."""
import asyncio
import copy
from types import SimpleNamespace

import pytest

from mikubot.provider import Provider, ProviderError
from mikubot.settings import DEFAULTS


@pytest.fixture
def anyio_backend():
    return 'asyncio'


@pytest.fixture
def provider():
    config = copy.deepcopy(DEFAULTS)
    config['scheduler']['global_model_concurrency'] = 1
    return Provider(SimpleNamespace(get=lambda: config), SimpleNamespace())


async def settle():
    await asyncio.sleep(0)
    await asyncio.sleep(0)


def worker(provider, key, name, purpose, order, entered, release):
    async def run():
        async with provider._provider_slot({'id': key}, purpose):
            order.append(name)
            entered.set()
            await release.wait()
    return asyncio.create_task(run())


async def cleanup(tasks):
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)


def assert_empty(provider):
    assert not provider._scheduler.waiting
    assert provider._global_gate.active == provider._background_gate.active == 0
    assert not provider._global_gate.queue and not provider._background_gate.queue
    assert all(gate.active == 0 and not gate.queue and not gate.locked()
               for gate in provider._provider_locks.values())


@pytest.mark.anyio
@pytest.mark.parametrize('purpose', ['chat', 'computer', 'tool_summary'])
@pytest.mark.parametrize('same_model', [False, True])
async def test_queued_reply_passes_planner_without_preempting_active_request(provider, purpose, same_model):
    order, tasks = [], []
    active_entered, planner_entered, reply_entered = (asyncio.Event() for _ in range(3))
    release_active, release_reply, release_planner = (asyncio.Event() for _ in range(3))
    key = 'active' if same_model else 'other'
    try:
        tasks.append(worker(provider, 'active', 'active', 'planner', order, active_entered, release_active))
        await asyncio.wait_for(active_entered.wait(), 1)
        tasks.append(worker(provider, key, 'planner', 'planner', order, planner_entered, release_planner))
        await settle()
        tasks.append(worker(provider, key, 'reply', purpose, order, reply_entered, release_reply))
        await settle()
        assert order == ['active'] and not tasks[0].done()
        assert not planner_entered.is_set() and not reply_entered.is_set()

        release_active.set()
        await asyncio.wait_for(reply_entered.wait(), 1)
        assert order == ['active', 'reply'] and not planner_entered.is_set()
        assert provider._global_gate.active == 1
        release_reply.set()
        await asyncio.wait_for(planner_entered.wait(), 1)
        release_planner.set()
        await asyncio.gather(*tasks)
        assert order == ['active', 'reply', 'planner']
        assert_empty(provider)
    finally:
        await cleanup(tasks)


@pytest.mark.anyio
async def test_planning_precedes_background_and_keeps_fifo_within_priority(provider):
    active = await provider._scheduler.acquire('active', False)
    order, tasks, release = [], [], asyncio.Event()
    release.set()
    try:
        for name, purpose in [('background', 'memory_extract'), ('plan-a', 'planner'),
                              ('plan-b', 'planner'), ('reply-a', 'chat'), ('reply-b', 'chat')]:
            tasks.append(worker(provider, 'shared', name, purpose, order, asyncio.Event(), release))
            await settle()
        provider._scheduler.release(active)
        await asyncio.gather(*tasks)
        assert order == ['reply-a', 'reply-b', 'plan-a', 'plan-b', 'background']
        assert_empty(provider)
    finally:
        provider._scheduler.release(active)
        await cleanup(tasks)


@pytest.mark.anyio
async def test_cancelled_queued_and_active_plans_release_slots(provider):
    active = await provider._scheduler.acquire('shared', False)
    order, tasks, release = [], [], asyncio.Event()
    entered = asyncio.Event()
    try:
        queued = worker(provider, 'shared', 'cancelled', 'planner', order, asyncio.Event(), release)
        tasks.append(queued)
        await settle()
        queued.cancel()
        with pytest.raises(asyncio.CancelledError):
            await queued
        assert not provider._scheduler.waiting and order == []

        plan = worker(provider, 'shared', 'active-plan', 'planner', order, entered, release)
        tasks.append(plan)
        await settle()
        provider._scheduler.release(active)
        await asyncio.wait_for(entered.wait(), 1)
        plan.cancel()
        with pytest.raises(asyncio.CancelledError):
            await plan
        assert_empty(provider)
    finally:
        provider._scheduler.release(active)
        await cleanup(tasks)


@pytest.mark.anyio
async def test_cancelled_planner_grant_before_resume_returns_its_atomic_slots(provider):
    active = await provider._scheduler.acquire('shared', False)
    order, release = [], asyncio.Event()
    plan = worker(provider, 'shared', 'planner', 'planner', order, asyncio.Event(), release)
    try:
        await settle()
        provider._scheduler.release(active)
        # The future has its slot, but the caller has not resumed into its body.
        plan.cancel()
        with pytest.raises(asyncio.CancelledError):
            await plan
        assert order == []
        assert_empty(provider)
    finally:
        provider._scheduler.release(active)
        await cleanup([plan])


@pytest.mark.anyio
async def test_planner_is_foreground_for_background_capacity_and_status(provider):
    provider.settings.get()['scheduler']['global_model_concurrency'] = 2
    active = await provider._scheduler.acquire('background-a', True)
    order, tasks, release = [], [], asyncio.Event()
    planner_entered, background_entered = asyncio.Event(), asyncio.Event()
    try:
        tasks.append(worker(provider, 'background-b', 'background', 'memory_extract',
                            order, background_entered, release))
        await settle()
        tasks.append(worker(provider, 'planner', 'planner', 'planner', order, planner_entered, release))
        await asyncio.wait_for(planner_entered.wait(), 1)
        assert not background_entered.is_set()
        assert provider._background_gate.active == 1 and provider._global_gate.active == 2
        tasks.append(worker(provider, 'planner', 'queued-planner', 'planner',
                            order, asyncio.Event(), release))
        await settle()
        status = provider.status()['scheduler']
        assert status['global_queued'] == 2
        assert status['background_queued'] == 1 and status['foreground_queued'] == 1
        release.set()
        # Freeing the real background slot is what permits the queued background job.
        provider._scheduler.release(active)
        await asyncio.gather(*tasks)
        assert background_entered.is_set()
        assert_empty(provider)
    finally:
        provider._scheduler.release(active)
        await cleanup(tasks)


@pytest.mark.anyio
@pytest.mark.parametrize('limit', ['model_queue_limit', 'global_model_queue_limit'])
async def test_planner_priority_does_not_bypass_queue_capacity(provider, limit):
    provider.settings.get()['scheduler'][limit] = 1
    active = await provider._scheduler.acquire('shared', False)
    tasks, release = [], asyncio.Event()
    try:
        tasks.append(worker(provider, 'shared', 'planner', 'planner', [], asyncio.Event(), release))
        await settle()
        with pytest.raises(ProviderError, match='队列已满'):
            async with provider._provider_slot({'id': 'shared'}, 'planner'):
                pytest.fail('Full queue admitted a planner')
        assert len(provider._scheduler.waiting) == 1
        assert provider._global_gate.active == 1 and provider._provider_locks['shared'].active == 1
    finally:
        await cleanup(tasks)
        provider._scheduler.release(active)
        assert_empty(provider)
