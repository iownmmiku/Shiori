"""Malformed model batches must not consume the durable source cursor."""
from copy import deepcopy
import asyncio
import json
from threading import Event
from types import SimpleNamespace

import pytest

from shiori.chat import ChatService
from shiori.db import Store, message_fingerprint
from shiori.memory_lifecycle import MemoryLifecycle
from shiori.settings import DEFAULTS


@pytest.fixture
def anyio_backend():
    return 'asyncio'


class Model:
    def __init__(self):
        self.answer = lambda records: []
        self.calls = 0
        self.guard = None

    async def complete(self, messages, **kwargs):
        self.guard = kwargs['before_send']
        assert kwargs['purpose'] == 'memory_extract' and kwargs['before_send']()
        self.calls += 1
        records = json.loads(messages[-1]['content'])
        return {'content': json.dumps(self.answer(records)), 'usage': {}}


@pytest.fixture
async def runtime(tmp_path):
    config = deepcopy(DEFAULTS)
    config['model'].update(base_url='https://fixture.invalid/v1', model='fixture')
    config['memory'].update(organization_enabled=False, daily_extractions=60)
    config['stickers']['enabled'] = False
    settings = SimpleNamespace(get=lambda: deepcopy(config))
    store, model = Store(tmp_path / 'candidate-validation'), Model()
    chat = ChatService(store, settings, model)
    records = [store.add_message('web:default', 'owner', 'user', content) for content in
               ('我喜欢蓝莓', '早餐经常吃蓝莓', '我每周都去跑步', '请记住这些偏好')]
    other = store.add_message('web:default', 'another-member', 'user', '其他成员的私人偏好')
    try:
        yield SimpleNamespace(store=store, chat=chat, model=model, records=records, other=other,
                              config=config, settings=settings)
    finally:
        await chat.close()
        store.close()


def candidate(records):
    return {'content': '用户喜欢蓝莓', 'kind': 'preference', 'confidence': .9,
            'property': 'preferred_fruit', 'source_ids': [records[0]['id']]}


@pytest.mark.anyio
@pytest.mark.parametrize('change', [
    lambda item: item.pop('kind'), lambda item: item.update(kind='unsupported'),
    lambda item: item.update(content=None), lambda item: item.update(content={'wrong': 'shape'}),
    lambda item: item.update(content='  '), lambda item: item.update(content='长' * 1001),
    lambda item: item.pop('source_ids'), lambda item: item.update(source_ids=[]),
    lambda item: item.update(source_ids='not-a-list'),
    lambda item: item.update(source_ids=[{'id': 'not-a-string'}]),
    lambda item: item.update(source_ids=['unknown-message']),
    lambda item: item.pop('confidence'), lambda item: item.update(confidence=True),
    lambda item: item.update(confidence='0.9'), lambda item: item.update(confidence=-.01),
    lambda item: item.update(confidence=1.01), lambda item: item.update(confidence=float('nan')),
    lambda item: item.update(confidence=float('inf')), lambda item: item.update(confidence=float('-inf')),
])
async def test_invalid_nonempty_batch_retains_all_sources_and_backoff(runtime, change):
    env = runtime
    def answer(records):
        valid = candidate(records)
        invalid = candidate(records)
        change(invalid)
        return [valid, invalid]
    env.model.answer = answer
    result = await env.chat._extract('web:default', 'owner')
    assert result['skipped'] is True
    assert env.store.list_memories('web:default') == [], 'No valid prefix may be committed'
    assert [row['id'] for row in MemoryLifecycle(env.chat).pending('web:default', 'owner')] == [row['id'] for row in env.records]
    assert not env.chat.memory_lifecycle.ready('web:default', 'owner')
    assert env.model.calls == 1
    with env.store._lock:
        claims = env.store._conn.execute("SELECT COUNT(*) FROM events WHERE kind='memory_extract_attempt'").fetchone()[0]
    assert claims == 1, 'Already-dispatched attempts stay spent'


@pytest.mark.anyio
@pytest.mark.parametrize('case', ['foreign-source', 'non-object', 'too-many'])
async def test_wrong_member_or_batch_shape_cannot_advance_sources(runtime, case):
    env = runtime
    def answer(records):
        if case == 'foreign-source':
            return [{**candidate(records), 'source_ids': [env.other['id']]}]
        if case == 'non-object':
            return [candidate(records), 'unexpected candidate']
        return [candidate(records) for _ in range(4)]
    env.model.answer = answer
    assert (await env.chat._extract('web:default', 'owner'))['skipped'] is True
    assert env.store.list_memories('web:default') == []
    assert len(env.chat.memory_lifecycle.pending('web:default', 'owner')) == 4


@pytest.mark.anyio
@pytest.mark.parametrize('confidence,expected', [(None, None), (.74, None), (.75, 'pending'), (.9, 'active')])
async def test_valid_empty_and_low_confidence_batches_complete_without_retries(runtime, confidence, expected):
    env = runtime
    env.model.answer = lambda records: [] if confidence is None else [{**candidate(records), 'confidence': confidence}]
    result = await env.chat._extract('web:default', 'owner')
    assert result['skipped'] is False
    assert env.chat.memory_lifecycle.pending('web:default', 'owner') == []
    saved = env.store.list_memories('web:default')
    assert [row['status'] for row in saved] == ([] if expected is None else [expected])
    assert env.chat.memory_lifecycle.ready('web:default', 'owner')


@pytest.mark.anyio
async def test_repeated_valid_source_references_are_normalized(runtime):
    env = runtime
    env.model.answer = lambda records: [{**candidate(records), 'source_ids': [records[0]['id'], records[0]['id']]}]
    assert (await env.chat._extract('web:default', 'owner'))['skipped'] is False
    assert env.store.list_memories('web:default')[0]['source_ids'] == [env.records[0]['id']]


@pytest.mark.anyio
@pytest.mark.parametrize('change', ['paused', 'auto_memory', 'disabled', 'source_edit', 'dependency_edit', 'zero_quota'])
async def test_queued_candidate_write_rechecks_current_authorization(runtime, change):
    env = runtime
    previous = env.store.create_memory({'content': '用户之前住在杭州', 'scope_id': 'web:default', 'user_id': 'owner'})
    env.model.answer = lambda records: [candidate(records)]
    entered, release, loop = asyncio.Event(), Event(), asyncio.get_running_loop()
    ingest = env.chat.memory.ingest_candidates
    def queued(*args, **kwargs):
        loop.call_soon_threadsafe(entered.set)
        assert release.wait(4)
        return ingest(*args, **kwargs)
    env.chat.memory.ingest_candidates = queued
    task = asyncio.create_task(env.chat._extract('web:default', 'owner'))
    try:
        await asyncio.wait_for(entered.wait(), 4)
        if change == 'paused':
            env.config['paused'] = True
        elif change == 'auto_memory':
            env.store.upsert_scope({'id': 'web:default', 'auto_memory': False})
        elif change == 'disabled':
            env.store.upsert_scope({'id': 'web:default', 'enabled': False})
        elif change == 'zero_quota':
            env.config['memory']['daily_extractions'] = 0
        elif change == 'source_edit':
            edited = {**env.records[0], 'content': '用户已经更正为不喜欢蓝莓'}
            with env.store._lock, env.store._conn:
                env.store._write('messages', edited, ('scope_id', 'user_id', 'role', 'created_at'))
        else:
            env.store.update_memory(previous['id'], {'content': '用户现在住在苏州'})
        release.set()
        result = await asyncio.wait_for(task, 4)
        assert result['skipped'] is True and result['saved'] == 0
        assert [row['id'] for row in env.store.list_memories('web:default')] == [previous['id']]
        assert len(env.chat.memory_lifecycle.pending('web:default', 'owner')) == 4
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.anyio
@pytest.mark.parametrize('change', ['paused', 'auto_memory', 'user_edit', 'review_manual'])
async def test_committed_candidates_are_preserved_without_unauthorized_auto_approval(runtime, change):
    env = runtime
    env.model.answer = lambda records: [candidate(records)]
    entered, release, loop = asyncio.Event(), Event(), asyncio.get_running_loop()
    ingest = env.chat.memory.ingest_candidates
    def saved_then_wait(*args, **kwargs):
        saved = ingest(*args, **kwargs)
        loop.call_soon_threadsafe(entered.set)
        assert release.wait(4)
        return saved
    env.chat.memory.ingest_candidates = saved_then_wait
    task = asyncio.create_task(env.chat._extract('web:default', 'owner'))
    try:
        await asyncio.wait_for(entered.wait(), 4)
        current = env.store.list_memories('web:default')[0]
        if change == 'paused':
            env.config['paused'] = True
        elif change == 'auto_memory':
            env.store.upsert_scope({'id': 'web:default', 'auto_memory': False})
        elif change == 'review_manual':
            env.config['memory']['review_mode'] = 'manual'
        else:
            current = env.store.update_memory(current['id'], {'content': '由用户更正后的独立描述'})
        release.set()
        result = await asyncio.wait_for(task, 4)
        saved = env.store.get_memory(current['id'])
        assert saved['status'] == 'pending'
        assert saved['content'] == current['content'] and saved['version'] == current['version']
        if change == 'review_manual':
            assert result['skipped'] is False
            assert env.chat.memory_lifecycle.pending('web:default', 'owner') == []
        else:
            assert result['skipped'] is True and result['saved'] == 1
            assert '按当前状态保留' in result['message']
            assert env.chat.memory_lifecycle._state('web:default', 'owner')[:2] == ('', '')
            # Editing the candidate intentionally withdraws its old source;
            # this must not be mistaken for advancing the extraction cursor.
            assert len(env.chat.memory_lifecycle.pending('web:default', 'owner')) == (3 if change == 'user_edit' else 4)
            assert len([row for row in env.store.list_messages('web:default') if row['user_id'] == 'owner']) == 4
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.anyio
@pytest.mark.parametrize('writes_before_revoke', [1, 2])
async def test_permission_change_during_batch_rolls_back_every_candidate(runtime, writes_before_revoke):
    env = runtime
    sources = [row['id'] for row in env.records]
    def before_write():
        # Revoke after a real insert: exercise the second-item and commit guards.
        count = env.store._conn.execute('SELECT COUNT(*) FROM memories').fetchone()[0]
        return count < writes_before_revoke
    with pytest.raises(PermissionError):
        env.chat.memory.ingest_candidates([
            candidate(env.records), {**candidate(env.records), 'content': '用户每周跑步'}],
            scope_id='web:default', user_id='owner', source_ids=sources, before_write=before_write)
    assert env.store.list_memories('web:default') == []
    assert env.store._conn.execute('SELECT COUNT(*) FROM memory_versions').fetchone()[0] == 0
    assert not env.store._conn.in_transaction
    assert len(env.chat.memory_lifecycle.pending('web:default', 'owner')) == 4


@pytest.mark.anyio
async def test_transactional_ingest_preserves_normal_memory_defaults(runtime):
    env = runtime
    raw = candidate(env.records)
    expected = env.store.create_memory({**raw, 'status': 'pending', 'subject': 'owner',
                                       'dependencies': {}, 'blocked_source_ids': [], 'shareable': False})
    env.store.delete_memory(expected['id'])
    # Use another source so its explicit deletion tombstone does not suppress it.
    raw['source_ids'] = [env.records[1]['id']]
    actual = env.chat.memory.ingest_candidates([raw], scope_id='web:default', user_id='owner',
                                             source_ids=[env.records[1]['id']])[0]
    ignored = {'id', 'created_at', 'updated_at', 'source_ids', 'source_fingerprints'}
    assert {key: value for key, value in actual.items() if key not in ignored} == {
        key: value for key, value in expected.items() if key not in ignored}
    assert actual['source_fingerprints'] == {env.records[1]['id']: message_fingerprint(env.records[1])}


@pytest.mark.anyio
@pytest.mark.parametrize('old_fact', ['expired', 'disabled_digest'])
async def test_unrelated_unrecallable_facts_do_not_block_new_extraction(runtime, old_fact):
    env = runtime
    fields = {'valid_to': '2000-01-01T00:00:00Z'} if old_fact == 'expired' else {
        'kind': 'event', 'property': 'conversation_digest', 'manual_digest': False}
    if old_fact == 'disabled_digest':
        env.config['context']['enabled'] = False
    previous = env.store.create_memory({'content': '无关的历史事实', 'user_id': 'owner', **fields})
    env.model.answer = lambda records: [candidate(records)]
    result = await env.chat._extract('web:default', 'owner')
    assert result['skipped'] is False and env.model.calls == 1
    assert env.chat.memory_lifecycle.pending('web:default', 'owner') == []
    saved = env.store.list_memories('web:default')
    assert len(saved) == 2 and all(row['status'] == 'active' for row in saved)
    assert env.store.get_memory(previous['id'])['version'] == previous['version']


@pytest.mark.anyio
@pytest.mark.parametrize('change', ['correct', 'delete', 'supersede'])
async def test_inflight_changes_to_old_fact_versions_still_discard_new_candidates(runtime, change):
    env = runtime
    previous = env.store.create_memory({'content': '之前的稳定事实', 'user_id': 'owner'})
    def answer(records):
        if change == 'correct':
            env.store.update_memory(previous['id'], {'content': '由用户纠正后的事实'})
        elif change == 'delete':
            env.store.delete_memory(previous['id'])
        else:
            env.store.update_memory(previous['id'], {'status': 'superseded'})
        return [candidate(records)]
    env.model.answer = answer
    result = await env.chat._extract('web:default', 'owner')
    assert result['skipped'] is True
    assert not any(row.get('automatic') for row in env.store.list_memories('web:default'))
    assert len(env.chat.memory_lifecycle.pending('web:default', 'owner')) == 4


@pytest.mark.anyio
async def test_new_automatic_approvals_do_not_invalidate_original_version_snapshot(runtime):
    env = runtime
    env.store.create_memory({'content': '无关的稳定旧事实', 'user_id': 'owner'})
    env.model.answer = lambda records: [candidate(records), {**candidate(records),
        'content': '用户每周跑步', 'property': 'exercise_habit', 'source_ids': [records[2]['id']]}]
    assert (await env.chat._extract('web:default', 'owner'))['skipped'] is False
    saved = env.store.list_memories('web:default')
    assert len(saved) == 3 and all(row['status'] == 'active' for row in saved)
    assert env.chat.memory_lifecycle.pending('web:default', 'owner') == []


@pytest.mark.anyio
async def test_version_guard_never_expands_thousands_of_old_fact_bodies(runtime, monkeypatch):
    env = runtime
    template = env.store.create_memory({'content': '无关的历史事实', 'user_id': 'owner'})
    with env.store._lock, env.store._conn:
        for i in range(3000):
            env.store._save_memory({**template, 'id': 'old-fact-' + str(i)})
    env.model.answer = lambda records: [candidate(records)]
    assert (await env.chat._extract('web:default', 'owner'))['skipped'] is False
    original_list = env.store._list
    def no_memory_bodies(table, *args, **kwargs):
        assert table != 'memories', 'A source/version guard cannot load every old memory body'
        return original_list(table, *args, **kwargs)
    def no_visibility(*args, **kwargs):
        raise AssertionError('Old memories not present in the prompt need no recall projection')
    monkeypatch.setattr(env.store, '_list', no_memory_bodies)
    monkeypatch.setattr(env.store, 'visible_memories', no_visibility)
    with env.store._lock:
        env.store._conn.execute('BEGIN IMMEDIATE')
        try:
            assert env.model.guard(), 'Current raw sources and original versions are still authorized'
        finally:
            env.store._conn.rollback()
