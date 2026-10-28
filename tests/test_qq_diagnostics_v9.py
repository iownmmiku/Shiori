"""Real SQLite + mocked OneBot/official sockets and model; no real QQ sends."""
import asyncio
import json
import time
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from qunyu.db import Store
from shiori.onebot import OneBotBridge
from shiori.official import OfficialBridge
from shiori.settings import DEFAULTS
from shiori.social import SocialEngine


@pytest.fixture
def anyio_backend():
    return 'asyncio'


class Config:
    def __init__(self):
        self.data = deepcopy(DEFAULTS)
        self.data['onebot'].update(chat_policy="whitelist", enabled=True, adapter='napcat', allowed_users=['101'], allowed_groups=['202'], action_timeout_seconds=.1)
        self.data['model'].update(base_url='https://fake.invalid/v1', model='fake')
    def get(self): return deepcopy(self.data)


class Provider:
    def __init__(self): self.calls = []
    async def complete(self, messages, **kwargs):
        self.calls.append(kwargs)
        body = json.loads(messages[-1]['content'])
        return {'content': json.dumps({'action': 'skip', 'target_message_id': body['current_message_id'],
            'target_user_id': body['current_user_id'], 'addressed_to_bot': False, 'intent': '',
            'reason': 'quoted-private-secret-from-model', 'needed_info': []})}


class Chat:
    def __init__(self, store, cfg):
        self.store, self.cfg, self.calls, self.fail, self.empty = store, cfg, [], False, False
        self.provider = Provider()
        self.social = SocialEngine(store, cfg, self.provider)
    async def observe(self, scope_id, user_id, text, images=None, metadata=None):
        item = self.store.add_message(scope_id, user_id, 'user', text, **(metadata or {}))
        self.social.observe(scope_id, item)
        return item
    async def respond(self, scope_id, user_id, text, images=None, metadata=None):
        self.calls.append((scope_id, user_id, text, metadata))
        if self.fail:
            raise RuntimeError('private-token-secret https://private.invalid/folder/user')
        item = self.store.add_message(scope_id, 'assistant', 'assistant', '模拟回复', source_ids=metadata['source_ids'])
        return {'reply': '' if self.empty else '模拟回复', 'message_id': item['id']}


class Socket:
    def __init__(self):
        self.queue, self.sent = asyncio.Queue(), []
        self.headers = {'x-self-id': '999'}
    async def receive_json(self):
        item = await self.queue.get()
        if item is None: raise ConnectionError('closed')
        return item
    async def send_json(self, item):
        self.sent.append(item)
        await self.queue.put({'echo': item['echo'], 'status': 'ok', 'retcode': 0, 'data': {'message_id': 1000 + len(self.sent)}})
    async def close(self, **kwargs): await self.queue.put(None)


def message(id=1, *, user=101, group=None, text='你好', at=False):
    parts = [{'type': 'text', 'data': {'text': text}}]
    if at: parts.insert(0, {'type': 'at', 'data': {'qq': '999'}})
    return {'post_type': 'message', 'message_type': 'group' if group else 'private', 'self_id': 999,
            'message_id': id, 'user_id': user, 'group_id': group, 'message': parts}


@pytest.fixture
def onebot(tmp_path):
    store, cfg = Store(tmp_path), Config()
    chat = Chat(store, cfg)
    bridge = OneBotBridge(store, cfg, chat)
    yield bridge, cfg, chat, store
    store.close()


async def connect(bridge, socket):
    task = asyncio.create_task(bridge.attach(socket))
    for _ in range(100):
        if bridge.status()['connected']: return task
        await asyncio.sleep(0)
    raise AssertionError('test socket did not connect')


@pytest.mark.anyio
async def test_new_and_old_implicit_private_are_always_reply_without_planner(onebot):
    bridge, cfg, chat, store = onebot
    old = store.get_scope('qq:private:101')
    assert old['always_reply'] is False and old['plan_enabled'] is None
    socket = Socket(); reader = await connect(bridge, socket)
    try:
        await bridge.handle_event(message(1))
        await bridge.handle_event(message(2))
        assert len(chat.calls) == 2 and chat.provider.calls == []
        scope = store.get_scope('qq:private:101')
        assert scope['always_reply'] is True and scope['plan_enabled'] is False
        assert len([x for x in store.list_events(limit=100) if x['kind'] == 'qq_private_policy']) == 1
        assert bridge.status()['last_message_result']['code'] == 'sent'
        assert bridge.status()['message_events_received'] == 2
        assert len(socket.sent) == 2
    finally:
        await bridge.stop(); await reader


@pytest.mark.anyio
async def test_explicit_private_planner_skip_is_preserved_and_diagnosed(onebot):
    bridge, _, chat, store = onebot
    store.upsert_scope({'id': 'qq:private:101', 'always_reply': False, 'plan_enabled': True})
    await bridge.handle_event(message())
    assert not chat.calls and len(chat.provider.calls) == 1
    assert store.get_scope('qq:private:101')['plan_enabled'] is True
    assert bridge.status()['last_message_result']['code'] == 'planner_skip'
    events = json.dumps(store.list_events(limit=100))
    assert 'quoted-private-secret-from-model' not in events
    await bridge.stop()


@pytest.mark.anyio
async def test_heartbeat_is_distinct_from_message_ingress(onebot):
    bridge, _, _, _ = onebot
    socket = Socket(); reader = await connect(bridge, socket)
    try:
        await bridge.handle_event({'post_type': 'meta_event', 'meta_event_type': 'heartbeat', 'self_id': 999})
        status = bridge.status()
        assert status['heartbeat_events_received'] == 1 and status['message_events_received'] == 0
        assert status['last_message_at'] is None and '心跳' in status['diagnostic']
        assert '消息上报' in status['diagnostic']
    finally:
        await bridge.stop(); await reader


@pytest.mark.anyio
async def test_allowlist_diagnostics_are_rate_limited_and_never_store_message_text(onebot):
    bridge, _, chat, store = onebot
    for index in range(10):
        await bridge.handle_event(message(index + 1, user=500 + index, text='private-secret-body'))
    assert not chat.calls
    assert bridge.status()['last_message_result']['code'] == 'private_not_allowed'
    assert bridge.status()['filtered_messages'] == 10
    events = [x for x in store.list_events(limit=100) if x['kind'] == 'qq_ingress']
    assert len(events) == 1
    assert 'private-secret-body' not in json.dumps(events)
    assert store.list_messages('qq:private:500') == []
    await bridge.stop()


@pytest.mark.anyio
async def test_known_connection_self_id_handles_group_at_when_event_omits_id(onebot):
    bridge, _, chat, store = onebot
    store.upsert_scope({'id': 'qq:group:202', 'plan_enabled': False})
    socket = Socket(); reader = await connect(bridge, socket)
    try:
        incoming = message(group=202, at=True)
        incoming.pop('self_id')
        await bridge.handle_event(incoming)
        assert len(chat.calls) == 1 and chat.calls[0][3]['at_bot'] is True
        assert chat.calls[0][3]['onebot_self_id'] == '999'
        assert len(socket.sent) == 1
    finally:
        await bridge.stop(); await reader


@pytest.mark.anyio
async def test_failed_generation_sends_one_generic_receipt_and_no_secret(onebot):
    bridge, _, chat, store = onebot
    chat.fail = True
    socket = Socket(); reader = await connect(bridge, socket)
    try:
        await bridge.handle_event(message(1))
        await bridge.handle_event(message(2))
        assert len(chat.calls) == 2 and len(socket.sent) == 1
        response = socket.sent[0]['params']['message'][0]['data']['text']
        assert '运行日志' in response
        assert 'private-token-secret' not in json.dumps(socket.sent)
        assert 'private-token-secret' not in json.dumps(store.list_events(limit=100))
        assert bridge.status()['last_message_result']['code'] == 'reply_failed'
    finally:
        await bridge.stop(); await reader


@pytest.mark.anyio
async def test_empty_generation_is_visible_and_gets_generic_receipt(onebot):
    bridge, _, chat, _ = onebot
    chat.empty = True
    socket = Socket(); reader = await connect(bridge, socket)
    try:
        await bridge.handle_event(message())
        assert len(socket.sent) == 1 and '没有生成' in socket.sent[0]['params']['message'][0]['data']['text']
        assert bridge.status()['last_message_result']['code'] in ('reply_empty', 'reply_failed')
    finally:
        await bridge.stop(); await reader


@pytest.mark.anyio
async def test_group_scope_permission_and_paused_are_distinct(onebot):
    bridge, cfg, _, store = onebot
    await bridge.handle_event(message(group=404, at=True))
    assert bridge.status()['last_message_result']['code'] == 'group_not_allowed'
    cfg.data['paused'] = True
    await bridge.handle_event(message(group=202, at=True))
    assert bridge.status()['last_message_result']['code'] == 'paused'
    cfg.data['paused'] = False
    store.upsert_scope({'id': 'qq:group:202', 'enabled': False})
    await bridge.handle_event(message(group=202, at=True))
    assert bridge.status()['last_message_result']['code'] == 'scope_disabled'
    await bridge.stop()


@pytest.mark.anyio
async def test_official_private_defaults_and_error_receipts(tmp_path):
    store, cfg = Store(tmp_path), Config()
    cfg.data['onebot']['adapter'] = 'official'
    cfg.data['qq_official'].update(chat_policy="whitelist", enabled=True, app_id='app', app_secret='fixture', allowed_users=['user-openid'])
    chat = Chat(store, cfg)
    bridge = OfficialBridge(store, cfg, chat)
    bridge._running = bridge._connected = True
    api = SimpleNamespace(post_c2c_message=AsyncMock(return_value={'id': 'sent'}))
    bridge._client = SimpleNamespace(api=api)
    def incoming(id):
        return {'t': 'C2C_MESSAGE_CREATE', 'd': {'id': id, 'author': {'user_openid': 'user-openid'}, 'content': '你好'}}
    try:
        await bridge.handle_event(incoming('first'))
        assert len(chat.calls) == 1 and not chat.provider.calls
        assert store.get_scope('qqofficial:private:user-openid')['always_reply'] is True
        chat.fail = True
        await bridge.handle_event(incoming('second'))
        assert api.post_c2c_message.await_count == 2
        assert '运行日志' in api.post_c2c_message.await_args.kwargs['content']
        assert bridge.status()['last_message_result']['code'] == 'reply_failed'
    finally:
        bridge._client = None
        await bridge.stop()
        store.close()


def test_actual_fastapi_reverse_ws_chat_service_and_delivery_receipt(tmp_path):
    from fastapi.testclient import TestClient
    from shiori.api import create_app
    app = create_app(tmp_path / 'actual-pipeline', start_workers=False)
    cfg = app.state.settings
    cfg.update({'onebot': {'enabled': True, 'adapter': 'napcat', 'allowed_users': ['101'],
        'access_token': 'isolated-test-onebot-token'}, 'model': {'base_url': 'https://fake.invalid/v1',
        'model': 'mock', 'api_key': 'fixture-key'}})
    app.state.store.upsert_scope({'id': 'qq:private:101', 'auto_memory': False})
    calls = []
    async def complete(messages, **kwargs):
        calls.append(kwargs.get('purpose', 'chat'))
        if kwargs.get('before_send'):
            assert kwargs['before_send']()
        return {'content': '来自真实 ChatService 流程的模拟回复', 'tool_calls': [], 'usage': {}}
    app.state.provider.complete = complete
    with TestClient(app) as client:
        with client.websocket_connect('/onebot/v11/ws', headers={
                'Authorization': 'Bearer isolated-test-onebot-token', 'X-Self-ID': '999'}) as ws:
            ws.send_json({'post_type': 'meta_event', 'meta_event_type': 'heartbeat', 'self_id': 999})
            ws.send_json(message())
            action = ws.receive_json()
            assert action['action'] == 'send_private_msg'
            assert action['params']['user_id'] == 101
            assert '真实 ChatService' in action['params']['message'][0]['data']['text'], (calls, app.state.store.list_events(limit=10))
            ws.send_json({'status': 'ok', 'retcode': 0, 'echo': action['echo'], 'data': {'message_id': 777}})
            for _ in range(100):
                if (app.state.onebot.status().get('last_message_result') or {}).get('code') == 'sent':
                    break
                time.sleep(.01)
            assert app.state.onebot.status()['last_message_result']['code'] == 'sent'
            assert any(item['kind'] == 'qq_sent' and item['details'].get('platform_message_id') == '777'
                       for item in app.state.store.list_events(limit=20))
            assert calls == ['chat']
            assert app.state.store.get_scope('qq:private:101')['always_reply'] is True
            assert app.state.onebot.status()['message_events_received'] == 1


def test_real_unconfigured_provider_returns_bounded_qq_error_receipt(tmp_path):
    from fastapi.testclient import TestClient
    from shiori.api import create_app
    app = create_app(tmp_path / 'unconfigured-model', start_workers=False)
    app.state.settings.update({'onebot': {'enabled': True, 'adapter': 'napcat', 'allowed_users': ['101'],
        'access_token': 'isolated-test-onebot-token'}})
    # Keep the actual Provider: an empty model configuration must fail before
    # making any HTTP/model call and the QQ user should receive a useful receipt.
    with TestClient(app) as client:
        with client.websocket_connect('/onebot/v11/ws', headers={
                'Authorization': 'Bearer isolated-test-onebot-token', 'X-Self-ID': '999'}) as ws:
            ws.send_json(message())
            action = ws.receive_json()
            assert action['action'] == 'send_private_msg'
            reply = action['params']['message'][0]['data']['text']
            assert '运行日志' in reply and '模型' in reply and len(reply) < 200
            ws.send_json({'status': 'ok', 'retcode': 0, 'echo': action['echo'], 'data': {'message_id': 888}})
            assert app.state.onebot.status()['last_message_result']['code'] == 'reply_failed'


@pytest.mark.anyio
async def test_private_chat_allowlist_is_shared_by_memory_context_reminders_and_diagnostics(tmp_path):
    from shiori.context import ContextEngine
    from shiori.memory import MemoryEngine
    from shiori.scheduler import TaskScheduler
    from shiori.diagnostics import build_diagnostics
    store, cfg = Store(tmp_path), Config()
    cfg.data['onebot']['owner_ids'] = []
    scope = 'qq:private:101'
    memory = MemoryEngine(store, cfg)
    context = ContextEngine(store, cfg, None)
    bridge = SimpleNamespace(status=lambda: {'connected': True}, send=AsyncMock(return_value=True))
    scheduler = TaskScheduler(store, cfg, bridge)
    now = datetime.now(timezone.utc)
    try:
        fact = store.create_memory({'scope_id': scope, 'user_id': '101', 'content': '喜欢薄荷绿色', 'visibility': 'private'})
        assert fact['id'] in {item['id'] for item in await memory.recall(scope, '101', '薄荷绿色')}
        assert context._permission(scope, '101')
        task = store.create_task({'scope_id': scope, 'user_id': '101', 'kind': 'reminder', 'title': '喝水',
                                  'due_at': (now - timedelta(seconds=1)).isoformat()})
        await scheduler.tick(now)
        assert store.get_task(task['id'])['delivery_status'] == 'delivered'
        bridge.send.assert_awaited_once()
        report = build_diagnostics(store, cfg, SimpleNamespace(status=lambda: {}), bridge)
        assert report['qq']['allowed_users'] == 1
        cfg.data['onebot']['allowed_users'] = []
        assert await memory.recall(scope, '101', '薄荷绿色') == []
        assert not context._permission(scope, '101')
        assert not scheduler._allowed({'scope_id': scope}, store.get_scope(scope), now)
    finally:
        memory.close(); context.close(); store.close()
