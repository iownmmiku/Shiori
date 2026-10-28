import asyncio
import io
import json
import zipfile
from datetime import datetime

import httpx
import pytest
from fastapi.testclient import TestClient

from shiori.api import create_app
from shiori.chat import ChatService
from qunyu.db import Store
from shiori.documents import extract_document
from shiori.provider import Provider, ProviderError
from shiori.settings import Settings
from auth_helpers import password_login


class ProviderFixture:
    def __init__(self):
        self.calls = []
    async def complete(self, messages, **kwargs):
        self.calls.append(messages)
        return {'content': '模型测试替身', 'tool_calls': [], 'message': {'role': 'assistant', 'content': '模型测试替身'}, 'usage': {}}


def test_docx_extraction_does_not_fetch_relationships():
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w') as archive:
        archive.writestr('word/document.xml', '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body><w:p><w:r><w:t>长期项目知识</w:t></w:r></w:p><w:p><w:r><w:t>部署顺序与注意事项</w:t></w:r></w:p></w:body></w:document>')
    result = extract_document('notes.docx', buffer.getvalue())
    assert result['text'] == '长期项目知识\n部署顺序与注意事项'
    assert result['warnings']
    with pytest.raises(ValueError):
        extract_document('bad.json', b'{not-json}')
    with pytest.raises(ValueError, match='8MB'):
        extract_document('big.txt', b'x' * (8 * 1024 * 1024 + 1))


def test_pdf_text_and_scan_boundary():
    from pypdf import PdfWriter
    from pypdf.generic import DictionaryObject, NameObject, DecodedStreamObject
    writer = PdfWriter()
    page = writer.add_blank_page(width=300, height=200)
    font = DictionaryObject({NameObject('/Type'): NameObject('/Font'), NameObject('/Subtype'): NameObject('/Type1'), NameObject('/BaseFont'): NameObject('/Helvetica')})
    page[NameObject('/Resources')] = DictionaryObject({NameObject('/Font'): DictionaryObject({NameObject('/F1'): writer._add_object(font)})})
    stream = DecodedStreamObject()
    stream.set_data(b'BT /F1 12 Tf 10 100 Td (Release planning and source references) Tj ET')
    page[NameObject('/Contents')] = writer._add_object(stream)
    buffer = io.BytesIO()
    writer.write(buffer)
    result = extract_document('notes.pdf', buffer.getvalue())
    assert 'Release planning' in result['text'] and result['pages'] == 1
    writer = PdfWriter()
    writer.add_blank_page(width=100, height=100)
    buffer = io.BytesIO()
    writer.write(buffer)
    with pytest.raises(ValueError, match='OCR'):
        extract_document('scan.pdf', buffer.getvalue())


def test_real_embedding_wire_format_budget_and_redaction(tmp_path, monkeypatch):
    store, settings = Store(tmp_path), Settings(tmp_path)
    settings.update({'model': {'base_url': 'https://fixture.example/v1', 'model': 'chat', 'api_key': 'fixture-not-user-key'},
                     'memory': {'semantic_enabled': True, 'embedding_model': 'fixture-embed', 'embedding_price': 2}})
    def handler(request):
        assert request.url.path == '/v1/embeddings'
        body = json.loads(request.content)
        assert body['encoding_format'] == 'float' and body['input'] == ['语义查询', '文档片段']
        return httpx.Response(200, json={'data': [{'index': 1, 'embedding': [0., 1.]}, {'index': 0, 'embedding': [1., 0.]}], 'usage': {'prompt_tokens': 12}})
    original = httpx.AsyncClient
    monkeypatch.setattr(httpx, 'AsyncClient', lambda *a, **kw: original(*a, transport=httpx.MockTransport(handler), **kw))
    assert asyncio.run(Provider(settings, store).embed(['语义查询', '文档片段'])) == [[1., 0.], [0., 1.]]
    assert store.list_usage()[0]['purpose'] == 'embedding'
    assert 'fixture-not-user-key' not in str(store.list_usage())
    settings.update({'budget': {'daily_tokens': 1}})
    with pytest.raises(ProviderError, match='预算'):
        asyncio.run(Provider(settings, store).embed(['预算不够不能发请求']))


def test_memory_review_search_and_document_import_api(tmp_path):
    app = create_app(tmp_path, start_workers=False)
    with TestClient(app) as client:
        password_login(client, app)
        source = app.state.store.add_message('web:default', 'owner', 'user', '我现在住杭州')
        old = app.state.store.create_memory({'content': '我住上海', 'property': 'residence_city', 'subject': 'owner'})
        pending = app.state.chat.memory.ingest_candidates([{'content': '我现在住杭州', 'kind': 'fact', 'property': 'residence_city', 'subject': 'owner', 'confidence': .95, 'source_ids': [source['id']]}], scope_id='web:default', user_id='owner', source_ids=[source['id']])[0]
        assert client.get('/api/memory/candidates').json()[0]['id'] == pending['id']
        assert client.get('/api/memories/' + pending['id'] + '/conflicts').json()[0]['id'] == old['id']
        assert client.post('/api/memories/' + pending['id'] + '/approve', json={}).status_code == 400
        assert client.post('/api/memories/' + pending['id'] + '/approve', json={'replace_conflicts': True}).json()['status'] == 'active'
        assert app.state.store.get_memory(old['id'])['status'] == 'superseded'
        result = client.get('/api/memory/search', params={'q': '现在住哪里'}).json()
        assert result['items'] and '上海' not in str(result['items'])
        text = ('项目说明\n' * 2000) + '关键部署步骤：先安装协议端，然后配置群屿。' + ('\n普通附录' * 2000)
        uploaded = client.post('/api/files', files={'file': ('guide.md', text.encode(), 'text/markdown')})
        assert uploaded.status_code == 200 and uploaded.json()['characters'] > 12000
        duplicate = client.post('/api/files', files={'file': ('guide.md', text.encode(), 'text/markdown')})
        assert duplicate.json()['duplicate']
        result = client.get('/api/memory/search', params={'q': '关键部署步骤'}).json()
        assert '先安装协议端' in str(result['items']) and len(json.dumps(result['items'])) < 30000


def test_chat_merge_sources_correction_batch_todos_and_reschedule(tmp_path):
    store, settings = Store(tmp_path), Settings(tmp_path)
    provider = ProviderFixture()
    chat = ChatService(store, settings, provider)
    async def scenario():
        a = await chat.observe('web:default', 'owner', '添加待办：')
        b = await chat.observe('web:default', 'owner', '检查资料\n准备发布')
        result = await chat.respond('web:default', 'owner', '添加待办：\n检查资料\n准备发布', metadata={'existing_message_id': b['id'], 'merged_message_ids': [a['id'], b['id']]})
        assert len(result['actions']) == len(store.list_tasks()) == 2
        assert len([x for x in store.list_messages('web:default') if x['role'] == 'user']) == 2
        await chat.respond('web:default', 'owner', '完成待办：检查资料')
        assert any(x['status'] == 'completed' for x in store.list_tasks())
        await chat.respond('web:default', 'owner', '明晚八点提醒我喝水')
        changed = await chat.respond('web:default', 'owner', '把喝水提醒改到后天晚上九点')
        assert '已改期' in changed['reply']
        remembered = await chat.respond('web:default', 'owner', '记住：我住上海')
        memory = store.list_memories()[0]
        corrected = await chat.respond('web:default', 'owner', f"更正记忆：{memory['id'][:8]} → 我住杭州")
        assert '杭州' in corrected['reply']
        await chat.respond('web:default', 'owner', '我住在哪里？')
        assert '上海' not in str(provider.calls[-1])
        other = await chat.observe('web:default', 'stranger', '继续')
        with pytest.raises(ValueError, match='同一请求人'):
            await chat.respond('web:default', 'owner', '继续', metadata={'existing_message_id': other['id']})
        await chat.close()
    asyncio.run(scenario())
