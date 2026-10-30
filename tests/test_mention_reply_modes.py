"""Mention reply modes retain shared collection and live authorization checks."""
import asyncio
import json
import time

import pytest

from test_message_debounce import setup, anyio_backend, send, drain
from test_group_unified_security import runtime, close as close_runtime, SCOPE


def configure(setup, *, mandatory=False, debounce=True, **scope_options):
    store, config, chat, flow, _, permission = setup
    config.config['identity'].update(respond_to_at=mandatory, respond_to_name=mandatory)
    config.config['debounce']['enabled'] = debounce
    store.upsert_scope({'id': 'qq:group:202', 'plan_enabled': True,
                        'proactive_reply': False, 'activity': 'quiet',
                        'merge_window_ms': 0, **scope_options})
    flow.allowed = lambda scope, user: permission['allowed'] and chat.social._allowed(scope)
    return store, config, chat, flow


def choose(chat, action):
    original = chat.provider.complete

    async def complete(messages, **kwargs):
        result = await original(messages, **kwargs)
        choice = json.loads(result['content'])
        choice.update(action=action, intent='统一回应' if action == 'reply' else '')
        return {'content': json.dumps(choice)}

    chat.provider.complete = complete


@pytest.mark.anyio
@pytest.mark.parametrize('kind', ['at', 'name'])
async def test_enabled_mandatory_mention_bypasses_planner_even_when_it_would_skip(setup, kind):
    _, _, chat, flow = configure(setup, mandatory=True, debounce=False, plan_enabled=False)
    choose(chat, 'skip')
    try:
        result = await send(flow, '你好' if kind == 'at' else '初音，你好',
                            direct=kind == 'at', wait=True)
        assert result['accepted'] and len(chat.calls) == 1
        assert not chat.provider.calls
    finally:
        await flow.close()


@pytest.mark.anyio
@pytest.mark.parametrize('kind', ['at', 'name'])
@pytest.mark.parametrize('action', ['reply', 'skip'])
async def test_disabled_mandatory_mention_runs_planner_in_quiet_nonproactive_scope(setup, kind, action):
    _, config, chat, flow = configure(setup, debounce=False)
    config.config['planner']['min_interval_ms'] = 60000
    chat.social._last_gate['qq:group:202'] = time.monotonic()
    chat.social._last_reply['qq:group:202'] = time.monotonic()
    choose(chat, action)
    try:
        result = await asyncio.wait_for(send(flow, '你好' if kind == 'at' else '初音，你好',
                                             direct=kind == 'at', wait=True), 1)
        assert result['accepted'] is (action == 'reply')
        assert len(chat.provider.calls) == 1
        assert len(chat.calls) == (1 if action == 'reply' else 0)
        assert chat.provider.calls[0]['signals']['at_bot' if kind == 'at' else 'named'] is True
        assert not flow._planner_batches
    finally:
        await flow.close()


@pytest.mark.anyio
@pytest.mark.parametrize('mandatory', [False, True])
async def test_multiple_members_mentions_share_one_debounce_turn(setup, mandatory):
    store, _, chat, flow = configure(setup, mandatory=mandatory)
    try:
        await send(flow, '成员甲的问题', user='101')
        await send(flow, '成员乙的问题', user='404')
        await send(flow, '成员甲的补充', user='101')
        await drain(flow)
        assert len(chat.provider.calls) == (0 if mandatory else 1)
        assert len(chat.calls) == 1
        assert chat.calls[0][2] == '成员甲的问题\n成员乙的问题\n成员甲的补充'
        assert chat.calls[0][4]['trusted_identity'] is None
        assert len(chat.calls[0][4]['source_ids']) == 3
        users = [row for row in store.list_messages('qq:group:202') if row['role'] == 'user']
        assert [row['user_id'] for row in users] == ['101', '404', '101']
        assert not flow._planner_batches
    finally:
        await flow.close()


@pytest.mark.anyio
@pytest.mark.parametrize('mandatory', [False, True])
@pytest.mark.parametrize('mentioned_in_text', [False, True])
async def test_group_speaker_id_is_not_a_name_mention_but_original_text_still_is(
        setup, mandatory, mentioned_in_text):
    store, _, chat, flow = configure(setup, mandatory=mandatory,
                                     bot_name='测试机器人', bot_aliases=['101'])
    try:
        await send(flow, '今天刚看完一部电影', user='101', direct=False)
        await send(flow, '101，聊聊这部电影' if mentioned_in_text else '大家聊聊这部电影',
                   user='404', direct=False)
        await drain(flow)
        assert len(chat.calls) == (1 if mentioned_in_text else 0)
        assert len(chat.provider.calls) == (1 if mentioned_in_text and not mandatory else 0)
        if chat.provider.calls:
            payload = chat.provider.calls[0]
            assert payload['signals']['named'] is True
            assert '[说话人 101]' in payload['current_message_text']
            assert '[说话人 404]' in payload['current_message_text']
            assert '101，聊聊这部电影' in payload['current_message_text']
        originals = [row for row in store.list_messages('qq:group:202') if row['role'] == 'user']
        assert len(originals) == 2
        assert all('[说话人' not in row['content'] for row in originals)
    finally:
        await flow.close()


@pytest.mark.anyio
async def test_no_debounce_mention_supersedes_waiting_ordinary_group_batch(setup):
    _, _, chat, flow = configure(setup, debounce=False, proactive_reply=True, activity='balanced')
    try:
        result = await send(flow, '大家晚上好', direct=False)
        assert result['code'] == 'planner_buffered' and not chat.provider.calls
        result = await asyncio.wait_for(send(flow, '我有一个问题', wait=True), 1)
        await asyncio.wait_for(drain(flow), 1)
        assert result['accepted']
        assert len(chat.provider.calls) == len(chat.calls) == 1
        assert chat.provider.calls[0]['current_message_text'] == '我有一个问题'
        assert not flow._planner_batches
    finally:
        await flow.close()


@pytest.mark.anyio
@pytest.mark.parametrize('unavailable', ['disabled', 'missing_model', 'missing_provider'])
@pytest.mark.parametrize('kind', ['at', 'name'])
async def test_optional_mentions_never_fall_back_to_reply_without_planner(setup, unavailable, kind):
    store, config, chat, flow = configure(setup, debounce=False)
    if unavailable == 'disabled':
        store.upsert_scope({'id': 'qq:group:202', 'plan_enabled': False})
    elif unavailable == 'missing_model':
        config.config['model'].update(base_url='', model='')
    else:
        chat.social.provider = None
    try:
        result = await send(flow, '你好' if kind == 'at' else '初音，你好',
                            direct=kind == 'at', wait=True)
        assert not result['accepted'] and not chat.provider.calls and not chat.calls
        assert not setup[4]
    finally:
        await flow.close()


@pytest.mark.anyio
@pytest.mark.parametrize('kind', ['at', 'name'])
@pytest.mark.parametrize('other_trigger', ['replacement', 'reply'])
@pytest.mark.parametrize('unavailable', ['disabled', 'missing_model'])
async def test_optional_mention_cannot_borrow_another_triggers_unavailable_planner_fallback(
        setup, kind, other_trigger, unavailable):
    store, config, chat, flow = configure(setup, debounce=False)
    if unavailable == 'disabled':
        store.upsert_scope({'id': 'qq:group:202', 'plan_enabled': False})
    else:
        config.config['model'].update(base_url='', model='')
    text = '更正一下，改成另一首旋律' if other_trigger == 'replacement' else '继续聊这首旋律'
    if kind == 'name':
        text += '，初音'
    if other_trigger == 'replacement':
        chat.social._open_requests.add(('qq:group:202', '303'))
    item = store.add_message('qq:group:202', '303', 'user', text,
                             at_bot=kind == 'at', reply_to_bot=other_trigger == 'reply')
    try:
        decision = await chat.social.plan(store.get_scope('qq:group:202'), item)
        assert not decision['reply'] and not chat.provider.calls
    finally:
        await flow.close()


@pytest.mark.anyio
@pytest.mark.parametrize('other_trigger', ['replacement', 'reply'])
@pytest.mark.parametrize('unavailable', ['disabled', 'missing_model'])
async def test_unmentioned_correction_and_reply_keep_existing_local_fallback(setup, other_trigger, unavailable):
    store, config, chat, flow = configure(setup, debounce=False)
    if unavailable == 'disabled':
        store.upsert_scope({'id': 'qq:group:202', 'plan_enabled': False})
    else:
        config.config['model'].update(base_url='', model='')
    text = '更正一下，改成另一首旋律' if other_trigger == 'replacement' else '继续聊这首旋律'
    if other_trigger == 'replacement':
        chat.social._open_requests.add(('qq:group:202', '303'))
    item = store.add_message('qq:group:202', '303', 'user', text,
                             at_bot=False, reply_to_bot=other_trigger == 'reply')
    try:
        decision = await chat.social.plan(store.get_scope('qq:group:202'), item)
        assert decision['reply'] and not chat.provider.calls
    finally:
        await flow.close()


@pytest.mark.anyio
@pytest.mark.parametrize('kind', ['at', 'name'])
@pytest.mark.parametrize('preset,mandatory', [('group_companion', False), ('quiet_assistant', True)])
async def test_group_presets_choose_semantic_or_mandatory_mention_mode(setup, kind, preset, mandatory):
    from shiori.presets import preset_patch

    store, _, chat, flow = configure(setup, debounce=False)
    patch = preset_patch(preset, 'qq:group:202')
    assert patch['trigger_at'] is mandatory and patch['trigger_name'] is mandatory
    store.upsert_scope({'id': 'qq:group:202', **patch})
    choose(chat, 'skip')
    try:
        result = await send(flow, '你好' if kind == 'at' else '初音，你好',
                            direct=kind == 'at', wait=True)
        assert result['accepted'] is mandatory
        assert len(chat.provider.calls) == (0 if mandatory else 1)
        assert len(chat.calls) == (1 if mandatory else 0)
    finally:
        await flow.close()


@pytest.mark.anyio
async def test_mention_in_legacy_continuation_still_requires_its_planner_decision(setup):
    store, _, chat, flow = configure(setup, debounce=False)
    choose(chat, 'skip')
    item = store.add_message('qq:group:202', '303', 'user', '这是新的提及', at_bot=True)
    try:
        decision = await chat.social.plan(store.get_scope('qq:group:202'), item,
                                          direct=True, continuation=True)
        assert not decision['reply'] and len(chat.provider.calls) == 1
        assert decision['semantic']
    finally:
        await flow.close()


@pytest.mark.anyio
@pytest.mark.parametrize('change', ['mandatory', 'planner', 'pause', 'permission', 'source'])
async def test_optional_mention_planner_rechecks_settings_and_authority_in_flight(setup, change):
    store, config, chat, flow = configure(setup, debounce=False)
    entered, release = asyncio.Event(), asyncio.Event()
    original = chat.provider.complete

    async def complete(messages, **kwargs):
        entered.set()
        await release.wait()
        return await original(messages, **kwargs)

    chat.provider.complete = complete
    pending = asyncio.create_task(send(flow, '请回答这个问题', wait=True))
    try:
        await asyncio.wait_for(entered.wait(), 1)
        if change == 'mandatory':
            store.upsert_scope({'id': 'qq:group:202', 'trigger_at': True})
        elif change == 'planner':
            store.upsert_scope({'id': 'qq:group:202', 'plan_enabled': False})
        elif change == 'pause':
            config.config['paused'] = True
        elif change == 'permission':
            config.config['onebot'].update(chat_policy='whitelist', allowed_groups=[])
            setup[5]['allowed'] = False
        else:
            store.clear_messages('qq:group:202')
        release.set()
        assert not (await asyncio.wait_for(pending, 1))['accepted']
        assert not chat.calls and not setup[4]
    finally:
        release.set()
        await asyncio.gather(pending, return_exceptions=True)
        await flow.close()


@pytest.mark.anyio
@pytest.mark.parametrize('change', ['mandatory', 'permission', 'pause'])
async def test_optional_mention_delivery_rechecks_live_settings_after_generation(setup, change):
    store, config, chat, flow = configure(setup)
    chat.blocker = asyncio.Event()
    try:
        await send(flow, '等待生成期间的权限检查')
        await asyncio.wait_for(chat.entered.wait(), 1)
        if change == 'mandatory':
            store.upsert_scope({'id': 'qq:group:202', 'trigger_at': True})
        elif change == 'pause':
            config.config['paused'] = True
        else:
            setup[5]['allowed'] = False
        chat.blocker.set()
        await drain(flow)
        assert len(chat.provider.calls) == len(chat.calls) == 1
        assert not setup[4]
    finally:
        chat.blocker.set()
        await flow.close()


@pytest.mark.anyio
async def test_optional_mentions_stay_in_busy_schedule_until_normal_release(setup):
    from test_schedule_delivery import attach

    configure(setup)
    store, _, chat, flow, sent, _, schedule, delivery = attach(setup)
    try:
        assert (await send(flow, '第一条留言', user='101'))['code'] == 'schedule_buffered'
        assert (await send(flow, '第二条留言', user='404'))['code'] == 'schedule_buffered'
        assert not chat.provider.calls and not chat.calls and not sent
        schedule.policy['status'] = 'free'
        await delivery.tick()
        await drain(flow)
        assert len(chat.provider.calls) == len(chat.calls) == len(sent) == 1
        assert len(chat.calls[0][4]['source_ids']) == 2
        assert len([row for row in store.list_messages('qq:group:202') if row['role'] == 'user']) == 2
    finally:
        await delivery.stop()
        await flow.close()


def configure_real_group(runtime):
    store, settings, model, _, _, _, _ = runtime
    settings.data['identity'].update(respond_to_at=False, respond_to_name=False)
    store.upsert_scope({'id': SCOPE, 'plan_enabled': True,
                        'proactive_reply': False, 'activity': 'quiet'})
    original = model.complete

    async def complete(messages, **kwargs):
        if kwargs.get('purpose') != 'planner':
            return await original(messages, **kwargs)
        assert kwargs['before_send']()
        model.calls.append((messages, kwargs))
        payload = json.loads(messages[-1]['content'])
        return {'content': json.dumps({'action': 'reply',
            'target_message_id': payload['current_message_id'],
            'target_user_id': payload['current_user_id'], 'addressed_to_bot': True,
            'intent': '回应各成员自己的请求', 'reason': '合并窗口中有对机器人的直接请求',
            'needed_info': []})}

    model.complete = complete


async def send_real_group(runtime, user, text, kind, *, addressed=True):
    _, _, _, access, _, flow, _ = runtime
    identity = access.identity('onebot', user, '999')
    if addressed and kind == 'name':
        text = '初音，' + text
    at_bot = addressed and kind == 'at'
    metadata = {'at_bot': at_bot, 'trusted_identity': identity,
                'is_admin': access.is_admin(identity), 'is_owner': access.is_admin(identity)}
    if not addressed:
        metadata['mentioned_user_ids'] = ['606']
    return await flow.handle(SCOPE, user, text, [], metadata, direct=at_bot, wait=False)


@pytest.mark.anyio
@pytest.mark.parametrize('kind', ['at', 'name'])
async def test_real_chat_optional_mentions_keep_each_members_local_command_authority(runtime, kind):
    configure_real_group(runtime)
    store, _, model, _, _, flow, sent = runtime
    try:
        await send_real_group(runtime, '101', '记住：我喜欢咖啡', kind)
        await send_real_group(runtime, '404', '记住：我喜欢茶', kind)
        await drain(flow)
        originals = {row['user_id']: row for row in store.list_messages(SCOPE) if row['role'] == 'user'}
        memories = {row['user_id']: row for row in store.list_memories(SCOPE)}
        assert set(memories) == {'101', '404'}
        assert memories['101']['content'] == '我喜欢咖啡'
        assert memories['404']['content'] == '我喜欢茶'
        assert all(memories[user]['source_ids'] == [originals[user]['id']] for user in memories)
        assert [kwargs['purpose'] for _, kwargs in model.calls] == ['planner']
        assert len({metadata['assistant_message_id'] for _, metadata in sent}) == 1
    finally:
        await close_runtime(runtime)


@pytest.mark.anyio
@pytest.mark.parametrize('kind', ['at', 'name'])
async def test_real_chat_optional_mentions_do_not_lend_admin_or_addressing_to_other_members(runtime, kind):
    configure_real_group(runtime)
    store, _, model, _, _, flow, _ = runtime
    victim = store.create_memory({'scope_id': SCOPE, 'user_id': '707',
        'content': '其他成员保留的群内事实', 'status': 'active', 'visibility': 'group'})
    try:
        await send_real_group(runtime, '404', '记住：这只是对另一个成员说的话', kind, addressed=False)
        await send_real_group(runtime, '505', '删除记忆：' + victim['id'], kind)
        await send_real_group(runtime, '101', '记住：管理员自己的音乐偏好', kind)
        await drain(flow)
        assert store.get_memory(victim['id'])['status'] == 'active'
        memories = store.list_memories(SCOPE)
        assert {row['user_id'] for row in memories} == {'707', '101'}
        assert not any('另一个成员' in row['content'] for row in memories)
        own = next(row for row in memories if row['user_id'] == '101')
        admin_source = next(row for row in store.list_messages(SCOPE)
                            if row['role'] == 'user' and row['user_id'] == '101')
        assert own['source_ids'] == [admin_source['id']]
        assert sum(kwargs.get('purpose') == 'planner' for _, kwargs in model.calls) == 1
    finally:
        await close_runtime(runtime)
