"""Event bodies and source inspection retain existing storage/privacy boundaries."""
import pytest
from fastapi.testclient import TestClient

from auth_helpers import password_login
from shiori.api import create_app
from shiori.db import Store, message_fingerprint


@pytest.fixture
def store(tmp_path):
    value = Store(tmp_path / 'data')
    yield value
    value.close()


@pytest.fixture
def client(tmp_path):
    app = create_app(tmp_path / 'api', start_workers=False)
    with TestClient(app) as connection:
        password_login(connection, app)
        yield connection, app.state.store


def episode(**changes):
    return {'title': '周六合奏', 'summary': '因下雨改在室内排练',
            'narrative': '小林先提议公园合奏，下雨后我们转到活动室，练完了新曲。',
            'participants': ['我', '小林'], 'location': '活动室',
            'outcome': '约定下周再练第二段', **changes}


def sourced(store, messages, **changes):
    return store.create_memory({'kind': 'event', 'episode': episode(),
        'source_ids': [item['id'] for item in messages],
        'source_fingerprints': {item['id']: message_fingerprint(item) for item in messages}, **changes})


def rewrite_message(store, message, **changes):
    changed = dict(message, **changes)
    with store._lock, store._conn:
        store._write('messages', changed, ('scope_id', 'user_id', 'role', 'created_at'))
    return changed


def test_episode_is_authoritative_full_body_and_legacy_events_stay_flat(store):
    body = episode(narrative='完整经过。' * 500 + '最后发现的青铜鸢尾')
    event = store.create_memory({'kind': 'event', 'episode': body, 'content': '过期短句'})
    assert event['episode'] == body
    assert body['narrative'] in event['content']
    assert all(value in event['content'] for value in [body['title'], body['outcome'], '我、小林'])
    assert len(event['content']) <= 6000
    assert '过期短句' not in event['content']
    assert 'event_time_start' not in event and 'event_time_end' not in event
    legacy = store.create_memory({'kind': 'event', 'content': '以前一起排练过'})
    assert 'episode' not in legacy and legacy['content'] == '以前一起排练过'


@pytest.mark.parametrize('change', [
    {'kind': 'fact', 'episode': episode()},
    {'kind': 'event', 'episode': {}},
    {'kind': 'event', 'episode': episode(narrative='')},
    {'kind': 'event', 'episode': episode(narrative='长' * 3601)},
    {'kind': 'event', 'episode': episode(participants=['人'] * 9)},
    {'kind': 'event', 'episode': episode(participants=['人' * 81])},
    {'kind': 'event', 'episode': episode(title='名' * 121)},
    {'kind': 'event', 'episode': episode(scope_id='qq:group:other')},
])
def test_invalid_episode_never_partially_writes(store, change):
    with pytest.raises(ValueError):
        store.create_memory({'content': '旧文', **change})
    assert store.list_memories() == []


def test_episode_update_versions_revokes_sources_and_invalidates_dependents(store):
    message = store.add_message('web:default', 'owner', 'user', '我们雨天去活动室排练')
    event = sourced(store, [message])
    dependent = store.create_memory({'content': '依赖排练安排', 'dependencies': {event['id']: 1}})
    assert store.memory_accessible('web:default', 'owner', dependent['id'])
    edited = store.update_memory(event['id'], {'episode': episode(outcome='后来取消了下次排练')})
    assert edited['version'] == 2 and '后来取消了下次排练' in edited['content']
    assert edited['blocked_source_ids'] == [message['id']]
    assert not store.memory_accessible('web:default', 'owner', dependent['id'])
    versions = store.list_memory_versions(event['id'])
    assert versions[0]['episode']['outcome'] == '约定下周再练第二段'
    assert versions[1]['episode'] == edited['episode']
    assert store.memory_sources_page(event['id'])['items'][0]['reason'] == 'source_revoked'


def test_content_only_patch_clears_episode_and_null_keeps_last_body(store):
    first = store.create_memory({'kind': 'event', 'episode': episode()})
    flat = store.update_memory(first['id'], {'content': '人工纠正：排练没有发生'})
    assert 'episode' not in flat and flat['content'] == '人工纠正：排练没有发生'
    second = store.create_memory({'kind': 'event', 'episode': episode()})
    cleared = store.update_memory(second['id'], {'episode': None})
    assert 'episode' not in cleared and cleared['content'] == second['content']
    assert store.update_memory(second['id'], {'kind': 'fact'})['kind'] == 'fact'
    third = store.create_memory({'kind': 'event', 'episode': episode()})
    with pytest.raises(ValueError):
        store.update_memory(third['id'], {'kind': 'fact'})
    assert store.get_memory(third['id']) == third


@pytest.mark.parametrize('fields', [
    {'event_time_end': 'not-a-date'}, {'event_time_end': False},
    {'event_time_start': '2026-10-05', 'event_time_end': '2026-10-04'},
    {'event_at': '2026-10-05', 'event_time_end': '2026-10-04'},
])
def test_event_end_validation(store, fields):
    with pytest.raises(ValueError):
        store.create_memory({'kind': 'event', 'content': '排练', **fields})


def test_event_time_unknown_stays_unknown_and_profile_uses_start(store):
    newest = store.create_memory({'kind': 'event', 'content': '新事件',
                                 'event_time_start': '2026-10-10T08:00:00+08:00',
                                 'event_time_end': '2026-10-10T00:00:00Z'})
    older = store.create_memory({'kind': 'event', 'content': '旧事件', 'event_time_start': '2000-01-01'})
    assert newest['event_time_start'] == newest['event_time_end'] == '2026-10-10T00:00:00.000000Z'
    assert [item['id'] for item in store.profile('web:default', 'owner')['recent_events']] == [newest['id'], older['id']]
    unknown = store.create_memory({'kind': 'event', 'content': '日期未知', 'event_time_start': None,
                                   'event_time_end': None})
    assert unknown['event_time_start'] is None and unknown['event_time_end'] is None


def test_candidates_keep_episode_and_deduplicate_complete_body(store):
    source = store.add_message('web:default', 'owner', 'user', '很长的排练经过')
    args = {'scope_id': 'web:default', 'user_id': 'owner', 'source_ids': [source['id']]}
    body = episode(narrative='前半经过。' * 300 + '结尾甲')
    one = store.ingest_memory_candidates([{'kind': 'event', 'episode': body,
        'content': '误导短句', 'source_fingerprints': {source['id']: 'untrusted'}}], **args)[0]
    duplicate, distinct = store.ingest_memory_candidates([
        {'kind': 'event', 'episode': body, 'content': '另一个误导短句'},
        {'kind': 'event', 'episode': episode(narrative='前半经过。' * 300 + '结尾乙')}
    ], **args)
    assert duplicate['id'] == one['id'] and distinct['id'] != one['id']
    assert one['episode'] == body and one['status'] == 'pending'
    assert one['source_fingerprints'] == {source['id']: message_fingerprint(source)}
    assert store.memory_sources_page(one['id'])['items'][0]['message'] == source


def test_source_pages_keep_order_and_never_return_modified_missing_or_foreign_text(store):
    own = store.add_message('web:default', 'owner', 'user', '可以核验的原消息')
    changed = store.add_message('web:default', 'owner', 'user', '已过期原消息')
    missing = store.add_message('web:default', 'owner', 'user', '将删除的原消息')
    foreign = store.add_message('web:other', 'owner', 'user', '其他会话秘密')
    stranger = store.add_message('web:default', 'other-user', 'user', '其他用户秘密')
    tool = store.add_message('web:default', 'owner', 'tool', '工具秘密')
    event = sourced(store, [own, changed, missing, foreign, stranger, tool])
    rewrite_message(store, changed, content='修改后的原消息')
    with store._lock, store._conn:
        store._conn.execute('DELETE FROM messages WHERE id=?', (missing['id'],))
    first = store.memory_sources_page(event['id'], limit=2)
    assert first['total'] == 6 and first['has_more'] and first['next_offset'] == 2
    assert [item['id'] for item in first['items']] == [own['id'], changed['id']]
    assert first['items'][0]['message'] == own
    assert first['items'][1]['reason'] == 'source_changed'
    second = store.memory_sources_page(event['id'], offset=2, limit=4)
    assert not second['has_more'] and second['next_offset'] == 6
    assert all(not item['available'] and item['message'] is None for item in second['items'])
    assert [item['reason'] for item in second['items']] == ['source_missing_or_outside_scope'] * 2 + ['source_identity_mismatch'] * 2
    assert store.memory_sources_page(event['id'], offset=6)['items'] == []


def test_old_sources_without_fingerprints_stay_hidden_and_group_identity_is_scoped(store):
    source = store.add_message('web:default', 'owner', 'user', '旧手工引用')
    old = store.create_memory({'content': '手工资料', 'source_ids': [source['id']]})
    item = store.memory_sources_page(old['id'])['items'][0]
    assert item == {'id': source['id'], 'available': False, 'message': None, 'reason': 'source_fingerprint_unavailable'}
    group_source = store.add_message('qqofficial:group:one', 'member', 'user', '群友的排练记录')
    public = sourced(store, [group_source], scope_id='qqofficial:group:one', user_id='owner', visibility='group')
    assert store.memory_sources_page(public['id'])['items'][0]['available']
    private = sourced(store, [group_source], scope_id='qqofficial:group:one', user_id='owner', visibility='private')
    assert store.memory_sources_page(private['id'])['items'][0]['message'] is None
    rewrite_message(store, group_source, user_id='changed-member')
    assert store.memory_sources_page(public['id'])['items'][0]['reason'] == 'source_changed'


def test_source_lookup_is_bounded_to_current_page(store):
    messages = [store.add_message('web:default', 'owner', 'user', str(i)) for i in range(25)]
    event = sourced(store, messages)
    queries = []
    store._conn.set_trace_callback(queries.append)
    try:
        page = store.memory_sources_page(event['id'], limit=3, offset=10)
    finally:
        store._conn.set_trace_callback(None)
    source_queries = [q for q in queries if 'SELECT id,data FROM messages' in q]
    assert len(source_queries) == 1
    assert all(message['id'] not in source_queries[0] for message in messages[:10] + messages[13:])
    assert [item['id'] for item in page['items']] == [item['id'] for item in messages[10:13]]


def test_private_assistant_sources_allow_bot_or_owner_identity_only(store):
    bot = store.add_message('web:default', 'assistant', 'assistant', '助手回复')
    reminder = store.add_message('web:default', 'owner', 'assistant', '本人的提醒')
    other = store.add_message('web:default', 'someone-else', 'assistant', '其他人的提醒')
    event = sourced(store, [bot, reminder, other])
    items = store.memory_sources_page(event['id'])['items']
    assert [item['available'] for item in items] == [True, True, False]
    assert items[2]['message'] is None and items[2]['reason'] == 'source_identity_mismatch'


def test_backup_roundtrip_preserves_flat_and_structured_event_versions(store, tmp_path):
    old = store.create_memory({'kind': 'event', 'content': '旧版纯文本事件'})
    source = store.add_message('web:default', 'owner', 'user', '原始排练经过')
    new = sourced(store, [source])
    backup = store.backup(tmp_path / 'events.db')
    store.update_memory(new['id'], {'content': '临时修改'})
    store.restore_from(backup)
    assert store.get_memory(old['id'])['content'] == '旧版纯文本事件'
    assert 'episode' not in store.get_memory(old['id'])
    assert store.get_memory(new['id']) == new
    assert store.list_memory_versions(new['id']) == [new]
    assert store.memory_sources_page(new['id'])['items'][0]['message'] == source
    no_messages = store.backup(tmp_path / 'without-messages.db', include_messages=False)
    store.restore_from(no_messages)
    assert store.get_memory(new['id'])['episode'] == new['episode']
    assert store.memory_sources_page(new['id'])['items'][0]['message'] is None


def test_api_detail_patch_sources_and_kind_filter_before_pagination(client):
    connection, store = client
    posted = connection.post('/api/memories', json={'kind': 'event', 'episode': episode(), 'status': 'pending'})
    assert posted.status_code == 200, posted.text
    event = posted.json()
    for i in range(4):
        store.create_memory({'kind': 'fact', 'content': f'较新的事实{i}', 'status': 'pending'})
    detail = connection.get(f"/api/memories/{event['id']}")
    assert detail.json() == event
    for path in ('/api/memories', '/api/memory/candidates'):
        response = connection.get(path, params={'kind': 'event', 'limit': 1}).json()
        assert response['total'] == 1 and response['items'][0]['id'] == event['id']
        assert not response['has_more']
        assert len(connection.get(path, params={'kind': 'fact'}).json()) == 4
        assert connection.get(path, params={'kind': 'unsupported', 'limit': 1}).status_code == 400
    assert connection.get('/api/memories/missing').status_code == 404
    assert connection.get(f"/api/memories/{event['id']}/sources").json()['items'] == []
    invalid = connection.put(f"/api/memories/{event['id']}", json={'content': '长' * 6001})
    assert invalid.status_code == 400
    edited = connection.put(f"/api/memories/{event['id']}", json={'episode': episode(outcome='结局已修正')})
    assert edited.status_code == 200 and '结局已修正' in edited.json()['content']
    cleared = connection.put(f"/api/memories/{event['id']}", json={'content': '人工更正内容'}).json()
    assert cleared['content'] == '人工更正内容' and 'episode' not in cleared
    assert connection.get(f"/api/memories/{event['id']}/sources", params={'offset': -1}).status_code == 400
    assert connection.get(f"/api/memories/{event['id']}/sources", params={'limit': 0}).status_code == 400


def test_api_sources_require_session_and_never_leak_old_excerpt(client):
    connection, store = client
    source = store.add_message('web:default', 'owner', 'user', '原文不可从旧快照恢复')
    event = sourced(store, [source])
    url = f"/api/memories/{event['id']}/sources"
    assert connection.get(url).json()['items'][0]['message'] == source
    store.clear_messages('web:default')
    response = connection.get(url)
    assert response.status_code == 200 and response.json()['items'][0]['message'] is None
    assert source['content'] not in response.text
    connection.cookies.clear()
    assert connection.get(url).status_code == 401
    assert connection.get(f"/api/memories/{event['id']}").status_code == 401
