"""Terminal model outcomes and consumption must survive malformed transport tails."""
import asyncio
import json

import httpx
import pytest

from shiori.db import Store
from shiori.model_stream import receive_chat
from shiori.provider import (Provider, ProviderConnectionError, ProviderContentFilterError, ProviderError,
                              ProviderOutputBudgetError, ProviderStreamError)
from shiori.settings import PROVIDER_DEFAULTS, Settings


ACTUAL = {'prompt_tokens': 12, 'completion_tokens': 0}
DONE = b'data: [DONE]\n\n'
PRIVATE = 'PRIVATE_TERMINAL_TAIL https://private.invalid/?api_key=fixture-secret'


def event(delta=None, finish=None, usage=None):
    value = {'choices': [{'index': 0, 'delta': delta or {}, 'finish_reason': finish}]}
    if usage is not None:
        value['usage'] = usage
    return ('data: ' + json.dumps(value) + '\n\n').encode()


def usage_event(usage=ACTUAL):
    return ('data: ' + json.dumps({'choices': [], 'usage': usage}) + '\n\n').encode()


class Chunks(httpx.AsyncByteStream):
    def __init__(self, parts, close_error=None, read_error=None):
        self.parts, self.close_error, self.closed, self.read_error = parts, close_error, False, read_error

    async def __aiter__(self):
        for part in self.parts:
            await asyncio.sleep(0)
            yield part
        if self.read_error:
            raise self.read_error

    async def aclose(self):
        self.closed = True
        if self.close_error:
            raise self.close_error


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    store, settings = Store(tmp_path), Settings(tmp_path)
    settings.update({'model': {'base_url': 'https://fixture.invalid/v1', 'model': 'fixture',
        'timeout_fallback': True}, 'providers': [dict(PROVIDER_DEFAULTS, id='backup',
        model='backup', base_url='https://backup.invalid/v1')], 'routing': {'fallback': ['backup']}})
    provider, requests, streams = Provider(settings, store), [], []
    original = httpx.AsyncClient

    def source(*, parts=None, body=None, close_error=None, read_error=None,
               content_type='text/event-stream', fallback_body=None):
        def handler(request):
            requests.append(request)
            if fallback_body is not None and request.url.host == 'backup.invalid':
                return httpx.Response(200, json=fallback_body)
            assert request.url.host == 'fixture.invalid', 'terminal failure must not dispatch a fallback'
            if parts is None:
                return httpx.Response(200, json=body)
            stream = Chunks(parts, close_error, read_error)
            streams.append(stream)
            return httpx.Response(200, headers={'content-type': content_type}, stream=stream)
        monkeypatch.setattr(httpx, 'AsyncClient', lambda **kw: original(transport=httpx.MockTransport(handler), **kw))
    yield provider, store, source, requests, streams
    store.close()


def assert_single_receipt(runtime, *, estimated=False):
    provider, store, _, requests, streams = runtime
    assert len(requests) == 1 and not provider._reservations and provider._global_gate.active == 0
    records = store.list_usage()
    assert len(records) == 1
    assert bool(records[0].get('estimated')) is estimated
    assert bool(records[0].get('unconfirmed')) is estimated
    assert all(stream.closed for stream in streams)
    assert PRIVATE not in json.dumps(store.list_events())
    if not estimated:
        assert records[0]['input_tokens'] == 12 and records[0]['output_tokens'] == 0


@pytest.mark.parametrize('bad', [None, -1, True, .5, '0', 2**63, 10**400],
                         ids=['null', 'negative', 'bool', 'fraction', 'string', 'sql_overflow', 'float_overflow'])
def test_filtered_malformed_counters_do_not_skip_a_later_actual_trailer(runtime, bad):
    provider, store, source, _, _ = runtime
    source(parts=[event(finish='content_filter', usage={'prompt_tokens': 12, 'completion_tokens': bad}),
                  usage_event() + DONE])
    with pytest.raises(ProviderContentFilterError) as failure:
        asyncio.run(provider.complete([{'role': 'user', 'content': 'hello'}]))
    assert failure.value.usage['output_tokens'] == 0
    assert_single_receipt(runtime)
    assert len([item for item in store.list_events() if item['kind'] == 'model_content_filter']) == 1


def test_late_malformed_usage_cannot_replace_an_actual_filter_receipt(runtime):
    provider, _, source, _, _ = runtime
    source(parts=[event(finish='content_filter', usage=ACTUAL) + usage_event({}) + DONE])
    with pytest.raises(ProviderContentFilterError):
        asyncio.run(provider.complete([{'role': 'user', 'content': 'hello'}]))
    assert_single_receipt(runtime)


@pytest.mark.parametrize('filtered', [False, True])
@pytest.mark.parametrize('bad', [2**63, 10**400, 1e40], ids=['sql_overflow', 'float_overflow', 'huge_float'])
def test_oversized_usage_has_a_safe_error_and_one_conservative_ledger_record(runtime, filtered, bad):
    provider, _, source, _, _ = runtime
    source(body={'choices': [{'message': {'content': PRIVATE},
        'finish_reason': 'content_filter' if filtered else 'stop'}],
        'usage': {'prompt_tokens': bad, 'completion_tokens': 0}})
    with pytest.raises(ProviderContentFilterError if filtered else ProviderError) as failure:
        asyncio.run(provider.complete([{'role': 'user', 'content': 'hello'}]))
    assert PRIVATE not in str(failure.value)
    assert_single_receipt(runtime, estimated=True)


@pytest.mark.parametrize('split', [False, True])
@pytest.mark.parametrize('tail', [event({'content': PRIVATE}, 'stop'), b'\xff\xfe\n\n', b'x' * (5 * 1024 * 1024)],
                         ids=['frame', 'invalid_utf8', 'oversized_tail'])
def test_done_ignores_later_frames_bytes_and_tools_independent_of_chunk_boundaries(runtime, split, tail):
    provider, _, source, _, _ = runtime
    prefix = event({'content': 'complete reply'}, 'stop') + usage_event() + DONE
    source(parts=[prefix, tail] if split else [prefix + tail])
    result = asyncio.run(provider.complete([{'role': 'user', 'content': 'hello'}]))
    assert result['content'] == 'complete reply' and not result['tool_calls']
    assert_single_receipt(runtime)


def test_done_cannot_be_followed_by_a_tool_completion_in_the_same_chunk(runtime):
    provider, _, source, _, _ = runtime
    first = event({'tool_calls': [{'index': 0, 'id': 'c1', 'function': {'name': 'calculate', 'arguments': '{'}}]})
    tail = event({'tool_calls': [{'index': 0, 'function': {'arguments': '}'}}]}, 'tool_calls')
    source(parts=[first + DONE + tail])
    with pytest.raises(ProviderStreamError):
        asyncio.run(provider.complete([{'role': 'user', 'content': 'hello'}], purpose='computer',
            tools=[{'type': 'function', 'function': {'name': 'calculate'}}]))
    assert_single_receipt(runtime, estimated=True)


@pytest.mark.parametrize('late_finish', ['stop', 'tool_calls'])
def test_length_is_a_sticky_terminal_outcome_and_keeps_its_actual_usage(runtime, late_finish):
    provider, _, source, _, _ = runtime
    source(parts=[event({'content': 'truncated'}, 'length') + event({'content': PRIVATE}, late_finish)
                  + usage_event() + DONE])
    with pytest.raises(ProviderOutputBudgetError):
        asyncio.run(provider.complete([{'role': 'user', 'content': 'hello'}], purpose='computer'))
    assert_single_receipt(runtime)


@pytest.mark.parametrize('close_error', [httpx.ReadTimeout(PRIVATE), httpx.ReadError(PRIVATE), OSError(PRIVATE), ValueError(PRIVATE)])
def test_filter_and_actual_usage_survive_socket_cleanup_failures_without_fallback(runtime, close_error):
    provider, store, source, _, _ = runtime
    source(parts=[event(finish='content_filter') + usage_event() + DONE], close_error=close_error)
    with pytest.raises(ProviderContentFilterError) as failure:
        asyncio.run(provider.complete([{'role': 'user', 'content': 'hello'}]))
    assert PRIVATE not in str(failure.value)
    assert_single_receipt(runtime)
    assert len([item for item in store.list_events() if item['kind'] == 'model_content_filter']) == 1


@pytest.mark.parametrize('filtered', [False, True])
@pytest.mark.parametrize('mode', ['off', 'on'])
def test_json_response_is_preserved_after_a_complete_body_close_failure(runtime, filtered, mode):
    provider, _, source, _, _ = runtime
    provider.settings.update({'model': {'stream_mode': mode}})
    body = {'choices': [{'message': {'content': PRIVATE if filtered else 'complete JSON reply'},
        'finish_reason': 'content_filter' if filtered else 'stop'}], 'usage': ACTUAL}
    source(parts=[json.dumps(body).encode()], content_type='application/json', close_error=httpx.ReadTimeout(PRIVATE))
    if filtered:
        with pytest.raises(ProviderContentFilterError):
            asyncio.run(provider.complete([{'role': 'user', 'content': 'hello'}]))
    else:
        result = asyncio.run(provider.complete([{'role': 'user', 'content': 'hello'}]))
        assert result['content'] == 'complete JSON reply'
    assert_single_receipt(runtime)


@pytest.mark.parametrize('mode', ['off', 'on'])
@pytest.mark.parametrize('fallback', [False, True], ids=['fallback_off', 'fallback_on'])
@pytest.mark.parametrize('continuation', [False, True], ids=['initial', 'tool_continuation'])
def test_json_partial_read_error_preserves_cause_and_obeys_fallback_after_close_error(runtime, mode, fallback, continuation):
    provider, store, source, requests, streams = runtime
    provider.settings.update({'model': {'stream_mode': mode, 'timeout_fallback': fallback}})
    read_failure = httpx.ReadError(PRIVATE)
    may_fallback = fallback and not continuation
    backup = {'choices': [{'message': {'content': 'complete backup reply'}, 'finish_reason': 'stop'}],
              'usage': {'prompt_tokens': 8, 'completion_tokens': 5}}
    source(parts=[b'{"choices":'], content_type='application/json',
        read_error=read_failure, close_error=httpx.ReadTimeout(PRIVATE),
        fallback_body=backup if may_fallback else None)
    messages = [{'role': 'user', 'content': 'hello'}]
    if continuation:
        messages.append({'role': 'tool', 'tool_call_id': 'c1', 'content': 'already executed'})
    if may_fallback:
        result = asyncio.run(provider.complete(messages))
        assert result['content'] == 'complete backup reply'
        assert [request.url.host for request in requests] == ['fixture.invalid', 'backup.invalid']
        records = store.list_usage()
        assert len(records) == 2
        primary = next(record for record in records if record['model'] == 'fixture')
        actual = next(record for record in records if record['model'] == 'backup')
        assert primary['estimated'] and primary['unconfirmed']
        assert not actual.get('estimated') and not actual.get('unconfirmed')
        assert actual['input_tokens'] == 8 and actual['output_tokens'] == 5
        assert not provider._reservations and provider._global_gate.active == 0
        assert all(stream.closed for stream in streams)
        assert PRIVATE not in json.dumps(store.list_events())
    else:
        with pytest.raises(ProviderConnectionError) as failure:
            asyncio.run(provider.complete(messages))
        assert failure.value.__cause__ is read_failure, 'cleanup timeout must not replace the original read error'
        assert not failure.value.retryable and PRIVATE not in str(failure.value)
        assert_single_receipt(runtime, estimated=True)


def test_json_read_failure_is_not_mistaken_for_eof_or_replaced_by_close_failure():
    failure = httpx.ReadError('fixture primary read failure')

    class Interrupted(Chunks):
        async def __aiter__(self):
            yield b'{"choices":'
            raise failure

    stream = Interrupted([], httpx.ReadTimeout(PRIVATE))
    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request:
                httpx.Response(200, headers={'content-type': 'application/json'}, stream=stream))) as client:
            with pytest.raises(httpx.ReadError) as caught:
                await receive_chat(client, 'https://fixture.invalid/v1', {}, {})
            assert caught.value is failure
    asyncio.run(scenario())
    assert stream.closed


@pytest.mark.parametrize('source', ['check', 'on_event'])
def test_cleanup_failure_does_not_replace_a_filter_authority_callback_error(source):
    failure = ValueError('authority revoked')
    stream = Chunks([event(finish='content_filter') + usage_event() + DONE], httpx.ReadTimeout(PRIVATE))
    delivered = False

    def check():
        if delivered and source == 'check':
            raise failure

    def preview(kind, _):
        nonlocal delivered
        if kind == 'reset':
            delivered = True
            if source == 'on_event':
                raise failure

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request:
                httpx.Response(200, headers={'content-type': 'text/event-stream'}, stream=stream))) as client:
            with pytest.raises(ValueError) as caught:
                await receive_chat(client, 'https://fixture.invalid/v1', {}, {}, check=check, on_event=preview)
            assert caught.value is failure
    asyncio.run(scenario())
    assert stream.closed


def test_cleanup_failure_cannot_replace_cancellation_after_a_filter_marker():
    received = asyncio.Event()

    class Stalled(Chunks):
        async def __aiter__(self):
            yield event(finish='content_filter')
            await asyncio.Event().wait()

    stream = Stalled([], httpx.ReadTimeout(PRIVATE))
    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request:
                httpx.Response(200, headers={'content-type': 'text/event-stream'}, stream=stream))) as client:
            task = asyncio.create_task(receive_chat(client, 'https://fixture.invalid/v1', {}, {},
                on_event=lambda *_: received.set()))
            await asyncio.wait_for(received.wait(), 1)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
    asyncio.run(scenario())
    assert stream.closed


def test_failed_ledger_write_keeps_the_reservation_until_a_successful_retry(runtime, monkeypatch):
    provider, store, _, _, _ = runtime
    async def scenario():
        key, _ = await provider._reserve(provider.resolve(), 'web:default', 'chat', 12, 16)
        record = {'input_tokens': 12, 'output_tokens': 0}
        with monkeypatch.context() as patch:
            patch.setattr(store, 'record_usage', lambda _: (_ for _ in ()).throw(OSError('fixture write failure')))
            with pytest.raises(OSError):
                await provider._settle(key, record)
        assert key in provider._reservations and store.list_usage() == []
        await provider._settle(key, record)
        await provider._settle(key, record)
        assert not provider._reservations and len(store.list_usage()) == 1
        assert store.list_usage()[0]['request_id']
    asyncio.run(scenario())
