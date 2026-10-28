"""Configured public search with durable attempt limits and bounded transport."""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import http.client
import inspect
import ipaddress
import json
import sqlite3
import time
from urllib.parse import quote, urlencode, urlsplit

from . import tools
from .settings import DEFAULTS, SEARCH_ENDPOINTS, validate_search_endpoint
from .timezones import day_bounds


def _now():
    return datetime.now(timezone.utc)


class SearchError(ValueError):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def _public_url(value):
    if not isinstance(value, str) or not value or len(value) > 3000 or any(ord(char) < 33 for char in value):
        return ''
    try:
        parsed = urlsplit(value)
        host = (parsed.hostname or '').rstrip('.').encode('idna').decode().lower()
        if (parsed.scheme not in ('http', 'https') or not host or parsed.username is not None
                or parsed.password is not None or parsed.port not in (None, 80, 443)
                or host == 'localhost' or host.endswith(('.localhost', '.local', '.internal'))):
            return ''
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            address = None
        if address is not None and not address.is_global:
            return ''
    except (ValueError, UnicodeError):
        return ''
    # Domain names are checked and DNS-pinned again by read_public when read.
    return value


def _request_json(url, *, method, headers, payload=None, params=None, timeout, before_send=None, deadline=None):
    """No redirects or proxy inheritance; credentials stay on the configured host."""
    deadline = min(deadline, time.monotonic() + timeout) if deadline is not None else time.monotonic() + timeout
    if before_send:
        before_send()
    validate_search_endpoint(url)
    if params:
        url += '?' + urlencode(params)
    parsed, host, port, addresses = tools.public_addresses(url)
    if before_send:
        before_send()
    if time.monotonic() >= deadline:
        raise TimeoutError()
    if parsed.scheme != 'https':
        raise SearchError('invalid_endpoint', '搜索服务必须使用公开 HTTPS 地址')
    connection = tools._PinnedHTTPS(host, port, addresses[0])
    connection.timeout = max(.001, deadline - time.monotonic())
    path = quote(parsed.path or '/', safe='/%:@!$&\'()*+,;=-._~')
    if parsed.query:
        path += '?' + parsed.query
    body = None if payload is None else json.dumps(payload, ensure_ascii=False, allow_nan=False).encode('utf-8')
    request_headers = {'Host': '[' + host + ']' if ':' in host else host,
                       'User-Agent': 'shiori public search', 'Accept': 'application/json',
                       'Accept-Encoding': 'identity', **headers}
    if body is not None:
        request_headers['Content-Type'] = 'application/json'
    try:
        if before_send:
            before_send()
        if time.monotonic() >= deadline:
            raise TimeoutError()
        connection.request(method, path, body=body, headers=request_headers)
        response = connection.getresponse()
        if response.status != 200:
            raise SearchError('http_error', '搜索服务暂不可用，请检查服务配置或额度')
        if 'json' not in response.getheader('Content-Type', '').lower():
            raise SearchError('invalid_response', '搜索服务未返回兼容的 JSON 结果')
        raw = bytearray()
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError()
            if connection.sock is not None:
                connection.sock.settimeout(remaining)
            chunk = response.read1(min(16384, 1024 * 1024 + 1 - len(raw)))
            if not chunk:
                break
            raw.extend(chunk)
            if len(raw) > 1024 * 1024:
                raise SearchError('response_too_large', '搜索服务响应超过 1MB 上限')
        try:
            value = json.loads(raw)
        except (ValueError, UnicodeError, RecursionError):
            raise SearchError('invalid_response', '搜索服务未返回兼容的 JSON 结果') from None
        if not isinstance(value, dict):
            raise SearchError('invalid_response', '搜索服务未返回兼容的 JSON 结果')
        return value
    finally:
        connection.close()


class SearchService:
    def __init__(self, store, settings, *, transport=None, public_fn=None):
        self.store, self.settings = store, settings
        self._transport = transport or _request_json
        self._public = public_fn or tools.search_public
        self._default_public = public_fn is None
        self._slots = asyncio.Semaphore(2)
        self._running = self._queued = 0
        self._workers = set()
        self._requests = {}
        self._closing = False

    def _config(self):
        cfg = self.settings.get()
        search = dict(DEFAULTS['search'], **cfg.get('search', {}))
        provider = search['provider']
        source = next((item for item in cfg.get('search_providers', []) if item.get('id') == provider), {})
        return cfg, search, {'id': provider, 'base_url': source.get('base_url') or SEARCH_ENDPOINTS.get(provider, ''),
                             'api_key': source.get('api_key', '')}

    @staticmethod
    def _identity(search, source, cfg):
        return tuple(sorted(search.items())), tuple(sorted(source.items())), cfg.get('time', {}).get('timezone', 'Asia/Shanghai')

    def _current(self, identity):
        cfg, search, source = self._config()
        if self._identity(search, source, cfg) != identity:
            raise SearchError('configuration_changed', '搜索设置已变化，请重新发起搜索')

    def _used(self, cfg):
        start, end = day_bounds(cfg, now=_now())
        with self.store._lock:
            count = self.store._conn.execute("SELECT COUNT(*) FROM events WHERE kind='search_attempt' AND created_at>=? AND created_at<?",
                                             (start, end)).fetchone()[0]
        return count, start, end

    def status(self):
        cfg, search, source = self._config()
        used, start, _ = self._used(cfg)
        return {**search, 'configured': search['provider'] == 'public' or bool(source['api_key']),
                'used': used, 'used_today': used, 'remaining': max(0, search['daily_calls'] - used),
                'day_start': start, 'timezone': cfg.get('time', {}).get('timezone', 'Asia/Shanghai'),
                'running': self._running, 'queued': self._queued}

    def _check_live(self, identity, deadline, state):
        if state.get('abandoned') or self._closing:
            raise asyncio.CancelledError()
        self._current(identity)
        if time.monotonic() >= deadline:
            raise TimeoutError()

    def _write(self, operation, *, deadline, check=None):
        # Another SQLite connection must not hold the application's shared
        # Store lock for its normal multi-second busy timeout. Retry outside it.
        while True:
            if check:
                check()
            if time.monotonic() >= deadline:
                raise TimeoutError()
            with self.store._lock:
                previous = self.store._conn.execute('PRAGMA busy_timeout').fetchone()[0]
                self.store._conn.execute('PRAGMA busy_timeout=25')
                try:
                    with self.store._conn:
                        if not self.store._conn.in_transaction:
                            self.store._conn.execute('BEGIN IMMEDIATE')
                        if check:
                            check()
                        return operation()
                except sqlite3.OperationalError as exc:
                    if 'locked' not in str(exc).lower() and 'busy' not in str(exc).lower():
                        raise
                finally:
                    self.store._conn.execute('PRAGMA busy_timeout=' + str(int(previous)))
            time.sleep(min(.02, max(0, deadline - time.monotonic())))

    def _claim(self, cfg, search, *, identity, deadline, state):
        def reserve():
            self._check_live(identity, deadline, state)
            used, _, _ = self._used(cfg)
            if used >= search['daily_calls']:
                raise SearchError('daily_limit', '今日网页搜索次数已用完，请稍后再试或调整搜索限额')
            return self.store.add_event('search_attempt', '网页搜索尝试', provider=search['provider'], status='running')
        return self._write(reserve, deadline=deadline, check=lambda: self._check_live(identity, deadline, state))

    def _finish(self, record, status, started, *, count=0, code='', state=None):
        if record is None:
            return
        try:
            def finish():
                abandoned = (state or {}).get('abandoned')
                record['details'].update(status=abandoned or status,
                    elapsed_ms=round((time.monotonic() - started) * 1000), result_count=count)
                if abandoned or code:
                    record['details']['code'] = abandoned or code
                self.store._write('events', record, ('kind', 'created_at'))
            self._write(finish, deadline=time.monotonic() + 30)
        except Exception:
            pass

    async def _io(self, function, state, *args, **kwargs):
        task = asyncio.create_task(asyncio.to_thread(function, *args, **kwargs))
        state['io'].add(task)
        return await asyncio.shield(task)

    async def _async_call(self, function, state, *args, **kwargs):
        async def run():
            value = function(*args, **kwargs)
            return await value if inspect.isawaitable(value) else value
        task = asyncio.create_task(run())
        state['orchestration'] = task
        try:
            return await task
        finally:
            state['orchestration'] = None

    async def _public_search(self, query, state, identity, deadline):
        if self._default_public:
            async def fetch(url):
                return await self._io(tools._read_public, state, url,
                    before_send=lambda: self._check_live(identity, deadline, state))
            return await self._async_call(self._public, state, query, fetch=fetch)
        if inspect.iscoroutinefunction(self._public):
            return await self._async_call(self._public, state, query)
        return await self._io(self._public, state, query)

    def _abandon(self, state, reason):
        state['abandoned'] = reason
        orchestration = state.get('orchestration')
        if orchestration is not None:
            orchestration.cancel()

    async def close(self):
        self._closing = True
        current = asyncio.current_task()
        callers = []
        for task, state in list(self._requests.items()):
            self._abandon(state, 'cancelled')
            if task is not current:
                task.cancel()
                callers.append(task)
        if callers:
            await asyncio.gather(*callers, return_exceptions=True)
        if self._workers:
            await asyncio.gather(*list(self._workers), return_exceptions=True)

    def resume(self):
        self._closing = False

    @staticmethod
    def _normalize(query, provider, source, raw, maximum):
        if not isinstance(raw, dict) or raw.get('error'):
            raise SearchError('invalid_response', '搜索服务未返回兼容的搜索结果')
        if provider == 'public':
            candidates = raw.get('links')
            if not isinstance(candidates, list):
                raise SearchError('invalid_response', '公开搜索未返回兼容的搜索结果')
            links = list(dict.fromkeys(url for item in candidates if (url := _public_url(item))))[:maximum]
            return {'query': query, 'source': _public_url(raw.get('source', '')), 'text': str(raw.get('text') or '')[:10000],
                    'links': links, 'channel': str(raw.get('channel') or 'public')[:40],
                    'results': [{'title': '', 'url': url, 'content': ''} for url in links],
                    'failures': [{'channel': item['channel'], 'status': 'unavailable'} for item in raw.get('failures', [])[:3]
                                 if isinstance(item, dict) and item.get('channel') in ('bing_html', 'bing_rss', 'duckduckgo_html')
                                 and item.get('status') == 'unavailable']}
        values = raw.get('web', {}).get('results', []) if provider == 'brave' and isinstance(raw.get('web'), dict) else raw.get('results')
        # Brave omits web when it found no web result; an explicit query still
        # distinguishes that documented empty response from an error envelope.
        if provider == 'brave' and 'web' not in raw and isinstance(raw.get('query'), dict):
            values = []
        if not isinstance(values, list):
            raise SearchError('invalid_response', '搜索服务未返回兼容的搜索结果')
        results, seen = [], set()
        for item in values[:100]:
            if not isinstance(item, dict):
                continue
            url = _public_url(item.get('url'))
            if not url or url in seen:
                continue
            title = item.get('title')
            content = (item.get('summary') or item.get('snippet') or item.get('content') or '') if provider == 'deepseek' else item.get('description' if provider == 'brave' else 'content', '')
            if not isinstance(title, str) or not isinstance(content, str) or not (title.strip() or content.strip()):
                continue
            results.append({'title': title.strip()[:240], 'url': url, 'content': content.strip()[:1000]})
            seen.add(url)
            if len(results) >= maximum:
                break
        if values and not results:
            raise SearchError('invalid_response', '搜索服务没有返回可用的公开结果')
        return {'query': query, 'source': source, 'text': '\n\n'.join(item['title'] + '\n' + item['content'] for item in results)[:10000],
                'links': [item['url'] for item in results], 'channel': provider, 'results': results, 'failures': []}

    async def _execute(self, query, cfg, search, source, identity, deadline, state, started):
        provider = search['provider']
        record, status, code, count = None, 'failed', '', 0
        try:
            self._check_live(identity, deadline, state)
            record = await asyncio.to_thread(self._claim, cfg, search,
                identity=identity, deadline=deadline, state=state)
            self._check_live(identity, deadline, state)
            try:
                if provider == 'public':
                    raw = await self._public_search(query, state, identity, deadline)
                else:
                    headers = {'X-Subscription-Token': source['api_key']} if provider == 'brave' else {'Authorization': 'Bearer ' + source['api_key']}
                    payload = ({'query': query, 'max_results': search['max_results'], 'search_depth': 'basic',
                                'include_answer': False, 'include_raw_content': False, 'auto_parameters': False} if provider == 'tavily' else
                               {'query': query, 'count': search['max_results'], 'need_content': False} if provider == 'deepseek' else None)
                    options = dict(method='GET' if provider == 'brave' else 'POST', headers=headers, payload=payload,
                        params={'q': query, 'count': search['max_results']} if provider == 'brave' else None,
                        timeout=max(.001, deadline - time.monotonic()))
                    if self._transport is _request_json:
                        options.update(deadline=deadline, before_send=lambda: self._check_live(identity, deadline, state))
                    if inspect.iscoroutinefunction(self._transport):
                        raw = await self._async_call(self._transport, state, source['base_url'], **options)
                    else:
                        raw = await self._io(self._transport, state, source['base_url'], **options)
                self._check_live(identity, deadline, state)
                result = self._normalize(query, provider, source['base_url'], raw, search['max_results'])
            except asyncio.CancelledError:
                raise
            except Exception:
                self._check_live(identity, deadline, state)
                if provider == 'public' or not search['fallback_public']:
                    raise
                raw = await self._public_search(query, state, identity, deadline)
                self._check_live(identity, deadline, state)
                result = self._normalize(query, 'public', '', raw, search['max_results'])
                result['failures'] = [{'channel': provider, 'status': 'unavailable'}, *result['failures']]
            status, count = 'fallback' if result['failures'] else 'success', len(result['links'])
            return result
        except asyncio.CancelledError:
            status, code = state.get('abandoned') or 'cancelled', state.get('abandoned') or 'cancelled'
            raise
        except TimeoutError:
            status, code = 'timeout', 'timeout'
            raise SearchError('timeout', '网页搜索超过等待期限，请稍后重试') from None
        except Exception as exc:
            code = exc.code if isinstance(exc, SearchError) else 'provider_unavailable'
            if isinstance(exc, SearchError):
                raise
            raise SearchError('provider_unavailable', '搜索服务暂不可用，请检查服务配置、网络或额度') from None
        finally:
            await asyncio.to_thread(self._finish, record, status, started, count=count, code=code, state=state)
            if state['io']:
                await asyncio.gather(*state['io'], return_exceptions=True)

    async def search(self, query):
        if self._closing:
            raise SearchError('closing', '网页搜索正在停止，请稍后重试')
        if not isinstance(query, str) or not 1 <= len(query.strip()) <= 300:
            raise SearchError('invalid_query', '搜索内容需为 1—300 个字符')
        query = query.strip()
        cfg, search, source = self._config()
        provider = search['provider']
        if not search['enabled']:
            raise SearchError('disabled', '网页搜索已停用')
        if provider not in ('public', *SEARCH_ENDPOINTS):
            raise SearchError('invalid_provider', '当前搜索服务配置无效')
        if provider == 'deepseek' and len(query) > 128:
            raise SearchError('invalid_query', 'DeepSeek 搜索内容需为 1—128 个字符，请缩短后重试')
        if provider != 'public':
            validate_search_endpoint(source['base_url'])
            if not source['api_key']:
                raise SearchError('not_configured', '当前搜索服务尚未配置独立 API 密钥')
        if self._queued >= 16:
            raise SearchError('queue_full', '网页搜索排队数量已满，请稍后再试')
        identity = self._identity(search, source, cfg)
        started, acquired, controller = time.monotonic(), False, None
        deadline = started + search['timeout_seconds']
        state = {'io': set()}
        caller = asyncio.current_task()
        self._requests[caller] = state
        self._queued += 1
        try:
            async with asyncio.timeout(search['timeout_seconds']):
                await self._slots.acquire()
                acquired = True
                self._queued -= 1
                self._running += 1
                controller = asyncio.create_task(self._execute(query, cfg, search, source, identity, deadline, state, started))
                self._workers.add(controller)
                def completed(done):
                    self._workers.discard(done)
                    self._running -= 1
                    self._slots.release()
                    if not done.cancelled():
                        done.exception()
                controller.add_done_callback(completed)
                return await asyncio.shield(controller)
        except asyncio.CancelledError:
            self._abandon(state, 'cancelled')
            raise
        except TimeoutError:
            self._abandon(state, 'timeout')
            raise SearchError('timeout', '网页搜索超过等待期限，请稍后重试') from None
        finally:
            self._requests.pop(caller, None)
            if not acquired:
                self._queued -= 1
            elif controller is None:
                self._running -= 1
                self._slots.release()
