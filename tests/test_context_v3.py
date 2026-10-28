import asyncio
import json

import pytest

from shiori.context import ContextEngine, MAX_INPUT_CHARS
from qunyu.db import Store
from shiori.diagnostics import build_diagnostics


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
def store(tmp_path):
    instance = Store(tmp_path / "context")
    yield instance
    instance.close()


def config():
    return {"model": {"base_url": "https://model.invalid/v1", "model": "test", "api_key": "secret-model-key"},
            "context": {"enabled": True, "trigger_messages": 6, "max_chars": 6000, "keep_recent": 2},
            "budget": {"daily_tokens": 100, "daily_cost": 1}, "memory": {}, "onebot": {"enabled": False}}


def history(store, n=10, scope="web:default", user="owner", **metadata):
    return [store.add_message(scope, user if i % 2 == 0 else "assistant", "user" if i % 2 == 0 else "assistant",
                              f"第{i}条：周末一起做饭，约好先讨论菜单。", **metadata) for i in range(n)]


class Provider:
    def __init__(self, callback=None):
        self.calls = []
        self.callback = callback

    async def complete(self, messages, **kwargs):
        self.calls.append((messages, kwargs))
        if self.callback:
            result = self.callback()
            if asyncio.iscoroutine(result):
                await result
        return {"content": "【人物与归属】owner 提议做饭。\n【话题与共同经历】讨论菜单。\n【明确约定】周末做饭。\n【未完成事项】菜单尚待确认。"}


@pytest.mark.anyio
async def test_automatic_compression_requires_both_opt_ins_and_threshold(store):
    cfg, provider = config(), Provider()
    engine = ContextEngine(store, cfg, provider)
    messages = history(store)
    result = await engine.build("web:default", "owner", messages)
    assert result["reason"] == "disabled" and result["messages"] == messages and not provider.calls
    store.upsert_scope({"id": "web:default", "context_compress": True})
    cfg["context"]["enabled"] = False
    assert (await engine.build("web:default", "owner", messages))["reason"] == "disabled"
    cfg["context"]["enabled"] = True
    assert not (await engine.build("web:default", "owner", messages[-4:]))["compressed"]
    assert not provider.calls


@pytest.mark.anyio
async def test_digest_preserves_recent_rounds_sources_and_reuses_without_billing(store):
    store.upsert_scope({"id": "web:default", "context_compress": True})
    messages, provider = history(store), Provider()
    engine = ContextEngine(store, config(), provider)
    first = await engine.build("web:default", "owner", messages)
    assert first["compressed"] and first["messages"] == messages[-2:]
    summary = first["summary"]
    assert summary["kind"] == "event" and summary["property"] == "conversation_digest" and summary["derived"]
    assert summary["status"] == "active" and set(summary["source_ids"]) == {record["id"] for record in messages[:-2]}
    assert set(first["source_ids"]) == {record["id"] for record in messages}
    assert first["memory_versions"][summary["id"]] == 1
    assert provider.calls[0][1]["purpose"] == "context_digest"
    assert "未完成事项" in provider.calls[0][0][0]["content"]
    second = await engine.build("web:default", "owner", messages)
    assert second["summary"]["id"] == summary["id"] and len(provider.calls) == 1
    assert not store.profile("web:default", "owner")["facts"]


@pytest.mark.anyio
async def test_complete_latest_turn_is_preserved_and_input_is_bounded(store):
    store.upsert_scope({"id": "web:default", "context_compress": True})
    cfg, provider = config(), Provider()
    cfg["context"].update(max_chars=1700, keep_recent=1)
    messages = history(store, 20)
    result = await ContextEngine(store, cfg, provider).build("web:default", "owner", messages)
    assert result["compressed"]
    assert messages[-2:] == result["messages"][-2:]
    assert result["messages"][0]["role"] == "user"
    assert sum(len(message["content"]) for message in provider.calls[0][0]) <= 1700
    assert len(result["messages"]) > 2  # unsummarized messages remain, never silently dropped


@pytest.mark.anyio
async def test_huge_single_round_is_not_silently_truncated(store):
    store.upsert_scope({"id": "web:default", "context_compress": True})
    messages = [store.add_message("web:default", "owner", "user", "长" * (MAX_INPUT_CHARS + 1))] + history(store, 4)
    cfg, provider = config(), Provider()
    cfg["context"]["trigger_messages"] = 2
    result = await ContextEngine(store, cfg, provider).build("web:default", "owner", messages)
    assert result["messages"] == messages and not result["compressed"] and not provider.calls


@pytest.mark.anyio
async def test_manual_accepts_filtered_history_and_honest_no_model_skip(store):
    messages, cfg, provider = history(store), config(), Provider()
    cfg["context"]["enabled"] = False
    result = await ContextEngine(store, cfg, provider).compact("web:default", "owner", history=messages[-6:])
    assert result["status"] == "compressed" and result["summary"]["manual_digest"]
    assert set(result["summary"]["source_ids"]) <= {record["id"] for record in messages[-6:]}
    cfg["model"]["model"] = ""
    result = await ContextEngine(store, cfg, None).compact("web:default", "owner", history=messages)
    assert result["status"] == "skipped" and result["reason"] == "model_unconfigured"


@pytest.mark.anyio
async def test_manifest_carries_memory_and_jargon_and_invalidates_digest_on_correction(store):
    store.upsert_scope({"id": "web:default", "context_compress": True})
    fact = store.create_memory({"content": "我住上海"})
    word = store.create_jargon({"term": "开席", "meaning": "开始做饭", "status": "approved"})
    messages = history(store, memory_versions={fact["id"]: 1}, jargon_versions={word["id"]: 1})
    engine = ContextEngine(store, config(), Provider())
    result = await engine.build("web:default", "owner", messages)
    summary = result["summary"]
    assert summary["dependencies"] == {fact["id"]: 1} and summary["jargon_dependencies"] == {word["id"]: 1}
    store.update_memory(fact["id"], {"content": "我住杭州"})
    assert summary["id"] not in {record["id"] for record in store.visible_memories("web:default", "owner")}
    assert not (await engine.build("web:default", "owner", messages))["compressed"]
    assert store.get_memory(summary["id"])["content"]  # retained for admin inspection, excluded from recall


@pytest.mark.anyio
async def test_blocked_source_and_deleted_original_invalidate_even_without_fact_dependencies(store):
    store.upsert_scope({"id": "web:default", "context_compress": True})
    messages = history(store)
    summary = (await ContextEngine(store, config(), Provider()).build("web:default", "owner", messages))["summary"]
    forgotten = store.create_memory({"content": "不再保存这个来源", "source_ids": [messages[0]["id"]]})
    store.delete_memory(forgotten["id"])
    assert summary["id"] not in {record["id"] for record in store.visible_memories("web:default", "owner")}
    store.clear_messages("web:default")
    assert summary["id"] not in {record["id"] for record in store.recall("web:default", "owner", "菜单")}


@pytest.mark.anyio
@pytest.mark.parametrize("mutation", ["clear", "correct", "pause", "disable", "opt_out"])
async def test_inflight_mutations_cannot_save_stale_digest(store, mutation):
    store.upsert_scope({"id": "web:default", "context_compress": True})
    cfg = config()
    fact = store.create_memory({"content": "我住上海"})
    messages = history(store, memory_versions={fact["id"]: 1})

    def change():
        if mutation == "clear":
            store.clear_messages("web:default")
        elif mutation == "correct":
            store.update_memory(fact["id"], {"content": "我住杭州"})
        elif mutation == "pause":
            cfg["paused"] = True
        elif mutation == "disable":
            store.upsert_scope({"id": "web:default", "enabled": False})
        else:
            cfg["context"]["enabled"] = False

    result = await ContextEngine(store, cfg, Provider(change)).build("web:default", "owner", messages)
    assert not result["compressed"]
    assert not any(record.get("property") == "conversation_digest" for record in store.list_memories())
    if mutation in {"clear", "correct", "pause", "disable"}:
        assert result["messages"] == []
    else:
        assert result["messages"] == messages


@pytest.mark.anyio
async def test_new_messages_during_generation_are_not_lost(store, monkeypatch):
    monkeypatch.setattr("qunyu.db.utc_now", lambda: "2026-10-15T12:00:00.000000Z")
    store.upsert_scope({"id": "web:default", "context_compress": True})
    messages, added = history(store), []

    def arrive():
        added.append(store.add_message("web:default", "owner", "user", "改成周日，周六没空"))

    result = await ContextEngine(store, config(), Provider(arrive)).build("web:default", "owner", messages)
    assert result["messages"][-1] == added[0]
    assert added[0]["id"] not in result["summary"]["source_ids"]
    assert added[0]["id"] in result["source_ids"]


@pytest.mark.anyio
async def test_scope_private_and_qq_whitelist_isolation(store):
    cfg, provider = config(), Provider()
    cfg["onebot"] = {"enabled": True, "adapter": "napcat", "allowed_groups": ["100"], "owner_ids": ["owner"]}
    store.upsert_scope({"id": "qq:group:100", "context_compress": True})
    private = store.create_memory({"scope_id": "qq:group:100", "user_id": "owner", "visibility": "private", "content": "个人秘密"})
    private_messages = history(store, scope="qq:group:100", memory_versions={private["id"]: 1})
    other = history(store, scope="qq:group:200")
    engine = ContextEngine(store, cfg, provider)
    result = await engine.build("qq:group:100", "owner", private_messages + other)
    assert result["messages"] == [] and not provider.calls
    public = history(store, scope="qq:group:100")
    result = await engine.build("qq:group:100", "owner", public)
    assert result["summary"]["visibility"] == "group"
    assert result["summary"]["id"] in {record["id"] for record in store.visible_memories("qq:group:100", "someone_else")}
    assert (await engine.build("qq:group:200", "owner", other))["reason"] == "not_allowed"


@pytest.mark.anyio
async def test_expression_dependencies_are_independent_of_dictionary_switch(store):
    store.upsert_scope({"id": "web:default", "context_compress": True, "dictionary_understand": False, "expression_use": True})
    expression = store.create_jargon({"term": "好耶", "meaning": "轻快肯定句", "entry_type": "expression", "use_allowed": True, "status": "approved"})
    messages = history(store, jargon_versions={expression["id"]: 1})
    result = await ContextEngine(store, config(), Provider()).build("web:default", "owner", messages)
    assert result["compressed"]
    store.upsert_scope({"id": "web:default", "expression_use": False})
    assert result["summary"]["id"] not in {record["id"] for record in store.visible_memories("web:default", "owner")}


@pytest.mark.anyio
async def test_incremental_digest_dependencies_remain_active_and_do_not_self_invalidate(store):
    store.upsert_scope({"id": "web:default", "context_compress": True})
    provider, engine = Provider(), ContextEngine(store, config(), None)
    engine.provider = provider
    messages = history(store)
    first = (await engine.build("web:default", "owner", messages))["summary"]
    more = history(store, 10)
    result = await engine.build("web:default", "owner", messages + more)
    assert result["compressed"] and result["summary"]["id"] != first["id"]
    assert result["summary"]["dependencies"][first["id"]] == first["version"]
    assert result["summary"]["id"] in {record["id"] for record in store.visible_memories("web:default", "owner")}
    store.delete_memory(first["id"])
    assert result["summary"]["id"] not in {record["id"] for record in store.visible_memories("web:default", "owner")}


@pytest.mark.anyio
async def test_model_error_keeps_valid_raw_history_and_logs_no_secret(store):
    store.upsert_scope({"id": "web:default", "context_compress": True})

    class Broken:
        async def complete(self, *args, **kwargs):
            raise RuntimeError("secret-model-key")

    messages = history(store)
    result = await ContextEngine(store, config(), Broken()).build("web:default", "owner", messages)
    assert result["reason"] == "model_failed" and result["messages"] == messages
    assert "secret-model-key" not in json.dumps(store.list_events())


def test_diagnostics_are_local_aggregate_only_and_redact_recursive_secrets(store):
    cfg = config()
    cfg["onebot"].update(enabled=True, access_token="secret-qq-key")
    store.record_usage({"input_tokens": 100, "output_tokens": 2, "cost": 0.25})
    store.create_memory({"content": "待确认", "status": "pending"})

    class Status:
        def status(self):
            return {"connected": False, "providers": [{"id": "local", "configured": True, "api_key": "extra-provider-secret", "last_error": "extra-provider-secret secret-model-key"}],
                    "routes": {"summary": {"id": "local"}}, "access_token": "secret-qq-key", "headers": {"Authorization": "Bearer unknown"}}

        def complete(self, *args, **kwargs):
            raise AssertionError("diagnostics must not contact a network")

    result = build_diagnostics(store, cfg, Status(), Status())
    encoded = json.dumps(result)
    assert all(secret not in encoded for secret in ("secret-model-key", "secret-qq-key", "extra-provider-secret", "Bearer unknown"))
    assert result["budget"]["tokens"] == 102 and result["budget"]["exhausted"]
    assert result["memory"]["pending"] == 1 and result["checks"]
    assert not result["network_tested"] and result["routes"]["summary"]["id"] == "local"


def test_expression_store_defaults_and_tombstone_category(store):
    scope = store.upsert_scope({"id": "web:new"})
    from shiori.settings import DEFAULTS, resolved_identity
    assert scope['plan_enabled'] is None and resolved_identity(DEFAULTS, scope)['plan_enabled']
    assert not scope["expression_use"] and not scope["expression_learn"] and not scope["context_compress"]
    word = store.create_jargon({"term": "好耶", "meaning": "开心", "status": "approved"})
    expression = store.create_jargon({"term": "好耶", "meaning": "短句", "status": "approved", "entry_type": "expression"})
    assert [record["id"] for record in store.match_jargon("web:default", "好耶")] == [word["id"]]
    assert store.delete_jargon(expression["id"])["entry_type"] == "expression"


@pytest.mark.anyio
@pytest.mark.parametrize("content,reason", [("", "empty_summary"), ("无界" * 1500, "summary_exceeds_output_limit")])
async def test_invalid_summary_output_preserves_original_messages(store, content, reason):
    store.upsert_scope({"id": "web:default", "context_compress": True})

    class Invalid:
        async def complete(self, *args, **kwargs):
            return {"content": content}

    messages = history(store)
    result = await ContextEngine(store, config(), Invalid()).build("web:default", "owner", messages)
    assert result["reason"] == reason and result["messages"] == messages
    assert not store.list_memories()


def test_diagnostics_uses_chat_and_embedding_routes_authoritatively(store):
    class Routed:
        def status(self):
            return {"providers": [{"id": "embedding-only", "configured": True}], "routes": {"chat": {"id": "disabled"}}}

        def resolve(self, purpose):
            if purpose == "chat":
                return {"enabled": False, "model": "disabled", "base_url": "https://invalid.example/v1"}
            return {"enabled": True, "model": "routed-embedding", "base_url": "https://invalid.example/v1"}

    result = build_diagnostics(store, config(), Routed(), None)
    assert not result["model"]["configured"] and result["memory"]["embedding_model_set"]


@pytest.mark.anyio
async def test_context_uses_summary_route_without_legacy_single_model(store):
    class Routed(Provider):
        def resolve(self, purpose):
            assert purpose == "summary"
            return {"enabled": True, "model": "summary-model", "base_url": "https://invalid.example/v1"}

    store.upsert_scope({"id": "web:default", "context_compress": True})
    cfg = config()
    cfg["model"] = {}
    provider = Routed()
    result = await ContextEngine(store, cfg, provider).build("web:default", "owner", history(store))
    assert result["compressed"] and len(provider.calls) == 1


@pytest.mark.anyio
async def test_source_edit_during_generation_returns_current_text_and_never_stale_summary(store):
    store.upsert_scope({"id": "web:default", "context_compress": True})
    messages = history(store)

    def edit():
        record = store.get_message(messages[0]["id"])
        record["content"] = "新的菜单约定：只做素食"
        with store._lock, store._conn:
            store._write("messages", record, ("scope_id", "user_id", "role", "created_at"))

    result = await ContextEngine(store, config(), Provider(edit)).build("web:default", "owner", messages)
    assert result["reason"] == "sources_changed" and not result["summary"]
    assert result["messages"][0]["content"] == "新的菜单约定：只做素食"


@pytest.mark.anyio
async def test_digest_json_fields_survive_schema_one_backup_and_restore(store, tmp_path):
    store.upsert_scope({"id": "web:default", "context_compress": True})
    result = await ContextEngine(store, config(), Provider()).build("web:default", "owner", history(store))
    destination = store.backup(tmp_path / "digest-backup.db")
    restored = Store(tmp_path / "restored")
    try:
        restored.restore_from(destination)
        assert restored._conn.execute("PRAGMA user_version").fetchone()[0] == 1
        summary = restored.get_memory(result["summary"]["id"])
        assert summary["source_fingerprints"] and summary["id"] in {record["id"] for record in restored.visible_memories("web:default", "owner")}
    finally:
        restored.close()
