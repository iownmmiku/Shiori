"""Public-source protocols are replaced; no real account or billed model."""
import asyncio
import copy
import json

import pytest

from shiori.character_catalog import CharacterCatalog
from shiori.distillation import DistillationService


@pytest.fixture
def anyio_backend():
    return 'asyncio'


@pytest.fixture
def catalog():
    calls = []
    details = {
        22520: {'id': 22520, 'name': '和泉 紗霧', 'summary': '和泉正宗的妹妹。面对陌生人时会躲回房间，借助绘画和熟人表达自己的想法。' * 5,
                'infobox': [{'key': '简体中文名', 'value': '和泉纱雾'}, {'key': '别名', 'value': [{'k': '罗马字', 'v': 'Izumi Sagiri'}]}]},
        22521: {'id': 22521, 'name': '和泉正宗', 'summary': '轻小说作家，面对困难先考虑家人。' * 6, 'infobox': []},
    }
    async def api(url, payload=None):
        calls.append((url, payload))
        if '/search/characters' in url:
            # Public search can return a character whose alias equals the work.
            return {'data': [{'id': 22520}]}
        if '/search/subjects' in url:
            return {'data': [{'id': 100, 'name': 'エロマンガ先生', 'name_cn': '埃罗芒阿老师'}]}
        if '/subjects/100/characters' in url:
            return [{'id': 22520}, {'id': 22521}]
        if url.endswith('/subjects'):
            return [{'name': 'エロマンガ先生', 'name_cn': '埃罗芒阿老师'}]
        return copy.deepcopy(details[int(url.rsplit('/', 1)[1])])
    async def unavailable(url):
        raise ValueError('网页返回 HTTP 403')
    return CharacterCatalog(json_fn=api, read_fn=unavailable), calls


@pytest.mark.anyio
@pytest.mark.parametrize('query', ['和泉纱雾', '和泉 紗霧', 'Izumi Sagiri'])
async def test_real_identity_aliases_use_core_api_without_search_engine(catalog, query):
    engine, calls = catalog
    result = await engine.lookup(query)
    assert len(result['characters']) == 1
    role = result['characters'][0]
    assert role['name'] == '和泉纱雾' and role['id'] == 'bgm-22520'
    assert '埃罗芒阿老师' in role['works']
    assert '和泉 紗霧' in role['aliases']
    assert all('bing.com' not in url for url, _ in calls)


@pytest.mark.anyio
async def test_work_only_search_returns_multiple_distinct_characters(catalog):
    engine, _ = catalog
    result = await engine.lookup('埃罗芒阿老师')
    assert {row['name'] for row in result['characters']} == {'和泉纱雾', '和泉正宗'}


@pytest.mark.anyio
async def test_optional_missing_infobox_does_not_break_character_search(catalog):
    engine, _ = catalog
    original = engine.json_fn
    async def missing(url, payload=None):
        result = await original(url, payload)
        if url.endswith('/characters/22520'):
            result['infobox'] = None
        return result
    engine.json_fn = missing
    result = await engine.lookup('和泉 紗霧')
    assert result['characters'][0]['id'] == 'bgm-22520'


@pytest.mark.anyio
async def test_work_filter_never_silently_substitutes_different_franchise(catalog):
    engine, _ = catalog
    async def empty(*args):
        return []
    engine._wiki = empty
    assert not (await engine.lookup('和泉纱雾', '完全不同的作品'))['characters']


@pytest.mark.anyio
async def test_core_api_failure_uses_actual_mediawiki_evidence():
    async def api(url, payload=None):
        if 'api.bgm.tv' in url:
            raise ValueError('资料接口返回 HTTP 503')
        if 'moegirl' in url:
            return {'query': {'pages': [{'pageid': 3, 'title': '测试角色', 'extract': '测试角色是测试作品中的人物。遇到困难先倾听别人的意见。' * 8}]}}
        return {'query': {'pages': []}}
    result = await CharacterCatalog(json_fn=api).lookup('测试角色')
    assert result['characters'][0]['source'] == '萌娘百科'
    assert result['characters'][0]['_page']['text'].startswith('测试角色')
    assert result['failures']


@pytest.mark.anyio
async def test_lookup_then_one_identity_confirmation_creates_complete_skill(catalog, tmp_path):
    core, _ = catalog
    calls = []
    class Model:
        async def complete(self, messages, **kwargs):
            calls.append(messages)
            assert kwargs['before_send']()
            return {'content': '遇到陌生人先观察，熟悉后通过绘画表达；证据 s1，资料不足保持未知。', 'usage': {'total_tokens': 80}}
    service = DistillationService(Model(), tmp_path, catalog=core)
    result = await service.lookup_characters({'query': '和泉纱雾'})
    assert not calls and '_page' not in result['characters'][0]
    assert not (service.root / 'artifacts').exists()
    payload = {'mode': 'character', 'lookup_id': result['lookup_id'], 'character_id': 'bgm-22520'}
    with pytest.raises(ValueError, match='先确认'):
        await service.generate(payload)
    with pytest.raises(ValueError, match='请选择本次检索'):
        await service.generate({**payload, 'confirmed': True, 'character_id': 'forged'})
    assert not calls
    generated = await service.generate({**payload, 'confirmed': True})
    assert len(calls) == 7 and generated['artifact'] and generated['prompt']
    assert generated['name'] == '和泉纱雾'
    assert any(s['url'] == 'https://bgm.tv/character/22520' and s['status'] == 'read' for s in generated['sources'])
    assert any(s['status'] == 'failed' for s in generated['sources'])
    assert generated['quality']['fact_verified'] is False


@pytest.mark.anyio
async def test_identity_and_source_checkpoint_resume_does_not_refetch(catalog, tmp_path):
    core, _ = catalog
    state = {}
    class Model:
        fail = True
        async def complete(self, messages, **kwargs):
            if self.fail:
                self.fail = False
                raise ValueError('fixture interruption')
            return {'content': '证据不足处写未知，保持角色的表达与关系边界。', 'usage': {'total_tokens': 20}}
    service = DistillationService(Model(), tmp_path, catalog=core)
    result = await service.lookup_characters({'query': '和泉纱雾'})
    payload = {'mode': 'character', 'lookup_id': result['lookup_id'], 'character_id': 'bgm-22520', 'confirmed': True}
    def save(**data):
        state.update(copy.deepcopy(data))
    with pytest.raises(ValueError, match='fixture interruption'):
        await service.generate(payload, checkpoint=save)
    async def must_not_refetch(role):
        raise AssertionError('Sources must come from the persisted checkpoint')
    core.research = must_not_refetch
    assert (await service.generate(payload, checkpoint=save, resume_state=state['distillation_cache']))['artifact']


def test_json_reader_rejects_private_dns_and_never_replays_redirect(monkeypatch):
    from shiori import tools
    with pytest.raises(ValueError):
        tools._request_public_json('https://127.0.0.1/private', {'keyword': 'private'})
    class Response:
        status = 302
    class Connection:
        closed = False
        def __init__(self, *args): pass
        def request(self, *args, **kwargs): pass
        def getresponse(self): return Response()
        def close(self): self.closed = True
    monkeypatch.setattr(tools, 'public_addresses', lambda url: (__import__('urllib.parse', fromlist=['urlsplit']).urlsplit(url), 'example.org', 443, ['8.8.8.8']))
    monkeypatch.setattr(tools, '_PinnedHTTPS', Connection)
    with pytest.raises(ValueError, match='HTTP 302'):
        tools._request_public_json('https://example.org/search', {'keyword': '角色'})


def test_json_response_preserves_long_evidence_without_text_preview_truncation(monkeypatch):
    from shiori import tools
    class Response:
        status = 200
        def getheader(self, key, default=''): return 'application/json'
        def read(self, limit): return json.dumps({'summary': '角色资料' * 6000}).encode()[:limit]
    class Connection:
        def __init__(self, *args): pass
        def request(self, *args, **kwargs): pass
        def getresponse(self): return Response()
        def close(self): pass
    monkeypatch.setattr(tools, 'public_addresses', lambda url: (__import__('urllib.parse', fromlist=['urlsplit']).urlsplit(url), 'example.org', 443, ['8.8.8.8']))
    monkeypatch.setattr(tools, '_PinnedHTTPS', Connection)
    assert len(tools._request_public_json('https://example.org/role')['summary']) == 24000
