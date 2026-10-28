import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from qunyu.db import Store
from shiori.official import OfficialBridge


@pytest.fixture
def anyio_backend():
    return "asyncio"


class Settings:
    def __init__(self):
        self.config = {"paused": False, "onebot": {"adapter": "official"},
                       "qq_official": {"enabled": True, "app_id": "app", "app_secret": "test-secret",
                                       "allowed_groups": ["group-openid"], "allowed_users": ["user-openid"]}}

    def get(self):
        return self.config


class Chat:
    def __init__(self, store):
        self.store = store
        self.calls = []

    async def respond(self, scope_id, user_id, text, images=None, metadata=None):
        self.calls.append((scope_id, user_id, text, images, metadata))
        self.store.add_message(scope_id, user_id, "user", text, **metadata)
        return {"reply": "收到", "message_id": "reply-id"}


@pytest.fixture
def connected(tmp_path):
    store = Store(tmp_path / "official-data")
    settings = Settings()
    chat = Chat(store)
    bridge = OfficialBridge(store, settings, chat)
    api = SimpleNamespace(post_group_message=AsyncMock(return_value={"id": "sent-group"}),
                          post_c2c_message=AsyncMock(return_value={"id": "sent-user"}))
    bridge._client = SimpleNamespace(api=api)
    bridge._running = bridge._connected = True
    yield bridge, api, settings, chat, store
    store.close()


def event(kind="GROUP_AT_MESSAGE_CREATE", message_id="msg-1", target="group-openid"):
    if kind == "C2C_MESSAGE_CREATE":
        return {"t": kind, "d": {"id": message_id, "author": {"user_openid": target}, "content": "你好"}}
    return {"t": kind, "d": {"id": message_id, "group_openid": target,
                             "author": {"member_openid": "member-openid"}, "content": "<@bot> 你好"}}


@pytest.mark.anyio
async def test_group_passive_reply_uses_original_message_and_openids(connected):
    bridge, api, _, chat, _ = connected
    await bridge.handle_event(event())
    assert chat.calls[0][:3] == ("qqofficial:group:group-openid", "member-openid", "你好")
    assert chat.calls[0][4]["is_owner"] is False
    api.post_group_message.assert_awaited_once_with(group_openid="group-openid", msg_type=0, content="收到", msg_id="msg-1", msg_seq=1)
    assert not api.post_c2c_message.called


@pytest.mark.anyio
async def test_private_allowlist_and_private_scope(connected):
    bridge, api, _, chat, _ = connected
    await bridge.handle_event(event("C2C_MESSAGE_CREATE", target="stranger"))
    assert not chat.calls
    await bridge.handle_event(event("C2C_MESSAGE_CREATE", target="user-openid"))
    assert chat.calls[0][0] == "qqofficial:private:user-openid"
    # Chat allowlists no longer confer administrator or computer privileges.
    assert chat.calls[0][4]["is_owner"] is False
    assert chat.calls[0][4]["is_admin"] is False
    api.post_c2c_message.assert_awaited_once_with(openid="user-openid", msg_type=0, content="收到", msg_id="msg-1", msg_seq=1)


@pytest.mark.anyio
async def test_unauthorized_paused_disabled_scopes_do_not_generate(connected):
    bridge, api, settings, chat, store = connected
    await bridge.handle_event(event(target="not-allowed"))
    settings.config["paused"] = True
    await bridge.handle_event(event())
    settings.config["paused"] = False
    store.upsert_scope({"id": "qqofficial:group:group-openid", "enabled": False})
    await bridge.handle_event(event())
    assert not chat.calls and not api.post_group_message.called


@pytest.mark.anyio
async def test_replayed_message_deduplicates_across_bridge_recreation(connected):
    bridge, api, settings, chat, store = connected
    await bridge.handle_event(event())
    await bridge.handle_event(event())
    replacement = OfficialBridge(store, settings, chat)
    replacement._client, replacement._running, replacement._connected = bridge._client, True, True
    await replacement.handle_event(event())
    assert len(chat.calls) == api.post_group_message.await_count == 1


@pytest.mark.anyio
async def test_reminder_is_proactive_and_only_actual_confirmation_is_success(connected):
    bridge, api, _, _, _ = connected
    assert await bridge.send("qqofficial:group:group-openid", "提醒") is True
    assert "msg_id" not in api.post_group_message.call_args.kwargs
    api.post_group_message.return_value = {"code": 304027, "message": "quota"}
    assert await bridge.send("qqofficial:group:group-openid", "提醒") is False
    api.post_group_message.side_effect = TimeoutError("must-not-leak-test-secret")
    assert await bridge.send("qqofficial:group:group-openid", "提醒") is False
    assert "must-not-leak" not in bridge.status()["last_error"]
    assert api.post_group_message.await_count == 3  # no blind retries


@pytest.mark.anyio
async def test_permission_revocation_while_generating_prevents_send(connected):
    bridge, api, settings, chat, _ = connected
    original = chat.respond

    async def revoke(*args, **kwargs):
        result = await original(*args, **kwargs)
        settings.config["qq_official"]["allowed_groups"] = []
        return result

    chat.respond = revoke
    await bridge.handle_event(event())
    assert len(chat.calls) == 1 and not api.post_group_message.called


@pytest.mark.anyio
async def test_attachment_filter_and_sdk_object_fields(connected):
    bridge, _, _, chat, _ = connected
    message = SimpleNamespace(id="image-1", group_openid="group-openid", content="看图",
                              author=SimpleNamespace(member_openid="member-openid"),
                              attachments=[SimpleNamespace(content_type="image/png", url="//example.org/a.png"),
                                           SimpleNamespace(content_type="application/pdf", url="https://example.org/a.pdf")])
    await bridge._handle_message("group", message)
    assert chat.calls[0][3] == ["https://example.org/a.png"]


@pytest.mark.anyio
async def test_switching_adapter_and_disconnected_send_fail_closed(connected):
    bridge, api, settings, _, _ = connected
    settings.config["onebot"]["adapter"] = "napcat"
    assert not await bridge.send("qqofficial:group:group-openid", "提醒")
    settings.config["onebot"]["adapter"] = "official"
    bridge._connected = False
    assert not await bridge.send("qqofficial:private:user-openid", "提醒")
    assert not api.post_group_message.called and not api.post_c2c_message.called


@pytest.mark.anyio
async def test_start_stop_are_nonblocking_and_sdk_credentials_stay_internal(tmp_path, monkeypatch):
    store, settings = Store(tmp_path / "lifecycle"), Settings()
    bridge = OfficialBridge(store, settings, Chat(store))
    entered, blocker = asyncio.Event(), asyncio.Event()

    class FakeClient:
        credentials = None
        closed = False

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            await self.close()

        async def start(self, **kwargs):
            self.credentials = kwargs
            bridge._connected = True
            entered.set()
            await blocker.wait()

        async def close(self):
            self.closed = True

    client = FakeClient()
    monkeypatch.setattr(bridge, "_build_client", lambda: client)
    try:
        await bridge.start()
        await asyncio.wait_for(entered.wait(), 1)
        assert client.credentials == {"appid": "app", "secret": "test-secret"}
        assert bridge.status()["connected"] is True
        assert "test-secret" not in str(bridge.status())
        await bridge.stop()
        await bridge.stop()
        assert client.closed and bridge._runner is None and bridge.status()["connected"] is False
    finally:
        await bridge.stop()
        store.close()


@pytest.mark.anyio
async def test_optional_sdk_constructs_without_connecting(tmp_path):
    store = Store(tmp_path / "sdk-construct")
    bridge = OfficialBridge(store, Settings(), Chat(store))
    try:
        client = bridge._build_client()
        assert client.intents == 1 << 25
        await client.close()
    finally:
        store.close()
