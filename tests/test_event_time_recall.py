"""Event occurrence dates form a bounded, authorized recall channel."""
from datetime import datetime, timezone
import sqlite3

import pytest

from shiori.db import Store
from shiori.memory import MemoryEngine
from shiori.timezones import message_time_reference


@pytest.fixture
def anyio_backend():
    return 'asyncio'


@pytest.fixture
async def env(tmp_path):
    store = Store(tmp_path)
    engine = MemoryEngine(store, {'memory': {'semantic_enabled': False}})
    try:
        yield store, engine
    finally:
        await engine.aclose()
        store.close()


def event(store, **fields):
    return store.create_memory({'kind': 'event', 'episode': {'title': '星图修复',
        'summary': '完成星图校准', 'narrative': '在天文馆修复星图', 'outcome': '保留了更正后的目录'},
        'event_time_start': '2026-10-20T08:00:00Z', 'event_time_known': True, **fields})


@pytest.mark.anyio
@pytest.mark.parametrize('query', ['2026-10-20', '2026年10月20日做了什么？', '昨天有什么事？', '昨晚呢？', '上周发生了什么？'])
async def test_date_only_recall_uses_occurrence_time_not_reporting_time(env, query):
    store, engine = env
    item = event(store, event_time_start='2026-10-20T12:00:00Z')
    anchor = datetime(2026, 10, 26 if query.startswith('上周') else 21, 1, tzinfo=timezone.utc)
    with message_time_reference(anchor):
        results = await engine.recall('web:default', 'owner', query)
    assert [row['id'] for row in results] == [item['id']]
    assert results[0]['retrieval']['time_boost'] == 0.12
    assert '星图' in results[0]['event_context']


@pytest.mark.anyio
async def test_time_candidates_survive_large_unrelated_library_and_unknown_dates(env):
    store, engine = env
    wanted = event(store)
    template = store.create_memory({'content': 'ordinary unrelated fact'})
    with store._lock, store._conn:
        for number in range(700):
            store._save_memory({**template, 'id': f'unrelated-{number}'})
    event(store, event_time_start=None, event_time_known=False)
    event(store, event_time_known=False)  # A stale timestamp does not make an unknown date known.
    event(store, event_time_start='2026-10-21T08:00:00Z')
    assert [row['id'] for row in await engine.recall('web:default', 'owner', '2026年10月20日做了什么？')] == [wanted['id']]
    assert await engine.recall('web:default', 'owner', '2026-10-20 submarine') == []


@pytest.mark.anyio
async def test_temporal_candidates_keep_scope_consent_validity_and_dependency_boundaries(env):
    store, engine = env
    store.upsert_scope({'id': 'web:default', 'memory_space': 'shared'})
    store.upsert_scope({'id': 'web:other', 'memory_space': 'shared'})
    own = event(store)
    shared = event(store, scope_id='web:other', visibility='group', shareable=True)
    event(store, user_id='other-user')
    event(store, scope_id='web:other', visibility='group', shareable=False)
    event(store, scope_id='web:other', visibility='private', shareable=True)
    event(store, valid_to='2000-01-01T00:00:00Z')
    event(store, dependencies={'missing': 1})
    assert {row['id'] for row in await engine.recall('web:default', 'owner', '2026-10-20')} == {own['id'], shared['id']}
    store.update_memory(shared['id'], {'shareable': False})
    store.delete_memory(own['id'])
    assert await engine.recall('web:default', 'owner', '2026-10-20') == []


@pytest.mark.anyio
@pytest.mark.parametrize('external', [False, True])
async def test_time_overlap_and_final_revocation(env, monkeypatch, external):
    store, engine = env
    item = event(store, event_time_start='2026-10-19T15:00:00Z', event_time_end='2026-10-19T17:00:00Z')
    ended = event(store, event_time_start='2026-10-19T14:00:00Z', event_time_end='2026-10-19T16:00:00Z')
    result = await engine.recall('web:default', 'owner', '2026-10-20')
    assert [row['id'] for row in result] == [item['id']] and ended['id'] not in {row['id'] for row in result}
    original = engine._rank_async
    async def revoked(*args):
        documents = await original(*args)
        if external:
            with sqlite3.connect(store.path) as connection:
                connection.execute("UPDATE memories SET data=json_set(data,'$.event_time_start',?,'$.event_time_end',NULL) WHERE id=?",
                    ('2026-10-21T08:00:00.000000Z', item['id']))
        else:
            store.update_memory(item['id'], {'event_time_start': '2026-10-21T08:00:00Z', 'event_time_end': None})
        return documents
    monkeypatch.setattr(engine, '_rank_async', revoked)
    assert await engine.recall('web:default', 'owner', '2026-10-20') == []


@pytest.mark.anyio
async def test_temporal_recall_uses_configured_calendar(env):
    store, engine = env
    engine.settings = {'memory': {'semantic_enabled': False}, 'time': {'timezone': 'America/New_York'}}
    previous_local_day = event(store, event_time_start='2026-10-20T02:00:00Z')
    target = event(store, event_time_start='2026-10-20T05:00:00Z')
    assert [row['id'] for row in await engine.recall('web:default', 'owner', '2026-10-20')] == [target['id']]
    assert [row['id'] for row in await engine.recall('web:default', 'owner', '2026-10-19')] == [previous_local_day['id']]


@pytest.mark.anyio
async def test_date_recall_preserves_literal_matches_in_ordinary_notes(env):
    store, engine = env
    note = store.create_memory({'kind': 'note', 'content': '2026-10-20 inspection checklist'})
    occurrence = event(store)
    assert {row['id'] for row in await engine.recall('web:default', 'owner', '2026-10-20')} == {note['id'], occurrence['id']}
