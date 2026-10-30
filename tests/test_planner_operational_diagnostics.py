"""Real local admission/cancellation and text-free delivery suppression evidence."""
import asyncio
from copy import deepcopy
import json
from types import SimpleNamespace

import httpx
import pytest

from shiori.db import Store
from shiori.provider import Provider, model_request_trace, mark_model_stage, model_trace_snapshot
import shiori.provider as provider_module
from shiori.settings import DEFAULTS
from shiori.social import SocialEngine, ConversationFlow


@pytest.fixture
def anyio_backend():
    return 'asyncio'


class Config:
    def __init__(self):
        self.value = deepcopy(DEFAULTS)
        self.value['model'].update(base_url='https://fixture.invalid/v1', model='fixture', stream_mode='off')
        self.value['onebot'].update(enabled=True, chat_policy='blacklist')
        self.value['planner'].update(min_interval_ms=0, group_batch_ms=0, max_age_ms=80)
        self.value['debounce']['enabled'] = False
        self.value['output'].update(enabled=True, mode='model', interval_ms=0)
        self.value['stickers']['enabled'] = False

    def get(self):
        return deepcopy(self.value)


def setup_social(tmp_path):
    store, config = Store(tmp_path), Config()
    config.value['identity'].update(respond_to_at=False, respond_to_name=False)
    scope = store.upsert_scope({'id': 'qq:group:202', 'plan_enabled': True, 'cooldown_seconds': 0,
                                'merge_window_ms': 0, 'auto_memory': False})
    provider = Provider(config, store)
    return store, config, scope, provider, SocialEngine(store, config, provider)


def message(store, scope, *, direct=True):
    return store.add_message(scope['id'], '303', 'user', 'Please answer this message', at_bot=direct)


def install_transport(monkeypatch, handler):
    real_client = httpx.AsyncClient
    monkeypatch.setattr(provider_module.httpx, 'AsyncClient',
        lambda **kwargs: real_client(transport=httpx.MockTransport(handler), **kwargs))


def valid_plan(request, action='reply'):
    payload = json.loads(json.loads(request.content)['messages'][-1]['content'])
    return json.dumps({'action': action, 'target_message_id': payload['current_message_id'],
        'target_user_id': payload['current_user_id'], 'addressed_to_bot': True,
        'intent': 'Answer the request', 'reason': 'The message is addressed to the bot', 'needed_info': []})


def response(content):
    return httpx.Response(200, json={'choices': [{'finish_reason': 'stop',
        'message': {'role': 'assistant', 'content': content}}],
        'usage': {'prompt_tokens': 100, 'completion_tokens': 30}})


@pytest.mark.anyio
async def test_successful_plan_has_correlated_queue_dispatch_response_and_parse(tmp_path, monkeypatch):
    store, config, scope, provider, social = setup_social(tmp_path)
    config.value['planner']['max_age_ms'] = 2000
    async def handler(request):
        await asyncio.sleep(.015)
        return response(valid_plan(request))
    install_transport(monkeypatch, handler)
    try:
        result = await social.plan(scope, message(store, scope), direct=True)
        assert result['reply']
        trace = result['planner_diagnostics']
        assert trace['dispatched'] and trace['stage'] == 'parse'
        assert {'recall', 'queue', 'prepare', 'dispatch', 'response', 'parse'} <= trace['timings_ms'].keys()
        usage = store.list_usage()[0]
        assert trace['request_id'] == usage['request_id'] and trace['request_ids'] == [usage['request_id']]
        assert trace['timings_ms']['dispatch'] >= 10
        event = next(e for e in store.list_events() if e['kind'] == 'planner_timing')
        assert event['details']['outcome'] == 'reply' and event['details']['trace_id'] == trace['trace_id']
        assert not provider._reservations and provider._global_gate.active == 0
    finally:
        store.close()


@pytest.mark.anyio
@pytest.mark.parametrize('phase', ['recall', 'queue', 'dispatch'])
async def test_shared_deadline_identifies_phase_and_only_dispatched_work_retains_consumption(tmp_path, monkeypatch, phase):
    store, config, scope, provider, social = setup_social(tmp_path)
    calls = []
    config.value['planner']['max_age_ms'] = 70
    async def handler(request):
        calls.append(request)
        await asyncio.sleep(.5)
        return response(valid_plan(request))
    install_transport(monkeypatch, handler)
    if phase == 'recall':
        async def knowledge(*args):
            await asyncio.sleep(.5)
            return {}
        social.knowledge_fn = knowledge
    slot = provider._provider_slot(provider.resolve('planner'), 'chat')
    try:
        if phase == 'queue':
            await slot.__aenter__()
        result = await social.plan(scope, message(store, scope), direct=True)
        assert not result['reply'] and result['error_code'] == 'timeout'
        error = next(e['details'] for e in store.list_events() if e['kind'] == 'planner_error')
        assert error['stage'] == phase and error['termination_reason'] == 'planner_deadline'
        assert error['request_trace']['stage'] == phase
        usage = store.list_usage()
        if phase == 'dispatch':
            assert len(calls) == len(usage) == 1
            assert usage[0]['unconfirmed'] and usage[0]['estimated']
            assert usage[0]['latency_ms'] >= 30
            assert usage[0]['termination_reason'] == 'planner_deadline'
            assert usage[0]['request_id'] == error['request_trace']['request_id']
            assert not any(key.startswith('_') for key in usage[0])
        else:
            assert not calls and not usage and not error['request_trace'].get('dispatched')
        assert not provider._reservations
    finally:
        if phase == 'queue':
            await slot.__aexit__(None, None, None)
        assert provider._global_gate.active == 0 and not provider._scheduler.waiting
        store.close()


@pytest.mark.anyio
async def test_background_cancel_does_not_inherit_expired_planner_deadline(tmp_path, monkeypatch):
    store, config, scope, provider, _ = setup_social(tmp_path)
    entered = asyncio.Event()
    async def handler(request):
        entered.set()
        await asyncio.sleep(10)
    install_transport(monkeypatch, handler)
    try:
        with model_request_trace(deadline=asyncio.get_running_loop().time() - 1) as trace:
            mark_model_stage('recall')
            task = asyncio.create_task(provider.complete([{'role':'user','content':'Fixture'}], purpose='chat'))
            await asyncio.wait_for(entered.wait(), 2)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert model_trace_snapshot(trace)['stage'] == 'recall'
            assert not model_trace_snapshot(trace)['request_ids']
        usage = store.list_usage()[0]
        assert usage['unconfirmed'] and usage['termination_reason'] == 'cancelled'
        assert not provider._reservations and provider._global_gate.active == 0
    finally:
        store.close()


@pytest.mark.anyio
async def test_passive_backoff_is_bounded_and_does_not_swallow_mentions_or_private_chat(tmp_path):
    store, config, scope, _, social = setup_social(tmp_path)
    config.value['planner']['max_age_ms'] = 2000
    calls = []
    class Broken:
        async def complete(self, *args, **kwargs):
            calls.append(kwargs['scope_id'])
            return {'content': 'PRIVATE RESPONSE MUST NOT ENTER DIAGNOSTICS'}
    social.provider = Broken()
    try:
        for _ in range(12):
            social._planner_failures.get(scope['id'], {}).update(until=0)
            result = await social.plan(scope, message(store, scope, direct=False))
            assert result['error_code'] == 'invalid_json'
        assert social._planner_failures[scope['id']]['count'] == 8
        error = next(e['details'] for e in store.list_events() if e['kind'] == 'planner_error')
        assert error['passive_backoff_ms'] == 30000
        before = len(calls)
        result = await social.plan(scope, message(store, scope, direct=False))
        assert result['error_code'] == 'planner_backoff' and len(calls) == before
        await social.plan(scope, message(store, scope), direct=True)
        assert len(calls) == before + 1
        private = store.upsert_scope({'id':'qq:private:303','plan_enabled':True,'always_reply':False})
        social._planner_failures[private['id']] = {'count':8, 'until':float('inf')}
        await social.plan(private, message(store, private), direct=False)
        assert len(calls) == before + 2
        events = json.dumps(store.list_events(100), ensure_ascii=False)
        assert 'PRIVATE RESPONSE' not in events
        assert error['response_prefix_kind'] == 'other' and not error['response_fenced']
    finally:
        store.close()


class FixtureChat:
    def __init__(self, store, config, mutation=None):
        self.store, self.settings = store, config
        self.social = SocialEngine(store, config, None)
        self.mutation = mutation
        self.entered = asyncio.Event()
        self.block = None

    def observe(self, scope, user, text, images=None, metadata=None):
        record = self.store.add_message(scope, user, 'user', text, **dict(metadata or {}))
        self.social.observe(scope, record)
        return record

    async def respond(self, scope, user, text, images=None, metadata=None):
        self.entered.set()
        if self.block:
            await self.block.wait()
        reply = 'First complete sentence.\n\nSecond complete sentence.'
        record = self.store.add_message(scope, 'assistant', 'assistant', reply,
                                       source_ids=metadata['source_ids'])
        if self.mutation:
            self.mutation(scope, metadata)
        return {'reply': reply, 'reply_parts':['First complete sentence.','Second complete sentence.'],
                'message_id':record['id']}


@pytest.mark.anyio
@pytest.mark.parametrize('change,reason', [('permission','permission_denied'),('identity','identity_changed'),
    ('source','source_changed'),('request','request_superseded')])
async def test_generated_reply_suppressed_before_outbox_has_specific_reason(tmp_path, change, reason):
    store, config = Store(tmp_path), Config()
    scope = 'qq:group:202'
    store.upsert_scope({'id':scope,'plan_enabled':False,'merge_window_ms':0,'cooldown_seconds':0})
    sent = []
    chat = FixtureChat(store, config)
    def mutate(scope, metadata):
        if change == 'permission': config.value['paused'] = True
        elif change == 'identity': config.value['identity']['name'] = 'A different identity'
        elif change == 'request': chat.social._advance_request((scope,'303'))
        else:
            with store._lock, store._conn:
                store._conn.execute('DELETE FROM messages WHERE id=?',(metadata['source_ids'][0],))
    chat.mutation = mutate
    async def emit(*args):
        sent.append(args)
        return True
    flow = ConversationFlow(store, config, chat, emit,
                            lambda s,u: not config.value.get('paused',False))
    try:
        await flow.handle(scope,'303','Please answer',[],{'at_bot':True},direct=True)
        assert not sent
        events = store.list_events(100)
        suppressed = next(e['details'] for e in events if e['kind']=='qq_suppressed')
        timing = next(e['details'] for e in events if e['kind']=='qq_reply_timing')
        assert suppressed['reason']==reason and suppressed['stage']=='after_generation'
        assert suppressed['assistant_message_id'] and suppressed['generated']
        assert timing['outcome']=='suppressed' and timing['reason']==reason and timing['confirmed_parts']==0
        assert timing['assistant_message_id']==suppressed['assistant_message_id']
        assert not any(e['kind']=='qq_outbox' for e in events)
        assert not flow._scope_jobs[scope]
    finally:
        await flow.close();store.close()


@pytest.mark.anyio
@pytest.mark.parametrize('acknowledged', [False, True])
async def test_delivery_outcome_distinguishes_all_confirmed_from_unknown_without_replay(tmp_path, acknowledged):
    store, config = Store(tmp_path), Config()
    scope='qq:group:202'
    store.upsert_scope({'id':scope,'plan_enabled':False,'merge_window_ms':0,'cooldown_seconds':0})
    chat=FixtureChat(store,config);sent=[]
    async def emit(*args):
        sent.append(args)
        return acknowledged
    flow=ConversationFlow(store,config,chat,emit,lambda *args:True)
    try:
        await flow.handle(scope,'303','Please answer',[],{'at_bot':True},direct=True)
        events=store.list_events(100)
        timing=next(e['details'] for e in events if e['kind']=='qq_reply_timing')
        receipt=next(e['details'] for e in events if e['kind']=='qq_outbox')
        assert timing['outcome']==('delivered' if acknowledged else 'unknown')
        assert len(sent)==(2 if acknowledged else 1)
        assert [p['status'] for p in receipt['parts']]==(['confirmed','confirmed'] if acknowledged else ['unknown','pending'])
        await asyncio.sleep(.02)
        assert len(sent)==(2 if acknowledged else 1)
    finally:
        await flow.close();store.close()


@pytest.mark.anyio
async def test_cancelled_generation_logs_reason_and_releases_flow(tmp_path):
    store,config=Store(tmp_path),Config();scope='qq:group:202'
    store.upsert_scope({'id':scope,'plan_enabled':False,'merge_window_ms':0,'cooldown_seconds':0})
    chat=FixtureChat(store,config);chat.block=asyncio.Event()
    async def emit(*args):
        pytest.fail('Cancelled generation must not send text')
    flow=ConversationFlow(store,config,chat,emit,lambda *args:True)
    try:
        await flow.handle(scope,'303','Please answer',[],{'at_bot':True},direct=True,wait=False)
        await asyncio.wait_for(chat.entered.wait(),2)
        await flow.close()
        timing=next(e['details'] for e in store.list_events(100) if e['kind']=='qq_reply_timing')
        assert timing['outcome']=='suppressed' and timing['reason']=='cancelled'
        assert not flow._scope_jobs[scope] and not flow._batches
    finally:
        await flow.close();store.close()
