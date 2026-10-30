"""Real Provider reservation paths with mock HTTP; no billed model requests."""
from copy import deepcopy
from datetime import datetime, timezone
import json

import httpx
import pytest

from qunyu.db import Store
from shiori.evaluation import load_replay_config, read_initial_usage, replay
from shiori.provider import estimate_tokens
from shiori.settings import DEFAULTS, Settings


@pytest.fixture
def anyio_backend():
    return 'asyncio'


def config():
    cfg = deepcopy(DEFAULTS)
    cfg['model'].update(base_url='https://mock.invalid/v1', model='mock')
    return cfg


def cases(count=10):
    return [{'id': f'case-{i}', 'scope_kind': 'private', 'scope': {'always_reply': True},
             'text': '你好，今天的音乐很有意思', 'expected': {'reply': True}} for i in range(count)]


def mock_transport(monkeypatch, *, timeout_first=False):
    sent = []
    original = httpx.AsyncClient
    def handler(request):
        body = json.loads(request.content)
        sent.append(body)
        if timeout_first and len(sent) == 1:
            raise httpx.ReadTimeout('test uncertain completion', request=request)
        return httpx.Response(200, json={'choices': [{'message': {'role': 'assistant', 'content': '模拟回复'}}],
            'usage': {'prompt_tokens': estimate_tokens(body['messages']), 'completion_tokens': body['max_tokens']}})
    monkeypatch.setattr(httpx, 'AsyncClient', lambda *a, **kw: original(*a, transport=httpx.MockTransport(handler), **kw))
    return sent


@pytest.mark.anyio
@pytest.mark.parametrize('limit_kind', ['tokens', 'cost'])
async def test_live_run_budget_accumulates_across_isolated_cases_and_initial_usage(monkeypatch, limit_kind):
    cfg = config()
    if limit_kind == 'tokens':
        cfg['budget'].update(daily_tokens=5000, daily_cost=0)
        initial = {'tokens': 1000}
    else:
        cfg['budget'].update(daily_tokens=0, daily_cost=5000)
        cfg['model'].update(input_price=1000000, output_price=1000000)
        initial = {'cost': 1000}
    sent = mock_transport(monkeypatch)
    report = await replay(cases(), config=cfg, live=True, generate=True, initial_usage=initial)
    assert 1 < len(sent) < 10
    assert report['summary']['budget_stopped']
    used = report['budget']['run_usage'][limit_kind]
    assert 0 < used <= 4000
    assert report['budget']['initial_usage'][limit_kind] == 1000
    assert report['records'][-1]['error'] == 'ReplayBudgetLimit'
    assert report['summary']['unrun_cases'] > 0
    assert report['summary']['decision_accuracy'] == 1
    assert cfg['budget']['daily_tokens' if limit_kind == 'tokens' else 'daily_cost'] == 5000


@pytest.mark.anyio
async def test_exactly_exhausted_positive_budget_never_turns_into_unlimited(monkeypatch):
    cfg = config()
    cfg['budget']['daily_tokens'] = 1000
    sent = mock_transport(monkeypatch)
    report = await replay(cases(), config=cfg, live=True, generate=True, initial_usage={'tokens': 1000})
    assert sent == []
    assert report['summary']['completed_cases'] == 0
    assert report['summary']['decision_accuracy'] is None
    assert report['summary']['tokens'] == 0
    assert report['summary']['budget_stopped'] and report['budget']['stopped_reason']


@pytest.mark.anyio
async def test_failed_unknown_request_usage_is_retained_and_limits_remaining_cases(monkeypatch):
    cfg = config()
    cfg['budget']['daily_tokens'] = 5000
    sent = mock_transport(monkeypatch, timeout_first=True)
    report = await replay(cases(), config=cfg, live=True, generate=True)
    first = report['records'][0]
    assert first['error'] == 'ProviderTimeoutError' and first['usage_unconfirmed']
    assert first['usage']['tokens'] > 0
    assert report['summary']['tokens'] == sum(x['usage']['tokens'] for x in report['records'])
    assert report['summary']['tokens'] <= 5000
    assert report['summary']['budget_stopped'] and 1 < len(sent) < 10


@pytest.mark.anyio
async def test_zero_configured_caps_remain_unlimited_with_nonzero_initial_usage(monkeypatch):
    cfg = config()
    cfg['budget'].update(daily_tokens=0, daily_cost=0)
    sent = mock_transport(monkeypatch)
    report = await replay(cases(3), config=cfg, live=True, generate=True,
                          initial_usage={'tokens': 999999, 'cost': 999999})
    assert len(sent) == 3
    assert not report['summary']['budget_stopped']
    assert report['summary']['completed_cases'] == 3


@pytest.mark.anyio
async def test_planner_budget_veto_is_not_misreported_as_semantic_skip(monkeypatch):
    cfg = config()
    cfg['identity']['respond_to_name'] = False
    cfg['budget']['daily_tokens'] = 1
    sent = mock_transport(monkeypatch)
    dataset = [{'id': 'planner', 'text': '初音未来，聊聊音乐', 'expected': {'reply': True}}]
    report = await replay(dataset, config=cfg, live=True)
    assert not sent
    assert report['records'][0]['error'] == 'ReplayBudgetLimit'
    assert report['summary']['completed_cases'] == 0
    assert report['summary']['decision_accuracy'] is None


def test_initial_usage_is_read_only_and_uses_hong_kong_day_boundaries(tmp_path):
    store = Store(tmp_path)
    for date, tokens in [('2026-10-15T15:59:59Z', 99), ('2026-10-15T16:00:00Z', 10),
                          ('2026-10-16T15:59:59Z', 20), ('2026-10-16T16:00:00Z', 88)]:
        store.record_usage({'scope_id': 'web:default', 'purpose': 'chat', 'model': 'mock',
                            'created_at': date, 'input_tokens': tokens, 'output_tokens': tokens, 'cost': tokens / 10})
    # Reading while the source is in WAL mode must see committed usage too.
    value = read_initial_usage(tmp_path, now=datetime(2026, 10, 16, 0, tzinfo=timezone.utc))
    assert value == {'input_tokens': 30, 'output_tokens': 30, 'tokens': 60, 'cost': 3.0}
    store.close()
    before = {p.name: p.read_bytes() for p in tmp_path.iterdir() if p.is_file()}
    assert read_initial_usage(tmp_path, now=datetime(2026, 10, 16, 0, tzinfo=timezone.utc)) == value
    assert {p.name: p.read_bytes() for p in tmp_path.iterdir() if p.is_file()} == before


def test_unreadable_usage_fails_closed_instead_of_assuming_zero(tmp_path):
    (tmp_path / 'qunyu.db').write_bytes(b'broken database')
    with pytest.raises(ValueError, match='只读'):
        read_initial_usage(tmp_path)
    assert (tmp_path / 'qunyu.db').read_bytes() == b'broken database'


def test_config_is_decoded_from_temporary_copy_without_repairing_original(tmp_path):
    settings = Settings(tmp_path)
    settings.update({'model': {'base_url': 'https://mock.invalid/v1', 'model': 'mock', 'api_key': 'local-test-secret'}})
    (tmp_path / 'admin.token').unlink()
    before = {p.name: p.read_bytes() for p in tmp_path.iterdir() if p.is_file()}
    loaded = load_replay_config(tmp_path)
    assert loaded['model']['api_key'] == 'local-test-secret'
    assert {p.name: p.read_bytes() for p in tmp_path.iterdir() if p.is_file()} == before


@pytest.mark.anyio
@pytest.mark.parametrize('value', [[], {'tokens': -1}, {'tokens': True}, {'cost': float('nan')}, {'input_tokens': 'bad'}])
async def test_invalid_initial_usage_is_rejected_before_any_provider(monkeypatch, value):
    sent = mock_transport(monkeypatch)
    with pytest.raises(ValueError, match='初始用量'):
        await replay(cases(1), config=config(), live=True, generate=True, initial_usage=value)
    assert not sent


@pytest.mark.anyio
async def test_fractional_token_cap_cannot_round_to_unlimited(monkeypatch):
    cfg = config()
    cfg['budget']['daily_tokens'] = 0.5
    sent = mock_transport(monkeypatch)
    with pytest.raises(ValueError, match='预算配置'):
        await replay(cases(1), config=cfg, live=True, generate=True)
    assert not sent
