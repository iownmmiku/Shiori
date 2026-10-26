"""Repeated mentions waiting behind a slow answer share the pending window."""
import asyncio

import pytest

from test_message_debounce import setup, anyio_backend, send, drain


@pytest.mark.anyio
async def test_multiple_mentions_waiting_for_active_reply_form_one_followup(setup):
    _, config, chat, flow, sent, _ = setup
    config.config['debounce'].update(seconds=.1, max_wait_seconds=2)
    chat.blocker = asyncio.Event()
    try:
        await send(flow, '第一个问题')
        await asyncio.wait_for(chat.entered.wait(), 1)
        await send(flow, '第二个问题')
        await asyncio.sleep(.14)  # Its timer expires while the first answer is busy.
        await send(flow, '再补充一点')
        await asyncio.sleep(.14)
        await send(flow, '最后补充')
        last = asyncio.get_running_loop().time()
        chat.blocker.set()
        await asyncio.sleep(.05)
        assert len(chat.calls) == 1  # Still wait for the latest message's quiet period.
        await drain(flow)
        assert [call[2] for call in chat.calls] == ['第一个问题', '第二个问题\n再补充一点\n最后补充']
        assert chat.calls[1][-1] - last >= .09
        assert len(chat.calls[1][4]['source_ids']) == 3
        assert len(sent) == 2
        assert not flow._debounce_batches and not flow._debounce_futures
    finally:
        chat.blocker.set()
        await flow.close()


@pytest.mark.anyio
@pytest.mark.parametrize('model_failure', [False, True])
async def test_real_chat_and_onebot_combine_repeated_mentions_before_planning(tmp_path, model_failure):
    from copy import deepcopy
    import json
    from types import SimpleNamespace
    from mikubot.chat import ChatService
    from mikubot.db import Store
    from mikubot.onebot import OneBotBridge
    from mikubot.provider import ProviderTimeoutError
    from mikubot.settings import DEFAULTS

    config = deepcopy(DEFAULTS)
    config['onebot'].update(enabled=True, chat_policy='blacklist')
    config['model'].update(base_url='https://fixture.invalid/v1', model='fixture')
    config['debounce'].update(enabled=True, seconds=.1, max_wait_seconds=1)
    config['stickers']['enabled'] = False
    config['output'].update(mode='model', interval_ms=0)
    calls, sent = [], []

    class Model:
        async def complete(self, messages, **kwargs):
            assert kwargs['before_send']()
            purpose = kwargs.get('purpose', 'chat')
            calls.append((purpose, messages))
            if purpose == 'planner':
                payload = json.loads(messages[-1]['content'])
                assert payload['current_message_text'] == '第一个问题\n再补充一点\n最后补充'
                return {'content': json.dumps({'action': 'reply', 'target_message_id': payload['current_message_id'],
                    'target_user_id': payload['current_user_id'], 'addressed_to_bot': True,
                    'intent': '一起回答这三条消息', 'reason': '同一成员的连续请求', 'needed_info': []})}
            if model_failure:
                raise ProviderTimeoutError('fixture response timeout')
            return {'content': '已经把三条补充一起收到了。\n\n现在统一回答这个问题。',
                    'message': {'role': 'assistant', 'content': '已经把三条补充一起收到了。\n\n现在统一回答这个问题。'},
                    'tool_calls': [], 'usage': {}}

    store = Store(tmp_path)
    store.upsert_scope({'id': 'qq:group:202', 'auto_memory': False, 'plan_enabled': True,
                        'merge_window_ms': 0, 'cooldown_seconds': 0})
    chat = ChatService(store, SimpleNamespace(get=lambda: deepcopy(config)), Model())
    bridge = OneBotBridge(store, chat.settings, chat)

    async def deliver(scope, text, **kwargs):
        sent.append(text)
        return True
    bridge.send = deliver
    try:
        for index, text in enumerate(['第一个问题', '再补充一点', '最后补充']):
            await bridge.handle_event({'post_type': 'message', 'message_type': 'group', 'self_id': 999,
                'group_id': 202, 'user_id': 303, 'message_id': index + 1,
                'message': [{'type': 'at', 'data': {'qq': '999'}}, {'type': 'text', 'data': {'text': text}}]}, wait=False)
            await asyncio.sleep(.035)
        await drain(bridge._flow)
        assert [purpose for purpose, _ in calls] == ['planner', 'chat']
        replies = [item for item in store.list_messages('qq:group:202') if item['role'] == 'assistant']
        assert len(replies) == 1
        if model_failure:
            assert len(sent) == 1 and '超时' in sent[0]
        else:
            assert len(sent) == 2  # One answer can have two output paragraphs.
            assert len(replies[0]['metadata']['source_ids']) == 3
        events = [item for item in store.list_events() if item['kind'] == 'qq_debounce']
        assert len(events) == 1 and events[0]['details']['input_count'] == 3
    finally:
        await bridge.stop()
        await chat.close()
        store.close()


@pytest.mark.anyio
async def test_waiting_followup_still_honors_stop_and_member_isolation(setup):
    _, _, chat, flow, sent, _ = setup
    chat.blocker = asyncio.Event()
    try:
        await send(flow, '原始问题', user='303')
        await asyncio.wait_for(chat.entered.wait(), 1)
        await send(flow, '不要漏掉我的补充', user='303')
        await asyncio.sleep(.14)
        await send(flow, '别人自己的问题', user='404')
        await send(flow, '停止回复', user='303')
        chat.blocker.set()
        await drain(flow)
        assert all(call[2] != '不要漏掉我的补充' for call in chat.calls)
        assert [text for _, text, _ in sent] == ['别人自己的问题']
        assert not flow._debounce_batches and not flow._debounce_futures
    finally:
        chat.blocker.set()
        await flow.close()
