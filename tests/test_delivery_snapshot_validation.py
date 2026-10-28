"""Saved multipart receipts retain exact provenance across manual resumption."""
from copy import deepcopy
import json
import re
import sqlite3
from types import SimpleNamespace
from unittest.mock import AsyncMock

from fastapi import FastAPI
from fastapi.responses import JSONResponse
import httpx
import pytest

from shiori.chat import ChatService
from shiori.db import Store
from shiori.delivery import DeliveryLedger, install_delivery
from shiori.onebot import OneBotBridge
from shiori.settings import DEFAULTS, resolved_identity


@pytest.fixture
def anyio_backend():
    return 'asyncio'


def environment(path):
    config = deepcopy(DEFAULTS)
    config['onebot'].update(enabled=True, chat_policy='whitelist', allowed_groups=['202'])
    config['output']['interval_ms'] = 0
    config['memory']['organization_enabled'] = False
    config['stickers']['enabled'] = False
    settings = SimpleNamespace(get=lambda: deepcopy(config))
    store = Store(path)
    chat = ChatService(store, settings, SimpleNamespace())
    bridge = OneBotBridge(store, settings, chat)
    bridge.send = AsyncMock(return_value=True)
    app = FastAPI()

    @app.exception_handler(ValueError)
    async def invalid_request(request, error):
        return JSONResponse({'error': str(error)}, status_code=400)

    install_delivery(app, store, bridge, None)
    return SimpleNamespace(config=config, settings=settings, store=store, chat=chat,
                           bridge=bridge, ledger=DeliveryLedger(store), app=app)


async def close(env):
    await env.bridge.stop()
    await env.chat.close()
    env.store.close()


@pytest.fixture
async def runtime(tmp_path):
    env = environment(tmp_path / 'live')
    try:
        yield env
    finally:
        await close(env)


def receipt(env, **dependencies):
    scope = env.store.get_scope('qq:group:202')
    context = env.store.add_message(scope['id'], '304', 'user', 'Supporting original context')
    source = env.store.add_message(scope['id'], '303', 'user', 'Original current request')
    assistant = env.store.add_message(scope['id'], 'assistant', 'assistant', 'FirstSecondThird',
        source_ids=[source['id']], context_source_ids=[context['id'], source['id']], **dependencies)
    batch = {'logical_message_id': assistant['id'], 'reply_message': assistant,
             'entries': [{'message': source}], 'identity': resolved_identity(env.settings.get(), scope),
             'dependencies': assistant['metadata']}
    record = env.ledger.create(scope['id'], '303', ['First', 'Second', 'Third'], {}, batch)
    return record, source, context, assistant, batch


async def resume(env, record_id):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=env.app), base_url='http://local') as client:
        return await client.post('/api/delivery/' + record_id + '/resume', json={'confirm': True})


@pytest.mark.anyio
@pytest.mark.parametrize('changed', ['source', 'assistant', 'context'])
async def test_accepted_restore_with_changed_rows_refuses_old_pending_text(runtime, tmp_path, changed):
    env = runtime
    record, source, context, assistant, _ = receipt(env)
    assert env.ledger.valid(record, env.bridge._flow)
    backup = env.store.backup(tmp_path / 'restored.db')
    target = {'source': source, 'assistant': assistant, 'context': context}[changed]
    with sqlite3.connect(backup) as candidate:
        candidate.execute("UPDATE messages SET data=json_set(data,'$.content',?) WHERE id=?",
                          ('Corrected stored content', target['id']))
    assert env.store.restore_from(backup, validate_only=True) == {'valid': True}
    env.store.restore_from(backup)
    response = await resume(env, record['id'])
    assert response.status_code == 400
    assert '来源' in response.json()['error']
    env.bridge.send.assert_not_awaited()
    assert all(part['status'] == 'pending' for part in env.ledger.get(record['id'])['parts'])


@pytest.mark.anyio
@pytest.mark.parametrize('field', ['sent_at', 'received_at'])
async def test_restored_context_clock_correction_refuses_old_pending_text(runtime, tmp_path, field):
    env = runtime
    record, _, context, _, _ = receipt(env)
    backup = env.store.backup(tmp_path / 'corrected-clock.db')
    with sqlite3.connect(backup) as candidate:
        candidate.execute("UPDATE messages SET data=json_set(data,?,?) WHERE id=?",
                          ('$.' + field, '2020-01-01T12:00:00Z', context['id']))
    assert env.store.restore_from(backup, validate_only=True) == {'valid': True}
    env.store.restore_from(backup)
    response = await resume(env, record['id'])
    assert response.status_code == 400
    env.bridge.send.assert_not_awaited()


@pytest.mark.anyio
async def test_unchanged_receipt_survives_restart_and_only_resumes_pending_parts(tmp_path):
    path = tmp_path / 'live'
    env = environment(path)
    try:
        record, *_ = receipt(env)
        record['parts'][0]['status'] = 'confirmed'
        record['parts'][1]['status'] = 'unknown'
        env.ledger.save(record)
    finally:
        await close(env)
    env = environment(path)
    try:
        response = await resume(env, record['id'])
        assert response.status_code == 200 and response.json()['sent'] == 1
        env.bridge.send.assert_awaited_once()
        assert env.bridge.send.call_args.args[1] == 'Third'
        assert [part['status'] for part in response.json()['record']['parts']] == ['confirmed', 'unknown', 'confirmed']
        assert (await resume(env, record['id'])).json()['sent'] == 0
        assert env.bridge.send.await_count == 1
    finally:
        await close(env)


@pytest.mark.anyio
@pytest.mark.parametrize('pending', [True, False])
async def test_legacy_receipt_is_retained_but_pending_resumption_is_refused(runtime, pending):
    env = runtime
    record, *_ = receipt(env)
    for key in ('snapshot_version', 'message_fingerprints', 'context_source_ids'):
        record.pop(key)
    record['parts'][0]['status'] = 'confirmed'
    record['parts'][1]['status'] = 'unknown'
    if not pending:
        record['parts'][2]['status'] = 'confirmed'
    env.ledger.save(record)
    before = deepcopy(env.ledger.get(record['id']))
    response = await resume(env, record['id'])
    if pending:
        assert response.status_code == 400 and '旧发送记录' in response.json()['error']
    else:
        assert response.status_code == 200 and response.json()['sent'] == 0
    assert env.ledger.get(record['id']) == before
    assert any(item['id'] == record['id'] for item in env.ledger.list())
    env.bridge.send.assert_not_awaited()


@pytest.mark.anyio
@pytest.mark.parametrize('revoke', ['memory', 'permission'])
async def test_live_dependency_and_permission_revocation_still_blocks_resumption(runtime, revoke):
    env = runtime
    memory = env.store.create_memory({'scope_id': 'qq:group:202', 'user_id': '303', 'kind': 'note',
                                     'content': 'An authorized fact', 'visibility': 'group'})
    record, *_ = receipt(env, memory_versions={memory['id']: memory['version']})
    assert env.ledger.valid(record, env.bridge._flow)
    if revoke == 'memory':
        env.store.delete_memory(memory['id'])
    else:
        env.config['onebot']['allowed_groups'] = []
    assert (await resume(env, record['id'])).status_code == 400
    env.bridge.send.assert_not_awaited()


@pytest.mark.anyio
async def test_source_change_while_first_pending_part_is_sending_stops_the_next(runtime):
    env = runtime
    record, source, *_ = receipt(env)

    async def deliver(scope_id, text, **kwargs):
        with env.store._lock, env.store._conn:
            env.store._conn.execute("UPDATE messages SET data=json_set(data,'$.content',?) WHERE id=?",
                                    ('A correction while sending', source['id']))
        return True

    env.bridge.send.side_effect = deliver
    response = await resume(env, record['id'])
    assert response.status_code == 200 and response.json()['sent'] == 1
    env.bridge.send.assert_awaited_once()
    assert [part['status'] for part in response.json()['record']['parts']] == ['confirmed', 'pending', 'pending']


@pytest.mark.anyio
@pytest.mark.parametrize('original', ['source', 'assistant'])
async def test_snapshot_keeps_original_batch_records_instead_of_blessing_changed_rows(runtime, original):
    env = runtime
    _, source, _, assistant, batch = receipt(env)
    target = source if original == 'source' else assistant
    with env.store._lock, env.store._conn:
        env.store._conn.execute("UPDATE messages SET data=json_set(data,'$.content',?) WHERE id=?",
                                ('Already changed before receipt creation', target['id']))
    record = env.ledger.create('qq:group:202', '303', ['Old text'], {}, batch)
    assert not env.ledger.valid(record, env.bridge._flow)
    assert (await resume(env, record['id'])).status_code == 400
    env.bridge.send.assert_not_awaited()


@pytest.mark.anyio
async def test_snapshot_is_complete_and_contains_hashes_instead_of_extra_message_bodies(runtime):
    env = runtime
    record, source, context, assistant, _ = receipt(env)
    assert record['snapshot_version'] == 1
    assert set(record['message_fingerprints']) == {source['id'], context['id'], assistant['id']}
    assert all(re.fullmatch('[0-9a-f]{64}', value) for value in record['message_fingerprints'].values())
    assert source['content'] not in json.dumps(record)
    assert context['content'] not in json.dumps(record)
    record['message_fingerprints'].pop(context['id'])
    env.ledger.save(record)
    assert (await resume(env, record['id'])).status_code == 400
    env.bridge.send.assert_not_awaited()


@pytest.mark.anyio
@pytest.mark.parametrize('wrong_role', ['source', 'assistant'])
async def test_matching_fingerprints_cannot_bless_invalid_request_or_reply_roles(runtime, wrong_role):
    env = runtime
    scope = env.store.get_scope('qq:group:202')
    source = env.store.add_message(scope['id'], '303', 'assistant' if wrong_role == 'source' else 'user', 'Question')
    assistant = env.store.add_message(scope['id'], 'assistant', 'user' if wrong_role == 'assistant' else 'assistant', 'Reply')
    record = env.ledger.create(scope['id'], '303', ['Reply'], {}, {
        'logical_message_id': assistant['id'], 'reply_message': assistant,
        'entries': [{'message': source}], 'identity': resolved_identity(env.settings.get(), scope)})
    assert (await resume(env, record['id'])).status_code == 400
    env.bridge.send.assert_not_awaited()
