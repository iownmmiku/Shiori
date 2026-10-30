"""Actual HTTP cancellation cannot release maintenance over a live DB worker."""
import asyncio
import threading

import httpx
import pytest
from fastapi.testclient import TestClient

from auth_helpers import password_login
from shiori import backup_archives
from shiori.api import create_app


@pytest.mark.parametrize('phase', ['upload', 'extract', 'validate', 'commit', 'rollback'])
def test_cancelled_restore_drains_threads_and_keeps_maintenance(tmp_path, monkeypatch, phase):
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()
    held = {}
    original_spool = backup_archives.spool_upload
    def spool(source, destination):
        if phase != 'upload':
            return original_spool(source, destination)
        held.update(source=source, folder=destination.parent)
        entered.set()
        try:
            assert release.wait(10)
            assert not source.closed and destination.parent.exists()
            return original_spool(source, destination)
        finally:
            finished.set()
    monkeypatch.setattr(backup_archives, 'spool_upload', spool)
    original_extract = backup_archives.extract_archive
    def extract(path, folder):
        if phase != 'extract':
            return original_extract(path, folder)
        held.update(folder=folder)
        entered.set()
        try:
            assert release.wait(10)
            assert path.is_file() and folder.is_dir()
            return original_extract(path, folder)
        finally:
            finished.set()
    monkeypatch.setattr(backup_archives, 'extract_archive', extract)
    app = create_app(tmp_path / 'app', start_workers=False)
    with TestClient(app) as client:
        password_login(client, app)
        store = app.state.store
        old = store.create_memory({'scope_id': 'web:default', 'user_id': 'owner', 'content': 'snapshot record'})
        archive = client.get('/api/backup').content
        live = store.create_memory({'scope_id': 'web:default', 'user_id': 'owner', 'content': 'current live record'})
        original_restore = store.restore_from
        def restore(candidate, *, validate_only=False):
            trigger = ((phase == 'validate' and validate_only) or
                       (phase in ('commit', 'rollback') and not validate_only and
                        not candidate.name.startswith('before-restore-')))
            if not trigger:
                return original_restore(candidate, validate_only=validate_only)
            held.update(folder=candidate.parent)
            entered.set()
            try:
                assert release.wait(10)
                assert candidate.is_file(), 'candidate was cleaned under its active thread'
                result = original_restore(candidate, validate_only=validate_only)
                if phase == 'rollback':
                    raise OSError('fixture: fail after database replacement')
                return result
            finally:
                finished.set()
        monkeypatch.setattr(store, 'restore_from', restore)

        async def scenario():
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                         base_url='http://testserver', cookies=client.cookies) as http:
                request = asyncio.create_task(http.post('/api/backup/restore',
                    files={'file': ('backup.zip', archive, 'application/zip')}))
                try:
                    assert await asyncio.to_thread(entered.wait, 5)
                    request.cancel()
                    await asyncio.sleep(.05)
                    request.cancel()  # a second disconnect must also drain
                    await asyncio.sleep(.01)
                    assert not request.done(), 'HTTP cancellation abandoned the restore worker'
                    assert held['folder'].is_dir()
                    if phase == 'upload':
                        assert not held['source'].closed
                    busy = await http.get('/api/memories')
                    assert busy.status_code == 503, busy.text
                    assert store.get_memory(live['id'])['content'] == 'current live record'
                    release.set()
                    with pytest.raises(asyncio.CancelledError):
                        await request
                    assert finished.is_set()
                    assert app.state.backup_restore is None
                    assert not held['folder'].exists(), 'private restore temporary files were retained'
                    available = await http.get('/api/memories')
                    assert available.status_code == 200, available.text
                    ids = {row['id'] for row in available.json()}
                    assert old['id'] in ids
                    assert (live['id'] in ids) == (phase != 'commit')
                    assert app.state.settings.get()['paused'] == (phase == 'commit')
                finally:
                    release.set()
                    if not request.done():
                        with pytest.raises(asyncio.CancelledError):
                            await request
        try:
            client.portal.call(scenario)
        finally:
            release.set()
