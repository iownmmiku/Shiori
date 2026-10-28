import asyncio
import json

import pytest
from fastapi.testclient import TestClient

from shiori import __version__
from shiori.api import create_app
from qunyu.db import Store
from shiori.distillation import DistillationService
from auth_helpers import password_login


def legacy(store, record, **changes):
    raw = dict(record, **changes)
    raw.pop('pinned', None)
    with store._conn:
        store._conn.execute('UPDATE memories SET data=? WHERE id=?', (json.dumps(raw), record['id']))


def test_old_profile_and_bounded_preferences(tmp_path, monkeypatch):
    store = Store(tmp_path)
    item = store.create_memory({'scope_id':'web:a','user_id':'me','kind':'preference','content':'喜欢音乐'})
    legacy(store, item, importance=None, updated_at=None, confidence='bad')
    assert store.profile('web:a','me')['preferences'][0]['pinned'] is False
    monkeypatch.setattr(store, 'profile', lambda *_: pytest.fail('chat must not build full profile'))
    assert store.profile_preferences('web:a','me')[0]['content'] == '喜欢音乐'
    assert store.profile_preferences('web:a','other') == []
    store.close()


def test_profile_sql_paging_never_loads_whole_projection(tmp_path, monkeypatch):
    store=Store(tmp_path)
    for index in range(121):
        store.create_memory({'scope_id':'web:default','user_id':'owner','kind':'preference',
                             'content':f'分页偏好{index}','importance':index/121})
    original=store._allowed_memories
    counts=[]
    def bounded(*args,**kwargs):
        assert kwargs.get('roots') is not None
        counts.append(len(kwargs['roots']))
        return original(*args,**kwargs)
    monkeypatch.setattr(store,'_allowed_memories',bounded)
    first=store.profile('web:default','owner',include_summary=False)
    second=store.profile('web:default','owner',offset=first['next_offset'])
    third=store.profile('web:default','owner',offset=second['next_offset'])
    assert first['summary']=='' and first['has_more'] and not third['has_more']
    ids=[x['id'] for page in (first,second,third) for x in page['memories']]
    assert len(ids)==len(set(ids))==121 and max(counts)==50
    assert [len(page['memories']) for page in (first,second,third)]==[50,50,21]
    with pytest.raises(ValueError):store.profile('web:default','owner',offset=-1)
    store.close()


def test_ui_cache_refresh_is_independent_of_public_version(tmp_path):
    app=create_app(tmp_path,start_workers=False)
    with TestClient(app) as client:
        for path in ('/','/index.html'):
            response=client.get(path)
            assert response.status_code==200
            assert f'?v={__version__}&amp;rev=' in response.text
            assert response.headers['cache-control']=='no-store'
        asset=client.get(f'/static/app.js?v={__version__}')
        assert 'must-revalidate' in asset.headers['cache-control']
        assert 'profile-page' in asset.text


def test_preference_dependency_and_sharing_boundaries(tmp_path):
    store = Store(tmp_path)
    for key in ('qq:group:1','qq:group:2'):
        store.upsert_scope({'id':key,'memory_space':'shared'})
    source = store.create_memory({'scope_id':'qq:group:1','user_id':'me','kind':'fact','content':'依据','visibility':'group','shareable':True})
    shared = store.create_memory({'scope_id':'qq:group:1','user_id':'me','kind':'preference','content':'共享偏好','visibility':'group','shareable':True,'dependencies':{source['id']:source['version']}})
    store.create_memory({'scope_id':'qq:group:1','user_id':'me','kind':'preference','content':'未授权','visibility':'group'})
    assert [r['id'] for r in store.profile_preferences('qq:group:2','me')] == [shared['id']]
    store.delete_memory(source['id'])
    assert store.profile_preferences('qq:group:2','me') == []
    store.close()


def test_direct_generation_no_research_and_cancellable(tmp_path):
    class Model:
        async def complete(self, messages, **kwargs):
            assert kwargs['before_send']()
            assert '人格设计师' in messages[0]['content']
            return {'content':'温柔但有主见，喜欢天文。','usage':{'total_tokens':100}}
    async def forbidden(*_):
        pytest.fail('direct generation must not search or read the web')
    service = DistillationService(Model(), tmp_path, search_fn=forbidden, read_fn=forbidden)
    result = asyncio.run(service.generate({'mode':'generate','description':'喜欢天文的伙伴'}))
    assert result['preview_only'] and not result['applied'] and result['artifact'] is None
    with pytest.raises(ValueError):
        asyncio.run(service.generate({'mode':'generate','description':''}))
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(service.generate({'mode':'generate','description':'取消'}, cancel_check=lambda: False))


def test_profiles_inheritance_overrides_and_bulk_binding(tmp_path):
    app=create_app(tmp_path,start_workers=False)
    with TestClient(app) as client:
        password_login(client,app)
        persona=client.post('/api/personas',json={'name':'测试人格','system_prompt':'说话简短'}).json()
        config=client.get('/api/bot-profiles').json()
        config={k:config[k] for k in ('default_id','items')}
        config['items'][0]['values']['persona_id']=persona['id']
        assert client.put('/api/bot-profiles',json=config).status_code==200
        created=client.post('/api/scopes',json={'name':'新聊天'}).json()
        assert created['persona_id']==persona['id']
        key=created['id']
        patched=client.put('/api/scopes/'+key,json={'persona_id':'default'}).json()
        assert patched['persona_id']=='default' and 'persona_id' in patched['profile_overrides']
        assert client.post('/api/bot-profiles/bind',json={'scope_ids':[key],'profile_id':'default'}).status_code==200
        assert app.state.store.get_scope(key)['persona_id']==persona['id']
        assert client.post('/api/bot-profiles/bind',json={'scope_ids':[key,'missing'],'profile_id':'default'}).status_code==400
        assert client.put('/api/scopes/'+key,json={'profile_id':'missing'}).status_code==400
        assert app.state.store.get_scope(key)['persona_id']==persona['id']
        assert client.delete('/api/personas/'+persona['id']).status_code==200
        assert app.state.store.get_scope(key)['persona_id']=='default'


def test_exception_trace_is_redacted_before_persistence(tmp_path, caplog):
    app=create_app(tmp_path,start_workers=False)
    secret='test-secret-for-redaction'
    app.state.settings.update({'model':{'api_key':secret}})
    try:
        raise KeyError(secret)
    except KeyError as exc:
        event=app.state.store.add_exception('social_error','测试错误',exc,scope_id='web:default')
    details=event['details']
    assert details['error']=='KeyError' and details['error_id']
    assert 'test_exception_trace' in details['traceback']
    assert secret not in json.dumps(event) and secret not in caplog.text
    app.state.store.close()


def test_direct_generation_job_saves_only_on_apply(tmp_path, monkeypatch):
    app=create_app(tmp_path,start_workers=False)
    async def complete(messages, **kwargs):
        if kwargs.get('before_send'): assert kwargs['before_send']()
        return {'content':'喜欢天文，表达简短，不编造已经完成的事情。','usage':{'total_tokens':100}}
    monkeypatch.setattr(app.state.provider, 'complete', complete)
    with TestClient(app) as client:
        password_login(client,app)
        before=client.get('/api/personas').json()
        job=client.post('/api/persona/distill/start',json={'mode':'generate','description':'喜欢天文的伙伴','name':'星星'}).json()
        import time
        for _ in range(100):
            result=client.get('/api/jobs/'+job['id']).json()
            if result['status'] in ('completed','failed'): break
            time.sleep(.01)
        assert result['status']=='completed',result
        assert result['result']['mode']=='generate'
        assert client.get('/api/personas').json()==before
        assert client.post('/api/persona/distill/'+job['id']+'/trial',json={'text':'你好'}).status_code==200
        saved=client.post('/api/persona/distill/'+job['id']+'/apply',json={}).json()
        assert saved['name']=='星星'
        assert app.state.store.get_scope('web:default')['persona_id']=='default'


def test_qq_self_test_does_not_claim_offline_token_mismatch(tmp_path, monkeypatch):
    from shiori.qq_pairing import LocalQQPairing
    async def discover(self):
        return {'candidates':[{'adapter':'napcat','reachable':False,'message':'标准端口未响应'}]}
    monkeypatch.setattr(LocalQQPairing,'discover',discover)
    app=create_app(tmp_path,start_workers=False)
    with TestClient(app) as client:
        password_login(client,app)
        result=client.post('/api/qq/self-test').json()
        checks={row['id']:row for row in result['checks']}
        assert checks['authentication']['status']=='unknown'
        assert checks['echo']['status']=='unknown'
        assert checks['ingress']['status']=='unknown'
        assert '不证明真实 QQ 收发' in result['message']


def test_onebot_probe_is_read_only_and_cleans_pending(tmp_path):
    app=create_app(tmp_path,start_workers=False)
    bridge=app.state.onebot
    app.state.settings.update({'onebot':{'enabled':True,'access_token':'fixture-access-token'}})
    actions=[]
    class Socket:
        async def send_json(self, action):
            actions.append(action)
            bridge._pending[action['echo']][1].set_result({'status':'ok','retcode':0,'data':{'online':True}})
    bridge._socket=Socket()
    result=asyncio.run(bridge.probe())
    assert result['ok'] and result['online']
    assert [a['action'] for a in actions]==['get_status']
    assert not bridge._pending
    bridge._socket=None
    app.state.store.close()
