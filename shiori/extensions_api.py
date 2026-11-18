"""Extension management, extension APIs and extension pages.

All of it sits behind the console's sign-in: /api/ routes through the shared
middleware, the pages through the same session check here.
"""
import asyncio
import json

from fastapi import Body, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response

from .extension_host import ExtensionRequest


def install_extensions(app, host, auth_http):
    @app.get('/api/extensions')
    async def extensions():
        return {'items': await asyncio.to_thread(host.list)}

    @app.put('/api/extensions/{extension_id}')
    async def update_extension(extension_id: str, payload: dict = Body(...)):
        if not isinstance(payload, dict) or set(payload) - {'enabled', 'confirm', 'config', 'clear'}:
            raise ValueError('扩展设置参数无效')
        result = None
        if 'config' in payload or 'clear' in payload:
            result = await asyncio.to_thread(host.set_config, extension_id, payload.get('config') or {}, payload.get('clear') or [])
        if 'enabled' in payload:
            if type(payload['enabled']) is not bool or type(payload.get('confirm', False)) is not bool:
                raise ValueError('启用参数须为布尔值')
            result = await asyncio.to_thread(host.set_enabled, extension_id, payload['enabled'], confirm=payload.get('confirm', False))
        if result is None:
            raise ValueError('没有要保存的内容')
        return result

    async def dispatch(extension_id: str, path: str, request: Request):
        handler = host.api_handler(extension_id, request.method, path)
        body = None
        raw = await request.body()
        if raw:
            try:
                body = json.loads(raw.decode('utf-8'))
            except (UnicodeError, ValueError) as exc:
                raise ValueError('请求内容须为 JSON') from exc
        try:
            result = handler(ExtensionRequest(request.method, path, dict(request.query_params), body))
            if asyncio.iscoroutine(result):
                result = await result
        except (ValueError, KeyError, PermissionError):
            raise
        except Exception as exc:
            host.store.add_exception('extension_error', '扩展接口出错', exc, extension=extension_id, path=path)
            return JSONResponse({'detail': '扩展接口出错，详情见运行日志'}, status_code=500)
        return result

    app.add_api_route('/api/extensions/{extension_id}/api/{path:path}', dispatch, methods=['GET', 'POST', 'PUT', 'DELETE'])

    @app.get('/extensions/{extension_id}/pages/{page_id}')
    async def page_root(extension_id: str, page_id: str):
        return RedirectResponse(f'/extensions/{extension_id}/pages/{page_id}/', status_code=307)

    @app.get('/extensions/{extension_id}/pages/{page_id}/{asset:path}')
    async def page_asset(extension_id: str, page_id: str, asset: str, request: Request):
        if not auth_http.authenticated(request):
            return Response('请先登录 Shiori 控制台。', status_code=401, media_type='text/plain; charset=utf-8')
        try:
            path, kind = host.page_file(extension_id, page_id, asset)
        except KeyError:
            return Response('找不到这个扩展页面或文件。', status_code=404, media_type='text/plain; charset=utf-8')
        return FileResponse(path, media_type=kind, headers={'Cache-Control': 'no-cache, max-age=0, must-revalidate'})
