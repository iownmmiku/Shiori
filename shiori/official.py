"""Optional QQ Open Platform bridge, using Tencent's qq-botpy SDK.

Official group/user openids are deliberately distinct from OneBot QQ numbers.
No network connection is opened until an enabled, configured bridge is started.
"""
from __future__ import annotations

import asyncio
import base64
import io
from collections import OrderedDict
from datetime import datetime, timezone
from .timezones import ingress_times
import re
import time
from typing import Any

from .social import ConversationFlow
from .access import manager_for, private_allowed, chat_allowed
from .settings import resolved_identity
from .output import OutputLimitError, split_transport_output
from .qq_diagnostics import QQDiagnostics, private_scope


def _field(value: Any, name: str, default: Any = None) -> Any:
    return value.get(name, default) if isinstance(value, dict) else getattr(value, name, default)


def _ids(value: Any) -> set[str]:
    if isinstance(value, str):
        value = re.split(r"[,，;\s]+", value)
    return {str(item).strip() for item in value or [] if str(item).strip()}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class OfficialBridge:
    def __init__(self, store, settings, chat):
        self.store, self.settings, self.chat = store, settings, chat
        self.access = manager_for(settings, chat)
        self._client = None
        self._runner = None
        self._running = False
        self._connected = False
        self._last_error = ""
        self._last_event_at = None
        self._last_connected_at = None
        self._seen = OrderedDict()
        self._event_tasks = set()
        self._gateway_tasks = set()
        self._lifecycle_lock = asyncio.Lock()
        self._outgoing = OrderedDict()
        self._diagnostics = QQDiagnostics(store, 'qqofficial')
        self._failure_feedback_at = {}
        self._flow = ConversationFlow(store, settings, chat, self._emit, self._allowed, self._diagnostics.mark)

    def _config(self) -> dict:
        return self.settings.get().get("qq_official", {})

    def _enabled(self) -> bool:
        full = self.settings.get()
        return bool(full.get("qq_official", {}).get("enabled")) and full.get("onebot", {}).get("adapter", "official") == "official"

    def status(self) -> dict:
        return {"adapter": "official", "label": "QQ 官方机器人", "mode": "official-sdk",
                "enabled": self._enabled(), "connected": self._connected and self._enabled(),
                "reconnecting": self._running and not self._connected,
                "self_id": str(self._config().get("app_id", "")), "last_error": self._last_error,
                "last_event_at": self._last_event_at, "last_connected_at": self._last_connected_at,
                **self._diagnostics.status(connected=self._connected and self._enabled())}

    def _build_client(self):
        # Import lazily so local chat and OneBot remain usable without this SDK.
        import botpy
        from botpy.gateway import BotWebSocket

        bridge = self

        def track(task, collection):
            if task is not None:
                collection.add(task)
                task.add_done_callback(collection.discard)

        class TrackedWebSocket(BotWebSocket):
            async def _send_heart(self, *args, **kwargs):
                track(asyncio.current_task(), bridge._gateway_tasks)
                return await super()._send_heart(*args, **kwargs)

        class Client(botpy.Client):
            async def on_ready(self):
                if bridge._running and bridge._enabled():
                    bridge._connected = True
                    bridge._last_error = ""
                    bridge._last_connected_at = _now()
                    bridge.store.add_event("qq_official", "QQ 官方机器人已连接")

            async def on_group_at_message_create(self, message):
                await bridge._handle_message("group", message, direct=True, wait=False)

            async def on_group_message_create(self, message):
                # Availability depends on the application's platform permission.
                await bridge._handle_message("group", message, direct=False, wait=False)

            async def on_c2c_message_create(self, message):
                await bridge._handle_message("private", message, wait=False)

            async def on_error(self, event_method, *args, **kwargs):
                bridge._last_error = "官方事件处理失败，请检查运行日志"
                bridge.store.add_event("qq_official_error", "官方事件处理失败", event=str(event_method))

            def _schedule_event(self, coro, event_name, *args, **kwargs):
                task = super()._schedule_event(coro, event_name, *args, **kwargs)
                track(task, bridge._event_tasks)
                return task

            async def bot_connect(self, session):
                # SDK 1.2.1 spawns gateway/heartbeat tasks independently. Track
                # them so switching adapters cannot leave a live receiver behind.
                track(asyncio.current_task(), bridge._gateway_tasks)
                gateway = TrackedWebSocket(session, self._connection)
                try:
                    await gateway.ws_connect()
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    bridge._last_error = f"官方连接中断（{type(exc).__name__}）"
                    if bridge._running and bridge._enabled():
                        self._connection.add(session)
                finally:
                    bridge._connected = False

        return Client(intents=botpy.Intents(public_messages=True), bot_log=False, ext_handlers=False)

    async def start(self) -> None:
        async with self._lifecycle_lock:
            if self._running:
                return
            if not self._enabled():
                return
            cfg = self._config()
            if not cfg.get("app_id") or not cfg.get("app_secret"):
                self._last_error = "请填写官方机器人 AppID 和 AppSecret"
                return
            try:
                client = self._build_client()
            except ImportError:
                self._last_error = "缺少 qq-botpy，请重新安装官方连接器依赖"
                return
            self._client = client
            self._running = True
            self._last_error = ""
            self._runner = asyncio.create_task(self._run(client, dict(cfg)), name="shiori.qq_official")

    async def _run(self, client, cfg: dict) -> None:
        loop = asyncio.get_running_loop()
        previous_handler = loop.get_exception_handler()
        try:
            async with client:
                await client.start(appid=str(cfg["app_id"]), secret=str(cfg["app_secret"]))
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._last_error = f"官方机器人启动失败（{type(exc).__name__}），请检查凭据与平台权限"
            self.store.add_event("qq_official_error", self._last_error)
        finally:
            self._connected = False
            self._running = False
            handler = loop.get_exception_handler()
            if getattr(handler, "__module__", "") == "botpy.client":
                loop.set_exception_handler(previous_handler)

    async def stop(self) -> None:
        async with self._lifecycle_lock:
            self._running = False
            self._connected = False
            runner, client = self._runner, self._client
            self._runner = self._client = None
            tasks = set(self._event_tasks) | set(self._gateway_tasks)
            if runner is not None:
                tasks.add(runner)
            tasks.discard(asyncio.current_task())
            for task in tasks:
                task.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
            self._event_tasks.clear()
            self._gateway_tasks.clear()
            await self._flow.close()
            if client is not None:
                try:
                    await client.close()
                except Exception:
                    pass

    def _rejection(self, scope_id: str, user_id: str | None = None):
        if not self._running or not self._enabled():
            return 'connector_disabled'
        if self.settings.get().get('paused'):
            return 'paused'
        parts = str(scope_id).split(":")
        if len(parts) != 3 or parts[0] != "qqofficial" or not re.fullmatch(r"[A-Za-z0-9_.-]{1,180}", parts[2]):
            return 'invalid_scope'
        cfg = self._config()
        if parts[1] == "group":
            return None if chat_allowed(self.settings.get(), 'qqofficial', 'group', parts[2]) else 'group_not_allowed'
        if parts[1] != 'private' or (user_id is not None and parts[2] != user_id):
            return 'invalid_scope'
        return None if private_allowed(self.settings.get(), 'qqofficial', parts[2]) else 'private_not_allowed'

    def _allowed(self, scope_id: str, user_id: str | None = None) -> bool:
        return self._rejection(scope_id, user_id) is None

    async def handle_event(self, payload: dict) -> None:
        """Normalized event entry point, useful for local protocol verification."""
        if not isinstance(payload, dict):
            return
        event_type = str(payload.get("t", "")).upper()
        kind = {"GROUP_AT_MESSAGE_CREATE": "group", "GROUP_MESSAGE_CREATE": "group", "C2C_MESSAGE_CREATE": "private"}.get(event_type)
        if kind:
            await self._handle_message(kind, payload.get("d") or {}, direct=event_type != 'GROUP_MESSAGE_CREATE')

    async def _handle_message(self, kind: str, message: Any, *, direct: bool = True, wait: bool = True) -> None:
        if not self._running:
            return
        self._diagnostics.message()
        self._last_event_at = _now()
        message_times = ingress_times(_field(message, 'timestamp', None), received_at=self._last_event_at)
        author = _field(message, "author", {})
        user_id = str(_field(author, "member_openid" if kind == "group" else "user_openid", "") or "")
        target = str(_field(message, "group_openid", "") or "") if kind == "group" else user_id
        platform_id = str(_field(message, "id", "") or "")
        scope_id = f"qqofficial:{kind}:{target}"
        app_id = str(self._config().get('app_id', ''))
        if not user_id or not platform_id or not re.fullmatch(r'[A-Za-z0-9_.-]{1,180}', user_id):
            self._diagnostics.mark('invalid_sender')
            return
        trusted_identity = self.access.identity('qqofficial', user_id, app_id)
        code = self.access.pairing_code(str(_field(message, 'content', '') or '').strip())
        if code and kind == 'private' and self._enabled() and not self.settings.get().get('paused'):
            if self.access.capture_pairing(code, trusted_identity, private=True):
                self.store.add_event('admin_pairing', '已处理QQ管理员配对消息，请在管理员页面查看绑定结果', platform='qqofficial', account_id=user_id)
                self._diagnostics.mark('admin_pairing', scope_id=scope_id, user_id=user_id, stage='command')
            return
        if self._enabled() and self._rejection(scope_id, user_id) != 'invalid_scope':
            self.store.discover_scope(scope_id)
        rejection = self._rejection(scope_id, user_id)
        if rejection:
            self._diagnostics.mark(rejection, scope_id=scope_id, user_id=user_id)
            return
        scope = private_scope(self.store, scope_id)
        if not scope.get("enabled", True):
            self._diagnostics.mark('scope_disabled', scope_id=scope_id, user_id=user_id)
            return
        self._last_event_at = _now()
        app_id = str(self._config().get("app_id", ""))
        key = (app_id, scope_id, platform_id)
        if key in self._seen:
            self._diagnostics.mark('duplicate_message', scope_id=scope_id, user_id=user_id, emit=False)
            return
        for prior in self.store.list_messages(scope_id, limit=1000):
            metadata = prior.get("metadata") or {}
            if metadata.get("platform_message_id") == platform_id and metadata.get("official_app_id") == app_id:
                self._seen[key] = None
                self._diagnostics.mark('duplicate_message', scope_id=scope_id, user_id=user_id, emit=False)
                return
        text = str(_field(message, "content", "") or "").strip()
        if direct:
            text = re.sub(r"^(?:<@!?[A-Za-z0-9_.-]+>\s*)+", "", text).strip()
        images = []
        for attachment in _field(message, "attachments", []) or []:
            content_type = str(_field(attachment, "content_type", "") or "")
            url = str(_field(attachment, "url", "") or "")
            if url.startswith("//"):
                url = "https:" + url
            if content_type.startswith("image/") and url.startswith("https://") and len(url) <= 3000:
                images.append(url)
            if len(images) == 3:
                break
        if not text and not images:
            self._diagnostics.mark('empty_message', scope_id=scope_id, user_id=user_id)
            return
        self._seen[key] = None
        while len(self._seen) > 4096:
            self._seen.popitem(last=False)
        metadata = {**message_times, "platform": "qqofficial", "official_app_id": app_id, "platform_message_id": platform_id,
                    "source_id": platform_id, "sender_name": user_id, "images": images,
                    "is_owner":self.access.is_admin(trusted_identity), "is_admin":self.access.is_admin(trusted_identity),
                    "trusted_identity":trusted_identity,
                    "at_bot": direct and kind == 'group'}
        metadata['mentioned_user_ids'] = re.findall(r'<@!?([A-Za-z0-9_.-]+)>', text)
        if kind == "group":
            metadata["group_id"] = target
        reference = _field(message, 'message_reference', {}) or {}
        reply_id = str(_field(reference, 'message_id', '') or _field(message, 'ref_msg_idx', '') or '')
        if reply_id:
            metadata['reply_to_id'] = reply_id
            metadata['reply_to_bot'] = (self._outgoing.get(reply_id) == scope_id or any(
                event.get('kind') == 'qq_official_sent'
                and (event.get('details') or {}).get('scope_id') == scope_id
                and str((event.get('details') or {}).get('platform_message_id')) == reply_id
                and str((event.get('details') or {}).get('official_app_id', '')) == app_id
                for event in self.store.list_events(limit=200)))
            for prior in reversed(self.store.list_messages(scope_id, limit=200)):
                if str((prior.get('metadata') or {}).get('platform_message_id', '')) == reply_id:
                    metadata.update(reply_to_source_id=prior['id'], reply_to_user_id=prior['user_id'])
                    break
        try:
            self._diagnostics.mark('accepted', scope_id=scope_id, user_id=user_id, emit=False)
            command = getattr(self.chat, 'handle_platform_command', None)
            if command:
                result = await command(scope_id, user_id, text, metadata)
                if result is not None:
                    self._diagnostics.mark('command', scope_id=scope_id, user_id=user_id, stage='command', emit=False)
                    if result.get('reply'):
                        await self._send(scope_id, result['reply'], msg_id=platform_id, metadata=metadata)
                    return
            if hasattr(self.chat, 'observe'):
                result = await self._flow.handle(scope_id, user_id, text, images, metadata, direct=direct, reply_to_bot=metadata.get('reply_to_bot', False), wait=wait)
                if result and result.get('accepted') is False:
                    self._diagnostics.mark(result['code'], scope_id=scope_id, user_id=user_id, stage='skipped')
                return
            if kind == 'group' and (not direct or not resolved_identity(self.settings.get(), scope)['trigger_at']):
                self.store.add_message(scope_id, user_id, 'user', text, **metadata)
                self._diagnostics.mark('passive_only', scope_id=scope_id, user_id=user_id, stage='skipped')
                return
            result = await self.chat.respond(scope_id, user_id, text, images=images, metadata=metadata)
            if result.get("reply"):
                delivered = await self._send(scope_id, result["reply"], msg_id=platform_id)
                if not delivered:
                    self.store.add_event("qq_official_delivery", "官方回复未确认送达", scope_id=scope_id, message_id=result.get("message_id"))
            else:
                self._diagnostics.mark('reply_empty', scope_id=scope_id, user_id=user_id, stage='failed')
                await self._failure_feedback(scope_id, platform_id, metadata)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.store.add_event("qq_official_error", "QQ 官方回复失败，请检查模型与连接", scope_id=scope_id, error=type(exc).__name__)
            self._diagnostics.mark('reply_failed', scope_id=scope_id, user_id=user_id, stage='failed')
            await self._failure_feedback(scope_id, platform_id, metadata)

    async def _failure_feedback(self, scope_id, platform_id, metadata):
        if not self._allowed(scope_id):
            return
        now = time.monotonic()
        if now - self._failure_feedback_at.get(scope_id, -float('inf')) < 30:
            return
        self._failure_feedback_at[scope_id] = now
        while len(self._failure_feedback_at) > 512:
            self._failure_feedback_at.pop(next(iter(self._failure_feedback_at)))
        await self._send(scope_id, '这次回复没能完成，请检查本地模型、预算与运行日志后再试。', msg_id=platform_id,
                         metadata=dict(metadata, failure_feedback=True))

    async def _emit(self, scope_id, text, metadata):
        if metadata.get('sticker'):
            return await self._send_part(scope_id,'',msg_id=metadata.get('platform_message_id'),
                msg_seq=(metadata.get('output_part') or {}).get('index',1),metadata=metadata)
        return await self._send(scope_id, text, msg_id=metadata.get('platform_message_id'),
                                msg_seq=(metadata.get('output_part') or {}).get('index', 1), metadata=metadata)

    async def send(self, scope_id: str, text: str) -> bool:
        """Send a proactive reminder; never disguise it as an old passive reply."""
        return await self._send(scope_id, text)

    async def _send(self, scope_id: str, text: str, *, msg_id: str | None = None, msg_seq: int = 1, metadata=None) -> bool:
        if not self._allowed(scope_id):
            return False
        scope = self.store.get_scope(scope_id)
        try:
            parts = split_transport_output(text, self.settings.get(), scope)
        except OutputLimitError:
            if not (metadata or {}).get('assistant_message_id'):
                self.store.add_message(scope_id, 'assistant', 'assistant', str(text), delivery_blocked=True)
            self.store.add_event('qq_official_delivery', '完整链接或代码超过本地发送长度上限，原文已保留', scope_id=scope_id)
            return False
        for index, part in enumerate(parts, 1):
            receipt = dict(metadata or {})
            if len(parts) > 1:
                receipt['output_part'] = {'index': index, 'total': len(parts)}
            if not await self._send_part(scope_id, part, msg_id=msg_id, msg_seq=msg_seq + index - 1, metadata=receipt):
                return False
        return bool(parts)

    async def _send_part(self, scope_id: str, text: str, *, msg_id: str | None = None, msg_seq: int = 1, metadata=None) -> bool:
        sticker=(metadata or {}).get('sticker')
        if not self._allowed(scope_id) or not self._connected or self._client is None or not str(text).strip() and not sticker:
            return False
        scope = self.store.get_scope(scope_id)
        if not scope.get("enabled", True):
            return False
        _, kind, target = scope_id.split(":")
        kwargs = {"msg_type": 0, "content": str(text)}
        if msg_id:
            kwargs.update(msg_id=msg_id, msg_seq=msg_seq)
        try:
            api = self._client.api
            if sticker:
                from PIL import Image
                from botpy.http import Route
                guard=(metadata or {}).get('sticker_current')
                if callable(guard) and not guard(): return False
                _,raw=self.chat.stickers.payload(sticker['id'],scope_id,sticker['version'])
                # Official group/C2C media requires upload then msg_type=7.
                # botpy 1.2.1 lacks a file_data wrapper, so use its signed HTTP client.
                with Image.open(io.BytesIO(raw)) as image:
                    image.thumbnail((1024,1024))
                    output=io.BytesIO();image.convert('RGBA').save(output,format='PNG')
                if output.tell()>2*1024*1024: return False
                route=Route('POST','/v2/groups/{group_openid}/files',group_openid=target) if kind=='group' else Route('POST','/v2/users/{openid}/files',openid=target)
                upload=await asyncio.wait_for(api._http.request(route,json={'file_type':1,'file_data':base64.b64encode(output.getvalue()).decode(),'srv_send_msg':False}),timeout=20)
                if not isinstance(upload,dict) or not upload.get('file_info'): return False
                self.chat.stickers.payload(sticker['id'],scope_id,sticker['version'])
                if not self._allowed(scope_id) or callable(guard) and not guard(): return False
                kwargs.update(msg_type=7,content=' ',media={'file_info':upload['file_info']})
            operation = api.post_group_message(group_openid=target, **kwargs) if kind == "group" else api.post_c2c_message(openid=target, **kwargs)
            result = await asyncio.wait_for(operation, timeout=20)
            confirmed = isinstance(result, dict) and bool(result.get("id")) and result.get("code", 0) in (None, 0)
            if not confirmed:
                self._diagnostics.mark('send_failed', scope_id=scope_id, stage='failed')
                self._last_error = "官方发送未获确认，请检查权限、额度与消息审核状态"
                self.store.add_event("qq_official_delivery", self._last_error, scope_id=scope_id,
                                     output_part=(metadata or {}).get('output_part'))
            else:
                self._diagnostics.mark('reply_failed' if (metadata or {}).get('failure_feedback') else 'sent', scope_id=scope_id,
                                       stage='failed' if (metadata or {}).get('failure_feedback') else 'sent', emit=False)
                self._outgoing[str(result['id'])] = scope_id
                while len(self._outgoing) > 2048:
                    self._outgoing.popitem(last=False)
                self.store.add_event('qq_official_sent', '官方消息已确认发送', scope_id=scope_id, platform_message_id=str(result['id']),
                                     output_part=(metadata or {}).get('output_part'), assistant_message_id=(metadata or {}).get('assistant_message_id'),
                                     official_app_id=str(self._config().get('app_id', '')))
            return confirmed
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._diagnostics.mark('send_failed', scope_id=scope_id, stage='failed')
            self._last_error = f"官方发送未确认（{type(exc).__name__}），请检查权限或发送额度"
            self.store.add_exception("qq_official_delivery", self._last_error, exc, scope_id=scope_id,
                                 output_part=(metadata or {}).get('output_part'))
            return False
