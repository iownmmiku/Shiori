"""Settings history: the version each save replaces is kept on this machine and
can be put back whole, which is what undoes a supplier overwritten by mistake."""
import shutil
import subprocess
from pathlib import Path

import pytest

from shiori import settings as st
from shiori.settings import Settings

GOOD = {'id': 'main', 'name': '我的模型服务', 'base_url': 'https://good.test/v1', 'api_key': 'good-key-abcdef', 'enabled': True}


def test_each_changing_save_keeps_the_version_it_replaced(tmp_path):
    store = Settings(tmp_path)
    store.update({'planner': {'eagerness': 30}})
    before = len(store.history())
    store.update({'planner': {'eagerness': 60}})
    store.update({'planner': {'eagerness': 60}})           # nothing changed: nothing kept
    history = store.history()
    assert len(history) == before + 1
    assert history[0]['sections'] == ['回复判断'] and history[0]['same_as_current'] is False
    assert history[0]['replaced_at'].endswith('Z') and st._HISTORY_ID.fullmatch(history[0]['id'])
    store.update({'debounce': {'enabled': True}, 'planner': {'eagerness': 30}})
    assert set(store.history()[0]['sections']) == {'回复判断', '消息防抖'}


def test_an_overwritten_supplier_comes_back_with_its_key(tmp_path):
    store = Settings(tmp_path)
    store.update({'provider_sources': [dict(GOOD)]})
    # The mistake: another service's address and key typed into the supplier everything runs on.
    store.update({'provider_sources': [dict(GOOD, name='Jev', base_url='https://other.test/v1', api_key='other-key-123456')]})
    assert store.get()['provider_sources'][0]['api_key'] == 'other-key-123456'
    version = store.history()[0]
    assert version['sections'] == ['供应商连接']
    public = store.restore_history(version['id'])
    source = store.get()['provider_sources'][0]
    assert (source['name'], source['base_url'], source['api_key']) == ('我的模型服务', 'https://good.test/v1', 'good-key-abcdef')
    assert 'api_key' not in public['provider_sources'][0] and public['provider_sources'][0]['api_key_set'] is True
    # It survives a restart, and the mistaken version is itself in the history now.
    assert Settings(tmp_path).get()['provider_sources'][0]['api_key'] == 'good-key-abcdef'
    newest = store.history()[0]
    assert newest['sections'] == ['供应商连接'] and store.history()[1]['same_as_current'] is True
    store.restore_history(newest['id'])
    assert store.get()['provider_sources'][0]['base_url'] == 'https://other.test/v1'
    # No key appears in what the page is given.
    assert 'key-' not in str(store.history())


def test_restoring_leaves_remote_access_and_update_rights_alone(tmp_path):
    store = Settings(tmp_path)
    store.update({'planner': {'eagerness': 30}})
    store.update({'planner': {'eagerness': 70}, 'webui': {'remember_days': 7}})
    old = store.history()[0]['id']
    store.restore_history(old)
    assert store.get()['planner']['eagerness'] == 30 and store.get()['webui']['remember_days'] == 7


def test_bad_or_missing_versions_change_nothing(tmp_path):
    store = Settings(tmp_path)
    store.update({'planner': {'eagerness': 30}})
    store.update({'planner': {'eagerness': 70}})
    folder = tmp_path / st.HISTORY_DIR
    (folder / 'notes.json').write_text('{}', 'utf-8')                     # not one of ours
    (folder / '20200101T000000000000Z.json').write_text('{broken', 'utf-8')
    assert all(st._HISTORY_ID.fullmatch(item['id']) and item['id'] != '20200101T000000000000Z' for item in store.history())
    for bad in ('../settings', 'latest', '', None, '20200101T000000000000Z', '20990101T000000000000Z'):
        with pytest.raises(ValueError):
            store.restore_history(bad)
    assert store.get()['planner']['eagerness'] == 70
    assert not list(tmp_path.glob('.settings-restore-*'))                 # nothing left behind


def test_only_the_newest_versions_are_kept(tmp_path, monkeypatch):
    monkeypatch.setattr(st, 'HISTORY_KEEP', 3)
    store = Settings(tmp_path)
    for value in range(10, 80, 10):
        store.update({'planner': {'eagerness': value}})
    assert len(list((tmp_path / st.HISTORY_DIR).glob('*.json'))) == 3 and len(store.history()) == 3


def test_the_api_lists_and_restores_for_a_signed_in_admin_only(tmp_path):
    from fastapi.testclient import TestClient
    from auth_helpers import password_login
    from shiori.api import create_app
    app = create_app(tmp_path, start_workers=False)
    app.state.settings.update({'planner': {'eagerness': 30}})
    app.state.settings.update({'planner': {'eagerness': 70}})
    with TestClient(app) as client:
        assert client.get('/api/settings/history').status_code == 401
        assert client.post('/api/settings/history/20200101T000000000000Z/restore').status_code == 401
        password_login(client, app)
        items = client.get('/api/settings/history').json()['items']
        target = next(item for item in items if item['sections'] == ['回复判断'])
        response = client.post(f"/api/settings/history/{target['id']}/restore")
        assert response.status_code == 200, response.text
        assert response.json()['planner']['eagerness'] == 30
        assert client.post('/api/settings/history/20990101T000000000000Z/restore').status_code in (400, 422)


def test_changing_a_supplier_in_use_asks_once_before_saving():
    node = shutil.which('node')
    if not node:
        pytest.skip('Node unavailable')
    script = Path(__file__).resolve().parent.parent / 'shiori' / 'static' / 'workspace-pages.js'
    program = r'''
const fs = require("fs"), source = fs.readFileSync(process.argv[process.argv.length - 1], "utf8");
const body = source.slice(source.indexOf("let sourceChangeAcknowledged"), source.indexOf("async function handleSubmit"));
const make = new Function("sources", "catalog", "routeNames", "state", body + "; return sourceInUse;");
const catalog = {providers:[{id:"main",base_url:"https://good.test/v1"},{id:"spare",base_url:"https://spare.test/v1"}],
  models:[{id:"m1",provider_id:"main"},{id:"m2",provider_id:"spare"}], routing:{chat:"m1",planner:"m1",memory:"",fallback:[]}};
const check = make(() => catalog.providers, catalog, {chat:"日常对话",planner:"Planner 语义规划"}, {settings:{prescreen:{mode:"off",source_id:""}}});
const fail = text => { console.error(text); process.exit(1); };
if (check("", {base_url:"https://new.test/v1", api_key:"k"})) fail("a new supplier needs no warning");
if (check("main", {base_url:"https://good.test/v1/", api_key:""})) fail("saving without changing address or key needs no warning");
if (check("spare", {base_url:"https://elsewhere.test/v1", api_key:""})) fail("a supplier nothing runs on needs no warning");
const first = check("main", {base_url:"https://other.test/v1", api_key:"k"});
if (!first.includes("日常对话") || !first.includes("Planner 语义规划") || !first.includes("再点一次保存")) fail("missing warning: " + first);
if (check("main", {base_url:"https://other.test/v1", api_key:"k"})) fail("the same save again must go through");
if (!check("main", {base_url:"https://third.test/v1", api_key:""})) fail("a different change must be asked about again");
const key = check("main", {base_url:"https://good.test/v1", api_key:"new"});
if (!key.includes("密钥")) fail("a key change must be named: " + key);
'''
    result = subprocess.run([node, '-e', program, str(script)], capture_output=True, text=True, encoding='utf-8', timeout=20)
    assert result.returncode == 0, result.stdout + result.stderr
