import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

from shiori.timeparse import HK, parse_due
from shiori.tools import calculate, public_addresses


def test_bing_redirects_are_decoded_only_for_known_host_and_path():
    import base64
    from shiori.tools import _search_result_url
    target = 'https://docs.python.org/3/?q=中文'
    encoded = 'a1' + base64.urlsafe_b64encode(target.encode()).decode().rstrip('=')
    assert _search_result_url('https://www.bing.com/ck/a?!&&u=' + encoded + '&ntb=1') == target
    assert _search_result_url('/ck/a?u=' + encoded) == target
    unrelated = 'https://example.org/ck/a?u=' + encoded
    assert _search_result_url(unrelated) == unrelated
    assert not _search_result_url('https://bing.com/other?u=' + encoded)
    assert not _search_result_url('https://bing.com/ck/a?u=a1bad%%%')
    assert not _search_result_url('https://bing.com/ck/a?u=' + encoded + '&u=' + encoded)


def test_search_link_domain_boundaries_and_invalid_destinations():
    import base64
    from shiori.tools import _search_result_url
    assert _search_result_url('https://notbing.com/page')
    assert _search_result_url('https://microsoft.com.example.org/page')
    for url in ('https://cn.bing.com/search', 'https://www.microsoft.com/page', 'http://localhost/private',
                'http://192.168.1.1/', 'http://[::1]/', 'file:///secret', 'javascript:alert(1)',
                'https://user:pass@example.org/private', 'https://example.org:22/secret', 'https://example.org/\nsecret'):
        assert not _search_result_url(url)
        encoded = 'a1' + base64.urlsafe_b64encode(url.encode()).decode().rstrip('=')
        assert not _search_result_url('https://www.bing.com/ck/a?u=' + encoded)
    assert not _search_result_url('https://example.org/' + 'x' * 4000)


def test_search_parsing_is_bounded_and_does_not_invent_empty_results(monkeypatch):
    from shiori import tools
    async def page(url):
        return {'url': url, 'text': '真实搜索结果正文。' * 100,
                'links': ['https://www.bing.com/internal'] * 35 + ['https://notbing.com/source'] * 2 +
                         ['https://result.example/' + str(i) for i in range(200)]}
    monkeypatch.setattr(tools, 'read_public', page)
    result = asyncio.run(tools.search_public('动画公开资料'))
    assert len(result['links']) == 10 and result['links'][0] == 'https://notbing.com/source'
    async def empty(url):
        return {'url': url, 'text': '没有可用的搜索结果。' * 100, 'links': ['https://www.bing.com/internal']}
    monkeypatch.setattr(tools, 'read_public', empty)
    with pytest.raises(ValueError, match='没有返回可用公开链接'):
        asyncio.run(tools.search_public('没有结果'))


def test_html_relative_links_are_kept_with_finite_collection():
    from shiori.tools import _TextParser
    parser = _TextParser()
    parser.feed('<a href="/ck/a?u=a1abc">结果</a>' + '<a href="https://example.org/result">链接</a>' * 500)
    assert parser.links[0].startswith('/ck/a?') and len(parser.links) == 120


def test_calculator_limits_and_no_eval():
    assert calculate('(12+8)*3')['result'] == 60
    for value in ('__import__("os").system("whoami")', '2**100000', '1/0', '[1][0]'):
        with pytest.raises(ValueError): calculate(value)


def test_public_web_rejects_internal_and_mixed_dns():
    for url in ('file:///etc/passwd', 'http://localhost', 'http://127.0.0.1', 'http://169.254.169.254', 'http://[::1]', 'http://10.0.0.1', 'http://example.com:22'):
        with pytest.raises(ValueError): public_addresses(url)
    fake_dns = [(2, 1, 6, '', ('8.8.8.8', 443)), (2, 1, 6, '', ('127.0.0.1', 443))]
    with patch('socket.getaddrinfo', return_value=fake_dns):
        with pytest.raises(ValueError): public_addresses('https://example.org')


def test_time_parser_uses_hk_and_requires_explicit_date():
    now = datetime(2026, 10, 15, 14, 0, tzinfo=HK)
    due, _, repeat = parse_due('明晚八点提醒我交作业', now)
    assert datetime.fromisoformat(due).astimezone(HK) == datetime(2026, 10, 16, 20, tzinfo=HK)
    due, _, repeat = parse_due('每天晚上9点提醒我喝水', now)
    assert repeat == 'daily'
    assert parse_due('八点提醒我', now) is None
    with pytest.raises(ValueError): parse_due('今天上午9点提醒我', now)


def test_secrets_protected_and_redacted(tmp_path):
    from shiori.settings import Settings
    secret = 'test-secret-not-real-12345'
    settings = Settings(tmp_path)
    settings.update({'model': {'api_key': secret, 'base_url': 'https://example.com/v1', 'model': 'test'}})
    assert secret not in settings.path.read_text('utf-8')
    assert 'api_key' not in settings.public()['model']
    assert Settings(tmp_path).get()['model']['api_key'] == secret
    settings.update({'model': {'api_key': ''}})
    assert settings.get()['model']['api_key'] == secret
    settings.update({'model': {'clear_api_key': True}})
    assert not settings.get()['model']['api_key']
