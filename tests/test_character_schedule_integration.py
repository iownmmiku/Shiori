"""Public workbench auth and native chat context boundaries."""
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

from auth_helpers import password_login
from shiori.api import create_app
from shiori.chat import ChatService
from shiori.db import Store
from test_life_schedule import Settings, Provider, prepare


def test_schedule_api_requires_auth_and_local_admin(tmp_path):
    app = create_app(tmp_path, start_workers=False)
    with TestClient(app) as client:
        assert client.get('/api/character-schedule/state').status_code == 401
        password_login(client, app)
        state = client.get('/api/character-schedule/state')
        assert state.status_code == 200, state.text
        value = state.json()
        assert value['settings']['enabled'] is False
        assert value['settings']['reply_control'] is False
        persona_id = value['persona_id']
        preview = client.post('/api/character-schedule/preview', json={
            'persona_id': persona_id, 'date': '2026-10-29', 'time': '12:00'})
        assert preview.status_code == 200, preview.text
        assert preview.json()['model_called'] is False
        assert client.post('/api/character-schedule/settings', json={'enabled': True}).status_code == 200
        assert client.post('/api/character-schedule/settings', json={'unknown': True}).status_code == 400


def test_native_chat_context_uses_cached_life_plan_without_diaries_or_real_memories(tmp_path):
    store = Store(tmp_path)
    settings, provider = Settings(), Provider()
    chat = ChatService(store, settings, provider)
    try:
        today = datetime.now(ZoneInfo('Asia/Hong_Kong')).date().isoformat()
        pid = prepare(chat.character_schedule, day=today, status='busy')
        scope = store.upsert_scope({'id': 'web:default', 'persona_id': pid})
        chat.character_schedule.save_settings({'enabled': True, 'reply_control': True})
        message = store.add_message(scope['id'], 'owner', 'user', '现在在做什么？')
        messages = chat._context(scope, 'owner', message['content'], [], [], [], message['id'])
        assert any('fictional_schedule' in item['content'] for item in messages if isinstance(item.get('content'), str))
        assert chat.character_schedule.contact_policy(scope['id'])['active'] is False
        assert provider.calls == []
        assert store.list_memories(scope_id=scope['id']) == []
    finally:
        store.close()


def test_both_connectors_share_no_queue_rows(tmp_path):
    app = create_app(tmp_path, start_workers=False)
    try:
        one = app.state.onebot._flow.schedule_delivery
        official = app.state.official._flow.schedule_delivery
        assert one.prefix == 'qq:' and official.prefix == 'qqofficial:'
        assert app.state.character_schedule.queue_status() == []
    finally:
        app.state.store.close()


def test_diary_capture_uses_original_receipt_day_across_midnight(tmp_path):
    store = Store(tmp_path)
    chat = ChatService(store, Settings(), Provider())
    try:
        pid = prepare(chat.character_schedule, day='2026-10-28')
        scope_id = 'web:default'
        store.upsert_scope({'id':scope_id, 'persona_id':pid})
        chat.character_schedule.save_settings({'enabled':True, 'diary_chat_enabled':True,
                                               'diary_scopes':[scope_id]})
        message = store.add_message(scope_id, 'owner', 'user', '午夜前收到的聊天',
                                    received_at='2026-10-28T15:59:59Z')
        chat._capture_schedule_material(message)
        with store._lock:
            rows = store._conn.execute('SELECT day FROM life_materials').fetchall()
        assert [row['day'] for row in rows] == ['2026-10-28']
    finally:
        store.close()
