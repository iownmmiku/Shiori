"""Sticker management uses the WebUI's existing authenticated boundary."""
import asyncio
import sqlite3
import tempfile
from pathlib import Path

from fastapi import Body
from fastapi.responses import Response


def install_stickers(app, chat):
    @app.get('/api/stickers')
    async def stickers(scope_id: str = '', category: str = '', q: str = '', offset: int = 0, limit: int = 50):
        if len(q) > 240 or len(category) > 20: raise ValueError('表情包筛选过长')
        return await asyncio.to_thread(chat.stickers.page,scope_id,category,q,offset,limit)

    @app.get('/api/stickers/backup')
    async def backup_stickers():
        library=chat.stickers
        def snapshot():
            with tempfile.TemporaryDirectory(dir=library.root) as folder:
                path=Path(folder)/'library.sqlite3'
                with library._lock:
                    destination=sqlite3.connect(path)
                    try: library._conn.backup(destination)
                    finally: destination.close()
                return path.read_bytes()
        raw=await asyncio.to_thread(snapshot)
        return Response(raw,media_type='application/vnd.sqlite3',headers={'Content-Disposition':'attachment; filename="shiori-stickers.sqlite3"','Cache-Control':'no-store'})

    @app.get('/api/stickers/{key}/image')
    async def sticker_image(key: str):
        item,raw=chat.stickers.get(key,with_image=True)
        if item['status']=='deleted': raise KeyError(key)
        return Response(raw,media_type=item['mime'],headers={'Cache-Control':'no-store','X-Content-Type-Options':'nosniff'})

    @app.put('/api/stickers/{key}')
    async def update_sticker(key: str, payload: dict = Body(...)):
        return chat.stickers.update(key,payload)

    @app.delete('/api/stickers/{key}')
    async def delete_sticker(key: str):
        return chat.stickers.delete(key)
