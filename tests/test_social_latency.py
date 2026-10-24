"""Bounded optional QQ work and Planner deadlines; no network or credentials."""
import asyncio
import json
import time
from copy import deepcopy

import pytest

from mikubot.db import Store
from mikubot.settings import DEFAULTS
from mikubot.social import ConversationFlow, SocialEngine


@pytest.fixture
def anyio_backend():
    return 'asyncio'


class Config:
    def __init__(self):
        self.value = deepcopy(DEFAULTS)
        self.value['onebot'].update(enabled=True, chat_policy='blacklist')
        self.value['model'].update(base_url='https://fixture.invalid/v1', model='fixture')
        self.value['planner'].update(min_interval_ms=0, group_batch_ms=0, max_age_ms=60)
        self.value['output'].update(enabled=False)

    def get(self):
        return deepcopy(self.value)


class Chat:
    def __init__(self, store, settings):
        self.store = store
        self.social = SocialEngine(store, settings, None)
        self.calls = []

    async def observe(self, scope, user, text, images, metadata):
        return self.store.add_message(scope, user, 'user', text, **metadata)

    async def respond(self, scope, user, text, images, metadata):
        self.calls.append(text)
        message = self.store.add_message(scope, 'assistant', 'assistant', '回答：' + text,
                                         source_ids=metadata['source_ids'])
        return {'reply': '回答：' + text, 'message_id': message['id'],
                'timings': {'memory_recall_ms': 2, 'reply_model_ms': 4, 'total_ms': 6}}


class Stickers:
    def __init__(self):
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.cancelled = asyncio.Event()
        self.guard = None
        self.sent = []

    async def choose(self, scope, user, text, reply, current):
        self.guard = current
        self.started.set()
        try:
            await self.release.wait()
        except asyncio.CancelledError:
            self.cancelled.set()
            raise
        return {'id': 'fixture', 'version': 1}

    def note_sent(self, *args):
        self.sent.append(args)


@pytest.fixture
def setup(tmp_path):
    store, config = Store(tmp_path), Config()
    scope = 'qq:group:202'
    store.upsert_scope({'id': scope, 'plan_enabled': False, 'merge_window_ms': 0, 'cooldown_seconds': 0})
    chat = Chat(store, config)
    chat.stickers = Stickers()
    output, permission = [], {'allowed': True}

    async def emit(scope, text, metadata):
        output.append((text, metadata))
        return True

    flow = ConversationFlow(store, config, chat, emit, lambda *_: permission['allowed'])
    yield store, config, chat, flow, output, permission
    store.close()


async def send(flow, text='你好', *, scope='qq:group:202', user='303', direct=True):
    return await flow.handle(scope, user, text, [], {'at_bot': direct}, direct=direct)


async def drain_stickers(flow):
    if flow._sticker_jobs:
        await asyncio.wait_for(asyncio.gather(*list(flow._sticker_jobs), return_exceptions=True), .5)
    await asyncio.sleep(0)


@pytest.mark.anyio
async def test_optional_selection_does_not_hold_text_future_or_next_turn(setup):
    store, _, chat, flow, sent, _ = setup
    try:
        await asyncio.wait_for(send(flow, '第一轮'), .5)
        await asyncio.wait_for(chat.stickers.started.wait(), .5)
        assert flow._scope_jobs['qq:group:202'] == 0
        assert not flow._locks[('qq:group:202', '303')].locked()
        assert not flow._output_locks['qq:group:202'].locked()
        old_guard = chat.stickers.guard
        await asyncio.wait_for(send(flow, '第二轮'), .5)
        assert chat.stickers.cancelled.is_set()
        assert not old_guard()
        assert chat.calls == ['第一轮', '第二轮']
        assert [text for text, _ in sent] == ['回答：第一轮', '回答：第二轮']
        assert len(flow._sticker_tasks) <= 1
        events = [x for x in store.list_events() if x['kind'] == 'qq_reply_timing']
        assert len(events) == 2
        assert events[0]['details']['chat_timings']['reply_model_ms'] == 4
        assert events[0]['details']['ingress_to_complete_ms'] >= 0
        assert '第一轮' not in json.dumps(events, ensure_ascii=False)
    finally:
        await flow.close()


@pytest.mark.anyio
async def test_passive_message_from_another_member_invalidates_pending_sticker(setup):
    _, _, chat, flow, sent, _ = setup
    try:
        await send(flow)
        await chat.stickers.started.wait()
        guard = chat.stickers.guard
        await send(flow, '普通旁聊', user='404', direct=False)
        chat.stickers.release.set()
        await drain_stickers(flow)
        assert not guard()
        assert len(sent) == 1 and not chat.stickers.sent
    finally:
        await flow.close()


@pytest.mark.anyio
@pytest.mark.parametrize('change', ['permission', 'source', 'identity', 'scope', 'paused', 'persona'])
async def test_optional_result_dropped_if_sources_or_authority_changed(setup, change):
    store, config, chat, flow, sent, permission = setup
    try:
        await send(flow)
        await chat.stickers.started.wait()
        if change == 'permission':
            permission['allowed'] = False
        elif change == 'source':
            store.clear_messages('qq:group:202')
        elif change == 'identity':
            config.value['identity']['name'] = '另一个机器人'
        elif change == 'scope':
            store.upsert_scope({'id': 'qq:group:202', 'enabled': False})
        elif change == 'persona':
            persona = store.get_persona(store.get_scope('qq:group:202')['persona_id'])
            store.save_persona(dict(persona, description='已更换的人格描述'))
        else:
            config.value['paused'] = True
        chat.stickers.release.set()
        await drain_stickers(flow)
        assert len(sent) == 1 and not chat.stickers.sent
    finally:
        await flow.close()


@pytest.mark.anyio
async def test_selection_timeout_cancels_provider_and_releases_task_slot(setup):
    store, _, chat, flow, sent, _ = setup
    flow._sticker_timeout = .025
    try:
        await send(flow)
        await drain_stickers(flow)
        assert chat.stickers.cancelled.is_set()
        assert not flow._sticker_tasks and not flow._sticker_jobs
        assert len(sent) == 1
        assert len([x for x in store.list_events() if x['kind'] == 'sticker_timeout']) == 1
    finally:
        await flow.close()


@pytest.mark.anyio
async def test_close_cancels_optional_work_without_leaking_or_replaying(setup):
    _, _, chat, flow, sent, _ = setup
    await send(flow)
    await chat.stickers.started.wait()
    await asyncio.wait_for(flow.close(), .5)
    assert chat.stickers.cancelled.is_set()
    assert not flow._jobs and not flow._sticker_jobs and not flow._sticker_tasks
    chat.stickers.release.set()
    await asyncio.sleep(0)
    assert len(sent) == 1 and not chat.stickers.sent


@pytest.mark.anyio
async def test_sticker_drops_instead_of_waiting_for_busy_output_lock(setup):
    _, _, chat, flow, sent, _ = setup
    try:
        await send(flow)
        await chat.stickers.started.wait()
        async with flow._output_locks['qq:group:202']:
            chat.stickers.release.set()
            await drain_stickers(flow)
        assert len(sent) == 1 and not chat.stickers.sent
    finally:
        await flow.close()


@pytest.mark.anyio
async def test_selection_deadline_does_not_shorten_image_ack_wait(setup):
    _, _, chat, flow, sent, _ = setup
    flow._sticker_timeout = .01
    acknowledged = asyncio.Event()
    async def emit(scope, text, metadata):
        if metadata.get('sticker'):
            await asyncio.sleep(.04)
            acknowledged.set()
        sent.append((text, metadata))
        return True
    flow.emit = emit
    chat.stickers.release.set()
    try:
        await send(flow)
        await drain_stickers(flow)
        assert acknowledged.is_set() and len(sent) == 2
        assert len(chat.stickers.sent) == 1
    finally:
        await flow.close()


@pytest.mark.anyio
async def test_cancelled_optional_tasks_remain_under_global_cap_until_drained(setup):
    _, _, _, flow, _, _ = setup
    try:
        # Rapid superseding turns can cancel tasks before their first tick. Such
        # not-yet-drained cancellations still count against the population cap.
        for _ in range(140):
            flow._schedule_sticker('qq:group:202', '303', 'input', 'reply', {}, {})
        assert len(flow._sticker_jobs) == 128
        assert len(flow._sticker_tasks) <= 1
    finally:
        await flow.close()
    assert not flow._sticker_jobs and not flow._sticker_tasks


@pytest.mark.anyio
async def test_image_ack_timeout_is_not_misreported_as_selection_timeout(setup):
    store, _, chat, flow, sent, _ = setup
    async def emit(scope, text, metadata):
        if metadata.get('sticker'):
            raise TimeoutError('mock transport ack deadline')
        sent.append((text, metadata))
        return True
    flow.emit = emit
    chat.stickers.release.set()
    try:
        await send(flow)
        await drain_stickers(flow)
        assert len(sent) == 1 and not chat.stickers.sent
        assert not any(x['kind'] == 'sticker_timeout' for x in store.list_events())
        assert any(x['kind'] == 'sticker_send_error' for x in store.list_events())
    finally:
        await flow.close()


@pytest.mark.anyio
async def test_timing_write_failure_cannot_leave_turn_future_or_locks_hanging(setup, monkeypatch):
    store, _, chat, flow, sent, _ = setup
    chat.stickers = None
    original = store.add_event
    def failing_telemetry(kind, *args, **kwargs):
        if kind == 'qq_reply_timing':
            raise OSError('test telemetry write failure')
        return original(kind, *args, **kwargs)
    monkeypatch.setattr(store, 'add_event', failing_telemetry)
    try:
        result = await asyncio.wait_for(send(flow), .5)
        await asyncio.sleep(0)
        assert result['accepted'] and len(sent) == 1
        assert flow._scope_jobs['qq:group:202'] == 0
        assert not chat.social._open_requests and not chat.social._deliveries
        assert not flow._jobs and not flow._batches
        assert not flow._locks[('qq:group:202', '303')].locked()
    finally:
        await flow.close()


@pytest.mark.anyio
async def test_message_received_during_old_text_ack_prevents_late_sticker_selection(setup):
    _, _, chat, flow, sent, _ = setup
    entered, acknowledge = asyncio.Event(), asyncio.Event()
    async def emit(scope, text, metadata):
        sent.append((text, metadata))
        if text:
            entered.set()
            await acknowledge.wait()
        return True
    flow.emit = emit
    try:
        first = asyncio.create_task(send(flow, '第一轮'))
        await asyncio.wait_for(entered.wait(), .5)
        await send(flow, '别人的旁聊', user='404', direct=False)
        acknowledge.set()
        await asyncio.wait_for(first, .5)
        await drain_stickers(flow)
        assert not chat.stickers.started.is_set()
        assert len(sent) == 1 and not chat.stickers.sent
    finally:
        acknowledge.set()
        await flow.close()


class SlowPlanner:
    def __init__(self):
        self.calls = 0
        self.cancelled = False
        self.delay = 1
    async def complete(self, messages, **kwargs):
        self.calls += 1
        try:
            await asyncio.sleep(self.delay)
        except asyncio.CancelledError:
            self.cancelled = True
            raise
        value = json.loads(messages[-1]['content'])
        return {'content': json.dumps({'action': 'reply', 'target_message_id': value['current_message_id'],
            'target_user_id': value['current_user_id'], 'addressed_to_bot': True, 'intent': '回答',
            'reason': '当前问题需要回答', 'needed_info': []})}


@pytest.mark.anyio
@pytest.mark.parametrize('stage', ['knowledge', 'provider', 'combined'])
@pytest.mark.parametrize('direct', [True, False])
async def test_planner_deadline_covers_knowledge_and_provider_without_mandatory_fallback(setup, stage, direct):
    store, config, chat, flow, sent, _ = setup
    model = SlowPlanner()
    chat.social.provider = model
    scope = store.upsert_scope({'id': 'qq:group:202', 'plan_enabled': True})
    cancelled = asyncio.Event()
    async def knowledge(*_):
        try:
            await asyncio.sleep(1 if stage == 'knowledge' else .15)
        except asyncio.CancelledError:
            cancelled.set()
            raise
        return {}
    if stage != 'provider':
        chat.social.knowledge_fn = knowledge
    if stage == 'combined':
        config.value['planner']['max_age_ms'] = 250
        model.delay = .15
    message = store.add_message(scope['id'], '303', 'user', '这件事大家怎么看？', at_bot=direct)
    started = time.monotonic()
    try:
        result = await chat.social.plan(scope, message, direct=direct)
        elapsed = time.monotonic() - started
        assert (.18 if stage == 'combined' else .04) <= elapsed < .7
        assert result['action'] == 'skip' and not result['reply']
        assert ('等待过久' in result['error_feedback']) is direct
        assert result['planner_ms'] >= 40
        assert not chat.social._open_requests
        if stage == 'knowledge':
            assert cancelled.is_set() and model.calls == 0
        else:
            assert model.cancelled and model.calls == 1
        assert not chat.calls and not sent
    finally:
        await flow.close()


@pytest.mark.anyio
async def test_timeout_feedback_is_suppressed_after_permission_change(setup):
    store, config, chat, flow, sent, _ = setup
    chat.social.provider = SlowPlanner()
    scope = store.upsert_scope({'id': 'qq:group:202', 'plan_enabled': True})
    message = store.add_message(scope['id'], '303', 'user', '回答我', at_bot=True)
    task = asyncio.create_task(chat.social.plan(scope, message, direct=True))
    await asyncio.sleep(.02)
    config.value['paused'] = True
    result = await task
    assert result['action'] == 'skip' and result['error_feedback'] == ''
    assert not chat.calls and not sent
    await flow.close()


@pytest.mark.anyio
async def test_private_always_reply_still_skips_planner_entirely(setup):
    store, _, chat, flow, _, _ = setup
    model = SlowPlanner()
    chat.social.provider = model
    scope = store.upsert_scope({'id': 'qq:private:303', 'plan_enabled': True, 'always_reply': True})
    message = store.add_message(scope['id'], '303', 'user', '你好')
    result = await chat.social.plan(scope, message)
    assert result['reply'] and model.calls == 0
    await flow.close()


@pytest.mark.anyio
async def test_planner_hard_deadline_keeps_dispatched_provider_usage_unknown(setup, monkeypatch):
    import httpx
    from mikubot.provider import Provider
    store, config, chat, flow, sent, _ = setup
    config.value['planner']['max_age_ms'] = 250
    config.value['model']['stream_mode'] = 'off'
    dispatched = []
    async def handler(request):
        dispatched.append(request)
        await asyncio.sleep(3)
        raise AssertionError('must be cancelled before a completion is returned')
    original = httpx.AsyncClient
    monkeypatch.setattr(httpx, 'AsyncClient', lambda **kwargs: original(transport=httpx.MockTransport(handler), **kwargs))
    provider = Provider(config, store)
    chat.social.provider = provider
    scope = store.upsert_scope({'id': 'qq:group:202', 'plan_enabled': True})
    message = store.add_message(scope['id'], '303', 'user', '请回答这个问题', at_bot=True)
    try:
        decision = await chat.social.plan(scope, message, direct=True)
        assert decision['action'] == 'skip' and len(dispatched) == 1
        usage = store.list_usage()
        assert len(usage) == 1 and usage[0]['estimated'] and usage[0]['unconfirmed']
        assert usage[0]['purpose'] == 'planner' and not provider._reservations
        assert not chat.calls and not sent
    finally:
        await flow.close()
