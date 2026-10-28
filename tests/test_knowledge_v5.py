import asyncio
from datetime import datetime, timedelta, timezone
import json

import pytest

from qunyu.db import Store
from shiori.knowledge import PersonaKnowledge, _now


@pytest.fixture
def anyio_backend():
    return 'asyncio'


@pytest.fixture
def store(tmp_path):
    instance = Store(tmp_path / 'knowledge')
    instance.save_persona({'id': 'interested', 'name': '兴趣伙伴', 'system_prompt': '温暖自然',
                           'interests': ['动画', '音乐'], 'exploration_enabled': True})
    yield instance
    instance.close()


def config(**values):
    return {'paused': False, 'exploration': {'enabled': True, 'daily_searches': 6, 'max_results': 4,
                                           'interval_hours': 24, 'valid_days': 90, **values}}


class Sources:
    def __init__(self):
        self.queries, self.urls = [], []

    async def search(self, query):
        self.queries.append(query)
        return {'links': ['https://news.example/animation', 'https://news.example/animation', 'file:///private']}

    async def read(self, url):
        self.urls.append(url)
        return {'title': '动画音乐新闻', 'url': url, 'text': '动画音乐的新消息：本月公开介绍了制作过程，关注音乐与画面的合作。' * 8,
                'published_at': _now().isoformat()}


class Provider:
    def __init__(self, callback=None):
        self.calls, self.callback = [], callback

    async def complete(self, messages, **kwargs):
        self.calls.append((messages, kwargs))
        if self.callback:
            self.callback()
        return {'content': '本月公开介绍了动画与音乐制作合作，具体播出时间原文未说明。公开笔记仅涵盖音乐与画面制作的协作环节，后续计划仍需核对来源。'}


def engine(store, cfg=None, provider=None, sources=None):
    sources = sources or Sources()
    service = PersonaKnowledge(store, cfg or config(), provider, sources.search, sources.read)
    if not service.status()['degraded']:
        service.configure_sources('interested', {'allowed_domains': ['news.example']})
    return service, sources


@pytest.mark.anyio
async def test_exploration_date_does_not_pass_chinese_to_locale_strftime(store, monkeypatch):
    class AsciiFormatDate(datetime):
        def __format__(self, spec):
            spec.encode('ascii')
            return super().__format__(spec)
    monkeypatch.setattr('shiori.knowledge._now', lambda: AsciiFormatDate(2026, 10, 19, tzinfo=timezone.utc))
    service, sources = engine(store, provider=Provider())
    await service.explore('interested')
    assert sources.queries and sources.queries[0].endswith('2026年10月')


@pytest.mark.anyio
async def test_public_exploration_is_sourced_and_not_user_memory(store):
    provider = Provider()
    service, sources = engine(store, provider=provider)
    store.add_message('web:default', 'owner', 'user', '我的私人地址是保密的')
    result = await service.explore('interested')
    assert result['created'] == 1 and len(service.list('interested', 'pending')) == 1
    item = result['items'][0]
    assert item['url'] == sources.urls[0] and item['retrieved_at'] and item['valid_until']
    assert item['method'] == 'model_summary' and provider.calls[0][1]['purpose'] == 'knowledge'
    assert '私人地址' not in json.dumps([messages for messages, _ in provider.calls], ensure_ascii=False)
    assert not store.list_memories('web:default')
    assert not service.context('interested', '动画音乐')
    service.approve(item['id'])
    assert service.context('interested', '动画音乐')[0]['id'] == item['id']


@pytest.mark.anyio
async def test_opt_ins_and_manual_budget_persist_across_restart(store):
    cfg = config(enabled=False, daily_searches=1)
    service, sources = engine(store, cfg)
    assert (await service.explore('interested'))['skipped'] and not sources.queries
    assert (await service.explore('interested', manual=True))['created'] == 1
    reboot, _ = engine(store, cfg, sources=sources)
    assert reboot.status()['searches_today'] == 1
    assert (await reboot.explore('interested', manual=True))['skipped']
    assert len(sources.queries) == 1


@pytest.mark.anyio
async def test_no_model_keeps_labelled_excerpt_and_deduplicates_url(store):
    service, sources = engine(store)
    first = await service.explore('interested', manual=True)
    second = await service.explore('interested', manual=True)
    assert first['items'][0]['method'] == 'source_excerpt'
    assert second['created'] == 0 and second['updated'] == 1
    assert first['items'][0]['id'] == second['items'][0]['id']
    assert len(service.list('interested')) == 1
    assert '动画' in sources.queries[0] and '音乐' in sources.queries[1]


@pytest.mark.anyio
async def test_search_failure_consumes_attempt_but_does_not_fabricate(store):
    async def fail(query):
        raise ValueError('private detail should not appear')
    service, sources = engine(store)
    service.search_fn = fail
    result = await service.explore('interested', manual=True)
    assert result['created'] == 0 and result['skipped']
    assert not service.list('interested') and service.status()['searches_today'] == 1
    assert 'private detail' not in json.dumps(result)


@pytest.mark.anyio
async def test_paused_blocks_manual_and_config_revocation_discards_inflight(store):
    cfg = config()
    service, sources = engine(store, cfg)
    cfg['paused'] = True
    assert (await service.explore('interested', manual=True))['skipped'] and not sources.queries
    cfg['paused'] = False
    original = sources.search
    async def revoke(query):
        result = await original(query)
        cfg['paused'] = True
        return result
    service.search_fn = revoke
    result = await service.explore('interested')
    assert result['skipped'] and not service.list('interested') and not sources.urls


@pytest.mark.anyio
async def test_interest_change_after_model_request_discards_summary(store):
    provider = Provider(lambda: store.save_persona({'id': 'interested', 'interests': ['园艺']}))
    service, _ = engine(store, provider=provider)
    result = await service.explore('interested')
    assert not result['created'] and not service.list('interested')


@pytest.mark.anyio
async def test_provider_rechecks_before_external_send_after_queue_wait(store):
    cfg = config()
    class QueuedProvider:
        sent = False
        async def complete(self, messages, **kwargs):
            cfg['paused'] = True
            if kwargs['before_send']():
                self.sent = True
            raise ValueError('已暂停')
    provider = QueuedProvider()
    service, _ = engine(store, cfg, provider)
    result = await service.explore('interested')
    assert not provider.sent and not result['created'] and not service.list('interested')


@pytest.mark.anyio
async def test_deleted_sources_not_revived_on_scheduled_exploration(store):
    service, sources = engine(store)
    result = await service.explore('interested')
    deleted = service.delete(result['items'][0]['id'])
    assert deleted['text'] == '' and not service.context('interested', '动画')
    assert (await service.explore('interested'))['created'] == 0
    assert len(sources.urls) == 1 and not service.list('interested')


def test_corrupt_file_degrades_without_overwriting_original(store):
    path = store.data_dir / 'persona_knowledge.json'
    path.write_text('{broken data', 'utf-8')
    service, _ = engine(store)
    assert service.status()['degraded'] and not service.status()['enabled']
    assert path.read_text('utf-8') == '{broken data'
    assert store.list_events()[0]['kind'] == 'knowledge_warning'
    with pytest.raises(ValueError, match='损坏'):
        service.export_state()


@pytest.mark.anyio
async def test_valid_export_keeps_budget_and_sources(store):
    service, _ = engine(store)
    await service.explore('interested', manual=True)
    data = json.loads(service.export_state())
    PersonaKnowledge.validate_state(data)
    assert data['budget']['searches'] == 1 and data['items'][0]['url']


@pytest.mark.anyio
async def test_long_schedule_interval_supported(store):
    service, sources = engine(store, config(interval_hours=720))
    await service.explore('interested')
    service._state['personas']['interested']['last_attempt_at'] = (_now() - timedelta(hours=200)).isoformat()
    await service.tick()
    assert len(sources.queries) == 1


@pytest.mark.anyio
async def test_schedule_runs_once_and_reboot_does_not_replay(store):
    service, sources = engine(store)
    await service.tick()
    await service.tick()
    reboot, _ = engine(store, sources=sources)
    await reboot.tick()
    assert len(sources.queries) == 1
    assert reboot.status()['personas']['interested']['last_success_at']


@pytest.mark.anyio
async def test_expired_knowledge_excluded_from_prompt_but_still_manageable(store):
    service, _ = engine(store)
    result = await service.explore('interested')
    service._state['items'][0]['valid_until'] = (_now() - timedelta(seconds=1)).isoformat()
    assert service.list('interested') and not service.context('interested', '动画音乐')


@pytest.mark.anyio
async def test_cjk_retrieval_uses_overlapping_words_inside_sentence(store):
    service, _ = engine(store)
    await service.explore('interested')
    assert service.context('interested', '请介绍音乐')


@pytest.mark.anyio
async def test_stop_revokes_manual_inflight_write(store):
    service, sources = engine(store)
    original = sources.read
    async def stop_read(url):
        result = await original(url)
        await service.stop()
        return result
    service.read_fn = stop_read
    assert not (await service.explore('interested', manual=True))['created']
    assert not service.list('interested')


def test_invalid_backup_dates_rejected():
    with pytest.raises(ValueError):
        PersonaKnowledge.validate_state({'version': 1, 'items': [{'status': 'active'}],
                                        'budget': {'day': '', 'searches': 0}, 'personas': {}})


@pytest.mark.parametrize('interest,title,body', [
    ('音乐', '本周音乐创作资讯', '乐队介绍了音乐制作中的编曲与录音流程。音乐创作团队同时发布了乐器录制的幕后说明。'),
    ('动画', '新番动画制作访谈', '制作方介绍动画分镜和背景绘制的合作过程。动画本次采用新的绘制技术，并计划公开幕后访谈。'),
    ('网络热梗', '网络流行语的最新用法', '网络热梗在不同社群有不同含义。本文解释网络流行语的来源，并提醒使用热梗时核对语境。'),
    ('日本动画制作', '日本动画制作流程介绍', '日本动画团队介绍制作环节。动画制作需要协调分镜、作画与声音，公开资料展示了制作过程。'),
    ('machine learning', 'Machine Learning research overview', 'Machine learning research covers models, training and evaluation. These machine learning methods are explained with careful experimental details.'),
    ('Japanese anime', 'Japanese anime production interview', 'Japanese anime teams discuss animation production and visual composition. This interview focuses on Japanese anime rather than unrelated entertainment.')
])
def test_topic_filter_accepts_substantive_short_long_and_english_topics(interest, title, body):
    from shiori.knowledge import assess_topic_relevance
    result = assess_topic_relevance(interest, title, body)
    assert result['relevant'], result
    assert result['matched_terms'] and result['reason']


@pytest.mark.parametrize('title,body', [
    ('4399游戏怎么用手柄', '请连接手柄并进入游戏设置，随后选择控制器映射。这里介绍的是游戏控制操作，与艺术资讯无关。'),
    ('本周音乐资讯', '音乐\n首页\n登录\n这里详细介绍游戏控制器的连接方法，包括硬件识别、驱动安装、按键映射以及常见故障排查。'),
    ('音乐会员账号密码大全', '音乐会员免费账号密码分享。账号：fixture-only-test 密码：fixture-only-password。音乐账号可以共享。'),
    ('免费账号共享', '这里提供免费账号和密码。音乐来源未标注，动画仅是导航标签。请使用这些账号和密码登录。')
])
def test_topic_filter_rejects_unrelated_title_only_navigation_and_credentials(title, body):
    from shiori.knowledge import assess_topic_relevance
    assert not assess_topic_relevance('音乐', title, body)['relevant']


def test_qualified_topic_needs_more_than_one_broad_keyword():
    from shiori.knowledge import assess_topic_relevance
    assert not assess_topic_relevance('Japanese anime', 'Anime news',
                                     'Anime news is updated here. Anime audiences read entertainment announcements. Anime streaming is discussed.')['relevant']


@pytest.mark.anyio
async def test_irrelevant_or_credential_search_results_never_reach_model_or_storage(store):
    sources = Sources()
    async def search(query):
        return {'links': ['https://junk.example/controller', 'https://junk.example/accounts']}
    async def read(url):
        if 'accounts' in url:
            return {'url': url, 'title': '音乐账号密码大全', 'text': '音乐账号密码共享。账号：fixture-test 密码：fixture-only。免费账号列表。' * 4}
        return {'url': url, 'title': '4399怎么连接手柄', 'text': '这篇资料介绍手柄的连接和按键设置。连接设备之后进入系统设置，调整控制器按键映射。' * 3}
    provider = Provider()
    service = PersonaKnowledge(store, config(), provider, search, read)
    store.save_persona({'id': 'interested', 'interests': ['音乐']})
    result = await service.explore('interested', manual=True)
    assert result['created'] == 0 and result['skipped'] and result['reason'] == 'no_relevant_sources'
    assert result['filtered'] == 2 and len(result['rejections']) == 2
    assert not service.list('interested') and not provider.calls
    assert 'fixture-only' not in service.path.read_text('utf-8')


@pytest.mark.anyio
async def test_off_topic_model_summary_falls_back_to_related_source_excerpt(store):
    class OffTopicProvider:
        async def complete(self, messages, **kwargs):
            return {'content': '这里介绍如何连接游戏手柄，使用按键映射完成控制器配置。请打开系统设置并查看对应驱动，随后按步骤进行操作。'}
    service, _ = engine(store, provider=OffTopicProvider())
    result = await service.explore('interested')
    assert result['items'][0]['method'] == 'source_excerpt'
    assert result['items'][0]['relevance']['relevant']
