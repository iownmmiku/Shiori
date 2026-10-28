"""Bounded, authenticated inspection of learning, without loading all records."""
from __future__ import annotations
import asyncio
import json
from datetime import timedelta
from fastapi import Body
from .timezones import local_now, day_bounds


def snapshot(store, settings, kind, scope_id=None):
    table = 'memories' if kind == 'memory' else 'jargon'
    clauses, args = ["status<>'deleted'"], []
    if scope_id:
        store.get_scope(scope_id)
        clauses.append('scope_id=?');args.append(scope_id)
    if table == 'jargon':clauses.append("COALESCE(json_extract(data,'$.entry_type'),'jargon')='jargon'")
    where=' AND '.join(clauses)
    with store._lock:
        counts=dict(store._conn.execute(f'SELECT status,COUNT(*) FROM {table} WHERE {where} GROUP BY status',args).fetchall())
        scopes=[{'scope_id':r[0],'count':r[1]} for r in store._conn.execute(f'SELECT scope_id,COUNT(*) FROM {table} WHERE {where} GROUP BY scope_id ORDER BY COUNT(*) DESC LIMIT 30',args)]
        recent=[json.loads(r[0]) for r in store._conn.execute(f'SELECT data FROM {table} WHERE {where} ORDER BY updated_at DESC LIMIT 8',args)]
        trend=[]
        for offset in range(6,-1,-1):
            moment=local_now(settings)-timedelta(days=offset);start,end=day_bounds(settings,now=moment)
            count=store._conn.execute(f'SELECT COUNT(*) FROM {table} WHERE {where} AND created_at>=? AND created_at<?',[*args,start,end]).fetchone()[0]
            trend.append({'day':moment.strftime('%m-%d'),'count':count})
        cursors=[]
        if table=='memories':
            rows=store._conn.execute("SELECT data FROM events WHERE kind='memory_cursor' ORDER BY created_at DESC LIMIT 32").fetchall()
            cursors=[json.loads(r[0])['details'] for r in rows]
            cursors=[r for r in cursors if not scope_id or r.get('scope_id')==scope_id]
    return {'counts':counts,'total':sum(counts.values()),'scopes':scopes,'recent':recent,'trend':trend,
            'progress':cursors,'policy':settings.get()['memory' if table=='memories' else 'language']}


def install_learning_dashboard(app, store, settings):
    @app.get('/api/learning/dashboard')
    async def dashboard(kind:str='memory',scope_id:str|None=None):
        if kind not in ('memory','language'):raise ValueError('学习类型无效')
        return await asyncio.to_thread(snapshot,store,settings,kind,scope_id)

    @app.put('/api/learning/policy')
    async def policy(payload:dict=Body(...)):
        if set(payload)-{'memory','language'}:raise ValueError('只能修改记忆与黑话策略')
        settings.update(payload)
        return {'ok':True}
