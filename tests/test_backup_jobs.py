"""Background export is bounded, cancellable, private, and restorable."""
import asyncio
import io
import json
import threading
import zipfile

import pytest
from fastapi.testclient import TestClient

from auth_helpers import password_login
from shiori.api import create_app
from shiori.backup_archives import BackupExports, extract_archive, validate_archive
from shiori.db import Store


def test_redaction_does_not_hold_live_database_lock(tmp_path):
    store = Store(tmp_path / 'live')
    arrived, release = threading.Event(), threading.Event()
    result = []
    def progress(phase, done, total):
        if phase == 'sanitize' and not arrived.is_set():
            arrived.set()
            assert release.wait(5)
    def export():
        try:
            result.append(store.backup(tmp_path / 'copy.db', progress=progress))
        except Exception as exc:
            result.append(exc)
    worker = threading.Thread(target=export)
    try:
        worker.start()
        assert arrived.wait(5)
        assert store._lock.acquire(timeout=1), 'redaction blocked live DB'
        try:
            store.add_message('web:default', 'owner', 'user', 'written after snapshot')
        finally:
            store._lock.release()
        release.set()
        worker.join(5)
        assert result == [tmp_path / 'copy.db']
        assert store.restore_from(result[0], validate_only=True)['valid']
    finally:
        release.set()
        worker.join(5)
        store.close()


def test_candidate_validation_does_not_hold_live_database_lock(tmp_path, monkeypatch):
    store = Store(tmp_path / 'live')
    candidate = store.backup(tmp_path / 'candidate.db')
    arrived, release = threading.Event(), threading.Event()
    original_loads, result = json.loads, []
    def loads(value, *args, **kwargs):
        if threading.current_thread() is worker and not arrived.is_set():
            arrived.set()
            assert release.wait(5)
        return original_loads(value, *args, **kwargs)
    def validate():
        try:
            result.append(store.restore_from(candidate, validate_only=True))
        except Exception as exc:
            result.append(exc)
    worker = threading.Thread(target=validate)
    monkeypatch.setattr(json, 'loads', loads)
    try:
        worker.start()
        assert arrived.wait(5)
        assert store._lock.acquire(timeout=1)
        store._lock.release()
        release.set()
        worker.join(5)
        assert result == [{'valid': True}]
    finally:
        release.set()
        worker.join(5)
        store.close()


def test_progress_cancel_and_restart_have_no_live_effect(tmp_path, monkeypatch):
    asyncio.run(_progress_cancel_and_restart(tmp_path, monkeypatch))


async def _progress_cancel_and_restart(tmp_path, monkeypatch):
    store = Store(tmp_path)
    arrived, release = threading.Event(), threading.Event()
    original = store.backup
    def delayed(*args, **kwargs):
        arrived.set()
        assert release.wait(5)
        return original(*args, **kwargs)
    monkeypatch.setattr(store, 'backup', delayed)
    manager = BackupExports(tmp_path, store, lambda: {'settings.json': '{}'})
    try:
        started = manager.start()
        assert len(started['id']) == 32 and started['status'] == 'running'
        assert await asyncio.to_thread(arrived.wait, 5)
        assert manager.snapshot()['can_cancel']
        with pytest.raises(ValueError): manager.start()
        manager.cancel(started['id'])
        release.set()
        cancelled = await manager.wait(started['id'])
        assert cancelled['status'] == 'cancelled'
        assert not list(tmp_path.glob('.backup-export-*'))
        second = manager.start()
        assert second['id'] != started['id']
        completed = await manager.wait(second['id'])
        assert completed['status'] == 'completed'
        assert completed['progress'] == 100
        assert validate_archive(manager._path) > 0
        restored = tmp_path / 'restored'
        restored.mkdir()
        assert store.restore_from(extract_archive(manager._path, restored)['qunyu.db'], validate_only=True)
    finally:
        release.set()
        await manager.close()
        store.close()


@pytest.mark.parametrize('target', ['job', 'close'])
def test_repeated_export_cancellation_keeps_thread_files_until_finished(tmp_path, monkeypatch, target):
    async def scenario():
        store = Store(tmp_path)
        entered, release, finished = threading.Event(), threading.Event(), threading.Event()
        original = store.backup
        def delayed(destination, **kwargs):
            entered.set()
            try:
                assert release.wait(10)
                assert destination.parent.is_dir(), 'export directory removed under active thread'
                return original(destination, **kwargs)
            finally:
                finished.set()
        monkeypatch.setattr(store, 'backup', delayed)
        manager = BackupExports(tmp_path, store, lambda: {'settings.json': '{}'})
        try:
            manager.start()
            assert await asyncio.to_thread(entered.wait, 5)
            task = manager._task if target == 'job' else asyncio.create_task(manager.close())
            await asyncio.sleep(.01)
            task.cancel()
            await asyncio.sleep(.01)
            task.cancel()
            await asyncio.sleep(.01)
            assert not task.done() and manager.busy()
            assert list(tmp_path.glob('.backup-export-*'))
            release.set()
            await task
            assert finished.is_set()
            assert manager.snapshot()['status'] == 'cancelled'
            assert not list(tmp_path.glob('.backup-export-*'))
        finally:
            release.set()
            await manager.close()
            store.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('cancelled', [False, True])
def test_disconnected_backup_download_releases_lease(tmp_path, cancelled):
    async def scenario():
        store = Store(tmp_path)
        manager = BackupExports(tmp_path, store, lambda: {'settings.json': '{}'})
        try:
            job = manager.start()
            await manager.wait(job['id'])
            response = manager.download(job['id'])
            assert manager._downloads == 1
            with pytest.raises(ValueError):
                manager.start()
            async def send(message):
                raise asyncio.CancelledError() if cancelled else OSError('fixture: disconnected')
            async def receive():
                return {'type': 'http.disconnect'}
            if cancelled:
                # Starlette's task group may consume disconnect cancellation;
                # either way the lease must be released by response finalization.
                try:
                    await response({'type': 'http', 'method': 'GET', 'headers': []}, receive, send)
                except asyncio.CancelledError:
                    pass
            else:
                with pytest.raises(OSError):
                    await response({'type': 'http', 'method': 'GET', 'headers': []}, receive, send)
            assert manager._downloads == 0
            fresh = manager.start()
            assert fresh['id'] != job['id']
            assert (await manager.wait(fresh['id']))['status'] == 'completed'
        finally:
            await manager.close()
            store.close()
    asyncio.run(scenario())


def test_duplicate_and_oversize_archive_rejected_before_extraction(tmp_path, monkeypatch):
    from shiori import backup_archives
    duplicate = tmp_path / 'duplicate.zip'
    with zipfile.ZipFile(duplicate, 'w') as archive:
        archive.writestr('qunyu.db', b'fake')
        with pytest.warns(UserWarning): archive.writestr('qunyu.db', b'fake')
    with pytest.raises(ValueError): extract_archive(duplicate, tmp_path)
    assert not (tmp_path / 'qunyu.db').exists()
    expanded = tmp_path / 'expanded.zip'
    with zipfile.ZipFile(expanded, 'w', zipfile.ZIP_DEFLATED) as archive:
        archive.writestr('qunyu.db', b'0' * 10000)
    monkeypatch.setattr(backup_archives, 'MAX_EXPANDED_BYTES', 1024)
    with pytest.raises(ValueError): extract_archive(expanded, tmp_path)
    assert not (tmp_path / 'qunyu.db').exists()


def test_backup_job_api_auth_progress_download_and_restore(tmp_path):
    app = create_app(tmp_path / 'app', start_workers=False)
    with TestClient(app) as client:
        for url in ['/api/backup/jobs/current', '/api/backup/jobs/missing/file']:
            assert client.get(url).status_code == 401
        assert client.post('/api/backup/jobs').status_code == 401
        password_login(client, app)
        assert client.get('/api/backup/jobs/current').json()['status'] == 'idle'
        result = client.post('/api/backup/jobs')
        assert result.status_code == 200, result.text
        identifier = result.json()['id']
        completed = client.portal.call(app.state.backup_exports.wait, identifier)
        assert completed['status'] == 'completed', completed
        downloaded = client.get(completed['file_url'])
        assert downloaded.status_code == 200
        assert client.post('/api/backup/jobs/wrong/cancel').status_code == 404
        restored = client.post('/api/backup/restore', files={'file': ('backup.zip', downloaded.content)})
        assert restored.status_code == 200, restored.text


def test_restore_upload_larger_than_general_request_limit_uses_disk(tmp_path):
    app = create_app(tmp_path / 'app', start_workers=False)
    with TestClient(app) as client:
        password_login(client, app)
        original = zipfile.ZipFile(io.BytesIO(client.get('/api/backup').content))
        # Two legitimate JSON files add more than the generic 32 MiB envelope;
        # their per-file limit and 1 GiB expanded contract still apply.
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, 'w', zipfile.ZIP_STORED) as archive:
            for name in original.namelist():
                raw = original.read(name)
                if name in ('settings.json', 'manifest.json'):
                    raw += b' ' * (16 * 1024 * 1024 - len(raw))
                archive.writestr(name, raw)
        assert buffer.tell() > 32 * 1024 * 1024
        restored = client.post('/api/backup/restore', files={'file': ('large.zip', buffer.getvalue())})
        assert restored.status_code == 200, restored.text
