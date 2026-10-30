"""Diary copies obey source deletion/editing, including in-flight requests."""
import asyncio
from datetime import datetime, timedelta, timezone
import json

import pytest

from test_life_schedule import async_test, finish, prepare, service
from shiori.db import message_fingerprint


def material(service, *, day=None, scope='qq:private:source'):
    day = day or (datetime.now(timezone.utc) - timedelta(days=2)).date().isoformat()
    pid = prepare(service, day)
    service.store.upsert_scope({'id': scope, 'persona_id': pid})
    service.save_settings({'enabled': True, 'diary_chat_enabled': True, 'diary_scopes': [scope]})
    source = service.store.add_message(scope, 'user', 'user', 'private original text')
    assert service.capture_message(scope, 'user', 'user', source['content'], source['id'],
                                   now=day + 'T12:00:00+08:00')
    return pid, day, scope, source


def change_source(store, source, action):
    if action == 'clear':
        store.clear_messages(source['scope_id'])
    else:
        with store._lock, store._conn:
            if action == 'delete':
                store._conn.execute('DELETE FROM messages WHERE id=?', (source['id'],))
            else:
                changed = dict(source, content='edited replacement text')
                store._conn.execute('UPDATE messages SET data=? WHERE id=?', (json.dumps(changed), source['id']))


@pytest.mark.parametrize('action', ['clear', 'delete', 'edit'])
@async_test
async def test_removed_or_edited_material_is_not_sent(service, action):
    pid, day, scope, source = material(service)
    change_source(service.store, source, action)
    service.provider.responses = [{'title': 'fiction', 'content': 'independent fictional day'}]
    result = await finish(service, service.submit({'persona_id': pid, 'date': day, 'kind': 'diary', 'scope_id': scope}))
    assert result['status'] == 'completed'
    payload = json.loads(service.provider.calls[0][0][1]['content'])
    assert payload['conversation_material'] == []
    assert 'private original text' not in json.dumps(payload)


@pytest.mark.parametrize('phase', ['before_send', 'after_send'])
@pytest.mark.parametrize('action', ['clear', 'delete', 'edit'])
@async_test
async def test_changed_source_invalidates_in_flight_diary(service, phase, action):
    pid, day, scope, source = material(service)
    entered, release = asyncio.Event(), asyncio.Event()
    sent = []

    async def delayed(messages, **kwargs):
        if phase == 'after_send':
            kwargs['before_send']()
            sent.append(messages)
        entered.set()
        await release.wait()
        if phase == 'before_send':
            kwargs['before_send']()
            sent.append(messages)
        return {'content': json.dumps({'title': 'private', 'content': 'must not persist'})}

    service.provider.complete = delayed
    job = service.submit({'persona_id': pid, 'date': day, 'kind': 'diary', 'scope_id': scope})
    await entered.wait()
    change_source(service.store, source, action)
    release.set()
    result = await finish(service, job)
    assert result['status'] == 'failed'
    assert len(sent) == (1 if phase == 'after_send' else 0)
    assert service.diary(pid, day, scope) is None


@async_test
async def test_deleted_source_cannot_return_through_previous_diary(service):
    pid, day, scope, source = material(service)
    service.provider.responses = [{'title': 'private', 'content': 'private original text'}]
    await finish(service, service.submit({'persona_id': pid, 'date': day, 'kind': 'diary', 'scope_id': scope}))
    later = (datetime.fromisoformat(day) + timedelta(days=1)).date().isoformat()
    _, record = service._record(pid)
    service.save({'persona_id': pid, 'kind': 'day', 'date': later, 'value': record['days'][day]})
    change_source(service.store, source, 'edit')
    service.provider.responses = [{'title': 'independent', 'content': 'fiction'}]
    await finish(service, service.submit({'persona_id': pid, 'date': later, 'kind': 'diary', 'scope_id': scope}))
    payload = json.loads(service.provider.calls[-1][0][1]['content'])
    assert payload['conversation_material'] == [] and payload['recent_diaries'] == []


@async_test
async def test_clear_removes_chat_diary_continuations_but_keeps_pure_fiction(service):
    pid, day, scope, source = material(service)
    service.provider.responses = [{'title': 'private', 'content': 'private original text'}]
    await finish(service, service.submit({'persona_id': pid, 'date': day, 'kind': 'diary', 'scope_id': scope}))
    later = (datetime.fromisoformat(day) + timedelta(days=1)).date().isoformat()
    _, record = service._record(pid)
    service.save({'persona_id': pid, 'kind': 'day', 'date': later, 'value': record['days'][day]})
    service.provider.responses = [{'title': 'continuation', 'content': 'based on yesterday'}]
    await finish(service, service.submit({'persona_id': pid, 'date': later, 'kind': 'diary', 'scope_id': scope}))
    continuation = service.diary(pid, later, scope)
    assert continuation['material_count'] == 0 and source['id'] in continuation['source_fingerprints']
    service.provider.responses = [{'title': 'fiction', 'content': 'pure fictional schedule'}]
    await finish(service, service.submit({'persona_id': pid, 'date': day, 'kind': 'diary'}))
    service.store.clear_messages(scope)
    assert service.diary(pid, day, scope) is None and service.diary(pid, later, scope) is None
    assert service.diary(pid, day)['content'] == 'pure fictional schedule'
    assert service.store._conn.execute('SELECT COUNT(*) FROM life_materials').fetchone()[0] == 0


@async_test
async def test_diary_continuity_never_accumulates_unbounded_source_history(service):
    pid, day, scope, current = material(service)
    previous_day = (datetime.fromisoformat(day) - timedelta(days=1)).date().isoformat()
    fingerprints = {}
    for index in range(480):
        source = service.store.add_message(scope, 'user', 'user', f'earlier source {index}')
        fingerprints[source['id']] = message_fingerprint(source)
    _, record = service._record(pid)
    previous = {'id': 'large-history', 'title': 'old', 'content': 'older excerpt must be omitted',
                'date': previous_day, 'material_count': 0, 'source_fingerprints': fingerprints}
    with service.store._lock, service.store._conn:
        service.store._conn.execute('INSERT INTO life_diaries VALUES(?,?,?,?,?,?)',
            (previous['id'], pid, record['key'], previous_day, scope, json.dumps(previous)))
    service.provider.responses = [{'title': 'today', 'content': 'bounded diary'}]
    result = await finish(service, service.submit({'persona_id': pid, 'date': day, 'kind': 'diary', 'scope_id': scope}))
    assert result['status'] == 'completed'
    payload = json.loads(service.provider.calls[-1][0][1]['content'])
    assert payload['recent_diaries'] == []
    assert payload['conversation_material'] == [{'role': 'user', 'content': current['content']}]
    assert 'source_fingerprints' not in json.dumps(payload)
    assert service.diary(pid, day, scope)['source_fingerprints'] == {current['id']: message_fingerprint(current)}
