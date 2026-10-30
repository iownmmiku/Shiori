import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
import zipfile

import pytest
from fastapi.testclient import TestClient
from auth_helpers import password_login, FIXTURE_PASSWORD
from shiori import update_runtime as worker
from shiori.api import create_app

def archive(path, entries):
    with zipfile.ZipFile(path,'w',zipfile.ZIP_DEFLATED) as output:
        for name,value in entries.items():
            info=zipfile.ZipInfo('placeholder');info.filename='shiori/'+name
            output.writestr(info,value)

@pytest.mark.parametrize('name',['../escape','data/private.txt','.venv/python','C:/escape','folder/../../escape','CON','a:stream'])
def test_update_archive_rejects_unsafe_destinations(tmp_path,name):
    package=tmp_path/'app.zip';stage=tmp_path/'stage';stage.mkdir()
    archive(package,{'shiori.exe':b'MZfixture',name:b'unsafe'})
    with pytest.raises(ValueError):worker.stage_archive(package,stage,'portable','0.1.4')
    assert not (tmp_path/'escape').exists()

def test_program_replacement_preserves_unrelated_files_and_can_rollback(tmp_path):
    target,stage,data=tmp_path/'program',tmp_path/'stage',tmp_path/'program/personal'
    for folder in (target,stage,data):folder.mkdir(parents=True,exist_ok=True)
    (target/'app.txt').write_text('old');(target/'notes.txt').write_text('mine');(data/'secret.txt').write_text('private')
    (stage/'app.txt').write_text('new');(stage/'added.txt').write_text('added')
    files={name:worker.digest(stage/name) for name in ('app.txt','added.txt')}
    backup=tmp_path/'backup'
    journal=worker.replace_program(stage,target,files,backup,data)
    assert (target/'app.txt').read_text()=='new' and (target/'notes.txt').read_text()=='mine'
    assert (data/'secret.txt').read_text()=='private'
    worker.rollback_program(target,backup,journal,data)
    assert (target/'app.txt').read_text()=='old' and not (target/'added.txt').exists()

def test_replacement_failure_rolls_back_completed_operations(tmp_path,monkeypatch):
    target,stage=tmp_path/'program',tmp_path/'stage';target.mkdir();stage.mkdir()
    for name in ('a','b'):(target/name).write_text('old');(stage/name).write_text('new')
    original=worker.os.replace
    def fail(source,dest):
        if Path(source)==stage/'b':raise OSError('fixture write failure')
        return original(source,dest)
    monkeypatch.setattr(worker.os,'replace',fail)
    with pytest.raises(OSError):worker.replace_program(stage,target,{'a':'sha','b':'sha'},tmp_path/'backup',tmp_path/'data')
    assert [(target/name).read_text() for name in ('a','b')]==['old','old']

def test_data_backup_excludes_worker_and_download_recursion(tmp_path):
    data=tmp_path/'data';(data/'updates/work').mkdir(parents=True);(data/'security').mkdir()
    (data/'security/password.json').write_text('fixture-secret');(data/'updates/work/payload.zip').write_bytes(b'not copied')
    destination=data/'updates/work/data-backup'
    worker.backup_data(data,destination)
    assert (destination/'security/password.json').read_text()=='fixture-secret'
    assert not (destination/'updates').exists()

@pytest.mark.parametrize('healthy',[True,False])
def test_worker_switches_files_and_recovers_program_and_data_on_failed_health(tmp_path,monkeypatch,healthy):
    target,data=tmp_path/'program',tmp_path/'data';target.mkdir();data.mkdir()
    (target/'shiori.exe').write_bytes(b'old-program');(target/'personal.txt').write_text('keep')
    (data/'admin.token').write_text('isolated-token');(data/'qunyu.db').write_bytes(b'old-database')
    work=data/'updates'/('1'*32);work.mkdir(parents=True)
    package=work/'update.zip';archive(package,{'shiori.exe':b'new-program'})
    cfg={'target':str(target),'data':str(data),'archive':str(package),'sha256':worker.digest(package),
         'download_id':'1'*32,'kind':'portable','version':'0.1.4','parent_pid':42,'port':18700,'launch':['fixture-program']}
    path=work/'switch.json';path.write_text(json.dumps(cfg))
    monkeypatch.setattr(worker,'wait_parent',lambda *a,**k:None)
    launches=[]
    def launch(*args,**kwargs):
        launches.append(args)
        if len(launches)==1:(data/'qunyu.db').write_bytes(b'migrated-database')
        return SimpleNamespace(poll=lambda:0)
    monkeypatch.setattr(worker.subprocess,'Popen',launch)
    monkeypatch.setattr(worker,'health',lambda *a,**k:healthy)
    worker.run(path)
    result=json.loads((data/'updates/install-result.json').read_text('utf-8'))
    assert result['status']==('completed' if healthy else 'failed')
    assert (target/'shiori.exe').read_bytes()==(b'new-program' if healthy else b'old-program')
    assert (target/'personal.txt').read_text()=='keep'
    assert (data/'qunyu.db').read_bytes()==(b'migrated-database' if healthy else b'old-database')
    assert len(launches)==(1 if healthy else 2)
    assert (work/'data-backup/qunyu.db').read_bytes()==b'old-database'

def test_remote_update_policy_cannot_be_enabled_by_remote_api(tmp_path):
    app=create_app(tmp_path,start_workers=False,allow_remote=True)
    with TestClient(app,base_url='http://127.0.0.1:8765',client=('127.0.0.1',1234)) as local:
        password_login(local,app)
        app.state.settings.update({'webui':{'remote_enabled':True}})
        remote=TestClient(app,base_url='https://192.168.1.2:8765',client=('192.168.1.10',1234))
        assert remote.post('/api/auth/login',json={'password':FIXTURE_PASSWORD}).status_code==200
        assert remote.post('/api/updates/remote-policy',json={'enabled':True}).status_code==403
        assert remote.put('/api/settings',json={'updates':{'allow_remote':True}}).status_code==403
        assert local.post('/api/updates/remote-policy',json={'enabled':True}).status_code==200
        assert remote.get('/api/updates').json()['remote_switch_enabled'] is True


@pytest.mark.parametrize('status,body,accepted,read_error', [
    (200, b'{"ok":true}', True, False),
    (200, b'{"ok":false}', False, False),
    (200, b'not JSON', False, False),
    (403, b'{"detail":"fixture denied"}', False, False),
    (200, b'{"ok":true}', False, True),
])
def test_failed_health_drains_shutdown_reply_before_wait_or_rollback(tmp_path, monkeypatch, status, body, accepted, read_error):
    import http.client
    import io
    import urllib.error

    target, data = tmp_path / 'program', tmp_path / 'data'
    target.mkdir(); data.mkdir()
    (target / 'shiori.exe').write_bytes(b'old-program')
    (data / 'admin.token').write_text('fixture-token')
    (data / 'qunyu.db').write_bytes(b'old-data')
    work = data / 'updates' / ('d' * 32)
    work.mkdir(parents=True)
    package = work / 'update.zip'
    archive(package, {'shiori.exe': b'new-program'})
    config = {'target': str(target), 'data': str(data), 'archive': str(package),
        'sha256': worker.digest(package), 'download_id': 'd' * 32, 'kind': 'portable',
        'version': '0.1.4', 'parent_pid': 42, 'port': 18700, 'launch': ['fixture-never-launched']}
    path = work / 'switch.json'
    path.write_text(json.dumps(config))

    class Response(io.BytesIO):
        status = 200
        consumed = False
        def read(self, size=-1):
            if read_error:
                raise http.client.IncompleteRead(super().read(4), len(body) - 4)
            return super().read(size)
        def close(self):
            if not self.closed:
                self.consumed = self.tell() == len(body)
            super().close()

    response = Response(body)
    requests, waits, launches = [], [], []
    def open_response(request, *, timeout):
        requests.append(request)
        assert request.full_url == 'http://127.0.0.1:18700/api/shutdown'
        assert request.get_header('Authorization') == 'Bearer fixture-token'
        assert timeout == 3
        if status != 200:
            raise urllib.error.HTTPError(request.full_url, status, 'fixture denied', {}, response)
        return response

    def wait(*, timeout):
        assert response.closed and response.consumed, 'read the complete shutdown response before waiting for exit'
        waits.append(timeout)

    def launch(*args, **kwargs):
        launches.append(args)
        if len(launches) == 1:
            (data / 'qunyu.db').write_bytes(b'migrated-data')
        return SimpleNamespace(poll=lambda: None, wait=wait)

    monkeypatch.setattr(worker.urllib.request, 'build_opener', lambda *_: SimpleNamespace(open=open_response))
    monkeypatch.setattr(worker, 'wait_parent', lambda *args, **kwargs: None)
    monkeypatch.setattr(worker.subprocess, 'Popen', launch)
    monkeypatch.setattr(worker, 'health', lambda *args, **kwargs: False)
    worker.run(path)
    assert len(requests) == 1 and response.closed
    assert response.consumed is (not read_error)
    result = json.loads((data / 'updates' / 'install-result.json').read_text('utf-8'))
    if accepted:
        assert waits == [30] and len(launches) == 2
        assert result['status'] == 'failed'
        assert (target / 'shiori.exe').read_bytes() == b'old-program'
        assert (data / 'qunyu.db').read_bytes() == b'old-data'
    else:
        assert not waits and len(launches) == 1
        assert result['status'] == 'unknown'
        assert (target / 'shiori.exe').read_bytes() == b'new-program'
        assert (data / 'qunyu.db').read_bytes() == b'migrated-data'


def test_update_health_gate_blocks_mutations_and_chat_but_keeps_status(tmp_path):
    app=create_app(tmp_path,start_workers=False)
    with TestClient(app) as client:
        password_login(client,app)
        app.state.provider._updating=True
        assert client.post('/api/chat',json={'scope_id':'web:default','user_id':'owner','text':'must wait'}).status_code==503
        assert client.put('/api/settings',json={'paused':True}).status_code==503
        assert client.get('/api/updates').status_code==200
        assert client.get('/healthz').status_code==200
        assert client.post('/api/shutdown').status_code==200
        app.state.provider._updating=False

def test_real_source_program_replacement_restarts_on_same_port_and_preserves_memory(tmp_path):
    """Real launcher, socket, SQLite, detached restart and authenticated shutdown."""
    import hashlib,shutil,socket,subprocess,sys,time,urllib.request
    from shiori import __version__
    target,data=tmp_path/'program',tmp_path/'data';target.mkdir();data.mkdir()
    source=Path(__file__).resolve().parents[1]
    shutil.copytree(source/'shiori',target/'shiori',ignore=shutil.ignore_patterns('__pycache__','*.pyc'))
    (target/'requirements-server.txt').write_text('# unchanged fixture requirements\n')
    with socket.socket() as sock:sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
    launch=[sys.executable,'-m','shiori','--no-browser','--data-dir',str(data),'--port',str(port),'--strict-port']
    work=data/'updates'/('c'*32);work.mkdir(parents=True)
    package=work/'update.zip'
    entries={p.relative_to(target).as_posix():p.read_bytes() for p in target.rglob('*') if p.is_file()}
    entries['new-version-marker.txt']=b'updated'
    manifest={'version':__version__,'files':{name:hashlib.sha256(raw).hexdigest() for name,raw in entries.items()}}
    entries['SOURCE_MANIFEST.json']=json.dumps(manifest).encode()
    with zipfile.ZipFile(package,'w',zipfile.ZIP_DEFLATED) as out:
        for name,raw in entries.items():out.writestr(f'shiori-{__version__}-source/'+name,raw)
    first=subprocess.Popen(launch,cwd=target,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
    opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
    def shutdown():
        token=(data/'admin.token').read_text().strip()
        request=urllib.request.Request(f'http://127.0.0.1:{port}/api/shutdown',data=b'',headers={'Authorization':'Bearer '+token})
        with opener.open(request,timeout=3) as response:
            assert json.load(response)['ok'] is True
    try:
        deadline=time.monotonic()+30
        while not (data/'admin.token').exists() and time.monotonic()<deadline:time.sleep(.1)
        assert worker.health(data,port,__version__,seconds=30)
        from shiori.db import Store
        store=Store(data);saved=store.create_memory({'content':'升级前已经存在的记忆'});store.close()
        shutdown()
        try:
            first.wait(timeout=30)
        except subprocess.TimeoutExpired:
            log = data / 'runtime.log'
            tail = log.read_text('utf-8', errors='replace')[-8000:] if log.exists() else '(no runtime.log)'
            pytest.fail(f'Initial instance did not exit within 30s after confirmed shutdown. Runtime log:\n{tail}')
        config={'target':str(target),'data':str(data),'archive':str(package),'sha256':worker.digest(package),'download_id':'c'*32,
                'kind':'source','version':__version__,'parent_pid':first.pid,'port':port,'launch':launch}
        path=work/'switch.json';path.write_text(json.dumps(config))
        assert worker.run(path) is True
        assert (target/'new-version-marker.txt').read_text()=='updated'
        store=Store(data);assert store.get_memory(saved['id'])['content']=='升级前已经存在的记忆';store.close()
        assert json.loads((data/'server.json').read_text())['port']==port
    finally:
        try:shutdown()
        except OSError:pass
        if first.poll() is None:first.terminate();first.wait(timeout=15)
