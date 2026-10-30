"""Verify the frozen update helper and same-port restart in disposable folders.

The release is staged locally so acceptance never depends on publishing an
untested asset. The real manager prepares its helper/configuration; the frozen
helper verifies, backs up, replaces and starts the real application. A deliberately
wrong target version exercises health-check failure and rollback.
"""
from __future__ import annotations
import argparse
import ctypes
import hashlib
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import time
from unittest.mock import patch
import urllib.request
from uuid import uuid4

from shiori import __version__
from shiori import updates


TERMINAL_STATUSES = {'completed', 'failed', 'unknown', 'recovery_required'}


def bounded_seconds(value):
    seconds = int(value)
    if not 30 <= seconds <= 900:
        raise argparse.ArgumentTypeError('Timeout must be between 30 and 900 seconds')
    return seconds


class WorkerProcess:
    """Keep a handle to this exact helper, avoiding recycled-PID cleanup."""
    def __init__(self, pid):
        from ctypes import wintypes
        self.kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        self.kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        self.kernel.OpenProcess.restype = wintypes.HANDLE
        self.kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        self.kernel.WaitForSingleObject.restype = wintypes.DWORD
        self.kernel.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
        self.kernel.TerminateProcess.restype = wintypes.BOOL
        self.kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        self.kernel.CloseHandle.restype = wintypes.BOOL
        self.handle = self.kernel.OpenProcess(0x100000 | 0x0001, False, pid)
        if not self.handle and ctypes.get_last_error() != 87:
            raise ctypes.WinError(ctypes.get_last_error())

    def wait(self, seconds=0):
        if not self.handle:
            return True
        result = self.kernel.WaitForSingleObject(self.handle, int(seconds * 1000))
        if result == 0xFFFFFFFF:
            raise ctypes.WinError(ctypes.get_last_error())
        return result == 0

    def terminate(self):
        if not self.wait() and not self.kernel.TerminateProcess(self.handle, 1):
            raise ctypes.WinError(ctypes.get_last_error())

    def close(self):
        if self.handle:
            self.kernel.CloseHandle(self.handle)
            self.handle = None


def wait_for_update(result_path, worker, seconds):
    started = time.monotonic()
    deadline = started + seconds
    result, last_status = {}, None
    while time.monotonic() < deadline:
        try:
            result = json.loads(result_path.read_text('utf-8'))
        except (OSError, ValueError):
            pass
        status = result.get('status')
        if status != last_status:
            print(f'update_status={status} elapsed={time.monotonic() - started:.1f}s', flush=True)
            last_status = status
        # A terminal file alone does not exclude a later restart by the helper.
        if status in TERMINAL_STATUSES and worker.wait():
            return result
        time.sleep(.25)
    raise AssertionError(f'Update helper did not finish within {seconds}s: {result}')


def settle_worker(worker, seconds):
    """Do not leave a helper able to relaunch the QA app after test failure."""
    deadline = time.monotonic() + seconds
    while not worker.wait():
        if time.monotonic() >= deadline:
            worker.terminate()
            if not worker.wait(15):
                raise AssertionError('QA update helper could not be stopped')
            return False
        time.sleep(.25)
    return True


def port_open(port):
    with socket.socket() as connection:
        connection.settimeout(.2)
        return connection.connect_ex(('127.0.0.1', port)) == 0


def main():
    smoke_started = time.monotonic()
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--program',type=Path,required=True)
    parser.add_argument('--archive',type=Path,required=True)
    parser.add_argument('--work-dir',type=Path,required=True)
    parser.add_argument('--mode',choices=['installed','portable'],default='installed')
    parser.add_argument('--rollback',action='store_true')
    parser.add_argument('--update-timeout',type=bounded_seconds,default=360,
                        help='Seconds for extraction, replacement, health check and rollback (30-900)')
    parser.add_argument('--cleanup-timeout',type=bounded_seconds,default=180,
                        help='Additional seconds to let an unfinished helper settle before QA cleanup (30-900)')
    args=parser.parse_args()
    if sys.platform!='win32':raise RuntimeError('Frozen acceptance requires Windows')
    work=args.work_dir.resolve()
    if work.exists():raise RuntimeError('Use a fresh acceptance folder')
    work.mkdir(parents=True)
    target,data=work/'program',work/'personal-data'
    shutil.copytree(args.program.resolve(),target)
    baseline={p.relative_to(target).as_posix():hashlib.sha256(p.read_bytes()).hexdigest() for p in target.rglob('*') if p.is_file()}
    if args.mode=='installed':
        (target/'.shiori-install.json').write_text(json.dumps({'schema':1,'version':__version__,'files':baseline}))
    (target/'user-keep.txt').write_text('unknown file must survive','utf-8')
    with socket.socket() as listener:
        listener.bind(('127.0.0.1',0));port=listener.getsockname()[1]
    exe=target/'shiori.exe'
    command=[str(exe),'--no-browser','--data-dir',str(data),'--port',str(port),'--strict-port']
    original=subprocess.Popen(command,cwd=target,stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,
                              creationflags=subprocess.CREATE_NO_WINDOW)
    opener=urllib.request.build_opener(urllib.request.ProxyHandler({}),urllib.request.HTTPCookieProcessor())
    token=''
    worker = None
    output = None
    def request(path,body=None):
        headers={'Authorization':'Bearer '+token}
        if body is not None:headers['Content-Type']='application/json'
        req=urllib.request.Request(f'http://127.0.0.1:{port}'+path,data=None if body is None else json.dumps(body).encode(),headers=headers)
        with opener.open(req,timeout=5) as response:return json.load(response)
    try:
        deadline=time.monotonic()+40
        while time.monotonic()<deadline:
            try:
                token=(data/'admin.token').read_text().strip()
                if request('/healthz').get('version')==__version__:break
            except (OSError,ValueError):pass
            time.sleep(.15)
        else:raise AssertionError('Initial frozen process failed to start')
        request('/api/session',{'token':token})
        request('/api/auth/setup',{'password':'fixture-update-password-v015'})
        saved=request('/api/memories',{'content':'升级前的长期记忆','status':'active'})
        (data/'operator-note.txt').write_text('personal file outside program','utf-8')
        password=data/'security/webui-password.json'
        password_hash=hashlib.sha256(password.read_bytes()).hexdigest()
        # Exercise a failing upgrade, not a historical version that the
        # downgrade guard correctly rejects before the helper can run.
        version=__version__ if not args.rollback else f'{int(__version__.split(".")[0]) + 1}.0.0'
        key=uuid4().hex;folder=data/'updates'/key;folder.mkdir(parents=True)
        archive=folder/f'shiori-{version}-Windows-x64.zip';shutil.copy2(args.archive.resolve(),archive)
        manager=updates.UpdateManager(data,runtime={'mode':args.mode,'switch_supported':True,'update_kind':'portable',
            'service':'','install_supported':args.mode=='installed','default_kind':'portable','current_version':__version__})
        manager.download={'id':key,'tag':'v'+version,'kind':'portable','name':archive.name,'status':'ready',
            'total':archive.stat().st_size,'sha256':hashlib.sha256(archive.read_bytes()).hexdigest()}
        # Only the identity of the host process is supplied by this harness.
        # Helper preparation, filesystem changes and process execution are real.
        with patch.object(updates,'application_base',lambda:target),patch.object(updates.os,'getpid',lambda:original.pid),patch.object(updates.sys,'argv',command):
            receipt=manager.launch_replacement(key)
        assert receipt['ok'] and receipt['worker_pid']
        worker = WorkerProcess(receipt['worker_pid'])
        request('/api/shutdown',{})
        original.wait(timeout=35)
        result_path=data/'updates/install-result.json'
        result=wait_for_update(result_path,worker,args.update_timeout)
        assert result.get('status')==('failed' if args.rollback else 'completed'),result
        deadline=time.monotonic()+30
        while time.monotonic()<deadline:
            try:
                if request('/healthz').get('version')==__version__ and request('/api/memories?limit=50').get('total')==1:break
            except (OSError,ValueError):pass
            time.sleep(.2)
        else:raise AssertionError('Restarted application did not expose the original data')
        assert (target/'user-keep.txt').read_text('utf-8')=='unknown file must survive'
        assert (data/'operator-note.txt').read_text('utf-8')=='personal file outside program'
        assert hashlib.sha256(password.read_bytes()).hexdigest()==password_hash
        assert request('/api/memories?limit=50')['items'][0]['id']==saved['id']
        assert json.loads((data/'server.json').read_text('utf-8'))['port']==port
        if args.rollback:
            for name,sha in baseline.items():assert hashlib.sha256((target/name).read_bytes()).hexdigest()==sha,name
        else:assert (target/'.shiori-update.json').is_file()
        output={'passed':True,'version':__version__,'mode':args.mode,'rollback':args.rollback,'frozen_helper':True,
                'status':result['status'],'same_port':True,'password_preserved':True,'memory_preserved':True,
                'user_files_preserved':True,'data_backup':(folder/'data-backup').is_dir()}
    finally:
        # First release the original process in case handoff itself failed.
        if original.poll() is None:
            try:request('/api/shutdown',{})
            except (OSError,ValueError):pass
            try:original.wait(timeout=10)
            except subprocess.TimeoutExpired:original.terminate();original.wait(timeout=15)
        helper_settled = True
        try:
            if worker is not None:
                helper_settled = settle_worker(worker,args.cleanup_timeout)
        finally:
            if worker is not None:worker.close()
            # The helper can no longer start another instance. Stop only this
            # fresh QA data directory, then verify both discovery and listener.
            stopped=subprocess.run([str(exe),'--data-dir',str(data),'--stop','--no-browser'],cwd=target,
                                   stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,stderr=subprocess.PIPE,
                                   timeout=40,creationflags=subprocess.CREATE_NO_WINDOW)
            deadline=time.monotonic()+30
            while time.monotonic()<deadline:
                if not (data/'server.json').exists() and not port_open(port):break
                time.sleep(.2)
            else:raise AssertionError(f'QA instance did not exit after cleanup (stop exit={stopped.returncode})')
        assert helper_settled,'QA helper exceeded cleanup deadline and required termination; acceptance failed'
        print('cleanup=complete helper_exited=true server_file_absent=true port_closed=true',flush=True)
    # Never leave a passing result for a run whose cleanup did not complete.
    output.update(elapsed_seconds=round(time.monotonic()-smoke_started,2),cleanup_verified=True,
                  helper_exited=True,update_timeout_seconds=args.update_timeout)
    (work/'result.json').write_text(json.dumps(output,indent=2),'utf-8');print(json.dumps(output))


if __name__=='__main__':main()
