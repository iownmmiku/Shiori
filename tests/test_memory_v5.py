import asyncio
import copy
import json

import httpx
import pytest

from shiori.context import ContextEngine
from qunyu.db import Store
from shiori.memory import MemoryEngine
from shiori.provider import Provider
from shiori.settings import DEFAULTS


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
def store(tmp_path):
    instance = Store(tmp_path / "memory-v5")
    yield instance
    instance.close()


def shared(store):
    for scope in ("qq:group:1", "qq:group:2"):
        store.upsert_scope({"id": scope, "memory_space": "音乐同好"})
    return store.create_memory({"scope_id": "qq:group:1", "user_id": "alice", "content": "喜欢演奏电子音乐",
                                "visibility": "group", "shareable": True})


def config():
    return {"model": {"base_url": "https://provider.invalid/v1", "model": "chat", "api_key": "test-secret"},
            "memory": {"semantic_enabled": True, "embedding_model": "embed", "top_k": 8},
            "onebot": {"enabled": True, "adapter": "napcat", "allowed_groups": ["1", "2"]},
            "context": {"enabled": True, "trigger_messages": 2, "keep_recent": 1, "max_chars": 18000}}


def visible(store, scope="qq:group:2"):
    return {record["id"] for record in store.visible_memories(scope, "alice")}


def test_shared_facts_require_explicit_record_and_space_consent(store):
    fact = shared(store)
    assert fact["id"] in visible(store)
    assert fact["scope_id"] == store.get_memory(fact["id"])["scope_id"] == "qq:group:1"
    assert store.allowed_memory_sources("qq:group:2", "alice", fact["id"]) == []
    unshared = store.create_memory({"scope_id": "qq:group:1", "content": "没有勾选共享", "visibility": "group"})
    private = store.create_memory({"scope_id": "qq:group:1", "content": "私人秘密", "visibility": "private", "shareable": True})
    digest = store.create_memory({"scope_id": "qq:group:1", "content": "对话原文摘要", "kind": "event",
                                  "property": "conversation_digest", "visibility": "group", "shareable": True})
    pending = store.create_memory({"scope_id": "qq:group:1", "content": "未审核", "status": "pending", "shareable": True})
    assert not {unshared["id"], private["id"], digest["id"], pending["id"]} & visible(store)
    store.upsert_scope({"id": "qq:group:2", "memory_space": ""})
    assert fact["id"] not in visible(store)
    assert fact["id"] in visible(store, "qq:group:1")


@pytest.mark.parametrize("change", ["source_exit", "target_exit", "source_disabled", "target_disabled", "consent", "delete", "private"])
def test_sharing_revocation_invalidates_derived_dependencies_immediately(store, change):
    fact = shared(store)
    derived = store.create_memory({"scope_id": "qq:group:2", "content": "对共同音乐偏好的归纳", "kind": "note",
                                   "dependencies": {fact["id"]: 1}})
    assert derived["id"] in visible(store)
    if change == "source_exit":
        store.upsert_scope({"id": "qq:group:1", "memory_space": ""})
    elif change == "target_exit":
        store.upsert_scope({"id": "qq:group:2", "memory_space": "别的空间"})
    elif change == "source_disabled":
        store.upsert_scope({"id": "qq:group:1", "enabled": False})
    elif change == "target_disabled":
        store.upsert_scope({"id": "qq:group:2", "enabled": False})
    elif change == "consent":
        store.update_memory(fact["id"], {"shareable": False})
    elif change == "delete":
        store.delete_memory(fact["id"])
    else:
        store.update_memory(fact["id"], {"visibility": "private"})
    assert fact["id"] not in visible(store) and derived["id"] not in visible(store)


def test_shared_fact_cannot_hide_private_or_unshared_dependency(store):
    private = store.create_memory({"scope_id": "qq:group:1", "content": "私人依据", "visibility": "private"})
    fact = shared(store)
    store.update_memory(fact["id"], {"dependencies": {private["id"]: private["version"]}})
    assert fact["id"] not in visible(store)


def test_automatic_candidate_cannot_grant_its_own_sharing_consent(store):
    shared(store)
    source = store.add_message("qq:group:1", "alice", "user", "喜欢爵士乐")
    candidate = store.ingest_memory_candidates([{"content": "喜欢爵士乐", "shareable": True}],
        scope_id="qq:group:1", user_id="alice", source_ids=[source["id"]])[0]
    assert candidate["shareable"] is False
    store.approve_memory(candidate["id"])
    assert candidate["id"] not in visible(store)


def test_sharing_never_copies_raw_history_language_or_tasks(store):
    shared(store)
    store.add_message("qq:group:1", "alice", "user", "只有一群能读的原话")
    store.create_jargon({"scope_id": "qq:group:1", "term": "茶会", "meaning": "一群的专用意义", "status": "approved"})
    store.create_jargon({"scope_id": "qq:group:1", "term": "好耶", "meaning": "庆祝", "status": "approved", "entry_type": "expression"})
    store.create_task({"scope_id": "qq:group:1", "title": "只归一群的事"})
    assert store.list_messages("qq:group:2") == []
    assert store.match_jargon("qq:group:2", "茶会") == []
    assert store.list_jargon("qq:group:2") == [] and store.list_tasks("qq:group:2") == []


@pytest.mark.anyio
@pytest.mark.parametrize("change", ["delete", "edit", "private", "consent", "source_exit", "target_exit", "source_disabled",
                                         "paused", "revoke_qq", "model", "key", "semantic_off"])
async def test_no_new_document_send_after_query_await_revocation(store, change):
    fact = shared(store)
    cfg = config()
    entered, release, calls = asyncio.Event(), asyncio.Event(), []

    async def embed(texts, *, scope_id):
        calls.append(list(texts))
        if len(calls) == 1:
            entered.set()
            await release.wait()
        return [[1., 0.] for _ in texts]

    engine = MemoryEngine(store, cfg, embed)
    try:
        task = asyncio.create_task(engine.recall("qq:group:2", "alice", "音乐"))
        await entered.wait()
        if change == "delete":
            store.delete_memory(fact["id"])
        elif change == "edit":
            store.update_memory(fact["id"], {"content": "现在喜欢绘画"})
        elif change == "private":
            store.update_memory(fact["id"], {"visibility": "private"})
        elif change == "consent":
            store.update_memory(fact["id"], {"shareable": False})
        elif change == "source_exit":
            store.upsert_scope({"id": "qq:group:1", "memory_space": ""})
        elif change == "target_exit":
            store.upsert_scope({"id": "qq:group:2", "memory_space": ""})
        elif change == "source_disabled":
            store.upsert_scope({"id": "qq:group:1", "enabled": False})
        elif change == "paused":
            cfg["paused"] = True
        elif change == "revoke_qq":
            cfg["onebot"]["allowed_groups"] = ["2"]
        elif change == "model":
            cfg["memory"]["embedding_model"] = "another-embedding"
        elif change == "key":
            cfg["model"]["api_key"] = "another-account"
        else:
            cfg["memory"]["semantic_enabled"] = False
        release.set()
        result = await task
        assert calls == [["音乐"]]
        if change not in {"model", "key", "semantic_off"}:
            assert result == []
    finally:
        engine.close()


@pytest.mark.anyio
async def test_provider_lock_guard_cancels_body_when_deleted_while_queued(store):
    fact = shared(store)
    entered, release, sent = asyncio.Event(), asyncio.Event(), []

    async def embed(texts, *, scope_id, before_send):
        if texts != ["音乐"]:
            entered.set()
            await release.wait()  # models a provider budget/concurrency lock
        if not before_send():
            raise PermissionError("revoked before actual HTTP")
        sent.append(list(texts))
        return [[1., 0.] for _ in texts]

    cfg = config()
    cfg['memory']['background_indexing'] = False  # Exercise the explicit synchronous build mode.
    engine = MemoryEngine(store, cfg, embed)
    try:
        task = asyncio.create_task(engine.recall("qq:group:2", "alice", "音乐"))
        await entered.wait()
        store.delete_memory(fact["id"])
        release.set()
        assert await task == [] and sent == [["音乐"]]
    finally:
        engine.close()


@pytest.mark.anyio
async def test_real_provider_rechecks_memory_guard_after_its_request_lock(store, monkeypatch):
    fact = shared(store)
    cfg = copy.deepcopy(DEFAULTS)
    cfg.update(config())
    entered, release, sent = asyncio.Event(), asyncio.Event(), []

    class Settings:
        def get(self):
            return copy.deepcopy(cfg)

    class QueuedLock:
        count = 0

        async def __aenter__(self):
            self.count += 1
            if self.count == 2:
                entered.set()
                await release.wait()

        async def __aexit__(self, *args):
            return False

    def handler(request):
        texts = json.loads(request.content)["input"]
        sent.append(texts)
        return httpx.Response(200, json={"data": [{"index": i, "embedding": [1., 0.]} for i in range(len(texts))]})

    original = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda *a, **kw: original(*a, transport=httpx.MockTransport(handler), **kw))
    provider = Provider(Settings(), store)
    provider._call_lock = QueuedLock()
    engine = MemoryEngine(store, Settings(), provider.embed)
    try:
        task = asyncio.create_task(engine.recall("qq:group:2", "alice", "音乐"))
        await entered.wait()
        store.delete_memory(fact["id"])
        release.set()
        assert await task == [] and sent == [["音乐"]]
    finally:
        engine.close()


@pytest.mark.anyio
@pytest.mark.parametrize("change", ["source_exit", "revoke_qq"])
async def test_context_depends_on_shared_fact_but_never_crosses_raw_history(store, change):
    fact = shared(store)
    secret = store.add_message("qq:group:1", "alice", "user", "一群原话：绝不作为二群聊天来源")
    store.update_memory(fact["id"], {"source_ids": [secret["id"]]})
    fact = store.get_memory(fact["id"])
    store.upsert_scope({"id": "qq:group:2", "context_compress": True})
    history = []
    for i in range(4):
        history.append(store.add_message("qq:group:2", "alice", "user", f"二群本轮话题 {i}"))
        history.append(store.add_message("qq:group:2", "alice", "assistant", f"共享资料提到电子音乐 {i}",
                                         memory_versions={fact["id"]: fact["version"]}, source_ids=[history[-1]["id"]]))

    class Provider:
        async def complete(self, messages, **kwargs):
            assert secret["content"] not in json.dumps(messages, ensure_ascii=False)
            return {"content": "【话题】本群讨论音乐，共享事实提到电子音乐。"}

    cfg = config()
    engine = ContextEngine(store, cfg, Provider())
    result = await engine.compact("qq:group:2", "alice", history)
    summary = result["summary"]
    assert summary and summary["dependencies"][fact["id"]] == fact["version"]
    assert secret["id"] not in summary["source_ids"]
    assert all(store.get_message(source)["scope_id"] == "qq:group:2" for source in summary["source_ids"])
    if change == "source_exit":
        store.upsert_scope({"id": "qq:group:1", "memory_space": ""})
        assert summary["id"] not in visible(store)
    else:
        cfg["onebot"]["allowed_groups"] = ["2"]
    assert engine._latest_summary("qq:group:2", "alice") is None


@pytest.mark.anyio
async def test_revoked_shared_origin_invalidates_local_derived_text_before_embedding(store):
    fact = shared(store)
    store.create_memory({"scope_id": "qq:group:2", "content": "共享音乐资料的本群推导", "kind": "note",
                         "dependencies": {fact["id"]: 1}})
    cfg, calls = config(), []
    cfg["onebot"]["allowed_groups"] = ["2"]

    async def embed(texts, **kwargs):
        calls.append(texts)
        return [[1., 0.] for _ in texts]

    engine = MemoryEngine(store, cfg, embed)
    try:
        assert await engine.recall("qq:group:2", "alice", "音乐") == []
        assert calls == []
    finally:
        engine.close()


LEGACY_PERSONAS = [
    {"id": "default", "name": "温柔陪伴", "description": "认真听你说话的日常伙伴", "system_prompt": "你是群屿，一个温暖自然的聊天伙伴。认真理解上下文，简洁真诚，不编造记忆。", "color": "#52735b"},
    {"id": "quiet", "name": "务实助手", "description": "安静可靠，帮助整理与完成任务", "system_prompt": "你是群屿的务实助手。表达简明，关注事实、任务和结果，不确定时说明。", "color": "#607484"},
    {"id": "lively", "name": "群聊搭子", "description": "轻松开朗，懂得适时接话", "system_prompt": "你是群屿的群聊伙伴。自然开朗，适时接话，尊重每个人，不强行玩梗或刷屏。", "color": "#bd9055"},
]


def legacy_seed(store):
    with store._lock, store._conn:
        for persona in LEGACY_PERSONAS:
            store._write("personas", copy.deepcopy(persona), ())
        store._conn.execute("DELETE FROM events WHERE kind='shiori_persona_seed_v1'")
    store.upsert_scope({"id": "qq:group:1", "persona_id": "quiet"})


def test_old_templates_migrate_once_and_rebind_references(store):
    legacy_seed(store)
    store._seed()
    assert [record["id"] for record in store.list_personas()] == ["default"]
    assert store.get_persona("default")["name"] == "初音未来"
    assert store.get_scope("qq:group:1")["persona_id"] == "default"
    store._seed()
    assert len([e for e in store.list_events() if e["kind"] == "shiori_persona_seed_v1"]) == 1


def test_edited_and_custom_personas_are_preserved_and_deleted_do_not_reappear(store):
    legacy_seed(store)
    store.save_persona({"id": "default", "system_prompt": "用户写的专属人格提示词"})
    store.save_persona({"id": "quiet", "description": "用户改写的助手"})
    custom = store.save_persona({"id": "custom", "name": "用户角色", "system_prompt": "用户自建"})
    store._seed()
    assert {p["id"] for p in store.list_personas()} == {"default", "quiet", "custom"}
    assert store.get_persona("default")["system_prompt"] == "用户写的专属人格提示词"
    assert store.get_scope("qq:group:1")["persona_id"] == "quiet"
    store.delete_persona("quiet")
    reopened = Store(store.data_dir)
    try:
        assert {p["id"] for p in reopened.list_personas()} == {"default", custom["id"]}
    finally:
        reopened.close()


def test_schema1_backup_without_new_fields_restores_isolated_and_migrates_persona(store, tmp_path):
    legacy_seed(store)
    fact = store.create_memory({"scope_id": "qq:group:1", "content": "旧版事实", "visibility": "group"})
    # Serialize exactly as an old release did, before optional JSON extensions.
    with store._lock, store._conn:
        old = store.get_memory(fact["id"])
        old.pop("shareable", None)
        store._write("memories", old, ("scope_id", "user_id", "kind", "status", "visibility", "valid_from", "valid_to", "version", "created_at", "updated_at"))
        for scope in store.list_scopes():
            scope.pop("memory_space", None)
            store._write("scopes", scope, ())
    backup = store.backup(tmp_path / "old-schema1.db")
    store.restore_from(backup)
    assert store._conn.execute("PRAGMA user_version").fetchone()[0] == 1
    assert store.get_persona("default")["name"] == "初音未来"
    assert not store.get_memory(fact["id"]).get("shareable", False)
    store.upsert_scope({"id": "qq:group:1", "memory_space": "shared"})
    store.upsert_scope({"id": "qq:group:2", "memory_space": "shared"})
    assert fact["id"] not in visible(store)
