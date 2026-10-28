"""Output controls persist independently of segmentation and reject invalid limits."""
from copy import deepcopy

import pytest
from fastapi.testclient import TestClient

from auth_helpers import password_login
from shiori.api import create_app
from shiori.db import Store
from shiori.settings import Settings


def test_output_controls_defaults_and_persistence(tmp_path):
    settings = Settings(tmp_path)
    default = settings.get()['output']
    assert (default['reply_max_chars'], default['message_max_chars'], default['remove_parentheses']) == (0, 0, False)
    patch = {'enabled': False, 'reply_max_chars': 20000, 'message_max_chars': 4000, 'remove_parentheses': True}
    settings.update({'output': patch})
    restored = Settings(tmp_path).get()['output']
    assert all(restored[key] == value for key, value in patch.items())
    assert restored['mode'] == 'model'
    settings.update({'output': {'reply_max_chars': 0, 'message_max_chars': 0}})
    assert settings.get()['output']['remove_parentheses'] is True


@pytest.mark.parametrize(('key', 'value'), [
    ('reply_max_chars', -1), ('reply_max_chars', 20001), ('reply_max_chars', True),
    ('reply_max_chars', 1.5), ('reply_max_chars', '120'), ('reply_max_chars', None),
    ('message_max_chars', -1), ('message_max_chars', 4001), ('message_max_chars', False),
    ('message_max_chars', 1.0), ('message_max_chars', '40'),
    ('remove_parentheses', 1), ('remove_parentheses', 'false'), ('remove_parentheses', None),
])
def test_invalid_global_output_controls_rejected_without_mutation(tmp_path, key, value):
    settings = Settings(tmp_path)
    before = deepcopy(settings.get())
    with pytest.raises(ValueError):
        settings.update({'output': {key: value}})
    assert settings.get() == before


@pytest.mark.parametrize('container', ['output', 'settings', 'flat', 'settings_flat'])
def test_scope_output_controls_allow_partial_inheritance_and_legacy_forms(tmp_path, container):
    store = Store(tmp_path)
    controls = {'reply_max_chars': 0, 'message_max_chars': 130, 'remove_parentheses': True}
    if container == 'output':
        patch = {'output': controls}
    elif container == 'settings':
        patch = {'settings': {'output': controls}}
    else:
        patch = {'output_' + key: value for key, value in controls.items()}
        if container == 'settings_flat':
            patch = {'settings': patch}
    try:
        scope = store.upsert_scope({'id': 'web:content', **patch})
        assert all(scope[key] == value for key, value in patch.items())
        inherited = store.upsert_scope({'id': 'web:partial', 'output': {'reply_max_chars': None, 'remove_parentheses': None}})
        assert inherited['output']['reply_max_chars'] is None
    finally:
        store.close()


@pytest.mark.parametrize(('key', 'value'), [('reply_max_chars', 20001), ('message_max_chars', 4001), ('reply_max_chars', True), ('message_max_chars', 1.1), ('remove_parentheses', 'false')])
def test_scope_output_controls_reject_invalid_values(tmp_path, key, value):
    store = Store(tmp_path)
    try:
        with pytest.raises(ValueError):
            store.upsert_scope({'id': 'web:bad', 'output': {key: value}})
    finally:
        store.close()


def test_output_controls_management_api_roundtrip_and_rejection(tmp_path):
    app = create_app(tmp_path, start_workers=False)
    with TestClient(app) as client:
        password_login(client, app)
        controls = {'reply_max_chars': 500, 'message_max_chars': 100, 'remove_parentheses': True}
        response = client.put('/api/settings', json={'output': controls})
        assert response.status_code == 200, response.text
        assert all(client.get('/api/settings').json()['output'][key] == value for key, value in controls.items())
        response = client.post('/api/scopes', json={'id': 'web:controls', 'output': controls})
        assert response.status_code == 200, response.text
        assert response.json()['output'] == controls
        response = client.put('/api/scopes/web:controls', json={'output': {'message_max_chars': 4001}})
        assert response.status_code == 400, response.text
        assert app.state.store.get_scope('web:controls')['output'] == controls
