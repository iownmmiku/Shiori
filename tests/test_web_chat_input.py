"""One bounded browser batch retains its originals and server-owned authority."""
import asyncio
from datetime import datetime, timezone
import json
import threading

import pytest
from fastapi.testclient import TestClient

from auth_helpers import password_login
from shiori.api import create_app
from shiori.web_chat_input import chat_input


IMAGE = 'data:image/png;base64,YQ=='


@pytest.mark.parametrize('payload', [
    {'messages': None}, {'messages': []}, {'messages': [{'text': 'a'}] * 9},
    {'messages': ['a']}, {'messages': [{'text': 1}]}, {'messages': [{'text': ''}]},
    {'messages': [{'text': 'a', 'images': False}]}, {'messages': [{'text': 'a', 'images': {}}]},
    {'messages': [{'text': 'a', 'images': None}]}, {'messages': [{'text': 'a', 'images': [1]}]},
    {'messages': [{'text': 'a', 'attachments': None}]}, {'messages': [{'text': 'a', 'attachments': False}]},
    {'messages': [{'text': 'a', 'attachments': {}}]}, {'messages': [{'text': 'a', 'attachments': [1]}]},
    {'messages': [{'text': 'a', 'scope_id': 'web:other'}]},
    {'messages': [{'text': 'a', 'user_id': 'administrator'}]},
    {'messages': [{'text': 'a', 'metadata': {'is_owner': True}}]},
    {'messages': [{'text': 'a', 'existing_message_id': 'foreign'}]},
    {'messages': [{'text': 'a'}], 'mode': 'computer'},
    {'messages': [{'text': 'a'}], 'text': 'hidden instruction'},
    {'messages': [{'text': 'a'}], 'images': [IMAGE]},
    {'messages': [{'text': 'a'}], 'attachments': ['a' * 32]},
    {'messages': [{'text': 'a' * 12000}, {'text': 'b'}]},
    {'messages': [{'text': 'a', 'images': [IMAGE] * 3}, {'text': 'b', 'images': [IMAGE]}]},
    {'messages': [{'text': 'a', 'attachments': [str(i) * 32 for i in range(6)]}]},
])
def test_invalid_batch_rejected_before_it_can_be_observed(payload):
    with pytest.raises(ValueError):
        chat_input(payload)


def test_canonical_input_strips_text_deduplicates_references_and_retains_time():
    payload = {'messages': [{'text': ' first ', 'images': [IMAGE], 'attachments': ['a' * 32],
                             'sent_at': '2020-01-02T03:04:05+08:00'},
                            {'text': 'second', 'attachments': ['a' * 32, 'b' * 32]}]}
    result = chat_input(payload)
    assert result['text'] == 'first\nsecond' and result['images'] == [IMAGE]
    assert result['attachments'] == ['a' * 32, 'b' * 32]
    assert result['messages'][0]['sent_at'].startswith('2020-01-01T19:04:05')
    assert result['messages'][1]['sent_at'] is None
    assert all(entry['received_at'] for entry in result['messages'])
    assert chat_input({'text': 'legacy'}) is None
    assert chat_input(dict(payload, text=result['text'], images=result['images'], attachments=result['attachments']))
    with pytest.raises(ValueError):
        chat_input(payload, max_images=0)


def test_exact_boundaries_allow_eight_originals_twelve_thousand_characters_and_five_references():
    originals = [{'text': 'a' * 1499} for _ in range(7)] + [{'text': 'b' * 1500}]
    originals[0]['images'] = [IMAGE] * 3
    originals[0]['attachments'] = [str(index) * 32 for index in range(5)]
    result = chat_input({'messages': originals})
    assert len(result['messages']) == 8 and len(result['text']) == 12000
    assert len(result['images']) == 3 and len(result['attachments']) == 5


@pytest.fixture
def client(tmp_path, monkeypatch):
    app = create_app(tmp_path / 'web-batch', start_workers=False)
    calls = []

    async def complete(messages, **kwargs):
        assert kwargs['before_send']()
        calls.append((messages, kwargs))
        return {'content': '已收到完整问题。', 'message': {'role': 'assistant', 'content': '已收到完整问题。'},
                'tool_calls': [], 'usage': {}}

    monkeypatch.setattr(app.state.provider, 'complete', complete)
    with TestClient(app) as http:
        password_login(http, app)
        app.state.settings.update({'model': {'base_url': 'https://fixture.invalid/v1', 'model': 'fixture', 'vision': True},
                                   'stickers': {'enabled': False}})
        app.state.store.upsert_scope({'id': 'web:default', 'auto_memory': False, 'allow_tools': False})
        yield http, app, calls


def result_of(response, stream):
    assert response.status_code == 200, response.text
    if not stream:
        return response.json()
    events = [json.loads(line[6:]) for line in response.text.splitlines() if line.startswith('data: ')]
    assert events[0]['type'] == 'start' and events[-1]['type'] == 'result', events
    return events[-1]['data']


@pytest.mark.parametrize('stream', [False, True])
def test_http_batch_preserves_originals_with_server_owned_scope_and_identity(client, stream):
    http, app, calls = client
    payload = {'messages': [{'text': '第一条原文', 'sent_at': '2020-01-02T03:04:05+08:00'},
                            {'text': '第二条原文', 'sent_at': '2020-01-02T03:05:05+08:00'}],
               'stream': stream, 'user_id': 'forged-admin', 'is_owner': False,
               'existing_message_id': 'forged-source', 'merged_message_ids': ['forged-source']}
    result = result_of(http.post('/api/chat', json=payload), stream)
    assert result['reply'] == '已收到完整问题。' and len(calls) == 1
    records = app.state.store.list_messages('web:default')
    users = [record for record in records if record['role'] == 'user']
    assert [record['content'] for record in users] == ['第一条原文', '第二条原文']
    assert all(record['scope_id'] == 'web:default' and record['user_id'] == 'owner' for record in users)
    assert users[0]['sent_at'].startswith('2020-01-01T19:04:05')
    assert users[1]['sent_at'].startswith('2020-01-01T19:05:05')
    assert all(record['received_at'] for record in users)
    answer = app.state.store.get_message(result['message_id'])
    assert answer['metadata']['source_ids'] == [record['id'] for record in users]
    assert calls[0][0][-1]['content'] == '第一条原文\n第二条原文'
    time_context = next(message['content'] for message in calls[0][0] if str(message['content']).startswith('【当前原消息时间】'))
    assert all(record['id'] in time_context for record in users)
    assert '2020-01-02T03:04:05' in time_context and '2020-01-02T03:05:05' in time_context


@pytest.mark.parametrize('stream', [False, True])
@pytest.mark.parametrize('invalid', ['bad_second', 'cross_scope_attachment', 'unknown_attachment', 'computer', 'hidden_text', 'too_many_images'])
def test_bad_batch_never_saves_a_partial_first_record(client, stream, invalid):
    http, app, calls = client
    entries = [{'text': '这条也不能先保存'}, {'text': '第二条'}]
    payload = {'messages': entries, 'stream': stream}
    if invalid == 'bad_second':
        entries[1]['images'] = False
    elif invalid == 'cross_scope_attachment':
        item = app.state.attachments.create('web:other', 'other.txt', b'other', {'text': 'other'}, 'context')
        entries[1]['attachments'] = [item['id']]
    elif invalid == 'unknown_attachment':
        entries[1]['attachments'] = ['a' * 32]
    elif invalid == 'computer':
        payload['mode'] = 'computer'
    elif invalid == 'hidden_text':
        payload['text'] = '不可见的另一个请求'
    else:
        app.state.settings.update({'context': {'max_images': 1}})
        entries[0]['images'], entries[1]['images'] = [IMAGE], [IMAGE]
    response = http.post('/api/chat', json=payload)
    assert response.status_code in (400, 403), response.text
    assert not app.state.store.list_messages('web:default') and not calls


@pytest.mark.parametrize('stream', [False, True])
def test_attachment_reference_is_leased_before_observation_and_included_once(client, monkeypatch, stream):
    http, app, calls = client
    item = app.state.attachments.create('web:default', 'reference.txt', b'context', {'text': 'untrusted attachment text'}, 'context')
    original = app.state.chat.observe

    async def observe(*args, **kwargs):
        assert app.state.attachments._active[item['id']] == 1
        return await original(*args, **kwargs)

    monkeypatch.setattr(app.state.chat, 'observe', observe)
    payload = {'messages': [{'text': '请看资料', 'attachments': [item['id']]},
                            {'text': '回答问题', 'attachments': [item['id']]}], 'stream': stream}
    result = result_of(http.post('/api/chat', json=payload), stream)
    assert result['reply'] and len(calls) == 1
    assert sum('untrusted attachment text' in str(message['content']) for message in calls[0][0]) == 1
    assert not app.state.attachments._active


@pytest.mark.parametrize('stream', [False, True])
def test_batch_preserves_image_only_original_and_combined_multimodal_input(client, stream):
    http, app, calls = client
    payload = {'messages': [{'text': '', 'images': [IMAGE]}, {'text': '图里是什么'}], 'stream': stream}
    result = result_of(http.post('/api/chat', json=payload), stream)
    assert result['reply'] and len(calls) == 1
    originals = [record for record in app.state.store.list_messages('web:default') if record['role'] == 'user']
    assert originals[0]['content'] == '' and originals[0]['metadata']['images'] == [IMAGE]
    content = calls[0][0][-1]['content']
    assert content == [{'type': 'text', 'text': '图里是什么'}, {'type': 'image_url', 'image_url': {'url': IMAGE}}]


@pytest.mark.parametrize('stream', [False, True])
def test_clearing_batch_sources_while_model_waits_prevents_answer_commit(client, monkeypatch, stream):
    http, app, _ = client
    entered, proceed = threading.Event(), threading.Event()

    async def complete(messages, **kwargs):
        assert kwargs['before_send']()
        entered.set()
        while not proceed.is_set():
            await asyncio.sleep(.01)
        return {'content': '不得写入这条旧答复', 'tool_calls': [], 'usage': {}}

    monkeypatch.setattr(app.state.provider, 'complete', complete)
    responses = []
    worker = threading.Thread(target=lambda: responses.append(http.post('/api/chat',
        json={'messages': [{'text': '第一条'}, {'text': '第二条'}], 'stream': stream})))
    worker.start()
    try:
        assert entered.wait(4)
        assert http.delete('/api/messages').status_code == 200
    finally:
        proceed.set()
        worker.join(5)
    assert not worker.is_alive() and responses
    response = responses[0]
    if stream:
        events = [json.loads(line[6:]) for line in response.text.splitlines() if line.startswith('data: ')]
        assert events[-1]['type'] == 'error'
        assert not any(event['type'] == 'result' for event in events)
    else:
        assert response.status_code == 400, response.text
    assert not app.state.store.list_messages('web:default')


def test_stream_batch_logout_cancels_producer_and_never_commits_answer(client, monkeypatch):
    http, app, _ = client
    entered, cancelled = threading.Event(), threading.Event()

    async def complete(messages, **kwargs):
        assert kwargs['before_send']()
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    monkeypatch.setattr(app.state.provider, 'complete', complete)
    responses = []
    worker = threading.Thread(target=lambda: responses.append(http.post('/api/chat',
        json={'messages': [{'text': '第一条'}, {'text': '第二条'}], 'stream': True})))
    worker.start()
    try:
        assert entered.wait(4)
        assert http.post('/api/auth/logout').status_code == 200
        worker.join(4)
        assert not worker.is_alive() and cancelled.is_set()
        events = [json.loads(line[6:]) for line in responses[0].text.splitlines() if line.startswith('data: ')]
        assert events[-1]['type'] == 'error' and events[-1]['code'] == 'authentication_required'
        assert not any(message['role'] == 'assistant' for message in app.state.store.list_messages('web:default'))
        assert not app.state.attachments._active
    finally:
        if worker.is_alive():
            http.post('/api/auth/logout')
            worker.join(4)


def test_time_endpoint_requires_auth_reports_effective_timezone_and_disable_switch(client):
    http, app, _ = client
    app.state.settings.update({'time': {'timezone': 'America/New_York', 'awareness_enabled': False}})
    response = http.get('/api/time')
    assert response.status_code == 200
    assert response.headers['cache-control'] == 'no-store'
    info = response.json()
    assert info['timezone'] == 'America/New_York' and info['awareness_enabled'] is False
    instant = datetime.fromisoformat(info['now'])
    assert abs((datetime.now(timezone.utc) - instant).total_seconds()) < 5
    http.cookies.clear()
    assert http.get('/api/time').status_code == 401


def test_wrong_platform_is_rejected_without_discovering_a_new_scope(client):
    http, app, calls = client
    before = app.state.store.list_scopes()
    response = http.post('/api/chat', json={'scope_id': 'qq:private:123456', 'messages': [{'text': 'forbidden'}]})
    assert response.status_code == 400
    assert app.state.store.list_scopes() == before and not calls
