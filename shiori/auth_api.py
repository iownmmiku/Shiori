"""Password WebUI authentication and explicitly local launcher credentials."""
from __future__ import annotations

import asyncio
import hmac
import ipaddress
import socket
from urllib.parse import urlsplit

from fastapi import Body, Request
from fastapi.responses import JSONResponse

from .web_auth import WebAuth, AuthError, COOKIE_NAME, SESSION_SECONDS

BOOTSTRAP_COOKIE = 'shiori_bootstrap'


def token_matches(value, expected):
    try:
        return isinstance(value, str) and len(value) <= 512 and hmac.compare_digest(value.encode('utf-8'), expected.encode('utf-8'))
    except UnicodeError:
        return False


def local_peer(request):
    host = request.client.host if request.client else ''
    try:
        if host == 'testclient':  # Starlette's in-process test transport.
            peer_is_local = True
        else:
            address = ipaddress.ip_address(host)
            peer_is_local = (address.ipv4_mapped or address).is_loopback if address.version == 6 else address.is_loopback
        # A local reverse proxy is not a local desktop. Preserve Host at proxies.
        requested = urlsplit('http://' + request.headers.get('host', '')).hostname
        if requested in ('localhost','testserver'):
            return peer_is_local
        authority = ipaddress.ip_address(requested or '')
        return peer_is_local and authority.is_loopback
    except ValueError:
        return False


class AuthHTTP:
    def __init__(self, app, settings, data_dir, allow_remote):
        self.app, self.settings = app, settings
        self.auth = WebAuth(data_dir, lambda: settings.token)
        self.data_dir = data_dir
        self.allow_remote = allow_remote
        self.bound_remote = allow_remote
        self.initial_access = dict(settings.get()['webui'])
        self.access_saved = False

    def internal(self, request):
        if not local_peer(request):
            return False
        authorization = request.headers.get('authorization', '')
        value = authorization[7:] if authorization.startswith('Bearer ') else ''
        return bool(value) and token_matches(value, self.settings.token)

    def proof(self, request):
        return local_peer(request) and bool(request.cookies.get(BOOTSTRAP_COOKIE)) and token_matches(
            request.cookies[BOOTSTRAP_COOKIE], self.settings.token)

    def authenticated(self, request):
        return self.auth.authenticate(request.cookies.get(COOKIE_NAME, ''), is_local=local_peer(request)) or self.internal(request)

    def allowed_host(self, request):
        authority = request.headers.get('host', '')
        if any(ch.isspace() for ch in authority) or '@' in authority:
            return False
        try:
            parsed = urlsplit('http://' + authority)
            host = (parsed.hostname or '').lower()
            parsed.port
        except ValueError:
            return False
        if host in ('localhost', 'testserver'):
            return True
        try:
            address = ipaddress.ip_address(host)
            return address.is_loopback or self.allow_remote
        except ValueError:
            return self.allow_remote and host in self.settings.get()['webui']['allowed_hosts']

    def remote_ready(self):
        return self.allow_remote and self.auth.status()['mode'] == 'password'

    def secure_transport(self, request):
        cfg = self.settings.get()['webui']
        public = cfg['public_url']
        peer = request.client.host if request.client else ''
        try:
            address = ipaddress.ip_address(peer) if peer != 'testclient' else None
            if address and address.version == 6 and address.ipv4_mapped: address = address.ipv4_mapped
            trusted_proxy = peer == 'testclient' or address.is_loopback or str(address) in cfg['trusted_proxy_ips']
        except ValueError: trusted_proxy = False
        return request.url.scheme == 'https' or bool(trusted_proxy and public
            and request.headers.get('host','').lower() == urlsplit(public).netloc.lower()
            and request.headers.get('x-forwarded-proto','').lower() == 'https')

    def origin_allowed(self, request, origin):
        if origin == str(request.base_url).rstrip('/'):
            return True
        public = self.settings.get()['webui']['public_url']
        return bool(public and origin == public and request.headers.get('host','').lower() == urlsplit(public).netloc.lower())

    def binding(self, request):
        # Listener state belongs to this running process. Host headers behind
        # HTTPS proxies describe a public endpoint, not our bound socket.
        actual_host = getattr(self.app.state, 'listen_host', '0.0.0.0' if self.allow_remote else '127.0.0.1')
        port = getattr(self.app.state, 'listen_port', None) or request.url.port or (443 if request.url.scheme == 'https' else 80)
        requested_port = getattr(self.app.state, 'requested_port', port)
        changed = port != requested_port
        warning = (f'启动端口发生变化：请求 {requested_port}，实际监听 {port}。固定端口的 OneBot 反向 WebSocket、反向代理和容器映射不会自动修改。'
                   '请释放原端口后重启，或同步修改外部地址；服务器可用 --strict-port 禁止顺延。') if changed else ''
        return dict(actual_host=actual_host, actual_port=port, requested_port=requested_port, port_changed=changed, port_warning=warning)

    def access(self, request):
        cfg = self.settings.get()['webui']
        binding = self.binding(request)
        actual_host, port = binding['actual_host'], binding['actual_port']
        changed = self.access_saved or any(cfg[key] != self.initial_access[key] for key in ('remote_enabled','host','port'))
        restart = changed and (cfg['remote_enabled'] != self.bound_remote or (cfg['remote_enabled'] and cfg['host'] != actual_host) or cfg['port'] != port)
        urls = [f'http://127.0.0.1:{port}/']
        if self.remote_ready():
            if cfg['public_url']:
                urls.append(cfg['public_url'] + '/')
            if actual_host in ('0.0.0.0','::'):
                try:
                    hosts = [value[4][0] for value in socket.getaddrinfo(socket.gethostname(), port, family=socket.AF_INET)]
                except OSError:
                    hosts = []
            else:
                hosts = [actual_host]
            for host in dict.fromkeys(hosts):
                try:
                    address = ipaddress.ip_address(host)
                    if not cfg['require_https'] and not address.is_loopback and not address.is_unspecified:
                        urls.append(f'http://[{host}]:{port}/' if address.version == 6 else f'http://{host}:{port}/')
                except ValueError:
                    pass
        return dict(cfg, **binding, actual_remote_enabled=self.remote_ready(),
                    restart_required=restart, urls=urls)

    def status(self, request):
        value = self.auth.status(request.cookies.get(COOKIE_NAME, ''), is_local=local_peer(request))
        reveal = local_peer(request) and (self.proof(request) or value['authenticated'] or self.internal(request))
        value.pop('password_file', None)
        value.update(local_setup_allowed=bool(reveal), remote_enabled=self.settings.get()['webui']['remote_enabled'],
                     actual_remote_enabled=self.remote_ready(), host=self.settings.get()['webui']['host'],
                     port=self.settings.get()['webui']['port'])
        if reveal:
            value.update(data_dir=str(self.data_dir), password_file=str(self.auth.path))
        access = self.access(request)
        value.update(listen_host=access['actual_host'], restart_required=access['restart_required'], urls=access['urls'])
        value.update(self.binding(request))
        return value


def install_auth(app, settings, data_dir, *, allow_remote=False):
    http = AuthHTTP(app, settings, data_dir, allow_remote)
    app.state.web_auth, app.state.auth_http = http.auth, http

    @app.exception_handler(AuthError)
    async def auth_error(request, exc):
        return JSONResponse({'detail': str(exc), 'code': exc.code}, status_code=exc.status_code)

    def session_response(request, value):
        response = JSONResponse({'ok': True, 'expires_at': value['expires_at']})
        response.set_cookie(COOKIE_NAME, value['token'], httponly=True, samesite='strict',
                            secure=http.secure_transport(request), max_age=SESSION_SECONDS)
        response.delete_cookie('qunyu_session')
        response.delete_cookie('mikubot_session')
        response.delete_cookie('mikubot_bootstrap')
        return response

    @app.get('/api/auth/status')
    async def status(request: Request):
        return http.status(request)

    @app.post('/api/auth/bootstrap')
    async def bootstrap(request: Request, payload: dict = Body(...)):
        token = payload.get('token')
        if not local_peer(request) or not token_matches(token, settings.token):
            raise AuthError('请在运行 Shiori 的电脑上打开桌面程序设置密码', 403)
        response = JSONResponse({'ok': True})
        response.set_cookie(BOOTSTRAP_COOKIE, settings.token, httponly=True, samesite='strict',
                            secure=http.secure_transport(request), max_age=1800, path='/api/auth')
        return response

    @app.post('/api/auth/setup')
    async def setup(request: Request, payload: dict = Body(...)):
        if not http.proof(request):
            raise AuthError('首次设置及密码复位只能在本机启动的窗口完成', 403)
        mode = payload.get('mode', 'password')
        if mode == 'local_only':
            await asyncio.to_thread(http.auth.enable_local_only, is_local=True, bootstrap_token=settings.token)
            settings.update({'webui': {'remote_enabled': False}})
            http.allow_remote = False
            return {'ok': True, 'mode': 'local_only'}
        if mode != 'password':
            raise AuthError('请选择密码登录或仅本机免密')
        if 'confirm_password' in payload and payload['confirm_password'] != payload.get('password'):
            raise AuthError('两次输入的密码不一致')
        value = await asyncio.to_thread(http.auth.setup, payload.get('password'), is_local=True, bootstrap_token=settings.token)
        return session_response(request, value)

    @app.post('/api/auth/login')
    async def login(request: Request, payload: dict = Body(...)):
        value = await asyncio.to_thread(http.auth.login, payload.get('password'), peer=request.client.host if request.client else 'unknown')
        return session_response(request, value)

    @app.post('/api/auth/logout')
    async def logout(request: Request):
        http.auth.logout(request.cookies.get(COOKIE_NAME, ''))
        response = JSONResponse({'ok': True})
        for name in (COOKIE_NAME, 'mikubot_session', 'mikubot_bootstrap', 'qunyu_session'):
            response.delete_cookie(name)
        response.delete_cookie(BOOTSTRAP_COOKIE, path='/api/auth')
        return response

    @app.post('/api/auth/password')
    async def password(request: Request, payload: dict = Body(...)):
        if 'confirm_password' in payload and payload['confirm_password'] != payload.get('new_password'):
            raise AuthError('两次输入的密码不一致')
        if http.auth.status()['mode'] == 'local_only' and local_peer(request):
            value = await asyncio.to_thread(http.auth.setup, payload.get('new_password'), is_local=True, bootstrap_token=settings.token)
        else:
            value = await asyncio.to_thread(http.auth.change_password, payload.get('current_password'), payload.get('new_password'),
                                            token=request.cookies.get(COOKIE_NAME, ''), is_local=local_peer(request))
        return session_response(request, value)

    @app.get('/api/auth/access')
    async def get_access(request: Request):
        return http.access(request)

    @app.put('/api/auth/access')
    async def set_access(request: Request, payload: dict = Body(...)):
        if payload.get('remote_enabled') is True and http.auth.status()['mode'] != 'password':
            raise AuthError('开启远程访问前，请先在本机设置密码')
        settings.update({'webui': {key: value for key, value in payload.items() if key in ('remote_enabled', 'host', 'port', 'allowed_hosts', 'public_url','require_https','trusted_proxy_ips')}})
        http.access_saved = True
        if payload.get('remote_enabled') is False:
            # Rebinding waits for restart; access is revoked immediately.
            http.allow_remote = False
        return http.access(request)

    return http
