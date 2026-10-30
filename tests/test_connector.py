import asyncio
import json
from copy import deepcopy
from datetime import datetime, timedelta, timezone

import pytest

from qunyu.db import Store
from shiori.onebot import OneBotBridge
from shiori.scheduler import TaskScheduler
from shiori.settings import DEFAULTS
from shiori.social import SocialEngine


class Config:
    def __init__(self):
        self.data = deepcopy(DEFAULTS)
        self.data['onebot'].update(chat_policy="whitelist", enabled=True, owner_ids=['101'], allowed_groups=['202'], access_token='local-test-only-token', action_timeout_seconds=.03)

    def get(self):
        return deepcopy(self.data)


class Chat:
    def __init__(self, store):
        self.store, self.calls = store, []

    async def respond(self, scope_id, user_id, text, images=None, metadata=None):
        self.calls.append((scope_id, user_id, text, images, metadata))
        self.store.add_message(scope_id, user_id, 'user', text, **(metadata or {}))
        message = self.store.add_message(scope_id, 'assistant', 'assistant', '测试回复')
        return {'reply': '测试回复', 'message_id': message['id']}


class Socket:
    def __init__(self, acknowledge=True, result=None):
        self.incoming = asyncio.Queue()
        self.sent = []
        self.acknowledge = acknowledge
        self.result = result

    async def receive_json(self):
        frame = await self.incoming.get()
        if frame is None:
            raise ConnectionError('closed')
        return frame

    async def send_json(self, data):
        self.sent.append(data)
        if self.acknowledge:
            result = self.result or {'status': 'ok', 'retcode': 0, 'data': {'message_id': 987}}
            await self.incoming.put(dict(result, echo=data['echo']))

    async def close(self, code=1000):
        await self.incoming.put(None)


@pytest.fixture
def store(tmp_path):
    value = Store(tmp_path)
    yield value
    value.close()


@pytest.fixture
def anyio_backend():
    return 'asyncio'


async def connected(bridge, socket):
    task = asyncio.create_task(bridge.attach(socket))
    for _ in range(100):
        if bridge.status()['connected']:
            return task
        await asyncio.sleep(0)
    raise AssertionError('mock socket did not attach')


def event(message_id=1, user_id=303, group_id=202, mention=True):
    parts = [{'type': 'text', 'data': {'text': '你好'}}]
    if mention:
        parts.insert(0, {'type': 'at', 'data': {'qq': '999'}})
    return {'post_type': 'message', 'message_type': 'group', 'self_id': 999, 'message_id': message_id,
            'user_id': user_id, 'group_id': group_id, 'sender': {'nickname': '测试成员'}, 'message': parts}


@pytest.mark.anyio
async def test_private_owner_and_group_allowlist_with_durable_dedup(store):
    cfg, chat = Config(), Chat(store)
    bridge = OneBotBridge(store, cfg, chat)
    socket = Socket()
    reader = await connected(bridge, socket)
    try:
        await bridge.handle_event(event(group_id=404))
        private = dict(event(), message_type='private', user_id=303)
        await bridge.handle_event(private)
        assert chat.calls == []
        await bridge.handle_event(event())
        await bridge.handle_event(event())
        assert len(chat.calls) == 1
        assert chat.calls[0][1] == '303'
        assert chat.calls[0][4]['is_owner'] is False
        assert socket.sent[0]['params']['group_id'] == 202
        other = OneBotBridge(store, cfg, chat)
        await other.handle_event(event())
        assert len(chat.calls) == 1
        await bridge.handle_event(dict(private, user_id=101, message_id=2))
        assert chat.calls[-1][4]['is_owner'] is True
        assert socket.sent[-1]['action'] == 'send_private_msg'
    finally:
        await bridge.stop()
        await reader


@pytest.mark.anyio
async def test_group_mention_active_cooldown_and_image_provenance(store):
    cfg = Config()
    cfg.data['identity'].update(respond_to_at=False, respond_to_name=False)
    cfg.data['model'].update(vision=True, base_url='https://mock.invalid/v1', model='planner-test')
    class Planner:
        def __init__(self): self.calls = []
        async def complete(self, messages, **kwargs):
            self.calls.append((messages, kwargs))
            payload = json.loads(messages[-1]['content'])
            return {'content': json.dumps({'action': 'reply', 'target_message_id': payload['current_message_id'],
                'target_user_id': payload['current_user_id'], 'addressed_to_bot': payload['signals']['at_bot'],
                'intent': '回应当前成员发送的图片与文字', 'reason': '本轮消息有可回应的信息且无人回答', 'needed_info': []})}
    planner = Planner()
    class ObservedChat(Chat):
        def __init__(self):
            super().__init__(store)
            self.social = SocialEngine(store, cfg, planner)
        async def observe(self, scope_id, user_id, text, images=None, metadata=None):
            record = store.add_message(scope_id, user_id, 'user', text, **metadata)
            self.social.observe(scope_id, record)
            return record
        async def respond(self, scope_id, user_id, text, images=None, metadata=None):
            self.calls.append((scope_id, user_id, text, images, metadata))
            message = store.add_message(scope_id, 'assistant', 'assistant', '测试回复', source_ids=metadata['source_ids'])
            return {'reply': '测试回复', 'message_id': message['id']}
    chat = ObservedChat()
    store.upsert_scope({'id': 'qq:group:202', 'proactive_reply': False})
    bridge = OneBotBridge(store, cfg, chat)
    reader = await connected(bridge, Socket())
    try:
        await bridge.handle_event(event(1, mention=False))
        assert not chat.calls
        store.upsert_scope({'id': 'qq:group:202', 'activity': 'active', 'proactive_reply': True, 'settings': {'cooldown_seconds': 60}})
        image = event(2, mention=False)
        image['message'].append({'type': 'image', 'data': {'url': 'https://example.com/picture.png'}})
        await bridge.handle_event(image)
        assert chat.calls[-1][3] == ['https://example.com/picture.png']
        assert chat.calls[-1][4]['platform_message_id'] == '2'
        await bridge.handle_event(event(3, mention=False))
        assert len(chat.calls) == 1
        await bridge.handle_event(event(4, mention=True))
        assert len(chat.calls) == 2
        assert len(planner.calls) == 2 and all(x[1]['purpose'] == 'planner' for x in planner.calls)
        assert len([m for m in store.list_messages('qq:group:202') if m['role'] == 'user']) == 4
    finally:
        await bridge.stop()
        await reader


@pytest.mark.anyio
@pytest.mark.parametrize('acknowledge,result', [(False, None), (True, {'status': 'ok', 'retcode': 0, 'data': {}}), (True, {'status': 'failed', 'retcode': 100, 'data': None})])
async def test_echo_timeout_or_missing_send_receipt_never_delivered(store, acknowledge, result):
    cfg = Config()
    bridge = OneBotBridge(store, cfg, Chat(store))
    store.get_scope('qq:group:202')
    reader = await connected(bridge, Socket(acknowledge, result))
    try:
        assert await bridge.send('qq:group:202', '测试') is False
        assert not bridge._pending
    finally:
        await bridge.stop()
        await reader


@pytest.mark.anyio
async def test_reader_keeps_processing_echo_while_event_reply_waits(store):
    cfg, chat = Config(), Chat(store)
    bridge = OneBotBridge(store, cfg, chat)
    socket = Socket()
    reader = await connected(bridge, socket)
    try:
        await socket.incoming.put(event())
        for _ in range(200):
            if chat.calls and not bridge._events:
                break
            await asyncio.sleep(.001)
        assert len(socket.sent) == 1
        assert not [e for e in store.list_events() if e['kind'] == 'qq_delivery']
        cfg.data['onebot']['adapter'] = 'official'
        assert await bridge.send('qq:group:202', '禁止跨适配器发送') is False
        assert not bridge.status()['connected']
    finally:
        await bridge.stop()
        await reader


class Bridge:
    def __init__(self, online=True, outcome=True):
        self.online, self.outcome, self.sent = online, outcome, []

    def status(self):
        return {'connected': self.online}

    async def send(self, scope_id, text):
        self.sent.append((scope_id, text))
        return self.outcome


def reminder(store, now, scope='web:default', **extra):
    return store.create_task(dict(scope_id=scope, user_id='101', kind='reminder', title='喝水', due_at=(now - timedelta(minutes=1)).isoformat(), **extra))


@pytest.mark.anyio
async def test_local_reminder_persisted_once_and_repeat_skips_missed_days(store):
    now = datetime(2026, 10, 15, 8, tzinfo=timezone.utc)
    cfg, bridge = Config(), Bridge()
    once = reminder(store, now)
    repeat = reminder(store, now - timedelta(days=5), repeat='daily')
    cancelled = reminder(store, now, status='cancelled')
    scheduler = TaskScheduler(store, cfg, bridge)
    await asyncio.gather(scheduler.tick(now), scheduler.tick(now))
    assert store.get_task(once['id'])['delivery_status'] == 'delivered'
    next_task = store.get_task(repeat['id'])
    assert datetime.fromisoformat(next_task['due_at']) > now
    assert next_task['status'] == 'pending'
    assert store.get_task(cancelled['id'])['status'] == 'cancelled'
    assert len(store.list_messages('web:default')) == 2
    assert not bridge.sent


@pytest.mark.anyio
async def test_qq_offline_preserved_and_backlog_drained_without_burst(store):
    now = datetime(2026, 10, 15, 8, tzinfo=timezone.utc)
    cfg, bridge = Config(), Bridge(online=False)
    first = reminder(store, now, 'qq:group:202')
    second = reminder(store, now, 'qq:group:202')
    scheduler = TaskScheduler(store, cfg, bridge)
    await scheduler.tick(now)
    assert store.get_task(first['id'])['status'] == 'pending'
    bridge.online = True
    await scheduler.tick(now)
    await scheduler.tick(now + timedelta(seconds=5))
    assert len(bridge.sent) == 1
    await scheduler.tick(now + timedelta(seconds=61))
    assert len(bridge.sent) == 2
    assert store.get_task(second['id'])['delivery_status'] == 'delivered'


@pytest.mark.anyio
async def test_unknown_send_and_interrupted_task_require_manual_retry(store):
    now = datetime(2026, 10, 15, 8, tzinfo=timezone.utc)
    cfg, bridge = Config(), Bridge(outcome=False)
    task = reminder(store, now, 'qq:group:202')
    interrupted = reminder(store, now, 'qq:private:101', status='running')
    scheduler = TaskScheduler(store, cfg, bridge)
    await scheduler.tick(now)
    await scheduler.tick(now + timedelta(minutes=2))
    assert len(bridge.sent) == 1
    assert store.get_task(task['id'])['status'] == 'failed'
    assert store.get_task(task['id'])['delivery_status'] != 'delivered'
    assert store.get_task(interrupted['id'])['status'] == 'failed'


@pytest.mark.anyio
async def test_revoked_scope_adapter_switch_and_pause_prevent_scheduled_sends(store):
    now = datetime(2026, 10, 15, 8, tzinfo=timezone.utc)
    cfg, bridge = Config(), Bridge()
    task = reminder(store, now, 'qq:group:202')
    scheduler = TaskScheduler(store, cfg, bridge)
    cfg.data['onebot']['adapter'] = 'official'
    await scheduler.tick(now)
    cfg.data['onebot']['adapter'] = 'napcat'
    cfg.data['paused'] = True
    await scheduler.tick(now)
    cfg.data['paused'] = False
    store.upsert_scope({'id': 'qq:group:202', 'enabled': False})
    await scheduler.tick(now)
    assert not bridge.sent
    assert store.get_task(task['id'])['status'] == 'pending'
