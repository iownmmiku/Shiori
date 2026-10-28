"""Approval ledger tests use an explicit MCP substitute; no external tools run."""
import asyncio
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
from unittest.mock import AsyncMock

import pytest
import httpx

from shiori.chat import ChatService
from qunyu.db import Store
from shiori.mcp_client import schema_hash
from shiori.provider import ProviderError
from shiori.settings import DEFAULTS
from shiori.tool_actions import ToolActions, bounded_result


SCHEMA = {'type': 'object', 'properties': {'text': {'type': 'string'}},
          'required': ['text'], 'additionalProperties': False}


@pytest.fixture
def anyio_backend():
    return 'asyncio'


class Settings:
    token = 'local-admin-token-do-not-leak'

    def __init__(self):
        self.config = deepcopy(DEFAULTS)
        self.config['mcp_servers'] = [{'id': 'notes', 'name': '测试便签',
            'url': 'http://127.0.0.1:5678/mcp', 'enabled': True,
            'token': 'private-mcp-token-do-not-leak', 'allowed_tools': ['append_note']}]
        self.config['onebot'].update(chat_policy="whitelist", enabled=True, adapter='napcat', owner_ids=['101'], allowed_groups=['202'])

    def get(self):
        return deepcopy(self.config)


class FakeMCPClient:
    def __init__(self):
        self.discover_count = 0
        self.call_count = 0
        self.preflight_count = 0
        self.block = False
        self.entered, self.release = asyncio.Event(), asyncio.Event()
        self.result = {'content': [{'type': 'text', 'text': '写入完成'}], 'text': '写入完成', 'isError': False}
        self.error = None

    async def discover(self, config):
        self.discover_count += 1
        return {'protocol_version': '2025-11-25', 'tools': [
            {'name': 'append_note', 'description': '追加便签', 'inputSchema': deepcopy(SCHEMA), 'schema_hash': schema_hash(SCHEMA)},
            {'name': 'delete_all', 'description': '未允许工具', 'inputSchema': deepcopy(SCHEMA), 'schema_hash': schema_hash(SCHEMA)},
        ]}

    async def call(self, config, name, arguments, expected_schema_hash=None, before_call=None):
        self.preflight_count += 1
        assert name == 'append_note' and expected_schema_hash == schema_hash(SCHEMA)
        self.entered.set()
        if self.block:
            await self.release.wait()
        before_call()
        self.call_count += 1
        if self.error:
            raise self.error
        return deepcopy(self.result)


class QuietProvider:
    async def complete(self, messages, **kwargs):
        message = {'role': 'assistant', 'content': '收到'}
        return {'content': '收到', 'tool_calls': [], 'message': message, 'usage': {}}


@pytest.fixture
def environment(tmp_path):
    store, settings = Store(tmp_path), Settings()
    store.upsert_scope({'id': 'qq:group:202', 'allow_tools': True, 'allow_mcp': True, 'auto_memory': False})
    chat, client = ChatService(store, settings, QuietProvider()), FakeMCPClient()
    chat.tool_actions = ToolActions(chat, client)
    yield store, settings, chat, client
    chat.tool_actions.close()
    chat.memory.close()
    store.close()


async def propose(environment, text='把讨论要点写入便签'):
    store, settings, chat, client = environment
    actions = chat.tool_actions
    if not actions.available('qq:group:202', '101'):
        await actions.discover('notes')
    source = await chat.observe('qq:group:202', '101', text, metadata={'is_owner': True})
    plan = await chat.social.plan(store.get_scope('qq:group:202'), source, direct=True)
    metadata = {'source_ids': [source['id']], '_context_source_ids': [source['id']],
                'request_generation': plan['generation']}
    alias = actions.available('qq:group:202', '101')[0]['function']['name']
    result = actions.propose(alias, {'text': '讨论要点'}, 'qq:group:202', '101', metadata)
    return result['run_id'], source, metadata


@pytest.mark.anyio
async def test_discovery_cache_allowlist_and_proposal_do_not_execute(environment):
    store, settings, chat, client = environment
    actions = chat.tool_actions
    assert not actions.available('qq:group:202', '101')
    await actions.discover('notes')
    first = actions.available('qq:group:202', '101')
    assert len(first) == 1 and first[0]['_tool']['name'] == 'append_note'
    assert first == actions.available('qq:group:202', '101') and client.discover_count == 1
    run_id, source, metadata = await propose(environment)
    assert actions.list()[0]['status'] == 'pending' and client.call_count == 0
    settings.config['mcp_servers'][0]['allowed_tools'] = []
    assert not actions.available('qq:group:202', '101')
    with pytest.raises(ValueError):
        actions.propose(first[0]['function']['name'], {'text': '不应执行'}, 'qq:group:202', '101', metadata)
    assert client.call_count == 0


@pytest.mark.anyio
async def test_expired_or_changed_catalogue_needs_explicit_discovery(environment):
    store, settings, chat, client = environment
    await chat.tool_actions.discover('notes')
    chat.tool_actions._catalog['notes']['expires'] = 0
    assert not chat.tool_actions.available('qq:group:202', '101')
    assert client.discover_count == 1
    await chat.tool_actions.discover('notes')
    settings.config['mcp_servers'][0]['url'] = 'http://127.0.0.1:5679/mcp'
    assert not chat.tool_actions.available('qq:group:202', '101')
    assert client.discover_count == 2
    settings.config['mcp_servers'][0]['enabled'] = False
    # The setup UI explicitly allows discovering a disabled service. This must
    # not make the real disabled configuration usable by chat or approval.
    await chat.tool_actions.discover('notes')
    assert client.discover_count == 3
    assert not chat.tool_actions.available('qq:group:202', '101')


@pytest.mark.anyio
@pytest.mark.parametrize('arguments', [{}, {'text': 42}, {'text': 'ok', 'extra': True}])
async def test_proposal_rejects_schema_errors_without_creating_run(environment, arguments):
    store, settings, chat, client = environment
    await chat.tool_actions.discover('notes')
    source = store.add_message('qq:group:202', '101', 'user', '记入便签')
    alias = chat.tool_actions.available(source['scope_id'], '101')[0]['function']['name']
    with pytest.raises(ValueError):
        chat.tool_actions.propose(alias, arguments, source['scope_id'], '101', {'source_ids': [source['id']]})
    assert not chat.tool_actions.list() and not client.call_count


@pytest.mark.anyio
async def test_approval_and_rejection_are_per_proposal_and_never_replay(environment):
    store, settings, chat, client = environment
    first, _, _ = await propose(environment)
    assert (await chat.tool_actions.approve(first))['status'] == 'succeeded'
    with pytest.raises(ValueError):
        await chat.tool_actions.approve(first)
    second, _, _ = await propose(environment)
    assert chat.tool_actions.reject(second)['status'] == 'rejected'
    with pytest.raises(ValueError):
        await chat.tool_actions.approve(second)
    assert client.call_count == 1


@pytest.mark.anyio
@pytest.mark.parametrize('change', ['pause', 'scope_off', 'tools_off', 'mcp_off', 'model_tools_off',
    'revoke_group', 'connector_off', 'adapter_switch', 'allowlist', 'server_off',
    'server_deleted', 'server_url', 'server_token', 'source_cleared', 'generation', 'expired'])
async def test_current_authority_and_request_are_rechecked_before_approval(environment, change):
    store, settings, chat, client = environment
    run_id, source, metadata = await propose(environment)
    if change == 'pause': settings.config['paused'] = True
    elif change == 'scope_off': store.upsert_scope({'id': source['scope_id'], 'enabled': False})
    elif change == 'tools_off': store.upsert_scope({'id': source['scope_id'], 'allow_tools': False})
    elif change == 'mcp_off': store.upsert_scope({'id': source['scope_id'], 'allow_mcp': False})
    elif change == 'model_tools_off':
        settings.config['providers'] = [{'id': 'chatmodel', 'tools': False, 'enabled': True, 'model': 'm', 'base_url': 'https://model.example/v1'}]
        settings.config['routing']['chat'] = 'chatmodel'
    elif change == 'revoke_group': settings.config['onebot']['allowed_groups'] = []
    elif change == 'connector_off': settings.config['onebot']['enabled'] = False
    elif change == 'adapter_switch': settings.config['onebot']['adapter'] = 'official'
    elif change == 'allowlist': settings.config['mcp_servers'][0]['allowed_tools'] = []
    elif change == 'server_off': settings.config['mcp_servers'][0]['enabled'] = False
    elif change == 'server_deleted': settings.config['mcp_servers'] = []
    elif change == 'server_url': settings.config['mcp_servers'][0]['url'] = 'http://127.0.0.1:5679/mcp'
    elif change == 'server_token': settings.config['mcp_servers'][0]['token'] = 'replacement-token'
    elif change == 'source_cleared': store.clear_messages(source['scope_id'])
    elif change == 'generation':
        newer = await chat.observe(source['scope_id'], '101', '群屿，改问晚餐菜单', metadata={'is_owner': True})
        await chat.social.plan(store.get_scope(source['scope_id']), newer, direct=True)
    elif change == 'expired':
        chat.tool_actions._update(run_id, expires_at=(datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat())
    result = await chat.tool_actions.approve(run_id)
    assert result['status'] == 'cancelled', change
    assert client.call_count == 0, change


@pytest.mark.anyio
async def test_permission_change_during_preflight_blocks_actual_tools_call(environment):
    store, settings, chat, client = environment
    run_id, _, _ = await propose(environment)
    client.block = True
    pending = asyncio.create_task(chat.tool_actions.approve(run_id))
    await asyncio.wait_for(client.entered.wait(), 2)
    settings.config['mcp_servers'][0]['allowed_tools'] = []
    client.release.set()
    assert (await pending)['status'] == 'cancelled'
    assert client.preflight_count == 1 and client.call_count == 0


@pytest.mark.anyio
@pytest.mark.parametrize('kind', ['memory', 'jargon'])
async def test_changed_referenced_knowledge_cancels_approval(environment, kind):
    store, settings, chat, client = environment
    await chat.tool_actions.discover('notes')
    source = store.add_message('qq:group:202', '101', 'user', '记入便签')
    metadata = {'source_ids': [source['id']]}
    if kind == 'memory':
        reference = store.create_memory({'scope_id': source['scope_id'], 'user_id': '101', 'content': '旧讨论要点', 'visibility': 'group'})
        metadata['_memory_versions'] = {reference['id']: reference['version']}
    else:
        reference = store.create_jargon({'scope_id': source['scope_id'], 'term': '要点', 'meaning': '讨论总结', 'status': 'approved'})
        metadata['_jargon_versions'] = {reference['id']: reference['version']}
    alias = chat.tool_actions.available(source['scope_id'], '101')[0]['function']['name']
    result = chat.tool_actions.propose(alias, {'text': '旧讨论要点'}, source['scope_id'], '101', metadata)
    if kind == 'memory': store.delete_memory(reference['id'])
    else: store.delete_jargon(reference['id'])
    assert (await chat.tool_actions.approve(result['run_id']))['status'] == 'cancelled'
    assert not client.call_count


@pytest.mark.anyio
async def test_approval_cannot_race_or_retry_unknown_result(environment):
    store, settings, chat, client = environment
    run_id, _, _ = await propose(environment)
    client.block = True
    client.error = TimeoutError('external outcome unknown')
    pending = asyncio.create_task(chat.tool_actions.approve(run_id))
    await asyncio.wait_for(client.entered.wait(), 2)
    with pytest.raises(ValueError):
        await chat.tool_actions.approve(run_id)
    client.release.set()
    assert (await pending)['status'] == 'unknown'
    with pytest.raises(ValueError):
        await chat.tool_actions.approve(run_id)
    assert client.call_count == 1


@pytest.mark.anyio
async def test_restart_cancels_pending_marks_running_unknown_without_execution(environment):
    store, settings, chat, client = environment
    pending, _, _ = await propose(environment)
    running, _, _ = await propose(environment)
    chat.tool_actions._update(running, status='running')
    reloaded = ToolActions(chat, client)
    statuses = {x['id']: x['status'] for x in reloaded.list()}
    assert statuses[pending] == 'cancelled' and statuses[running] == 'unknown'
    for run_id in (pending, running):
        with pytest.raises(ValueError):
            await reloaded.approve(run_id)
    assert not client.call_count


@pytest.mark.anyio
async def test_known_secrets_are_redacted_and_never_sent_as_arguments(environment):
    store, settings, chat, client = environment
    run_id, source, metadata = await propose(environment)
    alias = chat.tool_actions.available(source['scope_id'], '101')[0]['function']['name']
    secret = settings.config['mcp_servers'][0]['token']
    with pytest.raises(ValueError, match='密钥'):
        chat.tool_actions.propose(alias, {'text': secret}, source['scope_id'], '101', metadata)
    client.result = {'isError': False, secret: 'secret used as a property key', 'text': secret + settings.token, 'content': [{'type': 'text', 'text': secret}]}
    await chat.tool_actions.approve(run_id)
    saved = chat.tool_actions.path.read_text('utf-8')
    assert secret not in saved and settings.token not in saved
    assert secret not in json.dumps(chat.tool_actions.list())


@pytest.mark.parametrize('value', ['\\' * 50000, {'text': '\\"' * 30000}, {'nested': ['内容' * 30000]}],
                         ids=['backslashes', 'escaped_quotes', 'unicode_nested'])
def test_bounded_results_remain_valid_json_and_within_the_announced_limit(value):
    result = bounded_result(value, 5000)
    raw = json.dumps(result, ensure_ascii=False, sort_keys=True, allow_nan=False)
    assert json.loads(raw)['truncated']
    assert len(raw) <= 5000


@pytest.mark.anyio
@pytest.mark.parametrize('token', ['0', 'a'])
async def test_short_secret_redaction_preserves_ledger_structure_and_identifiers(environment, token):
    store, settings, chat, client = environment
    settings.config['mcp_servers'][0]['token'] = token
    run_id, source, metadata = await propose(environment)
    client.result = {'isError': False, 'text': token, 'content': [{'type': 'text', 'text': token}],
                     'status': 'valid', 'counter': 0}
    approved = await chat.tool_actions.approve(run_id)
    assert approved['status'] == 'succeeded' and approved['id'] == run_id
    raw = json.loads(chat.tool_actions.path.read_text('utf-8'))
    assert raw[0]['status'] == 'succeeded' and raw[0]['id'] == run_id
    assert raw[0]['tool_name'] == 'append_note'
    assert raw[0]['source_ids'] == [source['id']]
    assert raw[0]['result']['text'] != token
    reloaded = ToolActions(chat, client)
    assert reloaded.list()[0]['status'] == 'succeeded'
    assert reloaded.list()[0]['id'] == run_id
    with pytest.raises(ValueError):
        await reloaded.approve(run_id)
    assert client.call_count == 1


@pytest.mark.anyio
async def test_builtin_mutation_is_recorded_when_final_model_budget_fails(environment, monkeypatch):
    store, settings, chat, client = environment

    class BudgetProvider:
        calls = 0
        async def complete(self, messages, **kwargs):
            self.calls += 1
            if self.calls > 1:
                raise ProviderError('今日模型预算已用完')
            calls = [{'id': 'create-1', 'type': 'function', 'function': {'name': 'create_task',
                      'arguments': json.dumps({'title': '整理书桌'}, ensure_ascii=False)}}]
            message = {'role': 'assistant', 'content': '', 'tool_calls': calls}
            return {'content': '', 'tool_calls': calls, 'message': message, 'usage': {}}

    chat.provider = provider = BudgetProvider()
    monkeypatch.setattr(chat, '_direct', AsyncMock(return_value=None))
    result = await chat.respond('web:default', 'owner', '请创建待办整理书桌，并说明步骤')
    assert len(store.list_tasks()) == 1
    records = [x for x in chat.tool_actions.list() if x['server_id'] == '']
    assert len(records) == 1 and records[0]['status'] == 'succeeded'
    assert result['actions'][0]['ok'] and '不会自动重跑' in result['reply']
    assert provider.calls == 2


@pytest.mark.anyio
async def test_new_web_request_invalidates_older_awaiting_approval(environment):
    store, settings, chat, client = environment
    store.upsert_scope({'id': 'web:default', 'allow_mcp': True, 'auto_memory': False})
    await chat.tool_actions.discover('notes')
    source = store.add_message('web:default', 'owner', 'user', '把这条记入外部便签')
    alias = chat.tool_actions.available('web:default', 'owner')[0]['function']['name']
    result = chat.tool_actions.propose(alias, {'text': '旧请求'}, 'web:default', 'owner', {'source_ids': [source['id']]})
    await chat.respond('web:default', 'owner', '改问晚上吃什么')
    approved = await chat.tool_actions.approve(result['run_id'])
    assert approved['status'] == 'cancelled'
    assert not client.call_count


@pytest.mark.anyio
async def test_real_web_api_request_generation_invalidates_prior_approval(tmp_path):
    from shiori.api import create_app
    app = create_app(tmp_path, start_workers=False)
    store, chat, settings = app.state.store, app.state.chat, app.state.settings
    settings._data['mcp_servers'] = deepcopy(Settings().config['mcp_servers'])
    store.upsert_scope({'id': 'web:default', 'allow_mcp': True, 'auto_memory': False})
    client = FakeMCPClient()
    chat.tool_actions.client = client
    await chat.tool_actions.discover('notes')
    alias = chat.tool_actions.available('web:default', 'owner')[0]['function']['name']

    class ProposalProvider:
        calls = 0
        async def complete(self, messages, **kwargs):
            self.calls += 1
            if self.calls == 1:
                calls = [{'id': 'external-1', 'type': 'function', 'function': {'name': alias,
                          'arguments': json.dumps({'text': '讨论要点'}, ensure_ascii=False)}}]
                message = {'role': 'assistant', 'content': '', 'tool_calls': calls}
                return {'content': '', 'tool_calls': calls, 'message': message, 'usage': {}}
            message = {'role': 'assistant', 'content': '收到'}
            return {'content': '收到', 'tool_calls': [], 'message': message, 'usage': {}}

    chat.provider = ProposalProvider()
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://127.0.0.1',
                                     headers={'Authorization': 'Bearer ' + settings.token}) as browser:
            first = await browser.post('/api/chat', json={'text': '把讨论要点写入外部便签'})
            assert first.status_code == 200, first.text
            run_id = first.json()['pending_actions'][0]['run_id']
            assert client.call_count == 0
            second = await browser.post('/api/chat', json={'text': '改问晚上吃什么'})
            assert second.status_code == 200, second.text
            approved = await browser.post(f'/api/tool-runs/{run_id}/approve')
            assert approved.status_code == 200, approved.text
            assert approved.json()['status'] == 'cancelled'
            assert client.call_count == 0
    finally:
        await chat.close()
        store.close()
