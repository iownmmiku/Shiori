"""Pauses between reply segments can follow how long each one takes to type, and a private chat
on the QQ official bot can show “正在输入…” while the reply is written."""
import asyncio
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from shiori.output import TYPING_MAX_MS, output_config, part_gap_seconds
from shiori.settings import DEFAULTS, Settings


@pytest.fixture
def anyio_backend():
    return 'asyncio'


def test_without_a_typing_speed_the_fixed_interval_stays():
    assert part_gap_seconds({'interval_ms': 600, 'typing_cpm': 0}, '一段很长很长很长的回复内容') == 0.6
    assert part_gap_seconds({'interval_ms': 0}, '好') == 0


def test_a_typing_speed_pauses_by_length_within_bounds():
    options = {'interval_ms': 600, 'typing_cpm': 300}
    assert part_gap_seconds(options, '十个字的一段回复内容') == 2.0          # 10 characters at 300 a minute
    assert part_gap_seconds(options, '好') == 0.6                          # never shorter than the interval
    assert part_gap_seconds({'interval_ms': 0, 'typing_cpm': 90}, '这' * 300) == TYPING_MAX_MS / 1000
    assert part_gap_seconds(options, '  好的  ') == 0.6                    # surrounding spaces are not typed


def test_typing_speed_is_off_or_thirty_to_six_hundred(tmp_path):
    config = deepcopy(DEFAULTS)
    assert output_config(config)['typing_cpm'] == 0
    config['output']['typing_cpm'] = 90
    assert output_config(config)['typing_cpm'] == 90
    for bad in (10, 601, '90', True):
        config['output']['typing_cpm'] = bad
        with pytest.raises(ValueError, match='typing_cpm'):
            output_config(config)
    settings = Settings(tmp_path)
    settings.update({'output': {'typing_cpm': 120}})
    assert settings.get()['output']['typing_cpm'] == 120
    with pytest.raises(ValueError, match='打字速度'):
        settings.update({'output': {'typing_cpm': 20}})
    with pytest.raises(ValueError, match='正在输入'):
        settings.update({'qq_official': {'input_notify': 'yes'}})


def test_a_chat_can_set_its_own_typing_speed(tmp_path):
    from shiori.db import Store
    store = Store(tmp_path)
    try:
        store.upsert_scope({'id': 'qq:group:1', 'output': {'typing_cpm': 200}})
        assert output_config(deepcopy(DEFAULTS), store.get_scope('qq:group:1'))['typing_cpm'] == 200
        with pytest.raises(ValueError, match='typing_cpm'):
            store.upsert_scope({'id': 'qq:group:1', 'output': {'typing_cpm': 10}})
    finally:
        store.close()


@pytest.mark.anyio
async def test_each_later_segment_waits_for_its_own_typing_time(tmp_path, monkeypatch):
    from shiori import social
    from shiori.chat import ChatService
    from shiori.db import Store
    from shiori.onebot import OneBotBridge
    settings, store = Settings(tmp_path), Store(tmp_path)
    settings.update({'model': {'base_url': 'https://fixture.invalid/v1', 'model': 'fixture'},
                     'onebot': {'enabled': True, 'chat_policy': 'blacklist', 'access_token': 'fixture-onebot-token'},
                     'output': {'mode': 'model', 'interval_ms': 0, 'typing_cpm': 300},
                     'debounce': {'enabled': False}, 'stickers': {'enabled': False}, 'memory': {'organization_enabled': False}})
    store.upsert_scope({'id': 'qq:private:101', 'auto_memory': False, 'plan_enabled': False, 'merge_window_ms': 0})

    class Model:
        async def complete(self, messages, **kwargs):
            reply = '好呀\n\n我刚到家，正准备做饭'
            return {'content': reply, 'message': {'role': 'assistant', 'content': reply}, 'tool_calls': [], 'usage': {}}
    gaps, sent = [], []
    real = social.part_gap_seconds

    def recording(options, part):
        gaps.append((part, real(options, part)))
        return 0
    monkeypatch.setattr(social, 'part_gap_seconds', recording)
    chat = ChatService(store, settings, Model())
    bridge = OneBotBridge(store, settings, chat)
    bridge._self_id = '999'

    async def send(scope, text, **kwargs):
        sent.append(text)
        return True
    bridge.send = send
    try:
        await bridge.handle_event({'post_type': 'message', 'message_type': 'private', 'self_id': 999, 'user_id': 101,
                                   'message_id': 1, 'message': [{'type': 'text', 'data': {'text': '在干嘛'}}]})
        for task in list(bridge._events):
            await asyncio.wait_for(task, 10)
    finally:
        await bridge.stop()
        await chat.close()
        store.close()
    assert sent == ['好呀', '我刚到家，正准备做饭']
    assert gaps == [('我刚到家，正准备做饭', 2.0)]          # only before the second part: 10 characters at 300 a minute


class OfficialSettings:
    def __init__(self, notify):
        self.config = {'paused': False, 'onebot': {'adapter': 'official'},
                       'qq_official': {'enabled': True, 'app_id': 'app', 'app_secret': 'test-secret', 'input_notify': notify,
                                       'allowed_groups': ['group-openid'], 'allowed_users': ['user-openid']}}

    def get(self):
        return self.config


class OfficialChat:
    def __init__(self, store):
        self.store = store

    async def respond(self, scope_id, user_id, text, images=None, metadata=None):
        self.store.add_message(scope_id, user_id, 'user', text, **metadata)
        return {'reply': '收到', 'message_id': 'reply-id'}


def official(tmp_path, notify, request=None):
    from qunyu.db import Store
    from shiori.official import OfficialBridge
    store = Store(tmp_path / 'official')
    bridge = OfficialBridge(store, OfficialSettings(notify), OfficialChat(store))
    api = SimpleNamespace(post_group_message=AsyncMock(return_value={'id': 'g'}), post_c2c_message=AsyncMock(return_value={'id': 'u'}),
                          _http=SimpleNamespace(request=request or AsyncMock(return_value={})))
    bridge._client = SimpleNamespace(api=api)
    bridge._running = bridge._connected = True
    return bridge, api, store


def private_message(target='user-openid'):
    return {'t': 'C2C_MESSAGE_CREATE', 'd': {'id': 'msg-1', 'author': {'user_openid': target}, 'content': '你好'}}


@pytest.mark.anyio
async def test_a_private_chat_shows_typing_before_the_reply(tmp_path):
    bridge, api, store = official(tmp_path, True)
    try:
        await bridge.handle_event(private_message())
        await asyncio.gather(*list(bridge._notify_tasks))
        route, = api._http.request.await_args.args
        assert route.method == 'POST' and route.url.endswith('/v2/users/user-openid/messages')
        assert api._http.request.await_args.kwargs['json'] == {
            'msg_type': 6, 'input_notify': {'input_type': 1, 'input_second': 60}, 'msg_id': 'msg-1', 'msg_seq': 99}
        api.post_c2c_message.assert_awaited_once_with(openid='user-openid', msg_type=0, content='收到', msg_id='msg-1', msg_seq=1)
    finally:
        store.close()


@pytest.mark.anyio
async def test_typing_is_off_by_default_and_never_in_groups(tmp_path):
    bridge, api, store = official(tmp_path, False)
    try:
        await bridge.handle_event(private_message())
        assert not api._http.request.called and api.post_c2c_message.called
        bridge.settings.config['qq_official']['input_notify'] = True
        await bridge.handle_event({'t': 'GROUP_AT_MESSAGE_CREATE', 'd': {'id': 'msg-2', 'group_openid': 'group-openid',
                                                                       'author': {'member_openid': 'member'}, 'content': '<@bot> 你好'}})
        assert not api._http.request.called and api.post_group_message.called
    finally:
        store.close()


@pytest.mark.anyio
async def test_a_failed_typing_status_does_not_hold_up_the_reply(tmp_path):
    bridge, api, store = official(tmp_path, True, request=AsyncMock(side_effect=RuntimeError('40034128')))
    try:
        await bridge.handle_event(private_message())
        await asyncio.gather(*list(bridge._notify_tasks), return_exceptions=True)
        assert api.post_c2c_message.called
        assert any(event['kind'] == 'qq_official_input_notify' for event in store.list_events(limit=20))
    finally:
        store.close()
