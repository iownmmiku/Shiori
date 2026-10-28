import asyncio
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from auth_helpers import password_login
from shiori import tools
from shiori.api import create_app
from shiori.distillation import DiscoveryError, DistillationService


@pytest.fixture
def anyio_backend():
    return 'asyncio'


@pytest.mark.anyio
async def test_search_html_failure_falls_back_to_real_feed_urls(monkeypatch):
    calls = []

    async def read(url):
        calls.append(url)
        if 'format=rss' in url:
            return {'url': url, 'text': '角色', 'feed_items': [
                {'url': 'https://wiki.example/character'}, {'url': 'http://127.0.0.1/private'},
                {'url': 'https://wiki.example/character'}]}
        raise ValueError('网页返回 HTTP 403')

    monkeypatch.setattr(tools, 'read_public', read)
    result = await tools.search_public('角色 作品')
    assert len(calls) == 2
    assert result['channel'] == 'bing_rss' and result['links'] == ['https://wiki.example/character']
    assert result['failures'] == [{'channel': 'bing_html', 'status': 'unavailable'}]


@pytest.mark.anyio
async def test_another_search_engine_decodes_redirects_but_not_private_targets(monkeypatch):
    from urllib.parse import quote

    async def read(url):
        if 'duckduckgo' not in url:
            raise ValueError('网页返回 HTTP 503')
        return {'url': url, 'text': '有证据的公开搜索结果。' * 20, 'search_links': [
            'https://duckduckgo.com/l/?uddg=' + quote('https://docs.example/角色', safe=''),
            'https://duckduckgo.com/l/?uddg=' + quote('http://192.168.1.1/private', safe=''),
            'https://duckduckgo.com/settings']}

    monkeypatch.setattr(tools, 'read_public', read)
    result = await tools.search_public('角色 作品')
    assert result['channel'] == 'duckduckgo_html'
    assert result['links'] == ['https://docs.example/角色']


def test_search_headings_survive_large_navigation_and_prefer_result_links():
    parser = tools._TextParser()
    parser.feed('<a class href="/nav">导航</a>' * 140 + '<h2><a href="https://wiki.example/character">角色</a></h2>'
                '<a class="result__a" href="https://official.example/character">角色资料</a>')
    assert len(parser.links) == 120
    assert parser.search_links == ['https://wiki.example/character', 'https://official.example/character']


@pytest.mark.anyio
async def test_inaccessible_wikis_cannot_starve_other_search_results(tmp_path):
    async def search(query):
        if 'site:' in query:
            return {'links': ['https://blocked.example/' + str(i) for i in range(6)]}
        return {'links': ['https://official.example/character', 'https://other.example/character']}

    active = peak = 0

    async def read(url):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        try:
            await asyncio.sleep(.005)
            if 'blocked' in url:
                raise ValueError('网页返回 HTTP 403')
            return {'url': url, 'title': '角色资料', 'text': '角色是作品中的人物。她会先看资料再行动，面对不确定性会坦率说明。' * 3}
        finally:
            active -= 1

    model = AsyncMock()
    service = DistillationService(model, tmp_path, search_fn=search, read_fn=read)
    result = await service.discover({'work': '作品', 'character': '角色'})
    assert len(result['candidates']) == 2
    assert len(result['failures']) == 2 and peak <= 3
    assert all(item['officiality'] == 'not_verified' for item in result['candidates'])
    model.complete.assert_not_called()


@pytest.mark.anyio
async def test_only_work_name_is_not_enough_to_create_a_character_candidate(tmp_path):
    async def read(url):
        return {'url': url, 'title': '作品介绍', 'text': '作品设定介绍，但没有任何目标人物资料。' * 10}

    service = DistillationService(AsyncMock(), tmp_path, read_fn=read)
    with pytest.raises(DiscoveryError, match='未匹配角色名'):
        await service.discover({'work': '作品', 'character': '阿米娅', 'source_urls': ['https://wiki.example/work']})


def test_search_failure_returns_actionable_reason_and_records_diagnostics(tmp_path):
    app = create_app(tmp_path, start_workers=False)

    async def search(query):
        raise ValueError('网页返回 HTTP 403')

    app.state.distillation.search_fn = search
    with TestClient(app) as client:
        password_login(client, app)
        result = client.post('/api/persona/distill/character/search', json={'work': '作品', 'character': '角色'})
        assert result.status_code == 400
        assert 'HTTP 403' in result.json()['detail'] and '资料链接' in result.json()['detail']
        events = [row for row in app.state.store.list_events() if row['kind'] == 'distillation_discovery']
        assert events and len(events[-1]['details']['failures']) == 3
