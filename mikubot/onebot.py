"""NapCat / SnowLuma OneBot v11 bridge. Reverse authentication belongs to API."""
from __future__ import annotations

import asyncio
import base64
import html
import json
import re
import time
import uuid
from collections import OrderedDict
from datetime import datetime, timezone
from urllib.parse import urlsplit

from .social import ConversationFlow
from .settings import resolve_model, resolved_identity
from .output import OutputLimitError, split_transport_output
from .access import manager_for, private_allowed, chat_allowed
from .qq_diagnostics import QQDiagnostics, private_scope
from .timezones import config_zone, ingress_times


def _ids(value):
    if isinstance(value, str):
        value = re.split(r"[,，;\s]+", value)
    return {str(item).strip() for item in (value or [])}


def scope_value(scope, key, default=None):
    return scope.get(key, (scope.get('settings') or {}).get(key, default))


def is_quiet(scope, now=None, *, settings=None):
    """Quiet windows follow the configured calendar, defaulting to UTC+8."""
    start, end = scope_value(scope, 'quiet_start', ''), scope_value(scope, 'quiet_end', '')
    if not start or not end or not re.fullmatch(r'\d{2}:\d{2}', start + '') or not re.fullmatch(r'\d{2}:\d{2}', end + ''):
        return False
    current = (now or datetime.now(timezone.utc)).astimezone(config_zone(settings)).strftime('%H:%M')
    return start <= current < end if start < end else current >= start or current < end


class OneBotBridge:
    def __init__(self, store, settings, chat):
        self.store, self.settings, self.chat = store, settings, chat
        self.access = manager_for(settings, chat)
        self._socket = None
        self._mode = 'reverse'
        self._running = False
        self._forward_task = None
        self._events = set()
        self._pending = {}
        self._seen = OrderedDict()
        self._last_reply = {}
        self._lock = asyncio.Lock()
        self._self_id = ''
        self._last_error = ''
        self._last_event_at = None
        self._last_connected_at = None
        self._outgoing = OrderedDict()
        self._diagnostics = QQDiagnostics(store, 'onebot')
        self._failure_feedback_at = {}
        self._flow = ConversationFlow(store, settings, chat, self._emit, self._allowed, self._diagnostics.mark)

    def _config(self):
        return self.settings.get().get('onebot', {})

    def _enabled(self):
        cfg = self._config()
        return bool(cfg.get('enabled')) and cfg.get('adapter', 'napcat') in ('napcat', 'snowluma')

    def status(self):
        cfg = self._config()
        adapter = cfg.get('adapter', 'napcat')
        return {'adapter': adapter, 'label': {'napcat': 'NapCat · OneBot v11', 'snowluma': 'SnowLuma · OneBot v11', 'official': 'QQ 官方机器人'}.get(adapter, adapter),
                'enabled': self._enabled(), 'connected': self._socket is not None and self._enabled(),
                'mode': self._mode, 'reconnecting': bool(self._running and cfg.get('ws_url') and self._socket is None),
                'self_id': self._self_id, 'last_error': self._last_error,
                'last_event_at': self._last_event_at, 'last_connected_at': self._last_connected_at,
                **self._diagnostics.status(connected=self._socket is not None and self._enabled())}

    async def start(self):
        if self._running:
            return
        self._running = True
        if self._enabled() and self._config().get('ws_url'):
            self._forward_task = asyncio.create_task(self._forward_loop())

    async def stop(self):
        self._running = False
        if self._forward_task:
            self._forward_task.cancel()
            await asyncio.gather(self._forward_task, return_exceptions=True)
            self._forward_task = None
        socket, self._socket = self._socket, None
        if socket:
            self._fail_pending(socket)
            await self._close(socket)
        for task in list(self._events):
            task.cancel()
        if self._events:
            await asyncio.gather(*self._events, return_exceptions=True)
        await self._flow.close()

    async def _close(self, socket, code=1000):
        try:
            await socket.close(code=code)
        except Exception:
            pass

    def _fail_pending(self, socket):
        for _, (bound_socket, future) in list(self._pending.items()):
            if bound_socket is socket and not future.done():
                future.set_result(None)

    async def attach(self, websocket):
        if not self._enabled():
            await self._close(websocket, 1008)
            return
        header_id = str(getattr(websocket, 'headers', {}).get('x-self-id', ''))
        if header_id.isdigit():
            self._self_id = header_id
        await self._read_connection(websocket, 'reverse')

    async def _read_connection(self, socket, mode):
        async with self._lock:
            if mode == 'forward' and self._socket is not None and self._mode == 'reverse':
                await self._close(socket)
                return
            previous, self._socket = self._socket, socket
            self._mode = mode
            self._last_connected_at = datetime.now(timezone.utc).isoformat()
            self._last_error = ''
        if previous is not None and previous is not socket:
            self._fail_pending(previous)
            await self._close(previous)
        try:
            bad_frames = 0
            while self._enabled() and self._socket is socket:
                try:
                    payload = await socket.receive_json() if hasattr(socket, 'receive_json') else json.loads(await socket.recv())
                except (json.JSONDecodeError, UnicodeError):
                    bad_frames += 1
                    self.store.add_event('qq_protocol_error','忽略了格式错误的连接器消息；未记录原始内容',consecutive_errors=bad_frames)
                    if bad_frames >= 3:
                        self._last_error='连接器连续发送无效JSON，请检查OneBot配置与连接器版本'
                        await self._close(socket,1008)
                        break
                    continue
                bad_frames = 0
                if not isinstance(payload, dict):
                    continue
                echo = str(payload.get('echo', ''))
                pending = self._pending.get(echo)
                if pending and pending[0] is socket:
                    if not pending[1].done():
                        timing = getattr(pending[1], '_mikubot_ack_timing', None)
                        if timing is not None:
                            timing['received'] = time.perf_counter()
                        pending[1].set_result(payload)
                    continue
                # Reading must remain free to receive action echoes while replies run.
                if len(self._events) < 64:
                    task = asyncio.create_task(self.handle_event(payload, wait=False))
                    self._events.add(task)
                    task.add_done_callback(self._event_done)
                elif payload.get('post_type') == 'message':
                    self._diagnostics.message()
                    self._diagnostics.mark('ingress_busy')
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if self._socket is socket:
                self._last_error = f'连接中断（{type(exc).__name__}）'
        finally:
            self._fail_pending(socket)
            if self._socket is socket:
                self._socket = None

    async def probe(self):
        """Read-only OneBot API roundtrip; this never sends a chat message."""
        socket = self._socket
        if socket is None or not self._enabled():
            return {'ok': False, 'message': '尚无可用的 OneBot WebSocket 连接'}
        echo = uuid.uuid4().hex
        future = asyncio.get_running_loop().create_future()
        self._pending[echo] = (socket, future)
        try:
            action = {'action': 'get_status', 'params': {}, 'echo': echo}
            if hasattr(socket, 'send_json'): await socket.send_json(action)
            else: await socket.send(json.dumps(action))
            result = await asyncio.wait_for(future, 5)
            ok = bool(result and result.get('status') == 'ok' and result.get('retcode') == 0)
            data = (result or {}).get('data') or {}
            return {'ok': ok, 'message': 'OneBot API Echo 往返通过' if ok else 'API 未返回成功回执，请检查连接器日志',
                    'online': data.get('online') if isinstance(data, dict) else None}
        except (TimeoutError, OSError, ConnectionError, RuntimeError):
            return {'ok': False, 'message': 'API Echo 超时或连接中断，请检查连接器的 API 响应与网络'}
        finally:
            self._pending.pop(echo, None)

    def _event_done(self, task):
        self._events.discard(task)
        if not task.cancelled() and task.exception():
            self.store.add_exception('qq_error', 'QQ 消息处理失败', task.exception())

    async def _forward_loop(self):
        delay = 1
        while self._running and self._enabled():
            cfg = self._config()
            url = cfg.get('ws_url', '')
            if not url:
                return
            if self._socket is not None:
                await asyncio.sleep(1)
                continue
            try:
                import websockets
                parsed = urlsplit(url)
                if parsed.scheme not in ('ws', 'wss') or not parsed.hostname:
                    raise ValueError('无效 WebSocket 地址')
                headers = {'Authorization': 'Bearer ' + cfg['access_token']} if cfg.get('access_token') else {}
                async with websockets.connect(url, additional_headers=headers, open_timeout=10,
                                              ping_interval=20, ping_timeout=20, max_size=8 * 1024 * 1024) as socket:
                    delay = 1
                    await self._read_connection(socket, 'forward')
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._last_error = f'正向连接失败（{type(exc).__name__}）'
            if self._running:
                await asyncio.sleep(delay)
                delay = min(delay * 2, 30)

    def _rejection(self, scope_id, user_id=None):
        if not self._enabled():
            return 'connector_disabled'
        if self.settings.get().get('paused'):
            return 'paused'
        cfg = self._config()
        parts = scope_id.split(':')
        if len(parts) != 3 or parts[0] != 'qq' or not parts[2].isdigit():
            return 'invalid_scope'
        if parts[1] == 'group':
            return None if chat_allowed(self.settings.get(), 'onebot', 'group', parts[2]) else 'group_not_allowed'
        if parts[1] != 'private' or (user_id is not None and user_id != parts[2]):
            return 'invalid_scope'
        return None if private_allowed(self.settings.get(), 'onebot', parts[2]) else 'private_not_allowed'

    def _allowed(self, scope_id, user_id=None):
        return self._rejection(scope_id, user_id) is None

    @staticmethod
    def _message(payload):
        raw = payload.get('message') or payload.get('raw_message', '')
        if isinstance(raw, str):
            segments, offset = [], 0
            for match in re.finditer(r'\[CQ:(\w+)((?:,[^\]]*)?)\]', raw):
                segments.append({'type': 'text', 'data': {'text': html.unescape(raw[offset:match.start()])}})
                data = {}
                for part in match.group(2).lstrip(',').split(','):
                    key, sep, value = part.partition('=')
                    if sep:
                        data[key] = html.unescape(value)
                segments.append({'type': match.group(1), 'data': data})
                offset = match.end()
            segments.append({'type': 'text', 'data': {'text': html.unescape(raw[offset:])}})
        else:
            segments = raw if isinstance(raw, list) else []
        text, images, mentions, reply_id = [], [], [], None
        for segment in segments:
            if not isinstance(segment, dict):
                continue
            data = segment.get('data') or {}
            kind = segment.get('type')
            if kind == 'text':
                text.append(str(data.get('text', '')))
            elif kind == 'at':
                mentioned_id = str(data.get('qq', ''))
                mentions.append(mentioned_id)
                if mentioned_id != str(payload.get('self_id', '')):
                    text.append('@' + mentioned_id + ' ')
            elif kind == 'reply':
                reply_id = str(data.get('id', ''))
            elif kind == 'image' and len(images) < 3:
                source = str(data.get('url') or data.get('file') or '')
                if source.startswith('http://'):
                    try: host=(urlsplit(source).hostname or '').lower()
                    except ValueError: host=''
                    if any(host==domain or host.endswith('.'+domain) for domain in ('qpic.cn','qq.com','qq.com.cn')):
                        source='https://'+source[len('http://'):]
                if source.startswith(('https://', 'http://', 'data:image/')) and len(source) <= 4 * 1024 * 1024:
                    images.append(source)
                elif re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9_.-]{5,255}',source):
                    images.append('onebot-image:'+source)
        return ''.join(text).strip(), images, mentions, reply_id

    async def handle_event(self, payload, *, wait=True):
        if not isinstance(payload, dict):
            return
        self._last_event_at = datetime.now(timezone.utc).isoformat()
        message_times = ingress_times(payload.get('time'), received_at=self._last_event_at)
        if payload.get('self_id'):
            self._self_id = str(payload['self_id'])
        if payload.get('post_type') != 'message':
            if payload.get('post_type') == 'meta_event' and payload.get('meta_event_type') == 'heartbeat':
                self._diagnostics.heartbeat()
            return
        self._diagnostics.message()
        if not payload.get('self_id') and self._self_id:
            payload = dict(payload, self_id=self._self_id)
        user_id = str(payload.get('user_id', ''))
        if not user_id.isdigit():
            self._diagnostics.mark('invalid_sender')
            return
        if user_id == str(payload.get('self_id', '')):
            self._diagnostics.mark('self_message', user_id=user_id, emit=False)
            return
        message_type = payload.get('message_type')
        if message_type == 'group':
            target = str(payload.get('group_id', ''))
        elif message_type == 'private':
            target = user_id
        else:
            self._diagnostics.mark('unsupported_message', user_id=user_id)
            return
        scope_id = f'qq:{message_type}:{target}'
        trusted_identity = self.access.identity('onebot', user_id, str(payload.get('self_id', '')))
        # Manual mode records a candidate; a locally enabled 60-second pairing
        # window grants only the identity proven by this private ingress.
        binding_text, _, _, _ = self._message(payload)
        code = self.access.pairing_code(binding_text)
        if (code and message_type == 'private' and self._enabled() and not self.settings.get().get('paused')):
            if self.access.capture_pairing(code, trusted_identity, private=True):
                self.store.add_event('admin_pairing', '已处理QQ管理员配对消息，请在管理员页面查看绑定结果', platform='onebot', account_id=user_id)
                self._diagnostics.mark('admin_pairing', scope_id=scope_id, user_id=user_id, stage='command')
            return
        if self._enabled() and self._rejection(scope_id, user_id) != 'invalid_scope':
            display = payload.get('group_name', '') if message_type == 'group' else (payload.get('sender') or {}).get('nickname', '')
            self.store.discover_scope(scope_id, display)
        rejection = self._rejection(scope_id, user_id)
        if rejection:
            self._diagnostics.mark(rejection, scope_id=scope_id, user_id=user_id)
            return
        scope = private_scope(self.store, scope_id)
        if not scope_value(scope, 'enabled', True):
            self._diagnostics.mark('scope_disabled', scope_id=scope_id, user_id=user_id)
            return
        platform_id = str(payload.get('message_id', ''))
        event_key = (str(payload.get('self_id', '')), scope_id, platform_id)
        if platform_id:
            if event_key in self._seen:
                self._diagnostics.mark('duplicate_message', scope_id=scope_id, user_id=user_id, emit=False)
                return
            # Preserve deduplication across bridge restarts using stored provenance.
            for message in self.store.list_messages(scope_id, limit=200):
                meta = message.get('metadata') or {}
                if str(meta.get('platform_message_id', '')) == platform_id and str(meta.get('onebot_self_id', '')) == event_key[0]:
                    self._seen[event_key] = time.monotonic()
                    self._diagnostics.mark('duplicate_message', scope_id=scope_id, user_id=user_id, emit=False)
                    return
            self._seen[event_key] = time.monotonic()
            while len(self._seen) > 4096:
                self._seen.popitem(last=False)
        text, images, mentions, reply_id = self._message(payload)
        if any(image.startswith('onebot-image:') for image in images):
            resolved=[]
            for image in images:
                if not image.startswith('onebot-image:'):resolved.append(image);continue
                socket=self._socket
                if socket is None:continue
                echo=uuid.uuid4().hex;future=asyncio.get_running_loop().create_future();self._pending[echo]=(socket,future)
                try:
                    action={'action':'get_image','params':{'file':image[len('onebot-image:'):]},'echo':echo}
                    if hasattr(socket,'send_json'):await socket.send_json(action)
                    else:await socket.send(json.dumps(action))
                    result=await asyncio.wait_for(future,10)
                    value=(result or {}).get('data') or {}
                    url=value.get('url') if isinstance(value,dict) else None
                    if isinstance(url,str) and url.startswith('https://') and len(url)<=3000:resolved.append(url)
                except (OSError,TimeoutError,RuntimeError):pass
                finally:self._pending.pop(echo,None)
            if len(resolved)!=len(images):
                await self.send(scope_id,'这张QQ图片暂时无法读取，请重新发送原图后重试。')
                return
            images=resolved
        if not text and not images:
            self._diagnostics.mark('empty_message', scope_id=scope_id, user_id=user_id)
            return
        text = text or '请看看这张图片。'
        metadata = {**message_times, 'platform': 'qq', 'platform_message_id': platform_id,
                    'onebot_self_id': str(payload.get('self_id', '')), 'source_id': platform_id,
                    'sender_name': str((payload.get('sender') or {}).get('card') or (payload.get('sender') or {}).get('nickname') or user_id),
                    'is_owner': self.access.is_admin(trusted_identity), 'is_admin': self.access.is_admin(trusted_identity),
                    'trusted_identity':trusted_identity, 'images': images,
                    'mentioned_user_ids': [item for item in mentions if item != str(payload.get('self_id', ''))]}
        if message_type == 'group':
            metadata['group_id'] = target
        if reply_id:
            metadata['reply_to_id'] = reply_id
            metadata.update(self._reply_context(scope_id, reply_id))
        mentioned = str(payload.get('self_id', '')) in mentions
        metadata['at_bot'] = mentioned
        command = getattr(self.chat, 'handle_platform_command', None)
        self._diagnostics.mark('accepted', scope_id=scope_id, user_id=user_id, emit=False)
        if command:
            result = await command(scope_id, user_id, text, metadata)
            if result is not None:
                self._diagnostics.mark('command', scope_id=scope_id, user_id=user_id, stage='command', emit=False)
                if result.get('reply'):
                    await self.send(scope_id, result['reply'], metadata=metadata)
                return
        if hasattr(self.chat, 'observe'):
            try:
                result = await self._flow.handle(scope_id, user_id, text, images, metadata,
                                               direct=message_type == 'private' or mentioned,
                                               reply_to_bot=metadata.get('reply_to_bot', False), wait=wait)
                if result and result.get('accepted') is False:
                    self._diagnostics.mark(result['code'], scope_id=scope_id, user_id=user_id, stage='skipped')
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.store.add_exception('qq_error', 'QQ 消息进入回复流程失败', exc, scope_id=scope_id)
                self._diagnostics.mark('reply_failed', scope_id=scope_id, user_id=user_id, stage='failed')
                await self._failure_feedback(scope_id, metadata)
            return
        identity = resolved_identity(self.settings.get(), scope)
        reply = message_type == 'private' or (mentioned and identity['trigger_at'])
        if not reply:
            self.store.add_message(scope_id, user_id, 'user', text, **metadata)
            self._diagnostics.mark('planner_skip', scope_id=scope_id, user_id=user_id, stage='skipped')
            return
        self._last_reply[scope_id] = time.monotonic()
        if images and not resolve_model(self.settings.get(), 'chat', model_id=scope.get('model_id') or None).get('vision'):
            self.store.add_message(scope_id, user_id, 'user', text, **metadata)
            await self.send(scope_id, '当前模型尚未开启图片输入。请补充文字，或在本地设置中确认模型支持视觉后开启。')
            return
        try:
            result = await self.chat.respond(scope_id, user_id, text, images=images, metadata=metadata)
            if result.get('reply'):
                delivered = await self.send(scope_id, result['reply'])
                if not delivered:
                    self.store.add_event('qq_delivery', '回复未确认送达', scope_id=scope_id, message_id=result.get('message_id'))
            else:
                self._diagnostics.mark('reply_empty', scope_id=scope_id, user_id=user_id, stage='failed')
                await self._failure_feedback(scope_id, metadata)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.store.add_exception('qq_error', 'QQ 回复失败，请检查模型与连接', exc, scope_id=scope_id)
            self._diagnostics.mark('reply_failed', scope_id=scope_id, user_id=user_id, stage='failed')
            await self._failure_feedback(scope_id, metadata)

    async def _failure_feedback(self, scope_id, metadata):
        if not self._allowed(scope_id):
            return
        now = time.monotonic()
        if now - self._failure_feedback_at.get(scope_id, -float('inf')) < 30:
            return
        self._failure_feedback_at[scope_id] = now
        while len(self._failure_feedback_at) > 512:
            self._failure_feedback_at.pop(next(iter(self._failure_feedback_at)))
        await self.send(scope_id, '这次回复没能完成，请检查本地模型、预算与运行日志后再试。', metadata=dict(metadata, failure_feedback=True))

    async def _emit(self, scope_id, text, metadata):
        if metadata.get('sticker'):
            return await self._send_part(scope_id,'',metadata=metadata)
        return await self.send(scope_id, text, metadata=metadata)

    def _reply_context(self, scope_id, platform_id):
        own = self._outgoing.get(str(platform_id)) == scope_id
        if not own:
            own = any(event.get('kind') == 'qq_sent' and (event.get('details') or {}).get('scope_id') == scope_id
                      and str((event.get('details') or {}).get('platform_message_id')) == str(platform_id)
                      for event in self.store.list_events(limit=200))
        context = {'reply_to_bot': own}
        for message in reversed(self.store.list_messages(scope_id, limit=200)):
            if str((message.get('metadata') or {}).get('platform_message_id', '')) == str(platform_id):
                context.update(reply_to_source_id=message['id'], reply_to_user_id=message['user_id'])
                break
        return context

    async def send(self, scope_id, text, *, metadata=None):
        if not self._allowed(scope_id):
            return False
        scope = self.store.get_scope(scope_id)
        try:
            parts = split_transport_output(text, self.settings.get(), scope)
        except OutputLimitError:
            if not (metadata or {}).get('assistant_message_id'):
                self.store.add_message(scope_id, 'assistant', 'assistant', str(text), delivery_blocked=True)
            self.store.add_event('qq_delivery', '完整链接或代码超过本地发送长度上限，原文已保留', scope_id=scope_id)
            return False
        for index, part in enumerate(parts, 1):
            receipt = dict(metadata or {})
            if len(parts) > 1:
                receipt['output_part'] = {'index': index, 'total': len(parts)}
            if not await self._send_part(scope_id, part, metadata=receipt):
                return False
        return bool(parts)

    async def _send_part(self, scope_id, text, *, metadata=None):
        started = time.perf_counter()
        sticker=(metadata or {}).get('sticker')
        if not self._allowed(scope_id) or self._socket is None or not str(text).strip() and not sticker:
            return False
        try:
            scope = self.store.get_scope(scope_id)
        except KeyError:
            return False
        if not scope_value(scope, 'enabled', True):
            return False
        _, kind, target = scope_id.split(':')
        params = {kind + '_id' if kind == 'group' else 'user_id': int(target),
                  'message': [{'type': 'text', 'data': {'text': str(text)}}]}
        if sticker:
            try:
                guard=(metadata or {}).get('sticker_current')
                if callable(guard) and not guard(): return False
                _,raw=self.chat.stickers.payload(sticker['id'],scope_id,sticker['version'])
                params['message']=[{'type':'image','data':{'file':'base64://'+base64.b64encode(raw).decode()}}]
            except (KeyError,ValueError,AttributeError,TypeError): return False
        socket = self._socket
        echo = uuid.uuid4().hex
        future = asyncio.get_running_loop().create_future()
        ack_timing = {}
        future._mikubot_ack_timing = ack_timing
        self._pending[echo] = (socket, future)
        write_started, write_finished, dispatched = started, None, False
        def timings():
            now = time.perf_counter()
            received = ack_timing.get('received')
            values = {'send_total_ms': round((now - started) * 1000, 2),
                      'preflight_ms': round((write_started - started) * 1000, 2)}
            if write_finished is not None:
                values.update(socket_write_ms=round((write_finished - write_started) * 1000, 2),
                              ack_wait_ms=round(max(0, (received or now) - write_finished) * 1000, 2))
            if received is not None:
                values['ack_resume_ms'] = round(max(0, now - received) * 1000, 2)
            return values
        try:
            action = {'action': 'send_group_msg' if kind == 'group' else 'send_private_msg', 'params': params, 'echo': echo}
            write_started = time.perf_counter()
            # Once handed to the transport a cancelled send has unknown delivery,
            # even if the caller has not yet received the connector's echo.
            dispatched = True
            if hasattr(socket, 'send_json'):
                await socket.send_json(action)
            else:
                await socket.send(json.dumps(action, ensure_ascii=False))
            write_finished = time.perf_counter()
            timeout = float(self._config().get('action_timeout_seconds', 30))
            result = await asyncio.wait_for(future, max(0.01, min(timeout, 60)))
            confirmed = bool(result and result.get('status') == 'ok' and result.get('retcode') == 0
                             and isinstance(result.get('data'), dict) and result['data'].get('message_id') is not None)
            if not confirmed:
                code = result.get('retcode') if isinstance(result, dict) else None
                code = str(code)[:16] if isinstance(code, (int, float)) else None
                self._last_error = '连接器未确认送达' + (f'（retcode={code}）' if code is not None else '')
                self._diagnostics.mark('send_failed', scope_id=scope_id, stage='failed')
                self.store.add_event('qq_delivery', 'QQ 发送结果未确认；请勿自动重复发送', scope_id=scope_id,
                                     platform_code=code,
                                     assistant_message_id=(metadata or {}).get('assistant_message_id'), send_timing=timings(),
                                     output_part=(metadata or {}).get('output_part'))
            else:
                self._diagnostics.mark('reply_failed' if (metadata or {}).get('failure_feedback') else 'sent', scope_id=scope_id,
                                       stage='failed' if (metadata or {}).get('failure_feedback') else 'sent', emit=False)
                message_id = str(result['data']['message_id'])
                self._outgoing[message_id] = scope_id
                while len(self._outgoing) > 2048:
                    self._outgoing.popitem(last=False)
                self.store.add_event('qq_sent', 'QQ 消息已确认发送', scope_id=scope_id, platform_message_id=message_id,
                                     output_part=(metadata or {}).get('output_part'), assistant_message_id=(metadata or {}).get('assistant_message_id'),
                                     send_timing=timings())
            return confirmed
        except asyncio.CancelledError:
            if dispatched:
                try:
                    self.store.add_event('qq_delivery', 'QQ发送等待已取消，已派发分段的送达状态未知；不会自动重发',
                        scope_id=scope_id, assistant_message_id=(metadata or {}).get('assistant_message_id'),
                        output_part=(metadata or {}).get('output_part'), send_timing=timings(), delivery_status='unknown')
                except Exception:
                    pass  # A failed timing write must not replace cancellation.
            raise
        except Exception as exc:
            self._diagnostics.mark('send_failed', scope_id=scope_id, stage='failed')
            self._last_error = f'发送结果未确认（{type(exc).__name__}）'
            self.store.add_exception('qq_delivery', '发送超时或连接中断，送达状态未知', exc, scope_id=scope_id,
                                 output_part=(metadata or {}).get('output_part'),
                                 assistant_message_id=(metadata or {}).get('assistant_message_id'), send_timing=timings())
            return False
        finally:
            self._pending.pop(echo, None)

