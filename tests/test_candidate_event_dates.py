"""Event dates belong to a supported source clause, never the whole batch."""
import asyncio
from copy import deepcopy
import json

import pytest

from mikubot.chat import ChatService
from mikubot.db import Store
from mikubot.settings import DEFAULTS


class Config:
    def __init__(self):
        self.data = deepcopy(DEFAULTS)

    def get(self):
        return deepcopy(self.data)


class Model:
    async def complete(self, messages, **kwargs):
        sources = json.loads(messages[-1]['content'])
        return {'content': json.dumps([
            {'kind': 'event', 'content': '用户去了海边', 'confidence': .9, 'source_ids': [sources[0]['id']]},
            {'kind': 'event', 'content': '用户看了电影', 'confidence': .9, 'source_ids': [sources[0]['id']]},
        ], ensure_ascii=False)}


@pytest.fixture
def anyio_backend():
    return 'asyncio'


@pytest.fixture
def chat(tmp_path):
    store = Store(tmp_path)
    service = ChatService(store, Config(), Model())
    yield service
    asyncio.run(service.close())
    store.close()


def source(text, sent='2026-10-23T10:00:00+08:00'):
    return {'content': text, 'sent_at': sent}


@pytest.mark.parametrize('text,candidate,start', [
    ('今天去海边，昨天看电影', '用户去了海边', '2026-10-22T16:00:00.000000Z'),
    ('今天去海边，昨天看电影', '用户看了电影', '2026-10-21T16:00:00.000000Z'),
    ('昨天看电影，今天去海边', '用户去了海边', '2026-10-22T16:00:00.000000Z'),
    ('今天去海边昨天看电影', '用户看了电影', '2026-10-21T16:00:00.000000Z'),
    ('2026-10-21去海边，2026-10-22看电影', '去了海边', '2026-10-20T16:00:00.000000Z'),
    ('今天和朋友去海边，昨天和朋友看电影', '和朋友看电影', '2026-10-21T16:00:00.000000Z'),
    ('今天去海边，还吃了烤鱼，昨天看电影', '吃了烤鱼', '2026-10-22T16:00:00.000000Z'),
    ('海边是今天去的，电影是昨天看的', '去了海边', '2026-10-22T16:00:00.000000Z'),
    # A model's generated relative date cannot overrule the actual source.
    ('今天去海边，昨天看电影', '昨天去了海边', '2026-10-22T16:00:00.000000Z'),
])
def test_candidate_uses_its_matching_event_clause(chat, text, candidate, start):
    result = chat._candidate_times([source(text)], 'event', candidate)
    assert result['event_time_known'] is True
    assert result['event_time_start'] == start


@pytest.mark.parametrize('text,candidate', [
    ('今天去海边，昨天看电影', '去了海边也看了电影'),
    ('今天去海边，昨天也去海边', '去了海边'),
    ('去过海边。今天看电影', '去过海边'),
    ('今天去海边。见到了老朋友', '见到了老朋友'),
    ('海边今天去的电影昨天看的', '去了海边'),
    ('今天去海边，昨天看电影', '发生了一些事情'),
    ('2026-99-99去海边，昨天看电影', '去了海边'),
])
def test_ambiguous_undated_or_unmatched_events_remain_unknown(chat, text, candidate):
    result = chat._candidate_times([source(text)], 'event', candidate)
    assert result['event_time_known'] is False
    assert 'event_time_start' not in result


def test_multiple_source_ids_match_the_event_instead_of_the_first_report(chat):
    result = chat._candidate_times([source('昨天看电影'), source('今天去海边')], 'event', '用户去了海边')
    assert result['event_time_start'] == '2026-10-22T16:00:00.000000Z'
    same_day = chat._candidate_times([source('今天去海边'), source('昨天去海边', '2026-10-24T10:00:00+08:00')], 'event', '去了海边')
    assert same_day['event_time_start'] == '2026-10-22T16:00:00.000000Z'


@pytest.mark.anyio
async def test_extraction_persists_distinct_event_days_from_one_original(tmp_path):
    store = Store(tmp_path)
    service = ChatService(store, Config(), Model())
    try:
        store.add_message('web:default', 'owner', 'user', '今天去海边，昨天看电影', sent_at='2026-10-23T10:00:00+08:00')
        result = await service._extract('web:default', 'owner', automatic=True)
        assert result['skipped'] is False
        records = {item['content']: item for item in store.list_memories('web:default')}
        assert records['用户去了海边']['event_time_start'] == '2026-10-22T16:00:00.000000Z'
        assert records['用户看了电影']['event_time_start'] == '2026-10-21T16:00:00.000000Z'
        assert all(item['reported_at'] == '2026-10-23T02:00:00.000000Z' for item in records.values())
    finally:
        await service.close()
        store.close()
