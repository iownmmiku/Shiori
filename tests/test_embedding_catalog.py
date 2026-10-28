"""Embedding setup tests the embedding wire and never changes chat routing."""
import json
import shutil
import subprocess
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from auth_helpers import password_login
from shiori.api import create_app
from shiori.settings import Settings, resolve_model


@pytest.fixture
def fixture(tmp_path, monkeypatch):
    app = create_app(tmp_path, start_workers=False)
    observed, response = [], {'bad': False, 'mutation': None}
    def handler(request):
        observed.append(request)
        assert request.url.path == '/v1/embeddings'
        assert request.headers['authorization'] == 'Bearer fixture-embedding-secret'
        body = json.loads(request.content)
        assert body['model'] == 'embedding-fixture' and body['encoding_format'] == 'float'
        if response['mutation']: response['mutation']()
        if response['bad']: return httpx.Response(200, json={'data': [{'index': 0, 'embedding': [0, 0]}]})
        return httpx.Response(200, json={'data': [{'index': 0, 'embedding': [.5, .25, .75]}], 'usage': {'prompt_tokens': 7}})
    original = httpx.AsyncClient
    monkeypatch.setattr(httpx, 'AsyncClient', lambda *args, **kwargs: original(*args, transport=httpx.MockTransport(handler), **kwargs))
    with TestClient(app) as client:
        password_login(client, app)
        source = client.post('/api/model-providers', json={'name': '向量服务', 'base_url': 'https://fixture.example/v1', 'api_key': 'fixture-embedding-secret'}).json()
        result = client.post('/api/model-catalog/models', json={'provider_id': source['id'], 'model_type': 'embedding', 'models': ['embedding-fixture']})
        assert result.status_code == 200, result.text
        item = result.json()['added'][0]
        yield client, app, item, observed, response


def test_real_embedding_test_and_activation_preserves_chat(fixture):
    client, app, item, requests, _ = fixture
    before = app.state.settings.get()
    result = client.post('/api/model-catalog/embedding/test', json={'model_id': item['id']})
    assert result.status_code == 200 and result.json()['dimension'] == 3, result.text
    assert not result.json()['activated'] and app.state.settings.get() == before
    assert len(requests) == 1
    assert app.state.store.list_usage()[0]['purpose'] == 'embedding'
    result = client.post('/api/model-catalog/embedding/test', json={'model_id': item['id'], 'activate': True})
    assert result.status_code == 200 and result.json()['activated'], result.text
    config = app.state.settings.get()
    assert config['routing']['chat'] == before['routing']['chat'] and config['model'] == before['model']
    assert config['routing']['embedding'] == item['id'] and config['memory']['semantic_enabled']
    snapshot = client.get('/api/model-providers').json()
    assert snapshot['embedding']['available'] and snapshot['embedding']['model'] == 'embedding-fixture'
    assert 'fixture-embedding-secret' not in str(snapshot)
    assert Settings(app.state.settings.data_dir).get()['providers'][0]['model_type'] == 'embedding'
    assert client.put('/api/model-catalog/embedding', json={'enabled': False}).status_code == 200
    assert app.state.settings.get()['routing']['embedding'] == item['id']


def test_bad_vector_does_not_enable_or_replace_existing_route(fixture):
    client, app, item, _, response = fixture
    response['bad'] = True
    before = app.state.settings.get()
    result = client.post('/api/model-catalog/embedding/test', json={'model_id': item['id'], 'activate': True})
    assert result.status_code == 400 and '向量' in result.json()['detail']
    assert app.state.settings.get() == before


def test_source_change_during_test_cannot_activate_stale_result(fixture):
    client, app, item, _, response = fixture
    def mutate():
        app.state.model_catalog.save_source({'api_key': 'replacement-fixture-secret'}, item['provider_id'])
    response['mutation'] = mutate
    result = client.post('/api/model-catalog/embedding/test', json={'model_id': item['id'], 'activate': True})
    assert result.status_code == 400 and not app.state.settings.get()['memory']['semantic_enabled']


def test_embedding_type_cannot_be_chat_or_fallback_or_chat_connection_test(fixture):
    client, app, item, requests, _ = fixture
    for purpose, value in [('chat', item['id']), ('fallback', [item['id']]), ('planner', item['id'])]:
        assert client.put('/api/model-catalog/routing', json={purpose: value}).status_code == 400
    assert client.post('/api/model/test', json={'provider_id': item['id']}).status_code == 400
    assert client.put('/api/scopes/web:default', json={'model_id': item['id']}).status_code == 400
    assert not requests
    app.state.model_catalog.update_model(item['id'], {'enabled': False})
    assert client.put('/api/model-catalog/embedding', json={'model_id': item['id'], 'enabled': True}).status_code == 400
    assert client.post('/api/model-catalog/embedding/test', json={'model_id': item['id'], 'activate': True}).status_code == 400


def test_legacy_independent_model_test_and_catalog_do_not_rewrite_config(fixture):
    client, app, _, _, _ = fixture
    app.state.settings.update({'model': {'base_url': 'https://fixture.example/v1', 'model': 'legacy-chat', 'api_key': 'fixture-embedding-secret'},
                               'memory': {'embedding_model': 'embedding-fixture'}})
    before = app.state.settings.path.read_bytes()
    snapshot = client.get('/api/model-providers').json()
    assert snapshot['embedding']['model'] == 'embedding-fixture' and snapshot['embedding']['legacy']
    assert app.state.settings.path.read_bytes() == before
    assert client.post('/api/model-catalog/embedding/test', json={'model_id': ''}).status_code == 200
    assert app.state.settings.path.read_bytes() == before


def test_chat_type_cannot_route_to_embedding_and_invalid_type_is_atomic(fixture):
    client, app, item, _, _ = fixture
    model = app.state.model_catalog.add_models({'provider_id': item['provider_id'], 'model_type': 'chat', 'models': ['chat-fixture']})['added'][0]
    assert client.put('/api/model-catalog/routing', json={'embedding': model['id']}).status_code == 400
    assert client.post('/api/model-catalog/embedding/test', json={'model_id': model['id']}).status_code == 400
    before = app.state.settings.get()
    result = client.post('/api/model-catalog/models', json={'provider_id': item['provider_id'], 'model_type': 'unknown', 'models': ['invalid']})
    assert result.status_code == 400 and app.state.settings.get() == before


def test_embedding_page_interaction_contract():
    node = shutil.which('node')
    if not node: pytest.skip('Frontend regression requires Node.js')
    result = subprocess.run([node, str(Path(__file__).with_name('frontend_embedding.cjs'))], capture_output=True, text=True, encoding='utf-8', timeout=20)
    assert result.returncode == 0, result.stdout + result.stderr
