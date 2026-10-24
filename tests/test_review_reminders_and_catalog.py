"""Issue regressions: scoped summaries, stable model IDs and local reminder calendars."""
import asyncio
import json
from pathlib import Path
import shutil
import subprocess
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

from auth_helpers import password_login
from mikubot.api import create_app
from mikubot.chat import ChatService
from mikubot.db import Store
from mikubot.model_catalog import ModelCatalog
from mikubot.onebot import is_quiet
from mikubot.scheduler import TaskScheduler
from mikubot.settings import Settings
from mikubot.timeparse import calendar_fields, next_occurrence, parse_due
import mikubot.timeparse as timeparse


NY = 'America/New_York'


class SummaryProvider:
    def __init__(self):
        self.calls = []

    async def complete(self, messages, **kwargs):
        self.calls.append((messages, kwargs))
        return {'content': '明天先开会，再吃饭。', 'tool_calls': [], 'usage': {}}


@pytest.mark.parametrize('command', ['总结最近聊天', '/总结'])
def test_summary_with_history_uses_current_scope_output_and_sources(tmp_path, command):
    store, settings, provider = Store(tmp_path), Settings(tmp_path), SummaryProvider()
    store.upsert_scope({'id': 'web:default', 'output': {'enabled': False}})
    chat = ChatService(store, settings, provider)

    async def scenario():
        try:
            source = store.add_message('web:default', 'owner', 'user', '明天先开会再吃饭')
            result = await chat.respond('web:default', 'owner', command)
            assert result['reply'] == '明天先开会，再吃饭。'
            messages, options = provider.calls[0]
            assert options['purpose'] == 'summary'
            assert '一条完整消息' in messages[0]['content']
            assert source['id'] in result['context_source_ids']
            assert store.get_message(result['message_id'])['content'] == result['reply']
        finally:
            await chat.close()
            store.close()
    asyncio.run(scenario())


def test_unbound_profile_prevents_model_deletion_without_mutating_settings(tmp_path):
    settings = Settings(tmp_path)
    catalog = ModelCatalog(settings, None)
    source = catalog.save_source({'name': 'Fixture', 'base_url': 'https://fixture.invalid/v1'})
    model = catalog.add_models({'provider_id': source['id'], 'models': ['chat-model']})['added'][0]
    profiles = settings.get()['bot_profiles']
    profiles['items'].append({'id': 'unused', 'name': '备用机器人', 'values': {'model_id': model['id']}})
    settings.update({'bot_profiles': profiles})
    before, disk = settings.get(), settings.path.read_bytes()
    with pytest.raises(ValueError, match='备用机器人'):
        catalog.delete_model(model['id'])
    assert settings.get() == before and settings.path.read_bytes() == disk
    profiles['items'][-1]['values']['model_id'] = ''
    settings.update({'bot_profiles': profiles})
    assert catalog.delete_model(model['id']) == {'ok': True}


def test_renaming_actual_model_preserves_references_and_allows_old_name_again(tmp_path):
    settings = Settings(tmp_path)
    catalog = ModelCatalog(settings, None)
    source = catalog.save_source({'name': 'Fixture', 'base_url': 'https://fixture.invalid/v1'})
    first = catalog.add_models({'provider_id': source['id'], 'models': ['model-a']})['added'][0]
    catalog.update_routing({'chat': first['id']})
    renamed = catalog.update_model(first['id'], {'model': 'model-b'})
    second = catalog.add_models({'provider_id': source['id'], 'models': ['model-a']})['added'][0]
    assert renamed['id'] == first['id'] != second['id']
    assert settings.get()['routing']['chat'] == first['id']
    assert {m['model'] for m in settings.get()['providers']} == {'model-a', 'model-b'}
    assert catalog.add_models({'provider_id': source['id'], 'models': ['model-a']})['skipped'] == ['model-a']
    with pytest.raises(ValueError, match='同名模型'):
        catalog.update_model(second['id'], {'model': 'model-b'})


@pytest.mark.parametrize('text', ['明天上午9点提醒我喝水', '2026-10-21上午9点提醒我喝水'])
def test_natural_and_explicit_dates_use_configured_timezone(text):
    due, _, _ = parse_due(text, datetime(2026, 10, 20, 12, tzinfo=timezone.utc), settings={'time': {'timezone': NY}})
    assert datetime.fromisoformat(due) == datetime(2026, 10, 21, 13, tzinfo=timezone.utc)


def test_relative_hours_are_elapsed_time_and_days_use_local_calendar():
    now = datetime(2026, 3, 8, 6, 30, tzinfo=timezone.utc)
    due, _, _ = parse_due('1小时后提醒我喝水', now, settings={'time': {'timezone': NY}})
    assert datetime.fromisoformat(due) == now + timedelta(hours=1)
    tomorrow, _, _ = parse_due('1天后提醒我喝水', now, settings={'time': {'timezone': NY}})
    assert datetime.fromisoformat(tomorrow) == now + timedelta(hours=23)


def test_nonexistent_time_rejected_and_overlap_chooses_first_occurrence():
    cfg = {'time': {'timezone': NY}}
    with pytest.raises(ValueError, match='不存在'):
        parse_due('2026-3-8上午2点30提醒我喝水', datetime(2026, 3, 7, tzinfo=timezone.utc), settings=cfg)
    due, _, _ = parse_due('2026-11-1上午1点30提醒我喝水', datetime(2026, 11, 1, 4, tzinfo=timezone.utc), settings=cfg)
    assert datetime.fromisoformat(due) == datetime(2026, 11, 1, 5, 30, tzinfo=timezone.utc)
    with pytest.raises(ValueError, match='日期不存在'):
        parse_due('2027-2-30上午9点提醒我喝水', datetime(2027, 2, 1, tzinfo=timezone.utc), settings=cfg)


def test_non_hour_timezone_and_long_overdue_weekly_calendar():
    due, _, _ = parse_due('明天上午9点提醒我喝水', datetime(2026, 10, 20, tzinfo=timezone.utc), settings={'time': {'timezone': 'Asia/Kathmandu'}})
    assert due == '2026-10-21T03:15:00+00:00'
    first = '2020-01-05T14:00:00+00:00'
    task = dict(due_at=first, repeat='weekly', **calendar_fields(first, timezone_name=NY))
    following, _ = next_occurrence(task, datetime(2026, 11, 9, 12, tzinfo=timezone.utc))
    assert following['due_at'] == '2026-11-15T14:00:00+00:00'


def test_invalid_task_calendar_is_rejected_atomically(tmp_path):
    store = Store(tmp_path)
    try:
        task = store.create_task({'title': '测试', 'kind': 'reminder', 'due_at': '2027-10-30T13:00:00Z', 'timezone': NY})
        for patch in ({'timezone': 'Invalid/Zone'}, {'wall_time': '25:00:00'}, {'calendar_anchor': '2027-02-30'}):
            with pytest.raises(ValueError):
                store.update_task(task['id'], patch)
            assert store.get_task(task['id']) == task
    finally:
        store.close()


@pytest.mark.parametrize('repeat,first,expected', [
    ('daily', '2026-10-31T13:00:00+00:00', '2026-11-01T14:00:00+00:00'),
    ('weekly', '2026-10-25T13:00:00+00:00', '2026-11-01T14:00:00+00:00'),
    ('daily', '2026-03-07T14:00:00+00:00', '2026-03-08T13:00:00+00:00'),
    ('weekly', '2026-03-01T14:00:00+00:00', '2026-03-08T13:00:00+00:00'),
])
def test_local_calendar_preserves_daily_and_weekly_clock_across_dst(repeat, first, expected):
    task = dict(due_at=first, repeat=repeat, **calendar_fields(first, timezone_name=NY))
    following, skipped = next_occurrence(task, datetime.fromisoformat(first), {'time': {'timezone': 'Asia/Tokyo'}})
    assert datetime.fromisoformat(following['due_at']) == datetime.fromisoformat(expected)
    assert following['timezone'] == NY and following['wall_time'] == '09:00:00' and not skipped


def test_repeating_missing_clock_is_skipped_and_overlap_not_delivered_twice():
    first = '2026-03-07T07:30:00+00:00'
    task = dict(due_at=first, repeat='daily', **calendar_fields(first, timezone_name=NY))
    following, skipped = next_occurrence(task, datetime.fromisoformat(first))
    assert following['due_at'] == '2026-03-09T06:30:00+00:00' and skipped == 1
    ambiguous = '2026-11-01T05:30:00+00:00'
    task = dict(due_at=ambiguous, repeat='daily', **calendar_fields(ambiguous, timezone_name=NY))
    following, _ = next_occurrence(task, datetime.fromisoformat(ambiguous))
    assert following['due_at'] == '2026-11-02T06:30:00+00:00'


def test_old_tasks_gain_calendar_on_first_edit_and_global_changes_do_not_move_saved_tasks(tmp_path):
    settings, store = Settings(tmp_path), Store(tmp_path)
    settings.update({'time': {'timezone': NY}})
    store.configure_timezone(settings)
    try:
        task = store.create_task({'title': '喝水', 'kind': 'reminder', 'due_at': '2026-10-31T13:00:00+00:00', 'repeat': 'daily'})
        assert task['timezone'] == NY and task['wall_time'] == '09:00:00'
        settings.update({'time': {'timezone': 'Asia/Tokyo'}})
        patched = store.update_task(task['id'], {'title': '喝水改名'})
        following, _ = next_occurrence(patched, datetime(2026, 10, 31, 13, tzinfo=timezone.utc), settings)
        assert following['due_at'] == '2026-11-01T14:00:00+00:00'
        # Emulate a legacy JSON record without rewriting the public task schema.
        legacy = dict(task)
        for field in ('timezone', 'wall_time', 'calendar_anchor'):
            legacy.pop(field)
        with store._lock, store._conn:
            store._conn.execute('UPDATE tasks SET data=? WHERE id=?', (json.dumps(legacy), task['id']))
        old = store.update_task(task['id'], {'title': '旧提醒'})
        assert old['timezone'] == 'Asia/Tokyo' and old['wall_time'] == '22:00:00'
    finally:
        store.close()


@pytest.mark.parametrize('platform', ['onebot', 'qqofficial'])
@pytest.mark.parametrize('window,stamp', [
    (('22:00', '23:00'), '2026-10-20T02:30:00+00:00'),
    (('22:00', '07:00'), '2026-10-20T04:30:00+00:00'),
    (('22:00', '23:00'), '2026-12-20T03:30:00+00:00'),
])
def test_qq_reminders_wait_for_configured_quiet_window(tmp_path, platform, window, stamp):
    settings, store = Settings(tmp_path), Store(tmp_path)
    patch = {'time': {'timezone': NY}}
    if platform == 'onebot':
        patch['onebot'] = {'enabled': True, 'access_token': 'fixture-access-token'}
        scope_id = 'qq:group:123'
    else:
        patch.update(onebot={'adapter': 'official'}, qq_official={'enabled': True, 'app_id': 'fixture-app', 'app_secret': 'fixture-secret'})
        scope_id = 'qqofficial:group:fixture-group'
    settings.update(patch)
    store.upsert_scope({'id': scope_id, 'quiet_start': window[0], 'quiet_end': window[1]})
    now, sent = datetime.fromisoformat(stamp), []

    class Bridge:
        def status(self): return {'connected': True}
        async def send(self, sid, content):
            sent.append((sid, content))
            return True

    scheduler = TaskScheduler(store, settings, Bridge())
    task = store.create_task({'scope_id': scope_id, 'title': '喝水', 'kind': 'reminder', 'due_at': now.isoformat()})
    async def scenario():
        try:
            await scheduler.tick(now)
            assert not sent and store.get_task(task['id'])['status'] == 'pending'
            await scheduler.tick(now + timedelta(hours=8))
            await scheduler.tick(now + timedelta(hours=9))
            assert sent == [(scope_id, '⏰ 提醒：喝水')]
        finally:
            store.close()
    asyncio.run(scenario())


def test_quiet_default_and_full_natural_chat_calendar(tmp_path, monkeypatch):
    assert is_quiet({'quiet_start': '22:00', 'quiet_end': '07:00'}, datetime(2026, 10, 20, 14, 30, tzinfo=timezone.utc))
    real_datetime = datetime
    class FixedDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            fixed = real_datetime(2026, 10, 20, 12, tzinfo=timezone.utc)
            return fixed.astimezone(tz) if tz else fixed.replace(tzinfo=None)
    monkeypatch.setattr(timeparse, 'datetime', FixedDatetime)
    # Receipt, original-message interpretation and future-time validation now
    # share a clock; freezing only the parser would model a delayed replay.
    monkeypatch.setattr('mikubot.timezones.datetime', FixedDatetime)
    monkeypatch.setattr('mikubot.chat.datetime', FixedDatetime)
    store, settings, provider = Store(tmp_path), Settings(tmp_path), SummaryProvider()
    settings.update({'time': {'timezone': NY}})
    chat = ChatService(store, settings, provider)
    async def scenario():
        try:
            assert chat.is_local_command('web:default', '明天上午9点提醒我喝水')
            result = await chat.respond('web:default', 'owner', '明天上午9点提醒我喝水')
            task = store.list_tasks()[0]
            assert task['due_at'].startswith('2026-10-21T13:00:00')
            assert NY in result['reply'] and '2026-10-21 09:00' in result['reply']
            settings.update({'time': {'timezone': 'Asia/Tokyo'}})
            changed = await chat.respond('web:default', 'owner', '把喝水提醒改到明天上午10点')
            assert NY in changed['reply'] and '10:00' in changed['reply']
            assert store.list_tasks()[0]['due_at'].startswith('2026-10-21T14:00:00')
            await chat.respond('web:default', 'owner', '你好')
            assert 'Asia/Tokyo' in provider.calls[-1][0][0]['content']
            assert '(UTC+8)' not in provider.calls[-1][0][0]['content']
        finally:
            await chat.close()
            store.close()
    asyncio.run(scenario())


def test_task_api_interprets_explicit_zone_and_rejects_dst_gap(tmp_path):
    app = create_app(tmp_path, start_workers=False)
    with TestClient(app) as client:
        password_login(client, app)
        response = client.post('/api/tasks', json={'title': '每周开会', 'kind': 'reminder', 'repeat': 'weekly', 'due_at': '2027-10-30T09:00', 'timezone': NY})
        assert response.status_code == 200, response.text
        task = response.json()
        assert task['due_at'].startswith('2027-10-30T13:00:00')
        assert task['timezone'] == NY and task['wall_time'] == '09:00:00'
        bad = client.post('/api/tasks', json={'title': '不存在', 'due_at': '2027-03-14T02:30', 'timezone': NY})
        assert bad.status_code == 400 and '不存在' in bad.text
        invalid = client.post('/api/tasks', json={'title': '无效时区', 'due_at': '2027-03-14T09:00', 'timezone': 'Invalid/Zone'})
        assert invalid.status_code == 400 and 'IANA' in invalid.text
        naive = client.post('/api/tasks', json={'title': '无时区', 'due_at': '2027-03-14T09:00'})
        assert naive.status_code == 400
        cancelled = client.post('/api/tasks/' + task['id'] + '/cancel')
        assert cancelled.status_code == 200
        retry = client.post('/api/tasks/' + task['id'] + '/retry')
        assert retry.status_code == 200
        assert retry.json()['wall_time'] == '09:00:00' and retry.json()['calendar_anchor'] == task['calendar_anchor']
        following, _ = next_occurrence(retry.json(), datetime(2027, 11, 1, 12, tzinfo=timezone.utc))
        assert datetime.fromisoformat(following['due_at']).astimezone(ZoneInfo(NY)).weekday() == 5


def test_reminder_ui_shows_saved_timezone_instead_of_browser_timezone():
    node = shutil.which('node')
    if not node:
        pytest.skip('Optional frontend regression requires Node.js')
    result = subprocess.run([node, str(Path(__file__).with_name('frontend_reminder_timezones.cjs'))],
                            capture_output=True, text=True, encoding='utf-8', timeout=20)
    assert result.returncode == 0, result.stdout + result.stderr
