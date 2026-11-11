"""Failure alerts (alerts.py): the administrators hear about an outage once it
is real, hear when it is over, and are never flooded or sent chat content."""
import asyncio
from copy import deepcopy

import pytest

from shiori import alerts as al
from shiori.api import create_app
from shiori.db import Store
from shiori.provider import (Provider, ProviderBudgetExceededError, ProviderConnectionError, ProviderContentFilterError,
                             ProviderError, ProviderRateLimitError, ProviderTimeoutError, service_failure)
from shiori.runtime_logs import severity, summary
from shiori.settings import DEFAULTS, Settings as SettingsStore


@pytest.fixture
def anyio_backend():
    return 'asyncio'


class Settings:
    def __init__(self, **alerts):
        self.config = deepcopy(DEFAULTS)
        self.config['alerts'] = dict(al.DEFAULTS, enabled=True, **alerts)
        self.config['access']['administrators'] = [
            {'platform': 'onebot', 'account_id': '10001', 'instance_id': '', 'label': '管理员'},
            {'platform': 'qqofficial', 'account_id': 'openid-x', 'instance_id': '', 'label': '官方平台管理员'}]

    def get(self):
        return deepcopy(self.config)


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


class Outbox:
    def __init__(self, ok=True):
        self.sent, self.ok = [], ok

    async def __call__(self, scope_id, text):
        self.sent.append((scope_id, text))
        return self.ok


@pytest.fixture
def setup(tmp_path):
    store = Store(tmp_path)
    settings, clock, outbox = Settings(), Clock(), Outbox()
    yield al.Alerts(settings, store, outbox, clock=clock), settings, store, clock, outbox
    store.close()


def events(store):
    return [(item['message'], item['details']) for item in store.list_events(50) if item['kind'] == 'alert'][::-1]


async def settle(alerts):
    if alerts._jobs:
        await asyncio.gather(*list(alerts._jobs))


def test_settings_section_is_validated(tmp_path):
    assert al.validate({}) == al.DEFAULTS and al.DEFAULTS['enabled'] is False
    for bad in ({'enabled': 'yes'}, {'model_failures': 1}, {'model_minutes': -1}, {'repeat_minutes': 2}, {'unknown': 1}):
        with pytest.raises(ValueError):
            al.validate(bad)
    store = SettingsStore(tmp_path)
    assert store.get()['alerts'] == al.DEFAULTS
    assert store.update({'alerts': {'enabled': True, 'model_failures': 3}})['alerts']['model_failures'] == 3
    with pytest.raises(ValueError):
        store.update({'alerts': {'repeat_minutes': 0}})


@pytest.mark.anyio
async def test_a_model_outage_is_reported_once_it_is_real_and_again_when_it_ends(setup):
    alerts, _, store, clock, outbox = setup
    for _ in range(5):                       # five failures within seconds: a blip, not an outage yet
        alerts.model_result('m1', 'gemini', False, '连接失败')
        clock.now += 5
    assert not events(store)
    clock.now += 120
    alerts.model_result('m1', 'gemini', False, '连接失败')
    await settle(alerts)
    assert len(outbox.sent) == 1 and outbox.sent[0][0] == 'qq:private:10001'   # only the QQ administrator
    text = outbox.sent[0][1]
    assert text.startswith(al.PREFIX) and '「gemini」' in text and '连续失败 6 次' in text and '连接失败' in text
    alerts.model_result('m1', 'gemini', False, '连接失败')    # still down: not repeated before the interval
    clock.now += 31 * 60
    alerts.model_result('m1', 'gemini', False, '超时')
    await settle(alerts)
    assert len(outbox.sent) == 2 and '仍未恢复' in outbox.sent[1][1]
    clock.now += 60
    alerts.model_result('m1', 'gemini', True)
    await settle(alerts)
    assert len(outbox.sent) == 3 and '已恢复' in outbox.sent[2][1] and '分钟' in outbox.sent[2][1]
    alerts.model_result('m1', 'gemini', True)    # a healthy model stays quiet
    await settle(alerts)
    assert len(outbox.sent) == 3 and [details['alert'] for _, details in events(store)] == ['model_down', 'model_down', 'model_recovered']


@pytest.mark.anyio
async def test_a_prescreen_outage_is_reported_and_says_replies_go_on(setup):
    alerts, _, store, clock, outbox = setup
    for _ in range(6):                       # its breaker lets about three calls a minute through
        alerts.prescreen_result(False, '余额不足')
        clock.now += 20
    assert not events(store)
    clock.now += 30
    alerts.prescreen_result(False, '余额不足')
    await settle(alerts)
    assert len(outbox.sent) == 1
    text = outbox.sent[0][1]
    assert '快速预筛已连续失败 7 次' in text and '（余额不足）' in text and '回复不受影响' in text and '无法回复' not in text
    alerts.model_result('m1', 'gemini', True)     # a working model says nothing about the pre-screen
    await settle(alerts)
    assert len(outbox.sent) == 1
    alerts.prescreen_result(True)
    await settle(alerts)
    assert len(outbox.sent) == 2 and '快速预筛已恢复' in outbox.sent[1][1]
    assert [details['alert'] for _, details in events(store)] == ['prescreen_down', 'prescreen_recovered']


@pytest.mark.anyio
async def test_scattered_failures_and_a_switched_off_monitor_stay_quiet(setup):
    alerts, settings, store, clock, outbox = setup
    for _ in range(20):                      # every other call fails, for an hour
        alerts.model_result('m1', 'gemini', False, '超时')
        clock.now += 90
        alerts.model_result('m1', 'gemini', True)
        clock.now += 90
    settings.config['alerts']['enabled'] = False
    for _ in range(10):
        alerts.model_result('m1', 'gemini', False, '超时')
        clock.now += 60
    await settle(alerts)
    assert not outbox.sent and not events(store)
    settings.config['alerts']['enabled'] = True      # switched on in the middle of the outage
    alerts.model_result('m1', 'gemini', False, '超时')
    await settle(alerts)
    assert len(outbox.sent) == 1


@pytest.mark.anyio
async def test_a_lost_qq_link_is_reported_when_it_is_back(setup):
    alerts, _, store, clock, outbox = setup
    alerts.link(True)                         # first connection after start
    alerts.link(False)
    clock.now += 30
    alerts.link(True)                         # a short reconnect is routine
    await settle(alerts)
    assert not outbox.sent
    alerts.link(False)
    alerts.link(False)
    clock.now += 600
    alerts.link(True)
    await settle(alerts)
    assert len(outbox.sent) == 1 and '中断约 10 分钟' in outbox.sent[0][1]


@pytest.mark.anyio
async def test_long_stalls_are_reported_and_the_daily_cap_holds(setup):
    alerts, settings, store, clock, outbox = setup
    alerts.stalled(3.0)
    await settle(alerts)
    assert not outbox.sent
    for _ in range(al.DAILY_LIMIT + 5):
        alerts.stalled(40.0)
        clock.now += settings.config['alerts']['repeat_minutes'] * 60 + 1
    await settle(alerts)
    assert len(outbox.sent) == al.DAILY_LIMIT and '停顿了约 40 秒' in outbox.sent[0][1]
    # Past the cap the event is still written, marked as sent to nobody.
    assert len(events(store)) == al.DAILY_LIMIT + 5 and events(store)[-1][1]['recipients'] == 0


@pytest.mark.anyio
async def test_undelivered_alerts_say_so_and_the_test_button_reports_honestly(tmp_path):
    store, settings, outbox = Store(tmp_path), Settings(), Outbox(ok=False)
    alerts = al.Alerts(settings, store, outbox, clock=Clock())
    alerts.stalled(60)
    await settle(alerts)
    assert [details['alert'] for _, details in events(store)] == ['loop_stall', 'undelivered']
    assert await alerts.test() == {'recipients': 1, 'delivered': 0}
    outbox.ok = True
    assert await alerts.test() == {'recipients': 1, 'delivered': 1}
    settings.config['access']['administrators'] = []
    with pytest.raises(ValueError):
        await alerts.test()
    line = summary('alert', {'alert': 'model_down', 'recipients': 1})
    assert '1 位管理员' in line and severity('alert', {'alert': 'model_down'}) == 'WARNING'
    assert '没有可送达' in summary('alert', {'alert': 'loop_stall', 'recipients': 0})
    await alerts.close()
    store.close()


def test_only_failures_of_the_model_service_count():
    assert service_failure(ProviderConnectionError('x')) == '连接失败'
    assert service_failure(ProviderTimeoutError('x')) == '超时'
    assert service_failure(ProviderRateLimitError('x')) == '服务限流'
    rejected = ProviderError('模型服务返回 401：密钥无效或未授权。')
    rejected.status_code = 401
    assert service_failure(rejected) == '服务返回 401'
    # The bot's own vetoes and a refusal of one request say nothing about the service.
    for local in (ProviderBudgetExceededError('今日预算已用完'), ProviderContentFilterError(),
                  ProviderError('模型响应已收到，但来源、权限或模型配置已变化，未继续使用其结果。')):
        assert service_failure(local) == ''


@pytest.mark.anyio
async def test_the_provider_reports_routed_calls_to_the_listener(tmp_path, monkeypatch):
    settings, store = SettingsStore(tmp_path), Store(tmp_path)
    settings.update({'model': {'base_url': 'https://model.test/v1', 'model': 'mock'}})
    provider, seen = Provider(settings, store), []
    provider.on_model_result = lambda *args: seen.append(args)

    async def down(*args, **kwargs):
        raise ProviderConnectionError('无法连接模型服务')

    async def vetoed(*args, **kwargs):
        raise ProviderBudgetExceededError('今日预算已用完')

    async def fine(*args, **kwargs):
        return {'content': '好'}

    monkeypatch.setattr(provider, '_complete', down)
    with pytest.raises(ProviderError):
        await provider.complete([{'role': 'user', 'content': '在吗'}])
    monkeypatch.setattr(provider, '_complete', vetoed)
    with pytest.raises(ProviderError):
        await provider.complete([{'role': 'user', 'content': '在吗'}])
    monkeypatch.setattr(provider, '_complete', fine)
    assert (await provider.complete([{'role': 'user', 'content': '在吗'}]))['content'] == '好'
    assert [(ok, reason) for _, _, ok, reason in seen] == [(False, '连接失败'), (True, '')]
    # A connection test from the settings page passes its own config and is not live traffic.
    monkeypatch.setattr(provider, '_complete', down)
    with pytest.raises(ProviderError):
        await provider.complete([{'role': 'user', 'content': '在吗'}], config={'id': 'x', 'base_url': 'https://model.test/v1', 'model': 'mock'})
    assert len(seen) == 2
    store.close()


def test_the_app_wires_the_monitor_to_its_signals(tmp_path):
    app = create_app(tmp_path, start_workers=False)
    alerts = app.state.alerts
    assert app.state.provider.on_model_result == alerts.model_result and app.state.onebot.on_link == alerts.link
    assert alerts.recipients() == []
    chat = app.state.chat
    chat.social.prescreen.on_result(False, '超时')
    chat.reset_engines()                         # rebuilt engines (after a restore) keep reporting
    chat.social.prescreen.on_result(False, '超时')
    assert alerts._models[('prescreen', 'prescreen')]['failures'] == 2
    app.state.store.close()
