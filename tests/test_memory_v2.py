import asyncio
from datetime import datetime, timedelta, timezone
import sqlite3
import json

import pytest

from qunyu.db import Store, TABLES
from shiori.memory import CHUNK_SIZE, MAX_SEMANTIC_CHUNKS, MemoryEngine, tokenize


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
def store(tmp_path):
    instance = Store(tmp_path / "memory-v2")
    yield instance
    instance.close()


def settings(semantic=False, model="embedding-test"):
    return {"memory": {"semantic_enabled": semantic, "embedding_model": model, "top_k": 8, "background_indexing": False},
            "model": {"base_url": "https://provider.invalid/v1", "api_key": "never-store-this-key"}}


def create(store, content, **fields):
    return store.create_memory({"scope_id": "web:default", "user_id": "owner", "content": content, **fields})


def test_chinese_word_and_unknown_phrase_channels_and_ascii_boundaries(store):
    terms = tokenize("喜欢星际微分咖啡 and Party")
    assert "喜欢" in terms and "际微" in terms and "party" in terms and "and" not in terms
    target = create(store, "喜欢星际微分咖啡")
    create(store, "party is fun")
    results = store.recall("web:default", "owner", "微分咖啡")
    assert results[0]["id"] == target["id"] and results[0]["retrieval"]["bm25"] > 0
    assert store.recall("web:default", "owner", "art") == []


@pytest.mark.anyio
async def test_inspection_explains_bm25_importance_person_and_time(store):
    engine = MemoryEngine(store, settings())
    create(store, "参加部署会议", kind="event", subject="Bob", visibility="group", event_at="2026-10-14T10:00:00+08:00")
    target = create(store, "参加部署会议", kind="event", subject="Alice", pinned=True,
                    event_at="2026-10-15T10:00:00+08:00")
    try:
        result = await engine.inspect("web:default", "owner", "Alice 2026-10-15 部署会议")
        assert result["items"][0]["id"] == target["id"]
        reason = result["items"][0]["retrieval"]
        assert reason["bm25"] > 0 and reason["person_boost"] > 0 and reason["time_boost"] > 0
        assert reason["reasons"] and result["mode"] == "lexical"
    finally:
        engine.close()


@pytest.mark.anyio
async def test_long_notes_return_relevant_chunks_with_original_identity_and_sources(store):
    source = store.add_message("web:default", "owner", "user", "资料来源")
    text = "普通说明。" * 1200 + "关键配置：星辉协议使用端口 9456，必须开启握手认证。" + "其他说明。" * 1200
    note = create(store, text, kind="note", source_ids=[source["id"]], filename="协议手册.md")
    engine = MemoryEngine(store, settings())
    try:
        results = await engine.recall("web:default", "owner", "星辉协议端口")
        assert results and all(item["memory_id"] == note["id"] for item in results)
        match = next(item for item in results if "9456" in item["excerpt"])
        assert len(match["excerpt"]) <= CHUNK_SIZE and match["chunk_index"] > 0
        assert match["content"] == match["excerpt"] and match["content_truncated"]
        assert match["retrieval"]["citation"]["source_ids"] == [source["id"]]
        assert store.get_memory(note["id"])["content"] == text
    finally:
        engine.close()


@pytest.mark.anyio
async def test_semantic_off_makes_no_provider_call_or_index_file(store):
    create(store, "我喜欢猫")

    async def forbidden(*args, **kwargs):
        raise AssertionError("default must never bill embeddings")

    engine = MemoryEngine(store, settings(), forbidden)
    try:
        results = await engine.recall("web:default", "owner", "猫")
        assert results and not engine.index_path.exists()
    finally:
        engine.close()


@pytest.mark.anyio
async def test_semantic_paraphrase_uses_only_bounded_chunks_and_cache(store):
    target = create(store, "每天喝一杯拿铁", kind="preference")
    create(store, "只吃蔬菜", kind="preference")
    calls = []

    async def embed(texts, *, scope_id):
        calls.append((list(texts), scope_id))
        return [[1.0, 0.0] if ("拿铁" in text or "最爱什么饮品" in text) else [0.0, 1.0] for text in texts]

    engine = MemoryEngine(store, settings(True), embed)
    try:
        first = await engine.inspect("web:default", "owner", "最爱什么饮品")
        assert first["items"][0]["id"] == target["id"] and first["mode"] == "hybrid"
        assert first["semantic"]["embedded_chunks"] == 2
        second = await engine.inspect("web:default", "owner", "最爱什么饮品")
        assert second["semantic"]["cache_hits"] == 2 and second["semantic"]["embedded_chunks"] == 0
        assert len(calls) == 2  # query + documents; identical query reuses its bounded cache
        assert second['semantic']['query_cache_hit'] is True
        assert all(scope == "web:default" for _, scope in calls)
        with sqlite3.connect(engine.index_path) as connection:
            cache_dump = " ".join(str(row) for row in connection.iterdump())
        assert "never-store-this-key" not in cache_dump and "每天喝一杯拿铁" not in cache_dump
    finally:
        engine.close()


@pytest.mark.anyio
async def test_long_document_is_never_fully_sent_to_embedding_provider(store):
    text = "普通文字。" * 16000 + "海盐校验规则 123" + "更多内容。" * 16000
    create(store, text, kind="note")
    calls = []

    async def embed(texts, *, scope_id):
        calls.append(texts)
        return [[1.0, 0.0] for _ in texts]

    engine = MemoryEngine(store, settings(True), embed)
    try:
        result = await engine.recall("web:default", "owner", "海盐校验规则")
        assert result
        assert max(len(batch) for batch in calls) <= MAX_SEMANTIC_CHUNKS
        assert all(len(text) < CHUNK_SIZE + 400 for batch in calls for text in batch)
        assert sum(len(text) for batch in calls for text in batch) < 12000
    finally:
        engine.close()


@pytest.mark.anyio
async def test_semantic_failure_or_bad_dimensions_degrade_without_exposing_remote_error(store):
    target = create(store, "喜欢咖啡")

    async def fail(texts, *, scope_id):
        raise RuntimeError("api_key=do-not-expose")

    engine = MemoryEngine(store, settings(True), fail)
    try:
        result = await engine.inspect("web:default", "owner", "咖啡")
        assert result["items"][0]["id"] == target["id"] and result["mode"] == "lexical"
        assert result["semantic"]["error"] == "RuntimeError" and "do-not-expose" not in str(result)
    finally:
        engine.close()


@pytest.mark.anyio
async def test_invalid_document_vectors_fall_back_without_caching(store):
    create(store, "配置说明")
    requests = 0

    async def embed(texts, *, scope_id):
        nonlocal requests
        requests += 1
        return [[1.0, 0.0]] if requests == 1 else [[float("nan"), 0.0] for _ in texts]

    engine = MemoryEngine(store, settings(True), embed)
    try:
        result = await engine.inspect("web:default", "owner", "配置")
        assert result["mode"] == "lexical" and result["items"]
        with sqlite3.connect(engine.index_path) as connection:
            assert connection.execute("SELECT COUNT(*) FROM vectors").fetchone()[0] == 0
    finally:
        engine.close()


@pytest.mark.anyio
async def test_cached_old_memory_can_be_semantically_found_outside_new_embedding_budget(store):
    target = create(store, "拿铁特别好喝", importance=0)

    async def embed(texts, *, scope_id):
        return [[1.0, 0.0] if ("拿铁" in text or "最爱什么饮品" in text) else [0.0, 1.0] for text in texts]

    engine = MemoryEngine(store, settings(True), embed)
    try:
        await engine.recall("web:default", "owner", "拿铁")
        for i in range(20):
            create(store, f"完全不同的工作记录{i}", importance=1)
        result = await engine.recall("web:default", "owner", "最爱什么饮品")
        assert result and result[0]["id"] == target["id"]
    finally:
        engine.close()


@pytest.mark.anyio
async def test_unauthorized_documents_are_never_embedded_or_recalled(store):
    create(store, "群内知识", scope_id="qq:group:1", visibility="group")
    create(store, "群内私人秘密", scope_id="qq:group:1", visibility="private")
    create(store, "另一群秘密", scope_id="qq:group:2", visibility="group")
    calls = []

    async def embed(texts, *, scope_id):
        calls.extend(texts)
        return [[1.0, 0.0] for _ in texts]

    engine = MemoryEngine(store, settings(True), embed)
    try:
        results = await engine.recall("qq:group:1", "owner", "知识")
        assert results and all(item["content"] == "群内知识" for item in results)
        assert not any("秘密" in text for text in calls)
        profile = engine.profile("qq:group:1", "owner")
        assert all("秘密" not in item["content"] for item in profile["facts"])
    finally:
        engine.close()


@pytest.mark.anyio
async def test_corrected_deleted_and_dependency_invalidated_vectors_do_not_resurrect(store):
    fact = create(store, "爱喝咖啡")
    derived = create(store, "咖啡派生摘要", kind="note", dependencies={fact["id"]: 1})

    async def embed(texts, *, scope_id):
        return [[1.0, 0.0] for _ in texts]

    engine = MemoryEngine(store, settings(True), embed)
    try:
        assert len(await engine.recall("web:default", "owner", "咖啡")) == 2
        store.update_memory(fact["id"], {"content": "爱喝奶茶"})
        results = await engine.recall("web:default", "owner", "咖啡")
        assert all(item["id"] != derived["id"] and "爱喝咖啡" not in item["content"] for item in results)
        store.delete_memory(fact["id"])
        assert await engine.recall("web:default", "owner", "咖啡") == []
        with sqlite3.connect(engine.index_path) as connection:
            assert connection.execute("SELECT COUNT(*) FROM vectors").fetchone()[0] == 0
    finally:
        engine.close()


@pytest.mark.anyio
async def test_edit_during_embedding_discards_inflight_old_text(store):
    fact = create(store, "旧事实")
    entered, release = asyncio.Event(), asyncio.Event()

    async def embed(texts, *, scope_id):
        entered.set()
        await release.wait()
        return [[1.0, 0.0] for _ in texts]

    engine = MemoryEngine(store, settings(True), embed)
    try:
        task = asyncio.create_task(engine.recall("web:default", "owner", "旧事实"))
        await entered.wait()
        store.update_memory(fact["id"], {"content": "新事实"})
        release.set()
        assert await task == []
    finally:
        engine.close()


@pytest.mark.anyio
async def test_clear_index_during_embedding_does_not_repopulate_old_generation(store):
    create(store, "代际测试")
    entered, release = asyncio.Event(), asyncio.Event()

    async def embed(texts, *, scope_id):
        entered.set()
        await release.wait()
        return [[1.0, 0.0] for _ in texts]

    engine = MemoryEngine(store, settings(True), embed)
    try:
        task = asyncio.create_task(engine.inspect("web:default", "owner", "测试"))
        await entered.wait()
        engine.clear_index()
        release.set()
        result = await task
        assert result["mode"] == "lexical" and result["items"]
        assert not engine.index_path.exists()
    finally:
        engine.close()


@pytest.mark.anyio
async def test_model_and_dimension_signatures_prevent_vector_space_mixing(store):
    create(store, "模型测试")
    config = settings(True)
    # Exercise an unannounced remote dimension change with caching disabled;
    # a short-lived cached query intentionally preserves its original space.
    config['memory']['query_cache_ttl_seconds'] = 0
    dimension, calls = 2, []

    async def embed(texts, *, scope_id):
        calls.append(texts)
        return [[1.0] + [0.0] * (dimension - 1) for _ in texts]

    engine = MemoryEngine(store, config, embed)
    try:
        await engine.recall("web:default", "owner", "测试")
        dimension = 3
        changed_dimension = await engine.inspect("web:default", "owner", "测试")
        assert changed_dimension["semantic"]["cache_hits"] == 0
        config["memory"]["embedding_model"] = "second-model"
        changed_model = await engine.inspect("web:default", "owner", "测试")
        assert changed_model["semantic"]["cache_hits"] == 0 and len(calls) == 6
    finally:
        engine.close()


def test_pending_review_conflicts_and_explicit_replacement_keep_new_fact_valid(store):
    old = create(store, "喜欢咖啡", kind="preference", subject="owner", property="饮料")
    source = store.add_message("web:default", "owner", "user", "我现在喜欢奶茶")
    engine = MemoryEngine(store, settings())
    candidate = engine.ingest_candidates([{"content": "喜欢奶茶", "kind": "preference", "property": "饮料",
                                           "dependencies": {old["id"]: 1}, "status": "active"}],
                                         scope_id="web:default", user_id="owner", source_ids=[source["id"]])[0]
    assert candidate["status"] == "pending" and candidate["dependencies"] == {}
    assert store.recall("web:default", "owner", "奶茶") == []
    assert engine.conflicts(candidate["id"])[0]["id"] == old["id"]
    with pytest.raises(ValueError, match="冲突"):
        engine.approve(candidate["id"])
    assert store.get_memory(old["id"])["status"] == "active"
    # Even an explicitly added dependency on the fact being replaced must not
    # immediately invalidate the newly approved fact.
    store.update_memory(candidate["id"], {"dependencies": {old["id"]: 1}})
    approved = engine.approve(candidate["id"], replace_conflicts=True)
    assert approved["dependencies"] == {} and approved["supersedes"] == [old["id"]]
    assert store.get_memory(old["id"])["status"] == "superseded"
    assert store.recall("web:default", "owner", "奶茶")[0]["id"] == approved["id"]
    assert store.recall("web:default", "owner", "咖啡") == []
    assert engine.approve(candidate["id"])["version"] == approved["version"]
    engine.close()


def test_candidate_authorization_rejects_forged_assistant_and_other_scope_sources(store):
    engine = MemoryEngine(store, settings())
    assistant = store.add_message("web:default", "owner", "assistant", "模型猜测")
    other = store.add_message("qq:private:alice", "alice", "user", "私聊事实")
    for source in (assistant, other):
        with pytest.raises(ValueError):
            engine.ingest_candidates([{"content": "事实"}], scope_id="web:default", user_id="owner", source_ids=[source["id"]])
    assert store.list_memories() == []
    with pytest.raises(ValueError, match="来源消息不存在"):
        engine.ingest_candidates([{"content": "事实"}], scope_id="web:default", user_id="owner", source_ids=["forged"])
    engine.close()


def test_legacy_explicit_self_fact_conflicts_with_new_canonical_property(store):
    old = create(store, "我住上海")
    assert old["property"] == "residence_city"
    # Simulate an actual v0.1 record persisted before property inference existed.
    raw = dict(old)
    raw.pop("property")
    with store._lock, store._conn:
        store._conn.execute("UPDATE memories SET data=? WHERE id=?", (json.dumps(raw), old["id"]))
    assert store.get_memory(old["id"])["property"] == "residence_city"
    source = store.add_message("web:default", "owner", "user", "我现在住杭州")
    candidate = store.ingest_memory_candidates([{"content": "我现在住杭州", "property": "居住地"}],
                    scope_id="web:default", user_id="owner", source_ids=[source["id"]])[0]
    assert candidate["property"] == "residence_city"
    assert store.memory_conflicts(candidate["id"])[0]["id"] == old["id"]
    approved = store.approve_memory(candidate["id"], replace_conflicts=True)
    assert approved["status"] == "active" and store.get_memory(old["id"])["status"] == "superseded"


@pytest.mark.parametrize("content, expected", [
    ("我目前居住在杭州", "residence_city"), ("我的生日是五月一日", "birthday"),
    ("希望被称呼为：小屿", "preferred_name"), ("我朋友住上海", ""),
    ("我喜欢喝奶茶", ""), ("我对花生过敏", ""), ("我住在哪里？", ""),
    ("我以前住上海", ""), ("我可能住上海", ""),
])
def test_property_inference_is_deliberately_narrow(store, content, expected):
    record = create(store, content)
    assert record.get("property", "") == expected


def test_candidate_idempotency_rejection_and_deleted_timeline(store):
    source = store.add_message("web:default", "owner", "user", "今天心情好")
    engine = MemoryEngine(store, settings())
    kwargs = {"scope_id": "web:default", "user_id": "owner", "source_ids": [source["id"]]}
    first = engine.ingest_candidates([{"content": "心情好"}], **kwargs)[0]
    assert engine.ingest_candidates([{"content": "心情好"}], **kwargs)[0]["id"] == first["id"]
    assert engine.reject(first["id"])["status"] == "rejected"
    assert engine.ingest_candidates([{"content": "心情好"}], **kwargs) == []
    store.delete_memory(first["id"])
    versions = store.list_memory_versions(first["id"])
    assert len(versions) == 1 and versions[0]["status"] == "deleted" and not versions[0]["content"]
    engine.close()


def test_blocked_sources_are_monotonic_across_metadata_edits(store):
    record = create(store, "旧事实", source_ids=["source-old"])
    corrected = store.update_memory(record["id"], {"content": "新事实", "source_ids": ["source-new"]})
    result = store.update_memory(record["id"], {"pinned": True, "blocked_source_ids": []})
    assert result["blocked_source_ids"] == corrected["blocked_source_ids"] == ["source-old"]


@pytest.mark.anyio
async def test_corrupt_disposable_vector_cache_degrades_and_rebuilds(store):
    create(store, "缓存测试")

    async def embed(texts, *, scope_id):
        return [[1.0, 0.0] for _ in texts]

    engine = MemoryEngine(store, settings(True), embed)
    try:
        engine.index_path.write_bytes(b"corrupt derived cache")
        first = await engine.inspect("web:default", "owner", "测试")
        assert first["mode"] == "lexical" and first["items"]
        second = await engine.inspect("web:default", "owner", "测试")
        assert second["mode"] == "hybrid" and second["items"]
    finally:
        engine.close()


def test_profile_contains_only_current_scoped_evidence_preferences_and_events(store):
    preference = create(store, "喜欢合作游戏", kind="preference", property="游戏", source_ids=["p1"])
    event = create(store, "一起打过游戏", kind="event", source_ids=["e1"])
    create(store, "别人喜欢恐怖游戏", user_id="other", visibility="group", kind="preference")
    profile = store.profile("web:default", "owner")
    assert profile["structured_preferences"][0]["memory_id"] == preference["id"]
    assert profile["recent_events"][0]["id"] == event["id"]
    assert {item["source_ids"][0] for item in profile["evidence"]} == {"p1", "e1"}


@pytest.mark.anyio
async def test_clear_index_and_legacy_backup_restore_keep_main_schema_compatible(store, tmp_path):
    original = create(store, "可恢复记忆")
    snapshot = store.backup(tmp_path / "v01.db")

    async def embed(texts, *, scope_id):
        return [[1.0, 0.0] for _ in texts]

    engine = MemoryEngine(store, settings(True), embed)
    try:
        await engine.recall("web:default", "owner", "记忆")
        assert engine.index_path.exists()
        store.delete_memory(original["id"])
        store.restore_from(snapshot)
        engine.clear_index()
        assert not engine.index_path.exists()
        assert (await engine.recall("web:default", "owner", "记忆"))[0]["id"] == original["id"]
        with sqlite3.connect(store.path) as connection:
            assert connection.execute("PRAGMA user_version").fetchone()[0] == 1
            assert {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")} == set(TABLES)
    finally:
        engine.close()
