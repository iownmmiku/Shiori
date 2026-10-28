"""Reduce excessive model paragraphs without inventing message boundaries."""
from itertools import combinations

import pytest

from shiori.output import _balanced_authored_parts, model_reply_parts


def config(maximum):
    return {'output': {'mode': 'model', 'max_parts': maximum, 'platform_max_chars': 20000}}


def test_many_short_authored_parts_do_not_accumulate_in_the_last_message():
    original = [f'段落{index:02d}。' for index in range(30)]
    parts = model_reply_parts('\n\n'.join(original), config(8))
    assert len(parts) == 8
    assert '\n\n'.join(parts) == '\n\n'.join(original)
    assert max(map(len, parts)) == 4 * len(original[0]) + 6
    assert len(parts[-1]) <= max(map(len, parts[:-1]))


@pytest.mark.parametrize('lengths,maximum', [
    ([1, 1, 1, 1], 2),
    ([1, 9, 2, 8, 3, 7], 3),
    ([100, 2, 3, 4, 2], 3),
    ([2, 4, 20, 4, 2], 2),
    ([1, 1, 1, 100, 1, 1, 1], 4),
    ([5, 3, 15, 1, 2, 8, 2], 5),
])
def test_merge_minimizes_longest_group_without_reordering_or_losing_text(lengths, maximum):
    original = [chr(0x4e00 + index) * length for index, length in enumerate(lengths)]
    parts = _balanced_authored_parts(original, maximum)
    # An independent exhaustive oracle considers every legal cut for these
    # small examples, including the two preserved separator characters.
    best = min(max(len('\n\n'.join(original[start:end]))
                   for start, end in zip((0, *cuts), (*cuts, len(original))))
               for cuts in combinations(range(1, len(original)), maximum - 1))
    assert len(parts) == maximum
    assert max(map(len, parts)) == best
    assert '\n\n'.join(parts) == '\n\n'.join(original)
    assert _balanced_authored_parts(parts, maximum) == parts
    assert model_reply_parts('\n\n'.join(parts), config(maximum)) == parts


def test_one_large_authored_paragraph_remains_whole():
    giant = '这是模型写出的同一段完整内容。' * 200
    assert model_reply_parts(giant, config(2)) == [giant]
    original = ['开头', giant, '继续', '结尾']
    parts = model_reply_parts('\n\n'.join(original), config(2))
    assert len(parts) == 2
    assert any(giant in part for part in parts)
    assert '\n\n'.join(parts) == '\n\n'.join(original)


def test_merging_protects_code_blank_lines_and_complete_json():
    code = '```python\nitems = [1, 2]\n\nprint(items)\n```'
    original = ['开头', code, '说明', '结尾']
    parts = model_reply_parts('\n\n'.join(original), config(2))
    assert len(parts) == 2
    assert any(code in part for part in parts)
    assert '\n\n'.join(parts) == '\n\n'.join(original)
    document = '{\n  "first": 1,\n\n  "second": [2, 3]\n}'
    assert model_reply_parts(document, config(1)) == [document]


def test_one_allowed_message_preserves_all_authored_paragraphs():
    text = '第一段\n\n第二段\n\n第三段'
    assert model_reply_parts(text, config(1)) == [text]
    assert model_reply_parts('嗯', config(8)) == ['嗯']
    assert model_reply_parts(' \n\n\t ', config(8)) == []
