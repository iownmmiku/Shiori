"""Persistent reminder scheduling with conservative, observable delivery state."""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

from .onebot import _ids, is_quiet, scope_value
from .access import private_allowed, chat_allowed
from .timeparse import next_occurrence


def _datetime(value):
    if value is None:
        return datetime.now(timezone.utc)
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace('Z', '+00:00'))
    elif isinstance(value, (int, float)):
        value = datetime.fromtimestamp(value, timezone.utc)
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


class TaskScheduler:
    def __init__(self, store, settings, bridge):
        self.store, self.settings, self.bridge = store, settings, bridge
        self._task = None
        self._lock = asyncio.Lock()
        self._last_qq_delivery = {}

    async def start(self):
        if self._task and not self._task.done():
            return
        self._task = asyncio.create_task(self._loop())

    async def stop(self):
        if self._task:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None

    async def _loop(self):
        while True:
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.store.add_event('scheduler_error', '提醒调度暂时失败', error=type(exc).__name__)
            await asyncio.sleep(5)

    def _allowed(self, task, scope, now):
        cfg = self.settings.get()
        if cfg.get('paused') or not scope_value(scope, 'enabled', True) or not scope_value(scope, 'allow_tools', True):
            return False
        scope_id = task['scope_id']
        if scope_id.startswith('web:'):
            return True
        parts = scope_id.split(':')
        if len(parts) != 3:
            return False
        platform, kind, target = parts
        if platform == 'qq':
            qq = cfg.get('onebot', {})
            if not qq.get('enabled') or qq.get('adapter', 'napcat') == 'official':
                return False
            allowed = chat_allowed(cfg, 'onebot', kind, target)
        elif platform == 'qqofficial':
            qq = cfg.get('qq_official', {})
            if cfg.get('onebot', {}).get('adapter') != 'official' or not qq.get('enabled'):
                return False
            allowed = chat_allowed(cfg, 'qqofficial', kind, target)
        else:
            return False
        return allowed and kind in ('group', 'private') and not is_quiet(scope, now, settings=cfg) and self.bridge.status().get('connected', False)

    def _finish(self, task, now):
        repeat = task.get('repeat', 'none')
        patch = {'status': 'completed', 'delivery_status': 'delivered', 'result': '提醒已送达', 'error': ''}
        if repeat in ('daily', 'weekly'):
            following, skipped = next_occurrence(task, now, self.settings)
            patch.update({key: following[key] for key in ('due_at', 'timezone', 'wall_time', 'calendar_anchor')})
            patch.update(status='pending', delivery_status='pending', result='上次提醒已送达；等待下次触发')
            if skipped:
                patch['result'] += '；已跳过夏令时切换中不存在的本地钟点'
                self.store.add_event('reminder_calendar_skip', '已跳过时区切换中不存在的提醒钟点', task_id=task['id'], timezone=following['timezone'], skipped=skipped)
        self.store.update_task(task['id'], patch)
        self.store.add_event('reminder_delivered', '提醒已送达', task_id=task['id'], scope_id=task['scope_id'], occurrence_due=task.get('due_at'))

    async def tick(self, now=None):
        now = _datetime(now)
        async with self._lock:
            qq_sent = False
            for listed in self.store.due_tasks(now.isoformat()):
                task = self.store.get_task(listed['id'])
                if task.get('status') == 'running':
                    # The process may have died after an external send but before commit.
                    self.store.update_task(task['id'], {'status': 'failed', 'delivery_status': 'failed', 'error': '上次执行被中断，送达状态未知；请核对后手动重试'})
                    continue
                if task.get('status') != 'pending' or task.get('kind') != 'reminder' or not task.get('due_at'):
                    continue
                try:
                    if _datetime(task['due_at']) > now:
                        continue
                    scope = self.store.get_scope(task['scope_id'])
                except (ValueError, KeyError):
                    self.store.update_task(task['id'], {'status': 'failed', 'delivery_status': 'failed', 'error': '提醒时间或所属会话无效'})
                    continue
                if not self._allowed(task, scope, now):
                    continue
                local = task['scope_id'].startswith('web:')
                if not local and (qq_sent or (now - self._last_qq_delivery.get(task['scope_id'], datetime.min.replace(tzinfo=timezone.utc))).total_seconds() < 60):
                    continue
                # Commit before attempting delivery; unknown outcomes never auto-retry.
                task = self.store.update_task(task['id'], {'status': 'running', 'delivery_status': 'pending', 'error': ''})
                content = '⏰ 提醒：' + task['title']
                try:
                    if local:
                        previous = self.store.list_messages(task['scope_id'], limit=200)
                        already = any((m.get('metadata') or {}).get('task_id') == task['id'] and (m.get('metadata') or {}).get('occurrence_due') == task['due_at'] for m in previous)
                        if not already:
                            self.store.add_message(task['scope_id'], task['user_id'], 'assistant', content, task_id=task['id'], occurrence_due=task['due_at'], reminder=True)
                        delivered = True
                    else:
                        # Reserve the per-group window even when the outcome is unknown.
                        self._last_qq_delivery[task['scope_id']] = now
                        qq_sent = True
                        delivered = await self.bridge.send(task['scope_id'], content)
                    current = self.store.get_task(task['id'])
                    if current.get('status') == 'cancelled':
                        self.store.add_event('reminder_cancelled', '取消请求到达时提醒已尝试发送，请核对送达状态', task_id=task['id'])
                        continue
                    if delivered:
                        self._finish(task, now)
                    else:
                        self.store.update_task(task['id'], {'status': 'failed', 'delivery_status': 'failed', 'error': '发送未确认，可能已送达；请核对后手动重试，避免重复'})
                except asyncio.CancelledError:
                    # Durable running state is deliberately left for recovery.
                    raise
                except Exception as exc:
                    self.store.update_task(task['id'], {'status': 'failed', 'delivery_status': 'failed', 'error': '执行未完成或送达未知，请核对后重试（' + type(exc).__name__ + '）'})
                    self.store.add_event('reminder_failed', '提醒执行失败', task_id=task['id'], error=type(exc).__name__)
