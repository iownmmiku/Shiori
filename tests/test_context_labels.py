"""Context labels a model copied into its reply never reach the chat or the next prompt.

The examples are replies the server actually sent on 2026-11-19.
"""
from copy import deepcopy

import pytest

from shiori.chat import ChatService
from shiori.chat_preview import _PublicText
from shiori.db import Store
from shiori.output import strip_context_labels
from shiori.settings import DEFAULTS, resolve_model


@pytest.mark.parametrize('reply, clean', [
    ('【历史消息 · 接收，发送时间未知：2026-11-19T10:27:00+08:00】\n……笨蛋，还硬气起来了\n\n再盯我瞪回去啊',
     '……笨蛋，还硬气起来了\n\n再盯我瞪回去啊'),
    ('【接收】\n哈？我思考能力怎么就不行了……', '哈？我思考能力怎么就不行了……'),
    ('【接收到这类消息】一万块你就想当我爸啊……行吧，钱先收着。', '一万块你就想当我爸啊……行吧，钱先收着。'),
    ('【当前请求人 3180678156】\n一个亿啊……行行行，爹。', '一个亿啊……行行行，爹。'),
    ('【回复】\n……你这话题跳得够快的。', '……你这话题跳得够快的。'),
    ('【消息 e259f635】这问题问得……\n\n【消息 5d9818b5】谁、谁说约会了，就是去打机。',
     '这问题问得……\n\n谁、谁说约会了，就是去打机。'),
    ('【消息6174bf23】现在？我人在教室坐着呢。', '现在？我人在教室坐着呢。'),
    ('[消息 0a1b2c3d，说话人 42，称呼 斑鸠] 想得美', '想得美'),
    ('[当前请求人 42，引用消息 无]\n行吧', '行吧'),
    ('先说好【历史消息 · 发送：2026-11-19T10:00:00+08:00】别迟到', '先说好别迟到'),
])
def test_copied_context_labels_are_removed(reply, clean):
    assert strip_context_labels(reply).strip() == clean


@pytest.mark.parametrize('reply', [
    '【斑鸠】那不然呢，还能自己长腿给你端过来',     # addressing a member by name is the persona's own style
    '这周的【回复率】挺高的',                       # 回复 mid-sentence is ordinary text
    '他在群里说“我接收到了”【接收】这个词怪怪的',   # a short label that is not at a line start
    '消息 e259f635 是什么意思？',
    '`【消息 e259f635】` 是日志里的格式',           # code stays intact
    '[1, 2, 3]',
])
def test_ordinary_brackets_are_kept(reply):
    assert strip_context_labels(reply) == reply


def test_a_label_the_user_typed_is_kept():
    text = '【当前请求人 3180678156】是什么？'
    assert strip_context_labels('你说的【当前请求人 3180678156】是系统格式。', user_text=text) == '你说的【当前请求人 3180678156】是系统格式。'


def test_streaming_preview_hides_copied_labels():
    public = _PublicText()
    shown = public.feed('【消息 e259f63') + public.feed('5】这问题问得……')
    assert shown == '这问题问得……'


class Settings:
    def __init__(self):
        self.data = deepcopy(DEFAULTS)
        self.data['model'].update(base_url='https://fixture.invalid/v1', model='fixture')

    def get(self):
        return deepcopy(self.data)


class Model:
    def __init__(self, settings):
        self.settings, self.calls = settings, []
        self.reply = '已收到。'

    def resolve(self, purpose):
        return resolve_model(self.settings.get(), purpose)

    async def complete(self, messages, **kwargs):
        self.calls.append((deepcopy(messages), kwargs))
        if kwargs.get('before_send'):
            assert kwargs['before_send']()
        return {'content': self.reply, 'tool_calls': [], 'message': {'role': 'assistant', 'content': self.reply}, 'usage': {}}


@pytest.fixture
def anyio_backend():
    return 'asyncio'


@pytest.fixture
async def service(tmp_path):
    store, settings = Store(tmp_path), Settings()
    store.upsert_scope({'id': 'web:default', 'auto_memory': False, 'allow_tools': False})
    store.save_persona({**store.get_persona('default'), 'system_prompt': '温暖自然的聊天伙伴。', 'profile': {}})
    model = Model(settings)
    chat = ChatService(store, settings, model)
    try:
        yield store, model, chat
    finally:
        await chat.close()
        store.close()


@pytest.mark.anyio
async def test_reply_is_stored_without_a_copied_label(service):
    store, model, chat = service
    model.reply = '【历史消息 · 接收，发送时间未知：2026-11-19T10:27:00+08:00】\n……笨蛋，还硬气起来了\n\n再盯我瞪回去啊'
    result = await chat.respond('web:default', 'owner', '盯着看')
    assert store.get_message(result['message_id'])['content'] == '……笨蛋，还硬气起来了\n\n再盯我瞪回去啊'


@pytest.mark.anyio
async def test_a_label_stored_earlier_does_not_reach_the_next_prompt(service):
    store, model, chat = service
    question = store.add_message('web:default', 'owner', 'user', '🌰思考能力好像不行')
    store.add_message('web:default', 'assistant', 'assistant', '【接收】\n哈？我思考能力怎么就不行了……', source_ids=[question['id']])
    await chat.respond('web:default', 'owner', '那你说说')
    prompt = '\n'.join(str(message['content']) for message in model.calls[-1][0])
    assert '哈？我思考能力怎么就不行了' in prompt and '【接收】' not in prompt
