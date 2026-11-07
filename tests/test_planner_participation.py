"""Planner participation: picture-only text, the unprompted-reply cap, room
context and the Planner's output budget. Local fakes only."""
import json
import time
from copy import deepcopy

import pytest

from shiori.db import Store
from shiori.onebot import OneBotBridge
from shiori.settings import DEFAULTS, Settings as SettingsStore
from shiori.social import (IMAGE_ONLY_TEXT, LEGACY_IMAGE_TEXT, PLANNER_MAX_TOKENS, SocialEngine,
                           _topic, plain_text)

GROUP = 'qq:group:202'


@pytest.fixture
def anyio_backend():
    return 'asyncio'


class Settings:
    def __init__(self):
        self.config = deepcopy(DEFAULTS)
        self.config['model'].update(base_url='https://test.invalid/v1', model='mock')
        self.config['onebot'].update(enabled=True, chat_policy='blacklist')
        self.config['identity'].update(respond_to_at=True, respond_to_name=True)

    def get(self):
        return deepcopy(self.config)


class Planner:
    def __init__(self, action='reply'):
        self.calls, self.action = [], action

    async def complete(self, messages, **kwargs):
        self.calls.append((messages, kwargs))
        payload = json.loads(messages[-1]['content'])
        return {'content': json.dumps({'action': self.action, 'target_message_id': payload['current_message_id'],
            'target_user_id': payload['current_user_id'], 'addressed_to_bot': False,
            'intent': '补充一句具体信息', 'reason': '可以补充', 'needed_info': []})}

    def payload(self, index=-1):
        return json.loads(self.calls[index][0][-1]['content'])


@pytest.fixture
def setup(tmp_path):
    store, settings, planner = Store(tmp_path), Settings(), Planner()
    store.upsert_scope({'id': GROUP, 'cooldown_seconds': 0, 'merge_window_ms': 0})
    social = SocialEngine(store, settings, planner)
    yield store, settings, planner, social
    store.close()


def say(store, social, text, user='303', **metadata):
    record = store.add_message(GROUP, user, 'user', text, **metadata)
    social.observe(GROUP, record)
    return record


def test_picture_markers_are_neutral_and_carry_no_topic():
    assert plain_text(LEGACY_IMAGE_TEXT) == IMAGE_ONLY_TEXT
    assert plain_text('请看看这张图片。还有这个') == '请看看这张图片。还有这个'
    assert _topic(LEGACY_IMAGE_TEXT) == set() and _topic(IMAGE_ONLY_TEXT) == set()
    # Two unrelated pictures must not look like a same-topic follow-up.
    assert not _topic('[图片]') & _topic('[图片]')


@pytest.mark.anyio
async def test_picture_only_group_message_is_stored_with_a_neutral_marker(tmp_path):
    store, captured = Store(tmp_path), []

    class Chat:
        def observe(self, *args, **kwargs):
            pass

    bridge = OneBotBridge(store, Settings(), Chat())

    async def handle(scope_id, user_id, text, images, metadata, **kwargs):
        captured.append((text, images))
    bridge._flow.handle = handle
    try:
        await bridge.handle_event({'post_type': 'message', 'message_type': 'group', 'group_id': 202,
                                   'self_id': 999, 'user_id': 303, 'message_id': 'p1',
                                   'message': [{'type': 'image', 'data': {'file': 'abc.png'}}]})
        assert captured and captured[0][0] == IMAGE_ONLY_TEXT and captured[0][1]
    finally:
        await bridge.stop()
        store.close()


@pytest.mark.anyio
async def test_planner_sees_old_placeholders_as_pictures_and_the_room_state(setup):
    store, _, planner, social = setup
    say(store, social, LEGACY_IMAGE_TEXT, user='404')
    say(store, social, '这个能刷分吗', user='505')
    current = say(store, social, LEGACY_IMAGE_TEXT, user='303')
    await social.plan(store.get_scope(GROUP), current)
    payload = planner.payload()
    assert payload['current_message_text'] == IMAGE_ONLY_TEXT
    assert [item['text'] for item in payload['history']] == [IMAGE_ONLY_TEXT, '这个能刷分吗']
    room = payload['participation']['room']
    assert room['messages_last_2min'] == 3 and room['speakers_last_2min'] == 3
    assert room['unprompted_limit'] == 3 and room['your_unprompted_replies_in_window'] == 0
    assert '成员之间互相提问不是在问你' in payload['participation']['policy']
    assert planner.calls[0][1]['max_tokens'] == PLANNER_MAX_TOKENS >= 1024


@pytest.mark.anyio
async def test_unprompted_replies_are_capped_without_calling_the_planner(setup):
    store, settings, planner, social = setup
    for _ in range(3):
        social.note_reply(GROUP, '303', proactive=True)
    social.note_reply(GROUP, '303')  # an @ reply never counts toward the cap
    # A member the bot was not just talking with: no Planner call at all.
    result = await social.plan(store.get_scope(GROUP), say(store, social, '今天重听了电子音乐专辑', user='404'))
    assert not result['reply'] and '上限' in result['reason'] and not planner.calls
    # Addressed messages are not limited.
    result = await social.plan(store.get_scope(GROUP), say(store, social, '这个怎么看', at_bot=True))
    assert result['reply']
    # Once the unprompted replies leave the window (and the raised bar has
    # settled), judging resumes.
    for index in range(3):
        social._proactive_replies[GROUP][index] -= 1801
    result = await social.plan(store.get_scope(GROUP), say(store, social, '电子音乐专辑推荐一下', user='404'))
    assert result['reply'] and planner.calls
    assert planner.payload()['participation']['room']['your_unprompted_replies_in_window'] == 0
    assert planner.payload()['participation']['room']['your_replies_last_10min'] == 4


@pytest.mark.anyio
async def test_zero_disables_the_cap_but_not_the_raised_bar(setup):
    store, settings, planner, social = setup
    settings.config['planner'].update(eagerness=100, proactive_max_replies=3)
    for _ in range(4):
        social.note_reply(GROUP, '303', proactive=True)
    for index in range(4):
        social._proactive_replies[GROUP][index] -= 590  # inside the cap window, bar mostly settled
    result = await social.plan(store.get_scope(GROUP), say(store, social, '今天重听了电子音乐专辑', user='404'))
    assert not result['reply'] and '上限' in result['reason'] and not planner.calls
    settings.config['planner']['proactive_max_replies'] = 0
    result = await social.plan(store.get_scope(GROUP), say(store, social, '今天重听了电子音乐专辑', user='404'))
    assert result['reply'] and planner.calls


def test_proactive_cap_settings_are_validated(tmp_path):
    store = SettingsStore(tmp_path)
    assert store.get()['planner']['proactive_max_replies'] == 3
    assert store.get()['planner']['proactive_window_seconds'] == 600
    store.update({'planner': {'proactive_max_replies': 0, 'proactive_window_seconds': 1800}})
    assert store.get()['planner']['proactive_max_replies'] == 0
    for bad in ({'proactive_max_replies': 31}, {'proactive_max_replies': -1}, {'proactive_window_seconds': 30},
                {'proactive_max_replies': 2.5}):
        with pytest.raises(ValueError):
            store.update({'planner': bad})


@pytest.mark.anyio
async def test_flow_counts_only_unprompted_group_replies(tmp_path):
    from shiori.chat import ChatService

    class Model:
        async def complete(self, *args, **kwargs):
            return {'content': '好呀', 'message': {'role': 'assistant', 'content': '好呀'}, 'tool_calls': [], 'usage': {}}

    store, settings = Store(tmp_path), Settings()
    settings.config['output'].update(mode='model', interval_ms=0)
    settings.config['stickers']['enabled'] = False
    store.upsert_scope({'id': GROUP, 'plan_enabled': False, 'auto_memory': False, 'merge_window_ms': 0})
    chat = ChatService(store, settings, Model())
    bridge = OneBotBridge(store, settings, chat)

    async def emit(scope_id, text, metadata):
        return True
    bridge._flow.emit = emit
    try:
        await bridge._flow.handle(GROUP, '303', '在吗', [], {'at_bot': True}, direct=True)
        social = chat.social
        assert len(social._bot_replies[GROUP]) == 1 and not social._proactive_replies[GROUP]
        social.note_reply(GROUP, '303', proactive=True)
        assert social._within(social._proactive_replies[GROUP], 600, time.monotonic()) == 1
    finally:
        await bridge.stop()
        await chat.close()
        store.close()
