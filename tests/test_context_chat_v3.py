"""Exercise context provenance through ChatService, not only the engine."""
import copy

import pytest

from shiori.chat import ChatService
from qunyu.db import Store
from shiori.settings import DEFAULTS, resolve_model


DIGEST = "CONVERSATION_DIGEST_ONLY：大家约好周日一起做饭，菜单尚待确认。"


@pytest.fixture
def anyio_backend():
    return "asyncio"


class MutableSettings:
    def __init__(self):
        self.data = copy.deepcopy(DEFAULTS)
        self.data["model"].update(base_url="https://mock.invalid/v1", model="mock")
        self.data["context"].update(enabled=True, trigger_messages=6, max_chars=6000, keep_recent=2)

    def get(self):
        return copy.deepcopy(self.data)


class RecordingProvider:
    def __init__(self, settings):
        self.settings = settings
        self.calls = []
        self.on_digest = None
        self.on_chat = None

    def resolve(self, purpose):
        return resolve_model(self.settings.get(), purpose)

    async def complete(self, messages, **kwargs):
        purpose = kwargs.get("purpose", "chat")
        self.calls.append({"messages": copy.deepcopy(messages), "purpose": purpose, "kwargs": kwargs})
        if purpose == "context_digest":
            if self.on_digest:
                self.on_digest()
            content = DIGEST
        else:
            content = self.on_chat(messages) if self.on_chat else f"CHAT_REPLY_{len(self.chat_calls)}"
        return {"content": content, "tool_calls": [], "message": {"role": "assistant", "content": content},
                "usage": {"input_tokens": 10, "output_tokens": 5}}

    @property
    def chat_calls(self):
        return [call for call in self.calls if call["purpose"] == "chat"]


@pytest.fixture
async def service(tmp_path, anyio_backend):
    store, settings = Store(tmp_path), MutableSettings()
    store.upsert_scope({"id": "web:default", "context_compress": True, "auto_memory": False, "allow_tools": False})
    provider = RecordingProvider(settings)
    chat = ChatService(store, settings, provider)
    yield store, settings, provider, chat
    await chat.close()
    store.close()


def seed(store, **metadata):
    return [store.add_message("web:default", "owner" if i % 2 == 0 else "assistant",
                              "user" if i % 2 == 0 else "assistant", f"ORIGINAL_ROUND_{i}：讨论周日菜单。", **metadata)
            for i in range(10)]


def digest_records(store):
    return [record for record in store.list_memories() if record.get("property") == "conversation_digest"]


def sent_text(call):
    return "\n".join(str(message["content"]) for message in call["messages"])


@pytest.mark.anyio
async def test_chat_injects_digest_as_separate_system_data_and_records_manifest(service):
    store, settings, provider, chat = service
    originals = seed(store)
    result = await chat.respond("web:default", "owner", "那菜单接下来怎么安排？")
    assert result["context_compressed"] and len(provider.chat_calls) == 1
    messages = provider.chat_calls[0]["messages"]
    summaries = [message for message in messages if DIGEST in str(message["content"])]
    assert len(summaries) == 1 and summaries[0]["role"] == "system"
    assert "只作背景资料" in summaries[0]["content"]
    assert all(DIGEST not in message["content"] for message in messages if message["role"] in {"user", "assistant"})
    assert "ORIGINAL_ROUND_0" not in sent_text(provider.chat_calls[0])
    assert "ORIGINAL_ROUND_8" in sent_text(provider.chat_calls[0])
    summary = digest_records(store)[0]
    saved = store.get_message(result["message_id"])
    assert saved["metadata"]["memory_versions"][summary["id"]] == summary["version"]
    assert {record["id"] for record in originals} <= set(saved["metadata"]["context_source_ids"])
    assert summary["id"] not in {item["id"] for item in result["memories"]}


@pytest.mark.anyio
async def test_corrected_dependency_removes_old_digest_and_old_reply_from_chat(service):
    store, settings, provider, chat = service
    fact = store.create_memory({"content": "我的居住城市是旧城", "pinned": True})
    seed(store, memory_versions={fact["id"]: fact["version"]})
    first = await chat.respond("web:default", "owner", "居住城市有什么安排？")
    old_digest = digest_records(store)[0]
    old_reply = first["reply"]
    store.update_memory(fact["id"], {"content": "我的居住城市是新城"})
    second = await chat.respond("web:default", "owner", "居住城市后来怎么说？")
    sent = sent_text(provider.chat_calls[-1])
    assert DIGEST not in sent and "旧城" not in sent and old_reply not in sent
    assert "新城" in sent and not second["context_compressed"]
    assert old_digest["id"] not in store.get_message(second["message_id"])["metadata"]["memory_versions"]


@pytest.mark.anyio
async def test_source_deletion_invalidates_digest_and_prevents_inherited_reply_reuse(service):
    store, settings, provider, chat = service
    originals = seed(store)
    first = await chat.respond("web:default", "owner", "菜单还有什么要确认？")
    summary = digest_records(store)[0]
    deleted = originals[0]["id"]
    with store._lock, store._conn:
        store._conn.execute("DELETE FROM messages WHERE id=?", (deleted,))
    settings.data["context"]["trigger_messages"] = 100
    second = await chat.respond("web:default", "owner", "继续讨论吧")
    sent = sent_text(provider.chat_calls[-1])
    assert DIGEST not in sent and first["reply"] not in sent
    assert "ORIGINAL_ROUND_8" in sent and not second["context_compressed"]
    saved = store.get_message(second["message_id"])["metadata"]
    assert deleted not in saved["context_source_ids"] and summary["id"] not in saved["memory_versions"]


@pytest.mark.anyio
async def test_global_context_off_excludes_digest_from_recall_and_old_summary_reply(service):
    store, settings, provider, chat = service
    seed(store)
    provider.on_chat = lambda messages: "OLD_REPLY_FROM_DIGEST：" + DIGEST
    first = await chat.respond("web:default", "owner", "说说周日做饭的安排")
    summary = digest_records(store)[0]
    # The Store can still expose this valid event for an explicitly invoked
    # history tool. Normal automatic chat recall must exclude it.
    assert summary["id"] in {record["id"] for record in store.recall("web:default", "owner", "CONVERSATION_DIGEST_ONLY")}
    settings.data["context"]["enabled"] = False
    provider.on_chat = None
    assert summary["id"] not in {record["id"] for record in await chat._recall_context("web:default", "owner", "CONVERSATION_DIGEST_ONLY")}
    result = await chat.respond("web:default", "owner", "CONVERSATION_DIGEST_ONLY 还有什么？")
    sent = sent_text(provider.chat_calls[-1])
    assert DIGEST not in sent and first["reply"] not in sent
    assert not result["context_compressed"]
    assert summary["id"] not in store.get_message(result["message_id"])["metadata"]["memory_versions"]


@pytest.mark.anyio
async def test_conflicting_new_history_version_cannot_authorize_old_recalled_body(service):
    store, settings, provider, chat = service
    originals = seed(store)
    fact = store.create_memory({"content": "项目代号是OLD_FACT_BODY", "pinned": True})

    def correct_while_digest_awaits():
        current = store.update_memory(fact["id"], {"content": "项目代号是NEW_FACT_BODY"})
        store.add_message("web:default", "assistant", "assistant", "NEW_BACKGROUND_CURRENT_V2",
                          source_ids=[originals[-2]["id"]], memory_versions={fact["id"]: current["version"]})

    provider.on_digest = correct_while_digest_awaits
    provider.on_chat = lambda messages: "STALE_BODY_ACCEPTED" if "OLD_FACT_BODY" in str(messages) else "FRESH_BODY_ACCEPTED"
    try:
        result = await chat.respond("web:default", "owner", "项目代号接下来呢？")
    except ValueError:
        assert all(record["content"] != "STALE_BODY_ACCEPTED" for record in store.list_messages("web:default"))
        return
    assert result["reply"] == "FRESH_BODY_ACCEPTED"
    saved = store.get_message(result["message_id"])
    assert saved["metadata"]["memory_versions"][fact["id"]] == store.get_memory(fact["id"])["version"]
    assert "OLD_FACT_BODY" not in sent_text(provider.chat_calls[-1])


@pytest.mark.anyio
async def test_global_disable_during_final_completion_never_saves_summary_based_output(service):
    store, settings, provider, chat = service
    seed(store)

    def disable_on_old_summary(messages):
        if DIGEST in str(messages):
            settings.data["context"]["enabled"] = False
            return "STALE_SUMMARY_OUTPUT"
        return "FRESH_UNCOMPRESSED_OUTPUT"

    provider.on_chat = disable_on_old_summary
    try:
        result = await chat.respond("web:default", "owner", "菜单接下来怎么安排？")
    except ValueError:
        assert all(record["content"] != "STALE_SUMMARY_OUTPUT" for record in store.list_messages("web:default"))
        return
    assert result["reply"] == "FRESH_UNCOMPRESSED_OUTPUT" and not result["context_compressed"]
    assert DIGEST not in sent_text(provider.chat_calls[-1])
    assert all(record["content"] != "STALE_SUMMARY_OUTPUT" for record in store.list_messages("web:default"))
