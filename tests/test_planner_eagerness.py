"""Unprompted replies: the Planner's 0—100 score against an eagerness
threshold that rises after each unprompted reply. Local fakes only."""
import json
from copy import deepcopy

import pytest

from shiori.db import Store
from shiori.planner_result import PlanValidationError, parse_plan, response_format
from shiori.settings import DEFAULTS, Settings as SettingsStore
from shiori.social import REFRACTORY_HALF_LIFE, SocialEngine

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

    def get(self):
        return deepcopy(self.config)


class Planner:
    """Answers with the scripted score/addressing for each call."""
    def __init__(self):
        self.calls, self.value, self.addressed, self.action = [], 80, False, 'reply'

    async def complete(self, messages, **kwargs):
        self.calls.append((messages, kwargs))
        payload = json.loads(messages[-1]['content'])
        plan = {'action': self.action, 'target_message_id': payload['current_message_id'],
                'target_user_id': payload['current_user_id'], 'addressed_to_bot': self.addressed,
                'intent': '补充一句' if self.action == 'reply' else '', 'reason': '理由', 'needed_info': []}
        if self.value is not None:
            plan['value'] = self.value
        return {'content': json.dumps(plan)}


@pytest.fixture
def setup(tmp_path):
    store, settings, planner = Store(tmp_path), Settings(), Planner()
    store.upsert_scope({'id': GROUP, 'cooldown_seconds': 0, 'merge_window_ms': 0, 'followup_seconds': 90})
    social = SocialEngine(store, settings, planner)
    yield store, settings, planner, social
    store.close()


def say(store, text='有人知道这个活动怎么打吗', user='303', **metadata):
    return store.add_message(GROUP, user, 'user', text, **metadata)


async def plan(store, social, **kwargs):
    return await social.plan(store.get_scope(GROUP), say(store, **kwargs))


def options(settings):
    return {**settings.get()['planner']}


def test_value_is_parsed_leniently_but_never_invented():
    base = {'action': 'skip', 'target_message_id': 'm', 'target_user_id': 'u', 'addressed_to_bot': False,
            'intent': '', 'reason': 'r', 'needed_info': []}
    read = lambda **extra: parse_plan(json.dumps({**base, **extra}), message_id='m', user_id='u').get('value')
    assert read() is None
    assert read(value=72) == 72 and read(value=71.6) == 72 and read(value='65') == 65
    assert read(value=140) == 100 and read(value=-3) == 0
    for bad in (True, 'high', [80]):
        with pytest.raises(PlanValidationError):
            read(value=bad)
    schema = response_format('json_schema')['json_schema']['schema']
    assert schema['properties']['value'] == {'type': 'integer'} and 'value' in schema['required']


def test_threshold_follows_eagerness_and_rises_after_each_unprompted_reply(setup):
    _, settings, _, social = setup
    for eagerness, expected in ((0, 95), (50, 70), (100, 45)):
        settings.config['planner']['eagerness'] = eagerness
        assert social.reply_threshold(GROUP, options(settings)) == expected
    settings.config['planner']['eagerness'] = 50
    social.note_reply(GROUP, '303')  # an addressed reply never raises it
    assert social.reply_threshold(GROUP, options(settings)) == 70
    social.note_reply(GROUP, '303', proactive=True)
    assert social.reply_threshold(GROUP, options(settings)) == 90
    social._proactive_replies[GROUP][0] -= REFRACTORY_HALF_LIFE
    assert social.reply_threshold(GROUP, options(settings)) == 80
    social._proactive_replies[GROUP][0] -= 10 * REFRACTORY_HALF_LIFE
    assert social.reply_threshold(GROUP, options(settings)) == 70


@pytest.mark.anyio
async def test_score_decides_unprompted_replies(setup):
    store, _, planner, social = setup
    planner.value = 69
    result = await plan(store, social)
    assert not result['reply'] and '69' in result['reason'] and result['threshold'] == 70
    planner.value = 70
    assert (await plan(store, social))['reply']
    # The model's own "skip" does not veto a high score; its reason becomes the intent.
    planner.action, planner.value = 'skip', 85
    result = await plan(store, social)
    assert result['reply'] and result['intent']
    # A model that gives no score keeps its own decision at a middle value.
    planner.action, planner.value = 'reply', None
    assert (await plan(store, social))['reply']


@pytest.mark.anyio
async def test_one_unprompted_reply_raises_the_bar_for_the_next(setup):
    store, _, planner, social = setup
    planner.value = 80
    assert (await plan(store, social))['reply']
    social.note_reply(GROUP, '303', proactive=True)
    result = await plan(store, social, user='404')
    assert not result['reply'] and result['threshold'] == 90
    planner.value = 95
    assert (await plan(store, social, user='404'))['reply']


@pytest.mark.anyio
async def test_speech_to_the_bot_ignores_the_threshold(setup):
    store, settings, planner, social = setup
    settings.config['planner']['eagerness'] = 0
    planner.value, planner.addressed = 20, True
    result = await plan(store, social)
    assert result['reply'] and result['addressed_to_bot']


@pytest.mark.anyio
async def test_when_held_back_only_a_recent_partner_reaches_the_planner(setup):
    store, settings, planner, social = setup
    for _ in range(2):
        social.note_reply(GROUP, '303', proactive=True)  # threshold 70 + ~40
    assert social.reply_threshold(GROUP, options(settings)) > 100
    # A member the bot was not talking to: skipped before any Planner call.
    result = await plan(store, social, user='505')
    assert not result['reply'] and '门槛' in result['reason'] and not planner.calls
    # The member it just answered: judged, answered only if talking to the bot.
    planner.value = 100
    result = await plan(store, social, user='303')
    assert not result['reply'] and len(planner.calls) == 1 and '不是在对机器人说话' in result['reason']
    planner.addressed = True
    assert (await plan(store, social, user='303'))['reply']


@pytest.mark.anyio
async def test_cooldown_no_longer_lets_a_follow_up_through_unaddressed(setup):
    store, _, planner, social = setup
    store.upsert_scope({'id': GROUP, 'cooldown_seconds': 600})
    social.note_reply(GROUP, '303')
    planner.value = 100
    result = await plan(store, social, text='刚才那个活动我也想知道', user='303')
    assert not result['reply'] and planner.calls
    planner.addressed = True
    assert (await plan(store, social, text='那你再说说', user='303'))['reply']
    count = len(planner.calls)
    assert not (await plan(store, social, user='404'))['reply'] and len(planner.calls) == count


@pytest.mark.anyio
async def test_a_group_can_set_its_own_eagerness(setup):
    store, settings, planner, social = setup
    store.upsert_scope({'id': GROUP, 'planner_eagerness': 100})
    planner.value = 50
    result = await plan(store, social)
    assert result['reply']
    store.upsert_scope({'id': GROUP, 'planner_eagerness': None})
    assert not (await plan(store, social))['reply']
    with pytest.raises(ValueError):
        store.upsert_scope({'id': GROUP, 'planner_eagerness': 101})


def test_eagerness_setting_is_validated(tmp_path):
    store = SettingsStore(tmp_path)
    assert store.get()['planner']['eagerness'] == 50
    store.update({'planner': {'eagerness': 0}})
    assert store.get()['planner']['eagerness'] == 0
    for bad in (101, -1, 50.5, '50'):
        with pytest.raises(ValueError):
            store.update({'planner': {'eagerness': bad}})


@pytest.mark.anyio
async def test_gate_is_logged_with_the_planner_timing(setup):
    store, _, planner, social = setup
    planner.value = 40
    await plan(store, social)
    timing = next(item for item in store.list_events(20) if item['kind'] == 'planner_timing')
    assert timing['details']['gate'] == {'value': 40, 'threshold': 70.0, 'eagerness': 50,
                                         'scored': True, 'result': 'below_threshold'}
