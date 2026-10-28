"""Trusted identities, migrations and connector WebUI contracts; no real QQ."""
import copy
import json
from unittest.mock import AsyncMock

import httpx
import pytest

from shiori.access import AccessManager
from qunyu.db import Store
from shiori.onebot import OneBotBridge
from shiori.official import OfficialBridge
from shiori.qq_pairing import LocalQQPairing, loopback_url, revision
from shiori.settings import Settings


@pytest.fixture
def anyio_backend():
    return 'asyncio'


@pytest.fixture
def settings(tmp_path, monkeypatch):
    # Identity and connector contracts do not depend on native DPAPI. Native
    # settings/portable suites separately exercise actual encryption.
    import base64
    monkeypatch.setattr('qunyu.settings._protect', lambda value:'local:' + base64.b64encode(value.encode()).decode() if value else '')
    return Settings(tmp_path / 'data')


def test_admin_requires_signed_ingress_and_exact_platform_instance(settings):
    access = AccessManager(settings)
    settings.update({'access':{'administrators':[
        {'platform':'onebot','account_id':'123','instance_id':'456'},
        {'platform':'qqofficial','account_id':'openid','instance_id':'app1'}]}})
    assert access.is_admin(access.identity('local','shiori'))
    assert access.is_admin(access.identity('onebot','123','456'))
    assert access.is_admin(access.identity('qqofficial','openid','app1'))
    assert not access.is_admin(access.identity('onebot','123','789'))
    assert not access.is_admin(access.identity('qqofficial','openid','app2'))
    assert not access.is_admin({'platform':'local','account_id':'shiori','instance_id':'','is_admin':True})
    assert not access.is_admin(dict(access.identity('onebot','999','456'), account_id='123'))
    assert not access.is_admin(access.identity('onebot','999','456'))
    persisted = access.identity('onebot','123','456')
    assert AccessManager(Settings(settings.data_dir)).is_admin(persisted)
    item = next(x for x in access.list_administrators() if x['account_id'] == '123')
    assert access.remove(item['id'])
    assert not access.is_admin(persisted)
    with pytest.raises(ValueError):
        access.remove(access.list_administrators()[0]['id'])


def test_old_owner_migration_one_time_official_users_never_admin(tmp_path):
    directory = tmp_path / 'legacy'
    directory.mkdir()
    (directory / 'settings.json').write_text(json.dumps({'onebot':{'owner_ids':['123']},
        'qq_official':{'allowed_users':['openid']}}), encoding='utf-8')
    settings = Settings(directory)
    cfg = settings.get()
    assert cfg['onebot']['allowed_users'] == ['123']
    assert cfg['onebot']['owner_ids'] == []
    access = AccessManager(settings)
    assert access.is_admin(access.identity('onebot','123','456'))
    assert not access.is_admin(access.identity('qqofficial','openid','app1'))
    access.remove(access.list_administrators()[1]['id'])
    new_access = AccessManager(Settings(directory))
    assert not new_access.is_admin(new_access.identity('onebot','123','456'))
    assert Settings(directory).get()['onebot']['allowed_users'] == ['123']


def test_group_only_config_and_separate_lists(settings):
    settings.update({'onebot':{'enabled':True,'access_token':'test-token-1234','allowed_groups':['456'],'allowed_users':['123']}})
    assert settings.get()['onebot']['owner_ids'] == []
    access = AccessManager(settings)
    assert not access.is_admin(access.identity('onebot','123','456'))
    settings.update({'access':{'administrators':[{'platform':'onebot','account_id':'987','instance_id':'456'}]}})
    assert settings.get()['onebot']['allowed_users'] == ['123']


@pytest.mark.parametrize('patch', [
    {'computer':{'max_steps':True}}, {'computer':{'runtime':'sandbox'}},
    {'computer':{'timeout_seconds':0}}, {'computer':{'workspace':'bad\x00path'}},
    {'access':{'administrators':[{'platform':'local','account_id':'shiori'}]}},
    {'access':{'administrators':[{'platform':'onebot','account_id':'nickname'}]}},
])
def test_invalid_privilege_and_computer_config(settings, patch):
    with pytest.raises(ValueError):
        settings.update(patch)


def test_pairing_needs_private_ingress_and_local_confirmation(settings):
    now = [1000]
    access = AccessManager(settings, clock=lambda:now[0])
    item = access.create_pairing()
    identity = access.identity('onebot','123','456')
    assert not access.capture_pairing(item['code'], identity, private=False)
    assert not access.capture_pairing(item['code'], {'platform':'onebot','account_id':'123','instance_id':'456'})
    assert not access.capture_pairing(item['code'], access.identity('onebot','123'))
    assert access.capture_pairing(item['code'], identity)
    assert not access.is_admin(identity)
    assert access.status(item['id'])['status'] == 'awaiting_confirmation'
    assert not access.capture_pairing(item['code'], access.identity('onebot','987','456'))
    access.confirm_pairing(item['id'], '我的QQ')
    assert access.is_admin(identity)
    assert settings.get()['onebot']['allowed_users'] == ['123']
    with pytest.raises(KeyError):
        access.status(item['id'])
    expired = access.create_pairing(30)
    now[0] += 31
    assert not access.capture_pairing(expired['code'], identity)
    with pytest.raises(ValueError):
        access.confirm_pairing(expired['id'])


@pytest.mark.anyio
async def test_onebot_binding_before_chat_whitelist_no_memory_or_model(settings):
    store = Store(settings.data_dir)
    try:
        settings.update({'onebot':{'enabled':True,'access_token':'test-token-1234','allowed_groups':['456']}})
        access = AccessManager(settings)
        chat = type('Chat', (), {})()
        chat.access, chat.respond = access, AsyncMock()
        bridge = OneBotBridge(store, settings, chat)
        item = access.create_pairing()
        payload = {'post_type':'message','message_type':'private','self_id':456,'user_id':123,
                   'message_id':11,'message':[{'type':'text','data':{'text':'/绑定 ' + item['code']}}]}
        await bridge.handle_event(payload)
        assert access.status(item['id'])['identity']['account_id'] == '123'
        assert not chat.respond.called
        assert not store.list_messages('qq:private:123')
        access.confirm_pairing(item['id'])
        chat.handle_platform_command = AsyncMock(return_value={'reply':''})
        payload.update(message_id=12, message=[{'type':'text','data':{'text':'/身份'}}])
        await bridge.handle_event(payload)
        metadata = chat.handle_platform_command.await_args.args[3]
        assert access.is_admin(metadata['trusted_identity'])
        assert metadata['is_admin']
        assert not chat.respond.called
    finally:
        store.close()


@pytest.mark.anyio
async def test_official_allowed_chat_user_not_privileged_and_pairing(settings):
    store = Store(settings.data_dir)
    try:
        settings.update({'onebot':{'adapter':'official'}, 'qq_official':{'enabled':True,'app_id':'app','app_secret':'x','allowed_users':['user']}})
        access = AccessManager(settings)
        chat = type('Chat', (), {})()
        chat.access, chat.respond = access, AsyncMock()
        chat.handle_platform_command = AsyncMock(return_value={'reply':''})
        bridge = OfficialBridge(store, settings, chat)
        bridge._running = True
        await bridge.handle_event({'t':'C2C_MESSAGE_CREATE','d':{'id':'msg1','author':{'user_openid':'user'},'content':'/身份'}})
        metadata = chat.handle_platform_command.await_args.args[3]
        assert not metadata['is_owner'] and not access.is_admin(metadata['trusted_identity'])
        pairing = access.create_pairing()
        await bridge.handle_event({'t':'C2C_MESSAGE_CREATE','d':{'id':'msg2','author':{'user_openid':'newuser'},'content':'绑定 ' + pairing['code']}})
        assert access.status(pairing['id'])['identity']['instance_id'] == 'app'
        assert not store.list_messages('qqofficial:private:newuser')
        assert chat.handle_platform_command.await_count == 1
    finally:
        store.close()


@pytest.mark.parametrize('url', ['http://evil.example:6099','http://192.168.1.1:6099','http://127.0.0.1:6099/path',
    'http://user:secret@127.0.0.1:6099','http://127.0.0.1:6099?token=secret','http://localhost','file:///tmp/x'])
def test_connector_url_rejects_remote_credentials_or_path(url):
    with pytest.raises(ValueError):
        loopback_url(url)


class WebUI:
    def __init__(self, adapter='napcat'):
        self.adapter, self.reads, self.writes, self.requests = adapter, 0, 0, []
        self.accounts = [{'uin':'12345','nickname':'fixture'}]
        self.applied = True
        self.race_at = None
        self.schema = {'network':{'websocketClients':[{'name':'other','url':'ws://other:123','token':'other-secret','enable':True}]},'musicSignUrl':'untouched'} if adapter == 'napcat' else {
            'networks':{'wsClients':[{'name':'other','url':'ws://other:123','accessToken':'other-secret','enabled':True}], 'httpServers':[],'httpClients':[],'wsServers':[]},
            'statusCommand':{'enabled':True,'swallow':False,'cooldownSeconds':5,'trigger':'#sl'},'historySync':{'enabled':False}}

    def __call__(self, request):
        self.requests.append(request)
        path = request.url.path
        body = json.loads(request.content) if request.content else None
        if path == '/':
            return httpx.Response(200, text='Fixture WebUI')
        if path == '/api/auth/login':
            assert body['hash'] == __import__('hashlib').sha256(b'fixture-password.napcat').hexdigest()
            return httpx.Response(200, json={'code':0,'data':{'Credential':'fixture-credential'}})
        if path == '/api/login':
            assert body['password'] == 'fixture-password'
            return httpx.Response(200, json={'success':True,'token':'fixture-credential'})
        assert request.headers['Authorization'] == 'Bearer fixture-credential'
        if path == '/api/qq-list':
            return httpx.Response(200, json={'list':self.accounts})
        if path.endswith('GetConfig') or request.method == 'GET':
            self.reads += 1
            if self.reads == self.race_at:
                self.schema['other_window_changed'] = True
            value = copy.deepcopy(self.schema)
            return httpx.Response(200, json={'code':0,'data':value} if self.adapter == 'napcat' else {'config':value})
        self.writes += 1
        self.schema = json.loads(body['config']) if self.adapter == 'napcat' else body
        return httpx.Response(200, json={'code':0,'data':None} if self.adapter == 'napcat' else {'success':True,'saved':True,'applied':self.applied,'online':True})


@pytest.mark.anyio
@pytest.mark.parametrize('adapter', ['napcat','snowluma'])
async def test_connector_merge_idempotence_and_undo_preserves_other_nodes(settings, adapter):
    fixture = WebUI(adapter)
    original = copy.deepcopy(fixture.schema)
    connector = LocalQQPairing(settings, 'ws://127.0.0.1:18765/onebot/v11/ws', transport=httpx.MockTransport(fixture))
    url = 'http://localhost:6099'
    result = await connector.connect(adapter, url, 'fixture-password')
    assert result['saved'] and result['changed']
    assert result['applied'] is (None if adapter == 'napcat' else True)
    clients = connector._clients(fixture.schema, adapter)
    assert clients[0] == connector._clients(original, adapter)[0]
    own = clients[1]
    assert own['url'] == 'ws://127.0.0.1:18765/onebot/v11/ws'
    assert len(own['token' if adapter == 'napcat' else 'accessToken']) >= 12
    assert settings.get()['onebot']['enabled']
    repeated = await connector.connect(adapter, url, 'fixture-password')
    assert not repeated['changed'] and fixture.writes == 1 and len(clients) == 2
    serialized = json.dumps(result)
    assert 'fixture-password' not in serialized and 'fixture-credential' not in serialized and 'other-secret' not in serialized
    assert not (settings.data_dir / 'qq_pairing.json').exists()
    await connector.disconnect(repeated['undo_id'], 'fixture-password')
    assert fixture.schema == original
    assert fixture.writes == 2


@pytest.mark.anyio
@pytest.mark.parametrize('adapter', ['napcat','snowluma'])
async def test_renamed_connector_reuses_old_managed_node(settings, adapter):
    fixture = WebUI(adapter)
    connector = LocalQQPairing(settings, 'ws://127.0.0.1:18765/onebot/v11/ws', transport=httpx.MockTransport(fixture))
    url = 'http://localhost:6099'
    await connector.connect(adapter, url, 'fixture-password')
    clients = connector._clients(fixture.schema, adapter)
    clients[-1]['name'] = connector._legacy_connection_name
    old_token = clients[-1]['token' if adapter == 'napcat' else 'accessToken']
    result = await connector.connect(adapter, url, 'fixture-password')
    clients = connector._clients(fixture.schema, adapter)
    assert len(clients) == 2
    assert clients[-1]['name'] == connector.connection_name
    assert clients[-1]['token' if adapter == 'napcat' else 'accessToken'] == old_token
    assert result['changed']
    assert not (await connector.connect(adapter, url, 'fixture-password'))['changed']


@pytest.mark.anyio
async def test_connector_concurrent_change_prevents_write(settings):
    fixture = WebUI()
    fixture.race_at = 2
    connector = LocalQQPairing(settings, 'ws://127.0.0.1:18765/onebot/v11/ws', transport=httpx.MockTransport(fixture))
    with pytest.raises(ValueError, match='其他窗口'):
        await connector.connect('napcat','http://127.0.0.1:6099','fixture-password')
    assert fixture.writes == 0 and not settings.get()['onebot']['enabled']


@pytest.mark.anyio
async def test_snow_multiple_accounts_require_choice_and_report_partial_apply(settings):
    fixture = WebUI('snowluma')
    fixture.accounts.append({'uin':'56789','nickname':'fixture2'})
    fixture.applied = False
    connector = LocalQQPairing(settings,'ws://127.0.0.1:18765/onebot/v11/ws', transport=httpx.MockTransport(fixture))
    result = await connector.connect('snowluma','http://127.0.0.1:5099','fixture-password')
    assert result['requires_account'] and fixture.writes == 0
    result = await connector.connect('snowluma','http://127.0.0.1:5099','fixture-password','56789')
    assert result['saved'] and not result['applied'] and result['account_id'] == '56789'


@pytest.mark.anyio
async def test_connector_unknown_schema_and_expected_revision(settings):
    fixture = WebUI()
    connector = LocalQQPairing(settings,'ws://127.0.0.1:18765/onebot/v11/ws', transport=httpx.MockTransport(fixture))
    with pytest.raises(ValueError, match='已变更'):
        await connector.connect('napcat','http://127.0.0.1:6099','fixture-password',expected_revision='old')
    fixture.schema = {'unknownVersion':True}
    with pytest.raises(ValueError, match='版本暂不支持'):
        await connector.connect('napcat','http://127.0.0.1:6099','fixture-password')
    assert fixture.writes == 0


@pytest.mark.anyio
async def test_connector_undo_refuses_manually_changed_owned_node(settings):
    fixture = WebUI()
    connector = LocalQQPairing(settings,'ws://127.0.0.1:18765/onebot/v11/ws', transport=httpx.MockTransport(fixture))
    result = await connector.connect('napcat','http://127.0.0.1:6099','fixture-password')
    fixture.schema['network']['websocketClients'][1]['url'] = 'ws://127.0.0.1:19999/ws/other'
    with pytest.raises(ValueError, match='已被修改'):
        await connector.disconnect(result['undo_id'],'fixture-password')
    assert fixture.writes == 1


@pytest.mark.anyio
async def test_discovery_is_bounded_readonly_and_never_claims_identified(settings):
    fixture = WebUI()
    connector = LocalQQPairing(settings,'ws://127.0.0.1:18765/onebot/v11/ws', transport=httpx.MockTransport(fixture))
    result = await connector.discover()
    assert {r.url.port for r in fixture.requests} == {6099,5099}
    assert len(fixture.requests) == 2 and all(r.method == 'GET' for r in fixture.requests)
    assert all(x['reachable'] and not x['verified'] for x in result['candidates'])
