"""Real chat records and request planning; only the model/transport are fixtures."""
import asyncio
from copy import deepcopy

import pytest

from shiori.chat import ChatService
from shiori.db import Store
from shiori.onebot import OneBotBridge
from shiori.settings import DEFAULTS


@pytest.fixture
def anyio_backend():
    return 'asyncio'


class Settings:
    def __init__(self):
        self.config = deepcopy(DEFAULTS)
        self.config['onebot'].update(enabled=True, chat_policy='blacklist')
        self.config['model'].update(base_url='https://fixture.invalid/v1', model='fixture')
        self.config['output'].update(mode='model', interval_ms=10)
        self.config['stickers']['enabled'] = False

    def get(self):
        return deepcopy(self.config)


class Model:
    async def complete(self, messages, **kwargs):
        text = messages[-1]['content']
        reply = '旧第一句。\n\n旧第二句。\n\n旧第三句。' if '原问题' in text else '新第一句。\n\n新第二句。'
        return {'content': reply, 'message': {'role': 'assistant', 'content': reply}, 'tool_calls': [], 'usage': {}}


@pytest.mark.anyio
@pytest.mark.parametrize('kind', ['private', 'group'])
@pytest.mark.parametrize('followup,expected_old', [
    ('初音未来，另外一个新问题，请再说明一下', 3),
    ('停止回复', 1),
    ('换个问题，我想问新的问题', 1),
])
async def test_new_ordinary_request_does_not_cut_off_reply_already_being_sent(tmp_path, kind, followup, expected_old):
    store, settings = Store(tmp_path), Settings()
    scope = 'qq:' + kind + (':303' if kind == 'private' else ':202')
    store.upsert_scope({'id': scope, 'plan_enabled': False, 'auto_memory': False, 'merge_window_ms': 0})
    chat = ChatService(store, settings, Model())
    bridge = OneBotBridge(store, settings, chat)
    sent, first_dispatched, acknowledge = [], asyncio.Event(), asyncio.Event()

    async def send(scope_id, text, **kwargs):
        sent.append(text)
        if len(sent) == 1:
            first_dispatched.set()
            await acknowledge.wait()
        return True

    bridge.send = send
    task = asyncio.create_task(bridge._flow.handle(scope, '303', '初音未来，原问题请详细回答', [], {'at_bot': True}, direct=True))
    try:
        await asyncio.wait_for(first_dispatched.wait(), 5)
        await bridge._flow.handle(scope, '303', followup, [], {'at_bot': followup != '停止回复'}, direct=True, wait=False)
        acknowledge.set()
        await asyncio.wait_for(task, 5)
        await asyncio.wait_for(asyncio.gather(*list(bridge._flow._jobs)), 5)
        old = [part for part in sent if part.startswith('旧')]
        assert old == ['旧第一句。', '旧第二句。', '旧第三句。'][:expected_old]
        if followup != '停止回复':
            assert sent[expected_old:] == ['新第一句。', '新第二句。']
        assert not chat.social._deliveries
    finally:
        acknowledge.set()
        await bridge.stop()
        await chat.close()
        store.close()


@pytest.mark.anyio
async def test_busy_followup_does_not_cancel_an_answer_already_on_the_wire(tmp_path):
    store, settings = Store(tmp_path), Settings()
    settings.config['scheduler']['scope_queue_limit'] = 1
    scope = 'qq:group:202'
    store.upsert_scope({'id': scope, 'plan_enabled': False, 'auto_memory': False, 'merge_window_ms': 0})
    chat = ChatService(store, settings, Model())
    bridge = OneBotBridge(store, settings, chat)
    sent, dispatched, acknowledge = [], asyncio.Event(), asyncio.Event()

    async def send(scope_id, text, **kwargs):
        sent.append(text)
        if len(sent) == 1:
            dispatched.set()
            await acknowledge.wait()
        return True

    bridge.send = send
    first = asyncio.create_task(bridge._flow.handle(scope, '303', '初音未来，原问题请回答', [], {'at_bot': True}, direct=True))
    try:
        await asyncio.wait_for(dispatched.wait(), 5)
        second = asyncio.create_task(bridge._flow.handle(scope, '303', '初音未来，另一个问题', [], {'at_bot': True}, direct=True, wait=False))
        for _ in range(100):
            if chat.social._generations[(scope, '303')] == 2:
                break
            await asyncio.sleep(.001)
        assert chat.social._generations[(scope, '303')] == 2
        acknowledge.set()
        _, result = await asyncio.wait_for(asyncio.gather(first, second), 5)
        assert result['code'] == 'ingress_busy'
        assert sent[:3] == ['旧第一句。', '旧第二句。', '旧第三句。']
        assert '消息较多' in sent[3]
        assert not chat.social._deliveries and (scope, '303') not in chat.social._open_requests
    finally:
        acknowledge.set()
        await bridge.stop()
        await chat.close()
        store.close()
