"""Inline Markdown destinations are indivisible at arbitrary nesting depths."""
import pytest

from shiori.output import (_atoms, OutputLimitError, model_reply_parts,
                            prepare_reply, remove_parenthetical_content,
                            reply_char_count, split_output, strip_memory_citations)


LINKS = [
    '[target](https://example.test/a(b))',
    '[target](https://example.test/a(b(c(d)e)f)g)',
    '[target](https://example.test/' + '(' * 64 + 'payload' + ')' * 64 + ')',
    r'[target](https://example.test/a\(b\))',
    r'[target](https://example.test/a(b\(c\)d)e)',
    '[target](https://example.test/a(b) "title (not a delimiter)")',
    '[target](<https://example.test/a)b(c)>)',
    r'[escaped\]label](https://example.test/a(b))',
]


@pytest.mark.parametrize('link', LINKS)
def test_complete_nested_and_escaped_links_are_single_atoms(link):
    assert list(_atoms(link)) == [link]
    assert remove_parenthetical_content(link + '(remove)') == link
    assert model_reply_parts('before\n\n' + link + '\n\nafter') == ['before', link, 'after']


@pytest.mark.parametrize('link', LINKS)
@pytest.mark.parametrize('extra', [-1, 0, 1, 2])
def test_strict_display_cap_never_returns_a_partial_nested_link(link, extra):
    limit = reply_char_count(link) + extra
    source = link + ' ' + 'tail' * 100
    result = prepare_reply(source, {'output': {'reply_max_chars': limit}})
    assert reply_char_count(result['reply']) <= limit
    assert result['original_reply'] == source
    if link not in result['reply']:
        assert 'https://' not in result['reply'] and 'target' not in result['reply']
    else:
        assert result['reply'].startswith(link)


def test_authored_boundaries_skip_complete_link_destinations_with_blank_lines():
    # Conservatively treat a balanced destination as opaque even if a model
    # used blank lines that a strict Markdown renderer might reject.
    link = '[label](https://example.test/a(inside\n\nparentheses)\n\n "title")'
    assert list(_atoms(link)) == [link]
    assert model_reply_parts('first\n\n' + link + '\n\nlast') == ['first', link, 'last']


def test_unfinished_link_destination_is_not_cut_or_parenthesis_filtered():
    link = '[label](https://example.test/a(b) unfinished\n\nrest'
    assert list(_atoms(link)) == [link]
    assert remove_parenthetical_content(link) == link
    assert model_reply_parts(link) == [link]
    result = prepare_reply(link, {'output': {'reply_max_chars': 10}})
    assert 'https://' not in result['reply'] and result['original_reply'] == link


def test_transport_does_not_break_a_nested_link_that_exceeds_the_hard_cap():
    link = '[label](https://example.test/' + '(' * 30 + 'x' + ')' * 30 + ')'
    with pytest.raises(OutputLimitError):
        split_output(link, {'output': {'platform_max_chars': 40}})
    physical = split_output('a' * 35 + link + 'b' * 35,
                            {'output': {'platform_max_chars': len(link) + 2}})
    assert any(link in part for part in physical)
    assert ''.join(physical) == 'a' * 35 + link + 'b' * 35


def test_citation_removal_respects_the_entire_nested_link_span():
    key = '7ab675d0-memory-id'
    link = '[7ab675d0](https://example.test/a([7ab675d0]))'
    assert strip_memory_citations(link + ' outside[7ab675d0]', [key]) == link + ' outside'


def test_code_and_complete_json_keep_existing_protection():
    code = '````md\n```python\nprint("[x](a(b))")\n\n```\n````'
    assert list(_atoms(code)) == [code]
    assert model_reply_parts(code + '\n\nlast') == [code, 'last']
    unfinished = '```python\nvalue = (1)\n\nnot closed'
    assert list(_atoms(unfinished)) == [unfinished]
    assert remove_parenthetical_content(unfinished) == unfinished
    document = '{"url": "[x](https://example.test/a(b))",\n\n"value": "(keep)"}'
    assert model_reply_parts(document) == [document]
    assert remove_parenthetical_content(document) == document
