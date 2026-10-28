import json

import pytest

from qunyu.db import Store
from shiori.memory import MemoryEngine


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
def memory(tmp_path):
    store = Store(tmp_path / "relations-v6")
    engine = MemoryEngine(store, {"memory": {"top_k": 8}})
    yield store, engine
    engine.close()
    store.close()


def fact(store, content, **fields):
    return store.create_memory({"content": content, "scope_id": "web:default", "user_id": "owner", **fields})


def relation(engine, left, right, **fields):
    return engine.propose_relation("web:default", "owner", {"from_id": left["id"], "to_id": right["id"], "label": "共同活动", **fields})


@pytest.mark.anyio
async def test_pending_edges_do_not_affect_retrieval_then_approved_path_has_versions(memory):
    store, engine = memory
    left = fact(store, "pianoclub 星辉钢琴社的联系人")
    right = fact(store, "星期六下午两点活动室碰面", kind="event")
    edge = relation(engine, left, right, relation_type="event_link")
    assert edge["status"] == "pending" and edge["shareable"] is False
    before = await engine.inspect("web:default", "owner", "pianoclub")
    assert not before["related"]
    approved = engine.approve_relation(edge["id"], "web:default", "owner")
    after = await engine.inspect("web:default", "owner", "pianoclub")
    linked = next(item for item in after["items"] if item["id"] == right["id"])
    assert linked["related"] and linked["path_ids"] == [left["id"], edge["id"], right["id"]]
    assert linked["memory_versions"] == {left["id"]: 1, right["id"]: 1, edge["id"]: approved["version"]}


@pytest.mark.anyio
async def test_graph_stops_after_two_hops_and_handles_cycles(memory):
    store, engine = memory
    facts = [fact(store, text) for text in ("pianoclub 星辉钢琴社", "咖啡讲座", "周六展览", "星期天晚饭")]
    for left, right in zip(facts, facts[1:]):
        edge = relation(engine, left, right)
        engine.approve_relation(edge["id"], "web:default", "owner")
    cycle = relation(engine, facts[2], facts[0])
    # An unapproved cycle must not shorten the third hop.
    assert cycle["status"] == "pending"
    found = await engine.inspect("web:default", "owner", "pianoclub")
    assert {item["id"] for item in found["related"]} == {facts[1]["id"], facts[2]["id"]}
    assert max(item["retrieval"]["hops"] for item in found["related"]) == 2
    assert facts[3]["id"] not in {item["id"] for item in found["items"]}


@pytest.mark.anyio
@pytest.mark.parametrize("change", ["edit", "delete", "archive", "disable"])
async def test_endpoint_revocation_invalidates_edges_and_descendants(memory, change):
    store, engine = memory
    left, right = fact(store, "星辉钢琴社"), fact(store, "咖啡讲座")
    edge = relation(engine, left, right)
    edge = engine.approve_relation(edge["id"], "web:default", "owner")
    derived = fact(store, "基于关系得出的记录", dependencies={edge["id"]: edge["version"]})
    if change == "edit":
        store.update_memory(left["id"], {"content": "联系人已更换"})
    elif change == "delete":
        store.delete_memory(left["id"])
    elif change == "archive":
        store.update_memory(left["id"], {"status": "archived"})
    else:
        store.upsert_scope({"id": "web:default", "enabled": False})
    current = {item["id"] for item in store.visible_memories("web:default", "owner")}
    assert edge["id"] not in current and derived["id"] not in current
    assert not (await engine.inspect("web:default", "owner", "星辉钢琴社"))["related"]


def test_private_and_other_scope_endpoints_cannot_be_joined_by_nickname(memory):
    store, engine = memory
    left = fact(store, "小明喜欢音乐", subject="小明")
    private_other = fact(store, "小明的私事", subject="小明", user_id="other")
    other_scope = fact(store, "小明喜欢绘画", subject="小明", scope_id="web:elsewhere")
    for right in (private_other, other_scope):
        with pytest.raises(ValueError):
            relation(engine, left, right, relation_type="person_relation")


def test_relation_identity_uses_origin_scope_and_user_ids(memory):
    store, engine = memory
    left, right = fact(store, "钢琴社", subject="小明"), fact(store, "周六讲座", kind="event", subject="小明")
    edge = relation(engine, left, right)
    assert edge["endpoint_identities"] == {left["id"]: ["web:default", "owner"], right["id"]: ["web:default", "owner"]}
    with pytest.raises(PermissionError):
        engine.approve_relation(edge["id"], "web:default", "other")


def test_source_changes_block_approval_and_rejected_candidate_stays_rejected(memory):
    store, engine = memory
    left, right = fact(store, "星辉钢琴社"), fact(store, "咖啡讲座")
    source = store.add_message("web:default", "owner", "user", "两个活动有关")
    edge = relation(engine, left, right, source_ids=[source["id"]])
    rejected = engine.reject_relation(edge["id"], "web:default", "owner")
    assert relation(engine, left, right, source_ids=[source["id"]])["id"] == rejected["id"]
    with pytest.raises(ValueError):
        engine.approve_relation(edge["id"], "web:default", "owner")
    source = store.add_message("web:default", "owner", "user", "另一个新的关联来源")
    second = relation(engine, left, right, relation_type="event_link", source_ids=[source["id"]])
    with store._lock, store._conn:
        store._conn.execute("DELETE FROM messages WHERE id=?", (source["id"],))
    with pytest.raises(KeyError):
        engine.approve_relation(second["id"], "web:default", "owner")


@pytest.mark.anyio
async def test_model_analysis_only_creates_pending_candidates_and_rechecks_race(memory):
    store, engine = memory
    left, right = fact(store, "星辉钢琴社"), fact(store, "咖啡讲座")

    class Model:
        async def complete(self, messages, **kwargs):
            assert kwargs["purpose"] == "memory_extract" and kwargs["before_send"]()
            return {"content": json.dumps([{"from_id": left["id"], "to_id": right["id"], "label": "资料可能相关"}])}

    result = await engine.analyze_relations("web:default", "owner", provider=Model())
    assert result["created"] == 1 and result["candidates"][0]["status"] == "pending"
    assert not (await engine.inspect("web:default", "owner", "星辉钢琴社"))["related"]

    class RacingModel:
        async def complete(self, messages, **kwargs):
            store.delete_memory(left["id"])
            assert not kwargs["before_send"]()
            return {"content": json.dumps([{"from_id": left["id"], "to_id": right["id"], "label": "过期资料"}])}

    result = await engine.analyze_relations("web:default", "owner", provider=RacingModel())
    assert result["created"] == 0


@pytest.mark.anyio
async def test_local_analysis_uses_common_message_only_and_waits_for_review(memory):
    store, engine = memory
    source = store.add_message("web:default", "owner", "user", "活动中认识了社员")
    fact(store, "社员资料", source_ids=[source["id"]])
    fact(store, "音乐活动", kind="event", source_ids=[source["id"]])
    fact(store, "同名人物不应自动绑定", subject="社员")
    result = await engine.analyze_relations("web:default", "owner")
    assert result["created"] == 1 and result["candidates"][0]["status"] == "pending"


def test_relations_and_versions_survive_schema_one_backup(memory, tmp_path):
    store, engine = memory
    left, right = fact(store, "钢琴社"), fact(store, "咖啡讲座")
    edge = relation(engine, left, right)
    engine.approve_relation(edge["id"], "web:default", "owner")
    backup = store.backup(tmp_path / "backup.db")
    restored = Store(tmp_path / "restored")
    try:
        restored.restore_from(backup)
        assert restored._conn.execute("PRAGMA user_version").fetchone()[0] == 1
        assert restored.get_memory(edge["id"])["status"] == "active"
        assert edge["id"] in {item["id"] for item in restored.visible_memories("web:default", "owner")}
    finally:
        restored.close()


@pytest.mark.anyio
async def test_shared_graph_requires_each_record_consent_and_source_membership(memory):
    store, engine = memory
    origin, target = "qq:group:1", "qq:group:2"
    for scope in (origin, target):
        store.upsert_scope({"id": scope, "memory_space": "music"})
    left = fact(store, "pianoclub 钢琴社", scope_id=origin, visibility="group", shareable=True)
    right = fact(store, "星期六讨论创作", scope_id=origin, visibility="group", shareable=True)
    edge = engine.propose_relation(origin, "owner", {"from_id": left["id"], "to_id": right["id"], "label": "共同创作"})
    edge = engine.approve_relation(edge["id"], origin, "owner")
    assert not (await engine.inspect(target, "owner", "pianoclub"))["related"]
    edge = store.update_memory(edge["id"], {"shareable": True})
    linked = (await engine.inspect(target, "owner", "pianoclub"))["related"]
    assert linked and linked[0]["scope_id"] == origin
    store.update_memory(right["id"], {"shareable": False})
    assert not (await engine.inspect(target, "owner", "pianoclub"))["related"]
    store.upsert_scope({"id": origin, "memory_space": ""})
    assert not (await engine.inspect(target, "owner", "pianoclub"))["items"]


@pytest.mark.anyio
async def test_forgetting_source_invalidates_relation_even_when_not_an_endpoint(memory):
    store, engine = memory
    source = store.add_message("web:default", "owner", "user", "曾经提供过的关联资料")
    left, right = fact(store, "pianoclub 钢琴社"), fact(store, "创作讲座")
    forgotten = fact(store, "用户要求遗忘的另一项资料", source_ids=[source["id"]])
    edge = relation(engine, left, right, source_ids=[source["id"]])
    engine.approve_relation(edge["id"], "web:default", "owner")
    assert (await engine.inspect("web:default", "owner", "pianoclub"))["related"]
    store.delete_memory(forgotten["id"])
    assert not (await engine.inspect("web:default", "owner", "pianoclub"))["related"]
    assert engine.list_relations("web:default", "owner") == []


def test_ordinary_store_recall_never_returns_edge_as_an_independent_fact(memory):
    store, engine = memory
    left, right = fact(store, "钢琴社"), fact(store, "创作讲座")
    edge = relation(engine, left, right, label="xylophone")
    engine.approve_relation(edge["id"], "web:default", "owner")
    assert store.recall("web:default", "owner", "xylophone") == []
