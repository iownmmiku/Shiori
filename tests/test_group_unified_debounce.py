"""Shared group collection, with one decision and one logical reply per window."""
import asyncio

import pytest

from test_message_debounce import setup, send, drain


@pytest.fixture
def anyio_backend():
    return 'asyncio'


@pytest.mark.anyio
async def test_mixed_mentions_have_one_planner_one_generation_and_all_sources(setup):
    store, _, chat, flow, _, _ = setup
    store.upsert_scope({'id': 'qq:group:202', 'plan_enabled': True, 'trigger_at': False, 'trigger_name': False})
    try:
        await send(flow, '第一位的问题', user='101')
        await send(flow, '第二位的问题', user='404')
        await send(flow, '第一位的补充', user='101')
        await drain(flow)
        assert len(chat.provider.calls) == len(chat.calls) == 1
        assert '[说话人 101]' in chat.provider.calls[0]['current_message_text']
        assert '[说话人 404]' in chat.provider.calls[0]['current_message_text']
        assert len(chat.calls[0][4]['source_ids']) == 3
        assistant = [item for item in store.list_messages('qq:group:202') if item['role'] == 'assistant']
        assert len(assistant) == 1
        events = [item for item in store.list_events(limit=50) if item['kind'] == 'qq_debounce']
        assert events[-1]['details']['group_unified'] is True
        assert events[-1]['details']['participant_count'] == 2
    finally:
        await flow.close()


@pytest.mark.anyio
async def test_mixed_followups_during_generation_form_one_next_turn(setup):
    _, _, chat, flow, sent, _ = setup
    chat.blocker = asyncio.Event()
    try:
        await send(flow, '开始的一轮', user='101')
        await asyncio.wait_for(chat.entered.wait(), 1)
        await send(flow, '后续成员甲', user='404')
        await asyncio.sleep(.12)
        await send(flow, '后续成员乙', user='505')
        await asyncio.sleep(.05)
        assert len(chat.calls) == 1
        chat.blocker.set()
        await drain(flow)
        assert len(chat.calls) == 2
        assert chat.calls[1][2] == '后续成员甲\n后续成员乙'
        assert sent[0][1] == '开始的一轮'
    finally:
        chat.blocker.set()
        await flow.close()


@pytest.mark.anyio
async def test_member_stop_cannot_cancel_an_active_mixed_turn(setup):
    _, _, chat, flow, sent, _ = setup
    chat.blocker = asyncio.Event()
    try:
        await send(flow, '成员甲当前问题', user='101')
        await send(flow, '成员乙当前问题', user='404')
        await asyncio.wait_for(chat.entered.wait(), 1)
        await send(flow, '停止回复', user='404')
        chat.blocker.set()
        await drain(flow)
        assert len(chat.calls) == 1
        assert len(sent) == 1
        assert '成员甲当前问题' in sent[0][1]
    finally:
        chat.blocker.set()
        await flow.close()


@pytest.mark.anyio
async def test_private_conversations_keep_separate_windows(setup):
    store, _, chat, flow, _, _ = setup
    for user in ('101', '404'):
        store.upsert_scope({'id': 'qq:private:' + user, 'plan_enabled': False})
    try:
        await send(flow, '甲私聊', user='101', scope='qq:private:101')
        await send(flow, '乙私聊', user='404', scope='qq:private:404')
        await drain(flow)
        assert len(chat.calls) == 2
        assert {call[2] for call in chat.calls} == {'甲私聊', '乙私聊'}
    finally:
        await flow.close()
