"""Pending jargon that nobody reviews: it can expire, the administrators can be
reminded, and an administrator can approve or reject it from a private chat."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone

import pytest

from shiori import alerts as al
from shiori import social as so
from shiori.api import create_app
from shiori.db import Store
from shiori.settings import DEFAULTS, Settings as SettingsStore
from shiori.social import SocialEngine

GROUP, ADMIN_CHAT = 'qq:group:202', 'qq:private:10001'


class Settings:
    def __init__(self, **language):
        self.config = deepcopy(DEFAULTS)
        self.config['onebot'].update(enabled=True, chat_policy='blacklist')
        self.config['language'].update(language)

    def get(self):
        return deepcopy(self.config)


def candidate(store, term, status='pending', scope=GROUP):
    return store.create_jargon({'scope_id': scope, 'term': term, 'meaning': f'{term} 的意思', 'status': status,
                                'created_by': 'social', 'source_ids': []})


@pytest.fixture
def store(tmp_path):
    value = Store(tmp_path)
    value.upsert_scope({'id': GROUP, 'name': '开黑群'})
    yield value
    value.close()


def test_the_two_day_counts_are_validated(tmp_path):
    settings = SettingsStore(tmp_path)
    assert settings.get()['language']['pending_expire_days'] == 0 and settings.get()['language']['pending_digest_days'] == 0
    settings.update({'language': {'pending_expire_days': 14, 'pending_digest_days': 7}})
    for bad in ({'pending_expire_days': -1}, {'pending_expire_days': 400}, {'pending_digest_days': 1.5}, {'pending_digest_days': True}):
        with pytest.raises(ValueError):
            settings.update({'language': bad})


def test_unreviewed_candidates_expire_and_cannot_come_back(store):
    social = SocialEngine(store, Settings(pending_expire_days=7), None)
    old, kept = candidate(store, '开饭'), candidate(store, '上分', status='approved')
    now = datetime.now(timezone.utc)
    assert social.sweep_pending_jargon(now=now + timedelta(days=6)) == {'expired': 0, 'reminded': False}
    assert social.sweep_pending_jargon(now=now + timedelta(days=8)) == {'expired': 1, 'reminded': False}
    expired = store.get_jargon(old['id'])
    assert expired['status'] == 'rejected' and '7 天未审核' in expired['review_reason']
    assert store.get_jargon(kept['id'])['status'] == 'approved'
    assert any('自动作废' in item['message'] for item in store.list_events(20))
    # Switched off (the default), nothing ever expires.
    late = candidate(store, '下播')
    SocialEngine(store, Settings(), None).sweep_pending_jargon(now=now + timedelta(days=400))
    assert store.get_jargon(late['id'])['status'] == 'pending'


def test_administrators_are_reminded_of_the_backlog_at_the_set_interval(store):
    social, sent = SocialEngine(store, Settings(pending_digest_days=3, pending_expire_days=30), None), []
    social.notify_admins = sent.append
    now = datetime.now(timezone.utc)
    assert social.sweep_pending_jargon(now=now)['reminded'] is False          # nothing waiting
    candidate(store, '开饭')
    candidate(store, '上分')
    assert social.sweep_pending_jargon(now=now + timedelta(days=2))['reminded'] is True
    assert '有 2 条黑话在等审核' in sent[0] and '待审黑话' in sent[0] and '超过 30 天' in sent[0] and '开饭' not in sent[0]
    assert social.sweep_pending_jargon(now=now + timedelta(days=2, hours=12))['reminded'] is False   # reminded recently
    assert social.sweep_pending_jargon(now=now + timedelta(days=29))['reminded'] is True
    assert len(sent) == 2
    quiet = SocialEngine(store, Settings(pending_digest_days=3), None)        # nobody to tell
    assert quiet.sweep_pending_jargon(now=now + timedelta(days=60))['reminded'] is False


def test_the_sweep_runs_at_most_hourly_while_messages_arrive(store, monkeypatch):
    social, runs = SocialEngine(store, Settings(pending_expire_days=7), None), []
    monkeypatch.setattr(social, 'sweep_pending_jargon', lambda: runs.append(1) or setattr(social, '_jargon_swept', so.time.monotonic()))
    for index in range(5):
        social.observe(GROUP, store.add_message(GROUP, '303', 'user', f'消息 {index}'))
    assert len(runs) == 1
    social._jargon_swept -= so.JARGON_SWEEP_SECONDS + 1
    social.observe(GROUP, store.add_message(GROUP, '303', 'user', '一小时后'))
    assert len(runs) == 2


@pytest.mark.anyio
async def test_a_routine_notice_goes_out_even_with_failure_alerts_off(store):
    settings, sent = Settings(), []
    settings.config['access']['administrators'] = [{'platform': 'onebot', 'account_id': '10001', 'instance_id': '', 'label': '管理员'}]

    async def send(scope_id, text):
        sent.append((scope_id, text))
        return True

    alerts = al.Alerts(settings, store, send)
    assert settings.config['alerts']['enabled'] is False and alerts.notice('jargon_digest', '有 2 条黑话在等审核') is True
    import asyncio
    await asyncio.gather(*list(alerts._jobs))
    assert sent == [(ADMIN_CHAT, al.PREFIX + '有 2 条黑话在等审核')]
    settings.config['access']['administrators'] = []
    assert alerts.notice('jargon_digest', 'x') is False


@pytest.fixture
def anyio_backend():
    return 'asyncio'


def test_an_administrator_reviews_by_number_in_a_private_chat(tmp_path):
    app = create_app(tmp_path, start_workers=False)
    store, chat = app.state.store, app.state.chat
    store.upsert_scope({'id': GROUP, 'name': '开黑群'})
    first, second, third = (candidate(store, term) for term in ('开饭', '上分', '下播'))
    admin, review = {'is_owner': True}, lambda text, meta, scope=ADMIN_CHAT: chat._review_jargon(scope, '10001', text, meta)['reply']
    assert chat.is_local_command(ADMIN_CHAT, '待审黑话') and chat.is_local_command(ADMIN_CHAT, '通过黑话：1')
    # Only an administrator, and only in private.
    assert '只有管理员' in review('待审黑话', {})
    assert '私聊' in review('待审黑话', admin, GROUP)
    assert '先发送“待审黑话”' in review('通过黑话：1', admin)
    listing = review('待审黑话', admin)
    assert '共 3 条' in listing and '1. 开饭：开饭 的意思（开黑群）' in listing and '3. 下播' in listing
    assert '请写列表里的编号' in review('通过黑话：7', admin) and '请写列表里的编号' in review('拒绝黑话：', admin)
    assert review('通过黑话：1 3', admin).startswith('已通过 2 条：开饭、下播。')
    assert store.get_jargon(first['id'])['status'] == 'approved' and store.get_jargon(third['id'])['status'] == 'approved'
    assert 'QQ 私聊中通过' in store.get_jargon(first['id'])['review_reason']
    # Numbers keep their meaning after part of the list is handled; handled ones are not touched again.
    again = review('拒绝黑话：全部', admin)
    assert again.startswith('已拒绝 1 条：上分。') and '另有 2 条此前已经处理过' in again
    assert store.get_jargon(second['id'])['status'] == 'rejected' and store.get_jargon(first['id'])['status'] == 'approved'
    assert review('待审黑话', admin) == '现在没有待审核的黑话。'
    # A list older than half an hour has to be asked for again.
    candidate(store, '摸鱼')
    review('待审黑话', admin)
    chat._jargon_reviews[(ADMIN_CHAT, '10001')]['at'] -= 3600
    assert '先发送“待审黑话”' in review('通过黑话：1', admin)
    store.close()
