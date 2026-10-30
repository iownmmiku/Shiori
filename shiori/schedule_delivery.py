"""Durable schedule holds; release through the ordinary conversation flow.

Only message references, fingerprints and hold control fields are retained here. Authentication, attachments,
group authority and text delivery remain owned by the normal chat pipeline.
An attempted dispatch is never replayed automatically after a restart.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import math
import random
import time
from uuid import uuid4

from .db import message_fingerprint


ADAPTIVE_REVIEW_DELAY = 1.0
MAX_ACTIVE_ROWS = 128


class ScheduleDelivery:
    def __init__(self, flow, connected=lambda: True, *, prefix=None):
        self.flow, self.store, self.connected = flow, flow.store, connected
        if prefix not in (None, 'qq:', 'qqofficial:'):
            raise ValueError('日程留言平台前缀无效')
        self.prefix = prefix
        self._task = None
        self._reviews = {}
        self._handoffs = {}
        self._stopping = False
        self.reload()

    def reload(self, *, restored=False):
        """Recreate extension tables after restoring a pre-schedule backup."""
        with self.store._lock:
            self.store._conn.execute('''CREATE TABLE IF NOT EXISTS character_schedule_queue (
                id TEXT PRIMARY KEY, scope_id TEXT NOT NULL, persona_key TEXT NOT NULL,
                status TEXT NOT NULL, payload TEXT NOT NULL, created REAL NOT NULL,
                due REAL NOT NULL, reason TEXT NOT NULL DEFAULT '')''')
            self.store._conn.execute('CREATE INDEX IF NOT EXISTS character_schedule_queue_pending ON character_schedule_queue(status,scope_id,created)')
            sql = "UPDATE character_schedule_queue SET status='uncertain',reason='上次派发期间程序退出，请核对聊天记录；不会自动重复发送' WHERE status='dispatching'"
            self.store._conn.execute(sql + (' AND scope_id LIKE ?' if self.prefix else ''),
                                     (self.prefix + '%',) if self.prefix else ())
            if restored:
                sql = "UPDATE character_schedule_queue SET status='uncertain',reason='留言来自恢复的备份，请核对平台后明确重试；不会自动重放' WHERE status='pending'"
                self.store._conn.execute(sql + (' AND scope_id LIKE ?' if self.prefix else ''),
                                        (self.prefix + '%',) if self.prefix else ())
            self.store._conn.commit()

    @property
    def service(self):
        return getattr(self.flow.chat, 'character_schedule', None)

    def _rows(self, status=None, *, active_only=False):
        with self.store._lock:
            sql = 'SELECT * FROM character_schedule_queue'
            args, clauses = [], []
            if status:
                clauses.append('status=?')
                args.append(status)
            elif active_only:
                clauses.append("status IN ('pending','uncertain','dispatching')")
            if self.prefix:
                clauses.append('scope_id LIKE ?')
                args.append(self.prefix + '%')
            if clauses:
                sql += ' WHERE ' + ' AND '.join(clauses)
            # Active holds are control records: an older unresolved handoff must
            # never hide a newer pending input from release or administrator actions.
            limit = '' if status or active_only else ' LIMIT 500'
            return [dict(row) for row in self.store._conn.execute(sql + ' ORDER BY created,rowid' + limit, args)]

    def _update(self, row_id, status, reason=''):
        with self.store._lock:
            self.store._conn.execute('UPDATE character_schedule_queue SET status=?,reason=? WHERE id=?'
                                     + (' AND scope_id LIKE ?' if self.prefix else ''),
                                     (status, reason, row_id, self.prefix + '%') if self.prefix else (status, reason, row_id))
            self.store._conn.commit()

    def _persona_key(self, scope_id):
        # get_scope() creates unknown scopes; checking a queued identity must
        # never recreate a chat that an administrator has removed.
        with self.store._lock:
            if not self.store._conn.execute('SELECT 1 FROM scopes WHERE id=?', (scope_id,)).fetchone():
                raise KeyError(scope_id)
        scope = self.store.get_scope(scope_id)
        persona = self.store.get_persona(scope.get('persona_id', 'default'))
        return hashlib.sha256(json.dumps(persona, ensure_ascii=False, sort_keys=True).encode()).hexdigest()

    def _expire(self, row, now, expiry):
        """Expire old inputs individually; a recent arrival keeps its own TTL."""
        retained = []
        stamps = []
        items = json.loads(row['payload'])
        for item in items:
            stamp = item.get('held_at', row['created'])
            if not isinstance(stamp, (int, float)) or not math.isfinite(stamp):
                stamp = row['created']
            if now - stamp < expiry:
                retained.append(item)
                stamps.append(stamp)
        if not retained:
            self._update(row['id'], 'expired', '留言超过保留时间；原文仍在聊天记录中')
            return None
        if len(retained) != len(items):
            row = dict(row, payload=json.dumps(retained), created=min(stamps))
            with self.store._lock, self.store._conn:
                self.store._conn.execute('UPDATE character_schedule_queue SET payload=?,created=? WHERE id=?',
                                         (row['payload'], row['created'], row['id']))
        return row

    def _follow_handoff(self, row_id, futures):
        """Keep a handed-off row active until every normal turn confirms delivery."""
        attempt = object()
        self._handoffs[row_id] = attempt

        def receipt(future):
            try:
                return None if future.cancelled() else future.result()
            except Exception:
                return None

        def completed(done):
            try:
                if self._handoffs.get(row_id) is not attempt:
                    return
                results = [receipt(future) for future in futures if future.done()]
                failures = [value for value in results if not isinstance(value, dict)
                            or not value.get('accepted') or value.get('outcome') != 'delivered']
                if failures:
                    failed = failures[0]
                    # Clearing a hold is final even if an older normal-flow
                    # receipt completes later during shutdown.
                    with self.store._lock, self.store._conn:
                        changed = self.store._conn.execute("UPDATE character_schedule_queue SET status='uncertain',reason=? WHERE id=? AND status IN ('submitted','dispatching')",
                            ('正常回复流程未确认完整送达，请核对聊天记录；不会自动重复发送', row_id))
                    if changed.rowcount:
                        self._event('schedule_handoff_interrupted', '日程留言交接后未完成正常处理', queue_id=row_id,
                                    code=failed.get('code', 'interrupted') if isinstance(failed, dict) else 'interrupted')
                elif len(results) == len(futures):
                    with self.store._lock, self.store._conn:
                        self.store._conn.execute("UPDATE character_schedule_queue SET status='submitted',reason=? WHERE id=? AND status='dispatching'",
                            ('正常聊天流程已确认完整送达', row_id))
            except Exception as exc:
                # A shutdown/restore may already have closed or replaced SQLite.
                # Do not turn an optional receipt callback into a replay.
                try:
                    self.store.add_exception('schedule_handoff_receipt_error', '日程留言交接回执未保存', exc, queue_id=row_id)
                except Exception:
                    pass
            finally:
                if all(future.done() for future in futures) and self._handoffs.get(row_id) is attempt:
                    self._handoffs.pop(row_id, None)

        for future in futures:
            future.add_done_callback(completed)

    def status(self):
        result = []
        for row in self._rows(active_only=True):
            if row['status'] not in ('pending', 'uncertain', 'dispatching'):
                continue
            result.append({'id': row['id'], 'scope_id': row['scope_id'], 'status': row['status'],
                           'count': len(json.loads(row['payload'])), 'due_at': row['due'],
                           'created_at': row['created'], 'reason': row['reason']})
        return result

    def clear(self, row_id):
        self._update(str(row_id), 'cancelled', '管理员已清除待回留言')

    def clear_scope(self, scope_id, action='clear'):
        changed = 0
        for row in self._rows(active_only=True):
            if row['scope_id'] == scope_id and row['status'] in ('pending', 'uncertain'):
                self._update(row['id'], 'pending' if action == 'retry' else 'cancelled',
                             '管理员明确要求重试' if action == 'retry' else '管理员已清除待回留言')
                changed += 1
        return {'ok': True, 'changed': changed}

    def cancel_member(self, scope_id, user_id):
        for row in self._rows('pending'):
            if row['scope_id'] != scope_id:
                continue
            retained = []
            for item in json.loads(row['payload']):
                try:
                    if str(self.store.get_message(item['id'])['user_id']) != str(user_id):
                        retained.append(item)
                except KeyError:
                    continue
            with self.store._lock:
                self.store._conn.execute('UPDATE character_schedule_queue SET payload=?,status=? WHERE id=?',
                    (json.dumps(retained), 'pending' if retained else 'cancelled', row['id']))
                self.store._conn.commit()

    def _event(self, kind, message, **details):
        self.store.add_event(kind, message, **details)

    def _schedule_review(self, scope_id, persona_key):
        """Coalesce a burst without making ingress wait for a model request."""
        service = self.service
        running = self._reviews.get(scope_id)
        if (self._stopping or not service or not config_adaptive(service)
                or len(self._reviews) >= 128
                or (running is not None and not running.done())):
            return

        def current():
            if (self._stopping or self.service is not service or not self.connected()
                    or getattr(self.flow, '_closed', False) or self.flow.settings.get().get('paused')
                    or not config_adaptive(service)):
                return False
            try:
                return (self._persona_key(scope_id) == persona_key
                        and self.store.get_scope(scope_id).get('enabled', True))
            except KeyError:
                return False

        async def review():
            try:
                await asyncio.sleep(ADAPTIVE_REVIEW_DELAY)
                if not current():
                    return
                policy = service.contact_policy(scope_id)
                if not policy.get('active') or policy.get('status') != 'busy':
                    return
                rows = [row for row in self._rows('pending')
                        if row['scope_id'] == scope_id and row['persona_key'] == persona_key]
                materials = []
                now = time.time()
                expiry = float(service.get_settings().get('queue_expire_hours', 72)) * 3600
                for row in rows:
                    row = self._expire(row, now, expiry)
                    if row is None:
                        continue
                    for item in json.loads(row['payload']):
                        try:
                            message = self.store.get_message(item['id'])
                        except KeyError:
                            continue
                        if (message['scope_id'] == scope_id and message['role'] == 'user'
                                and message_fingerprint(message) == item['fingerprint']
                                and self.flow.allowed(scope_id, str(message['user_id']))):
                            materials.append((row, item, message))
                if not materials or not current():
                    return
                selected = materials[-8:]

                def eligible():
                    # The service may wait behind another paid request. Recheck
                    # the exact bounded material after that wait and before it
                    # commits a decision; an old text snapshot grants no access.
                    if not current():
                        return False
                    policy = service.contact_policy(scope_id)
                    if not policy.get('active') or policy.get('status') != 'busy':
                        return False
                    expiry = float(service.get_settings().get('queue_expire_hours', 72)) * 3600
                    for row, item, original in selected:
                        with self.store._lock:
                            held = self.store._conn.execute('SELECT status,payload FROM character_schedule_queue WHERE id=?', (row['id'],)).fetchone()
                        stamp = item.get('held_at', row['created'])
                        if not isinstance(stamp, (int, float)) or not math.isfinite(stamp):
                            stamp = row['created']
                        if (held is None or held['status'] != 'pending' or item not in json.loads(held['payload'])
                                or time.time() - stamp >= expiry):
                            return False
                        try:
                            message = self.store.get_message(item['id'])
                        except KeyError:
                            return False
                        if (message_fingerprint(message) != item['fingerprint']
                                or not self.flow.allowed(scope_id, str(original['user_id']))):
                            return False
                    return True

                # Speaker labels preserve attribution, never authorization.
                # The review has no tools; bounded text comes from this chat only.
                combined = '\n'.join(f"[说话人 {str(message['user_id'])[:64]}] {message['content'][:400]}"
                                     for _, _, message in selected)
                await service.review_contact(scope_id, combined, eligible=eligible)
                if current():
                    await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.store.add_exception('schedule_contact_review_error', '日程联系判断未完成，保持原安排', exc,
                                         scope_id=scope_id)
            finally:
                if self._reviews.get(scope_id) is asyncio.current_task():
                    self._reviews.pop(scope_id, None)

        self._reviews[scope_id] = asyncio.create_task(review(), name='shiori.schedule.review')

    async def capture(self, message, *, direct=False, reply_to_bot=False):
        service = self.service
        if not service:
            return False
        scope_id = message['scope_id']
        if self.prefix and not scope_id.startswith(self.prefix):
            return False
        try:
            key = self._persona_key(scope_id)
        except KeyError:
            return False
        policy = service.contact_policy(scope_id)
        waiting = [row for row in self._rows('pending') if row['scope_id'] == scope_id]
        if not policy.get('active') or (policy.get('status') == 'free' and not waiting):
            return False
        config = service.get_settings()
        now = time.time()
        expiry = float(config.get('queue_expire_hours', 72)) * 3600
        for row in waiting:
            if row['persona_key'] != key:
                self._update(row['id'], 'cancelled', '角色已改变，旧留言不会交给新角色')
        waiting = [pruned for row in waiting if row['persona_key'] == key
                   and (pruned := self._expire(row, now, expiry)) is not None]
        if any(item['id'] == message['id'] for row in waiting for item in json.loads(row['payload'])):
            return True
        maximum = int(config.get('queue_limit', 60))
        unresolved = [row for row in self._rows(active_only=True) if row['scope_id'] == scope_id]
        if sum(len(json.loads(row['payload'])) for row in unresolved) >= maximum:
            self._event('schedule_queue_full', '角色待回留言已达到上限，本条保留在聊天记录中', scope_id=scope_id)
            if policy.get('status') == 'busy':
                self._schedule_review(scope_id, key)
            return True
        item = {'id': message['id'], 'fingerprint': message_fingerprint(message),
                'direct': bool(direct), 'reply_to_bot': bool(reply_to_bot), 'held_at': now}
        row = waiting[-1] if waiting and len(json.loads(waiting[-1]['payload'])) < 8 else None
        if row:
            items = json.loads(row['payload'])
            if any(x['id'] == item['id'] for x in items):
                return True
            items.append(item)
            with self.store._lock:
                self.store._conn.execute('UPDATE character_schedule_queue SET payload=? WHERE id=?', (json.dumps(items), row['id']))
                self.store._conn.commit()
        else:
            # Slow contact has a fixed window; new messages do not extend it.
            low = float(policy.get('slow_min_seconds', config.get('slow_min_seconds', 60)))
            high = max(low, float(policy.get('slow_max_seconds', config.get('slow_max_seconds', 180))))
            due = now + random.uniform(low, high) if policy.get('status') == 'slow' else float(policy.get('due_at') or now)
            with self.store._lock:
                count = self.store._conn.execute("SELECT COUNT(*) FROM character_schedule_queue WHERE status IN ('pending','uncertain','dispatching')").fetchone()[0]
                if count >= MAX_ACTIVE_ROWS:
                    self._event('schedule_queue_full', '角色待回队列已达到全局上限，本条保留在聊天记录中', scope_id=scope_id)
                    if policy.get('status') == 'busy':
                        self._schedule_review(scope_id, key)
                    return True
                self.store._conn.execute('INSERT INTO character_schedule_queue VALUES (?,?,?,?,?,?,?,?)',
                    (uuid4().hex, scope_id, key, 'pending', json.dumps([item]), now, due, '等待角色恢复联系'))
                self.store._conn.commit()
        self._event('schedule_message_held', '已暂存日程控制下的留言', scope_id=scope_id, source_id=message['id'], activity=policy.get('title', ''))
        if policy.get('status') == 'busy':
            self._schedule_review(scope_id, key)
        return True

    async def tick(self, now=None):
        now = time.time() if now is None else now
        service = self.service
        if (self._stopping or not service or not self.connected() or getattr(self.flow, '_closed', False)
                or self.flow.settings.get().get('paused')):
            return
        config = service.get_settings()
        seen = set()
        for row in self._rows('pending'):
            scope_id = row['scope_id']
            row = self._expire(row, now, float(config.get('queue_expire_hours', 72)) * 3600)
            if row is None:
                continue
            if scope_id in seen:
                continue
            try:
                if row['persona_key'] != self._persona_key(scope_id):
                    self._update(row['id'], 'cancelled', '已切换或修改人格，旧留言不会交给新角色')
                    continue
            except KeyError:
                self._update(row['id'], 'cancelled', '人格或会话已删除')
                continue
            policy = service.contact_policy(scope_id)
            if self.store.get_scope(scope_id).get('enabled', True) is False:
                self._update(row['id'], 'cancelled', '会话已暂停')
                continue
            if policy.get('active'):
                if policy.get('status') == 'busy':
                    seen.add(scope_id)
                    continue
                if policy.get('status') == 'slow' and row['due'] > now:
                    seen.add(scope_id)
                    continue
            entries = []
            for item in json.loads(row['payload']):
                try:
                    message = self.store.get_message(item['id'])
                    if (message['scope_id'] != scope_id or message['role'] != 'user'
                            or message_fingerprint(message) != item['fingerprint']):
                        continue
                    if not self.flow.allowed(scope_id, str(message['user_id'])):
                        continue
                    entries.append((item, message))
                except KeyError:
                    continue
            if not entries:
                self._update(row['id'], 'cancelled', '留言已删除、已改变或会话权限已撤销')
                continue
            seen.add(scope_id)
            # Save before dispatching: a crash can never auto-repeat an uncertain turn.
            self._update(row['id'], 'dispatching', '正在交给正常聊天流程')
            futures = set()
            accepted = 0
            try:
                for item, message in entries:
                    metadata = dict(message.get('metadata') or {}, schedule_release=True)
                    scope_key = (scope_id, 'group' if ':group:' in scope_id else str(message['user_id']))
                    result = await self.flow._queue_debounce(scope_key, message['content'], metadata.get('images') or [],
                        metadata, message, direct=item['direct'], reply_to_bot=item['reply_to_bot'], wait=False)
                    if not result.get('accepted'):
                        if not accepted and result.get('code') == 'ingress_busy':
                            self._update(row['id'], 'pending', '正常回复队列繁忙，等待下一次交接')
                        else:
                            self._update(row['id'], 'uncertain', '派发未完整接受，请核对聊天记录；不会自动重复发送')
                        break
                    accepted += 1
                    pending = self.flow._debounce_batches.get(scope_key)
                    if pending and pending['future'] not in futures:
                        futures.add(pending['future'])
                else:
                    self._update(row['id'], 'dispatching' if futures else 'uncertain',
                                 '已交给正常聊天流程，等待完整送达确认' if futures else '正常流程未提供交接回执，请核对聊天记录')
                    self._event('schedule_messages_released', '角色留言已交给统一回复流程', scope_id=scope_id,
                                source_ids=[message['id'] for _, message in entries])
            except BaseException:
                self._update(row['id'], 'uncertain', '交接期间中断，请核对聊天记录；不会自动重复发送')
                raise
            finally:
                if futures:
                    self._follow_handoff(row['id'], futures)
        # Keep only recent receipts; pending/unknown entries remain visible.
        with self.store._lock:
            self.store._conn.execute("DELETE FROM character_schedule_queue WHERE status IN ('submitted','cancelled','expired') AND created<?", (now - 7 * 86400,))
            self.store._conn.commit()

    async def start(self):
        self._stopping = False
        if self._task and not self._task.done():
            return
        async def loop():
            while True:
                try:
                    await self.tick()
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    self.store.add_exception('schedule_queue_error', '日程待回留言检查未完成', exc)
                await asyncio.sleep(2)
        self._task = asyncio.create_task(loop(), name='shiori.schedule.delivery')

    async def stop(self):
        self._stopping = True
        reviews = list(self._reviews.values())
        for task in reviews:
            task.cancel()
        if reviews:
            await asyncio.gather(*reviews, return_exceptions=True)
        self._reviews.clear()
        if self._task:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None


def config_adaptive(service):
    return bool(service.get_settings().get('adaptive_enabled', False))
