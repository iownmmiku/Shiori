"""Shared preferences and graph recall keep fresh, bounded authorization."""
from copy import deepcopy
from types import SimpleNamespace

import pytest

from mikubot.chat import ChatService
from mikubot.db import Store, message_fingerprint
from mikubot.memory import MemoryEngine
from mikubot.settings import DEFAULTS


@pytest.fixture
def anyio_backend():
    return 'asyncio'


@pytest.fixture
def memory(tmp_path):
    store = Store(tmp_path)
    engine = MemoryEngine(store, {})
    try:
        yield store, engine
    finally:
        engine.close()
        store.close()


def fact(store, identifier, **fields):
    return store.create_memory({'id': identifier, 'scope_id': 'web:default', 'user_id': 'owner',
                                'content': identifier, **fields})


def edge(store, identifier, left, right, **fields):
    return store.create_memory({'id': identifier, 'scope_id': left['scope_id'], 'user_id': 'owner',
        'content': '已核对的关联', 'kind': 'note', 'property': 'memory_relation', 'derived': True,
        'from_id': left['id'], 'to_id': right['id'], 'label': '相关事实', 'relation_type': 'related',
        'dependencies': {left['id']: left['version'], right['id']: right['version']},
        'endpoint_identities': {record['id']: [record['scope_id'], record['user_id']] for record in (left, right)},
        **fields})


@pytest.mark.anyio
@pytest.mark.parametrize('origin_allowed', [False, True])
async def test_shared_preference_obeys_adapter_permission_without_stopping_target_chat(tmp_path, origin_allowed):
    store = Store(tmp_path)
    config = deepcopy(DEFAULTS)
    config['model'].update(base_url='https://fixture.invalid/v1', model='fixture')
    config['onebot'].update(enabled=origin_allowed, adapter='napcat', allowed_groups=['origin'])
    config['stickers']['enabled'] = False
    config['memory']['organization_enabled'] = False
    settings = SimpleNamespace(get=lambda: deepcopy(config))
    store.upsert_scope({'id': 'web:default', 'memory_space': 'shared'})
    store.upsert_scope({'id': 'qq:group:origin', 'memory_space': 'shared'})
    pref = fact(store, 'shared-pref', scope_id='qq:group:origin', kind='preference',
                content='SHARED_PREF_MARKER 喜欢星辉咖啡', visibility='group', shareable=True, pinned=True)
    own = fact(store, 'own-pref', kind='preference', content='OWN_PREF_MARKER 喜欢合作游戏')
    if not origin_allowed:
        with store._lock, store._conn:
            for number in range(70):
                store._save_memory(dict(pref, id=f'withdrawn-{number}'))
    class Model:
        calls = 0
        async def complete(self, messages, **kwargs):
            self.calls += 1
            assert kwargs['before_send']()
            assert ('SHARED_PREF_MARKER' in str(messages)) is origin_allowed
            assert 'OWN_PREF_MARKER' in str(messages)
            return {'content': '你好。', 'tool_calls': [], 'usage': {}}
    model = Model()
    chat = ChatService(store, settings, model)
    try:
        result = await chat.respond('web:default', 'owner', '你好')
        assert result['reply'] == '你好。' and model.calls == 1
        assert own['id'] in {record['id'] for record in result['memories']}
    finally:
        await chat.close()
        store.close()


def test_preference_dependency_from_revoked_origin_is_rechecked(memory):
    store, _ = memory
    store.upsert_scope({'id': 'web:default', 'memory_space': 'shared'})
    store.upsert_scope({'id': 'web:other', 'memory_space': 'shared'})
    dependency = fact(store, 'dep', scope_id='web:other', visibility='group', shareable=True)
    fact(store, 'invalid-pref', kind='preference', pinned=True, dependencies={dependency['id']: 1})
    allowed = fact(store, 'allowed-pref', kind='preference')
    result = store.profile_preferences('web:default', 'owner', 3, scope_allowed=lambda origin: origin != 'web:other')
    assert [record['id'] for record in result] == [allowed['id']]


@pytest.mark.anyio
async def test_unrelated_relation_library_is_not_decoded_for_each_recall(memory, monkeypatch):
    store, engine = memory
    seed = fact(store, 'target-cobalt-iris')
    target = fact(store, 'actual-neighbor')
    left, right = fact(store, 'unrelated-left'), fact(store, 'unrelated-right')
    template = edge(store, 'a-unrelated-0', left, right)
    with store._lock, store._conn:
        for number in range(1, 500):
            store._save_memory(dict(template, id=f'a-unrelated-{number}'))
    wanted = edge(store, 'z-wanted', seed, target)
    await engine.recall('web:default', 'owner', 'target-cobalt-iris')
    decoded = []
    original = store._list
    def tracked(table, *args, **kwargs):
        records = original(table, *args, **kwargs)
        if table == 'memories':
            decoded.extend(record['id'] for record in records)
        return records
    monkeypatch.setattr(store, '_list', tracked)
    def no_global_relations(*args, **kwargs):
        pytest.fail('Graph recall loaded the entire relation library')
    monkeypatch.setattr(store, 'memory_property_records', no_global_relations)
    result = await engine.recall('web:default', 'owner', 'target-cobalt-iris')
    assert target['id'] in {record['id'] for record in result}
    assert wanted['id'] in decoded and not any(identifier.startswith('a-unrelated-') for identifier in decoded)
    assert len(decoded) < 50


def test_invalid_first_pages_do_not_hide_a_later_valid_neighbor(memory, monkeypatch):
    store, engine = memory
    seed, target = fact(store, 'seed'), fact(store, 'target')
    expired = fact(store, 'expired', valid_to='2000-01-01T00:00:00Z')
    template = edge(store, 'a-expired-000', seed, expired)
    with store._lock, store._conn:
        for number in range(1, 70):
            store._save_memory(dict(template, id=f'a-expired-{number:03d}'))
    wanted = edge(store, 'z-valid', seed, target)
    pages = []
    original = store.memory_relations_for_node
    def tracked(*args, **kwargs):
        result = original(*args, **kwargs)
        pages.append([record['id'] for record in result])
        return result
    monkeypatch.setattr(store, 'memory_relations_for_node', tracked)
    result = engine._related('web:default', 'owner', [seed], limit=4)
    assert [record['id'] for record in result] == [target['id']]
    assert result[0]['memory_versions'][wanted['id']] == wanted['version']
    assert len(pages) >= 3 and max(map(len, pages)) <= 32


def test_relation_walk_preserves_two_hop_limit_and_exact_path_dependencies(memory):
    store, engine = memory
    a, b, c, d = [fact(store, name) for name in ('a', 'b', 'c', 'd')]
    first, second, _ = edge(store, 'ab', a, b), edge(store, 'bc', b, c), edge(store, 'cd', c, d)
    result = engine._related('web:default', 'owner', [a], limit=4)
    assert [record['id'] for record in result] == ['b', 'c']
    assert result[1]['path_ids'] == ['a', first['id'], 'b', second['id'], 'c']
    assert result[1]['memory_versions'] == {'a': 1, first['id']: 1, 'b': 1, second['id']: 1, 'c': 1}


@pytest.mark.parametrize('change', ['endpoint_version', 'endpoint_identity', 'source_deleted', 'source_withdrawn', 'pending'])
def test_neighbor_candidates_keep_endpoint_and_source_authorization(memory, change):
    store, engine = memory
    seed, target = fact(store, 'seed'), fact(store, 'target')
    source = store.add_message('web:default', 'owner', 'user', '这两条记录有关联。')
    relation = edge(store, 'relation', seed, target, source_ids=[source['id']],
                    source_fingerprints={source['id']: message_fingerprint(source)})
    assert [record['id'] for record in engine._related('web:default', 'owner', [seed])] == ['target']
    if change == 'endpoint_version':
        store.update_memory(target['id'], {'content': '已更正的事实'})
    elif change == 'endpoint_identity':
        store.update_memory(relation['id'], {'endpoint_identities': {'seed': ['web:default', 'someone-else'],
                                                                   'target': ['web:default', 'owner']}})
    elif change == 'source_deleted':
        store.clear_messages('web:default')
    elif change == 'source_withdrawn':
        withdrawn = fact(store, 'withdrawn', source_ids=[source['id']])
        store.delete_memory(withdrawn['id'])
    else:
        store.update_memory(relation['id'], {'status': 'pending'})
    assert engine._related('web:default', 'owner', [seed]) == []


def test_relation_candidates_obey_shared_scope_permissions_and_consent(memory):
    store, _ = memory
    for scope_id in ('web:default', 'web:origin'):
        store.upsert_scope({'id': scope_id, 'memory_space': 'shared'})
    a, b = [fact(store, name, scope_id='web:origin', visibility='group', shareable=True) for name in ('a', 'b')]
    relation = edge(store, 'relation', a, b, visibility='group', shareable=True)
    page = lambda **kwargs: store.memory_relations_for_node('web:default', 'owner', 'a', **kwargs)
    assert [record['id'] for record in page()] == [relation['id']]
    assert page(scope_allowed=lambda origin: origin != 'web:origin') == []
    store.update_memory(relation['id'], {'shareable': False})
    assert page() == []


@pytest.mark.anyio
async def test_path_revision_during_later_hop_is_rejected_at_final_boundary(memory, monkeypatch):
    store, engine = memory
    seed = fact(store, 'target-cobalt-iris')
    b, c = fact(store, 'b'), fact(store, 'c')
    edge(store, 'ab', seed, b)
    edge(store, 'bc', b, c)
    original = store.memory_relations_for_node
    def revise(*args, **kwargs):
        result = original(*args, **kwargs)
        if args[2] == b['id']:
            store.update_memory(seed['id'], {'content': 'already revised'})
        return result
    monkeypatch.setattr(store, 'memory_relations_for_node', revise)
    result = await engine.recall('web:default', 'owner', 'target-cobalt-iris')
    assert result == []
