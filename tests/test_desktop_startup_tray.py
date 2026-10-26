"""Offline WinForms lifecycle checks; no native window, tray or server is used."""
import sys
import threading
import time
from types import ModuleType, SimpleNamespace

import pytest

from mikubot.desktop import DesktopShell


class Event:
    def __init__(self):
        self.handlers = []

    def __iadd__(self, handler):
        self.handlers.append(handler)
        return self

    def fire(self):
        for handler in self.handlers:
            handler()


def wait_for(predicate):
    deadline = time.monotonic() + 1
    while not predicate() and time.monotonic() < deadline:
        time.sleep(.002)
    assert predicate()


def desktop(monkeypatch, tmp_path, *, phase=None, callback=True):
    shell = DesktopShell('http://127.0.0.1:8765/', tmp_path,
                         stop=lambda: None, keep_in_tray=True, hidden=True)
    registered, native_created = threading.Event(), threading.Event()
    registered.set()
    shell.tray_available = registered.is_set
    monkeypatch.setattr(shell, '_protect_navigation', lambda: None)
    shown, restored = [], []
    native_hidden, finished = threading.Event(), threading.Event()

    def lose_tray():
        registered.clear()
        if callback:
            shell.on_tray_visibility(False)

    def create_window(*args, **kwargs):
        if phase == 'create':
            assert shell.window is None and kwargs['hidden'] is True
            lose_tray()
        window = SimpleNamespace(hidden=kwargs['hidden'],
            events=SimpleNamespace(closing=Event(), before_show=Event(), shown=Event()))

        def show():
            assert native_created.is_set(), 'show must run after native startup Hide'
            assert threading.current_thread() is not threading.main_thread()
            native_hidden.clear()
            shown.append(True)

        window.show = show
        window.restore = lambda: restored.append(True)
        window.destroy = lambda: None
        return window

    def start(**kwargs):
        if phase == 'before_show':
            lose_tray()
        shell.window.events.before_show.fire()
        # Match pywebview 6.2.1 winforms.py: initial Show/Hide runs after
        # before_show and shown. The backend's created event is set afterwards.
        if shell.window.hidden:
            native_hidden.set()
        shell.window.events.shown.fire()
        if phase == 'shown':
            lose_tray()
            # A recovery racing the initial native Hide must still win later.
            native_hidden.set()
        if phase == 'exit':
            shell.show()
            shell.close()
        native_created.set()
        if phase == 'after_ready':
            assert shell.ready.is_set()
            lose_tray()
        if phase in {'create', 'before_show', 'shown', 'after_ready'}:
            wait_for(lambda: bool(restored))
            assert not native_hidden.is_set()
        else:
            assert not finished.wait(.03)
            assert not shown and not restored
        finished.set()

    webview, winforms = ModuleType('webview'), ModuleType('webview.platforms.winforms')
    webview.settings = {}
    webview.create_window, webview.start = create_window, start
    winforms._is_chromium = lambda: True
    winforms._main_window_created = native_created
    monkeypatch.setitem(sys.modules, 'webview', webview)
    monkeypatch.setitem(sys.modules, 'webview.platforms.winforms', winforms)
    return shell, native_hidden, shown


@pytest.mark.parametrize('phase', ['create', 'before_show', 'shown', 'after_ready'])
def test_tray_loss_during_startup_and_ready_reopens_window(monkeypatch, tmp_path, phase):
    shell, _, shown = desktop(monkeypatch, tmp_path, phase=phase)
    shell.run()
    assert shown


def test_native_startup_rechecks_tray_even_without_loss_callback(monkeypatch, tmp_path):
    shell, _, shown = desktop(monkeypatch, tmp_path, phase='before_show', callback=False)
    shell.run()
    assert shown


def test_healthy_minimized_startup_stays_hidden(monkeypatch, tmp_path):
    shell, native_hidden, shown = desktop(monkeypatch, tmp_path)
    shell.run()
    assert native_hidden.is_set() and not shown


def test_exit_does_not_drain_pending_show(monkeypatch, tmp_path):
    shell, _, shown = desktop(monkeypatch, tmp_path, phase='exit')
    shell.run()
    shell.show()
    assert shell.exiting.is_set() and not shown


def test_show_before_slow_startup_keeps_request_without_waiting(tmp_path):
    shell = DesktopShell('http://127.0.0.1:8765/', tmp_path, stop=lambda: None)

    class NotReady:
        def wait(self, *args):
            pytest.fail('show must not wait for GUI readiness or consume a timeout')

    shell.ready = NotReady()
    shell.show()
    assert shell._show_requested.is_set() and shell.window is None
    native_created = threading.Event()
    shown = threading.Event()
    shell.window = SimpleNamespace(show=shown.set, restore=lambda: None)
    worker = threading.Thread(target=shell._show_when_created, args=(native_created,), daemon=True)
    worker.start()
    try:
        assert not shown.wait(.02)
        assert shell._show_requested.is_set()
        native_created.set()
        assert shown.wait(1)
    finally:
        shell.exiting.set()
        worker.join(1)
    assert not worker.is_alive()
