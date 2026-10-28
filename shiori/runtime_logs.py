"""Bounded, redacted runtime diagnostics and message-pipeline events."""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re

from fastapi import Request
from fastapi.responses import Response


def install_logs(app, settings, store, data_dir):
    path = Path(data_dir) / 'runtime.log'

    def redact(text):
        cfg = settings.get()
        secrets = [settings.token, cfg['model'].get('api_key'), cfg['onebot'].get('access_token'), cfg['qq_official'].get('app_secret')]
        secrets += [x.get('api_key') for x in [*cfg.get('providers',[]), *cfg.get('provider_sources',[]), *cfg.get('search_providers',[])]]
        secrets += [x.get('token') for x in cfg.get('mcp_servers',[])]
        value = str(text)
        for secret in secrets:
            if secret and (len(secret) >= 6 or value == secret): value = value.replace(secret, '[已隐藏]')
        value = re.sub(r'(?i)(Bearer\s+)[a-z0-9_\-.+/=]{8,}',r'\1[已隐藏]',value)
        value = re.sub(r'(?i)((?:api[_-]?key|access[_-]?token|password|app[_-]?secret)\s*[=:]\s*)[^\s,;}]+',r'\1[已隐藏]',value)
        return value

    store.exception_redactor = redact

    def snapshot(limit=200, level='', q='', after=''):
        if not 1 <= limit <= 2000 or len(q)>256 or len(after)>160:
            raise ValueError('日志数量须为1—2000，搜索词不超过256字')
        level = level.upper()
        if level not in ('','ALL','DEBUG','INFO','WARNING','WARN','ERROR','CRITICAL'):
            raise ValueError('日志级别无效')
        if level == 'WARN': level = 'WARNING'
        rows = []
        if path.is_file() and not path.is_symlink():
            with path.open('rb') as stream:
                stat = path.stat()
                start = max(0, stat.st_size - 512 * 1024)
                stream.seek(start)
                if start: stream.readline()
                stamp = datetime.fromtimestamp(stat.st_mtime,timezone.utc).isoformat()
                identity = hashlib.sha256(str((stat.st_dev,stat.st_ino)).encode()).hexdigest()[:12]
                for _ in range(4000):
                    offset = stream.tell()
                    raw = stream.readline(32768)
                    if not raw: break
                    text = raw.decode('utf-8',errors='replace').rstrip()
                    match = re.match(r'^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d(?:[,.]\d+)?)\s+(DEBUG|INFO|WARNING|ERROR|CRITICAL)\s+(.*)',text)
                    kind = 'ERROR' if text.startswith(('Traceback','  File ')) else 'INFO'
                    if match:
                        try: stamp = datetime.fromisoformat(match[1].replace(',','.')).astimezone(timezone.utc).isoformat()
                        except ValueError: pass
                        kind, text = match[2], match[3]
                    rows.append({'id':f'r:{identity}:{offset:016d}','time':stamp,'level':kind,'message':redact(text),'source':'runtime'})
        for item in store.list_events(limit=1000):
            details = item.get('details') or {}
            code = str(details.get('reason') or details.get('code') or details.get('status') or '')
            severity = 'ERROR' if any(x in code.lower() for x in ('error','failed')) else 'WARNING' if any(x in code.lower() for x in ('blocked','denied','not_allowed','missing')) else 'INFO'
            rows.append({'id':'e:'+item['id'],'time':item['created_at'],'level':severity,
                         'message':redact(f"[{item['kind']}] {item.get('message','')}" + (' '+json.dumps(details,ensure_ascii=False) if details else '')),'source':'event'})
        rows.sort(key=lambda item:(item['time'],item['id']))
        cursor = rows[-1]['id'] if rows else after
        reset = not bool(after)
        if after:
            found = next((i for i,value in enumerate(rows) if value['id']==after),None)
            if found is not None: rows = rows[found+1:]
            else: reset = True
        needle = q.casefold()
        selected = [value for value in rows if (level in ('','ALL') or value['level']==level)
                    and (not needle or needle in value['message'].casefold())]
        return {'lines':selected[-limit:],'cursor':cursor,'reset':reset,'truncated':len(selected)>limit,
                'available':path.exists() or bool(rows),'message':'包含运行日志和 QQ/模型/Planner 等事件；密钥已脱敏'}

    @app.get('/api/logs')
    async def logs(limit:int=200,level:str='',q:str='',after:str=''):
        return snapshot(limit,level,q,after)

    @app.get('/api/logs/export')
    async def export_logs(level:str='',q:str=''):
        data = snapshot(2000,level,q)
        text = '\n'.join(f"{x['time']} {x['level']} {x['message']}" for x in data['lines'])+'\n'
        return Response(text,media_type='text/plain; charset=utf-8',headers={'Content-Disposition':'attachment; filename="shiori-runtime.log"'})

    app.state.log_snapshot = snapshot
