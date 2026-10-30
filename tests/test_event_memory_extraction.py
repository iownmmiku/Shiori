"""Episodes preserve a sourced event without widening ownership or time claims."""
from copy import deepcopy
import json
from types import SimpleNamespace

import pytest

from shiori.chat import ChatService
from shiori.db import Store
from shiori.memory_events import episode_content
from shiori.settings import DEFAULTS


@pytest.fixture
def anyio_backend():
    return 'asyncio'


@pytest.fixture
async def runtime(tmp_path):
    config = deepcopy(DEFAULTS)
    config['model'].update(base_url='https://fixture.invalid/v1', model='fixture')
    config['memory'].update(organization_enabled=False, daily_extractions=60)
    config['stickers']['enabled'] = False

    class Model:
        def __init__(self):
            self.answer = lambda records: []
            self.calls = []

        async def complete(self, messages, **kwargs):
            assert kwargs['purpose'] == 'memory_extract' and kwargs['before_send']()
            records = json.loads(messages[-1]['content'])
            self.calls.append({'records': records, 'prompt': messages[0]['content'], **kwargs})
            return {'content': json.dumps(self.answer(records), ensure_ascii=False), 'usage': {}}

    store, model = Store(tmp_path / 'events'), Model()
    chat = ChatService(store, SimpleNamespace(get=lambda: deepcopy(config)), model)
    try:
        yield SimpleNamespace(config=config, store=store, model=model, chat=chat)
    finally:
        await chat.close()
        store.close()


def episode(**changes):
    return {'title': '参观天文馆', 'summary': '用户参观天文馆并看了星空展。',
            'narrative': '用户在天文馆参观星空展，先看星图，再看陨石展品，最后买了纪念册。',
            'participants': [], 'location': '天文馆', 'outcome': '买了纪念册。', **changes}


def event(records, **changes):
    return {'kind': 'event', 'property': '', 'episode': episode(),
            'source_ids': [records[0]['id']], 'confidence': .9, **changes}


@pytest.mark.anyio
async def test_single_long_user_story_creates_complete_automatic_episode_without_assistant_facts(runtime):
    env = runtime
    narrative = '用户参观天文馆。' + '用户先看星图，接着看陨石展品，并把展板上的星座名称记入笔记。' * 45 + '最后买了纪念册。'
    original = env.store.add_message('web:default', 'owner', 'user', '昨天' + narrative.replace('。', '，'),
                                     sent_at='2026-10-23T10:00:00+08:00')
    env.store.add_message('web:default', 'assistant', 'assistant', 'ASSISTANT_INVENTED_AWARD')
    env.store.add_message('web:default', 'other-person', 'user', 'OTHER_PERSON_SECRET')
    env.model.answer = lambda records: [event(records, episode=episode(narrative=narrative),
        content='模型另填的一句不完整摘要', subject='invented-third-person',
        occurred_at='1999-01-01T00:00:00Z', event_time_known=False, pinned=True, shareable=True)]
    result = await env.chat._extract('web:default', 'owner')
    assert not result['skipped']
    stored = env.store.list_memories('web:default')[0]
    assert stored['content'] == episode_content(stored['episode']) and len(stored['content']) > 1000
    assert stored['episode']['narrative'] == narrative and stored['status'] == 'active'
    assert stored['subject'] == stored['user_id'] == 'owner' and stored['source_ids'] == [original['id']]
    assert not stored['pinned'] and not stored['shareable']
    assert stored['event_time_known'] and stored['event_time_start'] == '2026-10-21T16:00:00.000000Z'
    assert stored['reported_at'] == '2026-10-23T02:00:00.000000Z' and not stored.get('occurred_at')
    assert [row['id'] for row in env.model.calls[0]['records']] == [original['id']]
    assert env.model.calls[0]['max_tokens'] == 4000
    assert env.chat.memory_lifecycle.pending('web:default', 'owner') == []


@pytest.mark.anyio
@pytest.mark.parametrize('review,confidence,status', [('automatic', .8, 'pending'), ('manual', .95, 'pending')])
async def test_episode_keeps_unknown_dates_and_respects_review_mode(runtime, review, confidence, status):
    env = runtime
    env.config['memory']['review_mode'] = review
    env.store.add_message('web:default', 'owner', 'user', '我在天文馆参观星空展，先看星图，再看陨石展品，最后买了纪念册。')
    env.model.answer = lambda records: [event(records, confidence=confidence,
        event_time_known=True, event_time_start='2000-01-01T00:00:00Z', event_time_end='2000-01-02T00:00:00Z')]
    assert not (await env.chat._extract('web:default', 'owner'))['skipped']
    stored = env.store.list_memories('web:default')[0]
    assert stored['status'] == status and stored['event_time_known'] is False
    assert not stored.get('event_time_start') and not stored.get('event_time_end')
    assert stored['episode']['participants'] == []


@pytest.mark.anyio
async def test_two_atomic_facts_and_two_distinct_episodes_fit_one_batch(runtime):
    env = runtime
    source = env.store.add_message('web:default', 'owner', 'user',
        '我喜欢蓝莓，每周跑步。昨天在天文馆参观星空展，先看星图，再看陨石展品，最后买了纪念册，今天去海边看日落。',
        sent_at='2026-10-23T10:00:00+08:00')
    def answer(records):
        return [
            {'kind': 'preference', 'property': 'fruit', 'content': '用户喜欢蓝莓', 'confidence': .9, 'source_ids': [source['id']]},
            {'kind': 'fact', 'property': 'exercise', 'content': '用户每周跑步', 'confidence': .9, 'source_ids': [source['id']]},
            event(records),
            event(records, episode=episode(title='海边日落', summary='用户去海边看日落。',
                narrative='用户去海边看日落。', location='海边', outcome='')),
        ]
    env.model.answer = answer
    result = await env.chat._extract('web:default', 'owner')
    assert not result['skipped'] and result['outcome']['inserted'] == 4
    stored = env.store.list_memories('web:default')
    assert len(stored) == 4 and sum(item['kind'] == 'event' for item in stored) == 2
    events = {item['episode']['title']: item for item in stored if item['kind'] == 'event'}
    assert events['参观天文馆']['event_time_start'] == '2026-10-21T16:00:00.000000Z'
    assert events['海边日落']['event_time_start'] == '2026-10-22T16:00:00.000000Z'


@pytest.mark.anyio
@pytest.mark.parametrize('case', ['three-events', 'three-facts', 'five-total', 'non-event-episode',
    'unknown-episode-field', 'participant-identity', 'long-narrative', 'empty-narrative', 'assistant-source', 'foreign-source'])
async def test_invalid_episode_batch_writes_nothing_and_keeps_source_cursor(runtime, case):
    env = runtime
    original = env.store.add_message('web:default', 'owner', 'user', '我参观过天文馆。')
    assistant = env.store.add_message('web:default', 'assistant', 'assistant', '助手生成内容')
    foreign = env.store.add_message('web:default', 'another', 'user', '另一人的故事')
    def answer(records):
        first = event(records)
        bad = event(records)
        if case == 'three-events':
            return [first, bad, event(records)]
        if case == 'three-facts':
            return [{'kind': 'fact', 'content': str(index), 'source_ids': [original['id']], 'confidence': .9} for index in range(3)]
        if case == 'five-total':
            return [event(records) for _ in range(5)]
        if case == 'non-event-episode':
            bad['kind'] = 'fact'
        elif case == 'unknown-episode-field':
            bad['episode']['event_time_known'] = True
        elif case == 'participant-identity':
            bad['episode']['participants'] = [{'user_id': 'invented', 'name': '小王'}]
        elif case == 'long-narrative':
            bad['episode']['narrative'] = '长' * 3601
        elif case == 'empty-narrative':
            bad['episode']['narrative'] = ' '
        elif case == 'assistant-source':
            bad['source_ids'] = [assistant['id']]
        elif case == 'foreign-source':
            bad['source_ids'] = [foreign['id']]
        return [first, bad]
    env.model.answer = answer
    result = await env.chat._extract('web:default', 'owner')
    assert result['skipped'] and env.store.list_memories('web:default') == []
    assert [row['id'] for row in env.chat.memory_lifecycle.pending('web:default', 'owner')] == [original['id']]


@pytest.mark.anyio
async def test_legacy_plain_event_and_single_short_manual_message_remain_supported(runtime):
    env = runtime
    original = env.store.add_message('web:default', 'owner', 'user', '去过海边。')
    env.model.answer = lambda records: [{'kind': 'event', 'content': '用户去过海边',
        'confidence': .9, 'source_ids': [original['id']]}]
    assert not (await env.chat._extract('web:default', 'owner'))['skipped']
    saved = env.store.list_memories('web:default')[0]
    assert saved['content'] == '用户去过海边' and 'episode' not in saved
    assert saved['event_time_known'] is False
    assert (await env.chat._extract('web:default', 'owner'))['skipped']
    assert len(env.model.calls) == 1
