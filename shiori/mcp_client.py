"""Bounded MCP 2025 Streamable HTTP client; never retries a tool invocation.

Implements the initialize lifecycle of protocol versions 2025-03-26 through
2025-11-25. It deliberately does not launch processes, open the legacy SSE
transport, implement OAuth, sampling, roots, elicitation or task execution.
Official protocol: https://modelcontextprotocol.io/specification/2025-11-25
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import hashlib
import ipaddress
import json
import math
import re
import socket
from urllib.parse import urlsplit, urlunsplit

import httpx
from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError
from . import __version__

MAX_RESPONSE = 1024 * 1024
MAX_TOOLS = 50
MAX_SCHEMA = 12000
MAX_ARGUMENTS = 16000
MAX_PAGES = 10
PROTOCOL_VERSION = '2025-11-25'
SUPPORTED_VERSIONS = {'2025-03-26', '2025-06-18', PROTOCOL_VERSION}
_NAME = re.compile(r'^[A-Za-z0-9_.-]{1,128}$')


class MCPError(ValueError):
    """A fixed, credential-free failure safe to expose in the local UI."""


def validate_mcp_url(url: str, *, resolve: bool = False) -> str:
    """Static validation is suitable for settings; runtime also checks DNS."""
    if not isinstance(url, str) or not url or len(url) > 2000 or any(ord(c) < 33 for c in url):
        raise MCPError('MCP 地址格式无效')
    try:
        parsed = urlsplit(url)
        host = (parsed.hostname or '').rstrip('.').encode('idna').decode('ascii').lower()
        port = parsed.port
    except (ValueError, UnicodeError):
        raise MCPError('MCP 地址格式无效') from None
    if (not host or parsed.scheme not in ('http', 'https') or parsed.username is not None
            or parsed.password is not None or parsed.query or parsed.fragment or '\\' in url):
        raise MCPError('MCP 地址需为无凭据、查询参数和片段的 HTTP/HTTPS 地址')
    if port is not None and not 1 <= port <= 65535:
        raise MCPError('MCP 地址端口无效')
    local = host == 'localhost'
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        ip = None
    if ip is not None:
        local = ip.is_loopback
        if not local and not ip.is_global:
            raise MCPError('MCP 仅允许公开 HTTPS 或本机回环地址')
    else:
        if host.endswith(('.localhost', '.local', '.internal')) or '.' not in host and not local:
            raise MCPError('MCP 仅允许公开 HTTPS 或本机回环地址') from None
    if parsed.scheme != 'https' and not local:
        raise MCPError('远程 MCP 必须使用 HTTPS，本机回环可使用 HTTP')
    netloc = f'[{host}]' if ':' in host else host
    if port is not None:
        netloc += f':{port}'
    normalized = urlunsplit((parsed.scheme, netloc, parsed.path or '/', '', ''))
    if resolve:
        _addresses(normalized)
    return normalized


def _addresses(url: str) -> list[str]:
    parsed = urlsplit(url)
    host = parsed.hostname
    try:
        addresses = list(dict.fromkeys(x[4][0] for x in socket.getaddrinfo(
            host, parsed.port or (443 if parsed.scheme == 'https' else 80), type=socket.SOCK_STREAM)))
        ips = [ipaddress.ip_address(x) for x in addresses]
    except (OSError, ValueError):
        raise MCPError('无法解析 MCP 地址') from None
    local = host == 'localhost'
    try:
        local = local or ipaddress.ip_address(host).is_loopback
    except ValueError:
        pass
    if not ips or (local and not all(x.is_loopback for x in ips)) or (not local and not all(x.is_global for x in ips)):
        raise MCPError('MCP 地址解析到不允许的网络范围')
    return addresses


class _PinnedTransport(httpx.AsyncBaseTransport):
    """Connect to a validated address while retaining HTTPS hostname checks."""
    def __init__(self, url: str, address: str):
        self.url, self.address = httpx.URL(url), address
        self.inner = httpx.AsyncHTTPTransport(retries=0, trust_env=False)

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        headers = request.headers.copy()
        # Explicit Host matters because the network URL contains the pinned IP.
        headers['Host'] = self.url.netloc.decode('ascii')
        extensions = dict(request.extensions, sni_hostname=self.url.host)
        pinned = httpx.Request(request.method, request.url.copy_with(host=self.address),
                               headers=headers, stream=request.stream, extensions=extensions)
        return await self.inner.handle_async_request(pinned)

    async def aclose(self):
        await self.inner.aclose()


def _json_bytes(value, *, limit: int, message: str) -> bytes:
    # Prevent recursive objects, JSON recursion bombs and NaN before serialization.
    def walk(item, depth=0, ancestors=None):
        if depth > 20:
            raise MCPError(message)
        if item is None or type(item) in (str, bool, int):
            return
        if type(item) is float:
            if not math.isfinite(item):
                raise MCPError(message)
            return
        if type(item) not in (dict, list):
            raise MCPError(message)
        ancestors = set() if ancestors is None else ancestors
        if id(item) in ancestors or len(item) > 1000:
            raise MCPError(message)
        ancestors.add(id(item))
        if isinstance(item, dict):
            if not all(isinstance(k, str) for k in item):
                raise MCPError(message)
            values = item.values()
        else:
            values = item
        for child in values:
            walk(child, depth + 1, ancestors)
        ancestors.remove(id(item))
    try:
        walk(value)
        data = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False).encode('utf-8')
    except (TypeError, ValueError, RecursionError, UnicodeError):
        raise MCPError(message) from None
    if len(data) > limit:
        raise MCPError(message)
    return data


def schema_hash(schema: dict) -> str:
    if not isinstance(schema, dict) or schema.get('type') != 'object':
        raise MCPError('MCP 工具参数描述必须为对象结构')
    data = _json_bytes(schema, limit=MAX_SCHEMA, message='MCP 工具参数描述过大或嵌套过深')
    _validate_schema(schema)
    return hashlib.sha256(data).hexdigest()


def _validate_schema(root):
    """Use standard Draft 2020-12 with references and regex disabled."""
    message = 'MCP 工具参数描述无效或包含不支持的引用/正则'
    def visit(node, depth=0):
        if depth > 12:
            raise MCPError('MCP 工具参数描述嵌套超过限制')
        if isinstance(node, dict):
            if any(key in node for key in ('$ref', '$dynamicRef', '$recursiveRef', 'pattern', 'patternProperties')):
                raise MCPError(message)
            for child in node.values():
                visit(child, depth + 1)
        elif isinstance(node, list):
            for child in node:
                visit(child, depth + 1)
    visit(root)
    try:
        Draft202012Validator.check_schema(root)
    except (SchemaError, ValueError, TypeError, RecursionError):
        raise MCPError(message) from None


def _decode_json(raw: bytes):
    try:
        return json.loads(raw.decode('utf-8'), parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
    except (ValueError, UnicodeError, RecursionError):
        raise MCPError('MCP 服务返回了无效数据') from None


def _rpc_result(value, request_id):
    if not isinstance(value, dict) or value.get('jsonrpc') != '2.0':
        raise MCPError('MCP 服务返回了无效协议消息')
    if value.get('id') != request_id or type(value.get('id')) is not type(request_id):
        raise MCPError('MCP 服务响应与请求不匹配')
    if 'error' in value:
        raise MCPError('MCP 服务拒绝了请求')
    if not isinstance(value.get('result'), dict):
        raise MCPError('MCP 服务返回了无效结果')
    return value['result']


class _SSEEvents:
    """Parse UTF-8 SSE framing across arbitrary chunks and CR/LF boundaries."""
    def __init__(self):
        self.pending = bytearray()
        self.data_lines = []
        self.first = True
        self.skip_lf = False

    def feed(self, chunk):
        self.pending.extend(chunk)
        if self.first:
            if len(self.pending) < 3:
                return
            self.first = False
            if self.pending.startswith(b'\xef\xbb\xbf'):
                del self.pending[:3]
        while self.pending:
            if self.skip_lf:
                self.skip_lf = False
                if self.pending[0] == 10:
                    del self.pending[0]
                    if not self.pending:
                        break
            match = re.search(br'[\r\n]', self.pending)
            if not match:
                break
            position = match.start()
            line = self.pending[:position]
            delimiter = self.pending[position]
            del self.pending[:position + 1]
            self.skip_lf = delimiter == 13
            if line.startswith(b'data:'):
                self.data_lines.append(bytes(line[5:]).removeprefix(b' '))
            elif not line:
                if not self.data_lines:
                    continue
                event = b'\n'.join(self.data_lines)
                self.data_lines.clear()
                if event.strip():
                    yield event


class _Session:
    def __init__(self, client, url, token):
        self.client, self.url, self.token = client, url, token
        self.session_id, self.version, self.counter = '', '', 0

    def headers(self):
        headers = {'Accept': 'application/json, text/event-stream', 'Content-Type': 'application/json',
                   'Accept-Encoding': 'identity'}
        if self.token:
            headers['Authorization'] = 'Bearer ' + self.token
        if self.session_id:
            headers['MCP-Session-Id'] = self.session_id
        if self.version:
            headers['MCP-Protocol-Version'] = self.version
        return headers

    async def request(self, method, params=None, *, notification=False):
        body = {'jsonrpc': '2.0', 'method': method}
        request_id = None
        if not notification:
            self.counter += 1
            request_id = self.counter
            body['id'] = request_id
        if params is not None:
            body['params'] = params
        async with self.client.stream('POST', self.url, json=body, headers=self.headers()) as response:
            if response.status_code >= 300 or response.status_code < 200:
                raise MCPError('MCP 服务连接失败或拒绝了请求')
            if method == 'initialize':
                sid = response.headers.get('MCP-Session-Id', '')
                if len(sid) > 256 or any(not 0x21 <= ord(c) <= 0x7e for c in sid):
                    raise MCPError('MCP 服务会话标识无效')
                self.session_id = sid
            if notification:
                if response.status_code != 202:
                    raise MCPError('MCP 服务未接受初始化通知')
                return None
            content_type = response.headers.get('Content-Type', '').split(';', 1)[0].strip().lower()
            if content_type not in ('application/json', 'text/event-stream'):
                raise MCPError('MCP 服务响应格式不受支持')
            length = response.headers.get('Content-Length')
            if length and (not length.isdigit() or int(length) > MAX_RESPONSE):
                raise MCPError('MCP 服务响应超过 1MB 限制')
            raw = bytearray()
            events = _SSEEvents()
            async for chunk in response.aiter_bytes():
                raw.extend(chunk)
                if len(raw) > MAX_RESPONSE:
                    raise MCPError('MCP 服务响应超过 1MB 限制')
                if content_type != 'text/event-stream':
                    continue
                for event in events.feed(chunk):
                    value = _decode_json(event)
                    if isinstance(value, dict) and 'method' in value and 'id' not in value:
                        continue  # Ignore progress/log notifications, never display them.
                    # We advertised no server-to-client request capabilities.
                    if isinstance(value, dict) and 'method' in value:
                        raise MCPError('MCP 服务请求了未启用的客户端能力')
                    return _rpc_result(value, request_id)
            if content_type == 'text/event-stream':
                raise MCPError('MCP 服务响应流在返回结果前中断')
            return _rpc_result(_decode_json(bytes(raw)), request_id)

    async def initialize(self):
        result = await self.request('initialize', {'protocolVersion': PROTOCOL_VERSION,
            'capabilities': {}, 'clientInfo': {'name': 'shiori', 'version': __version__}})
        version = result.get('protocolVersion')
        if version not in SUPPORTED_VERSIONS:
            raise MCPError('MCP 服务协议版本不受支持（仅支持 2025 Streamable HTTP）')
        capabilities = result.get('capabilities')
        if not isinstance(capabilities, dict) or not isinstance(capabilities.get('tools'), dict):
            raise MCPError('MCP 服务未声明工具能力')
        self.version = version
        await self.request('notifications/initialized', notification=True)

    async def tools(self):
        tools, seen_names, cursors = [], set(), set()
        cursor = None
        for _ in range(MAX_PAGES):
            page = await self.request('tools/list', {'cursor': cursor} if cursor else {})
            records = page.get('tools')
            if not isinstance(records, list) or len(seen_names) + len(records) > MAX_TOOLS:
                raise MCPError('MCP 工具目录无效或超过 50 个工具限制')
            for record in records:
                if not isinstance(record, dict) or not isinstance(record.get('name'), str) or not _NAME.fullmatch(record['name']):
                    raise MCPError('MCP 工具名称格式不受支持')
                name = record['name']
                if name in seen_names:
                    raise MCPError('MCP 工具目录包含重复名称')
                seen_names.add(name)
                schema = record.get('inputSchema')
                fingerprint = schema_hash(schema)
                execution = record.get('execution', {})
                if not isinstance(execution, dict) or execution.get('taskSupport') == 'required':
                    # Tasks need separate polling and approval semantics.
                    continue
                description = record.get('description', '')
                if not isinstance(description, str) or len(description) > 4000:
                    raise MCPError('MCP 工具描述格式无效或过长')
                tools.append({'name': name, 'description': description, 'inputSchema': schema,
                              'schema_hash': fingerprint})
            cursor = page.get('nextCursor')
            if cursor is None:
                return tools
            if not isinstance(cursor, str) or not cursor or len(cursor) > 1000 or cursor in cursors:
                raise MCPError('MCP 工具分页游标无效')
            cursors.add(cursor)
        raise MCPError('MCP 工具目录分页超过限制')

    async def close(self):
        if self.session_id:
            try:
                async with asyncio.timeout(2):
                    async with self.client.stream('DELETE', self.url, headers=self.headers()):
                        pass
            except Exception:
                pass


class MCPClient:
    def __init__(self, *, transport=None, timeout=20):
        self.transport = transport
        self.timeout = min(20, max(0.01, float(timeout)))

    @asynccontextmanager
    async def _session(self, config):
        if not isinstance(config, dict) or not config.get('enabled', False):
            raise MCPError('此 MCP 服务尚未启用')
        url = validate_mcp_url(config.get('url', ''))
        token = config.get('token', '')
        if not isinstance(token, str) or len(token) > 8000 or any(ord(c) < 0x21 or ord(c) > 0x7e for c in token):
            raise MCPError('MCP 访问凭据格式无效')
        deadline = asyncio.get_running_loop().time() + self.timeout
        async with asyncio.timeout_at(deadline):
            addresses = await asyncio.to_thread(_addresses, url)
        transport = self.transport or _PinnedTransport(url, addresses[0])
        async with httpx.AsyncClient(transport=transport, timeout=self.timeout, follow_redirects=False, trust_env=False) as client:
            session = _Session(client, url, token)
            try:
                async with asyncio.timeout_at(deadline):
                    yield session
            finally:
                # Cleanup has its own bound and cannot turn a completed call into
                # a failure just because the operation's deadline has elapsed.
                await session.close()

    async def discover(self, config) -> dict:
        try:
            async with self._session(config) as session:
                await session.initialize()
                tools = await session.tools()
                if config.get('token') and config['token'] in json.dumps(tools, ensure_ascii=False):
                    raise MCPError('MCP 工具目录包含不允许的敏感数据')
                return {'tools': tools, 'protocol_version': session.version}
        except MCPError:
            raise
        except (httpx.HTTPError, TimeoutError, OSError, ValueError):
            raise MCPError('无法连接 MCP 服务或读取工具目录，请检查地址、凭据及协议') from None

    async def call(self, config, name: str, arguments: dict, expected_schema_hash: str | None = None, *, before_call=None) -> dict:
        if not isinstance(name, str) or not _NAME.fullmatch(name):
            raise MCPError('MCP 工具名称格式不受支持')
        if not isinstance(arguments, dict):
            raise MCPError('MCP 工具参数必须为对象')
        _json_bytes(arguments, limit=MAX_ARGUMENTS, message='MCP 工具参数过大或嵌套过深')
        allowed = config.get('allowed_tools', []) if isinstance(config, dict) else []
        if not isinstance(allowed, list) or name not in allowed:
            raise MCPError('此 MCP 工具未获准使用')
        attempted = False
        try:
            async with self._session(config) as session:
                await session.initialize()
                tools = await session.tools()
                tool = next((x for x in tools if x['name'] == name), None)
                if not tool:
                    raise MCPError('MCP 工具已移除或无法以普通请求调用')
                if expected_schema_hash is not None and expected_schema_hash != tool['schema_hash']:
                    raise MCPError('MCP 工具参数描述已变化，请重新检查并确认')
                if not Draft202012Validator(tool['inputSchema']).is_valid(arguments):
                    raise MCPError('MCP 工具参数不符合已确认的结构')
                if before_call is not None:
                    before_call()  # No await between the final permission check and POST.
                if not config.get('enabled') or name not in config.get('allowed_tools', []):
                    raise MCPError('MCP 工具权限已撤回')
                attempted = True
                result = await session.request('tools/call', {'name': name, 'arguments': arguments})
                return self._content(result, config.get('token', ''))
        except asyncio.CancelledError:
            # The caller owns cancellation; do not resume or retry side effects.
            raise
        except MCPError:
            if attempted:
                raise MCPError('MCP 工具执行结果未知，请先检查外部服务；系统不会自动重试') from None
            raise
        except (httpx.HTTPError, TimeoutError, OSError, ValueError):
            if attempted:
                raise MCPError('MCP 工具执行结果未知，请先检查外部服务；系统不会自动重试') from None
            raise MCPError('MCP 工具调用前检查失败，请重新检查服务、权限和参数描述') from None

    @staticmethod
    def _content(result, token):
        content = result.get('content')
        if not isinstance(content, list) or len(content) > 100 or type(result.get('isError', False)) is not bool:
            raise MCPError('MCP 工具返回格式无效')
        if result.get('isError'):
            # Server-provided error bodies can contain credentials or internals.
            text = '外部 MCP 工具报告执行失败，请检查外部服务；系统不会自动重试'
            return {'content': [{'type': 'text', 'text': text}], 'isError': True, 'text': text}
        blocks, texts = [], []
        for item in content:
            if not isinstance(item, dict):
                raise MCPError('MCP 工具返回格式无效')
            if item.get('type') == 'text' and isinstance(item.get('text'), str):
                text = item['text'].replace(token, '[凭据已隐藏]') if token else item['text']
                blocks.append({'type': 'text', 'text': text})
                texts.append(text)
            elif item.get('type') in ('image', 'audio', 'resource', 'resource_link'):
                # No attachment fetching, base64 rendering or automatic link navigation.
                blocks.append({'type': 'text', 'text': '[外部工具返回了附件，当前仅展示文本]'})
            else:
                raise MCPError('MCP 工具返回格式无效')
        structured = result.get('structuredContent')
        if structured is not None and not isinstance(structured, dict):
            raise MCPError('MCP 工具结构化结果必须为对象')
        if structured is not None and not texts:
            text = _json_bytes(structured, limit=MAX_RESPONSE, message='MCP 工具结构化结果无效').decode('utf-8')
            text = text.replace(token, '[凭据已隐藏]') if token else text
            blocks.append({'type': 'text', 'text': text})
            texts.append(text)
        return {'content': blocks, 'isError': result.get('isError', False), 'text': '\n'.join(texts)}
