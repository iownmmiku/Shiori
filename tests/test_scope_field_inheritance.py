import copy

import pytest

from shiori.db import Store
from shiori.profiles import resolve_scope
from shiori.settings import DEFAULTS


class Config:
    def __init__(self):
        self.value = copy.deepcopy(DEFAULTS)
        self.value['bot_profiles'] = {'default_id': 'main', 'items': [
            {'id': 'main', 'name': '默认配置', 'values': {}}]}

    def get(self):
        return copy.deepcopy(self.value)


@pytest.fixture
def bound(tmp_path):
    store, config = Store(tmp_path), Config()
    store.scope_resolver = lambda scope: resolve_scope(scope, config)
    store.upsert_scope({'id': 'qq:group:42', 'profile_id': '',
                        'auto_memory': False, 'dictionary_use': True,
                        'settings': {'auto_memory': False, 'unrelated': 'keep'}})
    yield store, config
    store.close()


def test_single_field_reset_clears_raw_override_and_follows_future_defaults(bound):
    store, config = bound
    result = store.upsert_scope({'id': 'qq:group:42', 'inherit_fields': ['auto_memory'], 'name': '改名'})
    assert result['auto_memory'] is True
    assert result['dictionary_use'] is True
    assert result['profile_overrides'] == ['dictionary_use']
    assert result['settings'] == {'unrelated': 'keep'}
    assert 'inherit_fields' not in result
    config.value['bot_profiles']['items'][0]['values']['auto_memory'] = False
    assert store.get_scope('qq:group:42')['auto_memory'] is False
    config.value['bot_profiles']['items'][0]['values']['auto_memory'] = True
    assert store.get_scope('qq:group:42')['auto_memory'] is True
    assert store.get_scope('qq:group:42')['dictionary_use'] is True


def test_rename_only_retains_inheritance_and_existing_independent_choice(bound):
    store, config = bound
    store.upsert_scope({'id': 'qq:group:42', 'inherit_fields': ['auto_memory']})
    store.upsert_scope({'id': 'qq:group:42', 'name': '只改名称'})
    config.value['bot_profiles']['items'][0]['values'].update(auto_memory=False, dictionary_use=False)
    scope = store.get_scope('qq:group:42')
    assert scope['auto_memory'] is False and scope['dictionary_use'] is True


@pytest.mark.parametrize('payload', [
    {'inherit_fields': 'auto_memory'}, {'inherit_fields': ['not_a_field']},
    {'inherit_fields': [{}]}, {'inherit_fields': ['auto_memory'], 'auto_memory': True},
    {'inherit_fields': ['auto_memory'], 'settings': {'auto_memory': True}},
    {'inherit_fields': ['auto_memory'], 'profile_overrides': []},
    {'inherit_fields': ['auto_memory'], 'profile_id': ''},
])
def test_invalid_reset_does_not_mutate_scope(bound, payload):
    store, _ = bound
    before = store.get_scope('qq:group:42')
    with pytest.raises(ValueError):
        store.upsert_scope({'id': 'qq:group:42', **payload})
    assert store.get_scope('qq:group:42') == before


def test_legacy_and_new_unbound_scopes_cannot_silently_change_binding(bound):
    store, _ = bound
    with store._lock, store._conn:
        store._write('scopes', {'id': 'qq:group:legacy', 'name': '旧会话'}, ())
    for scope in ('qq:group:legacy', 'qq:group:unknown'):
        with pytest.raises(ValueError, match='绑定'):
            store.upsert_scope({'id': scope, 'inherit_fields': ['auto_memory']})
    assert 'qq:group:unknown' not in {scope['id'] for scope in store.list_scopes()}
