"""Event retrieval keeps the actual matching span and enough story context."""
from copy import deepcopy
import json
from types import SimpleNamespace

import pytest

from shiori.chat import ChatService
from shiori.db import Store
from shiori.memory import CHUNK_SIZE
from shiori.memory_events import episode_content, episode_prompt_text, normalize_episode

from shiori.settings import DEFAULTS


@pytest.fixture
def anyio_backend():
    return 'asyncio'


@pytest.fixture
async def env(tmp_path):
    config = deepcopy(DEFAULTS)
    config['model'].update(base_url='https://fixture.invalid/v1', model='fixture')
    config['time']['awareness_enabled'] = False
    config['context'].update(enabled=False, max_input_chars=40000)
    config['memory'].update(organization_enabled=False, semantic_enabled=False)
    config['stickers']['enabled'] = False

    class Model:
        def __init__(self):
            self.calls = []

        async def complete(self, messages, **options):
            assert options['before_send']()
            self.calls.append(deepcopy(messages))
            return {'content': '已根据这段经历回答。', 'tool_calls': [], 'usage': {}}

    store, model = Store(tmp_path / 'recall'), Model()
    store.upsert_scope({'id': 'web:default', 'auto_memory': False, 'allow_tools': False,
                        'use_persona_knowledge': False})
    chat = ChatService(store, SimpleNamespace(get=lambda: deepcopy(config)), model)
    try:
        yield SimpleNamespace(store=store, chat=chat, engine=chat.memory, model=model, config=config)
    finally:
        await chat.close()
        store.close()


def story(**changes):
    return normalize_episode({'title': '星图修复记录', 'summary': '用户在展馆修复星图，记录了问题与最终处理。',
        'narrative': '先核对入口展板。' + '随后逐页核对参展目录和图例。' * 110
                     + '中途发现 cobaltphase7823 校准标记与原始索引不一致，重新排列了星图。'
                     + '接着逐一核对各张展板的编号。' * 100,
        'participants': ['用户', '同行朋友'], 'location': '展馆',
        'outcome': '完成校准，保留了更正后的星图和纪念册。', **changes})


def event(store, episode=None, **fields):
    episode = episode or story()
    return store.create_memory({'kind': 'event', 'scope_id': 'web:default', 'user_id': 'owner',
        'episode': episode, 'content': episode_content(episode), 'event_time_known': False, **fields})


def references(messages):
    block = next(item['content'] for item in messages
                 if str(item.get('content', '')).startswith('以下是当前允许使用的参考数据'))
    return block.split('\n', 1)[1]


def relation(store, identifier, left, right):
    return store.create_memory({'id': identifier, 'scope_id': left['scope_id'], 'user_id': 'owner',
        'kind': 'note', 'property': 'memory_relation', 'content': '经过确认的关联', 'derived': True,
        'from_id': left['id'], 'to_id': right['id'], 'label': '同一次参观', 'relation_type': 'event_link',
        'dependencies': {item['id']: item['version'] for item in (left, right)},
        'endpoint_identities': {item['id']: [item['scope_id'], item['user_id']] for item in (left, right)}})


@pytest.mark.anyio
async def test_middle_detail_recall_retains_real_span_and_story_context_in_model_prompt(env):
    saved = event(env.store)
    assert len(saved['content']) > 2200
    result = await env.engine.recall('web:default', 'owner', 'cobaltphase7823')
    found = next(item for item in result if item['id'] == saved['id'])
    assert 'cobaltphase7823' in found['excerpt'] and found['chunk_index'] > 0
    assert found['excerpt'] == saved['content'][found['retrieval']['start']:found['retrieval']['end']]
    assert len(found['event_context']) <= 2200
    for value in (saved['episode']['title'], saved['episode']['summary'], saved['episode']['outcome'], 'cobaltphase7823'):
        assert value in found['event_context']
    assert '未展示全部经过' in found['event_context']
    reply = await env.chat.respond('web:default', 'owner', 'cobaltphase7823 最后怎么处理的？')
    assert reply['reply'] and env.model.calls
    refs = json.loads(references(env.model.calls[-1]))['memories']
    contextual = next(item for item in refs if item['id'] == saved['id'])
    for value in (saved['episode']['title'], saved['episode']['summary'], saved['episode']['outcome'], 'cobaltphase7823'):
        assert value in contextual['content']
    assert contextual['event_time_known'] is False and not contextual.get('event_time_start')


@pytest.mark.anyio
async def test_fact_note_and_legacy_event_keep_existing_excerpt_behavior(env):
    for kind, query in [('fact', 'factmarker811'), ('note', 'notemarker822'), ('event', 'legacy833')]:
        saved = env.store.create_memory({'kind': kind, 'content': query + ' ' + '原有资料。' * 240})
        found = next(item for item in await env.engine.recall('web:default', 'owner', query) if item['id'] == saved['id'])
        assert 'event_context' not in found
        assert found['content'] == found['excerpt']
        assert found['excerpt'] == saved['content'][found['retrieval']['start']:found['retrieval']['end']]


@pytest.mark.anyio
async def test_event_structure_does_not_widen_scope_or_private_user_visibility(env):
    env.store.upsert_scope({'id': 'web:default', 'memory_space': 'shared-organization'})
    env.store.upsert_scope({'id': 'web:other', 'memory_space': 'shared-organization'})
    own = event(env.store, story(title='OWNER_VISIBLE'))
    other_user = event(env.store, story(title='OTHER_USER_SECRET'), user_id='someone-else')
    other_scope = event(env.store, story(title='OTHER_SCOPE_SECRET'), scope_id='web:other', shareable=True)
    not_shared = event(env.store, story(title='UNSHARED_GROUP_EVENT'), scope_id='web:other', visibility='group')
    records = await env.engine.recall('web:default', 'owner', 'cobaltphase7823')
    assert {item['id'] for item in records} == {own['id']}
    assert not {other_user['id'], other_scope['id'], not_shared['id']} & {item['id'] for item in records}
    current = env.store.add_message('web:default', 'owner', 'user', 'cobaltphase7823')
    prompt = env.chat._context(env.store.get_scope('web:default'), 'owner', current['content'], [], records, [], current['id'], history=[])
    assert all(marker not in references(prompt) for marker in ('OTHER_USER_SECRET', 'OTHER_SCOPE_SECRET', 'UNSHARED_GROUP_EVENT'))


@pytest.mark.anyio
async def test_event_correction_and_delete_invalidate_cached_recall(env):
    saved = event(env.store)
    assert any(row['id'] == saved['id'] for row in await env.engine.recall('web:default', 'owner', 'cobaltphase7823'))
    replacement = story(title='更新后的展馆事件', narrative='更正：实际处理的是 copperphase9124，已完成重新校准。',
                        summary='更正后的经过。', outcome='保留了最新版本。')
    updated = env.store.update_memory(saved['id'], {'episode': replacement})
    assert updated['content'] == episode_content(replacement)
    assert await env.engine.recall('web:default', 'owner', 'cobaltphase7823') == []
    latest = await env.engine.recall('web:default', 'owner', 'copperphase9124')
    assert latest and latest[0]['version'] == updated['version']
    assert '更新后的展馆事件' in latest[0]['event_context']
    env.store.delete_memory(saved['id'])
    assert await env.engine.recall('web:default', 'owner', 'copperphase9124') == []


@pytest.mark.anyio
async def test_unknown_event_date_is_neutral_despite_current_creation_time(env):
    saved = event(env.store)
    result = await env.engine.inspect('web:default', 'owner', '今天 cobaltphase7823')
    found = next(row for row in result['items'] if row['id'] == saved['id'])
    assert found['retrieval']['time_boost'] == 0
    assert found['event_time_known'] is False and not found.get('event_time_start')
    assert found['created_at'] and not found.get('occurred_at')


@pytest.mark.anyio
async def test_event_reference_json_stays_whole_inside_existing_total_budget(env):
    memories = []
    for index in range(9):
        value = story(title=f'事件{index}' + '题' * 110, summary='概' * 500, outcome='果' * 600,
                      location='地' * 200, participants=[f'称呼{person}' + '人' * 70 for person in range(8)])
        saved = event(env.store, value)
        excerpt = saved['content'][1800:1800 + CHUNK_SIZE]
        memories.append({**saved, 'excerpt': excerpt, 'event_context': episode_prompt_text(saved, excerpt)})
    current = env.store.add_message('web:default', 'owner', 'user', '这些经历的结果是什么？')
    messages = env.chat._context(env.store.get_scope('web:default'), 'owner', current['content'], [], memories, [], current['id'], history=[])
    raw = references(messages)
    packed = json.loads(raw)
    assert len(raw) <= 16000 and 0 < len(packed['memories']) < len(memories)
    originals = {item['id']: item for item in memories}
    assert all(item['content'] == originals[item['id']]['event_context'] for item in packed['memories'])
    assert packed['omitted_references']['memories'] == len(memories) - len(packed['memories'])
    assert messages[-1]['content'] == current['content']


@pytest.mark.anyio
async def test_related_event_retains_context_and_huge_stale_graph_is_bounded(env, monkeypatch):
    seed = env.store.create_memory({'content': '参观前的约定'})
    target = event(env.store)
    relation(env.store, 'a-valid-event', seed, target)
    found = env.engine._related('web:default', 'owner', [seed], limit=1)[0]
    assert found['id'] == target['id'] and found['excerpt'] == target['content'][:CHUNK_SIZE]
    assert target['episode']['outcome'] in found['event_context'] and len(found['event_context']) <= 2200
    expired = env.store.create_memory({'content': '过期事件', 'valid_to': '2000-01-01T00:00:00Z'})
    second_seed = env.store.create_memory({'content': '高连接度的约定'})
    template = relation(env.store, 'stale-0000', second_seed, expired)
    with env.store._lock, env.store._conn:
        for index in range(1, 1100):
            env.store._save_memory({**template, 'id': f'stale-{index:04d}'})
    pages = []
    original = env.store.memory_relations_for_node
    def tracked(*args, **kwargs):
        page = original(*args, **kwargs)
        pages.append(len(page))
        return page
    monkeypatch.setattr(env.store, 'memory_relations_for_node', tracked)
    assert env.engine._related('web:default', 'owner', [second_seed], limit=4) == []
    assert 0 < sum(pages) <= 512 and max(pages) <= 32


def test_episode_normalization_and_maximum_shape_are_bounded_without_invented_fields():
    minimal = normalize_episode({'narrative': ' 原文说明的经过。 '})
    assert minimal['narrative'] == '原文说明的经过。'
    assert minimal['title'] == minimal['summary'] == minimal['location'] == minimal['outcome'] == ''
    assert minimal['participants'] == []
    complete = normalize_episode({'title': '题' * 120, 'summary': '概' * 500, 'narrative': '经' * 3600,
        'participants': [str(index) + '人' * 79 for index in range(8)], 'location': '地' * 200, 'outcome': '果' * 600})
    assert len(episode_content(complete)) <= 6000
    assert normalize_episode({'narrative': '经过', 'participants': [' 同行者 ', '同行者']})['participants'] == ['同行者']
    for invalid in ({'narrative': ''}, {'narrative': '经' * 3601}, {'narrative': '经过', 'location': None},
                    {'narrative': '经过', 'participants': ['称呼'] * 9},
                    {'narrative': '经过', 'participants': ['人' * 81]},
                    {'narrative': '经过', 'participants': [{'user_id': 'someone'}]},
                    {'narrative': '经过', 'event_time_known': True}):
        with pytest.raises(ValueError):
            normalize_episode(invalid)
