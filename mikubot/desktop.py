"""Windows WebView2 shell; the HTTP app remains usable from other browsers."""
from __future__ import annotations

import logging
import threading
import webbrowser
from pathlib import Path
from urllib.parse import urlsplit

WEBVIEW2_DOWNLOAD = 'https://developer.microsoft.com/microsoft-edge/webview2/#download-section'


def local_origin(url: str) -> str:
    value = urlsplit(url)
    if (value.scheme != 'http' or value.hostname != '127.0.0.1' or value.username
            or value.password or value.port is None):
        raise ValueError('桌面主界面只能连接本机 127.0.0.1 服务')
    return f'http://127.0.0.1:{value.port}'


def navigation_allowed(url: str, origin: str) -> bool:
    try:
        value = urlsplit(url)
        return (not value.username and not value.password and value.scheme == 'http'
                and value.hostname == '127.0.0.1' and f'http://127.0.0.1:{value.port}' == origin)
    except ValueError:
        return False


def external_link(url: str) -> bool:
    try:
        value = urlsplit(url)
        return value.scheme in ('http', 'https') and bool(value.hostname) and not value.username and not value.password
    except ValueError:
        return False


class DesktopUnavailable(RuntimeError):
    pass


class DesktopShell:
    def __init__(self, url: str, data_dir: Path, *, stop, keep_in_tray=False, hidden=False):
        self.url = url
        self.origin = local_origin(url)
        self.data_dir = Path(data_dir)
        self.stop = stop
        self.keep_in_tray = keep_in_tray
        self.tray_available = None
        self.hidden = hidden
        self.window = None
        self.exiting = threading.Event()
        self.ready = threading.Event()
        self._show_requested = threading.Event()
        self._navigation_handlers = []
        self._startup_error = None

    def show(self):
        if self.exiting.is_set():
            return
        self._show_requested.set()
        self.hidden = False
        if self.window:
            self.window.hidden = False

    def on_tray_visibility(self, available):
        self.keep_in_tray = available
        if not available:
            self.show()

    def _can_hide_in_tray(self):
        return self.keep_in_tray and (self.tray_available is None or self.tray_available())

    def _show_when_created(self, native_created):
        # WinForms fires both before_show and shown before its initial Hide().
        # Wait for the end of that sequence off the GUI thread, without dropping
        # requests made during slow WebView startup or before self.window exists.
        while not self.exiting.is_set():
            if native_created.wait(.1):
                break
        if self.exiting.is_set():
            return
        if self.hidden and not self._can_hide_in_tray():
            self.show()
        while not self.exiting.is_set():
            if not self._show_requested.wait(.1):
                continue
            self._show_requested.clear()
            if self.exiting.is_set():
                return
            try:
                self.window.show()
                if not self.exiting.is_set():
                    self.window.restore()
            except Exception:
                logging.exception('桌面窗口显示失败')

    def refresh_auth(self):
        if self.ready.wait(10) and self.window and not self.exiting.is_set():
            self.window.load_url(self.url)
            self.show()

    def close(self):
        self.exiting.set()
        if self.window:
            try:
                self.window.destroy()
            except Exception:
                logging.exception('桌面窗口退出失败')

    def _closing(self):
        if not self.exiting.is_set() and self._can_hide_in_tray():
            self.window.hide()
            return False
        self.exiting.set()
        self.stop()
        return True

    def _protect_navigation(self):
        """Attach before the WinForms loop handles its first network navigation."""
        native = self.window.native
        control = native.webview

        def navigating(_sender, args):
            target = str(args.Uri)
            if not navigation_allowed(target, self.origin):
                args.Cancel = True
                if external_link(target) and getattr(args, 'IsUserInitiated', False):
                    webbrowser.open(target)

        def popup(_sender, args):
            args.Handled = True
            target = str(args.Uri)
            if external_link(target) and getattr(args, 'IsUserInitiated', False):
                webbrowser.open(target)

        def initialized(_sender, args):
            if not args.IsSuccess:
                self._startup_error = RuntimeError(str(getattr(args, 'InitializationException', 'WebView2 初始化失败')))
                self.stop()
                self.close()
                return
            # Replace pywebview's default new-window handler, which also accepts
            # file:// and custom protocols, with HTTP(S)-only external opening.
            control.CoreWebView2.NewWindowRequested -= native.browser.on_new_window_request
            control.CoreWebView2.NewWindowRequested += popup

        control.NavigationStarting += navigating
        control.CoreWebView2InitializationCompleted += initialized
        if control.CoreWebView2 is not None:
            control.CoreWebView2.NewWindowRequested -= native.browser.on_new_window_request
            control.CoreWebView2.NewWindowRequested += popup
        self._navigation_handlers = [navigating, popup, initialized]

    def run(self, *, server_alive=None):
        if threading.current_thread() is not threading.main_thread():
            raise RuntimeError('桌面窗口必须在主线程启动')
        try:
            import webview
            from webview.platforms.winforms import _is_chromium, _main_window_created
            if not _is_chromium():
                raise DesktopUnavailable('未检测到 WebView2 Runtime 或 .NET Framework 4.6.2。')
        except DesktopUnavailable:
            raise
        except Exception as exc:
            raise DesktopUnavailable('无法加载 Windows 桌面组件。') from exc
        webview.settings['ALLOW_FILE_URLS'] = False
        webview.settings['OPEN_EXTERNAL_LINKS_IN_BROWSER'] = False
        webview.settings['ALLOW_DOWNLOADS'] = True
        webview.settings['REMOTE_DEBUGGING_PORT'] = None
        self.window = webview.create_window('mikubot', self.url, width=1240, height=820,
                                            min_size=(900, 620), background_color='#f5faf9',
                                            js_api=None, text_select=True,
                                            hidden=bool(self.hidden and self._can_hide_in_tray()))
        self.window.events.closing += self._closing
        def protect():
            try:
                self._protect_navigation()
            except Exception as exc:
                self._startup_error = exc
                self.stop()
                threading.Thread(target=self.close, daemon=True).start()
        self.window.events.before_show += protect
        self.window.events.before_show += self.ready.set
        threading.Thread(target=self._show_when_created, args=(_main_window_created,),
                         name='mikubot-window-show', daemon=True).start()

        if server_alive:
            def watch_server():
                while not self.exiting.wait(.3):
                    if not server_alive():
                        self.close()
                        return
            threading.Thread(target=watch_server, daemon=True).start()
        try:
            from .brand import asset_path
            webview.start(gui='edgechromium', debug=False, private_mode=False,
                          storage_path=str(self.data_dir / 'desktop-profile'),
                          icon=str(asset_path('mikubot.ico')))
            if self._startup_error:
                raise DesktopUnavailable('桌面导航保护初始化失败。') from self._startup_error
        except Exception as exc:
            raise DesktopUnavailable('WebView2 窗口启动失败。') from exc
        finally:
            self.exiting.set()
            self.stop()


def unavailable_message(exc: Exception, *, data_dir: Path, port: int) -> str:
    return (f'{exc}\n\n桌面版需要 Microsoft Edge WebView2 Runtime（Evergreen）。\n'
            f'官方下载：{WEBVIEW2_DOWNLOAD}\n\n将自动使用系统浏览器继续，安装组件后再打开桌面窗口。\n'
            f'运行日志：{data_dir / "runtime.log"}')
