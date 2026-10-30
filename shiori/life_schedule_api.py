"""Authenticated character-life workbench endpoints (no AstrBot bridge)."""
from __future__ import annotations

from fastapi import Body, HTTPException


def install_character_schedule(app, service):
    app.state.character_schedule = service

    def admin():
        access = app.state.access
        if not access.is_admin(access.identity('local', 'shiori')):
            raise HTTPException(403, '当前本地账号没有管理员权限')

    @app.get('/api/character-schedule/state')
    async def state(persona_id: str = '', date: str = ''):
        admin()
        return service.state(persona_id, date)

    @app.post('/api/character-schedule/settings')
    async def settings(payload: dict = Body(...)):
        admin()
        return {'settings': service.save_settings(payload)}

    @app.post('/api/character-schedule/save')
    async def save(payload: dict = Body(...)):
        admin()
        return service.save(payload)

    @app.post('/api/character-schedule/generate')
    async def generate(payload: dict = Body(...)):
        admin()
        return service.submit(payload)

    @app.get('/api/character-schedule/job')
    async def job(id: str):
        admin()
        return service.job(id)

    @app.post('/api/character-schedule/cancel')
    async def cancel(payload: dict = Body(...)):
        admin()
        return service.cancel(str(payload.get('id', '')))

    @app.post('/api/character-schedule/restore')
    async def restore(payload: dict = Body(...)):
        admin()
        return service.restore(payload)

    @app.post('/api/character-schedule/preview')
    async def preview(payload: dict = Body(...)):
        admin()
        return service.preview(payload)

    @app.get('/api/character-schedule/diary')
    async def diary(persona_id: str, date: str, scope_id: str = ''):
        admin()
        return {'diary': service.diary(persona_id, date, scope_id)}

    @app.post('/api/character-schedule/queue')
    async def queue(payload: dict = Body(...)):
        admin()
        scope_id = payload.get('scope_id')
        if not isinstance(scope_id, str) or len(scope_id) > 300:
            raise ValueError('请选择有效会话')
        if payload.get('action') not in ('clear', 'retry'):
            raise ValueError('队列操作须为 clear 或 retry')
        if not service.queue_clear:
            raise ValueError('当前没有可处理的日程留言')
        return service.queue_clear(scope_id, action=payload['action'])
