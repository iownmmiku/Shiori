"""Publication metadata and RSS parsing use authored network substitutes."""
import pytest

from shiori.tools import _TextParser, _parse_feed, _read_public, public_date


def test_html_dates_keep_publication_update_and_event_separate():
    parser = _TextParser()
    parser.feed('''<html><head><title>音乐制作资讯</title>
        <meta property="article:published_time" content="2026-10-10T09:00:00+08:00">
        <meta property="article:modified_time" content="2026-10-12T10:00:00+08:00">
        <script type="application/ld+json">{"@graph":[{"@type":"MusicEvent","name":"音乐会","startDate":"2026-11-03"}]}</script>
        </head><body><p>音乐团队的新资讯。</p></body></html>''')
    assert parser.published_at.startswith('2026-10-10')
    assert parser.updated_at.startswith('2026-10-12')
    assert parser.event_dates[0]['date'].startswith('2026-11-03')
    assert parser.event_dates[0]['confirmed'] is False
    assert 'startDate' not in ''.join(parser.parts)


def test_no_page_date_does_not_fall_back_to_collection_time():
    parser = _TextParser()
    parser.feed('<title>音乐资讯</title><p>原文没有日期</p>')
    assert parser.published_at == parser.updated_at == ''
    assert public_date('not a date') == ''
    assert public_date('Wed, 14 Oct 2026 09:00:00 GMT').startswith('2026-10-14')


def test_rss_and_atom_preserve_article_dates_but_do_not_fetch_links():
    rss = _parse_feed('''<rss><channel><item><title>音乐资讯</title><link>https://news.example/article</link>
      <pubDate>Wed, 14 Oct 2026 09:00:00 GMT</pubDate><description>音乐新专辑。</description></item></channel></rss>''', 'https://news.example/feed')
    assert rss[0]['published_at'].startswith('2026-10-14')
    atom = _parse_feed('''<feed xmlns="http://www.w3.org/2005/Atom"><entry><title>动画资讯</title>
      <link href="/article"/><published>2026-10-14</published><updated>2026-10-15</updated></entry></feed>''', 'https://news.example/feed')
    assert atom[0]['url'] == 'https://news.example/article' and atom[0]['updated_at'].startswith('2026-10-15')
    with pytest.raises(ValueError, match='XML'):
        _parse_feed('<!DOCTYPE a [<!ENTITY x SYSTEM "file:///secret">]><rss>&x;</rss>', 'https://news.example/feed')


def test_redirected_rss_fetch_revalidates_dns_for_every_hop(monkeypatch):
    import shiori.tools as module
    resolved = []
    def addresses(url):
        resolved.append(url)
        if '127.0.0.1' in url:
            raise ValueError('不能读取本机')
        from urllib.parse import urlsplit
        return urlsplit(url), 'feed.example', 80, ['93.184.216.34']
    class Response:
        status = 302
        def getheader(self, key, default=''):
            return 'http://127.0.0.1/private-feed' if key == 'Location' else default
    class Connection:
        def __init__(self, *args, **kwargs):
            pass
        def request(self, *args, **kwargs):
            pass
        def getresponse(self):
            return Response()
        def close(self):
            pass
    monkeypatch.setattr(module, 'public_addresses', addresses)
    monkeypatch.setattr(module.http.client, 'HTTPConnection', Connection)
    with pytest.raises(ValueError, match='本机'):
        _read_public('http://feed.example/rss')
    assert resolved == ['http://feed.example/rss', 'http://127.0.0.1/private-feed']
