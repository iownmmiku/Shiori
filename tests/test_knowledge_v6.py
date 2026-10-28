"""Authored public-source fixtures: no real web/model calls or truth claims."""
import copy
import json
from datetime import timedelta

import pytest

from qunyu.db import Store
from shiori.knowledge import PersonaKnowledge, _now


@pytest.fixture
def anyio_backend():
    return 'asyncio'


@pytest.fixture
def env(tmp_path):
    store = Store(tmp_path / 'knowledge-v6')
    store.save_persona({'id': 'music', 'name': '音乐伙伴', 'system_prompt': '温暖自然',
                        'interests': ['音乐'], 'exploration_enabled': True})
    cfg = {'paused': False, 'exploration': {'enabled': True, 'daily_searches': 12, 'valid_days': 90,
                                          'max_results': 4, 'interval_hours': 24}}
    async def search(query):
        return {'links': ['https://a.example/news']}
    async def read(url):
        return {'url': url, 'title': '音乐创作团队公开新专辑制作计划',
                'text': '音乐团队介绍新专辑的编曲制作过程。音乐作品的录音环节涉及乐器演奏与编曲合作。' * 3,
                'published_at': _now().isoformat()}
    engine = PersonaKnowledge(store, cfg, None, search, read)
    engine.configure_sources('music', {'allowed_domains': ['a.example', 'b.example']})
    yield store, cfg, engine, read
    store.close()


@pytest.mark.anyio
async def test_undated_sources_wait_for_review_and_edit_revokes_use(env):
    store, cfg, engine, read = env
    async def undated(url):
        record = await read(url)
        record.pop('published_at')
        return record
    engine.read_fn = undated
    item = (await engine.explore('music', manual=True))['items'][0]
    assert item['status'] == 'pending' and item['published_at'] == ''
    assert item['retrieved_at'] and not engine.context('music', '音乐')
    engine.approve(item['id'])
    assert engine.context('music', '音乐')
    changed = engine.edit(item['id'], {'text': '用户核对后的音乐制作笔记：音乐团队介绍编曲、乐器演奏和音乐录制过程。'})
    assert changed['status'] == 'pending' and not engine.context('music', '音乐')
    engine.reject(item['id'], '来源无法确认')
    assert not engine.list('music', 'pending') and engine.list('music', 'rejected')[0]['reason'] == '来源无法确认'
    assert (await engine.explore('music', manual=True))['created'] == 0
    engine.clear_blocked('music', item['id'])
    assert (await engine.explore('music', manual=True))['created'] == 1


@pytest.mark.anyio
async def test_old_dates_never_become_recent_when_retrieved(env):
    store, cfg, engine, read = env
    published = (_now() - timedelta(days=365)).isoformat()
    async def old(url):
        record = await read(url)
        return {**record, 'published_at': published, 'updated_at': _now().isoformat()}
    engine.read_fn = old
    item = (await engine.explore('music', manual=True))['items'][0]
    assert item['published_at'] == published and item['updated_at'] != published
    assert item['status'] == 'pending' and any('时效' in reason for reason in item['review_reasons'])
    assert not engine.context('music', '音乐')


@pytest.mark.anyio
async def test_deleted_tombstones_free_capacity_and_still_block_relearning(env, monkeypatch):
    import shiori.knowledge as module
    monkeypatch.setattr(module, 'MAX_ITEMS', 1)
    store, cfg, engine, read = env
    item = (await engine.explore('music', manual=True))['items'][0]
    engine.delete(item['id'])
    assert engine.status()['capacity_used'] == 0 and engine.status()['blocked_urls'] == 1
    result = await engine.explore('music', manual=True, urls=['https://b.example/new'])
    assert result['created'] == 1 and engine.status()['capacity_used'] == 1
    assert (await engine.explore('music', manual=True, urls=['https://a.example/news']))['created'] == 0
    state = json.loads(engine.export_state())
    assert state['version'] == 2 and len(state['items']) == 1 and len(state['blocked_urls']) == 1


@pytest.mark.anyio
async def test_v1_migration_keeps_url_block_and_review_unknown_date(env):
    store, cfg, engine, read = env
    item = (await engine.explore('music', manual=True))['items'][0]
    for key in ('published_at', 'updated_at', 'sources', 'event_dates', 'review_reasons', 'conflicts'):
        item.pop(key, None)
    removed = {**item, 'id': 'removed', 'url': 'https://a.example/removed', 'status': 'deleted', 'text': ''}
    legacy = {'version': 1, 'items': [item, removed], 'budget': {'day': '', 'searches': 0}, 'personas': {}}
    engine.path.write_text(json.dumps(legacy), 'utf-8')
    migrated = PersonaKnowledge(store, cfg, None)
    assert not migrated.status()['degraded'] and migrated.status()['capacity_used'] == 1
    assert migrated.list('music', 'pending')[0]['id'] == item['id']
    assert migrated.list('music', 'deleted')[0]['id'] == 'removed'
    assert json.loads(migrated.path.read_text('utf-8'))['version'] == 2


@pytest.mark.anyio
async def test_domain_rules_revoke_active_sources_and_filter_redirects(env):
    store, cfg, engine, read = env
    item = (await engine.explore('music', manual=True))['items'][0]
    assert engine.context('music', '音乐')
    engine.configure_sources('music', {'blocked_domains': ['a.example'], 'use_bing': False})
    assert not engine.context('music', '音乐') and not engine.list('music')
    with pytest.raises(ValueError, match='域名'):
        engine.approve(item['id'])
    engine.configure_sources('music', {'allowed_domains': ['b.example'], 'manual_urls': ['https://b.example/start']})
    async def redirect(url):
        return await read('https://a.example/redirected')
    engine.read_fn = redirect
    assert (await engine.explore('music', manual=True))['created'] == 0


@pytest.mark.anyio
async def test_persona_quota_interval_ttl_and_excluded_topics(env):
    store, cfg, engine, read = env
    store.save_persona({'id': 'music', 'exploration': {'daily_searches': 1, 'interval_hours': 48, 'valid_days': 7}})
    item = (await engine.explore('music', manual=True))['items'][0]
    lifetime = __import__('datetime').datetime.fromisoformat(item['valid_until']) - __import__('datetime').datetime.fromisoformat(item['retrieved_at'])
    assert lifetime == timedelta(days=7)
    assert (await engine.explore('music', manual=True))['skipped']
    status = engine.status()['personas']['music']
    assert status['remaining_searches'] == 0 and status['effective_config']['interval_hours'] == 48
    store.save_persona({'id': 'music', 'exploration': {'daily_searches': 10}, 'excluded_topics': ['专辑']})
    assert (await engine.explore('music', manual=True))['filtered'] == 1


@pytest.mark.anyio
async def test_cross_source_conflicting_event_dates_require_explicit_review(env):
    store, cfg, engine, read = env
    async def sources(url):
        item = await read(url)
        item['event_dates'] = [{'label': '音乐会', 'date': '2026-11-01' if 'a.example' in url else '2026-11-08'}]
        return item
    engine.read_fn = sources
    result = await engine.explore('music', manual=True, urls=['https://a.example/news', 'https://b.example/news'])
    assert result['pending'] == 2
    item = result['items'][0]
    assert item['corroboration'] == 'multiple_domains_candidate' and len(item['sources']) == 2
    assert item['conflicts'] and item['event_dates'][0]['confirmed'] is False
    with pytest.raises(ValueError, match='冲突'):
        engine.approve(item['id'])
    assert engine.approve(item['id'], accept_conflicts=True)['status'] == 'active'


@pytest.mark.anyio
async def test_source_rule_edit_during_fetch_discards_all_inflight_candidates(env):
    store, cfg, engine, read = env
    async def revoke(url):
        record = await read(url)
        engine.configure_sources('music', {'blocked_domains': ['a.example']})
        return record
    engine.read_fn = revoke
    assert (await engine.explore('music', manual=True))['created'] == 0
    assert not engine.list('music', '')


@pytest.mark.parametrize('patch', [
    {'use_bing': 'false'}, {'allowed_domains': ['localhost']}, {'blocked_domains': ['https://a.example']},
    {'rss_urls': ['http://127.0.0.1/feed']}, {'manual_urls': ['http://router.internal/']}, {'unexpected': True},
])
def test_source_rules_validate_public_hosts_and_types(env, patch):
    with pytest.raises(ValueError):
        env[2].configure_sources('music', patch)

@pytest.mark.anyio
async def test_weak_source_and_body_broadcast_dates_are_review_candidates(env):
    store, cfg, engine, read = env
    engine.configure_sources('music', {'allowed_domains': []})
    async def dated(url):
        item = await read(url)
        item['text'] += '音乐纪录片计划于2026年11月3日播出，音乐团队尚未公布全部阵容。'
        return item
    engine.read_fn = dated
    item = (await engine.explore('music', manual=True))['items'][0]
    assert item['source_quality'] == 'unreviewed_source' and item['status'] == 'pending'
    assert item['event_dates'][0]['source'] == 'text_date_candidate'
    assert item['event_dates'][0]['date'].startswith('2026-11-03')
    assert not engine.context('music', '音乐')


@pytest.mark.anyio
async def test_rss_discovery_still_reads_each_article_and_preserves_feed_date_uncertainty(env):
    store, cfg, engine, read = env
    engine.configure_sources('music', {'rss_urls': ['https://a.example/feed'], 'use_bing': False})
    calls = []
    async def source(url):
        calls.append(url)
        if url.endswith('/feed'):
            return {'url': url, 'feed_items': [{'url': 'https://a.example/article', 'published_at': _now().isoformat()}]}
        record = await read(url)
        record.pop('published_at')
        return record
    engine.read_fn = source
    item = (await engine.explore('music', manual=True))['items'][0]
    assert calls == ['https://a.example/feed', 'https://a.example/article']
    assert item['date_source'] == 'feed_metadata' and item['status'] == 'pending'


@pytest.mark.anyio
async def test_changed_publication_date_does_not_inherit_prior_approval(env):
    store, cfg, engine, read = env
    async def no_date(url):
        record = await read(url)
        record.pop('published_at')
        return record
    engine.read_fn = no_date
    item = (await engine.explore('music', manual=True))['items'][0]
    engine.approve(item['id'])
    engine.read_fn = read
    changed = (await engine.explore('music', manual=True))['items'][0]
    assert not changed.get('approved_at')
    assert changed['published_at'] != item['published_at']

@pytest.mark.anyio
async def test_background_refresh_preserves_user_edits_and_reviews_changed_source(env):
    store, cfg, engine, read = env
    item = (await engine.explore('music', manual=True))['items'][0]
    engine.edit(item['id'], {'text': '音乐团队已核对的制作笔记，保留用户自己的音乐资料整理。'})
    engine.approve(item['id'])
    refreshed = (await engine.explore('music', manual=True))['items'][0]
    assert refreshed['id'] == item['id'] and '用户自己' in refreshed['text']
    async def updated(url):
        item = await read(url)
        item['text'] += '音乐团队追加了新专辑的录音计划。'
        return item
    engine.read_fn = updated
    revision = (await engine.explore('music', manual=True))['items'][0]
    assert revision['id'] != item['id'] and revision['status'] == 'pending' and revision['revision_of'] == item['id']
    assert engine.list('music')[0]['id'] == item['id']
    engine.approve(revision['id'])
    assert [record['id'] for record in engine.list('music')] == [revision['id']]

@pytest.mark.anyio
async def test_deleting_a_revision_revokes_same_url_active_note_until_unblocked(env):
    store, cfg, engine, read = env
    item = (await engine.explore('music', manual=True))['items'][0]
    engine.edit(item['id'], {'text': '音乐用户编辑的笔记。'})
    engine.approve(item['id'])
    async def updated(url):
        item = await read(url)
        item['text'] += '音乐制作团队增加最新计划。'
        return item
    engine.read_fn = updated
    revision = (await engine.explore('music', manual=True))['items'][0]
    engine.delete(revision['id'])
    assert not engine.context('music', '音乐')
    with pytest.raises(ValueError, match='屏蔽'):
        engine.approve(item['id'])
    engine.clear_blocked('music', revision['id'])
    engine.approve(item['id'])
    assert engine.context('music', '音乐')
