"""Durable extraction pacing, strict quotas and shared retry scheduling."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

import mikubot.db as db_module
import mikubot.memory_lifecycle as lifecycle_module
from mikubot.db import Store
from mikubot.memory_lifecycle import MemoryLifecycle
from mikubot.settings import DEFAULTS


@pytest.fixture
def anyio_backend():
    return 'asyncio'


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    clock = [datetime(2026, 10, 25, 4, tzinfo=timezone.utc)]
    monkeypatch.setattr(lifecycle_module, '_now', lambda: clock[0])
    monkeypatch.setattr(db_module, 'utc_now', lambda: clock[0].isoformat(timespec='microseconds').replace('+00:00', 'Z'))
    config = deepcopy(DEFAULTS)
    config['model'].update(base_url='https://fixture.invalid/v1', model='fixture')
    config['memory'].update(daily_extractions=60, extraction_messages=2, extraction_idle_seconds=180)
    store = Store(tmp_path)
    chat = SimpleNamespace(store=store, settings=SimpleNamespace(get=lambda: deepcopy(config)), _jobs=set())
    lifecycle = MemoryLifecycle(chat)
    env = SimpleNamespace(clock=clock, config=config, store=store, chat=chat, lifecycle=lifecycle)
    try:
        yield env
    finally:
        store.close()


def claim_count(store):
    return store._conn.execute("SELECT COUNT(*) FROM events WHERE kind='memory_extract_attempt'").fetchone()[0]


def seed(env, user='42', count=2, *, age=timedelta(hours=1)):
    now = env.clock[0]
    env.clock[0] -= age
    try:
        return [env.store.add_message('web:default', user, 'user', f'有效消息 {index}') for index in range(count)]
    finally:
        env.clock[0] = now


def test_automatic_claims_are_paced_globally_and_survive_restart(runtime):
    env = runtime
    assert env.lifecycle.claim('web:default', '42', automatic=True)
    assert not env.lifecycle.claim('qq:group:2', '43', automatic=True)
    env.lifecycle = MemoryLifecycle(env.chat)
    env.clock[0] += timedelta(minutes=23, seconds=59)
    assert not env.lifecycle.allowed(automatic=True)
    env.clock[0] += timedelta(seconds=1)
    assert env.lifecycle.claim('qq:group:2', '43', automatic=True)
    env.clock[0] += timedelta(hours=8)
    assert env.lifecycle.claim('web:default', '42', automatic=True)
    assert not env.lifecycle.claim('web:default', '42', automatic=True)
    assert claim_count(env.store) == 3


def test_manual_extraction_bypasses_pacing_but_consumes_the_same_cap(runtime):
    env = runtime
    env.config['memory']['daily_extractions'] = 3
    assert env.lifecycle.claim('web:default', '42', automatic=True)
    assert env.lifecycle.claim('web:default', '42')
    assert env.lifecycle.claim('web:default', '42')
    env.clock[0] += timedelta(hours=9)
    assert not env.lifecycle.claim('web:default', '42')
    assert not env.lifecycle.claim('web:default', '42', automatic=True)
    assert claim_count(env.store) == 3


def test_spent_sixty_claims_stay_spent_until_local_midnight(runtime):
    env = runtime
    # Hong Kong/Shanghai 00:19, followed by the remainder of that same day.
    env.clock[0] = datetime(2026, 10, 24, 16, 19, tzinfo=timezone.utc)
    for _ in range(60):
        assert env.lifecycle.claim('web:default', '42')
    before = list(env.store._conn.execute("SELECT id,created_at,data FROM events WHERE kind='memory_extract_attempt'"))
    env.clock[0] = datetime(2026, 10, 25, 15, 59, 59, tzinfo=timezone.utc)
    env.lifecycle = MemoryLifecycle(env.chat)
    assert not env.lifecycle.allowed(automatic=True)
    assert not env.lifecycle.claim('web:default', '42')
    assert list(env.store._conn.execute("SELECT id,created_at,data FROM events WHERE kind='memory_extract_attempt'")) == before
    env.clock[0] += timedelta(seconds=1)
    assert env.lifecycle.claim('web:default', '42', automatic=True)
    assert not env.lifecycle.claim('web:default', '42', automatic=True)
    assert claim_count(env.store) == 61


def test_midnight_does_not_remove_spacing_from_a_recent_previous_day_claim(runtime):
    env = runtime
    env.clock[0] = datetime(2026, 10, 25, 15, 50, tzinfo=timezone.utc)
    assert env.lifecycle.claim('web:default', '42', automatic=True)
    env.clock[0] += timedelta(minutes=10)
    assert not env.lifecycle.claim('web:default', '42', automatic=True)
    env.clock[0] += timedelta(minutes=14)
    assert env.lifecycle.claim('web:default', '42', automatic=True)


@pytest.mark.parametrize('at,day_hours', [
    (datetime(2026, 3, 8, 5, tzinfo=timezone.utc), 23),
    (datetime(2026, 11, 1, 4, tzinfo=timezone.utc), 25),
])
def test_spacing_uses_the_actual_configured_calendar_day_on_dst(runtime, at, day_hours):
    env = runtime
    env.config['time']['timezone'] = 'America/New_York'
    env.clock[0] = at
    assert env.lifecycle.claim('web:default', '42', automatic=True)
    env.clock[0] += timedelta(seconds=day_hours * 3600 / 60 - 1)
    assert not env.lifecycle.claim('web:default', '42', automatic=True)
    env.clock[0] += timedelta(seconds=1)
    assert env.lifecycle.claim('web:default', '42', automatic=True)


def test_legacy_usage_and_zero_quota_also_block_claims(runtime):
    env = runtime
    env.config['memory']['daily_extractions'] = 2
    for _ in range(2):
        env.store.record_usage({'purpose': 'memory_extract'})
    assert not env.lifecycle.claim('web:default', '42')
    assert claim_count(env.store) == 0
    env.clock[0] += timedelta(days=1)
    env.config['memory']['daily_extractions'] = 0
    assert not env.lifecycle.allowed()
    assert not env.lifecycle.claim('web:default', '42', automatic=True)
    assert claim_count(env.store) == 0


def test_concurrent_manual_claims_cannot_exceed_remaining_daily_cap(runtime):
    env = runtime
    env.config['memory']['daily_extractions'] = 2
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda user: env.lifecycle.claim('web:default', str(user)), range(20)))
    assert sum(results) == 2
    assert claim_count(env.store) == 2


def test_concurrent_automatic_claims_are_atomic_across_connections(runtime):
    env = runtime
    stores = [Store(env.store.data_dir) for _ in range(4)]
    lifecycles = [MemoryLifecycle(SimpleNamespace(store=store, settings=env.chat.settings)) for store in stores]
    try:
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(lambda item: item[1].claim('web:default', str(item[0]), automatic=True), enumerate(lifecycles)))
        assert sum(results) == 1
        assert claim_count(env.store) == 1
    finally:
        for store in stores:
            store.close()


def test_failure_backoff_is_persistent_exponential_and_clears_only_on_success(runtime):
    env = runtime
    records = seed(env)
    for minutes in (5, 10, 20, 40, 60, 60):
        env.lifecycle.attempt('web:default', '42', 'TimeoutError')
        env.lifecycle = MemoryLifecycle(env.chat)
        assert not env.lifecycle.ready('web:default', '42')
        env.clock[0] += timedelta(minutes=minutes, seconds=-1)
        assert not env.lifecycle.ready('web:default', '42')
        env.clock[0] += timedelta(seconds=1)
        assert env.lifecycle.ready('web:default', '42')
        env.lifecycle.attempt('web:default', '42')
    assert env.lifecycle._details('web:default', '42')['failure_count'] == 6
    env.lifecycle.advance('web:default', '42', records, automatic=True)
    assert env.lifecycle.ready('web:default', '42')
    assert env.lifecycle._details('web:default', '42')['failure_count'] == 0
    assert env.lifecycle._state('web:default', '42')[3] == ''


@pytest.mark.anyio
async def test_observe_and_poll_share_backoff_and_neither_reads_pending_when_paced(runtime, monkeypatch):
    env = runtime
    seed(env)
    calls = []
    async def extract(scope, user, **kwargs):
        calls.append((scope, user))
    env.chat._extract = extract
    env.lifecycle.attempt('web:default', '42', 'TimeoutError')
    env.lifecycle.observe('web:default', '42')
    await env.lifecycle._poll_once()
    assert not calls and not env.chat._jobs
    assert not env.lifecycle.claim('web:default', '42', automatic=True)
    env.clock[0] += timedelta(minutes=5)
    env.lifecycle.observe('web:default', '42')
    await asyncio.gather(*list(env.chat._jobs))
    assert calls == [('web:default', '42')]
    await asyncio.sleep(0)
    assert env.lifecycle.claim('web:default', '43', automatic=True)
    def forbidden(*args, **kwargs):
        raise AssertionError('paced work must not scan pending sources')
    monkeypatch.setattr(env.lifecycle, '_pending', forbidden)
    env.lifecycle.observe('web:default', '42')
    await env.lifecycle._poll_once()
    assert calls == [('web:default', '42')]


@pytest.mark.anyio
async def test_poll_continues_after_one_pair_raises_and_propagates_cancellation(runtime):
    env = runtime
    seed(env, 'a')
    seed(env, 'b')
    calls = []
    async def extract(scope, user, **kwargs):
        calls.append(user)
        if user == 'a':
            raise RuntimeError('offline fixture failure')
        assert env.lifecycle.claim(scope, user, automatic=True)
    env.chat._extract = extract
    await env.lifecycle._poll_once()
    assert calls == ['a', 'b']
    assert not env.lifecycle.ready('web:default', 'a')
    assert claim_count(env.store) == 1
    env.clock[0] += timedelta(minutes=24)
    async def cancel(*args, **kwargs):
        raise asyncio.CancelledError()
    env.chat._extract = cancel
    with pytest.raises(asyncio.CancelledError):
        await env.lifecycle._poll_once()


@pytest.mark.anyio
async def test_poll_chooses_recent_pairs_and_rotates_them_after_a_durable_attempt(runtime):
    env = runtime
    seed(env, 'old', age=timedelta(days=5))
    seed(env, 'recent-a', age=timedelta(hours=1))
    seed(env, 'recent-b', age=timedelta(hours=2))
    calls = []
    async def extract(scope, user, **kwargs):
        calls.append(user)
        assert env.lifecycle.claim(scope, user, automatic=True)
        env.lifecycle.attempt(scope, user)
    env.chat._extract = extract
    await env.lifecycle._poll_once()
    env.clock[0] += timedelta(minutes=24)
    env.lifecycle = MemoryLifecycle(env.chat)
    await env.lifecycle._poll_once()
    assert calls == ['recent-a', 'recent-b']


@pytest.mark.anyio
async def test_malformed_restored_pair_timestamp_does_not_starve_healthy_pairs(runtime):
    env = runtime
    malformed = seed(env, 'malformed')[0]
    malformed['created_at'] = 'invalid restored timestamp'
    with env.store._lock, env.store._conn:
        env.store._write('messages', malformed, ('scope_id', 'user_id', 'role', 'created_at'))
    seed(env, 'healthy')
    calls = []
    async def extract(scope, user, **kwargs):
        calls.append(user)
        assert env.lifecycle.claim(scope, user, automatic=True)
    env.chat._extract = extract
    await env.lifecycle._poll_once()
    assert calls == ['healthy']
    assert not env.lifecycle.ready('web:default', 'malformed')
