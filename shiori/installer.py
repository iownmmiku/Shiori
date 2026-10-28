"""Offline, per-user Windows installer. Importing this module never opens a GUI."""
from __future__ import annotations

import argparse
import base64
import ctypes
import hashlib
import json
import os
from pathlib import Path, PurePosixPath, PureWindowsPath
import queue
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import urllib.request
import zipfile

INSTALL_RECORD = '.shiori-install.json'
MAX_FILES = 50000
MAX_UNPACKED = 4 * 1024**3


class InstallError(Exception):
    """An actionable installation error safe to display to the user."""


def default_target() -> Path:
    base = os.environ.get('LOCALAPPDATA')
    if not base:
        base = str(Path.home() / 'AppData' / 'Local') if os.name == 'nt' else str(Path.home() / '.local' / 'share')
    return Path(base) / 'Programs' / 'shiori'


def _linked(path: Path) -> bool:
    from .filesystem import is_link
    return is_link(path)


def validate_target(target: Path | str) -> Path:
    value = str(target).strip()
    windows = PureWindowsPath(value)
    if not value or (windows.drive and (len(windows.parts) == 1 or not windows.is_absolute())):
        raise InstallError('请选择 Shiori 专用子文件夹，不能直接安装到磁盘根目录。')
    raw = Path(target).expanduser().absolute()
    for part in (raw, *raw.parents):
        if _linked(part):
            raise InstallError('安装路径包含链接或目录联接，请选择普通文件夹。')
    path = raw.resolve()
    if len(path.parts) > 1:
        _relative_name('/'.join(path.parts[1:]))
    forbidden = {Path.home().resolve(), Path.home().parent.resolve()}
    for name in ('WINDIR', 'SystemRoot', 'ProgramFiles', 'ProgramFiles(x86)', 'LOCALAPPDATA', 'APPDATA', 'USERPROFILE'):
        if os.environ.get(name):
            forbidden.add(Path(os.environ[name]).resolve())
    for name in ('WINDIR', 'SystemRoot', 'ProgramFiles', 'ProgramFiles(x86)'):
        if os.environ.get(name) and path.is_relative_to(Path(os.environ[name]).resolve()):
            raise InstallError('请选择当前用户的安装目录，不能安装到 Windows 或系统程序目录。')
    if path == Path(path.anchor) or path in forbidden or len(path.parts) < 3:
        raise InstallError('请选择 Shiori 专用子文件夹，不能直接安装到磁盘根目录或系统目录。')
    if path.name.casefold() in {'data', 'windows', 'system32', 'program files', 'program files (x86)'}:
        raise InstallError('请选择 Shiori 专用文件夹，不能使用数据目录或系统目录。')
    if path.exists() and not path.is_dir():
        raise InstallError('安装位置已存在同名文件，请选择其他文件夹。')
    return path


def _relative_name(value: str) -> str:
    if not isinstance(value, str) or not value or '\\' in value or '\x00' in value:
        raise InstallError('安装包包含无效路径。')
    pure = PurePosixPath(value)
    windows = PureWindowsPath(value)
    # The checks also protect Linux-based tests of a Windows installer.
    if pure.is_absolute() or windows.is_absolute() or windows.drive or any(x in {'', '.', '..'} for x in value.split('/')):
        raise InstallError('安装包包含越界路径。')
    for component in pure.parts:
        if ':' in component or component.endswith((' ', '.')) or any(ord(c) < 32 for c in component):
            raise InstallError('安装包包含 Windows 不支持的文件名。')
        if component.split('.')[0].casefold() in {'con', 'prn', 'aux', 'nul', *(f'com{i}' for i in range(1, 10)), *(f'lpt{i}' for i in range(1, 10))}:
            raise InstallError('安装包包含保留文件名。')
    return pure.as_posix()


def _manifest(value: dict) -> dict:
    if not isinstance(value, dict) or value.get('schema') != 1:
        raise InstallError('安装包清单版本不受支持，请重新下载安装包。')
    digest = value.get('sha256', '')
    if not isinstance(digest, str) or len(digest) != 64 or any(c not in '0123456789abcdefABCDEF' for c in digest):
        raise InstallError('安装包校验清单损坏。')
    version = value.get('version')
    if not isinstance(version, str) or not version or len(version) > 80:
        raise InstallError('安装包版本信息损坏。')
    root = _relative_name(value.get('root', 'shiori'))
    executable = _relative_name(value.get('executable', 'shiori.exe'))
    if '/' in root or executable != 'shiori.exe':
        raise InstallError('安装包入口不受支持。')
    return {**value, 'root': root, 'executable': executable, 'sha256': digest.lower()}


def _sha256(path: Path) -> str:
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def _extract(payload: Path, manifest: dict, stage: Path, progress) -> dict[str, str]:
    files: dict[str, str] = {}
    with payload.open('rb') as stream:
        progress(5, '正在校验内置程序包…')
        if hashlib.file_digest(stream, 'sha256').hexdigest() != manifest['sha256']:
            raise InstallError('内置程序包校验失败，请重新下载安装包。')
        stream.seek(0)
        with zipfile.ZipFile(stream) as archive:
            entries = archive.infolist()
            if len(entries) > MAX_FILES or sum(x.file_size for x in entries) > MAX_UNPACKED:
                raise InstallError('安装包体积或文件数量异常。')
            seen: set[str] = set()
            planned = []
            for entry in entries:
                # ZipInfo.filename normalizes backslashes and truncates NULs;
                # validate the original archive spelling before normalization.
                name = _relative_name(entry.orig_filename.rstrip('/'))
                mode = (entry.external_attr >> 16) & 0xffff
                kind = stat.S_IFMT(mode)
                if (kind not in (0, stat.S_IFREG, stat.S_IFDIR) or (kind == stat.S_IFDIR and not entry.is_dir())
                        or entry.external_attr & 0x400):
                    raise InstallError('安装包包含链接或特殊文件。')
                if name == manifest['root'] and entry.is_dir():
                    continue
                prefix = manifest['root'] + '/'
                if not name.startswith(prefix):
                    raise InstallError('安装包目录结构损坏。')
                relative = _relative_name(name[len(prefix):])
                if relative.split('/')[0].casefold() in {'data', INSTALL_RECORD.casefold()}:
                    raise InstallError('安装包不能包含用户数据或安装记录。')
                if relative.casefold() in seen:
                    raise InstallError('安装包包含重复文件路径。')
                seen.add(relative.casefold())
                planned.append((entry, relative))
            file_names = {name.casefold() for entry, name in planned if not entry.is_dir()}
            for _, relative in planned:
                if any(str(parent).casefold() in file_names for parent in PurePosixPath(relative).parents if str(parent) != '.'):
                    raise InstallError('安装包中文件与目录相互冲突。')
            for index, (entry, relative) in enumerate(planned):
                destination = stage / relative
                if entry.is_dir():
                    destination.mkdir(parents=True, exist_ok=True)
                    continue
                destination.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(entry) as source, destination.open('xb') as output:
                    shutil.copyfileobj(source, output)
                files[relative] = _sha256(destination)
                progress(10 + int(45 * (index + 1) / max(len(planned), 1)), '正在准备程序文件…')
    if manifest['executable'] not in files:
        raise InstallError('安装包缺少 shiori.exe。')
    return files


def is_instance_running(target: Path, data_dir=None) -> bool:
    """Read-only check: never terminate, shut down, or authenticate to the app."""
    paths = _installation_data_locations(target)
    if data_dir is not None: paths = list(dict.fromkeys([*paths, Path(data_dir).resolve()]))
    return any(_data_instance_running(data) for data in paths)


def _installation_data_locations(target: Path):
    from .data_paths import default_data_dir
    return list(dict.fromkeys([(target / 'data').resolve(), default_data_dir()]))


def _data_instance_running(data: Path) -> bool:
    if os.name == 'nt':
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.OpenMutexW.restype = ctypes.c_void_p
        kernel.OpenMutexW.argtypes = [ctypes.c_uint32, ctypes.c_bool, ctypes.c_wchar_p]
        kernel.CloseHandle.argtypes = [ctypes.c_void_p]
        name = 'Local\\Qunyu-' + hashlib.sha256(str(data).lower().encode()).hexdigest()[:24]
        handle = kernel.OpenMutexW(0x100000, False, name)
        if handle:
            kernel.CloseHandle(handle)
            return True
        if ctypes.get_last_error() == 5:
            raise InstallError('无法检查运行状态，请退出 Shiori 后重试。')
    try:
        server = json.loads((data / 'server.json').read_text('utf-8'))
        port = server.get('port')
        if type(port) is not int or not 1 <= port <= 65535:
            return False
        token = (data / 'admin.token').read_text('utf-8').strip()
        identity = hashlib.sha256(token.encode()).hexdigest()[:16]
        with urllib.request.urlopen(f'http://127.0.0.1:{port}/healthz', timeout=.7) as result:
            info = json.load(result)
        return info.get('identity') == identity
    except (OSError, ValueError, TypeError, KeyError):
        return False


def _safe_destination(target: Path, relative: str) -> Path:
    destination = target / _relative_name(relative)
    for path in (destination, *destination.parents):
        if path == target.parent:
            break
        if _linked(path):
            raise InstallError(f'目标路径包含链接，请先移走：{relative}')
        if path != destination and path.exists() and not path.is_dir():
            raise InstallError(f'目标文件夹被同名文件占用，请先移走：{relative}')
    return destination


def _acquire_application_guard(target: Path, data_dir=None):
    """Use the launcher mutex to close the check/replace race on Windows."""
    if os.name != 'nt':
        return lambda: None
    guards = []
    try:
        paths = _installation_data_locations(target)
        if data_dir is not None: paths = list(dict.fromkeys([*paths, Path(data_dir).resolve()]))
        for data in paths:
            guards.append(_acquire_data_guard(data))
    except BaseException:
        for release in reversed(guards): release()
        raise
    return lambda: [release() for release in reversed(guards)]


def _acquire_data_guard(data):
    if Path(data).is_dir():
        from .startup import data_guard
        guard=data_guard(data)
        try:guard.__enter__()
        except OSError as exc:raise InstallError(str(exc)) from exc
        return lambda:guard.__exit__(None,None,None)
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.CreateMutexW.restype = ctypes.c_void_p
    kernel.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_bool, ctypes.c_wchar_p]
    kernel.CloseHandle.argtypes = [ctypes.c_void_p]
    name = 'Local\\Qunyu-' + hashlib.sha256(str(data).lower().encode()).hexdigest()[:24]
    handle = kernel.CreateMutexW(None, False, name)
    error = ctypes.get_last_error()
    if not handle:
        raise InstallError('无法锁定安装目录，请退出 Shiori 后重试。')
    if error == 183:
        kernel.CloseHandle(handle)
        raise InstallError('Shiori 已启动。请退出后重新安装。')
    return lambda: kernel.CloseHandle(handle)


def _old_record(target: Path) -> dict:
    path = _safe_destination(target, INSTALL_RECORD)
    if not path.exists():
        path = _safe_destination(target, '.mikubot-install.json')
    if not path.exists():
        return {}
    try:
        if path.stat().st_size > 16 * 1024**2:
            raise ValueError()
        record = json.loads(path.read_text('utf-8'))
        if record.get('schema') != 1 or not isinstance(record.get('files'), dict):
            raise ValueError()
        for name, digest in record['files'].items():
            _relative_name(name)
            if name.split('/')[0].casefold() in {'data', INSTALL_RECORD.casefold()} or not isinstance(digest, str) or len(digest) != 64:
                raise ValueError()
        return record
    except (OSError, ValueError, TypeError, AttributeError) as exc:
        raise InstallError('旧安装记录损坏。请备份整个文件夹并选择新的安装目录。') from exc


def _ensure_parent(destination: Path, target: Path, created: list[Path]):
    missing = []
    parent = destination.parent
    while not parent.exists():
        missing.append(parent)
        if parent == target:
            break
        parent = parent.parent
    for path in reversed(missing):
        path.mkdir()
        created.append(path)


def create_shortcuts(target: Path, desktop: bool = True, start_menu: bool = True, data_dir=None) -> list[str]:
    if os.name != 'nt' or not (desktop or start_menu):
        return []
    # Only base64 data is interpolated; arbitrary quotes, $(), or backticks in a
    # user-chosen path can never become PowerShell code. No token is an argument.
    arguments = subprocess.list2cmdline(['--data-dir', str(Path(data_dir).resolve())]) if data_dir is not None else ''
    data = base64.b64encode(json.dumps({'target': str(target), 'desktop': desktop, 'menu': start_menu, 'arguments': arguments}).encode()).decode()
    script = """$ErrorActionPreference = 'Stop'
$c = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('%s')) | ConvertFrom-Json
$shell = New-Object -ComObject WScript.Shell
$locations = @()
if ($c.desktop) { $locations += [Environment]::GetFolderPath('Desktop') }
if ($c.menu) { $locations += [IO.Path]::Combine([Environment]::GetFolderPath('Programs'), 'Shiori') }
foreach ($folder in $locations) {
    [IO.Directory]::CreateDirectory($folder) | Out-Null
    $link = $shell.CreateShortcut([IO.Path]::Combine($folder, 'shiori.lnk'))
    $link.TargetPath = [IO.Path]::Combine($c.target, 'shiori.exe')
    $link.WorkingDirectory = $c.target
    $link.Arguments = $c.arguments
    $link.Description = 'Shiori · 本地 AI 群友'
    $link.IconLocation = $link.TargetPath + ',0'
    $link.Save()
    if ($c.menu -and $folder -eq [IO.Path]::Combine([Environment]::GetFolderPath('Programs'), 'Shiori')) {
        $stop = $shell.CreateShortcut([IO.Path]::Combine($folder, '退出 shiori.lnk'))
        $stop.TargetPath = $link.TargetPath
        $stop.WorkingDirectory = $c.target
        $stop.Arguments = ('--stop --no-browser ' + $c.arguments).Trim()
        $stop.Description = '安全退出 Shiori，保留配置和聊天数据'
        $stop.IconLocation = $link.IconLocation
        $stop.Save()
    }
}
""" % data
    encoded = base64.b64encode(script.encode('utf-16le')).decode()
    result = subprocess.run(['powershell.exe', '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass', '-EncodedCommand', encoded],
                            capture_output=True, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0), timeout=30)
    if result.returncode:
        return ['程序已安装，但快捷方式创建失败。请直接运行安装目录中的 shiori.exe。']
    return []


def install_payload(payload: Path | str, manifest: dict, target: Path | str, *,
                    progress=None, desktop_shortcut: bool = True, start_menu_shortcut: bool = True, data_dir=None) -> dict:
    """Stage the entire verified payload, then replace only owned program files.

    A legacy portable directory is supported if it contains shiori.exe. Every
    replaced legacy file is retained in a recovery directory because there is
    no ownership manifest to distinguish programs from same-name user files.
    Files absent from the payload and previous manifest are never touched;
    data/ is always excluded. No application process is ever terminated.
    """
    progress = progress or (lambda value, message: None)
    manifest = _manifest(manifest)
    target = validate_target(target)
    payload = Path(payload)
    if is_instance_running(target, data_dir) if data_dir is not None else is_instance_running(target):
        raise InstallError('Shiori 正在运行。请在托盘菜单选择“退出 Shiori”，再点击安装。')
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise InstallError('安装目录不可写，请选择当前用户拥有的文件夹。') from exc
    lock = target.parent / ('.shiori-' + hashlib.sha256(str(target).casefold().encode()).hexdigest()[:16] + '.installing')
    try:
        lock_fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError as exc:
        raise InstallError(f'此目录已有安装任务。请先关闭其他安装窗口；若上次异常退出，请删除此标记后重试：{lock}') from exc
    except OSError as exc:
        raise InstallError('安装目录不可写，请选择当前用户拥有的文件夹。') from exc
    os.close(lock_fd)
    stage = backup = None
    preserve_backup = False
    created: list[Path] = []
    moved: list[str] = []
    installed: list[str] = []
    release_guard = lambda: None
    try:
        stage = Path(tempfile.mkdtemp(prefix='.shiori-stage-', dir=target.parent))
        backup = Path(tempfile.mkdtemp(prefix='.shiori-backup-', dir=target.parent))
        files = _extract(payload, manifest, stage, progress)
        old = _old_record(target)
        legacy = any(_safe_destination(target, name).is_file() for name in ('shiori.exe', 'mikubot.exe'))
        previous = old.get('files', {})
        # Do not erase a user's unrelated files on an initial install. Every
        # replaced legacy file is permanently retained after a successful
        # upgrade; a matching name alone cannot prove program ownership.
        for relative in files:
            destination = _safe_destination(target, relative)
            if destination.exists() and (not destination.is_file() or (not old and not legacy)):
                raise InstallError(f'安装位置已有同名文件或目录，请选择新的专用文件夹：{relative}')
            if old and destination.exists() and relative not in previous:
                raise InstallError(f'新版本文件与用户文件重名，已保留原文件；请先移走：{relative}')
        obsolete = []
        for relative, digest in previous.items():
            destination = _safe_destination(target, relative)
            if relative not in files and destination.is_file() and _sha256(destination) == digest:
                obsolete.append(relative)
        if shutil.disk_usage(target.parent).free < sum((stage / name).stat().st_size for name in files) + 16 * 1024**2:
            raise InstallError('磁盘剩余空间不足，请释放空间或选择其他磁盘。')
        if is_instance_running(target, data_dir) if data_dir is not None else is_instance_running(target):
            raise InstallError('Shiori 正在运行。请退出后重新安装。')
        release_guard = _acquire_application_guard(target, data_dir) if data_dir is not None else _acquire_application_guard(target)
        record = {'schema': 1, 'version': manifest['version'], 'files': files}
        legacy_backup = bool(legacy and not old)
        if legacy_backup:
            record['legacy_backup'] = str(backup)
        (stage / INSTALL_RECORD).write_text(json.dumps(record, ensure_ascii=False, indent=2), 'utf-8')
        operations = [*files, *obsolete, INSTALL_RECORD]
        try:
            for index, relative in enumerate(operations):
                destination = _safe_destination(target, relative)
                _ensure_parent(destination, target, created)
                if destination.exists():
                    saved = backup / relative
                    saved.parent.mkdir(parents=True, exist_ok=True)
                    os.replace(destination, saved)
                    moved.append(relative)
                if relative in files or relative == INSTALL_RECORD:
                    os.replace(stage / relative, destination)
                    installed.append(relative)
                progress(60 + int(35 * (index + 1) / len(operations)), '正在安装程序…')
        except Exception as exc:
            errors = []
            for relative in reversed(installed):
                try:
                    (target / relative).unlink()
                except OSError as rollback_error:
                    errors.append(rollback_error)
            for relative in reversed(moved):
                try:
                    os.replace(backup / relative, target / relative)
                except OSError as rollback_error:
                    errors.append(rollback_error)
            for directory in reversed(created):
                try:
                    directory.rmdir()
                except OSError:
                    pass
            if errors:
                preserve_backup = True
                raise InstallError(f'安装中断且部分文件无法自动恢复。旧程序保存在 {backup}。用户 data 未被修改，请退出程序后恢复或重试。') from exc
            raise InstallError('安装中断，原程序文件已恢复，用户数据未修改。请先退出 Shiori，检查目录权限后重试。') from exc
        if legacy_backup:
            preserve_backup = True
        progress(98, '正在创建快捷方式…')
        try:
            warnings = (create_shortcuts(target, desktop_shortcut, start_menu_shortcut, data_dir)
                        if data_dir is not None else create_shortcuts(target, desktop_shortcut, start_menu_shortcut))
        except (OSError, subprocess.SubprocessError):
            warnings = ['程序已安装，但快捷方式创建失败。请直接运行安装目录中的 shiori.exe。']
        if legacy_backup:
            warnings.append(f'旧便携版没有安装清单。所有被替换文件已保留在 {backup}；如有同名个人文件，请从该目录取回。此备份不会自动删除。')
        progress(100, '安装完成')
        return {'target': str(target), 'version': manifest['version'], 'files': len(files), 'warnings': warnings,
                'legacy_backup': str(backup) if legacy_backup else None}
    except InstallError:
        raise
    except (OSError, ValueError, zipfile.BadZipFile) as exc:
        raise InstallError('安装包无法读取或目标目录不可写。请重新下载安装包，或选择当前用户拥有的目录。') from exc
    finally:
        if stage:
            shutil.rmtree(stage, ignore_errors=True)
        if backup and not preserve_backup:
            shutil.rmtree(backup, ignore_errors=True)
        release_guard()
        lock.unlink(missing_ok=True)


def launch_installed(target: Path | str, data_dir=None):
    target = Path(target)
    arguments = [str(target / 'shiori.exe')]
    if data_dir is not None: arguments += ['--data-dir', str(Path(data_dir).resolve())]
    return subprocess.Popen(arguments, cwd=target,
                            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))


def bundled_payload() -> tuple[Path, dict]:
    base = Path(getattr(sys, '_MEIPASS', Path(__file__).resolve().parent.parent))
    folder = base / 'installer_payload'
    try:
        return folder / 'payload.zip', _manifest(json.loads((folder / 'manifest.json').read_text('utf-8-sig')))
    except (OSError, ValueError) as exc:
        raise InstallError('安装包缺少内置程序，请下载完整的 Shiori 安装程序。') from exc


def gui_install_target(selected: Path | str) -> Path:
    """Selecting a parent creates shiori; existing owned installs stay in place."""
    path = Path(selected).expanduser().absolute()
    if path.name.casefold() in ('shiori', 'mikubot') or any((path / name).is_file() for name in (INSTALL_RECORD, '.mikubot-install.json')):
        return path
    return path / 'shiori'


def run_gui(payload: Path, manifest: dict, initial_target: Path, data_dir=None):
    import tkinter as tk
    from tkinter import filedialog, messagebox, ttk

    window = tk.Tk()
    window.title('Shiori · 一键安装')
    from .brand import asset_path
    try:
        window.iconbitmap(default=str(asset_path('shiori.ico')))
    except tk.TclError:
        pass  # Non-Windows Tk backends do not all support .ico files.
    window.geometry('640x560')
    window.minsize(540, 420)
    window.configure(bg='#f5f8f8')
    # Reserve the action row before packing any scrollable content. Tk uses
    # logical sizing under DPI scaling, so fixed pixel heights are insufficient.
    footer = ttk.Frame(window, padding=(20, 12))
    footer.pack(side='bottom', fill='x')
    body = ttk.Frame(window)
    body.pack(fill='both', expand=True)
    canvas = tk.Canvas(body, highlightthickness=0, bg='#f5f8f8')
    scrollbar = ttk.Scrollbar(body, orient='vertical', command=canvas.yview)
    scrollbar.pack(side='right', fill='y')
    canvas.pack(side='left', fill='both', expand=True)
    canvas.configure(yscrollcommand=scrollbar.set)
    frame = ttk.Frame(canvas, padding=20)
    content = canvas.create_window((0, 0), window=frame, anchor='nw')
    frame.bind('<Configure>', lambda event: canvas.configure(scrollregion=canvas.bbox('all')))
    def resize_content(event):
        canvas.itemconfigure(content, width=event.width)
        for child in frame.winfo_children():
            if isinstance(child, ttk.Label) and child.cget('wraplength'):
                child.configure(wraplength=max(200, event.width - 40))
    canvas.bind('<Configure>', resize_content)
    canvas.bind_all('<MouseWheel>', lambda event: canvas.yview_scroll(-int(event.delta / 120), 'units'))
    heading = ttk.Frame(frame)
    heading.pack(fill='x')
    try:
        window._brand_image = tk.PhotoImage(file=str(asset_path('app-64.png'))).subsample(2, 2)
        ttk.Label(heading, image=window._brand_image).pack(side='left', padx=(0, 12))
    except tk.TclError:
        pass
    ttk.Label(heading, text='安装 Shiori', font=('Microsoft YaHei UI', 22, 'bold')).pack(side='left')
    ttk.Label(frame, text=f"版本 {manifest['version']} · 离线安装 · 无需管理员", font=('Microsoft YaHei UI', 10)).pack(anchor='w', pady=(8, 18))
    ttk.Label(frame, text='安装位置').pack(anchor='w')
    row = ttk.Frame(frame)
    row.pack(fill='x', pady=(6, 12))
    path_var = tk.StringVar(value=str(initial_target))
    path_entry = ttk.Entry(row, textvariable=path_var)
    path_entry.pack(side='left', fill='x', expand=True)
    def browse():
        selected = filedialog.askdirectory(title='选择安装位置（自动创建 Shiori 文件夹）', initialdir=str(Path(path_var.get()).parent))
        if selected:
            path_var.set(str(gui_install_target(selected)))
    browse_button = ttk.Button(row, text='浏览…', command=browse)
    browse_button.pack(side='right', padx=(8, 0))
    desktop_var, menu_var, launch_var = tk.BooleanVar(value=True), tk.BooleanVar(value=True), tk.BooleanVar(value=True)
    checks = []
    for label, value in [('创建桌面快捷方式', desktop_var), ('添加到开始菜单', menu_var), ('安装完成后启动 Shiori', launch_var)]:
        checkbox = ttk.Checkbutton(frame, text=label, variable=value)
        checkbox.pack(anchor='w', pady=2)
        checks.append(checkbox)
    ttk.Label(frame, text='程序与数据分开保存：默认数据位于当前用户的 LocalAppData\\shiori\\data。升级保留配置、聊天与记忆；移除程序时请保留数据目录。安装前请先退出 Shiori。', wraplength=535).pack(anchor='w', pady=(12, 8))
    progress_var = tk.IntVar(value=0)
    ttk.Progressbar(frame, maximum=100, variable=progress_var).pack(fill='x')
    status_var = tk.StringVar(value='准备就绪')
    ttk.Label(frame, textvariable=status_var, wraplength=535).pack(anchor='w', pady=(6, 8))
    events = queue.Queue()
    state = {'busy': False, 'done': False}
    action = ttk.Button(footer, text='一键安装')
    action.pack(anchor='e')

    def start():
        if state['done']:
            window.destroy()
            return
        try:
            selected = validate_target(gui_install_target(path_var.get()))
            path_var.set(str(selected))
        except InstallError as exc:
            messagebox.showerror('无法安装', str(exc), parent=window)
            return
        state['busy'] = True
        options = (desktop_var.get(), menu_var.get(), launch_var.get())
        for widget in (path_entry, browse_button, action, *checks):
            widget.configure(state='disabled')
        def worker():
            try:
                result = install_payload(payload, manifest, selected,
                                         progress=lambda number, text: events.put(('progress', number, text)),
                                         desktop_shortcut=options[0], start_menu_shortcut=options[1], data_dir=data_dir)
                if options[2]:
                    try:
                        launch_installed(selected, data_dir)
                    except OSError:
                        result['warnings'].append('程序已安装，但未能自动启动。请从桌面或安装目录打开 Shiori。')
                events.put(('done', result))
            except Exception as exc:
                events.put(('error', str(exc)))
        threading.Thread(target=worker, daemon=True).start()

    def poll():
        while True:
            try:
                event = events.get_nowait()
            except queue.Empty:
                break
            if event[0] == 'progress':
                progress_var.set(event[1])
                status_var.set(event[2])
            elif event[0] == 'done':
                state.update(busy=False, done=True)
                action.configure(state='normal', text='完成', command=window.destroy)
                status_var.set('安装完成，已保留原有数据。' + ('\n' + '\n'.join(event[1]['warnings']) if event[1]['warnings'] else ''))
                if event[1]['warnings']:
                    messagebox.showwarning('安装完成', '\n'.join(event[1]['warnings']), parent=window)
            else:
                state['busy'] = False
                status_var.set('安装未完成，可调整目录后重试。')
                for widget in (path_entry, browse_button, action, *checks):
                    widget.configure(state='normal')
                messagebox.showerror('安装未完成', event[1], parent=window)
        window.after(100, poll)

    def close():
        if state['busy']:
            messagebox.showinfo('正在安装', '正在写入程序文件，请等待安装结束后关闭。', parent=window)
        else:
            window.destroy()
    action.configure(command=start)
    window.protocol('WM_DELETE_WINDOW', close)
    window.after(100, poll)
    window.mainloop()


def wait_for_parent(pid, timeout_ms=120000):
    """Wait on a process handle; never terminate a process or reuse its PID."""
    if type(pid) is not int or pid <= 0 or pid == os.getpid():
        raise InstallError('等待退出的程序编号无效。')
    if os.name != 'nt': raise InstallError('自动更新安装仅支持 Windows。')
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_bool, ctypes.c_uint32]
    kernel.OpenProcess.restype = ctypes.c_void_p
    kernel.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
    kernel.WaitForSingleObject.restype = ctypes.c_uint32
    kernel.CloseHandle.argtypes = [ctypes.c_void_p]
    handle = kernel.OpenProcess(0x100000, False, pid)
    if not handle:
        if ctypes.get_last_error() == 87: return  # Already exited.
        raise InstallError('无法确认旧程序是否退出，请完全退出 Shiori 后手动安装。')
    try:
        if kernel.WaitForSingleObject(handle, timeout_ms) != 0:
            raise InstallError('旧程序尚未退出，请完成任务并退出 Shiori 后重试；未写入程序文件。')
    finally: kernel.CloseHandle(handle)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description='Shiori 离线一键安装')
    parser.add_argument('--target', type=Path, default=default_target())
    parser.add_argument('--silent', action='store_true', help='无界面安装，供部署与验收使用')
    parser.add_argument('--no-shortcuts', action='store_true')
    parser.add_argument('--no-launch', action='store_true')
    parser.add_argument('--wait-pid', type=int, help='等待指定旧程序退出后再安装，不强制结束进程')
    parser.add_argument('--data-dir', type=Path, help='更新后继续使用这份数据目录')
    args = parser.parse_args(argv)
    try:
        if args.wait_pid is not None: wait_for_parent(args.wait_pid)
        payload, manifest = bundled_payload()
        if args.silent:
            result = install_payload(payload, manifest, args.target,
                                     desktop_shortcut=not args.no_shortcuts, start_menu_shortcut=not args.no_shortcuts, data_dir=args.data_dir)
            if not args.no_launch:
                launch_installed(result['target'], args.data_dir)
            if sys.stdout:
                print(json.dumps(result, ensure_ascii=False))
        else:
            run_gui(payload, manifest, args.target, args.data_dir)
        return 0
    except Exception as exc:
        if not args.silent:
            if os.name == 'nt':
                ctypes.windll.user32.MessageBoxW(None, str(exc), 'Shiori 安装未完成', 0x10)
            elif sys.stderr:
                print(str(exc), file=sys.stderr)
        elif sys.stderr:
            print(str(exc), file=sys.stderr)
        return 1
