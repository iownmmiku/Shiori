"""Bounded OpenAI-compatible SSE assembly; partial tools are never executed."""
from __future__ import annotations

import asyncio
import codecs
import json
import sys
import time
from contextlib import asynccontextmanager

import anyio
import httpx

MAX_RESPONSE_BYTES = 4 * 1024 * 1024
MAX_LINE_BYTES = 256 * 1024
FILTER_USAGE_GRACE_SECONDS = 1.0
# Matches the local reconciliation ceiling, comfortably above supported model
# contexts while leaving ample headroom for SQLite's signed 64-bit daily sums.
MAX_USAGE_TOKENS = 100000000


def usage_token_count(value):
    if (isinstance(value, bool) or not isinstance(value, (int, float))
            or not 0 <= value <= MAX_USAGE_TOKENS or int(value) != value):
        raise ValueError('模型用量须为安全范围内的非负整数')
    return int(value)


def _complete_usage(usage):
    try:
        for key in ('prompt_tokens', 'completion_tokens'):
            usage_token_count(usage.get(key))
        return True
    except (ValueError, TypeError, AttributeError):
        return False


class ModelStreamError(ValueError):
    pass


class IncompleteModelStreamError(ModelStreamError):
    def __init__(self, *, done, finished, tool_calls, received_bytes):
        suffix = '，未执行工具' if tool_calls else '，未采用残缺回复'
        super().__init__('模型流在完整回复结束前中断' + suffix)
        self.diagnostics = {'done_received': done, 'finish_received': finished,
                            'has_tool_calls': tool_calls, 'received_bytes': received_bytes}


class _CompletedBodyStream(httpx.AsyncByteStream):
    """Keep EOF distinct from HTTPX's automatic response-close operation."""
    def __init__(self, stream):
        self.stream, self.complete = stream, False

    async def __aiter__(self):
        async for chunk in self.stream:
            yield chunk
        self.complete = True

    async def aclose(self):
        active_error = sys.exception()
        try:
            await self.stream.aclose()
        except Exception:
            # The HTTP body already arrived in full. Decoding and application
            # validation still decide whether it is a usable result.
            if not self.complete and active_error is None:
                raise


async def preserve_completed_body(response):
    """HTTPX response hook, before both eager .post() reads and .stream()."""
    if not isinstance(response.stream, _CompletedBodyStream):
        response.stream = _CompletedBodyStream(response.stream)


@asynccontextmanager
async def _response_stream(client, url, payload, headers, *, filtered):
    context = client.stream('POST', url, json=payload, headers=headers)
    response = await context.__aenter__()
    await preserve_completed_body(response)
    failed = False
    try:
        yield response
    except BaseException:
        failed = True
        raise
    finally:
        # Browser cancellation may inherit an AnyIO cancellation scope. Close
        # the accepted upstream socket before that scope aborts cleanup I/O.
        try:
            with anyio.CancelScope(shield=True):
                await context.__aexit__(None, None, None)
        except Exception:
            # A socket-close failure cannot replace an authority/cancellation
            # error or turn an already known policy stop into a retry.
            if not failed and not filtered():
                raise


async def receive_chat(client, url, payload, headers, *, first_token_seconds=90, check=None, on_event=None):
    try:
        async with asyncio.timeout(first_token_seconds) as first_watch:
            return await _receive_chat(client, url, payload, headers, first_token_seconds=first_token_seconds,
                                       check=check, first_watch=first_watch, on_event=on_event)
    except TimeoutError as exc:
        raise httpx.ReadTimeout('模型首个有效内容等待超时') from exc


async def _receive_chat(client, url, payload, headers, *, first_token_seconds, check, first_watch, on_event):
    started, first_token_ms = time.monotonic(), None
    content_filtered = False
    async with _response_stream(client, url, payload, headers, filtered=lambda: content_filtered) as response:
        if response.status_code != 200 or 'text/event-stream' not in response.headers.get('content-type', '').lower():
            chunks, size = [], 0
            async for chunk in response.aiter_bytes():
                size += len(chunk)
                if size > MAX_RESPONSE_BYTES:
                    raise ModelStreamError('模型响应超过安全接收上限')
                chunks.append(chunk)
            return httpx.Response(response.status_code, headers=response.headers, content=b''.join(chunks), request=response.request), None

        content, calls, usage = [], {}, {}
        finished, done, size, events, preview_blocked = False, False, 0, 0, False
        finish_reason = None
        content_filtered, filtered_at = False, None
        decoder, buffer, data_lines = codecs.getincrementaldecoder('utf-8-sig')('strict'), b'', []
        processed_bytes = 0

        def event():
            nonlocal finished, done, usage, first_token_ms, events, preview_blocked, finish_reason, content_filtered, filtered_at
            if not data_lines:
                return
            value = '\n'.join(data_lines)
            data_lines.clear()
            events += 1
            if events > 20000:
                raise ModelStreamError('模型流事件超过安全上限')
            if value.strip() == '[DONE]':
                done = True
                return
            callback_running = False
            try:
                item = json.loads(value)
                if not isinstance(item, dict):
                    raise ValueError()
                preview_text, saw_tools = [], False
                for choice in item.get('choices', []):
                    if choice.get('index', 0) != 0:
                        continue
                    if finished:
                        continue  # Choice completion is terminal; usage may follow.
                    delta = choice.get('delta') or {}
                    if choice.get('finish_reason') == 'content_filter' or isinstance(delta, dict) and delta.get('refusal'):
                        content_filtered = True
                        filtered_at = filtered_at or time.monotonic()
                        finished, finish_reason = True, 'content_filter'
                        content.clear()
                        calls.clear()
                        preview_text.clear()
                        preview_blocked = True
                        first_watch.reschedule(None)
                        if on_event:
                            callback_running = True
                            if check:
                                check()
                            on_event('reset', '')
                            callback_running = False
                        continue
                    if content_filtered:
                        continue  # A later stop/length cannot undo a policy stop.
                    if not isinstance(delta, dict):
                        raise ValueError()
                    text = delta.get('content')
                    meaningful = False
                    if text is not None:
                        if not isinstance(text, str):
                            raise ValueError()
                        content.append(text)
                        preview_text.append(text)
                        meaningful = bool(text)
                    for part in delta.get('tool_calls') or []:
                        saw_tools = True
                        index = part.get('index')
                        if type(index) is not int or not 0 <= index < 64:
                            raise ValueError()
                        target = calls.setdefault(index, {'id': '', 'type': 'function', 'function': {'name': '', 'arguments': ''}})
                        identifier = part.get('id')
                        if identifier:
                            if not isinstance(identifier, str):
                                raise ValueError()
                            if identifier != target['id']:
                                target['id'] += identifier
                        if part.get('type', 'function') != 'function':
                            raise ValueError()
                        function = part.get('function') or {}
                        for key in ('name', 'arguments'):
                            fragment = function.get(key, '')
                            if not isinstance(fragment, str):
                                raise ValueError()
                            target['function'][key] += fragment
                            meaningful = meaningful or bool(fragment)
                    if meaningful and first_token_ms is None:
                        first_token_ms = round((time.monotonic() - started) * 1000)
                        first_watch.reschedule(None)
                    if choice.get('finish_reason') is not None:
                        finished = True
                        finish_reason = choice['finish_reason']
                # Recognize a policy stop before optional usage/error fields:
                # a malformed trailer must not replace a known filter outcome.
                if item.get('error') and not content_filtered:
                    raise ValueError()
                if item.get('usage') is not None:
                    if not isinstance(item['usage'], dict):
                        raise ValueError()
                    if _complete_usage(item['usage']) or not _complete_usage(usage):
                        usage = item['usage']
                if on_event and not content_filtered:
                    callback_running = True
                    if check:
                        check()
                    if saw_tools and not preview_blocked:
                        preview_blocked = True
                        on_event('reset', '')
                    elif not preview_blocked and preview_text:
                        # Only content is public. reasoning_content and tool
                        # argument fragments never enter this channel.
                        on_event('delta', ''.join(preview_text))
                    callback_running = False
            except (ValueError, TypeError, KeyError, AttributeError) as exc:
                if callback_running:
                    raise  # A dispatch/authority guard is never an optional trailer.
                if content_filtered:
                    return  # Malformed optional trailers cannot revive this result.
                raise ModelStreamError('模型流返回无效事件，未使用不完整结果') from exc

        def line(value):
            if not value:
                event()
            elif value.startswith('data:'):
                data_lines.append(value[5:].lstrip(' '))

        iterator = response.aiter_bytes().__aiter__()
        try:
            while not done:
                if check:
                    check()
                remaining = first_token_seconds - (time.monotonic() - started)
                if content_filtered and (time.monotonic() - filtered_at >= FILTER_USAGE_GRACE_SECONDS
                                         or _complete_usage(usage)):
                    break
                if first_token_ms is None and not content_filtered and remaining <= 0:
                    raise httpx.ReadTimeout('模型首个有效内容等待超时', request=response.request)
                try:
                    if content_filtered:
                        chunk = await asyncio.wait_for(iterator.__anext__(), FILTER_USAGE_GRACE_SECONDS - (time.monotonic() - filtered_at))
                    else:
                        chunk = await asyncio.wait_for(iterator.__anext__(), remaining) if first_token_ms is None else await iterator.__anext__()
                except StopAsyncIteration:
                    break
                except TimeoutError as exc:
                    if content_filtered:
                        break
                    raise httpx.ReadTimeout('模型首个有效内容等待超时', request=response.request) from exc
                except httpx.HTTPError:
                    if content_filtered:
                        break
                    raise
                size += len(chunk)
                buffer += chunk
                while b'\n' in buffer and not done:
                    value, buffer = buffer.split(b'\n', 1)
                    processed_bytes += len(value) + 1
                    if processed_bytes > MAX_RESPONSE_BYTES:
                        raise ModelStreamError('模型响应超过安全接收上限')
                    if len(value) > MAX_LINE_BYTES:
                        raise ModelStreamError('模型流单行超过安全上限')
                    # Decode complete lines so bytes after [DONE], including
                    # invalid UTF-8, cannot change a completed result depending
                    # on the transport's chunk boundaries.
                    line(decoder.decode(value + b'\n')[:-1].rstrip('\r'))
                if not done and processed_bytes + len(buffer) > MAX_RESPONSE_BYTES:
                    raise ModelStreamError('模型响应超过安全接收上限')
                if not done and len(buffer) > MAX_LINE_BYTES:
                    raise ModelStreamError('模型流单行超过安全上限')
            if not done:
                final = decoder.decode(buffer, final=True)
                if final:
                    line(final.rstrip('\r'))
                event()
        except UnicodeError as exc:
            if not content_filtered:
                raise ModelStreamError('模型流包含无效编码') from exc
        if check:
            check()
        # Some relays omit finish_reason but send an explicit terminal sentinel.
        # Accept that only for text: tool arguments need the choice completion.
        if not (finished or done and not calls):
            raise IncompleteModelStreamError(done=done, finished=finished,
                                            tool_calls=bool(calls), received_bytes=size)
        for call in ([] if content_filtered else calls.values()):
            if not call['id'] or not call['function']['name']:
                raise ModelStreamError('模型工具调用缺少完整标识')
            try:
                arguments = json.loads(call['function']['arguments'])
                if not isinstance(arguments, dict):
                    raise ValueError()
            except ValueError as exc:
                raise ModelStreamError('模型工具参数不完整，未执行工具') from exc
        message = {'role': 'assistant', 'content': ''.join(content)}
        if calls:
            message['tool_calls'] = [calls[key] for key in sorted(calls)]
        result = {'choices': [{'message': message, 'finish_reason': finish_reason}], 'usage': usage}
        return httpx.Response(200, json=result, request=response.request), first_token_ms
