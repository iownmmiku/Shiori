from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import json
import sqlite3

import pytest

from qunyu.db import Store


@pytest.fixture
def store(tmp_path):
    instance = Store(tmp_path / "data")
    yield instance
    instance.close()


def memory(store, **changes):
    return store.create_memory({"scope_id": "qq:private:alice", "user_id": "alice",
                                "content": "喜欢猫", **changes})


def test_empty_store_has_only_config_seeds_and_wal(store):
    assert store.stats()["messages"] == store.stats()["memories"] == 0
    assert {p["id"] for p in store.list_personas()} == {"default"}
    assert store.get_persona("default")["name"] == "林星遥"
    with sqlite3.connect(store.path) as conn:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"


def test_default_persona_is_a_complete_character_and_never_replaces_an_existing_one(tmp_path):
    data = tmp_path / "data"
    store = Store(data)
    try:
        persona = store.get_persona("default")
        assert persona["interests"] and persona["exploration_enabled"] is True
        assert set(persona["profile"]) == {"residence", "occupation", "age", "birthday", "routine",
                                           "background", "relationship"}
        assert len(persona["system_prompt"]) <= 6000 and sum(map(len, persona["profile"].values())) <= 2000
        assert "未成年" in persona["system_prompt"]  # a minor: no romance or sexual roleplay
        store.save_persona({**persona, "name": "初音未来", "profile": {}})
    finally:
        store.close()
    reopened = Store(data)
    try:
        assert reopened.get_persona("default")["name"] == "初音未来"
    finally:
        reopened.close()


def test_scope_lazy_defaults_preserve_extensions(store):
    assert store.get_scope("qq:group:123")["dictionary_use"] is False
    original = store.upsert_scope({"id": "qq:group:123", "extra": "kept", "settings": {"a": 1}})
    current = store.upsert_scope({"id": original["id"], "settings": {"b": 2}})
    assert current["extra"] == "kept" and current["settings"] == {"a": 1, "b": 2}


def test_recall_and_profile_scope_and_private_boundaries(store):
    own = memory(store)
    memory(store, user_id="bob", content="喜欢狗")
    memory(store, scope_id="qq:private:other", content="喜欢鸟")
    assert [m["id"] for m in store.recall("qq:private:alice", "alice", "喜欢")] == [own["id"]]
    assert store.profile("qq:private:alice", "alice")["summary"] == "喜欢猫"
    assert store.profile("qq:private:other", "alice")["summary"] == "喜欢鸟"


@pytest.mark.parametrize("scope", ["qq:group:123", "qqofficial:group:openid"])
def test_public_group_never_reveals_private_memory_even_to_same_user(store, scope):
    private = memory(store, scope_id=scope, visibility="private", content="秘密猫")
    shared = memory(store, scope_id=scope, visibility="group", content="群里的猫")
    assert [m["id"] for m in store.recall(scope, "alice", "猫")] == [shared["id"]]
    assert private["id"] not in {m["id"] for m in store.profile(scope, "alice")["memories"]}
    assert len(store.list_memories(scope)) == 2


def test_validity_excludes_expired_future_and_archived_records(store):
    memory(store, valid_to="2000-01-01T00:00:00Z")
    memory(store, valid_from="2999-01-01T00:00:00Z")
    memory(store, status="archived")
    current = memory(store, valid_to="2999-01-01T00:00:00+08:00")
    assert [m["id"] for m in store.recall(current["scope_id"], "alice", "猫")] == [current["id"]]


def test_correction_invalidates_transitive_derived_memory(store):
    fact = memory(store, source_ids=["old-source"])
    summary = memory(store, kind="note", content="旧摘要：喜欢猫", dependencies={fact["id"]: 1})
    episode = memory(store, kind="event", content="猫事件", dependencies={summary["id"]: 1})
    assert len(store.recall(fact["scope_id"], "alice", "猫")) == 3
    corrected = store.update_memory(fact["id"], {"content": "喜欢狗", "source_ids": ["new-source"]})
    assert corrected["version"] == 2
    assert corrected["blocked_source_ids"] == ["old-source"]
    assert store.recall(fact["scope_id"], "alice", "猫") == []
    assert store.get_memory(episode["id"])["status"] == "active"
    assert store.profile(fact["scope_id"], "alice")["summary"] == "喜欢狗"


def test_missing_private_and_cyclic_dependencies_are_not_recalled(store):
    private = memory(store, user_id="bob")
    memory(store, content="私密派生", dependencies={private["id"]: 1})
    memory(store, content="缺失派生", dependencies={"not-there": 1})
    first = memory(store, id="cycle-1", content="循环甲", dependencies={"cycle-2": 1})
    memory(store, id="cycle-2", content="循环乙", dependencies={first["id"]: 1})
    assert store.recall(first["scope_id"], "alice", "") == []


def test_delete_is_idempotent_tombstone_and_scrubs_versions(store):
    record = memory(store, content="不可保留的正文", source_ids=["m1"])
    store.update_memory(record["id"], {"content": "纠正正文", "source_ids": ["m2"]})
    deleted = store.delete_memory(record["id"])
    assert deleted["status"] == "deleted" and deleted["content"] == ""
    assert deleted["blocked_source_ids"] == ["m1", "m2"]
    assert store.delete_memory(record["id"]) == deleted
    assert store.list_memories() == []
    with sqlite3.connect(store.path) as conn:
        snapshots = conn.execute("SELECT data FROM memory_versions WHERE memory_id=?", (record["id"],)).fetchall()
    assert len(snapshots) == 1 and "不可保留的正文" not in snapshots[0][0]
    with pytest.raises(ValueError):
        store.update_memory(record["id"], {"content": "恢复"})


def test_jargon_multiple_meanings_boundaries_approval_and_blocked_sources(store):
    first = store.create_jargon({"term": "GG", "meaning": "比赛结束", "status": "approved", "source_ids": ["j1"]})
    second = store.create_jargon({"term": "GG", "meaning": "哥哥", "status": "approved", "topic": "称呼"})
    store.create_jargon({"term": "开香槟", "meaning": "过早庆祝", "status": "pending"})
    store.create_jargon({"scope_id": "qq:group:other", "term": "GG", "meaning": "别群含义", "status": "approved"})
    assert store.match_jargon("web:default", "EGGPLANT") == []
    assert {x["id"] for x in store.match_jargon("web:default", "gg!")} == {first["id"], second["id"]}
    assert store.match_jargon("web:default", "开香槟") == []
    corrected = store.update_jargon(first["id"], {"meaning": "再见"})
    assert corrected["version"] == 2 and corrected["blocked_source_ids"] == ["j1"]
    assert store.delete_jargon(first["id"])["blocked_source_ids"] == ["j1"]


def test_task_idempotency_survives_reopen_and_due_filter(store, tmp_path):
    task = store.create_task({"title": "提醒", "kind": "reminder", "due_at": "2020-01-01T08:00:00+08:00", "idempotency_key": "same"})
    assert store.create_task({"title": "different", "idempotency_key": "same"})["id"] == task["id"]
    other = Store(store.data_dir)
    try:
        assert other.create_task({"idempotency_key": "same"})["id"] == task["id"]
        assert len(other.due_tasks("2020-01-01T00:00:00Z")) == 1
        other.update_task(task["id"], {"status": "cancelled"})
        assert other.due_tasks("2020-01-01T00:00:00Z") == []
        other.update_task(task["id"], {"status": "failed"})
        assert other.due_tasks("2020-01-01T00:00:00Z") == []
    finally:
        other.close()


def test_messages_chronological_pagination_and_clear_is_scoped(store):
    first = store.add_message("web:default", "owner", "user", "第一条")
    second = store.add_message("web:default", "owner", "assistant", "第二条", source_ids=[first["id"]])
    store.add_message("qq:private:bob", "bob", "user", "别的聊天")
    assert store.list_messages("web:default", limit=1)[0]["id"] == second["id"]
    assert store.list_messages("web:default", before=second["id"])[0]["id"] == first["id"]
    store.clear_messages("web:default")
    assert len(store.list_messages("qq:private:bob")) == 1


def test_usage_hong_kong_day_and_secret_redaction(store):
    hk = timezone(timedelta(hours=8))
    start = datetime.now(hk).replace(hour=0, minute=0, second=0, microsecond=0)
    store.record_usage({"created_at": (start - timedelta(seconds=1)).isoformat(), "input_tokens": 5, "cost": 3})
    store.record_usage({"created_at": (start + timedelta(seconds=1)).isoformat(), "input_tokens": 7, "output_tokens": 2, "cost": 1, "api_key": "secret"})
    assert store.stats()["cost_today"] == 1
    assert store.stats()["tokens_today"] == 9
    assert "api_key" not in store.list_usage()[0]
    event = store.add_event("test", "测试", model={"api_key": "secret", "name": "model"})
    assert event["details"]["model"] == {"name": "model"}


def test_backup_restore_uses_same_object_and_excludes_messages_and_secrets(store, tmp_path):
    original = memory(store, content="备份记忆")
    store.update_memory(original["id"], {"content": "纠正后的备份记忆"})
    store.add_message("web:default", "owner", "user", "聊天")
    store.upsert_scope({"id": "web:default", "settings": {"api_key": "hidden", "normal": True}})
    backup = store.backup(tmp_path / "backup.db", include_messages=False)
    store.create_memory({"content": "不在备份中"})
    connection = store._conn
    store.restore_from(backup)
    assert store._conn is connection
    assert store.list_messages("web:default") == []
    assert len(store.list_memories()) == 1
    assert store.get_scope("web:default")["settings"] == {"normal": True}
    assert store.get_memory(original["id"])["version"] == 2


def test_restore_rejects_extra_schema_before_touching_live_data(store, tmp_path):
    original = memory(store)
    candidate = store.backup(tmp_path / "bad.db")
    with sqlite3.connect(candidate) as conn:
        conn.execute("CREATE VIEW surprise AS SELECT * FROM memories")
    with pytest.raises(ValueError):
        store.restore_from(candidate)
    assert store.get_memory(original["id"])["content"] == "喜欢猫"


def test_concurrent_task_creation_and_messages(store):
    def write(i):
        store.add_message("web:default", "owner", "user", str(i))
        return store.create_task({"title": "once", "idempotency_key": "thread-key"})["id"]
    with ThreadPoolExecutor(max_workers=6) as executor:
        ids = list(executor.map(write, range(30)))
    assert len(set(ids)) == 1
    assert len(store.list_messages("web:default")) == 30


def test_usage_totals_include_over_ten_thousand_rows_without_list_cap(store, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("budget aggregation must never use paginated lists")

    monkeypatch.setattr(store, "list_usage", forbidden)
    timestamp = "2026-10-15T04:00:00.000000Z"
    rows = []
    for index in range(10001):
        scope_id = "web:default" if index < 10000 else "qq:group:other"
        payload = {"id": str(index), "scope_id": scope_id, "created_at": timestamp,
                   "input_tokens": 1, "output_tokens": 2, "cost": 0.01}
        rows.append((str(index), timestamp, 1, 2, 0.01, json.dumps(payload)))
    with store._lock, store._conn:
        store._conn.executemany("INSERT INTO usage(id,created_at,input_tokens,output_tokens,cost,data) VALUES (?,?,?,?,?,?)", rows)
    totals = store.usage_totals(day="2026-10-15")
    assert totals["input_tokens"] == 10001 and totals["output_tokens"] == 20002 and totals["tokens"] == 30003
    assert totals["cost"] == pytest.approx(100.01)
    assert store.usage_totals("web:default", day="2026-10-15")["tokens"] == 30000
    assert store.usage_totals("qq:group:other", day="2026-10-15")["tokens"] == 3


def test_usage_totals_hong_kong_boundaries_and_empty_dates(store):
    for timestamp, count in [("2026-10-14T15:59:59Z", 1), ("2026-10-14T16:00:00Z", 2),
                             ("2026-10-15T15:59:59Z", 4), ("2026-10-15T16:00:00Z", 8)]:
        store.record_usage({"scope_id": "web:default", "created_at": timestamp, "input_tokens": count})
    assert store.usage_totals(day="2026-10-15")["tokens"] == 6
    assert store.usage_totals(day=datetime(2026, 10, 14, 16, tzinfo=timezone.utc))["tokens"] == 6
    assert store.usage_totals(day="2026-10-13") == {"input_tokens": 0, "output_tokens": 0, "tokens": 0, "cost": 0.0}
    with pytest.raises(ValueError):
        store.usage_totals(day="2026-02-30")
