"""Reference packing and overlap through the real chat/context assembly."""
from copy import deepcopy
from types import SimpleNamespace
import json

import pytest

from mikubot.chat import ChatService
from mikubot.db import Store
from mikubot.settings import DEFAULTS


@pytest.fixture
def anyio_backend():
    return 'asyncio'


class Model:
    def __init__(self):
        self.calls = []

    async def complete(self, messages, **options):
        assert options['before_send']()
        self.calls.append(deepcopy(messages))
        return {'content': '明白。', 'tool_calls': [], 'usage': {}}


@pytest.fixture
async def env(tmp_path):
    config = deepcopy(DEFAULTS)
    config['time']['awareness_enabled'] = False
    config['context']['enabled'] = False
    config['memory']['organization_enabled'] = False
    config['stickers']['enabled'] = False
    settings = SimpleNamespace(get=lambda: deepcopy(config))
    store, model = Store(tmp_path), Model()
    store.upsert_scope({'id': 'web:default', 'auto_memory': False, 'allow_tools': False,
                        'use_persona_knowledge': False})
    chat = ChatService(store, settings, model)
    try:
        yield store, chat, model, config
    finally:
        await chat.close()
        store.close()


def reference_data(messages):
    block = next(item['content'] for item in messages if str(item['content']).startswith('以下是当前允许使用的参考数据'))
    return block.split('\n', 1)[1]


def essential_data(messages):
    block = next(item['content'] for item in messages if str(item['content']).startswith('【本轮回复计划与命令回执】'))
    return json.loads(block.split('\n', 1)[1])


@pytest.mark.anyio
async def test_long_memory_references_are_whole_json_and_keep_reply_plan(env):
    store, chat, _, _ = env
    memories = [store.create_memory({'content': ('参考标签' + str(i) + '。' + '长篇原文资料。' * 400)[:2200],
                                    'kind': 'note'}) for i in range(8)]
    current = store.add_message('web:default', 'owner', 'user', '这些资料提到了什么？')
    plan = {'target_message_id': current['id'], 'target_user_id': 'owner', 'intent': '结合资料回答', 'needed_info': []}
    messages = chat._context(store.get_scope('web:default'), 'owner', current['content'], [], memories, [],
                             current['id'], {'planner': plan}, history=[])
    raw = reference_data(messages)
    data = json.loads(raw)
    assert len(raw) <= 16000 and essential_data(messages)['reply_plan'] == plan
    assert 0 < len(data['memories']) < len(memories)
    assert data['omitted_references']['memories'] == len(memories) - len(data['memories'])
    originals = {item['id']: item for item in memories}
    assert all(item['content'] == originals[item['id']]['content'] for item in data['memories'])
    assert messages[-1]['content'] == current['content']


@pytest.mark.anyio
async def test_required_command_receipts_remain_whole_outside_optional_reference_budget(env):
    store, chat, _, _ = env
    memory = store.create_memory({'content': '只作为可选参考的记忆'})
    current = store.add_message('web:default', 'owner', 'user', '然后呢？')
    receipt = [{'source_id': 'already-executed-source', 'reply': '既有真实回执' * 4000}]
    messages = chat._context(store.get_scope('web:default'), 'owner', current['content'], [], [memory], [],
                             current['id'], {'_handled_commands': receipt}, history=[])
    data = json.loads(reference_data(messages))
    assert essential_data(messages)['completed_commands'] == receipt
    assert data['memories'][0]['id'] == memory['id']
    assert sum(str(item['content']).count('already-executed-source') for item in messages) == 1


@pytest.mark.anyio
async def test_input_budget_never_silently_discards_completed_receipts_or_reply_plan(env):
    store, chat, _, config = env
    config['context']['max_input_chars'] = 5000
    current = store.add_message('web:default', 'owner', 'user', '然后呢？')
    receipt = [{'source_id': 'already-executed-source', 'reply': '既有真实回执' * 4000}]
    plan = {'target_message_id': current['id'], 'target_user_id': 'owner', 'intent': '继续交流', 'needed_info': []}
    with pytest.raises(ValueError, match='必要回执'):
        chat._context(store.get_scope('web:default'), 'owner', current['content'], [], [], [],
                      current['id'], {'_handled_commands': receipt, 'planner': plan}, history=[])


@pytest.mark.anyio
@pytest.mark.parametrize('copies', [1, 2])
async def test_time_lookup_does_not_repeat_recent_messages_or_merge_distinct_ids(env, copies):
    store, chat, model, _ = env
    original = 'UNIQUE_ORIGINAL_01234 我们约好去公园。'
    for _ in range(copies):
        source = store.add_message('web:default', 'owner', 'user', original)
        store.add_message('web:default', 'assistant', 'assistant', '约定已经说清楚。', source_ids=[source['id']])
    result = await chat.respond('web:default', 'owner', '今天我们约好了什么？')
    assert result['reply']
    prompt = '\n'.join(str(item['content']) for item in model.calls[-1])
    assert prompt.count('UNIQUE_ORIGINAL_01234') == copies


@pytest.mark.anyio
async def test_time_reference_is_retained_if_input_budget_discards_its_recent_original(env):
    store, chat, _, config = env
    config['context']['max_input_chars'] = 5000
    original = store.add_message('web:default', 'owner', 'user', 'KEEP_TIME_REFERENCE 我们约好去公园。')
    long_recent = store.add_message('web:default', 'owner', 'user', '其他话题' * 1000)
    current = store.add_message('web:default', 'owner', 'user', '今天约好了什么？')
    messages = chat._context(store.get_scope('web:default'), 'owner', current['content'], [], [], [], current['id'],
        history=[original, long_recent, current], recalled_history=[original])
    assert sum('KEEP_TIME_REFERENCE' in str(item['content']) for item in messages) == 1


@pytest.mark.anyio
async def test_overlapping_reference_blocks_keep_one_copy_and_its_provenance(env):
    store, chat, _, _ = env
    original = store.add_message('web:default', 'owner', 'user', 'ONE_REFERENCE_SOURCE 约好去公园。')
    current = store.add_message('web:default', 'owner', 'user', '上次的约定是什么？')
    manifest = {'message_ids': set(), 'memory_versions': {}, 'jargon_versions': {}, 'knowledge_refs': {}}
    messages = chat._context(store.get_scope('web:default'), 'owner', current['content'], [], [], [], current['id'],
        manifest=manifest, history=[], recalled_history=[original], continuity=[original])
    assert sum('ONE_REFERENCE_SOURCE' in str(item['content']) for item in messages) == 1
    assert original['id'] in manifest['message_ids']
