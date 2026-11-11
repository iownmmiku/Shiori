"""Feedback on unprompted replies: being told "nobody asked you" right after
joining in is recorded and makes the bot hold back for a while."""
from copy import deepcopy

import pytest

from shiori import social as so
from shiori.db import Store
from shiori.runtime_logs import summary
from shiori.settings import DEFAULTS
from shiori.social import SocialEngine

GROUP = 'qq:group:202'


class Settings:
    def __init__(self):
        self.config = deepcopy(DEFAULTS)
        self.config['onebot'].update(enabled=True, chat_policy='blacklist')

    def get(self):
        return deepcopy(self.config)


@pytest.fixture
def setup(tmp_path):
    store = Store(tmp_path)
    store.upsert_scope({'id': GROUP, 'cooldown_seconds': 0})
    social = SocialEngine(store, Settings(), None)
    yield store, social
    store.close()


def say(store, social, user, text, **metadata):
    message = store.add_message(GROUP, user, 'user', text, **metadata)
    social.observe(GROUP, message)
    return message


def threshold(store, social):
    scope = store.get_scope(GROUP)
    return social.reply_threshold(GROUP, social._planner_options(scope))


def feedback(store):
    return [item['details'] for item in store.list_events(50) if item['kind'] == 'planner_feedback']


def test_being_told_off_after_joining_in_raises_the_threshold_once(setup):
    store, social = setup
    social.note_reply(GROUP, '303', proactive=True)
    before = threshold(store, social)
    told = say(store, social, '404', '没喊你')
    assert threshold(store, social) == pytest.approx(before + so.FEEDBACK_BOOST, abs=0.5)
    event = feedback(store)[0]
    assert event['feedback'] == 'unwanted' and event['from_target'] is False and event['source_ids'] == [told['id']]
    assert '来自其他成员' in summary('planner_feedback', event)
    say(store, social, '404', '闭嘴')          # the same reply is not counted twice
    assert len(feedback(store)) == 1
    # The effect wears off: half of it is left after one half-life.
    social._rebukes[GROUP][0] -= so.FEEDBACK_HALF_LIFE
    social._proactive_replies[GROUP].clear()
    assert threshold(store, social) == pytest.approx(95 - 50 * 0.5 + so.FEEDBACK_BOOST / 2, abs=0.5)


@pytest.mark.parametrize('text', ['没喊你', '没叫你啊', '又没问你', '谁问你了', '没人叫你', '不是在说你', '没跟你说话',
                                  '关你什么事', '别插嘴', '闭嘴吧'])
def test_the_usual_ways_of_saying_it_are_recognised(setup, text):
    store, social = setup
    social.note_reply(GROUP, '303', proactive=True)
    say(store, social, '303', text)
    assert feedback(store) and feedback(store)[0]['from_target'] is True


@pytest.mark.parametrize('text', ['没问题', '你说得对', '叫你呢', '好的谢谢', '有人知道你们几点开吗'])
def test_ordinary_replies_are_not_feedback(setup, text):
    store, social = setup
    social.note_reply(GROUP, '303', proactive=True)
    say(store, social, '303', text)
    assert not feedback(store)


def test_it_must_be_about_the_bots_line(setup):
    store, social = setup
    # A reply the bot was asked for is not an unprompted one.
    social.note_reply(GROUP, '303', proactive=False)
    say(store, social, '303', '没喊你')
    assert not feedback(store)
    # Later chatter between other members is theirs, unless it quotes the bot.
    social.note_reply(GROUP, '303', proactive=True)
    for text in ('今天打本吗', '打，等我吃完饭', '行'):
        say(store, social, '404', text)
    say(store, social, '505', '没喊你')
    assert not feedback(store)
    say(store, social, '505', '没喊你', reply_to_bot=True)
    assert len(feedback(store)) == 1
    # A long message that merely contains the words is something else.
    social.note_reply(GROUP, '303', proactive=True)
    say(store, social, '303', '我刚才那句话的意思是这件事本来就没叫你去处理，是他自己非要揽下来的，结果现在出了问题又来怪别人')
    assert len(feedback(store)) == 1


def test_only_the_first_minutes_after_the_reply_count(setup):
    store, social = setup
    social.note_reply(GROUP, '303', proactive=True)
    social._last_unprompted[GROUP]['at'] -= so.FEEDBACK_SECONDS + 1
    say(store, social, '303', '没喊你')
    assert not feedback(store) and GROUP not in social._last_unprompted
