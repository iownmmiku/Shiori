import asyncio
from unittest.mock import Mock

from fastapi.testclient import TestClient

from auth_helpers import password_login
from shiori.api import create_app
from shiori.context import ContextEngine
from shiori.db import Store
from shiori.memory import MemoryEngine, _TermCache


def seed(store, count=240):
    return [store.create_memory({'scope_id':'web:default','user_id':'owner','kind':'fact',
        'content':f'第{i}条笔记，喜欢音乐与抹茶。','status':'active'}) for i in range(count)]


def test_paged_api_is_bounded_and_preserves_legacy_list_and_search(tmp_path, monkeypatch):
    app=create_app(tmp_path,start_workers=False)
    seed(app.state.store)
    with TestClient(app) as client:
        password_login(client,app)
        original=client.get('/api/memories').json()
        assert isinstance(original,list) and len(original)==240
        monkeypatch.setattr(app.state.store,'list_memories',lambda *a,**k: (_ for _ in ()).throw(AssertionError('unbounded list used')))
        page=client.get('/api/memories?scope_id=web:default&limit=50&offset=50').json()
        assert len(page['items'])==50 and page['total']==240 and page['next_offset']==100 and page['has_more']
        assert [x['id'] for x in page['items']]==[x['id'] for x in original[50:100]]
        found=client.get('/api/memories?scope_id=web:default&limit=50&q=第239条').json()
        assert found['total']==1 and found['items'][0]['content'].startswith('第239条')
        assert client.get('/api/memories?limit=50&offset=-1').status_code==400


def test_blocked_sources_and_review_counts_do_not_decode_memory_bodies(tmp_path, monkeypatch):
    store=Store(tmp_path)
    item=seed(store,120)[0]
    store.update_memory(item['id'],{'blocked_source_ids':['old-message']})
    store.delete_memory(item['id'])
    monkeypatch.setattr(store,'list_memories',lambda *a,**k: (_ for _ in ()).throw(AssertionError('full memory load')))
    assert store.blocked_memory_sources('web:default')=={'old-message'}
    assert store.memory_review_counts()['active']==119
    assert store.memory_page('web:default',limit=50)['total']==119
    store.close()


def test_history_checks_only_referenced_memories_and_their_dependencies(tmp_path, monkeypatch):
    store=Store(tmp_path)
    item=seed(store,240)[0]
    message=store.add_message('web:default','assistant','assistant','记得你喜欢抹茶',memory_versions={item['id']:1})
    real=store.visible_memories
    visited=[]
    def bounded(*args,**kwargs):
        assert kwargs.get('record_ids') is not None
        visited.append(set(kwargs['record_ids']))
        return real(*args,**kwargs)
    monkeypatch.setattr(store,'visible_memories',bounded)
    engine=ContextEngine(store,{'context':{'enabled':False}},None)
    assert engine._manifest('web:default','owner',[message])[0]
    assert visited==[{item['id']}]
    store.delete_memory(item['id'])
    assert engine._manifest('web:default','owner',[message])[0]==[]
    store.close()


def test_token_cache_is_bounded_and_never_caches_query_scores_or_authority(tmp_path):
    cache=_TermCache(max_bytes=8000)
    for i in range(100): cache.terms(f'第{i}条笔记，喜欢咖啡与音乐')
    assert cache.bytes<=cache.max_bytes and len(cache.items)<100
    store=Store(tmp_path)
    item=seed(store,20)[0]
    engine=MemoryEngine(store,{})
    first=asyncio.run(engine.recall('web:default','owner','抹茶'))
    assert first
    cache_count=len(engine._term_cache.items)
    asyncio.run(engine.recall('web:default','owner','音乐'))
    assert len(engine._term_cache.items)==cache_count
    store.delete_memory(item['id'])
    assert item['id'] not in {x['id'] for x in asyncio.run(engine.recall('web:default','owner','抹茶',30))}
    engine.close()
    assert engine._term_cache.bytes==0
    store.close()


def test_final_worker_boundary_rechecks_revocation(tmp_path,monkeypatch):
    store=Store(tmp_path)
    item=seed(store,1)[0]
    engine=MemoryEngine(store,{})
    def related(*args):
        store.delete_memory(item['id'])
        return []
    monkeypatch.setattr(engine,'_related',related)
    assert asyncio.run(engine.recall('web:default','owner','抹茶'))==[]
    engine.close();store.close()


def test_scope_permission_callback_is_per_origin_not_per_record(tmp_path):
    store=Store(tmp_path)
    seed(store,240)
    allowed=Mock(return_value=True)
    assert len(store.visible_memories('web:default','owner',scope_allowed=allowed))==240
    assert allowed.call_count==1
    assert store.visible_memories('web:default','owner',scope_allowed=lambda origin:False)==[]
    store.close()


def test_planner_preview_uses_cache_without_embedding_and_rechecks_worker_race(tmp_path, monkeypatch):
    store=Store(tmp_path)
    item=seed(store,20)[0]
    embed=Mock(side_effect=AssertionError('Planner must not call the embedding service'))
    engine=MemoryEngine(store,{'memory':{'semantic_enabled':True}},embedding_fn=embed)
    assert asyncio.run(engine.recall_local('web:default','owner','抹茶'))
    assert engine._term_cache.bytes and not embed.called
    original=engine._rank_async
    async def race(*args):
        docs=await original(*args)
        store.delete_memory(item['id'])
        return docs
    monkeypatch.setattr(engine,'_rank_async',race)
    assert item['id'] not in {x['id'] for x in asyncio.run(engine.recall_local('web:default','owner','抹茶',30))}
    engine.close();store.close()
