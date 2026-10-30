"""Persona life plans and fictional diaries, independent of conversational memory.

Rules originate in the workspace's persona_schedule plugin. AstrBot hooks, model
transports and message queues are deliberately replaced by Shiori services.
"""
from __future__ import annotations

import asyncio
from collections import OrderedDict
import copy
from datetime import date, datetime, timedelta, timezone
import json
import re
import time
from uuid import uuid4
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .db import message_fingerprint
from .life_rules.core import (active_events, digest, merge_locked_slots, minute,
                              slot_at, slow_bounds, validate_events,
                              validate_profile, validate_slots, weekly_for_day)
from .life_rules.prompts import (SYSTEM, PROFILE, CALENDAR, DAY, DIARY, ADAPTIVE,
                                parse_object, public_calendar, validate_diary)
from .life_rules.recurrence import apply_recurrences, is_recurring_event, validate_rules
from .life_rules.adaptive import (create_adjustment, effective_activity,
                                  target_activity, validate_decision)
from .settings import resolve_model
from .timezones import config_zone


DEFAULTS = {
    'enabled': False, 'reply_control': False, 'control_scope': 'all', 'scope_ids': [],
    'model_id': '', 'diary_model_id': '', 'timezone': 'Asia/Hong_Kong',
    'auto_schedule_enabled': True, 'auto_schedule_time': '00:00',
    'diary_enabled': True, 'diary_chat_enabled': False, 'diary_scopes': [],
    'daily_calls': 12, 'diary_limit': 180, 'slow_min_seconds': 60,
    'slow_max_seconds': 180, 'queue_limit': 60, 'queue_expire_hours': 72,
    'adaptive_enabled': True, 'transition_notice_enabled': False,
    'transition_notice_minutes': 10, 'adaptive_max_delay_minutes': 30,
    'active_conversation_minutes': 10, 'search_enabled': False,
}


def _stamp():
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


def _at(now=None):
    if now is None:
        return datetime.now(timezone.utc)
    if isinstance(now, (int, float)):
        return datetime.fromtimestamp(now, timezone.utc)
    if isinstance(now, str):
        now = datetime.fromisoformat(now.replace('Z', '+00:00'))
    return now.replace(tzinfo=timezone.utc) if now.tzinfo is None else now


def _json(value):
    return json.dumps(value, ensure_ascii=False, separators=(',', ':'), allow_nan=False)


class CharacterSchedule:
    def __init__(self, store, settings, provider, data_dir):
        self.store, self.settings, self.provider = store, settings, provider
        self._worker = None
        self._tasks, self._locks = {}, {}
        self._closing = False
        self._generation_slot = asyncio.Semaphore(1)
        self._retry_after = {}
        self._adjustments, self._last_contact = {}, {}
        self.search = None
        self.queue_status = None
        self.queue_clear = None
        self._record_cache = OrderedDict()
        self._cache_bytes = 0
        self._cache_version = None
        self.reload()

    def reload(self):
        """Reload after a SQLite backup restore; stale tasks cannot write back."""
        self._closing = False
        self._epoch = getattr(self, '_epoch', 0) + 1
        for task in list(self._tasks.values()):
            task.cancel()
        self._tasks.clear()
        self._adjustments.clear()
        self._last_contact.clear()
        self._retry_after.clear()
        self._record_cache.clear()
        self._cache_bytes = 0
        self._cache_version = None
        store = self.store
        with store._lock, store._conn:
            store._conn.executescript('''
                CREATE TABLE IF NOT EXISTS life_settings (id INTEGER PRIMARY KEY, data TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS life_personas (id TEXT PRIMARY KEY, data TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS life_jobs (id TEXT PRIMARY KEY, data TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS life_calls (id TEXT PRIMARY KEY, day TEXT NOT NULL, persona_id TEXT NOT NULL, purpose TEXT NOT NULL, created_at TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS life_calls_day ON life_calls(day);
                CREATE TABLE IF NOT EXISTS life_diaries (id TEXT PRIMARY KEY, persona_id TEXT NOT NULL, persona_key TEXT NOT NULL, day TEXT NOT NULL, scope_id TEXT NOT NULL, data TEXT NOT NULL,
                    UNIQUE(persona_id,persona_key,day,scope_id));
                CREATE INDEX IF NOT EXISTS life_diaries_persona ON life_diaries(persona_id,day DESC);
                CREATE TABLE IF NOT EXISTS life_materials (id TEXT PRIMARY KEY, persona_id TEXT NOT NULL, persona_key TEXT NOT NULL, scope_id TEXT NOT NULL, day TEXT NOT NULL, at REAL NOT NULL, data TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS life_materials_day ON life_materials(persona_id,persona_key,scope_id,day,at);
            ''')
            for row in store._conn.execute('SELECT id,data FROM life_jobs').fetchall():
                job = json.loads(row[1])
                if job['status'] in ('queued', 'running'):
                    job.update(status='interrupted', message='程序已重启，已保存步骤保留；请重新生成以继续。', updated_at=_stamp())
                    store._conn.execute('UPDATE life_jobs SET data=? WHERE id=?', (_json(job), row[0]))
        self.get_settings()

    def get_settings(self):
        with self.store._lock:
            row = self.store._conn.execute('SELECT data FROM life_settings WHERE id=1').fetchone()
        defaults = dict(DEFAULTS, timezone=config_zone(self.settings).key)
        return dict(defaults, **(json.loads(row[0]) if row else {}))

    def save_settings(self, payload):
        if not isinstance(payload, dict) or set(payload) - set(DEFAULTS):
            raise ValueError('日程设置包含未知项目')
        current = self.get_settings()
        result = dict(current)
        bools = {k for k, v in DEFAULTS.items() if type(v) is bool}
        for key, value in payload.items():
            if key in bools:
                if type(value) is not bool:
                    raise ValueError(f'{key} 必须为开关值')
            elif key in ('scope_ids', 'diary_scopes'):
                if not isinstance(value, list) or len(value) > 1000 or any(not isinstance(x, str) or len(x) > 300 for x in value):
                    raise ValueError('会话列表格式无效')
                known = {x['id'] for x in self.store.list_scopes()}
                if set(value) - known:
                    raise ValueError('请选择已经收到消息的会话')
                value = sorted(set(value))
            elif key in ('model_id', 'diary_model_id'):
                if not isinstance(value, str) or len(value) > 200:
                    raise ValueError('模型 ID 无效')
                if value:
                    cfg = resolve_model(self.settings.get(), 'chat', model_id=value)
                    if not cfg.get('enabled', True) or cfg.get('model_type') == 'embedding' or not cfg.get('model'):
                        raise ValueError('请选择可用的对话模型')
            elif key == 'timezone':
                if not isinstance(value, str) or len(value) > 100:
                    raise ValueError('时区无效')
                try:
                    ZoneInfo(value)
                except ZoneInfoNotFoundError as exc:
                    raise ValueError('请填写有效 IANA 时区，例如 Asia/Hong_Kong') from exc
            elif key == 'control_scope':
                if value not in ('all', 'selected'):
                    raise ValueError('控制范围须为 all 或 selected')
            elif key == 'auto_schedule_time':
                if not isinstance(value, str):
                    raise ValueError('生成时间须为 HH:MM')
                minute(value)
            else:
                bounds = {'daily_calls': (0, 1000), 'diary_limit': (7, 1095),
                          'slow_min_seconds': (1, 3600), 'slow_max_seconds': (1, 7200),
                          'queue_limit': (1, 200), 'queue_expire_hours': (1, 168),
                          'transition_notice_minutes': (1, 60), 'adaptive_max_delay_minutes': (1, 120),
                          'active_conversation_minutes': (1, 60)}
                low, high = bounds[key]
                if type(value) is not int or not low <= value <= high:
                    raise ValueError(f'{key} 须为 {low}—{high} 的整数')
            result[key] = value
        if result['slow_min_seconds'] > result['slow_max_seconds']:
            raise ValueError('最短慢回时间不能超过最长慢回时间')
        with self.store._lock, self.store._conn:
            self.store._conn.execute('INSERT OR REPLACE INTO life_settings VALUES(1,?)', (_json(result),))
            # Opt-out clears pending private material, never expands collection.
            if not result['diary_chat_enabled'] or not result['diary_enabled'] or not result['enabled']:
                self.store._conn.execute('DELETE FROM life_materials')
            else:
                scopes = result['diary_scopes']
                if scopes:
                    self.store._conn.execute('DELETE FROM life_materials WHERE scope_id NOT IN (' + ','.join('?' for _ in scopes) + ')', scopes)
                else:
                    self.store._conn.execute('DELETE FROM life_materials')
        if current != result:
            self._adjustments.clear()
        # The workbench history is bounded; cancellation must cover every live
        # task even when many older jobs share the same creation timestamp.
        for job_id in list(self._tasks):
            job = self.job(job_id)
            if not job.get('automatic') or job['status'] not in ('queued', 'running'):
                continue
            allowed = result['enabled'] and (result['diary_enabled'] if job['kind'] == 'diary' else result['auto_schedule_enabled'])
            if job.get('scope_id'):
                allowed = allowed and result['diary_chat_enabled'] and job['scope_id'] in result['diary_scopes']
            if not allowed:
                self.cancel(job['id'])
        return result

    def _persona(self, persona_id):
        if not isinstance(persona_id, str) or not persona_id or len(persona_id) > 200:
            raise ValueError('请选择一个人格')
        persona = self.store.get_persona(persona_id)
        key = digest({k: persona.get(k) for k in ('id', 'name', 'system_prompt', 'description', 'profile')})
        return persona, key

    def _record(self, persona_id):
        persona, key = self._persona(persona_id)
        with self.store._lock:
            row = self.store._conn.execute('SELECT data FROM life_personas WHERE id=?', (persona_id,)).fetchone()
        record = json.loads(row[0]) if row else {}
        if record.get('key') != key:
            # A stale editor without the new fingerprint field must still fail
            # its numeric precondition after the first identity change.
            revision = record.get('revision', 0) + (1 if row else 0)
            record = {'key': key, 'revision': revision, 'profile': None,
                      'days': {}, 'calendars': {}, 'recurrences': [], 'snapshots': [], 'locked_days': {}}
        return persona, record

    @staticmethod
    def _check_identity(payload, record):
        if 'persona_key' in payload and payload['persona_key'] != record['key']:
            raise ValueError('人格设定已变化，请刷新后重新编辑或生成')

    def _cached_record(self, persona_id, now=None):
        """Small immutable read projection; edit snapshots never enter chat cache."""
        persona, key = self._persona(persona_id)
        with self.store._lock:
            version = self.store._conn.execute('PRAGMA data_version').fetchone()[0]
            if self._cache_version != version:
                self._record_cache.clear()
                self._cache_bytes = 0
                self._cache_version = version
            cached = self._record_cache.get(persona_id)
            if cached and cached['key'] == key:
                day = self._today(cached['record'], now)
                if cached['date'] == day:
                    self._record_cache.move_to_end(persona_id)
                    return persona, cached['record']
        _, record = self._record(persona_id)
        day = self._today(record, now)
        wanted = {(date.fromisoformat(day) + timedelta(days=offset)).isoformat() for offset in (-1, 0, 1, 2)}
        projection = {k: record.get(k) for k in ('key', 'revision', 'profile')}
        projection['days'] = {k: v for k, v in record['days'].items() if k in wanted}
        size = len(_json(projection).encode())
        with self.store._lock:
            previous = self._record_cache.pop(persona_id, None)
            if previous:
                self._cache_bytes -= previous['size']
            if size <= 4 * 1024 * 1024:
                self._record_cache[persona_id] = {'key': key, 'date': day, 'record': projection, 'size': size}
                self._cache_bytes += size
                while len(self._record_cache) > 24 or self._cache_bytes > 8 * 1024 * 1024:
                    _, expired = self._record_cache.popitem(last=False)
                    self._cache_bytes -= expired['size']
        return persona, projection

    def _commit(self, persona_id, record, *, snapshot='', expected=None):
        persona, current = self._record(persona_id)
        if record['key'] != current['key'] or expected is not None and expected != current['revision']:
            raise ValueError('人格或日程已被修改，请刷新页面后重试')
        record = copy.deepcopy(record)
        if snapshot:
            saved = {k: v for k, v in current.items() if k != 'snapshots'}
            snapshots = list(current.get('snapshots', []))
            snapshots.append({'id': uuid4().hex, 'kind': snapshot, 'at': _stamp(), 'record': saved})
            while len(snapshots) > 20 or len(_json(snapshots).encode()) > 8 * 1024 * 1024:
                snapshots.pop(0)
            record['snapshots'] = snapshots
        record['revision'] = current['revision'] + 1
        record['updated_at'] = _stamp()
        if len(_json(record).encode()) > 16 * 1024 * 1024:
            raise ValueError('该人格日程数据已达容量，请先清理旧安排')
        with self.store._lock, self.store._conn:
            self.store._conn.execute('INSERT OR REPLACE INTO life_personas VALUES(?,?)', (persona_id, _json(record)))
            cached = self._record_cache.pop(persona_id, None)
            if cached:
                self._cache_bytes -= cached['size']
        # Editing tomorrow must not renew today's finite contact exception.
        # Preserve its consumed window only while the exact owning slot remains.
        for identity, adjustment in list(self._adjustments.items()):
            if identity[0] != persona_id:
                continue
            source = record['days'].get(adjustment.get('date'), {})
            old_source = current['days'].get(adjustment.get('date'), {})
            if (adjustment.get('persona_key') == record['key']
                    and source.get('timezone') == old_source.get('timezone')
                    and adjustment.get('source_slot') in source.get('slots', [])):
                adjustment.update(_revision=record['revision'], source_revision=record['revision'])
            else:
                self._adjustments.pop(identity, None)
        return record

    @staticmethod
    def _date(value, default):
        if value in (None, ''):
            return default
        if not isinstance(value, str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}', value):
            raise ValueError('日期须为 YYYY-MM-DD')
        day = date.fromisoformat(value)
        if not 1970 <= day.year <= 2100:
            raise ValueError('日期需在 1970—2100 年之间')
        return day.isoformat()

    def _today(self, record=None, now=None):
        zone = ZoneInfo((record or {}).get('profile', {}).get('timezone') if (record or {}).get('profile') else self.get_settings()['timezone'])
        return _at(now).astimezone(zone).date().isoformat()

    def _models(self):
        cfg = self.settings.get()
        items = [resolve_model(cfg, model_id='default')]
        items += [resolve_model(cfg, model_id=p['id']) for p in cfg.get('providers', [])]
        return [{k: p.get(k, '') for k in ('id', 'name', 'model')} for p in items
                if p.get('enabled', True) and p.get('model') and p.get('model_type') != 'embedding']

    def usage(self):
        day = _at().astimezone(ZoneInfo(self.get_settings()['timezone'])).date().isoformat()
        with self.store._lock:
            count = self.store._conn.execute('SELECT COUNT(*) FROM life_calls WHERE day=?', (day,)).fetchone()[0]
        return {'date': day, 'calls': count, 'limit': self.get_settings()['daily_calls']}

    def state(self, persona_id='', day=''):
        personas = self.store.list_personas()
        if persona_id not in {p['id'] for p in personas}:
            persona_id = personas[0]['id'] if personas else ''
        result = {'settings': self.get_settings(), 'personas': [{k: p.get(k, '') for k in ('id', 'name')} for p in personas],
                  'scopes': [{'id': s['id'], 'name': s.get('name') or s['id'], 'persona_id': s.get('persona_id', '')} for s in self.store.list_scopes()],
                  'models': self._models(), 'usage': self.usage(), 'persona_id': persona_id,
                  'profile': None, 'calendar': None, 'day': None, 'recurrences': [], 'diaries': [],
                  'snapshots': [], 'history': [], 'current': None, 'revision': 0,
                  'persona_key': '',
                  'jobs': self.jobs(persona_id), 'pending': self.queue_status() if self.queue_status else []}
        if not persona_id:
            result['today'] = self._today()
            result['date'] = self._date(day, result['today'])
            return result
        _, record = self._record(persona_id)
        result['today'] = self._today(record)
        day = self._date(day, result['today'])
        result.update(date=day, profile=record.get('profile'), calendar=record.get('calendars', {}).get(day[:4]),
                      day=record.get('days', {}).get(day), recurrences=record.get('recurrences', []),
                      revision=record['revision'], persona_key=record['key'])
        snapshots = [{k: s[k] for k in ('id', 'kind', 'at')} for s in reversed(record.get('snapshots', []))]
        result['snapshots'] = result['history'] = snapshots
        with self.store._lock:
            rows = self.store._conn.execute('SELECT data FROM life_diaries WHERE persona_id=? ORDER BY day DESC LIMIT 60', (persona_id,)).fetchall()
        result['diaries'] = [dict(json.loads(r[0]), current_persona=json.loads(r[0]).get('persona_key') == record['key']) for r in rows]
        result['current'] = self._snapshot(persona_id)
        return result

    @staticmethod
    def _events_for_day(record, day):
        calendar = record['calendars'].get(day[:4], {'events': []})
        calendar = apply_recurrences(calendar, record['recurrences'], int(day[:4]))
        return active_events(calendar['events'], day)

    def _apply_profile(self, record, profile):
        record['profile'] = profile
        record['days'] = {k: v for k, v in record['days'].items() if v.get('origin') == 'manual'}
        record['calendars'] = {k: v for k, v in record['calendars'].items() if v.get('origin') == 'manual'}
        for day, schedule in record['days'].items():
            # Durable manual wall-clock arrangements follow the edited role
            # timezone; their old zone must not be used by slot_at afterwards.
            schedule['timezone'] = profile['timezone']
            schedule['events'] = self._events_for_day(record, day)

    def save(self, payload):
        if not isinstance(payload, dict):
            raise ValueError('日程内容须为对象')
        pid = payload.get('persona_id')
        _, record = self._record(pid)
        self._check_identity(payload, record)
        day = self._date(payload.get('date'), self._today(record))
        kind, value = payload.get('kind'), payload.get('value')
        if kind == 'profile':
            self._apply_profile(record, validate_profile(value))
        elif kind == 'day':
            if not record.get('profile'):
                raise ValueError('请先生成或填写角色档案和周表')
            if not isinstance(value, dict):
                raise ValueError('当天日程须为对象')
            record['days'][day] = {'date': day, 'timezone': record['profile']['timezone'],
                'slots': validate_slots(value.get('slots')), 'summary': str(value.get('summary', ''))[:1000],
                'events': self._events_for_day(record, day), 'origin': 'manual'}
            record['locked_days'][day] = [s for s in record['days'][day]['slots'] if s.get('locked')]
            if not record['locked_days'][day]:
                record['locked_days'].pop(day, None)
        elif kind == 'calendar':
            rows = value.get('events') if isinstance(value, dict) else value
            if isinstance(rows, list):
                rows = [row for row in rows if not is_recurring_event(row)]
            events = validate_events(rows, int(day[:4]), manual=True)
            previous = record['calendars'].get(day[:4], {}).get('events', [])
            for event in events:
                old = next((row for row in previous if row.get('id') == event['id']), None)
                if old and all(event.get(key) == old.get(key) for key in event if key != 'origin'):
                    event['origin'] = old['origin']
            calendar = {'year': int(day[:4]), 'events': events, 'notes': ['手动编辑的年度活动'], 'references': [], 'origin': 'manual'}
            record['calendars'][day[:4]] = apply_recurrences(calendar, record['recurrences'], int(day[:4]))
            record['days'] = {k: v for k, v in record['days'].items() if k[:4] != day[:4] or v.get('origin') == 'manual'}
            for saved_day, schedule in record['days'].items():
                if saved_day[:4] == day[:4]:
                    schedule['events'] = self._events_for_day(record, saved_day)
        elif kind == 'recurrences':
            record['recurrences'] = validate_rules(value)
            for year, calendar in record['calendars'].items():
                record['calendars'][year] = apply_recurrences(calendar, record['recurrences'], int(year))
            for saved_day, schedule in list(record['days'].items()):
                events = self._events_for_day(record, saved_day)
                if schedule.get('origin') == 'manual':
                    schedule['events'] = events
                elif schedule.get('events', []) != events:
                    record['days'].pop(saved_day)
        else:
            raise ValueError('未知的日程编辑类型')
        self._prune(record, day)
        self._commit(pid, record, snapshot=kind, expected=payload.get('revision'))
        return self.state(pid, day)

    def restore(self, payload):
        pid = payload.get('persona_id')
        _, record = self._record(pid)
        self._check_identity(payload, record)
        found = next((x for x in record['snapshots'] if x['id'] == payload.get('id')), None)
        if not found:
            raise ValueError('编辑快照不存在或已被清理')
        restored = copy.deepcopy(found['record'])
        if restored['key'] != record['key']:
            raise ValueError('旧人格的快照不能恢复到当前人格')
        restored['snapshots'] = record['snapshots']
        self._commit(pid, restored, snapshot='restore', expected=payload.get('revision'))
        return self.state(pid)

    def _snapshot(self, persona_id, now=None, scope_id=''):
        persona, record = self._cached_record(persona_id, now)
        if not record.get('profile'):
            return None
        profile = record['profile']
        local = _at(now).astimezone(ZoneInfo(profile['timezone']))
        schedule = record['days'].get(local.date().isoformat())
        if not schedule:
            return None
        slot, end = slot_at(schedule, local)
        slot = copy.deepcopy(slot)
        adjustment = self._adjustments.get((persona_id, scope_id))
        if adjustment and adjustment.get('_revision') == record['revision']:
            slot = effective_activity(slot, local, adjustment, umo=scope_id, cid=scope_id, persona_key=record['key'],
                                      schedule_revision=record['revision'], schedule_date=local.date().isoformat(),
                                      source_revision=record['revision'])
        next_contact = None
        if slot['status'] == 'busy':
            for offset in range(3):
                next_day = local.date() + timedelta(days=offset)
                plan = record['days'].get(next_day.isoformat())
                if not plan:
                    break
                for candidate in plan['slots']:
                    start = datetime.combine(next_day, datetime.min.time(), tzinfo=local.tzinfo) + timedelta(minutes=minute(candidate['start']))
                    if start.timestamp() >= local.timestamp() and candidate['status'] != 'busy':
                        next_contact = start.timestamp()
                        break
                if next_contact:
                    break
        cfg = self.get_settings()
        return {'persona_id': persona_id, 'persona_name': persona['name'], 'persona_key': record['key'],
                'revision': record['revision'], 'date': local.date().isoformat(), 'now': local.isoformat(timespec='seconds'),
                'timezone': profile['timezone'], 'title': slot['title'], 'status': slot['status'],
                'slot': slot, 'ends_at': end, 'due_at': next_contact if slot['status'] == 'busy' else local.timestamp(),
                'next_contact_at': next_contact, 'summary': schedule.get('summary', ''),
                'events': [{k: e.get(k) for k in ('title', 'kind', 'description', 'origin')} for e in schedule.get('events', [])],
                'slow_min_seconds': slow_bounds(slot, cfg)[0], 'slow_max_seconds': slow_bounds(slot, cfg)[1]}

    def context(self, scope_id, user_id=None, now=None):
        cfg = self.get_settings()
        if not cfg['enabled']:
            return None
        with self.store._lock:
            if not self.store._conn.execute('SELECT 1 FROM scopes WHERE id=?', (scope_id,)).fetchone():
                return None
        try:
            scope = self.store.get_scope(scope_id)
            value = self._snapshot(scope.get('persona_id', ''), now, scope_id)
        except (KeyError, ValueError):
            return None
        if not value:
            return None
        result = {k: value[k] for k in ('persona_id', 'persona_name', 'date', 'timezone', 'title', 'status', 'slot', 'summary', 'events')}
        result['fictional_schedule'] = True
        result['reply_control'] = self.contact_policy(scope_id, now).get('active', False)
        if cfg['transition_notice_enabled']:
            _, record = self._cached_record(scope['persona_id'], now)
            local = _at(now).astimezone(ZoneInfo(value['timezone']))
            today = record['days'].get(local.date().isoformat())
            target = target_activity(today, local, cfg['transition_notice_minutes'],
                next_schedule=record['days'].get((local.date() + timedelta(days=1)).isoformat()))
            if target and target.get('starts_at', float('inf')) - local.timestamp() <= cfg['transition_notice_minutes'] * 60:
                result['upcoming_activity'] = dict({k: target.get(k) for k in ('starts_at', 'ends_at')},
                                                    title=target['slot']['title'], status=target['slot']['status'])
        return result

    def contact_policy(self, scope_id, now=None):
        cfg = self.get_settings()
        inactive = {'active': False, 'status': 'free', 'due_at': None}
        if not cfg['enabled'] or not cfg['reply_control'] or scope_id.startswith('web:'):
            return inactive
        if cfg['control_scope'] == 'selected' and scope_id not in cfg['scope_ids']:
            return inactive
        with self.store._lock:
            if not self.store._conn.execute('SELECT 1 FROM scopes WHERE id=?', (scope_id,)).fetchone():
                return dict(inactive, reason='会话不存在')
        try:
            scope = self.store.get_scope(scope_id)
            value = self._snapshot(scope.get('persona_id', ''), now, scope_id)
        except (KeyError, ValueError):
            return inactive
        return dict(value, active=True) if value else dict(inactive, reason='缺少已生成日程，正常回复')

    def preview(self, payload):
        pid = payload.get('persona_id')
        _, record = self._record(pid)
        self._check_identity(payload, record)
        day = self._date(payload.get('date'), self._today(record))
        at = payload.get('time', '12:00')
        minute(at)
        zone = ZoneInfo(record['profile']['timezone'] if record.get('profile') else self.get_settings()['timezone'])
        local = datetime.fromisoformat(day + 'T' + at).replace(tzinfo=zone)
        if local.astimezone(timezone.utc).astimezone(zone).replace(tzinfo=None) != local.replace(tzinfo=None):
            raise ValueError('这个本地时间在夏令时切换中不存在，请选择其他钟点')
        scope_id = payload.get('scope_id', '')
        if not isinstance(scope_id, str) or len(scope_id) > 300:
            raise ValueError('预演会话格式无效')
        if scope_id:
            with self.store._lock:
                exists = self.store._conn.execute('SELECT 1 FROM scopes WHERE id=?', (scope_id,)).fetchone()
            if not exists:
                raise ValueError('请选择已有会话进行预演')
            if self.store.get_scope(scope_id).get('persona_id') != pid:
                raise ValueError('所选会话正在使用其他人格，请选择同一人格的会话')
        value = self._snapshot(pid, local, scope_id)
        return {'date': day, 'time': at, 'current': value, 'policy': self.contact_policy(scope_id, local),
                'read_only': True, 'model_called': False}

    def capture_message(self, scope_id, user_id, role, content, source_id='', now=None):
        cfg = self.get_settings()
        if not (cfg['enabled'] and cfg['diary_enabled'] and cfg['diary_chat_enabled'] and scope_id in cfg['diary_scopes']):
            return False
        if role not in ('user', 'assistant') or not isinstance(content, str) or not content.strip() or content.lstrip().startswith('/'):
            return False
        scope = self.store.get_scope(scope_id)
        try:
            pid = scope.get('persona_id', '')
            _, record = self._record(pid)
        except (ValueError, KeyError):
            return False
        current = _at(now)
        day = self._today(record, current)
        source_id = str(source_id)
        try:
            source = self.store.get_message(source_id)
        except KeyError:
            return False
        if (source['scope_id'] != scope_id or source['role'] != role
                or str(source['user_id']) != str(user_id) or source['content'] != content):
            return False
        key = digest([pid, record['key'], scope_id, source_id, role])
        material = {'role': role, 'content': content.strip()[:1000], 'source_id': source_id,
                    'source_fingerprint': message_fingerprint(source)}
        with self.store._lock, self.store._conn:
            self.store._conn.execute('DELETE FROM life_materials WHERE at<?', (current.timestamp() - 8 * 86400,))
            self.store._conn.execute('INSERT OR IGNORE INTO life_materials VALUES(?,?,?,?,?,?,?)',
                (key, pid, record['key'], scope_id, day, current.timestamp(), _json(material)))
            self.store._conn.execute('DELETE FROM life_materials WHERE persona_id=? AND persona_key=? AND scope_id=? AND day=? AND id NOT IN (SELECT id FROM life_materials WHERE persona_id=? AND persona_key=? AND scope_id=? AND day=? ORDER BY at DESC LIMIT 120)',
                (pid, record['key'], scope_id, day, pid, record['key'], scope_id, day))
            self.store._conn.execute('DELETE FROM life_materials WHERE id NOT IN (SELECT id FROM life_materials ORDER BY at DESC LIMIT 4000)')
        return True

    def diary(self, persona_id, day, scope_id=''):
        _, record = self._record(persona_id)
        day = self._date(day, self._today(record))
        with self.store._lock:
            row = self.store._conn.execute('SELECT data FROM life_diaries WHERE persona_id=? AND persona_key=? AND day=? AND scope_id=?',
                (persona_id, record['key'], day, scope_id)).fetchone()
        return json.loads(row[0]) if row else None

    def _reserve_call(self, persona_id, purpose):
        day = _at().astimezone(ZoneInfo(self.get_settings()['timezone'])).date().isoformat()
        with self.store._lock, self.store._conn:
            count = self.store._conn.execute('SELECT COUNT(*) FROM life_calls WHERE day=?', (day,)).fetchone()[0]
            if count >= self.get_settings()['daily_calls']:
                raise ValueError('今日日程模型调用次数已达上限；已完成步骤保留，明天或调整配额后可继续')
            self.store._conn.execute('INSERT INTO life_calls VALUES(?,?,?,?,?)', (uuid4().hex, day, persona_id, purpose, _stamp()))
            self.store._conn.execute('DELETE FROM life_calls WHERE day<?', ((_at().date() - timedelta(days=90)).isoformat(),))

    def _model(self, purpose):
        cfg = self.get_settings()
        model = cfg['diary_model_id'] or cfg['model_id'] if purpose == 'schedule_diary' else cfg['model_id']
        return resolve_model(self.settings.get(), 'chat', model_id=model or None)

    async def _structured(self, persona_id, instruction, payload, validator, *, purpose='schedule', check=None, max_tokens=3000):
        selected = self._model(purpose)
        if not selected.get('base_url') or not selected.get('model') or not selected.get('enabled', True):
            raise ValueError('请先在模型配置中设置可用的对话模型，或在日程设置选择专用模型')
        request = dict(payload)
        for attempt in range(2):
            if check:
                check()
            self._reserve_call(persona_id, purpose)
            def guard():
                if self._closing or self.settings.get().get('paused'):
                    raise ValueError('软件已暂停或正在关闭')
                if check:
                    check()
                if self._model(purpose) != selected:
                    raise ValueError('模型配置已变化，停止使用旧请求')
                return True
            response = await self.provider.complete([
                {'role': 'system', 'content': SYSTEM + instruction},
                {'role': 'user', 'content': _json(request)}], config=selected, purpose=purpose,
                scope_id='persona:' + persona_id, max_tokens=max_tokens, before_send=guard)
            guard()
            try:
                return validator(parse_object(response.get('content') or ''))
            except (ValueError, TypeError, KeyError) as exc:
                if attempt:
                    raise ValueError('模型两次未生成有效日程数据，请重试或手动编辑') from exc
                request['format_correction'] = str(exc)[:300]

    def _prune(self, record, protected=''):
        today = self._today(record)
        days = record['days']
        automatic = sorted((k for k, v in days.items() if v.get('origin') != 'manual' and k not in (today, protected)), reverse=True)
        for key in automatic[43:]:
            days.pop(key, None)
        locked = record.get('locked_days', {})
        for key in sorted(locked, reverse=True)[90:]:
            locked.pop(key, None)

    def _save_job(self, job):
        job['updated_at'] = _stamp()
        with self.store._lock, self.store._conn:
            self.store._conn.execute('INSERT OR REPLACE INTO life_jobs VALUES(?,?)', (job['id'], _json(job)))
            self.store._conn.execute('''DELETE FROM life_jobs
                WHERE json_extract(data,'$.status') NOT IN ('queued','running')
                AND id NOT IN (SELECT id FROM life_jobs
                    ORDER BY json_extract(data,'$.created_at') DESC, rowid DESC LIMIT 100)''')

    def jobs(self, persona_id=''):
        with self.store._lock:
            rows = self.store._conn.execute('''SELECT data FROM life_jobs
                ORDER BY json_extract(data,'$.status') IN ('queued','running') DESC,
                    json_extract(data,'$.created_at') DESC, rowid DESC LIMIT 104''').fetchall()
        return [json.loads(x[0]) for x in rows if not persona_id or json.loads(x[0])['persona_id'] == persona_id][:20]

    def job(self, job_id):
        with self.store._lock:
            row = self.store._conn.execute('SELECT data FROM life_jobs WHERE id=?', (str(job_id),)).fetchone()
        if not row:
            raise ValueError('日程任务不存在')
        return json.loads(row[0])

    def submit(self, payload, *, automatic=False):
        if self._closing:
            raise ValueError('日程服务正在关闭')
        if not isinstance(payload, dict):
            raise ValueError('生成请求须为对象')
        pid = payload.get('persona_id')
        _, record = self._record(pid)
        self._check_identity(payload, record)
        if payload.get('revision') is not None and payload['revision'] != record['revision']:
            raise ValueError('人格或日程已被修改，请刷新页面后重试')
        day = self._date(payload.get('date'), self._today(record))
        kind = payload.get('kind', 'all')
        if kind not in ('all', 'profile', 'calendar', 'day', 'diary'):
            raise ValueError('未知的生成类型')
        scope_id = payload.get('scope_id', '')
        if not isinstance(scope_id, str) or len(scope_id) > 300:
            raise ValueError('日记会话无效')
        if scope_id and (kind != 'diary' or scope_id not in self.get_settings()['diary_scopes']):
            raise ValueError('日记仅能使用明确允许的会话素材')
        if kind == 'diary' and day >= self._today(record):
            raise ValueError('只能为已经结束的日期写日记')
        if kind == 'diary':
            if day not in record['days']:
                raise ValueError('这一天没有已保存日程，不能凭空补写回忆')
            self._check_diary_scope(pid, scope_id)
        if len(self._tasks) >= 4:
            raise ValueError('已有多个日程任务等待处理，请稍后重试')
        if any(j['persona_id'] == pid and j['status'] in ('queued', 'running') for j in self.jobs()):
            raise ValueError('这个人格已有生成任务，请等待完成或先取消')
        job = {'id': uuid4().hex, 'persona_id': pid, 'persona_key': record['key'], 'kind': kind, 'date': day,
               'scope_id': scope_id, 'status': 'queued', 'stage': 'queued', 'message': '等待生成', 'progress': 0,
               'created_at': _stamp(), 'events': [], 'automatic': automatic, 'revision': record['revision']}
        self._save_job(job)
        self._tasks[job['id']] = asyncio.create_task(self._run_job(job))
        self._tasks[job['id']].add_done_callback(lambda done, job_id=job['id']: self._tasks.pop(job_id, None))
        return job

    def cancel(self, job_id):
        job = self.job(job_id)
        if job['status'] in ('queued', 'running'):
            job.update(status='cancelled', message='任务已取消，已保存步骤保留')
            self._save_job(job)
            task = self._tasks.get(job_id)
            if task:
                task.cancel()
        return job

    async def _run_job(self, job):
        pid, day = job['persona_id'], job['date']
        epoch = self._epoch
        lock = self._locks.setdefault(pid, asyncio.Lock())
        def check():
            if epoch != self._epoch or self._closing or self.job(job['id'])['status'] == 'cancelled':
                raise asyncio.CancelledError()
            _, current = self._record(pid)
            if current['key'] != job['persona_key'] or current['revision'] != job['revision']:
                raise ValueError('人格或日程已被修改，旧任务已停止；已完成步骤保留')
            if job['automatic']:
                cfg = self.get_settings()
                if not cfg['enabled'] or not (cfg['diary_enabled'] if job['kind'] == 'diary' else cfg['auto_schedule_enabled']):
                    raise ValueError('自动日程或日记已关闭，自动任务停止')
        def progress(stage, message, amount):
            check()
            job.update(stage=stage, message=message, progress=amount)
            job['events'].append({'at': _stamp(), 'stage': stage, 'message': message})
            job['events'] = job['events'][-20:]
            self._save_job(job)
        def commit(record):
            check()
            record = self._commit(pid, record, expected=job['revision'])
            job['revision'] = record['revision']
            self._save_job(job)
            return record
        try:
            async with lock, self._generation_slot:
                job['status'] = 'running'
                progress('prepare', '正在准备角色生活资料', 5)
                persona, record = self._record(pid)
                kind = job['kind']
                prompt = {'name': persona['name'], 'persona': (persona.get('system_prompt') or persona.get('description') or '')[:24000],
                          'profile_fields': persona.get('profile') or {}}
                if kind == 'profile' or not record.get('profile'):
                    progress('profile', '正在分析角色档案和每周作息', 15)
                    profile = await self._structured(pid, PROFILE, dict(prompt, default_timezone=self.get_settings()['timezone']), validate_profile, check=check, max_tokens=4000)
                    old = record.get('profile')
                    if old:
                        profile['weekly'] = merge_locked_slots(profile['weekly'], old['weekly'], weekly=True)
                    self._apply_profile(record, profile)
                    record = commit(record)
                profile = record['profile']
                if kind == 'profile':
                    pass
                elif kind == 'diary':
                    progress('diary', '正在整理已经结束的这一天', 50)
                    await self._generate_diary(pid, record, day, job['scope_id'], prompt, check)
                else:
                    year = day[:4]
                    if kind == 'calendar' or year not in record['calendars']:
                        progress('calendar', '正在整理节庆和年度活动', 45)
                        candidates, warnings = public_calendar(profile, int(year))
                        references = []
                        if self.get_settings()['search_enabled'] and self.search:
                            try:
                                query = f"{profile['world_name']} {profile['identity'][:100]} 节庆 日期 {year}"
                                found = await asyncio.wait_for(self.search(query), 20)
                                references = found.get('results', [])[:6] if isinstance(found, dict) else found[:6]
                            except Exception:
                                warnings.append('本次公开检索未完成，模型创作将单独标明')
                        urls = {x.get('url') for x in references if isinstance(x, dict)}
                        events = await self._structured(pid, CALENDAR, dict(prompt, year=int(year), profile=profile,
                            public_calendar_candidates=candidates, search_references=references),
                            lambda obj: validate_events(obj.get('events'), int(year), source_urls=urls), check=check, max_tokens=3500)
                        matched = {(e['start'], e['title']) for e in candidates}
                        for event in events:
                            if (event['start'], event['title']) in matched and event['start'] == event['end']:
                                event['origin'] = 'public_calendar'
                        calendar = {'year': int(year), 'events': events, 'notes': warnings, 'references': references, 'origin': 'generated'}
                        record['calendars'][year] = apply_recurrences(calendar, record['recurrences'], int(year))
                        record['days'] = {k: v for k, v in record['days'].items() if k[:4] != year or v.get('origin') == 'manual'}
                        for saved_day, schedule in record['days'].items():
                            if saved_day[:4] == year:
                                schedule['events'] = self._events_for_day(record, saved_day)
                        record = commit(record)
                    if kind in ('all', 'day'):
                        progress('day', '正在编排并校验当天日程', 75)
                        events = active_events(record['calendars'][year]['events'], day)
                        recent = [v for k, v in sorted(record['days'].items()) if k < day][-3:]
                        value = await self._structured(pid, DAY, dict(prompt, date=day, profile=profile,
                            base_routine=weekly_for_day(profile, day), today_events=events, recent_days=recent),
                            lambda obj: {'summary': str(obj.get('summary', ''))[:1000], 'slots': validate_slots(obj.get('slots'))}, check=check, max_tokens=3000)
                        # Date overrides replace the overlapping portion of a
                        # weekly lock. Never promote a generated day's inherited
                        # weekly rows into independent locks on the next rebuild.
                        old = record['days'].get(day)
                        date_locks = (old['slots'] if old and old.get('origin') == 'manual'
                                      else record.get('locked_days', {}).get(day, []))
                        base = merge_locked_slots(weekly_for_day(profile, day), date_locks)
                        slots = merge_locked_slots(value['slots'], base)
                        record['days'][day] = dict(value, slots=slots, date=day, timezone=profile['timezone'], events=events, origin='generated')
                        self._prune(record, day)
                        record = commit(record)
                progress('completed', '已完成，日程和角色日记保存在独立资料中', 100)
                job['status'] = 'completed'
                self._save_job(job)
        except asyncio.CancelledError:
            if epoch != self._epoch:
                return
            if self.job(job['id'])['status'] == 'cancelled':
                job.update(status='cancelled', message='任务已取消，已保存步骤保留')
            else:
                job.update(status='interrupted', message='任务已中断，已保存步骤保留')
            self._save_job(job)
        except Exception as exc:
            if epoch != self._epoch:
                return
            message = str(exc)[:400] if isinstance(exc, (ValueError, KeyError)) else '模型或网络未能完成本次生成，请检查运行日志后重试'
            job.update(status='failed', message=message, error=type(exc).__name__)
            self._save_job(job)
            if hasattr(self.store, 'add_exception'):
                self.store.add_exception('schedule_error', '角色日程生成未完成', exc, persona_id=pid, job_id=job['id'])
        finally:
            self._tasks.pop(job['id'], None)

    def _check_diary_scope(self, pid, scope_id):
        if not scope_id:
            return
        cfg = self.get_settings()
        if not (cfg['enabled'] and cfg['diary_enabled'] and cfg['diary_chat_enabled'] and scope_id in cfg['diary_scopes']):
            raise ValueError('素材授权已变化，此会话未允许用于日记素材')
        with self.store._lock:
            row = self.store._conn.execute('SELECT 1 FROM scopes WHERE id=?', (scope_id,)).fetchone()
        if not row or self.store.get_scope(scope_id).get('persona_id') != pid:
            raise ValueError('日记来源会话已变化，请选择正在使用当前人格的会话')

    async def _generate_diary(self, pid, record, day, scope_id, prompt, check):
        if day >= self._today(record):
            raise ValueError('只能为已经结束的日期写日记')
        schedule = record['days'].get(day)
        if not schedule:
            raise ValueError('这一天没有已保存日程，不能凭空补写回忆')
        source_snapshot, material_ids = {}, []
        def diary_check():
            check()
            self._check_diary_scope(pid, scope_id)
            if not self.store.message_source_snapshot_current(scope_id, source_snapshot):
                raise ValueError('日记来源消息已删除或修改，旧素材不再使用')
            if material_ids:
                with self.store._lock:
                    count = self.store._conn.execute('SELECT COUNT(*) FROM life_materials WHERE id IN '
                        '(SELECT value FROM json_each(?))', (_json(material_ids),)).fetchone()[0]
                if count != len(material_ids):
                    raise ValueError('日记素材已清除，旧素材不再使用')
        diary_check()
        materials, fingerprints = [], {}
        if scope_id:
            with self.store._lock:
                rows = self.store._conn.execute('SELECT id,data FROM life_materials WHERE persona_id=? AND persona_key=? AND scope_id=? AND day=? ORDER BY at DESC LIMIT 120',
                    (pid, record['key'], scope_id, day)).fetchall()
            size = 0
            for row in reversed(rows):
                entry = json.loads(row[1])
                try:
                    source = self.store.get_message(entry.get('source_id', ''))
                except KeyError:
                    continue
                fingerprint = message_fingerprint(source)
                if (source['scope_id'] != scope_id or source['role'] != entry['role']
                        or source['content'].strip()[:1000] != entry['content']
                        or entry.get('source_fingerprint', fingerprint) != fingerprint):
                    continue
                if size + len(entry['content']) > 24000:
                    break
                size += len(entry['content'])
                materials.append({'role': entry['role'], 'content': entry['content']})
                material_ids.append(row[0])
                fingerprints[source['id']] = fingerprint
        with self.store._lock:
            rows = self.store._conn.execute('SELECT data FROM life_diaries WHERE persona_id=? AND persona_key=? AND scope_id=? AND day<? ORDER BY day DESC LIMIT 3',
                (pid, record['key'], scope_id, day)).fetchall()
            legacy_derived = self.store._chat_derived_life_diaries(self.store._conn, scope_id)
        recent = []
        for row in rows:
            previous = json.loads(row[0])
            sources = previous.get('source_fingerprints', {})
            # Do not let a continuing diary accumulate an unbounded ancestry.
            # Omit an older excerpt rather than dropping any of its dependencies.
            if (not sources and previous.get('id') in legacy_derived
                    or len(fingerprints.keys() | sources.keys()) > 480
                    or self.store.message_source_snapshot(scope_id, sources) is None):
                continue
            recent.append({k: previous.get(k) for k in ('date', 'title', 'mood', 'content')})
            fingerprints.update(sources)
        source_snapshot = self.store.message_source_snapshot(scope_id, fingerprints)
        if source_snapshot is None:
            raise ValueError('日记来源消息已删除或修改，旧素材不再使用')
        value = await self._structured(pid, DIARY, dict(prompt, date=day,
            profile={k: v for k, v in record['profile'].items() if k != 'weekly'}, saved_day=schedule,
            recent_diaries=recent, conversation_material=materials), validate_diary,
            purpose='schedule_diary', check=diary_check, max_tokens=1600)
        diary_check()
        cfg_now = self.get_settings()
        value.update(id=uuid4().hex, persona_id=pid, persona_key=record['key'], date=day, scope_id=scope_id,
                     fictional=True, created_at=_stamp(), schedule=copy.deepcopy(schedule), material_count=len(materials),
                     source_fingerprints=fingerprints)
        with self.store._lock, self.store._conn:
            self.store._conn.execute('INSERT OR REPLACE INTO life_diaries VALUES(?,?,?,?,?,?)',
                (value['id'], pid, record['key'], day, scope_id, _json(value)))
            self.store._conn.execute('DELETE FROM life_diaries WHERE persona_id=? AND scope_id=? AND id NOT IN (SELECT id FROM life_diaries WHERE persona_id=? AND scope_id=? ORDER BY day DESC LIMIT ?)',
                (pid, scope_id, pid, scope_id, cfg_now['diary_limit']))
        return value

    async def review_contact(self, scope_id, message, contexts=(), *, eligible=None):
        """Optional, rate-limited decision; never called by context or status reads."""
        cfg = self.get_settings()
        policy = self.contact_policy(scope_id)
        if not cfg['adaptive_enabled'] or not policy.get('active') or policy.get('status') != 'busy' or eligible is not None and not eligible():
            return policy
        pid = policy['persona_id']
        key = (pid, scope_id)
        cached = self._adjustments.get(key)
        now = _at()
        if cached and now.timestamp() - cached.get('_reviewed_at', 0) < 180:
            return policy
        persona, record = self._record(pid)
        zone = ZoneInfo(policy['timezone'])
        local = now.astimezone(zone)
        schedule = record['days'][local.date().isoformat()]
        target = target_activity(schedule, local,
            previous_schedule=record['days'].get((local.date() - timedelta(days=1)).isoformat()),
            next_schedule=record['days'].get((local.date() + timedelta(days=1)).isoformat()))
        if not target:
            return policy
        active = now.timestamp() - self._last_contact.get(key, 0) < cfg['active_conversation_minutes'] * 60
        try:
            revision, persona_key = record['revision'], record['key']
            def check():
                _, current = self._record(pid)
                live = self.contact_policy(scope_id)
                if (eligible is not None and not eligible() or not self.get_settings()['adaptive_enabled']
                        or not live.get('active') or live.get('persona_id') != pid
                        or current['revision'] != revision or current['key'] != persona_key):
                    raise ValueError('联系范围或日程已变化')
            decision = await self._structured(pid, ADAPTIVE, {
                'persona': (persona.get('system_prompt') or persona.get('description') or '')[:12000],
                'target': target, 'message': str(message)[:4000], 'contexts': list(contexts)[-6:],
                'active_conversation': active, 'max_delay_minutes': cfg['adaptive_max_delay_minutes']},
                lambda obj: validate_decision(obj, target, cfg['adaptive_max_delay_minutes'], active_conversation=active),
                purpose='schedule_adaptive', check=check, max_tokens=500)
            check()
            adjustment = create_adjustment(decision, target, local, umo=scope_id, cid=scope_id, persona_key=persona_key,
                schedule_revision=revision, max_delay_minutes=cfg['adaptive_max_delay_minutes'], previous=cached)
            if adjustment:
                adjustment.update(_revision=revision, _reviewed_at=now.timestamp())
                self._adjustments[key] = adjustment
            else:
                self._adjustments[key] = {'_revision': revision, '_reviewed_at': now.timestamp()}
        except Exception:
            # A failed optional judgement must not repeatedly call on every ping.
            if eligible is None or eligible():
                self._adjustments[key] = {'_revision': record['revision'], '_reviewed_at': now.timestamp()}
        return self.contact_policy(scope_id)

    def note_contact(self, scope_id):
        try:
            pid = self.store.get_scope(scope_id).get('persona_id', '')
            self._last_contact[(pid, scope_id)] = time.time()
        except KeyError:
            pass

    async def start(self):
        self._closing = False
        if not self._worker or self._worker.done():
            self._worker = asyncio.create_task(self._loop())

    async def close(self):
        self._closing = True
        tasks = [task for task in [self._worker, *self._tasks.values()] if task]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._worker = None

    async def _loop(self):
        while not self._closing:
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if hasattr(self.store, 'add_exception'):
                    self.store.add_exception('schedule_tick_error', '角色日程后台检查未完成', exc)
            await asyncio.sleep(30)

    async def tick(self, now=None):
        cfg = self.get_settings()
        if not cfg['enabled'] or self.settings.get().get('paused') or self._tasks or self.usage()['calls'] >= cfg['daily_calls']:
            return
        for persona in self.store.list_personas():
            pid = persona['id']
            _, record = self._record(pid)
            if not record.get('profile'):
                continue  # Automatic generation only follows an explicitly prepared persona.
            local = _at(now).astimezone(ZoneInfo(record['profile']['timezone']))
            today = local.date().isoformat()
            retry_key = (pid, today)
            if self._retry_after.get(retry_key, 0) > time.time():
                continue
            if cfg['auto_schedule_enabled'] and local.hour * 60 + local.minute >= minute(cfg['auto_schedule_time']) and today not in record['days']:
                self._retry_after[retry_key] = time.time() + 3600
                self.submit({'persona_id': pid, 'date': today, 'kind': 'day'}, automatic=True)
                return
            if cfg['diary_enabled']:
                for day in sorted(record['days'], reverse=True):
                    if not (today > day >= (local.date() - timedelta(days=7)).isoformat()):
                        continue
                    scopes = ['']
                    if cfg['diary_chat_enabled']:
                        scopes.extend(s['id'] for s in self.store.list_scopes()
                                      if s['id'] in cfg['diary_scopes'] and s.get('persona_id') == pid)
                    for scope_id in scopes:
                        if not self.diary(pid, day, scope_id):
                            self._retry_after[retry_key] = time.time() + 3600
                            self.submit({'persona_id': pid, 'date': day, 'kind': 'diary', 'scope_id': scope_id}, automatic=True)
                            return
