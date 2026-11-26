"""A model is paused only for failures of the model, not for one request's used-up output allowance."""
import asyncio

import httpx
import pytest

from shiori.db import Store
from shiori.provider import Provider, ProviderContentFilterError, ProviderError, ProviderOutputBudgetError
from shiori.settings import Settings


def body(content, finish='stop'):
    return {'choices': [{'message': {'content': content}, 'finish_reason': finish}],
            'usage': {'prompt_tokens': 12, 'completion_tokens': 30}}


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    store, settings = Store(tmp_path), Settings(tmp_path)
    settings.update({'model': {'base_url': 'https://fixture.invalid/v1', 'model': 'fixture', 'stream_mode': 'off'}})
    provider, answers, requests = Provider(settings, store), [], []
    original = httpx.AsyncClient

    def handler(request):
        requests.append(request)
        status, payload = answers.pop(0)
        return httpx.Response(status, json=payload)
    monkeypatch.setattr(httpx, 'AsyncClient', lambda **kw: original(transport=httpx.MockTransport(handler), **kw))
    yield provider, answers, requests
    store.close()


def call(provider, purpose):
    return asyncio.run(provider.complete([{'role': 'user', 'content': 'hello'}], purpose=purpose))


@pytest.mark.parametrize('finish,error', [('length', ProviderOutputBudgetError), ('content_filter', ProviderContentFilterError)])
def test_used_up_allowances_do_not_pause_the_model_for_other_tasks(runtime, finish, error):
    provider, answers, requests = runtime
    answers.extend([(200, body('', finish)), (200, body('', finish)), (200, body('{"action":"skip"}'))])
    for _ in range(2):
        with pytest.raises(error):
            call(provider, 'jargon_learning')
    assert call(provider, 'planner')['content'] == '{"action":"skip"}'
    assert len(requests) == 3
    assert all(not state.get('until') for state in provider._health.values())


def test_repeated_upstream_failures_still_pause_the_model_and_say_so(runtime):
    provider, answers, requests = runtime
    answers.extend([(500, {'error': 'upstream'}), (500, {'error': 'upstream'})])
    for _ in range(2):
        with pytest.raises(ProviderError) as failure:
            call(provider, 'jargon_learning')
        assert failure.value.status_code == 500
    with pytest.raises(ProviderError) as paused:
        call(provider, 'planner')
    assert len(requests) == 2 and paused.value.circuit_open is True


def test_an_upstream_rate_limit_carries_its_status(runtime):
    provider, answers, _ = runtime
    answers.extend([(429, {'error': 'slow down'}), (429, {'error': 'slow down'})])
    with pytest.raises(ProviderError) as failure:
        call(provider, 'planner')
    assert failure.value.status_code == 429
