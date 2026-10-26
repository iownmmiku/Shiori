"""Bounded local tools and public web fetching with DNS-pinned connections."""
from __future__ import annotations

import ast
import asyncio
import base64
import http.client
import ipaddress
import math
import operator
import json
import re
import socket
import ssl
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from urllib.parse import parse_qs, quote, urljoin, urlsplit
from xml.etree import ElementTree


def public_date(value):
    """Normalize supplied publication metadata; collection time is never a substitute."""
    if not isinstance(value, str) or len(value) > 100 or not value.strip():
        return ''
    try:
        result = datetime.fromisoformat(value.strip().replace('Z', '+00:00'))
    except ValueError:
        try:
            result = parsedate_to_datetime(value)
        except (ValueError, TypeError, OverflowError):
            return ''
    if result.tzinfo is None:
        result = result.replace(tzinfo=timezone.utc)
    return result.isoformat()


def calculate(expression: str) -> dict:
    if len(expression) > 200:
        raise ValueError('计算表达式过长')
    expression = expression.replace('×', '*').replace('÷', '/').replace('^', '**')
    ops = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul,
           ast.Div: operator.truediv, ast.FloorDiv: operator.floordiv, ast.Mod: operator.mod, ast.Pow: operator.pow}
    count = 0
    def visit(node):
        nonlocal count
        count += 1
        if count > 40:
            raise ValueError('计算过于复杂')
        if isinstance(node, ast.Constant) and type(node.value) in (int, float):
            value = node.value
        elif isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
            value = visit(node.operand) * (-1 if isinstance(node.op, ast.USub) else 1)
        elif isinstance(node, ast.BinOp) and type(node.op) in ops:
            left, right = visit(node.left), visit(node.right)
            if isinstance(node.op, ast.Pow) and (abs(right) > 16 or abs(left) > 1e8):
                raise ValueError('指数计算超过限制')
            value = ops[type(node.op)](left, right)
        else:
            raise ValueError('仅支持数字、括号与加减乘除等基本运算')
        if isinstance(value, complex) or not math.isfinite(value) or abs(value) > 1e100:
            raise ValueError('计算结果超过限制')
        return value
    try:
        result = visit(ast.parse(expression, mode='eval').body)
        return {'expression': expression, 'result': result}
    except (SyntaxError, ZeroDivisionError, OverflowError) as exc:
        raise ValueError('表达式格式错误或无法计算') from exc


def public_addresses(url: str):
    parsed = urlsplit(url)
    if parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError('仅支持公开的 HTTP/HTTPS 网页')
    if len(url) > 3000 or parsed.port not in (None, 80, 443):
        raise ValueError('网页地址或端口不受支持')
    host = parsed.hostname.rstrip('.').encode('idna').decode()
    if host == 'localhost' or host.endswith(('.localhost', '.local', '.internal')):
        raise ValueError('不能读取本机或内部网络地址')
    port = parsed.port or (443 if parsed.scheme == 'https' else 80)
    try:
        addresses = list(dict.fromkeys(x[4][0] for x in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)))
    except socket.gaierror as exc:
        raise ValueError('无法解析网页地址') from exc
    if not addresses or any(not ipaddress.ip_address(x).is_global for x in addresses):
        raise ValueError('不能读取本机、私网或保留地址')
    return parsed, host, port, addresses


class _PinnedHTTPS(http.client.HTTPSConnection):
    def __init__(self, host, port, address):
        super().__init__(host, port, timeout=12, context=ssl.create_default_context())
        self.address = address
    def connect(self):
        raw = socket.create_connection((self.address, self.port), self.timeout)
        self.sock = self._context.wrap_socket(raw, server_hostname=self.host)


class _TextParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts, self.hidden, self.title, self.in_title, self.links = [], 0, [], False, []
        self.search_links, self.in_result_heading = [], False
        self.published_at, self.updated_at, self.event_dates = '', '', []
        self._jsonld = None
    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        if tag == 'meta':
            name = (attributes.get('property') or attributes.get('name') or attributes.get('itemprop') or '').lower()
            value = public_date(attributes.get('content', ''))
            if name in ('article:published_time', 'datepublished', 'date', 'pubdate') and value:
                self.published_at = self.published_at or value
            if name in ('article:modified_time', 'datemodified', 'last-modified') and value:
                self.updated_at = self.updated_at or value
        if tag == 'time':
            name = attributes.get('itemprop', '').lower()
            value = public_date(attributes.get('datetime', ''))
            if name == 'datepublished' and value:
                self.published_at = self.published_at or value
            if name == 'datemodified' and value:
                self.updated_at = self.updated_at or value
        if tag == 'script' and attributes.get('type', '').lower() == 'application/ld+json':
            self._jsonld = []
        if tag in ('script', 'style', 'noscript', 'svg'):
            self.hidden += 1
        if tag == 'title':
            self.in_title = True
        if tag == 'h2':
            self.in_result_heading = True
        if tag == 'a' and not self.hidden:
            href = dict(attrs).get('href') or ''
            if len(self.links) < 120 and len(href) <= 3000 and href.startswith(('http://', 'https://', '/')):
                self.links.append(href)
            if (len(self.search_links) < 30 and len(href) <= 3000
                    and (self.in_result_heading or 'result__a' in (attributes.get('class') or '').split())):
                self.search_links.append(href)
        if tag in ('p', 'div', 'br', 'li', 'h1', 'h2', 'h3', 'article', 'section'):
            self.parts.append('\n')
    def handle_endtag(self, tag):
        if tag == 'script' and self._jsonld is not None:
            try:
                value = json.loads(''.join(self._jsonld))
                self._metadata(value)
            except (ValueError, RecursionError, TypeError):
                pass
            self._jsonld = None
        if tag in ('script', 'style', 'noscript', 'svg'):
            self.hidden = max(0, self.hidden - 1)
        if tag == 'title':
            self.in_title = False
        if tag == 'h2':
            self.in_result_heading = False
    def handle_data(self, data):
        if self._jsonld is not None and sum(map(len, self._jsonld)) < 100000:
            self._jsonld.append(data[:100000])
        if not self.hidden:
            self.parts.append(data)
            if self.in_title:
                self.title.append(data)

    def _metadata(self, value, depth=0):
        if depth > 8:
            return
        if isinstance(value, list):
            for entry in value[:100]:
                self._metadata(entry, depth + 1)
        elif isinstance(value, dict):
            self.published_at = self.published_at or public_date(value.get('datePublished'))
            self.updated_at = self.updated_at or public_date(value.get('dateModified'))
            date = public_date(value.get('startDate'))
            if date and len(self.event_dates) < 10:
                self.event_dates.append({'date': date, 'label': str(value.get('name', '活动日期'))[:100],
                                         'source': 'structured_metadata', 'confirmed': False})
            for name in ('@graph', 'mainEntity', 'subjectOf'):
                if name in value:
                    self._metadata(value[name], depth + 1)


def _parse_feed(text, base_url):
    """RSS/Atom discovery only; every article still passes pinned web fetching."""
    if re.search(r'<!\s*(?:DOCTYPE|ENTITY)', text, re.I):
        raise ValueError('订阅源包含不支持的 XML 声明')
    try:
        root = ElementTree.fromstring(text)
    except ElementTree.ParseError as exc:
        raise ValueError('订阅源格式无效') from exc
    name = lambda tag: tag.rsplit('}', 1)[-1].lower()
    entries = []
    for element in root.iter():
        if name(element.tag) not in ('item', 'entry'):
            continue
        item = {'url': '', 'title': '', 'text': '', 'published_at': '', 'updated_at': ''}
        for child in element:
            tag, value = name(child.tag), ''.join(child.itertext()).strip()
            if tag == 'link' and child.get('rel', 'alternate') == 'alternate':
                item['url'] = urljoin(base_url, child.get('href') or value)
            elif tag == 'title':
                item['title'] = value[:240]
            elif tag in ('description', 'summary', 'content'):
                parser = _TextParser()
                parser.feed(value)
                item['text'] = ''.join(parser.parts)[:3000]
            elif tag in ('pubdate', 'published'):
                item['published_at'] = public_date(value)
            elif tag == 'updated':
                item['updated_at'] = public_date(value)
        if item['url'] and len(entries) < 80:
            entries.append(item)
    return entries


def _read_public(url: str, *, before_send=None):
    for _ in range(4):
        if before_send:
            before_send()
        parsed, host, port, addresses = public_addresses(url)
        if before_send:
            before_send()
        connection = _PinnedHTTPS(host, port, addresses[0]) if parsed.scheme == 'https' else http.client.HTTPConnection(addresses[0], port, timeout=12)
        path = quote(parsed.path or '/', safe='/%:@!$&\'()*+,;=-._~') + ('?' + quote(parsed.query, safe='/%:@!$&\'()*+,;=-._~?') if parsed.query else '')
        try:
            if before_send:
                before_send()
            connection.request('GET', path, headers={'Host': host if parsed.port is None else f'{host}:{port}', 'User-Agent': 'mikubot (+local assistant)', 'Accept': 'text/html,text/plain,application/json,application/rss+xml,application/atom+xml', 'Accept-Encoding': 'identity'})
            response = connection.getresponse()
            if response.status in (301, 302, 303, 307, 308):
                location = response.getheader('Location')
                if not location:
                    raise ValueError('网页重定向无目标')
                url = urljoin(url, location)
                continue
            if response.status != 200:
                raise ValueError(f'网页返回 HTTP {response.status}')
            content_type = response.getheader('Content-Type', '')
            if not any(x in content_type for x in ('text/', 'application/json', 'application/xhtml', 'application/rss+xml', 'application/atom+xml', 'application/xml')):
                raise ValueError('当前仅支持文本网页，不能下载此类型附件')
            raw = response.read(1024 * 1024 + 1)
            if len(raw) > 1024 * 1024:
                raise ValueError('网页超过 1MB 读取限制')
            charset = 'utf-8'
            if 'charset=' in content_type:
                charset = content_type.split('charset=', 1)[1].split(';')[0].strip(' "\'')
            try:
                text = raw.decode(charset, errors='replace')
            except LookupError:
                text = raw.decode('utf-8', errors='replace')
            parser = _TextParser()
            feed_items = []
            if 'html' in content_type:
                parser.feed(text)
                text = '\n'.join(x.strip() for x in ''.join(parser.parts).splitlines() if x.strip())
            elif 'xml' in content_type:
                feed_items = _parse_feed(text, url)
                text = '\n'.join(item['title'] + '\n' + item['text'] for item in feed_items)
            links, link_chars = [], 0
            for href in parser.links:
                resolved = urljoin(url, href)
                if link_chars + len(resolved) > 16000:
                    break
                links.append(resolved)
                link_chars += len(resolved)
            return {'url': url, 'title': ''.join(parser.title).strip() or host, 'text': text[:16000], 'truncated': len(text) > 16000,
                    'links': links, 'search_links': [urljoin(url, href) for href in parser.search_links], 'published_at': parser.published_at,
                    'updated_at': parser.updated_at or public_date(response.getheader('Last-Modified', '')),
                    'event_dates': parser.event_dates, 'feed_items': feed_items}
        except (OSError, http.client.HTTPException) as exc:
            raise ValueError('网页读取失败或超时') from exc
        finally:
            connection.close()
    raise ValueError('网页重定向次数超过限制')


async def read_public(url):
    return await asyncio.to_thread(_read_public, str(url))


def _request_public_json(url, payload=None):
    """Bounded public API requests with the same DNS pinning as web reads.

    No credentials, cookies or redirects. POST is used only for public search;
    a redirect must never replay even this small query body to another host.
    """
    parsed, host, port, addresses = public_addresses(url)
    if parsed.scheme != 'https':
        raise ValueError('资料接口必须使用 HTTPS')
    body = None if payload is None else json.dumps(payload, ensure_ascii=False).encode('utf-8')
    if body is not None and len(body) > 4096:
        raise ValueError('资料检索参数过长')
    connection = _PinnedHTTPS(host, port, addresses[0])
    path = quote(parsed.path or '/', safe='/%:@!$&\'()*+,;=-._~') + ('?' + parsed.query if parsed.query else '')
    headers = {'Host': host, 'User-Agent': 'mikubot/0.1 (https://github.com/iownmmiku/mikubot)',
               'Accept': 'application/json', 'Accept-Encoding': 'identity'}
    if body is not None:
        headers['Content-Type'] = 'application/json'
    try:
        connection.request('GET' if body is None else 'POST', path, body=body, headers=headers)
        response = connection.getresponse()
        if response.status != 200:
            raise ValueError(f'资料接口返回 HTTP {response.status}')
        if 'json' not in response.getheader('Content-Type', '').lower():
            raise ValueError('资料接口未返回 JSON，站点可能需要验证')
        raw = response.read(1024 * 1024 + 1)
        if len(raw) > 1024 * 1024:
            raise ValueError('资料接口超过 1MB 读取限制')
        value = json.loads(raw)
        if not isinstance(value, (dict, list)):
            raise ValueError('资料接口返回格式无效')
        return value
    except (OSError, http.client.HTTPException, UnicodeError, json.JSONDecodeError, RecursionError) as exc:
        raise ValueError('资料接口读取失败或格式无效') from exc
    finally:
        connection.close()


async def request_public_json(url, payload=None):
    return await asyncio.to_thread(_request_public_json, str(url), payload)


def _domain(host, domain):
    return host == domain or host.endswith('.' + domain)


def _search_result_url(value):
    """Decode Bing's known /ck/a u=a1 redirect without following it.

    Discovery does not resolve hosts. Opening a result always goes through
    read_public's DNS-pinned checks, including after HTTP redirects.
    """
    if not isinstance(value, str) or len(value) > 5000 or any(ord(ch) < 33 for ch in value):
        return ''
    try:
        url = urljoin('https://www.bing.com/', value) if value.startswith('/') else value
        parsed = urlsplit(url)
        host = (parsed.hostname or '').rstrip('.').lower()
        if _domain(host, 'bing.com') and parsed.path.rstrip('/') == '/ck/a':
            encoded = parse_qs(parsed.query, max_num_fields=30).get('u', [''])
            if len(encoded) != 1 or not encoded[0].startswith('a1') or len(encoded[0]) > 4200:
                return ''
            payload = encoded[0][2:]
            url = base64.b64decode(payload + '=' * (-len(payload) % 4), altchars=b'-_', validate=True).decode('utf-8')
            parsed = urlsplit(url)
            host = (parsed.hostname or '').rstrip('.').lower()
        if _domain(host, 'duckduckgo.com') and parsed.path.rstrip('/') == '/l':
            values = parse_qs(parsed.query, max_num_fields=30).get('uddg', [])
            if len(values) != 1:
                return ''
            url = values[0]
            parsed = urlsplit(url)
            host = (parsed.hostname or '').rstrip('.').lower()
        if (not host or parsed.scheme not in ('http', 'https') or parsed.username or parsed.password
                or parsed.port not in (None, 80, 443) or len(url) > 3000 or any(ord(ch) < 33 for ch in url)):
            return ''
        if any(_domain(host, domain) for domain in ('bing.com', 'microsoft.com', 'duckduckgo.com')):
            return ''
        if host == 'localhost' or host.endswith(('.localhost', '.local', '.internal')):
            return ''
        try:
            if not ipaddress.ip_address(host).is_global:
                return ''
        except ValueError:
            host.encode('idna')
        return url
    except (ValueError, UnicodeError, TypeError):
        return ''


async def search_public(query, *, fetch=None):
    if not query.strip() or len(query) > 300:
        raise ValueError('搜索内容需为 1—300 个字符')
    channels = [
        ('bing_html', 'https://www.bing.com/search?q=' + quote(query)),
        ('bing_rss', 'https://www.bing.com/search?format=rss&q=' + quote(query)),
        ('duckduckgo_html', 'https://html.duckduckgo.com/html/?q=' + quote(query)),
    ]
    failures = []
    fetch = fetch or read_public
    for channel, address in channels:
        try:
            page = await asyncio.wait_for(fetch(address), timeout=6)
            body = str(page.get('text') or '')
            if (not page.get('feed_items') and len(body) < 100) or any(x in body.lower() for x in ('verify you are human', 'unusual traffic', '请完成验证', 'anomaly-modal')):
                raise ValueError('搜索渠道需要验证或暂不可用')
            candidates = (page.get('search_links') or [item.get('url') for item in page.get('feed_items', []) if isinstance(item, dict)]
                          or page.get('links', []))
            urls = []
            for candidate in candidates[:120]:
                result = _search_result_url(candidate)
                if result and result not in urls:
                    urls.append(result)
                    if len(urls) == 10:
                        break
            if not urls:
                raise ValueError('搜索渠道没有返回可用公开链接')
            return {'query': query, 'source': page['url'], 'text': body[:10000], 'links': urls,
                    'channel': channel, 'failures': failures}
        except (ValueError, OSError, TimeoutError):
            failures.append({'channel': channel, 'status': 'unavailable'})
    raise ValueError('搜索渠道没有返回可用公开链接；渠道可能超时、需要验证或页面格式已变化，请直接提供资料链接后重试')
