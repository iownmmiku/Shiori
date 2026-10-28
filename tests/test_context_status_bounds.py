"""The dashboard uses the same valid summary as chat without loading all facts."""
from fastapi.testclient import TestClient

from auth_helpers import password_login
from shiori.api import create_app
from shiori.db import message_fingerprint


def test_context_status_selects_valid_summary_without_full_memory_projection(tmp_path, monkeypatch):
    app = create_app(tmp_path, start_workers=False)
    store = app.state.store
    app.state.settings.update({'context': {'enabled': True}})
    store.upsert_scope({'id': 'web:default', 'context_compress': True, 'auto_memory': False})
    for i in range(300):
        store.create_memory({'content': f'无关事实{i}，用户喜欢音乐。'})
    source = store.add_message('web:default', 'owner', 'user', '我们约好周末讨论菜单。')
    digest = store.create_memory({'content': '周末讨论菜单的旧对话摘要。', 'kind': 'event',
        'property': 'conversation_digest', 'derived': True, 'manual_digest': False,
        'source_ids': [source['id']], 'source_fingerprints': {source['id']: message_fingerprint(source)},
        'covered_message_ids': [source['id']]})
    original = store.visible_memories
    visits = []
    def bounded(*args, **kwargs):
        assert kwargs.get('record_ids') is not None, 'Status must not load all memories'
        visits.append(set(kwargs['record_ids']))
        return original(*args, **kwargs)
    monkeypatch.setattr(store, 'visible_memories', bounded)
    with TestClient(app) as client:
        password_login(client, app)
        response = client.get('/api/context/status')
        assert response.status_code == 200, response.text
        data = response.json()
        assert data['summary']['id'] == digest['id'] and data['source_count'] == 1 and data['covered_count'] == 1
        assert visits and all(ids <= {digest['id']} for ids in visits)
        app.state.settings.update({'context': {'enabled': False}})
        data = client.get('/api/context/status').json()
        assert not data['enabled'] and data['summary'] is None
