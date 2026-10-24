import asyncio
from copy import deepcopy
from datetime import datetime,timedelta,timezone
import json
from types import SimpleNamespace
import pytest
from fastapi.testclient import TestClient
from mikubot.api import create_app
from mikubot.chat import ChatService
from mikubot.db import Store
from mikubot.settings import Settings,DEFAULTS
from mikubot.memory import _time_window
from mikubot.learning_dashboard import snapshot
from mikubot.delivery import DeliveryLedger

@pytest.fixture
def anyio_backend():return 'asyncio'

class Model:
    async def complete(self,messages,**kwargs):
        data=json.loads(messages[-1]['content'])
        return {'content':json.dumps([{'kind':'event','subject':'42','property':'','content':'用户昨晚和机器人讨论了番剧。',
               'confidence':.96,'source_ids':[data[0]['id']]}]),'usage':{}}

@pytest.mark.anyio
async def test_short_chat_extracts_once_and_cursor_survives_restart(tmp_path):
    s=Settings(tmp_path);store=Store(tmp_path);chat=ChatService(store,s,Model())
    message=store.add_message('web:default','42','user','昨晚和你讨论了番剧，很开心。')
    try:
        await chat._extract('web:default','42',automatic=True)
        records=store.list_memories(scope_id='web:default')
        assert len(records)==1 and records[0]['status']=='active'
        from mikubot.timezones import message_anchor, config_zone, utc_timestamp
        expected = _time_window('昨晚', message_anchor(message), config_zone(s))
        assert records[0]['event_time_known'] is True
        assert records[0]['event_time_start'] == utc_timestamp(expected[0])
        assert records[0]['event_time_end'] == utc_timestamp(expected[1])
        assert records[0]['reported_at'] == message['received_at']
        assert 'occurred_at' not in records[0]
        assert chat.memory_lifecycle.pending('web:default','42')==[]
        await chat.close();store.close()
        store=Store(tmp_path);chat=ChatService(store,s,Model())
        assert chat.memory_lifecycle.pending('web:default','42')==[]
    finally:await chat.close();store.close()

@pytest.mark.anyio
async def test_manual_review_and_conflicts_remain_pending(tmp_path):
    s=Settings(tmp_path);s.update({'memory':{'review_mode':'manual'}})
    store=Store(tmp_path);chat=ChatService(store,s,Model())
    store.add_message('web:default','42','user','昨晚一起聊了番剧。')
    try:
        await chat._extract('web:default','42',automatic=True)
        assert store.list_memories(scope_id='web:default')[0]['status']=='pending'
    finally:await chat.close();store.close()

def test_group_word_sharing_never_exposes_private_words_and_can_be_disabled(tmp_path):
    s=Settings(tmp_path);store=Store(tmp_path);store.configure_timezone(s)
    for scope in ('qq:group:1','qq:group:2','qq:private:3'):store.upsert_scope({'id':scope})
    shared=store.create_jargon({'scope_id':'qq:group:1','term':'咕咕','meaning':'推迟安排','status':'approved'})
    store.create_jargon({'scope_id':'qq:private:3','term':'秘密词','meaning':'私人内容','status':'approved'})
    assert [r['id'] for r in store.match_jargon('qq:group:2','咕咕秘密词')]==[shared['id']]
    assert not store.match_jargon('qq:private:3','咕咕')
    s.update({'language':{'group_isolation':True}})
    assert not store.match_jargon('qq:group:2','咕咕')
    assert store.match_jargon('qq:group:1','咕咕')
    store.close()

def test_last_night_uses_configured_calendar():
    from zoneinfo import ZoneInfo
    start,end=_time_window('还记得昨晚的事吗',datetime(2026,10,23,2,tzinfo=timezone.utc),ZoneInfo('America/New_York'))
    assert start.astimezone(ZoneInfo('America/New_York')).hour==18
    assert end-start==timedelta(hours=12)

def test_dashboard_uses_bounded_rows_and_counts_pending(tmp_path):
    s=Settings(tmp_path);store=Store(tmp_path)
    for i in range(12):store.create_memory({'content':str(i),'status':'pending' if i<3 else 'active'})
    data=snapshot(store,s,'memory')
    assert data['total']==12 and data['counts']['pending']==3
    assert len(data['recent'])==8 and len(data['trend'])==7
    store.close()

@pytest.mark.anyio
async def test_qq_image_bytes_reach_model_instead_of_expiring_cdn_url(tmp_path,monkeypatch):
    import base64
    from mikubot.onebot import OneBotBridge
    raw=base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jRZkAAAAASUVORK5CYII=')
    calls=[]
    class Vision:
        async def complete(self,messages,**kwargs):
            calls.append(messages)
            return {'content':'看到了图片。','message':{'role':'assistant','content':'看到了图片。'},'tool_calls':[],'usage':{}}
    s=Settings(tmp_path);s.update({'model':{'base_url':'https://fixture.invalid/v1','model':'vision','vision':True},'onebot':{'enabled':True,'chat_policy':'blacklist','access_token':'fixture-onebot-token'},'stickers':{'enabled':False}})
    store=Store(tmp_path);store.upsert_scope({'id':'qq:private:42','auto_memory':False,'always_reply':True})
    chat=ChatService(store,s,Vision());bridge=OneBotBridge(store,s,chat)
    import mikubot.stickers as stickers
    monkeypatch.setattr(stickers,'download_image',lambda url,**kwargs:(raw,'image/png'))
    class Socket:
        async def send_json(self,action):
            assert action['action']=='get_image'
            bridge._pending[action['echo']][1].set_result({'status':'ok','data':{'url':'https://gchat.qpic.cn/fixture.png'}})
    bridge._socket=Socket()
    async def send(*args,**kwargs):return True
    bridge.send=send
    try:
        await bridge.handle_event({'post_type':'message','message_type':'private','self_id':99,'user_id':42,'message_id':1,
            'message':[{'type':'text','data':{'text':'看看这张图片'}},{'type':'image','data':{'file':'fixture-image.png'}}]})
        assert calls
        request=json.dumps(calls[-1])
        assert 'data:image/png;base64,' in request and 'https://gchat.qpic.cn/fixture.png' not in request
    finally:
        bridge._socket=None
        await bridge.stop();await chat.close();store.close()

@pytest.mark.anyio
async def test_resume_skips_unknown_and_confirmed_parts_and_checks_authority(tmp_path):
    from mikubot.delivery import install_delivery
    from fastapi import FastAPI
    store=Store(tmp_path);settings=Settings(tmp_path)
    settings.update({'output':{'interval_ms':0}})
    source=store.add_message('web:default','42','user','问题')
    assistant=store.add_message('web:default','assistant','assistant','整段回复')
    from mikubot.settings import resolved_identity
    flow=SimpleNamespace(settings=settings,store=store,chat=SimpleNamespace(social=SimpleNamespace(_allowed=lambda s:True,_blocked_sources=lambda s:set())),allowed=lambda *a:True,
        _output_locks={'web:default':asyncio.Lock()})
    sent=[]
    async def emit(scope,text,metadata):sent.append(text);return True
    flow.emit=emit
    ledger=DeliveryLedger(store)
    record=ledger.create('web:default','42',['第一','第二','第三'],{}, {'logical_message_id':assistant['id'],'entries':[{'message':source}],'identity':resolved_identity(settings.get(),store.get_scope('web:default'))})
    record['parts'][0]['status']='confirmed';record['parts'][1]['status']='unknown';ledger.save(record)
    app=FastAPI();install_delivery(app,store,SimpleNamespace(_flow=flow),None)
    import httpx
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test') as client:
        assert (await client.post('/api/delivery/'+record['id']+'/resume',json={'confirm':True})).json()['sent']==1
        assert sent==['第三']
        assert (await client.post('/api/delivery/'+record['id']+'/resume',json={'confirm':True})).json()['sent']==0
    store.close()
