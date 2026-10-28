"""Bounded provisional browser previews with one authoritative final result."""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging

import anyio
from fastapi import HTTPException
from fastapi.responses import StreamingResponse

from .chat_preview import preview_events
from .provider import ProviderError


MAX_PREVIEW_CHARS = 120000


def chat_stream(request, run, *, lease, lifecycle, authenticated):
    async def events():
        wake = asyncio.Event()
        pending = []
        length = 0
        disabled = False

        def preview(kind, text):
            nonlocal length, disabled
            if kind == 'reset':
                pending.clear()
                pending.append({'type': 'reset'})
                length, disabled = 0, False
            elif kind == 'delta' and text and not disabled:
                length += len(text)
                if length > MAX_PREVIEW_CHARS:
                    pending.clear()
                    pending.append({'type': 'reset'})
                    disabled = True
                elif pending and pending[-1]['type'] == 'delta':
                    pending[-1]['text'] += text
                else:
                    pending.append({'type': 'delta', 'text': text})
            wake.set()

        def encode(event):
            return 'data: ' + json.dumps(event, ensure_ascii=False) + '\n\n'

        task, disconnected = None, None
        async def watch_disconnect():
            # Request.is_disconnected() uses an already-cancelled probe. With
            # nested BaseHTTPMiddleware that probe can be cancelled before it
            # reaches the server's receive queue and miss a closed browser.
            # A dedicated blocking receive also wakes an otherwise idle model
            # stream immediately when the socket closes.
            while True:
                if (await request.receive())['type'] == 'http.disconnect':
                    return
        try:
            # Middleware returns once headers are available. These contexts
            # deliberately live inside the body iterator until the producer
            # has actually stopped, including cancellation and lease cleanup.
            async with lifecycle():
                with lease(), preview_events(preview):
                    if not authenticated():
                        yield encode({'type': 'error', 'code': 'authentication_required', 'message': '登录已失效，请重新登录。'})
                        return
                    task = asyncio.create_task(run())
                    task.add_done_callback(lambda _: wake.set())
                    disconnected = asyncio.create_task(watch_disconnect())
                    disconnected.add_done_callback(lambda _: wake.set())
                    try:
                        yield encode({'type': 'start'})
                        while True:
                            if not authenticated():
                                pending.clear()
                                yield encode({'type': 'reset'})
                                yield encode({'type': 'error', 'code': 'authentication_required', 'message': '登录已失效，请重新登录。'})
                                return
                            if disconnected.done():
                                return
                            try:
                                await asyncio.wait_for(wake.wait(), .4)
                            except TimeoutError:
                                yield ': heartbeat\n\n'
                                continue
                            wake.clear()
                            # Coalesce token bursts and keep a single bounded
                            # pending text buffer instead of one queue entry
                            # (and browser repaint) per token.
                            if not task.done():
                                await asyncio.sleep(.025)
                            if not authenticated():
                                pending.clear()
                                yield encode({'type': 'reset'})
                                yield encode({'type': 'error', 'code': 'authentication_required', 'message': '登录已失效，请重新登录。'})
                                return
                            batch, pending[:] = list(pending), []
                            for event in batch:
                                yield encode(event)
                            if task.done():
                                result = await task
                                yield encode({'type': 'result', 'data': result})
                                return
                    finally:
                        with anyio.CancelScope(shield=True):
                            disconnected.cancel()
                            with contextlib.suppress(asyncio.CancelledError):
                                await disconnected
                            if not task.done():
                                task.cancel()
                            # Keep the lease and request admission until all
                            # settlement/tool cancellation work actually ends.
                            while not task.done():
                                try:
                                    await asyncio.shield(task)
                                except asyncio.CancelledError:
                                    continue
                                except Exception:
                                    break
                            if not task.cancelled():
                                task.exception()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # Unknown exceptions and HTTP response bodies can contain secrets;
            # only errors intentionally authored by our own model/API layer
            # are suitable for the browser.
            if isinstance(exc, ProviderError):
                message = str(exc)
            elif isinstance(exc, (ValueError, KeyError)):
                message = str(exc) if isinstance(exc, ValueError) else '记录不存在或已被删除。'
            elif isinstance(exc, PermissionError):
                message = '当前会话或用户无权执行此操作。'
            elif isinstance(exc, HTTPException):
                message = str(exc.detail) if exc.status_code < 500 else '本次回复未能完成，请查看运行日志。'
            else:
                logging.getLogger(__name__).exception('Browser chat stream failed')
                message = '本次回复未能完成，请查看运行日志。'
            yield encode({'type': 'reset'})
            yield encode({'type': 'error', 'message': message})

    return StreamingResponse(events(), media_type='text/event-stream', headers={
        'Cache-Control': 'no-store', 'X-Accel-Buffering': 'no', 'X-Content-Type-Options': 'nosniff',
    })
