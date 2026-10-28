from __future__ import annotations
import re
from datetime import date, datetime, time, timedelta, timezone

from .timezones import config_zone

HK = timezone(timedelta(hours=8))


def local_wall(value, zone):
    """Resolve a wall clock explicitly: gaps fail, overlaps use the first instant."""
    naive = value.replace(tzinfo=None)
    choices = []
    for fold in (0, 1):
        candidate = naive.replace(tzinfo=zone, fold=fold)
        if candidate.astimezone(timezone.utc).astimezone(zone).replace(tzinfo=None) == naive:
            choices.append(candidate)
    if not choices:
        raise ValueError(f'所选本地时间在 {zone} 的夏令时或时区切换中不存在，请选择其他时间')
    return min(choices, key=lambda item: item.astimezone(timezone.utc))


def calendar_fields(due_at, settings=None, *, timezone_name=None):
    """Snapshot the calendar so a later global timezone change cannot move a task."""
    zone = config_zone({'time': {'timezone': timezone_name}}) if timezone_name is not None else config_zone(settings)
    due = datetime.fromisoformat(str(due_at).replace('Z', '+00:00'))
    if due.tzinfo is None:
        raise ValueError('提醒时间必须包含时区')
    local = due.astimezone(zone)
    return {'timezone': str(zone), 'wall_time': local.time().isoformat(), 'calendar_anchor': local.date().isoformat()}


def validate_calendar(record, settings=None):
    fields = calendar_fields(record['due_at'], settings, timezone_name=record.get('timezone'))
    for key, value in fields.items():
        record.setdefault(key, value)
    wall = record['wall_time']
    if not isinstance(wall, str) or not re.fullmatch(r'\d{2}:\d{2}(?::\d{2}(?:\.\d{1,6})?)?', wall):
        raise ValueError('提醒本地钟点格式无效')
    try:
        time.fromisoformat(wall)
        date.fromisoformat(record['calendar_anchor'])
    except (TypeError, ValueError) as exc:
        raise ValueError('提醒本地钟点或日历起点无效') from exc
    return record


def next_occurrence(record, now, settings=None):
    """Advance a local daily/weekly calendar, skipping missed or nonexistent slots."""
    task = validate_calendar(dict(record), settings)
    zone = config_zone({'time': {'timezone': task['timezone']}})
    anchor, clock = date.fromisoformat(task['calendar_anchor']), time.fromisoformat(task['wall_time'])
    days = 1 if task['repeat'] == 'daily' else 7
    current = now.astimezone(timezone.utc)
    steps = max(0, (current.astimezone(zone).date() - anchor).days // days)
    skipped = 0
    for _ in range(32):
        day = anchor + timedelta(days=steps * days)
        try:
            target = local_wall(datetime.combine(day, clock), zone).astimezone(timezone.utc)
        except ValueError:
            skipped += 1
        else:
            if target > current:
                return dict(task, due_at=target.isoformat()), skipped
        steps += 1
    raise ValueError('未能找到下一个有效的本地提醒时间，请检查周期设置')


def format_due(due_at, settings=None, *, timezone_name=None):
    zone = config_zone({'time': {'timezone': timezone_name}}) if timezone_name is not None else config_zone(settings)
    value = datetime.fromisoformat(str(due_at).replace('Z', '+00:00')).astimezone(zone)
    return value.strftime('%Y-%m-%d %H:%M') + f'（{zone}，UTC' + value.strftime('%z')[:3] + ':' + value.strftime('%z')[3:] + '）'


def parse_due(text: str, now=None, *, settings=None, timezone_name=None):
    """Deliberately bounded Chinese time parser; ambiguous times are not guessed."""
    zone = (config_zone({'time': {'timezone': timezone_name}}) if timezone_name is not None else
            config_zone(settings) if settings is not None or now is None else now.tzinfo or HK)
    now = now or datetime.now(zone)
    if now.tzinfo is None:
        now = local_wall(now, zone)
    else:
        now = now.astimezone(zone)
    text = text.strip()
    relative = re.search(r'(\d{1,4})\s*(分钟|小时|天)后', text)
    if relative:
        value = int(relative[1])
        if not 0 < value <= 366:
            raise ValueError('相对提醒时间超出支持范围')
        delta = {'分钟': timedelta(minutes=value), '小时': timedelta(hours=value), '天': timedelta(days=value)}[relative[2]]
        target = (local_wall(now.replace(tzinfo=None) + delta, zone).astimezone(timezone.utc)
                  if relative[2] == '天' else now.astimezone(timezone.utc) + delta)
        return target.isoformat(), relative.span(), 'none'
    pattern = re.compile(r'(?:(每天|每周[一二三四五六日天])|(今天|明天|后天|明晚|今晚)|(?:(\d{4})[-/](\d{1,2})[-/](\d{1,2})))\s*(上午|下午|晚上|早上|中午)?\s*(\d{1,2}|[一二三四五六七八九十]{1,3})[点:：](?:(\d{1,2})分?|半)?')
    match = pattern.search(text)
    if not match:
        return None
    repeat, day, year, month, date, period, hour_raw, minute_raw = match.groups()
    numbers = {'一': 1, '二': 2, '三': 3, '四': 4, '五': 5, '六': 6, '七': 7, '八': 8, '九': 9, '十': 10, '十一': 11, '十二': 12}
    hour = int(hour_raw) if hour_raw.isdigit() else numbers.get(hour_raw)
    if hour is None:
        return None
    minute = 30 if match[0].endswith('半') else int(minute_raw or 0)
    if period in ('下午', '晚上', '中午') or day in ('明晚', '今晚'):
        if hour < 12:
            hour += 12
    if not 0 <= hour <= 23 or not 0 <= minute <= 59:
        raise ValueError('提醒时间格式不正确')
    if year:
        try:
            target = datetime(int(year), int(month), int(date), hour, minute)
        except ValueError as exc:
            raise ValueError('提醒日期不存在，请检查年月日') from exc
    else:
        target = now.replace(tzinfo=None, hour=hour, minute=minute, second=0, microsecond=0)
        if day in ('明天', '明晚'):
            target += timedelta(days=1)
        elif day == '后天':
            target += timedelta(days=2)
        elif repeat and repeat.startswith('每周'):
            weekday = '一二三四五六日'.index(repeat[-1].replace('天', '日'))
            target += timedelta(days=(weekday - target.weekday()) % 7)
    recurrence = 'daily' if repeat == '每天' else 'weekly' if repeat else 'none'
    target = local_wall(target, zone)
    if target.astimezone(timezone.utc) <= now.astimezone(timezone.utc):
        if recurrence == 'daily':
            target = local_wall(target.replace(tzinfo=None) + timedelta(days=1), zone)
        elif recurrence == 'weekly':
            target = local_wall(target.replace(tzinfo=None) + timedelta(days=7), zone)
        else:
            raise ValueError('这个时间已经过去，请提供未来时间')
    return target.astimezone(timezone.utc).isoformat(), match.span(), recurrence
