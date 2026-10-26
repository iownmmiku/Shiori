"""Content-filtered completions are terminal failures, never public replies."""
import asyncio
import json
from unittest.mock import AsyncMock

import httpx
import pytest

from mikubot.chat import ChatService
from mikubot.chat_preview import preview_events
from mikubot.db import Store
from mikubot import model_stream
from mikubot.model_stream import receive_chat
from mikubot.onebot import OneBotBridge
from mikubot.provider import Provider, ProviderContentFilterError, ProviderError, is_budget_exceeded
from mikubot.settings import PROVIDER_DEFAULTS, Settings
from mikubot.social import _failure_feedback


RAW = 'PRIVATE_UPSTREAM_FILTER_BODY https://private.invalid/v1?api_key=private-key Traceback'
USAGE = {'prompt_tokens': 12, 'completion_tokens': 0}
TOOLS = [{'type': 'function', 'function': {'name': 'calculate', 'parameters': {'type': 'object'}}}]


class Chunks(httpx.AsyncByteStream):
    def __init__(self, parts):
        self.parts, self.closed = parts, False

    async def __aiter__(self):
        for part in self.parts:
            await asyncio.sleep(0)
            yield part

    async def aclose(self):
        self.closed = True


def frame(delta, finish=None):
    return ('data: ' + json.dumps({'choices': [{'index': 0, 'delta': delta,
        'finish_reason': finish}]}, ensure_ascii=False) + '\n\n').encode()


def trailer():
    return ('data: ' + json.dumps({'choices': [], 'usage': USAGE}) + '\n\ndata: [DONE]\n\n').encode()


@pytest.fixture
def setup_provider(tmp_path, monkeypatch):
    store, settings = Store(tmp_path), Settings(tmp_path)
    settings.update({'model': {'base_url': 'https://fixture.invalid/v1', 'model': 'fixture',
        'timeout_fallback': True}, 'providers': [dict(PROVIDER_DEFAULTS, id='backup',
        name='备用模型', model='backup', base_url='https://backup.invalid/v1')],
        'routing': {'fallback': ['backup']}})
    original, requests, streams = httpx.AsyncClient, [], []

    def source(body, *, streaming=False):
        def handler(request):
            requests.append(request)
            assert request.url.host == 'fixture.invalid', 'a filtered generation must not use a backup model'
            if streaming:
                stream = body if isinstance(body, httpx.AsyncByteStream) else Chunks(body)
                streams.append(stream)
                return httpx.Response(200, headers={'content-type': 'text/event-stream'}, stream=stream)
            return httpx.Response(200, json=body)
        monkeypatch.setattr(httpx, 'AsyncClient', lambda **kwargs:
            original(transport=httpx.MockTransport(handler), **kwargs))

    yield Provider(settings, store), settings, store, source, requests, streams
    store.close()


def assert_filtered(model, store, requests, error):
    assert isinstance(error, ProviderContentFilterError)
    assert not error.retryable and not is_budget_exceeded(error)
    assert RAW not in str(error) and 'private.invalid' not in str(error)
    assert len(requests) == 1 and not model._reservations and model._global_gate.active == 0
    usage = store.list_usage()
    assert len(usage) == 1 and usage[0]['input_tokens'] == 12 and usage[0]['output_tokens'] == 0
    assert not usage[0].get('unconfirmed') and not usage[0].get('estimated')
    assert error.usage['input_tokens'] == 12 and error.usage['output_tokens'] == 0
    events = store.list_events()
    assert len([event for event in events if event['kind'] == 'model_content_filter']) == 1
    assert RAW not in json.dumps(events, ensure_ascii=False)
    assert not any(event['kind'] == 'model_stream_incomplete' for event in events)


@pytest.mark.parametrize('message', [
    {'content': RAW},
    {},
    None,
    {'content': RAW, 'tool_calls': [{'function': {'name': 'calculate', 'arguments': '{'}}]},
])
def test_json_content_filter_precedes_body_and_tool_validation(setup_provider, message):
    model, _, store, source, requests, _ = setup_provider
    source({'choices': [{'message': message, 'finish_reason': 'content_filter'}], 'usage': USAGE})
    seen = []
    async def scenario():
        with preview_events(lambda kind, text: seen.append((kind, text))):
            with pytest.raises(ProviderContentFilterError) as captured:
                await model.complete([{'role': 'user', 'content': 'hello'}])
            return captured.value
    error = asyncio.run(scenario())
    assert_filtered(model, store, requests, error)
    assert all(kind != 'delta' for kind, _ in seen)


def test_json_content_filter_does_not_require_a_message(setup_provider):
    model, _, store, source, requests, _ = setup_provider
    source({'choices': [{'finish_reason': 'content_filter'}], 'usage': USAGE})
    with pytest.raises(ProviderContentFilterError) as captured:
        asyncio.run(model.complete([{'role': 'user', 'content': 'hello'}]))
    assert_filtered(model, store, requests, captured.value)


def test_json_explicit_refusal_is_not_an_assistant_reply(setup_provider):
    model, _, store, source, requests, _ = setup_provider
    source({'choices': [{'message': {'content': RAW, 'refusal': RAW}, 'finish_reason': 'stop'}], 'usage': USAGE})
    with pytest.raises(ProviderContentFilterError) as captured:
        asyncio.run(model.complete([{'role': 'user', 'content': 'hello'}]))
    assert_filtered(model, store, requests, captured.value)


@pytest.mark.parametrize('usage', [
    {},
    {'prompt_tokens': None, 'completion_tokens': None},
    {'prompt_tokens': 12, 'completion_tokens': None},
    {'prompt_tokens': 12, 'completion_tokens': -1},
])
def test_filter_with_untrustworthy_usage_keeps_one_unconfirmed_estimate(setup_provider, usage):
    model, _, store, source, requests, _ = setup_provider
    source({'choices': [{'message': {'content': RAW}, 'finish_reason': 'content_filter'}], 'usage': usage})
    with pytest.raises(ProviderContentFilterError) as captured:
        asyncio.run(model.complete([{'role': 'user', 'content': 'hello'}]))
    assert not captured.value.retryable and len(requests) == 1 and not model._reservations
    records = store.list_usage()
    assert len(records) == 1 and records[0]['estimated'] and records[0]['unconfirmed']
    assert records[0]['input_tokens'] > 0
    assert records[0]['output_tokens'] == json.loads(requests[0].content)['max_tokens']
    assert captured.value.usage['estimated'] and captured.value.usage['unconfirmed']
    assert RAW not in str(captured.value) and RAW not in json.dumps(store.list_events())


@pytest.mark.parametrize('failure', [httpx.ReadError, httpx.ReadTimeout, None])
def test_filtered_stream_stall_or_disconnect_cannot_trigger_model_fallback(setup_provider, monkeypatch, failure):
    model, _, store, source, requests, streams = setup_provider
    monkeypatch.setattr(model_stream, 'FILTER_USAGE_GRACE_SECONDS', .03)
    class UnfinishedTrailer(Chunks):
        async def __aiter__(self):
            yield frame({}, 'content_filter')
            if failure:
                raise failure(RAW)
            await asyncio.sleep(1)
            yield trailer()
    source(UnfinishedTrailer([]), streaming=True)
    with pytest.raises(ProviderContentFilterError) as captured:
        asyncio.run(model.complete([{'role': 'user', 'content': 'hello'}]))
    assert not captured.value.retryable and len(requests) == 1 and streams[0].closed
    records = store.list_usage()
    assert len(records) == 1 and records[0]['estimated'] and records[0]['unconfirmed']
    assert records[0]['output_tokens'] == json.loads(requests[0].content)['max_tokens']
    assert not model._reservations and model._global_gate.active == 0
    assert RAW not in str(captured.value) and RAW not in json.dumps(store.list_events())


def test_json_http_200_error_envelope_cannot_become_a_public_reply(setup_provider):
    model, _, store, source, requests, _ = setup_provider
    source({'error': {'message': RAW}, 'choices': [{'message': {'content': RAW},
        'finish_reason': 'stop'}], 'usage': USAGE})
    seen = []
    async def scenario():
        with preview_events(lambda kind, text: seen.append((kind, text))):
            with pytest.raises(ProviderError) as captured:
                await model.complete([{'role': 'user', 'content': 'hello'}])
            return captured.value
    error = asyncio.run(scenario())
    assert not isinstance(error, ProviderContentFilterError) and not error.retryable
    assert len(requests) == 1 and not model._reservations
    assert RAW not in str(error) and all(kind != 'delta' for kind, _ in seen)
    assert RAW not in json.dumps(store.list_events())


@pytest.mark.parametrize('extra', [
    {'usage': 'invalid'},
    {'error': {'message': RAW}},
    {'usage': 'invalid', 'error': {'message': RAW}},
])
def test_sse_filter_marker_precedes_usage_or_error_envelope_validation(setup_provider, extra):
    model, _, store, source, requests, streams = setup_provider
    terminal = {'choices': [{'index': 0, 'delta': {'content': RAW}, 'finish_reason': 'content_filter'}], **extra}
    source([('data: ' + json.dumps(terminal) + '\n\n').encode(), trailer()], streaming=True)
    seen = []
    async def scenario():
        with preview_events(lambda kind, text: seen.append((kind, text))):
            with pytest.raises(ProviderContentFilterError) as captured:
                await model.complete([{'role': 'user', 'content': 'hello'}])
            return captured.value
    assert_filtered(model, store, requests, asyncio.run(scenario()))
    assert all(kind != 'delta' for kind, _ in seen) and streams[0].closed


def test_sse_filter_error_envelope_retains_usage_from_the_same_terminal_frame(setup_provider):
    model, _, store, source, requests, _ = setup_provider
    terminal = {'choices': [{'index': 0, 'delta': {'content': RAW}, 'finish_reason': 'content_filter'}],
        'error': {'message': RAW}, 'usage': USAGE}
    source([('data: ' + json.dumps(terminal) + '\n\ndata: [DONE]\n\n').encode()], streaming=True)
    with pytest.raises(ProviderContentFilterError) as captured:
        asyncio.run(model.complete([{'role': 'user', 'content': 'hello'}]))
    assert_filtered(model, store, requests, captured.value)


@pytest.mark.parametrize('source', ['check', 'on_event'])
def test_filter_callback_preserves_the_original_authority_error(source):
    failure = ValueError('request authority was revoked')
    delivered, failed = False, False
    seen = []
    class FilterFrame(Chunks):
        async def __aiter__(self):
            nonlocal delivered
            delivered = True
            yield frame({}, 'content_filter') + trailer()
    stream = FilterFrame([])
    def check():
        nonlocal failed
        if source == 'check' and delivered and not failed:
            failed = True
            raise failure
    def on_event(kind, text):
        nonlocal failed
        if source == 'on_event' and kind == 'reset' and not failed:
            failed = True
            raise failure
        seen.append((kind, text))
    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request:
            httpx.Response(200, headers={'content-type': 'text/event-stream'}, stream=stream))) as client:
            with pytest.raises(ValueError) as captured:
                await receive_chat(client, 'https://fixture.invalid/v1', {}, {},
                    check=check, on_event=on_event)
            assert captured.value is failure
    asyncio.run(scenario())
    assert failed and stream.closed and not seen


@pytest.mark.parametrize('terminal_delta', [
    {'content': RAW},
    {'content': RAW, 'tool_calls': [{'index': 0, 'function': {'arguments': '{'}}]},
    {'content': {'invalid': RAW}, 'tool_calls': 'invalid'},
    None,
])
def test_sse_filtered_terminal_never_previews_its_body(setup_provider, terminal_delta):
    model, _, store, source, requests, streams = setup_provider
    source([frame({'content': 'earlier provisional draft'}),
        frame(terminal_delta, 'content_filter'), trailer()], streaming=True)
    seen = []
    async def scenario():
        with preview_events(lambda kind, text: seen.append((kind, text))):
            with pytest.raises(ProviderContentFilterError) as captured:
                await model.complete([{'role': 'user', 'content': 'hello'}])
            return captured.value
    error = asyncio.run(scenario())
    assert_filtered(model, store, requests, error)
    assert ('delta', 'earlier provisional draft') in seen and seen[-1] == ('reset', '')
    assert not any('PRIVATE_UPSTREAM_FILTER_BODY' in text for _, text in seen)
    assert streams[0].closed


@pytest.mark.parametrize('late_finish', ['stop', 'length'])
def test_sse_filter_is_sticky_and_discards_incomplete_tools(setup_provider, late_finish):
    model, _, store, source, requests, _ = setup_provider
    source([frame({'tool_calls': [{'index': 0, 'id': 'c1', 'function': {
        'name': 'calculate', 'arguments': '{'}}]}), frame({}, 'content_filter'),
        frame({'content': RAW}, late_finish), trailer()], streaming=True)
    with pytest.raises(ProviderContentFilterError) as captured:
        asyncio.run(model.complete([{'role': 'user', 'content': 'hello'}], tools=TOOLS))
    assert_filtered(model, store, requests, captured.value)


def test_sse_explicit_refusal_is_filtered_before_content_preview(setup_provider):
    model, _, store, source, requests, _ = setup_provider
    source([frame({'content': RAW, 'refusal': RAW}, 'stop'), trailer()], streaming=True)
    seen = []
    async def scenario():
        with preview_events(lambda kind, text: seen.append((kind, text))):
            with pytest.raises(ProviderContentFilterError) as captured:
                await model.complete([{'role': 'user', 'content': 'hello'}])
            return captured.value
    assert_filtered(model, store, requests, asyncio.run(scenario()))
    assert all(kind != 'delta' for kind, _ in seen)


def test_filter_finish_without_done_still_preserves_actual_usage(setup_provider):
    model, _, store, source, requests, _ = setup_provider
    terminal = {'choices': [{'index': 0, 'delta': {}, 'finish_reason': 'content_filter'}], 'usage': USAGE}
    source([('data: ' + json.dumps(terminal) + '\n\n').encode()], streaming=True)
    with pytest.raises(ProviderContentFilterError) as captured:
        asyncio.run(model.complete([{'role': 'user', 'content': 'hello'}]))
    assert_filtered(model, store, requests, captured.value)


def test_filter_only_finish_stops_first_content_deadline_while_waiting_for_usage():
    class DelayedTrailer(Chunks):
        async def __aiter__(self):
            yield frame({}, 'content_filter')
            await asyncio.sleep(.07)
            yield trailer()
    stream = DelayedTrailer([])
    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request:
            httpx.Response(200, headers={'content-type': 'text/event-stream'}, stream=stream))) as client:
            response, ttft = await receive_chat(client, 'https://fixture.invalid/v1', {}, {}, first_token_seconds=.03)
            assert response.json()['choices'][0]['finish_reason'] == 'content_filter'
            assert response.json()['usage'] == USAGE and ttft is None
    asyncio.run(scenario())
    assert stream.closed


@pytest.mark.parametrize('planner', [False, True])
def test_content_filter_feedback_does_not_relay_upstream_text(planner):
    error = ProviderContentFilterError()
    feedback = _failure_feedback(error, planner=planner)
    assert any(word in feedback for word in ('过滤', '安全', '拦截', '拒绝'))
    assert not is_budget_exceeded(error) and '预算不足' not in feedback
    assert 'private.invalid' not in feedback and 'Traceback' not in feedback


def test_chat_does_not_execute_filtered_tools_or_save_model_body(setup_provider, monkeypatch):
    model, settings, store, source, requests, _ = setup_provider
    source({'choices': [{'message': {'content': RAW, 'tool_calls': [{'id': 'c1', 'type': 'function',
        'function': {'name': 'calculate', 'arguments': '{"expression":"1+1"}'}}]},
        'finish_reason': 'content_filter'}], 'usage': USAGE})
    chat = ChatService(store, settings, model)
    tool = AsyncMock()
    monkeypatch.setattr(chat, '_tool', tool)
    async def scenario():
        try:
            with pytest.raises(ProviderContentFilterError) as captured:
                await chat.respond('web:default', 'owner', '你好，今天聊点什么？')
            return captured.value
        finally:
            await chat.close()
    error = asyncio.run(scenario())
    assert_filtered(model, store, requests, error)
    tool.assert_not_awaited()
    assert not any(item['role'] == 'assistant' for item in store.list_messages('web:default'))
    assert RAW not in json.dumps(store.list_messages('web:default'), ensure_ascii=False)


def test_qq_sends_one_safe_filter_feedback_without_tools_or_raw_model_body(setup_provider, monkeypatch):
    model, settings, store, source, requests, _ = setup_provider
    settings.update({'output': {'interval_ms': 0}})
    # This in-memory connector fixture has no real token or persistence need.
    settings._data['onebot'].update(enabled=True, access_token='isolated-fixture-token',
        owner_ids=['101'], allowed_groups=['202'], chat_policy='whitelist')
    store.upsert_scope({'id': 'qq:group:202', 'plan_enabled': False,
        'merge_window_ms': 0, 'cooldown_seconds': 0})
    source({'choices': [{'message': {'content': RAW, 'tool_calls': [{'id': 'c1',
        'function': {'name': 'calculate', 'arguments': '{'}}]},
        'finish_reason': 'content_filter'}], 'usage': USAGE})
    chat = ChatService(store, settings, model)
    tool = AsyncMock()
    monkeypatch.setattr(chat, '_tool', tool)
    bridge = OneBotBridge(store, settings, chat)
    bridge.send = AsyncMock(return_value=True)
    async def scenario():
        try:
            await bridge._flow.handle('qq:group:202', '303', '初音未来，你好', [], {})
        finally:
            await bridge._flow.close()
            await chat.close()
    asyncio.run(scenario())
    assert len(requests) == 1 and bridge.send.await_count == 1
    tool.assert_not_awaited()
    sent = bridge.send.call_args.args[1]
    assert RAW not in sent and 'private.invalid' not in sent and 'private-key' not in sent
    assert any(word in sent for word in ('过滤', '安全', '拦截', '拒绝'))
    assert bridge.send.call_args.kwargs['metadata']['failure_feedback']
    assistant = [item for item in store.list_messages('qq:group:202') if item['role'] == 'assistant']
    assert len(assistant) == 1 and assistant[0]['metadata']['failure_feedback']
    assert assistant[0]['content'] == sent and RAW not in json.dumps(assistant, ensure_ascii=False)
    assert RAW not in json.dumps(store.list_events(), ensure_ascii=False)
    assert len(store.list_usage()) == 1 and store.list_usage()[0]['input_tokens'] == 12
