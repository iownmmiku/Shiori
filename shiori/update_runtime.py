"""Detached, stdlib-only program replacement. Never runs an installer.

This file can be copied outside the program tree before the owning process
exits. All archive members and writes are bounded; user data is backed up
separately and never included in the program replacement set.
"""
from __future__ import annotations
import argparse
from contextlib import contextmanager
import hashlib
import http.client
import json
import os
from pathlib import Path, PurePosixPath, PureWindowsPath
import shutil
import stat
import subprocess
import sys
import time
import urllib.request
import zipfile

PROTECTED = {'data', 'updates', '.git', '.venv', 'venv', '.venv-releases', '.shiori-runtimes', '.mikubot-runtimes', 'node_modules'}
RECEIPT = '.shiori-update.json'
LEGACY_RECEIPT = '.mikubot-update.json'
INSTALL_MARKERS = ('.mikubot-install.json', '.shiori-install.json')
TRANSIENT = {'server.json', '.instance-owner.json', '.process.lock'}


@contextmanager
def worker_guard(work, *, wait_seconds=0):
    """An OS lease survives stale files, but is released on crash or reboot."""
    path = regular(Path(work) / 'worker.lock')
    with path.open('a+b') as lock:
        if path.stat().st_size == 0:
            lock.write(b'0'); lock.flush()
        lock.seek(0)
        deadline = time.monotonic() + wait_seconds
        while True:
            try:
                if os.name == 'nt':
                    import msvcrt
                    msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise ValueError('更新助手仍在运行，请等待完成') from None
                time.sleep(.05)
        try:
            yield
        finally:
            lock.seek(0)
            if os.name == 'nt':
                msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def worker_running(work):
    try:
        with worker_guard(work):
            return False
    except (OSError, ValueError):
        return True


def read_record(path, maximum=16 * 1024 * 1024):
    path = regular(Path(path))
    if path.stat().st_size > maximum:
        raise ValueError('更新恢复记录过大')
    value = json.loads(path.read_text('utf-8-sig'))
    if not isinstance(value, dict):
        raise ValueError('更新恢复记录无效')
    return value


def recovery_journal(work, target, data):
    """Validate every rollback path before stopping the running application."""
    backup = regular(work / 'program-backup')
    journal = read_record(backup / 'journal.json')
    hashes = journal.get('backup_hashes')
    if journal.get('schema') != 2 or not isinstance(hashes, dict):
        raise ValueError('旧备份缺少完整性清单，请保留备份并手动恢复')
    for key in ('changed', 'created'):
        names = journal.get(key)
        if not isinstance(names, list) or not all(isinstance(name, str) for name in names) or len(names) > 50010 or len(set(names)) != len(names):
            raise ValueError('更新恢复清单无效')
        for name in names:
            destination(target, name, data)
            if key == 'changed':
                saved_file = destination(backup, name)
                if not saved_file.is_file() or digest(saved_file) != hashes.get(name):
                    raise ValueError('旧程序备份不完整，请保留更新目录并手动恢复')
    if set(journal['changed']) & set(journal['created']):
        raise ValueError('更新恢复清单冲突')
    saved = regular(work / 'data-backup')
    if not saved.is_dir():
        raise ValueError('缺少更新前的数据备份，不能自动回滚')
    data_size(saved)  # Also rejects nested links before any write.
    manifest = read_record(work / 'data-backup-manifest.json')
    files = manifest.get('files')
    if manifest.get('schema') != 1 or not isinstance(files, dict) or len(files) > 100000:
        raise ValueError('数据备份缺少完整性清单，不能自动回滚')
    actual = {path.relative_to(saved).as_posix(): path for path in saved.rglob('*') if path.is_file()}
    if set(actual) != set(files) or any(digest(regular(path)) != files[name] for name, path in actual.items()):
        raise ValueError('数据备份校验失败，不能自动回滚')
    return journal


def restore_data(data, saved):
    for directory, dirs, names in os.walk(data, followlinks=False):
        parent = Path(directory)
        dirs[:] = [name for name in dirs if parent != data or name != 'updates']
        for name in dirs:
            regular(parent / name)
        for name in names:
            path = regular(parent / name)
            if parent == data and name in TRANSIENT:
                continue
            if not (saved / path.relative_to(data)).exists():
                path.unlink()
    for path in saved.rglob('*'):
        regular(path)
        if path.is_file():
            target = regular(data / path.relative_to(saved))
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, target)

def digest(path):
    with Path(path).open('rb') as stream: return hashlib.file_digest(stream, 'sha256').hexdigest()

def linked(path):
    return path.is_symlink() or bool(getattr(path.lstat(), 'st_file_attributes', 0) & 0x400) if path.exists() or path.is_symlink() else False

def regular(path):
    for part in (path, *path.parents):
        if linked(part): raise ValueError('更新路径包含链接或目录联接')
    return path

def relative(name):
    if not isinstance(name, str) or not name or '\\' in name or '\x00' in name: raise ValueError('更新文件名无效')
    path, windows = PurePosixPath(name), PureWindowsPath(name)
    if path.is_absolute() or windows.drive or any(p in ('', '.', '..') for p in name.split('/')): raise ValueError('更新文件越界')
    if path.parts[0].casefold() in PROTECTED: raise ValueError('更新文件包含受保护目录')
    if any(':' in p or p.endswith((' ', '.')) or any(ord(c) < 32 for c in p) for p in path.parts): raise ValueError('更新文件名无效')
    if any(p.split('.')[0].casefold() in {'con','prn','aux','nul',*(f'com{i}' for i in range(1,10)),*(f'lpt{i}' for i in range(1,10))} for p in path.parts): raise ValueError('更新文件名无效')
    return path.as_posix()

def destination(root, name, data=None):
    path = regular(root / relative(name))
    if not path.resolve().is_relative_to(root.resolve()): raise ValueError('更新目标越界')
    if data and (path == data or path.is_relative_to(data)): raise ValueError('更新文件与个人数据重叠')
    return path

def atomic_json(path, value):
    regular(path)
    temp = regular(path.with_suffix(path.suffix + '.tmp'))
    with temp.open('w', encoding='utf-8') as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.flush(); os.fsync(stream.fileno())
    os.replace(temp, path)
    sync_directory(path.parent)


def sync_directory(path):
    if os.name != 'nt':
        descriptor = os.open(path, os.O_RDONLY)
        try: os.fsync(descriptor)
        finally: os.close(descriptor)


def durable_copy(source, target):
    checksum = hashlib.sha256()
    with Path(source).open('rb') as incoming, Path(target).open('wb') as output:
        while chunk := incoming.read(1024 * 1024):
            output.write(chunk); checksum.update(chunk)
        output.flush(); os.fsync(output.fileno())
    shutil.copystat(source, target)
    sync_directory(Path(target).parent)
    return checksum.hexdigest()

def stage_archive(archive, stage, kind, version):
    prefixes = tuple(f'{brand}/' if kind == 'portable' else f'{brand}-{version}-source/' for brand in ('shiori', 'mikubot'))
    prefix = None
    files, folded, total = {}, set(), 0
    with zipfile.ZipFile(archive) as source:
        if len(source.infolist()) > 50000: raise ValueError('更新文件数量过多')
        declared=sum(item.file_size for item in source.infolist())
        if declared>4*1024**3 or shutil.disk_usage(stage).free<declared+64*1024**2:raise ValueError('更新展开空间不足')
        for item in source.infolist():
            if prefix is None:
                prefix = next((p for p in prefixes if item.filename.startswith(p)), '')
            if not item.filename.startswith(prefix) or item.is_dir(): raise ValueError('更新包目录结构无效')
            if not prefix: raise ValueError('更新包目录结构无效')
            name = relative(item.filename[len(prefix):])
            mode = item.external_attr >> 16
            if stat.S_ISLNK(mode) or name.casefold() in folded: raise ValueError('更新包包含链接或重名文件')
            folded.add(name.casefold());total += item.file_size
            if total > 4*1024**3 or item.flag_bits & 1: raise ValueError('更新包过大或已加密')
            if name in ('settings.json','admin.token','webui-password.json',RECEIPT,LEGACY_RECEIPT,*INSTALL_MARKERS): raise ValueError('更新包包含个人配置')
            target = destination(stage, name)
            target.parent.mkdir(parents=True, exist_ok=True)
            with source.open(item) as incoming, target.open('xb') as output: shutil.copyfileobj(incoming, output, 1024*1024)
            files[name] = digest(target)
        if kind == 'source':
            manifest = json.loads((stage/'SOURCE_MANIFEST.json').read_text('utf-8'))
            expected = manifest.get('files')
            if manifest.get('version') != version or not isinstance(expected, dict) or set(files) != set(expected)|{'SOURCE_MANIFEST.json'}:
                raise ValueError('源码更新清单不完整')
            if any(files.get(name) != sha for name, sha in expected.items()): raise ValueError('源码更新内容校验失败')
        elif not prefix or prefix[:-1] + '.exe' not in files: raise ValueError('程序更新包缺少启动文件')
    return files

def backup_data(data, backup):
    backup.mkdir(parents=True, exist_ok=False)
    if os.name != 'nt': backup.chmod(0o700)
    files = {}
    for directory, dirs, names in os.walk(data, followlinks=False):
        parent = Path(directory)
        dirs[:] = [name for name in dirs if parent != data or name != 'updates']
        for name in dirs:
            if linked(parent/name): raise ValueError('数据目录包含链接，请先手动备份')
        for name in names:
            path = regular(parent/name)
            if path.parent == data and name in TRANSIENT: continue
            target = backup/path.relative_to(data);target.parent.mkdir(parents=True, exist_ok=True)
            files[path.relative_to(data).as_posix()] = durable_copy(path, target)
    return files

def data_size(data):
    total=0
    for directory,dirs,names in os.walk(data,followlinks=False):
        parent=Path(directory)
        dirs[:]=[name for name in dirs if parent!=data or name!='updates']
        for name in dirs:regular(parent/name)
        for name in names:
            path=regular(parent/name)
            if parent==data and name in TRANSIENT:continue
            total+=path.stat().st_size
    return total

def replace_program(stage, target, files, backup, data):
    backup.mkdir(parents=True, exist_ok=False)
    journal = {'schema':2, 'changed':[], 'created':[], 'files':files, 'backup_hashes':{}}
    # Keep the journal durable before each file mutation, including first use.
    atomic_json(backup/'journal.json', journal)
    previous = {}
    for marker in (LEGACY_RECEIPT, '.mikubot-install.json', RECEIPT, '.shiori-install.json'):
        path = regular(target/marker)
        if path.is_file():
            old = json.loads(path.read_text('utf-8')); previous.update(old.get('files', {}))
    obsolete = [name for name, sha in previous.items() if name not in files and destination(target,name,data).is_file() and digest(target/name)==sha]
    try:
        for name in [*files, *obsolete, RECEIPT, *(name for name in INSTALL_MARKERS if (target/name).exists())]:
            path = destination(target, name, data)
            if path.exists():
                if not path.is_file(): raise ValueError('程序文件与目录冲突')
                saved = backup/name;saved.parent.mkdir(parents=True, exist_ok=True)
                journal['backup_hashes'][name] = durable_copy(path,saved)
                journal['changed'].append(name)
            else: journal['created'].append(name)
            atomic_json(backup/'journal.json', journal)
            if name in files:
                path.parent.mkdir(parents=True, exist_ok=True);os.replace(stage/name,path)
            elif name in obsolete: path.unlink()
        return journal
    except BaseException:
        rollback_program(target, backup, journal, data)
        raise

def rollback_program(target, backup, journal, data):
    for name in reversed(journal['created']): destination(target,name,data).unlink(missing_ok=True)
    for name in reversed(journal['changed']): shutil.copy2(backup/name,destination(target,name,data))

def wait_parent(pid, seconds=120):
    deadline = time.monotonic()+seconds
    if os.name == 'nt':
        import ctypes
        kernel=ctypes.WinDLL('kernel32',use_last_error=True)
        kernel.OpenProcess.restype=ctypes.c_void_p
        kernel.WaitForSingleObject.argtypes=[ctypes.c_void_p,ctypes.c_uint32]
        kernel.CloseHandle.argtypes=[ctypes.c_void_p]
        handle=kernel.OpenProcess(0x100000,False,pid)
        if not handle:
            if ctypes.get_last_error()==87:return
            raise ValueError('无法确认原进程是否退出')
        try:
            if kernel.WaitForSingleObject(handle,int(seconds*1000))!=0:raise ValueError('原程序尚未退出，未替换文件')
        finally:kernel.CloseHandle(handle)
    else:
        while time.monotonic()<deadline:
            try:os.kill(pid,0)
            except ProcessLookupError:return
            time.sleep(.25)
        raise ValueError('原程序尚未退出，未替换文件')

def request_shutdown(data, port):
    token = (data / 'admin.token').read_text().strip()
    request = urllib.request.Request(f'http://127.0.0.1:{port}/api/shutdown', data=b'',
        headers={'Authorization': 'Bearer ' + token})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        try:
            response = opener.open(request, timeout=3)
        except urllib.error.HTTPError as exc:
            response = exc
        with response:
            # Closing a successful response without consuming its body can reset
            # the peer connection and strand Windows Proactor server cleanup.
            raw = response.read(16385)
            status = response.status
    except http.client.HTTPException as exc:
        raise OSError('新版本退出响应不完整，未覆盖运行中的程序') from exc
    try:
        value = json.loads(raw) if len(raw) <= 16384 and status == 200 else None
    except (ValueError, UnicodeError):
        value = None
    if not isinstance(value, dict) or value.get('ok') is not True:
        raise OSError('新版本未确认退出请求，未覆盖运行中的程序')


def health(data, port, version, seconds=60):
    identity=hashlib.sha256((data/'admin.token').read_text().strip().encode()).hexdigest()[:16]
    deadline=time.monotonic()+seconds
    while time.monotonic()<deadline:
        try:
            opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
            with opener.open(f'http://127.0.0.1:{port}/healthz',timeout=2) as response: value=json.load(response)
            if value.get('identity')==identity and value.get('version')==version:return True
        except (OSError,ValueError):pass
        time.sleep(.5)
    return False

def run(config):
    work = regular(Path(config).resolve().parent)
    with worker_guard(work, wait_seconds=3):
        return _run(config)


def _run(config):
    cfg=json.loads(regular(Path(config)).read_text('utf-8'))
    target,data=regular(Path(cfg['target']).resolve()),regular(Path(cfg['data']).resolve())
    work=regular(Path(config).resolve().parent)
    if work != data/'updates'/cfg['download_id'] or target==data or target.is_relative_to(data):raise ValueError('更新工作目录无效')
    result=regular(data/'updates/install-result.json')
    stage,backup=work/'program-stage',work/'program-backup'
    service=cfg.get('service','')
    if service and not __import__('re').fullmatch(r'(?:shiori|mikubot)(?:@[a-zA-Z0-9_-]+)?\.service',service):raise ValueError('服务名称无效')
    journal=None;started=None;data_saved=False;stopped=False;success=False
    launch=list(cfg['launch']);dropin=None;dropin_old=None;dropin_changed=False
    def status(state,message):
        record={'download_id':cfg['download_id'],'status':state,'message':message,'lease_version':1,'worker_pid':os.getpid(),'at':__import__('datetime').datetime.now(__import__('datetime').timezone.utc).isoformat()}
        atomic_json(result,record)
        atomic_json(regular(work/'outcome.json'),record)
    try:
        if cfg.get('recover') is True:
            journal = recovery_journal(work, target, data)
            status('recovering', '正在等待退出，随后恢复更新前的程序和数据')
            if service: subprocess.run(['systemctl', 'stop', service], check=True, timeout=130)
            wait_parent(cfg['parent_pid']); stopped = True
            rollback_program(target, backup, journal, data)
            restore_data(data, work / 'data-backup')
            saved_service = work / 'service-backup.json'
            if service and saved_service.exists():
                record = read_record(saved_service)
                dropin = regular(Path('/etc/systemd/system') / (service + '.d') / '92-shiori-update-runtime.conf')
                if record.get('existed'):
                    dropin.write_text(record['content'], 'utf-8')
                else:
                    dropin.unlink(missing_ok=True)
                subprocess.run(['systemctl', 'daemon-reload'], check=True, timeout=30)
            # The old application must not inherit an unfinished update gate.
            status('failed', '上次更新已中断；已恢复更新前的程序和数据，可以重新尝试更新')
            if service: subprocess.run(['systemctl', 'start', service], check=True, timeout=90)
            else: subprocess.Popen(cfg['launch'], cwd=target, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
            return True
        status('preparing','正在校验更新文件并准备替换')
        archive=regular(Path(cfg['archive']))
        if archive.parent!=work or digest(archive)!=cfg['sha256']:raise ValueError('更新文件校验失败')
        stage.mkdir(exist_ok=False)
        files=stage_archive(archive,stage,cfg['kind'],cfg['version'])
        # Select the entry point from the incoming manifest, not stale files.
        if cfg['kind'] == 'source':
            module = next((brand for brand in ('shiori', 'mikubot') if brand + '/__main__.py' in files), None)
            if module is None: raise ValueError('源码更新包缺少启动模块')
            if '-m' in launch: launch[launch.index('-m') + 1] = module
        else:
            executable = next((brand + '.exe' for brand in ('shiori', 'mikubot') if brand + '.exe' in files), None)
            if executable is None: raise ValueError('程序更新包缺少启动文件')
            launch[0] = str(target/executable)
        total=sum((stage/name).stat().st_size for name in files)
        if shutil.disk_usage(target).free<total*2+64*1024**2:raise ValueError('程序盘空间不足，未替换文件')
        if shutil.disk_usage(data).free<data_size(data)+total+64*1024**2:raise ValueError('数据备份空间不足，未替换文件')
        if service:subprocess.run(['systemctl','stop',service],check=True,timeout=130)
        wait_parent(cfg['parent_pid']);stopped=True
        status('backing_up','正在备份配置、密码、聊天与记忆')
        data_files = backup_data(data,work/'data-backup')
        atomic_json(work/'data-backup-manifest.json', {'schema':1,'files':data_files})
        data_saved=True
        requirements=cfg.get('requirements','requirements-server.txt')
        if requirements not in ('requirements-server.txt','requirements-desktop.txt'):raise ValueError('依赖清单无效')
        dependency_files=['requirements-server.txt']+(['requirements-desktop.txt'] if requirements=='requirements-desktop.txt' else [])
        changed_dependencies=cfg['kind']=='source' and any(not (target/name).is_file() or (stage/name).read_bytes()!=(target/name).read_bytes() for name in dependency_files)
        if changed_dependencies:
            status('preparing','依赖发生变化，正在准备独立运行环境；原环境保留')
            runtime=regular(target/'.shiori-runtimes'/cfg['download_id'])
            runtime.parent.mkdir(exist_ok=True)
            subprocess.run([cfg.get('base_python',sys.executable),'-m','venv',str(runtime)],check=True,timeout=180)
            python=runtime/('Scripts/python.exe' if os.name=='nt' else 'bin/python')
            subprocess.run([str(python),'-m','pip','install','-r',str(stage/requirements)],check=True,timeout=1200)
            subprocess.run([str(python),'-c','import fastapi,httpx,uvicorn'],check=True,timeout=30)
            launch[0]=str(python)
            if service:
                dropin=regular(Path('/etc/systemd/system')/(service+'.d')/'92-shiori-update-runtime.conf')
                dropin_old=dropin.read_bytes() if dropin.exists() else None
                atomic_json(work/'service-backup.json', {'existed':dropin_old is not None,'content':dropin_old.decode('utf-8') if dropin_old is not None else ''})
        status('replacing','正在替换程序文件，个人数据保持原目录')
        journal=replace_program(stage,target,files,backup,data)
        atomic_json(target/RECEIPT,{'schema':1,'version':cfg['version'],'sha256':cfg['sha256'],'files':files,'data_schema':1})
        for marker in INSTALL_MARKERS:
            if (target/marker).exists():atomic_json(target/marker,{'schema':1,'version':cfg['version'],'files':files})
        if dropin is not None:
            def unit_argument(value):
                if any(c in value for c in ('\n','\r','\x00')):raise ValueError('服务启动参数无效')
                return '"'+value.replace('\\','\\\\').replace('"','\\"').replace('%','%%').replace('$','$$')+'"'
            dropin.parent.mkdir(parents=True,exist_ok=True)
            dropin.write_text('[Service]\nExecStart=\nExecStart='+' '.join(unit_argument(v) for v in launch)+'\n','utf-8');dropin_changed=True
            subprocess.run(['systemctl','daemon-reload'],check=True,timeout=30)
        status('restarting','程序文件已替换，正在重启并检查运行状态')
        if service:subprocess.run(['systemctl','start',service],check=True,timeout=90)
        else:started=subprocess.Popen(launch,cwd=target,stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        if not health(data,cfg['port'],cfg['version']):raise ValueError('新版本启动检查未通过')
        success=True;status('completed','版本切换完成，程序已自动重启；数据和旧程序备份已保留')
    except Exception as exc:
        if cfg.get('recover') is True:
            status('recovery_required', '自动恢复未完成，请保留更新目录和备份后检查日志；未继续启动聊天')
            return False
        if not stopped and not service:
            try:wait_parent(cfg['parent_pid'],seconds=10);stopped=True
            except ValueError:pass
        restored=False
        if journal is not None:
            if service:subprocess.run(['systemctl','stop',service],timeout=130,check=True)
            elif started is not None and started.poll() is None:
                try:
                    request_shutdown(data, cfg['port'])
                    started.wait(timeout=30)
                except (OSError,subprocess.TimeoutExpired):
                    status('unknown','新版本未能安全退出，未尝试覆盖运行中的文件；请保留备份并检查日志');return
            rollback_program(target,backup,journal,data)
            if dropin_changed:
                if dropin_old is None:dropin.unlink()
                else:dropin.write_bytes(dropin_old)
                subprocess.run(['systemctl','daemon-reload'],check=True,timeout=30)
            if data_saved:
                # Restore only files captured before startup. No user requests
                # are admitted by the update gate during the health window.
                restore_data(data,work/'data-backup')
            restored=True
        if stopped:
            if service:subprocess.run(['systemctl','start',service],timeout=90,check=True)
            else:subprocess.Popen(cfg['launch'],cwd=target,stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        message=str(exc) if isinstance(exc,ValueError) else '更新发生文件或启动错误'
        status('failed',message+('；旧程序和数据已恢复' if restored else '；当前程序未替换'))
    return success

if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--config',required=True)
    run(parser.parse_args().config)
