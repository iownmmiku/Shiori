"""Author message boundaries during generation, without a second model call."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from auth_helpers import password_login
from shiori.api import create_app
from shiori.db import Store
from shiori.settings import Settings
from shiori.output import OutputLimitError, generation_instructions, model_reply_parts, output_config, split_output
from shiori.official import OfficialBridge
from test_output_v5 import bridge_setup, Settings as FixtureSettings, Chat as FixtureChat


@pytest.fixture
def anyio_backend(): return 'asyncio'


@pytest.mark.parametrize('reply,parts', [
    ('嗯', ['嗯']),
    ('第一句。第二句。第三句。', ['第一句。第二句。第三句。']),
    ('好呀。\n\n今天感觉怎么样？多说几句也放在这一条。', ['好呀。','今天感觉怎么样？多说几句也放在这一条。']),
    ('第一行\n第二行\n\n换个话题', ['第一行\n第二行','换个话题']),
    ('\n \n嗯\r\n \r\n\t再说一句\n\n ', ['嗯','再说一句']),
    (' \n\n \t', []),
    ('前面\n\n```python\na=1\n\nprint(a)\n```\n\n后面', ['前面','```python\na=1\n\nprint(a)\n```','后面']),
    ('前面\n\n~~~~text\na\n\nb\n~~~~\n\n后面', ['前面','~~~~text\na\n\nb\n~~~~','后面']),
    ('````md\n```python\n\na\n```\n````\n\n完成', ['````md\n```python\n\na\n```\n````','完成']),
    ('开始\n\n```py\n未关闭\n\n仍然是代码', ['开始','```py\n未关闭\n\n仍然是代码']),
    ('{\n  "text": "一",\n\n  "count": 1\n}', ['{\n  "text": "一",\n\n  "count": 1\n}']),
    ('["一",\n\n"二"]', ['["一",\n\n"二"]']),
])
def test_only_authored_blank_lines_are_message_boundaries(reply, parts):
    assert model_reply_parts(reply) == parts


def test_model_mode_ignores_old_character_targets_and_does_not_invent_breaks():
    text='这是一段完整的话。它里面可以有许多句子。'*25
    config={'output':{'target_chars':20,'max_chars':40}}
    assert output_config(config)['mode']=='model'
    assert split_output(text,config)==[text]
    assert model_reply_parts('嗯\n\n好的\n\n第三条')==['嗯','好的','第三条']
    assert split_output(text,{'output':dict(config['output'],mode='length')})!=[text]


def test_disabled_mode_and_excess_parts_keep_full_content():
    reply='一\n\n二\n\n三\n\n四'
    assert model_reply_parts(reply,{'output':{'enabled':False}})==[reply]
    assert model_reply_parts(reply,{'output':{'max_parts':2}})==['一\n\n二','三\n\n四']
    assert generation_instructions({'output':{'enabled':False}}).find('一条完整消息')>=0
    assert generation_instructions({'output':{'mode':'length'}})==''


def test_platform_safety_is_separate_and_never_truncates_a_message():
    reply='自然的一条消息。'*35
    config={'output':{'platform_max_chars':80}}
    assert model_reply_parts(reply,config)==[reply]
    physical=split_output(reply,config)
    assert len(physical)>1 and ''.join(physical)==reply and max(map(len,physical))<=80
    code='```python\n'+'a=1\n'*50+'```'
    assert model_reply_parts(code,config)==[code]
    with pytest.raises(OutputLimitError): split_output(code,config)


def test_generation_prompt_and_chosen_parts_persist_without_second_request(tmp_path,monkeypatch):
    app=create_app(tmp_path,start_workers=False)
    reply='嗯。\n\n这一条可以很长，也可以有很多句。'+('仍然在同一条消息里。'*20)+'\n\n```py\na=1\n\nprint(a)\n```'
    seen=[]
    async def complete(messages,**kwargs):
        assert kwargs['before_send']()
        seen.append(messages)
        assert '在生成回复时自行决定' in messages[0]['content']
        return {'content':reply,'tool_calls':[],'message':{'role':'assistant','content':reply},'usage':{}}
    monkeypatch.setattr(app.state.provider,'complete',complete)
    with TestClient(app) as client:
        password_login(client,app)
        response=client.post('/api/chat',json={'text':'今天怎么样？'})
        assert response.status_code==200,response.text
        result=response.json()
        assert len(seen)==1 and result['reply']==reply
        assert result['reply_parts']==model_reply_parts(reply)
        message=app.state.store.get_message(result['message_id'])
        assert message['content']==reply and message['metadata']['reply_parts']==result['reply_parts']
        assert client.put('/api/scopes/web:default',json={'output':{'mode':'model','max_parts':2}}).status_code==200
        assert client.put('/api/settings',json={'output':{'mode':'invalid'}}).status_code==400
        assert client.put('/api/scopes/web:default',json={'output':{'mode':'invalid'}}).status_code==400


@pytest.mark.anyio
@pytest.mark.parametrize('failure', ['none','send','permission','source'])
async def test_onebot_sends_chosen_messages_and_keeps_mid_reply_guards(bridge_setup,failure):
    store,settings,chat,bridge=bridge_setup
    settings.config['output'].update(mode='model',target_chars=20,max_chars=40)
    parts=['嗯。','这一条里面包含很多句。'*20,'最后一句。']
    chat.respond=AsyncMock(return_value={'reply':'\n\n'.join(parts),'message_id':'fixture'})
    sent=[]
    async def send(scope_id,text,**kwargs):
        sent.append((text,kwargs['metadata']))
        if failure=='permission':settings.config['onebot']['allowed_groups']=[]
        if failure=='source':store.clear_messages(scope_id)
        return not (failure=='send' and len(sent)==2)
    bridge.send=AsyncMock(side_effect=send)
    await bridge._flow.handle('qq:group:202','303','初音未来你好',[],{})
    count=3 if failure=='none' else 2 if failure=='send' else 1
    assert [item[0] for item in sent]==parts[:count]
    assert [item[1]['output_part'] for item in sent]==[{'index':n+1,'total':3} for n in range(count)]
    assert chat.respond.await_count==1
    await bridge.stop()


@pytest.mark.anyio
async def test_official_model_parts_preserve_code_and_unique_sequence(tmp_path):
    store,settings=Store(tmp_path),FixtureSettings()
    settings.config['onebot']['adapter']='official'
    settings.config['output'].update(mode='model')
    settings.config['qq_official'].update(chat_policy='whitelist',enabled=True,allowed_groups=['gid'])
    store.upsert_scope({'id':'qqofficial:group:gid','plan_enabled':False})
    chat=FixtureChat(store,settings)
    parts=['好的','```python\na=1\n\nprint(a)\n```','还有问题吗？']
    chat.respond=AsyncMock(return_value={'reply':'\n\n'.join(parts)})
    bridge=OfficialBridge(store,settings,chat)
    bridge._connected=bridge._running=True
    sent=[]
    async def post(**kwargs):sent.append(kwargs);return {'id':str(len(sent))}
    bridge._client=SimpleNamespace(api=SimpleNamespace(post_group_message=post))
    await bridge._flow.handle('qqofficial:group:gid','uid','初音未来你好',[],{'platform_message_id':'source'})
    assert [item['content'] for item in sent]==parts
    assert [item['msg_seq'] for item in sent]==[1,2,3]
    await bridge.stop();store.close()
