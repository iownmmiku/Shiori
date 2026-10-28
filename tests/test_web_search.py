"""Configured search protocols, persistence and real worker admission; no API calls."""
import asyncio
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
import socket
import sqlite3
import threading
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from shiori import tools
from shiori.db import Store
import shiori.db as db_module
import shiori.search as search_module
from shiori.search import SearchError, SearchService, _request_json
from shiori.settings import DEFAULTS, SEARCH_ENDPOINTS, Settings


@pytest.fixture
def anyio_backend():
    return 'asyncio'


@pytest.fixture
def env(tmp_path):
    config = deepcopy(DEFAULTS)
    config['search']['provider'] = 'tavily'
    config['search_providers'] = [{'id': key, 'base_url': endpoint, 'api_key': key + '-private-token'}
                                  for key, endpoint in SEARCH_ENDPOINTS.items()]
    store = Store(tmp_path)
    value = SimpleNamespace(config=config, settings=SimpleNamespace(get=lambda: deepcopy(config)), store=store)
    try:
        yield value
    finally:
        store.close()


def response(provider='tavily'):
    row = {'title': 'Source title', 'url': 'https://source.example/article',
           'content': 'Supported source excerpt', 'description': 'Brave excerpt',
           'summary': 'DeepSeek summary', 'snippet': 'Shorter snippet'}
    return {'web': {'results': [row]}} if provider == 'brave' else {'results': [row]}


@pytest.mark.anyio
@pytest.mark.parametrize('provider', ['tavily', 'deepseek', 'brave'])
async def test_protocol_uses_only_the_selected_provider_key_and_normalizes_actual_results(env, provider):
    env.config['search']['provider'] = provider
    transport = AsyncMock(return_value=response(provider))
    service = SearchService(env.store, env.settings, transport=transport)
    try:
        result = await service.search('公开资料')
        assert result['links'] == ['https://source.example/article']
        assert result['channel'] == provider and result['results'][0]['title'] == 'Source title'
        assert result['source'] == SEARCH_ENDPOINTS[provider]
        options = transport.call_args.kwargs
        if provider == 'brave':
            assert options['method'] == 'GET'
            assert options['headers'] == {'X-Subscription-Token': 'brave-private-token'}
            assert options['params'] == {'q': '公开资料', 'count': 5}
            assert options['payload'] is None
        else:
            assert options['method'] == 'POST'
            assert options['headers'] == {'Authorization': 'Bearer ' + provider + '-private-token'}
            assert options['params'] is None
            if provider == 'tavily':
                assert options['payload'] == {'query': '公开资料', 'max_results': 5, 'search_depth': 'basic',
                    'include_answer': False, 'include_raw_content': False, 'auto_parameters': False}
            else:
                assert options['payload'] == {'query': '公开资料', 'count': 5, 'need_content': False}
                assert result['results'][0]['content'] == 'DeepSeek summary'
        assert service.status()['used_today'] == 1
        events = json.dumps(env.store.list_events(), ensure_ascii=False)
        assert '公开资料' not in events and 'private-token' not in events
    finally:
        await service.close()


@pytest.mark.anyio
@pytest.mark.parametrize('provider', ['tavily', 'deepseek', 'brave'])
async def test_empty_api_results_are_successful_and_never_trigger_public_fallback(env, provider):
    env.config['search'].update(provider=provider, fallback_public=True)
    public = AsyncMock(side_effect=AssertionError('empty results must not cause another search'))
    raw = {'web': {'results': []}} if provider == 'brave' else {'results': []}
    service = SearchService(env.store, env.settings, transport=AsyncMock(return_value=raw), public_fn=public)
    try:
        result = await service.search('A query with no match')
        assert result['results'] == result['links'] == [] and result['text'] == ''
        public.assert_not_awaited()
        assert env.store.list_events()[0]['details']['status'] == 'success'
    finally:
        await service.close()


@pytest.mark.anyio
async def test_deepseek_query_limit_is_explicit_and_does_not_silently_truncate_or_dispatch(env):
    env.config['search']['provider'] = 'deepseek'
    transport = AsyncMock()
    service = SearchService(env.store, env.settings, transport=transport)
    try:
        with pytest.raises(SearchError, match='128'):
            await service.search('字' * 129)
        transport.assert_not_awaited()
        assert service.status()['used_today'] == 0
    finally:
        await service.close()


@pytest.mark.anyio
@pytest.mark.parametrize('fallback', [False, True])
async def test_provider_failure_only_uses_public_fallback_when_explicitly_enabled_and_redacts_error(env, fallback):
    env.config['search']['fallback_public'] = fallback
    public = AsyncMock(return_value={'source': 'https://www.bing.com/search', 'text': 'Actual snippets',
                                    'links': ['https://source.example/article'], 'channel': 'bing_rss'})
    transport = AsyncMock(side_effect=RuntimeError('private-token-DO-NOT-LOG https://private.invalid/body'))
    service = SearchService(env.store, env.settings, transport=transport, public_fn=public)
    try:
        if fallback:
            result = await service.search('private query DO-NOT-LOG')
            assert result['channel'] == 'bing_rss'
            assert result['failures'] == [{'channel': 'tavily', 'status': 'unavailable'}]
            public.assert_awaited_once()
        else:
            with pytest.raises(SearchError) as caught:
                await service.search('private query DO-NOT-LOG')
            assert 'DO-NOT-LOG' not in str(caught.value)
            public.assert_not_awaited()
        assert service.status()['used_today'] == 1
        assert 'DO-NOT-LOG' not in json.dumps(env.store.list_events())
    finally:
        await service.close()


@pytest.mark.anyio
async def test_attempt_limit_persists_after_failures_and_service_restart(env):
    env.config['search']['daily_calls'] = 2
    transport = AsyncMock(side_effect=[response(), RuntimeError('upstream failed')])
    service = SearchService(env.store, env.settings, transport=transport)
    try:
        await service.search('first')
        with pytest.raises(SearchError):
            await service.search('second')
        await service.close()
        service = SearchService(env.store, env.settings, transport=transport)
        assert service.status()['used_today'] == 2 and service.status()['remaining'] == 0
        with pytest.raises(SearchError) as caught:
            await service.search('third')
        assert caught.value.code == 'daily_limit' and transport.await_count == 2
    finally:
        await service.close()


@pytest.mark.anyio
async def test_daily_claim_is_atomic_across_two_store_connections(env):
    env.config['search']['daily_calls'] = 1
    other = Store(env.store.data_dir)
    transport = AsyncMock(return_value=response())
    services = [SearchService(store, env.settings, transport=transport) for store in (env.store, other)]
    try:
        results = await asyncio.gather(*(service.search('concurrent') for service in services), return_exceptions=True)
        assert sum(isinstance(item, dict) for item in results) == 1
        errors = [item for item in results if isinstance(item, SearchError)]
        assert len(errors) == 1 and errors[0].code == 'daily_limit'
        assert transport.await_count == 1
        assert all(service.status()['used_today'] == 1 for service in services)
    finally:
        for service in services:
            await service.close()
        other.close()


@pytest.mark.anyio
async def test_quota_uses_configured_midnight_without_resetting_prior_attempts(env, monkeypatch):
    clock = [datetime(2026, 10, 25, 15, 59, tzinfo=timezone.utc)]
    monkeypatch.setattr(search_module, '_now', lambda: clock[0])
    monkeypatch.setattr(db_module, 'utc_now', lambda: clock[0].isoformat().replace('+00:00', 'Z'))
    env.config['search']['daily_calls'] = 1
    service = SearchService(env.store, env.settings, transport=AsyncMock(return_value=response()))
    try:
        await service.search('before midnight')
        with pytest.raises(SearchError):
            await service.search('same day')
        clock[0] += timedelta(minutes=2)
        assert service.status()['used_today'] == 0
        await service.search('after midnight')
        assert len([item for item in env.store.list_events() if item['kind'] == 'search_attempt']) == 2
    finally:
        await service.close()


@pytest.mark.anyio
@pytest.mark.parametrize('raw', [{'error': {'message': 'secret-upstream'}}, {'unexpected': []},
    {'results': [{'title': 'Fake private result', 'url': 'http://127.0.0.1/private', 'content': 'secret-upstream'}]}])
async def test_error_envelopes_and_private_results_cannot_become_search_evidence(env, raw):
    service = SearchService(env.store, env.settings, transport=AsyncMock(return_value=raw))
    try:
        with pytest.raises(SearchError) as caught:
            await service.search('evidence')
        assert 'secret-upstream' not in str(caught.value)
        assert 'secret-upstream' not in json.dumps(env.store.list_events())
    finally:
        await service.close()


def test_search_keys_are_separate_protected_clearable_and_preserved_on_provider_switch(tmp_path):
    settings = Settings(tmp_path)
    settings.update({'search_providers': [{'id': 'tavily', 'api_key': 'TAVILY-private-4889'}]})
    settings.update({'search_providers': [{'id': 'brave', 'api_key': 'BRAVE-private-4889'}]})
    settings.update({'search': {'provider': 'brave'}, 'search_providers': [{'id': 'tavily', 'api_key': ''}]})
    stored = settings.path.read_text('utf-8')
    assert 'TAVILY-private-4889' not in stored and 'BRAVE-private-4889' not in stored
    settings = Settings(tmp_path)
    providers = {item['id']: item for item in settings.get()['search_providers']}
    assert providers['tavily']['api_key'] == 'TAVILY-private-4889'
    assert providers['brave']['api_key'] == 'BRAVE-private-4889'
    assert all('api_key' not in item and item['api_key_set'] for item in settings.public()['search_providers'])
    settings.update({'search_providers': [{'id': 'brave', 'clear_api_key': True}]})
    providers = {item['id']: item for item in Settings(tmp_path).get()['search_providers']}
    assert providers['brave']['api_key'] == '' and providers['tavily']['api_key'] == 'TAVILY-private-4889'


@pytest.mark.parametrize('patch', [
    {'search': {'enabled': 'yes'}}, {'search': {'fallback_public': 1}}, {'search': {'provider': []}},
    {'search': {'daily_calls': True}}, {'search': {'max_results': 11}}, {'search': {'timeout_seconds': 0}},
    {'search': {'unknown': 1}}, {'search_providers': [{'id': []}]}, {'search_providers': [{'id': {}}]},
    {'search_providers': [{'id': 'other'}]}, {'search_providers': [{'id': 'brave'}, {'id': 'brave'}]},
    {'search_providers': [{'id': 'brave', 'api_key': []}]},
    *[{'search_providers': [{'id': 'brave', 'base_url': value}]} for value in (
        'http://example.org/search', 'https://user:password@example.org/search', 'https://example.org/?key=secret',
        'https://example.org/#fragment', 'https://127.0.0.1/', 'https://[::1]/', 'https://localhost/',
        'https://internal.local/', 'https://example.org:8443/', 'https://example.org/\r\n', ' https://example.org/')],
], ids=['enabled', 'fallback', 'provider-type', 'limit-bool', 'results-range', 'timeout-range', 'unknown',
        'id-list', 'id-dict', 'id-unknown', 'id-duplicate', 'key-type', 'http', 'userinfo', 'query', 'fragment',
        'loopback', 'ipv6', 'localhost', 'local', 'port', 'crlf', 'whitespace'])
def test_invalid_configuration_is_rejected_without_mutation(tmp_path, patch):
    settings = Settings(tmp_path)
    before = settings.get()
    with pytest.raises(ValueError):
        settings.update(patch)
    assert settings.get() == before


@pytest.mark.anyio
async def test_real_thread_timeouts_keep_actual_workers_admitted_until_they_finish(env):
    env.config['search']['timeout_seconds'] = .08
    release = threading.Event()
    calls = []
    def transport(url, **kwargs):
        calls.append(url)
        release.wait(3)
        return response()
    service = SearchService(env.store, env.settings, transport=transport)
    try:
        results = await asyncio.gather(service.search('one'), service.search('two'), return_exceptions=True)
        assert all(isinstance(item, SearchError) and item.code == 'timeout' for item in results)
        assert len(calls) == 2 and service.status()['running'] == 2
        with pytest.raises(SearchError, match='等待期限'):
            await service.search('queued third')
        assert len(calls) == 2 and service.status()['used_today'] == 2
    finally:
        release.set()
        await service.close()
    assert service.status()['running'] == 0


@pytest.mark.anyio
async def test_cancellation_stops_public_channel_orchestration_but_keeps_started_io_owned(env, monkeypatch):
    env.config['search']['provider'] = 'public'
    release = threading.Event()
    calls = []
    def read(url, **kwargs):
        calls.append(url)
        release.wait(3)
        raise ValueError('channel unavailable')
    monkeypatch.setattr(tools, '_read_public', read)
    service = SearchService(env.store, env.settings)
    task = asyncio.create_task(service.search('public query'))
    try:
        for _ in range(100):
            if calls:
                break
            await asyncio.sleep(.005)
        assert len(calls) == 1
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert service.status()['running'] == 1
        release.set()
        await service.close()
        assert len(calls) == 1
        assert env.store.list_events()[0]['details']['status'] == 'cancelled'
        service.resume()
        assert service.status()['running'] == 0
    finally:
        release.set()
        await service.close()


@pytest.mark.anyio
@pytest.mark.parametrize('change', ['cancel', 'key', 'endpoint', 'timezone'])
async def test_locked_database_does_not_block_event_loop_or_send_stale_requests_after_wait(env, change):
    transport = AsyncMock(return_value=response())
    service = SearchService(env.store, env.settings, transport=transport)
    blocker = sqlite3.connect(env.store.path)
    blocker.execute('BEGIN IMMEDIATE')
    task = asyncio.create_task(service.search('must not dispatch'))
    try:
        await asyncio.sleep(.06)
        # The background SQLite claim retries outside the shared Store lock.
        began = time.monotonic()
        env.store.list_messages('web:default')
        assert time.monotonic() - began < .15
        assert not task.done()
        if change == 'cancel':
            task.cancel()
        elif change == 'key':
            env.config['search_providers'][0]['api_key'] = ''
        elif change == 'endpoint':
            env.config['search_providers'][0]['base_url'] = 'https://another.example/search'
        else:
            env.config['time']['timezone'] = 'America/New_York'
        blocker.rollback()
        if change == 'cancel':
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            with pytest.raises(SearchError) as caught:
                await task
            assert caught.value.code == 'configuration_changed'
        await service.close()
        transport.assert_not_awaited()
        assert service.status()['used_today'] == 0
    finally:
        blocker.rollback()
        blocker.close()
        await service.close()


@pytest.mark.anyio
async def test_queue_uses_same_timezone_identity_as_the_actual_claim(env):
    transport = AsyncMock(return_value=response())
    service = SearchService(env.store, env.settings, transport=transport)
    await service._slots.acquire()
    await service._slots.acquire()
    task = asyncio.create_task(service.search('queued query'))
    try:
        await asyncio.sleep(.01)
        env.config['time']['timezone'] = 'America/New_York'
        service._slots.release()
        with pytest.raises(SearchError) as caught:
            await task
        assert caught.value.code == 'configuration_changed'
        transport.assert_not_awaited()
        assert service.status()['used_today'] == 0
    finally:
        service._slots.release()
        await service.close()


@pytest.mark.anyio
async def test_close_rejects_new_work_until_explicit_resume(env):
    service = SearchService(env.store, env.settings, transport=AsyncMock(return_value=response()))
    await service.close()
    with pytest.raises(SearchError) as caught:
        await service.search('closed')
    assert caught.value.code == 'closing'
    service.resume()
    assert (await service.search('resumed'))['links']
    await service.close()


def test_real_transport_rejects_mixed_private_dns_without_opening_a_connection(monkeypatch):
    monkeypatch.setattr(socket, 'getaddrinfo', lambda *args, **kwargs: [
        (socket.AF_INET, socket.SOCK_STREAM, 6, '', ('8.8.8.8', 443)),
        (socket.AF_INET, socket.SOCK_STREAM, 6, '', ('127.0.0.1', 443))])
    opened = []
    monkeypatch.setattr(tools, '_PinnedHTTPS', lambda *args: opened.append(args))
    with pytest.raises(ValueError):
        _request_json(SEARCH_ENDPOINTS['tavily'], method='POST', headers={}, payload={'query': 'q'}, timeout=1)
    assert opened == []


@pytest.mark.anyio
@pytest.mark.parametrize('provider', ['tavily', 'public'])
@pytest.mark.parametrize('change', ['cancel', 'configuration'])
async def test_dns_wait_never_sends_first_request_after_cancellation_or_configuration_change(env, monkeypatch, provider, change):
    env.config['search']['provider'] = provider
    entered, release = threading.Event(), threading.Event()
    connections = []
    def resolve(url):
        entered.set()
        release.wait(3)
        parsed = search_module.urlsplit(url)
        return parsed, parsed.hostname, 443, ['8.8.8.8']
    def connect(*args):
        connections.append(args)
        raise AssertionError('stale requests must never open a connection')
    monkeypatch.setattr(tools, 'public_addresses', resolve)
    monkeypatch.setattr(tools, '_PinnedHTTPS', connect)
    service = SearchService(env.store, env.settings)
    task = asyncio.create_task(service.search('DNS wait'))
    try:
        for _ in range(100):
            if entered.is_set():
                break
            await asyncio.sleep(.005)
        assert entered.is_set()
        if change == 'cancel':
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        elif provider == 'public':
            env.config['search']['enabled'] = False
        else:
            env.config['search_providers'][0]['api_key'] = ''
        release.set()
        if change != 'cancel':
            with pytest.raises(SearchError) as caught:
                await task
            assert caught.value.code == 'configuration_changed'
        await service.close()
        assert connections == []
        assert env.store.list_events()[0]['details']['status'] in ('cancelled', 'failed')
    finally:
        release.set()
        await service.close()


@pytest.mark.anyio
async def test_default_transport_dns_wait_uses_original_absolute_deadline(env, monkeypatch):
    env.config['search']['timeout_seconds'] = .05
    entered, release = threading.Event(), threading.Event()
    connections = []
    def resolve(url):
        entered.set()
        release.wait(3)
        parsed = search_module.urlsplit(url)
        return parsed, parsed.hostname, 443, ['8.8.8.8']
    monkeypatch.setattr(tools, 'public_addresses', resolve)
    monkeypatch.setattr(tools, '_PinnedHTTPS', lambda *args: connections.append(args))
    service = SearchService(env.store, env.settings)
    try:
        with pytest.raises(SearchError) as caught:
            await service.search('DNS timeout')
        assert entered.is_set() and caught.value.code == 'timeout'
        release.set()
        await service.close()
        assert connections == []
    finally:
        release.set()
        await service.close()


@pytest.mark.parametrize('kind', ['redirect', 'oversize'])
def test_real_transport_never_follows_redirects_or_reads_over_one_megabyte(monkeypatch, kind):
    connections = []
    class Response:
        status = 302 if kind == 'redirect' else 200
        def getheader(self, key, default=''):
            return 'application/json'
        def read1(self, size):
            return b'x' * size
    class Connection:
        sock = None
        def __init__(self, *args):
            connections.append(self)
            self.closed = False
        def request(self, *args, **kwargs):
            pass
        def getresponse(self):
            return Response()
        def close(self):
            self.closed = True
    monkeypatch.setattr(tools, 'public_addresses', lambda url: (search_module.urlsplit(url), 'example.org', 443, ['8.8.8.8']))
    monkeypatch.setattr(tools, '_PinnedHTTPS', Connection)
    with pytest.raises(SearchError) as caught:
        _request_json(SEARCH_ENDPOINTS['tavily'], method='POST', headers={}, payload={'query': 'q'}, timeout=1)
    assert caught.value.code == ('http_error' if kind == 'redirect' else 'response_too_large')
    assert len(connections) == 1 and connections[0].closed
