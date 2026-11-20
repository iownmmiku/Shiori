"""What a QQ message quotes (引用回复) and what it forwards (合并转发) reaches the chat model.

On the server 832 messages in three days quoted another one, 127 of them a picture, and the
model saw only the quoted message's number; a forwarded chat on its own was dropped as empty.
"""
import asyncio
import base64

import pytest

from shiori.chat import ChatService
from shiori.db import Store
from shiori.onebot import OneBotBridge
from shiori.settings import Settings


SCOPE = 'qq:group:202'
RAW = base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jRZkAAAAASUVORK5CYII=')
PICTURE = 'https://gchat.qpic.cn/fixture-cat.png'


@pytest.fixture
def anyio_backend():
    return 'asyncio'


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    settings, store = Settings(tmp_path), Store(tmp_path)
    settings.update({'model': {'base_url': 'https://fixture.invalid/v1', 'model': 'vision', 'vision': True},
        'onebot': {'enabled': True, 'chat_policy': 'blacklist', 'access_token': 'fixture-onebot-token'},
        'debounce': {'enabled': False}, 'stickers': {'enabled': False}, 'memory': {'organization_enabled': False}})
    store.upsert_scope({'id': SCOPE, 'auto_memory': False, 'plan_enabled': False, 'merge_window_ms': 0})
    calls = []

    class Model:
        async def complete(self, messages, **kwargs):
            if kwargs.get('purpose', 'chat') == 'chat':
                calls.append(messages)
            return {'content': '嗯。', 'message': {'role': 'assistant', 'content': '嗯。'}, 'tool_calls': [], 'usage': {}}
    chat = ChatService(store, settings, Model())
    bridge = OneBotBridge(store, settings, chat)
    bridge._self_id = '999'

    async def send(scope, text, **kwargs):
        return True
    bridge.send = send
    monkeypatch.setattr('shiori.stickers.download_image', lambda *a, **kw: (RAW, 'image/png'))
    yield store, chat, bridge, calls
    store.close()


class Connector:
    """Answers OneBot actions from a table; anything else fails."""

    def __init__(self, bridge, answers):
        self.bridge, self.answers, self.actions = bridge, answers, []

    async def send_json(self, action):
        self.actions.append(action)
        data = self.answers.get(action['action'])
        result = {'status': 'ok', 'retcode': 0, 'data': data} if data is not None else {'status': 'failed', 'retcode': 100}
        self.bridge._pending[action['echo']][1].set_result(result)


def event(identifier, segments, *, user=101, name='小明'):
    return {'post_type': 'message', 'message_type': 'group', 'group_id': 202, 'self_id': 999, 'user_id': user,
            'message_id': identifier, 'sender': {'card': name}, 'message': segments}


def at_bot(text, *, reply=None):
    return ([{'type': 'reply', 'data': {'id': str(reply)}}] if reply else []) + [
        {'type': 'at', 'data': {'qq': '999'}}, {'type': 'text', 'data': {'text': text}}]


def last_request(calls):
    """Text and pictures of the final user message of the latest chat call."""
    content = calls[-1][-1]['content']
    if isinstance(content, str):
        return content, []
    return (''.join(part.get('text', '') for part in content if part.get('type') == 'text'),
            [part for part in content if part.get('type') == 'image_url'])


async def finish(chat, bridge):
    await asyncio.sleep(0)
    for task in list(bridge._events):
        await asyncio.wait_for(task, 10)
    await bridge.stop()
    await chat.close()


@pytest.mark.anyio
async def test_a_quoted_picture_and_its_text_reach_the_model(runtime):
    store, chat, bridge, calls = runtime
    bridge._socket = Connector(bridge, {})
    try:
        await bridge.handle_event(event(11, [{'type': 'text', 'data': {'text': '看我家猫'}},
                                            {'type': 'image', 'data': {'url': PICTURE}}]))
        await bridge.handle_event(event(12, at_bot('这是什么品种', reply=11), user=102, name='小红'))
    finally:
        await finish(chat, bridge)
    text, pictures = last_request(calls)
    assert '【对方引用的消息】小明说：看我家猫（原消息的 1 张图附在本条最后）' in text and '这是什么品种' in text
    assert len(pictures) == 1 and pictures[0]['image_url']['url'].startswith('data:image/png')


@pytest.mark.anyio
async def test_a_quote_the_store_does_not_have_is_asked_from_the_connector(runtime):
    store, chat, bridge, calls = runtime
    connector = Connector(bridge, {'get_msg': {'sender': {'user_id': 103, 'nickname': '老王'},
                                               'message': [{'type': 'text', 'data': {'text': '周六去爬山吗'}},
                                                           {'type': 'image', 'data': {'url': PICTURE}}]}})
    bridge._socket = connector
    try:
        await bridge.handle_event(event(21, at_bot('你去吗', reply=5001)))
    finally:
        await finish(chat, bridge)
    assert [action['action'] for action in connector.actions] == ['get_msg']
    assert connector.actions[0]['params'] == {'message_id': 5001}
    text, pictures = last_request(calls)
    # Pictures only come from messages Shiori stored itself.
    assert '【对方引用的消息】老王说：周六去爬山吗（原消息带 1 张图，你看不到）' in text and not pictures


@pytest.mark.anyio
async def test_quoting_her_own_reply_names_her(runtime):
    store, chat, bridge, calls = runtime
    reply = store.add_message(SCOPE, 'assistant', 'assistant', '明天记得带伞哦')
    store.add_event('qq_sent', 'QQ 消息已确认发送', scope_id=SCOPE, platform_message_id='7001', assistant_message_id=reply['id'])
    bridge._socket = Connector(bridge, {})
    try:
        await bridge.handle_event(event(31, at_bot('你说的是哪天', reply=7001)))
    finally:
        await finish(chat, bridge)
    text, _ = last_request(calls)
    assert '【对方引用的消息】你说：明天记得带伞哦' in text
    stored = next(m for m in store.list_messages(SCOPE) if m['role'] == 'user')
    assert stored['metadata']['reply_to_bot'] is True and 'reply_to_source_id' not in stored['metadata']


@pytest.mark.anyio
async def test_a_forwarded_chat_is_read_and_kept(runtime):
    store, chat, bridge, calls = runtime
    connector = Connector(bridge, {'get_forward_msg': {'messages': [
        {'sender': {'nickname': '甲'}, 'message': [{'type': 'text', 'data': {'text': '晚上吃火锅'}}]},
        {'sender': {'nickname': '乙'}, 'message': [{'type': 'image', 'data': {'url': PICTURE}}]},
        {'sender': {'card': '丙'}, 'message': [{'type': 'forward', 'data': {'id': 'inner'}}]}]}})
    bridge._socket = connector
    try:
        await bridge.handle_event(event(41, [{'type': 'at', 'data': {'qq': '999'}}, {'type': 'forward', 'data': {'id': 'fw-1'}}]))
    finally:
        await finish(chat, bridge)
    assert connector.actions[0]['action'] == 'get_forward_msg' and connector.actions[0]['params']['message_id'] == 'fw-1'
    stored = next(m for m in store.list_messages(SCOPE) if m['role'] == 'user')
    assert stored['content'] == '【合并转发的聊天记录】\n甲：晚上吃火锅\n乙： [图片]\n丙： [合并转发]'
    assert '甲：晚上吃火锅' in last_request(calls)[0]


@pytest.mark.anyio
async def test_an_unreadable_forward_is_still_answered(runtime):
    store, chat, bridge, calls = runtime
    bridge._socket = Connector(bridge, {})
    try:
        await bridge.handle_event(event(51, [{'type': 'at', 'data': {'qq': '999'}}, {'type': 'forward', 'data': {'id': 'fw-2'}}]))
    finally:
        await finish(chat, bridge)
    stored = next(m for m in store.list_messages(SCOPE) if m['role'] == 'user')
    assert stored['content'] == '【合并转发的聊天记录，内容读不到】' and calls


def test_inline_forward_content_needs_no_connector():
    bridge = OneBotBridge.__new__(OneBotBridge)
    bridge._socket = None
    data = {'content': [{'type': 'node', 'data': {'nickname': '甲', 'content': [{'type': 'text', 'data': {'text': '早'}}]}}]}
    assert asyncio.run(bridge._forward_text(data)) == '【合并转发的聊天记录】\n甲：早'


def test_a_long_forward_is_cut_with_a_count():
    bridge = OneBotBridge.__new__(OneBotBridge)
    bridge._socket = None
    nodes = [{'sender': {'nickname': f'人{i}'}, 'message': '第%d句' % i} for i in range(40)]
    text = asyncio.run(bridge._forward_text({'content': nodes}))
    assert text.count('\n') == 31 and text.endswith('……后面还有 10 条')
