"""Local conversation organization and read-only QQ history inspection.

Project membership is UI metadata only: it never changes memory sharing,
profile inheritance, tool permissions, or QQ delivery policy.
"""
from __future__ import annotations

import asyncio
import json
from uuid import uuid4

from fastapi import Body, Query, Request

from .backup_archives import drain_task
from .db import utc_now


MAX_PROJECTS = 200
MAX_CONVERSATIONS = 2000
QQ_WHERE = "(id GLOB 'qq:group:*' OR id GLOB 'qq:private:*' OR id GLOB 'qqofficial:group:*' OR id GLOB 'qqofficial:private:*')"
# Summarize inside SQLite so hundreds of sidebar previews cannot load or return
# embedded image payloads, full model output, and attachment metadata at once.
QQ_PREVIEW = """json_object('id',id,'scope_id',scope_id,'user_id',user_id,'role',role,
    'content',substr(json_extract(data,'$.content'),1,300),'created_at',created_at,
    'sent_at',json_extract(data,'$.sent_at'),'received_at',json_extract(data,'$.received_at'),
    'metadata',json_object('image_count',CASE
        WHEN json_type(data,'$.metadata.images')='array' THEN json_array_length(data,'$.metadata.images')
        WHEN json_type(data,'$.images')='array' THEN json_array_length(data,'$.images') ELSE 0 END))"""


def _text(value, field, maximum, *, required=True, multiline=False):
    if not isinstance(value, str):
        raise ValueError(f'{field} 必须为字符串')
    value = value.strip()
    if (required and not value) or len(value) > maximum:
        raise ValueError(f'{field} 必须为 {1 if required else 0}—{maximum} 字')
    if any(ord(char) < 32 and (not multiline or char not in '\n\r\t') for char in value):
        raise ValueError(f'{field} 不能含控制字符')
    return value


def _payload(payload, allowed):
    if not isinstance(payload, dict) or set(payload) - allowed:
        raise ValueError('包含不支持的字段')
    return dict(payload)


def _boolean(value, field):
    if type(value) is not bool:
        raise ValueError(f'{field} 必须为布尔值')
    return value


class ChatWorkspace:
    def __init__(self, store):
        self.store = store

    @staticmethod
    def _metadata(scope):
        value = scope.get('chat_workspace')
        return value if isinstance(value, dict) else {}

    @classmethod
    def _conversation(cls, scope, latest='', projects=None):
        meta = cls._metadata(scope)
        project_id = meta.get('project_id')
        if not isinstance(project_id, str) or (projects is not None and project_id not in projects):
            project_id = None
        return {'id': scope['id'], 'name': scope.get('name') or '新对话',
                'project_id': project_id, 'pinned': meta.get('pinned') is True,
                'archived': meta.get('archived') is True,
                'updated_at': max(str(meta.get('updated_at') or ''), str(latest or ''))}

    def _scope(self, scope_id, *, qq=False):
        if not isinstance(scope_id, str) or len(scope_id) > 300:
            raise ValueError('会话 ID 无效')
        prefixes = ('qq:group:', 'qq:private:', 'qqofficial:group:', 'qqofficial:private:') if qq else ('web:',)
        if not scope_id.startswith(prefixes):
            raise ValueError('仅支持 QQ 会话' if qq else '仅支持本地 Chat 会话')
        # get_scope creates missing scopes; inspection/patching must never do so.
        return self.store._get('scopes', scope_id)

    def _project_id(self, value):
        if value is None:
            return None
        value = _text(value, 'project_id', 100)
        self.store._get('chat_projects', value)
        return value

    def snapshot(self):
        with self.store._lock:
            projects = self.store._list('chat_projects')
            projects.sort(key=lambda row: (row.get('archived') is True, row.get('name', ''), row['id']))
            project_ids = {row['id'] for row in projects}
            rows = self.store._conn.execute("""SELECT s.data,
                (SELECT MAX(created_at) FROM messages WHERE scope_id=s.id) AS latest
                FROM scopes s WHERE s.id GLOB 'web:*'
                ORDER BY COALESCE(latest,json_extract(s.data,'$.chat_workspace.updated_at'),'') DESC,s.id
                LIMIT ?""", (MAX_CONVERSATIONS + 1,)).fetchall()
            conversations = [self._conversation(json.loads(row['data']), row['latest'], project_ids)
                             for row in rows[:MAX_CONVERSATIONS]]
            conversations.sort(key=lambda row: (not row['pinned'], row['archived']))
            return {'projects': projects, 'conversations': conversations,
                    'truncated': len(rows) > MAX_CONVERSATIONS}

    def create_project(self, payload):
        data = _payload(payload, {'name', 'description'})
        name = _text(data.get('name'), 'name', 100)
        description = _text(data.get('description', ''), 'description', 2000, required=False, multiline=True)
        with self.store._lock, self.store._conn:
            if self.store._conn.execute('SELECT COUNT(*) FROM chat_projects').fetchone()[0] >= MAX_PROJECTS:
                raise ValueError(f'项目数量不能超过 {MAX_PROJECTS}')
            now = utc_now()
            return self.store._write('chat_projects', {'id': uuid4().hex, 'name': name,
                'description': description, 'archived': False, 'created_at': now, 'updated_at': now}, ())

    def update_project(self, project_id, payload):
        data = _payload(payload, {'name', 'description', 'archived'})
        if 'name' in data:
            data['name'] = _text(data['name'], 'name', 100)
        if 'description' in data:
            data['description'] = _text(data['description'], 'description', 2000, required=False, multiline=True)
        if 'archived' in data:
            data['archived'] = _boolean(data['archived'], 'archived')
        with self.store._lock, self.store._conn:
            project = self.store._get('chat_projects', project_id)
            return self.store._write('chat_projects', {**project, **data, 'updated_at': utc_now()}, ())

    def delete_project(self, project_id):
        with self.store._lock, self.store._conn:
            self.store._get('chat_projects', project_id)
            rows = self.store._conn.execute("SELECT data FROM scopes WHERE id GLOB 'web:*' "
                "AND json_extract(data,'$.chat_workspace.project_id')=?", (project_id,)).fetchall()
            for row in rows:
                scope = json.loads(row[0])
                scope['chat_workspace'] = {**self._metadata(scope), 'project_id': None, 'updated_at': utc_now()}
                self.store._write('scopes', scope, ())
            self.store._conn.execute('DELETE FROM chat_projects WHERE id=?', (project_id,))
        return {'ok': True}

    def create_conversation(self, payload):
        data = _payload(payload, {'name', 'project_id'})
        name = _text(data.get('name', '新对话'), 'name', 100)
        with self.store._lock, self.store._conn:
            project_id = self._project_id(data.get('project_id'))
            if self.store._conn.execute("SELECT COUNT(*) FROM scopes WHERE id GLOB 'web:*'").fetchone()[0] >= MAX_CONVERSATIONS:
                raise ValueError(f'本地对话数量不能超过 {MAX_CONVERSATIONS}')
            now = utc_now()
            scope = self.store.upsert_scope({'id': 'web:' + uuid4().hex, 'name': name,
                'chat_workspace': {'project_id': project_id, 'pinned': False, 'archived': False,
                                   'created_at': now, 'updated_at': now}})
            return self._conversation(scope)

    def update_conversation(self, scope_id, payload):
        data = _payload(payload, {'name', 'project_id', 'pinned', 'archived'})
        if 'name' in data:
            data['name'] = _text(data['name'], 'name', 100)
        for field in ('pinned', 'archived'):
            if field in data:
                data[field] = _boolean(data[field], field)
        with self.store._lock, self.store._conn:
            scope = self._scope(scope_id)
            if 'project_id' in data:
                data['project_id'] = self._project_id(data['project_id'])
            if 'name' in data:
                scope['name'] = data.pop('name')
            scope['chat_workspace'] = {**self._metadata(scope), **data, 'updated_at': utc_now()}
            # Write the raw record: resolved profile fields must not become
            # accidental per-conversation overrides during UI organization.
            self.store._write('scopes', scope, ())
            return self._conversation(scope)

    def qq_chats(self, limit=200, offset=0):
        with self.store._lock:
            total = self.store._conn.execute(f'SELECT COUNT(*) FROM scopes WHERE {QQ_WHERE}').fetchone()[0]
            # Rank using the indexed latest timestamp first. Only the bounded
            # result page should count histories or load message bodies.
            rows = self.store._conn.execute(f"""WITH page AS MATERIALIZED (
                SELECT id,(SELECT MAX(created_at) FROM messages WHERE scope_id=scopes.id) AS last_at
                FROM scopes WHERE {QQ_WHERE}
                ORDER BY COALESCE(last_at,'') DESC,id LIMIT ? OFFSET ?)
                SELECT s.data,page.last_at,
                (SELECT COUNT(*) FROM messages WHERE scope_id=page.id) AS message_count,
                (SELECT {QQ_PREVIEW} FROM messages WHERE scope_id=page.id ORDER BY created_at DESC,rowid DESC LIMIT 1) AS last_message
                FROM page JOIN scopes s ON s.id=page.id
                ORDER BY COALESCE(page.last_at,'') DESC,page.id""", (limit, offset)).fetchall()
            items = []
            for row in rows:
                scope = json.loads(row['data'])
                resolved = getattr(self.store, 'scope_resolver', lambda value: value)(scope)
                platform, kind, _ = scope['id'].split(':', 2)
                items.append({**resolved, 'kind': kind, 'platform': platform,
                    'last_message': json.loads(row['last_message']) if row['last_message'] else None,
                    'message_count': row['message_count'], 'last_at': row['last_at']})
            return {'items': items, 'total': total, 'has_more': offset + len(items) < total}

    def qq_messages(self, scope_id, *, before=None, after=None, limit=80):
        if before is not None and after is not None:
            raise ValueError('before 与 after 不能同时使用')
        if type(limit) is not int or not 1 <= limit <= 200:
            raise ValueError('limit 必须为 1—200')
        with self.store._lock:
            self._scope(scope_id, qq=True)
            args, where = [scope_id], 'scope_id=?'
            cursor_id = before if before is not None else after
            if cursor_id is not None:
                cursor_id = _text(cursor_id, '消息游标', 100)
                cursor = self.store._conn.execute('SELECT created_at,rowid FROM messages WHERE scope_id=? AND id=?',
                    (scope_id, cursor_id)).fetchone()
                if cursor is None:
                    raise ValueError('消息游标不存在于当前会话')
                op = '<' if before is not None else '>'
                # Give SQLite an index range as well as the rowid tie-breaker.
                # Without this bound every empty poll after a late cursor can
                # scan the entire scope history while holding the Store lock.
                where += f' AND created_at{op}=? AND (created_at{op}? OR (created_at=? AND rowid{op}?))'
                args.extend((cursor[0], cursor[0], cursor[0], cursor[1]))
            direction = 'ASC' if after is not None else 'DESC'
            rows = self.store._conn.execute(f'SELECT data FROM messages WHERE {where} '
                f'ORDER BY created_at {direction},rowid {direction} LIMIT ?', (*args, limit + 1)).fetchall()
            items = [json.loads(row[0]) for row in rows[:limit]]
            if after is None:
                items.reverse()
            return {'items': items, 'has_more': len(rows) > limit,
                    'next_before': items[0]['id'] if items else None,
                    'next_after': items[-1]['id'] if items else None}


def install_chat_workspace(app, store):
    workspace = ChatWorkspace(store)
    app.state.chat_workspace = workspace

    async def run(request, function, *args, **kwargs):
        # The middleware also drains this owned worker before releasing its
        # active-request count, so restore cannot overtake cancelled writes.
        worker = asyncio.create_task(asyncio.to_thread(function, *args, **kwargs))
        request.state.chat_workspace_worker = worker
        try:
            return await asyncio.shield(worker)
        except asyncio.CancelledError:
            await drain_task(worker)
            raise

    @app.get('/api/chat/workspace')
    async def snapshot(request: Request):
        return await run(request, workspace.snapshot)

    @app.post('/api/chat/projects')
    async def create_project(request: Request, payload: dict = Body(...)):
        return await run(request, workspace.create_project, payload)

    @app.patch('/api/chat/projects/{project_id}')
    async def update_project(project_id: str, request: Request, payload: dict = Body(...)):
        return await run(request, workspace.update_project, project_id, payload)

    @app.delete('/api/chat/projects/{project_id}')
    async def delete_project(project_id: str, request: Request):
        return await run(request, workspace.delete_project, project_id)

    @app.post('/api/chat/conversations')
    async def create_conversation(request: Request, payload: dict = Body(default={})):
        return await run(request, workspace.create_conversation, payload)

    @app.patch('/api/chat/conversations/{scope_id}')
    async def update_conversation(scope_id: str, request: Request, payload: dict = Body(...)):
        return await run(request, workspace.update_conversation, scope_id, payload)

    @app.get('/api/qq/chats')
    async def qq_chats(request: Request, limit: int = Query(200, ge=1, le=500), offset: int = Query(0, ge=0, le=100000)):
        return await run(request, workspace.qq_chats, limit, offset)

    @app.get('/api/qq/chats/{scope_id}/messages')
    async def qq_messages(scope_id: str, request: Request, before: str | None = None, after: str | None = None,
                          limit: int = Query(80, ge=1, le=200)):
        return await run(request, workspace.qq_messages, scope_id, before=before, after=after, limit=limit)
