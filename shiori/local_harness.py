"""Run local computer tools inside the owning chat turn, with durable receipts."""
from __future__ import annotations

import asyncio
import concurrent.futures
import copy
import hashlib
import itertools
import secrets
import threading

from jsonschema import ValidationError, validate

from .computer import TOOLS
from .db import message_fingerprint


class LocalHarness:
    def __init__(self, computer):
        self.computer = computer
        self.runs = computer.runs
        self._active = {}

    def allowed(self, scope_id, user_id, metadata):
        identity = (metadata or {}).get('trusted_identity') or {}
        return (str(scope_id).startswith('web:') and str(user_id) == 'owner'
                and identity.get('platform') == 'local'
                and self.computer.allowed(identity, scope_id))

    def tools(self, scope_id, user_id, metadata):
        if not self.allowed(scope_id, user_id, metadata):
            return []
        tools = copy.deepcopy(TOOLS)
        automatic = self.computer.automatic()
        docker = self.computer.settings.get()['computer']['runtime'] == 'docker'
        for tool in tools:
            description = tool['function']['description']
            if docker:
                description = description.replace('在本机运行Shell命令。不是隔离沙箱', '在断网Docker容器的/workspace中运行POSIX命令')
                description = description.replace('在本机Python子进程运行代码。不是隔离沙箱', '在断网Docker容器的/workspace中运行Python')
            if automatic:
                description = description.replace('覆盖前需管理员确认', '按管理员已开启的权限执行')
                description = description.replace('需管理员确认', '按已开启权限执行')
                description = description.replace('必须等待管理员确认命令', '按已开启权限执行')
                description = description.replace('必须等待管理员确认', '按已开启权限执行')
            tool['function']['description'] = description
        return tools

    def _current(self, scope_id, user_id, metadata, payload, before_send):
        if not self.allowed(scope_id, user_id, metadata):
            return False
        if self.computer.settings.get()['computer'] != payload['computer_permissions']:
            return False
        store = self.computer.store
        if not store.message_source_snapshot_current(scope_id, payload['source_snapshot']):
            return False
        try:
            latest = next((item for item in reversed(store.list_messages(scope_id, limit=100))
                           if item['role'] == 'user'), None)
            if latest is None or latest['id'] not in payload['source_ids']:
                return False
        except KeyError:
            return False
        references = payload.get('attachments') or []
        if references and (not self.computer.chat or not self.computer.chat.attachments_current(scope_id, references)):
            return False
        return before_send is None or bool(before_send())

    def guard(self, job):
        if not self.computer.guard(job):
            return False
        payload = job.get('_payload', {})
        if not payload.get('_inline'):
            return True
        entry = self._active.get(payload.get('_inline'))
        if entry is None:
            return False
        def check():
            try:
                return bool(entry['check']())
            except (ValueError, KeyError, PermissionError):
                return False
        if threading.get_ident() == entry['thread']:
            return check()
        # ComputerService._process runs in a worker thread. Chat guards may
        # depend on task-local state, so always evaluate them on the chat loop.
        answer = concurrent.futures.Future()
        def check_on_loop():
            if answer.set_running_or_notify_cancel():
                try:
                    answer.set_result(check())
                except BaseException as exc:
                    answer.set_exception(exc)
        try:
            entry['loop'].call_soon_threadsafe(check_on_loop)
            return bool(answer.result(timeout=5))
        except (RuntimeError, concurrent.futures.TimeoutError):
            answer.cancel()
            return False

    @staticmethod
    def _emit(job):
        from .chat_preview import emit_activity
        emit_activity('job', job)

    async def execute(self, name, arguments, scope_id, user_id, metadata, *, before_send=None):
        if not self.allowed(scope_id, user_id, metadata):
            raise PermissionError('当前本地对话未开启电脑权限')
        spec = next((tool['function']['parameters'] for tool in TOOLS if tool['function']['name'] == name), None)
        if spec is None:
            raise ValueError('未知本地电脑工具')
        try:
            validate(arguments, spec)
        except ValidationError as exc:
            raise ValueError('电脑工具参数格式不正确') from exc
        metadata = copy.deepcopy(metadata)
        sources = list(dict.fromkeys(metadata.get('source_ids') or []))
        if not sources:
            raise PermissionError('电脑操作缺少当前管理员请求来源')
        store = self.computer.store
        try:
            for key in sources:
                source = store.get_message(key)
                if source['scope_id'] != scope_id or source['role'] != 'user' or str(source['user_id']) != str(user_id):
                    raise PermissionError('电脑操作来源不属于当前管理员对话')
            context = list(dict.fromkeys([*sources, *(metadata.get('_context_source_ids') or [])]))
            fingerprints = {key: message_fingerprint(store.get_message(key)) for key in context}
            source_snapshot = store.message_source_snapshot(scope_id, fingerprints)
            if source_snapshot is None:
                raise PermissionError('电脑操作引用已变化')
        except KeyError as exc:
            raise PermissionError('电脑操作来源已失效') from exc
        token = secrets.token_hex(16)
        payload = {'_inline': token, 'tool': name, 'arguments': copy.deepcopy(arguments),
                   'source_ids': sources, 'source_snapshot': source_snapshot,
                   'attachments': copy.deepcopy(metadata.get('reference_attachments') or []),
                   'computer_permissions': copy.deepcopy(self.computer.settings.get()['computer'])}
        check = lambda: self._current(scope_id, str(user_id), metadata, payload, before_send)
        if not check():
            raise PermissionError('当前请求、引用或电脑权限已变化')
        self._active[token] = {'check': check, 'thread': threading.get_ident(), 'loop': asyncio.get_running_loop()}
        job_id, task = None, None
        try:
            job = self.runs.submit('computer', payload, identity=metadata['trusted_identity'], scope_id=scope_id,
                                   user_id=str(user_id), title='本地对话：' + name)
            job_id = job['id']
            self.runs._jobs[job_id]['inline'] = True
            self.runs._save()
            task = self.runs._tasks[job_id]
            last = None
            while True:
                current = self.runs.get(job_id)
                changed = (current['status'], current.get('event_cursor'))
                if changed != last:
                    self._emit(current)
                    last = changed
                if task.done():
                    break
                if not check():
                    raise PermissionError('当前请求、引用或电脑权限已变化，电脑操作已停止')
                await asyncio.wait({task}, timeout=.2)
            if current['status'] in ('cancelled', 'interrupted'):
                raise asyncio.CancelledError()
            if not check():
                raise PermissionError('当前请求、引用或电脑权限已变化，未继续使用操作结果')
            result = current.get('result') or {}
            output = result.get('output')
            succeeded = current['status'] == 'completed' and not (isinstance(output, dict) and output.get('ok') is False)
            return {'job_id': job_id, 'status': current['status'] if succeeded else 'failed', 'ok': succeeded,
                    **result, **({'error': current['error']} if current.get('error') else {})}
        finally:
            if task is not None and not task.done():
                self.runs.cancel(job_id)
                # A cancelled chat must not leave its subprocess running.
                # Keep draining even if the HTTP request is cancelled again.
                while not task.done():
                    try:
                        await asyncio.shield(task)
                    except asyncio.CancelledError:
                        continue
                self._emit(self.runs.get(job_id))
            self._active.pop(token, None)

    def _files(self, root):
        result = {}
        for path in itertools.islice(root.rglob('*'), 1000):
            if not path.is_file() or path.is_symlink():
                continue
            try:
                path = self.computer.path(root, str(path))
                stat = path.stat()
                result[str(path.relative_to(root))] = (stat.st_mtime_ns, stat.st_size)
            except (ValueError, OSError):
                continue
        return result

    async def run(self, ctx, payload):
        if not payload.get('_inline'):
            return await self.computer.run(ctx, payload)
        async with self.computer._slot:
            ctx.check()
            root = self.computer.workspace()
            cfg = self.computer.settings.get()['computer']
            if cfg['runtime'] == 'docker':
                from .sandbox import availability
                ready = await asyncio.to_thread(availability)
                if not ready['available']:
                    raise ValueError(ready['message'])
            ctx.checkpoint(computer_permissions=payload['computer_permissions'])
            writing = payload['tool'] in ('write_file', 'edit_file', 'execute_shell', 'execute_python')
            before = self._files(root) if writing else {}
            ctx.progress(stage='executing', message='执行 ' + payload['tool'])
            output = await self.computer.execute(ctx, root, payload['tool'], payload['arguments'])
            ctx.check()
            artifacts = []
            if writing:
                for relative, stamp in self._files(root).items():
                    if before.get(relative) == stamp or stamp[1] > 8 * 1024 * 1024:
                        continue
                    path = self.computer.path(root, relative)
                    artifacts.append({'name': path.name, 'path': relative, 'bytes': stamp[1],
                                      'sha256': hashlib.sha256(path.read_bytes()).hexdigest()})
                    if len(artifacts) == 20:
                        break
            self.runs._event(ctx.id, 'tool_result', {'tool': payload['tool'], 'result': output})
            return {'tool': payload['tool'], 'output': output, 'artifacts': artifacts, 'workspace': str(root)}
