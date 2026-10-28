"""Exercise an actual Windows desktop EXE in a caller-selected isolated directory.

No QQ/model configuration is added. Win32 calls address only windows owned by
the process this script starts; it never drives another application.
"""
from __future__ import annotations

import argparse
import ctypes
import json
import os
import subprocess
import sys
import time
import urllib.request
from ctypes import wintypes
from pathlib import Path


def wait_until(predicate, *, timeout=30):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = predicate()
        if result:
            return result
        time.sleep(.15)
    raise AssertionError('等待桌面验收条件超时')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--exe', type=Path, required=True)
    parser.add_argument('--data-dir', type=Path, required=True)
    parser.add_argument('--port', type=int, default=18895)
    parser.add_argument('--tray', action='store_true', help='同时验证关闭隐藏、二次启动唤醒及后台退出')
    parser.add_argument('--screenshot', type=Path, help='只截图本验收进程的主窗口')
    args = parser.parse_args()
    if os.name != 'nt':
        parser.error('原生桌面验收只支持 Windows')
    data = args.data_dir.resolve()
    if data.exists() and next(data.iterdir(), None) is not None:
        parser.error('只能使用不存在或全空的隔离验收目录')
    data.mkdir(parents=True, exist_ok=True)
    user32 = ctypes.WinDLL('user32', use_last_error=True)
    enum_callback = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    user32.EnumWindows.argtypes = [enum_callback, wintypes.LPARAM]
    user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    user32.IsWindowVisible.argtypes = [wintypes.HWND]
    user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
    user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]

    command = [str(args.exe.resolve()), '--data-dir', str(data), '--port', str(args.port)]
    if not args.tray:
        command.append('--no-tray')
    process = subprocess.Popen(command, cwd=args.exe.resolve().parent,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def window(visible=True):
        found = []
        def inspect(hwnd, _):
            pid = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            if pid.value == process.pid and bool(user32.IsWindowVisible(hwnd)) == visible:
                title = ctypes.create_unicode_buffer(512)
                user32.GetWindowTextW(hwnd, title, len(title))
                if title.value == 'shiori':
                    found.append(hwnd)
            return True
        user32.EnumWindows(enum_callback(inspect), 0)
        return found[0] if found else None

    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    result = {'desktop': False, 'second_launch': False, 'close': False, 'tray': args.tray}
    try:
        def record():
            try:
                value = json.loads((data / 'server.json').read_text('utf-8'))
                return value if value.get('mode') == 'desktop' and value.get('pid') == process.pid else None
            except (OSError, ValueError):
                return None
        current = wait_until(record)
        handle = wait_until(window)
        base = f'http://127.0.0.1:{current["port"]}'
        with opener.open(base + '/healthz', timeout=3) as response:
            assert json.loads(response.read(16384)).get('identity')
        # Let WebView2 finish loading bootstrap/setup. Password protection is
        # validated by API tests; this proves the compiled native host exists.
        time.sleep(2)
        result['desktop'] = True
        assert process.poll() is None
        if args.screenshot:
            from PIL import ImageGrab
            # The handle was selected by this process's PID and exact title.
            # A window capture does not read unrelated desktop windows.
            picture = ImageGrab.grab(window=int(handle))
            assert picture.width >= 800 and picture.height >= 600
            args.screenshot.parent.mkdir(parents=True, exist_ok=True)
            picture.convert('RGB').save(args.screenshot, quality=90)
            result['screenshot'] = str(args.screenshot)
        if args.tray:
            user32.PostMessageW(handle, 0x0010, 0, 0)  # WM_CLOSE to our own host.
            wait_until(lambda: not user32.IsWindowVisible(handle), timeout=5)
            assert process.poll() is None
        repeated = subprocess.run(command, cwd=args.exe.resolve().parent, timeout=12,
                                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        assert repeated.returncode == 0 and process.poll() is None
        assert wait_until(window) == handle
        assert record()['pid'] == current['pid']
        result['second_launch'] = True
        if args.tray:
            subprocess.run([str(args.exe.resolve()), '--data-dir', str(data), '--stop', '--no-browser'],
                           cwd=args.exe.resolve().parent, timeout=10, check=True)
        else:
            user32.PostMessageW(handle, 0x0010, 0, 0)
        process.wait(timeout=20)
        assert not (data / 'server.json').exists()
        result['close'] = True
        result['password_file_created'] = (data / 'security' / 'webui-password.json').is_file()
        assert result['password_file_created']
    finally:
        if process.poll() is None:
            try:
                subprocess.run([str(args.exe.resolve()), '--data-dir', str(data), '--stop', '--no-browser'],
                               cwd=args.exe.resolve().parent, timeout=5, check=False)
                process.wait(timeout=10)
            except (subprocess.TimeoutExpired, OSError):
                process.terminate()
                process.wait(timeout=5)
    (data / 'desktop-smoke.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
