"""Who a group message is for: the Planner's ordered view of the conversation,
its stated audience, other bots, and the hints passed on to the reply."""
import json
from copy import deepcopy
from types import SimpleNamespace

import pytest

from shiori.chat import ChatService
from shiori.db import Store
from shiori.planner_result import parse_plan, response_format
from shiori.settings import DEFAULTS, Settings as SettingsStore
from shiori.social import SocialEngine

GROUP = 'qq:group:202'


@pytest.fixture
def anyio_backend():
    return 'asyncio'


class Settings:
    def __init__(self):
        self.config = deepcopy(DEFAULTS)
        self.config['model'].update(base_url='https://test.invalid/v1', model='mock')
        self.config['onebot'].update(enabled=True, chat_policy='blacklist')
        self.config['planner'].update(min_interval_ms=0)
        self.config['time']['awareness_enabled'] = False

    def get(self):
        return deepcopy(self.config)


class Planner:
    def __init__(self):
        self.calls, self.plan = [], {}

    async def complete(self, messages, **kwargs):
        self.calls.append((messages, kwargs))
        payload = json.loads(messages[-1]['content'])
        plan = {'action': 'reply', 'target_message_id': payload['current_message_id'],
                'target_user_id': payload['current_user_id'], 'addressed_to_bot': False,
                'intent': '接一句', 'reason': '理由', 'needed_info': [], 'value': 50, **self.plan}
        return {'content': json.dumps(plan, ensure_ascii=False)}

    def payload(self):
        return json.loads(self.calls[-1][0][-1]['content'])


@pytest.fixture
def setup(tmp_path):
    store, settings, planner = Store(tmp_path), Settings(), Planner()
    store.upsert_scope({'id': GROUP, 'cooldown_seconds': 0, 'merge_window_ms': 0})
    social = SocialEngine(store, settings, planner)
    yield store, settings, planner, social
    store.close()


def say(store, user, text, name=None, **metadata):
    return store.add_message(GROUP, user, 'user', text, **({'sender_name': name} if name else {}), **metadata)


@pytest.mark.anyio
async def test_planner_sees_the_conversation_in_order_with_its_own_lines(setup):
    store, _, planner, social = setup
    question = say(store, '303', '这个活动怎么打', name='阿九')
    bot = store.add_message(GROUP, 'assistant', 'assistant', '先把据点清了再说', source_ids=[question['id']])
    say(store, '404', '@阿九 你走狭路吗', name='小林', mentioned_user_ids=['303'])
    say(store, '303', '走了', name='阿九', reply_to_source_id=question['id'])
    current = say(store, '303', '别这么凶嘛', name='阿九', reply_to_bot=True, reply_to_source_id=bot['id'])
    await social.plan(store.get_scope(GROUP), current)
    payload = planner.payload()
    assert [(line['from'], line.get('to')) for line in payload['history']] == [
        ('阿九(303)', None), ('你', ['阿九(303)']), ('小林(404)', ['阿九(303)']), ('阿九(303)', ['阿九(303)'])]
    assert payload['history'][1]['text'] == '先把据点清了再说'
    assert payload['current_from'] == '阿九(303)' and payload['current_to'] == ['你']
    assert payload['current_follows'] == '阿九(303)'
    prompt = planner.calls[-1][0][0]['content']
    assert '"你"就是你本人' in prompt and 'audience' in prompt and 'speaker_intent' in prompt


@pytest.mark.anyio
async def test_stated_audience_decides_whether_it_was_said_to_the_bot(setup):
    store, settings, planner, social = setup
    settings.config['planner']['eagerness'] = 0  # threshold 95: only speech to the bot passes
    planner.plan = {'audience': 'bot', 'addressed_to_bot': False, 'speaker_intent': '在回应机器人刚才的话'}
    result = await social.plan(store.get_scope(GROUP), say(store, '303', '别这么说嘛'))
    assert result['reply'] and result['addressed_to_bot'] and result['speaker_intent'] == '在回应机器人刚才的话'
    planner.plan = {'audience': 'member', 'addressed_to_bot': True, 'speaker_intent': '在问队友抓谁'}
    result = await social.plan(store.get_scope(GROUP), say(store, '303', '现在想抓谁'))
    assert not result['reply'] and result['audience'] == 'member'


@pytest.mark.anyio
async def test_other_bots_are_context_only_even_when_they_mention_the_bot(setup):
    store, settings, planner, social = setup
    settings.config['onebot']['bot_accounts'] = ['228729701']
    say(store, '228729701', '（两只兔耳耷拉下来）', name='兔兔')
    result = await social.plan(store.get_scope(GROUP), say(store, '228729701', '你好呀', at_bot=True), direct=True)
    assert not result['reply'] and '其他机器人' in result['reason'] and not planner.calls
    await social.plan(store.get_scope(GROUP), say(store, '303', '它俩又吵起来了'))
    assert planner.payload()['history'][0]['from'] == '兔兔(228729701)[机器人]'


def test_audience_and_speaker_intent_are_optional_hints():
    base = {'action': 'skip', 'target_message_id': 'm', 'target_user_id': 'u', 'addressed_to_bot': False,
            'intent': '', 'reason': 'r', 'needed_info': []}
    parse = lambda **extra: parse_plan(json.dumps({**base, **extra}, ensure_ascii=False), message_id='m', user_id='u')
    assert 'audience' not in parse() and 'speaker_intent' not in parse()
    plan = parse(audience='member', speaker_intent='  在问队友  ')
    assert plan['audience'] == 'member' and plan['speaker_intent'] == '在问队友'
    assert 'audience' not in parse(audience='everyone') and 'speaker_intent' not in parse(speaker_intent=3)
    schema = response_format('json_schema')['json_schema']['schema']
    assert schema['properties']['audience']['enum'] == ['bot', 'member', 'group', 'none']
    assert {'audience', 'speaker_intent'} <= set(schema['required'])


def test_bot_accounts_setting_is_validated(tmp_path):
    store = SettingsStore(tmp_path)
    assert store.get()['onebot']['bot_accounts'] == []
    store.update({'onebot': {'bot_accounts': '228729701，2257218996\n228729701'}})
    assert store.get()['onebot']['bot_accounts'] == ['228729701', '2257218996']
    for bad in (['abc'], ['123'], [str(10 ** 5 + i) for i in range(51)]):
        with pytest.raises(ValueError):
            store.update({'onebot': {'bot_accounts': bad}})


@pytest.mark.anyio
async def test_reply_prompt_carries_who_the_message_was_for(tmp_path):
    config = deepcopy(DEFAULTS)
    config['time']['awareness_enabled'] = False
    config['context']['enabled'] = False
    config['stickers']['enabled'] = False
    store = Store(tmp_path)
    store.upsert_scope({'id': GROUP, 'auto_memory': False, 'allow_tools': False, 'use_persona_knowledge': False})

    class Model:
        async def complete(self, messages, **options):
            return {'content': '好', 'tool_calls': [], 'usage': {}}

    chat = ChatService(store, SimpleNamespace(get=lambda: deepcopy(config)), Model())
    try:
        current = store.add_message(GROUP, '303', 'user', '现在想抓谁')
        plan = {'target_message_id': current['id'], 'target_user_id': '303', 'intent': '补一句建议',
                'needed_info': [], 'audience': 'member', 'speaker_intent': '在问队友抓谁'}
        messages = chat._context(store.get_scope(GROUP), '303', current['content'], [], [], [],
                                 current['id'], {'planner': plan}, history=[])
        essential = next(item['content'] for item in messages if str(item['content']).startswith('【本轮回复计划与命令回执】'))
        data = json.loads(essential.split('\n', 1)[1])
        assert data['reply_plan']['audience'] == 'member' and data['reply_plan']['speaker_intent'] == '在问队友抓谁'
        assert '不要替被问的人回答' in messages[0]['content']
    finally:
        await chat.close()
        store.close()
