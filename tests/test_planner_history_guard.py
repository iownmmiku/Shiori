"""Fixed Planner provenance is prepared once; live revocation is never cached."""
import asyncio
from copy import deepcopy
from datetime import datetime, timezone
import json
import sqlite3
from types import SimpleNamespace

import pytest

from mikubot.chat import ChatService
from mikubot.db import Store, message_fingerprint
from mikubot.settings import DEFAULTS


SCOPE = 'qq:group:202'


@pytest.fixture
def service(tmp_path):
    store = Store(tmp_path)
    config = deepcopy(DEFAULTS)
    config['onebot'].update(enabled=True, chat_policy='blacklist')
    store.upsert_scope({'id': SCOPE, 'auto_memory': False})
    chat = ChatService(store, SimpleNamespace(get=lambda: deepcopy(config)), object())
    yield store, config, chat
    asyncio.run(chat.close())
    store.close()


def chain(store, count=30, *, memory=None, jargon=None, knowledge=None):
    """Legacy replies cite direct ancestors, without flattened dependencies."""
    records = []
    for index in range(count):
        record = store.add_message(SCOPE, '303', 'user', f'Question {index}')
        records.append(record)
        metadata = {'context_source_ids': [item['id'] for item in records[-24:]]}
        if index == 0:
            if memory:
                metadata['memory_versions'] = {memory['id']: memory['version']}
            if jargon:
                metadata['jargon_versions'] = {jargon['id']: jargon['version']}
            if knowledge:
                metadata['knowledge_refs'] = knowledge
        records.append(store.add_message(SCOPE, 'assistant', 'assistant', f'Answer {index}', **metadata))
    return records


def rewrite(store, record, **changes):
    changed = {**record, **changes}
    with store._lock, store._conn:
        store._conn.execute('UPDATE messages SET scope_id=?,data=? WHERE id=?',
                            (changed['scope_id'], json.dumps(changed), changed['id']))


def history(chat):
    return chat._history(SCOPE, limit=14, user_id='303')


def fact(store, **changes):
    return store.create_memory({'scope_id': SCOPE, 'user_id': '303', 'content': 'A verified fact',
        'visibility': 'group', 'kind': 'fact', **changes})


def term(store):
    return store.create_jargon({'scope_id': SCOPE, 'term': 'A phrase', 'meaning': 'A friendly greeting',
        'status': 'approved', 'use_allowed': True})


def test_stream_guard_does_one_batch_read_without_rewalking_legacy_ancestors(service, monkeypatch):
    store, _, chat = service
    records = chain(store, 200)
    referenced = history(chat)
    assert len(referenced) == 14
    guard = chat.social.history_guard_fn(SCOPE, '303', referenced)
    queries = []
    store._conn.set_trace_callback(queries.append)
    monkeypatch.setattr(chat.context, '_manifest', lambda *_a, **_k: pytest.fail('Provenance graph rebuilt'))
    monkeypatch.setattr(store, 'get_message', lambda *_: pytest.fail('Per-ancestor row decode'))
    try:
        for _ in range(20):
            assert guard()
    finally:
        store._conn.set_trace_callback(None)
    reads = [query for query in queries if query.startswith('SELECT id,data FROM messages')]
    assert len(reads) == 20
    assert all(records[0]['id'] in query and records[-1]['id'] in query for query in reads)


@pytest.mark.parametrize('change', ['delete', 'edit', 'metadata', 'move', 'external_delete', 'external_edit', 'withdraw'])
def test_stream_guard_detects_old_ancestor_mutations_outside_latest_window(service, change):
    store, _, chat = service
    records = chain(store)
    guard = chat.social.history_guard_fn(SCOPE, '303', history(chat))
    assert guard()
    old = records[0]
    if change == 'delete':
        with store._lock, store._conn:
            store._conn.execute('DELETE FROM messages WHERE id=?', (old['id'],))
    elif change == 'edit':
        rewrite(store, old, content='Corrected old source')
    elif change == 'metadata':
        rewrite(store, old, metadata={'sender_name': 'An altered identity'})
    elif change == 'move':
        rewrite(store, old, scope_id='qq:group:other')
    elif change.startswith('external_'):
        with sqlite3.connect(store.path) as external:
            if change == 'external_delete':
                external.execute('DELETE FROM messages WHERE id=?', (old['id'],))
            else:
                external.execute("UPDATE messages SET data=json_set(data,'$.content','external correction') WHERE id=?", (old['id'],))
    else:
        memory = fact(store, source_ids=[old['id']])
        store.delete_memory(memory['id'])
        assert store.get_message(old['id']) == old
    assert not guard()


@pytest.mark.parametrize('change', ['memory_edit', 'memory_delete', 'jargon_disable', 'jargon_edit', 'revoke', 'pause', 'scope_disable'])
def test_stream_guard_rechecks_legacy_ancestor_dependencies_and_authority(service, change):
    store, config, chat = service
    memory, jargon = fact(store), term(store)
    records = chain(store, memory=memory, jargon=jargon)
    latest = history(chat)
    assert all(not (item.get('metadata') or {}).get('memory_versions') for item in latest)
    guard = chat.social.history_guard_fn(SCOPE, '303', latest)
    assert guard()
    if change == 'memory_edit':
        store.update_memory(memory['id'], {'importance': .9})
    elif change == 'memory_delete':
        store.delete_memory(memory['id'])
    elif change == 'jargon_disable':
        store.update_jargon(jargon['id'], {'status': 'disabled'})
    elif change == 'jargon_edit':
        store.update_jargon(jargon['id'], {'meaning': 'Corrected meaning'})
    elif change == 'revoke':
        config['onebot']['blocked_groups'] = ['202']
    elif change == 'pause':
        config['paused'] = True
    else:
        store.upsert_scope({'id': SCOPE, 'enabled': False})
    # The legacy record itself did not change, so the raw-row comparison alone
    # cannot discover these dependency and policy changes.
    assert store.get_message(records[1]['id']) == records[1]
    assert not guard()


def test_stream_guard_observes_memory_expiry_without_database_write(service, monkeypatch):
    import mikubot.db as db_module
    store, _, chat = service
    memory = fact(store, valid_to='2300-01-01T00:00:00Z')
    chain(store, memory=memory)
    guard = chat.social.history_guard_fn(SCOPE, '303', history(chat))
    assert guard()
    changes = store._conn.total_changes
    monkeypatch.setattr(db_module, 'utc_now', lambda: '2300-01-02T00:00:00Z')
    assert not guard()
    assert store._conn.total_changes == changes


@pytest.mark.parametrize('change', ['delete', 'expiry', 'sources'])
def test_stream_guard_observes_knowledge_changes_outside_database(service, monkeypatch, change):
    import mikubot.chat as chat_module
    store, _, chat = service
    knowledge = {'id': 'fixture-knowledge', 'persona_id': 'default', 'status': 'active',
        'title': 'A verified source', 'text': 'A sourced detail', 'url': 'https://example.org/reference',
        'retrieved_at': '2026-01-01T00:00:00+00:00', 'valid_until': '2300-01-01T00:00:00+00:00'}
    chat.knowledge._state['items'].append(knowledge)
    chain(store, knowledge={knowledge['id']: chat._knowledge_fingerprint(knowledge)})
    guard = chat.social.history_guard_fn(SCOPE, '303', history(chat))
    assert guard()
    if change == 'delete':
        knowledge['status'] = 'deleted'
    elif change == 'expiry':
        monkeypatch.setattr(chat_module, 'datetime', SimpleNamespace(fromisoformat=datetime.fromisoformat,
            now=lambda *_: datetime(2300, 1, 2, tzinfo=timezone.utc)))
    else:
        chat.knowledge._state['sources']['default'] = {'blocked_domains': ['example.org']}
    assert not guard()


def test_stream_guard_survives_unrelated_messages_and_usage_writes(service):
    store, _, chat = service
    chain(store)
    guard = chat.social.history_guard_fn(SCOPE, '303', history(chat))
    assert guard()
    for index in range(220):
        store.add_message(SCOPE, '404', 'user', f'New background message {index}')
    store.record_usage({'scope_id': SCOPE, 'purpose': 'planner', 'input_tokens': 10,
                        'output_tokens': 10, 'model': 'fixture', 'cost': 0})
    assert guard()


def test_snapshot_accepts_legacy_json_layout_but_detects_changed_records(service):
    store, _, chat = service
    records = chain(store, 1)
    with store._lock, store._conn:
        for record in records:
            store._conn.execute('UPDATE messages SET data=? WHERE id=?',
                (json.dumps(record, ensure_ascii=True, indent=4), record['id']))
    guard = chat.social.history_guard_fn(SCOPE, '303', history(chat))
    assert guard()
    rewrite(store, records[0], content='A corrected legacy record')
    assert not guard()


@pytest.mark.parametrize('manual', [False, True])
@pytest.mark.parametrize('switch', ['global', 'scope'])
def test_digest_policy_changes_match_chat_history_and_keep_manual_digests(service, manual, switch):
    store, config, chat = service
    config['context']['enabled'] = True
    store.upsert_scope({'id': SCOPE, 'context_compress': True})
    source = store.add_message(SCOPE, '303', 'user', 'Earlier original discussion')
    digest = fact(store, kind='event', property='conversation_digest', derived=True, manual_digest=manual,
        source_ids=[source['id']], source_fingerprints={source['id']: message_fingerprint(source)})
    reply = store.add_message(SCOPE, 'assistant', 'assistant', 'Reply using the digest',
        source_ids=[source['id']], memory_versions={digest['id']: digest['version']})
    store.add_message(SCOPE, '303', 'user', 'A new question')
    assert reply['id'] in {record['id'] for record in history(chat)}
    guard = chat.social.history_guard_fn(SCOPE, '303', history(chat))
    assert guard()
    if switch == 'global':
        config['context']['enabled'] = False
    else:
        store.upsert_scope({'id': SCOPE, 'context_compress': False})
    assert guard() is manual
    assert (reply['id'] in {record['id'] for record in history(chat)}) is manual


@pytest.mark.parametrize('change', ['none', 'isolation', 'origin_revoke', 'origin_pause', 'source_delete', 'source_withdraw', 'dictionary'])
def test_shared_jargon_history_matches_visibility_permission_and_source_policy(service, change, monkeypatch):
    store, config, chat = service
    origin = 'qq:group:404'
    store.get_scope(origin)
    source = store.add_message(origin, '777', 'user', 'Source for the shared phrase')
    jargon = store.create_jargon({'scope_id': origin, 'term': 'A shared phrase', 'meaning': 'A greeting',
        'status': 'approved', 'source_ids': [source['id']]})
    question = store.add_message(SCOPE, '303', 'user', 'An earlier question')
    reply = store.add_message(SCOPE, 'assistant', 'assistant', 'Reply using shared phrase',
        source_ids=[question['id']], jargon_versions={jargon['id']: jargon['version']})
    store.add_message(SCOPE, '303', 'user', 'A new question')
    referenced = history(chat)
    assert reply['id'] in {record['id'] for record in referenced}
    assert store.jargon_visible(SCOPE, jargon)
    calls, original = [], store.list_jargon

    def listing(*args, **kwargs):
        calls.append(kwargs)
        return original(*args, **kwargs)

    monkeypatch.setattr(store, 'list_jargon', listing)
    guard = chat.social.history_guard_fn(SCOPE, '303', referenced)
    assert guard()
    assert calls and all(set(call['record_ids']) == {jargon['id']} for call in calls)
    if change == 'isolation':
        config['language']['group_isolation'] = True
    elif change == 'origin_revoke':
        config['onebot']['blocked_groups'] = ['404']
    elif change == 'origin_pause':
        store.upsert_scope({'id': origin, 'enabled': False})
    elif change == 'source_delete':
        with store._lock, store._conn:
            store._conn.execute('DELETE FROM messages WHERE id=?', (source['id'],))
    elif change == 'source_withdraw':
        withdrawn = fact(store, scope_id=origin, user_id='777', source_ids=[source['id']])
        store.delete_memory(withdrawn['id'])
    elif change == 'dictionary':
        store.upsert_scope({'id': SCOPE, 'dictionary_understand': False})
    assert guard() is (change == 'none')
    if change != 'dictionary':
        assert (reply['id'] in {record['id'] for record in history(chat)}) is (change == 'none')
