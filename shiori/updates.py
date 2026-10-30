"""Explicit GitHub release selection and verified downloads; never self-publish."""
from __future__ import annotations

from contextlib import asynccontextmanager
import asyncio
import copy
from datetime import datetime, timezone
import hashlib
import json
import logging
import os
from pathlib import Path
import platform
import re
import secrets
import shutil
import subprocess
import sys
import time
from urllib.parse import quote, urljoin, urlsplit

import httpx
from fastapi import Request

from . import __version__
from .data_paths import application_base
from .filesystem import is_link

REPOSITORY = 'iownmmiku/Shiori'
RELEASES_URL = f'https://github.com/{REPOSITORY}/releases'
API_URL = f'https://api.github.com/repos/{REPOSITORY}/releases'
MAX_DOWNLOAD = 2 * 1024**3
KINDS = {'installer': 'Windows-Setup.exe', 'portable': 'Windows-x64.zip', 'source': 'source.zip'}
CDN_HOSTS = {'github.com', 'release-assets.githubusercontent.com', 'objects.githubusercontent.com', 'github-releases.githubusercontent.com'}


def version_key(tag):
    if not isinstance(tag, str) or len(tag) > 80:
        raise ValueError('发行版本号无效')
    match = re.fullmatch(r'v?(0|[1-9]\d{0,5})\.(0|[1-9]\d{0,5})\.(0|[1-9]\d{0,5})(?:-([0-9A-Za-z]+(?:[.-][0-9A-Za-z]+)*))?', tag)
    if not match:
        raise ValueError('仅支持带语义版本号的 GitHub Release')
    major, minor, patch, preview = match.groups()
    return (int(major), int(minor), int(patch), int(not preview),
            tuple((0, int(part)) if part.isdigit() else (1, part) for part in (preview or '').split('.') if part))


def _time():
    return datetime.now(timezone.utc).isoformat()


def runtime_info():
    frozen = bool(getattr(sys, 'frozen', False))
    base = application_base()
    windows = sys.platform == 'win32' and platform.machine().lower() in {'amd64', 'x86_64'}
    mode = 'installed' if frozen and windows and any((base / name).is_file() for name in ('.shiori-install.json', '.mikubot-install.json')) else 'portable' if frozen else 'source'
    service = ''
    if sys.platform == 'linux':
        try:
            match = re.search(r'/((?:shiori|mikubot)(?:@[a-zA-Z0-9_-]+)?\.service)(?:/|$)', Path('/proc/self/cgroup').read_text())
            service = match[1] if match else ''
        except OSError: pass
    supported = windows if frozen else sys.platform in ('win32','linux','darwin') and not Path('/.dockerenv').exists()
    return {'mode': mode, 'default_kind': 'installer' if mode == 'installed' else 'portable' if frozen and windows else 'source',
            'switch_supported': supported, 'update_kind': 'portable' if frozen else 'source', 'service':service,
            'install_supported': mode == 'installed' and windows,
            'current_version': __version__, 'repository': REPOSITORY, 'releases_url': RELEASES_URL}


def _asset_url(tag, name):
    return f'{RELEASES_URL}/download/{quote(tag, safe="")}/{quote(name, safe="")}'


def _asset_names(version, kind):
    # Published assets keep their original names until a Shiori release exists.
    return tuple(f'{brand}-{version}-{KINDS[kind]}' for brand in ('shiori', 'mikubot'))


def normalize_release(raw):
    if not isinstance(raw, dict) or raw.get('draft') or type(raw.get('id')) is not int or raw['id'] <= 0:
        return None
    tag = raw.get('tag_name')
    try: key = version_key(tag)
    except ValueError: return None
    version = tag.removeprefix('v')
    assets = {}
    source = raw.get('assets') or []
    if not isinstance(source, list): return None
    for kind, suffix in KINDS.items():
        item = next((item for name in _asset_names(version, kind) for item in source
                     if isinstance(item, dict) and item.get('name') == name), None)
        if not item or item.get('state') != 'uploaded' or type(item.get('id')) is not int:
            continue
        name = item['name']
        if item.get('browser_download_url') != _asset_url(tag, name):
            continue
        size = item.get('size')
        if type(size) is not int or not 0 < size <= MAX_DOWNLOAD:
            continue
        digest = item.get('digest', '')
        digest = digest[7:].lower() if isinstance(digest, str) and re.fullmatch(r'sha256:[a-fA-F0-9]{64}', digest) else ''
        checksum_name = Path(name).with_suffix('.sha256').name
        checksum = next((x for x in source if isinstance(x, dict) and x.get('name') == checksum_name
                         and x.get('state') == 'uploaded' and x.get('browser_download_url') == _asset_url(tag, checksum_name)
                         and type(x.get('size')) is int and 0 < x['size'] <= 32768), None)
        assets[kind] = {'id': item['id'], 'name': name, 'url': item['browser_download_url'], 'size': size,
                        'sha256': digest, 'checksum_url': checksum['browser_download_url'] if checksum else '',
                        'verified_download_available': bool(digest or checksum)}
    current = version_key(__version__)
    return {'id': raw['id'], 'tag': tag, 'version': version, 'name': str(raw.get('name') or tag)[:200],
            'notes': str(raw.get('body') or '此版本没有填写发行说明。')[:20000],
            'published_at': str(raw.get('published_at') or '')[:64],
            'prerelease': bool(raw.get('prerelease') or not key[3]),
            'relation': 'newer' if key > current else 'older' if key < current else 'current',
            'url': f'{RELEASES_URL}/tag/{quote(tag, safe="")}', 'assets': assets}


class UpdateManager:
    def __init__(self, data_dir, *, client_factory=None, runtime=None):
        self.data_dir = Path(data_dir).resolve()
        self.folder = self.data_dir / 'updates'
        self.client_factory = client_factory or (lambda: httpx.AsyncClient(timeout=httpx.Timeout(30, connect=10),
            follow_redirects=False, headers={'User-Agent': f'shiori/{__version__}', 'Accept-Encoding': 'identity'}))
        self.runtime = runtime or runtime_info()
        self.releases, self.checked_at, self.page, self.has_more = [], '', 1, False
        self.include_prerelease = False
        self._cache, self._check_lock, self._task = {}, asyncio.Lock(), None
        self._launching = False
        self._cleaning = False
        self._auto_task = None
        self._recovery = {}
        self._recovery_lock = asyncio.Lock()
        self._recovery_stamp = None
        self.download = {'status': 'idle', 'message': '尚未下载更新'}
        try:
            self._safe_folder()
            record = self.folder / 'state.json'
            if record.exists() and not is_link(record) and record.stat().st_size <= 16384:
                value = json.loads(record.read_text('utf-8'))
                if isinstance(value, dict):
                    self.download = value
                    self._file_path(value)
                    if value.get('status') in {'downloading', 'installing'}:
                        self.download.update(status='interrupted', message='上次更新已中断，请重新下载或核对安装结果；不会自动安装。')
        except (OSError, ValueError, KeyError, TypeError):
            self.download = {'status': 'idle', 'message': '旧下载记录不可用，请重新下载；程序与数据未修改。'}

    def _safe_folder(self):
        for path in (self.folder, self.data_dir):
            if is_link(path): raise ValueError('更新目录包含链接，请使用普通数据目录')
        self.folder.mkdir(exist_ok=True, parents=True)
        return self.folder

    def _file_path(self, record=None):
        item = self.download if record is None else record
        if not re.fullmatch(r'[a-f0-9]{32}', str(item.get('id', ''))):
            raise ValueError('下载记录无效，请重新下载')
        version_key(item['tag'])
        kind = item['kind']
        name = item.get('name')
        if name not in _asset_names(item['tag'].removeprefix('v'), kind): raise ValueError('下载文件名与版本不一致')
        folder = self._safe_folder() / item['id']
        path = folder / name
        if is_link(folder) or is_link(path) or not path.resolve().is_relative_to(self.folder.resolve()):
            raise ValueError('下载路径包含链接或越界')
        return path

    def _save(self):
        folder = self._safe_folder()
        target, temp = folder / 'state.json', folder / 'state.tmp'
        if is_link(target) or is_link(temp): raise ValueError('更新记录包含链接')
        temp.write_text(json.dumps(self.download, ensure_ascii=False), 'utf-8')
        temp.replace(target)
        identifier = self.download.get('id', '')
        if re.fullmatch(r'[a-f0-9]{32}', identifier):
            artifact_folder = folder / identifier
            if is_link(artifact_folder): raise ValueError('更新目录包含链接')
            artifact_folder.mkdir(exist_ok=True)
            artifact = artifact_folder / 'update-artifact.json'
            if is_link(artifact): raise ValueError('更新管理记录包含链接')
            artifact.write_text(json.dumps({key: self.download.get(key) for key in
                ('id', 'tag', 'kind', 'name', 'status', 'started_at')}, ensure_ascii=False), 'utf-8')

    def snapshot(self):
        value = copy.deepcopy(self.download)
        value['file_url'] = f'/api/updates/files/{value["id"]}' if value.get('status') == 'ready' else ''
        try: value['can_install'] = (value.get('status') == 'ready' and value.get('kind') == 'installer'
                    and self.runtime['install_supported'] and version_key(value['tag']) >= version_key(__version__)
                    and version_key(value['tag']) >= version_key('0.1.1'))
        except (ValueError, KeyError): value['can_install'] = False
        value.pop('sha256', None)
        outcome = {}
        result = self.folder / 'install-result.json'
        if result.is_file() and not is_link(result) and result.stat().st_size <= 4096:
            try:
                raw = json.loads(result.read_text('utf-8-sig'))
                if raw.get('status') in ('preparing','backing_up','replacing','restarting','recovering','recovery_required','completed', 'failed', 'unknown') and raw.get('download_id') == self.download.get('id'):
                    outcome = {key: str(raw.get(key, ''))[:500] for key in ('status', 'message', 'at')}
                    if value.get('status') in ('interrupted','installing'): value.update(status=outcome['status'], message=outcome['message'])
            except (ValueError, OSError, AttributeError): pass
        return {**self.runtime, 'recovery': copy.deepcopy(self._recovery), 'automatic_active': bool(self._auto_task and not self._auto_task.done()) or outcome.get('status') in ('preparing','backing_up','replacing','restarting','recovering'), 'install_result': outcome, 'checked_at': self.checked_at, 'releases': copy.deepcopy(self.releases),
                'page': self.page, 'has_more': self.has_more, 'include_prerelease': self.include_prerelease, 'download': value}

    @staticmethod
    def _allowed_url(url, *, api=False):
        value = urlsplit(str(url))
        hosts = {'api.github.com'} if api else CDN_HOSTS
        if value.scheme != 'https' or value.hostname not in hosts or value.username or value.password or value.port not in (None, 443):
            raise ValueError('更新地址不是受支持的 GitHub HTTPS 地址')
        if api and not value.path.startswith(f'/repos/{REPOSITORY}/releases'):
            raise ValueError('更新接口不属于 Shiori 仓库')

    @asynccontextmanager
    async def _stream(self, client, url, *, api=False):
        for _ in range(6):
            self._allowed_url(url, api=api)
            async with client.stream('GET', url, headers={'Accept': 'application/vnd.github+json' if api else 'application/octet-stream',
                                                        'X-GitHub-Api-Version': '2022-11-28'}) as response:
                if response.status_code in {301, 302, 303, 307, 308}:
                    url = urljoin(str(response.url), response.headers.get('location', ''))
                    continue
                if response.status_code in (403, 429):
                    raise ValueError('GitHub 请求受限，请稍后重试；未修改当前程序')
                if response.status_code == 404:
                    raise ValueError('GitHub 发行版或文件不存在，可能尚未发布；请重新检查版本')
                if response.status_code != 200:
                    raise ValueError(f'GitHub 暂时无法提供更新（HTTP {response.status_code}）')
                yield response
                return
        raise ValueError('GitHub 下载重定向过多，请稍后重试')

    async def _bytes(self, client, url, maximum, *, api=False):
        async with self._stream(client, url, api=api) as response:
            output = bytearray()
            async for chunk in response.aiter_bytes():
                output.extend(chunk)
                if len(output) > maximum: raise ValueError('GitHub 响应超过大小限制')
            return bytes(output)

    async def _json(self, client, url):
        try: return json.loads(await self._bytes(client, url, 8 * 1024**2, api=True))
        except (json.JSONDecodeError, UnicodeError): raise ValueError('GitHub 返回了无法识别的发行版信息') from None

    async def check(self, *, include_prerelease=False, page=1):
        if type(include_prerelease) is not bool or type(page) is not int or not 1 <= page <= 100:
            raise ValueError('版本筛选参数无效')
        async with self._check_lock:
            cache_key = (include_prerelease, page)
            cached = self._cache.get(cache_key)
            if cached and time.monotonic() - cached[0] < 60:
                values, has_more, checked = cached[1:]
            else:
                try:
                    async with self.client_factory() as client:
                        raw = await self._json(client, f'{API_URL}?per_page=20&page={page}')
                except httpx.HTTPError:
                    raise ValueError('无法连接 GitHub，请检查网络后重试；当前版本未修改') from None
                if not isinstance(raw, list): raise ValueError('GitHub 发行版列表格式不正确')
                values = [value for item in raw if (value := normalize_release(item)) is not None
                          and (include_prerelease or not value['prerelease'])]
                values.sort(key=lambda value: version_key(value['tag']), reverse=True)
                has_more, checked = len(raw) == 20, _time()
                self._cache[cache_key] = (time.monotonic(), values, has_more, checked)
                if len(self._cache) > 12: self._cache.pop(next(iter(self._cache)))
            self.releases, self.page, self.has_more, self.checked_at = copy.deepcopy(values), page, has_more, checked
            self.include_prerelease = include_prerelease
            return self.snapshot()

    def start_download(self, release_id, kind):
        if self.startup_pending(): raise ValueError('上次更新尚未结束或需要恢复，请先核验更新状态')
        if self._cleaning: raise ValueError('正在清理历史更新，请稍后再试')
        if self._auto_task and not self._auto_task.done(): raise ValueError('一键更新进行中，请先取消或等待完成')
        if self._launching: raise ValueError('正在准备安装，请勿切换下载')
        if type(release_id) is not int or not isinstance(kind, str) or kind not in KINDS: raise ValueError('请选择发行版与文件类型')
        release = next((x for x in self.releases if x['id'] == release_id), None)
        if release is None: raise ValueError('请先检查 GitHub 版本并选择列表中的发行版')
        asset = release['assets'].get(kind)
        if not asset or not asset['verified_download_available']: raise ValueError('此发行文件缺少 SHA-256 校验信息，请在发行页面核对')
        if self._task and not self._task.done():
            if self.download.get('release_id') == release_id and self.download.get('kind') == kind: return self.snapshot()
            raise ValueError('已有下载进行中，请先取消或等待完成')
        if self.download.get('status') == 'installing': raise ValueError('安装器已经启动，请先完成或退出安装流程')
        self.download = {'id': secrets.token_hex(16), 'release_id': release_id, 'tag': release['tag'], 'kind': kind,
                         'name': asset['name'], 'status': 'downloading', 'received': 0, 'total': asset['size'],
                         'message': '正在核对 GitHub 发行文件', 'started_at': _time()}
        self._save()
        self._task = asyncio.create_task(self._download(copy.deepcopy(release), kind))
        return self.snapshot()

    async def _download(self, release, kind):
        part = None
        try:
            path = self._file_path()
            path.parent.mkdir(exist_ok=True)
            part = path.with_suffix(path.suffix + '.part')
            async with self.client_factory() as client:
                # Re-fetch the selected immutable identity, not a moving latest URL.
                fresh = normalize_release(await self._json(client, f'{API_URL}/{release["id"]}'))
                if not fresh or fresh['tag'] != release['tag'] or fresh['id'] != release['id']:
                    raise ValueError('发行版已变更，请重新检查版本')
                asset = fresh['assets'].get(kind)
                if not asset or asset != release['assets'][kind]: raise ValueError('发行文件已变更，请重新检查版本')
                expected = asset['sha256']
                if not expected:
                    checksum = (await self._bytes(client, asset['checksum_url'], 32768)).decode('utf-8-sig')
                    line = next((line for line in checksum.splitlines() if re.fullmatch(r'[a-fA-F0-9]{64}\s+\*?' + re.escape(asset['name']), line)), '')
                    if not line: raise ValueError('发行文件的 SHA-256 清单无效')
                    expected = line[:64].lower()
                if shutil.disk_usage(self.folder).free < asset['size'] + 64 * 1024**2:
                    raise ValueError('数据盘剩余空间不足，请清理空间后重试')
                digest, received, tick = hashlib.sha256(), 0, 0.0
                async with self._stream(client, asset['url']) as response:
                    length = response.headers.get('content-length')
                    if length is not None and length != str(asset['size']): raise ValueError('下载文件大小与发行信息不一致')
                    with part.open('xb') as output:
                        async for chunk in response.aiter_bytes(1024 * 1024):
                            received += len(chunk)
                            if received > asset['size']: raise ValueError('下载文件超过发行信息声明的大小')
                            output.write(chunk); digest.update(chunk)
                            self.download.update(received=received, message='正在下载并计算 SHA-256')
                            if time.monotonic() - tick >= 1:
                                self._save(); tick = time.monotonic()
                    if received != asset['size'] or digest.hexdigest() != expected:
                        raise ValueError('文件大小或 SHA-256 校验失败，未启动安装')
                with part.open('rb') as stream: signature = stream.read(4)
                if kind == 'installer' and not signature.startswith(b'MZ') or kind != 'installer' and signature != b'PK\x03\x04':
                    raise ValueError('发行文件格式不正确，未启动安装')
                part.replace(path)
                self.download.update(status='ready', sha256=expected, received=received, message='下载完成，SHA-256 已校验', completed_at=_time())
                self._save()
        except asyncio.CancelledError:
            self.download.update(status='cancelled', message='下载已取消，当前程序未修改')
            self._save()
        except Exception as exc:
            message = str(exc) if isinstance(exc, ValueError) else '下载未完成，请检查网络、磁盘权限后重试；当前程序未修改'
            logging.getLogger(__name__).warning('GitHub 更新下载失败 (%s)', type(exc).__name__)
            self.download.update(status='failed', message=message[:500])
            try: self._save()
            except (OSError, ValueError): pass
        finally:
            if part is not None and part.exists() and not is_link(part): part.unlink(missing_ok=True)

    async def cancel(self):
        if self._launching: raise ValueError('正在准备安装，请稍后操作')
        if self._auto_task and not self._auto_task.done():
            self._auto_task.cancel()
            try: await self._auto_task
            except asyncio.CancelledError: pass
        if self._task and not self._task.done():
            self._task.cancel()
            try: await self._task
            except asyncio.CancelledError:
                self.download.update(status='cancelled', message='下载已取消，当前程序未修改')
                self._save()
        return self.snapshot()

    def verified_file(self, download_id):
        if self.download.get('id') != download_id or self.download.get('status') != 'ready':
            raise ValueError('下载尚未就绪，请重新下载')
        path = self._file_path()
        try:
            with path.open('rb') as stream: digest = hashlib.file_digest(stream, 'sha256').hexdigest()
            valid = path.stat().st_size == self.download['total'] and digest == self.download.get('sha256')
        except OSError: valid = False
        if not valid: raise ValueError('已下载文件发生变化，请重新下载')
        return path

    def launch_installer(self, download_id, *, automatic=False):
        if self._cleaning: raise ValueError('正在清理历史更新，请稍后再试')
        if not self.runtime['install_supported'] or self.download.get('kind') != 'installer':
            raise ValueError('只有 Windows 安装版支持直接启动更新；请下载对应文件手动部署')
        key = version_key(self.download['tag'])
        if key < version_key(__version__) or key < version_key('0.1.1'):
            raise ValueError('旧版本仅支持下载后手动部署；请先备份数据，避免旧版读取新版数据')
        path = self.verified_file(download_id)
        from .installer import validate_target
        target = validate_target(application_base())
        if self.data_dir.is_relative_to(target) or target.is_relative_to(self.data_dir):
            raise ValueError('数据与程序目录存在包含关系，请先将数据移到独立目录后更新')
        # The downloaded installer runs from the data directory, waits for this
        # exact process to exit, and then uses the existing staged installer.
        command = [str(path), '--target', str(target), '--wait-pid', str(os.getpid()), '--data-dir', str(self.data_dir)]
        if automatic:
            folder = self._safe_folder()
            worker, config = folder / 'update-worker.ps1', folder / 'update-worker.json'
            if any(is_link(value) for value in (worker, config, folder/'install-result.json', folder/'install-result.json.tmp', folder/'install.log')):
                raise ValueError('更新工作文件包含链接，未启动安装')
            worker.write_text((Path(__file__).parent/'resources'/'update-worker.ps1').read_text('utf-8'), 'utf-8-sig')
            config.write_text(json.dumps({'installer': str(path), 'sha256': self.download['sha256'], 'parent_pid': os.getpid(), 'download_id': self.download['id'],
                'arguments': subprocess.list2cmdline(command[1:]+['--silent','--no-launch','--no-shortcuts']),
                'application': str(target/'shiori.exe'), 'launch_arguments': subprocess.list2cmdline(['--data-dir',str(self.data_dir)]),
                'result': str(folder/'install-result.json'), 'log': str(folder/'install.log')}, ensure_ascii=False), 'utf-8')
            # Use the system copy, not a powershell.exe found through PATH.
            powershell = Path(os.environ.get('SystemRoot', r'C:\Windows'))/'System32'/'WindowsPowerShell'/'v1.0'/'powershell.exe'
            command = [str(powershell), '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass', '-File', str(worker), '-ConfigPath', str(config)]
        process = subprocess.Popen(command, cwd=path.parent,
                                   stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                   creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        self.download.update(status='installing', automatic=automatic, message='安装器已启动，Shiori 即将退出；安装后自动重新打开' if automatic else '安装器已启动，Shiori 即将退出；请在安装窗口完成更新')
        try: self._save()
        except (OSError, ValueError): logging.getLogger(__name__).warning('安装器已启动，但更新记录未能写入')
        return {'ok': True, 'installer_pid': process.pid, 'message': self.download['message']}

    def start_automatic(self, release_id, handoff):
        if not self.runtime['install_supported']: raise ValueError('一键更新需要 Windows 安装版；源码与便携部署请手动更新')
        release = next((value for value in self.releases if value['id'] == release_id), None)
        if not release or version_key(release['tag']) < version_key(__version__): raise ValueError('请先检查并选择当前或更新的发行版本')
        self.start_download(release_id, 'installer')
        async def finish():
            try:
                await asyncio.shield(self._task)
                if self.download.get('status') != 'ready': return
                await handoff(self.download['id'], automatic=True)
            except asyncio.CancelledError: raise
            except Exception:
                logging.getLogger(__name__).exception('一键更新交接失败')
                self.download.update(status='failed', message='下载后未能交接安装，请先停止后台任务并检查运行日志；当前程序未替换。')
                self._save()
        self._auto_task = asyncio.create_task(finish())
        return self.snapshot()

    async def close(self):
        if self._launching: return
        await self.cancel()

    def startup_pending(self):
        return self.snapshot().get('install_result',{}).get('status') in ('preparing','backing_up','replacing','restarting','recovering','recovery_required','unknown')

    async def reconcile_startup(self, *, force=False):
        from .update_recovery import inspect_interruption
        async with self._recovery_lock:
            result = self.folder / 'install-result.json'
            try:
                stamp = (result.stat().st_mtime_ns, result.stat().st_size)
            except OSError:
                self._recovery = {}
                return
            # Active leases are rechecked until the worker finishes or exits.
            if not force and self._recovery and self._recovery_stamp == stamp:
                return
            self._recovery = await asyncio.to_thread(inspect_interruption, self)
            try: self._recovery_stamp = (result.stat().st_mtime_ns, result.stat().st_size)
            except OSError: self._recovery_stamp = None

    def launch_recovery(self, download_id):
        from .update_recovery import recovery_config
        from .update_runtime import atomic_json, regular
        work, target, config = recovery_config(self, download_id)
        if self.runtime.get('mode') == 'source':
            worker = regular(work / 'recovery-worker.py')
            worker.write_bytes((Path(__file__).parent / 'update_runtime.py').read_bytes())
            command = [sys.executable, str(worker), '--config', str(work / 'recover.json')]
        else:
            helper = regular(work / ('recovery-helper-' + secrets.token_hex(6)))
            helper.mkdir(exist_ok=False)
            for directory, dirs, names in os.walk(target / '_internal', followlinks=False):
                for name in [*dirs, *names]: regular(Path(directory) / name)
            shutil.copy2(target / 'shiori.exe', helper / 'shiori.exe')
            shutil.copytree(target / '_internal', helper / '_internal', symlinks=False)
            command = [str(helper / 'shiori.exe'), '--update-worker', str(work / 'recover.json')]
        atomic_json(work / 'recover.json', config)
        if os.name != 'nt': (work / 'recover.json').chmod(0o600)
        if config.get('service'):
            subprocess.run(['systemd-run','--unit','shiori-recovery-'+download_id[:12], '--collect','--property=Type=exec', *command],
                           check=True, timeout=15, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        else:
            subprocess.Popen(command, cwd=target, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0), start_new_session=os.name != 'nt')
        self.download.update(status='installing', message='正在恢复更新前的程序和数据，随后自动重启')
        self._save()
        return {'ok': True}

    def launch_replacement(self, download_id):
        if self._cleaning: raise ValueError('正在清理历史更新，请稍后再试')
        if not self.runtime.get('switch_supported'): raise ValueError('当前运行环境不支持程序内切换版本；容器请更换镜像')
        path = self.verified_file(download_id)
        target = application_base().resolve()
        if target == self.data_dir or target.is_relative_to(self.data_dir): raise ValueError('程序与数据目录重叠，无法安全更新')
        from .update_runtime import regular
        regular(target);regular(self.data_dir)
        receipt=next((target/name for name in ('.shiori-update.json', '.mikubot-update.json') if (target/name).is_file()), target/'.shiori-update.json')
        if receipt.is_file():
            from .update_runtime import destination,digest
            previous=json.loads(regular(receipt).read_text('utf-8'))
            for name,sha in previous.get('files',{}).items():
                installed=destination(target,name,self.data_dir)
                if not installed.is_file() or digest(installed)!=sha:raise ValueError('程序文件存在本地修改，请先保存修改，未覆盖代码')
        key = version_key(self.download['tag'])
        if key < version_key(__version__): raise ValueError('此历史版本未声明数据库降级兼容性，未自动覆盖；请从匹配备份恢复')
        if self.runtime['mode']=='source' and (target/'.git').exists() and not receipt.is_file():
            changed = subprocess.check_output(['git','status','--porcelain','--untracked-files=no'],cwd=target,text=True,timeout=10)
            if changed.strip(): raise ValueError('源码目录存在未提交修改，请先保存修改，未覆盖代码')
        folder=path.parent
        if self.runtime.get('update_kind')=='source':
            worker=folder/'update-worker.py'
            worker.write_bytes((Path(__file__).parent/'update_runtime.py').read_bytes())
            command=[sys.executable,str(worker),'--config',str(folder/'switch.json')]
            launch=[sys.executable,'-m','shiori',*sys.argv[1:]]
        else:
            # The helper uses its own EXE and libraries so Windows does not
            # retain locks on the very program files being replaced.
            helper=folder/'helper';helper.mkdir(exist_ok=False)
            for directory,dirs,names in os.walk(target/'_internal',followlinks=False):
                for name in [*dirs,*names]:regular(Path(directory)/name)
            shutil.copy2(target/'shiori.exe',helper/'shiori.exe')
            shutil.copytree(target/'_internal',helper/'_internal',symlinks=False)
            command=[str(helper/'shiori.exe'),'--update-worker',str(folder/'switch.json')]
            launch=[str(target/'shiori.exe'),*sys.argv[1:]]
        record=json.loads((self.data_dir/'server.json').read_text('utf-8'))
        launch += ['--data-dir',str(self.data_dir),'--port',str(record['port']),'--strict-port']
        config={'target':str(target),'data':str(self.data_dir),'archive':str(path),'sha256':self.download['sha256'],
                'parent_pid':os.getpid(),'download_id':download_id,'kind':self.download['kind'],
                'version':self.download['tag'].removeprefix('v'),'port':record['port'],'launch':launch,'service':self.runtime.get('service',''),
                'base_python':getattr(sys,'_base_executable',sys.executable)}
        config['requirements']='requirements-desktop.txt' if sys.platform=='win32' and record.get('mode')=='desktop' else 'requirements-server.txt'
        regular(folder/'switch.json').write_text(json.dumps(config),'utf-8')
        if os.name!='nt':(folder/'switch.json').chmod(0o600)
        if config['service']:
            command=['systemd-run','--unit','shiori-update-'+download_id[:12],'--collect','--property=Type=exec',*command]
            subprocess.run(command,check=True,timeout=15,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
            pid=None
        else:
            process=subprocess.Popen(command,cwd=target,stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,
                creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0),start_new_session=os.name!='nt')
            pid=process.pid
        self.download.update(status='installing',automatic=True,message='更新文件已校验，正在自动备份、替换程序并重启')
        self._save()
        return {'ok':True,'worker_pid':pid,'message':self.download['message']}

    def start_switch(self, release_id, handoff):
        if not self.runtime.get('switch_supported'): raise ValueError('当前环境不支持自动切换，请使用对应部署方式更新')
        release=next((value for value in self.releases if value['id']==release_id),None)
        if not release or version_key(release['tag'])<version_key(__version__): raise ValueError('请选择当前或较新版本；旧版需要匹配的数据备份')
        self.start_download(release_id,self.runtime['update_kind'])
        async def finish():
            try:
                await asyncio.shield(self._task)
                if self.download.get('status')=='ready': await handoff(self.download['id'],replacement=True)
            except asyncio.CancelledError: raise
            except Exception as exc:
                self.download.update(status='failed',message=str(exc)[:500] if isinstance(exc,ValueError) else '未能交接自动更新，当前程序未替换；请查看运行日志')
                self._save();logging.getLogger(__name__).exception('程序切换交接失败')
        self._auto_task=asyncio.create_task(finish())
        return self.snapshot()


def install_updates(app, data_dir):
    from fastapi import Body, HTTPException, Request
    from fastapi.responses import FileResponse
    from .auth_api import local_peer
    from .runs import TERMINAL
    manager = UpdateManager(data_dir)
    app.state.updates = manager

    def snapshot(request):
        return {**manager.snapshot(), 'local_install_allowed': local_peer(request) and callable(app.state.shutdown),
                'local_policy_allowed':local_peer(request), 'remote_switch_enabled':app.state.settings.get().get('updates',{}).get('allow_remote',False),
                'switch_allowed': callable(app.state.shutdown) and (local_peer(request) or app.state.settings.get().get('updates',{}).get('allow_remote',False)),
                'storage_cleanup_allowed': local_peer(request) or app.state.settings.get().get('updates',{}).get('allow_remote',False)}

    @app.get('/api/updates/storage')
    async def update_storage(request: Request):
        from .update_storage import inventory
        result = await asyncio.to_thread(inventory, manager)
        return {**result, 'cleanup_allowed': snapshot(request)['storage_cleanup_allowed']}

    @app.post('/api/updates/storage/cleanup')
    async def clean_update_storage(request: Request, payload: dict = Body(...)):
        from .update_storage import cleanup
        if not snapshot(request)['storage_cleanup_allowed']:
            raise HTTPException(403, '请在运行电脑上清理，或先由本机授权远程更新管理')
        if manager._cleaning or manager._launching or manager.snapshot()['automatic_active'] or (manager._task and not manager._task.done()):
            raise ValueError('更新或清理仍在进行，暂时不能清理历史文件')
        manager._cleaning = True
        worker = asyncio.create_task(asyncio.to_thread(cleanup, manager, payload.get('items')))
        try:
            return await asyncio.shield(worker)
        except asyncio.CancelledError:
            # Do not let a disconnected UI start a new update while deletion is
            # still running in the worker thread.
            from .backup_archives import drain_task
            await drain_task(worker)
            raise
        finally:
            manager._cleaning = False

    def check_idle():
        backups = getattr(app.state, 'backup_exports', None)
        if backups is not None and backups.busy():
            raise ValueError('仍在生成备份，请等待完成或取消后更新')
        if any(job['status'] not in TERMINAL for job in app.state.runs.list()):
            raise ValueError('仍有后台任务进行中，请先完成或停止任务再安装')
        if app.state.provider._reservations:
            raise ValueError('仍有模型请求进行中，请等待回复完成后更新')

    async def handoff(download_id, *, automatic=False, replacement=False):
        check_idle()
        if manager._launching: raise ValueError('已在准备安装，请勿重复点击')
        manager._launching = app.state.provider._updating = True
        try:
            result = (await asyncio.to_thread(manager.launch_replacement, download_id) if replacement
                      else await asyncio.to_thread(manager.launch_installer, download_id, automatic=automatic))
        except BaseException:
            manager._launching = app.state.provider._updating = False
            raise
        asyncio.get_running_loop().call_later(.5, app.state.shutdown)
        return result

    @app.post('/api/updates/recover')
    async def recover(request: Request, payload: dict = Body(...)):
        if not snapshot(request)['switch_allowed']:
            raise HTTPException(403, '请在运行电脑上恢复更新，或先由本机授权远程更新管理')
        if payload.get('download_id') != manager.download.get('id'):
            raise ValueError('更新记录已经改变，请重新核验')
        await manager.reconcile_startup(force=True)
        if not manager._recovery.get('can_rollback'):
            raise ValueError('当前备份未通过恢复校验，不能自动回滚')
        if manager._launching:
            raise ValueError('更新恢复已经开始，请等待重启')
        manager._launching = app.state.provider._updating = True
        try:
            result = await asyncio.to_thread(manager.launch_recovery, payload['download_id'])
        except BaseException:
            manager._launching = False
            raise
        asyncio.get_running_loop().call_later(.5, app.state.shutdown)
        return result

    @app.post('/api/updates/recheck')
    async def recheck(request: Request):
        await manager.reconcile_startup(force=True)
        return snapshot(request)

    @app.post('/api/updates/switch')
    async def switch(request: Request, payload: dict = Body(...)):
        if not snapshot(request)['switch_allowed']: raise HTTPException(403,'服务器自动更新尚未由运行电脑授权开启')
        check_idle()
        manager.start_switch(payload.get('release_id'),handoff)
        return snapshot(request)

    @app.post('/api/updates/remote-policy')
    async def remote_policy(request: Request,payload: dict=Body(...)):
        if not local_peer(request):raise HTTPException(403,'请在运行电脑上授权远程切换版本')
        if type(payload.get('enabled')) is not bool:raise ValueError('远程更新开关须为布尔值')
        app.state.settings.update({'updates':{'allow_remote':payload['enabled']}})
        return snapshot(request)

    @app.post('/api/updates/apply')
    async def apply(request: Request, payload: dict = Body(...)):
        if not local_peer(request): raise HTTPException(403, '请在运行 Shiori 的电脑上发起一键更新')
        if not callable(app.state.shutdown): raise ValueError('当前启动方式不能安全退出，请手动部署')
        check_idle()
        manager.start_automatic(payload.get('release_id'), handoff)
        return snapshot(request)

    @app.get('/api/updates')
    async def status(request: Request):
        await manager.reconcile_startup()
        return snapshot(request)

    @app.post('/api/updates/check')
    async def check(request: Request, payload: dict = Body(default={})):
        await manager.check(include_prerelease=payload.get('include_prerelease', False), page=payload.get('page', 1))
        return snapshot(request)

    @app.post('/api/updates/download')
    async def download(request: Request, payload: dict = Body(...)):
        manager.start_download(payload.get('release_id'), payload.get('kind'))
        return snapshot(request)

    @app.post('/api/updates/cancel')
    async def cancel(request: Request):
        await manager.cancel()
        return snapshot(request)

    @app.get('/api/updates/files/{download_id}')
    async def file(download_id: str):
        path = await asyncio.to_thread(manager.verified_file, download_id)
        return FileResponse(path, filename=path.name, media_type='application/octet-stream')

    @app.post('/api/updates/install')
    async def install(request: Request, payload: dict = Body(...)):
        if not local_peer(request): raise HTTPException(403, '请在运行 Shiori 的电脑上启动安装，远程页面只能下载')
        if not callable(app.state.shutdown): raise ValueError('当前启动方式不能安全退出，请下载后手动安装')
        if manager._auto_task and not manager._auto_task.done(): raise ValueError('一键更新进行中，请勿重复启动安装')
        return await handoff(payload.get('download_id'), automatic=payload.get('automatic') is True)
