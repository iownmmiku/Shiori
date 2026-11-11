"""Opening a topic in a quiet group (topic_starter.py): off by default and hard
to trigger; when it does speak it is one line, stored as the bot's own."""
import json
from copy import deepcopy
from datetime import datetime, timedelta, timezone

import pytest

from shiori import topic_starter as ts
from shiori.db import Store
from shiori.settings import DEFAULTS, Settings as SettingsStore

GROUP = 'qq:group:202'
# Stored messages always carry the real insertion time, so the tests run around the real present
# and move the older messages back in the database.
NOON = datetime.now(timezone.utc).replace(microsecond=0)
LOCAL_HOUR = (NOON + timedelta(hours=8)).hour                 # the default zone is UTC+8


def backdate(store, message, at):
    stamp = at.isoformat().replace('+00:00', 'Z')
    with store._lock, store._conn:
        store._conn.execute("UPDATE messages SET created_at=?, data=json_set(data,'$.created_at',?) WHERE id=?",
                            (stamp, stamp, message['id']))


@pytest.fixture
def anyio_backend():
    return 'asyncio'


class Settings:
    def __init__(self, **options):
        self.config = deepcopy(DEFAULTS)
        self.config['onebot'].update(enabled=True, chat_policy='blacklist')
        self.config['topic_starter'] = dict(ts.DEFAULTS, enabled=True, start_hour=0, end_hour=24, **options)

    def get(self):
        return deepcopy(self.config)


class Writer:
    def __init__(self, reply='昨晚那场流星雨有人拍到了吗'):
        self.calls, self.reply = [], reply

    async def complete(self, messages, **kwargs):
        self.calls.append((messages, kwargs))
        if isinstance(self.reply, Exception):
            raise self.reply
        return {'content': self.reply}


class Group:
    """A store with one group whose last messages are `quiet_for` old at NOON."""
    def __init__(self, tmp_path, *, quiet_for=timedelta(hours=4), messages=14, proactive=True):
        self.store = Store(tmp_path)
        self.store.upsert_scope({'id': GROUP, 'name': '观星群', 'proactive_reply': proactive})
        self.sent = []
        for index in range(messages):
            at = NOON - quiet_for - timedelta(minutes=(messages - index) * 3)
            backdate(self.store, self.store.add_message(GROUP, str(300 + index % 3), 'user', f'聊到第 {index} 句',
                                                        sender_name=f'成员{index % 3}'), at)

    async def send(self, scope_id, text):
        self.sent.append((scope_id, text))
        return True

    def starter(self, settings, writer, **options):
        self.noted = []
        return ts.TopicStarter(self.store, settings, writer, self.send,
                               note_reply=lambda scope_id, user_id, proactive=False: self.noted.append((scope_id, proactive)), **options)


def test_the_settings_section_is_off_by_default_and_validated(tmp_path):
    assert ts.DEFAULTS['enabled'] is False and ts.validate({}) == ts.DEFAULTS
    for bad in ({'enabled': 'yes'}, {'quiet_minutes': 5}, {'gap_hours': 1}, {'start_hour': 22, 'end_hour': 10}, {'end_hour': 30}, {'x': 1}):
        with pytest.raises(ValueError):
            ts.validate(bad)
    settings = SettingsStore(tmp_path)
    assert settings.get()['topic_starter'] == ts.DEFAULTS
    assert settings.update({'topic_starter': {'enabled': True, 'quiet_minutes': 240}})['topic_starter']['quiet_minutes'] == 240
    for text, expected in (('“昨晚的流星雨有人拍到了吗”\n（解释）', '昨晚的流星雨有人拍到了吗'), ('SKIP', ''), ('skip。', ''), ('', ''),
                           ('@成员A 你在吗', ''), ('字' * 300, '字' * ts.MAX_CHARS)):
        assert ts._line(text) == expected


@pytest.mark.anyio
async def test_a_quiet_but_lively_group_gets_one_line_stored_as_the_bots_own(tmp_path):
    group, writer = Group(tmp_path), Writer()
    starter = group.starter(Settings(), writer)
    assert await starter.tick(NOON) == GROUP
    assert group.sent == [(GROUP, '昨晚那场流星雨有人拍到了吗')] and group.noted == [(GROUP, True)]
    last = group.store.list_messages(GROUP, limit=1)[-1]
    assert last['role'] == 'assistant' and last['content'] == '昨晚那场流星雨有人拍到了吗' and last['metadata']['topic_starter'] is True
    system, options = writer.calls[0][0][0]['content'], writer.calls[0][1]
    payload = json.loads(writer.calls[0][0][1]['content'])
    assert options['purpose'] == 'topic_starter' and 'SKIP' in system
    assert payload['local_time'] == (NOON + timedelta(hours=8)).strftime('%H:%M')
    assert len(payload['recent']) == 14 <= ts.RECENT_FOR_PROMPT and payload['recent'][-1] == {'from': '成员1', 'text': '聊到第 13 句'}
    event = next(item['details'] for item in group.store.list_events(10) if item['kind'] == 'topic_starter')
    assert event['delivered'] is True and event['quiet_minutes'] == 243
    # It has the last word now, and the gap holds even after members answer and go quiet again.
    assert await starter.tick(NOON + timedelta(minutes=5)) is None
    group.store.add_message(GROUP, '303', 'user', '拍到了一颗')
    assert await starter.tick(NOON + timedelta(hours=5)) is None and len(writer.calls) == 1
    # A restart forgets nothing: the stored line says when it last spoke first.
    fresh = group.starter(Settings(), writer)
    assert await fresh.tick(NOON + timedelta(hours=5)) is None and len(writer.calls) == 1
    group.store.close()


@pytest.mark.anyio
@pytest.mark.parametrize('case', ['off', 'paused', 'night', 'chatting', 'dead', 'no_unprompted', 'quiet_mode', 'offline', 'not_allowed'])
async def test_it_stays_silent_whenever_a_condition_is_missing(tmp_path, case):
    settings, writer, at, options = Settings(), Writer(), NOON, {}
    group = Group(tmp_path, quiet_for=timedelta(minutes=20) if case == 'chatting' else timedelta(hours=4),
                  messages=3 if case == 'dead' else 14, proactive=case != 'no_unprompted')
    if case == 'off':
        settings.config['topic_starter']['enabled'] = False
    elif case == 'paused':
        settings.config['paused'] = True
    elif case == 'night':                                     # the present hour is outside the allowed hours
        settings.config['topic_starter'].update({'start_hour': LOCAL_HOUR + 1, 'end_hour': 24} if LOCAL_HOUR < 23
                                                else {'start_hour': 0, 'end_hour': 23})
    elif case == 'quiet_mode':
        group.store.upsert_scope({'id': GROUP, 'activity': 'quiet'})
    elif case == 'offline':
        options['connected'] = lambda: False
    elif case == 'not_allowed':
        options['allowed'] = lambda scope_id: False
    starter = group.starter(settings, writer, **options)
    assert await starter.tick(at) is None and not group.sent and not writer.calls
    group.store.close()


@pytest.mark.anyio
async def test_a_skip_or_a_failure_sends_nothing_and_is_not_retried_at_once(tmp_path):
    group, writer = Group(tmp_path), Writer('SKIP')
    starter = group.starter(Settings(), writer)
    assert await starter.tick(NOON) is None and not group.sent and len(writer.calls) == 1
    assert await starter.tick(NOON + timedelta(minutes=30)) is None and len(writer.calls) == 1
    writer.reply = RuntimeError('model down')
    assert await starter.tick(NOON + timedelta(hours=3)) is None and not group.sent and len(writer.calls) == 2
    assert any(item['kind'] == 'topic_starter_error' for item in group.store.list_events(10))
    group.store.close()


@pytest.mark.anyio
async def test_a_message_arriving_while_it_writes_cancels_the_line(tmp_path):
    group = Group(tmp_path)

    class Slow(Writer):
        async def complete(self, messages, **kwargs):
            group.store.add_message(GROUP, '303', 'user', '有人在吗')
            return await super().complete(messages, **kwargs)
    starter = group.starter(Settings(), Slow())
    assert await starter.tick(NOON) is None and not group.sent
    group.store.close()


@pytest.mark.anyio
async def test_an_undelivered_line_is_not_stored(tmp_path):
    group = Group(tmp_path)

    async def failing(scope_id, text):
        return False
    starter = ts.TopicStarter(group.store, Settings(), Writer(), failing)
    assert await starter.tick(NOON) is None
    assert group.store.list_messages(GROUP, limit=1)[-1]['role'] == 'user'
    assert await starter.tick(NOON + timedelta(minutes=5)) is None     # and the gap still applies
    group.store.close()
