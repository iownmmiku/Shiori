"""Issues #4/#5/#6: verify real outbound payloads and source attribution."""
import asyncio
import json

import httpx
import pytest
from fastapi.testclient import TestClient

from auth_helpers import password_login
from shiori.api import create_app
from shiori.character_catalog import CharacterCatalog
from shiori.chat import _schema
from shiori.distillation import DistillationService
from shiori.provider import input_size


@pytest.fixture
def client(tmp_path, monkeypatch):
    app = create_app(tmp_path, start_workers=False)
    app.state.settings.update({'model': {'base_url': 'https://fixture.invalid/v1', 'model': 'offline',
                                         'stream_mode': 'off'}, 'budget': {'daily_tokens': 0}})
    sent, responses = [], []
    original = httpx.AsyncClient
    def handler(request):
        sent.append(json.loads(request.content))
        message = responses.pop(0) if responses else {'role': 'assistant', 'content': '测试答复。'}
        return httpx.Response(200, json={'choices': [{'message': message}],
                                         'usage': {'prompt_tokens': 10, 'completion_tokens': 5}})
    monkeypatch.setattr(httpx, 'AsyncClient', lambda **kw: original(transport=httpx.MockTransport(handler), **kw))
    with TestClient(app) as connection:
        password_login(connection, app)
        yield app, connection, sent, responses


@pytest.mark.parametrize('sizes', [(1000,), (80000,), (40000, 40000)])
def test_accepted_reference_keeps_full_tail_and_packaging(client, sizes):
    app, connection, sent, _ = client
    app.state.settings.update({'context': {'max_input_chars': 200000}})
    ids, markers = [], []
    for index, size in enumerate(sizes):
        begin, end = f'BEGIN_{index}', f'END_REQUIRED_{index}'
        body = begin + 'x' * (size-len(begin)-len(end)) + end
        result = connection.post('/api/chat/attachments', data={'purpose': 'context'},
                                 files={'file': ('较长资料名称'+str(index)+'.txt', body.encode(), 'text/plain')})
        assert result.status_code == 200, result.text
        ids.append(result.json()['id']); markers.extend((begin, end))
    result = connection.post('/api/chat', json={'text': '请阅读本次附件', 'attachments': ids})
    assert result.status_code == 200, result.text
    assert len(sent) == 1
    outbound = json.dumps(sent[0]['messages'], ensure_ascii=False)
    assert all(marker in outbound for marker in markers)


def test_required_attachment_does_not_silently_disappear_when_budget_is_small(client):
    app, connection, sent, _ = client
    app.state.settings.update({'context': {'max_input_chars': 16000}})
    item = connection.post('/api/chat/attachments', data={'purpose': 'context'},
                           files={'file': ('reference.txt', b'x'*16000, 'text/plain')}).json()
    result = connection.post('/api/chat', json={'text': '请阅读附件', 'attachments': [item['id']]})
    assert result.status_code == 400 and '参考附件或工具定义' in result.json()['detail']
    assert not sent


@pytest.mark.parametrize('maximum,tools', [(16000, True), (16000, False), (80000, True)])
def test_history_trimming_reserves_the_actual_tool_definitions(client, maximum, tools):
    app, connection, sent, _ = client
    app.state.settings.update({'context': {'max_input_chars': maximum}})
    app.state.store.upsert_scope({'id': 'web:default', 'allow_tools': tools})
    for index in range(4):
        app.state.store.add_message('web:default', 'owner', 'user', f'旧消息{index}：'+'x'*3000)
    result = connection.post('/api/chat', json={'text': '你好，请继续聊天'})
    assert result.status_code == 200, result.text
    assert len(sent) == 1 and input_size(sent[0]['messages'], sent[0].get('tools'))[0] <= maximum
    assert bool(sent[0].get('tools')) == tools
    assert sent[0]['messages'][-1]['content'] == '你好，请继续聊天'


def test_extensions_are_budgeted_without_silently_disabling_them(client, monkeypatch):
    app, connection, sent, _ = client
    app.state.settings.update({'context': {'max_input_chars': 16000}})
    oversized = _schema('mcp_fixture', 'x'*17000, {})
    monkeypatch.setattr(app.state.chat.tool_actions, 'available', lambda *args: [oversized])
    result = connection.post('/api/chat', json={'text': '你好'})
    assert result.status_code == 400 and '工具定义' in result.json()['detail']
    assert not sent and not app.state.store.list_usage()


def test_tool_continuation_keeps_current_request_calls_and_results(client):
    app, connection, sent, responses = client
    app.state.settings.update({'context': {'max_input_chars': 16000}})
    for index in range(4):
        app.state.store.add_message('web:default', 'owner', 'user', f'旧消息{index}：'+'x'*3000)
    responses.append({'role': 'assistant', 'content': None, 'tool_calls': [
        {'id': 'call_fixture', 'type': 'function', 'function': {'name': 'calculate', 'arguments': '{"expression":"1+1"}'}}]})
    result = connection.post('/api/chat', json={'text': '帮我算一下 1 加 1'})
    assert result.status_code == 200, result.text
    assert len(sent) == 2
    followup = sent[-1]
    assert input_size(followup['messages'], followup.get('tools'))[0] <= 16000
    assert any(m['content'] == '帮我算一下 1 加 1' for m in followup['messages'])
    assert followup['messages'][-2]['tool_calls'][0]['id'] == 'call_fixture'
    assert followup['messages'][-1]['role'] == 'tool'
    assert followup['messages'][-1]['tool_call_id'] == 'call_fixture'


def catalog_for(body, *, aliases=False):
    async def source(url, payload=None):
        if 'api.bgm.tv' in url:
            if aliases and '/search/subjects' in url:
                return {'data': [{'id': 7, 'name_cn': '作品甲', 'name': 'Work Alpha'}]}
            return {'data': []}
        return {'query': {'pages': [{'pageid': 123, 'title': '小春', 'extract': body + '角色介绍和公开资料。'*12}]}}
    return CharacterCatalog(json_fn=source)


@pytest.mark.parametrize('body,work,found', [
    ('小春是作品甲中的虚构角色。', '作品甲', True),
    ('小春是作品甲中的虚构角色。', '作品乙', False),
    ('小春是作品甲乙中的虚构角色。', '作品甲', False),
    ('小春是《作品甲》中的人物。另有与作品乙的比较。', '作品乙', False),
    ('小春不是作品乙中的角色。小春是《作品甲》中的角色。', '作品乙', False),
    ('小春是《作品甲》中的人物。', '', True),
    ('小春是神秘故事中的人物。作品信息未核实。', '作品甲', False),
])
def test_wiki_work_filter_uses_identity_evidence_not_search_text(body, work, found):
    result = asyncio.run(catalog_for(body).lookup('小春', work))
    assert bool(result['characters']) is found
    if found:
        assert all('作品甲' in item['works'] and item['work'] == '作品甲' for item in result['characters'])


def test_wiki_work_alias_is_backed_by_subject_metadata():
    result = asyncio.run(catalog_for('小春是《Work Alpha》中的角色。', aliases=True).lookup('小春', '作品甲'))
    assert result['characters'] and all(item['work'] == 'Work Alpha' for item in result['characters'])


def test_old_lookup_cannot_resume_a_mislabeled_work(tmp_path):
    calls = []
    class Model:
        async def complete(self, *args, **kwargs): calls.append(1); raise AssertionError('Must reject before model')
    service = DistillationService(Model(), tmp_path)
    lookup = service._save('lookups', {'query': '小春', 'work': '作品乙', 'created_at': '2026-10-24T00:00:00Z',
        'characters': [{'id': 'moegirl-123', 'name': '小春', 'work': '作品乙', 'works': ['作品乙'],
                        '_page': {'url': 'https://zh.moegirl.org.cn/小春', 'title': '小春',
                                  'text': '小春是作品甲中的虚构角色。'+'角色介绍。'*20}}]})
    with pytest.raises(ValueError, match='来源未能确认限定作品'):
        asyncio.run(service.generate({'mode': 'character', 'lookup_id': lookup, 'character_id': 'moegirl-123', 'confirmed': True}))
    assert not calls
