"""Work that grew with the data must not hold the event loop (server stalls, 2026-11-16)."""
import asyncio
import json
import threading
from copy import deepcopy

import httpx
import pytest

from shiori import model_stream
from shiori.chat import ChatService
from shiori.db import Store
from shiori.delivery import DeliveryLedger
from shiori.model_stream import receive_chat
from shiori.onebot import OneBotBridge
from shiori.settings import DEFAULTS


def plan(store, sql, params=()):
    return ' | '.join(row[3] for row in store._conn.execute('EXPLAIN QUERY PLAN ' + sql, params))


def test_newest_events_are_read_through_an_index_instead_of_sorting_the_table(tmp_path):
    store = Store(tmp_path)
    try:
        for index in range(30):
            store.add_event('qq_sent' if index % 2 else 'planner_timing', 'fixture', index=index)
        statements = []
        store._conn.set_trace_callback(statements.append)
        newest = store.list_events(limit=5)
        store._conn.set_trace_callback(None)
        assert [item['details']['index'] for item in newest] == [29, 28, 27, 26, 25]
        query = next(sql for sql in statements if sql.startswith('SELECT data FROM events ORDER BY'))
        found = plan(store, query)
        assert 'events_time' in found and 'TEMP B-TREE' not in found, found
        # Lookups of one kind keep their own index.
        by_kind = plan(store, "SELECT COUNT(*) FROM events WHERE kind=? AND created_at>=? AND created_at<?",
                       ('qq_sent', '2026-01-01', '2027-01-01'))
        latest = plan(store, 'SELECT data FROM events WHERE kind=? ORDER BY created_at DESC,rowid DESC LIMIT 1', ('qq_sent',))
        assert 'events_kind_time' in by_kind and 'events_kind_time' in latest and 'TEMP B-TREE' not in latest
    finally:
        store.close()


def test_a_backup_from_before_the_event_time_index_still_restores_and_gets_it(tmp_path):
    import sqlite3
    store = Store(tmp_path / 'live')
    try:
        store.add_event('qq_sent', 'fixture')
        backup = store.backup(tmp_path / 'old.db')
        with sqlite3.connect(backup) as connection:
            connection.execute('DROP INDEX events_time')
        assert store.restore_from(backup, validate_only=True)['valid']
        store.restore_from(backup)
        assert store._conn.execute("SELECT 1 FROM sqlite_master WHERE name='events_time'").fetchone()
    finally:
        store.close()


def sse(text=None, finish=None):
    delta = {'content': text} if text is not None else {}
    return ('data: ' + json.dumps({'choices': [{'index': 0, 'delta': delta, 'finish_reason': finish}]}) + '\n\n').encode()


class Burst(httpx.AsyncByteStream):
    """A relay that held the reply back and then delivered it all at once."""

    def __init__(self, parts):
        self.parts = parts

    async def __aiter__(self):
        for part in self.parts:
            yield part

    async def aclose(self):
        pass


async def stream(parts, check):
    def handler(request):
        return httpx.Response(200, headers={'content-type': 'text/event-stream'}, stream=Burst(parts))
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        response, _ = await receive_chat(client, 'https://fixture.invalid/v1', {}, {}, check=check)
        return response.json()['choices'][0]['message']['content']


def test_a_burst_of_stream_chunks_checks_authority_once_not_once_per_chunk():
    calls = []
    parts = [sse('字') for _ in range(200)] + [sse(finish='stop'), b'data: [DONE]\n\n']
    assert asyncio.run(stream(parts, lambda: calls.append(1))) == '字' * 200
    # Once when the stream opens and once before the result is used, instead of 200+.
    assert len(calls) == 2


def test_a_slow_check_is_spaced_out_by_its_own_duration(monkeypatch):
    clock = [0.0]
    monkeypatch.setattr(model_stream.time, 'monotonic', lambda: clock[0])
    calls = []

    def check():
        calls.append(clock[0])
        clock[0] += 0.8          # one check of a long chat's sources

    class Ticking(Burst):
        async def __aiter__(self):
            for part in self.parts:
                clock[0] += 0.5  # a chunk every half second
                yield part

    def handler(request):
        parts = [sse('a') for _ in range(20)] + [sse(finish='stop'), b'data: [DONE]\n\n']
        return httpx.Response(200, headers={'content-type': 'text/event-stream'}, stream=Ticking(parts))

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            await receive_chat(client, 'https://fixture.invalid/v1', {}, {}, check=check, first_token_seconds=1000)
    asyncio.run(scenario())
    gaps = [later - earlier for earlier, later in zip(calls, calls[1:-1])]
    assert gaps and all(gap >= 0.8 + 3 * 0.8 - 1e-9 for gap in gaps), calls


def test_a_withdrawal_during_a_burst_still_stops_the_result():
    withdrawn = []
    parts = [sse('字') for _ in range(50)] + [sse(finish='stop'), b'data: [DONE]\n\n']

    def check():
        if withdrawn:
            raise RuntimeError('source withdrawn')
        withdrawn.append(True)   # withdrawn right after the stream opened

    with pytest.raises(RuntimeError, match='source withdrawn'):
        asyncio.run(stream(parts, check))


class Settings:
    def __init__(self):
        self.config = deepcopy(DEFAULTS)
        self.config['onebot'].update(enabled=True, chat_policy='blacklist')
        self.config['output'].update(mode='model', interval_ms=0)
        self.config['stickers']['enabled'] = False

    def get(self):
        return deepcopy(self.config)


@pytest.fixture
def anyio_backend():
    return 'asyncio'


@pytest.mark.anyio
async def test_the_delivery_receipt_is_built_off_the_event_loop(tmp_path, monkeypatch):
    store, config = Store(tmp_path), Settings()
    scope = 'qq:group:202'
    store.upsert_scope({'id': scope, 'plan_enabled': False, 'auto_memory': False, 'merge_window_ms': 0})

    class Model:
        async def complete(self, *args, **kwargs):
            return {'content': 'Hi.', 'message': {'role': 'assistant', 'content': 'Hi.'}, 'tool_calls': [], 'usage': {}}

    chat = ChatService(store, config, Model())
    bridge = OneBotBridge(store, config, chat)
    threads, original = [], DeliveryLedger.create

    def create(self, *args):
        threads.append(threading.current_thread() is threading.main_thread())
        return original(self, *args)
    monkeypatch.setattr(DeliveryLedger, 'create', create)

    class Socket:
        def __init__(self):
            self.sent = []

        async def send_json(self, action):
            self.sent.append(action)
            bridge._pending[action['echo']][1].set_result({'status': 'ok', 'retcode': 0, 'data': {'message_id': len(self.sent)}})
    bridge._socket = Socket()
    try:
        await asyncio.wait_for(bridge._flow.handle(scope, '303', 'Hello', [], {'at_bot': True}, direct=True), 5)
        assert threads == [False]
        receipt = DeliveryLedger(store).list(scope)[0]
        assert receipt['message_fingerprints'] and [part['status'] for part in receipt['parts']] == ['confirmed']
    finally:
        await bridge.stop()
        await chat.close()
        store.close()
