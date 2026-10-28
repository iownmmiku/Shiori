import asyncio
import copy
import io
import json
from pathlib import Path
from types import SimpleNamespace
import zipfile

import pytest
from fastapi.testclient import TestClient

from shiori.access import AccessManager
from shiori.api import create_app
from shiori.distillation import DistillationService
from shiori.provider import _PriorityGate, ProviderError
from shiori.remote_guide import https_guide
from shiori.runs import RunManager
from shiori.settings import Settings
from shiori.web_auth import WebAuth
from auth_helpers import password_login, FIXTURE_PASSWORD


def test_one_minute_pairing_is_private_single_use_and_expires(tmp_path):
    settings=Settings(tmp_path)
    clock=[100.0]
    access=AccessManager(settings,clock=lambda:clock[0])
    item=access.create_pairing(automatic=True)
    assert item['expires_at']==160 and len(item['code'])==16
    identity=access.identity('onebot','123','456')
    assert not access.capture_pairing(item['code'],identity,private=False)
    assert not access.capture_pairing(item['code'],dict(identity,account_id='789'))
    assert access.capture_pairing(item['code'],identity)
    assert access.is_admin(identity)
    assert access.status(item['id'])['status']=='bound'
    assert not access.capture_pairing(item['code'],access.identity('onebot','789','456'))
    expired=access.create_pairing(automatic=True)
    clock[0]=161
    clock[0]+=60
    assert not access.capture_pairing(expired['code'],identity)


def test_https_required_and_forwarded_header_cannot_fake_encryption(tmp_path):
    app=create_app(tmp_path,start_workers=False,allow_remote=True)
    with TestClient(app,base_url='http://127.0.0.1',client=('127.0.0.1',1)) as local:
        password_login(local,app)
        app.state.settings.update({'webui':{'remote_enabled':True,'public_url':'https://bot.example.com','allowed_hosts':['bot.example.com']}})
        with TestClient(app,base_url='http://bot.example.com',client=('192.168.1.10',2)) as plain:
            assert plain.post('/api/auth/login',json={'password':FIXTURE_PASSWORD}).status_code==403
            assert plain.post('/api/auth/login',json={'password':FIXTURE_PASSWORD},headers={'X-Forwarded-Proto':'https'}).status_code==403
        with TestClient(app,base_url='https://bot.example.com',client=('192.168.1.10',2)) as encrypted:
            response=encrypted.post('/api/auth/login',json={'password':FIXTURE_PASSWORD})
            assert response.status_code==200
            assert 'Secure' in response.headers['set-cookie']
        app.state.settings.update({'webui':{'require_https':False}})
        with TestClient(app,base_url='http://192.168.1.1',client=('192.168.1.10',2)) as lan:
            assert lan.post('/api/auth/login',json={'password':FIXTURE_PASSWORD}).status_code==200


def test_host_password_reset_revokes_sessions_without_deleting_data(tmp_path):
    token='fixture-bootstrap-token-with-enough-characters'
    auth=WebAuth(tmp_path,token)
    session=auth.setup(FIXTURE_PASSWORD,is_local=True,bootstrap_token=token)
    marker=tmp_path/'keep.txt';marker.write_text('记忆与聊天应保留','utf-8')
    assert auth.authenticate(session['token'])
    auth.reset_from_host()
    assert not auth.authenticate(session['token'])
    assert auth.status()['setup_required']
    assert marker.read_text('utf-8')=='记忆与聊天应保留'
    assert FIXTURE_PASSWORD not in auth.path.read_text('utf-8')


def test_checkpoint_reuses_finished_chunks_and_rejects_changed_material(tmp_path):
    state={}
    calls=[]
    class Model:
        fail=True
        async def complete(self,messages,**kwargs):
            assert kwargs['before_send']()
            calls.append(messages[-1]['content'])
            if self.fail and len(calls)==2: raise ConnectionError('模拟进程中断')
            return {'content':'有依据时自然回应，不确定时先确认。','usage':{'total_tokens':100}}
    model=Model();service=DistillationService(model,tmp_path)
    data=io.BytesIO()
    with zipfile.ZipFile(data,'w') as archive:
        archive.writestr('notes.txt','观察表达与互动习惯。\n'*1000)
    inventory=service.inspect_corpus(data.getvalue(),'notes.zip')
    payload={'mode':'corpus','confirmed':True,'upload_id':inventory['upload_id'],'name':'测试人物'}
    def checkpoint(**fields): state.update(copy.deepcopy(fields))
    with pytest.raises(ConnectionError):
        asyncio.run(service.generate(payload,checkpoint=checkpoint))
    cached=state['distillation_cache'];assert cached['results']
    initial=calls[0];model.fail=False
    result=asyncio.run(DistillationService(model,tmp_path).generate(payload,checkpoint=checkpoint,resume_state=cached))
    assert result['resumed_steps']>=1 and calls.count(initial)==1
    assert result['usage']['calls']>=len(calls)
    with pytest.raises(ValueError,match='变化'):
        asyncio.run(service.generate(dict(payload,name='不同要求'),checkpoint=checkpoint,resume_state=cached))


def test_run_resume_survives_restart_and_never_replays_computer_tasks(tmp_path):
    async def scenario():
        runs=RunManager(tmp_path)
        async def interrupted(ctx,payload):
            ctx.checkpoint(distillation_cache={'results':{'one':'completed'}})
            raise RuntimeError('中断')
        runs.register('distillation',interrupted)
        job=runs.submit('distillation',{'mode':'generate'},identity={'platform':'local'})
        await asyncio.gather(runs._tasks[job['id']])
        restored=RunManager(tmp_path)
        async def finish(ctx,payload):
            assert ctx.job['_state']['distillation_cache']['results']['one']=='completed'
            return {'ok':True}
        restored.register('distillation',finish)
        new=restored.resume(job['id'])
        await asyncio.gather(restored._tasks[new['id']])
        assert restored.get(new['id'])['status']=='completed'
        restored._jobs[job['id']]['kind']='computer'
        with pytest.raises(ValueError): restored.resume(job['id'])
    asyncio.run(scenario())


def test_context_approval_requires_unique_same_sender_scope_and_live_authority(tmp_path):
    app=create_app(tmp_path,start_workers=False)
    access=app.state.access;runs=app.state.runs
    app.state.settings.update({'access':{'administrators':[{'platform':'onebot','account_id':'101','instance_id':'9000'}]}})
    identity=access.identity('onebot','101','9000')
    async def scenario():
        async def handler(ctx,payload):
            await ctx.approve('测试动作',{})
            return {'ok':True}
        runs.register('fixture',handler,lambda job:access.is_admin(job['identity']))
        job=runs.submit('fixture',{},identity=identity,scope_id='qq:private:101',user_id='101')
        await asyncio.sleep(.01)
        command=app.state.chat.handle_platform_command
        assert await command('qq:private:102','101','同意',{'trusted_identity':identity}) is None
        assert await command('qq:private:101','101','同意',{'trusted_identity':identity,'forwarded':True}) is None
        reply=await command('qq:private:101','101','同意',{'trusted_identity':identity})
        assert '已确认' in reply['reply']
        await asyncio.gather(runs._tasks[job['id']])
        assert runs.get(job['id'])['status']=='completed'
        second=runs.submit('fixture',{},identity=identity,scope_id='qq:private:101',user_id='101')
        third=runs.submit('fixture',{},identity=identity,scope_id='qq:private:101',user_id='101')
        await asyncio.sleep(.01)
        assert '多个' in (await command('qq:private:101','101','确认',{'trusted_identity':identity}))['reply']
        await runs.close();await app.state.chat.close();app.state.store.close()
    asyncio.run(scenario())


def test_model_queue_has_bounded_parallelism_and_cancellation():
    async def scenario():
        gate=_PriorityGate(capacity=2,queue_limit=1)
        await gate.acquire();await gate.acquire()
        waiting=asyncio.create_task(gate.acquire(10));await asyncio.sleep(0)
        with pytest.raises(ProviderError,match='队列已满'):await gate.acquire()
        waiting.cancel()
        with pytest.raises(asyncio.CancelledError):await waiting
        gate.release();gate.release()
        assert not gate.locked() and gate.active==0
    asyncio.run(scenario())


def test_docker_missing_fails_closed_before_process_creation(tmp_path,monkeypatch):
    import shiori.sandbox as sandbox
    app=create_app(tmp_path,start_workers=False)
    assert app.state.settings.get()['computer']['runtime']=='docker'
    monkeypatch.setattr(sandbox,'availability',lambda:{'available':False,'message':'Docker不可用'})
    import shiori.computer as computer
    monkeypatch.setattr(computer.subprocess,'Popen',lambda *_args,**_kwargs:pytest.fail('must not run host shell'))
    with pytest.raises(ValueError,match='Docker不可用'):
        app.state.computer._process(SimpleNamespace(check=lambda:True,id='fixture'),tmp_path,'execute_shell','echo test')
    app.state.store.close()


def test_docker_command_is_local_offline_restricted_and_mounts_no_secrets(tmp_path,monkeypatch):
    import shiori.sandbox as sandbox
    monkeypatch.setattr(sandbox,'availability',lambda:{'available':True})
    monkeypatch.setattr(sandbox,'docker_prefix',lambda:['docker','--host','local-engine'])
    root=tmp_path/'workspace';root.mkdir()
    command,identity=sandbox.prepare(root,tmp_path/'history','execute_python','print(1)')
    assert command[:3]==['docker','--host','local-engine']
    assert command[command.index('--network')+1]=='none'
    assert '--read-only' in command and '--cap-drop' in command
    assert '--pull=never' in command and '--privileged' not in command
    assert '--env-file' not in command and len(identity)==len('shiori-')+32
    assert sum('source=' in arg for arg in command)==2


@pytest.mark.parametrize('url',['https://bad.example/{x}','http://bad.example','https://user:pass@bad.example','https://bad.example?q=key','https://bad.example:1234'])
def test_https_guide_rejects_unsafe_or_incompatible_input(url):
    with pytest.raises(ValueError): https_guide(url,8765)


def test_canonical_import_compatibility_shares_one_module():
    import importlib
    assert importlib.import_module('qunyu.db') is importlib.import_module('shiori.db')


def test_explicit_proxy_trust_only_establishes_https_not_local_authority(tmp_path):
    app=create_app(tmp_path,start_workers=False,allow_remote=True)
    with TestClient(app,base_url='http://127.0.0.1',client=('127.0.0.1',1)) as local:
        password_login(local,app)
        app.state.settings.update({'webui':{'public_url':'https://bot.example.com','allowed_hosts':['bot.example.com'],
                                           'trusted_proxy_ips':['172.30.49.1']}})
        for address,expected in [('172.30.49.1',200),('172.30.49.2',403)]:
            with TestClient(app,base_url='http://bot.example.com',client=(address,123)) as proxy:
                headers={'X-Forwarded-Proto':'https'}
                reply=proxy.post('/api/auth/login',json={'password':FIXTURE_PASSWORD},headers=headers)
                assert reply.status_code==expected
                if expected==200:
                    assert 'Secure' in reply.headers['set-cookie']
                    status=proxy.get('/api/auth/status',headers=headers).json()
                    assert not status['local_setup_allowed'] and 'password_file' not in status
                    assert proxy.post('/api/auth/bootstrap',json={'token':app.state.settings.token},headers=headers).status_code==403
        with pytest.raises(ValueError):app.state.settings.update({'webui':{'trusted_proxy_ips':['172.30.49.0/24']}})


def test_limited_writer_survives_owned_stream_closing():
    from shiori.computer_worker import LimitedWriter
    stream=io.StringIO();writer=LimitedWriter(stream)
    writer.write('正常输出');stream.close()
    writer.flush();writer.close()
