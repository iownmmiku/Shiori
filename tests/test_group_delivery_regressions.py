"""Group ingress replay and bounded OneBot delivery, using local transports only."""
import asyncio
import json
from copy import deepcopy

import pytest

from shiori.chat import ChatService
from shiori.db import Store
from shiori.delivery import DeliveryLedger
from shiori.onebot import OneBotBridge
from shiori.settings import DEFAULTS


@pytest.fixture
def anyio_backend():
    return 'asyncio'


class Settings:
    def __init__(self):
        self.config = deepcopy(DEFAULTS)
        self.config['onebot'].update(enabled=True, chat_policy='blacklist', action_timeout_seconds=.04)
        self.config['output'].update(mode='model', interval_ms=0)
        self.config['stickers']['enabled'] = False

    def get(self):
        return deepcopy(self.config)


class Chat:
    def __init__(self):
        self.calls = []

    async def respond(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        return {'reply': 'Hello', 'message_id': 'fixture'}


def event(*, scope=202, self_id=999, message_id='original'):
    return {'post_type': 'message', 'message_type': 'group', 'group_id': scope,
            'self_id': self_id, 'user_id': 303, 'message_id': message_id,
            'message': [{'type': 'at', 'data': {'qq': str(self_id)}},
                        {'type': 'text', 'data': {'text': 'Hello'}}]}


@pytest.mark.anyio
async def test_restart_replay_is_deduplicated_beyond_recent_history(tmp_path):
    store = Store(tmp_path)
    original = store.add_message('qq:group:202', '303', 'user', 'Hello',
                                 platform_message_id='original', onebot_self_id='999')
    for i in range(201):
        store.add_message('qq:group:202', '404', 'user', f'Other message {i}')
    assert original['id'] not in {item['id'] for item in store.list_messages('qq:group:202', 200)}
    store.close()
    store = Store(tmp_path)
    chat = Chat()
    bridge = OneBotBridge(store, Settings(), chat)
    try:
        await bridge.handle_event(event())
        assert not chat.calls
        assert len(store.list_messages('qq:group:202', 1000)) == 202
        assert bridge.status()['last_message_result']['code'] == 'duplicate_message'
    finally:
        await bridge.stop()
        store.close()


@pytest.mark.anyio
@pytest.mark.parametrize('scope,self_id', [(404, 999), (202, 888)])
async def test_same_platform_id_from_another_group_or_bot_is_not_a_duplicate(tmp_path, scope, self_id):
    store, chat = Store(tmp_path), Chat()
    store.add_message('qq:group:202', '303', 'user', 'Hello',
                      platform_message_id='original', onebot_self_id='999')
    bridge = OneBotBridge(store, Settings(), chat)
    try:
        await bridge.handle_event(event(scope=scope, self_id=self_id))
        assert len(chat.calls) == 1
    finally:
        await bridge.stop()
        store.close()


def test_provenance_lookup_is_indexed_and_handles_legacy_metadata(tmp_path):
    store = Store(tmp_path)
    try:
        store.add_message('qq:group:202', '303', 'user', 'Legacy without provenance')
        store.add_message('qq:group:202', '303', 'user', 'Legacy numeric IDs',
                          platform_message_id=123, onebot_self_id=999)
        store.add_message('qq:group:202', '303', 'user', 'Legacy unknown bot', platform_message_id='legacy')
        store.add_message('qq:group:202', 'assistant', 'assistant', 'An outgoing reply',
                          platform_message_id='assistant-only', onebot_self_id='999')
        assert store.onebot_message_seen('qq:group:202', '123', '999')
        assert store.onebot_message_seen('qq:group:202', 'legacy', '')
        assert not store.onebot_message_seen('qq:group:202', 'legacy', '999')
        assert not store.onebot_message_seen('qq:group:202', '', '')
        assert not store.onebot_message_seen('qq:group:202', 'assistant-only', '999')
        statements = []
        store._conn.set_trace_callback(statements.append)
        assert store.onebot_message_seen('qq:group:202', '123', '999')
        store._conn.set_trace_callback(None)
        lookup = next(statement for statement in statements if statement.startswith('SELECT 1 FROM messages'))
        plan = store._conn.execute('EXPLAIN QUERY PLAN ' + lookup).fetchall()
        assert any('SEARCH' in row[3] and 'messages_onebot_provenance' in row[3] for row in plan)
    finally:
        store.close()


def test_old_backup_without_provenance_index_remains_restorable(tmp_path):
    import sqlite3
    store = Store(tmp_path / 'live')
    try:
        store.add_message('qq:group:202', '303', 'user', 'Hello',
                          platform_message_id='original', onebot_self_id='999')
        backup = store.backup(tmp_path / 'old.db')
        with sqlite3.connect(backup) as connection:
            connection.execute('DROP INDEX messages_onebot_provenance')
        assert store.restore_from(backup, validate_only=True)['valid']
        store.restore_from(backup)
        assert store.onebot_message_seen('qq:group:202', 'original', '999')
        assert store._conn.execute("SELECT 1 FROM sqlite_master WHERE name='messages_onebot_provenance'").fetchone()
    finally:
        store.close()


def test_backup_with_modified_provenance_index_is_still_rejected(tmp_path):
    import sqlite3
    store = Store(tmp_path / 'live')
    try:
        backup = store.backup(tmp_path / 'modified.db')
        with sqlite3.connect(backup) as connection:
            connection.execute('DROP INDEX messages_onebot_provenance')
            connection.execute('CREATE INDEX messages_onebot_provenance ON messages(user_id)')
        with pytest.raises(ValueError, match='结构不兼容'):
            store.restore_from(backup, validate_only=True)
    finally:
        store.close()


class Socket:
    def __init__(self, *, blocked=False):
        self.sent, self.incoming = [], asyncio.Queue()
        self.dispatched, self.write_cancelled = asyncio.Event(), asyncio.Event()
        self.blocked = blocked

    async def send_json(self, action):
        self.sent.append(action)
        self.dispatched.set()
        if self.blocked:
            try:
                await asyncio.Event().wait()
            finally:
                self.write_cancelled.set()

    async def receive_json(self):
        return await self.incoming.get()

    async def close(self, *args, **kwargs):
        pass


class TextSocket(Socket):
    send_json = None

    def __getattribute__(self, name):
        if name == 'send_json':
            raise AttributeError(name)
        return super().__getattribute__(name)

    async def send(self, text):
        await Socket.send_json(self, json.loads(text))


@pytest.mark.anyio
@pytest.mark.parametrize('transport', [Socket, TextSocket])
async def test_blocked_socket_write_obeys_action_deadline_and_clears_pending(tmp_path, transport):
    store = Store(tmp_path)
    bridge = OneBotBridge(store, Settings(), Chat())
    socket = transport(blocked=True)
    store.get_scope('qq:group:202')
    bridge._socket = socket
    try:
        assert not await asyncio.wait_for(bridge.send('qq:group:202', 'Hello'), .5)
        assert socket.write_cancelled.is_set()
        assert len(socket.sent) == 1 and not bridge._pending
        assert not [item for item in store.list_events() if item['kind'] == 'qq_sent']
        assert any(item['kind'] == 'qq_delivery' and '未知' in item['message'] for item in store.list_events())
    finally:
        await bridge.stop()
        store.close()


@pytest.mark.anyio
async def test_write_and_ack_share_one_action_deadline(tmp_path):
    store, config = Store(tmp_path), Settings()
    config.config['onebot']['action_timeout_seconds'] = .3
    class SlowWrite(Socket):
        async def send_json(self, action):
            await super().send_json(action)
            await asyncio.sleep(.2)
    bridge = OneBotBridge(store, config, Chat())
    socket = SlowWrite()
    store.get_scope('qq:group:202')
    bridge._socket = socket
    try:
        # Separate .3-second write/ACK timeouts would still be waiting at .45.
        assert not await asyncio.wait_for(bridge.send('qq:group:202', 'Hello'), .45)
        assert len(socket.sent) == 1 and not bridge._pending
        detail = next(item['details'] for item in store.list_events() if item['kind'] == 'qq_delivery')
        assert detail['send_timing']['socket_write_ms'] >= 100
    finally:
        await bridge.stop()
        store.close()


@pytest.mark.anyio
async def test_cancellation_during_socket_write_is_unknown_and_never_replayed(tmp_path):
    store, config = Store(tmp_path), Settings()
    config.config['onebot']['action_timeout_seconds'] = 10
    bridge = OneBotBridge(store, config, Chat())
    socket = Socket(blocked=True)
    store.get_scope('qq:group:202')
    bridge._socket = socket
    task = asyncio.create_task(bridge.send('qq:group:202', 'Hello'))
    try:
        await asyncio.wait_for(socket.dispatched.wait(), 1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert len(socket.sent) == 1 and not bridge._pending and socket.write_cancelled.is_set()
        detail = next(item['details'] for item in store.list_events() if item['kind'] == 'qq_delivery')
        assert detail['delivery_status'] == 'unknown'
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await bridge.stop()
        store.close()


@pytest.mark.anyio
async def test_late_ack_cannot_confirm_a_different_message_or_replay_timed_out_text(tmp_path):
    store, config = Store(tmp_path), Settings()
    bridge, socket = OneBotBridge(store, config, Chat()), Socket()
    store.get_scope('qq:group:202')
    reader = asyncio.create_task(bridge._read_connection(socket, 'reverse'))
    await asyncio.sleep(0)
    try:
        assert not await bridge.send('qq:group:202', 'Old message')
        old_echo = socket.sent[0]['echo']
        config.config['onebot']['action_timeout_seconds'] = 1
        socket.dispatched.clear()
        second = asyncio.create_task(bridge.send('qq:group:202', 'New message'))
        await asyncio.wait_for(socket.dispatched.wait(), 1)
        await socket.incoming.put({'echo': old_echo, 'status': 'ok', 'retcode': 0, 'data': {'message_id': 10}})
        await asyncio.sleep(0)
        assert not second.done()
        await socket.incoming.put({'echo': socket.sent[1]['echo'], 'status': 'ok', 'retcode': 0, 'data': {'message_id': 11}})
        assert await asyncio.wait_for(second, 1)
        assert len(socket.sent) == 2 and not bridge._pending
        confirmed = [item for item in store.list_events() if item['kind'] == 'qq_sent']
        assert [item['details']['platform_message_id'] for item in confirmed] == ['11']
    finally:
        reader.cancel()
        await asyncio.gather(reader, return_exceptions=True)
        await bridge.stop()
        store.close()


@pytest.mark.anyio
async def test_old_socket_cannot_acknowledge_a_new_connection_send(tmp_path):
    store, config = Store(tmp_path), Settings()
    config.config['onebot']['action_timeout_seconds'] = 1
    bridge = OneBotBridge(store, config, Chat())
    old, new = Socket(), Socket()
    store.get_scope('qq:group:202')
    old_reader = asyncio.create_task(bridge._read_connection(old, 'reverse'))
    await asyncio.sleep(0)
    new_reader = asyncio.create_task(bridge._read_connection(new, 'reverse'))
    await asyncio.sleep(0)
    task = asyncio.create_task(bridge.send('qq:group:202', 'New connection'))
    try:
        await asyncio.wait_for(new.dispatched.wait(), 1)
        ack = {'echo': new.sent[0]['echo'], 'status': 'ok', 'retcode': 0, 'data': {'message_id': 12}}
        await old.incoming.put(ack)
        await asyncio.sleep(0)
        assert not task.done()
        await new.incoming.put(ack)
        assert await asyncio.wait_for(task, 1)
        assert len(new.sent) == 1 and not old.sent and not bridge._pending
    finally:
        task.cancel()
        old_reader.cancel()
        new_reader.cancel()
        await asyncio.gather(task, old_reader, new_reader, return_exceptions=True)
        await bridge.stop()
        store.close()


@pytest.mark.anyio
async def test_blocked_second_part_stops_remaining_parts_and_releases_group_output_lock(tmp_path):
    store, config = Store(tmp_path), Settings()
    scope = 'qq:group:202'
    store.upsert_scope({'id': scope, 'plan_enabled': False, 'auto_memory': False, 'merge_window_ms': 0})
    class Model:
        async def complete(self, *args, **kwargs):
            text = 'First.\n\nSecond.\n\nThird.'
            return {'content': text, 'message': {'role': 'assistant', 'content': text}, 'tool_calls': [], 'usage': {}}
    chat = ChatService(store, config, Model())
    bridge = OneBotBridge(store, config, chat)
    class PartialSocket(Socket):
        async def send_json(self, action):
            await super().send_json(action)
            if len(self.sent) == 2:
                await asyncio.Event().wait()
            bridge._pending[action['echo']][1].set_result({'status': 'ok', 'retcode': 0,
                                                         'data': {'message_id': len(self.sent)}})
    socket = PartialSocket()
    bridge._socket = socket
    try:
        await asyncio.wait_for(bridge._flow.handle(scope, '303', 'Hello', [], {'at_bot': True}, direct=True), 2)
        assert len(socket.sent) == 2 and not bridge._pending
        assert not bridge._flow._output_locks[scope].locked()
        receipt = DeliveryLedger(store).list(scope)[0]
        assert [part['status'] for part in receipt['parts']] == ['confirmed', 'unknown', 'pending']
        # A later real request can still deliver its own complete reply.
        await asyncio.wait_for(bridge._flow.handle(scope, '303', 'New question', [], {'at_bot': True}, direct=True), 2)
        assert len(socket.sent) == 5
    finally:
        await bridge.stop()
        await chat.close()
        store.close()
