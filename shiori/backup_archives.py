"""Bounded, cancellable backup files. No model calls and no secret export."""
from __future__ import annotations

import asyncio
import logging
import secrets
import shutil
import tempfile
import threading
import zipfile
from datetime import datetime
from pathlib import Path


MAX_ARCHIVE_BYTES = 256 * 1024 * 1024
MAX_EXPANDED_BYTES = 1024 * 1024 * 1024
MAX_JSON_BYTES = 16 * 1024 * 1024
CHUNK = 1024 * 1024
DISK_RESERVE = 32 * 1024 * 1024
ALLOWED = {'qunyu.db', 'settings.json', 'manifest.json', 'persona_knowledge.json',
           'tool_history.json', 'jobs_history.json'}


async def drain_task(task):
    """Wait for owned work even after ASGI/AnyIO or asyncio cancellation.

    Cancellation of an await does not stop to_thread's OS thread. Owners must
    drain before closing its files, releasing maintenance, or closing Store.
    The original caller decides whether to propagate cancellation/results.
    """
    import anyio
    with anyio.CancelScope(shield=True):
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                continue
            except Exception:
                break
        if not task.cancelled():
            task.exception()  # consume failure if its HTTP caller went away


async def restore_thread(function, *args, **kwargs):
    """A cancelled restore cannot delete files underneath an active thread."""
    task = asyncio.create_task(asyncio.to_thread(function, *args, **kwargs))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        await drain_task(task)
        raise


def require_space(folder, amount):
    if shutil.disk_usage(folder).free < amount + DISK_RESERVE:
        raise ValueError('备份临时磁盘空间不足，请清理空间后重试')


def validate_archive(path):
    """Validate the same contract for exported and imported archives."""
    if path.stat().st_size > MAX_ARCHIVE_BYTES:
        raise ValueError('备份压缩包超过 256 MiB 限制')
    with zipfile.ZipFile(path) as archive:
        members = archive.infolist()
        names = [item.filename for item in members]
        if (set(names) - ALLOWED or 'qunyu.db' not in names or len(names) != len(set(names))):
            raise ValueError('备份内容不符合格式')
        total = 0
        for item in members:
            if (item.is_dir() or item.flag_bits & 1 or item.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED)
                    or (item.external_attr >> 16) & 0o170000 == 0o120000):
                raise ValueError('备份含有不支持的文件类型或加密内容')
            if item.file_size < 0 or item.file_size > (MAX_EXPANDED_BYTES if item.filename == 'qunyu.db' else MAX_JSON_BYTES):
                raise ValueError('备份文件超过容量限制（数据库 1 GiB，单个配置或历史文件 16 MiB）')
            total += item.file_size
        if total > MAX_EXPANDED_BYTES:
            raise ValueError('备份解压总量超过 1 GiB 限制')
        return total


def spool_upload(source, destination):
    """UploadFile already spills multipart to disk; never read it all again."""
    source.seek(0)
    total = 0
    with destination.open('wb') as target:
        while chunk := source.read(CHUNK):
            total += len(chunk)
            if total > MAX_ARCHIVE_BYTES:
                raise ValueError('备份压缩包超过 256 MiB 限制')
            require_space(destination.parent, len(chunk))
            target.write(chunk)
    return total


def extract_archive(path, folder):
    total = validate_archive(path)
    require_space(folder, total)
    output = {}
    with zipfile.ZipFile(path) as archive:
        for item in archive.infolist():
            destination = folder / item.filename
            read = 0
            with archive.open(item) as source, destination.open('xb') as target:
                while chunk := source.read(CHUNK):
                    read += len(chunk)
                    if read > item.file_size:
                        raise ValueError('备份解压长度与声明不一致')
                    target.write(chunk)
            if read != item.file_size:
                raise ValueError('备份文件不完整')
            output[item.filename] = destination
    return output


class BackupExports:
    """One bounded job and file, with progress owned by this process only."""

    def __init__(self, data_dir, store, auxiliary):
        self.data_dir, self.store, self.auxiliary = Path(data_dir), store, auxiliary
        self._lock = threading.Lock()
        self._cancel = threading.Event()
        self._task = self._temporary = self._path = None
        self._downloads = 0
        self._state = {'id': '', 'status': 'idle', 'phase': '', 'message': '尚未生成备份',
                       'progress': 0, 'processed': 0, 'total': 0, 'size': 0,
                       'file_url': '', 'can_cancel': False}

    def snapshot(self):
        with self._lock:
            return dict(self._state)

    def busy(self):
        return self._task is not None and not self._task.done()

    def _set(self, **values):
        with self._lock:
            self._state.update(values)

    def _check(self):
        if self._cancel.is_set():
            raise InterruptedError('备份已取消')

    def _report(self, phase, processed=0, total=0):
        self._check()
        base, span, message = {
            'snapshot': (0, 30, '正在复制数据库快照'),
            'sanitize': (30, 30, '正在移除备份中的密钥'),
            'compact': (60, 5, '正在整理备份数据库'),
            'archive': (65, 30, '正在压缩备份文件'),
            'validate': (95, 4, '正在检查备份格式与容量'),
        }[phase]
        progress = min(99, base + int(span * min(1, processed / total))) if total else base
        self._set(phase=phase, processed=processed, total=total, progress=progress, message=message)

    def _clear(self):
        if self._temporary:
            self._temporary.cleanup()
        self._temporary = self._path = None

    def start(self):
        if self.busy() or self._downloads:
            raise ValueError('已有备份正在生成或下载，请完成后再试')
        self._clear()
        self._cancel = threading.Event()
        identifier = secrets.token_hex(16)
        self._temporary = tempfile.TemporaryDirectory(prefix='.backup-export-', dir=self.data_dir)
        self._set(id=identifier, status='running', phase='snapshot', message='正在准备备份',
                  progress=0, processed=0, total=0, size=0, file_url='', can_cancel=True)
        self._task = asyncio.create_task(self._run(identifier))
        return self.snapshot()

    async def _run(self, identifier):
        try:
            # RunManager's bounded history is owned by the event loop, while
            # its workers may mutate it between awaits. Snapshot auxiliary
            # state here; all database traversal/ZIP I/O stays in the thread.
            auxiliary = self.auxiliary()
            worker = asyncio.create_task(asyncio.to_thread(self._build, auxiliary))
            try:
                path = await asyncio.shield(worker)
            except asyncio.CancelledError:
                # Cancelling the await cannot stop its thread. Signal and drain
                # it before deleting the private snapshot or closing Store.
                self._cancel.set()
                await drain_task(worker)
                raise
            self._check()
            self._path = path
            self._set(status='completed', phase='ready', progress=100,
                      size=path.stat().st_size, file_url=f'/api/backup/jobs/{identifier}/file',
                      message='备份已完成并通过格式与容量检查，可以下载', can_cancel=False)
        except (Exception, asyncio.CancelledError) as exc:
            cancelled = self._cancel.is_set() or isinstance(exc, asyncio.CancelledError)
            self._set(status='cancelled' if cancelled else 'failed', phase='', can_cancel=False,
                      file_url='', message='备份已取消，原始数据未修改' if cancelled else
                      str(exc) if isinstance(exc, ValueError) else '备份未完成，请检查磁盘空间和运行日志')
            if not cancelled:
                logging.getLogger(__name__).exception('生成备份失败')
            self._clear()

    def _build(self, auxiliary):
        folder = Path(self._temporary.name)
        with self.store._lock:
            estimated = (self.store._conn.execute('PRAGMA page_count').fetchone()[0]
                         * self.store._conn.execute('PRAGMA page_size').fetchone()[0])
        if estimated > MAX_EXPANDED_BYTES:
            raise ValueError('数据库超过 1 GiB 备份上限，请先整理数据或使用宿主机完整备份')
        require_space(folder, estimated * 2 + MAX_JSON_BYTES)
        database = self.store.backup(folder / 'qunyu.db', progress=self._report, cancelled=self._cancel.is_set)
        self._check()
        files = {'qunyu.db': database}
        total = database.stat().st_size
        for name, text in auxiliary.items():
            if name not in ALLOWED or name == 'qunyu.db':
                raise ValueError('备份辅助文件不符合格式')
            raw = text.encode('utf-8') if isinstance(text, str) else text
            if len(raw) > MAX_JSON_BYTES:
                raise ValueError('单个备份配置或历史文件超过 16 MiB 上限')
            total += len(raw)
            if total > MAX_EXPANDED_BYTES:
                raise ValueError('备份解压总量超过 1 GiB 上限')
            path = folder / name
            path.write_bytes(raw)
            files[name] = path
        if total > MAX_EXPANDED_BYTES:
            raise ValueError('备份解压总量超过 1 GiB 上限')
        require_space(folder, min(total, MAX_ARCHIVE_BYTES))
        destination = folder / 'backup.zip'
        processed = 0
        with zipfile.ZipFile(destination, 'w', zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
            for name, path in files.items():
                with path.open('rb') as source, archive.open(name, 'w') as target:
                    while chunk := source.read(CHUNK):
                        self._report('archive', processed, total)
                        target.write(chunk)
                        processed += len(chunk)
                        if archive.fp.tell() > MAX_ARCHIVE_BYTES:
                            raise ValueError('备份压缩包超过 256 MiB 上限，未生成不可恢复的文件')
        self._report('validate', 0, 1)
        validate_archive(destination)
        # Check CRCs using bounded reads; the corresponding restore consumes the
        # same allowed members and capacity contract.
        with zipfile.ZipFile(destination) as archive:
            for name in archive.namelist():
                with archive.open(name) as source:
                    while source.read(CHUNK):
                        self._check()
        self._report('validate', 1, 1)
        return destination

    async def wait(self, identifier):
        if self.snapshot()['id'] != identifier:
            raise KeyError(identifier)
        await asyncio.shield(self._task)
        return self.snapshot()

    def cancel(self, identifier):
        if self.snapshot()['id'] != identifier:
            raise KeyError(identifier)
        if self.busy():
            self._cancel.set()
            self._set(message='正在取消备份，请稍候', can_cancel=False)
        return self.snapshot()

    def download(self, identifier):
        from fastapi.responses import FileResponse
        state = self.snapshot()
        if state['id'] != identifier or state['status'] != 'completed' or self._path is None:
            raise ValueError('备份尚未完成或已失效，请重新生成')
        with self._lock:
            self._downloads += 1
        def finish():
            with self._lock:
                self._downloads = max(0, self._downloads - 1)
        class BackupFileResponse(FileResponse):
            async def __call__(self, scope, receive, send):
                try:
                    await super().__call__(scope, receive, send)
                finally:
                    # Response background callbacks may not run when send()
                    # raises or the client cancels an in-progress download.
                    finish()
        return BackupFileResponse(self._path, media_type='application/zip',
            filename=f'shiori-backup-{datetime.now():%Y%m%d-%H%M%S}.zip')

    async def close(self):
        self._cancel.set()
        if self._task:
            await drain_task(self._task)
        self._clear()
