"""The chat prompt opens with the same bytes every turn, so a service's prompt cache can reuse it.

On the server the opening started with the current time, so every request differed from its
first character and the cache had nothing to reuse.
"""
from copy import deepcopy
from datetime import datetime, timedelta, timezone

import pytest

from shiori.chat import ChatService
from shiori.db import Store
from shiori.settings import DEFAULTS, resolve_model


class Settings:
    def __init__(self):
        self.data = deepcopy(DEFAULTS)
        self.data['model'].update(base_url='https://fixture.invalid/v1', model='fixture')

    def get(self):
        return deepcopy(self.data)


class Model:
    def __init__(self, settings):
        self.settings, self.calls = settings, []

    def resolve(self, purpose):
        return resolve_model(self.settings.get(), purpose)

    async def complete(self, messages, **kwargs):
        if kwargs.get('purpose', 'chat') == 'chat':
            self.calls.append(deepcopy(messages))
        return {'content': '好。', 'tool_calls': [], 'message': {'role': 'assistant', 'content': '好。'}, 'usage': {}}


@pytest.fixture
def anyio_backend():
    return 'asyncio'


@pytest.fixture
async def service(tmp_path, monkeypatch):
    from shiori import timezones
    now = [datetime(2027, 3, 1, 1, 0, tzinfo=timezone.utc)]

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return now[0].astimezone(tz) if tz else now[0].replace(tzinfo=None)
    monkeypatch.setattr(timezones, 'datetime', Clock)
    store, settings = Store(tmp_path), Settings()
    store.upsert_scope({'id': 'web:default', 'auto_memory': False, 'allow_tools': False})
    store.save_persona({**store.get_persona('default'), 'system_prompt': '温暖自然的聊天伙伴。', 'profile': {}})
    model = Model(settings)
    chat = ChatService(store, settings, model)
    try:
        yield store, model, chat, now
    finally:
        await chat.close()
        store.close()


@pytest.mark.anyio
async def test_the_opening_is_the_same_bytes_every_turn(service):
    store, model, chat, now = service
    await chat.respond('web:default', 'owner', '早上好')
    now[0] += timedelta(hours=3, minutes=17)
    await chat.respond('web:default', 'owner', '中午吃什么')
    first, second = model.calls
    assert first[0] == second[0]
    assert '【现实时间】' not in first[0]['content'] and '请求者ID' not in first[0]['content']
    # What changes per turn follows the opening; the clock sits right before the request.
    assert first[1]['content'] == second[1]['content'] == '请求者ID=owner。'
    clocks = [next(m['content'] for m in call if str(m['content']).startswith('【现实时间】')) for call in (first, second)]
    assert clocks[0] != clocks[1] and '09:00:00+08:00' in clocks[0] and '12:17:00+08:00' in clocks[1]
    assert all(call.index(next(m for m in call if str(m['content']).startswith('【现实时间】'))) > 2 for call in (first, second))


@pytest.mark.anyio
async def test_recalled_memories_and_a_reply_plan_do_not_change_the_opening(service, monkeypatch):
    store, model, chat, now = service
    scope = store.get_scope('web:default')
    monkeypatch.setattr(store, 'allowed_memory_sources', lambda *args, **kwargs: [])
    plain = chat._context(scope, 'owner', '你好', [], [], [], 'current', {}, history=[], group_request=None)
    memory = {'id': 'm1', 'kind': 'fact', 'content': '对方喜欢猫', 'scope_id': 'web:default', 'created_at': '2027-01-01T00:00:00Z'}
    planned = chat._context(scope, 'owner', '我喜欢什么', [], [memory], [], 'current',
                            {'planner': {'intent': '回答喜好', 'speaker_intent': '想知道自己喜欢什么', 'audience': 'bot'}},
                            history=[], group_request=None)
    assert plain[0] == planned[0]
    assert '长期记忆是从过去对话整理出的背景记录' in planned[1]['content']
    assert 'reply_plan来自回复前的判断' in planned[1]['content']
    assert '对方喜欢猫' in planned[2]['content']
