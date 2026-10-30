"""Native storage, identity, generation and diary boundaries without paid APIs."""
import asyncio
import copy
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
import json
from functools import wraps

import pytest

from shiori.db import Store
from shiori.life_schedule import CharacterSchedule
from shiori.settings import DEFAULTS


def async_test(function):
    @wraps(function)
    def run(*args, **kwargs):
        return asyncio.run(function(*args, **kwargs))
    return run


class Settings:
    def __init__(self):
        self.value = copy.deepcopy(DEFAULTS)
        self.value['model'].update(base_url='https://model.example/v1', model='fixture')

    def get(self):
        return copy.deepcopy(self.value)


def slot(start='00:00', end='24:00', title='休息', status='free', **extra):
    return dict(start=start, end=end, title=title, status=status, **extra)


def profile():
    return {'world_type': 'original', 'world_name': '星海', 'countries': [],
            'timezone': 'Asia/Hong_Kong', 'weekly': [slot(days=list(range(1, 8)))]}


class Provider:
    def __init__(self):
        self.calls = []
        self.responses = []
        self.block = None

    async def complete(self, messages, **kwargs):
        kwargs['before_send']()
        self.calls.append((messages, kwargs))
        if self.block:
            await self.block.wait()
        if not self.responses:
            raise AssertionError('unexpected model call')
        value = self.responses.pop(0)
        if isinstance(value, Exception):
            raise value
        return {'content': value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)}


@pytest.fixture
def service(tmp_path):
    store = Store(tmp_path)
    settings, provider = Settings(), Provider()
    value = CharacterSchedule(store, settings, provider, tmp_path)
    yield value
    store.close()


def prepare(service, day='2026-10-29', status='free'):
    pid = service.store.list_personas()[0]['id']
    service.save({'persona_id': pid, 'date': day, 'kind': 'profile', 'value': profile()})
    service.save({'persona_id': pid, 'date': day, 'kind': 'day',
                  'value': {'summary': '角色创作的一天', 'slots': [slot(status=status)]}})
    return pid


async def finish(service, job):
    await service._tasks[job['id']]
    return service.job(job['id'])


def test_defaults_never_delay_chat_and_cached_reads_never_call_model(service):
    pid = prepare(service)
    scope = service.store.upsert_scope({'id':'qq:group:123','persona_id':pid})
    assert not service.get_settings()['enabled']
    assert service.context(scope['id'], now='2026-10-29T12:00:00+08:00') is None
    assert not service.contact_policy(scope['id'])['active']
    assert service.state(pid, '2026-10-29')['day']['summary'] == '角色创作的一天'
    assert service.provider.calls == []


def test_context_only_returns_public_plan_and_control_is_separate(service):
    pid = prepare(service, status='busy')
    scope_id = 'qq:private:123'
    service.store.upsert_scope({'id':scope_id,'persona_id':pid})
    service.save_settings({'enabled': True})
    when = '2026-10-29T12:00:00+08:00'
    context = service.context(scope_id, now=when)
    assert context['status'] == 'busy' and context['fictional_schedule']
    assert context['reply_control'] is False
    assert 'diaries' not in context
    service.save_settings({'reply_control': True, 'control_scope': 'selected', 'scope_ids': []})
    assert not service.contact_policy(scope_id, when)['active']
    service.save_settings({'scope_ids': [scope_id]})
    policy = service.contact_policy(scope_id, when)
    assert policy['active'] and policy['persona_key'] and policy['revision']
    assert not service.contact_policy('web:default', when)['active']


def test_missing_day_falls_through_without_generation(service):
    pid = prepare(service)
    service.store.upsert_scope({'id':'qq:group:3','persona_id':pid})
    service.save_settings({'enabled': True, 'reply_control': True})
    assert not service.contact_policy('qq:group:3', '2026-11-01T12:00:00+08:00')['active']
    assert not service.provider.calls


def test_stale_save_rejected_and_snapshot_restores_without_touching_memory(service):
    pid = prepare(service)
    before = service.state(pid, '2026-10-29')
    payload = {'persona_id': pid, 'date': '2026-10-29', 'kind': 'day', 'revision': before['revision'],
               'value': {'summary': 'changed', 'slots': [slot(status='busy')]}}
    state = service.save(payload)
    with pytest.raises(ValueError, match='已被修改'):
        service.save(payload)
    service.restore({'persona_id': pid, 'id': state['snapshots'][0]['id']})
    assert service.state(pid, '2026-10-29')['day']['summary'] == '角色创作的一天'
    assert service.store.stats()['memories'] == 0


def test_persona_prompt_change_does_not_reuse_old_plan(service):
    pid = prepare(service)
    persona = service.store.get_persona(pid)
    service.store.save_persona(dict(persona, system_prompt='completely different'))
    assert service.state(pid, '2026-10-29')['profile'] is None
    assert service.state(pid, '2026-10-29')['day'] is None


@pytest.mark.parametrize('kind,value', [
    ('profile', profile()),
    ('day', {'slots': [slot()]}),
    ('calendar', {'events': []}),
    ('recurrences', []),
])
def test_stale_editor_identity_is_rejected_for_every_mutation(service, kind, value):
    pid = prepare(service)
    before = service.state(pid, '2026-10-29')
    persona = service.store.get_persona(pid)
    service.store.save_persona(dict(persona, system_prompt='another role identity'))
    with pytest.raises(ValueError, match='人格设定已变化'):
        service.save({'persona_id': pid, 'date': '2026-10-29', 'kind': kind,
                      'value': value, 'persona_key': before['persona_key'], 'revision': before['revision']})
    current = service.state(pid, '2026-10-29')
    assert current['profile'] is None and current['day'] is None and current['recurrences'] == []
    assert current['revision'] > before['revision']


def test_legacy_revision_and_explicit_key_cover_identity_change_before_persist(service):
    pid = prepare(service)
    original = service.state(pid)
    persona = service.store.get_persona(pid)
    service.store.save_persona(dict(persona, system_prompt='identity A'))
    first = service.state(pid)
    with pytest.raises(ValueError, match='已被修改'):
        service.save({'persona_id': pid, 'kind': 'profile', 'value': profile(), 'revision': original['revision']})
    service.store.save_persona(dict(persona, system_prompt='identity B'))
    second = service.state(pid)
    assert first['revision'] == second['revision'] and first['persona_key'] != second['persona_key']
    with pytest.raises(ValueError, match='人格设定已变化'):
        service.save({'persona_id': pid, 'kind': 'profile', 'value': profile(),
                      'revision': first['revision'], 'persona_key': first['persona_key']})


def test_stale_identity_cannot_restore_or_start_generation(service):
    pid = prepare(service)
    before = service.state(pid)
    persona = service.store.get_persona(pid)
    service.store.save_persona(dict(persona, name='changed role name'))
    with pytest.raises(ValueError, match='人格设定已变化'):
        service.restore({'persona_id': pid, 'id': before['snapshots'][0]['id'], 'persona_key': before['persona_key']})
    with pytest.raises(ValueError, match='人格设定已变化'):
        service.submit({'persona_id': pid, 'kind': 'profile', 'persona_key': before['persona_key']})
    assert not service.provider.calls


@async_test
async def test_generation_persists_each_stage_with_shared_provider_purpose(service):
    pid = service.store.list_personas()[0]['id']
    service.provider.responses = [profile(), {'events': []}, {'summary': 'new day', 'slots': [slot()]}]
    result = await finish(service, service.submit({'persona_id': pid, 'date': '2026-10-29'}))
    assert result['status'] == 'completed', result
    assert [x[1]['purpose'] for x in service.provider.calls] == ['schedule'] * 3
    assert service.usage()['calls'] == 3
    assert service.state(pid, '2026-10-29')['day']['summary'] == 'new day'


@async_test
async def test_failed_generation_retains_valid_profile_and_consumed_quota(service):
    pid = service.store.list_personas()[0]['id']
    service.provider.responses = [profile(), RuntimeError('upstream disconnected')]
    result = await finish(service, service.submit({'persona_id': pid, 'date': '2026-10-29'}))
    assert result['status'] == 'failed'
    assert service.state(pid, '2026-10-29')['profile']
    assert service.usage()['calls'] == 2
    assert 'upstream' not in result['message']


@async_test
async def test_invalid_json_retry_counts_and_daily_quota_is_enforced(service):
    pid = service.store.list_personas()[0]['id']
    service.save_settings({'daily_calls': 1})
    service.provider.responses = ['not json', profile()]
    result = await finish(service, service.submit({'persona_id': pid, 'kind': 'profile'}))
    assert result['status'] == 'failed' and '次数已达上限' in result['message']
    assert len(service.provider.calls) == 1
    assert service.usage()['calls'] == 1


@async_test
async def test_stale_generation_after_manual_edit_is_not_committed(service):
    pid = prepare(service)
    service.provider.block = asyncio.Event()
    service.provider.responses = [profile()]
    job = service.submit({'persona_id': pid, 'kind': 'profile'})
    await asyncio.sleep(.01)
    service.save({'persona_id': pid, 'kind': 'profile', 'value': dict(profile(), identity='manual')})
    service.provider.block.set()
    result = await finish(service, job)
    assert result['status'] == 'failed'
    assert service.state(pid)['profile']['identity'] == 'manual'


@async_test
async def test_cancel_stops_generation_and_keeps_previous_plan(service):
    pid = prepare(service)
    service.provider.block = asyncio.Event()
    job = service.submit({'persona_id': pid, 'kind': 'day'})
    await asyncio.sleep(.01)
    task = service._tasks[job['id']]
    service.cancel(job['id'])
    await task
    assert service.job(job['id'])['status'] == 'cancelled'
    assert service.state(pid, '2026-10-29')['day']['summary'] == '角色创作的一天'


def test_preview_is_read_only_and_does_not_capture_messages(service):
    pid = prepare(service)
    before = service.state(pid, '2026-10-29')['revision']
    result = service.preview({'persona_id': pid, 'date': '2026-10-29', 'time': '12:30'})
    assert result['read_only'] and not result['model_called']
    assert result['current']['status'] == 'free'
    assert before == service.state(pid, '2026-10-29')['revision']
    assert not service.provider.calls


def test_preview_never_creates_unknown_scopes_or_reports_another_persona_policy(service):
    pid = prepare(service)
    service.save_settings({'enabled': True, 'reply_control': True})
    before = service.store.list_scopes()
    assert service.context('qq:group:9999') is None
    assert not service.contact_policy('qq:group:9999')['active']
    with pytest.raises(ValueError, match='已有会话'):
        service.preview({'persona_id': pid, 'date': '2026-10-29', 'time': '12:30', 'scope_id':'qq:group:9999'})
    assert service.store.list_scopes() == before
    other = service.store.save_persona({'id':'another', 'name':'另一个角色', 'description':'另一个人格', 'system_prompt':'扮演另一个角色'})
    service.store.upsert_scope({'id':'qq:group:8888', 'persona_id':other['id']})
    with pytest.raises(ValueError, match='其他人格'):
        service.preview({'persona_id': pid, 'date':'2026-10-29', 'time':'12:30', 'scope_id':'qq:group:8888'})


def test_diary_material_is_explicit_scoped_bounded_and_deduplicated(service):
    pid = prepare(service)
    for scope in ('qq:private:11', 'qq:private:22'):
        service.store.upsert_scope({'id':scope,'persona_id':pid})
    assert not service.capture_message('qq:private:11', '11', 'user', 'secret')
    service.save_settings({'enabled': True, 'diary_chat_enabled': True, 'diary_scopes': ['qq:private:11']})
    source = service.store.add_message('qq:private:11', '11', 'user', 'secret')
    for _ in range(2):
        assert service.capture_message('qq:private:11', '11', 'user', 'secret', source['id'])
    assert not service.capture_message('qq:private:22', '22', 'user', 'other secret')
    assert not service.capture_message('qq:private:11', '11', 'user', '/command')
    with service.store._lock:
        assert service.store._conn.execute('SELECT COUNT(*) FROM life_materials').fetchone()[0] == 1
    service.save_settings({'diary_scopes': []})
    with service.store._lock:
        assert service.store._conn.execute('SELECT COUNT(*) FROM life_materials').fetchone()[0] == 0


@async_test
async def test_diary_uses_one_scope_and_never_writes_actual_memory(service):
    day = (datetime.now(timezone.utc) - timedelta(days=1)).date().isoformat()
    pid = prepare(service, day)
    for scope in ('qq:private:11', 'qq:private:22'):
        service.store.upsert_scope({'id':scope,'persona_id':pid})
    service.save_settings({'enabled': True, 'diary_chat_enabled': True, 'diary_scopes': ['qq:private:11', 'qq:private:22']})
    then = datetime.fromisoformat(day + 'T12:00:00+08:00')
    for scope, user, text in [('qq:private:11', '11', 'first private conversation'),
                              ('qq:private:22', '22', 'other private secret')]:
        source = service.store.add_message(scope, user, 'user', text)
        assert service.capture_message(scope, user, 'user', text, source['id'], now=then)
    service.provider.responses = [{'title': '一天', 'mood': '开心', 'content': '角色创作日记'}]
    result = await finish(service, service.submit({'persona_id': pid, 'date': day, 'kind': 'diary', 'scope_id': 'qq:private:11'}))
    assert result['status'] == 'completed', result
    sent = service.provider.calls[0][0][1]['content']
    assert 'first private conversation' in sent and 'other private secret' not in sent
    assert service.diary(pid, day, 'qq:private:11')['fictional']
    assert service.diary(pid, day, 'qq:private:22') is None
    assert service.store.stats()['memories'] == 0


def test_future_diary_rejected_before_model_call(service):
    pid = prepare(service)
    with pytest.raises(ValueError, match='已经结束'):
        service.submit({'persona_id': pid, 'date': '2099-01-01', 'kind': 'diary'})
    assert not service.provider.calls


@async_test
async def test_automatic_schedule_off_never_generates_and_manual_day_is_not_overwritten(service):
    pid = prepare(service)
    await service.tick('2026-10-30T12:00:00+08:00')
    assert not service._tasks
    service.save_settings({'enabled': True, 'diary_enabled': False})
    await service.tick('2026-10-29T12:00:00+08:00')
    assert not service._tasks


def test_settings_reject_embedding_unknown_scopes_invalid_bounds(service):
    with pytest.raises(ValueError):
        service.save_settings({'scope_ids': ['qq:group:missing']})
    with pytest.raises(ValueError):
        service.save_settings({'slow_min_seconds': 600, 'slow_max_seconds': 10})
    with pytest.raises(ValueError):
        service.save_settings({'enabled': 'true'})
    with pytest.raises(ValueError):
        service.save_settings({'daily_calls': False})
    with pytest.raises(ValueError, match='时区'):
        service.save_settings({'timezone': 'Invalid/Timezone'})


def test_missing_selected_persona_recovers_workbench_selector(service):
    assert service.state('already-deleted-persona')['persona_id'] == service.store.list_personas()[0]['id']


def test_workbench_reports_role_local_today_without_requiring_today_plan(service, monkeypatch):
    import shiori.life_schedule as schedule_module
    monkeypatch.setattr(schedule_module, '_at', lambda now=None: datetime.fromisoformat('2026-10-29T03:00:00+00:00'))
    pid = prepare(service, day='2026-10-20')
    service.save({'persona_id': pid, 'kind': 'profile', 'value': dict(profile(), timezone='America/New_York')})
    state = service.state(pid, '2000-01-01')
    assert state['today'] == '2026-10-28' and state['date'] == '2000-01-01'
    assert state['current'] is None


@async_test
async def test_zero_daily_quota_stops_model_calls_but_allows_manual_edits(service):
    service.save_settings({'daily_calls': 0})
    pid = prepare(service)
    assert service.state(pid, '2026-10-29')['day']['summary'] == '角色创作的一天'
    result = await finish(service, service.submit({'persona_id': pid, 'kind': 'profile'}))
    assert result['status'] == 'failed' and '次数已达上限' in result['message']
    assert service.provider.calls == []
    assert service.usage()['calls'] == service.usage()['limit'] == 0


def test_workbench_without_any_personas_returns_empty_data(service):
    for persona in service.store.list_personas():
        service.store.delete_persona(persona['id'])
    state = service.state()
    assert state['personas'] == [] and state['persona_id'] == ''
    assert state['day'] is None and state['diaries'] == [] and state['snapshots'] == []


def test_native_sqlite_backup_contains_plans_and_restart_retains_data(service, tmp_path):
    pid = prepare(service)
    another = CharacterSchedule(service.store, service.settings, service.provider, tmp_path)
    assert another.state(pid, '2026-10-29')['day']
    with service.store._lock:
        assert service.store._conn.execute('PRAGMA quick_check').fetchone()[0] == 'ok'


def test_hot_context_cache_excludes_snapshots_and_invalidates_after_edits(service, monkeypatch):
    pid = prepare(service)
    service.store.upsert_scope({'id': 'qq:private:7', 'persona_id': pid})
    service.save_settings({'enabled': True})
    service._record_cache.clear()
    service._cache_bytes = 0
    reads = []
    original = service._record
    def read(persona_id):
        reads.append(persona_id)
        return original(persona_id)
    monkeypatch.setattr(service, '_record', read)
    when = '2026-10-29T12:00:00+08:00'
    first = service.context('qq:private:7', now=when)
    for _ in range(30):
        assert service.context('qq:private:7', now=when) == first
    assert len(reads) == 1
    assert 'snapshots' not in service._record_cache[pid]['record']
    first['slot']['title'] = 'caller cannot mutate cache'
    assert service.context('qq:private:7', now=when)['slot']['title'] != first['slot']['title']
    service.save({'persona_id': pid, 'date': '2026-10-29', 'kind': 'day', 'value': {'summary': 'updated', 'slots': [slot(status='busy')]}})
    assert service.context('qq:private:7', now=when)['status'] == 'busy'


@async_test
async def test_reload_of_older_backup_recreates_tables_and_stops_old_writes(service):
    pid = prepare(service)
    service.provider.block = asyncio.Event()
    job = service.submit({'persona_id': pid, 'kind': 'profile'})
    await asyncio.sleep(.01)
    task = service._tasks[job['id']]
    with service.store._lock, service.store._conn:
        service.store._conn.execute('DROP TABLE life_personas')
        service.store._conn.execute('DELETE FROM life_jobs')
    service.reload()
    await task
    assert service.jobs() == []
    assert service.state(pid)['profile'] is None


@async_test
async def test_close_then_restore_reload_allows_new_manual_generation(service):
    pid = prepare(service)
    await service.close()
    service.reload()
    service.provider.responses = [profile()]
    result = await finish(service, service.submit({'persona_id': pid, 'kind': 'profile'}))
    assert result['status'] == 'completed', result


@async_test
async def test_optional_adaptive_fails_closed_and_does_not_repeat_each_ping(service):
    # Force a known local clock, while still exercising the real async helper.
    now = datetime.now(timezone.utc).astimezone(__import__('zoneinfo').ZoneInfo('Asia/Hong_Kong'))
    day = now.date().isoformat()
    pid = prepare(service, day=day, status='busy')
    service.store.upsert_scope({'id': 'qq:private:8', 'persona_id': pid})
    service.save_settings({'enabled': True, 'reply_control': True})
    service.provider.responses = [RuntimeError('temporary model failure')]
    policy = await service.review_contact('qq:private:8', 'hello')
    assert policy['status'] == 'busy'
    await service.review_contact('qq:private:8', 'hello again')
    assert len(service.provider.calls) == 1


@async_test
async def test_quota_reservation_is_atomic_across_background_and_adaptive(service):
    pid = prepare(service)
    service.save_settings({'daily_calls': 3})
    service.provider.responses = [profile()] * 20
    results = await asyncio.gather(*[service._structured(pid, 'profile', {}, lambda obj: obj,
        purpose='schedule_adaptive' if index % 2 else 'schedule') for index in range(20)], return_exceptions=True)
    assert sum(not isinstance(x, Exception) for x in results) == 3
    assert len(service.provider.calls) == service.usage()['calls'] == 3


@async_test
async def test_disabling_auto_generation_cancels_inflight_and_keeps_saved_plan(service):
    pid = prepare(service)
    service.save_settings({'enabled': True})
    service.provider.block = asyncio.Event()
    job = service.submit({'persona_id': pid, 'kind': 'profile'}, automatic=True)
    await asyncio.sleep(.01)
    task = service._tasks[job['id']]
    service.save_settings({'enabled': False})
    await task
    assert service.job(job['id'])['status'] == 'cancelled'
    assert service.state(pid, '2026-10-29')['profile']['world_name'] == '星海'


@async_test
async def test_revoking_diary_material_while_model_waits_discards_old_result(service):
    day = (datetime.now(timezone.utc) - timedelta(days=1)).date().isoformat()
    pid = prepare(service, day)
    service.store.upsert_scope({'id': 'qq:private:9', 'persona_id': pid})
    service.save_settings({'enabled': True, 'diary_chat_enabled': True, 'diary_scopes': ['qq:private:9']})
    service.provider.block = asyncio.Event()
    service.provider.responses = [{'title': 'Old', 'content': 'must not persist', 'mood': ''}]
    job = service.submit({'persona_id': pid, 'date': day, 'kind': 'diary', 'scope_id': 'qq:private:9'})
    await asyncio.sleep(.01)
    service.save_settings({'diary_enabled': False})
    service.provider.block.set()
    result = await finish(service, job)
    assert result['status'] == 'failed'
    assert service.diary(pid, day, 'qq:private:9') is None


def test_tomorrow_generation_does_not_reset_consumed_contact_window(service):
    from shiori.life_rules.adaptive import create_adjustment, target_activity
    pid = prepare(service, status='busy')
    scope_id = 'qq:private:10'
    service.store.upsert_scope({'id': scope_id, 'persona_id': pid})
    service.save_settings({'enabled': True, 'reply_control': True})
    _, record = service._record(pid)
    now = datetime.fromisoformat('2026-10-29T12:00:00+08:00')
    target = target_activity(record['days']['2026-10-29'], now)
    choice = {'action': 'brief_exception', 'activity_kind': 'rest', 'importance': 'urgent',
              'delay_minutes': 5, 'reason': '有限短答', 'tone': 'concerned', 'notice': ''}
    adjustment = create_adjustment(choice, target, now, umo=scope_id, cid=scope_id,
        persona_key=record['key'], schedule_revision=record['revision'])
    adjustment['_revision'] = record['revision']
    service._adjustments[(pid, scope_id)] = adjustment
    assert service.contact_policy(scope_id, now + timedelta(minutes=1))['status'] == 'free'
    service.save({'persona_id': pid, 'date': '2026-10-30', 'kind': 'day', 'value': {'slots': [slot()]}})
    current = service._adjustments[(pid, scope_id)]
    assert current['until'] == adjustment['until']
    assert service.contact_policy(scope_id, now + timedelta(minutes=6))['status'] == 'busy'
    assert create_adjustment(choice, target, now + timedelta(minutes=6), umo=scope_id, cid=scope_id,
        persona_key=record['key'], schedule_revision=current['source_revision'], previous=current) is None


def test_manual_days_follow_edited_profile_timezone_without_losing_slots(service):
    pid = prepare(service, status='busy')
    service.save({'persona_id': pid, 'kind': 'profile', 'value': dict(profile(), timezone='America/New_York')})
    saved = service.state(pid, '2026-10-29')['day']
    assert saved['timezone'] == 'America/New_York' and saved['origin'] == 'manual'
    assert saved['summary'] == '角色创作的一天'
    preview = service.preview({'persona_id': pid, 'date': '2026-10-29', 'time': '20:00'})
    assert preview['current']['status'] == 'busy'
    assert preview['current']['date'] == '2026-10-29'


def test_changing_role_timezone_invalidates_contact_window_anchored_to_old_clock(service):
    pid = prepare(service, status='busy')
    _, record = service._record(pid)
    identity = (pid, 'qq:private:clock')
    service._adjustments[identity] = {'date': '2026-10-29', 'persona_key': record['key'],
                                    'source_slot': record['days']['2026-10-29']['slots'][0]}
    service.save({'persona_id': pid, 'kind': 'profile', 'value': dict(profile(), timezone='America/New_York')})
    assert identity not in service._adjustments


def test_annual_edits_refresh_manual_day_events_and_preserve_untouched_provenance(service):
    pid = prepare(service)
    service.save({'persona_id': pid, 'date': '2026-10-29', 'kind': 'calendar',
                  'value': {'events': [{'id': 'party', 'title': '庆祝', 'start': '2026-10-29', 'end': '2026-10-29'}]}})
    assert service.state(pid, '2026-10-29')['day']['events'][0]['title'] == '庆祝'
    _, record = service._record(pid)
    record['calendars']['2026']['events'][0]['origin'] = 'public_calendar'
    service._commit(pid, record)
    events = service.state(pid, '2026-10-29')['calendar']['events']
    service.save({'persona_id': pid, 'date': '2026-10-29', 'kind': 'calendar', 'value': {'events': events}})
    assert service.state(pid, '2026-10-29')['calendar']['events'][0]['origin'] == 'public_calendar'
    service.save({'persona_id': pid, 'date': '2026-10-29', 'kind': 'calendar', 'value': {'events': []}})
    state = service.state(pid, '2026-10-29')
    assert state['day']['events'] == [] and state['day']['summary'] == '角色创作的一天'


def test_recurrences_refresh_manual_days_without_annual_calendar_and_keep_unaffected_cache(service):
    pid = prepare(service)
    _, record = service._record(pid)
    for day in ('2026-10-30', '2026-10-31'):
        record['days'][day] = dict(record['days']['2026-10-29'], date=day, origin='generated')
    service._commit(pid, record)
    rule = {'id': 'birthday', 'title': '生日', 'month': 10, 'day': 29}
    service.save({'persona_id': pid, 'kind': 'recurrences', 'value': [rule]})
    assert service.state(pid, '2026-10-29')['day']['events'][0]['title'] == '生日'
    assert service.state(pid, '2026-10-30')['day']['origin'] == 'generated'
    service.save({'persona_id': pid, 'kind': 'recurrences', 'value': [dict(rule, day=30)]})
    assert service.state(pid, '2026-10-29')['day']['events'] == []
    assert service.state(pid, '2026-10-30')['day'] is None
    assert service.state(pid, '2026-10-31')['day']['origin'] == 'generated'
    service.save({'persona_id': pid, 'date': '2026-10-30', 'kind': 'day', 'value': {'slots': [slot()]}})
    assert service.state(pid, '2026-10-30')['day']['events'][0]['title'] == '生日'
    assert service.provider.calls == []


@async_test
async def test_regeneration_uses_current_weekly_and_explicit_date_locks_only(service):
    pid = prepare(service)
    service.save({'persona_id': pid, 'date': '2026-10-29', 'kind': 'calendar', 'value': {'events': []}})
    weekly = [slot('00:00', '06:00', title='固定休息', status='busy', locked=True, days=list(range(1, 8))),
              slot('06:00', '24:00', days=list(range(1, 8)))]
    service.save({'persona_id': pid, 'kind': 'profile', 'value': dict(profile(), weekly=weekly)})
    manual = [slot('00:00', '02:00'), slot('02:00', '03:00', title='日期约定', status='slow', locked=True),
              slot('03:00', '24:00')]
    service.save({'persona_id': pid, 'date': '2026-10-29', 'kind': 'day', 'value': {'slots': manual}})
    for _ in range(2):
        service.provider.responses = [{'slots': [slot()]}]
        result = await finish(service, service.submit({'persona_id': pid, 'date': '2026-10-29', 'kind': 'day'}))
        assert result['status'] == 'completed', result
    _, record = service._record(pid)
    assert [s['title'] for s in record['locked_days']['2026-10-29']] == ['日期约定']
    # Simulate a restored record whose generated plan inherited an earlier
    # weekly template: those derived rows are never independent user locks.
    record['profile']['weekly'][0]['title'] = '新的周表休息'
    service._commit(pid, record)
    service.provider.responses = [{'slots': [slot()]}]
    result = await finish(service, service.submit({'persona_id': pid, 'date': '2026-10-29', 'kind': 'day'}))
    assert result['status'] == 'completed', result
    slots = service.state(pid, '2026-10-29')['day']['slots']
    assert [(s['start'], s['end'], s['title']) for s in slots] == [
        ('00:00', '02:00', '新的周表休息'), ('02:00', '03:00', '日期约定'),
        ('03:00', '06:00', '新的周表休息'), ('06:00', '24:00', '休息')]


@async_test
async def test_manual_day_locks_survive_independent_lock_archive_pruning(service):
    pid = prepare(service)
    service.save({'persona_id': pid, 'date': '2026-10-29', 'kind': 'calendar', 'value': {'events': []}})
    service.save({'persona_id': pid, 'date': '2026-10-29', 'kind': 'day',
                  'value': {'slots': [slot(title='日期锁定', status='busy', locked=True)]}})
    _, record = service._record(pid)
    record['locked_days'] = {}  # Older than the bounded independent archive.
    service._commit(pid, record)
    service.provider.responses = [{'slots': [slot()]}]
    result = await finish(service, service.submit({'persona_id': pid, 'date': '2026-10-29', 'kind': 'day'}))
    assert result['status'] == 'completed', result
    assert service.state(pid, '2026-10-29')['day']['slots'][0]['title'] == '日期锁定'


def test_history_pruning_keeps_latest_tied_jobs_and_all_active_jobs(service):
    for index in range(110):
        service._save_job({'id': str(index), 'persona_id': 'role', 'created_at': '2026-10-29T00:00:00+00:00',
                           'status': 'running' if index == 0 else 'completed'})
    assert service.job('0')['status'] == 'running'
    assert service.job('109')['status'] == 'completed'
    assert service.jobs()[0]['id'] == '0'
    assert len(service.jobs()) == 20
    with service.store._lock:
        assert service.store._conn.execute('SELECT COUNT(*) FROM life_jobs').fetchone()[0] == 101


@pytest.mark.parametrize('day', ['1969-12-31', '2101-01-01', '2026-02-30', '20261029'])
def test_unsupported_dates_fail_before_jobs_or_paid_generation(service, day):
    pid = service.store.list_personas()[0]['id']
    with pytest.raises(ValueError):
        service.submit({'persona_id': pid, 'date': day})
    assert service.jobs() == [] and service.provider.calls == [] and service.usage()['calls'] == 0


def test_stale_generation_revision_and_missing_diary_day_are_rejected_before_call(service):
    pid = prepare(service)
    before = service.state(pid, '2026-10-29')
    service.save({'persona_id': pid, 'kind': 'profile', 'value': profile()})
    with pytest.raises(ValueError, match='已被修改'):
        service.submit({'persona_id': pid, 'date': '2026-10-29', 'revision': before['revision']})
    with pytest.raises(ValueError, match='没有已保存日程'):
        service.submit({'persona_id': pid, 'kind': 'diary', 'date': '2000-01-01'})
    assert service.jobs() == [] and service.provider.calls == []


@async_test
async def test_scoped_diary_rechecks_material_permission_immediately_before_dispatch(service):
    day = (datetime.now(timezone.utc) - timedelta(days=1)).date().isoformat()
    pid = prepare(service, day)
    scope_id = 'qq:private:material'
    service.store.upsert_scope({'id': scope_id, 'persona_id': pid})
    service.save_settings({'enabled': True, 'diary_chat_enabled': True, 'diary_scopes': [scope_id]})
    source = service.store.add_message(scope_id, 'user', 'user', 'private material')
    assert service.capture_message(scope_id, 'user', 'user', source['content'], source['id'], now=day + 'T12:00:00+08:00')
    waiting, release = asyncio.Event(), asyncio.Event()
    original = service.provider.complete
    async def delayed(*args, **kwargs):
        waiting.set()
        await release.wait()
        return await original(*args, **kwargs)
    service.provider.complete = delayed
    service.provider.responses = [{'title': '一天', 'content': '角色日记'}]
    job = service.submit({'persona_id': pid, 'date': day, 'kind': 'diary', 'scope_id': scope_id})
    await waiting.wait()
    service.save_settings({'enabled': False})
    release.set()
    result = await finish(service, job)
    assert result['status'] == 'failed'
    assert service.provider.calls == [] and service.diary(pid, day, scope_id) is None


def test_diary_scope_cannot_borrow_another_persona_or_create_unknown_scope(service):
    day = '2000-01-01'
    pid = prepare(service, day)
    other = service.store.save_persona({'id': 'other-diary-role', 'name': '其他角色', 'system_prompt': '其他人格'})
    scope_id = 'qq:private:other-role'
    service.store.upsert_scope({'id': scope_id, 'persona_id': other['id']})
    service.save_settings({'enabled': True, 'diary_chat_enabled': True, 'diary_scopes': [scope_id]})
    with pytest.raises(ValueError, match='当前人格'):
        service.submit({'persona_id': pid, 'date': day, 'kind': 'diary', 'scope_id': scope_id})
    assert service.jobs() == [] and service.provider.calls == []


@async_test
async def test_adaptive_eligibility_is_rechecked_before_dispatch_and_response_commit(service):
    today = datetime.now(timezone.utc).astimezone(__import__('zoneinfo').ZoneInfo('Asia/Hong_Kong')).date().isoformat()
    pid = prepare(service, today, status='busy')
    scope_id = 'qq:private:eligible'
    service.store.upsert_scope({'id': scope_id, 'persona_id': pid})
    service.save_settings({'enabled': True, 'reply_control': True})
    allowed = True
    waiting, release = asyncio.Event(), asyncio.Event()
    original = service.provider.complete
    async def delayed(*args, **kwargs):
        waiting.set()
        await release.wait()
        return await original(*args, **kwargs)
    service.provider.complete = delayed
    review = asyncio.create_task(service.review_contact(scope_id, 'hello', eligible=lambda: allowed))
    await waiting.wait()
    allowed = False
    release.set()
    assert (await review)['status'] == 'busy'
    assert service.provider.calls == [] and (pid, scope_id) not in service._adjustments
    allowed = True
    async def revoke_after_response(*args, **kwargs):
        nonlocal allowed
        result = await original(*args, **kwargs)
        allowed = False
        return result
    service.provider.complete = revoke_after_response
    service.provider.responses = [{'action': 'brief_exception', 'activity_kind': 'rest', 'importance': 'urgent',
                                   'delay_minutes': 5, 'reason': '紧急', 'tone': 'plain'}]
    assert (await service.review_contact(scope_id, 'help', eligible=lambda: allowed))['status'] == 'busy'
    assert len(service.provider.calls) == 1 and (pid, scope_id) not in service._adjustments


@async_test
async def test_native_provider_rechecks_adaptive_eligibility_after_model_queue_wait(service, monkeypatch):
    import httpx
    from shiori.provider import Provider as NativeProvider
    today = datetime.now(timezone.utc).astimezone(__import__('zoneinfo').ZoneInfo('Asia/Hong_Kong')).date().isoformat()
    pid = prepare(service, today, status='busy')
    scope_id = 'qq:private:native-eligible'
    service.store.upsert_scope({'id': scope_id, 'persona_id': pid})
    service.save_settings({'enabled': True, 'reply_control': True})
    provider = NativeProvider(service.settings, service.store)
    service.provider = provider
    waiting, release = asyncio.Event(), asyncio.Event()
    allowed = True
    @asynccontextmanager
    async def occupied_model(*args, **kwargs):
        waiting.set()
        await release.wait()
        yield
    provider._provider_slot = occupied_model
    def no_transport(*args, **kwargs):
        pytest.fail('ineligible adaptive request must never create an HTTP client')
    monkeypatch.setattr(httpx, 'AsyncClient', no_transport)
    review = asyncio.create_task(service.review_contact(scope_id, 'hello', eligible=lambda: allowed))
    await waiting.wait()
    allowed = False
    release.set()
    assert (await review)['status'] == 'busy'
    assert (pid, scope_id) not in service._adjustments
    assert provider._reservations == {} and service.store.stats()['input_tokens'] == 0


@async_test
async def test_native_provider_global_budget_blocks_schedule_without_http_or_writes(service, monkeypatch):
    import httpx
    from shiori.provider import Provider as NativeProvider
    pid = service.store.list_personas()[0]['id']
    service.settings.value['budget']['daily_tokens'] = 1
    service.provider = NativeProvider(service.settings, service.store)
    def no_transport(*args, **kwargs):
        pytest.fail('exhausted budget must never create an HTTP client')
    monkeypatch.setattr(httpx, 'AsyncClient', no_transport)
    result = await finish(service, service.submit({'persona_id': pid, 'date': '2026-10-29'}))
    assert result['status'] == 'failed' and '预算' in result['message']
    assert service.state(pid, '2026-10-29')['profile'] is None
    assert service.provider._reservations == {} and service.store.stats()['input_tokens'] == 0
