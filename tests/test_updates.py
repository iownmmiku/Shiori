"""Release selection, executable trust boundaries, and local installer handoff."""
import asyncio
import copy
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient

from auth_helpers import FIXTURE_PASSWORD, password_login
from shiori import __version__
from shiori import updates, installer
from shiori.api import create_app

CONTENT = b'MZ-local-fixture-installer-not-executable'
STABLE_TAG = 'v' + __version__.partition('-')[0]


@pytest.fixture
def anyio_backend(): return 'asyncio'


def release(tag=STABLE_TAG, *, digest=True, kind='installer', body=CONTENT, identifier=12):
    name=f'shiori-{tag[1:]}-{updates.KINDS[kind]}'
    item={'id':identifier*10,'name':name,'state':'uploaded','size':len(body),
          'browser_download_url':updates._asset_url(tag,name)}
    sha=hashlib.sha256(body).hexdigest()
    if digest:item['digest']='sha256:'+sha
    assets=[item]
    if not digest:
        checksum=Path(name).with_suffix('.sha256').name
        assets.append({'id':identifier*10+1,'name':checksum,'state':'uploaded','size':len(name)+67,
                       'browser_download_url':updates._asset_url(tag,checksum)})
    return {'id':identifier,'tag_name':tag,'name':'fixture <script>bad</script>','body':'fixture notes',
            'draft':False,'prerelease':'-' in tag,'published_at':'2026-10-19T00:00:00Z','assets':assets}


def manager(tmp_path, items=None, *, payload=CONTENT, change=None, download_status=200, redirect=None):
    items=items or [release()];calls=[]
    def handle(request):
        calls.append(str(request.url))
        if request.url.host=='api.github.com':
            if request.url.path==f'/repos/{updates.REPOSITORY}/releases':return httpx.Response(200,json=items)
            value=copy.deepcopy(next(x for x in items if request.url.path.endswith('/'+str(x['id']))))
            if change:change(value)
            return httpx.Response(200,json=value)
        if request.url.path.endswith('.sha256'):
            item=items[0]['assets'][0]
            return httpx.Response(200,content=(hashlib.sha256(CONTENT).hexdigest()+'  '+item['name']+'\n').encode())
        if redirect and request.url.host=='github.com':return httpx.Response(302,headers={'location':redirect})
        return httpx.Response(download_status,content=payload,headers={'content-length':str(len(payload))})
    result=updates.UpdateManager(tmp_path,client_factory=lambda:httpx.AsyncClient(transport=httpx.MockTransport(handle)),
        runtime={'current_version':'0.1.1','repository':updates.REPOSITORY,'releases_url':updates.RELEASES_URL,
                 'mode':'source','default_kind':'source','install_supported':False})
    return result,calls


def test_semantic_order_and_untrusted_assets():
    assert updates.version_key('v0.1.10')>updates.version_key('0.1.2')
    assert updates.version_key('0.1.2')>updates.version_key('0.1.2-rc.10')>updates.version_key('0.1.2-rc.2')
    assert updates.version_key('0.2.0') < updates.version_key('0.2.1-beta') < updates.version_key('0.2.1')
    bad=release();bad['assets'][0]['browser_download_url']='https://evil.example/installer.exe'
    assert not updates.normalize_release(bad)['assets']
    bad['draft']=True;assert updates.normalize_release(bad) is None
    bad['draft']=False;bad['tag_name']='../../escape';assert updates.normalize_release(bad) is None


@pytest.mark.anyio
async def test_only_explicit_checks_contact_github_and_filter_previews(tmp_path):
    major, minor, patch = updates.version_key(__version__)[:3]
    preview = f'v{major}.{minor}.{patch + 1}-rc.1'
    item,calls=manager(tmp_path,[release(),release(preview,identifier=13),release('v0.1.0',identifier=10)])
    assert calls==[]
    result=await item.check()
    assert [x['tag'] for x in result['releases']]==[STABLE_TAG,'v0.1.0']
    assert result['releases'][-1]['relation']=='older'
    await item.check();assert len(calls)==1
    result=await item.check(include_prerelease=True)
    assert result['releases'][0]['tag']==preview and len(calls)==2
    for value in [0,True,'1',101]:
        with pytest.raises(ValueError):await item.check(page=value)


@pytest.mark.anyio
async def test_beta_release_requires_opt_in_and_keeps_exact_download_identity(tmp_path, monkeypatch):
    monkeypatch.setattr(updates, '__version__', '0.2.1-beta')
    item, calls = manager(tmp_path, [release('v0.2.1-beta'), release('v0.2.1', identifier=13)])
    result = await item.check()
    assert [value['tag'] for value in result['releases']] == ['v0.2.1']
    assert result['releases'][0]['relation'] == 'newer'
    result = await item.check(include_prerelease=True)
    beta = next(value for value in result['releases'] if value['prerelease'])
    assert beta['tag'] == 'v0.2.1-beta' and beta['relation'] == 'current'
    item.start_download(beta['id'], 'installer')
    await item._task
    assert item.download['status'] == 'ready'
    assert item.download['tag'] == 'v0.2.1-beta'
    assert item.verified_file(item.download['id']).name == 'shiori-0.2.1-beta-Windows-Setup.exe'
    assert updates._asset_url(beta['tag'], item.download['name']) in calls


@pytest.mark.anyio
@pytest.mark.parametrize('digest',[True,False])
async def test_verified_download_and_restart_never_launch_automatically(tmp_path,digest):
    item,calls=manager(tmp_path,[release(digest=digest)])
    await item.check()
    first=item.start_download(12,'installer');second=item.start_download(12,'installer')
    assert first['download']['id']==second['download']['id']
    await item._task
    result=item.snapshot()['download'];assert result['status']=='ready'
    assert item.verified_file(result['id']).read_bytes()==CONTENT
    assert not result['can_install']
    restored=updates.UpdateManager(tmp_path)
    assert restored.verified_file(result['id']).read_bytes()==CONTENT
    assert item.download['sha256'] not in json.dumps(result)
    item.verified_file(result['id']).write_bytes(b'tampered')
    with pytest.raises(ValueError,match='发生变化'):restored.verified_file(result['id'])


@pytest.mark.anyio
@pytest.mark.parametrize('mode',['digest','size','changed','untrusted_redirect','status'])
async def test_failed_or_changed_download_never_becomes_ready(tmp_path,mode):
    payload=CONTENT
    if mode=='digest':payload=b'MZ'+b'x'*(len(CONTENT)-2)
    if mode=='size':payload=CONTENT+b'extra'
    change=(lambda value:value['assets'][0].update(id=999)) if mode=='changed' else None
    item,calls=manager(tmp_path,payload=payload,change=change,download_status=404 if mode=='status' else 200,
                       redirect='http://127.0.0.1/evil.exe' if mode=='untrusted_redirect' else None)
    await item.check();item.start_download(12,'installer');await item._task
    assert item.download['status']=='failed'
    assert not list(item.folder.rglob('*.exe')) and not list(item.folder.rglob('*.part'))
    assert all('127.0.0.1' not in url for url in calls)


@pytest.mark.anyio
async def test_cancel_before_worker_starts_is_persisted(tmp_path):
    item,calls=manager(tmp_path)
    await item.check();item.start_download(12,'installer');await item.cancel()
    assert item.download['status']=='cancelled'
    assert updates.UpdateManager(tmp_path).download['status']=='cancelled'
    assert len(calls)==1


@pytest.mark.anyio
async def test_download_follows_only_github_cdn_and_retains_exact_version(tmp_path):
    item,calls=manager(tmp_path,redirect='https://release-assets.githubusercontent.com/fixed-fixture')
    await item.check();item.start_download(12,'installer');await item._task
    assert item.download['status']=='ready'
    assert any('release-assets.githubusercontent.com' in url for url in calls)
    with pytest.raises(ValueError):item.start_download(999,'installer')
    with pytest.raises(ValueError):item.start_download(12,[])


@pytest.mark.anyio
async def test_installer_handoff_preserves_data_and_blocks_downgrade(tmp_path,monkeypatch):
    item,calls=manager(tmp_path/'personal-data')
    await item.check();item.start_download(12,'installer');await item._task
    target=tmp_path/'program';target.mkdir()
    item.runtime.update(mode='installed',install_supported=True)
    monkeypatch.setattr(updates,'application_base',lambda:target)
    launched=[]
    def launch(args,**kwargs):launched.append((args,kwargs));return SimpleNamespace(pid=888)
    monkeypatch.setattr(updates.subprocess,'Popen',launch)
    original=copy.deepcopy(item.download)
    item.download['tag']='v0.1.0'
    with pytest.raises(ValueError,match='旧版本'):item.launch_installer(original['id'])
    item.download=original
    assert item.launch_installer(original['id'])['installer_pid']==888
    arguments=launched[0][0]
    assert arguments[1:]==['--target',str(target.resolve()),'--wait-pid',str(updates.os.getpid()),'--data-dir',str(item.data_dir)]
    assert all(launched[0][1][name]==updates.subprocess.DEVNULL for name in ('stdin','stdout','stderr'))
    assert item.download['status']=='installing'
    with pytest.raises(ValueError):item.launch_installer(original['id'])
    assert len(launched)==1


def test_authenticated_update_api_and_remote_install_rejection(tmp_path,monkeypatch):
    app=create_app(tmp_path/'app',start_workers=False,allow_remote=True)
    with TestClient(app) as client:
        assert client.get('/api/updates').status_code==401
        assert client.post('/api/updates/check',json={}).status_code==401
        password_login(client,app)
        assert client.get('/api/updates').json()['current_version']==__version__
        assert client.post('/api/updates/download',json={'release_id':12,'kind':[]}).status_code==400
        app.state.shutdown=lambda:None
        app.state.settings.update({'webui':{'remote_enabled':True}})
        remote=TestClient(app,base_url='https://192.168.1.2:8765',client=('192.168.1.10',1234))
        assert remote.post('/api/auth/login',json={'password':FIXTURE_PASSWORD}).status_code==200
        response=remote.post('/api/updates/install',json={'download_id':'unused'})
        assert response.status_code==403 and '运行' in response.text
        assert client.post('/api/updates/install',json={'download_id':'unused'}).status_code==400


def test_custom_data_is_checked_and_forwarded_to_installed_app(tmp_path,monkeypatch):
    seen=[];personal=(tmp_path/'custom').resolve()
    monkeypatch.setattr(installer,'_installation_data_locations',lambda target:[])
    monkeypatch.setattr(installer,'_data_instance_running',lambda value:seen.append(value) or value==personal)
    assert installer.is_instance_running(tmp_path/'program',personal) and seen==[personal]
    launched=[]
    monkeypatch.setattr(installer.subprocess,'Popen',lambda args,**kw:launched.append(args))
    installer.launch_installed(tmp_path/'program',personal)
    assert launched[0][-2:]==['--data-dir',str(personal)]


def test_wait_for_parent_uses_handle_and_does_not_kill(monkeypatch):
    called=[]
    class Function:
        def __init__(self,name,result):self.name,self.result=name,result
        def __call__(self,*args):called.append((self.name,args));return self.result
    kernel=SimpleNamespace(OpenProcess=Function('open',44),WaitForSingleObject=Function('wait',0),CloseHandle=Function('close',1))
    monkeypatch.setattr(installer,'os',SimpleNamespace(name='nt',getpid=lambda:222))
    monkeypatch.setattr(installer.ctypes,'WinDLL',lambda *a,**kw:kernel,raising=False)
    installer.wait_for_parent(333)
    assert called==[('open',(0x100000,False,333)),('wait',(44,120000)),('close',(44,))]
    kernel.WaitForSingleObject.result=258
    with pytest.raises(installer.InstallError,match='尚未退出'):installer.wait_for_parent(333)
    assert called[-1]==('close',(44,))


def test_update_installer_waits_before_gui_and_forwards_custom_data(tmp_path,monkeypatch):
    called=[];target=tmp_path/'program';personal=tmp_path/'personal'
    monkeypatch.setattr(installer,'wait_for_parent',lambda pid:called.append(('wait',pid)))
    def bundle():called.append(('bundle',));return Path('fixture.zip'),{'version':'0.1.2'}
    monkeypatch.setattr(installer,'bundled_payload',bundle)
    monkeypatch.setattr(installer,'run_gui',lambda payload,manifest,folder,data:called.append(('gui',folder,data)))
    assert installer.main(['--wait-pid','333','--data-dir',str(personal),'--target',str(target)])==0
    assert called==[('wait',333),('bundle',),('gui',target,personal)]


def test_custom_data_is_included_in_final_install_lock(tmp_path,monkeypatch):
    if installer.os.name!='nt':pytest.skip('Windows named mutex path')
    guarded=[];released=[];personal=(tmp_path/'custom').resolve()
    monkeypatch.setattr(installer,'_installation_data_locations',lambda target:[tmp_path/'default'])
    def acquire(value):guarded.append(value);return lambda:released.append(value)
    monkeypatch.setattr(installer,'_acquire_data_guard',acquire)
    close=installer._acquire_application_guard(tmp_path/'program',personal)
    assert guarded==[tmp_path/'default',personal]
    close();assert released==list(reversed(guarded))


@pytest.mark.anyio
async def test_cancel_midstream_removes_partial_file(tmp_path):
    entered=asyncio.Event();block=asyncio.Event();data=release()
    class SlowStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield CONTENT[:3];entered.set();await block.wait();yield CONTENT[3:]
        async def aclose(self):pass
    def respond(request):
        if request.url.host=='api.github.com':
            return httpx.Response(200,json=[data] if not request.url.path.endswith('/12') else data)
        return httpx.Response(200,stream=SlowStream())
    item=updates.UpdateManager(tmp_path,client_factory=lambda:httpx.AsyncClient(transport=httpx.MockTransport(respond)))
    await item.check();item.start_download(12,'installer')
    await asyncio.wait_for(entered.wait(),2);await item.cancel()
    assert item.download['status']=='cancelled' and not list(item.folder.rglob('*.part'))
    assert not list(item.folder.rglob('*.exe'))
