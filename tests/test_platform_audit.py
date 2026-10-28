"""Timezone, shared sticker budgets, immutable sandbox image, and SQL statistics."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import json
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from auth_helpers import password_login
from shiori.api import create_app
from shiori.db import Store
from shiori.provider import Provider
from shiori.settings import Settings
from shiori.statistics import snapshot
from shiori.timezones import config_zone, day_bounds
from test_stickers_and_batching import library, collect


@pytest.mark.parametrize('name,hours', [('Asia/Shanghai',24),('UTC',24),('America/New_York',23)])
def test_calendar_day_bounds_follow_configured_timezone_and_dst(name,hours):
    config={'time':{'timezone':name}}
    start,end=day_bounds(config,now=datetime(2026,3,8,12,tzinfo=timezone.utc))
    assert (datetime.fromisoformat(end)-datetime.fromisoformat(start)).total_seconds()==hours*3600
    assert str(config_zone(config))==name


@pytest.mark.parametrize('value',['Not/A_Zone','../zone','',None,True,123])
def test_invalid_timezone_never_replaces_saved_settings(tmp_path,value):
    settings=Settings(tmp_path)
    with pytest.raises(ValueError,match='时区'):settings.update({'time':{'timezone':value}})
    assert settings.get()['time']['timezone']=='Asia/Shanghai'


def test_store_and_planner_share_the_configured_day(tmp_path,monkeypatch):
    store=Store(tmp_path);settings=Settings(tmp_path);provider=Provider(settings,store)
    class Clock(datetime):
        @classmethod
        def now(cls,tz=None):return datetime(2026,10,20,12,tzinfo=timezone.utc).astimezone(tz)
    monkeypatch.setattr('shiori.timezones.datetime',Clock)
    for stamp,tokens in [('2026-10-20T02:00:00.000000Z',10),('2026-10-20T16:00:00.000000Z',20)]:
        store.record_usage({'created_at':stamp,'scope_id':'web:default','purpose':'planner','input_tokens':tokens,'output_tokens':0,'cost':0})
    assert store.usage_totals(day='2026-10-20')['tokens']==10
    assert provider.planner_totals('web:default')['tokens']==10
    settings.update({'time':{'timezone':'America/New_York'}})
    assert store.usage_totals(day='2026-10-20')['tokens']==20
    assert provider.planner_totals('web:default')['tokens']==20
    store.close()


def test_vision_and_selection_share_one_budget_without_repeating_calls(library):
    store,settings,model,engine=library
    settings.update({'stickers':{'daily_model_calls':1}})
    asyncio.run(collect(store,engine))
    assert engine.quota_status()['vision']==1
    assert asyncio.run(engine.choose('qq:group:1','123','谢谢','谢谢你的帮助',lambda:True)) is None
    assert len(model.calls)==1 and engine.quota_status()['selection']==0
    settings.update({'stickers':{'daily_model_calls':2}})
    asyncio.run(engine.choose('qq:group:1','123','谢谢','谢谢你的帮助',lambda:True))
    quota=engine.quota_status()
    assert quota['vision']==quota['selection']==1 and quota['total']==2
    assert engine.page()['quota']['total']==2


def test_shared_quota_is_atomic_under_parallel_attempts(library):
    _,settings,_,engine=library
    settings.update({'stickers':{'daily_model_calls':5}})
    with ThreadPoolExecutor(max_workers=8) as workers:
        claims=list(workers.map(lambda _:engine._claim_call('selection'),range(40)))
    assert sum(item is not None for item in claims)==5
    assert engine.quota_status()['total']==5


def test_fixed_sandbox_digest_is_checked_and_executes_an_immutable_reference(monkeypatch):
    from shiori import sandbox
    monkeypatch.setattr(sandbox,'docker_prefix',lambda:['docker','--host','local-test-engine'])
    seen=[];valid=True
    def run(command,**kwargs):
        seen.append(command)
        if 'info' in command:return SimpleNamespace(returncode=0,stdout='linux\n')
        return SimpleNamespace(returncode=0,stdout=json.dumps([{'RepoDigests':['python@'+(sandbox.IMAGE_DIGEST if valid else 'sha256:'+'0'*64)]}]).encode())
    monkeypatch.setattr(sandbox.subprocess,'run',run)
    assert sandbox.availability()['digest_verified']
    assert sandbox.IMAGE in seen[-1] and '@sha256:' in sandbox.IMAGE
    valid=False
    assert not sandbox.availability()['available']


def test_workspace_comma_is_rejected_when_docker_settings_are_saved(tmp_path):
    settings=Settings(tmp_path)
    with pytest.raises(ValueError,match='逗号'):settings.update({'computer':{'workspace':str(tmp_path/'a,b')}})
    settings.update({'computer':{'runtime':'local','workspace':str(tmp_path/'a,b')}})
    assert settings.get()['computer']['workspace'].endswith('a,b')


def test_statistics_use_bounded_sql_and_do_not_invent_missing_latency(tmp_path,monkeypatch):
    store=Store(tmp_path);settings=Settings(tmp_path)
    for index,scope in enumerate(['web:default','qq:private:123']):
        item=store.add_message(scope,'123','user','hello')
        with store._conn:store._conn.execute('UPDATE messages SET created_at=? WHERE id=?',('2026-10-20T02:00:00.000000Z',item['id']))
    store.record_usage({'created_at':'2026-10-20T02:00:00.000000Z','scope_id':'web:default','model':'<script>model</script>',
        'input_tokens':20,'output_tokens':10,'cost':0,'cost_known':False,'estimated':True})
    monkeypatch.setattr(store,'list_messages',lambda *a,**kw:pytest.fail('statistics must not deserialize all messages'))
    data=snapshot(store,settings,1,now=datetime(2026,10,20,12,tzinfo=timezone.utc))
    assert data['messages_total']==data['period_messages']==2
    assert {row['platform'] for row in data['platforms']}=={'web','onebot'}
    assert data['usage']['tokens']==30 and data['usage']['unknown_price_calls']==1
    assert data['usage']['average_response_ms'] is None and data['usage']['ttft_ms'] is None
    assert len(data['trend'])==25 and sum(row['messages'] for row in data['trend'])==2
    store.close()


def test_statistics_require_authentication_and_validate_range(tmp_path):
    app=create_app(tmp_path,start_workers=False)
    with TestClient(app) as client:
        assert client.get('/api/statistics').status_code==401
        password_login(client,app)
        result=client.get('/api/statistics?days=3')
        assert result.status_code==200 and result.json()['online_platforms']==1
        assert result.json()['process']['uptime_seconds']>=0
        assert client.get('/api/statistics?days=90').status_code==400
