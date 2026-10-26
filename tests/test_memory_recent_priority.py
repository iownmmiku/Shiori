"""Recent extraction stays useful without losing or reprocessing older inputs."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
from types import SimpleNamespace

import pytest

from mikubot.chat import ChatService
from mikubot.db import Store
import mikubot.db as db_module
import mikubot.memory_lifecycle as lifecycle_module
from mikubot.settings import DEFAULTS


@pytest.fixture
def anyio_backend():
    return 'asyncio'


class Model:
    def __init__(self):
        self.calls = []
        self.content = '[]'
        self.after_dispatch = None

    async def complete(self, messages, **kwargs):
        assert kwargs['purpose'] == 'memory_extract'
        assert kwargs['before_send']()
        self.calls.append(json.loads(messages[-1]['content']))
        if self.after_dispatch:
            self.after_dispatch()
        return {'content': self.content, 'usage': {}}


@pytest.fixture
async def runtime(tmp_path, monkeypatch):
    clock = [datetime(2026, 10, 26, 6, tzinfo=timezone.utc)]
    monkeypatch.setattr(lifecycle_module, '_now', lambda: clock[0], raising=False)
    monkeypatch.setattr(db_module, 'utc_now', lambda: clock[0].isoformat().replace('+00:00', 'Z'))
    config = deepcopy(DEFAULTS)
    config['model'].update(base_url='https://fixture.invalid/v1', model='fixture')
    config['memory'].update(organization_enabled=False, daily_extractions=60)
    config['stickers']['enabled'] = False
    settings = SimpleNamespace(get=lambda: deepcopy(config))
    store, provider = Store(tmp_path), Model()
    chat = ChatService(store, settings, provider)
    env = SimpleNamespace(store=store, chat=chat, provider=provider, config=config,
                          settings=settings, clock=clock, base=clock[0])
    try:
        yield env
    finally:
        await env.chat.close()
        store.close()


def message(env, at, text, user='42'):
    previous = env.clock[0]
    env.clock[0] = at
    try:
        stamp = at.isoformat()
        return env.store.add_message('web:default', user, 'user', text,
                                     sent_at=stamp, received_at=stamp)
    finally:
        env.clock[0] = previous


def ids(records):
    return [record['id'] for record in records]


def old_and_recent(env):
    old = [message(env, env.base - timedelta(days=5, minutes=40-index), f'旧消息 {index}')
           for index in range(40)]
    recent = [message(env, env.base - timedelta(hours=2, minutes=3-index), f'最近消息 {index}')
              for index in range(3)]
    return old, recent


@pytest.mark.anyio
async def test_recent_lane_reaches_current_messages_before_a_full_old_batch(runtime):
    env = runtime
    old, recent = old_and_recent(env)
    lifecycle = env.chat.memory_lifecycle
    assert ids(lifecycle.pending('web:default', '42', automatic=True)) == ids(recent)
    assert ids(lifecycle.pending('web:default', '42')) == ids(old[:32])


@pytest.mark.anyio
async def test_successful_recent_sources_survive_restart_without_skipping_or_reprocessing_backlog(runtime):
    env = runtime
    old, recent = old_and_recent(env)
    lifecycle = env.chat.memory_lifecycle
    chosen = lifecycle.pending('web:default', '42', automatic=True)
    lifecycle.advance('web:default', '42', chosen, automatic=True)
    await env.chat.close()
    env.chat = ChatService(env.store, env.settings, env.provider)
    lifecycle = env.chat.memory_lifecycle
    processed_backlog = []
    while True:
        pending = lifecycle.pending('web:default', '42', limit=7)
        if not pending:
            break
        assert not set(ids(pending)) & set(ids(recent))
        processed_backlog.extend(ids(pending))
        lifecycle.advance('web:default', '42', pending)
    assert processed_backlog == ids(old)
    assert lifecycle.pending('web:default', '42', automatic=True) == []


@pytest.mark.anyio
async def test_moving_recent_horizon_preserves_the_unprocessed_gap_for_backlog(runtime):
    env = runtime
    old = message(env, env.base - timedelta(days=5), '很早以前的未处理消息')
    recent = message(env, env.base - timedelta(hours=23), '第一轮最近消息')
    lifecycle = env.chat.memory_lifecycle
    lifecycle.advance('web:default', '42', [recent], automatic=True)
    gap = message(env, env.base + timedelta(hours=12), '服务休息期间的中间消息')
    env.clock[0] = env.base + timedelta(hours=48)
    newest = message(env, env.clock[0] - timedelta(hours=1), '重启后当前消息')
    assert ids(lifecycle.pending('web:default', '42', automatic=True)) == [newest['id']]
    lifecycle.advance('web:default', '42', [newest], automatic=True)
    assert ids(lifecycle.pending('web:default', '42', limit=400)) == [old['id'], gap['id']]
    lifecycle.advance('web:default', '42', [old])
    assert ids(lifecycle.pending('web:default', '42', limit=400)) == [gap['id']]
    lifecycle.advance('web:default', '42', [gap])
    assert lifecycle.pending('web:default', '42', automatic=True) == []


@pytest.mark.anyio
async def test_a_reserved_backlog_turn_does_not_get_taken_by_recent_messages(runtime):
    env = runtime
    old, _ = old_and_recent(env)
    for _ in range(3):
        env.store.add_event('memory_extract_attempt', 'isolated prior claim', scope_id='web:default', user_id='42')
    assert ids(env.chat.memory_lifecycle.pending('web:default', '42', automatic=True)) == ids(old[:32])


@pytest.mark.anyio
async def test_recent_selection_excludes_withdrawn_sources_and_other_members(runtime):
    env = runtime
    blocked = message(env, env.base - timedelta(hours=2), '已撤回来源')
    available = message(env, env.base - timedelta(hours=1), '仍允许使用的来源')
    message(env, env.base - timedelta(minutes=10), '其他成员的消息', user='someone-else')
    memory = env.store.create_memory({'scope_id': 'web:default', 'user_id': '42', 'kind': 'fact',
                                     'content': '临时事实', 'source_ids': [blocked['id']]})
    env.store.delete_memory(memory['id'])
    assert ids(env.chat.memory_lifecycle.pending('web:default', '42', automatic=True)) == [available['id']]


@pytest.mark.anyio
async def test_real_extraction_uses_recent_lane_and_valid_empty_result_is_not_repeated(runtime):
    env = runtime
    old, recent = old_and_recent(env)
    result = await env.chat._extract('web:default', '42', automatic=True)
    assert result['skipped'] is False
    assert len(env.provider.calls) == 1 and ids(env.provider.calls[0]) == ids(recent)
    assert ids(env.chat.memory_lifecycle.pending('web:default', '42', limit=400)) == ids(old)
    assert env.store.list_memories(scope_id='web:default') == []


@pytest.mark.anyio
@pytest.mark.parametrize('failure', ['invalid-json', 'permission', 'deleted-source'])
async def test_failed_or_revoked_recent_extraction_never_advances_old_or_recent_progress(runtime, failure):
    env = runtime
    old, recent = old_and_recent(env)
    if failure == 'invalid-json':
        env.provider.content = '{broken'
    elif failure == 'permission':
        env.provider.after_dispatch = lambda: env.config.update(paused=True)
    else:
        def delete():
            with env.store._lock, env.store._conn:
                env.store._conn.execute('DELETE FROM messages WHERE id=?', (recent[-1]['id'],))
        env.provider.after_dispatch = delete
    result = await env.chat._extract('web:default', '42', automatic=True)
    assert result['skipped'] is True
    expected = old + (recent[:-1] if failure == 'deleted-source' else recent)
    assert ids(env.chat.memory_lifecycle.pending('web:default', '42', limit=400)) == ids(expected)
    assert env.store.list_memories(scope_id='web:default') == []
