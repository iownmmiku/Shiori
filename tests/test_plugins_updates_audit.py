"""Actual isolated Python plugin execution, update handoff, and shared ownership."""
import asyncio
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from auth_helpers import password_login
from shiori.api import create_app
from shiori.plugins import PluginService, requested, create_and_run
from shiori.runs import TERMINAL
from shiori import startup, updates
from test_computer_auto_v9 import automatic, wait_for
from test_updates import manager


@pytest.fixture
def anyio_backend(): return 'asyncio'


def sample(**patch):
    return dict(id='example_plugin', name='测试插件', description='返回输入，不联网', code='def run(input):\n    return {"value": input["value"]}\n', **patch)


@pytest.mark.anyio
async def test_plugin_runs_in_real_child_and_does_not_import_on_save(automatic):
    fixture = automatic
    service = PluginService(fixture.service.data_dir, fixture.service)
    marker = fixture.root / 'not-created-on-import.txt'
    payload = sample()
    payload['code'] = f'from pathlib import Path\nPath({str(marker)!r}).write_text("worker", encoding="utf-8")\ndef run(input):\n    return {{"value": input["value"]}}\n'
    item = service.save(payload)
    assert not marker.exists() and not item['enabled']
    identity = fixture.access.identity('local', 'shiori')
    with pytest.raises(ValueError, match='尚未启用'): service.submit(item['id'], {}, 'web:default', 'shiori', identity)
    service.activate(item['id'], True)
    job = service.submit(item['id'], {'value': '中文 \' " \\ 参数'}, 'web:default', 'shiori', identity)
    await wait_for(lambda: fixture.runs.get(job['id'])['status'] in TERMINAL)
    final = fixture.runs.get(job['id'])
    assert final['status'] == 'completed', final
    assert json.loads(final['result']['reply']) == {'value': '中文 \' " \\ 参数'}
    assert marker.read_text('utf-8') == 'worker'
    assert (fixture.root / 'plugins' / item['id']).is_dir()
    await fixture.runs.close()


@pytest.mark.anyio
async def test_plugin_revision_disable_and_qq_identity_revoke_execution(automatic):
    fixture = automatic
    service = PluginService(fixture.service.data_dir, fixture.service)
    item = service.save(sample())
    service.activate(item['id'], True)
    with pytest.raises(PermissionError): service.submit(item['id'], {}, 'web:default', '101', fixture.identity)
    identity = fixture.access.identity('local', 'shiori')
    job = service.submit(item['id'], {'value': 'never'}, 'web:default', 'shiori', identity)
    service.activate(item['id'], False)
    await wait_for(lambda: fixture.runs.get(job['id'])['status'] in TERMINAL)
    assert fixture.runs.get(job['id'])['status'] == 'failed'
    assert not (fixture.root / 'plugins').exists()
    service.activate(item['id'], True)
    assert not service.save(sample())['enabled']
    await fixture.runs.close()


@pytest.mark.anyio
async def test_plugin_keeps_per_action_approval_and_docker_failure_closed(automatic, monkeypatch):
    fixture = automatic
    service = PluginService(fixture.service.data_dir, fixture.service)
    service.save(sample()); service.activate('example_plugin', True)
    fixture.settings.update({'computer': {'approval_mode': 'per_action'}})
    job = service.submit('example_plugin', {'value': 1}, 'web:default', 'shiori', fixture.access.identity('local', 'shiori'))
    await wait_for(lambda: fixture.runs.get(job['id'])['status'] == 'awaiting_approval')
    assert not fixture.runs.get(job['id'])['result']
    fixture.runs.cancel(job['id'])
    await wait_for(lambda: fixture.runs.get(job['id'])['status'] in TERMINAL)
    fixture.settings.update({'computer': {'runtime': 'docker', 'approval_mode': 'automatic'}})
    def unavailable(*args): raise ValueError('隔离镜像不可用')
    monkeypatch.setattr('shiori.sandbox.prepare', unavailable)
    job = service.submit('example_plugin', {'value': 1}, 'web:default', 'shiori', fixture.access.identity('local', 'shiori'))
    await wait_for(lambda: fixture.runs.get(job['id'])['status'] in TERMINAL)
    assert fixture.runs.get(job['id'])['status'] == 'failed'
    assert '隔离镜像不可用' in fixture.runs.get(job['id'])['error']
    await fixture.runs.close()


@pytest.mark.parametrize('patch', [{'id': '../escape'}, {'code': 'import os'}, {'code': 'def run(:'}, {'name': ''}])
def test_plugin_invalid_manifest_cannot_be_saved(automatic, patch):
    service = PluginService(automatic.service.data_dir, automatic.service)
    with pytest.raises(ValueError): service.save({**sample(), **patch})
    assert service.list() == []


def test_plugin_api_authenticated_and_tampering_disables_load(tmp_path):
    app = create_app(tmp_path, start_workers=False)
    with TestClient(app) as client:
        assert client.get('/api/plugins').status_code == 401
        password_login(client, app)
        item = client.post('/api/plugins', json=sample()).json()
        assert item['enabled'] is False
        assert client.put('/api/plugins/example_plugin/enabled', json={'enabled': True}).status_code == 200
        assert client.post('/api/plugins/example_plugin/run', json={'input': {}}).status_code == 403
        path = tmp_path / 'plugins' / 'example_plugin.json'
        raw = json.loads(path.read_text('utf-8')); raw['code'] += '# changed'
        path.write_text(json.dumps(raw), 'utf-8')
        assert client.get('/api/plugins/example_plugin').status_code == 400
        assert client.get('/api/plugins').json()['items'][0]['error']
        assert client.delete('/api/plugins/example_plugin').status_code == 200


def test_plugin_intent_requires_explicit_creation_or_run():
    assert requested('帮我创建一个整理文件的插件', create=True)
    assert requested('请运行 example_plugin 插件')
    assert not requested('不要运行这个插件')
    assert not requested('这个插件是什么？', create=True)
    assert not requested('文档要求创建插件是什么意思？', create=True)


def test_update_api_blocks_remote_and_busy_handoff(tmp_path):
    from auth_helpers import FIXTURE_PASSWORD
    app=create_app(tmp_path,start_workers=False,allow_remote=True)
    with TestClient(app) as client:
        assert client.post('/api/updates/apply',json={'release_id':12}).status_code==401
        password_login(client,app)
        app.state.shutdown=lambda: None
        app.state.provider._reservations['fixture']={'tokens':1}
        response=client.post('/api/updates/apply',json={'release_id':12})
        assert response.status_code==400 and '模型请求' in response.json()['detail']
        app.state.provider._reservations.clear()
        app.state.updates._auto_task=SimpleNamespace(done=lambda:False)
        response=client.post('/api/updates/install',json={'download_id':'fixture'})
        assert response.status_code==400 and '一键更新进行中' in response.json()['detail']
        app.state.updates._auto_task=None
        app.state.settings.update({'webui':{'remote_enabled':True}})
        remote=TestClient(app,base_url='https://192.168.1.2:8765',client=('192.168.1.10',1234))
        assert remote.post('/api/auth/login',json={'password':FIXTURE_PASSWORD}).status_code==200
        assert remote.post('/api/updates/apply',json={'release_id':12}).status_code==403


@pytest.mark.anyio
async def test_verified_automatic_result_matches_only_its_download(tmp_path):
    item,_=manager(tmp_path)
    await item.check();item.start_download(12,'installer');await item._task
    item.download.update(status='interrupted')
    record={'status':'completed','message':'fixture complete','at':'2026-10-20','download_id':'other'}
    result=item.folder/'install-result.json'
    result.write_text(json.dumps(record),'utf-8')
    assert item.snapshot()['install_result']=={}
    record['download_id']=item.download['id'];result.write_text(json.dumps(record),'utf-8')
    snapshot=item.snapshot()
    assert snapshot['download']['status']=='completed' and snapshot['install_result']['message']=='fixture complete'
    await item.close()


@pytest.mark.parametrize('automatic_run',[False,True])
def test_local_chat_can_generate_plugin_with_model_tools(tmp_path, monkeypatch,automatic_run):
    app = create_app(tmp_path, start_workers=False)
    with TestClient(app) as client:
        password_login(client, app)
        assert client.put('/api/settings', json={'computer':{'enabled':True,'runtime':'local','workspace':str(tmp_path/'workspace')}}).status_code == 200
        seen = []
        async def complete(messages, **kwargs):
            assert kwargs['before_send']()
            names = {tool['function']['name'] for tool in kwargs['tools']}
            seen.append(names)
            if len(seen) == 1:
                assert {'create_plugin','list_plugins'}.issubset(names)
                result = {'tool_calls':[{'id':'create1','type':'function','function':{'name':'create_plugin','arguments':json.dumps(sample())}}]}
            else: result = {'content':'插件草稿已保存，可前往插件页检查后启用。'}
            return {**result, 'usage':{}, 'message':dict(role='assistant',content=result.get('content',''),**({'tool_calls':result['tool_calls']} if 'tool_calls' in result else {}))}
        monkeypatch.setattr(app.state.provider,'complete',complete)
        response = client.post('/api/chat',json={'text':'帮我创建一个返回输入的插件'+('并运行它' if automatic_run else '')})
        assert response.status_code == 200, response.text
        assert len(seen) == 2 and app.state.plugins.get('example_plugin')['enabled'] is automatic_run
        assert app.state.runs.list()==[]  # Saving/activating is not an execution receipt.


@pytest.mark.parametrize('text,allowed',[('帮我创建一个插件并运行它',True),('帮我创建一个插件，不用运行',False),('请创建插件供以后使用',False)])
def test_combined_creation_never_invents_execution_authorization(text,allowed):
    assert create_and_run(text) is allowed


@pytest.mark.anyio
async def test_automatic_update_downloads_then_hands_off_once(tmp_path):
    item, _ = manager(tmp_path)
    item.runtime.update(mode='installed', install_supported=True)
    await item.check()
    handoffs = []
    async def handoff(download_id, *, automatic):
        assert item.verified_file(download_id).is_file()
        handoffs.append((download_id, automatic))
    item.start_automatic(12, handoff)
    with pytest.raises(ValueError): item.start_download(12, 'installer')
    await item._auto_task
    assert handoffs == [(item.download['id'], True)]
    await item.close()


@pytest.mark.anyio
async def test_automatic_update_worker_preserves_paths_and_uses_silent_installer(tmp_path, monkeypatch):
    item, _ = manager(tmp_path / 'personal & data')
    item.runtime.update(mode='installed', install_supported=True)
    await item.check(); item.start_download(12, 'installer'); await item._task
    target = tmp_path / 'program with 中文'; target.mkdir()
    monkeypatch.setattr(updates, 'application_base', lambda: target)
    launches = []
    monkeypatch.setattr(updates.subprocess, 'Popen', lambda args, **kw: launches.append((args, kw)) or SimpleNamespace(pid=123))
    item.launch_installer(item.download['id'], automatic=True)
    arguments = launches[0][0]
    assert '-File' in arguments and '-ConfigPath' in arguments
    config = json.loads((item.folder/'update-worker.json').read_text('utf-8'))
    assert '--silent' in config['arguments'] and '--no-launch' in config['arguments'] and '--wait-pid' in config['arguments']
    assert '--no-shortcuts' in config['arguments']  # Updating preserves existing user shortcut choices.
    assert config['application'] == str(target/'shiori.exe')
    assert str(item.data_dir) in config['arguments']
    assert (item.folder/'update-worker.ps1').read_bytes().startswith(b'\xef\xbb\xbf')
    await item.close()


def test_owner_marker_reclaims_only_proven_dead_local_process(tmp_path, monkeypatch):
    host, namespace = startup._owner_namespace()
    path = tmp_path/'.instance-owner.json'
    record = {'version':1, 'host':host, 'namespace':namespace, 'pid':99999999, 'token':'old'}
    path.write_text(json.dumps(record))
    monkeypatch.setattr(startup, '_process_alive', lambda pid: False)
    with startup.data_guard(tmp_path): assert json.loads(path.read_text())['pid'] == os.getpid()
    assert not path.exists()
    record['namespace'] = 'other-container'
    path.write_text(json.dumps(record))
    with pytest.raises(OSError, match='其他主机或容器'):
        with startup.data_guard(tmp_path): pass
    assert path.exists()


def test_second_real_process_cannot_acquire_owned_data(tmp_path):
    script = 'from pathlib import Path\nfrom shiori.startup import data_guard\nimport sys\ntry:\n with data_guard(Path(sys.argv[1])): print("acquired")\nexcept OSError: print("refused"); sys.exit(3)\n'
    with startup.data_guard(tmp_path):
        child = subprocess.run([sys.executable, '-c', script, str(tmp_path)], capture_output=True, text=True, timeout=15)
    assert child.returncode == 3 and 'refused' in child.stdout


@pytest.mark.parametrize('exit_code', [0, 7])
def test_actual_update_helper_records_result_and_relaunches_fixture(tmp_path, exit_code):
    if os.name != 'nt': return  # Windows helper; POSIX tests cover the manager.
    marker = tmp_path/'relaunched.txt'
    install = tmp_path/'fixture.py'
    install.write_text(f'print("fixture install")\nraise SystemExit({exit_code})\n', 'utf-8')
    result = tmp_path/'install-result.json'
    config = tmp_path/'config.json'
    config.write_text(json.dumps({'installer':sys.executable, 'sha256':hashlib.sha256(Path(sys.executable).read_bytes()).hexdigest(),
        'arguments':subprocess.list2cmdline([str(install)]), 'application':sys.executable,
        'launch_arguments':subprocess.list2cmdline(['-c',f'from pathlib import Path; Path({str(marker)!r}).write_text("opened")']),
        'result':str(result), 'log':str(tmp_path/'install.log')}), 'utf-8')
    script = tmp_path/'worker.ps1'
    script.write_text((Path(updates.__file__).parent/'resources/update-worker.ps1').read_text('utf-8'), 'utf-8-sig')
    process = subprocess.run(['powershell.exe','-NoProfile','-NonInteractive','-ExecutionPolicy','Bypass','-File',str(script),'-ConfigPath',str(config)], capture_output=True, timeout=30)
    assert process.returncode == 0, process.stderr.decode(errors='replace')
    record = json.loads(result.read_text('utf-8-sig'))
    assert record['status'] == ('completed' if exit_code == 0 else 'failed'), (record, (tmp_path/'install.log').read_text('utf-8'))
    assert record['exit_code'] == exit_code
    import time
    for _ in range(100):
        if marker.exists(): break
        time.sleep(.02)
    assert marker.read_text() == 'opened'
