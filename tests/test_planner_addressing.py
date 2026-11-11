"""Who a group message is for: the Planner's ordered view of the conversation,
its stated audience, other bots, and the hints passed on to the reply."""
import json
from copy import deepcopy
from datetime import datetime, timedelta, timezone
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
    asked = say(store, '303', '这关好难')
    store.add_message(GROUP, 'assistant', 'assistant', '多练练就好', source_ids=[asked['id']])
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


def gate(store):
    return next(item for item in store.list_events(20) if item['kind'] == 'planner_timing')['details']['gate']


@pytest.mark.anyio
async def test_a_bystander_right_after_the_bots_line_is_not_taken_as_talking_to_it(setup):
    """Replays a real misfire: the bot teased a member who took the wrong
    train; another member's advice to him came right after the bot's line,
    the Planner called the bot its audience, and the bot answered "少管我"."""
    store, settings, planner, social = setup
    settings.config['planner']['eagerness'] = 25  # threshold 82.5
    wrong = say(store, '94354530', '导致非常丝滑的坐反了', name='虚勢丶')
    store.add_message(GROUP, 'assistant', 'assistant', '行吧行吧，总算还知道换乘', source_ids=[wrong['id']])
    seven = say(store, '94354530', '这次坐了七个站才发觉', name='虚勢丶')
    store.add_message(GROUP, 'assistant', 'assistant', '……七个站？你是和地铁杠上了吗？', source_ids=[seven['id']])
    planner.plan = {'audience': 'bot', 'addressed_to_bot': True, 'value': 85, 'speaker_intent': '劝你走路别玩手机'}
    result = await social.plan(store.get_scope(GROUP), say(store, '2631101581', '走路少玩手机吧', name='四川莫'))
    payload = planner.payload()
    assert payload['current_follows'] == '你' and payload['your_partners'] == ['虚勢丶(94354530)']
    assert not result['reply'] and not result['addressed_to_bot'] and '近期没有和你对话' in result['reason']
    assert 'speaker_intent' not in result and 'audience' not in result
    assert gate(store) == {'value': 69, 'planner_value': 85, 'implicit_audience': True, 'threshold': 82.5,
                           'eagerness': 25, 'scored': True, 'audience': 'bot', 'result': 'below_threshold'}
    prompt = planner.calls[-1][0][0]['content']
    assert '紧跟在你发言后不等于对你说' in prompt and 'your_partners' in prompt and '看不到图片内容' in prompt
    # The member it was talking with is still answered without @ or a quote.
    planner.plan = {'audience': 'bot', 'addressed_to_bot': True, 'value': 85, 'speaker_intent': '在向你辩解'}
    result = await social.plan(store.get_scope(GROUP), say(store, '94354530', '我也不想啊', name='虚勢丶'))
    assert result['reply'] and result['addressed_to_bot'] and result['speaker_intent'] == '在向你辩解'
    assert gate(store)['result'] == 'addressed' and 'implicit_audience' not in gate(store)


@pytest.mark.anyio
async def test_a_picture_from_a_bystander_is_not_taken_as_said_to_the_bot(setup):
    store, settings, planner, social = setup
    settings.config['planner']['eagerness'] = 25
    settings.config['planner']['local_filter'] = 'off'   # by default a bystander's bare picture never reaches the Planner
    seven = say(store, '94354530', '这次坐了七个站才发觉', name='虚勢丶')
    store.add_message(GROUP, 'assistant', 'assistant', '你是和地铁杠上了吗', source_ids=[seven['id']])
    planner.plan = {'audience': 'bot', 'addressed_to_bot': True, 'value': 90, 'speaker_intent': '发图吓唬你'}
    picture = say(store, '189927747', '[图片]', name='大小姐', images=[{'file': 'meme.jpg'}])
    result = await social.plan(store.get_scope(GROUP), picture)
    assert planner.payload()['signals']['has_images']
    assert not result['reply'] and gate(store)['value'] == 69 and gate(store)['implicit_audience']


@pytest.mark.anyio
async def test_talking_with_the_bot_lapses_after_ten_minutes(setup):
    store, settings, planner, social = setup
    settings.config['planner']['eagerness'] = 0
    old = (datetime.now(timezone.utc) - timedelta(minutes=11)).isoformat()
    asked = say(store, '303', '这关好难', sent_at=old)
    store.add_message(GROUP, 'assistant', 'assistant', '多练练就好', source_ids=[asked['id']], sent_at=old)
    planner.plan = {'audience': 'bot', 'addressed_to_bot': True, 'value': 95}
    result = await social.plan(store.get_scope(GROUP), say(store, '303', '还是不会'))
    assert planner.payload()['your_partners'] == [] and not result['reply'] and gate(store)['implicit_audience']
    # Quoting or @-ing the bot makes a member a partner too.
    say(store, '404', '那你说怎么办', at_bot=True)
    result = await social.plan(store.get_scope(GROUP), say(store, '404', '快说嘛'))
    assert planner.payload()['your_partners'] == ['404'] and result['reply'] and result['addressed_to_bot']


def test_ascii_quotes_inside_a_value_no_longer_fail_the_plan():
    """Seen in production: speaker_intent quoting a chat line with ASCII quotes
    made 13 of 812 plans invalid JSON. Escaping them keeps the model's own
    decision; anything still broken stays invalid."""
    def plan(intent):
        return ('{"audience":"bot","speaker_intent":"' + intent + '","addressed_to_bot":true,"action":"reply",'
                '"target_message_id":"m","target_user_id":"u","intent":"回应","reason":"在问你","needed_info":[],"value":90}')
    result = parse_plan(plan('在回应你说的"七个站"'), message_id='m', user_id='u')
    assert result['speaker_intent'] == '在回应你说的"七个站"' and result['action'] == 'reply'
    assert parse_plan(plan('问"这个"和"那个" 的区别'), message_id='m', user_id='u')['speaker_intent'] == '问"这个"和"那个" 的区别'
    with pytest.raises(Exception) as caught:
        parse_plan(plan('他说"好",然后走了'), message_id='m', user_id='u')  # quote before a comma: ambiguous
    assert getattr(caught.value, 'code', '') == 'invalid_json'
    with pytest.raises(Exception) as caught:
        parse_plan('{"action":"reply"', message_id='m', user_id='u')
    assert caught.value.code == 'invalid_json'


@pytest.mark.anyio
async def test_bot_lines_are_sent_once_and_quotes_are_asked_as_brackets(setup):
    store, _, planner, social = setup
    social.history_fn = lambda scope_id, user_id: store.list_messages(scope_id, limit=14)
    asked = say(store, '303', '这关好难', name='阿九')
    store.add_message(GROUP, 'assistant', 'assistant', '多练练就好', source_ids=[asked['id']])
    await social.plan(store.get_scope(GROUP), say(store, '303', '还是不会', name='阿九'))
    payload = planner.payload()
    assert 'recent_bot_replies' not in payload
    assert [line['text'] for line in payload['history'] if line['from'] == '你'] == ['多练练就好']
    assert '引用别人的话用「」' in planner.calls[-1][0][0]['content']
