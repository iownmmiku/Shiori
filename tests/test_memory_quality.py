"""Extraction reads the surrounding chat and returns standalone, titled,
weighted memories with a lifetime; rewordings of one fact are merged."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
from types import SimpleNamespace

import pytest

from shiori.chat import ChatService
from shiori.db import Store
from shiori.memory import lexical_recall
from shiori.memory_quality import memory_hints, similarity, valid_until
from shiori.settings import DEFAULTS

GROUP = 'qq:group:900'


@pytest.fixture
def anyio_backend():
    return 'asyncio'


@pytest.fixture
async def runtime(tmp_path):
    cfg = deepcopy(DEFAULTS)
    cfg['model'].update(base_url='https://fixture.invalid/v1', model='fixture')
    cfg['memory'].update(organization_enabled=False, daily_extractions=60)
    cfg['onebot'].update(enabled=True, chat_policy='blacklist')
    cfg['stickers']['enabled'] = False

    class Model:
        def __init__(self):
            self.answer = lambda records: []
            self.calls = []

        async def complete(self, messages, **kwargs):
            assert kwargs['purpose'] == 'memory_extract' and kwargs['before_send']()
            self.calls.append(messages)
            return {'content': json.dumps(self.answer(json.loads(messages[-1]['content'])), ensure_ascii=False), 'usage': {}}

    store, model = Store(tmp_path / 'quality'), Model()
    store.upsert_scope({'id': GROUP})
    chat = ChatService(store, SimpleNamespace(get=lambda: deepcopy(cfg)), model)
    try:
        yield SimpleNamespace(cfg=cfg, store=store, chat=chat, model=model)
    finally:
        await chat.close()
        store.close()


def say(store, user, text, name, role='user'):
    return store.add_message(GROUP, user, role, text, sender_name=name)


def candidate(records, **changes):
    return {'kind': 'preference', 'property': '', 'content': '阿明想念家乡的擀面皮',
            'source_ids': [records[0]['id']], 'confidence': .95, **changes}


@pytest.mark.anyio
async def test_extraction_sees_nearby_chat_but_only_cites_the_member(runtime):
    e = runtime
    say(e.store, '777', '你们周末去哪玩', '小周')
    say(e.store, 'bot', '我打算去看流星雨！', '', role='assistant')
    own = say(e.store, '555', '我也想去，顺便吃家里那边的擀面皮', '阿明')
    other_chat = e.store.add_message('qq:group:901', '777', 'user', '别的群的话', sender_name='路人')
    await e.chat._extract(GROUP, '555')
    messages = e.model.calls[0]
    assert len(messages) == 3
    context = json.loads(messages[1]['content'])
    assert context['member_name'] == '阿明'
    lines = context['conversation']
    assert [line['from'] for line in lines] == ['other', 'you', 'member']
    assert lines[0]['speaker'] == '小周' and lines[1]['speaker'] == '林星遥'
    # Only the member's own line carries an id that may be cited.
    assert [line.get('id') for line in lines] == [None, None, own['id']]
    assert other_chat['content'] not in messages[1]['content']
    sources = json.loads(messages[-1]['content'])
    assert [item['id'] for item in sources] == [own['id']] and sources[0]['speaker'] == '阿明'
    # A nickname is member-controlled text: data, never part of the instructions.
    assert '阿明' not in messages[0]['content'] and 'durability' in messages[0]['content']


@pytest.mark.anyio
async def test_far_away_or_blocked_context_is_left_out(runtime):
    e = runtime
    old = say(e.store, '777', '三小时前的闲聊', '小周')
    with e.store._lock, e.store._conn:
        record = e.store.get_message(old['id'])
        record['created_at'] = (datetime.now(timezone.utc) - timedelta(hours=3)).isoformat().replace('+00:00', 'Z')
        record['received_at'] = record['metadata']['received_at'] = record['created_at']
        e.store._conn.execute('UPDATE messages SET created_at=?,data=? WHERE id=?',
                              (record['created_at'], json.dumps(record, ensure_ascii=False), old['id']))
    say(e.store, '555', '我最近在学吉他', '阿明')
    await e.chat._extract(GROUP, '555')
    # Nothing but the member's own line remains, so no context message is sent.
    assert len(e.model.calls[0]) == 2


@pytest.mark.anyio
async def test_hints_title_importance_lifetime_and_name_are_saved(runtime):
    e = runtime
    say(e.store, '555', '我想吃家里那边的擀面皮了', '阿明')
    say(e.store, '555', '我是学天文的，以后也想做这个', '阿明')
    e.model.answer = lambda records: [
        candidate(records, title='想吃家乡擀面皮', importance=.2, durability='days', topics=['美食', '家乡', 7]),
        candidate(records, kind='fact', content='阿明是天文专业的学生，以后想从事天文工作', source_ids=[records[1]['id']],
                  title='天文专业', importance=1.0, durability='lasting', topics=['学业', '天文'])]
    assert not (await e.chat._extract(GROUP, '555'))['skipped']
    saved = {item['title']: item for item in e.store.list_memories(GROUP)}
    wish, study = saved['想吃家乡擀面皮'], saved['天文专业']
    assert wish['subject'] == '555' and wish['subject_name'] == '阿明'
    assert wish['importance'] == .2 and wish['durability'] == 'days' and wish['topics'] == ['美食', '家乡']
    end = datetime.fromisoformat(wish['valid_to'].replace('Z', '+00:00'))
    assert timedelta(days=4) < end - datetime.now(timezone.utc) < timedelta(days=5)
    assert study['importance'] == .9 and study['valid_to'] is None and study['status'] == 'active'


@pytest.mark.anyio
async def test_a_passing_remark_from_long_ago_is_not_saved(runtime):
    e = runtime
    source = say(e.store, '555', '今晚想吃火锅', '阿明')
    with e.store._lock, e.store._conn:
        record = e.store.get_message(source['id'])
        record['metadata']['sent_at'] = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat().replace('+00:00', 'Z')
        e.store._conn.execute('UPDATE messages SET data=? WHERE id=?', (json.dumps(record, ensure_ascii=False), source['id']))
    e.model.answer = lambda records: [candidate(records, content='阿明想吃火锅', durability='days', importance=.2)]
    result = await e.chat._extract(GROUP, '555')
    assert result['outcome']['expired_on_arrival'] == 1 and e.store.list_memories(GROUP) == []
    assert e.chat.memory_lifecycle.pending(GROUP, '555') == []


@pytest.mark.anyio
async def test_a_reworded_repeat_merges_into_the_existing_memory(runtime):
    e = runtime
    say(e.store, '555', '我喜欢吃蓝莓', '阿明')
    e.model.answer = lambda records: [candidate(records, content='阿明喜欢吃蓝莓，经常买来当零食')]
    await e.chat._extract(GROUP, '555')
    say(e.store, '555', '蓝莓真的好吃', '阿明')
    e.model.answer = lambda records: [candidate(records, content='阿明喜欢吃蓝莓，常常买来当零食')]
    result = await e.chat._extract(GROUP, '555')
    assert result['outcome']['deduplicated'] == 1 and result['outcome']['inserted'] == 0
    assert len(e.store.list_memories(GROUP)) == 1
    # A different fact about the same food is still its own memory.
    say(e.store, '555', '不过我对芒果过敏', '阿明')
    e.model.answer = lambda records: [candidate(records, kind='fact', content='阿明对芒果过敏')]
    await e.chat._extract(GROUP, '555')
    assert len(e.store.list_memories(GROUP)) == 2


def test_store_validates_titles_topics_and_lifetimes(tmp_path):
    store = Store(tmp_path / 'fields')
    try:
        saved = store.create_memory({'content': '阿明喜欢天文', 'title': ' 天文爱好 ', 'topics': ['天文', ' '], 'durability': 'lasting'})
        assert saved['title'] == '天文爱好' and saved['topics'] == ['天文']
        assert 'title' not in store.update_memory(saved['id'], {'title': ''})
        for bad in ({'title': 'x' * 41}, {'title': 3}, {'topics': 'x'}, {'topics': ['x' * 21]}, {'durability': 'forever'}):
            with pytest.raises(ValueError):
                store.create_memory({'content': '无效字段', **bad})
    finally:
        store.close()


def test_hints_are_lenient_and_bounded():
    assert memory_hints({}) == {}
    assert memory_hints({'title': '  ', 'importance': True, 'durability': 'forever', 'topics': 'x'}) == {}
    assert memory_hints({'title': 'x' * 41, 'importance': float('nan')}) == {}
    hints = memory_hints({'title': ' 标题 ', 'importance': 0, 'durability': 'weeks', 'topics': ['a', 'a', 'b', 'c', 'd', 'e']})
    assert hints == {'title': '标题', 'importance': .1, 'durability': 'weeks', 'topics': ['a', 'b', 'c', 'd']}
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    assert valid_until('lasting', .9, start) is None and valid_until(None, .5, start) is None
    assert valid_until('weeks', .5, start) == '2026-01-31T00:00:00.000000Z'
    assert valid_until('days', .9, '2026-01-01T00:00:00Z').startswith('2026-01-10')
    assert similarity('阿明喜欢吃蓝莓', '阿明喜欢吃蓝莓！') == 1 and similarity('阿明喜欢蓝莓', '小周讨厌芒果') == 0
    assert similarity('', 'x') == 0


def test_recall_finds_a_member_by_nickname_and_lets_passing_remarks_fade():
    now = datetime.now(timezone.utc)
    stamp = lambda days: (now - timedelta(days=days)).isoformat().replace('+00:00', 'Z')
    base = {'kind': 'fact', 'status': 'active', 'version': 1, 'importance': .5, 'scope_id': GROUP, 'source_ids': []}
    records = [
        {**base, 'id': 'a', 'user_id': '555', 'subject': '555', 'subject_name': '阿明', 'content': '喜欢天文和摄影', 'updated_at': stamp(1)},
        {**base, 'id': 'b', 'user_id': '777', 'subject': '777', 'subject_name': '小周', 'content': '喜欢天文和摄影', 'updated_at': stamp(1)},
    ]
    results = lexical_recall(records, '阿明 喜欢什么', '999')
    assert results[0]['id'] == 'a' and results[0]['retrieval']['person_boost'] > 0
    old = [{**base, 'id': 'days', 'user_id': '555', 'durability': 'days', 'content': '想看流星雨', 'updated_at': stamp(30)},
           {**base, 'id': 'lasting', 'user_id': '555', 'durability': 'lasting', 'content': '想看流星雨', 'updated_at': stamp(30)}]
    ranked = lexical_recall(old, '流星雨', '555')
    assert [item['id'] for item in ranked] == ['lasting', 'days']
    assert ranked[0]['retrieval']['recency_boost'] > 10 * ranked[1]['retrieval']['recency_boost']
