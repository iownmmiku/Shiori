"""Jev pre-screen (prescreen.py): settings, the client, and how it sits in
front of the Planner. A screen may only ever skip unprompted messages, and
any doubt or failure must leave the Planner's decision untouched."""
import asyncio
import json
import time
from copy import deepcopy

import httpx
import pytest

from shiori import prescreen as ps
from shiori.db import Store
from shiori.runtime_logs import summary
from shiori.settings import DEFAULTS, Settings as SettingsStore
from shiori.social import SocialEngine

GROUP = 'qq:group:202'
SOURCE = {'id': 'jev', 'name': 'Jev 中转', 'base_url': 'https://relay.test', 'api_key': 'test-key-abcdef', 'enabled': True}


@pytest.fixture
def anyio_backend():
    return 'asyncio'


def jev(p_bot, levels=(0.9, 0.05, 0.03, 0.02), audience='member'):
    return {'model': 'jev-1.14.0', 'usage': {'input_tokens': 512, 'output_tokens': 20}, 'answers': {
        'to_bot': {'type': 'noul', 'noul': p_bot},
        'audience': {'type': 'choice', 'choice': audience, 'confidence': 0.5,
                     'probabilities': {'bot': 0.1, 'member': 0.7, 'group': 0.1, 'none': 0.1}},
        'worth': {'type': 'score', 'score': sum(i * p for i, p in enumerate(levels)), 'confidence': 0.4,
                  'legend': {str(i): 'x' for i in range(4)}, 'probabilities': {str(i): p for i, p in enumerate(levels)}}}}


class Settings:
    def __init__(self, mode='screen'):
        self.config = deepcopy(DEFAULTS)
        self.config['model'].update(base_url='https://test.invalid/v1', model='mock')
        self.config['onebot'].update(enabled=True, chat_policy='blacklist')
        self.config['planner'].update(min_interval_ms=0)
        self.config['time']['awareness_enabled'] = False
        self.config['provider_sources'] = [dict(SOURCE)]
        self.config['prescreen'] = dict(DEFAULTS['prescreen'], mode=mode, source_id='jev')

    def get(self):
        return deepcopy(self.config)


class Planner:
    def __init__(self, delay=0.0):
        self.calls, self.delay = [], delay

    async def complete(self, messages, **kwargs):
        self.calls.append(messages)
        if self.delay:
            await asyncio.sleep(self.delay)
        payload = json.loads(messages[-1]['content'])
        return {'content': json.dumps({'action': 'reply', 'target_message_id': payload['current_message_id'],
                                       'target_user_id': payload['current_user_id'], 'addressed_to_bot': False,
                                       'audience': 'group', 'intent': '接一句', 'reason': '理由', 'needed_info': [],
                                       'value': 90}, ensure_ascii=False)}


class Jev:
    """MockTransport handler recording what reached the relay."""
    def __init__(self, answer=None, status=200, delay=0.0):
        self.requests, self.answer, self.status, self.delay = [], answer or jev(0.05), status, delay

    async def __call__(self, request):
        self.requests.append(request)
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.status != 200:
            return httpx.Response(self.status, json={'detail': 'upstream error'})
        return httpx.Response(200, json=self.answer)


@pytest.fixture
def setup(tmp_path):
    store = Store(tmp_path)
    store.upsert_scope({'id': GROUP, 'cooldown_seconds': 0, 'merge_window_ms': 0})
    yield store
    store.close()


def engine(store, settings, planner, relay):
    social = SocialEngine(store, settings, planner)
    social.prescreen._client = httpx.AsyncClient(transport=httpx.MockTransport(relay))
    return social


def say(store, user, text, name='阿九', **metadata):
    return store.add_message(GROUP, user, 'user', text, sender_name=name, **metadata)


def events(store, kind):
    return [item['details'] for item in store.list_events(50) if item['kind'] == kind]


def test_endpoint_and_validation():
    assert ps.endpoint('https://relay.test') == 'https://relay.test/v1/systemone'
    assert ps.endpoint('https://relay.test/v1/') == 'https://relay.test/v1/systemone'
    assert ps.endpoint('https://relay.test/api/jev/systemone') == 'https://relay.test/api/jev/systemone'
    value = ps.validate({'mode': 'screen', 'source_id': 'jev'}, {'jev'})
    assert value['mode'] == 'screen' and value['model'] == 'jev-1.14.0' and value['auth_prefix'] == 'Bearer '
    # A missing connection turns it off instead of breaking the settings file.
    assert ps.validate({'mode': 'screen', 'source_id': 'gone'}, {'jev'})['mode'] == 'off'
    assert ps.validate({'mode': 'shadow', 'source_id': ''}, set())['mode'] == 'off'
    for bad in ({'mode': 'always'}, {'auth_header': 'Bad Header'}, {'auth_prefix': 'x\n'}, {'timeout_ms': 100},
                {'max_bot_probability': 1.5}, {'max_join_probability': True}, {'model': ''}, {'unknown': 1}):
        with pytest.raises(ValueError):
            ps.validate(bad, {'jev'})


def test_answers_must_be_typesafe_format():
    parsed = ps.parse_answers(jev(0.2, (0.6, 0.3, 0.05, 0.05)))
    assert parsed['p_bot'] == 0.2 and parsed['p_join'] == 0.4 and parsed['p_request'] == 0.1 and parsed['audience'] == 'member'
    for bad in ({'choices': [{'message': {'content': '{}'}}]}, {'answers': {'to_bot': {'noul': 0.1}}},
                {'answers': {**jev(0.1)['answers'], 'to_bot': {'noul': 7}}}):
        with pytest.raises(ValueError):
            ps.parse_answers(bad)


def test_settings_store_keeps_the_key_in_the_provider_source(tmp_path):
    store = SettingsStore(tmp_path)
    assert store.get()['prescreen']['mode'] == 'off'
    store.update({'provider_sources': [dict(SOURCE)]})
    store.update({'prescreen': {'mode': 'shadow', 'source_id': 'jev', 'model': 'typesafe/jev', 'max_join_probability': 0.25}})
    public = store.public()
    assert public['prescreen']['mode'] == 'shadow' and public['prescreen']['model'] == 'typesafe/jev'
    assert 'test-key-abcdef' not in json.dumps(public) and public['provider_sources'][0]['api_key_set']
    assert 'test-key-abcdef' not in (tmp_path / 'settings.json').read_text('utf-8')
    reloaded = SettingsStore(tmp_path)
    assert reloaded.get()['prescreen']['max_join_probability'] == 0.25 and not reloaded.warning
    # Removing the connection switches the pre-screen off.
    reloaded.update({'provider_sources': []})
    assert reloaded.get()['prescreen']['mode'] == 'off' and reloaded.get()['prescreen']['source_id'] == ''


@pytest.mark.anyio
async def test_screen_mode_skips_the_planner_only_on_a_clear_no(setup):
    store, relay, planner = setup, Jev(jev(0.05)), Planner()
    social = engine(store, Settings('screen'), planner, relay)
    say(store, '303', '今天打本吗')
    result = await social.plan(store.get_scope(GROUP), say(store, '404', '打，等我吃完饭', name='小林'))
    assert not result['reply'] and '快速预筛' in result['reason'] and not planner.calls
    sent = json.loads(relay.requests[0].content)
    assert relay.requests[0].headers['authorization'] == 'Bearer test-key-abcdef'
    assert str(relay.requests[0].url) == 'https://relay.test/v1/systemone' and sent['model'] == 'jev-1.14.0'
    assert sent['state']['latest']['text'] == '打，等我吃完饭' and sent['state']['history'][-1]['text'] == '今天打本吗'
    assert set(sent['questions']) == {'to_bot', 'audience', 'worth'}
    verdict = events(store, 'planner_prescreen')[0]['prescreen']
    assert verdict['ok'] and verdict['screened'] and verdict['mode'] == 'screen'
    await social.close()


@pytest.mark.anyio
async def test_screen_mode_hands_doubt_and_failures_to_the_planner(setup):
    store, planner = setup, Planner()
    relay = Jev(jev(0.05, (0.5, 0.3, 0.1, 0.1)))  # "could join in" at 0.5: not a clear no
    social = engine(store, Settings('screen'), planner, relay)
    result = await social.plan(store.get_scope(GROUP), say(store, '303', '有人知道流星雨几点吗'))
    assert len(planner.calls) == 1 and result['reply']
    timing = events(store, 'planner_timing')[0]
    assert timing['prescreen']['ok'] and not timing['prescreen']['screened']
    for broken in (Jev(status=500), Jev(answer={'choices': []})):
        social.prescreen._client = httpx.AsyncClient(transport=httpx.MockTransport(broken))
        await social.plan(store.get_scope(GROUP), say(store, '303', '还有人吗'))
        assert events(store, 'planner_timing')[0]['prescreen']['ok'] is False
    assert len(planner.calls) == 3
    await social.close()


@pytest.mark.anyio
async def test_screen_mode_times_out_into_the_planner(setup):
    store, planner = setup, Planner()
    settings = Settings('screen')
    settings.config['prescreen']['timeout_ms'] = 300
    social = engine(store, settings, planner, Jev(delay=1.0))
    await social.plan(store.get_scope(GROUP), say(store, '303', '在吗'))
    assert len(planner.calls) == 1 and events(store, 'planner_timing')[0]['prescreen']['error'] == 'timeout'
    await social.close()


@pytest.mark.anyio
async def test_shadow_mode_records_but_never_decides(setup):
    store, planner = setup, Planner(delay=0.05)
    social = engine(store, Settings('shadow'), planner, Jev(jev(0.02)))
    result = await social.plan(store.get_scope(GROUP), say(store, '303', '哈哈哈哈'))
    assert len(planner.calls) == 1 and result['reply']  # a "screened" shadow verdict changes nothing
    verdict = events(store, 'planner_timing')[0]['prescreen']
    assert verdict['mode'] == 'shadow' and verdict['screened']
    await social.close()


@pytest.mark.anyio
async def test_a_slow_shadow_answer_is_recorded_when_it_lands(setup):
    store, planner = setup, Planner()
    social = engine(store, Settings('shadow'), planner, Jev(jev(0.02), delay=0.1))
    await social.plan(store.get_scope(GROUP), say(store, '303', '哈哈哈哈'))
    assert 'prescreen' not in events(store, 'planner_timing')[0]
    await asyncio.sleep(0.3)
    late = events(store, 'planner_prescreen')[0]
    assert late['planner_outcome'] == 'reply' and late['prescreen']['screened'] and late['gate']['value'] == 90
    await social.close()


@pytest.mark.anyio
async def test_messages_said_to_the_bot_never_go_through_the_screen(setup):
    store, planner, relay = setup, Planner(), Jev(jev(0.01))
    social = engine(store, Settings('screen'), planner, relay)
    await social.plan(store.get_scope(GROUP), say(store, '303', '林星遥你在吗', at_bot=True), direct=True)
    await social.plan(store.get_scope(GROUP), say(store, '303', '回我一下', reply_to_bot=True), reply_to_bot=True)
    assert not relay.requests
    await social.close()


@pytest.mark.anyio
async def test_off_or_unconfigured_never_calls_the_relay(setup):
    store, relay = setup, Jev()
    for mode, key in (('off', SOURCE['api_key']), ('screen', '')):
        settings = Settings(mode)
        settings.config['provider_sources'][0]['api_key'] = key
        planner = Planner()
        social = engine(store, settings, planner, relay)
        await social.plan(store.get_scope(GROUP), say(store, '303', '随便聊聊'))
        assert len(planner.calls) == 1
        await social.close()
    assert not relay.requests


@pytest.mark.anyio
async def test_connection_test_sends_only_a_made_up_conversation(tmp_path):
    store = SettingsStore(tmp_path)
    store.update({'provider_sources': [dict(SOURCE, base_url='https://relay.test/custom/systemone')]})
    relay = Jev(jev(0.1, (0.2, 0.3, 0.4, 0.1)))
    client = ps.Prescreen(store)
    client._client = httpx.AsyncClient(transport=httpx.MockTransport(relay))
    result = await client.test({'source_id': 'jev', 'auth_header': 'x-api-key', 'auth_prefix': ''})
    sent = json.loads(relay.requests[0].content)
    assert result['ok'] and result['url'] == 'https://relay.test/custom/systemone'
    assert relay.requests[0].headers['x-api-key'] == 'test-key-abcdef'
    assert sent['state']['latest']['text'] == ps.PROBE['latest']['text']
    with pytest.raises(ValueError):
        await client.test({'source_id': ''})
    await client.close()


def test_runtime_log_lines_show_the_verdict():
    verdict = {'ok': True, 'mode': 'screen', 'p_bot': 0.04, 'p_join': 0.12, 'audience': 'member', 'screened': True, 'latency_ms': 420}
    line = summary('planner_prescreen', {'prescreen': verdict})
    assert '未调用 Planner' in line and '对它说 0.04' in line and '筛掉' in line and '420ms' in line
    shadow = summary('planner_timing', {'outcome': 'skip', 'gate': {'value': 20, 'threshold': 82.5},
                                        'prescreen': dict(verdict, mode='shadow')})
    assert 'Jev（影子）' in shadow and '会筛掉' in shadow
    failed = summary('planner_timing', {'outcome': 'skip', 'prescreen': {'ok': False, 'error': 'timeout', 'latency_ms': 2500}})
    assert 'Jev 未完成：timeout' in failed


@pytest.mark.anyio
async def test_repeated_failures_pause_the_screen(setup):
    store, planner, relay = setup, Planner(), Jev(status=500)
    social = engine(store, Settings('screen'), planner, relay)
    for n in range(ps.FAILURES_BEFORE_PAUSE):
        assert not social.prescreen.paused()
        await social.plan(store.get_scope(GROUP), say(store, '303', f'还有人吗 {n}'))
    assert len(relay.requests) == ps.FAILURES_BEFORE_PAUSE and social.prescreen.paused()
    last = events(store, 'planner_timing')[0]['prescreen']
    assert last['paused_seconds'] == ps.PAUSE_SECONDS and '暂停 60 秒' in summary('planner_timing', {'prescreen': last})
    # Paused: the Planner is asked straight away and the relay is left alone.
    await social.plan(store.get_scope(GROUP), say(store, '303', '在不在'))
    assert len(relay.requests) == ps.FAILURES_BEFORE_PAUSE and len(planner.calls) == ps.FAILURES_BEFORE_PAUSE + 1
    assert 'prescreen' not in events(store, 'planner_timing')[0]
    # Once the pause is over it is tried again, and one good answer clears the count.
    social.prescreen._paused_until = 0.0
    social.prescreen._client = httpx.AsyncClient(transport=httpx.MockTransport(Jev(jev(0.05))))
    result = await social.plan(store.get_scope(GROUP), say(store, '303', '我去吃饭了'))
    assert not result['reply'] and '快速预筛' in result['reason'] and social.prescreen._failures == 0
    await social.close()


@pytest.mark.anyio
async def test_a_good_answer_between_failures_starts_the_count_again(setup):
    store, planner = setup, Planner()
    social = engine(store, Settings('shadow'), planner, Jev(status=500))
    for relay in (Jev(status=500), Jev(status=500), Jev(jev(0.05)), Jev(status=500), Jev(status=500)):
        social.prescreen._client = httpx.AsyncClient(transport=httpx.MockTransport(relay))
        await social.plan(store.get_scope(GROUP), say(store, '303', '还有人吗'))
        await asyncio.sleep(0.05)
    assert not social.prescreen.paused() and social.prescreen._failures == 2
    await social.close()


@pytest.mark.anyio
async def test_a_failed_connection_test_does_not_pause_live_traffic(tmp_path):
    store = SettingsStore(tmp_path)
    store.update({'provider_sources': [dict(SOURCE)]})
    client = ps.Prescreen(store)
    client._client = httpx.AsyncClient(transport=httpx.MockTransport(Jev(status=500)))
    for _ in range(ps.FAILURES_BEFORE_PAUSE + 1):
        assert not (await client.test({'source_id': 'jev'}))['ok']
    assert not client.paused()
    # A successful test lifts a pause that live traffic caused.
    client._paused_until = time.monotonic() + 60
    client._client = httpx.AsyncClient(transport=httpx.MockTransport(Jev(jev(0.1))))
    assert (await client.test({'source_id': 'jev'}))['ok'] and not client.paused()
    await client.close()


def test_failures_are_named_without_the_relays_own_text():
    label = ps.failure_label
    assert label({'error': 'timeout'}) == '超时'
    assert label({'error': 'ConnectError'}) == '连接失败'
    assert label({'error': '回答缺少 noul、choice、score 或概率字段', 'status': 200}) == '回答格式不符'
    assert label({'error': 'HTTP 403', 'status': 403,
                  'error_detail': '{"code":"INSUFFICIENT_BALANCE","message":"Insufficient account balance"}'}) == '余额不足'
    assert label({'error': 'HTTP 403', 'status': 403,
                  'error_detail': '{"error":{"message":"insufficient balance","type":"billing_error"}}'}) == '余额不足'
    assert label({'error': 'HTTP 402', 'status': 402}) == '余额不足'
    assert label({'error': 'HTTP 403', 'status': 403, 'error_detail': 'forbidden'}) == '密钥无效或无权限'
    assert label({'error': 'HTTP 401', 'status': 401, 'error_detail': 'bad key sk-example'}) == '密钥无效或无权限'
    assert label({'error': 'HTTP 429', 'status': 429}) == '服务限流'
    assert label({'error': 'HTTP 502', 'status': 502, 'error_detail': '<html>bad gateway</html>'}) == '服务返回 502'


@pytest.mark.anyio
async def test_live_results_reach_the_listener(setup):
    store, planner, seen = setup, Planner(), []
    social = engine(store, Settings('shadow'), planner, Jev(status=403))
    social.prescreen.on_result = lambda ok, reason='': seen.append((ok, reason))
    await social.plan(store.get_scope(GROUP), say(store, '303', '还有人吗'))
    await asyncio.sleep(0.05)
    social.prescreen._client = httpx.AsyncClient(transport=httpx.MockTransport(Jev(jev(0.05))))
    await social.plan(store.get_scope(GROUP), say(store, '303', '有人在吗'))
    await asyncio.sleep(0.05)
    assert seen == [(False, '密钥无效或无权限'), (True, '')]
    await social.close()


@pytest.mark.anyio
async def test_a_connection_test_is_not_reported_and_a_broken_listener_changes_nothing(tmp_path):
    store = SettingsStore(tmp_path)
    store.update({'provider_sources': [dict(SOURCE)]})
    client, seen = ps.Prescreen(store), []
    client.on_result = lambda ok, reason='': seen.append(ok)
    client._client = httpx.AsyncClient(transport=httpx.MockTransport(Jev(status=500)))
    assert not (await client.test({'source_id': 'jev'}))['ok'] and not seen

    def broken(ok, reason=''):
        raise RuntimeError('listener failed')
    client.on_result = broken
    cfg = client.config({**store.get(), 'prescreen': dict(ps.DEFAULTS, mode='shadow', source_id='jev')})
    assert (await client.judge(ps.PROBE, cfg))['error'] == 'HTTP 500'
    await client.close()


@pytest.mark.anyio
async def test_the_connection_is_kept_open_between_messages(tmp_path):
    client = ps.Prescreen(SettingsStore(tmp_path))
    # httpx's default is 5 seconds, shorter than the usual gap between group messages.
    assert client._http()._transport._pool._keepalive_expiry == ps.KEEPALIVE_SECONDS >= 60
    await client.close()
