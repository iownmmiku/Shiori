"""Verified Windows notification-area presence and bounded recovery."""
from __future__ import annotations

import ctypes
import logging
import threading


def icon_registered(icon):
    """Check Explorer's record; pystray.visible alone only reflects Python state."""
    from pystray._util import win32
    hwnd = getattr(icon, '_hwnd', None)
    if not hwnd:
        return False
    data = win32.NOTIFYICONDATAW(cbSize=ctypes.sizeof(win32.NOTIFYICONDATAW),
        hWnd=hwnd, hID=id(icon), uFlags=win32.NIF_TIP, szTip=icon.title)
    return bool(win32.Shell_NotifyIcon(win32.NIM_MODIFY, data))


class SystemTray:
    def __init__(self, icon, *, on_visibility=None, probe=None, retry_delay=.4, monitor_interval=5):
        self.icon = icon
        self.on_visibility = on_visibility or (lambda _available: None)
        self.probe = probe or icon_registered
        self.retry_delay, self.monitor_interval = retry_delay, monitor_interval
        self._ready, self._stopping, self._visible = threading.Event(), threading.Event(), threading.Event()
        self._worker = None

    def _state(self, available):
        previous = self._visible.is_set()
        (self._visible.set if available else self._visible.clear)()
        if previous != available:
            self.on_visibility(available)

    def _registered(self):
        try:
            return bool(self.probe(self.icon))
        except Exception:
            logging.getLogger(__name__).debug('无法核验系统托盘', exc_info=True)
            return False

    def is_available(self):
        return not self._stopping.is_set() and self._visible.is_set() and self._registered()

    def _restore(self):
        for attempt in range(6):
            if self._stopping.is_set():
                return False
            try:
                self.icon.visible = False
                self.icon.visible = True
                if self._registered():
                    self._state(True)
                    return True
            except Exception:
                logging.getLogger(__name__).debug('系统托盘注册重试失败', exc_info=True)
            if attempt < 5 and self._stopping.wait(self.retry_delay):
                return False
        self._state(False)
        return False

    def _setup(self, _icon):
        try:
            if not self._restore():
                logging.getLogger(__name__).warning('系统托盘未能注册；关闭主窗口将完整退出。')
                self.icon.stop()
        finally:
            self._ready.set()

    def _run(self):
        try:
            self.icon.run(setup=self._setup)
        except Exception:
            logging.getLogger(__name__).exception('系统托盘停止工作；关闭主窗口将完整退出。')
        finally:
            self._state(False)
            self._ready.set()

    def _monitor(self):
        while not self._stopping.wait(self.monitor_interval):
            if self._worker is None or not self._worker.is_alive():
                return
            if self._registered():
                continue
            self._state(False)
            if not self._restore():
                logging.getLogger(__name__).warning('系统托盘恢复失败；已恢复主窗口，关闭窗口将完整退出。')
                self.stop()
                return

    def start(self, timeout=6):
        self._worker = threading.Thread(target=self._run, name='mikubot-tray', daemon=True)
        self._worker.start()
        if not self._ready.wait(timeout) or not self.is_available():
            self.stop()
            return False
        threading.Thread(target=self._monitor, name='mikubot-tray-monitor', daemon=True).start()
        return True

    def stop(self):
        self._stopping.set()
        self._state(False)
        try:
            self.icon.stop()
        except Exception:
            logging.getLogger(__name__).debug('系统托盘清理失败', exc_info=True)
