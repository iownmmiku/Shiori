import json
from types import SimpleNamespace

import pytest

from shiori import update_storage


def manager(tmp_path):
    root = tmp_path / 'updates'
    root.mkdir()
    return SimpleNamespace(download={'id':'f'*32}, _safe_folder=lambda: root)


def job(manager, key, *, status='ready', at='2026-10-01', backup=False):
    folder = manager._safe_folder() / key
    folder.mkdir()
    (folder/'update-artifact.json').write_text(json.dumps({'id':key,'tag':'v0.2.0','status':status,'started_at':at}), 'utf-8')
    (folder/'program.zip').write_bytes(b'archive')
    if backup:
        (folder/'data-backup').mkdir()
        (folder/'data-backup'/'qunyu.db').write_bytes(b'data')
        (folder/'switch.json').write_text('{}')
        (folder/'outcome.json').write_text(json.dumps({'download_id':key,'status':status,'at':at}))
    return folder


def test_storage_preserves_current_latest_recovery_and_unknown_jobs(tmp_path):
    mgr = manager(tmp_path)
    removable = job(mgr, 'a'*32, status='completed', backup=True)
    latest = job(mgr, 'b'*32, status='completed', backup=True, at='2026-10-02')
    active = job(mgr, 'f'*32)
    failed = job(mgr, 'c'*32, status='failed', backup=True)
    unknown = mgr._safe_folder()/('d'*32); unknown.mkdir(); (unknown/'important.txt').write_text('keep')
    items = {row['id']:row for row in update_storage.inventory(mgr)['items']}
    assert items['a'*32]['can_delete']
    assert all(not items[key*32]['can_delete'] for key in ('b','f','c','d'))
    result = update_storage.cleanup(mgr, [items['a'*32]])
    assert result['released_bytes'] > 0 and not removable.exists()
    assert all(folder.exists() for folder in (latest,active,failed,unknown))


def test_storage_rejects_stale_preview_and_paths_before_deletion(tmp_path):
    mgr = manager(tmp_path); folder = job(mgr, 'a'*32)
    item = update_storage.inventory(mgr)['items'][0]
    (folder/'new.txt').write_text('arrived after preview')
    with pytest.raises(ValueError, match='状态已改变'): update_storage.cleanup(mgr, [item])
    for key in ('../outside','A'*32,'a'*32+'/..'):
        with pytest.raises(ValueError): update_storage.cleanup(mgr, [{'id':key,'fingerprint':'x'}])
    assert folder.exists()


def test_storage_refuses_linked_descendant_without_following_or_deleting(tmp_path, monkeypatch):
    mgr = manager(tmp_path); folder = job(mgr, 'a'*32)
    suspect = folder/'outside-link'; suspect.mkdir()
    (suspect/'keep').write_text('untouched')
    original = update_storage.is_link
    monkeypatch.setattr(update_storage, 'is_link', lambda path: path == suspect or original(path))
    item = update_storage.inventory(mgr)['items'][0]
    assert not item['can_delete']
    with pytest.raises(ValueError): update_storage.cleanup(mgr, [dict(item, fingerprint='x')])
    assert (suspect/'keep').read_text() == 'untouched'


def test_storage_api_auth_remote_policy_and_active_job_protection(tmp_path):
    from fastapi.testclient import TestClient
    from auth_helpers import FIXTURE_PASSWORD, password_login
    from shiori.api import create_app
    app = create_app(tmp_path/'app', start_workers=False, allow_remote=True)
    with TestClient(app) as client:
        assert client.get('/api/updates/storage').status_code == 401
        password_login(client, app)
        mgr = app.state.updates
        folder = job(mgr, 'a'*32)
        item = client.get('/api/updates/storage').json()['items'][0]
        app.state.settings.update({'webui':{'remote_enabled':True}})
        remote = TestClient(app, base_url='https://192.168.1.2:8765', client=('192.168.1.10',1234))
        assert remote.post('/api/auth/login',json={'password':FIXTURE_PASSWORD}).status_code == 200
        assert not remote.get('/api/updates/storage').json()['cleanup_allowed']
        assert remote.post('/api/updates/storage/cleanup',json={'items':[item]}).status_code == 403
        mgr._cleaning = True
        assert client.post('/api/updates/storage/cleanup',json={'items':[item]}).status_code == 400
        mgr._cleaning = False
        assert folder.exists()
        response = client.post('/api/updates/storage/cleanup',json={'items':[item]})
        assert response.status_code == 200 and not folder.exists()


@pytest.mark.parametrize('worker_fails', [False, True])
@pytest.mark.parametrize('cancel_scope', [False, True])
def test_cancelled_cleanup_keeps_lock_until_worker_finishes(tmp_path, monkeypatch, worker_fails, cancel_scope):
    import asyncio
    import threading

    import anyio
    import httpx
    from fastapi import FastAPI
    from shiori.updates import install_updates

    app = FastAPI()
    app.state.settings = SimpleNamespace(get=lambda: {})
    app.state.shutdown = None
    install_updates(app, tmp_path)
    mgr = app.state.updates
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()
    held = {}
    items = [{'id': 'a' * 32, 'fingerprint': 'fixture'}]

    def blocked_cleanup(received_manager, received_items):
        assert received_manager is mgr and received_items == items
        entered.set()
        try:
            assert release.wait(10)
            held['cleaning_at_finish'] = mgr._cleaning
            if worker_fails:
                raise OSError('fixture: cleanup failed')
            return {'released_bytes': 0}
        finally:
            finished.set()

    monkeypatch.setattr(update_storage, 'cleanup', blocked_cleanup)

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                     base_url='http://testserver') as http:
            async def cleanup_request():
                with anyio.CancelScope() as scope:
                    held['scope'] = scope
                    return await http.post('/api/updates/storage/cleanup', json={'items': items})

            request = asyncio.create_task(cleanup_request())
            try:
                assert await asyncio.to_thread(entered.wait, 5)
                if cancel_scope:
                    held['scope'].cancel()
                else:
                    request.cancel()
                await asyncio.sleep(.02)
                for _ in range(2):
                    request.cancel()
                    await asyncio.sleep(.02)
                    assert not request.done(), 'cancellation abandoned the cleanup worker'
                    assert mgr._cleaning and not finished.is_set()
                    with pytest.raises(ValueError, match='正在清理'):
                        mgr.start_download(1, 'source')
                release.set()
                if cancel_scope:
                    await request  # the owning AnyIO scope consumes its cancellation
                else:
                    with pytest.raises(asyncio.CancelledError):
                        await request
                assert finished.is_set() and held['cleaning_at_finish']
                assert not mgr._cleaning
            finally:
                release.set()
                await asyncio.gather(request, return_exceptions=True)
                assert await asyncio.to_thread(finished.wait, 5)

    asyncio.run(scenario())
