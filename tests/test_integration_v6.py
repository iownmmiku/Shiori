import asyncio
import io
import json
import zipfile
from copy import deepcopy

import pytest
from fastapi.testclient import TestClient
from shiori.api import create_app
from shiori.chat import ChatService
from qunyu.db import Store
from shiori.settings import DEFAULTS
from shiori.evaluation import replay,validate_cases
from auth_helpers import password_login


@pytest.fixture
def client(tmp_path):
    app=create_app(tmp_path/'v6',start_workers=False)
    with TestClient(app) as c:
        password_login(c, app)
        yield c,app


def test_presets_and_overrides_do_not_connect_or_overwrite_identity(client):
    c,app=client
    c.put('/api/settings',json={'identity':{'name':'自定义Miku'}})
    assert len(c.get('/api/presets').json())==3
    result=c.post('/api/scopes/preset',json={'scope_id':'web:default','preset_id':'private_always'})
    assert result.status_code==200 and result.json()['always_reply']
    assert app.state.settings.get()['identity']['name']=='自定义Miku'
    assert not app.state.settings.get()['onebot']['enabled']
    c.post('/api/scopes',json={'id':'qq:group:42'})
    assert c.post('/api/scopes/preset',json={'scope_id':'qq:group:42','preset_id':'private_always'}).status_code==400
    assert c.put('/api/scopes/web:default',json={'planner_daily_calls':12,'use_persona_knowledge':False,'output':{'platform_max_chars':80}}).status_code==200
    assert c.put('/api/scopes/web:default',json={'planner_daily_calls':-1}).status_code==400
    assert c.put('/api/scopes/web:default',json={'use_persona_knowledge':'false'}).status_code==400
    assert c.put('/api/settings',json={'planner':{'daily_calls':True}}).status_code==400
    assert c.put('/api/settings',json={'onebot':{'max_message_chars':1}}).status_code==400


def test_partial_persona_edit_preserves_custom_prompt_and_validates_exploration(client):
    c,app=client
    c.put('/api/personas/default',json={'system_prompt':'保留这份自定义设定'})
    response=c.put('/api/personas/default',json={'exploration':{'daily_searches':0,'valid_days':7},'excluded_topics':['剧透']})
    assert response.status_code==200
    assert response.json()['system_prompt']=='保留这份自定义设定'
    assert c.put('/api/personas/default',json={'exploration':{'daily_searches':-1}}).status_code==400


def test_backup_tool_history_redacted_read_only_and_cannot_replay(client):
    c,app=client; actions=app.state.chat.tool_actions
    run=actions.begin_local('fixture',{'token':'unknown-secret','nested':{'password':'never-export'}},'web:default','owner',[])
    actions.finish_local(run,{'value':'receipt','api_key':'unconfigured-secret'})
    backup=c.get('/api/backup').content
    with zipfile.ZipFile(io.BytesIO(backup)) as archive:
        text=archive.read('tool_history.json').decode()
        assert 'unknown-secret' not in text and 'never-export' not in text and 'unconfigured-secret' not in text
        assert 'tool_history_read_only' in archive.read('manifest.json').decode()
    response=c.post('/api/backup/restore',files={'file':('v6.zip',backup,'application/zip')})
    assert response.status_code==200,response.text
    runs=c.get('/api/tool-runs').json()
    assert runs and all(x['read_only'] for x in runs)
    assert len(runs)==1
    assert c.post('/api/tool-runs/'+runs[0]['id']+'/approve').status_code==400
    assert app.state.settings.get()['paused']
    assert c.post('/api/backup/restore',files={'file':('again.zip',c.get('/api/backup').content,'application/zip')}).status_code==200
    assert len(c.get('/api/tool-runs').json())==1


class Config:
    def __init__(self): self.data=deepcopy(DEFAULTS)
    def get(self): return deepcopy(self.data)


class Model:
    def __init__(self): self.inputs=[]
    async def complete(self,messages,**kwargs):
        if kwargs.get('before_send'): assert kwargs['before_send']()
        self.inputs.append(messages)
        return {'content':'已找到旅行资料','tool_calls':[],'usage':{}}


def test_planner_needed_info_retrieves_bounded_local_memory_and_reinforces(tmp_path):
    async def run():
        store=Store(tmp_path); cfg=Config(); model=Model(); chat=ChatService(store,cfg,model)
        memory=store.create_memory({'scope_id':'web:default','user_id':'owner','content':'秘密旅行计划：横滨音乐节','kind':'note'})
        # A needed_info query can retrieve an item absent from the current text.
        result=await chat.respond('web:default','owner','那个呢',metadata={'planner':{'needed_info':['横滨音乐节']}})
        assert memory['id'] in store.get_message(result['message_id'])['metadata']['memory_versions']
        assert '横滨音乐节' in str(model.inputs)
        overview=await chat._planner_knowledge('web:default','owner','横滨')
        assert chat._planner_knowledge_current('web:default','owner',overview)
        store.delete_memory(memory['id'])
        assert not chat._planner_knowledge_current('web:default','owner',overview)
        await chat.close(); store.close()
    asyncio.run(run())


def test_synthetic_replay_is_explicitly_labeled_and_never_calls_network():
    from pathlib import Path
    cases=json.loads((Path(__file__).parents[1]/'evals/routing-synthetic.json').read_text('utf-8'))
    report=asyncio.run(replay(cases))
    assert report['mode']=='scripted_routing_regression'
    assert report['summary']['decision_accuracy']==1 and report['summary']['errors']==0
    assert report['summary']['cost']==0 and report['summary']['usefulness_proxy'] is None
    assert all(x['human_usefulness'] is None for x in report['records'])
    with pytest.raises(ValueError): validate_cases([{'id':'bad','text':'x','expected':{'reply':'yes'}}])
