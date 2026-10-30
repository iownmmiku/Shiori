"""Pure character-life rules adapted from iownmmiku/astrbot_plugin_persona_schedule 0.3.5.

This module has no AstrBot runtime dependency. Native storage, model calls and
chat delivery belong to Shiori.
"""
from __future__ import annotations

import copy
import hashlib
import json
import re
import uuid
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

STATES = {"free", "slow", "busy"}


LABELS = {"free": "空闲", "slow": "半忙", "busy": "忙碌"}


KINDS = {"festival", "vacation", "activity", "anniversary"}


def field(obj, name, default=None):
    return obj.get(name, default) if isinstance(obj, dict) else getattr(obj, name, default)


def digest(value) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()[:24]


def text(value, limit=1000) -> str:
    if not isinstance(value, str):
        raise ValueError("文字字段必须是字符串")
    return value.strip()[:limit]


def boolean(value) -> bool:
    if not isinstance(value, bool):
        raise ValueError("开关必须为 true 或 false")
    return value


def minute(value: str, *, end=False) -> int:
    if not isinstance(value, str) or not re.fullmatch(r"\d{2}:\d{2}", value):
        raise ValueError("时间格式应为 HH:MM")
    hour, part = map(int, value.split(":"))
    if end and (hour, part) == (24, 0):
        return 1440
    if not (0 <= hour < 24 and 0 <= part < 60):
        raise ValueError("时间超出范围")
    return hour * 60 + part


def _normalise_slots(rows, *, weekly=False, allow_empty=False):
    if not isinstance(rows, list) or not (0 if allow_empty else 1) <= len(rows) <= 120:
        raise ValueError("日程应包含 1～120 个时间段")
    result = []
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("时间段必须是对象")
        start, end = minute(row.get("start")), minute(row.get("end"), end=True)
        if start >= end:
            raise ValueError("结束时间必须晚于开始时间，跨午夜请拆成两段")
        state = row.get("status")
        if not isinstance(state, str) or state not in STATES:
            raise ValueError("状态只能为 free / slow / busy")
        title = text(row.get("title", ""), 120)
        if not title:
            raise ValueError("请填写日程名称")
        clean = {"start": row["start"], "end": row["end"], "status": state,
                 "title": title, "note": text(row.get("note", ""), 800),
                 "locked": boolean(row.get("locked", False))}
        delay_keys = ("slow_min_seconds", "slow_max_seconds")
        if any(key in row for key in delay_keys):
            if not all(key in row for key in delay_keys):
                raise ValueError("时段最短和最长延迟必须一起设置")
            low, high = (row[key] for key in delay_keys)
            if any(type(value) is not int or not 5 <= value <= 7200 for value in (low, high)):
                raise ValueError("时段延迟应为 5～7200 秒的整数")
            if low > high:
                raise ValueError("时段最短延迟不能超过最长延迟")
            clean.update(zip(delay_keys, (low, high)))
        if weekly:
            days = row.get("days")
            if not isinstance(days, list) or not days or any(type(d) is not int or d not in range(1, 8) for d in days):
                raise ValueError("days 应为周一到周日的数字数组（1～7）")
            clean["days"] = sorted(set(days))
        result.append(clean)
    return result


def validate_slots(rows, *, weekly=False):
    result = _normalise_slots(rows, weekly=weekly)
    for day in range(1, 8) if weekly else [None]:
        selected = sorted((r for r in result if day is None or day in r["days"]), key=lambda r: r["start"])
        cursor = 0
        for row in selected:
            start = minute(row["start"])
            if start != cursor:
                raise ValueError(f"{'周' + str(day) if day else '当天'}日程有重叠或空缺，请完整覆盖 00:00～24:00")
            cursor = minute(row["end"], end=True)
        if cursor != 1440:
            raise ValueError("日程必须完整覆盖 00:00～24:00")
    return sorted(result, key=lambda r: (r["start"], r.get("days", [0])))


def slow_bounds(slot, settings):
    """Use a validated slot's delay override, otherwise the global interval."""
    return (slot.get("slow_min_seconds", settings["slow_min_seconds"]),
            slot.get("slow_max_seconds", settings["slow_max_seconds"]))


def merge_locked_slots(generated, previous, *, weekly=False):
    """Overlay prior locked slots on a complete candidate without moving locks.

    `previous` may be a complete schedule or a list of locked fragments. All
    returned rows are independently validated. Weekly schedules are merged per
    weekday, then identical rows are grouped back into a shared `days` list.
    """
    candidates = validate_slots(generated, weekly=weekly)
    locks = [row for row in _normalise_slots([] if previous is None else previous,
                                           weekly=weekly, allow_empty=True)
             if row["locked"]]

    def clock(value):
        return f"{value // 60:02d}:{value % 60:02d}"

    def merge_day(day_rows, day_locks):
        day_locks = sorted(day_locks, key=lambda row: row["start"])
        cursor = 0
        for row in day_locks:
            if minute(row["start"]) < cursor:
                raise ValueError("锁定时段不能重叠")
            cursor = minute(row["end"], end=True)
        result = [copy.deepcopy(row) for row in day_locks]
        for row in day_rows:
            start, end = minute(row["start"]), minute(row["end"], end=True)
            cursor = start
            for lock in day_locks:
                left, right = minute(lock["start"]), minute(lock["end"], end=True)
                if right <= cursor:
                    continue
                if left >= end:
                    break
                if cursor < left:
                    result.append({**row, "start": clock(cursor), "end": clock(left)})
                cursor = max(cursor, right)
                if cursor >= end:
                    break
            if cursor < end:
                result.append({**row, "start": clock(cursor), "end": clock(end)})
        return sorted(result, key=lambda row: row["start"])

    if not weekly:
        return validate_slots(merge_day(candidates, locks))
    grouped = {}
    for weekday in range(1, 8):
        select = lambda rows: [{k: copy.deepcopy(v) for k, v in row.items() if k != "days"}
                               for row in rows if weekday in row["days"]]
        for row in merge_day(select(candidates), select(locks)):
            key = json.dumps(row, ensure_ascii=False, sort_keys=True)
            if key not in grouped:
                grouped[key] = {**row, "days": []}
            grouped[key]["days"].append(weekday)
    return validate_slots(list(grouped.values()), weekly=True)


def validate_profile(obj):
    if not isinstance(obj, dict):
        raise ValueError("角色档案必须为对象")
    world = obj.get("world_type")
    if not isinstance(world, str) or world not in {"real", "fanfiction", "original"}:
        raise ValueError("world_type 应为 real / fanfiction / original")
    countries = obj.get("countries", [])
    if not isinstance(countries, list) or len(countries) > 8 or any(not isinstance(c, str) or not re.fullmatch(r"[A-Z]{2}", c) for c in countries):
        raise ValueError("countries 应为国家/地区代码数组，如 CN、JP；无关联时留空")
    tz = text(obj.get("timezone", "Asia/Shanghai"), 80)
    try:
        ZoneInfo(tz)
    except (ValueError, ZoneInfoNotFoundError) as exc:
        raise ValueError("角色时区应为有效的 IANA 名称，如 Asia/Shanghai") from exc
    return {"world_type": world, "world_name": text(obj.get("world_name", ""), 160),
            "countries": sorted(set(countries)), "timezone": tz,
            "identity": text(obj.get("identity", ""), 300),
            "lifestyle": text(obj.get("lifestyle", ""), 2500),
            "calendar_notes": text(obj.get("calendar_notes", ""), 2500),
            "weekly": validate_slots(obj.get("weekly"), weekly=True)}


def validate_events(rows, year, *, manual=False, source_urls=()):
    if type(year) is not int or not 1970 <= year <= 2100:
        raise ValueError("年份应在 1970～2100")
    if not isinstance(rows, list) or len(rows) > 250:
        raise ValueError("活动应为数组，最多 250 项")
    result = []
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("活动必须为对象")
        if not all(isinstance(row.get(key), str) for key in ("start", "end")):
            raise ValueError("活动起止日期应为 YYYY-MM-DD")
        start, end = date.fromisoformat(row["start"]), date.fromisoformat(row["end"])
        if start > end or start.year > year or end.year < year or (end - start).days > 366:
            raise ValueError("活动日期应与选中年份相交，且不超过 366 天")
        kind = row.get("kind", "activity")
        if not isinstance(kind, str) or kind not in KINDS:
            raise ValueError("活动种类无效")
        title = text(row.get("title", ""), 160)
        if not title:
            raise ValueError("活动名称不能为空")
        urls = row.get("source_urls", [])
        if not isinstance(urls, list):
            raise ValueError("source_urls 必须是数组")
        urls = [u for u in urls if isinstance(u, str) and u.startswith("https://") and (manual or u in source_urls)][:8]
        origin = "manual" if manual else "reference" if urls else "generated"
        result.append({"id": text(row.get("id", uuid.uuid4().hex), 80), "title": title,
                       "start": start.isoformat(), "end": end.isoformat(), "kind": kind,
                       "day_off": boolean(row.get("day_off", False)),
                       "description": text(row.get("description", ""), 2000),
                       "origin": origin, "source_urls": urls})
    return sorted(result, key=lambda r: (r["start"], r["title"]))


def active_events(events, day):
    return [r for r in events if r["start"] <= day <= r["end"]]


def weekly_for_day(profile, day):
    weekday = date.fromisoformat(day).isoweekday()
    return [{k: v for k, v in row.items() if k != "days"}
            for row in profile["weekly"] if weekday in row["days"]]


def wall_boundary(day, minutes, zone, *, after=None, before=None):
    """Resolve a wall-clock boundary, including missing and repeated DST times.

    Repeated times use the first occurrence after ``after``. A boundary inside
    a spring-forward gap advances to the first real wall minute instead of
    accidentally waiting an extra hour. 24:00 means the next local midnight.
    """
    if isinstance(day, str):
        day = date.fromisoformat(day)
    naive = datetime.combine(day, datetime.min.time()) + timedelta(minutes=minutes)
    for skipped in range(181):
        wall = naive + timedelta(minutes=skipped)
        candidates = set()
        for fold in (0, 1):
            candidate = wall.replace(tzinfo=zone, fold=fold)
            stamp = candidate.timestamp()
            back = datetime.fromtimestamp(stamp, zone)
            if back.replace(tzinfo=None) == wall:
                candidates.add(stamp)
        if candidates:
            if before is not None:
                preceding = sorted(stamp for stamp in candidates if stamp <= before)
                return preceding[-1] if preceding else min(candidates)
            following = sorted(stamp for stamp in candidates if after is None or stamp > after)
            return following[0] if following else max(candidates)
    raise ValueError("角色时区的日程边界无法解析")


def slot_at(schedule, now):
    if not isinstance(now, datetime) or now.utcoffset() is None:
        raise ValueError("日程查询需要带角色时区的时间")
    # The caller's timestamp may be UTC; the saved day owns its wall clock.
    if schedule.get("timezone"):
        now = now.astimezone(ZoneInfo(schedule["timezone"]))
    if schedule.get("date") and schedule["date"] != now.date().isoformat():
        raise ValueError("日程日期与角色当地日期不一致")
    at = now.hour * 60 + now.minute
    row = next((r for r in schedule["slots"] if minute(r["start"]) <= at < minute(r["end"], end=True)), None)
    if row is None:
        raise ValueError("当前时间没有对应日程，请检查时段覆盖")
    end = wall_boundary(now.date(), minute(row["end"], end=True), now.tzinfo, after=now.timestamp())
    return row, end
