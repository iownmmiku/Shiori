import asyncio
import json
from copy import deepcopy
from datetime import datetime, timedelta, timezone

from shiori.chat import ChatService
from qunyu.db import Store
from shiori.settings import DEFAULTS


class Config:
    def __init__(self): self.data = deepcopy(DEFAULTS)
    def get(self): return deepcopy(self.data)


class Model:
    def __init__(self): self.next_calls = []; self.inputs = []
    async def complete(self, messages, **kwargs):
        self.inputs.append(deepcopy(messages))
        calls, self.next_calls = self.next_calls, []
        return {'content': '' if calls else '收到，结果见回执。', 'tool_calls': calls,
                'message': {'role':'assistant', 'content':None, 'tool_calls':calls}, 'usage':{}}
    def call(self, name, args):
        self.next_calls.append({'id':str(len(self.next_calls)), 'type':'function',
                                'function':{'name':name, 'arguments':json.dumps(args, ensure_ascii=False)}})


def test_calculation_receipt_followup_has_one_success(tmp_path):
    store, model = Store(tmp_path), Model()
    chat = ChatService(store, Config(), model)
    async def run():
        model.call('calculate', {'expression':'1+1'})
        first = await chat.respond('web:default', 'owner', '帮我算一下 1+1')
        assert len(first['actions']) == 1 and first['actions'][0]['result']['result'] == 2
        model.call('tool_history', {})
        second = await chat.respond('web:default', 'owner', '刚才操作结果如何')
        assert len(second['actions']) == 1 and second['actions'][0]['ok']
        assert second['actions'][0]['result'][0]['result']['result'] == 2
        assert 'NoneType' not in str(model.inputs)
        await chat.close()
    asyncio.run(run())


def test_tool_recall_deletion_cannot_reenter_receipt_or_actions(tmp_path):
    store, model = Store(tmp_path), Model()
    memory = store.create_memory({'scope_id':'web:default', 'user_id':'owner', 'content':'独有秘密星河岛', 'visibility':'private'})
    chat = ChatService(store, Config(), model)
    async def run():
        model.call('recall_memory', {'query':'独有秘密'})
        first = await chat.respond('web:default', 'owner', '看看我的长期资料')
        assert first['actions'][0]['result'][0]['id'] == memory['id']
        ledger = chat.tool_actions.list()[0]
        assert ledger['memory_versions'][memory['id']] == memory['version']
        store.delete_memory(memory['id'])
        model.inputs.clear()
        model.call('tool_history', {})
        second = await chat.respond('web:default', 'owner', '刚才的工具结果是什么')
        assert second['actions'][0]['result'] == []
        assert '星河岛' not in str(model.inputs) + str(second)
        assert '星河岛' not in str(store.get_message(second['message_id'])['metadata'])
        await chat.close()
    asyncio.run(run())


def test_multiple_model_tasks_distinct_and_replay_idempotent(tmp_path):
    store, model = Store(tmp_path), Model()
    chat = ChatService(store, Config(), model)
    async def run():
        model.call('create_task', {'title':'买牛奶'})
        model.call('create_task', {'title':'整理书桌'})
        result = await chat.respond('web:default', 'owner', '请创建两个待办：买牛奶和整理书桌，分开保存')
        assert len(store.list_tasks()) == 2
        assert len({x['result']['id'] for x in result['actions']}) == 2
        source = store.list_messages('web:default')[0]
        repeated = await chat._tool('create_task', {'title':'买牛奶'}, 'web:default', 'owner', source['content'], source['id'], {})
        assert repeated['id'] in {x['id'] for x in store.list_tasks()} and len(store.list_tasks()) == 2
        await chat.close()
    asyncio.run(run())


def test_legacy_incomplete_receipt_remains_visible_but_not_model_context(tmp_path):
    store, model = Store(tmp_path), Model()
    chat = ChatService(store, Config(), model)
    source = store.add_message('web:default', 'owner', 'user', '旧记录请求')
    chat.tool_actions._add(scope_id='web:default', user_id='owner', status='succeeded', tool_name='recall_memory',
                           source_ids=[source['id']], result={'secret':'旧无依赖正文'})
    async def run():
        model.call('tool_history', {})
        result = await chat.respond('web:default','owner','此前操作结果')
        assert result['actions'][0]['result'] == []
        assert chat.tool_actions.list()[1]['result']['secret'] == '旧无依赖正文'
        assert '旧无依赖正文' not in str(model.inputs)
        await chat.close()
    asyncio.run(run())


def test_public_persona_knowledge_deleted_invalidates_assistant_history(tmp_path):
    store, model = Store(tmp_path), Model()
    chat = ChatService(store, Config(), model)
    timestamp = datetime.now(timezone.utc)
    chat.knowledge._state['items'].append({'id':'note1','persona_id':'default','title':'番剧新作品', 'interest':'番剧',
        'text':'独有作品碧海之星','url':'https://example.com/anime','status':'active','retrieved_at':timestamp.isoformat(),
        'valid_until':(timestamp+timedelta(days=3)).isoformat()})
    async def run():
        first = await chat.respond('web:default','owner','近期番剧有什么')
        assert '碧海之星' in str(model.inputs)
        assert store.get_message(first['message_id'])['metadata']['knowledge_refs']
        chat.knowledge.delete('note1')
        model.inputs.clear()
        await chat.respond('web:default','owner','重复刚才番剧的内容')
        assert '碧海之星' not in str(model.inputs)
        await chat.close()
    asyncio.run(run())


def test_corrupt_ledger_starts_degraded_and_resume_never_replays(tmp_path):
    (tmp_path/'tool_runs.json').write_text('{bad', 'utf-8')
    store = Store(tmp_path)
    chat = ChatService(store, Config(), Model())
    assert chat.tool_actions.status()['suspended']
    assert chat.tool_actions.status()['warning']
    assert list(tmp_path.glob('tool_runs.corrupt-*.json'))
    assert chat.tool_actions.available('web:default','owner') == []
    assert chat.tool_actions.resume()['ok']
    assert not chat.tool_actions.status()['suspended']
    assert not chat.tool_actions.list()
    asyncio.run(chat.close())


def test_shared_fact_respond_revoke_removes_old_assistant_and_receipt(tmp_path):
    store, model, settings = Store(tmp_path), Model(), Config()
    for name in ('web:group:source', 'web:group:target'):
        store.upsert_scope({'id':name, 'memory_space':'音乐社'})
    source = store.add_message('web:group:source','other','user','共享资料是星海音乐节')
    memory = store.create_memory({'scope_id':'web:group:source','user_id':'other','content':'共享资料是星海音乐节',
                                  'visibility':'group','shareable':True,'source_ids':[source['id']]})
    chat = ChatService(store, settings, model)
    async def run():
        model.call('recall_memory', {'query':'音乐节'})
        first = await chat.respond('web:group:target','owner','音乐节资料')
        assert '星海音乐节' in str(model.inputs)
        metadata = store.get_message(first['message_id'])['metadata']
        assert metadata['memory_versions'][memory['id']] == memory['version']
        assert source['id'] not in metadata['context_source_ids']
        store.upsert_scope({'id':'web:group:target','memory_space':''})
        model.inputs.clear()
        model.call('tool_history', {})
        result = await chat.respond('web:group:target','owner','重复此前资料')
        assert not result['actions'][0]['result']
        assert '星海音乐节' not in str(model.inputs)
        await chat.close()
    asyncio.run(run())


def test_named_direct_command_preserves_original_source(tmp_path):
    store, model = Store(tmp_path), Model()
    chat = ChatService(store, Config(), model)
    async def run():
        result = await chat.respond('web:default', 'owner', '初音未来，记住：我喜欢音乐')
        assert '已记住' in result['reply'] and not model.inputs
        memory = store.list_memories()[0]
        assert memory['content'] == '我喜欢音乐'
        assert store.get_message(memory['source_ids'][0])['content'] == '初音未来，记住：我喜欢音乐'
        await chat.close()
    asyncio.run(run())


def test_new_scope_inherits_global_planner_and_nullable_updates(tmp_path):
    from shiori.settings import resolved_identity
    store, config = Store(tmp_path), Config()
    config.data['identity']['planner_enabled'] = False
    scope = store.upsert_scope({'id':'web:new'})
    assert scope['plan_enabled'] is None
    assert not resolved_identity(config.get(),scope)['plan_enabled']
    scope = store.upsert_scope({'id':'web:new','plan_enabled':True})
    assert resolved_identity(config.get(),scope)['plan_enabled']
    scope = store.upsert_scope({'id':'web:new','plan_enabled':None})
    assert not resolved_identity(config.get(),scope)['plan_enabled']


def test_persona_knowledge_revocation_invalidates_digest(tmp_path):
    store, model, config = Store(tmp_path), Model(), Config()
    config.data['model'].update(base_url='https://fake.invalid/v1', model='fixture')
    config.data['context']['keep_recent'] = 1
    chat = ChatService(store, config, model)
    stamp = datetime.now(timezone.utc)
    note = {'id':'k1','persona_id':'default','title':'新番音乐','interest':'新番','text':'作品星海之声',
            'url':'https://example.com/new-anime','status':'active','retrieved_at':stamp.isoformat(),
            'valid_until':(stamp+timedelta(days=2)).isoformat()}
    chat.knowledge._state['items'].append(note)
    async def run():
        await chat.respond('web:default','owner','聊聊新番音乐')
        store.add_message('web:default','owner','user','继续讨论')
        store.add_message('web:default','assistant','assistant','继续讨论的回答')
        store.add_message('web:default','owner','user','最后一个问题')
        result = await chat.context.compact('web:default','owner')
        assert result['compressed']
        digest = result['summary']
        assert digest['knowledge_refs']['k1']
        chat.knowledge.delete('k1')
        assert chat._changed({digest['id']:digest['version']},{},'web:default','owner')
        assert chat.context._latest_summary('web:default','owner') is None
        await chat.close()
    asyncio.run(run())
