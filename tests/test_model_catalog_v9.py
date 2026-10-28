"""Model sources use isolated encrypted settings and offline discovery fixtures."""
from types import SimpleNamespace
import io
import json
import zipfile

import httpx
import pytest
from fastapi.testclient import TestClient

from auth_helpers import password_login
from shiori.api import create_app
from shiori.catalog_api import install_catalog
from shiori.model_catalog import ModelCatalog
from qunyu.db import Store
from shiori.provider import Provider
from shiori.settings import Settings, resolve_model


SECRET = 'isolated-catalog-key-39'


@pytest.fixture
def anyio_backend():
    return 'asyncio'


@pytest.fixture
def catalog(tmp_path):
    settings = Settings(tmp_path)
    calls = []
    async def list_models(config):
        calls.append(dict(config))
        return ['alpha', 'beta', 'alpha', '  gamma  ', '', 's' * 161]
    provider = SimpleNamespace(list_models=list_models)
    return ModelCatalog(settings, provider), calls


def source(catalog, **extra):
    return catalog.save_source(dict(name='离线测试源', base_url='https://api.example.test/v1', api_key=SECRET, **extra))


def add(catalog, source_id, names=('alpha', 'beta')):
    return catalog.add_models({'provider_id': source_id, 'models': list(names)})


def test_distillation_route_inherits_chat_and_can_be_configured_independently(catalog):
    manager, _ = catalog
    saved_source = source(manager)
    models = add(manager, saved_source['id'])['added']
    chat, analysis = models[0]['id'], models[1]['id']
    manager.update_routing({'chat':chat})
    assert resolve_model(manager.settings.get(), 'distillation')['id'] == chat
    manager.update_routing({'distillation':analysis})
    assert resolve_model(manager.settings.get(), 'distillation')['id'] == analysis
    assert resolve_model(manager.settings.get(), 'chat')['id'] == chat
    assert manager.snapshot()['routes']['distillation']['model_id'] == analysis
    with pytest.raises(ValueError, match='用途'): manager.update_routing({'unknown':'x'})
    with pytest.raises(ValueError): manager.delete_model(analysis)
    manager.update_routing({'distillation':''})
    assert resolve_model(manager.settings.get(), 'distillation')['id'] == chat


def test_viewing_legacy_models_preserves_secrets_ids_and_routes(catalog):
    manager, _ = catalog
    manager.settings.update({'model': {'base_url': 'https://legacy.example/v1', 'model': 'old-default', 'api_key': SECRET},
                             'providers': [{'id': 'old-route', 'model': 'old-special', 'api_key': 'old-private-key',
                                            'base_url': 'https://old.example/v1'}], 'routing': {'planner': 'old-route'}})
    before = manager.settings.get()
    disk = manager.settings.path.read_bytes()
    value = manager.snapshot()
    assert manager.settings.get() == before and manager.settings.path.read_bytes() == disk
    assert {item['id'] for item in value['models']} == {'default', 'old-route'}
    assert value['routing']['planner'] == 'old-route'
    assert value['routes']['planner']['model'] == 'old-special'
    assert all(item['legacy'] and not item['editable'] for item in value['providers'])
    assert SECRET not in str(value) and 'old-private-key' not in str(value)
    with pytest.raises(ValueError, match='只读'):
        manager.save_source({'name': 'changed'}, 'legacy_default')
    with pytest.raises(ValueError, match='只读'):
        manager.delete_source('legacy_old-route')


def test_one_source_keeps_one_encrypted_key_for_multiple_models(catalog):
    manager, _ = catalog
    supplier = source(manager)
    result = add(manager, supplier['id'])
    assert len(result['added']) == 2
    assert SECRET not in manager.settings.path.read_text('utf-8')
    config = manager.settings.get()
    assert len(config['provider_sources']) == 1
    assert config['provider_sources'][0]['api_key'] == SECRET
    assert all(not item['api_key'] and item['source_id'] == supplier['id'] for item in config['providers'])
    public = manager.settings.public()
    assert public['provider_sources'][0]['api_key_set']
    assert SECRET not in str(public) and SECRET not in str(result)
    restored = Settings(manager.settings.data_dir)
    assert restored.get()['provider_sources'][0]['api_key'] == SECRET


def test_source_blank_key_preserves_and_explicit_clear_removes_it(catalog):
    manager, _ = catalog
    supplier = source(manager)
    manager.save_source({'name': '重命名', 'api_key': ''}, supplier['id'])
    assert manager.settings.get()['provider_sources'][0]['api_key'] == SECRET
    manager.save_source({'clear_api_key': True}, supplier['id'])
    assert manager.settings.get()['provider_sources'][0]['api_key'] == ''
    assert not manager.snapshot()['providers'][0]['api_key_set']


def test_batch_selection_and_manual_add_are_idempotent_per_source(catalog):
    manager, _ = catalog
    first = source(manager)
    response = add(manager, first['id'], ['alpha', 'alpha', {'model': 'beta', 'vision': True}])
    assert len(response['added']) == 2 and response['skipped'] == ['alpha']
    stable = {item['id'] for item in response['added']}
    repeated = add(manager, first['id'])
    assert not repeated['added'] and repeated['skipped'] == ['alpha', 'beta']
    assert stable <= {item['id'] for item in repeated['models']}
    second = source(manager, enabled=False)
    other = add(manager, second['id'], ['alpha'])['added'][0]
    assert other['id'] not in stable and not other['effective_enabled']


@pytest.mark.anyio
async def test_current_form_and_saved_source_discovery_do_not_save(catalog):
    manager, calls = catalog
    supplier = source(manager)
    before = manager.settings.get()
    result = await manager.discover({'base_url': 'http://127.0.0.1:9876/v1', 'api_key': ''}, supplier['id'])
    assert result['models'] == ['alpha', 'beta', 'gamma']
    assert calls[-1]['base_url'] == 'http://127.0.0.1:9876/v1' and calls[-1]['api_key'] == SECRET
    fresh = await manager.discover({'base_url': 'https://unsaved.example/v1', 'api_key': 'unsaved-key'})
    assert fresh['provider_id'] == '' and calls[-1]['api_key'] == 'unsaved-key'
    assert manager.settings.get() == before
    assert 'unsaved-key' not in str(fresh) and SECRET not in str(result)


def test_routes_resolve_actual_source_model_pair_and_honor_disable(catalog):
    manager, _ = catalog
    supplier = source(manager)
    models = add(manager, supplier['id'])['added']
    first, second = models[0]['id'], models[1]['id']
    value = manager.update_routing({'chat': first, 'planner': second, 'memory': second,
                                    'summary': first, 'embedding': second})
    assert value['routes']['chat']['model'] == 'alpha'
    assert value['routes']['chat']['provider_id'] == supplier['id']
    resolved = resolve_model(manager.settings.get(), 'planner')
    assert resolved['model'] == 'beta' and resolved['api_key'] == SECRET
    assert resolved['base_url'] == 'https://api.example.test/v1'
    manager.update_model(second, {'enabled': False})
    assert not resolve_model(manager.settings.get(), 'planner')['enabled']
    manager.update_model(second, {'enabled': True})
    manager.save_source({'enabled': False}, supplier['id'])
    assert not resolve_model(manager.settings.get(), 'chat')['enabled']
    assert not next(item for item in manager.snapshot()['models'] if item['id'] == first)['effective_enabled']


def test_deleting_models_never_silently_changes_routes_or_session_selection(catalog):
    manager, _ = catalog
    supplier = source(manager)
    model_id = add(manager, supplier['id'], ['alpha'])['added'][0]['id']
    manager.update_routing({'chat': model_id})
    with pytest.raises(ValueError, match='chat'):
        manager.delete_model(model_id)
    assert manager.settings.get()['routing']['chat'] == model_id
    manager.update_routing({'chat': '', 'fallback': [model_id]})
    with pytest.raises(ValueError, match='fallback'):
        manager.delete_model(model_id)
    manager.update_routing({'fallback': []})
    manager.store = SimpleNamespace(list_scopes=lambda: [{'id': 'web:test', 'model_id': model_id}])
    with pytest.raises(ValueError, match='会话'):
        manager.delete_model(model_id)
    manager.store = None
    manager.delete_model(model_id)
    assert manager.settings.get()['providers'] == []
    manager.delete_source(supplier['id'])
    assert manager.settings.get()['provider_sources'] == []


def test_source_with_models_cannot_be_deleted_and_default_is_protected(catalog):
    manager, _ = catalog
    supplier = source(manager)
    add(manager, supplier['id'])
    with pytest.raises(ValueError, match='先删除'):
        manager.delete_source(supplier['id'])
    with pytest.raises(ValueError, match='不能删除'):
        manager.delete_model('default')
    with pytest.raises(ValueError, match='不能停用'):
        manager.update_model('default', {'enabled': False})


@pytest.mark.parametrize('entries', [[''], [1], [{'model': False}], ['s' * 161], ['name\nline']])
def test_invalid_batch_is_atomic(catalog, entries):
    manager, _ = catalog
    supplier = source(manager)
    before = manager.settings.get()
    with pytest.raises(ValueError):
        add(manager, supplier['id'], ['valid-model', *entries])
    assert manager.settings.get() == before


def test_renaming_to_existing_model_after_whitespace_normalization_is_rejected(catalog):
    manager, _ = catalog
    supplier = source(manager)
    models = add(manager, supplier['id'])['added']
    with pytest.raises(ValueError, match='同名'):
        manager.update_model(models[1]['id'], {'model': ' alpha '})
    assert len(manager.settings.get()['providers']) == 2


@pytest.fixture
def api(tmp_path):
    app = create_app(tmp_path / 'catalog-api', start_workers=False)
    install_catalog(app, app.state.settings, app.state.provider, app.state.store)
    with TestClient(app) as client:
        yield client, app


def test_all_new_routes_require_a_webui_session(api):
    client, app = api
    for method, path, payload in [
        ('GET', '/api/model-providers', None), ('POST', '/api/model-providers', {}),
        ('POST', '/api/model-providers/discover', {}), ('PUT', '/api/model-providers/source-x', {}),
        ('DELETE', '/api/model-providers/source-x', None), ('POST', '/api/model-providers/source-x/discover', {}),
        ('POST', '/api/model-catalog/models', {}), ('PATCH', '/api/model-catalog/models/model-x', {}),
        ('DELETE', '/api/model-catalog/models/model-x', None), ('PUT', '/api/model-catalog/routing', {}),
    ]:
        assert client.request(method, path, json=payload).status_code == 401


def test_catalog_http_flow_and_protected_model_delete(api):
    client, app = api
    password_login(client, app)
    supplier = client.post('/api/model-providers', json={'name': 'HTTP测试源', 'base_url': 'https://api.example/v1', 'api_key': SECRET})
    assert supplier.status_code == 200, supplier.text
    source_id = supplier.json()['id']
    added = client.post('/api/model-catalog/models', json={'provider_id': source_id, 'models': ['alpha', 'beta', 'alpha']})
    assert added.status_code == 200, added.text
    assert len(added.json()['added']) == 2 and SECRET not in added.text
    model_id = added.json()['added'][0]['id']
    route = client.put('/api/model-catalog/routing', json={'chat': model_id})
    assert route.status_code == 200 and route.json()['routes']['chat']['model'] == 'alpha'
    assert client.delete('/api/model-catalog/models/' + model_id).status_code == 400
    assert client.patch('/api/model-catalog/models/' + model_id, json={'enabled': False}).status_code == 200
    assert not client.get('/api/model-providers').json()['routes']['chat']['enabled']
    assert client.put('/api/model-catalog/routing', json={'chat': ''}).status_code == 200
    assert client.delete('/api/model-catalog/models/' + model_id).status_code == 200


def test_http_discovery_uses_mock_not_a_public_model_call(api):
    client, app = api
    password_login(client, app)
    observed = []
    async def fake_models(config):
        observed.append(config)
        return ['manual-ready', 'manual-ready']
    app.state.provider.list_models = fake_models
    before = app.state.settings.get()
    response = client.post('/api/model-providers/discover', json={'base_url': 'http://127.0.0.1:12345/v1', 'api_key': SECRET})
    assert response.status_code == 200 and response.json()['models'] == ['manual-ready']
    assert observed[0]['api_key'] == SECRET and app.state.settings.get() == before
    assert SECRET not in response.text


@pytest.mark.anyio
async def test_actual_provider_wire_uses_source_credentials_and_session_model(tmp_path, monkeypatch):
    settings = Settings(tmp_path)
    store = Store(tmp_path)
    provider = Provider(settings, store)
    manager = ModelCatalog(settings, provider, store)
    try:
        supplier = source(manager)
        models = add(manager, supplier['id'])['added']
        manager.update_routing({'chat': models[0]['id']})
        store.upsert_scope({'id': 'web:chosen', 'model_id': models[1]['id']})
        store.upsert_scope({'id': 'web:inherit', 'model_id': ''})
        observed = []
        def handler(request):
            observed.append((str(request.url), request.headers.get('authorization'), json.loads(request.content)['model']))
            return httpx.Response(200, json={'choices': [{'message': {'role': 'assistant', 'content': '离线回执'}}],
                                             'usage': {'prompt_tokens': 3, 'completion_tokens': 4}})
        original = httpx.AsyncClient
        monkeypatch.setattr(httpx, 'AsyncClient', lambda *a, **kw: original(*a, transport=httpx.MockTransport(handler), **kw))
        await provider.complete([{'role': 'user', 'content': '检查路由'}], scope_id='web:chosen')
        await provider.complete([{'role': 'user', 'content': '检查继承'}], scope_id='web:inherit')
        assert [item[2] for item in observed] == ['beta', 'alpha']
        assert all(item[0] == 'https://api.example.test/v1/chat/completions' and item[1] == 'Bearer ' + SECRET for item in observed)
        assert SECRET not in str(store.list_usage())
    finally:
        store.close()


def test_catalog_backup_restore_preserves_entries_but_clears_source_keys(api):
    client, app = api
    password_login(client, app)
    supplier = client.post('/api/model-providers', json={'name': '备份源', 'base_url': 'https://api.example/v1', 'api_key': SECRET}).json()
    added = client.post('/api/model-catalog/models', json={'provider_id': supplier['id'], 'models': ['backup-model']}).json()
    model_id = added['added'][0]['id']
    assert client.put('/api/model-catalog/routing', json={'summary': model_id}).status_code == 200
    backup = client.get('/api/backup')
    assert backup.status_code == 200
    with zipfile.ZipFile(io.BytesIO(backup.content)) as archive:
        config = json.loads(archive.read('settings.json'))
        assert SECRET not in str(config)
        assert config['provider_sources'][0]['api_key_set']
    response = client.post('/api/backup/restore', files={'file': ('backup.zip', backup.content, 'application/zip')})
    assert response.status_code == 200, response.text
    restored = app.state.settings.get()
    assert restored['provider_sources'][0]['api_key'] == ''
    assert restored['providers'][0]['id'] == model_id and restored['routing']['summary'] == model_id


def test_http_scope_model_selection_prevents_deletion_until_inherited(api):
    client, app = api
    password_login(client, app)
    supplier = client.post('/api/model-providers', json={'name': '会话源', 'base_url': 'https://api.example/v1'}).json()
    model_id = client.post('/api/model-catalog/models', json={'provider_id': supplier['id'], 'models': ['chosen-model']}).json()['added'][0]['id']
    response = client.put('/api/scopes/web:default', json={'model_id': model_id})
    assert response.status_code == 200, response.text
    assert client.delete('/api/model-catalog/models/' + model_id).status_code == 400
    assert client.put('/api/scopes/web:default', json={'model_id': ''}).status_code == 200
    assert client.delete('/api/model-catalog/models/' + model_id).status_code == 200
