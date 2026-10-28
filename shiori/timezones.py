"""One configurable calendar for budgets, activity windows and statistics."""
from datetime import datetime, timedelta, timezone
import json
import math
import re
from contextlib import contextmanager
from contextvars import ContextVar
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

DEFAULT_TIMEZONE = 'Asia/Shanghai'
_DATETIME_TYPE = datetime
_message_reference = ContextVar('message_reference_time', default=None)


@contextmanager
def message_time_reference(anchor):
    """Per-request calendar, also propagated into asyncio.to_thread workers."""
    token = _message_reference.set(anchor)
    try:
        yield
    finally:
        _message_reference.reset(token)


def reference_time(default=None):
    return _message_reference.get() or default or datetime.now(timezone.utc)


def utc_timestamp(value):
    """Normalize an actual instant; a timezone-less date is deliberately unknown."""
    try:
        if isinstance(value, bool) or value is None:
            return None
        if isinstance(value, (int, float)):
            if not math.isfinite(value):
                return None
            parsed = datetime.fromtimestamp(value, timezone.utc)
        elif isinstance(value, _DATETIME_TYPE):
            parsed = value
        elif isinstance(value, str) and len(value) <= 100:
            parsed = datetime.fromisoformat(value.strip().replace('Z', '+00:00'))
        else:
            return None
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            return None
        return parsed.astimezone(timezone.utc).isoformat(timespec='microseconds').replace('+00:00', 'Z')
    except (ValueError, TypeError, OverflowError, OSError):
        return None


def ingress_times(sent_at=None, *, received_at=None):
    """The connector supplies send time; local ingress owns receipt time."""
    received = utc_timestamp(received_at) or utc_timestamp(datetime.now(timezone.utc))
    sent = utc_timestamp(sent_at)
    if sent and datetime.fromisoformat(sent.replace('Z', '+00:00')) > datetime.fromisoformat(received.replace('Z', '+00:00')) + timedelta(minutes=5):
        sent = None
    return {'sent_at': sent, 'received_at': received}


def message_anchor(message, *, fallback=None):
    """Resolve relative wording against its original message, never a later retry."""
    metadata = message.get('metadata') or {}
    for field in ('sent_at', 'received_at', 'created_at'):
        stamp = utc_timestamp(message.get(field) or metadata.get(field))
        if stamp:
            return datetime.fromisoformat(stamp.replace('Z', '+00:00'))
    stamp = utc_timestamp(fallback)
    return datetime.fromisoformat(stamp.replace('Z', '+00:00')) if stamp else None


def message_time_context(message, settings=None):
    config = settings if isinstance(settings, dict) else settings.get() if settings is not None else {}
    if not config.get('time', {}).get('awareness_enabled', True):
        return None
    zone = config_zone(config)
    metadata = message.get('metadata') or {}
    result = {'timezone': zone.key, 'anchor': None, 'anchor_source': 'unknown'}
    for field, source in (('sent_at', 'platform'), ('received_at', 'received'), ('created_at', 'recorded')):
        stamp = utc_timestamp(message.get(field) or metadata.get(field))
        local = datetime.fromisoformat(stamp.replace('Z', '+00:00')).astimezone(zone).isoformat(timespec='seconds') if stamp else None
        result['recorded_at' if field == 'created_at' else field] = local
        if result['anchor'] is None and local:
            result.update(anchor=local, anchor_source=source)
    return result


def message_time_label(message, settings=None):
    """A short history label, without repeating every provenance timestamp."""
    context = message_time_context(message, settings)
    if not context:
        return ''
    source = context['anchor_source']
    if source == 'unknown':
        return '【历史消息 · 时间未知】'
    name = {'platform': '发送', 'received': '接收，发送时间未知', 'recorded': '录入，发送时间未知'}[source]
    return f"【历史消息 · {name}：{context['anchor']}】"


def clock_instruction(settings=None):
    clock = current_time_context(settings)
    if not clock:
        return ''
    return (f"【现实时间】当前时间{clock['now']}，{clock['weekday']}，时区{clock['timezone']}。"
            '这是本次请求发出时的时钟；今天、明天等相对表述优先以原消息发送时间理解，发送时间未知时参考接收时间。'
            '历史录入时间不等于事件发生时间；仅在相关时自然提及，不必每次报时。\n')


def refresh_dispatch_time(messages, settings=None, *, purpose='chat', for_transport=False):
    """Refresh only application-owned clock fields, keeping caller input immutable."""
    updated = []
    for message in messages:
        item = dict(message)
        content = item.get('content')
        marker = item.get('_shiori_clock')
        if marker == 'system' and item.get('role') == 'system' and isinstance(content, str):
            # The first line is owned by ChatService. A persona may itself
            # contain this literal heading later; never rewrite those lines.
            item['content'] = re.sub(r'^【现实时间】[^\n]*(?:\n|$)', lambda _: clock_instruction(settings), content, count=1)
        elif marker == 'planner' and purpose == 'planner' and item.get('role') == 'user' and isinstance(content, str):
            try:
                payload = json.loads(content)
            except (ValueError, TypeError):
                payload = None
            if isinstance(payload, dict) and 'current_message_id' in payload and 'current_user_id' in payload:
                clock = current_time_context(settings)
                if clock:
                    payload['current_time'] = clock
                else:
                    payload.pop('current_time', None)
                item['content'] = json.dumps(payload, ensure_ascii=False, separators=(',', ':'))
        if for_transport:
            item.pop('_shiori_clock', None)
        updated.append(item)
    return updated


def config_zone(settings=None):
    config = settings if isinstance(settings, dict) else settings.get() if settings is not None else {}
    name = config.get('time', {}).get('timezone', DEFAULT_TIMEZONE)
    if not isinstance(name, str) or len(name) > 100:
        raise ValueError('时区须为有效的 IANA 名称，例如 Asia/Shanghai 或 America/New_York')
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ValueError('时区须为有效的 IANA 名称，例如 Asia/Shanghai 或 America/New_York') from exc


def local_now(settings=None):
    return datetime.now(config_zone(settings))


def current_time_context(settings=None, *, now=None):
    config = settings if isinstance(settings, dict) else settings.get() if settings is not None else {}
    if not config.get('time', {}).get('awareness_enabled', True):
        return None
    zone = config_zone(config)
    current = (now or datetime.now(timezone.utc)).astimezone(zone)
    return {'now': current.isoformat(timespec='seconds'), 'timezone': zone.key,
            'weekday': ('星期一', '星期二', '星期三', '星期四', '星期五', '星期六', '星期日')[current.weekday()]}


def day_bounds(settings=None, *, now=None):
    zone = config_zone(settings)
    current = (now or datetime.now(timezone.utc)).astimezone(zone)
    start = current.replace(hour=0, minute=0, second=0, microsecond=0)
    end = start + timedelta(days=1)
    stamp = lambda value: value.astimezone(timezone.utc).isoformat(timespec='microseconds').replace('+00:00', 'Z')
    return stamp(start), stamp(end)
