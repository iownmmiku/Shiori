"""Joining in (复读): a run of identical short lines in a group, sent once more, without a model."""
import asyncio
from copy import deepcopy

import pytest

from shiori.db import Store
from shiori.settings import DEFAULTS
from shiori.social import ConversationFlow, SocialEngine

GROUP = 'qq:group:202'


@pytest.fixture
def anyio_backend():
    return 'asyncio'


class Config:
    def __init__(self):
        self.config = deepcopy(DEFAULTS)
        self.config['onebot'].update(enabled=True, chat_policy='blacklist')
        self.config['debounce'].update(enabled=False)
        self.config['model'].update(base_url='https://fixture.invalid/v1', model='fixture')
        self.config['output'].update(enabled=False)

    def get(self):
        return deepcopy(self.config)


class Model:
    def __init__(self):
        self.calls = []

    async def complete(self, messages, **kwargs):
        self.calls.append(kwargs.get('purpose'))
        return {'content': '{"action": "skip", "reason": "测试"}'}


class Chat:
    def __init__(self, store, settings):
        self.store, self.settings = store, settings
        self.provider = Model()
        self.social = SocialEngine(store, settings, self.provider)

    async def observe(self, scope_id, user_id, text, images=None, metadata=None):
        message = self.store.add_message(scope_id, str(user_id), 'user', text, **dict(metadata or {}, images=images or []))
        self.social.observe(scope_id, message)
        return message


@pytest.fixture
def setup(tmp_path):
    store, config = Store(tmp_path), Config()
    chat = Chat(store, config)
    store.upsert_scope({'id': GROUP, 'plan_enabled': False, 'auto_memory': False, 'echo_repeat': 3})
    sent = []

    async def emit(scope_id, text, metadata):
        sent.append((scope_id, text, metadata))
        return True

    flow = ConversationFlow(store, config, chat, emit, lambda *_: True)
    yield store, chat, flow, sent
    asyncio.run(asyncio.sleep(0))
    store.close()


async def say(flow, user, text, *, scope=GROUP, images=None, **metadata):
    result = await flow.handle(scope, user, text, images or [], dict({'at_bot': False}, **metadata), wait=False)
    for task in list(flow._jobs):
        task.cancel()
    return result


def echoes(sent):
    # A joined line carries only these; replies and failure notices carry more.
    return [text for _, text, metadata in sent if set(metadata) == {'assistant_message_id', 'output_part'}]


@pytest.mark.anyio
async def test_three_members_saying_the_same_line_get_it_once_more(setup):
    store, chat, flow, sent = setup
    for user in ('1', '2'):
        assert (await say(flow, user, '草'))['code'] != 'echoed'
    assert echoes(sent) == []
    assert (await say(flow, '3', ' 草 '))['code'] == 'echoed'
    assert echoes(sent) == ['草']
    assert sent[-1][2]['output_part'] == {'index': 1, 'total': 1}
    # The same run is joined once; a fourth member does not make her say it again.
    await say(flow, '4', '草')
    assert echoes(sent) == ['草']
    mine = [m for m in store.list_messages(GROUP) if m['role'] == 'assistant']
    assert [m['content'] for m in mine] == ['草'] and mine[0]['metadata']['echo'] is True
    assert [e['details']['text'] for e in store.list_events(limit=20) if e['kind'] == 'qq_echo'] == ['草']
    assert 'chat' not in chat.provider.calls                                   # no model wrote it


@pytest.mark.anyio
async def test_a_run_needs_different_members_one_after_another(setup):
    store, _, flow, sent = setup
    for user in ('1', '1', '1', '2'):                    # one member repeating counts once
        await say(flow, user, '好耶')
    for user, line in (('3', '收到'), ('4', '收到'), ('5', '别的'), ('6', '收到')):   # broken by another line
        await say(flow, user, line)
    assert echoes(sent) == []


@pytest.mark.anyio
@pytest.mark.parametrize('kind', ['picture', 'mention', 'quote', 'at_bot', 'command', 'long', 'placeholder'])
async def test_only_short_plain_text_is_joined(setup, kind):
    store, _, flow, sent = setup
    line, extra = {'command': ('/状态', {}), 'long': ('长' * 51, {}), 'placeholder': ('[动画表情]', {})}.get(kind, ('哈哈', {}))
    extra = {'picture': {'images': ['data:image/png;base64,AAAA']}, 'mention': {'mentioned_user_ids': ['9']},
             'quote': {'reply_to_id': '12345'}, 'at_bot': {'at_bot': True}}.get(kind, extra)
    for user in ('1', '2', '3'):
        await say(flow, user, line, **extra)
    assert echoes(sent) == []


@pytest.mark.anyio
async def test_off_by_default_in_private_chats_quiet_hours_and_right_after_another_run(setup):
    store, _, flow, sent = setup
    store.upsert_scope({'id': 'qq:group:303', 'plan_enabled': False, 'auto_memory': False})
    for user in ('1', '2', '3'):
        await say(flow, user, '冲', scope='qq:group:303')            # echo_repeat not set: off
        await say(flow, user, '冲', scope='qq:private:303')
    assert echoes(sent) == []
    store.upsert_scope({'id': GROUP, 'activity': 'quiet'})
    for user in ('1', '2', '3'):
        await say(flow, user, '安静')
    assert echoes(sent) == []
    store.upsert_scope({'id': GROUP, 'activity': 'balanced'})
    for user in ('1', '2', '3'):
        await say(flow, user, '一')
    for user in ('4', '5', '6'):
        await say(flow, user, '二')                                   # within the cooldown
    assert echoes(sent) == ['一']
    flow._echoed_at[GROUP] -= 121
    for user in ('7', '8', '9'):
        await say(flow, user, '三')
    assert echoes(sent) == ['一', '三']


@pytest.mark.anyio
async def test_two_is_the_least_and_an_undelivered_line_lets_the_message_go_on(tmp_path):
    store, config = Store(tmp_path), Config()
    chat = Chat(store, config)
    store.upsert_scope({'id': GROUP, 'plan_enabled': False, 'auto_memory': False, 'echo_repeat': 2})

    async def refuse(scope_id, text, metadata):
        return False

    flow = ConversationFlow(store, config, chat, refuse, lambda *_: True)
    try:
        await say(flow, '1', '早')
        result = await say(flow, '2', '早')
        assert result['code'] != 'echoed'
        assert [e['message'] for e in store.list_events(limit=20) if e['kind'] == 'qq_echo'] == ['跟着复读未确认送达']
    finally:
        store.close()


def test_the_setting_is_zero_or_two_to_ten(tmp_path):
    store = Store(tmp_path)
    try:
        assert store.upsert_scope({'id': GROUP})['echo_repeat'] == 0
        assert store.upsert_scope({'id': GROUP, 'echo_repeat': 10})['echo_repeat'] == 10
        for bad in (1, 11, True, '3', 2.0):
            with pytest.raises(ValueError, match='跟着复读'):
                store.upsert_scope({'id': GROUP, 'echo_repeat': bad})
    finally:
        store.close()


def test_the_settings_page_saves_it_through_the_api(tmp_path):
    from fastapi.testclient import TestClient
    from shiori.api import create_app
    from auth_helpers import password_login
    app = create_app(tmp_path / 'api', start_workers=False)
    with TestClient(app) as client:
        password_login(client, app)
        saved = client.put('/api/scopes/' + GROUP, json={'echo_repeat': 3})
        assert saved.status_code == 200, saved.text
        assert saved.json()['echo_repeat'] == 3
        assert client.put('/api/scopes/' + GROUP, json={'echo_repeat': 1}).status_code == 400
        assert client.put('/api/scopes/' + GROUP, json={'echo_repeat': '3'}).status_code == 400
        assert client.put('/api/scopes/' + GROUP, json={'echo_repeat': 0}).json()['echo_repeat'] == 0
