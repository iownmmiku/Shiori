"""Public text previews are temporary, bounded and cancelled with their lease."""
import asyncio
from contextlib import asynccontextmanager, contextmanager
import json
import socket
from pathlib import Path
import shutil
import subprocess

import httpx
import pytest
from fastapi.testclient import TestClient
from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse
import uvicorn

from auth_helpers import password_login
from shiori.api import create_app
from shiori.chat_preview import emit_preview, preview_events, preview_filter
from shiori.db import Store
from shiori.provider import Provider, ProviderError
from shiori.settings import Settings
from shiori.web_chat_stream import chat_stream


def frame(delta, finish=None):
    return ('data: ' + json.dumps({'choices': [{'index': 0, 'delta': delta, 'finish_reason': finish}]}, ensure_ascii=False) + '\n\n').encode()


class Chunks(httpx.AsyncByteStream):
    def __init__(self, parts): self.parts, self.closed = parts, False
    async def __aiter__(self):
        for part in self.parts:
            await asyncio.sleep(0)
            yield part
    async def aclose(self): self.closed = True


@pytest.fixture
def provider(tmp_path, monkeypatch):
    store, settings = Store(tmp_path), Settings(tmp_path)
    settings.update({'model': {'base_url': 'https://fixture.invalid/v1', 'model': 'fixture'}})
    original = httpx.AsyncClient
    def source(parts):
        stream = Chunks(parts)
        monkeypatch.setattr(httpx, 'AsyncClient', lambda **kw: original(transport=httpx.MockTransport(
            lambda req: httpx.Response(200, headers={'content-type': 'text/event-stream'}, stream=stream)), **kw))
        return stream
    yield Provider(settings, store), store, source
    store.close()


def test_content_only_preview_resets_on_tool_call_and_ignores_reasoning(provider):
    model, store, source = provider
    source([frame({'reasoning_content': 'private reasoning'}), frame({'content': '临时草稿'}),
            frame({'tool_calls': [{'index': 0, 'id': 'call-1', 'function': {'name': 'calculate', 'arguments': '{"expression":"secret argument"}'}}]}, 'tool_calls'),
            frame({'content': '工具之后的草稿'})])
    seen = []
    async def scenario():
        with preview_events(lambda kind, text: seen.append((kind, text))):
            return await model.complete([{'role': 'user', 'content': 'calculate'}], tools=[{
                'type': 'function', 'function': {'name': 'calculate', 'parameters': {'type': 'object'}}}])
    result = asyncio.run(scenario())
    assert result['tool_calls'] and store.list_usage()
    assert ('delta', '临时草稿') in seen
    assert seen[-1][0] == 'reset'
    assert 'private reasoning' not in str(seen) and 'secret argument' not in str(seen)
    assert '工具之后的草稿' not in str(seen)


def test_partial_preview_is_reset_and_never_committed_as_complete(provider):
    model, store, source = provider
    stream = source([frame({'content': 'partial preview'})])
    seen = []
    async def scenario():
        with preview_events(lambda kind, text: seen.append((kind, text))):
            with pytest.raises(ProviderError, match='中断'):
                await model.complete([{'role': 'user', 'content': 'hi'}])
    asyncio.run(scenario())
    assert ('delta', 'partial preview') in seen and seen[-1][0] == 'reset'
    assert stream.closed and store.list_usage()[0]['unconfirmed']


def test_non_chat_generation_never_enters_browser_preview(provider):
    model, _, source = provider
    source([frame({'content': 'planner internal answer'}, 'stop')])
    seen = []
    async def scenario():
        with preview_events(lambda *args: seen.append(args)):
            await model.complete([{'role': 'user', 'content': 'hi'}], purpose='planner')
    asyncio.run(scenario())
    assert not seen


def test_preview_guard_is_checked_before_each_visible_fragment(provider):
    model, _, source = provider
    source([frame({'content': 'allowed'}), frame({'content': 'revoked'}, 'stop')])
    seen, valid = [], True
    def preview(kind, text):
        nonlocal valid
        seen.append((kind, text))
        if text == 'allowed': valid = False
    async def scenario():
        with preview_events(preview):
            with pytest.raises(ProviderError):
                await model.complete([{'role': 'user', 'content': 'hi'}], before_send=lambda: valid)
    asyncio.run(scenario())
    assert ('delta', 'allowed') in seen and 'revoked' not in str(seen)
    assert seen[-1][0] == 'reset'


def test_fragmented_private_tags_and_known_memory_markers_never_flash_in_preview():
    seen = []
    with preview_events(lambda kind, text: seen.append((kind, text))):
        with preview_filter(['7ab675d0-extra-id']):
            for char in '<think>private reasoning</think>记得[7ab675d0]，你好【记忆：7ab675d0】。':
                emit_preview('delta', char)
    assert ''.join(text for kind, text in seen if kind == 'delta') == '记得，你好。'
    assert '7ab675d0' not in str(seen) and 'private reasoning' not in str(seen)
    seen.clear()
    with preview_events(lambda kind, text: seen.append((kind, text))):
        with preview_filter(['7ab675d0-extra-id'], user_text='解释字面量 [7ab675d0]'):
            emit_preview('delta', '[7ab675d0]')
            emit_preview('delta', '<think>unclosed')
            emit_preview('reset')
            emit_preview('delta', '安全的下一轮答复')
    assert ('delta', '[7ab675d0]') in seen and seen[-1] == ('delta', '安全的下一轮答复')


def test_stream_yields_before_completion_and_closing_waits_for_producer_cleanup():
    async def scenario():
        held, produced, cancelled, finished = [], asyncio.Event(), asyncio.Event(), asyncio.Event()
        class Request:
            async def receive(self): await asyncio.Event().wait()
        @contextmanager
        def lease():
            held.append('lease')
            try: yield
            finally: held.remove('lease')
        @asynccontextmanager
        async def lifecycle():
            held.append('admission')
            try: yield
            finally: held.remove('admission')
        async def run():
            try:
                assert held == ['admission', 'lease']
                emit_preview('delta', 'visible before completion')
                produced.set()
                await asyncio.Event().wait()
            finally:
                cancelled.set()
                await asyncio.sleep(.01)
                assert held == ['admission', 'lease']
                finished.set()
        response = chat_stream(Request(), run, lease=lease, lifecycle=lifecycle, authenticated=lambda: True)
        body = response.body_iterator
        assert 'start' in await anext(body)
        await asyncio.wait_for(produced.wait(), 1)
        assert 'visible before completion' in await anext(body)
        await body.aclose()
        assert cancelled.is_set() and finished.is_set() and not held
    asyncio.run(scenario())


def test_authenticated_api_stream_final_result_and_invalid_scope(tmp_path):
    app = create_app(tmp_path, start_workers=False)
    with TestClient(app) as client:
        assert client.post('/api/chat', json={'text': 'hello', 'stream': True}).status_code == 401
        password_login(client, app)
        assert client.post('/api/chat', json={'scope_id': 'qq:private:12345', 'text': 'hello', 'stream': True}).status_code == 400
        response = client.post('/api/chat', json={'text': '添加待办：stream fixture', 'stream': True})
        assert response.status_code == 200 and 'text/event-stream' in response.headers['content-type']
        events = [json.loads(line[6:]) for line in response.text.splitlines() if line.startswith('data: ')]
        assert events[0]['type'] == 'start' and events[-1]['type'] == 'result'
        assert events[-1]['data']['reply']
        assert any(message['role'] == 'assistant' for message in app.state.store.list_messages('web:default'))


def test_browser_preview_frontend():
    node = shutil.which('node')
    if not node: pytest.skip('Node.js unavailable')
    result = subprocess.run([node, str(Path(__file__).with_name('frontend_chat_stream.cjs'))], capture_output=True,
                            text=True, encoding='utf-8', timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr


def test_real_tcp_upstream_to_browser_preview_and_cancel(tmp_path, monkeypatch):
    """Actual sockets prove previews cross middleware before final completion."""
    # Keep this loopback fixture on actual loopback sockets even when the
    # developer uses an HTTP proxy for their real model services.
    monkeypatch.setenv('NO_PROXY', '127.0.0.1,localhost')
    monkeypatch.setenv('no_proxy', '127.0.0.1,localhost')
    async def scenario():
        upstream = FastAPI()
        finish, upstream_closed = asyncio.Event(), asyncio.Event()
        @upstream.post('/v1/chat/completions')
        async def completion(request: Request):
            async def generate():
                try:
                    yield frame({'content': 'TCP 增量预览'})
                    while not finish.is_set():
                        if await request.is_disconnected(): return
                        await asyncio.sleep(.01)
                    yield frame({'content': '，最终完成。'}, 'stop')
                    yield b'data: {"choices":[],"usage":{"prompt_tokens":4,"completion_tokens":8}}\n\ndata: [DONE]\n\n'
                finally:
                    upstream_closed.set()
            return StreamingResponse(generate(), media_type='text/event-stream')
        app = create_app(tmp_path, start_workers=False)
        sockets, servers, tasks = [], [], []
        def serve(application):
            listener = socket.socket()
            listener.bind(('127.0.0.1', 0))
            listener.listen()
            sockets.append(listener)
            server = uvicorn.Server(uvicorn.Config(application, log_level='error', timeout_graceful_shutdown=3))
            servers.append(server)
            tasks.append(asyncio.create_task(server.serve(sockets=[listener])))
            return listener.getsockname()[1]
        try:
            upstream_port = serve(upstream)
            app.state.settings.update({'model': {'base_url': f'http://127.0.0.1:{upstream_port}/v1', 'model': 'fixture', 'tools': False},
                                       'memory': {'semantic_enabled': False}})
            browser_port = serve(app)
            for _ in range(400):
                if all(server.started for server in servers): break
                await asyncio.sleep(.005)
            assert all(server.started for server in servers)
            async with httpx.AsyncClient(base_url=f'http://127.0.0.1:{browser_port}',
                                         headers={'Authorization': 'Bearer ' + app.state.settings.token}, timeout=5) as client:
                events = []
                async with client.stream('POST', '/api/chat', json={'text': '普通问候', 'stream': True}) as response:
                    assert response.status_code == 200
                    async for line in response.aiter_lines():
                        if not line.startswith('data: '): continue
                        event = json.loads(line[6:]); events.append(event)
                        if event['type'] == 'delta' and not finish.is_set():
                            assert event['text'] == 'TCP 增量预览'
                            finish.set()
                        if event['type'] == 'result': break
                assert events[-1]['type'] == 'result'
                assert events[-1]['data']['reply'] == 'TCP 增量预览，最终完成。'
                assert len(app.state.store.list_usage()) == 1
                finish.clear(); upstream_closed.clear()
                async with client.stream('POST', '/api/chat', json={'text': '再发一个普通问候', 'stream': True}) as response:
                    async for line in response.aiter_lines():
                        if line.startswith('data: ') and json.loads(line[6:]).get('type') == 'delta': break
                await asyncio.wait_for(upstream_closed.wait(), 3)
                for _ in range(400):
                    if not app.state.provider._reservations and not app.state.resource_limits()['requests']['active']: break
                    await asyncio.sleep(.005)
                usage = app.state.store.list_usage()
                assert not app.state.provider._reservations
                assert len(usage) == 2 and sum(bool(item.get('unconfirmed')) for item in usage) == 1
                assert app.state.resource_limits()['requests']['active'] == 0
                assert not app.state.attachments._active
                assert len([message for message in app.state.store.list_messages('web:default') if message['role'] == 'assistant']) == 1
        finally:
            finish.set()
            for server in servers: server.should_exit = True
            await asyncio.gather(*tasks)
            for listener in sockets: listener.close()
    asyncio.run(scenario())
