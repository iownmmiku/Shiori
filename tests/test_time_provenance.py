"""Calendar behavior under delayed ingress, queued dispatch and old data."""
import asyncio
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json

import httpx
import pytest

from shiori.chat import ChatService
from shiori.db import Store
from shiori.provider import Provider
from shiori.settings import Settings
from shiori.timezones import (ingress_times, message_anchor, message_time_context,
                               message_time_reference, refresh_dispatch_time,
                               reference_time, utc_timestamp)


UTC = timezone.utc


@pytest.fixture
def anyio_backend():
    return 'asyncio'


def test_timestamp_normalization_rejects_unknown_zone_and_bad_platform_clock():
    received = '2026-10-23T02:00:00Z'
    assert utc_timestamp('2026-10-23T10:00:00+08:00') == '2026-10-23T02:00:00.000000Z'
    assert utc_timestamp(1792720800) is not None
    for value in ('2026-10-23T10:00:00', '2026-10-23', True, float('inf'), {}, 'bad'):
        assert utc_timestamp(value) is None
    assert ingress_times('2026-10-23T02:06:00Z', received_at=received)['sent_at'] is None
    assert ingress_times('2026-10-22T23:59:00Z', received_at=received)['sent_at'].startswith('2026-10-22')


def test_new_messages_keep_receipt_send_and_insert_clocks_separate(tmp_path):
    store = Store(tmp_path)
    try:
        message = store.add_message('web:default', 'owner', 'user', '昨天去海边了',
                                    sent_at='2026-10-21T23:59:00+08:00', received_at='2026-10-22T00:02:00+08:00')
        assert message['sent_at'] == '2026-10-21T15:59:00.000000Z'
        assert message['received_at'] == '2026-10-21T16:02:00.000000Z'
        assert message['metadata']['sent_at'] == message['sent_at']
        context = message_time_context(message, {'time': {'timezone': 'Asia/Tokyo'}})
        assert context['anchor'] == '2026-10-22T00:59:00+09:00'
        assert context['anchor_source'] == 'platform'
        assert message_anchor(message).day == 21
        unknown = store.add_message('web:default', 'owner', 'user', '你好')
        assert unknown['sent_at'] is None
        assert message_time_context(unknown)['anchor_source'] == 'received'
        legacy = {'created_at': '2020-01-01T00:00:00Z', 'metadata': {}}
        assert message_time_context(legacy)['anchor_source'] == 'recorded'
        assert message_time_context(legacy, {'time': {'awareness_enabled': False}}) is None
    finally:
        store.close()


@pytest.mark.anyio
async def test_request_calendar_is_isolated_and_propagates_into_worker_threads():
    first, second = datetime(2026, 10, 21, tzinfo=UTC), datetime(2026, 10, 22, tzinfo=UTC)
    async def read(stamp):
        with message_time_reference(stamp):
            await asyncio.sleep(0)
            return reference_time(), await asyncio.to_thread(reference_time)
    assert await asyncio.gather(read(first), read(second)) == [(first, first), (second, second)]
    assert reference_time() not in (first, second)


class Model:
    def __init__(self):
        self.prompts = []

    async def complete(self, messages, **kwargs):
        self.prompts.append(deepcopy(messages))
        return {'content': '收到。', 'message': {'role': 'assistant', 'content': '收到。'}, 'tool_calls': [], 'usage': {}}


@pytest.mark.anyio
async def test_history_and_merged_current_messages_show_original_times_without_changing_raw_data(tmp_path):
    settings, store, model = Settings(tmp_path), Store(tmp_path), Model()
    settings.update({'model': {'base_url': 'https://fixture.invalid/v1', 'model': 'fixture'}})
    store.upsert_scope({'id': 'web:default', 'auto_memory': False})
    chat = ChatService(store, settings, model)
    try:
        past = store.add_message('web:default', 'owner', 'user', '这是昨天的原文', sent_at='2026-10-21T15:00:00Z')
        first = await chat.observe('web:default', 'owner', '第一句', metadata={'sent_at': '2026-10-22T15:59:59Z'})
        second = await chat.observe('web:default', 'owner', '第二句', metadata={'sent_at': '2026-10-22T16:00:02Z'})
        await chat.respond('web:default', 'owner', '第一句\n第二句', metadata={'existing_message_id': second['id'], 'merged_message_ids': [first['id'], second['id']]})
        prompt = model.prompts[-1]
        history = next(message['content'] for message in prompt if '这是昨天的原文' in str(message['content']))
        assert '2026-10-21T23:00:00+08:00' in history
        material = next(message['content'] for message in prompt if str(message['content']).startswith('【当前原消息时间】'))
        assert '2026-10-22T23:59:59+08:00' in material
        assert '2026-10-23T00:00:02+08:00' in material
        assert store.get_message(past['id'])['content'] == '这是昨天的原文'
    finally:
        await chat.close()
        store.close()


@pytest.mark.anyio
async def test_delayed_history_recall_uses_platform_day_and_requested_day_anchor(tmp_path):
    settings, store, model = Settings(tmp_path), Store(tmp_path), Model()
    settings.update({'time': {'timezone': 'Asia/Shanghai'}})
    chat = ChatService(store, settings, model)
    try:
        matching = store.add_message('web:default', 'owner', 'user', '昨天晚上一起看了电影', sent_at='2026-10-21T12:00:00Z')
        store.add_message('web:default', 'owner', 'user', '其他日期的消息', sent_at='2026-10-20T12:00:00Z')
        # The messages were only inserted today, and the request arrives later.
        with message_time_reference(datetime(2026, 10, 22, 0, 30, tzinfo=UTC)):
            history = chat._time_history('web:default', 'owner', '昨天聊了什么')
        assert [item['id'] for item in history] == [matching['id']]
    finally:
        await chat.close()
        store.close()


@pytest.mark.anyio
async def test_relative_reminder_is_anchored_before_generation_delay(tmp_path):
    settings, store, model = Settings(tmp_path), Store(tmp_path), Model()
    store.upsert_scope({'id': 'web:default', 'auto_memory': False})
    chat = ChatService(store, settings, model)
    try:
        sent = datetime.now(UTC) - timedelta(minutes=1)
        result = await chat.respond('web:default', 'owner', '3分钟后提醒我喝水', metadata={'sent_at': sent.isoformat()})
        due = datetime.fromisoformat(result['actions'][0]['result']['due_at'])
        assert abs((due - sent - timedelta(minutes=3)).total_seconds()) < .01
        late = await chat.respond('web:default', 'owner', '1分钟后提醒我休息', metadata={'sent_at': (sent - timedelta(minutes=5)).isoformat()})
        assert '已经过去' in late['reply']
        assert len(store.list_tasks()) == 1
    finally:
        await chat.close()
        store.close()


def test_candidate_event_dates_are_source_ranges_and_unknown_stays_unknown(tmp_path):
    settings, store, model = Settings(tmp_path), Store(tmp_path), Model()
    chat = ChatService(store, settings, model)
    try:
        base = {'sent_at': '2026-10-22T00:30:00+08:00', 'received_at': '2026-10-23T00:30:00+08:00'}
        known = chat._candidate_times([{**base, 'content': '昨晚一起看了电影'}], 'event')
        assert known['event_time_start'] == '2026-10-21T10:00:00.000000Z'
        assert known['event_time_end'] == '2026-10-21T22:00:00.000000Z'
        assert known['event_time_known'] is True
        assert known['reported_at'] == '2026-10-21T16:30:00.000000Z'
        for kind, content in [('event', '一起看过电影'), ('event', '最近去了海边'), ('fact', '昨天说我住在上海')]:
            unknown = chat._candidate_times([{**base, 'content': content}], kind)
            assert unknown['event_time_known'] is False
            assert 'occurred_at' not in unknown and 'event_time_start' not in unknown
    finally:
        asyncio.run(chat.close())
        store.close()


@pytest.mark.anyio
async def test_extraction_drops_model_guessed_date_and_persists_only_source_supported_dates(tmp_path):
    class Extractor:
        async def complete(self, messages, **kwargs):
            records = json.loads(messages[-1]['content'])
            source_ids = {record['content']: record['id'] for record in records}
            return {'content': json.dumps([
                {'kind': 'event', 'content': '一起去过海边', 'confidence': .9,
                 'source_ids': [source_ids['一起去过海边']], 'occurred_at': '2020-01-01T00:00:00Z'},
                {'kind': 'event', 'content': '昨天一起看了电影', 'confidence': .9,
                 'source_ids': [source_ids['昨天一起看了电影']], 'event_at': '2020-01-01T00:00:00Z'}])}
    settings, store = Settings(tmp_path), Store(tmp_path)
    chat = ChatService(store, settings, Extractor())
    try:
        store.add_message('web:default', 'owner', 'user', '一起去过海边', sent_at='2026-10-22T10:00:00Z')
        store.add_message('web:default', 'owner', 'user', '昨天一起看了电影', sent_at='2026-10-22T10:00:00Z')
        result = await chat._extract('web:default', 'owner', automatic=True)
        assert result['skipped'] is False
        records = {item['content']: item for item in store.list_memories('web:default')}
        unknown, known = records['一起去过海边'], records['昨天一起看了电影']
        assert unknown['event_time_known'] is False
        assert 'occurred_at' not in unknown and 'event_time_start' not in unknown
        assert 'event_at' not in known
        assert known['event_time_start'] == '2026-10-20T16:00:00.000000Z'
        assert known['event_time_end'] == '2026-10-21T16:00:00.000000Z'
        assert known['reported_at'] == '2026-10-22T10:00:00.000000Z'
    finally:
        await chat.close()
        store.close()


def test_dispatch_refreshes_only_owned_clock_preserving_original_message_time():
    original = [
        {'role': 'system', 'content': '【现实时间】旧时钟\n角色设定\n【现实时间】人格中的原文\n', '_shiori_clock': 'system'},
        {'role': 'user', 'content': json.dumps({'current_message_id': 'm', 'current_user_id': 'u', 'current_time': {'now': 'old'}, 'current_message_time': {'sent_at': 'original'}}, ensure_ascii=False), '_shiori_clock': 'planner'}]
    untouched = deepcopy(original)
    refreshed = refresh_dispatch_time(original, {'time': {'timezone': 'UTC'}}, purpose='planner')
    assert '旧时钟' not in refreshed[0]['content']
    assert '【现实时间】人格中的原文' in refreshed[0]['content']
    assert json.loads(refreshed[1]['content'])['current_time']['timezone'] == 'UTC'
    assert json.loads(refreshed[1]['content'])['current_message_time']['sent_at'] == 'original'
    assert original == untouched
    disabled = refresh_dispatch_time(refreshed, {'time': {'awareness_enabled': False}}, purpose='planner')
    assert disabled[0]['content'].count('【现实时间】') == 1
    assert 'current_time' not in json.loads(disabled[1]['content'])
    custom = [{'role': 'system', 'content': '【现实时间】用户提供的原文，不是应用时钟\n'}]
    assert refresh_dispatch_time(custom) == custom
    transmitted = refresh_dispatch_time(original, for_transport=True)
    assert all('_shiori_clock' not in item for item in transmitted)


@pytest.mark.anyio
async def test_provider_refreshes_clock_after_queue_wait_and_each_http_retry(tmp_path, monkeypatch):
    from shiori import timezones
    from contextlib import asynccontextmanager
    fixed = [datetime(2026, 10, 22, 23, 59, tzinfo=UTC)]
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return fixed[0].astimezone(tz) if tz else fixed[0].replace(tzinfo=None)
    monkeypatch.setattr(timezones, 'datetime', Clock)
    settings, store = Settings(tmp_path), Store(tmp_path)
    settings.update({'model': {'base_url': 'https://fixture.invalid/v1', 'model': 'fixture'}, 'time': {'timezone': 'UTC'}})
    provider, seen = Provider(settings, store), []
    @asynccontextmanager
    async def slot(*args):
        fixed[0] += timedelta(minutes=2)
        yield
    monkeypatch.setattr(provider, '_provider_slot', slot)
    def handler(request):
        seen.append(json.loads(request.content)['messages'][0]['content'])
        if len(seen) == 1:
            fixed[0] += timedelta(minutes=3)
            return httpx.Response(503)
        return httpx.Response(200, json={'choices': [{'message': {'role': 'assistant', 'content': '收到。'}, 'finish_reason': 'stop'}], 'usage': {'prompt_tokens': 20, 'completion_tokens': 4}})
    original_client = httpx.AsyncClient
    monkeypatch.setattr(httpx, 'AsyncClient', lambda *args, **kwargs: original_client(*args, transport=httpx.MockTransport(handler), **kwargs))
    try:
        await provider.complete([{'role': 'system', 'content': '【现实时间】旧时钟\n', '_shiori_clock': 'system'}, {'role': 'user', 'content': '你好'}])
        assert '2026-10-23T00:01:00+00:00' in seen[0]
        assert '2026-10-23T00:04:00+00:00' in seen[1]
    finally:
        store.close()
