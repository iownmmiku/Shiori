import asyncio
import json

import pytest
from fastapi.testclient import TestClient

from auth_helpers import password_login
from shiori.api import create_app
from shiori.chat_preview import emit_preview
from shiori.output import (generation_instructions, prepare_reply, reply_char_count,
                            remove_parenthetical_content, split_output)


@pytest.mark.parametrize('source,expected', [
    ('（微笑）你好', '你好'),
    ('你好(微笑)，今天（轻轻（点头））怎么样？', '你好，今天怎么样？'),
    ('开始（动作\n\n和旁白）继续', '开始继续'),
    ('未配对（请不要吃掉后面的内容', '未配对（请不要吃掉后面的内容'),
    ('普通[方括号]【书名】不删', '普通[方括号]【书名】不删'),
    ('`print("(keep)")`（动作）', '`print("(keep)")`'),
    ('```py\nprint("（保留）")\n```（动作）', '```py\nprint("（保留）")\n```'),
    ('[说明（原样）](https://example.test/a(b))（动作）', '[说明（原样）](https://example.test/a(b))'),
    ('https://example.test/a(b) （动作）', 'https://example.test/a(b) '),
    ('{"text":"（原样）", "other": "(keep)"}', '{"text":"（原样）", "other": "(keep)"}'),
])
def test_parentheses_only_prose_and_paired_round_delimiters(source, expected):
    assert remove_parenthetical_content(source) == expected


def test_content_constraints_are_independent_of_segmentation_switch():
    for values in ({'enabled': False}, {'mode': 'length'}, {'mode': 'model'}):
        config = {'output': {**values, 'reply_max_chars': 80, 'message_max_chars': 35, 'remove_parentheses': True}}
        prompt = generation_instructions(config)
        assert '合计不超过80' in prompt and '不超过35' in prompt and '圆括号及其中' in prompt
        prepared = prepare_reply('（动作）' + '完整的一句话。'*30, config)
        assert reply_char_count(prepared['reply']) <= 80
        assert all(reply_char_count(part) <= 35 for part in prepared['reply_parts'])
        assert prepared['output_adjustment'] == {'limited': True, 'removed_parentheses': True}
        assert prepared['original_reply'].startswith('（动作）')


@pytest.mark.parametrize('limit', [1, 2, 10, 21, 50, 200])
def test_explicit_whole_reply_and_per_message_caps_hold_without_new_boundaries(limit):
    source = '\n\n'.join(('自然的一句话。'*10) for _ in range(12))
    config = {'output': {'reply_max_chars': limit, 'message_max_chars': max(1, limit//2), 'max_parts': 4}}
    result = prepare_reply(source, config)
    assert reply_char_count(result['reply']) <= limit
    assert len(result['reply_parts']) <= 4
    assert all(reply_char_count(part) <= max(1,limit//2) for part in result['reply_parts'])
    assert result['original_reply'] == source


def test_limits_keep_graphemes_and_protected_atoms_complete():
    family = '👩‍👩‍👧‍👦'
    result = prepare_reply(family*20, {'output': {'reply_max_chars': 5}})
    assert result['reply'] == family*4 + '…' and reply_char_count(result['reply']) == 5
    code = '```py\nprint("(keep)")\n```'
    result = prepare_reply(code + '。' + '后续很长的描述'*30, {'output': {'reply_max_chars': len(code)+10}})
    assert result['reply'].count('```') == 2
    result = prepare_reply(code, {'output': {'reply_max_chars': 10}})
    assert '```' not in result['reply'] and result['original_reply'] == code
    source = '{"long":"' + '内容'*40 + '"}'
    result = prepare_reply(source, {'output': {'reply_max_chars': 12}})
    assert not result['reply'].startswith('{') and result['original_reply'] == source


def test_default_limits_do_not_change_text_and_blank_parentheses_can_be_filtered():
    source = '（动作）正常的一句。\n\n还有一句。'
    assert prepare_reply(source)['reply'] == source
    assert 'original_reply' not in prepare_reply(source)
    result = prepare_reply('（微笑）\n\n(点头)', {'output': {'remove_parentheses': True}})
    assert result['reply'] == '' and result['reply_parts'] == [] and result['output_filtered']


def test_final_limited_boundaries_are_not_rebalanced_again_for_qq():
    lengths = [13,4,6,21,6,3,7,17,29,26]
    source = '\n\n'.join(chr(65+i)*size for i,size in enumerate(lengths))
    cfg = {'output': {'max_parts':5, 'message_max_chars':24, 'platform_max_chars':20000}}
    result = prepare_reply(source, cfg)
    parts = split_output(result['reply'], cfg, reply_parts=result['reply_parts'])
    assert parts == result['reply_parts']
    assert all(reply_char_count(part) <= 24 for part in parts)
    assert split_output('属于实际正文', cfg, reply_parts=['不属于正文的伪造内容']) == ['属于实际正文']


@pytest.mark.parametrize('only_parentheses', [False, True])
def test_chat_applies_once_and_saves_original_for_manual_viewing(tmp_path, monkeypatch, only_parentheses):
    app = create_app(tmp_path, start_workers=False)
    calls = []
    original = '（微笑）' if only_parentheses else '（微笑）你好。\n\n' + '这是更长的自然句子。'*20
    async def complete(messages, **kwargs):
        calls.append(messages)
        assert kwargs['before_send']()
        assert '合计不超过40' in messages[0]['content']
        return {'content': original, 'usage': {}, 'tool_calls': [], 'message': {'role': 'assistant', 'content': original}}
    monkeypatch.setattr(app.state.provider, 'complete', complete)
    with TestClient(app) as client:
        password_login(client, app)
        assert client.put('/api/settings', json={'output': {'reply_max_chars':40, 'message_max_chars':25, 'remove_parentheses':True}}).status_code == 200
        result = client.post('/api/chat', json={'text':'今天怎么样？'}).json()
        assert len(calls) == 1
        assert '（微笑）' not in result['reply'] and reply_char_count(result['reply']) <= 40
        saved = app.state.store.get_message(result['message_id'])
        assert saved['content'] == result['reply']
        assert saved['metadata']['original_reply'] == original
        assert saved['metadata']['reply_parts'] == result['reply_parts']
        assert saved['metadata']['output_adjustment']['removed_parentheses'] is True
        assert result['output_filtered'] is only_parentheses


def test_ai_summary_uses_content_policy_but_local_command_receipts_keep_details(tmp_path, monkeypatch):
    app = create_app(tmp_path, start_workers=False)
    calls = []
    original = '（点头）这是聊天总结。'*20
    async def complete(messages, **kwargs):
        calls.append(kwargs.get('purpose'))
        assert kwargs['before_send']()
        assert '合计不超过12' in messages[0]['content']
        return {'content': original, 'usage': {}, 'tool_calls': [], 'message': {'role':'assistant', 'content':original}}
    monkeypatch.setattr(app.state.provider, 'complete', complete)
    with TestClient(app) as client:
        password_login(client, app)
        app.state.store.add_message('web:default', 'owner', 'user', '上周认真讨论过的主题')
        client.put('/api/settings', json={'output': {'reply_max_chars':12, 'remove_parentheses':True}})
        summary = client.post('/api/chat', json={'text':'总结最近聊天'}).json()
        assert calls == ['summary']
        assert reply_char_count(summary['reply']) <= 12 and '（点头）' not in summary['reply']
        assert app.state.store.get_message(summary['message_id'])['metadata']['original_reply'] == original
        receipt = client.post('/api/chat', json={'text':'记住：这条包含（重要）括号，属于应原样保存的用户记忆'}).json()
        assert len(calls) == 1 and 'original_reply' not in app.state.store.get_message(receipt['message_id'])['metadata']
        assert '（重要）' in str(receipt['actions'])


def test_streaming_chat_wires_output_rules_into_preview_and_final_reply(tmp_path, monkeypatch):
    app = create_app(tmp_path, start_workers=False)
    source = '（秘密动作）你好。\n\n' + '这是一段更长的完整正文。'*10
    async def complete(messages, **kwargs):
        for char in source:
            emit_preview('delta', char)
            await asyncio.sleep(0)
        return {'content':source, 'usage':{}, 'tool_calls':[], 'message':{'role':'assistant','content':source}}
    monkeypatch.setattr(app.state.provider, 'complete', complete)
    with TestClient(app) as client:
        password_login(client, app)
        client.put('/api/settings', json={'output': {'reply_max_chars':30, 'message_max_chars':18, 'remove_parentheses':True}})
        response = client.post('/api/chat', json={'text':'你好呀', 'stream':True})
        assert response.status_code == 200
        events = [json.loads(line[6:]) for line in response.text.splitlines() if line.startswith('data: ')]
        assert events[-1]['type'] == 'result'
        visible = ''.join(event.get('text','') for event in events if event['type'] == 'delta')
        result = events[-1]['data']
        assert '秘密动作' not in visible and '（' not in visible and reply_char_count(visible) <= 30
        assert result['reply'].startswith('你好。') and reply_char_count(result['reply']) <= 30
        assert all(reply_char_count(part) <= 18 for part in result['reply_parts'])
        assert app.state.store.get_message(result['message_id'])['metadata']['original_reply'] == source
