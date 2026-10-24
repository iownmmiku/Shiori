"""Trailing-edge ingress buffering; all transports/models are local fixtures."""
import asyncio
import json
import time
from copy import deepcopy

import pytest

from mikubot.db import Store
from mikubot.profiles import resolve_scope, validate_profiles
from mikubot.settings import DEFAULTS, Settings, resolved_debounce
from mikubot.social import ConversationFlow, SocialEngine


@pytest.fixture
def anyio_backend():
    return 'asyncio'


class Config:
    def __init__(self):
        self.config = deepcopy(DEFAULTS)
        self.config['onebot'].update(enabled=True, chat_policy='blacklist')
        self.config['qq_official'].update(enabled=True, chat_policy='blacklist')
        self.config['debounce'].update(enabled=True, seconds=.1, max_wait_seconds=1)
        self.config['model'].update(base_url='https://fixture.invalid/v1', model='fixture', vision=True)
        self.config['output'].update(enabled=False)
        self.config['planner'].update(group_batch_ms=10000, min_interval_ms=0)

    def get(self):
        return deepcopy(self.config)


class Planner:
    def __init__(self):
        self.calls = []

    async def complete(self, messages, **kwargs):
        payload = json.loads(messages[-1]['content'])
        self.calls.append(payload)
        return {'content': json.dumps({'action': 'reply', 'target_message_id': payload['current_message_id'],
            'target_user_id': payload['current_user_id'], 'addressed_to_bot': True,
            'intent': '回应合并消息', 'reason': '测试合并后只判断一次', 'needed_info': []})}


class Chat:
    def __init__(self, store, settings):
        self.store, self.settings = store, settings
        self.provider = Planner()
        self.social = SocialEngine(store, settings, self.provider)
        self.calls = []
        self.entered = asyncio.Event()
        self.blocker = None

    async def observe(self, scope_id, user_id, text, images=None, metadata=None):
        message = self.store.add_message(scope_id, str(user_id), 'user', text, **dict(metadata or {}, images=images or []))
        self.social.observe(scope_id, message)
        return message

    async def respond(self, scope_id, user_id, text, images=None, metadata=None):
        self.calls.append((scope_id, user_id, text, images, metadata, time.monotonic()))
        self.entered.set()
        if self.blocker and len(self.calls) == 1:
            await self.blocker.wait()
        reply = text
        record = self.store.add_message(scope_id, 'assistant', 'assistant', reply, source_ids=metadata['source_ids'])
        return {'reply': reply, 'message_id': record['id']}


@pytest.fixture
def setup(tmp_path):
    store, config = Store(tmp_path), Config()
    chat = Chat(store, config)
    store.upsert_scope({'id': 'qq:group:202', 'plan_enabled': False, 'cooldown_seconds': 0,
                        'merge_window_ms': 2000, 'auto_memory': False})
    sent, permission = [], {'allowed': True}

    async def emit(scope_id, text, metadata):
        sent.append((scope_id, text, metadata))
        return True

    flow = ConversationFlow(store, config, chat, emit, lambda *_: permission['allowed'])
    yield store, config, chat, flow, sent, permission
    store.close()


async def send(flow, text, *, user='303', scope='qq:group:202', direct=True, images=None, wait=False):
    return await flow.handle(scope, user, text, images or [], {'at_bot': direct, 'trusted_identity': {'account_id': user}},
                             direct=direct, wait=wait)


async def drain(flow):
    while flow._jobs:
        await asyncio.wait_for(asyncio.gather(*list(flow._jobs)), 4)


@pytest.mark.anyio
@pytest.mark.parametrize('scope', ['qq:group:202', 'qq:private:303', 'qqofficial:group:202', 'qqofficial:private:303'])
async def test_latest_message_resets_window_and_original_sources_remain_separate(setup, scope):
    store, config, chat, flow, sent, _ = setup
    if scope.startswith('qqofficial:'):
        config.config['onebot']['adapter'] = 'official'
    store.upsert_scope({'id': scope, 'plan_enabled': False, 'merge_window_ms': 2000})
    try:
        await send(flow, '第一句话。', scope=scope, images=['data:image/png;base64,first'])
        await asyncio.sleep(.065)
        last = time.monotonic()
        await send(flow, '第二句话。', scope=scope, images=['data:image/png;base64,second'])
        await asyncio.sleep(.06)
        assert not chat.calls
        await drain(flow)
        assert len(chat.calls) == 1 and len(sent) == 1
        call = chat.calls[0]
        assert call[2] == '第一句话。\n第二句话。'
        assert call[-1] - last >= .09
        assert call[3] == ['data:image/png;base64,first', 'data:image/png;base64,second']
        assert len(call[4]['source_ids']) == 2
        raw = [m for m in store.list_messages(scope) if m['role'] == 'user']
        assert [m['content'] for m in raw] == ['第一句话。', '第二句话。']
        assert call[4]['source_ids'] == [m['id'] for m in raw]
    finally:
        await flow.close()


@pytest.mark.anyio
async def test_planner_runs_once_on_combined_text_without_second_batch_delay(setup):
    store, _, chat, flow, _, _ = setup
    store.upsert_scope({'id': 'qq:group:202', 'plan_enabled': True})
    started = time.monotonic()
    try:
        await send(flow, '今天终于把事情完成了', direct=False)
        await send(flow, '大家想不想一起庆祝', direct=False)
        await drain(flow)
        assert len(chat.provider.calls) == 1 and len(chat.calls) == 1
        assert chat.provider.calls[0]['current_message_text'] == '今天终于把事情完成了\n大家想不想一起庆祝'
        assert time.monotonic() - started < 1
    finally:
        await flow.close()


@pytest.mark.anyio
async def test_group_members_never_merge_authority_or_text(setup):
    _, _, chat, flow, _, _ = setup
    try:
        await send(flow, '管理员自己的请求', user='101')
        await send(flow, '普通成员的请求', user='404')
        await send(flow, '管理员的补充', user='101')
        await drain(flow)
        calls = {call[1]: call for call in chat.calls}
        assert calls['101'][2] == '管理员自己的请求\n管理员的补充'
        assert calls['404'][2] == '普通成员的请求'
        assert calls['101'][4]['trusted_identity']['account_id'] == '101'
        assert len(calls['101'][4]['source_ids']) == 2 and len(calls['404'][4]['source_ids']) == 1
    finally:
        await flow.close()


@pytest.mark.anyio
async def test_maximum_wait_prevents_a_continuous_sender_from_starving(setup):
    _, config, chat, flow, _, _ = setup
    config.config['debounce'].update(seconds=.3, max_wait_seconds=1)
    started = time.monotonic()
    try:
        await send(flow, '开始')
        for index in range(5):
            await asyncio.sleep(.18)
            await send(flow, f'补充{index}')
        await asyncio.wait_for(chat.entered.wait(), .3)
        assert .9 <= chat.calls[0][-1] - started < 1.17
        assert len(chat.calls[0][4]['source_ids']) == 6
        await drain(flow)
    finally:
        await flow.close()


@pytest.mark.anyio
async def test_followup_during_generation_queues_without_invalidating_current_answer(setup):
    _, _, chat, flow, sent, _ = setup
    chat.blocker = asyncio.Event()
    try:
        await send(flow, '原问题')
        await asyncio.wait_for(chat.entered.wait(), 1)
        generation = chat.calls[0][4]['request_generation']
        await send(flow, '追加问题')
        await asyncio.sleep(.14)
        assert chat.social.request_current('qq:group:202', '303', generation)
        assert len(chat.calls) == 1
        chat.blocker.set()
        await drain(flow)
        assert [text for _, text, _ in sent] == ['原问题', '追加问题']
    finally:
        chat.blocker.set()
        await flow.close()


@pytest.mark.anyio
async def test_new_input_during_first_part_ack_does_not_cut_off_remaining_parts(setup):
    _, config, chat, flow, sent, _ = setup
    config.config['output'].update(enabled=True, mode='model', interval_ms=10)
    dispatched, acknowledge = asyncio.Event(), asyncio.Event()

    async def emit(scope_id, text, metadata):
        sent.append((scope_id, text, metadata))
        if len(sent) == 1:
            dispatched.set()
            await acknowledge.wait()
        return True

    flow.emit = emit
    try:
        await send(flow, '第一段。\n\n第二段。\n\n第三段。')
        await asyncio.wait_for(dispatched.wait(), 1)
        await send(flow, '新的问题')
        await asyncio.sleep(.14)
        acknowledge.set()
        await drain(flow)
        assert [text for _, text, _ in sent] == ['第一段。', '第二段。', '第三段。', '新的问题']
    finally:
        acknowledge.set()
        await flow.close()


@pytest.mark.anyio
@pytest.mark.parametrize('action', ['stop', 'revoke', 'delete', 'close'])
async def test_pending_collection_never_calls_model_after_cancel_or_source_loss(setup, action):
    store, _, chat, flow, sent, permission = setup
    try:
        await send(flow, '等待期间的消息')
        if action == 'stop':
            result = await send(flow, '停止回复')
            assert result['code'] == 'interrupted'
        elif action == 'revoke':
            permission['allowed'] = False
        elif action == 'delete':
            store.clear_messages('qq:group:202')
        else:
            await flow.close()
        await drain(flow)
        assert not chat.calls and not sent
    finally:
        await flow.close()


@pytest.mark.anyio
async def test_stop_invalidates_active_turn_and_sealed_followup(setup):
    _, _, chat, flow, sent, _ = setup
    chat.blocker = asyncio.Event()
    try:
        await send(flow, '原问题')
        await asyncio.wait_for(chat.entered.wait(), 1)
        await send(flow, '第二轮问题')
        await asyncio.sleep(.13)
        assert (await send(flow, '停止回复'))['code'] == 'interrupted'
        chat.blocker.set()
        await drain(flow)
        assert len(chat.calls) == 1 and not sent
    finally:
        chat.blocker.set()
        await flow.close()


@pytest.mark.anyio
async def test_disabled_debounce_preserves_immediate_private_reply(setup):
    _, config, chat, flow, _, _ = setup
    config.config['debounce']['enabled'] = False
    try:
        await send(flow, '直接回复', scope='qq:private:303', wait=True)
        assert len(chat.calls) == 1 and not flow._debounce_batches
    finally:
        await flow.close()


@pytest.mark.anyio
async def test_close_resolves_waiters_even_when_timer_task_has_not_started(setup):
    _, _, _, flow, _, _ = setup
    pending = asyncio.create_task(send(flow, '等待结果', wait=True))
    await asyncio.sleep(0)
    await flow.close()
    assert (await asyncio.wait_for(pending, .2))['code'] == 'interrupted'


@pytest.mark.anyio
async def test_explicit_correction_discards_buffered_earlier_text(setup):
    _, _, chat, flow, sent, _ = setup
    try:
        await send(flow, '原本想问的内容')
        await send(flow, '换个问题，我想问新的问题')
        await drain(flow)
        assert len(chat.calls) == 1
        assert chat.calls[0][2] == '换个问题，我想问新的问题'
        assert [text for _, text, _ in sent] == ['换个问题，我想问新的问题']
    finally:
        await flow.close()


@pytest.mark.anyio
async def test_full_window_seals_safely_and_keeps_later_messages_in_next_turn(setup):
    _, _, chat, flow, _, _ = setup
    try:
        for index in range(10):
            await send(flow, f'片段{index}')
        await drain(flow)
        assert len(chat.calls) == 2
        assert [len(call[4]['source_ids']) for call in chat.calls] == [8, 2]
        assert chat.calls[0][2].splitlines() == [f'片段{i}' for i in range(8)]
        assert chat.calls[1][2] == '片段8\n片段9'
    finally:
        await flow.close()


@pytest.mark.anyio
@pytest.mark.parametrize('max_images, counts', [(3, [3, 1]), (2, [2, 2]), (1, [1, 1, 1, 1])])
async def test_image_limit_seals_window_without_dropping_fourth_image_or_originals(setup, max_images, counts):
    store, config, chat, flow, _, _ = setup
    config.config['context']['max_images'] = max_images
    originals = [f'data:image/png;base64,image{index}' for index in range(4)]
    try:
        for index, image in enumerate(originals):
            await send(flow, f'看看图片{index}', images=[image])
        await drain(flow)
        assert [len(call[3]) for call in chat.calls] == counts
        assert [image for call in chat.calls for image in call[3]] == originals
        raw = [record for record in store.list_messages('qq:group:202') if record['role'] == 'user']
        assert [record['metadata']['images'][0] for record in raw] == originals
        assert [ident for call in chat.calls for ident in call[4]['source_ids']] == [record['id'] for record in raw]
    finally:
        await flow.close()


@pytest.mark.anyio
async def test_a_multi_image_original_is_kept_whole_when_it_exceeds_remaining_capacity(setup):
    _, _, chat, flow, _, _ = setup
    originals = [f'data:image/png;base64,image{index}' for index in range(4)]
    try:
        await send(flow, '前两张', images=originals[:2])
        await send(flow, '后两张', images=originals[2:])
        await drain(flow)
        assert [call[3] for call in chat.calls] == [originals[:2], originals[2:]]
        assert [len(call[4]['source_ids']) for call in chat.calls] == [1, 1]
    finally:
        await flow.close()


@pytest.mark.anyio
async def test_duplicate_image_references_do_not_consume_extra_window_capacity(setup):
    _, config, chat, flow, _, _ = setup
    config.config['context']['max_images'] = 1
    image = 'data:image/png;base64,same'
    try:
        for index in range(4):
            await send(flow, f'同一张图的补充{index}', images=[image])
        await drain(flow)
        assert len(chat.calls) == 1 and chat.calls[0][3] == [image]
        assert len(chat.calls[0][4]['source_ids']) == 4
    finally:
        await flow.close()


def test_global_defaults_roundtrip_and_optional_profile_scope_inheritance(tmp_path):
    settings = Settings(tmp_path)
    assert settings.get()['debounce'] == {'enabled': False, 'seconds': 3, 'max_wait_seconds': 30}
    settings.update({'debounce': {'enabled': True, 'seconds': 4}})
    profile = deepcopy(settings.get()['bot_profiles'])
    profile['items'][0]['values'].update(debounce_seconds=2, debounce_enabled=None)
    settings.update({'bot_profiles': profile})
    restored = Settings(tmp_path)
    effective = resolve_scope({'id': 'qq:private:303', 'profile_id': '', 'profile_overrides': []}, restored)
    assert resolved_debounce(restored.get(), effective) == {'enabled': True, 'seconds': 2, 'max_wait_seconds': 30}
    effective = resolve_scope(dict(effective, debounce_enabled=False, profile_overrides=['debounce_enabled']), restored)
    assert resolved_debounce(restored.get(), effective)['enabled'] is False


@pytest.mark.parametrize('patch', [
    {'enabled': 'true'}, {'seconds': 0}, {'seconds': float('nan')}, {'seconds': True},
    {'max_wait_seconds': 301}, {'seconds': 10, 'max_wait_seconds': 5},
])
def test_invalid_global_debounce_is_rejected_without_mutation(tmp_path, patch):
    settings = Settings(tmp_path)
    before = settings.get()
    with pytest.raises(ValueError):
        settings.update({'debounce': patch})
    assert settings.get() == before


@pytest.mark.parametrize('patch', [
    {'debounce_enabled': 1}, {'debounce_seconds': '3'}, {'debounce_seconds': False},
    {'debounce_max_wait_seconds': float('inf')}, {'debounce_seconds': 5, 'debounce_max_wait_seconds': 2},
])
def test_invalid_profile_and_direct_store_values_are_rejected(tmp_path, patch):
    profile = deepcopy(DEFAULTS['bot_profiles'])
    profile['items'][0]['values'].update(patch)
    with pytest.raises(ValueError):
        validate_profiles(profile)
    store = Store(tmp_path)
    try:
        with pytest.raises(ValueError):
            store.upsert_scope({'id': 'qq:private:303', **patch})
    finally:
        store.close()
