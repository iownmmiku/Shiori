import asyncio
import json
import socket

import httpx
import pytest

from shiori.mcp_client import MCPClient, MCPError, MAX_RESPONSE, _PinnedTransport, schema_hash, validate_mcp_url


SCHEMA = {'type': 'object', 'properties': {'text': {'type': 'string', 'maxLength': 200}},
          'required': ['text'], 'additionalProperties': False}
TOOL = {'name': 'notes.add', 'description': '保存一条笔记', 'inputSchema': SCHEMA}


@pytest.fixture(autouse=True)
def public_dns(monkeypatch):
    monkeypatch.setattr(socket, 'getaddrinfo', lambda *a, **kw: [(socket.AF_INET, socket.SOCK_STREAM, 6, '', ('8.8.8.8', 443))])


def config(**changes):
    return {'id': 'notes', 'name': '笔记', 'url': 'https://fixture.example/mcp',
            'token': 'fixture-credential', 'enabled': True, 'allowed_tools': ['notes.add'], **changes}


def rpc(request, result, *, sse=False, headers=None):
    body = json.loads(request.content)
    response = {'jsonrpc': '2.0', 'id': body['id'], 'result': result}
    if sse:
        data = ('id: priming\ndata:\n\n'
                'event: message\ndata: {"jsonrpc":"2.0","method":"notifications/progress","params":{}}\n\n'
                'event: message\ndata: ' + json.dumps(response, ensure_ascii=False) + '\n\n').encode()
        return httpx.Response(200, content=data, headers={'Content-Type': 'text/event-stream; charset=utf-8', **(headers or {})})
    return httpx.Response(200, json=response, headers=headers)


def server(seen, *, tools=None, result=None, sse=False, callback=None, version='2025-11-25'):
    def handle(request):
        if request.method == 'DELETE':
            seen.append(('DELETE', dict(request.headers)))
            return httpx.Response(405)
        body = json.loads(request.content)
        method = body['method']
        seen.append((method, body, dict(request.headers)))
        assert request.headers['authorization'] == 'Bearer fixture-credential'
        assert request.headers['accept'] == 'application/json, text/event-stream'
        if method == 'initialize':
            assert 'mcp-session-id' not in request.headers
            assert body['params']['capabilities'] == {}
            return rpc(request, {'protocolVersion': version, 'capabilities': {'tools': {}},
                                 'serverInfo': {'name': 'fixture', 'version': '1'}}, sse=sse,
                       headers={'MCP-Session-Id': 'fixture-session'})
        assert request.headers['mcp-session-id'] == 'fixture-session'
        assert request.headers['mcp-protocol-version'] == version
        if callback:
            override = callback(request, body)
            if override is not None:
                return override
        if method == 'notifications/initialized':
            assert 'id' not in body
            return httpx.Response(202)
        if method == 'tools/list':
            return rpc(request, {'tools': tools if tools is not None else [TOOL]}, sse=sse)
        assert method == 'tools/call'
        return rpc(request, result if result is not None else {'content': [{'type': 'text', 'text': '已保存'}]}, sse=sse)
    return httpx.MockTransport(handle)


@pytest.mark.parametrize('sse', [False, True])
def test_handshake_discover_and_one_call(sse):
    seen = []
    client = MCPClient(transport=server(seen, sse=sse))
    discovered = asyncio.run(client.discover(config()))
    assert discovered['protocol_version'] == '2025-11-25'
    assert discovered['tools'][0]['schema_hash'] == schema_hash(SCHEMA)
    result = asyncio.run(client.call(config(), 'notes.add', {'text': '买牛奶'}, discovered['tools'][0]['schema_hash']))
    assert result == {'content': [{'type': 'text', 'text': '已保存'}], 'isError': False, 'text': '已保存'}
    assert [x[0] for x in seen] == ['initialize', 'notifications/initialized', 'tools/list', 'DELETE',
                                     'initialize', 'notifications/initialized', 'tools/list', 'tools/call', 'DELETE']


@pytest.mark.parametrize('version', ['2025-03-26', '2025-06-18'])
def test_version_negotiation(version):
    assert asyncio.run(MCPClient(transport=server([], version=version)).discover(config()))['protocol_version'] == version


def test_future_protocol_rejected_without_call():
    seen = []
    with pytest.raises(MCPError, match='协议版本'):
        asyncio.run(MCPClient(transport=server(seen, version='2026-07-28')).discover(config()))
    assert [x[0] for x in seen] == ['initialize', 'DELETE']


def test_pagination_bounded_and_no_duplicate_cursor():
    seen = []
    def callback(request, body):
        if body['method'] != 'tools/list':
            return None
        if body['params'].get('cursor') == 'next':
            return rpc(request, {'tools': [{**TOOL, 'name': 'notes.read'}]})
        return rpc(request, {'tools': [TOOL], 'nextCursor': 'next'})
    result = asyncio.run(MCPClient(transport=server(seen, callback=callback)).discover(config()))
    assert [x['name'] for x in result['tools']] == ['notes.add', 'notes.read']
    def cycle(request, body):
        if body['method'] == 'tools/list':
            return rpc(request, {'tools': [], 'nextCursor': 'same'})
    with pytest.raises(MCPError, match='游标'):
        asyncio.run(MCPClient(transport=server([], callback=cycle)).discover(config()))


@pytest.mark.parametrize('tools', [[TOOL] * 2, [{**TOOL, 'name': f't{i}'} for i in range(51)]])
def test_duplicate_or_oversized_catalog_rejected(tools):
    with pytest.raises(MCPError):
        asyncio.run(MCPClient(transport=server([], tools=tools)).discover(config()))


@pytest.mark.parametrize('schema', [
    {'type': 'array'}, {'type': 'object', 'properties': []},
    {'type': 'object', 'required': 'x'}, {'type': 'object', 'properties': {'x': {'type': 'bad'}}},
    {'type': 'object', '$ref': 'https://private.example/schema'},
    {'type': 'object', 'properties': {'x': {'$ref': '#/$defs/x'}}},
    {'type': 'object', 'properties': {'x': {'type': 'string', 'pattern': '(a+)+$'}}},
    {'type': 'object', 'description': 'x' * 12000},
])
def test_malformed_or_unsafe_schema_rejected(schema):
    with pytest.raises(MCPError):
        asyncio.run(MCPClient(transport=server([], tools=[{**TOOL, 'inputSchema': schema}])).discover(config()))


def test_recursive_and_deep_python_schema_rejected():
    recursive = {'type': 'object'}
    recursive['properties'] = {'x': recursive}
    with pytest.raises(MCPError):
        schema_hash(recursive)
    deep = {'type': 'object'}
    current = deep
    for _ in range(15):
        current['properties'] = {'x': {'type': 'object'}}
        current = current['properties']['x']
    with pytest.raises(MCPError):
        schema_hash(deep)


@pytest.mark.parametrize('arguments', [{}, {'text': 123}, {'text': 'x', 'extra': 1}, {'text': 'x' * 201}])
def test_arguments_validated_before_side_effect(arguments):
    seen = []
    with pytest.raises(MCPError, match='参数不符合'):
        asyncio.run(MCPClient(transport=server(seen)).call(config(), 'notes.add', arguments))
    assert 'tools/call' not in [x[0] for x in seen]


def test_schema_change_denies_before_call():
    seen = []
    with pytest.raises(MCPError, match='已变化'):
        asyncio.run(MCPClient(transport=server(seen)).call(config(), 'notes.add', {'text': 'a'}, 'stale-hash'))
    assert 'tools/call' not in [x[0] for x in seen]


def test_final_guard_denies_after_network_wait():
    seen = []
    def guard():
        assert seen[-1][0] == 'tools/list'
        raise MCPError('权限已经撤回')
    with pytest.raises(MCPError, match='权限已经撤回'):
        asyncio.run(MCPClient(transport=server(seen)).call(config(), 'notes.add', {'text': 'a'}, before_call=guard))
    assert [x[0] for x in seen] == ['initialize', 'notifications/initialized', 'tools/list', 'DELETE']


def test_permission_revoked_during_handshake():
    cfg, seen = config(), []
    def callback(request, body):
        if body['method'] == 'tools/list':
            cfg['allowed_tools'] = []
    with pytest.raises(MCPError, match='撤回'):
        asyncio.run(MCPClient(transport=server(seen, callback=callback)).call(cfg, 'notes.add', {'text': 'a'}))
    assert 'tools/call' not in [x[0] for x in seen]


@pytest.mark.parametrize('failure', ['timeout', 'disconnect', 'http500', 'malformed', 'rpc_error'])
def test_call_outcome_unknown_never_retried_or_leaked(failure):
    seen = []
    def callback(request, body):
        if body['method'] != 'tools/call':
            return None
        if failure == 'timeout':
            raise httpx.ReadTimeout('fixture-credential secret response')
        if failure == 'disconnect':
            raise httpx.RemoteProtocolError('fixture-credential secret response')
        if failure == 'http500':
            return httpx.Response(500, text='fixture-credential secret response')
        if failure == 'malformed':
            return httpx.Response(200, text='fixture-credential secret response', headers={'Content-Type': 'application/json'})
        return httpx.Response(200, json={'jsonrpc': '2.0', 'id': body['id'], 'error': {'message': 'fixture-credential secret response'}})
    with pytest.raises(MCPError, match='结果未知') as error:
        asyncio.run(MCPClient(transport=server(seen, callback=callback)).call(config(), 'notes.add', {'text': 'a'}))
    assert len([x for x in seen if x[0] == 'tools/call']) == 1
    assert 'fixture-credential' not in str(error.value)
    assert 'secret response' not in str(error.value)


def test_jsonrpc_response_id_must_match():
    def callback(request, body):
        if body['method'] == 'tools/list':
            return httpx.Response(200, json={'jsonrpc': '2.0', 'id': 999, 'result': {'tools': [TOOL]}})
    with pytest.raises(MCPError, match='不匹配'):
        asyncio.run(MCPClient(transport=server([], callback=callback)).discover(config()))


@pytest.mark.parametrize('sse', [False, True])
def test_response_body_limit(sse):
    def callback(request, body):
        if body['method'] == 'tools/list':
            return httpx.Response(200, content=b'x' * (MAX_RESPONSE + 1),
                                  headers={'Content-Type': 'text/event-stream' if sse else 'application/json'})
    with pytest.raises(MCPError, match='1MB'):
        asyncio.run(MCPClient(transport=server([], callback=callback)).discover(config()))


def test_redirect_not_followed():
    seen = []
    def handle(request):
        seen.append(str(request.url))
        return httpx.Response(307, headers={'Location': 'https://evil.example/mcp'})
    with pytest.raises(MCPError):
        asyncio.run(MCPClient(transport=httpx.MockTransport(handle)).discover(config()))
    assert len(seen) == 1


@pytest.mark.parametrize('url', ['http://example.com/mcp', 'https://a:b@example.com/mcp',
    'https://example.com/mcp?token=x', 'https://example.com/mcp#x', 'https://10.0.0.1/mcp',
    'https://169.254.169.254/mcp', 'http://192.168.1.1/mcp', 'file:///x',
    'https://a.local/mcp', 'https://example.com:99999/mcp'])
def test_url_static_denials(url):
    with pytest.raises(MCPError):
        validate_mcp_url(url)


@pytest.mark.parametrize('url', ['http://localhost:1234/mcp', 'http://127.0.0.1:1234/mcp', 'http://[::1]:1234/mcp'])
def test_loopback_allowed_only_with_loopback_dns(url, monkeypatch):
    monkeypatch.setattr(socket, 'getaddrinfo', lambda *a, **kw: [(socket.AF_INET, socket.SOCK_STREAM, 6, '', ('127.0.0.1', 1234))])
    assert validate_mcp_url(url, resolve=True) == url


@pytest.mark.parametrize('addresses', [['10.0.0.1'], ['8.8.8.8', '192.168.1.5'], ['127.0.0.1']])
def test_private_or_mixed_dns_denied_before_connect(addresses, monkeypatch):
    monkeypatch.setattr(socket, 'getaddrinfo', lambda *a, **kw: [
        (socket.AF_INET, socket.SOCK_STREAM, 6, '', (address, 443)) for address in addresses])
    seen = []
    with pytest.raises(MCPError, match='网络范围'):
        asyncio.run(MCPClient(transport=server(seen)).discover(config()))
    assert seen == []


def test_pin_transport_retains_host_and_tls_identity():
    seen = []
    async def run():
        transport = _PinnedTransport('https://fixture.example:8443/mcp', '8.8.8.8')
        await transport.inner.aclose()
        def handle(request):
            seen.append(request)
            return httpx.Response(200, json={})
        transport.inner = httpx.MockTransport(handle)
        async with httpx.AsyncClient(transport=transport) as client:
            await client.post('https://fixture.example:8443/mcp', json={})
    asyncio.run(run())
    assert seen[0].url.host == '8.8.8.8'
    assert seen[0].headers['host'] == 'fixture.example:8443'
    assert seen[0].extensions['sni_hostname'] == 'fixture.example'


def test_cleanup_failure_cannot_change_success():
    seen = []
    inner = server(seen)
    async def handle(request):
        if request.method == 'DELETE':
            raise httpx.ConnectError('secret credential during cleanup')
        return await inner.handle_async_request(request)
    result = asyncio.run(MCPClient(transport=httpx.MockTransport(handle)).call(config(), 'notes.add', {'text': 'a'}))
    assert result['text'] == '已保存'


def test_complete_result_kept_when_cleanup_exceeds_operation_deadline():
    seen = []
    inner = server(seen)
    async def handle(request):
        if request.method == 'DELETE':
            await asyncio.sleep(0.08)
            return httpx.Response(204)
        return await inner.handle_async_request(request)
    result = asyncio.run(MCPClient(transport=httpx.MockTransport(handle), timeout=0.05).call(config(), 'notes.add', {'text': 'a'}))
    assert result['text'] == '已保存'


def test_tool_errors_fixed_and_success_token_redacted():
    bad = asyncio.run(MCPClient(transport=server([], result={'content': [{'type': 'text', 'text': 'fixture-credential secret body'}], 'isError': True})).call(config(), 'notes.add', {'text': 'a'}))
    assert bad['isError'] and 'secret body' not in bad['text'] and 'fixture-credential' not in str(bad)
    good = asyncio.run(MCPClient(transport=server([], result={'content': [{'type': 'text', 'text': 'token=fixture-credential'}]})).call(config(), 'notes.add', {'text': 'a'}))
    assert good['text'] == 'token=[凭据已隐藏]'


def test_structured_result_is_valid_json_not_string_truncated():
    payload = {'items': ['x' * 200] * 80, 'last': '保留完整结构'}
    result = asyncio.run(MCPClient(transport=server([], result={'content': [], 'structuredContent': payload})).call(config(), 'notes.add', {'text': 'a'}))
    assert json.loads(result['text']) == payload


def test_disabled_unallowed_and_oversized_arguments_make_no_requests():
    seen = []
    client = MCPClient(transport=server(seen))
    for cfg, args in ((config(enabled=False), {'text': 'a'}), (config(allowed_tools=[]), {'text': 'a'}), (config(), {'text': 'x' * 16001})):
        with pytest.raises(MCPError):
            asyncio.run(client.call(cfg, 'notes.add', args))
    assert not seen


@pytest.mark.parametrize('delimiter', [b'\n', b'\r\n', b'\r'])
@pytest.mark.parametrize('chunk_size', [1, 7])
def test_sse_chunked_utf8_bom_multiline_and_line_endings(delimiter, chunk_size):
    class Chunks(httpx.AsyncByteStream):
        def __init__(self, data):
            self.data = data
        async def __aiter__(self):
            for index in range(0, len(self.data), chunk_size):
                yield self.data[index:index + chunk_size]
    def callback(request, body):
        if body['method'] != 'tools/list':
            return None
        value = json.dumps({'jsonrpc': '2.0', 'id': body['id'], 'result': {'tools': [TOOL]}}, ensure_ascii=False, indent=2)
        lines = [b'\xef\xbb\xbf: comment', b'id: 1', b'data:', b'', *[b'data: ' + x.encode() for x in value.splitlines()], b'', b'']
        return httpx.Response(200, stream=Chunks(delimiter.join(lines)), headers={'Content-Type': 'text/event-stream'})
    result = asyncio.run(MCPClient(transport=server([], callback=callback)).discover(config()))
    assert result['tools'][0]['description'] == '保存一条笔记'


def test_sse_partial_disconnect_unknown_once():
    seen = []
    def callback(request, body):
        if body['method'] == 'tools/call':
            return httpx.Response(200, content=b'data: {"jsonrpc":"2.0"', headers={'Content-Type': 'text/event-stream'})
    with pytest.raises(MCPError, match='结果未知'):
        asyncio.run(MCPClient(transport=server(seen, callback=callback)).call(config(), 'notes.add', {'text': 'a'}))
    assert len([x for x in seen if x[0] == 'tools/call']) == 1


def test_sse_server_request_does_not_run_unadvertised_capabilities():
    def callback(request, body):
        if body['method'] == 'tools/list':
            value = {'jsonrpc': '2.0', 'id': 88, 'method': 'sampling/createMessage', 'params': {'messages': []}}
            return httpx.Response(200, content=('data: ' + json.dumps(value) + '\n\n').encode(), headers={'Content-Type': 'text/event-stream'})
    with pytest.raises(MCPError, match='未启用'):
        asyncio.run(MCPClient(transport=server([], callback=callback)).discover(config()))


def test_tool_catalog_does_not_expose_reflected_token():
    tool = {**TOOL, 'description': 'fixture-credential must never reach model context'}
    with pytest.raises(MCPError, match='敏感数据') as error:
        asyncio.run(MCPClient(transport=server([], tools=[tool])).discover(config()))
    assert 'fixture-credential' not in str(error.value)


def test_async_total_deadline_is_unknown_after_single_call():
    seen = []
    inner = server(seen)
    async def handle(request):
        if request.method != 'DELETE' and json.loads(request.content)['method'] == 'tools/call':
            seen.append(('tools/call',))
            await asyncio.sleep(0.5)
        return await inner.handle_async_request(request)
    with pytest.raises(MCPError, match='结果未知'):
        asyncio.run(MCPClient(transport=httpx.MockTransport(handle), timeout=0.05).call(config(), 'notes.add', {'text': 'a'}))
    assert len([x for x in seen if x[0] == 'tools/call']) == 1
