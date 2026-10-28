"""The actual frontend's onboarding writes must satisfy the real HTTP contract."""
import json
from pathlib import Path
import shutil
import subprocess

from fastapi.testclient import TestClient
import pytest

from auth_helpers import password_login
from shiori.api import create_app


def frontend_requests(settings):
    node = shutil.which('node')
    if not node:
        pytest.skip('Frontend contract regression requires Node.js')
    script = r'''
const fs=require('node:fs'),vm=require('node:vm');
const input=JSON.parse(fs.readFileSync(0,'utf8')),calls=[];
const sandbox={window:{},state:{},routeNames:{chat:'聊天',memory:'记忆'},
  api:async(url,options)=>{calls.push({url,...options});return {};},
  getForm:form=>form.values,toast(){},bootstrap:async()=>{}};
vm.createContext(sandbox);
for(const file of ['quick-start.js','workspace-pages.js'])vm.runInContext(fs.readFileSync(input.static+'/'+file,'utf8'),sandbox);
(async()=>{
  const quick=sandbox.window.ShioriQuickStart,pages=sandbox.window.ShioriPages;
  const defaults=quick.buildPatch(1,{chat_model_id:'default'},input.settings);
  const source=quick.buildPatch(1,{chat_model_id:input.source_model},input.settings);
  await pages.handleAction('route-chat-model',{dataset:{id:'default'}});
  await pages.handleSubmit('catalog-routing',{values:{chat:'default',memory:'keep'}});
  process.stdout.write(JSON.stringify({defaults,source,calls}));
})().catch(error=>{console.error(error);process.exitCode=1;});
'''
    result = subprocess.run([node, '-e', script], input=json.dumps({
        'static': str(Path(__file__).resolve().parents[1] / 'shiori' / 'static'),
        'settings': settings, 'source_model': next(item['id'] for item in settings['providers'] if item.get('source_id')),
    }), capture_output=True, text=True, encoding='utf-8', timeout=20)
    assert result.returncode == 0, result.stdout + result.stderr
    return json.loads(result.stdout)


def test_onboarding_and_catalog_default_routes_preserve_existing_configuration(tmp_path):
    app = create_app(tmp_path / 'onboarding-api', start_workers=False)
    app.state.settings.update({
        'model': {'base_url': 'https://original.example.test/v1', 'model': 'original-chat', 'api_key': 'fixture-original-secret'},
        'providers': [{'id': 'keep', 'name': '其他任务', 'base_url': 'https://keep.example.test/v1', 'model': 'keep-model'}],
        'routing': {'chat': 'keep', 'planner': 'keep', 'memory': 'keep'},
    })
    with TestClient(app) as client:
        password_login(client, app)
        source = client.post('/api/model-providers', json={
            'name': '新供应商', 'base_url': 'http://127.0.0.1:12345/v1', 'api_key': 'fixture-source-secret',
        })
        assert source.status_code == 200, source.text
        added = client.post('/api/model-catalog/models', json={'provider_id': source.json()['id'], 'models': ['new-chat']})
        assert added.status_code == 200, added.text
        model_id = added.json()['added'][0]['id']
        before = app.state.settings.get()
        requests = frontend_requests(client.get('/api/settings').json())

        assert requests['defaults'] == {'routing': {'chat': ''}}
        assert client.put('/api/settings', json=requests['defaults']).status_code == 200
        assert app.state.settings.get()['routing']['chat'] == ''
        assert requests['source'] == {'routing': {'chat': model_id}}
        assert client.put('/api/settings', json=requests['source']).status_code == 200
        assert app.state.settings.get()['routing']['chat'] == model_id

        for request in requests['calls']:
            response = client.request(request['method'], request['url'], json=request['body'])
            assert response.status_code == 200, response.text
        after = app.state.settings.get()
        assert after['routing']['chat'] == ''
        assert after['routing']['memory'] == after['routing']['planner'] == 'keep'
        assert after['model'] == before['model']
        assert after['providers'] == before['providers']
        assert after['provider_sources'] == before['provider_sources']
        assert not after['computer']['enabled']
        assert not after['webui']['remote_enabled']
