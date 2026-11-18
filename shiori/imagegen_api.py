"""Image generation settings use the WebUI's existing authenticated boundary."""
import asyncio

from fastapi import Body

from .imagegen import ASPECTS, ImageGenError


def install_imagegen(app, chat):
    @app.get('/api/imagegen')
    async def imagegen_settings():
        return await asyncio.to_thread(chat.imagegen.public)

    @app.put('/api/imagegen')
    async def update_imagegen(payload: dict = Body(...)):
        return await asyncio.to_thread(chat.imagegen.update, payload)

    @app.post('/api/imagegen/test')
    async def test_imagegen(payload: dict = Body(...)):
        if set(payload) - {'prompt', 'service_id', 'aspect'}:
            raise ValueError('测试参数无效')
        service_id = payload.get('service_id') or None
        aspect = payload.get('aspect', 'square')
        if service_id is not None and not isinstance(service_id, str) or aspect not in ASPECTS:
            raise ValueError('测试参数无效')
        try:
            return await chat.imagegen.test(payload.get('prompt', ''), service_id=service_id, aspect=aspect)
        except ImageGenError as exc:
            # The owner sees the reason; the redacted upstream excerpt helps fix the configuration.
            raise ValueError(str(exc) + (f'（接口说明：{exc.detail}）' if exc.detail else '')) from None
