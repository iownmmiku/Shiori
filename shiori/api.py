from __future__ import annotations

import asyncio
import contextlib
import hashlib
import hmac
import io
import json
import os
import re
import sqlite3
import tempfile
import time
import zipfile
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import Body, FastAPI, File, Form, HTTPException, Request, UploadFile, WebSocket
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from . import __version__
from .chat import ChatService
from .db import Store
from .provider import Provider
from .settings import Settings, resolve_model, DEFAULTS
from .search import SearchError
from .tools import calculate, read_public


def create_app(data_dir=None, *, start_workers=True, allow_remote=False):
    from .data_paths import resolve_data_dir
    data_dir = resolve_data_dir(data_dir or os.environ.get('SHIORI_DATA_DIR') or os.environ.get('MIKUBOT_DATA_DIR') or os.environ.get('QUNYU_DATA_DIR'))
    data_dir.mkdir(parents=True, exist_ok=True)
    settings = Settings(data_dir)
    store = Store(data_dir)
    from .profiles import install_profiles
    provider = Provider(settings, store)
    chat = ChatService(store, settings, provider)
    from .access import manager_for
    chat.access = manager_for(settings, chat)
    from .onebot import OneBotBridge
    from .official import OfficialBridge
    from .scheduler import TaskScheduler
    onebot = OneBotBridge(store, settings, chat)
    official = OfficialBridge(store, settings, chat)
    chat.character_schedule.search = chat.search.search
    schedule_queues = [bridge._flow.schedule_delivery for bridge in (onebot, official)
                       if getattr(bridge._flow, 'schedule_delivery', None)]
    chat.character_schedule.queue_status = lambda: [row for queue in schedule_queues for row in queue.status()]
    def manage_schedule_queue(scope_id, action='clear'):
        results = [queue.clear_scope(scope_id, action=action) for queue in schedule_queues]
        return {'ok': True, 'changed': sum(result['changed'] for result in results)}
    chat.character_schedule.queue_clear = manage_schedule_queue

    class Router:
        def status(self):
            return official.status() if settings.get()['onebot'].get('adapter') == 'official' else onebot.status()
        async def send(self, scope_id, text):
            if scope_id.startswith('qqofficial:'):
                return await official.send(scope_id, text)
            return await onebot.send(scope_id, text)

    router = Router()
    scheduler = TaskScheduler(store, settings, router)

    @asynccontextmanager
    async def lifespan(app):
        async def start_when_ready():
            while app.state.updates.startup_pending():
                provider._updating = True
                await asyncio.sleep(.25)
            provider._updating = False
            await reconnect()
            await scheduler.start()
            await chat.knowledge.start()
            await chat.memory_lifecycle.start()
            await chat.character_schedule.start()
        starter = asyncio.create_task(start_when_ready()) if start_workers else None
        yield
        if starter:
            starter.cancel()
            with contextlib.suppress(asyncio.CancelledError): await starter
        if app.state.backup_restore is not None:
            app.state.backup_restore['cancel'].set()
            await drain_task(app.state.backup_restore['task'])
        await app.state.backup_exports.close()
        await app.state.updates.close()
        await app.state.runs.close()
        await scheduler.stop()
        await onebot.stop()
        await official.stop()
        await chat.close()
        store.close()

    app = FastAPI(title='Shiori', version=__version__, lifespan=lifespan, docs_url=None, redoc_url=None)
    install_profiles(app, store, settings)
    from .chat_workspace import install_chat_workspace
    install_chat_workspace(app, store)
    app.state.store, app.state.settings, app.state.chat = store, settings, chat
    app.state.search = chat.search
    app.state.provider, app.state.bridge, app.state.scheduler = provider, router, scheduler
    app.state.onebot, app.state.official = onebot, official
    from .learning_dashboard import install_learning_dashboard
    from .delivery import install_delivery
    install_learning_dashboard(app,store,settings)
    install_delivery(app,store,onebot,official)
    from .work_limits import WorkLimiter, WorkBusy
    inbound = WorkLimiter(settings, 'request', 8, 16)
    parsing = WorkLimiter(settings, 'parsing', 1, 4)
    app.state.parsing_slot = parsing.slot
    app.state.resource_limits = lambda: {'requests': inbound.status(), 'parsing': parsing.status()}
    from .statistics import install_statistics
    install_statistics(app,store,settings,onebot,official)
    app.state.shutdown = None
    app.state.backup_restore = None
    from .auth_api import install_auth, local_peer, token_matches
    auth_http = install_auth(app, settings, data_dir, allow_remote=allow_remote)
    from .life_schedule_api import install_character_schedule
    install_character_schedule(app, chat.character_schedule)
    from .catalog_api import install_catalog
    install_catalog(app, settings, provider, store)
    from .stickers_api import install_stickers
    install_stickers(app,chat)
    from .runtime_logs import install_logs
    install_logs(app, settings, store, data_dir)
    request_gate = asyncio.Condition()
    active_requests = 0
    maintenance = False

    async def reconnect():
        await onebot.stop()
        await official.stop()
        cfg = settings.get()
        if cfg['onebot'].get('adapter') == 'official':
            if cfg['qq_official'].get('enabled'):
                await official.start()
        elif cfg['onebot'].get('enabled'):
            await onebot.start()

    from .experience_api import install_experience
    install_experience(app, store, settings, provider, chat, router, data_dir, reconnect)
    from .plugins import install_plugins
    install_plugins(app, data_dir, chat)
    from .updates import install_updates
    install_updates(app, data_dir)

    @app.middleware('http')
    async def secure_local(request, call_next):
        # Authentication/stop endpoints and long-lived event streams must
        # remain available while regular API work is saturated.
        bounded = request.url.path.startswith('/api/') and not request.url.path.startswith('/api/auth/') and request.url.path not in (
            '/api/session', '/api/shutdown') and not request.url.path.endswith('/stream')
        try:
            if bounded and auth_http.authenticated(request):
                async with inbound.slot():
                    return await secure_local_inner(request, call_next)
            return await secure_local_inner(request, call_next)
        except WorkBusy as exc:
            return JSONResponse({'detail': str(exc), 'code': 'resource_busy'}, status_code=503)

    async def secure_local_inner(request, call_next):
        nonlocal active_requests, maintenance
        if not auth_http.allowed_host(request):
            return JSONResponse({'detail': '访问主机未获允许；远程域名请在本机配置'}, status_code=403)
        remote = not local_peer(request)
        if remote and not auth_http.allow_remote:
            return JSONResponse({'detail': '远程管理尚未开启，请在运行电脑上配置'}, status_code=403)
        if remote and settings.get()['webui']['require_https'] and not auth_http.secure_transport(request):
            return JSONResponse({'detail':'远程入口要求HTTPS。请使用配置的HTTPS域名或加密隧道；只有在运行电脑上明确允许可信局域网HTTP后才可使用明文入口。'},status_code=403)
        try:
            size = int(request.headers.get('content-length', '0') or 0)
        except ValueError:
            return JSONResponse({'detail': '请求长度格式错误'}, status_code=400)
        from .backup_archives import MAX_ARCHIVE_BYTES
        maximum = 8192 if request.url.path.startswith('/api/auth/') or request.url.path == '/api/session' else settings.get()['scheduler'].get('request_max_mb', 32) * 1024 * 1024
        if request.url.path == '/api/backup/restore':
            maximum = MAX_ARCHIVE_BYTES + 1024 * 1024  # multipart envelope
        if size > maximum:
            return JSONResponse({'detail': '请求内容超过大小限制'}, status_code=413)
        if request.url.path.startswith('/api/'):
            origin = request.headers.get('origin')
            if origin and not auth_http.origin_allowed(request, origin):
                return JSONResponse({'detail': '拒绝跨站请求'}, status_code=403)
            if request.headers.get('sec-fetch-site') == 'cross-site':
                return JSONResponse({'detail': '拒绝跨站请求'}, status_code=403)
            public_auth = {'/api/session','/api/auth/status','/api/auth/bootstrap','/api/auth/setup','/api/auth/login','/api/auth/logout'}
            if request.url.path not in public_auth:
                if remote and not auth_http.remote_ready():
                    return JSONResponse({'detail': '远程访问需要先在本机设置密码'}, status_code=403)
                if not auth_http.authenticated(request):
                    return JSONResponse({'detail': '请登录 Shiori；登录可能已失效', 'code':'authentication_required'}, status_code=401)
            updating = getattr(provider, '_updating', False) or app.state.updates.startup_pending()
            if updating and not (request.url.path.startswith('/api/updates') or request.url.path in ('/api/auth/status','/api/auth/login','/api/auth/logout','/api/shutdown')):
                return JSONResponse({'detail':'正在切换版本并检查启动状态，请稍后重试','code':'update_in_progress'},status_code=503)
            if request.method in ('POST','PUT','PATCH'):
                if request.url.path == '/api/backup/restore':
                    # Let multipart UploadFile spool to disk. Keep a receive cap
                    # even when a proxy omits or understates Content-Length.
                    receive, received = request._receive, 0
                    async def bounded_receive():
                        nonlocal received
                        message = await receive()
                        received += len(message.get('body', b''))
                        if received > maximum:
                            raise HTTPException(413, '请求体超过大小限制')
                        return message
                    request._receive = bounded_receive
                else:
                    raw = bytearray()
                    async for chunk in request.stream():
                        raw.extend(chunk)
                        if len(raw) > maximum:
                            return JSONResponse({'detail': '请求体超过大小限制'}, status_code=413)
                    request._body = bytes(raw)
        is_api = request.url.path.startswith('/api/')
        exclusive = request.url.path == '/api/backup/restore'
        if is_api:
            async with request_gate:
                if maintenance:
                    return JSONResponse({'detail': '正在恢复备份，请稍后重试'}, status_code=503)
                if exclusive:
                    maintenance = True
                    try:
                        await asyncio.wait_for(request_gate.wait_for(lambda: active_requests == 0), 35)
                    except (TimeoutError, asyncio.CancelledError):
                        maintenance = False
                        request_gate.notify_all()
                        return JSONResponse({'detail': '仍有请求执行中，请完成或停止后再恢复备份'}, status_code=503)
                else:
                    active_requests += 1
        try:
            result = await call_next(request)
        except asyncio.CancelledError:
            if exclusive and app.state.backup_restore is not None:
                app.state.backup_restore['cancel'].set()
            raise
        finally:
            if is_api:
                import anyio
                with anyio.CancelScope(shield=True):
                    workspace_worker = getattr(request.state, 'chat_workspace_worker', None)
                    if workspace_worker is not None:
                        await drain_task(workspace_worker)
                    # call_next can be cancelled before its endpoint finishes.
                    # Keep maintenance until every restore thread is drained,
                    # and the commit or rollback has really completed.
                    if exclusive and app.state.backup_restore is not None:
                        await drain_task(app.state.backup_restore['task'])
                    async with request_gate:
                        if exclusive:
                            maintenance = False
                        else:
                            active_requests -= 1
                        request_gate.notify_all()
        result.headers['X-Content-Type-Options'] = 'nosniff'
        result.headers['Referrer-Policy'] = 'no-referrer'
        result.headers['Content-Security-Policy'] = "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; connect-src 'self'; object-src 'none'; base-uri 'self'; frame-ancestors 'none'"
        if request.url.path.startswith('/api/'):
            result.headers['Cache-Control'] = 'no-store'
        elif request.url.path.startswith('/static/'):
            result.headers['Cache-Control'] = 'no-cache, max-age=0, must-revalidate'
        return result

    @app.exception_handler(WorkBusy)
    async def resources_busy(request, exc):
        return JSONResponse({'detail':str(exc), 'code':'resource_busy'}, status_code=503)

    @app.exception_handler(ValueError)
    async def bad_value(request, exc):
        return JSONResponse({'detail': str(exc)}, status_code=400)

    @app.exception_handler(SearchError)
    async def bad_search(request, exc):
        return JSONResponse({'detail': str(exc), 'code': exc.code}, status_code=400)

    @app.exception_handler(KeyError)
    async def missing(request, exc):
        return JSONResponse({'detail': '记录不存在或已被删除'}, status_code=404)

    @app.exception_handler(PermissionError)
    async def permission_denied(request,exc):
        return JSONResponse({'detail':'当前会话或用户无权执行此操作'},status_code=403)

    @app.post('/api/session')
    async def session(request: Request, payload: dict = Body(...)):
        value = str(payload.get('token', ''))
        if not local_peer(request) or not token_matches(value, settings.token):
            raise HTTPException(401, '本地管理凭据不正确')
        from .auth_api import BOOTSTRAP_COOKIE
        response = JSONResponse({'ok': True})
        response.set_cookie(BOOTSTRAP_COOKIE, settings.token, httponly=True, samesite='strict', max_age=1800, path='/api/auth')
        return response

    @app.get('/healthz')
    async def health():
        return {'name': 'Shiori', 'version': __version__, 'identity': hashlib.sha256(settings.token.encode()).hexdigest()[:16]}

    @app.get('/api/bootstrap')
    async def bootstrap(request: Request):
        cfg = settings.public()
        model = resolve_model(settings.get(), 'chat')
        return {'version': __version__, 'name': 'Shiori', 'listener': auth_http.binding(request), 'setup_required': not (model.get('base_url') and model.get('model')),
                'settings': cfg, 'stats': store.stats(), 'scopes': store.list_scopes(), 'personas': store.list_personas(), 'qq': router.status(),
        'capabilities': {'text': bool(model.get('base_url') and model.get('model')), 'vision': model.get('vision', False), 'voice': False,
                                 'memory': True, 'dictionary': True, 'reminders': True, 'embedding': bool(cfg['memory']['semantic_enabled']),
                                 'memory_review': True, 'knowledge': True, 'persona_knowledge': True, 'memory_spaces': True, 'planner': True, 'output_segments': True,
                                 'social': True, 'model_pool': True, 'context_digest': True, 'expressions': True, 'diagnostics': True, 'documents': ['txt', 'md', 'csv', 'json', 'pdf', 'docx'],
                                 'qq_adapters': ['napcat', 'snowluma', 'official']}, 'warning': settings.warning}

    @app.get('/api/settings')
    async def get_settings():
        return settings.public()

    @app.get('/api/system')
    async def system_status():
        from .system import autostart_status
        return autostart_status()

    @app.put('/api/system')
    async def system_settings(payload: dict = Body(...)):
        from .system import set_autostart
        if type(payload.get('autostart')) is not bool:
            raise ValueError('开机启动设置须为布尔值')
        return set_autostart(payload['autostart'], data_dir)

    @app.put('/api/settings')
    async def put_settings(request: Request, payload: dict = Body(...)):
        if 'updates' in payload and not local_peer(request):
            raise HTTPException(403,'远程更新授权只能在运行电脑上调整')
        if 'webui' in payload and not isinstance(payload['webui'], dict):
            raise ValueError('WebUI设置须为对象')
        if (payload.get('webui') or {}).get('remote_enabled') is True and auth_http.auth.status()['mode'] != 'password':
            raise ValueError('开启远程管理前请先在本机设置密码')
        result = settings.update(payload)
        if (payload.get('webui') or {}).get('remote_enabled') is False:
            auth_http.allow_remote = False
        if 'onebot' in payload or 'qq_official' in payload:
            await reconnect()
        store.add_event('settings', '设置已保存')
        return result

    @app.get('/api/models')
    async def models(provider_id: str = ''):
        cfg = resolve_model(settings.get(), model_id=provider_id) if provider_id else None
        if provider_id and not cfg.get('enabled', True):
            raise ValueError('模型或供应商已停用或不存在')
        return await provider.list_models(cfg)

    @app.post('/api/models')
    async def models_from_form(payload: dict = Body(default={})):
        provider_id = payload.get('provider_id')
        cfg = resolve_model(settings.get(), model_id=provider_id) if provider_id else dict(settings.get()['model'])
        if not cfg.get('enabled', True):
            raise ValueError('模型或供应商已停用或不存在')
        for key in ('base_url','model','api_key'):
            if key in payload and payload[key] not in ('',None):
                cfg[key] = payload[key]
        Settings._validate_model_url(cfg['base_url'])
        return await provider.list_models(cfg)

    @app.get('/api/providers/status')
    async def providers_status(): return provider.status()

    @app.get('/api/mcp/status')
    async def mcp_status(): return chat.tool_actions.status()

    @app.post('/api/mcp/discover')
    async def mcp_discover(payload: dict = Body(...)):
        return await chat.tool_actions.discover(str(payload.get('server_id','')))

    @app.get('/api/tool-runs')
    async def tool_runs(scope_id: str | None = None, status: str | None = None):
        return chat.tool_actions.list(scope_id,status)

    @app.post('/api/tool-runs/resume')
    async def resume_tools(): return chat.tool_actions.resume()

    @app.post('/api/tool-runs/{id}/approve')
    async def approve_tool_run(id: str):
        item=await chat.tool_actions.approve(id)
        try:
            job=app.state.resume_tool_result(item)
            return dict(item,job=job) if job else item
        except (ValueError,KeyError,PermissionError):
            return dict(item,continuation_message='工具实际回执已保存；原请求或权限已变化，未继续生成答复。')

    @app.post('/api/tool-runs/{id}/reject')
    async def reject_tool_run(id: str): return chat.tool_actions.reject(id)

    @app.get('/api/diagnostics')
    async def diagnostics(scope_id: str = 'web:default'):
        from .diagnostics import build_diagnostics
        return {**build_diagnostics(store, settings, provider, router), 'social': chat.social.status(scope_id),
                'persona_knowledge':chat.knowledge.status(), 'tools':chat.tool_actions.status()}

    @app.post('/api/model/test')
    async def model_test(payload: dict = Body(default={})):
        config = settings.get()
        provider_id = payload.get('provider_id')
        cfg = resolve_model(config, model_id=provider_id or 'default')
        if not cfg.get('enabled', True):
            raise ValueError('模型或供应商已停用，请先检查模型供应商配置')
        for key in ('base_url', 'model', 'api_key', 'vision', 'temperature', 'timeout_seconds'):
            if key in payload and payload[key] not in ('', None):
                cfg[key] = payload[key]
        started = time.monotonic()
        result = await provider.complete([{'role': 'user', 'content': '请只回复：连接成功'}], config=cfg, purpose='connection_test', max_tokens=40)
        return {'ok': True, 'reply': result['content'], 'latency_ms': round((time.monotonic() - started) * 1000), 'capabilities': {'text': True, 'vision_configured': bool(cfg.get('vision')), 'tools': '通过聊天工具任务验证'}, 'usage': result['usage']}

    def scope_payload(data, id=None):
        data = dict(data)
        if not id and not data.get('id'):
            data.setdefault('always_reply',True)
            data.setdefault('plan_enabled',False)
            data.setdefault('activity','quiet')
            data.pop('kind',None)
            data.pop('private_reply_policy',None)
        data['id'] = id or str(data.get('id') or 'web:' + __import__('uuid').uuid4().hex[:12])
        if not re.fullmatch(r'(web|qq|qqofficial):[\w:.-]{1,180}', data['id']):
            raise ValueError('对话 ID 格式不正确')
        for key in ('trigger_at', 'trigger_reply', 'trigger_name', 'plan_enabled', 'proactive_reply', 'debounce_enabled'):
            if key in data and data[key] is not None and type(data[key]) is not bool:
                raise ValueError('继承开关须为布尔值或空值')
        for key in ('enabled', 'dictionary_learn', 'dictionary_understand', 'dictionary_use', 'expression_learn', 'expression_use', 'always_reply', 'context_compress', 'auto_memory', 'allow_tools', 'allow_mcp', 'semantic_gate', 'use_persona_knowledge'):
            if key in data:
                if type(data[key]) is not bool:
                    raise ValueError('会话开关须为布尔值')
        if data.get('activity', 'balanced') not in ('quiet', 'balanced', 'active'):
            raise ValueError('活跃度设置不正确')
        for key in ('quiet_start', 'quiet_end'):
            value = data.get(key, '')
            if value and not re.fullmatch(r'(?:[01]\d|2[0-3]):[0-5]\d', value):
                raise ValueError('静默时间需采用 HH:MM 格式')
        if 'daily_tokens' in data:
            data['daily_tokens'] = max(0, min(10000000, int(data['daily_tokens'] or 0)))
        for key, minimum, maximum in (('cooldown_seconds', 0, 600), ('merge_window_ms', 0, 2000), ('followup_seconds', 0, 600)):
            if key in data:
                try:
                    value = float(data[key])
                    if not __import__('math').isfinite(value) or not minimum <= value <= maximum:
                        raise ValueError()
                except (ValueError, TypeError) as exc:
                    raise ValueError(f'{key} 超出支持范围 {minimum}—{maximum}') from exc
                data[key] = value
        for key, low, high in (('debounce_seconds', .1, 60), ('debounce_max_wait_seconds', 1, 300)):
            if key in data and data[key] is not None:
                value = data[key]
                if type(value) not in (int, float) or not __import__('math').isfinite(value) or not low <= value <= high:
                    raise ValueError(f'{key} 须为空值或 {low}—{high} 秒')
        if (data.get('debounce_seconds') is not None and data.get('debounce_max_wait_seconds') is not None
                and data['debounce_seconds'] > data['debounce_max_wait_seconds']):
            raise ValueError('消息防抖最长等待时间不能少于等待秒数')
        if 'bot_aliases' in data:
            if data['bot_aliases'] is not None:
                if not isinstance(data['bot_aliases'], list) or len(data['bot_aliases']) > 12 or any(not isinstance(x, str) or not 1 <= len(x.strip()) <= 30 for x in data['bot_aliases']):
                    raise ValueError('机器人别名最多 12 个，每个 1—30 字')
                data['bot_aliases'] = list(dict.fromkeys(x.strip() for x in data['bot_aliases']))
        if 'bot_name' in data and data['bot_name'] is not None:
            if not isinstance(data['bot_name'], str) or len(data['bot_name'].strip()) > 40:
                raise ValueError('机器人名称不超过 40 字')
            data['bot_name'] = data['bot_name'].strip()
        if 'memory_space' in data:
            if not isinstance(data['memory_space'], str) or len(data['memory_space'].strip()) > 80 or re.search(r'[\x00-\x1f]', data['memory_space']):
                raise ValueError('共享空间名称不超过 80 字，不能含控制字符')
            data['memory_space'] = data['memory_space'].strip()
        if 'persona_id' in data:
            store.get_persona(data['persona_id'])
        if 'model_id' in data:
            selected = data['model_id']
            if not isinstance(selected, str) or len(selected) > 64:
                raise ValueError('会话模型 ID 格式不正确')
            if selected and not resolve_model(settings.get(), model_id=selected).get('enabled', True):
                raise ValueError('请选择已启用的模型及供应商')
            if selected and resolve_model(settings.get(), model_id=selected).get('model_type') == 'embedding':
                raise ValueError('嵌入模型仅用于检索，请为会话选择对话模型')
        for key,low,high in (('planner_min_interval_ms',0,60000),('planner_daily_calls',0,100000),('planner_daily_tokens',0,10000000),('planner_max_age_ms',1000,120000)):
            if key in data and data[key] is not None and (type(data[key]) is not int or not low<=data[key]<=high):
                raise ValueError(f'{key}须为空值或{low}—{high}的整数')
        return data

    @app.get('/api/presets')
    async def presets():
        from .presets import list_presets
        return list_presets()

    @app.post('/api/scopes/preset')
    async def apply_preset(payload:dict=Body(...)):
        from .presets import preset_patch
        scope_id=str(payload.get('scope_id','web:default'))
        store.get_scope(scope_id)
        return store.upsert_scope(scope_payload(preset_patch(str(payload.get('preset_id','')),scope_id),scope_id))

    @app.get('/api/scopes')
    async def scopes(): return store.list_scopes()

    @app.post('/api/scopes/{id}/inherit')
    async def inherit_scope(id: str):
        return store.inherit_scope(id)

    @app.post('/api/scopes')
    async def new_scope(payload: dict = Body(...)): return store.upsert_scope(scope_payload(payload))

    @app.put('/api/scopes/{id:path}')
    async def edit_scope(id: str, payload: dict = Body(...)): return store.upsert_scope(scope_payload(payload, id))

    @app.get('/api/personas')
    async def personas(): return store.list_personas()

    def persona_payload(payload, id=None):
        data = dict(store.get_persona(id), **payload) if id else dict(payload)
        if id and isinstance(payload.get('exploration'),dict):
            data['exploration']=dict(store.get_persona(id).get('exploration') or {},**payload['exploration'])
        if id: data['id'] = id
        if not str(data.get('name', '')).strip() or len(str(data.get('system_prompt', ''))) > 6000:
            raise ValueError('人格名称必填，设定不超过 6000 字')
        if 'interests' in data:
            if not isinstance(data['interests'], list) or len(data['interests']) > 12 or any(not isinstance(x, str) or not 1 <= len(x.strip()) <= 80 for x in data['interests']):
                raise ValueError('兴趣最多 12 项，每项 1—80 字')
            data['interests'] = list(dict.fromkeys(x.strip() for x in data['interests']))
        if 'profile' in data:
            profile=data['profile']
            if not isinstance(profile,dict) or set(profile)-{'residence','occupation','age','birthday','routine','background','relationship'}:
                raise ValueError('人格资料字段无效')
            if any(not isinstance(value,str) or len(value)>500 for value in profile.values()) or sum(map(len,profile.values()))>2000:
                raise ValueError('人格资料每栏最多500字，合计最多2000字')
            data['profile']={key:value.strip() for key,value in profile.items() if value.strip()}
        if 'exploration_enabled' in data and type(data['exploration_enabled']) is not bool:
            raise ValueError('定期探索开关须为布尔值')
        if 'excluded_topics' in data:
            values=data['excluded_topics']
            if not isinstance(values,list) or len(values)>30 or any(not isinstance(x,str) or not 1<=len(x.strip())<=80 for x in values):
                raise ValueError('排除话题最多30项，每项1—80字')
            data['excluded_topics']=list(dict.fromkeys(x.strip() for x in values))
        if 'exploration' in data:
            config=data['exploration']
            if not isinstance(config,dict) or set(config)-{'interval_hours','daily_searches','valid_days'}: raise ValueError('人格探索配置无效')
            for key,low,high in (('interval_hours',1,720),('daily_searches',0,100),('valid_days',1,365)):
                value=config.get(key)
                if value is not None and (type(value) is not int or not low<=value<=high): raise ValueError(f'{key}须为空值或{low}—{high}的整数')
        return data

    @app.post('/api/personas')
    async def new_persona(payload: dict = Body(...)): return store.save_persona(persona_payload(payload))

    @app.put('/api/personas/{id}')
    async def edit_persona(id: str, payload: dict = Body(...)): return store.save_persona(persona_payload(payload, id))

    @app.delete('/api/personas/{id}')
    async def delete_persona(id: str):
        store.get_persona(id)
        remaining = [p for p in store.list_personas() if p['id'] != id]
        replacement = next((p['id'] for p in remaining if p['id'] == 'default'), remaining[0]['id'] if remaining else '')
        profiles = settings.get()['bot_profiles']
        for profile in profiles['items']:
            if profile['values'].get('persona_id') == id:
                profile['values']['persona_id'] = replacement
        settings.update({'bot_profiles': profiles})
        store.delete_persona(id)
        return {'ok': True}

    @app.get('/api/knowledge/status')
    async def knowledge_status(): return chat.knowledge.status()

    @app.get('/api/knowledge')
    async def knowledge_list(persona_id: str = 'default', status: str = 'active'):
        if status not in ('active', 'pending', 'rejected', 'deleted', ''): raise ValueError('知识状态无效')
        return chat.knowledge.list(persona_id, status)

    @app.post('/api/knowledge/explore')
    async def explore_knowledge(payload: dict = Body(...)):
        return await chat.knowledge.explore(str(payload.get('persona_id', 'default')), manual=True,urls=payload.get('urls'))

    @app.get('/api/knowledge/sources')
    async def knowledge_sources(persona_id:str='default'):
        store.get_persona(persona_id)
        return chat.knowledge.sources(persona_id)

    @app.put('/api/knowledge/sources')
    async def knowledge_sources_save(payload:dict=Body(...)):
        persona_id=str(payload.get('persona_id','default')); store.get_persona(persona_id)
        return chat.knowledge.configure_sources(persona_id,payload.get('rules',{}))

    @app.put('/api/knowledge/{id}')
    async def knowledge_edit(id:str,payload:dict=Body(...)): return chat.knowledge.edit(id,payload)

    @app.post('/api/knowledge/{id}/approve')
    async def knowledge_approve(id:str,payload:dict=Body(default={})):
        accept=payload.get('accept_conflicts',False)
        if type(accept) is not bool: raise ValueError('冲突确认须为布尔值')
        return chat.knowledge.approve(id,accept_conflicts=accept)

    @app.post('/api/knowledge/{id}/reject')
    async def knowledge_reject(id:str,payload:dict=Body(default={})):
        return chat.knowledge.reject(id,str(payload.get('reason','用户拒绝'))[:500])

    @app.post('/api/knowledge/{id}/unblock')
    async def knowledge_unblock(id:str,payload:dict=Body(...)):
        return chat.knowledge.clear_blocked(str(payload.get('persona_id','default')),id)

    @app.delete('/api/knowledge/{id}')
    async def delete_knowledge(id: str): return chat.knowledge.delete(id)

    @app.get('/api/messages')
    async def messages(scope_id: str = 'web:default', limit: int = 80): return store.list_messages(scope_id, limit=min(max(limit, 1), 200))

    @app.get('/api/time')
    async def program_time():
        from .timezones import config_zone
        config = settings.get()
        return {'now': datetime.now(timezone.utc).isoformat(), 'timezone': config_zone(config).key,
                'awareness_enabled': config['time']['awareness_enabled']}

    @app.delete('/api/messages')
    async def clear_messages(scope_id: str = 'web:default'):
        store.clear_messages(scope_id)
        chat.memory.clear_index()
        return {'ok': True}

    async def run_chat(request, payload, *, watch_disconnect=True):
        scope_id = str(payload.get('scope_id', 'web:default'))
        if not scope_id.startswith('web:'):
            raise ValueError('聊天试玩仅写入本地对话，请创建本地对话；不会向 QQ 群发送')
        scope = dict(store.get_scope(scope_id),always_reply=True,plan_enabled=False)
        from .web_chat_input import chat_input
        batch = chat_input(payload, max_images=settings.get()['context']['max_images'])
        text, images = (batch['text'], batch['images']) if batch else (str(payload.get('text', '')), payload.get('images') or [])
        references = app.state.resolve_attachments(scope_id, batch['attachments'] if batch else payload.get('attachments') or [])
        identity = chat.access.identity('local','shiori')
        metadata={'platform':'web','is_owner':True,'trusted_identity':identity}
        if batch:
            originals=[]
            for entry in batch['messages']:
                originals.append(await chat.observe(scope_id,'owner',entry['text'],entry['images'],metadata={**metadata,
                    'sent_at':entry['sent_at'],'received_at':entry['received_at'],'attachments':entry['attachments']}))
            observed = originals[-1]
            source_ids = [entry['id'] for entry in originals]
            # Planner gets the combined question, but its identity and original
            # message IDs are supplied by this authenticated server endpoint.
            planning_message = dict(observed,content=text)
        else:
            observed = await chat.observe(scope_id,'owner',text,images,metadata={**metadata,'attachments':[x['id'] for x in references]})
            source_ids = [observed['id']]
            planning_message = observed
        if payload.get('mode') == 'computer':
            job = app.state.computer.submit(scope_id,'owner',identity,text,source_ids=[observed['id']],attachments=references)
            reply = f"已创建电脑任务 {job['id']}，可在当前聊天查看进度和审批。"
            message = store.add_message(scope_id,'assistant','assistant',reply,job_id=job['id'],source_ids=[observed['id']])
            return {'reply':reply,'message_id':message['id'],'job':job,'memories':[],'jargon':[],'usage':None}
        plan = await chat.social.plan(scope,planning_message,direct=True)
        generation = plan.get('generation')
        if not plan.get('reply'):
            return {'reply': '', 'skipped': True, 'planner': plan, 'message': plan.get('reason', '本轮不回复')}
        task = asyncio.create_task(chat.respond(scope_id, 'owner', text, images=images, metadata={'platform': 'web', 'is_owner': True,'trusted_identity':identity,'reference_attachments':references,
                                  'planner':plan, 'request_generation':generation,'existing_message_id':observed['id'],'merged_message_ids':source_ids}))
        try:
            while not task.done():
                done, _ = await asyncio.wait({task}, timeout=.4)
                if not done and watch_disconnect and await request.is_disconnected():
                    chat.tool_actions.cancel_request(scope_id,'owner',generation)
                    task.cancel()
                    with contextlib.suppress(asyncio.CancelledError): await task
                    return Response(status_code=499)
            return await task
        except asyncio.CancelledError:
            chat.tool_actions.cancel_request(scope_id,'owner',generation)
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError): await task
            raise

    @app.post('/api/chat')
    async def send_chat(request: Request, payload: dict = Body(...)):
        if payload.get('stream') is not True:
            return await run_chat(request, payload)
        scope_id = str(payload.get('scope_id', 'web:default'))
        if not scope_id.startswith('web:'):
            raise ValueError('聊天试玩仅写入本地对话，请创建本地对话；不会向 QQ 群发送')
        store.get_scope(scope_id)
        # Reject malformed/revoked references before accepting a stream. The
        # real lease is acquired in its producer, not by response middleware.
        from .web_chat_input import chat_input
        batch = chat_input(payload, max_images=settings.get()['context']['max_images'])
        app.state.attachments.resolve(scope_id, batch['attachments'] if batch else payload.get('attachments') or [], acquire=False)

        @asynccontextmanager
        async def stream_lifecycle():
            nonlocal active_requests
            async with inbound.slot():
                async with request_gate:
                    if maintenance:
                        raise HTTPException(503, '正在恢复备份，请稍后重试')
                    active_requests += 1
                try:
                    yield
                finally:
                    async with request_gate:
                        active_requests -= 1
                        request_gate.notify_all()

        from .web_chat_stream import chat_stream
        return chat_stream(request, lambda: run_chat(request, payload, watch_disconnect=False),
                           lease=app.state.attachments.request, lifecycle=stream_lifecycle,
                           authenticated=lambda: auth_http.authenticated(request))

    def memory_payload(payload):
        from .memory_events import normalize_episode, episode_content
        data = dict(payload)
        data.setdefault('scope_id', 'web:default')
        data.setdefault('user_id', 'owner')
        data.setdefault('visibility', 'group' if ':group:' in data['scope_id'] else 'private')
        if data.get('episode') is not None:
            if data.get('kind') != 'event':
                raise ValueError('只有事件记忆可以包含 episode')
            data['episode'] = normalize_episode(data['episode'])
            data['content'] = episode_content(data['episode'])
        maximum = 300000 if data.get('kind') == 'note' else 6000
        if not str(data.get('content', '')).strip() or len(str(data['content'])) > maximum:
            raise ValueError(f'记忆内容需为 1—{maximum} 个字符')
        if 'shareable' in data and type(data['shareable']) is not bool:
            raise ValueError('允许共享须为布尔值')
        if data.get('shareable') and data.get('visibility') != 'group':
            raise ValueError('私有记忆不能加入群共享空间')
        return data

    @app.get('/api/memories')
    async def memories(scope_id: str | None = None, q: str = '', status: str | None = None, kind: str | None = None, limit: int | None = None, offset: int = 0):
        if limit is None:
            return await asyncio.to_thread(store.list_memories, scope_id=scope_id, q=q, status=status, kind=kind)
        return await asyncio.to_thread(store.memory_page, scope_id, q, status, kind=kind, limit=limit, offset=offset)

    @app.post('/api/memories')
    async def new_memory(payload: dict = Body(...)): return store.create_memory(memory_payload(payload))

    @app.get('/api/memories/{id}')
    async def memory_detail(id: str):
        return await asyncio.to_thread(store.get_memory, id)

    @app.get('/api/memories/{id}/sources')
    async def memory_sources(id: str, limit: int = 20, offset: int = 0):
        return await asyncio.to_thread(store.memory_sources_page, id, limit=limit, offset=offset)

    @app.put('/api/memories/{id}')
    async def edit_memory(id: str, payload: dict = Body(...)):
        if {'content', 'episode', 'kind', 'shareable', 'visibility'} & payload.keys():
            merged = dict(store.get_memory(id), **payload)
            if 'content' in payload and 'episode' not in payload:
                merged.pop('episode', None)
            memory_payload(merged)
        return store.update_memory(id, payload)

    @app.get('/api/memory/candidates')
    async def memory_candidates(scope_id: str | None = None, q: str = '', kind: str | None = None, limit: int | None = None, offset: int = 0):
        if limit is None:
            return await asyncio.to_thread(store.list_memories, scope_id=scope_id, q=q, status='pending', kind=kind)
        return await asyncio.to_thread(store.memory_page, scope_id, q, 'pending', kind=kind, limit=limit, offset=offset)

    @app.get('/api/memory/search')
    async def memory_search(scope_id: str = 'web:default', user_id: str = 'owner', q: str = ''):
        if not q.strip() or len(q) > 1000:
            raise ValueError('检索问题须为 1—1000 字')
        started = time.monotonic()
        result = await chat.memory.inspect(scope_id, user_id, q, limit=settings.get()['memory']['top_k'])
        result['items'] = [{**item, 'content': (item.get('excerpt') or item.get('retrieval', {}).get('excerpt') or item['content'])[:2200]} for item in result.get('items', result.get('results', []))]
        result.pop('results', None)
        result['elapsed_ms'] = round((time.monotonic() - started) * 1000)
        return result

    @app.get('/api/memory/relations')
    async def memory_relations(scope_id:str='web:default',user_id:str='owner',status:str|None=None):
        return chat.memory.list_relations(scope_id,user_id,status)

    @app.post('/api/memory/relations')
    async def memory_relation_propose(payload:dict=Body(...)):
        return chat.memory.propose_relation(str(payload.get('scope_id','web:default')),str(payload.get('user_id','owner')),payload)

    @app.post('/api/memory/relations/analyze')
    async def memory_relation_analyze(payload:dict=Body(...)):
        return await chat.memory.analyze_relations(str(payload.get('scope_id','web:default')),str(payload.get('user_id','owner')),provider=provider)

    @app.post('/api/memory/relations/{id}/review')
    async def memory_relation_review(id:str,payload:dict=Body(...)):
        scope_id=str(payload.get('scope_id','web:default')); user_id=str(payload.get('user_id','owner'))
        action=payload.get('action')
        if action not in ('approve','reject'): raise ValueError('关系审核操作无效')
        return getattr(chat.memory,action+'_relation')(id,scope_id,user_id)

    @app.get('/api/memory/organization')
    async def memory_organization(scope_id:str='web:default',user_id:str='owner'):
        return chat.memory.list_organization(scope_id,user_id)

    @app.post('/api/memory/organization')
    async def memory_organization_propose(payload:dict=Body(...)):
        return chat.memory.propose_organization(str(payload.get('scope_id','web:default')),str(payload.get('user_id','owner')))

    @app.post('/api/memory/organization/{id}/review')
    async def memory_organization_review(id:str,payload:dict=Body(...)):
        return chat.memory.review_organization(id,str(payload.get('scope_id','web:default')),str(payload.get('user_id','owner')),str(payload.get('action','')))

    @app.get('/api/memories/{id}/conflicts')
    async def memory_conflicts(id: str):
        return chat.memory.conflicts(id)

    @app.get('/api/memories/{id}/versions')
    async def memory_versions(id: str):
        return store.list_memory_versions(id)

    @app.post('/api/memories/{id}/approve')
    async def approve_memory(id: str, payload: dict = Body(default={})):
        replace = payload.get('replace_conflicts', False)
        if type(replace) is not bool:
            raise ValueError('替代冲突开关须为布尔值')
        return chat.memory.approve(id, replace_conflicts=replace)

    @app.post('/api/memories/{id}/reject')
    async def reject_memory(id: str):
        return chat.memory.reject(id)

    @app.delete('/api/memories/{id}')
    async def delete_memory(id: str):
        store.delete_memory(id)
        chat.memory.clear_index()
        return {'ok': True}

    @app.get('/api/profile')
    async def profile(scope_id: str = 'web:default', user_id: str = 'owner', limit: int = 50, offset: int = 0):
        return store.profile(scope_id, user_id, limit=limit, offset=offset, include_summary=False)

    @app.get('/api/context/status')
    async def context_status(scope_id: str = 'web:default', user_id: str = 'owner'):
        latest = await asyncio.to_thread(chat.context._latest_summary, scope_id, user_id)
        return {'summary': latest, 'source_count': len(latest.get('source_ids', [])) if latest else 0,
                'covered_count': len(latest.get('covered_message_ids', [])) if latest else 0,
                'last_compacted_at': latest.get('created_at') if latest else None,
                'enabled': settings.get()['context']['enabled'] and store.get_scope(scope_id).get('context_compress', False)}

    @app.post('/api/context/compact')
    async def compact_context(payload: dict = Body(...)):
        scope_id, user_id = str(payload.get('scope_id', 'web:default')), str(payload.get('user_id', 'owner'))
        result = await chat.context.compact(scope_id, user_id, history=chat._history(scope_id, limit=400, user_id=user_id))
        messages = {'compressed': '已整理旧对话，并保留最近原文。', 'not_allowed': '当前会话暂停或未允许访问，未整理。', 'model_failed': '模型整理失败，保留有效原文。', 'sources_changed': '整理期间来源已变化，未保存摘要。'}
        return {**result, 'message': messages.get(result.get('reason'), '本次没有需要整理的旧对话，保留原文。')}

    @app.get('/api/jargon')
    async def jargon(scope_id: str | None = None, q: str = '', status: str | None = None, limit:int|None=None,offset:int=0):
        if limit is None:return [x for x in store.list_jargon(scope_id=scope_id,q=q,status=status) if x.get('entry_type','jargon')!='expression']
        if not 1<=limit<=100 or offset<0 or offset>10000000:raise ValueError('词典分页参数无效')
        clauses=["status<> 'deleted'","COALESCE(json_extract(data,'$.entry_type'),'jargon')='jargon'"];args=[]
        if scope_id:clauses.append('scope_id=?');args.append(scope_id)
        if status:clauses.append('status=?');args.append(status)
        if q:clauses.append("(instr(term,?)>0 OR instr(json_extract(data,'$.meaning'),?)>0)");args.extend([q,q])
        where=' AND '.join(clauses)
        def page():
            with store._lock:
                total=store._conn.execute('SELECT COUNT(*) FROM jargon WHERE '+where,args).fetchone()[0]
                items=[json.loads(r[0]) for r in store._conn.execute('SELECT data FROM jargon WHERE '+where+' ORDER BY updated_at DESC,id LIMIT ? OFFSET ?',[*args,limit,offset])]
            return {'items':items,'total':total,'offset':offset,'has_more':offset+limit<total,'next_offset':offset+limit}
        return await asyncio.to_thread(page)

    @app.get('/api/expressions')
    async def expressions(scope_id: str | None = None, status: str | None = None):
        return [x for x in store.list_jargon(scope_id=scope_id, status=status) if x.get('entry_type') == 'expression']

    @app.get('/api/jargon/{id}/evidence')
    async def jargon_evidence(id: str, offset: int = 0, limit: int = 50):
        from .jargon_evidence import evidence_page
        return await asyncio.to_thread(evidence_page, store, id, offset=offset, limit=limit)

    @app.post('/api/expressions/analyze')
    async def analyze_expressions(payload: dict = Body(...)):
        return await chat.social.analyze_expressions(str(payload.get('scope_id', 'web:default')))

    @app.post('/api/jargon/analyze')
    async def analyze_jargon(payload: dict = Body(...)):
        return await chat.social.analyze(str(payload.get('scope_id', 'web:default')))

    @app.post('/api/memory/analyze')
    async def analyze_memory(payload: dict = Body(...)):
        scope_id, user_id = str(payload.get('scope_id', 'web:default')), str(payload.get('user_id', 'owner'))
        result = await chat._extract(scope_id, user_id)
        return {**(result or {}), 'candidates': [x for x in store.list_memories(scope_id=scope_id, status='pending') if x.get('user_id') == user_id]}

    @app.post('/api/jargon')
    async def new_jargon(payload: dict = Body(...)):
        data = dict(payload)
        data.setdefault('scope_id', 'web:default')
        data.setdefault('created_by', 'owner')
        data.setdefault('status', 'pending' if data.get('entry_type') == 'expression' else 'approved')
        if data.get('entry_type') == 'expression':
            data.setdefault('use_allowed', False)
            data['manual_taught'] = True
            data['automatic'] = False
        return store.create_jargon(data)

    @app.put('/api/jargon/{id}')
    async def edit_jargon(id: str, payload: dict = Body(...)): return store.update_jargon(id, payload)

    @app.delete('/api/jargon/{id}')
    async def delete_jargon(id: str):
        store.delete_jargon(id)
        return {'ok': True}

    @app.get('/api/tasks')
    async def tasks(scope_id: str | None = None, status: str | None = None): return store.list_tasks(scope_id=scope_id, status=status)

    def task_payload(payload):
        from .timeparse import calendar_fields, local_wall
        from .timezones import config_zone
        data = dict(payload)
        data.setdefault('scope_id', 'web:default')
        data.setdefault('user_id', 'owner')
        data.setdefault('kind', 'reminder' if data.get('due_at') else 'todo')
        if data.get('due_at'):
            try:
                due = datetime.fromisoformat(str(data['due_at']).replace('Z', '+00:00'))
            except (TypeError, ValueError) as exc:
                raise ValueError('提醒时间格式无效') from exc
            if due.tzinfo is None:
                if not data.get('timezone'):
                    raise ValueError('提醒需要未来时间并包含时区；本地钟点须同时提供 IANA timezone')
                due = local_wall(due, config_zone({'time': {'timezone': data['timezone']}}))
            if due <= datetime.now(timezone.utc):
                raise ValueError('提醒需要未来时间并包含时区')
            data['due_at'] = due.astimezone(timezone.utc).isoformat()
            data.update(calendar_fields(data['due_at'], settings, timezone_name=data.get('timezone')))
        if data['kind'] == 'reminder' and not data.get('due_at'):
            raise ValueError('提醒需要日期时间')
        return data

    @app.post('/api/tasks')
    async def new_task(payload: dict = Body(...)): return store.create_task(task_payload(payload))

    @app.put('/api/tasks/{id}')
    async def edit_task(id: str, payload: dict = Body(...)):
        data = dict(payload)
        if ('due_at' in data and data['due_at']) or 'timezone' in data:
            checked = task_payload(dict(store.get_task(id), **data))
            if checked.get('due_at'):
                data.update({key: checked[key] for key in ('due_at', 'timezone', 'wall_time', 'calendar_anchor')})
        return store.update_task(id, data)

    @app.post('/api/tasks/{id}/cancel')
    async def cancel_task(id: str): return store.update_task(id, {'status': 'cancelled'})

    @app.post('/api/tasks/{id}/retry')
    async def retry_task(id: str):
        task = store.get_task(id)
        if task.get('status') not in ('failed', 'cancelled'):
            raise ValueError('仅失败或已取消的任务可以手动重试')
        calendar = {}
        if task.get('due_at'):
            from .timeparse import validate_calendar
            snapshot = validate_calendar(dict(task), settings)
            calendar = {key: snapshot[key] for key in ('timezone', 'wall_time', 'calendar_anchor')}
        return store.update_task(id, {'status': 'pending', 'delivery_status': 'pending', 'error': '', 'due_at': datetime.now(timezone.utc).isoformat() if task.get('kind') == 'reminder' else task.get('due_at'), **calendar})

    @app.get('/api/usage')
    async def usage(): return [dict(x,cost_known=x.get('cost_known',float(x.get('cost',0))>0)) for x in store.list_usage(limit=300)]

    @app.post('/api/usage/{id}/reconcile')
    async def reconcile_usage(id: str, request: Request, payload: dict = Body(...)):
        if not local_peer(request):
            raise HTTPException(403, '消费对账只能在运行电脑本机进行')
        from .usage_ledger import reconcile
        return await asyncio.to_thread(reconcile, store, id, payload)

    @app.get('/api/usage/unconfirmed')
    async def unconfirmed_usage(offset: int = 0, limit: int = 50):
        if offset < 0 or not 1 <= limit <= 100:
            raise ValueError('用量分页范围无效')
        with store._lock:
            clause = "json_extract(data,'$.unconfirmed')=1"
            total = store._conn.execute('SELECT COUNT(*) FROM usage WHERE ' + clause).fetchone()[0]
            records = store._conn.execute('SELECT data FROM usage WHERE ' + clause + ' ORDER BY created_at DESC,rowid DESC LIMIT ? OFFSET ?', (limit, offset)).fetchall()
        return {'items': [json.loads(row[0]) for row in records], 'total':total, 'offset':offset, 'limit':limit}

    @app.get('/api/events')
    async def events(): return store.list_events(limit=80)

    @app.get('/api/qq/status')
    async def qq_status(): return router.status()

    @app.post('/api/qq/reconnect')
    async def qq_reconnect():
        await reconnect()
        return router.status()

    @app.post('/api/qq/official/test')
    async def official_test(payload: dict = Body(default={})):
        import httpx
        cfg = settings.get()['qq_official']
        appid, secret = str(payload.get('app_id') or cfg['app_id']), str(payload.get('app_secret') or cfg['app_secret'])
        if not appid or not secret:
            raise ValueError('请填写 AppID 和 AppSecret')
        async with httpx.AsyncClient(timeout=15) as client:
            response = await client.post('https://bots.qq.com/app/getAppAccessToken', json={'appId': appid, 'clientSecret': secret})
        try:
            valid = response.status_code == 200 and bool(response.json().get('access_token'))
        except ValueError:
            valid = False
        if not valid:
            raise ValueError('官方凭据校验未通过，请核对 AppID、AppSecret 与平台状态')
        return {'ok': True, 'message': '凭据有效；群与私聊事件仍需平台权限及真实账号验证'}

    @app.websocket('/onebot/v11/ws')
    async def reverse_ws(websocket: WebSocket):
        cfg = settings.get()['onebot']
        if not cfg['enabled'] or cfg.get('adapter') == 'official' or len(cfg.get('access_token', '')) < 12:
            await websocket.close(code=1008)
            return
        value = websocket.headers.get('authorization', '').removeprefix('Bearer ') or websocket.query_params.get('access_token', '')
        if not hmac.compare_digest(value, cfg['access_token']):
            await websocket.close(code=1008)
            return
        await websocket.accept()
        await onebot.attach(websocket)

    @app.post('/api/tools/calculate')
    async def calc(payload: dict = Body(...)): return calculate(str(payload.get('expression', '')))

    @app.post('/api/tools/read')
    async def read(payload: dict = Body(...)): return await read_public(str(payload.get('url', '')))

    @app.post('/api/tools/search')
    async def search(payload: dict = Body(...)):
        return await chat.search.search(payload.get('query', ''))

    @app.get('/api/search/status')
    async def search_status():
        return await asyncio.to_thread(chat.search.status)

    @app.post('/api/search/test')
    async def search_test(payload: dict = Body(...)):
        # This is a real query, governed by the same saved configuration and
        # durable quota as conversational and background searches.
        return await chat.search.search(payload.get('query', ''))

    @app.post('/api/files')
    async def upload(file: UploadFile = File(...), scope_id: str = Form('web:default')):
        from .documents import MAX_BYTES, extract_document
        name = Path(file.filename or '').name
        raw = await file.read(MAX_BYTES + 1)
        parsed = await asyncio.to_thread(extract_document, name, raw)
        fingerprint = hashlib.sha256(raw).hexdigest()
        existing = next((x for x in store.list_memories(scope_id=scope_id, status='active') if x.get('file_hash') == fingerprint and x.get('user_id') == 'owner'), None)
        memory = existing or store.create_memory({'scope_id': scope_id, 'user_id': 'owner', 'kind': 'note', 'content': parsed['text'], 'subject': name,
                                                 'visibility': 'group' if ':group:' in scope_id else 'private', 'filename': name, 'file_hash': fingerprint,
                                                 'document': {key: value for key, value in parsed.items() if key != 'text'}, 'pinned': True})
        return {'filename': name, **{key: value for key, value in parsed.items() if key != 'text'}, 'text': parsed['text'][:2000], 'memory': memory, 'duplicate': bool(existing)}

    def backup_auxiliary():
        public = settings.public()
        public['paused'] = True
        public['onebot']['enabled'] = False
        public['qq_official']['enabled'] = False
        public['webui']['remote_enabled'] = False
        files = {'settings.json': json.dumps(public, ensure_ascii=False)}
        includes = ['personas', 'messages', 'memories', 'jargon', 'tasks', 'usage', 'events']
        warnings = []
        if chat.knowledge.status().get('degraded'):
            warnings.append('人格知识损坏，未包含在此备份中；原件仍在data，请单独保留。')
        else:
            files['persona_knowledge.json'] = chat.knowledge.export_state()
            includes.append('persona_knowledge')
        files['tool_history.json'] = chat.tool_actions.export_history()
        includes.append('tool_history_read_only')
        files['jobs_history.json'] = json.dumps(app.state.runs.export_history(), ensure_ascii=False)
        includes.append('jobs_history_read_only')
        warnings.append('任务语料、Skill包与工作目录文件需单独保存；任务历史恢复后只读，不重放。')
        warnings.append('表情包图片库需在机器人配置→表情包中单独备份；本备份不包含图片。')
        if chat.tool_actions.recovery_warning:
            warnings.append('旧工具台账有恢复警告，损坏原件仍需单独保留。')
        files['manifest.json'] = json.dumps({'version': __version__,
            'created_at': datetime.now(timezone.utc).isoformat(), 'secrets_included': False,
            'includes': includes, 'warnings': warnings}, ensure_ascii=False)
        return files

    from .backup_archives import (BackupExports, extract_archive, spool_upload, require_space,
                                  drain_task, restore_thread)
    backups = app.state.backup_exports = BackupExports(data_dir, store, backup_auxiliary)

    @app.post('/api/backup/jobs')
    async def start_backup():
        return backups.start()

    @app.get('/api/backup/jobs/current')
    async def current_backup():
        return backups.snapshot()

    @app.post('/api/backup/jobs/{identifier}/cancel')
    async def cancel_backup(identifier: str):
        try:
            return backups.cancel(identifier)
        except KeyError:
            raise HTTPException(404, '备份任务不存在或已失效')

    @app.get('/api/backup/jobs/{identifier}/file')
    async def download_backup(identifier: str):
        return backups.download(identifier)

    @app.get('/api/backup')
    async def backup():
        task = backups.start()
        result = await backups.wait(task['id'])
        if result['status'] != 'completed':
            raise ValueError(result['message'])
        return backups.download(task['id'])

    @app.post('/api/backup/restore')
    async def restore(file: UploadFile = File(...)):
        if backups.busy():
            raise ValueError('备份正在生成，请完成或取消后再恢复')
        cancelled = asyncio.Event()
        task = asyncio.create_task(restore_contents(file, cancelled))
        operation = {'task': task, 'cancel': cancelled}
        app.state.backup_restore = operation
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            cancelled.set()
            await drain_task(task)
            raise
        finally:
            if task.done() and app.state.backup_restore is operation:
                app.state.backup_restore = None

    async def restore_contents(file, cancelled):
        def check_preparation():
            if cancelled.is_set():
                raise ValueError('恢复已取消，原始数据未修改')
        try:
            with tempfile.TemporaryDirectory(dir=data_dir) as temporary:
                folder = Path(temporary)
                uploaded = folder / 'upload.zip'
                await restore_thread(spool_upload, file.file, uploaded)
                check_preparation()
                members = await restore_thread(extract_archive, uploaded, folder)
                check_preparation()
                candidate = members['qunyu.db']
                await restore_thread(store.restore_from, candidate, validate_only=True)
                check_preparation()
                # Keep room for the original safety snapshot, SQLite copy-on-
                # write/journal work, and a rollback before stopping workers.
                await restore_thread(require_space, data_dir,
                    3 * max(candidate.stat().st_size, store.path.stat().st_size))
                def read_member(name, default):
                    return json.loads(members[name].read_text('utf-8')) if name in members else default
                from .knowledge import PersonaKnowledge
                knowledge_state = read_member('persona_knowledge.json', {'version':1,'items':[],'budget':{'day':'','searches':0},'personas':{}})
                PersonaKnowledge.validate_state(knowledge_state)
                from .tool_actions import ToolActions
                tool_history=read_member('tool_history.json', {'version':1,'runs':[]})
                ToolActions.validate_history(tool_history)
                from .runs import RunManager
                jobs_history=read_member('jobs_history.json', {'version':1,'jobs':[]})
                RunManager.validate_history(jobs_history)
                cfg = read_member('settings.json', {})
                if not isinstance(cfg, dict) or any(section in cfg and not isinstance(cfg[section], dict) for section in ('model', 'onebot', 'qq_official', 'budget', 'memory', 'routing', 'context', 'identity', 'output', 'exploration','planner','access','computer','webui','search')):
                    raise ValueError('备份设置格式不正确')
                cfg['paused'] = True
                cfg.setdefault('computer',{})['enabled']=False
                cfg.setdefault('webui',{})['remote_enabled']=False
                for section, flag in (('model', 'clear_api_key'), ('onebot', 'clear_access_token'), ('qq_official', 'clear_app_secret')):
                    cfg.setdefault(section, {})[flag] = True
                cfg['onebot']['enabled'] = False
                cfg['qq_official']['enabled'] = False
                cfg.setdefault('routing', dict(DEFAULTS['routing']))
                cfg['providers'] = [{**item, 'api_key': '', 'clear_api_key': True} for item in cfg.get('providers', [])]
                cfg['provider_sources'] = [{**item, 'api_key':'', 'clear_api_key':True} for item in cfg.get('provider_sources', [])]
                # Search-provider updates merge by ID; explicitly clear keys
                # already on this machine even when restoring an older backup.
                search_sources = {item['id']: item for item in [
                    *settings.get().get('search_providers', []), *cfg.get('search_providers', [])]}
                cfg['search_providers'] = [{**item, 'api_key':'', 'clear_api_key':True} for item in search_sources.values()]
                if cfg.get('search', {}).get('provider', 'public') != 'public':
                    cfg['search']['enabled'] = False
                cfg['mcp_servers'] = [{**item, 'token': '', 'clear_token':True,'enabled':False} for item in cfg.get('mcp_servers', [])]
                settings.update(cfg, dry_run=True)
                previous = settings.get()
                knowledge_path = data_dir / 'persona_knowledge.json'
                previous_knowledge = knowledge_path.read_bytes() if knowledge_path.exists() else None
                ledger_path=data_dir/'tool_runs.json'
                previous_ledger=ledger_path.read_bytes() if ledger_path.exists() else None
                current_history=json.loads(chat.tool_actions.export_history())
                previous_jobs=json.dumps(app.state.runs._jobs,ensure_ascii=False)
                safety = data_dir / ('before-restore-' + datetime.now().strftime('%Y%m%d-%H%M%S-%f') + '.db')
                snapshotted = False
                check_preparation()
                # Past this point an HTTP disconnect must not interrupt the
                # commit/rollback. The separately owned operation is drained.
                try:
                    settings.update({'paused': True})
                    await app.state.runs.close()
                    await scheduler.stop()
                    await onebot.stop()
                    await official.stop()
                    await chat.close()
                    await restore_thread(store.backup, safety)
                    snapshotted = True
                    await restore_thread(store.restore_from, candidate)
                    settings.update(cfg)
                    auth_http.allow_remote = False
                    knowledge_path.write_text(json.dumps(knowledge_state, ensure_ascii=False), 'utf-8')
                    ledger_path.write_text('[]','utf-8')
                    chat.reset_engines()
                    for queue in schedule_queues:
                        queue.reload(restored=True)
                    chat.tool_actions.import_history(current_history)
                    chat.tool_actions.import_history(tool_history)
                    app.state.runs.import_history(jobs_history)
                    chat.memory.clear_index()
                except (Exception, asyncio.CancelledError) as exc:
                    app.state.runs._jobs=json.loads(previous_jobs)
                    for restored_job in app.state.runs._jobs.values():
                        if restored_job.get('status') not in ('completed','failed','cancelled','interrupted'):
                            restored_job.update(status='interrupted',approval=None,error='恢复已回滚，原任务停止，未自动重放')
                    app.state.runs._save()
                    if snapshotted:
                        await restore_thread(store.restore_from, safety)
                    settings.update(previous)
                    if previous_knowledge is None:
                        knowledge_path.unlink(missing_ok=True)
                    else:
                        knowledge_path.write_bytes(previous_knowledge)
                    if previous_ledger is None: ledger_path.unlink(missing_ok=True)
                    else: ledger_path.write_bytes(previous_ledger)
                    chat.reset_engines()
                    for queue in schedule_queues:
                        queue.reload()
                    chat.memory.clear_index()
                    if start_workers:
                        await reconnect()
                        await scheduler.start()
                        await chat.knowledge.start()
                        await chat.character_schedule.start()
                    raise ValueError('恢复失败，已保留恢复前数据与设置') from exc
                if start_workers:
                    await scheduler.start()
                    await chat.knowledge.start()
                    await chat.character_schedule.start()
        except (zipfile.BadZipFile, sqlite3.Error, KeyError, json.JSONDecodeError, TypeError, OverflowError) as exc:
            raise ValueError('备份无效或版本不兼容') from exc
        return {'ok': True, 'message': '已恢复并暂停运行。工具回执与任务历史仅供查看，不会重新执行。密钥不在备份内，请重新配置、检查任务后恢复运行。'}

    @app.post('/api/shutdown')
    async def shutdown():
        if app.state.shutdown:
            asyncio.get_running_loop().call_later(.3, app.state.shutdown)
        return {'ok': True}

    static_dir = Path(__file__).parent / 'static'
    # A local source fix must refresh desktop/browser assets without requiring
    # a public version bump. The document carries a content-based asset key.
    index = (static_dir / 'index.html').read_text('utf-8')
    assets = sorted([*static_dir.glob('*.js'), *static_dir.glob('*.css')])
    revision = hashlib.sha256(index.encode() + b''.join(path.read_bytes() for path in assets)).hexdigest()[:16]
    index = index.replace('?v=' + __version__, '?v=' + __version__ + '&amp;rev=' + revision)

    @app.get('/', include_in_schema=False)
    @app.get('/chat', include_in_schema=False)
    @app.get('/chat/', include_in_schema=False)
    @app.get('/index.html', include_in_schema=False)
    async def ui_document():
        return Response(index, media_type='text/html', headers={'Cache-Control':'no-store'})

    app.mount('/static', StaticFiles(directory=static_dir), name='assets')
    app.mount('/', StaticFiles(directory=static_dir, html=True), name='ui')
    return app
