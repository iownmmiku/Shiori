"""No external service: verify planner semantics and policy boundaries with real SQLite."""
import asyncio
import json
from copy import deepcopy

import pytest

from qunyu.db import Store
from shiori.settings import DEFAULTS, resolved_identity
from shiori.social import SocialEngine


@pytest.fixture
def anyio_backend():
    return 'asyncio'


class Settings:
    def __init__(self):
        self.config = deepcopy(DEFAULTS)
        self.config['model'].update(base_url='https://test.invalid/v1', model='test')
        self.config['onebot'].update(chat_policy="whitelist", enabled=True, owner_ids=['101'], allowed_groups=['202'])
        self.config['identity'].update(respond_to_at=False, respond_to_name=False)

    def get(self):
        return deepcopy(self.config)


class Provider:
    def __init__(self):
        self.calls = []
        self.choice = {}
        self.blocker = None
        self.entered = asyncio.Event()

    async def complete(self, messages, **kwargs):
        self.calls.append((messages, kwargs))
        self.entered.set()
        if self.blocker:
            await self.blocker.wait()
        payload = json.loads(messages[-1]['content'])
        result = {'action': 'reply', 'target_message_id': payload['current_message_id'],
                  'target_user_id': payload['current_user_id'], 'addressed_to_bot': False,
                  'intent': '结合音乐兴趣回应对方的分享', 'reason': '分享的话题与人格兴趣相关且尚无人回应',
                  'needed_info': []}
        result.update(self.choice)
        return {'content': json.dumps(result, ensure_ascii=False)}


@pytest.fixture
def setup(tmp_path):
    store, settings, provider = Store(tmp_path), Settings(), Provider()
    store.upsert_scope({'id': 'qq:group:202', 'plan_enabled': True, 'cooldown_seconds': 0})
    social = SocialEngine(store, settings, provider)
    yield store, settings, provider, social
    store.close()


def message(store, text, *, scope='qq:group:202', user='303', **metadata):
    return store.add_message(scope, user, 'user', text, **metadata)


@pytest.mark.anyio
async def test_declarative_interest_message_gets_real_semantic_plan(setup):
    store, _, provider, social = setup
    item = message(store, '我今天重听了那张电子音乐专辑')
    result = await social.plan(store.get_scope(item['scope_id']), item)
    assert result['action'] == 'reply' and result['semantic'] is True
    assert result['intent'] == '结合音乐兴趣回应对方的分享'
    assert result['target_message_id'] == item['id'] and result['target_user_id'] == '303'
    assert provider.calls[0][1]['purpose'] == 'planner'
    assert social.status(item['scope_id'])['last_plan']['reason'] == result['reason']
    assert 'waiting' not in social.status(item['scope_id'])


@pytest.mark.anyio
async def test_mentions_and_quoted_names_can_be_semantically_vetoed(setup):
    store, _, provider, social = setup
    provider.choice.update(action='skip', reason='这里是在引用机器人名字向另一位成员提问', intent='')
    item = message(store, '“初音未来”这首歌名小林听过吗', at_bot=True)
    result = await social.plan(store.get_scope(item['scope_id']), item, direct=True)
    assert result['action'] == 'skip' and not result['addressed_to_bot']
    assert len(provider.calls) == 1


@pytest.mark.anyio
@pytest.mark.parametrize('field,metadata,text,direct', [
    ('trigger_at', {'at_bot': True}, '你好', True),
    ('trigger_reply', {'reply_to_bot': True}, '再说一遍', False),
    ('trigger_name', {}, 'miku你好', False),
])
async def test_disabled_mentions_use_planner_while_disabled_reply_stays_passive(setup, field, metadata, text, direct):
    store, _, provider, social = setup
    scope = store.upsert_scope({'id': 'qq:group:202', field: False, 'proactive_reply': False})
    result = await social.plan(scope, message(store, text, **metadata), direct=direct)
    if field == 'trigger_reply':
        assert result['action'] == 'skip' and not provider.calls
    else:
        assert result['action'] == 'reply' and result['semantic'] and result['explicit']
        assert len(provider.calls) == 1


@pytest.mark.anyio
async def test_enabled_name_trigger_still_works_when_at_is_off(setup):
    store, settings, provider, social = setup
    settings.config['model'].update(base_url='', model='')
    scope = store.upsert_scope({'id': 'qq:group:202', 'trigger_at': False, 'trigger_name': True})
    result = await social.plan(scope, message(store, '初音，帮我想个名字', at_bot=True), direct=True)
    assert result['action'] == 'reply' and not result['semantic'] and not provider.calls


@pytest.mark.anyio
@pytest.mark.parametrize('policy', ['always_reply', 'plan_enabled'])
async def test_private_mandatory_policy_never_calls_or_can_be_vetoed_by_planner(setup, policy):
    store, _, provider, social = setup
    provider.choice.update(action='skip')
    scope = store.upsert_scope({'id': 'qq:private:101', policy: policy == 'always_reply'})
    result = await social.plan(scope, message(store, '嗯', scope=scope['id'], user='101'))
    assert result['action'] == 'reply' and not provider.calls


@pytest.mark.anyio
async def test_planner_off_group_keeps_only_direct_enabled_triggers(setup):
    store, _, provider, social = setup
    scope = store.upsert_scope({'id': 'qq:group:202', 'plan_enabled': False, 'trigger_name': True})
    assert (await social.plan(scope, message(store, '初音未来你觉得如何')) )['action'] == 'reply'
    assert (await social.plan(scope, message(store, '大家说说今晚看什么番')))['action'] == 'skip'
    assert not provider.calls


@pytest.mark.anyio
async def test_web_dialogue_is_not_gated_by_qq_at_name_or_reply_switch(setup):
    store, settings, provider, social = setup
    settings.config['identity'].update(respond_to_at=False, respond_to_name=False, respond_to_reply=False)
    scope = store.upsert_scope({'id': 'web:default', 'plan_enabled': False})
    result = await social.plan(scope, message(store, '你好', scope='web:default', user='owner'), direct=True)
    assert result['action'] == 'reply' and not provider.calls


@pytest.mark.anyio
async def test_without_model_mentions_never_fall_back_to_mandatory_reply(setup):
    store, settings, provider, social = setup
    settings.config['model'].update(base_url='', model='')
    scope = store.get_scope('qq:group:202')
    result = await social.plan(scope, message(store, '初音未来，晚上好'))
    assert result['action'] == 'skip' and not result['semantic']
    assert (await social.plan(scope, message(store, '大家晚上好')))['action'] == 'skip'
    assert not provider.calls


@pytest.mark.anyio
@pytest.mark.parametrize('choice', [{'action': 'wait'}, {'target_user_id': '999'}, {'target_message_id': 'another'},
                                   {'addressed_to_bot': 'yes'}, {'needed_info': 'execute tool'}, {'intent': ''}])
async def test_invalid_or_redirecting_plan_does_not_reply(setup, choice):
    store, _, provider, social = setup
    provider.choice = choice
    result = await social.plan(store.get_scope('qq:group:202'), message(store, '初音未来帮帮我'))
    assert result['action'] == 'skip'


@pytest.mark.anyio
@pytest.mark.parametrize('change', ['permission', 'planner', 'source', 'stop'])
async def test_inflight_semantic_plan_cannot_outlive_source_or_permission(setup, change):
    store, settings, provider, social = setup
    provider.blocker = asyncio.Event()
    item = message(store, '初音未来帮我想想这个旋律')
    pending = asyncio.create_task(social.plan(store.get_scope(item['scope_id']), item))
    await provider.entered.wait()
    if change == 'permission':
        settings.config['onebot']['allowed_groups'] = []
    elif change == 'planner':
        store.upsert_scope({'id': item['scope_id'], 'plan_enabled': False})
    elif change == 'source':
        store.clear_messages(item['scope_id'])
    else:
        stopped = await social.plan(store.get_scope(item['scope_id']), message(store, '不用回答了'))
        assert stopped['interrupted']
    provider.blocker.set()
    assert (await pending)['action'] == 'skip'


def test_identity_scope_inheritance_and_alias_override(setup):
    store, settings, _, _ = setup
    settings.config['identity'].update(name='初音未来', aliases=['初音', 'miku'], respond_to_at=False)
    scope = store.upsert_scope({'id': 'qq:group:202', 'trigger_at': None, 'bot_name': '', 'bot_aliases': []})
    inherited = resolved_identity(settings.get(), scope)
    assert inherited['name'] == '初音未来' and inherited['aliases'] == ['初音未来', '初音', 'miku'] and not inherited['trigger_at']
    scope = store.upsert_scope({'id': scope['id'], 'bot_name': '小葱', 'bot_aliases': ['葱葱'], 'trigger_at': True})
    overridden = resolved_identity(settings.get(), scope)
    assert overridden['name'] == '小葱' and overridden['aliases'] == ['小葱', '葱葱'] and overridden['trigger_at']


@pytest.mark.anyio
@pytest.mark.parametrize('change', ['pause', 'clear', 'identity'])
async def test_queued_planner_payload_is_not_dispatched_after_authorization_changes(setup, change):
    store, settings, _, social = setup
    entered, release = asyncio.Event(), asyncio.Event()
    dispatched = False
    class Queued:
        async def complete(self, messages, **kwargs):
            nonlocal dispatched
            entered.set()
            await release.wait()
            if not kwargs['before_send']():
                raise PermissionError('request invalidated before dispatch')
            dispatched = True
            return {'content': '{}'}
    social.provider = Queued()
    pending = asyncio.create_task(social.plan(store.get_scope('qq:group:202'), message(store, '初音未来你好')))
    await entered.wait()
    if change == 'pause':
        settings.config['paused'] = True
    elif change == 'clear':
        store.clear_messages('qq:group:202')
    else:
        settings.config['identity']['name'] = '新名字'
    release.set()
    assert (await pending)['action'] == 'skip' and not dispatched


@pytest.mark.anyio
async def test_deleted_knowledge_cannot_reenter_planner_from_assistant_history(setup):
    from shiori.chat import ChatService
    from datetime import datetime, timedelta, timezone
    store, settings, provider, _ = setup
    chat = ChatService(store, settings, provider)
    entry = {'id': 'knowledge-one', 'persona_id': 'default', 'status': 'active', 'interest': '音乐',
             'title': '音乐资料', 'text': '此处是已经删除的知识内容', 'url': 'https://example.test/topic',
             'retrieved_at': datetime.now(timezone.utc).isoformat(),
             'valid_until': (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()}
    chat.knowledge._state['items'].append(entry)
    source = message(store, '请查一些音乐信息')
    store.add_message('qq:group:202', 'assistant', 'assistant', '此处是已经删除的知识内容',
                      source_ids=[source['id']], context_source_ids=[source['id']],
                      knowledge_refs={entry['id']: chat._knowledge_fingerprint(entry)}, persona_id='default')
    chat.knowledge.delete(entry['id'])
    await chat.social.plan(store.get_scope('qq:group:202'), message(store, '初音未来你好'))
    assert provider.calls and '此处是已经删除的知识内容' not in provider.calls[-1][0][-1]['content']
    await chat.close()
