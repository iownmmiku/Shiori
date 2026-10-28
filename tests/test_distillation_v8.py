"""Isolated corpus/source fixtures; no real web, model or user-data access."""
import asyncio
import io
import json
import stat
import zipfile

import pytest

from shiori.corpus import corpus_chunks, inspect_corpus, read_file
from shiori.distillation import DistillationService
from shiori.provider import ProviderStreamError, ProviderError


@pytest.fixture
def anyio_backend():
    return 'asyncio'


def archive(files):
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, 'w', zipfile.ZIP_DEFLATED) as output:
        for name, body in files.items():
            output.writestr(name, body if isinstance(body, bytes) else body.encode('utf-8'))
    return stream.getvalue()


class Model:
    def __init__(self, content='有明确证据时自然回应，表达简短。遇到分歧先确认事实。资料不足时保留未知。'):
        self.calls = []
        self.content = content
        self.after = None

    async def complete(self, messages, **kwargs):
        assert kwargs['purpose'] == 'distillation'
        assert kwargs['before_send']() is True
        self.calls.append((messages, kwargs))
        if self.after:
            self.after()
        return {'content': self.content, 'usage': {'input_tokens': 100, 'output_tokens': 80}}


@pytest.fixture
def service(tmp_path):
    pages = {
        'https://wiki.example/character': {'title': '测试人物 - 测试作品', 'text': '测试人物是测试作品中的虚构人物。面对问题，她会先确认事实再提出方案。' * 5},
        'https://official.example/character': {'title': '测试作品人物资料', 'text': '测试人物在测试作品中的不同场景里会认真倾听，在冲突时保留他人的意见。' * 5},
    }
    async def search(query):
        return {'links': list(pages), 'text': '不是人物资料的搜索摘要'}
    async def read(url):
        return {'url': url, **pages[url]}
    model = Model()
    engine = DistillationService(model, tmp_path, search_fn=search, read_fn=read)
    return engine, model, pages


def test_zip_inventory_keeps_files_separate_and_does_not_extract(tmp_path):
    raw = archive({'folder/chat.txt': '甲：今天做个计划\n乙：先整理资料\n甲：好，慢慢来',
                   'notes.md': '# 笔记\n可以观察的表达习惯', 'image.png': b'\x89PNG', 'nested.zip': b'not expanded',
                   'bad.json': '{'})
    result = inspect_corpus(raw, '资料.zip')
    assert result['readable_files'] == 2
    assert [row['status'] for row in result['files']] == ['readable', 'readable', 'skipped', 'skipped', 'failed']
    assert {row['name'] for row in result['speakers']} == {'甲', '乙'}
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize('name', ['../escape.txt', '/abs.txt', 'C:/escape.txt', 'folder\\..\\escape.txt', 'folder/a:b.txt'])
def test_zip_traversal_is_rejected_without_extracting(name):
    with pytest.raises(ValueError, match='不安全'):
        inspect_corpus(archive({name: '不应读取'}), 'bad.zip')


def test_zip_symlink_is_rejected():
    stream = io.BytesIO()
    info = zipfile.ZipInfo('link.txt')
    info.create_system = 3
    info.external_attr = (stat.S_IFLNK | 0o777) << 16
    with zipfile.ZipFile(stream, 'w') as output:
        output.writestr(info, '../../outside')
    with pytest.raises(ValueError, match='符号链接'):
        inspect_corpus(stream.getvalue(), 'bad.zip')


def test_zip_duplicate_case_insensitive_names_rejected():
    with pytest.raises(ValueError, match='重复'):
        inspect_corpus(archive({'A.txt': '第一份', 'a.txt': '第二份'}), 'bad.zip')


def test_zip_bomb_and_file_count_rejected():
    with pytest.raises(ValueError, match='压缩比'):
        inspect_corpus(archive({'big.txt': 'a' * (2 * 1024 * 1024)}), 'big.zip')
    with pytest.raises(ValueError, match='200'):
        inspect_corpus(archive({f'{i}.txt': 'x' for i in range(201)}), 'many.zip')


@pytest.mark.parametrize('name,body,expected', [
    ('file.txt', '简单文本'.encode('gb18030'), '简单文本'),
    ('file.md', 'Unicode资料'.encode('utf-16'), 'Unicode资料'),
    ('file.html', b'<head><script>evil()</script></head><p>hello</p><script>hidden</script>', 'hello'),
    ('file.srt', '1\n00:00:01,000 --> 00:00:02,000\n甲：你好\n2\n00:00:03,000 --> 00:00:04,000\n乙：好的'.encode(), '甲：你好'),
    ('file.vtt', b'WEBVTT\n\n00:01.000 --> 00:02.000\nHello world', 'Hello world'),
])
def test_read_text_formats(name, body, expected):
    text = read_file(name, body)['text']
    assert expected in text
    assert 'evil()' not in text and 'hidden' not in text and '-->' not in text


def test_jsonl_and_csv_recognize_speakers_and_onebot_text_only():
    lines = '\n'.join(json.dumps(item, ensure_ascii=False) for item in [
        {'sender': {'user_id': 111, 'nickname': '同名'}, 'message': [{'type': 'text', 'data': {'text': '目标文字'}}, {'type': 'image', 'data': {'file': 'private'}}]},
        {'sender': {'user_id': 222, 'nickname': '同名'}, 'message': '另一人的观点'},
    ])
    record = read_file('chat.jsonl', lines.encode())
    assert set(record['speakers']) == {'同名 (111)', '同名 (222)'}
    assert 'private' not in record['text']
    csv_record = read_file('chat.csv', 'speaker,text\n甲,这是一条消息\n乙,这是回复'.encode())
    assert csv_record['speakers'] == {'甲': 1, '乙': 1}


def test_json_top_level_sender_id_preserves_identity():
    body = json.dumps([{'name': '同名', 'sender_id': '1', 'text': 'one'}, {'name': '同名', 'sender_id': '2', 'text': 'two'}]).encode()
    result = read_file('chat.json', body)
    assert set(result['speakers']) == {'同名 (1)', '同名 (2)'}


@pytest.mark.parametrize('nested', [False, True])
def test_long_display_names_cannot_hide_distinct_sender_ids(nested):
    records = []
    for identity in ('101', '202'):
        sender = {'user_id': identity, 'nickname': '同名' * 100}
        records.append({'sender': sender, 'text': identity} if nested else
                       {'name': sender['nickname'], 'sender_id': identity, 'text': identity})
    result = read_file('chat.json', json.dumps(records).encode())
    assert len(result['speakers']) == 2
    assert {name[-5:] for name in result['speakers']} == {'(101)', '(202)'}
    assert all(len(name) <= 120 for name in result['speakers'])


def test_docx_extracts_text_and_pdf_without_text_reports_ocr():
    doc = archive({'word/document.xml': '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body><w:p><w:r><w:t>可读取的正文</w:t></w:r></w:p></w:body></w:document>'})
    assert '可读取的正文' in read_file('a.docx', doc)['text']
    from pypdf import PdfWriter
    stream = io.BytesIO()
    writer = PdfWriter()
    writer.add_blank_page(width=100, height=100)
    writer.write(stream)
    result = inspect_corpus(archive({'scan.pdf': stream.getvalue()}), 'scan.zip')
    assert result['readable_files'] == 0 and 'OCR' in result['files'][0]['reason']


def test_target_speaker_chunks_keep_other_people_as_context():
    result = inspect_corpus(archive({'chat.json': json.dumps([
        {'speaker': '甲', 'text': '先看清事实'}, {'speaker': '乙', 'text': '其他人的观点'},
        {'speaker': '甲', 'text': '我还不确定'}, {'speaker': '丙', 'text': '只做上下文'},
    ], ensure_ascii=False)}), 'chat.zip')
    chunks = corpus_chunks(result['_documents'], target_speaker='甲')
    text = '\n'.join(chunks)
    assert text.count('目标本人（仅此可作表达证据）') == 2
    assert '其他人／上下文（不可归因给目标）' in text
    with pytest.raises(ValueError, match='没有找到'):
        corpus_chunks(result['_documents'], target_speaker='不存在')


def test_long_message_chunks_repeat_file_and_speaker_attribution():
    result = inspect_corpus(json.dumps([{'speaker': '甲', 'text': '样本' * 9000}]).encode(), 'long.json')
    chunks = corpus_chunks(result['_documents'], target_speaker='甲')
    assert len(chunks) >= 3
    assert all('[文件 f1：long.json]' in chunk and '目标本人' in chunk for chunk in chunks)


@pytest.mark.anyio
async def test_target_speaker_exclusions_are_explicit_and_not_claimed_as_sources(service):
    engine, _, _ = service
    upload = engine.inspect_corpus(archive({'chat.json': json.dumps([{'speaker': '甲', 'text': '先看资料'}]),
                                         'other.md': '没有说话者归属的其他文章'}), 'mixed.zip')
    result = await engine.generate({'mode': 'corpus', 'upload_id': upload['upload_id'], 'confirmed': True, 'target_speaker': '甲'})
    assert len(result['sources']) == 1 and result['sources'][0]['title'] == 'chat.json'
    assert any('other.md' in warning and '未作' in warning for warning in result['warnings'])


def test_corpus_upload_is_private_task_storage_only(service, tmp_path):
    engine, model, _ = service
    result = engine.inspect_corpus(archive({'note.md': '用户资料，只用于蒸馏'}), 'data.zip')
    assert result['upload_id'] and result['readable_files'] == 1
    assert '_documents' not in result
    assert model.calls == []
    assert list(tmp_path.iterdir()) == [tmp_path / 'distillation']
    assert not list(tmp_path.rglob('*.db'))
    assert engine.corpus_inventory(result['upload_id'])['files'] == result['files']
    with pytest.raises(ValueError, match='标识'):
        engine.corpus_inventory('../../elsewhere')


@pytest.mark.anyio
async def test_character_discovery_never_auto_selects_or_calls_model(service):
    engine, model, _ = service
    result = await engine.discover({'character': '测试人物', 'work': '测试作品'})
    assert len(result['candidates']) == 2 and result['requires_confirmation'] is True
    assert len({row['id'] for row in result['candidates']}) == 2
    assert all(row['content_hash'] and row['officiality'] == 'not_verified' for row in result['candidates'])
    assert result['discovery_id'] and not model.calls
    with pytest.raises(ValueError, match='先确认'):
        await engine.generate({'mode': 'character', 'discovery_id': result['discovery_id']})
    assert not model.calls


@pytest.mark.anyio
async def test_character_generates_persona_and_complete_skill_draft(service):
    engine, model, _ = service
    discovery = await engine.discover({'character': '测试人物', 'work': '测试作品'})
    events = []
    result = await engine.generate({'mode': 'character', 'discovery_id': discovery['discovery_id'],
                                   'confirmed': True, 'candidate_ids': [row['id'] for row in discovery['candidates']],
                                   'version': '动画第一季', 'scope_id': 'web:distill-test'}, progress=events.append)
    assert result['mode'] == 'character' and result['preview_only'] and not result['applied']
    assert result['personality'] == result['prompt'] and len(result['personality']) <= 6000
    assert len(model.calls) == 7 and all(call[1]['scope_id'] == 'web:distill-test' for call in model.calls)
    assert result['quality']['fact_verified'] is False and result['quality']['human_review_required'] is True
    assert events[-1]['stage'] == 'completed'
    with zipfile.ZipFile(engine.artifact_path(result['artifact']['id'])) as bundle:
        names = set(bundle.namelist())
        assert {'SKILL.md', 'persona.md', 'manifest.json', 'LICENSE-CSP', 'references/sources.json', 'references/distillation.md', 'references/quality-report.json'} <= names
        assert len([name for name in names if name.startswith('references/research/')]) == 6
        manifest = json.loads(bundle.read('manifest.json'))
        sources = json.loads(bundle.read('references/sources.json'))
        assert manifest['version'] == '动画第一季' and manifest['source_count'] == 2
        assert all(source['retrieved_at'] and source['content_hash'] for source in sources)
        assert '权限' in bundle.read('SKILL.md').decode()


@pytest.mark.anyio
async def test_long_character_sources_are_batched_with_all_tails_and_ids(service):
    engine, model, pages = service
    for index, page in enumerate(pages.values()):
        page['text'] = '测试人物在测试作品中表现认真。' * 900 + f'原始来源尾部{index}'
    discovery = await engine.discover({'character':'测试人物','work':'测试作品'})
    result = await engine.generate({'mode':'character','discovery_id':discovery['discovery_id'],
        'confirmed':True,'candidate_ids':[row['id'] for row in discovery['candidates']]})
    batches = [messages for messages, kwargs in model.calls if '从本片段提取' in messages[-1]['content']]
    assert len(batches) == 4 and len(model.calls) == 11
    assert all(len(messages[-1]['content']) < 10000 for messages in batches)
    joined = '\n'.join(messages[-1]['content'] for messages in batches)
    assert '原始来源尾部0' in joined and '原始来源尾部1' in joined
    assert '[s1]' in joined and '[s2]' in joined
    tracks = [messages[-1]['content'] for messages, kwargs in model.calls if '本维度：' in messages[-1]['content']]
    assert len(tracks) == 5 and all(len(text) < 18000 and '[s1]' in text and '[s2]' in text for text in tracks)
    assert result['usage']['calls'] == 11


@pytest.mark.anyio
async def test_batched_research_resume_reuses_notes_and_retains_failed_attempts(service, monkeypatch):
    engine, model, pages = service
    for page in pages.values(): page['text'] = '测试人物在测试作品中表现认真。' * 900
    discovery = await engine.discover({'character':'测试人物','work':'测试作品'})
    payload = {'mode':'character','discovery_id':discovery['discovery_id'],'confirmed':True,
               'max_calls':13,'candidate_ids':[row['id'] for row in discovery['candidates']]}
    complete = model.complete
    attempts, saved = [], []
    async def flaky(messages, **kwargs):
        attempts.append(messages)
        if len(attempts) in (5,6): raise ProviderStreamError('流中断')
        return await complete(messages, **kwargs)
    monkeypatch.setattr(model, 'complete', flaky)
    save = lambda **state:saved.append(json.loads(json.dumps(state)))
    with pytest.raises(ProviderStreamError): await engine.generate(payload, checkpoint=save)
    assert len(attempts) == 6
    result = await engine.generate(payload, checkpoint=save, resume_state=saved[-1]['distillation_cache'])
    assert len(attempts) == 13 and result['usage']['calls'] == 13
    assert result['resumed_steps'] == 4 and result['usage']['estimated']


@pytest.mark.anyio
async def test_distillation_retries_current_analysis_once_and_counts_unknown_cost(service, monkeypatch):
    engine, model, _ = service
    complete = model.complete
    attempts, events = [], []
    async def flaky(messages, **kwargs):
        attempts.append(messages)
        if len(attempts) == 1: raise ProviderStreamError('流中断')
        return await complete(messages, **kwargs)
    monkeypatch.setattr(model, 'complete', flaky)
    result = await engine.generate({'mode':'generate','description':'温柔的读书伙伴'}, progress=events.append)
    assert len(attempts) == 2 and attempts[0] == attempts[1]
    assert result['usage']['calls'] == 2 and result['usage']['estimated']
    assert result['usage']['tokens'] > 180
    assert any(event['stage'] == 'retry' for event in events)


@pytest.mark.anyio
async def test_distillation_persistent_failure_stops_after_two_and_saves_attempt_budget(service, monkeypatch):
    engine, model, _ = service
    attempts, saved = [], []
    async def fail(messages, **kwargs):
        attempts.append(messages)
        raise ProviderStreamError('流中断')
    monkeypatch.setattr(model, 'complete', fail)
    with pytest.raises(ProviderStreamError):
        await engine.generate({'mode':'generate','description':'读书伙伴'}, checkpoint=lambda **state:saved.append(json.loads(json.dumps(state))))
    assert len(attempts) == 2
    cache = saved[-1]['distillation_cache']
    assert cache['usage']['calls'] == 2 and cache['usage']['estimated'] and not cache['results']


@pytest.mark.anyio
async def test_distillation_does_not_retry_invalid_result(service, monkeypatch):
    engine, model, _ = service
    attempts = []
    async def fail(messages, **kwargs):
        attempts.append(messages)
        raise ProviderError('模型流返回无效事件')
    monkeypatch.setattr(model, 'complete', fail)
    with pytest.raises(ProviderError): await engine.generate({'mode':'generate','description':'读书伙伴'})
    assert len(attempts) == 1


@pytest.mark.anyio
async def test_cancel_between_analysis_attempts_prevents_retry(service, monkeypatch):
    engine, model, _ = service
    attempts = []
    async def fail(messages, **kwargs):
        attempts.append(messages)
        raise ProviderStreamError('流中断')
    monkeypatch.setattr(model, 'complete', fail)
    with pytest.raises(asyncio.CancelledError):
        await engine.generate({'mode':'generate','description':'读书伙伴'}, cancel_check=lambda:not attempts)
    assert len(attempts) == 1


@pytest.mark.anyio
async def test_corpus_generates_description_only_and_requires_target(service):
    engine, model, _ = service
    upload = engine.inspect_corpus(archive({'chat.json': json.dumps([
        {'speaker': '甲', 'text': '先确认资料再决定'}, {'speaker': '乙', 'text': '我喜欢立刻决定'},
        {'speaker': '甲', 'text': '不用着急，我们看事实'},
    ], ensure_ascii=False)}), 'logs.zip')
    with pytest.raises(ValueError, match='多位说话者'):
        await engine.generate({'mode': 'corpus', 'upload_id': upload['upload_id'], 'confirmed': True})
    assert not model.calls
    result = await engine.generate({'mode': 'corpus', 'upload_id': upload['upload_id'], 'confirmed': True,
                                   'target_speaker': '甲', 'name': '资料人格'})
    assert result['artifact'] is None and result['mode'] == 'corpus' and result['target_speaker'] == '甲'
    assert len(model.calls) == 2
    assert '不生成 Skill' in model.calls[-1][0][-1]['content']
    assert '其他人／上下文' in model.calls[0][0][-1]['content']
    assert not (engine.root / 'artifacts').exists()


@pytest.mark.anyio
async def test_character_source_change_requires_reconfirmation(service):
    engine, model, pages = service
    discovery = await engine.discover({'character': '测试人物', 'work': '测试作品'})
    pages['https://wiki.example/character']['text'] += '新的版本修订'
    with pytest.raises(ValueError, match='确认后发生变化'):
        await engine.generate({'mode': 'character', 'confirmed': True, 'discovery_id': discovery['discovery_id'],
                               'candidate_ids': [discovery['candidates'][0]['id']]})
    assert not model.calls


@pytest.mark.anyio
async def test_forged_candidate_and_changed_redirect_rejected(service):
    engine, model, _ = service
    discovery = await engine.discover({'character': '测试人物', 'work': '测试作品'})
    with pytest.raises(ValueError, match='候选'):
        await engine.generate({'mode': 'character', 'confirmed': True, 'discovery_id': discovery['discovery_id'], 'candidate_ids': ['forged']})
    async def redirect(url):
        return {'url': 'https://elsewhere.example/other', 'text': discovery['candidates'][0]['snippet']}
    engine.read_fn = redirect
    with pytest.raises(ValueError, match='发生变化'):
        await engine.generate({'mode': 'character', 'confirmed': True, 'discovery_id': discovery['discovery_id'], 'candidate_ids': [discovery['candidates'][0]['id']]})
    assert not model.calls


@pytest.mark.anyio
async def test_cancel_before_or_after_model_prevents_artifact(service):
    engine, model, _ = service
    upload = engine.inspect_corpus(b'Only a sample writing style.', 'sample.txt')
    payload = {'mode': 'corpus', 'upload_id': upload['upload_id'], 'confirmed': True}
    with pytest.raises(asyncio.CancelledError):
        await engine.generate(payload, cancel_check=lambda: False)
    assert not model.calls
    state = {'active': True}
    model.after = lambda: state.update(active=False)
    with pytest.raises(asyncio.CancelledError):
        await engine.generate(payload, cancel_check=lambda: state['active'])
    assert len(model.calls) == 1 and not (engine.root / 'artifacts').exists()


@pytest.mark.anyio
async def test_budget_whole_run_does_not_reset_between_stages(service):
    engine, model, _ = service
    upload = engine.inspect_corpus(('样本写作。' * 1800).encode(), 'sample.txt')
    with pytest.raises(ValueError, match='至少.*次模型调用'):
        await engine.generate({'mode': 'corpus', 'upload_id': upload['upload_id'], 'confirmed': True, 'max_calls': 2})
    assert not model.calls
    upload = engine.inspect_corpus(('样本写作。' * 200).encode(), 'small.txt')
    with pytest.raises(ValueError, match='Token 上限'):
        await engine.generate({'mode': 'corpus', 'upload_id': upload['upload_id'], 'confirmed': True, 'max_total_tokens': 1000})
    assert not model.calls
    result = await engine.generate({'mode': 'corpus', 'upload_id': upload['upload_id'], 'confirmed': True, 'max_calls': 2})
    assert result['usage']['calls'] == 2 and result['usage']['tokens'] == 360


@pytest.mark.anyio
async def test_explicit_source_bypasses_unavailable_search(service):
    engine, model, _ = service
    async def fail(query):
        raise AssertionError('Explicit links must not depend on a working search engine')
    engine.search_fn = fail
    discovery = await engine.discover({'character': '测试人物', 'work': '测试作品', 'source_urls': ['https://wiki.example/character']})
    assert len(discovery['candidates']) == 1 and not discovery['failures']
    result = await engine.generate({'mode': 'character', 'discovery_id': discovery['discovery_id'], 'confirmed': True, 'candidate_ids': [discovery['candidates'][0]['id']]})
    assert sum(source['status'] == 'failed' for source in result['sources']) == 0
    assert len(model.calls) == 7


@pytest.mark.anyio
async def test_search_aggregate_page_is_not_a_person_candidate(service):
    engine, model, _ = service
    async def search(query):
        return {'links': ['https://www.bing.com/search?q=example']}
    async def read(url):
        return {'url': url, 'title': '测试人物 - Bing Search', 'text': '测试人物 测试作品 搜索结果' * 30}
    engine.search_fn, engine.read_fn = search, read
    with pytest.raises(ValueError, match='没有找到可确认'):
        await engine.discover({'character': '测试人物', 'work': '测试作品'})
    assert not model.calls


@pytest.mark.anyio
async def test_corpus_does_not_search_web_even_for_named_real_person(service):
    engine, model, _ = service
    async def forbidden(*args):
        raise AssertionError('corpus mode must not use network')
    engine.search_fn = engine.read_fn = forbidden
    upload = engine.inspect_corpus('这份文稿包含我的表达风格，但不提供任何生平。'.encode(), 'article.md')
    result = await engine.generate({'mode': 'corpus', 'upload_id': upload['upload_id'], 'confirmed': True, 'name': '真实人物样本'})
    assert result['artifact'] is None
    assert '不补传记' in model.calls[-1][0][-1]['content']
    assert all(source['source'] == 'user_corpus' for source in result['sources'])


@pytest.mark.anyio
async def test_empty_corpus_and_invalid_selected_file_do_not_call_model(service):
    engine, model, _ = service
    upload = engine.inspect_corpus(archive({'image.png': b'not text'}), 'images.zip')
    with pytest.raises(ValueError, match='可读取'):
        await engine.generate({'mode': 'corpus', 'upload_id': upload['upload_id'], 'confirmed': True})
    upload = engine.inspect_corpus(b'simple example', 'note.txt')
    with pytest.raises(ValueError, match='不存在'):
        await engine.generate({'mode': 'corpus', 'upload_id': upload['upload_id'], 'confirmed': True, 'selected_files': ['f999']})
    assert not model.calls


def test_artifact_identifier_never_resolves_outside_storage(service):
    engine, _, _ = service
    with pytest.raises(ValueError, match='标识'):
        engine.artifact_path('../../admin.token')
    with pytest.raises(ValueError, match='不存在'):
        engine.artifact_path('a' * 32)
