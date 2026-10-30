"""Deterministic schedule rules; no model credentials or chat platform calls."""
from copy import deepcopy
from datetime import datetime, timedelta
import builtins
from zoneinfo import ZoneInfo

import pytest

from shiori.life_rules.core import (
    active_events, merge_locked_slots, minute, slot_at, slow_bounds,
    validate_events, validate_profile, validate_slots, weekly_for_day,
)
from shiori.life_rules.adaptive import (
    adjustment_summary, create_adjustment, effective_activity, target_activity,
    validate_decision,
)
from shiori.life_rules.prompts import parse_object, public_calendar, validate_diary
from shiori.life_rules.recurrence import apply_recurrences, expand_rules, validate_rules


def slot(start="00:00", end="24:00", *, title="休息", status="free", **extra):
    return dict(start=start, end=end, title=title, status=status, **extra)


def profile():
    return {"world_type": "original", "world_name": "星海", "countries": [],
            "timezone": "Asia/Hong_Kong", "weekly": [slot(days=list(range(1, 8)))]}


def decision(**extra):
    return dict(action="defer", activity_kind="sleep", importance="normal",
                delay_minutes=20, tone="plain", reason="持续聊天，短暂晚些休息", **extra)


@pytest.mark.parametrize("rows", [
    [slot("01:00")], [slot(end="12:00"), slot("11:00")],
    [slot("23:00", "07:00")], [slot(end="25:00")],
    [slot(status="unknown")], [slot(status=[])], [slot(title="")],
    [slot(slow_min_seconds=5)], [slot(slow_min_seconds=60, slow_max_seconds=5)],
])
def test_day_requires_complete_non_overlapping_intervals(rows):
    with pytest.raises(ValueError):
        validate_slots(rows)


def test_week_requires_all_seven_days_and_daily_projection():
    draft = profile()
    draft["weekly"][0]["days"] = [1, 2, 3, 4, 5]
    with pytest.raises(ValueError):
        validate_profile(draft)
    clean = validate_profile(profile())
    assert weekly_for_day(clean, "2026-10-29") == validate_slots([slot()])
    assert minute("24:00", end=True) == 1440
    with pytest.raises(ValueError):
        minute("24:00")


def test_lock_overlay_preserves_manual_times_and_overrides():
    locked = slot("08:00", "10:00", title="值班", status="busy", locked=True,
                  slow_min_seconds=20, slow_max_seconds=40)
    result = merge_locked_slots([slot()], [locked])
    assert [(x["start"], x["end"]) for x in result] == [
        ("00:00", "08:00"), ("08:00", "10:00"), ("10:00", "24:00")]
    assert result[1]["title"] == "值班" and result[1]["locked"]
    assert slow_bounds(result[1], {"slow_min_seconds": 60, "slow_max_seconds": 180}) == (20, 40)
    assert not validate_slots(result)[0]["locked"]


def test_week_lock_is_only_applied_to_its_weekdays():
    result = merge_locked_slots([slot(days=list(range(1, 8)))],
                               [slot("08:00", "10:00", days=[2], locked=True, title="课程")], weekly=True)
    clean = {**profile(), "weekly": result}
    assert len(weekly_for_day(clean, "2026-10-27")) == 3
    assert len(weekly_for_day(clean, "2026-10-28")) == 1


def test_calendar_provenance_and_cross_year_dates():
    row = dict(title="冬季庆典", start="2026-12-28", end="2027-01-04", kind="festival",
               day_off=True, origin="canon", source_urls=["https://example.org/unsupported"])
    generated = validate_events([row], 2027)[0]
    assert generated["origin"] == "generated" and generated["source_urls"] == []
    referenced = validate_events([row], 2027, source_urls=row["source_urls"])[0]
    assert referenced["origin"] == "reference"
    assert active_events([referenced], "2027-01-04") == [referenced]
    assert not active_events([referenced], "2027-01-05")


def test_slot_query_uses_saved_timezone_and_excludes_old_day():
    plan = {"date": "2026-10-29", "timezone": "Asia/Hong_Kong",
            "slots": [slot(end="08:00", status="busy"), slot("08:00")]}
    utc = ZoneInfo("UTC")
    assert slot_at(plan, datetime(2026, 10, 28, 23, 59, tzinfo=utc))[0]["status"] == "busy"
    assert slot_at(plan, datetime(2026, 10, 29, 0, tzinfo=utc))[0]["status"] == "free"
    with pytest.raises(ValueError, match="当地日期"):
        slot_at(plan, datetime(2026, 10, 30, tzinfo=utc))
    with pytest.raises(ValueError, match="时区"):
        slot_at(plan, datetime(2026, 10, 29))


def test_repeated_dst_hour_does_not_return_an_expired_due_time():
    zone = ZoneInfo("America/New_York")
    now = datetime(2026, 11, 1, 1, 15, tzinfo=zone, fold=1)
    plan = {"date": "2026-11-01", "timezone": zone.key,
            "slots": [slot(end="01:30", status="busy", title="休息"), slot("01:30")]}
    _, due = slot_at(plan, now)
    assert due > now.timestamp()
    assert datetime.fromtimestamp(due, zone).fold == 1
    target = target_activity(plan, now)
    assert target["active"] and target["ends_at"] == due


def test_spring_gap_and_24_hour_day_use_local_wall_boundaries():
    zone = ZoneInfo("America/New_York")
    now = datetime(2026, 3, 8, 1, 59, tzinfo=zone)
    plan = {"date": "2026-03-08", "timezone": zone.key,
            "slots": [slot(end="02:30", status="busy"), slot("02:30")]}
    _, due = slot_at(plan, now)
    assert datetime.fromtimestamp(due, zone).isoformat() == "2026-03-08T03:00:00-04:00"
    _, midnight = slot_at({**plan, "slots": [slot()]}, now)
    assert datetime.fromtimestamp(midnight, zone).isoformat() == "2026-03-09T00:00:00-04:00"


def test_continuous_sleep_across_midnight_keeps_original_deferral_budget():
    zone = ZoneInfo("Asia/Hong_Kong")
    today = {"date": "2026-10-29", "revision": 4,
             "slots": [slot(end="23:50"), slot("23:50", title="睡觉", status="busy")]}
    tomorrow = {"date": "2026-10-30", "revision": 1,
                "slots": [slot(end="08:00", title="睡觉", status="busy"), slot("08:00")]}
    before = datetime(2026, 10, 29, 23, 55, tzinfo=zone)
    first = target_activity(today, before, next_schedule=tomorrow)
    override = create_adjustment(decision(), first, before, umo="qq:group:1", cid="c",
                                 persona_key="p", schedule_revision=4)
    assert override["until"] == datetime(2026, 10, 30, 0, 10, tzinfo=zone).timestamp()
    after = before + timedelta(minutes=6)
    second = target_activity(tomorrow, after, previous_schedule=today)
    renewed = create_adjustment(decision(), second, after, umo="qq:group:1", cid="c",
                                persona_key="p", schedule_revision=1, previous=override)
    assert renewed["until"] == override["until"]
    effective = effective_activity(tomorrow["slots"][0], after, renewed, umo="qq:group:1", cid="c",
                                   persona_key="p", schedule_revision=1, schedule_date=tomorrow["date"], source_revision=4)
    assert effective["status"] == "free"
    expired = effective_activity(tomorrow["slots"][0], after + timedelta(minutes=10), renewed,
                                 umo="qq:group:1", cid="c", persona_key="p", schedule_revision=1,
                                 schedule_date=tomorrow["date"], source_revision=4)
    assert expired["status"] == "busy"
    assert "reason" not in adjustment_summary(override)


def test_locked_or_fixed_duties_and_untrusted_enum_cannot_be_deferred():
    now = datetime(2026, 10, 29, 10, tzinfo=ZoneInfo("Asia/Hong_Kong"))
    for activity in [slot(title="上课", status="busy"), slot(title="睡觉", status="busy", locked=True)]:
        target = target_activity({"date": now.date().isoformat(), "slots": [activity]}, now)
        with pytest.raises(ValueError):
            validate_decision(decision(), target)
    target = target_activity({"date": now.date().isoformat(), "slots": [slot(title="睡觉", status="busy")]}, now)
    with pytest.raises(ValueError, match="持续对话"):
        validate_decision(decision(), target, active_conversation=False)
    invalid = decision()
    invalid["action"] = []
    with pytest.raises(ValueError):
        validate_decision(invalid, target)


def test_contact_override_does_not_cross_persona_or_session():
    now = datetime(2026, 10, 29, 0, 1, tzinfo=ZoneInfo("Asia/Hong_Kong"))
    activity = slot(title="睡觉", status="busy")
    plan = {"date": now.date().isoformat(), "slots": [activity]}
    target = target_activity(plan, now)
    override = create_adjustment(decision(), target, now, umo="qq:private:1", cid="c", persona_key="p", schedule_revision=1)
    for identity in [dict(umo="qq:private:2", cid="c", persona_key="p"),
                     dict(umo="qq:private:1", cid="c", persona_key="other")]:
        result = effective_activity(activity, now, override, **identity, schedule_revision=1)
        assert result["status"] == "busy"


def test_recurrences_keep_cross_years_leap_policy_and_manual_edits():
    rules = [dict(id="winter", title="冬祭", month=12, day=30, duration_days=5),
             dict(id="leap", title="生日", month=2, day=29, leap_policy="feb28")]
    rows = expand_rules(rules, 2027)
    assert any(x["start"] == "2026-12-30" and x["end"] == "2027-01-03" for x in rows)
    assert any(x["title"] == "生日" and x["start"] == "2027-02-28" for x in rows)
    raw = {"events": [dict(title="生日", start="2027-02-28", end="2027-02-28", origin="manual")]}
    merged = apply_recurrences(raw, rules, 2027)
    assert any(x.get("origin") == "manual" for x in merged["events"])
    assert raw["events"][0]["origin"] == "manual"
    assert apply_recurrences(merged, rules, 2027) == merged


def test_fictional_repeat_dates_require_explicit_mapping():
    rule = dict(id="star", title="星辰祭", calendar="fictional", month=8, day=8)
    with pytest.raises(ValueError, match="映射"):
        validate_rules([rule])
    row = expand_rules([{**rule, "mapping": "星海历第八日对应公历八月八日"}], 2027)[0]
    assert "映射" in row["description"] and row["origin"] == "recurring"


def test_model_contract_rejects_non_objects_and_diary_without_content():
    assert parse_object('```json\n{"events":[]}\n```') == {"events": []}
    for raw in ["[]", "not json", None, "x" * 300001]:
        with pytest.raises(ValueError):
            parse_object(raw)
    for raw in [[], {}, {"title": "空日记"}]:
        with pytest.raises(ValueError):
            validate_diary(raw)
    assert validate_diary({"title": "一天", "content": "活动后的感受"})["content"] == "活动后的感受"


def test_unavailable_public_calendar_is_explicit_and_no_culture_means_no_calendar(monkeypatch):
    original = builtins.__import__
    def unavailable(name, *args, **kwargs):
        if name == "holidays":
            raise ImportError("not installed")
        return original(name, *args, **kwargs)
    monkeypatch.setattr(builtins, "__import__", unavailable)
    assert public_calendar({"countries": []}, 2027) == ([], [])
    rows, warnings = public_calendar({"countries": ["JP"]}, 2027)
    assert rows == [] and any("未安装" in warning for warning in warnings)
