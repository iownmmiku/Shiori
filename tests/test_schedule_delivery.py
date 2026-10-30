"""Schedule holds use the existing debounce/transport fixture, not a QQ login."""
import asyncio
from copy import deepcopy
from datetime import datetime
import json
import time
from zoneinfo import ZoneInfo

import pytest

from shiori.schedule_delivery import ScheduleDelivery
from test_message_debounce import anyio_backend, setup, send, drain


SCOPE = 'qq:group:202'


class Schedule:
    def __init__(self, *, status='busy', adaptive=False):
        self.policy = {'active': True, 'status': status, 'due_at': time.time() + 600,
                       'title': '角色活动', 'persona_id': 'default', 'revision': 1}
        self.settings = {'queue_expire_hours': 72, 'queue_limit': 60,
                         'slow_min_seconds': 60, 'slow_max_seconds': 180,
                         'adaptive_enabled': adaptive}
        self.reviews = []
        self.review_blocker = None
        self.review_start_blocker = None
        self.review_waiting = asyncio.Event()
        self.review_started = asyncio.Event()
        self.review_status = None

    def contact_policy(self, _scope_id):
        return deepcopy(self.policy)

    def get_settings(self):
        return deepcopy(self.settings)

    async def review_contact(self, scope_id, text, *, eligible=None):
        self.review_waiting.set()
        if self.review_start_blocker:
            await self.review_start_blocker.wait()
        if eligible and not eligible():
            return
        self.reviews.append((scope_id, text))
        self.review_started.set()
        if self.review_blocker:
            await self.review_blocker.wait()
        if eligible and not eligible():
            return
        if self.review_status:
            self.policy['status'] = self.review_status


def attach(setup, *, status='busy', prefix=None, connected=lambda: True):
    store, config, chat, flow, sent, permission = setup
    schedule = Schedule(status=status)
    chat.character_schedule = schedule
    delivery = ScheduleDelivery(flow, connected, prefix=prefix)
    flow.schedule_delivery = delivery
    return store, config, chat, flow, sent, permission, schedule, delivery


@pytest.mark.anyio
async def test_busy_holds_explicit_messages_without_planner_or_delivery(setup):
    store, _, chat, flow, sent, _, _, delivery = attach(setup)
    store.upsert_scope({'id': SCOPE, 'plan_enabled': True})
    try:
        result = await send(flow, '等你忙完再聊')
        await delivery.tick()
        await drain(flow)
        assert result == {'accepted': True, 'code': 'schedule_buffered'}
        assert not chat.calls and not chat.provider.calls and not sent
        pending = delivery.status()
        assert len(pending) == 1 and pending[0]['count'] == 1
        # The queue carries references; text, credentials and attachments stay
        # in the ordinary message store rather than being duplicated here.
        payload = json.loads(delivery._rows('pending')[0]['payload'])
        assert set(payload[0]) == {'id', 'fingerprint', 'direct', 'reply_to_bot', 'held_at'}
    finally:
        await flow.close()


@pytest.mark.anyio
async def test_global_pause_preserves_backlog_until_resumed(setup):
    _, config, _, flow, sent, _, schedule, delivery = attach(setup)
    try:
        await send(flow, '暂停期间保留这条留言')
        schedule.policy['status'] = 'free'
        config.config['paused'] = True
        await delivery.tick()
        assert delivery.status()[0]['status'] == 'pending'
        assert not sent
        config.config['paused'] = False
        await delivery.tick()
        await drain(flow)
        assert len(sent) == 1 and delivery.status() == []
    finally:
        await delivery.stop()
        await flow.close()


@pytest.mark.anyio
async def test_group_members_release_once_with_images_and_original_sources(setup):
    store, _, chat, flow, sent, _, schedule, delivery = attach(setup)
    try:
        await send(flow, '管理员的留言', user='101', images=['data:image/png;base64,one'])
        await send(flow, '成员的留言', user='404', images=['data:image/png;base64,two'])
        assert delivery.status()[0]['count'] == 2 and not chat.calls
        originals = store.list_messages(SCOPE)
        schedule.policy['status'] = 'free'
        await delivery.tick()
        await drain(flow)
        assert len(chat.calls) == 1 and len(sent) == 1
        call = chat.calls[0]
        assert call[2] == '管理员的留言\n成员的留言'
        assert call[3] == ['data:image/png;base64,one', 'data:image/png;base64,two']
        assert call[4]['trusted_identity'] is None
        assert call[4]['source_ids'] == [x['id'] for x in originals]
        assert delivery.status() == []
        # The original messages were not observed a second time on release.
        assert len([x for x in store.list_messages(SCOPE) if x['role'] == 'user']) == 2
    finally:
        await flow.close()


@pytest.mark.anyio
async def test_slow_uses_slot_override_and_new_arrivals_do_not_extend_deadline(setup, monkeypatch):
    _, _, chat, flow, sent, _, schedule, delivery = attach(setup, status='slow')
    schedule.policy.update(slow_min_seconds=7, slow_max_seconds=9)
    import shiori.schedule_delivery as module
    monkeypatch.setattr(module.random, 'uniform', lambda low, high: low)
    try:
        before = time.time()
        await send(flow, '第一条')
        due = delivery.status()[0]['due_at']
        assert before + 7 <= due <= time.time() + 7
        await send(flow, '补充')
        assert delivery.status()[0]['due_at'] == due
        await delivery.tick(now=due - .01)
        assert not chat.calls and not sent
        await delivery.tick(now=due + .01)
        await drain(flow)
        assert len(chat.calls) == 1 and chat.calls[0][2] == '第一条\n补充'
    finally:
        await flow.close()


@pytest.mark.anyio
async def test_group_background_chatter_is_not_collected(setup):
    _, _, _, flow, _, _, _, delivery = attach(setup)
    try:
        await send(flow, '大家今天都做什么', direct=False)
        assert delivery.status() == []
        await drain(flow)
    finally:
        await flow.close()


@pytest.mark.anyio
async def test_commands_bypass_and_stop_only_cancels_its_own_member(setup):
    store, _, chat, flow, sent, _, schedule, delivery = attach(setup)
    try:
        await send(flow, '甲的留言', user='101')
        await send(flow, '乙的留言', user='404')
        await send(flow, '停止回复', user='101')
        assert delivery.status()[0]['count'] == 1
        remaining = json.loads(delivery._rows('pending')[0]['payload'])
        assert store.get_message(remaining[0]['id'])['user_id'] == '404'
        command = await send(flow, '/日程', user='101')
        assert command['code'] != 'schedule_buffered'
        await drain(flow)
        assert all('/日程' not in store.get_message(x['id'])['content'] for x in remaining)
        chat.calls.clear()
        sent.clear()
        schedule.policy['status'] = 'free'
        await delivery.tick()
        await drain(flow)
        assert len(chat.calls) == 1 and chat.calls[0][2] == '乙的留言'
    finally:
        await flow.close()


@pytest.mark.anyio
async def test_pending_restart_resumes_but_attempted_dispatch_never_replays(setup):
    _, _, chat, flow, sent, _, schedule, delivery = attach(setup)
    try:
        await send(flow, '尚未派发')
        resumed = ScheduleDelivery(flow)
        flow.schedule_delivery = resumed
        schedule.policy['status'] = 'free'
        await resumed.tick()
        await drain(flow)
        assert len(chat.calls) == 1 and len(sent) == 1
        schedule.policy['status'] = 'busy'
        await send(flow, '派发结果未知')
        pending = resumed._rows('pending')[0]
        resumed._update(pending['id'], 'dispatching')
        restarted = ScheduleDelivery(flow)
        flow.schedule_delivery = restarted
        schedule.policy['status'] = 'free'
        await restarted.tick()
        await drain(flow)
        assert len(chat.calls) == 1 and len(sent) == 1
        assert restarted.status()[0]['status'] == 'uncertain'
        assert '不会自动' in restarted.status()[0]['reason']
    finally:
        await flow.close()


@pytest.mark.anyio
@pytest.mark.parametrize('change', ['edit', 'switch', 'delete'])
async def test_old_persona_messages_are_discarded_on_identity_change(setup, change):
    store, _, chat, flow, sent, _, schedule, delivery = attach(setup)
    try:
        await send(flow, '给原角色的留言')
        if change == 'edit':
            store.save_persona({'id': 'default', 'system_prompt': '不同的人设'})
        elif change == 'switch':
            new = store.save_persona({'name': '另一个角色', 'system_prompt': '新角色'})
            store.upsert_scope({'id': SCOPE, 'persona_id': new['id']})
        else:
            store.delete_persona('default')
        schedule.policy['status'] = 'free'
        await delivery.tick()
        await drain(flow)
        assert not chat.calls and not sent and not delivery.status()
        assert delivery._rows()[0]['status'] == 'cancelled'
    finally:
        await flow.close()


@pytest.mark.anyio
@pytest.mark.parametrize('change', ['permission', 'clear_messages', 'source_edit', 'scope_pause', 'scope_delete'])
async def test_revoked_or_deleted_inputs_never_reach_generation(setup, change):
    store, _, chat, flow, sent, permission, schedule, delivery = attach(setup)
    try:
        await send(flow, '等待验证的留言')
        if change == 'permission':
            permission['allowed'] = False
        elif change == 'clear_messages':
            store.clear_messages(SCOPE)
        elif change == 'source_edit':
            original = store.list_messages(SCOPE)[0]
            with store._lock, store._conn:
                original['content'] = '改变后的文字'
                store._conn.execute('UPDATE messages SET data=? WHERE id=?', (json.dumps(original), original['id']))
        elif change == 'scope_pause':
            store.upsert_scope({'id': SCOPE, 'enabled': False})
        else:
            with store._lock, store._conn:
                store._conn.execute('DELETE FROM scopes WHERE id=?', (SCOPE,))
        schedule.policy['status'] = 'free'
        await delivery.tick()
        await drain(flow)
        assert not chat.calls and not sent and delivery.status() == []
        if change == 'scope_delete':
            assert all(x['id'] != SCOPE for x in store.list_scopes())
    finally:
        await flow.close()


@pytest.mark.anyio
async def test_disconnect_keeps_pending_without_generating_or_dropping(setup):
    _, _, chat, flow, sent, _, schedule, delivery = attach(setup, connected=lambda: False)
    try:
        await send(flow, '离线时保留')
        schedule.policy['status'] = 'free'
        await delivery.tick()
        assert not chat.calls and not sent and len(delivery.status()) == 1
        delivery.connected = lambda: True
        await delivery.tick()
        await drain(flow)
        assert len(chat.calls) == 1
    finally:
        await flow.close()


@pytest.mark.anyio
async def test_disabling_schedule_releases_existing_messages_through_normal_flow(setup):
    _, _, chat, flow, _, _, schedule, delivery = attach(setup)
    try:
        await send(flow, '随后关闭控制')
        schedule.policy['active'] = False
        await delivery.tick()
        await drain(flow)
        assert len(chat.calls) == 1 and not delivery.status()
    finally:
        await flow.close()


@pytest.mark.anyio
async def test_clear_and_expiry_do_not_generate(setup):
    _, _, chat, flow, sent, _, schedule, delivery = attach(setup)
    try:
        await send(flow, '清除这条')
        delivery.clear(delivery.status()[0]['id'])
        await send(flow, '过期这条')
        created = delivery.status()[0]['created_at']
        schedule.policy['status'] = 'free'
        await delivery.tick(now=created + 73 * 3600)
        await drain(flow)
        assert not chat.calls and not sent and not delivery.status()
        assert any(row['status'] == 'expired' for row in delivery._rows())
    finally:
        await flow.close()


@pytest.mark.anyio
async def test_reference_batch_limits_do_not_overwrite_messages(setup):
    store, _, _, flow, _, _, schedule, delivery = attach(setup)
    schedule.settings['queue_limit'] = 9
    try:
        for index in range(10):
            await send(flow, f'留言{index}')
        assert [row['count'] for row in delivery.status()] == [8, 1]
        assert len(store.list_messages(SCOPE)) == 10
        assert any(row['kind'] == 'schedule_queue_full' for row in store.list_events(limit=20))
    finally:
        await flow.close()


@pytest.mark.anyio
async def test_two_transport_queues_filter_status_recovery_and_actions(setup):
    store, _, _, flow, _, _, _, onebot = attach(setup, prefix='qq:')
    official = ScheduleDelivery(flow, prefix='qqofficial:')
    try:
        await send(flow, 'OneBot 留言')
        official_message = store.add_message('qqofficial:group:202', '303', 'user', '官方平台留言')
        assert await official.capture(official_message, direct=True)
        assert len(onebot.status()) == len(official.status()) == 1
        assert onebot.status()[0]['scope_id'] == SCOPE
        assert official.status()[0]['scope_id'] == 'qqofficial:group:202'
        official_id = official.status()[0]['id']
        onebot.clear(official_id)
        assert len(official.status()) == 1
        onebot._update(onebot.status()[0]['id'], 'dispatching')
        ScheduleDelivery(flow, prefix='qqofficial:')
        assert onebot.status()[0]['status'] == 'dispatching'
        assert not await onebot.capture(official_message, direct=True)
        assert official.clear_scope(SCOPE)['changed'] == 0
    finally:
        await flow.close()


@pytest.mark.anyio
async def test_persona_edit_during_adaptive_review_does_not_release_old_role_messages(setup, monkeypatch):
    store, _, _, flow, _, _, schedule, delivery = attach(setup)
    import shiori.schedule_delivery as module
    monkeypatch.setattr(module, 'ADAPTIVE_REVIEW_DELAY', .01)
    schedule.settings['adaptive_enabled'] = True
    schedule.review_blocker = asyncio.Event()
    try:
        message = store.add_message(SCOPE, '303', 'user', '原角色的请求')
        assert await asyncio.wait_for(delivery.capture(message, direct=True), .2) is True
        await asyncio.wait_for(schedule.review_started.wait(), 1)
        pending = list(delivery._reviews.values())
        store.save_persona({'id': 'default', 'system_prompt': '修改后的角色'})
        schedule.review_blocker.set()
        await asyncio.gather(*pending)
        await delivery.tick()
        assert delivery.status() == []
    finally:
        schedule.review_blocker.set()
        await delivery.stop()
        await flow.close()


@pytest.mark.anyio
@pytest.mark.parametrize('scope', ['qq:group:202', 'qq:private:303',
                                  'qqofficial:group:202', 'qqofficial:private:303'])
async def test_same_hold_and_release_contract_across_qq_transports(setup, scope):
    store, config, chat, flow, sent, _, schedule, delivery = attach(setup)
    if scope.startswith('qqofficial:'):
        config.config['onebot']['adapter'] = 'official'
    store.upsert_scope({'id': scope, 'plan_enabled': False, 'cooldown_seconds': 0})
    try:
        assert (await send(flow, '第一句', scope=scope))['code'] == 'schedule_buffered'
        await send(flow, '第二句', scope=scope)
        schedule.policy['status'] = 'free'
        await delivery.tick()
        await drain(flow)
        assert len(chat.calls) == len(sent) == 1
        assert chat.calls[0][0] == scope and chat.calls[0][2] == '第一句\n第二句'
    finally:
        await flow.close()


@pytest.mark.anyio
async def test_native_schedule_edits_release_the_same_original_pending_message(setup, tmp_path):
    from shiori.life_schedule import CharacterSchedule
    from test_life_schedule import profile, slot
    store, config, chat, flow, sent, _, _, delivery = attach(setup)
    native = CharacterSchedule(store, config, chat.provider, tmp_path)
    chat.character_schedule = native
    pid = store.get_scope(SCOPE)['persona_id']
    day = datetime.now(ZoneInfo('Asia/Hong_Kong')).date().isoformat()
    native.save({'persona_id': pid, 'date': day, 'kind': 'profile', 'value': profile()})
    native.save({'persona_id': pid, 'date': day, 'kind': 'day',
                 'value': {'summary': '日程角色创作', 'slots': [slot(status='busy')]}})
    native.save_settings({'enabled': True, 'reply_control': True, 'adaptive_enabled': False})
    try:
        assert native.contact_policy(SCOPE)['status'] == 'busy'
        assert (await send(flow, '真实服务下的留言'))['code'] == 'schedule_buffered'
        original_id = store.list_messages(SCOPE)[0]['id']
        native.save({'persona_id': pid, 'date': day, 'kind': 'day',
                     'value': {'summary': '结束忙碌', 'slots': [slot(status='free')]}})
        assert native.contact_policy(SCOPE)['status'] == 'free'
        await delivery.tick()
        await drain(flow)
        assert len(chat.calls) == len(sent) == 1
        assert chat.calls[0][4]['source_ids'] == [original_id]
        assert not chat.provider.calls
    finally:
        await native.close()
        await flow.close()


@pytest.mark.anyio
async def test_each_held_message_has_its_own_expiry(setup, monkeypatch):
    store, _, chat, flow, _, _, schedule, delivery = attach(setup)
    import shiori.schedule_delivery as module
    clock = {'now': time.time()}
    monkeypatch.setattr(module.time, 'time', lambda: clock['now'])
    schedule.settings['queue_expire_hours'] = 1
    try:
        await send(flow, '较早的留言')
        clock['now'] += 3500
        await send(flow, '刚收到的留言')
        newer_id = store.list_messages(SCOPE)[-1]['id']
        clock['now'] += 200
        schedule.policy['status'] = 'free'
        await delivery.tick(now=clock['now'])
        await drain(flow)
        assert len(chat.calls) == 1 and chat.calls[0][2] == '刚收到的留言'
        assert chat.calls[0][4]['source_ids'] == [newer_id]
    finally:
        await flow.close()


@pytest.mark.anyio
async def test_closed_normal_flow_exposes_unknown_handoff_without_automatic_replay(setup):
    _, _, chat, flow, sent, _, schedule, delivery = attach(setup)
    await send(flow, '交接时停止程序')
    schedule.policy['status'] = 'free'
    await delivery.tick()
    await flow.close()
    await asyncio.sleep(0)  # Complete the future's lightweight receipt callback.
    assert not chat.calls and not sent
    assert delivery.status()[0]['status'] == 'uncertain'
    restarted = ScheduleDelivery(flow)
    await restarted.tick()
    assert not chat.calls and not sent
    assert restarted.status()[0]['status'] == 'uncertain'


@pytest.mark.anyio
async def test_stop_after_handoff_preserves_other_members_sources_and_authority(setup):
    store, _, chat, flow, sent, _, schedule, delivery = attach(setup)
    try:
        await send(flow, '管理员自己的旧留言', user='101')
        await send(flow, '普通成员独立留言', user='404')
        other_id = store.list_messages(SCOPE)[-1]['id']
        schedule.policy['status'] = 'free'
        await delivery.tick()
        await send(flow, '停止回复', user='101')
        await drain(flow)
        assert len(chat.calls) == len(sent) == 1
        call = chat.calls[0]
        assert call[2] == '普通成员独立留言'
        assert call[4]['source_ids'] == [other_id]
        assert call[4]['trusted_identity'] == {'account_id': '404'}
    finally:
        await flow.close()


@pytest.mark.anyio
async def test_twenty_busy_inputs_are_persisted_immediately_and_coalesce_one_blocked_review(setup, monkeypatch):
    _, _, chat, flow, sent, _, schedule, delivery = attach(setup)
    import shiori.schedule_delivery as module
    from types import SimpleNamespace

    review_delay_started = asyncio.Event()
    release_review = asyncio.Event()

    async def gated_review_delay(seconds):
        assert seconds == module.ADAPTIVE_REVIEW_DELAY
        review_delay_started.set()
        await release_review.wait()

    # Hold only this module's collection delay. A 10 ms wall-clock sleep lets
    # slower runners start the review before all twenty inputs have arrived.
    # Keep asyncio itself unchanged for ingress, fixture timeouts and teardown.
    monkeypatch.setattr(module, 'asyncio', SimpleNamespace(**{
        **vars(asyncio), 'sleep': gated_review_delay,
    }))
    schedule.settings['adaptive_enabled'] = True
    schedule.review_blocker = asyncio.Event()
    try:
        for index in range(20):
            result = await asyncio.wait_for(send(flow, f'连续留言{index}', user=str(100 + index)), .3)
            assert result['code'] == 'schedule_buffered'
            assert sum(row['count'] for row in delivery.status()) == index + 1
        assert sum(row['count'] for row in delivery.status()) == 20
        await asyncio.wait_for(review_delay_started.wait(), 1)
        assert not schedule.reviews and len(delivery._reviews) == 1
        release_review.set()
        await asyncio.wait_for(schedule.review_started.wait(), 1)
        assert len(schedule.reviews) == 1 and len(delivery._reviews) == 1
        combined = schedule.reviews[0][1]
        assert combined.count('[说话人 ') == 8
        assert '[说话人 119]' in combined and '连续留言19' in combined
        assert '连续留言0\n' not in combined
        # New input during the blocked request stays on the same review task.
        await asyncio.wait_for(send(flow, '等待模型时的新留言', user='404'), .3)
        assert len(schedule.reviews) == 1
        assert sum(row['count'] for row in delivery.status()) == 21
        assert not chat.calls and not chat.provider.calls and not sent
    finally:
        release_review.set()
        schedule.review_blocker.set()
        await delivery.stop()
        await flow.close()


@pytest.mark.anyio
async def test_completed_background_review_wakes_normal_unified_release(setup, monkeypatch):
    _, _, chat, flow, sent, _, schedule, delivery = attach(setup)
    import shiori.schedule_delivery as module
    monkeypatch.setattr(module, 'ADAPTIVE_REVIEW_DELAY', .01)
    schedule.settings['adaptive_enabled'] = True
    schedule.review_status = 'free'
    try:
        await send(flow, '甲的请求', user='101')
        await send(flow, '乙的请求', user='404')
        await asyncio.gather(*list(delivery._reviews.values()))
        await drain(flow)
        assert len(schedule.reviews) == 1
        assert len(chat.calls) == len(sent) == 1
        assert chat.calls[0][2] == '甲的请求\n乙的请求'
        assert chat.calls[0][4]['trusted_identity'] is None
    finally:
        await delivery.stop()
        await flow.close()


@pytest.mark.anyio
@pytest.mark.parametrize('change', ['persona', 'service', 'permission', 'cancel'])
async def test_stale_or_unauthorized_background_review_never_calls_model(setup, monkeypatch, change):
    store, _, chat, flow, _, permission, schedule, delivery = attach(setup)
    import shiori.schedule_delivery as module
    monkeypatch.setattr(module, 'ADAPTIVE_REVIEW_DELAY', .01)
    schedule.settings['adaptive_enabled'] = True
    try:
        await send(flow, '判断前发生变更')
        pending = list(delivery._reviews.values())
        if change == 'persona':
            store.save_persona({'id': 'default', 'system_prompt': '新的角色'})
        elif change == 'service':
            chat.character_schedule = Schedule()
        elif change == 'permission':
            permission['allowed'] = False
        else:
            delivery.clear_scope(SCOPE)
        await asyncio.gather(*pending)
        assert not schedule.reviews
    finally:
        await delivery.stop()
        await flow.close()


@pytest.mark.anyio
async def test_stopping_delivery_cancels_blocked_adaptive_request_without_losing_pending(setup, monkeypatch):
    _, _, chat, flow, sent, _, schedule, delivery = attach(setup)
    import shiori.schedule_delivery as module
    monkeypatch.setattr(module, 'ADAPTIVE_REVIEW_DELAY', .01)
    schedule.settings['adaptive_enabled'] = True
    schedule.review_blocker = asyncio.Event()
    try:
        await send(flow, '保持队列，停止后台判断')
        await asyncio.wait_for(schedule.review_started.wait(), 1)
        await delivery.stop()
        assert not delivery._reviews
        assert delivery.status()[0]['status'] == 'pending'
        assert not chat.calls and not sent
    finally:
        schedule.review_blocker.set()
        await delivery.stop()
        await flow.close()


@pytest.mark.anyio
@pytest.mark.parametrize('change', ['pause', 'scope_pause', 'disconnect', 'closed'])
async def test_background_review_obeys_runtime_gates_before_start(setup, monkeypatch, change):
    store, config, _, flow, _, _, schedule, delivery = attach(setup)
    import shiori.schedule_delivery as module
    monkeypatch.setattr(module, 'ADAPTIVE_REVIEW_DELAY', .01)
    schedule.settings['adaptive_enabled'] = True
    try:
        await send(flow, '判断开始前暂停')
        pending = list(delivery._reviews.values())
        if change == 'pause':
            config.config['paused'] = True
        elif change == 'scope_pause':
            store.upsert_scope({'id': SCOPE, 'enabled': False})
        elif change == 'disconnect':
            delivery.connected = lambda: False
        else:
            flow._closed = True
        await asyncio.gather(*pending)
        assert not schedule.reviews
    finally:
        await delivery.stop()
        await flow.close()


@pytest.mark.anyio
@pytest.mark.parametrize('change', ['pause', 'permission', 'scope_pause', 'disconnect',
                                  'source_edit', 'cancel', 'expired', 'persona', 'service'])
async def test_adaptive_material_is_revalidated_after_waiting_for_service(setup, monkeypatch, change):
    store, config, chat, flow, _, permission, schedule, delivery = attach(setup)
    import shiori.schedule_delivery as module
    monkeypatch.setattr(module, 'ADAPTIVE_REVIEW_DELAY', .01)
    clock = {'now': time.time()}
    monkeypatch.setattr(module.time, 'time', lambda: clock['now'])
    schedule.settings['adaptive_enabled'] = True
    schedule.review_start_blocker = asyncio.Event()
    try:
        await send(flow, '服务正在等其他判断，尚未调用模型')
        pending = list(delivery._reviews.values())
        await asyncio.wait_for(schedule.review_waiting.wait(), 1)
        if change == 'pause':
            config.config['paused'] = True
        elif change == 'permission':
            permission['allowed'] = False
        elif change == 'scope_pause':
            store.upsert_scope({'id': SCOPE, 'enabled': False})
        elif change == 'disconnect':
            delivery.connected = lambda: False
        elif change == 'source_edit':
            original = store.list_messages(SCOPE)[0]
            with store._lock, store._conn:
                original['content'] = '原文已改变'
                store._conn.execute('UPDATE messages SET data=? WHERE id=?', (json.dumps(original), original['id']))
        elif change == 'cancel':
            delivery.clear_scope(SCOPE)
        elif change == 'expired':
            clock['now'] += 73 * 3600
        elif change == 'persona':
            store.save_persona({'id': 'default', 'system_prompt': '新角色'})
        else:
            chat.character_schedule = Schedule()
        schedule.review_start_blocker.set()
        await asyncio.gather(*pending)
        assert not schedule.reviews
    finally:
        schedule.review_start_blocker.set()
        await delivery.stop()
        await flow.close()


@pytest.mark.anyio
async def test_handoff_stays_active_until_normal_flow_confirms_delivery(setup):
    _, _, chat, flow, sent, _, schedule, delivery = attach(setup)
    try:
        await send(flow, '尚在普通合并窗口中')
        schedule.policy['status'] = 'free'
        await delivery.tick()
        assert delivery.status()[0]['status'] == 'dispatching'
        assert not chat.calls and not sent
        await drain(flow)
        assert delivery.status() == []
        assert delivery._rows()[0]['status'] == 'submitted'
        assert not delivery._handoffs
    finally:
        await flow.close()


@pytest.mark.anyio
async def test_restart_during_accepted_handoff_exposes_uncertainty(setup):
    _, _, chat, flow, sent, _, schedule, delivery = attach(setup)
    try:
        await send(flow, '接受交接后尚未发送')
        schedule.policy['status'] = 'free'
        await delivery.tick()
        restarted = ScheduleDelivery(flow)
        assert restarted.status()[0]['status'] == 'uncertain'
        await flow.close()
        await restarted.tick()
        assert not chat.calls and not sent
        assert restarted.status()[0]['status'] == 'uncertain'
    finally:
        await flow.close()


@pytest.mark.anyio
async def test_known_ingress_busy_before_first_acceptance_keeps_pending(setup):
    _, _, chat, flow, sent, _, schedule, delivery = attach(setup)
    try:
        await send(flow, '普通队列满时继续等待')
        schedule.policy['status'] = 'free'
        flow._scope_jobs[SCOPE] = 8
        await delivery.tick()
        assert delivery.status()[0]['status'] == 'pending'
        assert not chat.calls and not sent
        flow._scope_jobs[SCOPE] = 0
        await delivery.tick()
        await drain(flow)
        assert len(sent) == 1 and not delivery.status()
    finally:
        await flow.close()


@pytest.mark.anyio
async def test_partly_accepted_ingress_is_never_automatically_replayed(setup):
    _, config, chat, flow, sent, _, schedule, delivery = attach(setup)
    config.config['context']['max_images'] = 1
    config.config['scheduler']['scope_queue_limit'] = 1
    try:
        await send(flow, '已接收的第一条', images=['data:image/png;base64,one'])
        await send(flow, '未接收的第二条', images=['data:image/png;base64,two'])
        schedule.policy['status'] = 'free'
        await delivery.tick()
        assert delivery.status()[0]['status'] == 'uncertain'
        await drain(flow)
        await delivery.tick()
        await drain(flow)
        assert len(chat.calls) == len(sent) == 1
        assert chat.calls[0][2] == '已接收的第一条'
        assert delivery.status()[0]['status'] == 'uncertain'
    finally:
        await flow.close()


@pytest.mark.anyio
async def test_partial_transport_failure_remains_visible_without_replay(setup, monkeypatch):
    _, config, chat, flow, sent, _, schedule, delivery = attach(setup)
    import shiori.social as social
    config.config['output']['interval_ms'] = 0
    monkeypatch.setattr(social, 'split_output', lambda *args, **kwargs: ['第一段', '第二段'])

    async def emit(scope, text, metadata):
        sent.append((scope, text, metadata))
        return len(sent) == 1

    flow.emit = emit
    try:
        await send(flow, '两段回复只确认第一段')
        schedule.policy['status'] = 'free'
        await delivery.tick()
        await drain(flow)
        assert len(chat.calls) == 1 and len(sent) == 2
        assert delivery.status()[0]['status'] == 'uncertain'
        await delivery.tick()
        await drain(flow)
        assert len(chat.calls) == 1 and len(sent) == 2
    finally:
        await flow.close()


@pytest.mark.anyio
async def test_suppression_before_generation_is_visible_in_queue(setup):
    _, config, chat, flow, sent, _, schedule, delivery = attach(setup)
    try:
        await send(flow, '合并等待期间暂停')
        schedule.policy['status'] = 'free'
        await delivery.tick()
        config.config['paused'] = True
        await drain(flow)
        assert not chat.calls and not sent
        assert delivery.status()[0]['status'] == 'uncertain'
        config.config['paused'] = False
        await delivery.tick()
        await drain(flow)
        assert not chat.calls and not sent
    finally:
        await flow.close()


@pytest.mark.anyio
async def test_cleared_handoff_is_not_resurrected_by_shutdown_receipt(setup):
    _, _, _, flow, _, _, schedule, delivery = attach(setup)
    await send(flow, '清除后收到迟到的交接回执')
    row_id = delivery.status()[0]['id']
    schedule.policy['status'] = 'free'
    await delivery.tick()
    delivery.clear(row_id)
    await flow.close()
    await asyncio.sleep(0)
    assert delivery.status() == []
    assert delivery._rows()[0]['status'] == 'cancelled'


@pytest.mark.anyio
async def test_unresolved_handoffs_count_towards_global_queue_bound(setup, monkeypatch):
    store, _, _, flow, _, _, _, delivery = attach(setup)
    import shiori.schedule_delivery as module
    monkeypatch.setattr(module, 'MAX_ACTIVE_ROWS', 2)
    try:
        for index in range(2):
            await send(flow, f'未知结果{index}', scope=f'qq:group:{300 + index}')
            row = delivery._rows('pending')[0]
            delivery._update(row['id'], 'uncertain')
        await send(flow, '上限外的原文仍保留')
        assert len(delivery.status()) == 2
        assert not delivery._rows('pending')
        assert store.list_messages(SCOPE)[0]['content'] == '上限外的原文仍保留'
        delivery.clear(delivery.status()[0]['id'])
        assert (await send(flow, '清理后可以继续暂存'))['code'] == 'schedule_buffered'
        assert len(delivery._rows('pending')) == 1
    finally:
        await flow.close()


@pytest.mark.anyio
async def test_repeated_capture_never_duplicates_an_earlier_full_row(setup):
    store, _, _, flow, _, _, _, delivery = attach(setup)
    try:
        for index in range(9):
            await send(flow, f'原始留言{index}')
        original = store.list_messages(SCOPE)[0]
        assert await delivery.capture(original, direct=True)
        assert sum(row['count'] for row in delivery.status()) == 9
    finally:
        await flow.close()


@pytest.mark.anyio
async def test_later_slow_row_cannot_overtake_scope_head(setup, monkeypatch):
    _, _, chat, flow, _, _, schedule, delivery = attach(setup, status='slow')
    import shiori.schedule_delivery as module
    clock = {'now': time.time()}
    monkeypatch.setattr(module.time, 'time', lambda: clock['now'])
    delays = iter([100, 10])
    monkeypatch.setattr(module.random, 'uniform', lambda *_: next(delays))
    try:
        for index in range(9):
            await send(flow, f'留言{index}')
        await delivery.tick(now=clock['now'] + 11)
        await drain(flow)
        assert not chat.calls
        await delivery.tick(now=clock['now'] + 101)
        await drain(flow)
        assert len(chat.calls) == 1 and '留言0' in chat.calls[0][2] and '留言8' not in chat.calls[0][2]
        await delivery.tick(now=clock['now'] + 101)
        await drain(flow)
        assert len(chat.calls) == 2 and chat.calls[1][2] == '留言8'
    finally:
        await flow.close()


@pytest.mark.anyio
async def test_due_slow_hold_waits_through_busy_slot_then_releases_when_contact_returns(setup, monkeypatch):
    _, _, chat, flow, _, _, schedule, delivery = attach(setup, status='slow')
    import shiori.schedule_delivery as module
    monkeypatch.setattr(module.random, 'uniform', lambda *_: 5)
    try:
        await send(flow, '慢回期限已到但角色进入忙碌')
        due = delivery.status()[0]['due_at']
        schedule.policy['status'] = 'busy'
        await delivery.tick(now=due + 1)
        assert not chat.calls and delivery.status()[0]['status'] == 'pending'
        schedule.policy['status'] = 'slow'
        schedule.policy.update(slow_min_seconds=600, slow_max_seconds=600)
        await delivery.tick(now=due + 1)
        await drain(flow)
        assert len(chat.calls) == 1 and not delivery.status()
    finally:
        await flow.close()


@pytest.mark.anyio
async def test_split_handoff_requires_every_normal_turn_to_confirm_delivery(setup):
    _, config, chat, flow, sent, _, schedule, delivery = attach(setup)
    config.config['context']['max_images'] = 1

    async def emit(scope, text, metadata):
        sent.append((scope, text, metadata))
        return len(sent) != 1

    flow.emit = emit
    try:
        await send(flow, '第一轮未确认', images=['data:image/png;base64,one'])
        await send(flow, '第二轮送达', images=['data:image/png;base64,two'])
        schedule.policy['status'] = 'free'
        await delivery.tick()
        assert delivery.status()[0]['status'] == 'dispatching'
        await drain(flow)
        assert len(chat.calls) == len(sent) == 2
        assert delivery.status()[0]['status'] == 'uncertain'
        assert not delivery._handoffs
        await delivery.tick()
        await drain(flow)
        assert len(chat.calls) == 2
    finally:
        await flow.close()


@pytest.mark.anyio
async def test_handoff_receipts_that_complete_together_preserve_any_failure(setup):
    _, _, _, flow, _, _, _, delivery = attach(setup)
    try:
        await send(flow, '两个回执在同一轮循环完成')
        row_id = delivery.status()[0]['id']
        delivery._update(row_id, 'dispatching')
        good = asyncio.get_running_loop().create_future()
        failed = asyncio.get_running_loop().create_future()
        delivery._follow_handoff(row_id, {good, failed})
        good.set_result({'accepted': True, 'outcome': 'delivered'})
        failed.set_result({'accepted': False, 'outcome': 'unknown'})
        await asyncio.sleep(0)
        assert delivery.status()[0]['status'] == 'uncertain'
        assert not delivery._handoffs
    finally:
        await flow.close()


@pytest.mark.anyio
async def test_old_handoff_receipt_cannot_overwrite_an_explicit_retry(setup):
    _, _, _, flow, _, _, _, delivery = attach(setup)
    try:
        await send(flow, '旧交接与管理员重试分开核对')
        row_id = delivery.status()[0]['id']
        old = asyncio.get_running_loop().create_future()
        new = asyncio.get_running_loop().create_future()
        delivery._update(row_id, 'dispatching')
        delivery._follow_handoff(row_id, {old})
        delivery._update(row_id, 'uncertain')
        delivery.clear_scope(SCOPE, 'retry')
        delivery._update(row_id, 'dispatching')
        delivery._follow_handoff(row_id, {new})
        old.set_result({'accepted': False, 'outcome': 'unknown'})
        await asyncio.sleep(0)
        assert delivery.status()[0]['status'] == 'dispatching'
        new.set_result({'accepted': True, 'outcome': 'delivered'})
        await asyncio.sleep(0)
        assert delivery.status() == []
    finally:
        await flow.close()


@pytest.mark.anyio
async def test_scope_limit_counts_pending_uncertain_and_in_flight_inputs(setup):
    store, _, _, flow, _, _, schedule, delivery = attach(setup)
    schedule.settings['queue_limit'] = 3
    try:
        await send(flow, '结果待核对')
        unknown_id = delivery._rows('pending')[0]['id']
        delivery._update(unknown_id, 'uncertain')
        await send(flow, '正在普通流程中')
        delivery._update(delivery._rows('pending')[0]['id'], 'dispatching')
        await send(flow, '尚未派发')
        await send(flow, '超出同一会话上限的新原文')
        assert sum(row['count'] for row in delivery.status()) == 3
        assert {row['status'] for row in delivery.status()} == {'pending', 'uncertain', 'dispatching'}
        assert len(store.list_messages(SCOPE)) == 4
        delivery.clear(unknown_id)
        await send(flow, '核对清理后可以继续暂存')
        assert sum(row['count'] for row in delivery.status()) == 3
        assert delivery._rows('pending')[0]['payload'].count('fingerprint') == 2
    finally:
        await flow.close()
