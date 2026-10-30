import asyncio
import json
import uuid
from pathlib import Path
import shutil
import subprocess

import httpx
import pytest
from fastapi.testclient import TestClient

from shiori.api import create_app
from shiori.db import Store
from shiori.model_stream import ModelStreamError, receive_chat
from shiori.provider import Provider, ProviderError, ProviderTimeoutError, ProviderStreamError, ProviderOutputBudgetError, is_budget_exceeded
from shiori.settings import Settings, PROVIDER_DEFAULTS
from shiori.usage_ledger import reconcile, breakdown


class Chunks(httpx.AsyncByteStream):
    def __init__(self, parts, delay=0): self.parts, self.delay, self.closed = parts, delay, False
    async def __aiter__(self):
        for part in self.parts:
            if self.delay: await asyncio.sleep(self.delay)
            yield part
    async def aclose(self): self.closed = True


def sse(delta, finish=None):
    return ('data: ' + json.dumps({'choices':[{'index':0,'delta':delta,'finish_reason':finish}]}, ensure_ascii=False) + '\n\n').encode()


@pytest.fixture
def setup_provider(tmp_path, monkeypatch):
    store, settings = Store(tmp_path), Settings(tmp_path)
    settings.update({'model':{'base_url':'https://fixture.invalid/v1','model':'chat'}})
    original = httpx.AsyncClient
    def transport(handler): monkeypatch.setattr(httpx,'AsyncClient',lambda **kw:original(transport=httpx.MockTransport(handler),**kw))
    yield Provider(settings,store), settings, store, transport
    store.close()


def test_sse_unicode_chunks_usage_and_single_settlement(setup_provider):
    provider, settings, store, transport = setup_provider
    raw = sse({'role':'assistant'}) + sse({'content':'你好，'}) + sse({'content':'世界。'},'stop')
    raw += b'data: {"choices":[],"usage":{"prompt_tokens":12,"completion_tokens":6}}\n\ndata: [DONE]\n\n'
    stream=Chunks([raw[i:i+3] for i in range(0,len(raw),3)])
    seen=[]
    def handler(request):
        seen.append(json.loads(request.content))
        return httpx.Response(200,headers={'content-type':'text/event-stream'},stream=stream)
    transport(handler)
    result=asyncio.run(provider.complete([{'role':'user','content':'hi'}]))
    assert result['content']=='你好，世界。' and stream.closed
    assert seen[0]['stream'] is True
    assert result['usage']['input_tokens']==12 and result['usage']['output_tokens']==6
    assert result['usage']['ttft_ms'] is not None
    assert len(store.list_usage())==1 and not provider._reservations


def test_sse_tools_are_assembled_only_after_complete_arguments(setup_provider):
    provider, _, store, transport = setup_provider
    raw=sse({'tool_calls':[{'index':0,'id':'call-1','type':'function','function':{'name':'calculate','arguments':'{"ex'}}]})
    raw+=sse({'tool_calls':[{'index':0,'function':{'arguments':'pression":"1+1"}'}}]},'tool_calls')+b'data: [DONE]\n\n'
    transport(lambda request:httpx.Response(200,headers={'content-type':'text/event-stream'},stream=Chunks([raw])))
    result=asyncio.run(provider.complete([{'role':'user','content':'hi'}],tools=[{'type':'function','function':{'name':'calculate','parameters':{'type':'object'}}}]))
    assert json.loads(result['tool_calls'][0]['function']['arguments'])=={'expression':'1+1'}
    assert len(store.list_usage())==1


def test_partial_stream_is_not_returned_or_treated_as_free(setup_provider):
    provider, _, store, transport=setup_provider
    transport(lambda request:httpx.Response(200,headers={'content-type':'text/event-stream'},stream=Chunks([sse({'content':'partial'})])))
    with pytest.raises(ProviderError,match='中断'):
        asyncio.run(provider.complete([{'role':'user','content':'hi'}]))
    assert store.list_usage()[0]['unconfirmed'] and not provider._reservations


def test_done_sentinel_completes_text_without_finish_reason(setup_provider):
    provider, _, store, transport = setup_provider
    raw = sse({'content':'完整的纯文本。'}) + b'data: [DONE]\n\n'
    transport(lambda request:httpx.Response(200, headers={'content-type':'text/event-stream'}, stream=Chunks([raw])))
    assert asyncio.run(provider.complete([{'role':'user','content':'hi'}]))['content'] == '完整的纯文本。'
    assert len(store.list_usage()) == 1 and not store.list_usage()[0].get('unconfirmed')


def test_reasoning_exhaustion_is_not_misreported_as_network_or_daily_budget(setup_provider):
    provider, _, store, transport=setup_provider
    raw=sse({'reasoning':'private thinking fragment'},'length')
    raw+=b'data: {"choices":[],"usage":{"prompt_tokens":70,"completion_tokens":600,"completion_tokens_details":{"reasoning_tokens":600}}}\n\ndata: [DONE]\n\n'
    transport(lambda request:httpx.Response(200,headers={'content-type':'text/event-stream'},stream=Chunks([raw])))
    with pytest.raises(ProviderOutputBudgetError) as failure:
        asyncio.run(provider.complete([{'role':'user','content':'hi'}],purpose='distillation',max_tokens=600))
    assert not is_budget_exceeded(failure.value)
    records=store.list_usage()
    assert len(records)==1 and records[0]['output_tokens']==600 and not records[0].get('unconfirmed')
    assert not any(event['kind']=='model_stream_incomplete' for event in store.list_events())
    assert 'private thinking fragment' not in json.dumps(store.list_events())


def test_real_provider_analysis_retry_preserves_two_separate_usage_records(setup_provider, tmp_path):
    from shiori.distillation import DistillationService
    provider, _, store, transport = setup_provider
    seen = []
    def handler(request):
        seen.append(request)
        raw = sse({'content':'partial'}) if len(seen) == 1 else sse({'content':'完整的人格草稿'}, 'stop')
        return httpx.Response(200, headers={'content-type':'text/event-stream'}, stream=Chunks([raw]))
    transport(handler)
    result = asyncio.run(DistillationService(provider, tmp_path).generate({'mode':'generate','description':'温柔的读书伙伴'}))
    records = store.list_usage()
    assert len(seen) == 2 and len(records) == 2
    assert sum(bool(record.get('unconfirmed')) for record in records) == 1
    assert len({record['request_id'] for record in records}) == 2
    assert not provider._reservations and result['personality'] == '完整的人格草稿'
    assert result['usage']['calls'] == 2 and result['usage']['estimated']


def test_done_sentinel_does_not_authorize_unfinished_tools(setup_provider):
    provider, _, store, transport = setup_provider
    raw = sse({'tool_calls':[{'index':0,'id':'c1','function':{'name':'calculate','arguments':'{}'}}]}) + b'data: [DONE]\n\n'
    transport(lambda request:httpx.Response(200, headers={'content-type':'text/event-stream'}, stream=Chunks([raw])))
    with pytest.raises(ProviderStreamError, match='未执行工具'):
        asyncio.run(provider.complete([{'role':'user','content':'hi'}], tools=[{'type':'function','function':{'name':'calculate'}}]))
    event = next(item for item in store.list_events() if item['kind'] == 'model_stream_incomplete')
    assert event['details']['done_received'] and not event['details']['finish_received']
    assert event['details']['has_tool_calls'] and store.list_usage()[0]['unconfirmed']


@pytest.mark.parametrize('continuation', [False, True])
def test_stream_fallback_is_opt_in_and_does_not_replay_tool_continuations(setup_provider, continuation):
    provider, settings, store, transport = setup_provider
    settings.update({'providers':[dict(PROVIDER_DEFAULTS,id=key,model=key,base_url=f'https://{key}.invalid/v1',timeout_fallback=True) for key in ('a','b')],
                     'routing':{'chat':'a','fallback':['b']}})
    seen = []
    def handler(request):
        seen.append(request.url.host)
        if request.url.host == 'a.invalid':
            return httpx.Response(200, headers={'content-type':'text/event-stream'}, stream=Chunks([sse({'content':'PRIVATE_PARTIAL_TEXT'})]))
        return httpx.Response(200,json={'choices':[{'message':{'content':'完整的备用回答'}}]})
    transport(handler)
    messages = [{'role':'user','content':'hi'}]
    if continuation:
        messages.append({'role':'tool','tool_call_id':'c1','content':'already executed'})
        with pytest.raises(ProviderStreamError): asyncio.run(provider.complete(messages))
        assert seen == ['a.invalid']
    else:
        assert asyncio.run(provider.complete(messages))['content'] == '完整的备用回答'
        assert seen == ['a.invalid','b.invalid']
    assert sum(bool(row.get('unconfirmed')) for row in store.list_usage()) == 1
    event = next(item for item in store.list_events() if item['kind'] == 'model_stream_incomplete')
    assert 'PRIVATE_PARTIAL_TEXT' not in json.dumps(event)
    assert not event['details']['done_received'] and event['details']['received_bytes'] > 0


def test_heartbeats_do_not_extend_first_content_deadline():
    stream=Chunks([b': keepalive\n\n']*20,.01)
    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request:httpx.Response(200,headers={'content-type':'text/event-stream'},stream=stream))) as client:
            with pytest.raises(httpx.ReadTimeout):
                await receive_chat(client,'https://fixture.invalid/v1',{}, {},first_token_seconds=.03)
        assert stream.closed
    asyncio.run(scenario())


@pytest.mark.parametrize('tools_used',[False,True])
def test_opt_in_timeout_fallback_retains_cost_and_never_replays_tool_continuation(setup_provider,tools_used):
    provider,settings,store,transport=setup_provider
    settings.update({'providers':[dict(PROVIDER_DEFAULTS,id=key,model=key,base_url=f'https://{key}.invalid/v1',timeout_fallback=True) for key in ('a','b')], 'routing':{'chat':'a','fallback':['b']}})
    seen=[]
    def handler(request):
        seen.append(request.url.host)
        if request.url.host=='a.invalid': raise httpx.ReadTimeout('fixture',request=request)
        return httpx.Response(200,json={'choices':[{'message':{'content':'备用回答'}}]})
    transport(handler)
    messages=[{'role':'user','content':'hi'}]+([{'role':'tool','content':'already executed','tool_call_id':'c1'}] if tools_used else [])
    if tools_used:
        with pytest.raises(ProviderTimeoutError):asyncio.run(provider.complete(messages))
        assert seen==['a.invalid'] and len(store.list_usage())==1
    else:
        assert asyncio.run(provider.complete(messages))['content']=='备用回答'
        assert seen==['a.invalid','b.invalid'] and len(store.list_usage())==2
    assert sum(bool(item.get('unconfirmed')) for item in store.list_usage())==1


@pytest.mark.parametrize('failure', [httpx.ReadError, httpx.RemoteProtocolError])
@pytest.mark.parametrize('fallback,continuation', [(True, False), (False, False), (True, True)])
def test_hard_stream_disconnect_obeys_uncertain_fallback(setup_provider, failure, fallback, continuation):
    provider, settings, store, transport = setup_provider
    settings.update({'providers': [dict(PROVIDER_DEFAULTS, id=key, model=key,
        base_url=f'https://{key}.invalid/v1', stream_mode='on', timeout_fallback=fallback)
        for key in ('a', 'b')], 'routing': {'chat': 'a', 'fallback': ['b']}})
    class BrokenStream(Chunks):
        async def __aiter__(self):
            yield sse({'content': 'incomplete reply'})
            raise failure('fixture relay disconnected')
    calls = []
    def handler(request):
        calls.append(request.url.host)
        if request.url.host == 'a.invalid':
            return httpx.Response(200, headers={'content-type': 'text/event-stream'}, stream=BrokenStream([]))
        return httpx.Response(200, json={'choices': [{'message': {'content': '完整的备用回答'}, 'finish_reason': 'stop'}]})
    transport(handler)
    messages = [{'role': 'user', 'content': 'hi'}]
    if continuation:
        messages.append({'role': 'tool', 'tool_call_id': 'c1', 'content': 'already executed'})
    if fallback and not continuation:
        assert asyncio.run(provider.complete(messages))['content'] == '完整的备用回答'
        assert calls == ['a.invalid', 'b.invalid']
    else:
        from shiori.provider import ProviderConnectionError
        with pytest.raises(ProviderConnectionError):
            asyncio.run(provider.complete(messages))
        assert calls == ['a.invalid']
    assert sum(bool(item.get('unconfirmed')) for item in store.list_usage()) == 1
    assert not provider._reservations


def test_stream_capability_rejection_retries_only_rejected_request(setup_provider):
    provider,_,store,transport=setup_provider
    payloads=[]
    def handler(request):
        payload=json.loads(request.content);payloads.append(payload)
        if payload.get('stream'):return httpx.Response(400,json={'error':{'message':'stream is unsupported'}})
        return httpx.Response(200,json={'choices':[{'message':{'content':'兼容响应'}}]})
    transport(handler)
    assert asyncio.run(provider.complete([{'role':'user','content':'hi'}]))['content']=='兼容响应'
    assert len(payloads)==2 and not payloads[1].get('stream') and len(store.list_usage())==1


def test_reconciliation_replaces_effective_usage_once_and_keeps_audit(tmp_path):
    store=Store(tmp_path)
    try:
        item=store.record_usage({'input_tokens':100,'output_tokens':1000,'cost':1.1,'estimated':True,'unconfirmed':True,'purpose':'planner'})
        payload={'reconciliation_id':uuid.uuid4().hex,'action':'actual','input_tokens':10,'output_tokens':20,'cost':.03,'note':'核对服务商账单'}
        record=reconcile(store,item['id'],payload)
        assert record['original_estimate']['output_tokens']==1000 and not record['unconfirmed']
        assert reconcile(store,item['id'],payload)==record
        assert store.usage_totals()['tokens']==30 and len(store.list_usage())==1
        assert len([event for event in store.list_events() if event['kind']=='usage_reconciled'])==1
        with pytest.raises(ValueError):reconcile(store,item['id'],dict(payload,cost=.5))
    finally:store.close()


def test_unconfirmed_usage_api_requires_local_authenticated_admin(tmp_path):
    app=create_app(tmp_path,start_workers=False,allow_remote=True)
    with TestClient(app,base_url='http://127.0.0.1:8765',client=('127.0.0.1',1234)) as client:
        item=app.state.store.record_usage({'input_tokens':10,'output_tokens':90,'estimated':True,'unconfirmed':True})
        payload={'action':'not_billed','reconciliation_id':uuid.uuid4().hex,'note':'服务商确认没有计费'}
        assert client.post(f'/api/usage/{item["id"]}/reconcile',json=payload).status_code==401
        client.headers['Authorization']='Bearer '+app.state.settings.token
        assert client.get('/api/usage/unconfirmed').json()['total']==1
        response=client.post(f'/api/usage/{item["id"]}/reconcile',json=payload)
        assert response.status_code==200,response.text
        assert app.state.store.usage_totals()['tokens']==0
        assert client.get('/api/usage/unconfirmed').json()['total']==0
        assert len(app.state.store.list_usage())==1


def test_cgroup_metrics_report_limits_and_oom_without_touching_host(tmp_path):
    from shiori.resource_metrics import read_resources
    group, proc = tmp_path / 'group', tmp_path / 'proc'
    for name,value in {'memory.current':'950','memory.max':'1000','memory.peak':'980','cpu.max':'50000 100000','memory.events':'oom 2\noom_kill 1\n'}.items():
        (group / name).parent.mkdir(parents=True, exist_ok=True)
        (group / name).write_text(value)
    # Membership must be a fixture too: the host's real cgroup path does not
    # exist under a temporary root, and reading the host /proc would make this
    # test depend on the runner's container layout instead of the fixture.
    (proc / 'self').mkdir(parents=True, exist_ok=True)
    (proc / 'self' / 'cgroup').write_text('0::/\n')
    result=read_resources(group, proc_root=proc)
    assert result['available'] and result['memory_pressure'] and result['oom_kills']==1
    assert result['cpu_limit']==.5 and result['memory_limit_bytes']==1000


def test_ledger_controls_use_local_identity_and_safe_text():
    node=shutil.which('node')
    if not node:pytest.skip('Node unavailable')
    result=subprocess.run([node,str(Path(__file__).with_name('frontend_usage_ledger.cjs'))],capture_output=True,text=True,encoding='utf-8',timeout=20)
    assert result.returncode==0,result.stdout+result.stderr


def test_repeated_request_settlement_is_idempotent(setup_provider):
    provider,settings,store,_=setup_provider
    async def scenario():
        key,_=await provider._reserve(settings.get()['model'],'web:default','chat',10,20)
        record={'input_tokens':10,'output_tokens':5,'scope_id':'web:default'}
        await provider._settle(key,record)
        await provider._settle(key,record)
    asyncio.run(scenario())
    assert len(store.list_usage())==1 and store.list_usage()[0]['request_id']


def test_parser_admission_cancellation_does_not_leak_permits():
    from shiori.work_limits import WorkLimiter,WorkBusy
    from types import SimpleNamespace
    gate=WorkLimiter(SimpleNamespace(get=lambda:{'scheduler':{'parsing_concurrency':1,'parsing_queue_limit':1}}),'parsing',1,1)
    entered,release=asyncio.Event(),asyncio.Event()
    async def hold():
        async with gate.slot():entered.set();await release.wait()
    async def scenario():
        first=asyncio.create_task(hold());await entered.wait()
        second=asyncio.create_task(hold());await asyncio.sleep(0)
        await asyncio.sleep(0)
        with pytest.raises(WorkBusy):
            async with gate.slot():pytest.fail('overflow must not run')
        second.cancel()
        with pytest.raises(asyncio.CancelledError):await second
        release.set();await first
        assert gate.gate.active==0 and sum(gate.gate.counts().values())==0
    asyncio.run(scenario())


def test_authenticated_remote_session_cannot_write_off_usage(tmp_path):
    from auth_helpers import password_login,FIXTURE_PASSWORD
    app=create_app(tmp_path,start_workers=False,allow_remote=True)
    app.state.settings.update({'webui':{'remote_enabled':True}})
    with TestClient(app,base_url='http://127.0.0.1:8765',client=('127.0.0.1',1234)) as local:
        password_login(local,app)
        item=app.state.store.record_usage({'input_tokens':10,'output_tokens':90,'estimated':True,'unconfirmed':True})
        remote=TestClient(app,base_url='https://192.168.1.2:8765',client=('192.168.1.10',1235))
        try:
            assert remote.post('/api/auth/login',json={'password':FIXTURE_PASSWORD}).status_code==200
            response=remote.post(f'/api/usage/{item["id"]}/reconcile',json={'action':'not_billed','reconciliation_id':uuid.uuid4().hex,'note':'test'})
            assert response.status_code==403
        finally:
            remote.close()
        assert app.state.store.usage_totals()['tokens']==100


def test_total_model_deadline_cleans_up_unknown_request(setup_provider):
    provider,settings,store,transport=setup_provider
    settings.update({'model':{'total_timeout_seconds':5}})
    async def handler(request):
        await asyncio.sleep(30)
        return httpx.Response(200,json={'choices':[{'message':{'content':'too late'}}]})
    transport(handler)
    with pytest.raises(ProviderTimeoutError,match='总等待时限'):
        asyncio.run(provider.complete([{'role':'user','content':'hi'}]))
    assert not provider._reservations and provider._global_gate.active==0
    assert store.list_usage()[0]['unconfirmed']


def test_first_content_deadline_includes_waiting_for_headers():
    async def handler(request):
        await asyncio.sleep(10)
        return httpx.Response(200,json={})
    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            with pytest.raises(httpx.ReadTimeout):
                await receive_chat(client,'https://fixture.invalid/v1',{}, {},first_token_seconds=.03)
    asyncio.run(scenario())


def test_semantic_deadline_settles_dispatched_embedding_and_releases_model_queue(tmp_path,monkeypatch):
    from shiori.memory import MemoryEngine
    store,settings=Store(tmp_path),Settings(tmp_path)
    settings.update({'model':{'base_url':'https://fixture.invalid/v1','model':'chat'},'memory':{'semantic_enabled':True,'embedding_model':'embed'}})
    store.create_memory({'scope_id':'web:default','user_id':'owner','content':'喜欢咖啡'})
    original=httpx.AsyncClient
    async def handler(request):await asyncio.sleep(10)
    monkeypatch.setattr(httpx,'AsyncClient',lambda **kw:original(transport=httpx.MockTransport(handler),**kw))
    provider=Provider(settings,store);engine=MemoryEngine(store,settings,provider.embed)
    async def scenario():
        # Warm the cold lexical index first: this deadline bounds the online
        # embedding phases, not host-dependent local index construction.
        await engine._rank_async('web:default','owner',engine._config(),'咖啡')
        result=await engine.inspect('web:default','owner','咖啡',semantic_deadline=asyncio.get_running_loop().time()+.6)
        assert result['mode']=='lexical' and result['semantic']['error']=='TimeoutError'
        assert not provider._reservations and provider._global_gate.active==0
        assert store.list_usage()[0]['unconfirmed']
    try:asyncio.run(scenario())
    finally:engine.close();store.close()
