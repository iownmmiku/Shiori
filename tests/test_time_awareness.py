from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import shutil
import subprocess

import pytest
from fastapi.testclient import TestClient

from auth_helpers import password_login
from shiori.api import create_app
from shiori.chat import ChatService
from shiori.db import Store
from shiori.settings import Settings
from shiori.social import SocialEngine
from shiori.timezones import current_time_context


@pytest.fixture
def anyio_backend():
    return 'asyncio'


def test_timezone_clock_and_disabled_context():
    now = datetime(2027, 1, 1, 0, 30, tzinfo=timezone.utc)
    context = current_time_context({'time': {'timezone': 'America/New_York'}}, now=now)
    assert context == {'now': '2026-12-31T19:30:00-05:00', 'timezone': 'America/New_York', 'weekday': '星期四'}
    summer = current_time_context({'time': {'timezone': 'America/New_York'}}, now=now.replace(month=7))
    assert summer['now'].endswith('-04:00')
    assert current_time_context({'time': {'awareness_enabled': False}}, now=now) is None


def test_clock_switch_persists_and_invalid_values_do_not_mutate(tmp_path):
    settings = Settings(tmp_path)
    assert settings.get()['time']['awareness_enabled'] is True
    settings.update({'time': {'awareness_enabled': False, 'timezone': 'Asia/Hong_Kong'}})
    assert Settings(tmp_path).get()['time'] == {'awareness_enabled': False, 'timezone': 'Asia/Hong_Kong'}
    before = deepcopy(settings.get())
    for value in ('false', 0, None, []):
        with pytest.raises(ValueError, match='感知现实时间'):
            settings.update({'time': {'awareness_enabled': value}})
        assert settings.get() == before


class Model:
    def __init__(self):
        self.prompts = []

    async def complete(self, messages, **kwargs):
        self.prompts.append((messages, kwargs.get('purpose', 'chat')))
        if kwargs.get('purpose') == 'planner':
            payload = json.loads(messages[-1]['content'])
            return {'content': json.dumps({'action': 'reply', 'target_message_id': payload['current_message_id'],
                    'target_user_id': payload['current_user_id'], 'reason': '回应当前直接消息', 'intent': '回答',
                    'addressed_to_bot': True, 'needed_info': []})}
        reply = '已收到。'
        return {'content': reply, 'message': {'role': 'assistant', 'content': reply}, 'tool_calls': [], 'usage': {}}


@pytest.mark.anyio
async def test_chat_clock_refreshes_per_reply_and_switch_takes_effect(tmp_path, monkeypatch):
    from shiori import timezones
    fixed = [datetime(2027, 1, 1, 3, 30, tzinfo=timezone.utc)]

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return fixed[0].astimezone(tz) if tz else fixed[0].replace(tzinfo=None)

    monkeypatch.setattr(timezones, 'datetime', Clock)
    settings, store, model = Settings(tmp_path), Store(tmp_path), Model()
    settings.update({'model': {'base_url': 'https://fixture.invalid/v1', 'model': 'fixture'},
                     'time': {'timezone': 'Asia/Tokyo'}})
    store.upsert_scope({'id': 'web:clock', 'auto_memory': False})
    chat = ChatService(store, settings, model)
    try:
        await chat.respond('web:clock', 'owner', '现在方便聊天吗')
        assert '2027-01-01T12:30:00+09:00' in model.prompts[-1][0][0]['content']
        assert '星期五' in model.prompts[-1][0][0]['content']
        fixed[0] += timedelta(hours=2)
        await chat.respond('web:clock', 'owner', '再聊聊')
        assert '2027-01-01T14:30:00+09:00' in model.prompts[-1][0][0]['content']
        settings.update({'time': {'awareness_enabled': False}})
        await chat.respond('web:clock', 'owner', '继续聊天')
        assert '【现实时间】' not in model.prompts[-1][0][0]['content']
        assert '当前时间' not in model.prompts[-1][0][0]['content']
    finally:
        await chat.close()
        store.close()


@pytest.mark.anyio
@pytest.mark.parametrize('enabled', [True, False])
async def test_planner_obeys_clock_switch(tmp_path, enabled):
    settings, store, model = Settings(tmp_path), Store(tmp_path), Model()
    settings.update({'model': {'base_url': 'https://fixture.invalid/v1', 'model': 'fixture'},
                     'onebot': {'enabled': True, 'access_token': 'clock-fixture-token-123'},
                     'time': {'awareness_enabled': enabled, 'timezone': 'UTC'}})
    scope = store.upsert_scope({'id': 'qq:group:202'})
    engine = SocialEngine(store, settings, model)
    try:
        message = store.add_message(scope['id'], '303', 'user', '你好', at_bot=True)
        await engine.plan(scope, message, direct=True)
        payload = json.loads(model.prompts[-1][0][-1]['content'])
        assert ('current_time' in payload) is enabled
        if enabled:
            assert payload['current_time']['timezone'] == 'UTC'
    finally:
        await engine.close()
        store.close()


def test_authenticated_settings_endpoint_accepts_clock_switch(tmp_path):
    app = create_app(tmp_path, start_workers=False)
    with TestClient(app) as client:
        password_login(client, app)
        response = client.put('/api/settings', json={'time': {'awareness_enabled': False, 'timezone': 'Asia/Hong_Kong'}})
        assert response.status_code == 200, response.text
        assert client.get('/api/settings').json()['time']['awareness_enabled'] is False
        assert client.put('/api/settings', json={'time': {'awareness_enabled': 'false'}}).status_code == 400


def test_clock_ui_render_and_basic_configuration_entry():
    node = shutil.which('node')
    if not node:
        pytest.skip('Optional frontend regression requires Node.js')
    result = subprocess.run([node, str(Path(__file__).with_name('frontend_time_awareness.cjs'))],
                            capture_output=True, text=True, encoding='utf-8', timeout=20)
    assert result.returncode == 0, result.stdout + result.stderr
