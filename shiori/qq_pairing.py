"""Loopback-only connector configuration through supported WebUI APIs.

No connector management credentials or whole connector configurations are
written to disk or included in public results. Unknown schemas fail closed.
"""
from __future__ import annotations

import asyncio
import copy
import hashlib
import ipaddress
import json
import re
import secrets
from urllib.parse import urlsplit

import httpx


def loopback_url(value, *, websocket=False):
    try:
        parsed = urlsplit(str(value))
        allowed_schemes = ('ws','wss') if websocket else ('http','https')
        host = parsed.hostname or ''
        local = host == 'localhost' or ipaddress.ip_address(host).is_loopback
        port = parsed.port
    except (ValueError, TypeError):
        raise ValueError('本机连接地址须为有效的回环IP或localhost地址') from None
    if (parsed.scheme not in allowed_schemes or not local or not port or parsed.username is not None
            or parsed.password is not None or parsed.query or parsed.fragment or (not websocket and parsed.path not in ('','/'))):
        raise ValueError('自动配对仅支持本机回环地址，不能包含凭据、参数或额外路径；其他环境请手动配置')
    host = '127.0.0.1' if host == 'localhost' else host
    host = f'[{host}]' if ':' in host else host
    return f'{parsed.scheme}://{host}:{port}' + (parsed.path if websocket else '')


def revision(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


class LocalQQPairing:
    def __init__(self, settings, reverse_url, *, transport=None):
        self.settings = settings
        self.reverse_url = loopback_url(reverse_url, websocket=True)
        if urlsplit(self.reverse_url).path != '/onebot/v11/ws':
            raise ValueError('反向WebSocket须指向本机Shiori的/onebot/v11/ws入口')
        self.transport = transport
        self._lock = asyncio.Lock()
        self._undo = {}
        suffix = hashlib.sha256(str(getattr(settings, 'token', '') or settings.data_dir).encode()).hexdigest()[:12]
        self.connection_name = 'shiori-' + suffix
        self._legacy_connection_name = 'mikubot-' + suffix

    def _client(self, base_url):
        return httpx.AsyncClient(base_url=base_url, timeout=5, trust_env=False, follow_redirects=False, transport=self.transport)

    async def _request(self, client, method, path, body=None, credential=''):
        headers = {'Authorization':'Bearer ' + credential} if credential else {}
        headers['Origin'] = str(client.base_url).rstrip('/')
        try:
            response = await client.request(method, path, json=body, headers=headers)
            if response.status_code != 200:
                hint = '请先在连接器WebUI完成初始化、协议确认或修改初始密码' if response.status_code == 403 else '请检查WebUI地址、口令及登录状态'
                raise ValueError(f'连接器请求未成功（HTTP {response.status_code}）；{hint}')
            if len(response.content) > 2 * 1024 * 1024:
                raise ValueError('连接器返回过大，请改用手动配置')
            result = response.json()
            if not isinstance(result, dict):
                raise ValueError('连接器返回格式无法识别，请改用手动配置')
            return result
        except (httpx.HTTPError, json.JSONDecodeError, UnicodeError):
            raise ValueError('无法读取连接器WebUI，请检查是否启动以及地址是否正确') from None

    @staticmethod
    def _nap_data(result):
        if result.get('code') != 0:
            raise ValueError('NapCat请求失败，请检查WebUI口令、QQ登录和连接器版本')
        return result.get('data')

    async def _login(self, client, adapter, password, totp):
        if not isinstance(password, str) or not password or len(password) > 4096 or not isinstance(totp, str) or len(totp) > 100:
            raise ValueError('请输入连接器WebUI管理口令')
        if adapter == 'napcat':
            data = self._nap_data(await self._request(client, 'POST', '/api/auth/login', {
                'hash':hashlib.sha256((password + '.napcat').encode()).hexdigest(), 'totpCode':totp}))
            if isinstance(data, dict) and data.get('require2FA'):
                raise ValueError('NapCat已开启两步验证，请填写当前验证码')
            token = data.get('Credential') if isinstance(data, dict) else None
        else:
            result = await self._request(client, 'POST', '/api/login', {'password':password, 'totp':totp})
            if result.get('needsTotp'):
                raise ValueError('SnowLuma已开启两步验证，请填写当前验证码')
            if result.get('mustChangePassword'):
                raise ValueError('请先打开SnowLuma WebUI修改初始密码，再返回配对')
            token = result.get('token') if result.get('success') is True else None
        if not isinstance(token, str) or not token or len(token) > 8192:
            raise ValueError('连接器登录未成功，请检查管理口令或先在WebUI完成初始化')
        return token

    async def _read(self, client, adapter, account_id, credential):
        if adapter == 'napcat':
            value = self._nap_data(await self._request(client, 'POST', '/api/OB11Config/GetConfig', {}, credential))
        else:
            value = (await self._request(client, 'GET', '/api/config/' + account_id, credential=credential)).get('config')
        self._clients(value, adapter)
        return value

    @staticmethod
    def _clients(config, adapter):
        root, key = ('network','websocketClients') if adapter == 'napcat' else ('networks','wsClients')
        if not isinstance(config, dict) or not isinstance(config.get(root), dict) or not isinstance(config[root].get(key), list):
            raise ValueError('连接器配置版本暂不支持自动配对，请使用手动反向WebSocket配置')
        items = config[root][key]
        if len(items) > 100 or any(not isinstance(item, dict) or not isinstance(item.get('name'), str) for item in items):
            raise ValueError('连接器配置格式异常，请使用手动配置')
        return items

    async def _write(self, client, adapter, account_id, config, credential):
        if adapter == 'napcat':
            self._nap_data(await self._request(client, 'POST', '/api/OB11Config/SetConfig', {'config':json.dumps(config, ensure_ascii=False)}, credential))
            # NapCat acknowledges its configuration callback, not a live QQ
            # socket. Only the bridge status can establish an actual connection.
            return {'saved':True, 'applied':None}
        result = await self._request(client, 'POST', '/api/config/' + account_id, config, credential)
        if result.get('success') is not True or result.get('saved') is not True:
            raise ValueError('SnowLuma未确认保存配置，请在WebUI检查')
        return {'saved':True, 'applied':result.get('applied') is True, 'online':result.get('online') is True}

    async def discover(self):
        """Two standard ports only; reachable does not claim product identity."""
        async def probe(adapter, port):
            url = f'http://127.0.0.1:{port}'
            try:
                async with self._client(url) as client:
                    response = await client.get('/', timeout=0.7)
                    reachable = 200 <= response.status_code < 500
            except httpx.HTTPError:
                reachable = False
            return {'adapter':adapter, 'webui_url':url, 'reachable':reachable, 'verified':False,
                    'message':'发现本机Web服务，填写口令后验证连接器' if reachable else '标准端口未响应，可填写自定义本机地址'}
        return {'candidates':await asyncio.gather(probe('napcat',6099), probe('snowluma',5099)),
                'message':'仅检查两个常用本机端口，不修改配置；容器或远程连接请使用手动配置'}

    async def connect(self, adapter, webui_url, password, account_id='', totp='', expected_revision=None):
        if adapter not in ('napcat','snowluma'):
            raise ValueError('自动配对仅支持NapCat或SnowLuma')
        base_url = loopback_url(webui_url)
        async with self._lock, self._client(base_url) as client:
            credential = await self._login(client, adapter, password, totp)
            if adapter == 'snowluma':
                accounts = (await self._request(client, 'GET', '/api/qq-list', credential=credential)).get('list')
                if not isinstance(accounts, list):
                    raise ValueError('SnowLuma账号列表无法识别，请使用手动配置')
                accounts = [{'account_id':str(item.get('uin','')), 'label':str(item.get('nickname',''))[:80]}
                            for item in accounts if isinstance(item, dict) and re.fullmatch(r'\d{5,10}', str(item.get('uin','')))]
                if not accounts:
                    raise ValueError('SnowLuma尚无在线QQ账号，请先在连接器登录')
                if not account_id and len(accounts) != 1:
                    return {'requires_account':True, 'accounts':accounts, 'adapter':adapter, 'webui_url':base_url}
                account_id = str(account_id or accounts[0]['account_id'])
                if account_id not in {item['account_id'] for item in accounts}:
                    raise ValueError('所选QQ账号当前不在线，请重新选择')
            current = await self._read(client, adapter, account_id, credential)
            initial_revision = revision(current)
            if expected_revision is not None and expected_revision != initial_revision:
                raise ValueError('连接器配置已变更，请重新读取后再配对')
            updated = copy.deepcopy(current)
            clients = self._clients(updated, adapter)
            own = [item for item in clients if item['name'] in (self.connection_name, self._legacy_connection_name)]
            if len(own) > 1:
                raise ValueError('发现重名Shiori连接，请在WebUI检查后重试')
            receiver = self.settings.get()['onebot']
            token = str(receiver.get('access_token') or '')
            old_token = str((own[0] if own else {}).get('token' if adapter == 'napcat' else 'accessToken') or '')
            token = token if len(token) >= 12 else old_token if len(old_token) >= 12 else secrets.token_urlsafe(32)
            item = copy.deepcopy(own[0]) if own else {'name':self.connection_name}
            item['name'] = self.connection_name
            if adapter == 'napcat':
                item.update(enable=True, url=self.reverse_url, token=token, messagePostFormat='array', reportSelfMessage=False)
                item.setdefault('reconnectInterval', 5000)
                item.setdefault('heartInterval', 30000)
                item.setdefault('debug', False)
            else:
                item.update(enabled=True, url=self.reverse_url, accessToken=token, messageFormat='array', reportSelfMessage=False, role='Universal')
                item.setdefault('reconnectIntervalMs', 5000)
            if own:
                clients[clients.index(own[0])] = item
            else:
                clients.append(item)
            changed = current != updated
            status = {'saved':True, 'applied':None}
            local_patch = {'onebot':{'adapter':adapter, 'enabled':True, 'ws_url':'', 'access_token':token}, 'qq_official':{'enabled':False}}
            self.settings.update(local_patch, dry_run=True)
            if changed:
                latest = await self._read(client, adapter, account_id, credential)
                if revision(latest) != initial_revision:
                    raise ValueError('连接器配置刚刚被其他窗口修改，已停止写入，请重新配对')
                status = await self._write(client, adapter, account_id, updated, credential)
                after = await self._read(client, adapter, account_id, credential)
                if after != updated:
                    raise ValueError('连接器未确认完整保存或发生并发修改，请检查WebUI；未自动重复写入')
            self.settings.update(local_patch)
            undo_id = secrets.token_urlsafe(16)
            # Only the managed node fingerprint is retained; management tokens
            # and connector access tokens are not cached by this helper.
            self._undo[undo_id] = {'adapter':adapter,'webui_url':base_url,'account_id':account_id,'node_revision':revision(item)}
            if len(self._undo) > 32:
                self._undo.pop(next(iter(self._undo)))
            message = ('专属连接已保存，但连接器未确认热加载成功，请打开WebUI检查，再查看Shiori连接状态'
                       if status.get('applied') is False else
                       '专属反向连接已保存，请查看QQ连接状态并发送测试消息；WebUI管理口令不会保存')
            return dict(status, adapter=adapter, webui_url=base_url, account_id=account_id, changed=changed,
                        connection_name=self.connection_name, revision=revision(updated), undo_id=undo_id,
                        message=message)

    async def disconnect(self, undo_id, password, totp=''):
        async with self._lock:
            record = self._undo.get(undo_id)
            if record is None:
                raise ValueError('撤销入口已过期，请在连接器WebUI删除Shiori专属连接')
            adapter, account_id = record['adapter'], record['account_id']
            async with self._client(record['webui_url']) as client:
                credential = await self._login(client, adapter, password, totp)
                current = await self._read(client, adapter, account_id, credential)
                updated = copy.deepcopy(current)
                clients = self._clients(updated, adapter)
                own = [item for item in clients if item['name'] == self.connection_name]
                if len(own) > 1 or own and revision(own[0]) != record['node_revision']:
                    raise ValueError('专属连接已被修改，停止撤销；请在WebUI确认后手动处理')
                if own:
                    clients.remove(own[0])
                    if revision(await self._read(client, adapter, account_id, credential)) != revision(current):
                        raise ValueError('连接器配置已变更，已停止撤销，请重试')
                    status = await self._write(client, adapter, account_id, updated, credential)
                    if await self._read(client, adapter, account_id, credential) != updated:
                        raise ValueError('未确认撤销保存，请在WebUI检查；未自动重复写入')
                else:
                    status = {'saved':True,'applied':None}
                del self._undo[undo_id]
                return dict(status, changed=bool(own), connection_name=self.connection_name,
                            message='Shiori专属连接已移除，其他连接保持原样')
