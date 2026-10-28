"""Management API integration for identity, explicit sharing and public knowledge."""
import io
import json
import zipfile

import pytest
from fastapi.testclient import TestClient

from shiori.api import create_app
from shiori.settings import resolved_identity
from auth_helpers import password_login


@pytest.fixture
def client(tmp_path):
    app = create_app(tmp_path / 'api-v5', start_workers=False)
    with TestClient(app) as c:
        password_login(c, app)
        yield c, app


def backup_with(data, *, knowledge=None, omit_knowledge=False):
    incoming = zipfile.ZipFile(io.BytesIO(data))
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w') as archive:
        for name in incoming.namelist():
            if name == 'persona_knowledge.json':
                if omit_knowledge:
                    continue
                if knowledge is not None:
                    archive.writestr(name, json.dumps(knowledge))
                    continue
            archive.writestr(name, incoming.read(name))
    return buffer.getvalue()


def explore(c, app):
    async def search(query):
        assert '动画' in query
        return {'links': ['https://public.example/animation']}
    async def read(url):
        from datetime import datetime,timezone
        return {'url': url, 'title': '动画公开制作资讯', 'text': '公开资料介绍动画音乐制作环节，播出计划仍待后续公告。' * 8,
                'published_at':datetime.now(timezone.utc).isoformat()}
    app.state.chat.knowledge.configure_sources('researcher',{'allowed_domains':['public.example']})
    app.state.chat.knowledge.search_fn = search
    app.state.chat.knowledge.read_fn = read
    app.state.chat.knowledge.provider = None
    response = c.post('/api/knowledge/explore', json={'persona_id': 'researcher'})
    assert response.status_code == 200, response.text
    return response.json()


def persona(c):
    result = c.post('/api/personas', json={'id': 'researcher', 'name': '研究伙伴', 'system_prompt': '关注公开动画资料',
                                          'interests': ['动画'], 'exploration_enabled': False})
    assert result.status_code == 200, result.text
    return result.json()


def test_identity_defaults_scope_inheritance_and_explicit_override(client):
    c, app = client
    cfg = c.get('/api/settings').json()
    assert cfg['identity']['name'] == '初音未来'
    assert [item['name'] for item in c.get('/api/personas').json()] == ['初音未来']
    result = c.put('/api/settings', json={'identity': {'name': 'Miku', 'aliases': ['初音', '39'], 'respond_to_name': False}})
    assert result.status_code == 200, result.text
    scope = c.put('/api/scopes/web:default', json={'trigger_at': None, 'trigger_name': True, 'plan_enabled': False,
                                                'always_reply': True, 'bot_name': '', 'bot_aliases': []}).json()
    effective = resolved_identity(app.state.settings.get(), scope)
    assert effective['name'] == 'Miku' and effective['trigger_name'] and not effective['plan_enabled']
    assert effective['always_reply'] and '39' in effective['aliases']
    assert c.put('/api/scopes/web:default', json={'trigger_name': 'yes'}).status_code == 400
    assert c.put('/api/scopes/web:default', json={'always_reply': 1}).status_code == 400
    assert c.put('/api/scopes/web:default', json={'plan_enabled': None}).status_code == 200
    assert c.put('/api/scopes/web:default', json={'output': {'interval_ms':'bad'}}).status_code == 400
    assert c.put('/api/scopes/web:default', json={'output': {'target_chars':60,'max_chars':80}}).status_code == 200


def test_damaged_persona_knowledge_does_not_block_database_backup(tmp_path):
    data = tmp_path/'degraded'
    data.mkdir()
    (data/'persona_knowledge.json').write_text('{bad', 'utf-8')
    app = create_app(data, start_workers=False)
    with TestClient(app) as c:
        password_login(c, app)
        assert c.get('/api/knowledge/status').json()['degraded']
        archive = zipfile.ZipFile(io.BytesIO(c.get('/api/backup').content))
        assert 'qunyu.db' in archive.namelist() and 'persona_knowledge.json' not in archive.namelist()
        assert json.loads(archive.read('manifest.json'))['warnings']
        assert (data/'persona_knowledge.json').read_text('utf-8') == '{bad'


def test_shared_space_requires_both_scope_membership_and_memory_opt_in(client):
    c, app = client
    for scope_id in ('qq:group:100', 'qq:group:200'):
        response = c.post('/api/scopes', json={'id': scope_id, 'name': scope_id, 'memory_space': '游戏伙伴'})
        assert response.status_code == 200, response.text
    memory = c.post('/api/memories', json={'scope_id': 'qq:group:100', 'user_id': '1000', 'content': '游戏计划周六集合',
                                         'visibility': 'group'}).json()
    assert not app.state.store.recall('qq:group:200', '2000', '游戏计划')
    assert c.put('/api/memories/' + memory['id'], json={'shareable': True}).status_code == 200
    assert app.state.store.recall('qq:group:200', '2000', '游戏计划')
    assert c.put('/api/scopes/qq:group:200', json={'memory_space': ''}).status_code == 200
    assert not app.state.store.recall('qq:group:200', '2000', '游戏计划')
    assert c.post('/api/memories', json={'content': '秘密住址', 'visibility': 'private', 'shareable': True}).status_code == 400


def test_persona_interests_validation_and_public_knowledge_crud(client):
    c, app = client
    saved = persona(c)
    assert saved['interests'] == ['动画'] and not saved['exploration_enabled']
    assert c.put('/api/personas/researcher', json={'interests': [123]}).status_code == 400
    assert c.put('/api/personas/researcher', json={'exploration_enabled': 'yes'}).status_code == 400
    result = explore(c, app)
    assert result['created'] == 1
    records = c.get('/api/knowledge', params={'persona_id': 'researcher'}).json()
    assert len(records) == 1 and records[0]['url'] == 'https://public.example/animation'
    assert c.get('/api/knowledge/status').json()['searches_today'] == 1
    assert not c.get('/api/memories').json()
    assert c.delete('/api/knowledge/' + records[0]['id']).json()['status'] == 'deleted'
    assert not c.get('/api/knowledge', params={'persona_id': 'researcher'}).json()
    deleted = c.get('/api/knowledge', params={'persona_id': 'researcher', 'status': 'deleted'}).json()
    assert deleted[0]['text'] == ''


def test_knowledge_backup_restore_preserves_public_sources_budget_and_persona(client):
    c, app = client
    persona(c)
    item = explore(c, app)['items'][0]
    response = c.get('/api/backup')
    assert response.status_code == 200, response.text
    archive = zipfile.ZipFile(io.BytesIO(response.content))
    state = json.loads(archive.read('persona_knowledge.json'))
    assert state['items'][0]['id'] == item['id'] and state['budget']['searches'] == 1
    detail = state['personas']['researcher']['last_result']
    assert detail['reason'] == 'collected' and detail['created'] == 1
    assert state['personas']['researcher']['focus_cursors']
    assert c.delete('/api/knowledge/' + item['id']).status_code == 200
    restored = c.post('/api/backup/restore', files={'file': ('v5.zip', response.content, 'application/zip')})
    assert restored.status_code == 200, restored.text
    assert c.get('/api/settings').json()['paused']
    assert c.get('/api/knowledge', params={'persona_id': 'researcher'}).json()[0]['id'] == item['id']
    assert c.get('/api/knowledge/status').json()['searches_today'] == 1
    assert c.get('/api/knowledge/status').json()['personas']['researcher']['last_result'] == detail


def test_invalid_knowledge_backup_precheck_keeps_database_settings_and_file(client):
    c, app = client
    persona(c)
    item = explore(c, app)['items'][0]
    backup = c.get('/api/backup').content
    live = c.post('/api/memories', json={'content': '备份之后的新内容'}).json()
    before_settings = app.state.settings.get()
    before_file = app.state.chat.knowledge.path.read_bytes()
    invalid = backup_with(backup, knowledge={'version': 1, 'items': [{'id': 'bad'}], 'budget': {'day': '', 'searches': 0}, 'personas': {}})
    response = c.post('/api/backup/restore', files={'file': ('bad.zip', invalid, 'application/zip')})
    assert response.status_code == 400, response.text
    assert app.state.store.get_memory(live['id'])['content'] == '备份之后的新内容'
    assert app.state.settings.get() == before_settings
    assert app.state.chat.knowledge.path.read_bytes() == before_file
    assert c.get('/api/knowledge', params={'persona_id': 'researcher'}).json()[0]['id'] == item['id']


def test_old_zip_without_knowledge_restores_compatibly(client):
    c, app = client
    persona(c)
    old_zip = backup_with(c.get('/api/backup').content, omit_knowledge=True)
    explore(c, app)
    response = c.post('/api/backup/restore', files={'file': ('old.zip', old_zip, 'application/zip')})
    assert response.status_code == 200, response.text
    assert not c.get('/api/knowledge', params={'persona_id': 'researcher'}).json()
    assert c.get('/api/knowledge/status').json()['searches_today'] == 0


def test_new_static_module_is_served_locally_without_build_step(client):
    c, _ = client
    index = c.get('/').text
    assert 'shiori' in index and '/static/workspace-features.js' in index
    features = c.get('/static/workspace-features.js')
    assert features.status_code == 200 and 'ShioriFeatures' in features.text
