"""Local-chat MCP discovery uses substitutes and never contacts a service."""
import asyncio
from copy import deepcopy
from types import SimpleNamespace

import pytest

from shiori.access import AccessManager
from shiori.db import Store
from shiori.mcp_client import schema_hash
from shiori.settings import DEFAULTS
from shiori.tool_actions import ToolActions


@pytest.fixture
def anyio_backend():
    return 'asyncio'


class Settings:
    token = 'local-refresh-test-token'

    def __init__(self):
        self.config = deepcopy(DEFAULTS)
        self.config['mcp_servers'] = [{'id': 'notes', 'name': 'Notes', 'enabled': True,
            'url': 'http://127.0.0.1:5678/mcp', 'token': '', 'allowed_tools': ['read_note']}]

    def get(self):
        return deepcopy(self.config)


class DiscoveryClient:
    def __init__(self):
        self.calls, self.cancelled = [], 0
        self.entered, self.release = asyncio.Event(), asyncio.Event()
        self.block, self.fail = False, False

    async def discover(self, config):
        self.calls.append(deepcopy(config))
        self.entered.set()
        try:
            if self.block:
                await self.release.wait()
            if self.fail:
                raise RuntimeError('private upstream response')
            schema = {'type': 'object', 'properties': {}, 'additionalProperties': False}
            return {'tools': [{'name': name, 'inputSchema': schema, 'schema_hash': schema_hash(schema)}
                              for name in ('read_note', 'delete_all')]}
        except asyncio.CancelledError:
            self.cancelled += 1
            raise


@pytest.fixture
def environment(tmp_path):
    settings, store, client = Settings(), Store(tmp_path), DiscoveryClient()
    store.upsert_scope({'id': 'web:default', 'allow_tools': True, 'allow_mcp': True})
    access = AccessManager(settings)
    chat = SimpleNamespace(store=store, settings=settings, provider=SimpleNamespace(_updating=False),
                           access=access, social=SimpleNamespace(_allowed=lambda _: True))
    actions = ToolActions(chat, client)
    yield actions, settings, store, client, access.identity('local', 'shiori')
    actions.close()
    store.close()


@pytest.mark.anyio
async def test_refresh_missing_expired_and_changed_catalog_preserves_allowlist(environment):
    actions, settings, store, client, identity = environment
    await actions.refresh_for_chat('web:default', 'owner', identity)
    assert len(client.calls) == 1
    assert [item['_tool']['name'] for item in actions.available('web:default', 'owner', identity)] == ['read_note']
    await actions.refresh_for_chat('web:default', 'owner', identity)
    assert len(client.calls) == 1
    actions._catalog['notes']['expires'] = 0
    assert not actions.available('web:default', 'owner', identity)
    await actions.refresh_for_chat('web:default', 'owner', identity)
    assert len(client.calls) == 2
    settings.config['mcp_servers'][0]['url'] = 'http://127.0.0.1:5679/mcp'
    assert not actions.available('web:default', 'owner', identity)
    await actions.refresh_for_chat('web:default', 'owner', identity)
    assert client.calls[-1]['url'].endswith('5679/mcp') and len(client.calls) == 3
    assert settings.config['mcp_servers'][0]['allowed_tools'] == ['read_note']
    assert not actions.list()  # Discovery neither proposes nor executes a tool.


@pytest.mark.anyio
@pytest.mark.parametrize('gate', ['qq', 'forged', 'remote_admin', 'no_access', 'paused', 'updating',
    'closed', 'suspended', 'scope_disabled', 'tools_disabled', 'mcp_disabled', 'model_tools_disabled',
    'social_denied', 'docker', 'server_disabled', 'empty_allowlist'])
async def test_refresh_never_connects_without_local_authorization(environment, gate):
    actions, settings, store, client, identity = environment
    scope_id = 'web:default'
    if gate == 'qq':
        scope_id = 'qq:group:202'
        store.upsert_scope({'id': scope_id, 'allow_tools': True, 'allow_mcp': True})
    elif gate == 'forged': identity = dict(identity, proof='invalid')
    elif gate == 'remote_admin':
        settings.config['onebot']['owner_ids'] = ['101']
        identity = actions.chat.access.identity('onebot', '101')
    elif gate == 'no_access': actions.chat.access = None
    elif gate == 'paused': settings.config['paused'] = True
    elif gate == 'updating': actions.chat.provider._updating = True
    elif gate == 'closed': actions.close()
    elif gate == 'suspended': actions.mcp_suspended = True
    elif gate == 'scope_disabled': store.upsert_scope({'id': scope_id, 'enabled': False})
    elif gate == 'tools_disabled': store.upsert_scope({'id': scope_id, 'allow_tools': False})
    elif gate == 'mcp_disabled': store.upsert_scope({'id': scope_id, 'allow_mcp': False})
    elif gate == 'model_tools_disabled':
        settings.config['providers'] = [{'id': 'no-tools', 'enabled': True, 'tools': False}]
        store.upsert_scope({'id': scope_id, 'model_id': 'no-tools'})
    elif gate == 'social_denied': actions.chat.social._allowed = lambda _: False
    elif gate == 'docker': settings.config['computer'].update(enabled=True, runtime='docker', allow_external_tools=False)
    elif gate == 'server_disabled': settings.config['mcp_servers'][0]['enabled'] = False
    elif gate == 'empty_allowlist': settings.config['mcp_servers'][0]['allowed_tools'] = []
    await actions.refresh_for_chat(scope_id, 'owner', identity)
    assert not client.calls and not actions._catalog and not actions._refresh_jobs


@pytest.mark.anyio
async def test_concurrent_waiter_cancellation_keeps_other_authorized_chat(environment):
    actions, settings, store, client, identity = environment
    client.block = True
    first = asyncio.create_task(actions.refresh_for_chat('web:default', 'owner', identity))
    await client.entered.wait()
    store.upsert_scope({'id': 'web:second', 'allow_mcp': True, 'allow_tools': True})
    second = asyncio.create_task(actions.refresh_for_chat('web:second', 'owner', identity))
    await asyncio.sleep(0)
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first
    assert len(client.calls) == 1 and client.cancelled == 0
    store.upsert_scope({'id': 'web:default', 'allow_mcp': False})
    client.release.set()
    await second
    assert actions.available('web:second', 'owner', identity)
    assert not actions._refresh_jobs


@pytest.mark.anyio
async def test_last_waiter_cancellation_drains_discovery_without_backoff(environment):
    actions, settings, store, client, identity = environment
    client.block = True
    pending = asyncio.create_task(actions.refresh_for_chat('web:default', 'owner', identity))
    await client.entered.wait()
    pending.cancel()
    with pytest.raises(asyncio.CancelledError):
        await pending
    assert client.cancelled == 1 and not actions._refresh_jobs and not actions._catalog
    assert not actions._refresh_backoff
    client.block = False
    await actions.refresh_for_chat('web:default', 'owner', identity)
    assert len(client.calls) == 2 and actions.available('web:default', 'owner', identity)


@pytest.mark.anyio
async def test_total_deadline_cancels_parallel_discoveries_and_backs_off(environment, monkeypatch):
    actions, settings, store, client, identity = environment
    monkeypatch.setattr('shiori.tool_actions.MCP_CHAT_REFRESH_TIMEOUT', .03)
    settings.config['mcp_servers'].append(dict(settings.config['mcp_servers'][0], id='second'))
    client.block = True
    await asyncio.wait_for(actions.refresh_for_chat('web:default', 'owner', identity), .5)
    assert len(client.calls) == 2 and client.cancelled == 2
    assert not actions._refresh_jobs and not actions._catalog
    await actions.refresh_for_chat('web:default', 'owner', identity)
    assert len(client.calls) == 2


@pytest.mark.anyio
async def test_failed_refresh_keeps_expired_tools_hidden_and_manual_discovery_can_recover(environment):
    actions, settings, store, client, identity = environment
    await actions.discover('notes')
    actions._catalog['notes']['expires'] = 0
    client.fail = True
    await actions.refresh_for_chat('web:default', 'owner', identity)
    await actions.refresh_for_chat('web:default', 'owner', identity)
    assert len(client.calls) == 2 and not actions.available('web:default', 'owner', identity)
    assert 'private upstream response' not in str(actions.status())
    client.fail = False
    await actions.discover('notes')
    assert len(client.calls) == 3 and not actions._refresh_backoff
    assert actions.available('web:default', 'owner', identity)


@pytest.mark.anyio
async def test_refresh_retries_after_short_backoff_expires(environment):
    actions, settings, store, client, identity = environment
    client.fail = True
    await actions.refresh_for_chat('web:default', 'owner', identity)
    assert len(client.calls) == 1 and actions._refresh_backoff
    actions._refresh_backoff = {key: 0 for key in actions._refresh_backoff}
    client.fail = False
    await actions.refresh_for_chat('web:default', 'owner', identity)
    assert len(client.calls) == 2 and actions.available('web:default', 'owner', identity)


@pytest.mark.anyio
async def test_immediate_close_cleans_discovery_even_before_it_starts(environment):
    actions, settings, store, client, identity = environment
    pending = asyncio.create_task(actions.refresh_for_chat('web:default', 'owner', identity))
    # The caller creates its shared task before yielding; close runs before
    # that discovery coroutine gets its first opportunity to execute.
    await asyncio.sleep(0)
    await actions.aclose()
    await pending
    assert not client.calls and not actions._refresh_jobs


@pytest.mark.anyio
@pytest.mark.parametrize('change', ['scope', 'url', 'updating', 'allowlist'])
async def test_refresh_cannot_publish_after_permission_or_connection_change(environment, change):
    actions, settings, store, client, identity = environment
    client.block = True
    pending = asyncio.create_task(actions.refresh_for_chat('web:default', 'owner', identity))
    await client.entered.wait()
    if change == 'scope': store.upsert_scope({'id': 'web:default', 'allow_mcp': False})
    elif change == 'url': settings.config['mcp_servers'][0]['url'] = 'http://127.0.0.1:6789/mcp'
    elif change == 'updating': actions.chat.provider._updating = True
    else: settings.config['mcp_servers'][0]['allowed_tools'] = []
    client.release.set()
    await pending
    assert not actions._catalog and not actions._refresh_jobs


@pytest.mark.anyio
async def test_close_drains_inflight_discovery_and_prevents_late_catalog(environment):
    actions, settings, store, client, identity = environment
    client.block = True
    pending = asyncio.create_task(actions.refresh_for_chat('web:default', 'owner', identity))
    await client.entered.wait()
    await actions.aclose()
    await pending
    assert client.cancelled == 1 and not actions._refresh_jobs and not actions._catalog
    await actions.refresh_for_chat('web:default', 'owner', identity)
    assert len(client.calls) == 1
