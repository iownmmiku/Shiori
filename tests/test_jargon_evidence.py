import pytest
from fastapi.testclient import TestClient

from auth_helpers import password_login
from shiori.api import create_app
from shiori.db import Store
from shiori.jargon_evidence import evidence_page


def test_evidence_uses_canonical_ids_beyond_recent_history_and_pages(tmp_path):
    store = Store(tmp_path)
    try:
        sources = [store.add_message('qq:group:1', 'a', 'user', f'证据{i}') for i in range(52)]
        term = store.create_jargon({'scope_id':'qq:group:1', 'term':'测试群词', 'meaning':'语义',
                                    'source_ids':[row['id'] for row in sources]})
        for i in range(210): store.add_message('qq:group:1', 'b', 'user', f'新消息{i}')
        first = evidence_page(store, term['id'])
        second = evidence_page(store, term['id'], offset=50)
        assert first['total'] == 52 and first['has_more']
        assert first['items'][0]['message']['content'] == '证据0'
        assert len(second['items']) == 2 and not second['has_more']
        store.clear_messages('qq:group:1')
        assert all(row['status'] == 'missing' and 'message' not in row for row in evidence_page(store, term['id'])['items'])
    finally: store.close()


def test_evidence_never_returns_other_scope_content_and_deduplicates(tmp_path):
    store = Store(tmp_path)
    try:
        other = store.add_message('qq:group:2', 'b', 'user', '不应跨群显示')
        term = store.create_jargon({'scope_id':'qq:group:1','term':'旧词','meaning':'旧语义',
            'source_ids':[other['id']], 'evidence':{'message_ids':[other['id'],'removed-id']}})
        page = evidence_page(store, term['id'])
        assert page['total'] == 2
        assert page['items'] == [{'id':other['id'],'status':'unavailable'},{'id':'removed-id','status':'missing'}]
        for offset, limit in [(-1,50),(0,51),(True,1)]:
            with pytest.raises(ValueError): evidence_page(store, term['id'], offset=offset, limit=limit)
    finally: store.close()


def test_evidence_route_requires_authenticated_ui(tmp_path):
    app = create_app(tmp_path, start_workers=False)
    with TestClient(app) as client:
        assert client.get('/api/jargon/anything/evidence').status_code == 401
        password_login(client, app)
        term = app.state.store.create_jargon({'scope_id':'web:default','term':'手动','meaning':'无来源'})
        assert client.get(f'/api/jargon/{term["id"]}/evidence').json()['total'] == 0
        assert client.get(f'/api/jargon/{term["id"]}/evidence?limit=51').status_code == 400
