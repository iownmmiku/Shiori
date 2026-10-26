"""Canonical digest recovery and opt-out through actual context/chat paths."""
from copy import deepcopy

import pytest

from mikubot.chat import ChatService
from mikubot.context import ContextEngine
from mikubot.db import Store
from mikubot.settings import DEFAULTS, resolve_model


@pytest.fixture
def anyio_backend():
    return 'asyncio'


class Settings:
    def __init__(self):
        self.data = deepcopy(DEFAULTS)
        self.data['model'].update(base_url='https://model.invalid/v1', model='fixture')
        self.data['context'].update(enabled=True, trigger_messages=6, max_chars=16000, keep_recent=2)

    def get(self):
        return deepcopy(self.data)


class Model:
    def __init__(self, settings):
        self.settings, self.calls = settings, []

    def resolve(self, purpose):
        return resolve_model(self.settings.get(), purpose)

    async def complete(self, messages, **kwargs):
        allowed = not kwargs.get('before_send') or kwargs['before_send']()
        self.calls.append({'purpose': kwargs.get('purpose', 'chat'), 'allowed': allowed, 'messages': deepcopy(messages)})
        assert allowed, 'A withdrawn summary must be excluded before model dispatch'
        text = 'RECOVERY_DIGEST：讨论今天菜单，保留人物归属与消息次序。' if kwargs.get('purpose') == 'context_digest' else '今天约好讨论菜单。'
        return {'content': text, 'tool_calls': [], 'message': {'role': 'assistant', 'content': text}, 'usage': {}}


@pytest.fixture
def env(tmp_path):
    store = Store(tmp_path / 'context-recovery')
    settings = Settings()
    store.upsert_scope({'id': 'web:default', 'context_compress': True, 'auto_memory': False, 'allow_tools': False})
    provider = Model(settings)
    try:
        yield store, settings, provider
    finally:
        store.close()


def seed(store, start, count, scope='web:default', user='owner'):
    result = []
    for index in range(start, start + count):
        role = 'user' if index % 2 == 0 else 'assistant'
        metadata = {'source_ids': [result[-1]['id']]} if role == 'assistant' and result else {}
        result.append(store.add_message(scope, user if role == 'user' else 'assistant', role,
            f'ORIGINAL_{index:02d} 今天讨论菜单', **metadata))
    return result


@pytest.mark.anyio
@pytest.mark.parametrize('timestamp_ties', [False, True])
@pytest.mark.parametrize('legacy_order', [False, True])
async def test_cumulative_digest_coverage_and_invalidated_fallback_follow_database_order(env, monkeypatch, timestamp_ties, legacy_order):
    store, settings, provider = env
    if timestamp_ties:
        monkeypatch.setattr('mikubot.db.utc_now', lambda: '2026-10-26T00:00:00.000000+00:00')
    engine = ContextEngine(store, settings, provider)
    originals = seed(store, 0, 12)
    first = await engine.build('web:default', 'owner', originals)
    if legacy_order:
        # Older versions accidentally stored these IDs through a set.
        store.update_memory(first['summary']['id'], {
            'covered_message_ids': list(reversed(first['summary']['covered_message_ids']))})
    originals.extend(seed(store, 12, 12))
    second = await engine.build('web:default', 'owner', originals)
    assert second['summary']['covered_message_ids'] == [item['id'] for item in originals[:-2]]
    legacy_summary = dict(second['summary'], covered_message_ids=list(reversed(second['summary']['covered_message_ids'])))
    # Changing metadata invalidates descendants without withdrawing raw sources.
    store.update_memory(first['summary']['id'], {'confidence': .6})
    restored = engine._result('web:default', 'owner', second['messages'], legacy_summary)
    assert not restored['summary'] and not restored['compressed']
    assert restored['messages'] == originals
    assert len(restored['source_ids']) == len(originals)


@pytest.mark.anyio
async def test_invalidated_fallback_does_not_restore_foreign_users_scopes_or_missing_sources(env):
    store, settings, provider = env
    engine = ContextEngine(store, settings, provider)
    originals = seed(store, 0, 12)
    first = await engine.build('web:default', 'owner', originals)
    originals.extend(seed(store, 12, 12))
    second = await engine.build('web:default', 'owner', originals)
    other_user = store.add_message('web:default', 'other', 'user', 'OTHER_PRIVATE_USER')
    other_scope = store.add_message('web:other', 'owner', 'user', 'OTHER_PRIVATE_SCOPE')
    stale = dict(second['summary'], covered_message_ids=[other_scope['id'], other_user['id'], 'missing',
        *reversed(second['summary']['covered_message_ids']), originals[-1]['id']])
    store.update_memory(first['summary']['id'], {'confidence': .6})
    restored = engine._result('web:default', 'owner', second['messages'], stale)
    assert restored['messages'] == originals
    assert other_user['id'] not in restored['source_ids'] and other_scope['id'] not in restored['source_ids']


@pytest.mark.anyio
@pytest.mark.parametrize('gate', ['global', 'scope'])
async def test_disabling_automatic_digest_excludes_old_derived_replies_from_time_recall(env, gate):
    store, settings, provider = env
    chat = ChatService(store, settings, provider)
    try:
        seed(store, 0, 10)
        first = await chat.respond('web:default', 'owner', '说说做饭的安排')
        assert first['context_compressed']
        if gate == 'global':
            settings.data['context']['enabled'] = False
        else:
            store.upsert_scope({'id': 'web:default', 'context_compress': False})
        dated = chat._time_history('web:default', 'owner', '今天我们说了什么？')
        assert first['message_id'] not in {item['id'] for item in dated}
        assert any(item['role'] == 'user' and 'ORIGINAL_' in item['content'] for item in dated)
        second = await chat.respond('web:default', 'owner', '今天我们说了什么？')
        assert second['reply'] and not second['context_compressed']
        assert all(call['allowed'] for call in provider.calls)
        assert 'RECOVERY_DIGEST' not in str(provider.calls[-1]['messages'])
    finally:
        await chat.close()


@pytest.mark.anyio
@pytest.mark.parametrize('gate', ['global', 'scope'])
async def test_manual_digests_remain_authorized_when_automatic_compression_is_disabled(env, gate):
    store, settings, provider = env
    engine = ContextEngine(store, settings, provider)
    originals = seed(store, 0, 10)
    compacted = await engine.compact('web:default', 'owner', originals)
    digest = compacted['summary']
    assert digest['manual_digest']
    derived = store.add_message('web:default', 'assistant', 'assistant', '手动摘要支持的旧回答',
        context_source_ids=compacted['source_ids'], memory_versions={digest['id']: digest['version']})
    if gate == 'global':
        settings.data['context']['enabled'] = False
    else:
        store.upsert_scope({'id': 'web:default', 'context_compress': False})
    reference = engine.reference('web:default', 'owner', [derived])
    assert reference['messages'] == [derived]
    assert reference['memory_versions'][digest['id']] == digest['version']


@pytest.mark.anyio
async def test_live_guard_revokes_automatic_summary_reply_after_global_opt_out(env):
    store, settings, provider = env
    engine = ContextEngine(store, settings, provider)
    compacted = await engine.build('web:default', 'owner', seed(store, 0, 10))
    digest = compacted['summary']
    derived = store.add_message('web:default', 'assistant', 'assistant', '摘要支持的旧回答',
        context_source_ids=compacted['source_ids'], memory_versions={digest['id']: digest['version']})
    guard = engine.reference_guard('web:default', 'owner', [derived])
    assert guard()
    settings.data['context']['enabled'] = False
    assert not guard()
