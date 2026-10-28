"""Error feedback must not confuse uncertain accounting with a budget veto."""
import asyncio

import httpx
import pytest

from shiori.db import Store
from shiori.model_catalog import ModelCatalog
from shiori.provider import (Provider, ProviderError, ProviderBudgetExceededError,
    ProviderTimeoutError, ProviderConnectionError, ProviderRateLimitError, is_budget_exceeded)
from shiori.settings import Settings, resolve_model
from shiori.social import _failure_feedback


@pytest.mark.parametrize('error,word,budget', [
    (ProviderTimeoutError('模型响应超时，已保留预算估算'), '超时', False),
    (ProviderTimeoutError('预算不足'), '超时', False),
    (ProviderConnectionError('模型连接中断，已保留预算估算'), '连接', False),
    (ProviderConnectionError('额度不足'), '连接', False),
    (ProviderRateLimitError('服务限流或额度不足'), '服务商', False),
    (ProviderBudgetExceededError('a structured veto'), '不足', True),
    (ProviderError('模型响应超时，本次消费无法确认，已保留预算估算，请稍后重试。'), '超时', False),
    (ProviderError('模型连接中断，本次消费无法确认，已保留预算估算。'), '连接', False),
    (ProviderError('request timed out; unknown completion'), '超时', False),
    (ProviderError('TimeoutException'), '超时', False),
    (ValueError('今日 Token 预算不足'), '不足', True),
    (ValueError('今日费用预算已用完'), '不足', True),
    (ValueError('当前会话今日 Planner 次数已达上限'), '不足', True),
    (ValueError('本次预算预留已失效'), '没能完成', False),
])
@pytest.mark.parametrize('planner', [True, False])
def test_feedback_uses_error_type_and_preserves_legacy_budget_compatibility(error, word, budget, planner):
    assert is_budget_exceeded(error) is budget
    feedback = _failure_feedback(error, planner=planner)
    assert word in feedback
    assert ('今天的对话预算或次数已不足' in feedback) is budget


def test_feedback_never_relays_upstream_credentials_or_tracebacks():
    secret = 'https://private.invalid/v1?api_key=private-key Traceback'
    for cls in (ProviderError, ProviderTimeoutError, ProviderConnectionError, ProviderRateLimitError, ProviderBudgetExceededError):
        text = _failure_feedback(cls(secret))
        assert 'private.invalid' not in text and 'private-key' not in text and 'Traceback' not in text


@pytest.mark.parametrize('failure,expected,attempts,uncertain', [
    (httpx.ReadTimeout, ProviderTimeoutError, 1, True),
    (httpx.ReadError, ProviderConnectionError, 1, True),
    (httpx.ConnectTimeout, ProviderTimeoutError, 2, False),
    (httpx.ConnectError, ProviderConnectionError, 2, False),
])
def test_transport_errors_are_typed_and_unknown_completions_are_not_retried(tmp_path, monkeypatch, failure, expected, attempts, uncertain):
    store=Store(tmp_path);settings=Settings(tmp_path)
    settings.update({'model':{'base_url':'https://fixture.invalid/v1','model':'chat'}})
    requests=[]
    def handler(request):
        requests.append(request)
        raise failure('upstream private body',request=request)
    original=httpx.AsyncClient
    monkeypatch.setattr(httpx,'AsyncClient',lambda **kwargs:original(transport=httpx.MockTransport(handler),**kwargs))
    provider=Provider(settings,store)
    try:
        with pytest.raises(expected) as captured:
            asyncio.run(provider.complete([{'role':'user','content':'你好'}]))
        assert not is_budget_exceeded(captured.value)
        assert len(requests)==attempts and not provider._reservations
        usage=store.list_usage()
        assert bool(usage) is uncertain
        if uncertain: assert usage[0]['unconfirmed'] and usage[0]['estimated']
    finally:store.close()


@pytest.mark.parametrize('seconds', [None, 5, 240, 600])
def test_saved_model_timeout_reaches_actual_http_request(tmp_path, monkeypatch, seconds):
    store=Store(tmp_path);settings=Settings(tmp_path)
    model={'base_url':'https://fixture.invalid/v1','model':'chat'}
    if seconds is not None:model['timeout_seconds']=seconds
    settings.update({'model':model})
    seen=[]
    def handler(request):
        seen.append(request.extensions['timeout'])
        return httpx.Response(200,json={'choices':[{'message':{'content':'你好'}}]})
    original=httpx.AsyncClient
    monkeypatch.setattr(httpx,'AsyncClient',lambda **kwargs:original(transport=httpx.MockTransport(handler),**kwargs))
    try:
        asyncio.run(Provider(settings,store).complete([{'role':'user','content':'你好'}]))
        value=90 if seconds is None else seconds
        assert seen==[{'connect':min(12,value),'read':value,'write':value,'pool':value}]
        assert Settings(tmp_path).get()['model']['timeout_seconds']==value
    finally:store.close()


@pytest.mark.parametrize('invalid', [True, 0, -1, 601, 90.5, '90', None, float('nan'), float('inf')])
def test_invalid_timeouts_are_rejected_without_overwriting_settings(tmp_path, invalid):
    settings=Settings(tmp_path)
    for patch in ({'model':{'timeout_seconds':invalid}}, {'providers':[{'id':'slow','timeout_seconds':invalid}]}):
        with pytest.raises(ValueError,match='模型等待超时'):settings.update(patch)
        assert settings.get()['model']['timeout_seconds']==90 and not settings.get()['providers']


def test_model_catalog_roundtrip_and_independent_timeouts(tmp_path):
    settings=Settings(tmp_path);catalog=ModelCatalog(settings,None)
    source=catalog.save_source({'name':'本地测试','base_url':'https://fixture.invalid/v1'})
    models=catalog.add_models({'provider_id':source['id'],'models':['fast','slow']})['added']
    assert all(item['timeout_seconds']==90 for item in models)
    slow=models[1]['id']
    catalog.update_model(slow,{'timeout_seconds':240})
    catalog.update_model('default',{'timeout_seconds':120})
    current=Settings(tmp_path).get()
    assert resolve_model(current,model_id=slow)['timeout_seconds']==240
    assert resolve_model(current,model_id=models[0]['id'])['timeout_seconds']==90
    assert current['model']['timeout_seconds']==120


def test_local_budget_veto_is_typed_and_never_dispatches(tmp_path, monkeypatch):
    settings=Settings(tmp_path);store=Store(tmp_path)
    settings.update({'model':{'base_url':'https://fixture.invalid/v1','model':'chat'},'budget':{'daily_tokens':1}})
    monkeypatch.setattr(httpx,'AsyncClient',lambda **kw:pytest.fail('budget veto must occur before dispatch'))
    try:
        with pytest.raises(ProviderBudgetExceededError):
            asyncio.run(Provider(settings,store).complete([{'role':'user','content':'你好'}]))
        assert store.list_usage()==[]
    finally:store.close()


def test_http_429_is_upstream_throttling_and_not_local_budget_exhaustion(tmp_path, monkeypatch):
    store=Store(tmp_path);settings=Settings(tmp_path)
    settings.update({'model':{'base_url':'https://fixture.invalid/v1','model':'chat'}})
    original=httpx.AsyncClient;seen=[]
    def handler(request):
        seen.append(request)
        return httpx.Response(429,json={'error':{'message':'upstream private body'}})
    monkeypatch.setattr(httpx,'AsyncClient',lambda **kwargs:original(transport=httpx.MockTransport(handler),**kwargs))
    try:
        with pytest.raises(ProviderRateLimitError) as captured:
            asyncio.run(Provider(settings,store).complete([{'role':'user','content':'你好'}]))
        assert len(seen)==2 and not is_budget_exceeded(captured.value)
        assert '服务商' in _failure_feedback(captured.value) and not store.list_usage()
        assert 'upstream private body' not in str(captured.value)
    finally:store.close()
