"""An absent notification icon must never strand a hidden desktop process."""
import threading
import time
from types import SimpleNamespace

from shiori.desktop import DesktopShell
from shiori.system_tray import SystemTray


class Icon:
    def __init__(self, registration_after=1):
        self.title = 'fixture'
        self._visible = False
        self.registered = False
        self.attempts = 0
        self.registration_after = registration_after
        self.stopped = threading.Event()

    @property
    def visible(self):
        return self._visible

    @visible.setter
    def visible(self, value):
        self._visible = value
        if value:
            self.attempts += 1
            self.registered = self.attempts >= self.registration_after
        else:
            self.registered = False

    def run(self, setup):
        setup(self)
        self.stopped.wait(2)
        self.registered = False

    def stop(self):
        self.stopped.set()


def tray(icon, **kwargs):
    return SystemTray(icon, probe=lambda value: value.registered, retry_delay=.001,
                      monitor_interval=kwargs.pop('monitor_interval', .01), **kwargs)


def wait_for(predicate):
    deadline = time.monotonic() + 1
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(.002)
    assert predicate()


def test_python_visible_flag_does_not_confirm_failed_windows_registration():
    icon = Icon(registration_after=100)
    controller = tray(icon)
    try:
        assert controller.start() is False
        assert icon.attempts == 6
        assert not controller.is_available() and icon.stopped.is_set()
    finally:
        controller.stop()


def test_transient_startup_failure_retries_before_allowing_hide():
    icon, states = Icon(registration_after=3), []
    controller = tray(icon, on_visibility=states.append)
    try:
        assert controller.start()
        assert icon.attempts == 3 and controller.is_available()
        assert states == [True]
    finally:
        controller.stop()
    assert states == [True, False]


def test_missing_icon_at_close_quits_instead_of_hiding(tmp_path):
    icon = Icon()
    controller = tray(icon, monitor_interval=60)
    stopped, hidden = [], []
    shell = DesktopShell('http://127.0.0.1:8765/', tmp_path,
                         stop=lambda: stopped.append(True), keep_in_tray=True)
    shell.window = SimpleNamespace(hide=lambda: hidden.append(True))
    shell.tray_available = controller.is_available
    try:
        assert controller.start()
        icon.registered = False  # Explorer lost the icon; pystray.visible stays true.
        assert icon.visible is True
        assert shell._closing() is True
        assert stopped == [True] and not hidden
    finally:
        controller.stop()


def test_monitor_recovers_notification_icon_without_leaving_hidden_orphan():
    icon, states = Icon(), []
    controller = tray(icon, on_visibility=states.append)
    try:
        assert controller.start()
        icon.registered = False
        wait_for(lambda: icon.attempts >= 2 and controller.is_available())
        assert states[:3] == [True, False, True]
    finally:
        controller.stop()


def test_recovery_failure_revokes_hide_and_stops_icon_loop():
    icon, states = Icon(), []
    controller = tray(icon, on_visibility=states.append)
    try:
        assert controller.start()
        icon.registration_after = 100
        icon.registered = False
        wait_for(icon.stopped.is_set)
        assert not controller.is_available() and states[:2] == [True, False]
    finally:
        controller.stop()


def test_icon_loop_failure_is_reported_without_startup_hang():
    class Broken(Icon):
        def run(self, setup):
            raise RuntimeError('fixture initialization failure')
    controller = tray(Broken())
    assert controller.start(timeout=.1) is False
    assert not controller.is_available()
