import asyncio
import base64
import io
import json
import sqlite3
from types import SimpleNamespace

from PIL import Image
from fastapi.testclient import TestClient
import pytest

from auth_helpers import password_login
from shiori.api import create_app
from shiori.db import Store
from shiori.settings import Settings
from shiori.social import SocialEngine, ConversationFlow
from shiori.stickers import StickerLibrary, download_image, MAX_IMAGE
from shiori.provider import ProviderStreamError


def png():
    result=io.BytesIO()
    Image.new('RGB',(16,16),'cyan').save(result,format='PNG')
    return 'data:image/png;base64,'+base64.b64encode(result.getvalue()).decode()


class Model:
    def __init__(self):
        self.calls=[]
        self.description={'is_sticker':True,'clear':True,'meaning':'开心地感谢对方的帮助','category':'感谢','tags':['谢谢','开心'],'confidence':.98}
        self.after=None
    async def complete(self,messages,**kwargs):
        assert kwargs['before_send']()
        self.calls.append((messages,kwargs['purpose']))
        if kwargs['purpose']=='sticker_vision': result=dict(self.description)
        else:
            candidates=json.loads(messages[-1]['content'])['candidates']
            result={'id':candidates[0]['id'],'confidence':.98,'appropriate':True}
        if self.after:self.after()
        return {'content':json.dumps(result,ensure_ascii=False)}


@pytest.fixture
def library(tmp_path):
    store=Store(tmp_path);settings=Settings(tmp_path)
    settings.update({'onebot':{'enabled':True,'access_token':'isolated-fixture-token','chat_policy':'blacklist'},
        'model':{'base_url':'https://fixture.invalid/v1','model':'local-test','vision':True},
        'stickers':{'collect_probability':1,'send_probability':1}})
    model=Model();social=SocialEngine(store,settings,model)
    engine=StickerLibrary(store,settings,model,social._allowed)
    store.upsert_scope({'id':'qq:group:1'})
    yield store,settings,model,engine
    asyncio.run(engine.close());store.close()


async def collect(store,engine,scope='qq:group:1'):
    message=store.add_message(scope,'123','user','',images=[png()])
    engine.observe(message)
    if engine._jobs:await asyncio.gather(*engine._jobs)
    return message


def test_defaults_probabilities_and_invalid_settings(tmp_path):
    cfg=Settings(tmp_path)
    assert cfg.get()['stickers']['collect_probability']==.1
    assert cfg.get()['stickers']['send_probability']==.1
    assert cfg.get()['stickers']['send_enabled'] is True
    assert cfg.get()['planner']['group_batch_ms']==1500
    for patch in [{'collect_probability':-1},{'send_probability':True},{'send_probability':float('nan')},{'model_id':'missing'}]:
        with pytest.raises(ValueError):cfg.update({'stickers':patch})
    with pytest.raises(ValueError):cfg.update({'planner':{'group_batch_messages':1}})


def test_collect_preserves_local_image_and_deduplicates(library):
    store,cfg,model,engine=library
    asyncio.run(collect(store,engine));asyncio.run(collect(store,engine))
    page=engine.page()
    assert page['total']==1 and len(model.calls)==1
    item=page['items'][0]
    assert item['category']=='感谢' and item['clear'] is True
    stored,raw=engine.payload(item['id'],'qq:group:1',1)
    assert raw==download_image(png())[0] and stored['mime']=='image/png'
    assert 'image' not in item and 'data:' not in json.dumps(item)


@pytest.mark.parametrize('limit,success,attempts', [(3,True,2),(1,False,1)])
def test_sticker_incomplete_stream_retry_consumes_each_daily_call(library, monkeypatch, limit, success, attempts):
    store, cfg, model, engine = library
    cfg.update({'stickers':{'daily_recognitions':limit,'daily_model_calls':limit}})
    complete = model.complete
    seen = []
    async def flaky(messages, **kwargs):
        seen.append(messages)
        if len(seen) == 1: raise ProviderStreamError('流中断')
        return await complete(messages, **kwargs)
    monkeypatch.setattr(model,'complete',flaky)
    asyncio.run(collect(store,engine))
    assert len(seen) == attempts and engine.page()['total'] == int(success)
    quota = engine.quota_status()
    assert quota['vision'] == attempts and quota['total'] == attempts


@pytest.mark.parametrize('clear,confidence',[(False,.99),(True,.3)])
def test_ambiguous_stickers_are_quarantined_until_manually_labelled(library,clear,confidence):
    store,cfg,model,engine=library
    model.description.update(clear=clear,confidence=confidence,meaning='不清楚')
    asyncio.run(collect(store,engine))
    item=engine.page()['items'][0]
    assert item['category']=='待标注' and not item['clear']
    assert asyncio.run(engine.choose('qq:group:1','123','谢谢','谢谢你',lambda:True)) is None
    with pytest.raises(ValueError):engine.update(item['id'],{'clear':True})
    edited=engine.update(item['id'],{'clear':True,'meaning':'友好地表示感谢','category':'礼貌','tags':['感谢']})
    assert edited['confidence']==1 and edited['version']==2
    chosen=asyncio.run(engine.choose('qq:group:1','123','谢谢','谢谢你',lambda:True))
    assert chosen['id']==item['id'] and chosen['version']==2


def test_regular_images_are_not_saved_and_probability_is_a_gate(library):
    store,cfg,model,engine=library
    cfg.update({'stickers':{'collect_probability':.1}});engine.random=lambda:.5
    asyncio.run(collect(store,engine));assert model.calls==[]
    engine.random=lambda:.05;model.description['is_sticker']=False
    asyncio.run(collect(store,engine));assert len(model.calls)==1 and engine.page()['total']==0


def test_send_probability_ambiguity_and_current_version_are_rechecked(library):
    store,cfg,model,engine=library
    asyncio.run(collect(store,engine));item=engine.page()['items'][0]
    cfg.update({'stickers':{'send_probability':.1}});engine.random=lambda:.5
    assert asyncio.run(engine.choose('qq:group:1','123','谢谢','谢谢！',lambda:True)) is None
    assert len(model.calls)==1
    engine.random=lambda:0
    model.after=lambda:engine.update(item['id'],{'meaning':'含义已经改变'})
    assert asyncio.run(engine.choose('qq:group:1','123','谢谢','谢谢！',lambda:True)) is None
    with pytest.raises(ValueError):engine.payload(item['id'],'qq:group:1',1)


def test_scope_sharing_source_revocation_and_delete_tombstone(library):
    store,cfg,model,engine=library
    source=asyncio.run(collect(store,engine));item=engine.page()['items'][0]
    store.upsert_scope({'id':'qq:group:2'})
    with pytest.raises(ValueError):engine.payload(item['id'],'qq:group:2')
    for scope in ('qq:group:1','qq:group:2'):store.upsert_scope({'id':scope,'memory_space':'shared'})
    engine.update(item['id'],{'shareable':True})
    assert engine.payload(item['id'],'qq:group:2')[1]
    cfg.update({'onebot':{'blocked_groups':['1']}})
    with pytest.raises(ValueError):engine.payload(item['id'],'qq:group:2')
    cfg.update({'onebot':{'blocked_groups':[]}})
    tombstone=store.create_memory({'scope_id':'qq:group:1','user_id':'123','kind':'fact','content':'撤销这个来源','source_ids':[source['id']]})
    store.delete_memory(tombstone['id'])
    with pytest.raises(ValueError):engine.payload(item['id'],'qq:group:1')
    engine.delete(item['id']);asyncio.run(collect(store,engine))
    assert engine.page()['total']==0 and len(model.calls)==1


def test_vision_race_and_daily_quota_do_not_save_stale_images(library):
    store,cfg,model,engine=library
    cfg.update({'stickers':{'daily_recognitions':1}})
    model.after=lambda:cfg.update({'stickers':{'collect_enabled':False}})
    asyncio.run(collect(store,engine));assert engine.page()['total']==0
    model.after=None;cfg.update({'stickers':{'collect_enabled':True}})
    asyncio.run(collect(store,engine));assert len(model.calls)==1


def test_image_validation_rejects_bad_data_and_internal_network():
    for url in ['data:image/svg+xml;base64,PHN2Zz4=','data:image/png;base64,not-valid', 'data:image/png;base64,'+'a'*(MAX_IMAGE*2),'http://127.0.0.1/a.png']:
        with pytest.raises(ValueError):download_image(url)


def test_onebot_upgrades_known_qq_image_cdn_to_https():
    from shiori.onebot import OneBotBridge
    _,images,_,_=OneBotBridge._message({'message':[{'type':'image','data':{'url':'http://multimedia.nt.qq.com.cn/download?key=fixture'}}]})
    assert images==['https://multimedia.nt.qq.com.cn/download?key=fixture']


def test_authenticated_library_edit_preview_filter_and_backup(tmp_path):
    app=create_app(tmp_path/'app',start_workers=False)
    app.state.settings.update({'onebot':{'enabled':True,'access_token':'fixture-token'},'model':{'base_url':'https://fixture.invalid/v1','model':'test','vision':True},'stickers':{'collect_probability':1}})
    app.state.chat.stickers.provider=Model()
    asyncio.run(collect(app.state.store,app.state.chat.stickers))
    key=app.state.chat.stickers.page()['items'][0]['id']
    with TestClient(app) as client:
        assert client.get('/api/stickers').status_code==401
        assert client.get(f'/api/stickers/{key}/image').status_code==401
        assert client.get('/api/stickers/backup').status_code==401
        password_login(client,app)
        image=client.get(f'/api/stickers/{key}/image');assert image.status_code==200 and image.headers['content-type']=='image/png'
        edited=client.put(f'/api/stickers/{key}',json={'meaning':'谢谢你的帮助','category':'感谢','clear':True}).json()
        assert edited['version']==2
        page=client.get('/api/stickers?scope_id=qq:group:1&q=谢谢&category=感谢').json()
        assert page['total']==1 and len(page['items'])==1
        snapshot=tmp_path/'sticker-backup.sqlite3';snapshot.write_bytes(client.get('/api/stickers/backup').content)
        conn=sqlite3.connect(snapshot)
        try: assert conn.execute('SELECT length(image) FROM stickers').fetchone()[0]>0
        finally: conn.close()
        assert client.delete(f'/api/stickers/{key}').json()['deleted']
        assert client.get(f'/api/stickers/{key}/image').status_code==404


@pytest.mark.anyio
async def test_onebot_sticker_uses_real_image_segment_and_echo_receipt(library):
    from shiori.onebot import OneBotBridge
    store,cfg,model,engine=library
    await collect(store,engine);item=engine.page()['items'][0]
    chat=SimpleNamespace(stickers=engine)
    bridge=OneBotBridge(store,cfg,chat);sent=[]
    class Socket:
        async def send_json(self,action):
            sent.append(action)
            bridge._pending[action['echo']][1].set_result({'status':'ok','retcode':0,'data':{'message_id':42}})
    bridge._socket=Socket()
    metadata={'sticker':{'id':item['id'],'version':1}}
    assert await bridge._emit('qq:group:1','',metadata)
    segment=sent[0]['params']['message'][0]
    assert segment['type']=='image' and segment['data']['file'].startswith('base64://')
    assert bridge._outgoing['42']=='qq:group:1'
    engine.update(item['id'],{'enabled':False})
    assert not await bridge._emit('qq:group:1','',metadata) and len(sent)==1


@pytest.mark.anyio
async def test_flow_sends_one_sticker_only_after_confirmed_text(library):
    store,cfg,model,engine=library
    await collect(store,engine)
    social=SocialEngine(store,cfg,None)
    class Chat:
        async def observe(self,s,u,t,images,metadata):return store.add_message(s,u,'user',t,**metadata)
        async def respond(self,s,u,t,images,metadata):
            record=store.add_message(s,'assistant','assistant','谢谢你！',source_ids=metadata['source_ids'])
            return {'reply':'谢谢你！','message_id':record['id']}
    chat=Chat();chat.social=social;chat.stickers=engine
    sent=[]
    async def emit(scope,text,metadata):sent.append((text,metadata));return True
    flow=ConversationFlow(store,cfg,chat,emit,lambda s,u:True)
    await flow.handle('qq:group:1','123','谢谢',[],{'at_bot':True},direct=True)
    assert len(sent)==2 and sent[0][0]=='谢谢你！' and sent[1][0]=='' and sent[1][1]['sticker']['id']
    assert sent[1][1]['output_part']['index']==2 and callable(sent[1][1]['sticker_current'])
    assert engine.page()['items'][0]['use_count']==1
    await flow.close()
    sent.clear()
    async def failed(scope,text,metadata):sent.append((text,metadata));return False
    flow=ConversationFlow(store,cfg,chat,failed,lambda s,u:True)
    await flow.handle('qq:group:1','123','再次谢谢',[],{'at_bot':True},direct=True)
    assert len(sent)==1 and 'sticker' not in sent[0][1]
    await flow.close()


@pytest.mark.anyio
async def test_official_sticker_upload_revalidates_before_sending(tmp_path):
    from shiori.official import OfficialBridge
    store=Store(tmp_path);cfg=Settings(tmp_path)
    cfg.update({'onebot':{'adapter':'official'},'qq_official':{'enabled':True,'app_id':'1234567890','app_secret':'isolated-fixture-secret'},
        'model':{'base_url':'https://fixture.invalid/v1','model':'test','vision':True},'stickers':{'collect_probability':1}})
    scope='qqofficial:group:group-openid';store.upsert_scope({'id':scope})
    social=SocialEngine(store,cfg,None);engine=StickerLibrary(store,cfg,Model(),social._allowed)
    await collect(store,engine,scope);item=engine.page()['items'][0];calls=[]
    class HTTP:
        async def request(self,route,json):
            calls.append(('upload',json));return {'file_info':'fixture-file-info'}
    class API:
        _http=HTTP()
        async def post_group_message(self,**kwargs):calls.append(('send',kwargs));return {'id':'fixture-sent'}
    bridge=OfficialBridge(store,cfg,SimpleNamespace(stickers=engine))
    bridge._running=True;bridge._connected=True;bridge._client=SimpleNamespace(api=API())
    assert await bridge._emit(scope,'',{'platform_message_id':'incoming','output_part':{'index':3},'sticker':{'id':item['id'],'version':1}})
    assert calls[0][0]=='upload' and calls[0][1]['srv_send_msg'] is False
    assert calls[1][1]['msg_type']==7 and calls[1][1]['msg_seq']==3 and calls[1][1]['media']=={'file_info':'fixture-file-info'}
    current=[True]
    async def replaced(route,json):current[0]=False;return {'file_info':'old-request'}
    bridge._client.api._http.request=replaced
    assert not await bridge._emit(scope,'',{'sticker':{'id':item['id'],'version':1},'sticker_current':lambda:current[0]})
    assert len(calls)==2
    async def revoke(route,json):engine.update(item['id'],{'enabled':False});return {'file_info':'stale'}
    bridge._client.api._http.request=revoke
    assert not await bridge._emit(scope,'',{'sticker':{'id':item['id'],'version':1}})
    assert len(calls)==2
    await engine.close();store.close()


@pytest.mark.anyio
async def test_group_planner_batches_multiple_members_and_direct_messages_bypass(tmp_path):
    store=Store(tmp_path);cfg=Settings(tmp_path)
    cfg.update({'onebot':{'enabled':True,'access_token':'fixture-token'},'planner':{'group_batch_ms':50,'group_batch_messages':3}})
    scope=store.upsert_scope({'id':'qq:group:1'})
    social=SocialEngine(store,cfg,None);plans=[]
    async def plan(scope,message,**kwargs):
        plans.append((message['user_id'],message['content'],len(store.list_messages(scope['id']))))
        return social._decision(scope['id'],message['user_id'],message,'skip','test observation')
    social.plan=plan
    class Chat:
        async def observe(self,scope_id,user_id,text,images,metadata):return store.add_message(scope_id,user_id,'user',text,**metadata)
    chat=Chat();chat.social=social
    async def emit(*args):return True
    flow=ConversationFlow(store,cfg,chat,emit,lambda s,u:True)
    for i in range(5):await flow.handle(scope['id'],str(i%2),f'普通话题{i}',[],{},wait=False)
    assert plans==[]
    await asyncio.gather(*flow._jobs)
    assert plans==[('0','普通话题4',5)]
    await flow.handle(scope['id'],'1','另一句普通话',[],{},wait=False)
    await flow.handle(scope['id'],'2','@机器人 问一个问题',[],{'at_bot':True},direct=True)
    await asyncio.gather(*flow._jobs)
    assert len(plans)==2 and plans[-1][0]=='2'
    await flow.close();store.close()


@pytest.mark.anyio
async def test_pending_batch_closes_without_late_planner_calls(tmp_path):
    store=Store(tmp_path);cfg=Settings(tmp_path)
    cfg.update({'onebot':{'enabled':True,'access_token':'fixture-token'},'planner':{'group_batch_ms':1000}})
    store.upsert_scope({'id':'qq:group:1'})
    social=SocialEngine(store,cfg,None)
    class Chat:
        async def observe(self,s,u,t,images,metadata):return store.add_message(s,u,'user',t,**metadata)
    chat=Chat();chat.social=social
    async def emit(*args):return True
    flow=ConversationFlow(store,cfg,chat,emit,lambda s,u:True)
    await flow.handle('qq:group:1','1','普通群消息',[],{},wait=False)
    await flow.close()
    assert flow._planner_batches=={} and social.status()=={}
    store.close()
