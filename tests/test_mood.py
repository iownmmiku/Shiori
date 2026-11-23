"""Her mood rises with what people say to her, fades by half-lives, and colours the next reply."""
import asyncio
import json
from copy import deepcopy

import pytest

from shiori import mood
from shiori.chat import ChatService
from shiori.db import Store
from shiori.settings import DEFAULTS, resolve_model


def test_each_feeling_fades_by_its_own_half_life():
    after = mood.decayed({'joy': 1.0, 'annoyance': 1.0, 'warmth': 1.0}, 45)
    assert after['joy'] == pytest.approx(0.5)                   # joy halves in 45 minutes
    assert after['annoyance'] == pytest.approx(0.5 ** 1.5)      # annoyance in 30
    assert after['warmth'] == pytest.approx(0.5 ** (45 / 240))  # warmth lingers for hours


def test_a_nudge_is_weighted_and_never_goes_past_full():
    once = mood.stirred({}, {'joy': 0.8}, mood.WEIGHTS['bystander'])
    assert once['joy'] == pytest.approx(0.2)
    full = mood.stirred({'joy': 0.9}, {'joy': 1.0}, mood.WEIGHTS['owner'])
    assert full['joy'] == pytest.approx(1.0)
    assert mood.stirred({'joy': 0.5}, {'joy': 5, 'shyness': -1}, 1.0)['shyness'] == 0


def test_only_clear_feelings_are_described_strongest_first():
    assert mood.describe({'joy': 0.1, 'worry': 0.05}) == ''
    line = mood.describe({'joy': 0.7, 'shyness': 0.4, 'worry': 0.2, 'sadness': 0.16, 'warmth': 0.1})
    assert line.startswith('你现在的心情：很开心、比较害羞、有点担心。') and '难过' not in line and '亲近' not in line


def test_who_spoke_decides_the_weight():
    assert mood.weight_for({'is_admin': True}, 'qq:group:1') == 1.0
    assert mood.weight_for({}, 'web:default') == 1.0
    assert mood.weight_for({}, 'qq:private:9') == 0.65
    assert mood.weight_for({'at_bot': True}, 'qq:group:1') == 0.65
    assert mood.weight_for({}, 'qq:group:1') == 0.25


def test_the_state_survives_a_restart_and_keeps_fading(tmp_path):
    now = [1_000_000.0]
    path = tmp_path / 'mood.json'
    first = mood.Mood(path, clock=lambda: now[0])
    first.feel('default', {'joy': 1.0}, 1.0)
    now[0] += 45 * 60
    again = mood.Mood(path, clock=lambda: now[0])
    assert again.current('default')['joy'] == pytest.approx(0.5)
    assert again.current('someone-else') == {name: 0.0 for name in mood.NAMES}


@pytest.mark.parametrize('answer, expected', [
    ('{"joy":0.6,"sadness":0,"worry":0,"annoyance":0,"warmth":0.3,"shyness":0.2}', {'joy': 0.6, 'warmth': 0.3, 'shyness': 0.2}),
    ('想了想：{"joy":2,"sadness":0,"worry":0,"annoyance":0,"warmth":0,"shyness":0}', {'joy': 1.0}),
    ('不知道', None),
    ('{"joy":"很多"}', None),
])
def test_the_estimate_is_read_strictly(answer, expected):
    async def complete(messages):
        return {'content': answer}
    impulses = asyncio.run(mood.appraise(complete, '佐藤栞', '你今天好可爱', '哼…谢谢'))
    if expected is None:
        assert impulses is None
    else:
        assert {key: value for key, value in impulses.items() if value} == expected


class Settings:
    def __init__(self, enabled):
        self.data = deepcopy(DEFAULTS)
        self.data['model'].update(base_url='https://fixture.invalid/v1', model='fixture')
        self.data['mood'] = {'enabled': enabled}

    def get(self):
        return deepcopy(self.data)


class Model:
    def __init__(self, settings):
        self.settings, self.purposes, self.chats = settings, [], []

    def resolve(self, purpose):
        return resolve_model(self.settings.get(), purpose)

    async def complete(self, messages, **kwargs):
        purpose = kwargs.get('purpose', 'chat')
        self.purposes.append(purpose)
        if purpose == 'gate':
            return {'content': json.dumps({'joy': 0.9, 'sadness': 0, 'worry': 0, 'annoyance': 0, 'warmth': 0.5, 'shyness': 0.6}),
                    'tool_calls': [], 'usage': {}}
        self.chats.append(messages)
        return {'content': '嗯。', 'tool_calls': [], 'message': {'role': 'assistant', 'content': '嗯。'}, 'usage': {}}


@pytest.fixture
def anyio_backend():
    return 'asyncio'


async def settle(chat):
    for task in list(chat._jobs):
        await task


@pytest.mark.anyio
async def test_a_reply_moves_her_mood_and_the_next_reply_carries_it(tmp_path):
    store, settings = Store(tmp_path), Settings(True)
    store.upsert_scope({'id': 'web:default', 'auto_memory': False, 'allow_tools': False})
    model = Model(settings)
    chat = ChatService(store, settings, model)
    try:
        await chat.respond('web:default', 'owner', '你今天穿得好可爱')
        assert '你现在的心情' not in model.chats[0][1]['content']        # nothing felt yet
        await settle(chat)
        assert model.purposes.count('gate') == 1
        assert json.loads((tmp_path / 'mood.json').read_text('utf-8'))['default']['feelings']['joy'] == pytest.approx(0.9)
        await chat.respond('web:default', 'owner', '真的')
        assert '你现在的心情：很开心、比较害羞、比较亲近。' in model.chats[1][1]['content']
        assert '你现在的心情' not in model.chats[1][0]['content']         # the cached opening stays the same
        assert any(event['kind'] == 'mood' for event in store.list_events(limit=20))
    finally:
        await chat.close()
        store.close()


@pytest.mark.anyio
async def test_switched_off_nothing_is_evaluated_or_said(tmp_path):
    store, settings = Store(tmp_path), Settings(False)
    store.upsert_scope({'id': 'web:default', 'auto_memory': False, 'allow_tools': False})
    model = Model(settings)
    chat = ChatService(store, settings, model)
    try:
        chat.mood.feel('default', {'joy': 1.0}, 1.0)                     # even with a mood on file
        await chat.respond('web:default', 'owner', '你今天穿得好可爱')
        await settle(chat)
        assert 'gate' not in model.purposes
        assert all('心情' not in str(message['content']) for message in model.chats[0])
    finally:
        await chat.close()
        store.close()


def test_the_switch_is_a_boolean():
    assert mood.validate({}) == {'enabled': False}
    with pytest.raises(ValueError):
        mood.validate({'enabled': 'yes'})
