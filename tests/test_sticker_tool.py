"""The chat model sends stickers with a tool (server, 2026-11-17).

Asked "能不能发个表情包", the bot answered that it had none. Stickers were
chosen by a separate model call after the reply; the thinking model routed
there spent its 120-token budget before answering, so in a day none of the
46 uploaded stickers went out, and the chat model knew nothing about them.
Now the chat model has a send_sticker tool in QQ chats that can receive one.
The send probability decides on which replies it is invited to add one of
its own accord; when asked, it can always send one.
"""
import asyncio
import io
import json
from copy import deepcopy

import pytest
from PIL import Image

from shiori.chat import ChatService
from shiori.db import Store
from shiori.settings import DEFAULTS
from shiori.social import requested_sticker

PRIVATE, GROUP = 'qq:private:303', 'qq:group:404'


@pytest.fixture
def anyio_backend():
    return 'asyncio'


class Config:
    def __init__(self):
        self.config = deepcopy(DEFAULTS)
        self.config['onebot'].update(enabled=True, chat_policy='blacklist')
        self.config['output'].update(mode='model', interval_ms=0)
        self.config['model'].update(base_url='https://fixture.invalid/v1', model='fixture')
        self.config['stickers'].update(send_probability=0)

    def get(self):
        return deepcopy(self.config)


def answer(content='', calls=()):
    message = {'role': 'assistant', 'content': content}
    if calls:
        message['tool_calls'] = list(calls)
    return {'content': content, 'tool_calls': list(calls), 'message': message,
            'usage': {'input_tokens': 1, 'output_tokens': 1}}


def call(category='开心', meaning='高兴'):
    return {'id': 'call-1', 'type': 'function', 'function': {
        'name': 'send_sticker', 'arguments': json.dumps({'category': category, 'meaning': meaning}, ensure_ascii=False)}}


class Model:
    """Plays the chat model's turns in order; other purposes get an empty object."""

    def __init__(self, *turns):
        self.turns, self.seen = list(turns), []

    async def complete(self, messages, **kwargs):
        if kwargs.get('purpose', 'chat') != 'chat':
            return answer('{}')
        self.seen.append({'system': messages[0]['content'], 'tools': kwargs.get('tools') or []})
        return self.turns.pop(0) if self.turns else answer('嗯。')


def png(color):
    out = io.BytesIO()
    Image.new('RGB', (16, 16), color).save(out, format='PNG')
    return out.getvalue()


@pytest.fixture
def make(tmp_path):
    made = []

    def build(model, categories=('开心',)):
        store, config = Store(tmp_path / str(len(made))), Config()
        for scope in (PRIVATE, GROUP):
            store.upsert_scope({'id': scope, 'auto_memory': False, 'merge_window_ms': 0, 'plan_enabled': False})
        chat = ChatService(store, config, model)
        for index, category in enumerate(categories):
            chat.stickers.upload([(f'{index}.png', png(('red', 'blue', 'green')[index]))], category)
        for item in chat.stickers.page()['items']:
            chat.stickers.update(item['id'], {'clear': True, 'meaning': item['category'] + '的表情',
                                              'category': item['category'], 'tags': [item['category']]})
        made.append((store, chat))
        return store, config, chat
    yield build
    for store, chat in made:
        asyncio.run(chat.close())
        store.close()


def offered(tools):
    return next((tool['function'] for tool in tools or [] if tool['function']['name'] == 'send_sticker'), None)


async def over_qq(store, config, chat, text, scope=PRIVATE):
    """One message through the OneBot flow; returns the segments that went out."""
    from shiori.onebot import OneBotBridge
    bridge = OneBotBridge(store, config, chat)
    sent = []

    class Socket:
        async def send_json(self, action):
            sent.append(action)
            bridge._pending[action['echo']][1].set_result({'status': 'ok', 'retcode': 0, 'data': {'message_id': len(sent)}})
    bridge._socket = Socket()
    try:
        await asyncio.wait_for(bridge._flow.handle(scope, '303', text, [], {'at_bot': True}, direct=True), 10)
        if bridge._flow._sticker_jobs:
            await asyncio.wait_for(asyncio.gather(*list(bridge._flow._sticker_jobs)), 5)
    finally:
        await bridge.stop()
    return [action['params']['message'][0] for action in sent]


def test_the_tool_is_offered_only_where_a_sticker_can_be_sent(make):
    store, config, chat = make(Model(), categories=('害羞', '开心'))
    tool = offered(chat._request_tools(store.get_scope(PRIVATE), '303', '你好', {}))
    # Only the categories that hold a sticker, in the fixed order.
    assert tool['parameters']['properties']['category']['enum'] == ['开心', '害羞']
    assert tool['parameters']['required'] == ['category']
    assert chat._sticker_tool('web:default') is None                  # the local chat cannot show one
    store.upsert_scope({'id': PRIVATE, 'allow_tools': False})
    assert chat._request_tools(store.get_scope(PRIVATE), '303', '你好', {}) is None
    store.upsert_scope({'id': PRIVATE, 'allow_tools': True})
    config.config['stickers']['send_enabled'] = False
    assert chat._sticker_tool(PRIVATE) is None
    config.config['stickers']['send_enabled'] = True
    config.config['onebot']['blocked_users'] = ['303']
    assert chat._sticker_tool(PRIVATE) is None
    _, _, empty = make(Model(), categories=())
    assert empty._sticker_tool(PRIVATE) is None


def test_the_send_probability_decides_which_replies_are_invited(make):
    store, config, chat = make(Model())
    scope = store.get_scope(PRIVATE)
    rolls = iter([.3, .7])
    chat.stickers.random = lambda: next(rolls)
    config.config['stickers']['send_probability'] = .5
    invited, plain = {}, {}
    hit = offered(chat._request_tools(scope, '303', '今天考过了', invited))
    miss = offered(chat._request_tools(scope, '303', '今天考过了', plain))
    assert invited['_sticker_invited'] is True and '主动配一张' in hit['description']
    assert plain['_sticker_invited'] is False and '不要主动调用' in miss['description']
    # Either way the model may send one when asked.
    assert '对方要你发表情包时调用' in hit['description'] and '对方要你发表情包时调用' in miss['description']


@pytest.mark.anyio
@pytest.mark.parametrize('scope', [PRIVATE, GROUP])
async def test_asked_for_a_sticker_the_model_sends_one_after_its_text(make, scope):
    model = Model(answer('', [call('开心', '高兴')]), answer('给你。'))
    store, config, chat = make(model)
    segments = await over_qq(store, config, chat, '能不能发个表情包', scope)
    assert [segment['type'] for segment in segments] == ['text', 'image']
    assert segments[0]['data']['text'] == '给你。' and segments[1]['data']['file'].startswith('base64://')
    assert chat.stickers.page()['items'][0]['use_count'] == 1
    first = model.seen[0]
    assert offered(first['tools']) and '不要说自己没有表情包' in first['system']
    assert '顺手配一张' not in first['system']                       # probability 0: only when asked


@pytest.mark.anyio
async def test_a_merged_group_turn_may_send_a_sticker(make):
    # Members of one merged turn cannot lend each other permissions, but a
    # sticker only decorates the shared reply.
    model = Model(answer('', [call('开心')]), answer('都给你们。'))
    store, config, chat = make(model)
    entries = []
    for user, text in (('303', '发个表情包'), ('505', '我也要')):
        message = store.add_message(GROUP, user, 'user', text)
        entries.append({'message': message, 'text': text, 'images': []})
    result = await chat.respond_group(GROUP, entries)
    assert result['reply'] == '都给你们。' and requested_sticker(result)['category'] == '开心'
    names = [tool['function']['name'] for tool in model.seen[0]['tools']]
    assert 'send_sticker' in names and 'remember' not in names


@pytest.mark.anyio
async def test_text_written_with_the_sticker_call_needs_no_second_model_call(make):
    model = Model(answer('给你～', [call('开心')]))
    store, config, chat = make(model)
    segments = await over_qq(store, config, chat, '发个表情包')
    assert [segment['type'] for segment in segments] == ['text', 'image']
    assert segments[0]['data']['text'] == '给你～' and len(model.seen) == 1


@pytest.mark.anyio
async def test_an_invited_reply_may_go_without_a_sticker(make):
    model = Model(answer('恭喜。'))
    store, config, chat = make(model)
    config.config['stickers']['send_probability'] = 1
    segments = await over_qq(store, config, chat, '我今天考过了')
    assert [segment['type'] for segment in segments] == ['text']
    assert '顺手配一张' in model.seen[0]['system']


@pytest.mark.anyio
async def test_a_sticker_call_without_any_text_does_not_send_a_tool_report(make):
    model = Model(answer('', [call('开心')]), answer(''), answer(''), answer(''))
    store, config, chat = make(model)
    segments = await over_qq(store, config, chat, '发个表情包')
    texts = [segment['data'].get('text', '') for segment in segments if segment['type'] == 'text']
    assert not any('send_sticker' in text or '已执行' in text for text in texts)
    assert all(segment['type'] != 'image' for segment in segments)


def test_a_failed_or_unknown_action_is_not_taken_for_a_sticker():
    assert requested_sticker({'actions': [{'tool': 'send_sticker', 'ok': False, 'error': '没有'}]}) is None
    assert requested_sticker({'actions': [{'tool': 'remember', 'ok': True, 'result': {'sticker': {'id': 'x'}}}]}) is None
    assert requested_sticker({'reply': '好'}) is None
    sticker = {'id': 'x', 'version': 2}
    assert requested_sticker({'actions': [{'tool': 'send_sticker', 'ok': True, 'result': {'sticker': sticker}}]}) == sticker
