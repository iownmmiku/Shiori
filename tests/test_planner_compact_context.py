"""Keep planning input short without losing conversation targets or time anchors."""
from copy import deepcopy
import json

import pytest

from shiori.db import Store
from shiori.settings import DEFAULTS
from shiori.social import SocialEngine
from shiori.timezones import message_time_context, refresh_dispatch_time


@pytest.fixture
def anyio_backend():
    return 'asyncio'


class Config:
    def __init__(self):
        self.value = deepcopy(DEFAULTS)
        self.value['model'].update(base_url='https://fixture.invalid/v1', model='fixture')
        self.value['onebot'].update(enabled=True, chat_policy='blacklist')

    def get(self):
        return deepcopy(self.value)


class Planner:
    async def complete(self, messages, **kwargs):
        self.messages = messages
        assert kwargs['before_send']()
        self.payload = json.loads(messages[-1]['content'])
        return {'content': json.dumps({'action': 'reply',
            'target_message_id': self.payload['current_message_id'],
            'target_user_id': self.payload['current_user_id'], 'addressed_to_bot': True,
            'intent': '回应最新问题', 'reason': '正在向机器人提问', 'needed_info': []})}


@pytest.mark.anyio
@pytest.mark.parametrize('aware', [True, False])
async def test_current_message_has_one_copy_and_original_time_provenance_survives(tmp_path, aware):
    store, config, provider = Store(tmp_path), Config(), Planner()
    config.value['time']['awareness_enabled'] = aware
    social = SocialEngine(store, config, provider)
    try:
        scope = store.upsert_scope({'id': 'qq:group:202', 'plan_enabled': True})
        earlier = [store.add_message(scope['id'], '303', 'user', f'早先的话题 {index}',
            sent_at='2026-10-23T15:00:00Z', received_at='2026-10-23T15:00:01Z')
            for index in range(13)]
        text = 'miku，请继续回答昨天的问题。' * 20
        current = store.add_message(scope['id'], '303', 'user', text, at_bot=True,
            reply_to_source_id=earlier[-1]['id'], sent_at='2026-10-24T16:00:00Z')
        result = await social.plan(scope, current, direct=True)
        payload = provider.payload
        assert result['reply'] and result['target_message_id'] == current['id']
        assert payload['current_message_text'] == text
        assert payload['current_reply_to_source_id'] == earlier[-1]['id']
        assert [item['id'] for item in payload['history']] == [item['id'] for item in earlier]
        assert provider.messages[-1]['content'].count(text) == 1
        if aware:
            for item in payload['history']:
                assert item['time'] == {'anchor': '2026-10-23T23:00:00+08:00', 'anchor_source': 'platform'}
            assert payload['current_message_time']['anchor'] == '2026-10-25T00:00:00+08:00'
            old = deepcopy(payload)
            old['history'] = [dict(item, time=message_time_context(record, config.get()))
                for item, record in zip(payload['history'], earlier)]
            old['history'].append({'id': current['id'], 'speaker': current['user_id'], 'text': text[:700],
                'reply_to_source_id': earlier[-1]['id'], 'time': message_time_context(current, config.get())})
            old_wire = json.dumps(old, ensure_ascii=False)
            refreshed = refresh_dispatch_time(provider.messages, config.get(), purpose='planner')
            new_wire = refreshed[-1]['content']
            assert len(new_wire) < len(old_wire) * .75
            assert json.loads(new_wire)['history'] == payload['history']
        else:
            assert 'current_time' not in payload and 'current_message_time' not in payload
            assert all('time' not in item for item in payload['history'])
        assert store.get_message(current['id']) == current
    finally:
        await social.close()
        store.close()


@pytest.mark.anyio
async def test_compact_history_keeps_unknown_send_time_distinct_from_event_time(tmp_path):
    store, config, provider = Store(tmp_path), Config(), Planner()
    social = SocialEngine(store, config, provider)
    try:
        scope = store.upsert_scope({'id': 'qq:group:202', 'plan_enabled': True})
        old = store.add_message(scope['id'], '304', 'user', '以前提到的一件事',
            received_at='2026-10-23T15:00:00Z')
        current = store.add_message(scope['id'], '303', 'user', 'miku，这件事呢', at_bot=True)
        assert (await social.plan(scope, current, direct=True))['reply']
        history = {item['id']: item for item in provider.payload['history']}
        assert history[old['id']]['time']['anchor_source'] == 'received'
        assert history[old['id']]['time']['anchor'] == '2026-10-23T23:00:00+08:00'
    finally:
        await social.close()
        store.close()
