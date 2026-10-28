import json

import pytest

from qunyu.db import Store
from shiori.memory import MemoryEngine


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
def memory(tmp_path):
    store = Store(tmp_path / "memory-v6")
    engine = MemoryEngine(store, {"memory": {"top_k": 8}})
    yield store, engine
    engine.close()
    store.close()


def fact(store, content="星辉咖啡讲座", **fields):
    return store.create_memory({"scope_id": "web:default", "user_id": "owner", "content": content, **fields})


def proposal(engine, operation):
    return next(item for item in engine.propose_organization("web:default", "owner")["suggestions"] if item["details"]["operation"] == operation)


@pytest.mark.anyio
async def test_use_reinforcement_is_deduplicated_does_not_edit_fact_and_never_creates_a_match(memory):
    store, engine = memory
    item = fact(store)
    for _ in range(3):
        result = engine.mark_used("web:default", "owner", {item["id"]: 1}, "same-request")
    assert result["count"] == 0
    assert store.memory_use_stats("web:default", "owner")[item["id"]]["count"] == 1
    assert store.get_memory(item["id"])["version"] == 1
    assert len(store.list_memory_versions(item["id"])) == 1
    old_score = (await engine.inspect("web:default", "owner", "咖啡讲座"))["items"][0]["retrieval"]["score"]
    for index in range(20):
        engine.mark_used("web:default", "owner", {item["id"]: 1}, f"request-{index}")
    new_score = (await engine.inspect("web:default", "owner", "咖啡讲座"))["items"][0]["retrieval"]["score"]
    assert old_score < new_score < old_score + 0.07
    assert (await engine.inspect("web:default", "owner", "xylophone"))["items"] == []
    assert engine.mark_used("web:default", "other", {item["id"]: 1}, "other")["count"] == 0
    edited = store.update_memory(item["id"], {"content": "已换成绘画讲座"})
    assert engine.mark_used("web:default", "owner", {item["id"]: 1}, "stale")["count"] == 0
    engine.mark_used("web:default", "owner", {item["id"]: edited["version"]}, "updated")
    assert store.memory_use_stats("web:default", "owner")[item["id"]]["memory_version"] == edited["version"]


def test_overview_is_local_bounded_and_authorized(memory):
    store, engine = memory
    wanted = fact(store)
    fact(store, "其他人的私有咖啡讲座", user_id="other")
    result = engine.overview("web:default", "owner", "咖啡讲座", limit=1)
    assert len(result["memories"]) == 1 and result["memory_versions"] == {wanted["id"]: 1}
    store.upsert_scope({"id": "web:default", "enabled": False})
    assert engine.overview("web:default", "owner")["memories"] == []


def test_duplicate_proposal_needs_review_and_undo_restores_original_record(memory, monkeypatch):
    store, engine = memory
    # Windows can give adjacent writes identical clock values. Deliberately
    # make the second record the ID tie-break winner; review follows the
    # proposal's explicit keep_id rather than incidental insertion order.
    monkeypatch.setattr("qunyu.db.utc_now", lambda: "2026-01-01T00:00:00.000000Z")
    first, second = fact(store, id="z-created-first"), fact(store, id="a-created-second")
    suggestion = proposal(engine, "merge_duplicate")
    keep_id = suggestion["details"]["keep_id"]
    archived = next(item for item in (first, second) if item["id"] != keep_id)
    assert keep_id == second["id"]
    assert store.get_memory(first["id"])["status"] == store.get_memory(second["id"])["status"] == "active"
    assert engine.propose_organization("web:default", "owner")["created"] == 0
    reviewed = engine.review_organization(suggestion["id"], "web:default", "owner", "approve")
    assert reviewed["details"]["status"] == "approved"
    assert len(store.visible_memories("web:default", "owner")) == 1
    assert store.get_memory(keep_id)["version"] == 1
    assert store.get_memory(archived["id"])["merged_into"] == keep_id
    assert store.get_memory(archived["id"])["version"] == 2
    with pytest.raises(ValueError):
        engine.review_organization(suggestion["id"], "web:default", "owner", "approve")
    reverted = engine.review_organization(suggestion["id"], "web:default", "owner", "revert")
    assert reverted["details"]["status"] == "reverted"
    assert len(store.visible_memories("web:default", "owner")) == 2
    assert store.get_memory(archived["id"])["content"] == archived["content"]
    assert store.get_memory(archived["id"])["version"] == 3
    assert store.get_memory(keep_id)["version"] == 1
    assert "merged_into" not in store.get_memory(archived["id"])


def test_expired_archive_is_reversible_and_never_extends_validity(memory):
    store, engine = memory
    expired = fact(store, valid_to="2020-01-01T00:00:00Z")
    suggestion = proposal(engine, "archive_expired")
    assert store.get_memory(expired["id"])["status"] == "active"
    engine.review_organization(suggestion["id"], "web:default", "owner", "approve")
    assert store.get_memory(expired["id"])["status"] == "archived"
    engine.review_organization(suggestion["id"], "web:default", "owner", "revert")
    assert store.get_memory(expired["id"])["status"] == "active"
    assert not store.visible_memories("web:default", "owner")


@pytest.mark.parametrize("phase", ["pending", "approved"])
def test_edits_win_over_old_organization_and_old_preview_is_hidden(memory, phase):
    store, engine = memory
    first, second = fact(store), fact(store)
    suggestion = proposal(engine, "merge_duplicate")
    if phase == "approved":
        engine.review_organization(suggestion["id"], "web:default", "owner", "approve")
    store.update_memory(second["id"], {"content": "已经修正的新资料"})
    with pytest.raises(ValueError):
        engine.review_organization(suggestion["id"], "web:default", "owner", "revert" if phase == "approved" else "approve")
    shown = engine.list_organization("web:default", "owner")
    assert shown[0]["details"]["stale"] and "before" not in shown[0]["details"]


def test_delete_scrubs_organization_snapshots_and_blocks_undo(memory):
    store, engine = memory
    first, second = fact(store, "应当遗忘的内容"), fact(store, "应当遗忘的内容")
    suggestion = proposal(engine, "merge_duplicate")
    engine.review_organization(suggestion["id"], "web:default", "owner", "approve")
    store.delete_memory(first["id"])
    assert engine.list_organization("web:default", "owner") == []
    event = store._get("events", suggestion["id"])
    assert event["details"]["status"] == "invalidated"
    assert "before" not in event["details"]
    with pytest.raises(ValueError):
        engine.review_organization(suggestion["id"], "web:default", "owner", "revert")


def test_organization_respects_user_namespace_visibility_properties_and_consent(memory):
    store, engine = memory
    fact(store, "同样的称呼", user_id="a")
    fact(store, "同样的称呼", user_id="b")
    fact(store, "同样的称呼", property="preferred_name")
    fact(store, "同样的称呼", visibility="group")
    fact(store, "同样的称呼", shareable=True)
    fact(store, "同样的称呼", pinned=True)
    assert engine.propose_organization("web:default", "owner")["created"] == 0
    first, second = fact(store), fact(store)
    suggestion = proposal(engine, "merge_duplicate")
    with pytest.raises(PermissionError):
        engine.review_organization(suggestion["id"], "web:default", "other", "approve")
    with pytest.raises(PermissionError):
        engine.review_organization(suggestion["id"], "web:elsewhere", "owner", "approve")


def test_reject_organization_does_not_archive_or_recreate_same_suggestion(memory):
    store, engine = memory
    fact(store)
    fact(store)
    suggestion = proposal(engine, "merge_duplicate")
    engine.review_organization(suggestion["id"], "web:default", "owner", "reject")
    assert len(store.visible_memories("web:default", "owner")) == 2
    assert engine.propose_organization("web:default", "owner")["created"] == 0


def test_organization_is_atomic_on_write_failure(memory, monkeypatch):
    store, engine = memory
    records = [fact(store) for _ in range(3)]
    suggestion = proposal(engine, "merge_duplicate")
    original = store._save_memory
    writes = 0

    def fail_second(record):
        nonlocal writes
        writes += 1
        if writes == 2:
            raise RuntimeError("simulated storage failure")
        return original(record)

    monkeypatch.setattr(store, "_save_memory", fail_second)
    with pytest.raises(RuntimeError):
        engine.review_organization(suggestion["id"], "web:default", "owner", "approve")
    assert all(store.get_memory(item["id"])["version"] == 1 for item in records)
    assert store._get("events", suggestion["id"])["details"]["status"] == "pending"


@pytest.mark.parametrize("field,value", [("use_persona_knowledge", "false"), ("planner_daily_calls", True), ("planner_min_interval_ms", -1), ("planner_daily_tokens", 10000001), ("planner_max_age_ms", 999)])
def test_scope_overrides_reject_invalid_types_and_ranges(memory, field, value):
    store, engine = memory
    with pytest.raises(ValueError):
        store.upsert_scope({"id": "web:default", field: value})


def test_scope_overrides_allow_inheritance_and_platform_limit(memory):
    store, engine = memory
    scope = store.upsert_scope({"id": "web:default", "use_persona_knowledge": False, "planner_daily_calls": 0,
        "planner_min_interval_ms": None, "settings": {"output": {"platform_max_chars": 1800}}})
    assert scope["use_persona_knowledge"] is False and scope["planner_daily_calls"] == 0
    assert scope["planner_min_interval_ms"] is None and scope["settings"]["output"]["platform_max_chars"] == 1800
    with pytest.raises(ValueError):
        store.upsert_scope({"id": "web:default", "output_platform_max_chars": 10})


def test_optional_use_stats_failure_never_breaks_an_already_completed_reply(memory, monkeypatch):
    store, engine = memory
    item = fact(store)

    def disk_full(*args, **kwargs):
        raise OSError("simulated disk full")

    monkeypatch.setattr(store, "record_memory_use", disk_full)
    result = engine.mark_used("web:default", "owner", {item["id"]: 1}, "finished-reply")
    assert result == {"used": [], "count": 0, "error": "OSError"}


def test_many_history_dependencies_are_bounded_for_usage_only(memory):
    store, engine = memory
    records = [fact(store, f"记录{index}") for index in range(40)]
    result = engine.mark_used("web:default", "owner", {item["id"]: 1 for item in records}, "large-manifest")
    assert result["count"] == 32
    assert all(store.get_memory(item["id"])["version"] == 1 for item in records)
