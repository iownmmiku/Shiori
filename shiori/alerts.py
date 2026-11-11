"""Tells the administrators when the bot has stopped working.

A model service that is down, a QQ link that was lost or a frozen event loop
all look the same from a group: the bot just goes quiet, and whoever runs it
finds out hours later. This module watches signals that already exist and
sends the QQ administrators a private message:

  model   a model failed several calls in a row over a few minutes, and
          again when it works once more;
  screen  the fast pre-screen (prescreen.py) did the same. Replies go on
          without it, and the message says so;
  link    the QQ connection came back after being down for a while (nothing
          can be sent while it is down);
  stall   the event loop stopped for many seconds (loop_watch.py).

Every alert is also an ``alert`` event in the runtime log, delivered or not.
Alerts are off until switched on (settings ``alerts.enabled``), are spaced
out per cause and capped per day, and never contain chat text, addresses or
keys.
"""
from __future__ import annotations

import asyncio
import time
from collections import deque

DEFAULTS = {'enabled': False, 'model_failures': 5, 'model_minutes': 2, 'repeat_minutes': 30}
LINK_DOWN_SECONDS = 120
STALL_SECONDS = 15
DAILY_LIMIT = 20
PREFIX = '【Shiori 提醒】'
_LIMITS = {'model_failures': (2, 100), 'model_minutes': (0, 120), 'repeat_minutes': (5, 1440)}


def validate(config):
    """Check an alerts settings section; returns the cleaned section."""
    if not isinstance(config, dict) or set(config) - set(DEFAULTS):
        raise ValueError('故障提醒设置包含不支持的字段')
    value = dict(DEFAULTS, **config)
    if type(value['enabled']) is not bool:
        raise ValueError('故障提醒开关须为布尔值')
    for key, (low, high) in _LIMITS.items():
        if type(value[key]) is not int or not low <= value[key] <= high:
            raise ValueError(f'故障提醒的次数与时间须为整数，且在允许范围内（{key}：{low}—{high}）')
    return value


def _minutes(seconds):
    return max(1, round(seconds / 60))


class Alerts:
    """`send(scope_id, text)` delivers one private message and returns whether it was sent."""

    def __init__(self, settings, store, send, *, clock=time.monotonic):
        self.settings, self.store, self._send, self._clock = settings, store, send, clock
        self._models = {}
        self._link_down_at = None
        self._last = {}
        self._delivered = deque()
        self._jobs = set()

    def _config(self):
        return dict(DEFAULTS, **(self.settings.get().get('alerts') or {}))

    def recipients(self):
        """Private chats of the QQ administrators; the official QQ API cannot message first."""
        config = self.settings.get()
        ids = [str(item.get('account_id') or '') for item in config.get('access', {}).get('administrators', [])
               if item.get('platform') == 'onebot']
        ids += [str(value) for value in config.get('onebot', {}).get('owner_ids', [])]
        return ['qq:private:' + value for value in dict.fromkeys(ids) if value.isdigit()]

    def model_result(self, model_id, name, ok, reason=''):
        """One finished model call: a service failure, or a success."""
        self._outage('model', model_id, ok, f'模型「{name}」', reason,
                     '机器人可能无法回复，请检查模型服务，或到“机器人配置 → 模型”核对供应商的地址和密钥。')

    def prescreen_result(self, ok, reason=''):
        """One live call of the fast pre-screen: a failure, or an answer."""
        self._outage('prescreen', 'prescreen', ok, '快速预筛', reason,
                     '回复不受影响，判断会直接交给 Planner，只是不再节省调用；请检查预筛供应商的余额、地址和密钥。')

    def _outage(self, kind, key, ok, subject, reason, advice):
        """Many failures in a row that also lasted a while are an outage; the next success ends it."""
        state = self._models.setdefault((kind, key), {'failures': 0, 'since': 0.0, 'alerted': False})
        now = self._clock()
        if ok:
            if state['alerted']:
                self._raise(kind + '_recovered', key,
                            f'{subject}已恢复，此前中断约 {_minutes(now - state["since"])} 分钟。', spaced=False)
            state.update(failures=0, alerted=False)
            return
        if not state['failures']:
            state['since'] = now
        state['failures'] += 1
        config = self._config()
        lasted = now - state['since']
        if state['failures'] < config['model_failures'] or lasted < config['model_minutes'] * 60:
            return
        still = '仍未恢复，' if state['alerted'] else ''
        if self._raise(kind + '_down', key,
                       f'{subject}{still}已连续失败 {state["failures"]} 次，持续约 {_minutes(lasted)} 分钟'
                       f'{"（" + reason + "）" if reason else ""}。{advice}'):
            state['alerted'] = True

    def link(self, up):
        """The QQ connection opened or closed."""
        now = self._clock()
        if not up:
            if self._link_down_at is None:
                self._link_down_at = now
            return
        down_at, self._link_down_at = self._link_down_at, None
        if down_at is not None and now - down_at >= LINK_DOWN_SECONDS:
            self._raise('link_restored', 'onebot',
                        f'QQ 连接已恢复，此前中断约 {_minutes(now - down_at)} 分钟，期间的消息可能没有收到。')

    def stalled(self, seconds):
        """The event loop stood still for this long."""
        if seconds >= STALL_SECONDS:
            self._raise('loop_stall', 'loop', f'程序主循环停顿了约 {round(seconds)} 秒，期间所有对话都在等待；'
                                               '位置记在“运行日志”的“主循环停顿”里。')

    def _raise(self, kind, key, text, *, spaced=True):
        config = self._config()
        if not config['enabled']:
            return False
        now = self._clock()
        last = self._last.get((kind, key))
        if spaced and last is not None and now - last < config['repeat_minutes'] * 60:
            return False
        self._last[(kind, key)] = now
        while self._delivered and now - self._delivered[0] > 86400:
            self._delivered.popleft()
        targets = self.recipients() if len(self._delivered) < DAILY_LIMIT else []
        try:
            self.store.add_event('alert', text, alert=kind, recipients=len(targets))
        except Exception:
            pass  # The message matters more than its log line.
        if targets:
            self._delivered.append(now)
            try:
                task = asyncio.get_running_loop().create_task(self._deliver(targets, text))
            except RuntimeError:
                return True  # No loop to send from; the event above still records it.
            self._jobs.add(task)
            task.add_done_callback(self._jobs.discard)
        return True

    async def _deliver(self, targets, text):
        delivered = 0
        for scope_id in targets:
            try:
                delivered += bool(await self._send(scope_id, PREFIX + text))
            except asyncio.CancelledError:
                raise
            except Exception:
                continue
        if not delivered:
            try:
                self.store.add_event('alert', '提醒没有送达任何管理员 QQ：请确认 QQ 已连接，且机器人与管理员是好友',
                                     alert='undelivered', recipients=len(targets))
            except Exception:
                pass
        return delivered

    def notice(self, kind, text):
        """A routine note for the administrators (not a failure): sent even when failure alerts are off."""
        now = self._clock()
        while self._delivered and now - self._delivered[0] > 86400:
            self._delivered.popleft()
        targets = self.recipients() if len(self._delivered) < DAILY_LIMIT else []
        if not targets:
            return False
        self._delivered.append(now)
        try:
            task = asyncio.get_running_loop().create_task(self._deliver(targets, text))
        except RuntimeError:
            return False
        self._jobs.add(task)
        task.add_done_callback(self._jobs.discard)
        return True

    async def test(self):
        """Settings page button: send a test message now, whether or not alerts are on."""
        targets = self.recipients()
        if not targets:
            raise ValueError('还没有 QQ 管理员：请先在“电脑与管理员”里添加或绑定管理员 QQ')
        delivered = await self._deliver(targets, '这是一条测试提醒。收到它说明故障提醒可以送达这个 QQ。')
        return {'recipients': len(targets), 'delivered': delivered}

    async def close(self):
        for task in list(self._jobs):
            task.cancel()
        if self._jobs:
            await asyncio.gather(*self._jobs, return_exceptions=True)
        self._jobs.clear()
