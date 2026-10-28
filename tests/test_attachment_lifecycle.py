"""Temporary references and permanent documents have separate lifetimes."""
import asyncio
from datetime import timedelta
import json
from types import SimpleNamespace
import threading

from fastapi.testclient import TestClient
import pytest

from auth_helpers import password_login
from shiori.api import create_app
from shiori.attachments import AttachmentLibrary, bounded_parse
from shiori.db import Store


@pytest.fixture
def library(tmp_path):
    store = Store(tmp_path)
    runs = SimpleNamespace(_lock=threading.RLock(), _jobs={})
    result = AttachmentLibrary(tmp_path, store, runs)
    yield result
    store.close()


@pytest.fixture
def client(tmp_path):
    app = create_app(tmp_path / 'http', start_workers=False)
    with TestClient(app) as client:
        password_login(client, app)
        yield client, app


def upload(client, text='fixture', purpose='context', scope_id='web:default'):
    return client.post('/api/chat/attachments', data={'scope_id': scope_id, 'purpose': purpose},
                       files={'file': ('reference.txt', text.encode(), 'text/plain')})


def create(library, text='fixture', purpose='context'):
    return library.create('web:default', 'fixture.txt', text.encode(), {'text': text}, purpose)


def test_reference_limit_is_validated_at_upload_without_orphan(client):
    client, app = client
    assert upload(client, 'a' * 80001).status_code == 400
    assert not list(app.state.attachments.root.glob('*.json'))
    accepted = upload(client, 'a' * 80000)
    assert accepted.status_code == 200 and accepted.json()['characters'] == 80000
    assert upload(client, 'b' * 80001, 'memory').status_code == 200
    assert len(list(app.state.attachments.root.glob('*.json'))) == 1


def test_input_aggregate_limit_does_not_apply_to_owned_delete(client):
    client, app = client
    first, second = upload(client, 'a' * 40001).json(), upload(client, 'b' * 40001).json()
    response = client.post('/api/chat', json={'text': 'combine', 'attachments': [first['id'], second['id']]})
    assert response.status_code == 400
    path = app.state.attachments.root / (first['id'] + '.json')
    record = json.loads(path.read_text('utf-8'))
    record['text'] = 'legacy oversized body' * 10000
    record.pop('purpose')
    path.write_text(json.dumps(record), 'utf-8')
    assert client.get(f"/api/chat/attachments/{first['id']}").status_code == 200
    assert client.delete(f"/api/chat/attachments/{first['id']}").status_code == 200
    assert not path.exists()


def test_all_management_requires_auth_and_scope_ownership(client):
    client, app = client
    item = upload(client).json()
    other = client.post('/api/scopes', json={'name': 'other'}).json()['id']
    base = '/api/chat/attachments/' + item['id']
    assert client.get(base, params={'scope_id': other}).status_code == 403
    assert client.delete(base, params={'scope_id': other}).status_code == 403
    assert client.post('/api/chat/attachments/release', json={'scope_id': other, 'ids': [item['id']]}).status_code == 403
    assert client.get('/api/chat/attachments', params={'scope_id': other}).json()['items'] == []
    assert client.get('/api/chat/attachments/not-a-valid-id').status_code == 400
    with pytest.raises(ValueError, match='标识无效'):
        app.state.attachments.preview('web:default', '../../settings')
    client.cookies.clear()
    for method, url in [('GET', '/api/chat/attachments'), ('GET', base),
                        ('POST', '/api/chat/attachments/release'), ('POST', '/api/chat/attachments/cleanup')]:
        assert client.request(method, url).status_code == 401


def test_permanent_documents_do_not_exhaust_temporary_quota(library):
    for _ in range(201):
        item = create(library, purpose='memory')
        memory = library.store.get_memory(item['memory_id'])
        assert memory['attachment_id'] == item['id'] and memory['file_hash'] == item['sha256']
        library.store.delete_memory(item['memory_id'])
    assert not list(library.root.glob('*.json'))
    library.collect()
    assert not list(library.imports.glob('*.json'))
    assert create(library)['purpose'] == 'context'


def test_memory_receipt_has_no_body_and_deleting_receipt_preserves_note(library):
    item = create(library, 'permanent text', 'memory')
    receipt = json.loads((library.imports / (item['id'] + '.json')).read_text('utf-8'))
    assert 'text' not in receipt and receipt['memory_id'] == item['memory']['id']
    library.delete('web:default', item['id'])
    assert library.store.get_memory(item['memory_id'])['status'] == 'active'


def test_atomic_failure_removes_partial_file_and_rolls_back_active_note(library, monkeypatch):
    def fail(*args):
        raise OSError('fixture disk unavailable')
    monkeypatch.setattr(library, '_write', fail)
    for purpose in ('context', 'memory'):
        with pytest.raises(OSError):
            create(library, purpose=purpose)
    assert not list(library.root.glob('*')) and not list(library.imports.glob('*'))
    assert not library.store.list_memories(status='active')


def test_released_live_request_survives_gc_then_is_collected(library):
    item = create(library)
    with library.request():
        sources = library.resolve('web:default', [item['id']])
        assert library.release('web:default', [item['id']])['retained'] == [item['id']]
        library.collect()
        assert library.current('web:default', sources)
        assert library.list('web:default')['items'][0]['in_use']
    assert not library.current('web:default', sources)
    assert not library._active


def test_release_retains_queued_job_and_explicit_delete_revokes_it(library):
    item = create(library)
    references = library.resolve('web:default', [item['id']])
    library.runs._jobs['job'] = {'id': 'job', 'status': 'awaiting_approval',
                                '_payload': {'attachments': references}}
    assert library.release('web:default', [item['id']])['retained'] == [item['id']]
    assert library.current('web:default', references)
    assert library.list('web:default')['items'][0]['job_ids'] == ['job']
    library.delete('web:default', item['id'])
    assert not library.current('web:default', references)


def test_terminal_job_releases_reference_and_repeated_release_is_safe(library):
    item = create(library)
    library.runs._jobs['job'] = {'id': 'job', 'status': 'queued', '_payload': {'attachments': [item]}}
    library.release('web:default', [item['id']])
    library.runs._jobs['job']['status'] = 'completed'
    assert library.collect() == [item['id']]
    assert library.release('web:default', [item['id']]) == {'ok': True, 'deleted': [], 'retained': []}


def test_restart_management_and_legacy_unknown_are_conservative(library):
    current = create(library)
    path = library.root / ('a' * 32 + '.json')
    legacy = dict(id='a' * 32, name='legacy.txt', scope_id='web:default', user_id='owner',
                  text='legacy private text', sha256='fixture', created_at='2000-01-01T00:00:00+00:00',
                  expires_at='2000-01-02T00:00:00+00:00')
    path.write_text(json.dumps(legacy), 'utf-8')
    restarted = AttachmentLibrary(library.store.data_dir, library.store, library.runs)
    listed = restarted.list('web:default')
    assert listed['total'] == 2 and all('text' not in item for item in listed['items'])
    assert next(item for item in listed['items'] if item['id'] == legacy['id'])['purpose'] == 'legacy_unknown'
    assert restarted.cleanup('web:default', preview=False)['deleted'] == []
    assert restarted.release('web:default', [legacy['id']])['retained'] == [legacy['id']]
    assert path.exists()
    assert restarted.preview('web:default', legacy['id'])['preview'] == 'legacy private text'
    restarted.delete('web:default', legacy['id'])
    assert not path.exists() and (library.root / (current['id'] + '.json')).exists()


def test_expiry_cleanup_preview_is_nonmutating_and_protects_job(library, monkeypatch):
    old, protected = create(library), create(library)
    library.runs._jobs['queued'] = {'id': 'queued', 'status': 'queued', '_payload': {'attachments': [protected]}}
    future = library._now() + timedelta(days=2)
    monkeypatch.setattr(library, '_now', lambda: future)
    assert library.cleanup('web:default')['eligible_ids'] == [old['id']]
    assert (library.root / (old['id'] + '.json')).exists()
    assert library.cleanup('web:default', preview=False)['deleted'] == [old['id']]
    assert (library.root / (protected['id'] + '.json')).exists()


@pytest.mark.parametrize('stream', [False, True])
def test_real_http_request_holds_lease_while_client_releases_reference(client, monkeypatch, stream):
    client, app = client
    item = upload(client).json()
    client.put('/api/settings', json={'model': {'base_url': 'https://fixture.invalid/v1', 'model': 'fixture'}})
    entered, proceed = threading.Event(), threading.Event()
    async def complete(messages, **kwargs):
        assert kwargs['before_send']()
        entered.set()
        for _ in range(200):
            if proceed.is_set():
                break
            await asyncio.sleep(.01)
        assert kwargs['before_send'](), 'automatic release must not revoke the live source'
        return {'content': 'completed fixture', 'usage': {'input_tokens': 1, 'output_tokens': 1}}
    monkeypatch.setattr(app.state.provider, 'complete', complete)
    responses = []
    worker = threading.Thread(target=lambda: responses.append(client.post('/api/chat',
        json={'text': 'summarize', 'attachments': [item['id']], 'stream': stream})))
    worker.start()
    try:
        assert entered.wait(3)
        result = client.post('/api/chat/attachments/release', json={'ids': [item['id']]}).json()
        assert result['retained'] == [item['id']]
        assert (app.state.attachments.root / (item['id'] + '.json')).exists()
    finally:
        proceed.set()
        worker.join(5)
    assert not worker.is_alive() and responses[0].status_code == 200
    assert 'completed fixture' in responses[0].text
    assert not (app.state.attachments.root / (item['id'] + '.json')).exists()


def test_repeated_parse_cancellation_does_not_release_slot_before_thread_finishes():
    entered, finish, next_entered = threading.Event(), threading.Event(), threading.Event()
    def slow():
        entered.set()
        finish.wait(5)
        return 'first'
    async def scenario():
        slot = asyncio.Semaphore(1)
        async def parse(function):
            async with slot:
                return await bounded_parse(function)
        first = asyncio.create_task(parse(slow))
        second = None
        try:
            for _ in range(200):
                if entered.is_set():
                    break
                await asyncio.sleep(.005)
            assert entered.is_set()
            first.cancel()
            await asyncio.sleep(.01)
            first.cancel()
            second = asyncio.create_task(parse(lambda: next_entered.set()))
            await asyncio.sleep(.02)
            assert not next_entered.is_set() and not first.done()
        finally:
            finish.set()
        with pytest.raises(asyncio.CancelledError):
            await first
        if second:
            await asyncio.wait_for(second, 2)
        assert next_entered.is_set()
    asyncio.run(scenario())
