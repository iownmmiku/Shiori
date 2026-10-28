"""Output preferences must apply before provisional browser content appears."""
import pytest

from shiori import output
from shiori.chat_preview import (MAX_ADJUSTED_PREVIEW_CHARS, MAX_ADJUSTED_PREVIEW_CHECKPOINTS,
                                 _AdjustedPublicText, emit_preview, preview_events, preview_filter)


def collect(parts, options, *, memory_ids=(), user_text=''):
    seen = []
    with preview_events(lambda kind, text: seen.append((kind, text))):
        with preview_filter(memory_ids, user_text=user_text, output_options=options):
            for text in parts:
                emit_preview('delta', text)
    return seen


def visible(events):
    return ''.join(text for kind, text in events if kind == 'delta')


def test_default_and_disabled_preferences_keep_token_previews(monkeypatch):
    monkeypatch.setattr(output, 'prepare_reply', lambda *_: pytest.fail('Unconfigured previews must not format output'))
    for options in (None, {}, {'reply_max_chars': 0, 'message_max_chars': 0, 'remove_parentheses': False}):
        assert collect(['你', '好'], options) == [('delta', '你'), ('delta', '好')]


def test_adjusted_previews_wait_for_a_complete_authored_paragraph():
    options = {'reply_max_chars': 8}
    assert collect(list('这是一个尚未完成的段落'), options) == []
    text = '这是已经完成的一段。\n\n'
    events = collect(list(text), options)
    assert len(events) == 1
    assert visible(events) == output.prepare_reply(text.rstrip(), {'output': options})['reply']
    assert output.reply_char_count(visible(events)) <= 8


def test_caps_never_flash_excess_text_before_final_result():
    options = {'reply_max_chars': 12, 'message_max_chars': 8}
    text = '第一段内容需要缩短。\n\n第二段内容同样很长。\n\n第三段不得泄出。\n\n'
    events = collect(list(text), options)
    prefix = ''
    for _, delta in events:
        prefix += delta
        assert output.reply_char_count(prefix) <= 12
    assert visible(events) == output.prepare_reply(text.rstrip(), {'output': options})['reply']
    assert '第三段' not in visible(events)


def test_parentheses_across_blank_lines_are_held_and_removed_before_preview():
    text = '您好。（悄悄的动作\n\n这里也不应出现）\n\n正文继续。\n\n末段未完成'
    events = collect(list(text), {'remove_parentheses': True})
    assert visible(events) == '您好。\n\n正文继续。'
    assert not any(word in visible(events) for word in ('悄悄', '动作', '这里', '（', '）', '末段'))


@pytest.mark.parametrize('text', ['还没完成（动作\n\n继续', '未闭合代码\n\n`print(1)\n\n继续',
                                 '```python\nprint(1)\n\n没有关闭', '[未完成的链接\n\n说明'])
def test_unclosed_structures_do_not_expose_their_partial_paragraph(text):
    events = collect(list(text), {'remove_parentheses': True})
    # Already completed prose may appear, but no unfinished structure does.
    assert all(not any(marker in fragment for marker in ('（', '`', '[', 'print(1)'))
               for kind, fragment in events if kind == 'delta')


def test_complete_code_and_links_keep_literal_parentheses():
    options = {'remove_parentheses': True}
    code = '```python\nvalue = ("保留")\n\nprint(value)\n```'
    link = '[说明（保留）](https://example.invalid/a_(b))'
    text = code + '\n\n' + link + '\n\n' + '你好（删除动作）。\n\n'
    events = collect(list(text), options)
    assert visible(events) == output.prepare_reply(text.rstrip(), {'output': options})['reply']
    assert code in visible(events) and link in visible(events)
    assert '删除动作' not in visible(events)


@pytest.mark.parametrize('text', ['{\n"保留": "（不能删除）",\n\n"其他": 1\n}\n\n',
                                 '[\n"（保留）",\n\n1\n]\n\n'])
def test_structured_json_documents_wait_for_authoritative_result(text):
    assert collect(list(text), {'remove_parentheses': True}) == []
    assert collect(list(text), {'reply_max_chars': 20}) == []


def test_private_markers_stay_filtered_with_adjusted_previews():
    text = '<think>秘密\n\n推理</think>你好[7ab675d0]（动作）。\n\n'
    events = collect(list(text), {'remove_parentheses': True}, memory_ids=['7ab675d0-known-memory'])
    assert visible(events) == '你好。'
    assert not any(value in str(events) for value in ('7ab675d0', '秘密', '动作', '推理'))


def test_reset_clears_pending_code_parentheses_and_reply_budget():
    seen = []
    with preview_events(lambda kind, text: seen.append((kind, text))):
        with preview_filter(output_options={'reply_max_chars': 10, 'remove_parentheses': True}):
            emit_preview('delta', '（废弃草稿\n\n')
            emit_preview('reset')
            emit_preview('delta', '安全的新一轮。\n\n')
    assert seen[0] == ('reset', '') and visible(seen) == '安全的新一轮。'
    assert '废弃' not in str(seen)


def test_fragmented_crlf_boundaries_match_final_text():
    text = '第一段（动作）。\r\n\r\n第二段。\r\n\r\n'
    assert visible(collect(list(text), {'remove_parentheses': True})) == '第一段。\n\n第二段。'


def test_formatter_calls_are_per_completed_paragraph_and_strictly_bounded(monkeypatch):
    real, calls = output.prepare_reply, []
    def checked(text, config):
        calls.append(len(text))
        return real(text, config)
    monkeypatch.setattr(output, 'prepare_reply', checked)
    options = {'remove_parentheses': True, 'max_parts': 30}
    collect(list('长' * 5000), options)
    assert calls == [], 'A single unfinished paragraph must not be reparsed per token'
    collect(list('长' * 5000 + '\n\n'), options)
    assert calls == [5000]
    calls.clear()
    collect(list('短句。\n\n' * 200), options)
    assert len(calls) == MAX_ADJUSTED_PREVIEW_CHECKPOINTS
    assert calls == sorted(calls)


def test_oversize_pending_input_stops_preview_and_discards_its_buffer(monkeypatch):
    monkeypatch.setattr(output, 'prepare_reply', lambda *_: pytest.fail('Unfinished input must not be formatted'))
    public = _AdjustedPublicText(output_options={'remove_parentheses': True})
    assert public.feed('长' * MAX_ADJUSTED_PREVIEW_CHARS) == ''
    assert len(public._chars) == MAX_ADJUSTED_PREVIEW_CHARS
    assert public.feed('多') == ''
    assert public._disabled and not public._chars
    assert public.feed('不会恢复\n\n') == ''
    public.reset()
    assert not public._disabled and not public._chars
