"""Per-group QQ send limits: learning from refusals, pacing, retry and folding."""
import asyncio
import time
from copy import deepcopy

import pytest

from shiori.chat import ChatService
from shiori.db import Store
from shiori.delivery import NOT_SENT, DeliveryLedger
from shiori.onebot import OneBotBridge
from shiori.send_pacing import SendPacer, refusal
from shiori.settings import DEFAULTS

SCOPE = 'qq:group:202'
LIMITED = {'status': 'failed', 'retcode': 100, 'data': None,
           'wording': 'send group message rejected: result=299 err=本群每分钟只能发5条消息'}
NOT_FRIEND = {'status': 'failed', 'retcode': 100, 'data': None,
              'wording': 'send group message rejected: result=16 err=发送失败，请先添加对方为好友'}


@pytest.fixture
def anyio_backend():
    return 'asyncio'


class Settings:
    def __init__(self):
        self.config = deepcopy(DEFAULTS)
        self.config['onebot'].update(enabled=True, chat_policy='blacklist', action_timeout_seconds=2)
        self.config['output'].update(mode='model', interval_ms=0)
        self.config['stickers']['enabled'] = False

    def get(self):
        return deepcopy(self.config)


class Socket:
    """Answers each send with the next scripted response, then success."""
    def __init__(self, bridge, responses=()):
        self.bridge, self.responses, self.sent, self.at = bridge, list(responses), [], []

    async def send_json(self, action):
        self.sent.append(action)
        self.at.append(time.monotonic())
        response = self.responses.pop(0) if self.responses else {
            'status': 'ok', 'retcode': 0, 'data': {'message_id': len(self.sent)}}
        self.bridge._pending[action['echo']][1].set_result(response)

    async def close(self, *args, **kwargs):
        pass

    def texts(self):
        return [action['params']['message'][0]['data']['text'] for action in self.sent]


class Chat:
    pass


def bridge_for(tmp_path, *, window=.3, chat=None, config=None):
    store = Store(tmp_path)
    store.get_scope(SCOPE)
    bridge = OneBotBridge(store, config or Settings(), chat or Chat())
    bridge._pacer, bridge._pacer_loaded = SendPacer(window=window), True
    return store, bridge


def events(store, kind):
    return [item for item in store.list_events(200) if item['kind'] == kind]


def test_refusal_reads_snowluma_wording_and_stated_limits():
    assert refusal(LIMITED) == (True, 5)
    assert refusal({'status': 'failed', 'retcode': 100, 'message': '本群每分钟只能发3条消息'}) == (True, 3)
    assert refusal({'status': 'failed', 'wording': 'send group message rejected: result=299 err='}) == (True, 5)
    assert refusal(NOT_FRIEND) == (True, None)
    assert refusal({'status': 'failed', 'retcode': 100, 'wording': 'internal error'}) == (False, None)
    assert refusal({'status': 'ok', 'retcode': 0, 'wording': '本群每分钟只能发5条消息'}) == (False, None)
    assert refusal(None) == (False, None)
    assert refusal({'status': 'failed', 'wording': '每分钟只能发0条'}) == (True, None)


def test_pacer_is_open_until_a_limit_is_learned_then_counts_a_sliding_window():
    now = [100.0]
    pacer = SendPacer(clock=lambda: now[0], window=60)
    for _ in range(9):
        pacer.record(SCOPE)
    assert pacer.room(SCOPE) is None and pacer.delay(SCOPE) == 0
    pacer.learn(SCOPE, 10)
    assert pacer.room(SCOPE) == 1 and pacer.delay(SCOPE) == 0
    pacer.record(SCOPE)
    assert pacer.room(SCOPE) == 0 and pacer.delay(SCOPE) == pytest.approx(60)
    now[0] += 30
    assert pacer.delay(SCOPE) == pytest.approx(30)
    now[0] += 30.01
    assert pacer.room(SCOPE) == 10 and pacer.delay(SCOPE) == 0


def test_refused_send_holds_the_group_until_the_platform_window_moves():
    now = [0.0]
    pacer = SendPacer(clock=lambda: now[0], window=60)
    assert pacer.refused(SCOPE, 5) == pytest.approx(15)  # nothing counted here
    now[0] = 20
    pacer.record(SCOPE)
    now[0] = 30
    # Fewer than five counted, so another client used the budget: wait for
    # the oldest counted send to leave the platform's minute.
    assert pacer.refused(SCOPE, 5) == pytest.approx(50)
    assert pacer.room(SCOPE) == 0
    now[0] = 80.01
    assert pacer.room(SCOPE) == 5 and pacer.delay(SCOPE) == 0


@pytest.mark.anyio
async def test_rate_limited_part_is_retried_once_after_the_window_has_room(tmp_path):
    store, bridge = bridge_for(tmp_path)
    socket = bridge._socket = Socket(bridge, [LIMITED])
    try:
        assert await bridge.send(SCOPE, 'Hello') is True
        assert socket.texts() == ['Hello', 'Hello']
        assert socket.at[1] - socket.at[0] >= .3 / 4 - .01
        limited = events(store, 'qq_rate_limited')
        assert len(limited) == 1 and limited[0]['details']['limit'] == 5
        assert limited[0]['details']['delivery_status'] == 'not_sent' and not limited[0]['details']['retry']
        sent = events(store, 'qq_sent')
        assert len(sent) == 1 and sent[0]['details']['send_timing']['rate_wait_ms'] > 0
        assert not [item for item in events(store, 'qq_delivery') if '未确认' in item['message']]
        assert bridge._send_room(SCOPE) == 4 and bridge._send_room('qq:private:303') is None
    finally:
        await bridge.stop()
        store.close()


@pytest.mark.anyio
async def test_second_refusal_is_final_and_reported_as_not_sent(tmp_path):
    store, bridge = bridge_for(tmp_path)
    socket = bridge._socket = Socket(bridge, [LIMITED, LIMITED])
    try:
        assert await bridge.send(SCOPE, 'Hello') is NOT_SENT
        assert len(socket.sent) == 2
        assert [item['details']['retry'] for item in events(store, 'qq_rate_limited')] == [True, False]
        assert not events(store, 'qq_sent')
    finally:
        await bridge.stop()
        store.close()


@pytest.mark.anyio
async def test_retry_is_dropped_when_the_caller_no_longer_wants_the_part(tmp_path):
    store, bridge = bridge_for(tmp_path)
    socket = bridge._socket = Socket(bridge, [LIMITED])
    try:
        assert await bridge.send(SCOPE, 'Hello', metadata={'send_current': lambda: False}) is NOT_SENT
        assert len(socket.sent) == 1
        dropped = [item for item in events(store, 'qq_delivery') if '不再需要' in item['message']]
        assert dropped and dropped[0]['details']['delivery_status'] == 'not_sent'
    finally:
        await bridge.stop()
        store.close()


@pytest.mark.anyio
async def test_other_refusals_are_not_retried_but_known_undelivered(tmp_path):
    store, bridge = bridge_for(tmp_path)
    socket = bridge._socket = Socket(bridge, [NOT_FRIEND])
    try:
        assert await bridge.send(SCOPE, 'Hello') is NOT_SENT
        assert len(socket.sent) == 1 and bridge._send_room(SCOPE) is None
        refused = [item for item in events(store, 'qq_delivery') if '拒收' in item['message']]
        assert refused and '好友' in refused[0]['details']['reason']
    finally:
        await bridge.stop()
        store.close()


@pytest.mark.anyio
async def test_known_limit_paces_sends_instead_of_being_refused(tmp_path):
    store, bridge = bridge_for(tmp_path, window=.4)
    bridge._pacer.learn(SCOPE, 2)
    socket = bridge._socket = Socket(bridge)
    try:
        for text in ('one', 'two', 'three'):
            assert await bridge.send(SCOPE, text) is True
        assert socket.at[2] - socket.at[0] >= .4 - .02
        assert socket.at[1] - socket.at[0] < .2
        assert 'rate_wait_ms' in events(store, 'qq_sent')[0]['details']['send_timing']
    finally:
        await bridge.stop()
        store.close()


def test_limits_refused_within_a_day_survive_a_restart(tmp_path):
    store = Store(tmp_path)
    try:
        store.add_event('qq_rate_limited', 'fixture', scope_id=SCOPE, limit=5)
        store.add_event('qq_rate_limited', 'fixture', scope_id='qq:group:303', limit=4)
        with store._lock, store._conn:
            store._conn.execute("UPDATE events SET created_at='2020-01-01T00:00:00.000000Z' "
                                "WHERE kind='qq_rate_limited' AND json_extract(data,'$.details.scope_id')='qq:group:303'")
        bridge = OneBotBridge(store, Settings(), Chat())
        assert bridge._send_room(SCOPE) == 5
        assert bridge._send_room('qq:group:303') is None
    finally:
        store.close()


class Model:
    def __init__(self, text='First.\n\nSecond.\n\nThird.'):
        self.text = text

    async def complete(self, *args, **kwargs):
        return {'content': self.text, 'message': {'role': 'assistant', 'content': self.text}, 'tool_calls': [], 'usage': {}}


def flow_fixture(tmp_path):
    store, config = Store(tmp_path), Settings()
    store.upsert_scope({'id': SCOPE, 'plan_enabled': False, 'auto_memory': False, 'merge_window_ms': 0})
    chat = ChatService(store, config, Model())
    bridge = OneBotBridge(store, config, chat)
    bridge._pacer, bridge._pacer_loaded = SendPacer(window=.3), True
    return store, chat, bridge


@pytest.mark.anyio
async def test_tight_window_folds_the_remaining_parts_into_one_message(tmp_path):
    store, chat, bridge = flow_fixture(tmp_path)
    bridge._pacer.learn(SCOPE, 2)
    socket = bridge._socket = Socket(bridge)
    try:
        await asyncio.wait_for(bridge._flow.handle(SCOPE, '303', 'Hello', [], {'at_bot': True}, direct=True), 3)
        assert socket.texts() == ['First.', 'Second.\nThird.']
        folded = [item for item in events(store, 'qq_output') if '合并' in item['message']]
        assert folded and folded[0]['details']['original_parts'] == 3
        receipt = DeliveryLedger(store).list(SCOPE)[0]
        assert [part['status'] for part in receipt['parts']] == ['confirmed', 'confirmed']
    finally:
        await bridge.stop()
        await chat.close()
        store.close()


@pytest.mark.anyio
async def test_refused_part_stays_resumable_in_the_receipt(tmp_path):
    store, chat, bridge = flow_fixture(tmp_path)
    socket = bridge._socket = Socket(bridge, [{'status': 'ok', 'retcode': 0, 'data': {'message_id': 1}}, NOT_FRIEND])
    try:
        await asyncio.wait_for(bridge._flow.handle(SCOPE, '303', 'Hello', [], {'at_bot': True}, direct=True), 3)
        assert len(socket.sent) == 2
        receipt = DeliveryLedger(store).list(SCOPE)[0]
        assert [part['status'] for part in receipt['parts']] == ['confirmed', 'pending', 'pending']
        assert any(item['message'] == '群聊回复被平台拒收，未送达' for item in events(store, 'qq_delivery'))
        assert not bridge._flow._output_locks[SCOPE].locked()
    finally:
        await bridge.stop()
        await chat.close()
        store.close()


@pytest.mark.anyio
async def test_rate_limited_reply_part_is_delivered_after_waiting(tmp_path):
    store, chat, bridge = flow_fixture(tmp_path)
    ok = {'status': 'ok', 'retcode': 0, 'data': {'message_id': 1}}
    socket = bridge._socket = Socket(bridge, [ok, LIMITED])
    try:
        await asyncio.wait_for(bridge._flow.handle(SCOPE, '303', 'Hello', [], {'at_bot': True}, direct=True), 5)
        # After the refusal the group's limit is known (5), so nothing else is
        # folded and the refused part is sent again before the third.
        assert socket.texts() == ['First.', 'Second.', 'Second.', 'Third.']
        receipt = DeliveryLedger(store).list(SCOPE)[0]
        assert [part['status'] for part in receipt['parts']] == ['confirmed', 'confirmed', 'confirmed']
    finally:
        await bridge.stop()
        await chat.close()
        store.close()
