"""Stage timings preserve reply/error/delivery semantics; no external services."""
import asyncio
from copy import deepcopy
import json
import time

import pytest

from mikubot.chat import ChatService
from mikubot.db import Store
from mikubot.onebot import OneBotBridge
from mikubot.settings import DEFAULTS
from mikubot.timing import TurnTiming, add_duration, current_trace_id, measure


@pytest.fixture
def anyio_backend():
    return 'asyncio'


class Config:
    def __init__(self):
        self.value = deepcopy(DEFAULTS)
        self.value['onebot'].update(enabled=True, chat_policy='blacklist')
        self.value['model'].update(model='fixture', base_url='https://fixture.invalid/v1')
        self.value['stickers']['enabled'] = False

    def get(self):
        return deepcopy(self.value)


class Model:
    async def complete(self, messages, **kwargs):
        assert kwargs['before_send']()
        add_duration('model_queue_ms', .002)
        return {'content': '我们聊聊音乐吧。', 'tool_calls': [], 'usage': {},
                'message': {'role': 'assistant', 'content': '我们聊聊音乐吧。'}}


@pytest.mark.anyio
async def test_timing_context_does_not_leak_into_background_or_next_request():
    first = TurnTiming()
    async def background():
        assert current_trace_id() is None
        add_duration('background', 10)
    with first.activate():
        assert current_trace_id() == first.id
        add_duration('foreground', .01)
        add_duration('invalid', float('inf'))
        await asyncio.create_task(background())
        with measure('failed_step'):
            await asyncio.sleep(0)
    assert current_trace_id() is None
    add_duration('after', 10)
    assert first.snapshot()['foreground'] == 10
    assert 'failed_step' in first.snapshot()
    assert not {'background', 'invalid', 'after'} & first.durations.keys()
    second = TurnTiming()
    with pytest.raises(ValueError), second.activate(), measure('error'):
        raise ValueError('fixture')
    assert current_trace_id() is None
    assert second.id != first.id and 'error' in second.snapshot()


@pytest.mark.anyio
async def test_chat_records_scope_queue_recall_and_model_without_message_content(tmp_path):
    store, config = Store(tmp_path), Config()
    scope = 'qq:private:303'
    store.upsert_scope({'id': scope, 'auto_memory': False})
    chat = ChatService(store, config, Model())
    lock = chat._locks[scope]
    await lock.acquire()
    task = asyncio.create_task(chat.respond(scope, '303', '聊聊音乐'))
    try:
        await asyncio.sleep(.02)
        assert not task.done()
        lock.release()
        result = await task
        timings = result['timings']
        assert result['reply'] == '我们聊聊音乐吧。'
        assert timings['scope_wait_ms'] >= 10
        assert {'memory_recall_ms', 'context_ms', 'reply_model_ms', 'model_queue_ms'} <= timings.keys()
        assert timings['model_queue_ms'] == 2
        event = next(item for item in store.list_events() if item['kind'] == 'chat_timing')
        assert event['details']['status'] == 'completed'
        assert event['details']['assistant_message_id'] == result['message_id']
        assert event['details']['timings']['trace_id'] == timings['trace_id']
        assert '聊聊音乐' not in json.dumps(event, ensure_ascii=False)
        assert current_trace_id() is None
    finally:
        if lock.locked():
            lock.release()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await chat.close()
        store.close()


@pytest.mark.anyio
@pytest.mark.parametrize('cancel', [False, True])
async def test_chat_failure_and_cancel_are_timed_without_concealing_error(tmp_path, cancel):
    store, config = Store(tmp_path), Config()
    chat = ChatService(store, config, Model())
    entered = asyncio.Event()
    async def respond(*args):
        entered.set()
        if cancel:
            await asyncio.Event().wait()
        raise ValueError('fixture error')
    chat._respond = respond
    task = asyncio.create_task(chat.respond('qq:private:303', '303', 'hello'))
    try:
        await entered.wait()
        if cancel:
            task.cancel()
        with pytest.raises(asyncio.CancelledError if cancel else ValueError):
            await task
        event = next(item for item in store.list_events() if item['kind'] == 'chat_timing')
        assert event['details']['status'] == ('cancelled' if cancel else 'failed')
        assert event['details']['assistant_message_id'] is None
        assert 'timings' in event['details'] and current_trace_id() is None
    finally:
        await chat.close()
        store.close()


@pytest.mark.anyio
async def test_failed_timing_write_cannot_discard_a_saved_response(tmp_path, monkeypatch):
    store, config = Store(tmp_path), Config()
    scope = 'qq:private:303'
    store.upsert_scope({'id': scope, 'auto_memory': False})
    chat = ChatService(store, config, Model())
    add_event = store.add_event
    def fail_timing(kind, *args, **kwargs):
        if kind == 'chat_timing':
            raise OSError('fixture log failure')
        return add_event(kind, *args, **kwargs)
    monkeypatch.setattr(store, 'add_event', fail_timing)
    try:
        result = await chat.respond(scope, '303', '聊聊音乐')
        assert store.get_message(result['message_id'])['content'] == result['reply']
        assert result['timings']['trace_id'] and not chat._active
    finally:
        await chat.close()
        store.close()


@pytest.mark.anyio
async def test_cancelled_scope_wait_still_records_wait_and_does_not_unlock_another_turn(tmp_path):
    store, config = Store(tmp_path), Config()
    chat = ChatService(store, config, Model())
    scope = 'qq:private:303'
    lock = chat._locks[scope]
    await lock.acquire()
    task = asyncio.create_task(chat.respond(scope, '303', 'hello'))
    try:
        await asyncio.sleep(.02)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert lock.locked() and not chat._active
        event = next(item for item in store.list_events() if item['kind'] == 'chat_timing')
        assert event['details']['status'] == 'cancelled'
        assert event['details']['timings']['scope_wait_ms'] >= 10
    finally:
        lock.release()
        await chat.close()
        store.close()


@pytest.mark.anyio
async def test_failed_qq_image_read_is_timed_and_never_calls_the_model(tmp_path, monkeypatch):
    from mikubot import stickers
    store, config = Store(tmp_path), Config()
    config.value['model']['vision'] = True
    chat = ChatService(store, config, Model())
    def download(*args, **kwargs):
        time.sleep(.02)
        raise TimeoutError('fixture image deadline')
    monkeypatch.setattr(stickers, 'download_image', download)
    async def model(*args, **kwargs):
        pytest.fail('the inaccessible image must not be sent upstream')
    chat.provider.complete = model
    try:
        with pytest.raises(ValueError, match='图片未能读取'):
            await chat.respond('qq:private:303', '303', '看看图片', images=['https://fixture.invalid/image.png'])
        event = next(item for item in store.list_events() if item['kind'] == 'chat_timing')
        assert event['details']['status'] == 'failed'
        assert event['details']['timings']['image_prepare_ms'] >= 10
        assert 'reply_model_ms' not in event['details']['timings']
    finally:
        await chat.close()
        store.close()


class Socket:
    def __init__(self):
        self.incoming = asyncio.Queue()
        self.sent = []
        self.dispatched = asyncio.Event()

    async def send_json(self, value):
        self.sent.append(value)
        self.dispatched.set()

    async def receive_json(self):
        return await self.incoming.get()

    async def close(self, *args, **kwargs):
        pass


@pytest.mark.anyio
@pytest.mark.parametrize('outcome', ['success', 'rejected', 'timeout', 'cancelled', 'cancelled_log_failure'])
async def test_real_echo_reader_times_ack_and_never_replays_unknown_delivery(tmp_path, outcome, monkeypatch):
    store, config = Store(tmp_path), Config()
    fail_log = outcome == 'cancelled_log_failure'
    if fail_log:
        outcome = 'cancelled'
        original = store.add_event
        def add_event(kind, *args, **kwargs):
            if kind == 'qq_delivery':
                raise OSError('fixture timing write failure')
            return original(kind, *args, **kwargs)
        monkeypatch.setattr(store, 'add_event', add_event)
    config.value['onebot']['action_timeout_seconds'] = .03 if outcome == 'timeout' else 1
    scope = 'qq:private:303'
    store.upsert_scope({'id': scope})
    chat = ChatService(store, config, Model())
    bridge, socket = OneBotBridge(store, config, chat), Socket()
    reader = asyncio.create_task(bridge._read_connection(socket, 'reverse'))
    await asyncio.sleep(0)
    send = asyncio.create_task(bridge._send_part(scope, 'hello', metadata={
        'assistant_message_id': 'fixture-assistant', 'output_part': {'index': 1, 'total': 2}}))
    try:
        await asyncio.wait_for(socket.dispatched.wait(), 1)
        await asyncio.sleep(.01)
        if outcome in ('success', 'rejected'):
            await socket.incoming.put({'echo': socket.sent[0]['echo'],
                'status': 'ok' if outcome == 'success' else 'failed',
                'retcode': 0 if outcome == 'success' else 100, 'data': {'message_id': 321}})
        elif outcome == 'cancelled':
            send.cancel()
        if outcome == 'cancelled':
            with pytest.raises(asyncio.CancelledError):
                await send
        else:
            assert await asyncio.wait_for(send, 1) is (outcome == 'success')
        assert len(socket.sent) == 1 and not bridge._pending
        if fail_log:
            return
        kind = 'qq_sent' if outcome == 'success' else 'qq_delivery'
        event = next(item for item in store.list_events() if item['kind'] == kind)
        details = event['details']
        assert details['assistant_message_id'] == 'fixture-assistant'
        assert details['output_part'] == {'index': 1, 'total': 2}
        timing = details['send_timing']
        assert timing['ack_wait_ms'] >= 5
        assert all(value >= 0 for value in timing.values())
        assert ('ack_resume_ms' in timing) is (outcome in ('success', 'rejected'))
        if outcome == 'cancelled':
            assert details['delivery_status'] == 'unknown'
    finally:
        send.cancel()
        reader.cancel()
        await asyncio.gather(send, reader, return_exceptions=True)
        await bridge.stop()
        await chat.close()
        store.close()
