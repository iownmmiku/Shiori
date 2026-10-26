"""Native Windows desktop, optional browser UI, and explicit headless launcher."""
from __future__ import annotations

import argparse
import ctypes
import hashlib
import ipaddress
import json
import logging
from logging.handlers import RotatingFileHandler
import os
import socket
import sys
import threading
import time
import webbrowser
from pathlib import Path

import uvicorn

from .data_paths import application_base, resolve_data_dir
from .settings import Settings
from .notifications import notify_info
from .startup import ActivationServer, data_guard, instance_details, stop_instance, wake_instance


def _local_host(host: str) -> bool:
    if host == 'localhost':
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _bind_listeners(host: str, port_requested: int, *, strict_port: bool = False):
    """A selected LAN interface still gets a separate private desktop listener."""
    address = ipaddress.ip_address(host)
    if address.version == 6 and host != '::':
        raise ValueError('IPv6 监听请使用 --host ::（双栈）；本机桌面通过 127.0.0.1 连接。')
    last_error = None
    for port in range(port_requested, min(port_requested + (1 if strict_port else 30), 65536)):
        listeners = []
        try:
            targets = [(socket.AF_INET6 if address.version == 6 else socket.AF_INET, host)]
            if host not in ('0.0.0.0', '::', '127.0.0.1'):
                targets.append((socket.AF_INET, '127.0.0.1'))
            for family, bind_host in targets:
                listener = socket.socket(family, socket.SOCK_STREAM)
                listeners.append(listener)
                if os.name == 'nt':
                    listener.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
                else:
                    # Reuse a recently closed listener on POSIX; never enable
                    # SO_REUSEPORT or share a live listener with another process.
                    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                if family == socket.AF_INET6:
                    listener.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
                listener.bind((bind_host, port))
                listener.listen(128)
                listener.setblocking(False)
            if port != port_requested:
                logging.warning('请求监听 %s:%s 失败（%s），已改用端口 %s。固定端口的 OneBot 反向 WebSocket、反向代理和容器映射不会自动修改；请释放原端口后重启，或同步修改外部地址。服务器可用 --strict-port 禁止端口顺延。',
                                host, port_requested, last_error, port)
            return listeners, port
        except OSError as exc:
            last_error = exc
            for listener in listeners:
                listener.close()
    if strict_port:
        raise OSError(f'无法监听 {host}:{port_requested}；--strict-port 已禁止端口顺延。请检查监听地址或关闭占用端口的程序后重试。') from last_error
    raise OSError(f'无法监听 {host}:{port_requested} 或其后续端口，请检查监听地址或关闭占用端口的程序后重试。') from last_error


def _desktop_error(exc, data, port):
    from .desktop import unavailable_message
    message = unavailable_message(exc, data_dir=data, port=port)
    logging.error(message)
    if os.name == 'nt':
        notify_info('已切换至默认浏览器模式，详细原因可在运行日志中查看。')
        return True
    else:
        raise RuntimeError(message) from exc


def _show_existing(instance, data: Path, args) -> bool:
    if args.no_browser:
        return True
    url = instance['url'] + '/#bootstrap=' + instance['token']
    if args.browser or os.name != 'nt':
        webbrowser.open(url)
        return True
    if wake_instance(instance):
        return True
    # A manually started headless backend does not own a Windows GUI loop. An
    # attached shell uses that backend and leaves its deliberate lifetime alone.
    from .desktop import DesktopShell, DesktopUnavailable
    shell = DesktopShell(url, data, stop=lambda: None)
    activation = ActivationServer(shell.show)
    record_file = data / 'desktop.json'
    record_file.write_text(json.dumps({'pid': os.getpid(), 'service_pid': instance.get('pid'),
        'service_port': instance['port'], 'activation': activation.record}), encoding='utf-8')
    try:
        shell.run(server_alive=lambda: instance_details(data, timeout=.7) is not None)
    except DesktopUnavailable as exc:
        if _desktop_error(exc, data, instance['port']):
            webbrowser.open(url)
    finally:
        activation.close()
        _remove_record(record_file)
    return True


def _remove_record(path: Path):
    try:
        if json.loads(path.read_text('utf-8')).get('pid') == os.getpid():
            path.unlink()
    except (OSError, ValueError, AttributeError):
        pass


def main():
    base = application_base()
    parser = argparse.ArgumentParser(description='mikubot 本地聊天助手')
    parser.add_argument('--data-dir', help='明确指定数据目录；默认使用系统用户数据目录')
    parser.add_argument('--host', help='监听地址；非本机地址要求已设置密码，且不会自动打开防火墙')
    parser.add_argument('--port', type=int, default=None)
    parser.add_argument('--strict-port', action='store_true', help='端口不可用时直接退出，禁止顺延；推荐固定反向 WS、反向代理和容器部署使用')
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--browser', action='store_true', help='使用系统浏览器，Windows 默认使用独立桌面窗口')
    mode.add_argument('--no-browser', action='store_true', help='仅运行后台服务，不初始化桌面或浏览器')
    parser.add_argument('--minimized', action='store_true', help='启动桌面后隐藏到托盘，适用于开机启动')
    parser.add_argument('--no-tray', action='store_true')
    parser.add_argument('--stop', action='store_true', help='安全退出使用本数据目录的已运行实例')
    parser.add_argument('--reset-password', action='store_true', help='在运行电脑上重置WebUI密码，聊天与记忆保留')
    parser.add_argument('--init-password', action='store_true', help='在终端交互式设置首次密码，用于无头部署；不启动服务')
    parser.add_argument('--allow-http-lan', action='store_true', help='明确允许可信局域网HTTP；密码和聊天传输不加密，不用于公网')
    parser.add_argument('--open-data', action='store_true', help='打开数据文件夹，不启动机器人')
    parser.add_argument('--computer-python', type=Path, help=argparse.SUPPRESS)
    parser.add_argument('--update-worker', type=Path, help=argparse.SUPPRESS)
    parser.add_argument('--computer-output', type=Path, help=argparse.SUPPRESS)
    parser.add_argument('--replay', type=Path, help='在独立临时数据中回放标注案例，不启动 QQ 或管理台')
    parser.add_argument('--replay-output', type=Path, default=base / 'replay-report.json')
    parser.add_argument('--replay-live', action='store_true', help='明确使用现有模型配置，可能产生费用')
    parser.add_argument('--replay-generate', action='store_true', help='同时生成回答；操作工具全部关闭')
    args = parser.parse_args()
    if args.update_worker:
        from .update_runtime import run
        run(args.update_worker)
        return
    # Frozen Python workers must never initialize settings, passwords or a GUI.
    if args.computer_python:
        if not args.computer_output:
            parser.error('电脑 Python 子进程缺少输出位置')
        from .computer_worker import execute
        raise SystemExit(execute(args.computer_python, args.computer_output))
    data = resolve_data_dir(args.data_dir, base=base, migrate=not (args.stop or args.replay))
    if args.init_password:
        import getpass
        from .web_auth import WebAuth
        current=Settings(data)
        password=getpass.getpass('设置WebUI密码（至少8位）：')
        confirm=getpass.getpass('再次输入：')
        if password!=confirm: raise ValueError('两次密码不同，未保存')
        WebAuth(data,current.token).setup(password,is_local=True,bootstrap_token=current.token)
        if args.allow_http_lan: current.update({'webui':{'require_https':False}})
        print('首次密码已设置。可启动无头服务；远程入口默认要求HTTPS。')
        return
    if args.reset_password:
        from .web_auth import WebAuth
        current = Settings(data)
        WebAuth(data, current.token).reset_from_host()
        print('WebUI密码已重置，旧登录已失效。请在本机重新打开软件并设置密码。')
        return
    if args.replay:
        import asyncio
        from .evaluation import replay, load_replay_config, read_initial_usage
        config = load_replay_config(data) if args.replay_live else None
        usage = read_initial_usage(data) if args.replay_live else None
        report = asyncio.run(replay(json.loads(args.replay.read_text('utf-8-sig')), config=config,
            live=args.replay_live, generate=args.replay_generate, initial_usage=usage))
        args.replay_output.write_text(json.dumps(report, ensure_ascii=False, indent=2), 'utf-8')
        return
    if args.stop:
        stopped = stop_instance(data)
        if os.name == 'nt' and getattr(sys, 'frozen', False) and not args.no_browser:
            ctypes.windll.user32.MessageBoxW(None, '已请求退出 mikubot。' if stopped else 'mikubot 当前未运行。', 'mikubot', 0x40)
        return
    data.mkdir(parents=True, exist_ok=True)
    if args.open_data:
        if os.name == 'nt':
            os.startfile(str(data))
        else:
            webbrowser.open(data.as_uri())
        return
    if args.port is not None and not 1 <= args.port <= 65535:
        parser.error('端口须在 1 至 65535 之间')
    logging.basicConfig(handlers=[RotatingFileHandler(data / 'runtime.log',maxBytes=5*1024*1024,backupCount=3,encoding='utf-8')], level=logging.WARNING,
                        format='%(asctime)s %(levelname)s %(message)s')
    while True:
        existing = instance_details(data, timeout=1)
        if existing:
            _show_existing(existing, data, args)
            return
        guard = data_guard(data)
        try:
            guard.__enter__()
        except OSError:
            # A second click may race settings/server.json creation. Never load
            # settings while the owning process is initializing them.
            for _ in range(40):
                existing = instance_details(data, timeout=.3)
                if existing:
                    _show_existing(existing, data, args)
                    return
                time.sleep(.1)
            raise OSError('已有 mikubot 实例正在启动，请稍后重试。') from None
        try:
            use_browser = _run_instance(data, args, parser)
        finally:
            guard.__exit__(None, None, None)
        if not use_browser:
            return
        # The failed desktop service has fully stopped and released its lock.
        # Re-check for another launch before starting one browser backend.
        args.browser = True
        args.minimized = False


def _run_instance(data, args, parser):
    settings = Settings(data)
    if getattr(args,'allow_http_lan',False): settings.update({'webui':{'require_https':False}})
    webui = settings.get().get('webui', {})
    host = args.host or (webui.get('host', '0.0.0.0') if webui.get('remote_enabled') else '127.0.0.1')
    port_requested = args.port if args.port is not None else webui.get('port', 8765)
    if type(port_requested) is not int or not 1 <= port_requested <= 65535:
        parser.error('端口须在 1 至 65535 之间')
    if host == 'localhost':
        host = '127.0.0.1'
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        parser.error('--host 须为本机或监听网卡的 IP 地址')
    allow_remote = not _local_host(host)
    if allow_remote:
        from .web_auth import WebAuth
        if WebAuth(data, settings.token).status()['mode'] != 'password':
            raise ValueError('远程 WebUI 需要密码。请先用本机桌面设置密码，再开启远程访问。')
    listeners, port = _bind_listeners(host, port_requested, strict_port=args.strict_port)
    from .api import create_app
    try:
        app = create_app(data, allow_remote=allow_remote)
    except Exception:
        for listener in listeners:
            listener.close()
        raise
    settings = app.state.settings
    app.state.listen_host = host
    app.state.listen_port = port
    app.state.requested_port = port_requested
    identity = hashlib.sha256(settings.token.encode()).hexdigest()[:16]

    class RedactSecrets(logging.Filter):
        def filter(self, record):
            cfg = settings.get()
            text = record.getMessage()
            for value in (settings.token, cfg['model'].get('api_key'), cfg['onebot'].get('access_token'),
                          cfg['qq_official'].get('app_secret'), *(x.get('api_key') for x in cfg.get('providers', [])),
                          *(x.get('api_key') for x in cfg.get('provider_sources', [])),
                          *(x.get('api_key') for x in cfg.get('search_providers', [])),
                          *(x.get('token') for x in cfg.get('mcp_servers', []))):
                if value:
                    text = text.replace(value, '[已脱敏]')
            record.msg, record.args = text, ()
            return True
    for handler in logging.getLogger().handlers:
        handler.addFilter(RedactSecrets())
    app.state.identity = identity
    server = uvicorn.Server(uvicorn.Config(app, host=host, port=port, access_log=False,
        log_level='warning', log_config=None, proxy_headers=False, ws_max_size=8 * 1024 * 1024))
    app.state.shutdown_requested = False
    def shutdown():
        app.state.shutdown_requested = True
        server.should_exit = True
    app.state.shutdown = shutdown
    url = f'http://127.0.0.1:{port}/#bootstrap={settings.token}'
    desktop = os.name == 'nt' and not args.browser and not args.no_browser
    shell = None
    if desktop:
        from .desktop import DesktopShell
        shell = DesktopShell(url, data, stop=lambda: setattr(server, 'should_exit', True))
    icon = None
    activation = None
    thread = None
    instance_file = data / 'server.json'
    use_browser = False

    def open_ui(*_):
        if shell:
            shell.show()
        else:
            webbrowser.open(url)

    def quit_app(*_):
        app.state.shutdown()
        if shell:
            shell.close()

    def reset_password(*_):
        if os.name == 'nt' and ctypes.windll.user32.MessageBoxW(None,
            '重置WebUI密码并退出所有旧登录？聊天、记忆和模型配置会保留。', 'mikubot · 重置密码', 0x24) != 6:
            return
        from .web_auth import WebAuth
        # The shared auth object revokes its sessions immediately. This action
        # is owned by the host tray, never exposed as a remote reset endpoint.
        app.state.auth_http.auth.reset_from_host()
        if shell: shell.refresh_auth()
        else: webbrowser.open(url)

    if os.name == 'nt' and not args.no_tray:
        try:
            import pystray
            from .brand import tray_image
            image = tray_image()
            def toggle_pause(*_):
                settings.update({'paused': not settings.get()['paused']})
            native_icon = pystray.Icon('mikubot', image, 'mikubot · 本地 AI 群友', menu=pystray.Menu(
                pystray.MenuItem('打开主界面' if shell else '在浏览器打开', open_ui, default=True),
                pystray.MenuItem('在浏览器打开', lambda *_: webbrowser.open(url), visible=bool(shell)),
                pystray.MenuItem('暂停机器人', toggle_pause, checked=lambda _: settings.get()['paused']),
                pystray.MenuItem('打开数据文件夹', lambda *_: os.startfile(str(data))),
                pystray.MenuItem('查看运行日志', lambda *_: os.startfile(str(data / 'runtime.log'))),
                pystray.MenuItem('重置 WebUI 密码', reset_password),
                pystray.MenuItem('退出 mikubot', quit_app)))
            from .system_tray import SystemTray
            icon = SystemTray(native_icon, on_visibility=shell.on_tray_visibility if shell else None)
            if not icon.start():
                raise RuntimeError('系统托盘未能就绪')
            if shell:
                shell.tray_available = icon.is_available
                shell.keep_in_tray = True
                shell.hidden = bool(args.minimized)
        except Exception:
            logging.exception('系统托盘初始化失败；关闭桌面窗口将完整退出。')
            icon = None
    if shell:
        activation = ActivationServer(shell.show)
    instance_file.write_text(json.dumps({'port': port, 'requested_port': port_requested, 'pid': os.getpid(), 'host': host,
        'mode': 'desktop' if shell else ('headless' if args.no_browser else 'browser'),
        'activation': activation.record if activation else None}), encoding='utf-8')
    try:
        if shell:
            # The GUI owns the main thread; all service tasks own this one server.
            thread = threading.Thread(target=lambda: server.run(sockets=listeners))
            thread.start()
            for _ in range(150):
                if server.started or not thread.is_alive():
                    break
                time.sleep(.1)
            if not server.started:
                raise OSError('后台服务未能启动，请查看数据目录中的 runtime.log。')
            from .desktop import DesktopUnavailable
            try:
                shell.run(server_alive=thread.is_alive)
            except DesktopUnavailable as exc:
                server.should_exit = True
                thread.join(timeout=30)
                if not app.state.shutdown_requested:
                    use_browser = _desktop_error(exc, data, port)
        else:
            if not args.no_browser:
                def open_when_ready():
                    for _ in range(150):
                        if server.started:
                            webbrowser.open(url)
                            return
                        if server.should_exit:
                            return
                        time.sleep(.1)
                threading.Thread(target=open_when_ready, daemon=True).start()
            server.run(sockets=listeners)
    finally:
        server.should_exit = True
        if thread:
            thread.join(timeout=30)
            if thread.is_alive():
                logging.error('后台任务仍在结束中，请等待进程退出后再升级。')
                thread.join()
        if activation:
            activation.close()
        if icon:
            icon.stop()
        for listener in listeners:
            listener.close()
        _remove_record(instance_file)
    return use_browser


def run():
    # Help and diagnostics contain Chinese. Redirected Windows runners may
    # otherwise choose cp1252 even though the terminal expects UTF-8.
    for stream in (sys.stdout, sys.stderr):
        if callable(getattr(stream, 'reconfigure', None)):
            try: stream.reconfigure(encoding='utf-8', errors='backslashreplace')
            except (OSError, ValueError): pass
    try:
        main()
    except Exception as exc:
        logging.exception('mikubot 启动失败')
        if os.name == 'nt' and getattr(sys, 'frozen', False):
            if '--no-browser' in sys.argv:
                raise SystemExit(1) from exc
            logfile = next((getattr(x, 'baseFilename', '') for x in logging.getLogger().handlers
                            if getattr(x, 'baseFilename', '')), '')
            detail = f'\n运行日志：{logfile}' if logfile else '\n请检查用户数据目录权限；升级前先退出旧程序。'
            ctypes.windll.user32.MessageBoxW(None, f'mikubot 启动失败：{exc}{detail}', 'mikubot', 0x10)
        else:
            raise


if __name__ == '__main__':
    run()
