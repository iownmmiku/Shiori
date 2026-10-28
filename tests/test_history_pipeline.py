"""Issue #10 regressions through real Store/ChatService/ContextEngine paths."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
import sqlite3

import pytest

from shiori.chat import ChatService
from shiori.context import ContextEngine
from shiori.db import Store, message_fingerprint
from shiori.settings import DEFAULTS, resolve_model


@pytest.fixture
def anyio_backend():
    return 'asyncio'


class Settings:
    def __init__(self):
        self.data = deepcopy(DEFAULTS)
        self.data['model'].update(base_url='https://fixture.invalid/v1', model='fixture')

    def get(self):
        return deepcopy(self.data)


class Model:
    def __init__(self, settings):
        self.settings, self.calls = settings, []
        self.digest = '【明确约定】周日准备菜单。【未完成事项】继续讨论菜品。'

    def resolve(self, purpose):
        return resolve_model(self.settings.get(), purpose)

    async def complete(self, messages, **kwargs):
        self.calls.append((deepcopy(messages), kwargs))
        if kwargs.get('before_send'):
            assert kwargs['before_send']()
        content = self.digest if kwargs.get('purpose') == 'context_digest' else '已收到。'
        if callable(content):
            content = content()
        return {'content': content, 'tool_calls': [], 'message': {'role': 'assistant', 'content': content}, 'usage': {}}

    @property
    def chats(self):
        return [messages for messages, options in self.calls if options.get('purpose', 'chat') == 'chat']

    @property
    def digests(self):
        return [messages for messages, options in self.calls if options.get('purpose') == 'context_digest']


@pytest.fixture
async def service(tmp_path):
    store, settings = Store(tmp_path), Settings()
    store.upsert_scope({'id': 'web:default', 'auto_memory': False, 'allow_tools': False})
    model = Model(settings)
    chat = ChatService(store, settings, model)
    try:
        yield store, settings, model, chat
    finally:
        await chat.close()
        store.close()


def seed(store, count, *, first='开场请求：准备周日菜单', sent_at=None):
    records = []
    for i in range(count):
        role = 'user' if i % 2 == 0 else 'assistant'
        metadata = {'sent_at': sent_at} if sent_at else {}
        if role == 'assistant':
            metadata['source_ids'] = [records[-1]['id']]
        records.append(store.add_message('web:default', 'owner' if role == 'user' else 'assistant', role,
                                         first if i == 0 else f'对话{i}：继续讨论菜单', **metadata))
    return records


def sent(model):
    return '\n'.join(str(message['content']) for message in model.chats[-1])


@pytest.mark.anyio
@pytest.mark.parametrize('count', [40, 80])
async def test_default_followup_keeps_rounds_or_explicit_continuity_anchor(service, count):
    store, settings, model, chat = service
    originals = seed(store, count)
    result = await chat.respond('web:default', 'owner', '那后来呢')
    assert originals[0]['content'] in sent(model)
    assert not result['context_compressed'] and not model.digests
    assert originals[0]['id'] in store.get_message(result['message_id'])['metadata']['context_source_ids']
    if count == 80:
        assert '【上次话题衔接】' in sent(model) and '不是本轮新请求' in sent(model)


@pytest.mark.anyio
@pytest.mark.parametrize('count', range(25, 31))
async def test_history_window_starts_with_a_user_turn_even_at_odd_boundaries(service, count):
    store, _, _, chat = service
    seed(store, count)
    records = chat._history('web:default', limit=24, user_id='owner')
    assert records and records[0]['role'] == 'user'


@pytest.mark.anyio
async def test_time_recall_is_canonical_and_reaches_model_as_labeled_reference(service):
    store, settings, model, chat = service
    yesterday = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    original = store.add_message('web:default', 'owner', 'user', '历史长文' + '资料' * 500 + '末尾结论必须保留', sent_at=yesterday)
    reply = store.add_message('web:default', 'assistant', 'assistant', '当时的助手回复', source_ids=[original['id']], sent_at=yesterday)
    seed(store, 60, first='今天的另一个话题')
    records = chat._time_history('web:default', 'owner', '昨天聊了什么')
    recalled = next(item for item in records if item['id'] == original['id'])
    assert recalled == original and message_fingerprint(recalled) == message_fingerprint(original)
    assert reply['id'] in {item['id'] for item in records}
    result = await chat.respond('web:default', 'owner', '昨天聊了什么')
    assert '【历史消息（时间检索）】' in sent(model)
    assert '末尾结论必须保留' in sent(model) and '当时的助手回复' in sent(model)
    saved = store.get_message(result['message_id'])['metadata']
    assert original['id'] in saved['context_source_ids']


@pytest.mark.anyio
async def test_broad_time_window_does_not_only_keep_busiest_latest_day(service):
    store, _, _, chat = service
    older = store.add_message('web:default', 'owner', 'user', '五天前的约定', sent_at=(datetime.now(timezone.utc) - timedelta(days=5)).isoformat())
    for i in range(40):
        store.add_message('web:default', 'owner', 'user', f'今天的闲聊{i}')
    records = chat._time_history('web:default', 'owner', '最近聊了什么')
    assert older['id'] in {item['id'] for item in records}
    assert len(records) <= 48


@pytest.mark.anyio
async def test_time_recall_excludes_other_users_and_forgotten_sources(service):
    store, _, model, chat = service
    yesterday = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    forgotten = store.add_message('web:default', 'owner', 'user', 'FORGOTTEN_TIME_SECRET', sent_at=yesterday)
    store.add_message('web:default', 'other', 'user', 'OTHER_USER_TIME_SECRET', sent_at=yesterday)
    memory = store.create_memory({'content': '撤回资料', 'source_ids': [forgotten['id']]})
    store.delete_memory(memory['id'])
    await chat.respond('web:default', 'owner', '昨天聊了什么')
    assert 'FORGOTTEN_TIME_SECRET' not in sent(model) and 'OTHER_USER_TIME_SECRET' not in sent(model)


@pytest.mark.anyio
@pytest.mark.parametrize('count,trigger', [(200, 60), (400, 400)])
async def test_low_prompt_window_does_not_prevent_summary_trigger(service, count, trigger):
    store, settings, model, chat = service
    settings.data['context'].update(enabled=True, max_history_messages=16, trigger_messages=trigger)
    store.upsert_scope({'id': 'web:default', 'context_compress': True})
    seed(store, count)
    result = await chat.respond('web:default', 'owner', '继续讨论菜单')
    assert result['context_compressed'] and len(model.digests) == 1
    assert sum(item['role'] in ('user', 'assistant') for item in model.chats[-1]) <= 17


@pytest.mark.anyio
async def test_small_budget_cannot_silently_evict_a_summary(service, monkeypatch):
    store, settings, model, chat = service
    settings.data['context'].update(enabled=True, trigger_messages=6, keep_recent=2, max_input_chars=2500)
    store.upsert_scope({'id': 'web:default', 'context_compress': True})
    seed(store, 10)
    facts = [store.create_memory({'content': '菜单资料' * 500, 'user_id': 'owner', 'pinned': True}) for _ in range(3)]

    async def recall(*args):
        return facts

    monkeypatch.setattr(chat, '_recall_context', recall)
    result = await chat.respond('web:default', 'owner', '菜单继续怎么安排')
    assert result['context_compressed'] and model.digest in sent(model)
    summaries = [record for record in store.list_memories() if record.get('property') == 'conversation_digest']
    assert summaries[0]['id'] in store.get_message(result['message_id'])['metadata']['memory_versions']
    assert sum(len(str(item['content'])) for item in model.chats[-1]) <= 2500


@pytest.mark.anyio
async def test_oversize_required_summary_restores_raw_history_without_claiming_compression(service):
    store, settings, model, chat = service
    settings.data['context'].update(enabled=True, trigger_messages=6, keep_recent=2, max_input_chars=2000)
    store.upsert_scope({'id': 'web:default', 'context_compress': True})
    model.digest = '摘要' * 1000
    seed(store, 10)
    result = await chat.respond('web:default', 'owner', '继续')
    assert not result['context_compressed'] and model.digest not in sent(model)
    summaries = [record for record in store.list_memories() if record.get('property') == 'conversation_digest']
    assert summaries[0]['id'] not in store.get_message(result['message_id'])['metadata']['memory_versions']
    assert '对话' in sent(model)


@pytest.mark.anyio
async def test_oversized_summary_has_one_bounded_repair_and_event(service):
    store, settings, model, chat = service
    settings.data['context'].update(enabled=True, trigger_messages=6, keep_recent=2)
    store.upsert_scope({'id': 'web:default', 'context_compress': True})
    outputs = iter(['长' * 3503, '精简后保留的菜单约定'])
    model.digest = lambda: next(outputs)
    result = await chat.context.build('web:default', 'owner', seed(store, 10))
    assert result['compressed'] and result['summary']['content'] == '精简后保留的菜单约定'
    assert len(model.digests) == 2
    assert any(item.get('details', {}).get('reason') == 'summary_output_retry' for item in store.list_events())


@pytest.mark.anyio
async def test_unrepairable_summary_is_visible_in_events_and_keeps_raw_sources(service):
    store, settings, model, chat = service
    settings.data['context'].update(enabled=True, trigger_messages=6, keep_recent=2)
    store.upsert_scope({'id': 'web:default', 'context_compress': True})
    model.digest = '长' * 3503
    originals = seed(store, 10)
    result = await chat.context.build('web:default', 'owner', originals)
    assert not result['compressed'] and result['messages'] == originals
    assert len(model.digests) == 2
    assert any(item.get('details', {}).get('reason') == 'summary_exceeds_output_limit' for item in store.list_events())


@pytest.mark.anyio
async def test_long_history_keeps_its_tail_instead_of_slicing_at_5000(service):
    store, _, model, chat = service
    body = '长历史' * 5000 + '末尾的最终结论'
    store.add_message('web:default', 'owner', 'user', body)
    await chat.respond('web:default', 'owner', '解释这个结论')
    assert body in sent(model)


@pytest.mark.anyio
async def test_first_cursor_backfills_old_sources_and_legacy_empty_seed(service):
    store, _, _, chat = service
    original = store.add_message('web:default', 'owner', 'user', '五天前尚未整理的事件')
    original['created_at'] = (datetime.now(timezone.utc) - timedelta(days=5)).isoformat()
    with store._lock, store._conn:
        store._write('messages', original, ('scope_id', 'user_id', 'role', 'created_at'))
    lifecycle = chat.memory_lifecycle
    assert [item['id'] for item in lifecycle.pending('web:default', 'owner')] == [original['id']]
    lifecycle._save('web:default', 'owner', ((datetime.now(timezone.utc) - timedelta(days=1)).isoformat(), '', '', ''))
    assert [item['id'] for item in lifecycle.pending('web:default', 'owner')] == [original['id']]
    attempted = datetime.now(timezone.utc).isoformat()
    lifecycle._save('web:default', 'owner', ((datetime.now(timezone.utc) - timedelta(days=1)).isoformat(), '', attempted, 'Timeout'))
    assert [item['id'] for item in lifecycle.pending('web:default', 'owner')] == [original['id']]
    assert lifecycle._state('web:default', 'owner')[2:] == (attempted, 'Timeout')


@pytest.mark.anyio
async def test_all_history_paths_use_insertion_order_for_timestamp_ties(service, monkeypatch):
    store, _, _, chat = service
    monkeypatch.setattr('shiori.db.utc_now', lambda: '2026-10-23T00:00:00.000000Z')
    ids = iter(['z-last-lexically', 'm-middle', 'a-first-lexically'])
    monkeypatch.setattr('shiori.db.uuid4', lambda: next(ids))
    originals = [store.add_message('web:default', 'owner', 'user', f'同一时刻{i}') for i in range(3)]
    lifecycle = chat.memory_lifecycle
    assert lifecycle.pending('web:default', 'owner') == originals
    assert store.ordered_messages([item['id'] for item in reversed(originals)], scope_id='web:default') == originals
    assert chat._time_history('web:default', 'owner', '最近聊了什么') == originals
    lifecycle.advance('web:default', 'owner', originals[:1])
    assert lifecycle.pending('web:default', 'owner') == originals[1:]
    lifecycle.advance('web:default', 'owner', originals[1:])
    assert lifecycle.pending('web:default', 'owner') == []


@pytest.mark.anyio
async def test_blocked_queue_head_cannot_starve_later_valid_messages(service):
    store, _, _, chat = service
    blocked = [store.add_message('web:default', 'owner', 'user', f'已遗忘{i}') for i in range(40)]
    valid = [store.add_message('web:default', 'owner', 'user', f'仍有效{i}') for i in range(6)]
    memory = store.create_memory({'content': '已遗忘的资料', 'source_ids': [item['id'] for item in blocked]})
    store.delete_memory(memory['id'])
    assert len(store.blocked_memory_sources('web:default')) >= 40
    assert chat.memory_lifecycle.pending('web:default', 'owner') == valid
    chat.memory_lifecycle.advance('web:default', 'owner', valid)
    assert chat.memory_lifecycle.pending('web:default', 'owner') == []


@pytest.mark.anyio
async def test_old_backup_without_new_history_indexes_restores_and_rebuilds_them(service, tmp_path):
    store, _, _, _ = service
    original = store.add_message('web:default', 'owner', 'user', '旧备份中的对话')
    backup = tmp_path / 'old-schema-one.db'
    store.backup(backup)
    with sqlite3.connect(backup) as connection:
        connection.execute('DROP INDEX messages_origin_day')
        connection.execute('DROP INDEX messages_extraction_cursor')
    assert store.restore_from(backup, validate_only=True)['valid']
    store.restore_from(backup)
    assert store.get_message(original['id']) == original
    with store._lock:
        names = {row[0] for row in store._conn.execute("SELECT name FROM sqlite_master WHERE type='index'")}
    assert {'messages_origin_day', 'messages_extraction_cursor'} <= names


@pytest.mark.anyio
async def test_backup_cannot_replace_a_trusted_history_index(service, tmp_path):
    store, _, _, _ = service
    original = store.add_message('web:default', 'owner', 'user', '当前有效的对话')
    backup = tmp_path / 'modified-index.db'
    store.backup(backup)
    with sqlite3.connect(backup) as connection:
        connection.execute('DROP INDEX messages_origin_day')
        connection.execute('CREATE INDEX messages_origin_day ON messages(role)')
    with pytest.raises(ValueError, match='结构不兼容'):
        store.restore_from(backup)
    assert store.get_message(original['id']) == original
