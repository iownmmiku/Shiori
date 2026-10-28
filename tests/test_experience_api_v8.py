"""End-to-end local workflows with isolated files and scripted models.

No connector, public network, billed model, or real user file is used here.
"""
import asyncio
import io
import json
import threading
import time
import zipfile

import httpx
import pytest
from fastapi.testclient import TestClient

from shiori.api import create_app
from shiori.mcp_client import schema_hash
from shiori.provider import ProviderError
from shiori.runs import RunManager
from auth_helpers import password_login


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
def experience(tmp_path):
    app = create_app(tmp_path / "experience", start_workers=False)
    with TestClient(app) as client:
        password_login(client, app)
        yield client, app


def require_ok(response):
    assert response.status_code == 200, response.text
    return response.json()


def wait_job(client, job_id, status="completed"):
    for _ in range(200):
        job = require_ok(client.get(f"/api/jobs/{job_id}"))
        if job["status"] == status:
            return job
        if job["status"] in {"failed", "cancelled", "interrupted"} and job["status"] != status:
            raise AssertionError(job)
        time.sleep(.01)
    raise AssertionError(f"job did not become {status}: {job}")


def corpus_zip():
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as archive:
        archive.writestr("chat.json", json.dumps([
            {"speaker": "目标", "text": "先弄清事实，再把步骤列出来。"},
            {"speaker": "朋友", "text": "可以，我们慢慢来。"},
            {"speaker": "目标", "text": "不确定的地方先保留，不要急着判断。"},
        ], ensure_ascii=False))
        archive.writestr("ignored.png", b"fixture image, no OCR")
    return stream.getvalue()


def test_every_experience_route_requires_local_authentication(experience):
    client, _ = experience
    client.cookies.clear()
    requests = [
        ("GET", "/api/admins"), ("POST", "/api/admins"), ("DELETE", "/api/admins/example"),
        ("POST", "/api/admins/pairing"), ("GET", "/api/admins/pairing/example"),
        ("POST", "/api/admins/pairing/example/confirm"), ("DELETE", "/api/admins/pairing/example"),
        ("POST", "/api/qq/local/discover"), ("POST", "/api/qq/local/connect"),
        ("POST", "/api/qq/local/disconnect"), ("GET", "/api/jobs"),
        ("GET", "/api/jobs/example"), ("POST", "/api/jobs/example/cancel"),
        ("POST", "/api/jobs/example/approve"), ("GET", "/api/jobs/example/stream"),
        ("GET", "/api/jobs/example/files/0"), ("POST", "/api/computer/jobs"),
        ("POST", "/api/persona/distill/character/search"), ("POST", "/api/persona/distill/corpus"),
        ("POST", "/api/persona/distill/start"), ("POST", "/api/persona/distill/example/apply"),
        ("POST", "/api/persona/distill/example/trial"), ("GET", "/api/persona/distill/example/artifact"),
        ("POST", "/api/chat/attachments"), ("DELETE", "/api/chat/attachments/example"),
        ("POST", "/api/models"), ("POST", "/api/chat"), ("POST", "/api/scopes"),
    ]
    for method, path in requests:
        result = client.request(method, path, json={})
        assert result.status_code == 401, (method, path, result.text)


def test_cross_site_and_remote_host_cannot_use_authenticated_experience_api(experience):
    client, _ = experience
    for headers in ({"Origin": "https://unrelated.example"}, {"Sec-Fetch-Site": "cross-site"},
                    {"Host": "192.168.1.10:18765"}):
        response = client.post("/api/computer/jobs", headers=headers, json={"text": "fixture"})
        assert response.status_code == 403
    assert require_ok(client.get("/api/jobs")) == []


def test_administrators_are_multiple_and_builtin_local_account_is_fixed(experience):
    client, app = experience
    local = require_ok(client.get("/api/admins"))[0]
    assert local["built_in"] and local["account_id"] == "shiori"
    assert client.delete("/api/admins/" + local["id"]).status_code == 400
    for account in ("123456", "654321"):
        entries = require_ok(client.post("/api/admins", json={"platform": "onebot", "account_id": account}))
    assert {entry["account_id"] for entry in entries} == {"shiori", "123456", "654321"}
    assert set(app.state.settings.get()["onebot"]["allowed_users"]) == {"123456", "654321"}
    # Payload flags, local names, and unknown platforms never create built-in credentials.
    assert client.post("/api/admins", json={"platform": "local", "account_id": "shiori", "built_in": True}).status_code == 400
    assert client.post("/api/admins", json={"platform": "onebot", "account_id": "shiori", "is_admin": True}).status_code == 400
    entry = next(entry for entry in entries if entry["account_id"] == "123456")
    require_ok(client.delete("/api/admins/" + entry["id"]))
    assert not app.state.access.is_admin(app.state.access.identity("onebot", "123456", "9000"))
    # Revocation removes authority, while keeping ordinary chatting separately allowed.
    assert "123456" in app.state.settings.get()["onebot"]["allowed_users"]


def test_pairing_requires_verified_private_ingress_and_local_confirmation(experience):
    client, app = experience
    pairing = require_ok(client.post("/api/admins/pairing"))
    assert client.post(f"/api/admins/pairing/{pairing['id']}/confirm", json={}).status_code == 400
    forged = {"platform": "onebot", "account_id": "333", "instance_id": "9000", "proof": "forged"}
    assert not app.state.access.capture_pairing(pairing["code"], forged)
    identity = app.state.access.identity("onebot", "333", "9000")
    assert not app.state.access.capture_pairing(pairing["code"], identity, private=False)
    assert app.state.access.capture_pairing(pairing["code"], identity)
    current = require_ok(client.get(f"/api/admins/pairing/{pairing['id']}"))
    assert current["status"] == "awaiting_confirmation"
    assert not app.state.access.is_admin(identity)
    confirmed = require_ok(client.post(f"/api/admins/pairing/{pairing['id']}/confirm", json={"label": "测试管理员"}))
    assert confirmed["account_id"] == "333" and confirmed["instance_id"] == "9000"
    assert app.state.access.is_admin(identity)
    assert client.get(f"/api/admins/pairing/{pairing['id']}").status_code == 404
    assert not app.state.access.capture_pairing(pairing["code"], identity)
    other = require_ok(client.post("/api/admins/pairing"))
    require_ok(client.delete(f"/api/admins/pairing/{other['id']}"))
    assert client.get(f"/api/admins/pairing/{other['id']}").status_code == 404


def test_model_list_uses_unsaved_form_without_persisting_credentials(experience, monkeypatch):
    client, app = experience
    before = app.state.settings.get()["model"]
    observed = []
    async def list_models(config):
        observed.append(config)
        return {"models": ["fixture-model"]}
    monkeypatch.setattr(app.state.provider, "list_models", list_models)
    response = require_ok(client.post("/api/models", json={"base_url": "https://models.example/v1",
        "api_key": "fixture-unsaved-key", "model": "new-model"}))
    assert response["models"] == ["fixture-model"]
    assert observed[0]["api_key"] == "fixture-unsaved-key"
    assert observed[0]["base_url"] == "https://models.example/v1"
    assert app.state.settings.get()["model"] == before
    assert client.post("/api/models", json={"provider_id": "deleted-provider"}).status_code == 400
    assert len(observed) == 1


def test_new_local_scope_automatically_generates_identity_and_always_reply(experience):
    client, _ = experience
    first = require_ok(client.post("/api/scopes", json={"name": "写作讨论"}))
    second = require_ok(client.post("/api/scopes", json={"name": "写作讨论"}))
    assert first["id"].startswith("web:") and first["id"] != second["id"]
    assert first["always_reply"] and not first["plan_enabled"]


def test_chat_ignores_payload_identity_and_passes_authenticated_local_account(experience, monkeypatch):
    client, app = experience
    observed = {}
    async def respond(scope, user, text, **kwargs):
        observed.update(scope=scope, user=user, text=text, metadata=kwargs["metadata"])
        return {"reply": "测试回复"}
    monkeypatch.setattr(app.state.chat, "respond", respond)
    require_ok(client.post("/api/chat", json={"text": "你好", "user_id": "attacker", "is_admin": True,
        "trusted_identity": {"platform": "local", "account_id": "attacker"},
        "metadata": {"trusted_identity": {"platform": "onebot", "account_id": "123"}}}))
    identity = observed["metadata"]["trusted_identity"]
    assert observed["user"] == "owner"
    assert identity["platform"] == "local" and identity["account_id"] == "shiori"
    assert app.state.access.is_admin(identity)
    assert client.post("/api/chat", json={"scope_id": "qq:private:123", "text": "不能直接发送"}).status_code == 400


def test_attachment_reference_is_ephemeral_memory_is_explicit_and_scoped(experience, monkeypatch):
    client, app = experience
    body = "只有当前聊天可以读取的临时资料，不应成为长期记忆。"
    ref = require_ok(client.post("/api/chat/attachments", data={"scope_id": "web:default", "purpose": "context"},
        files={"file": ("reference.txt", body.encode(), "text/plain")}))
    assert ref["purpose"] == "context" and "memory" not in ref
    assert require_ok(client.get("/api/memories")) == []
    captured = []
    async def complete(messages, **kwargs):
        assert kwargs["before_send"]()
        captured.append(messages)
        return {"content": "已参考资料", "usage": {"input_tokens": 20, "output_tokens": 8}}
    require_ok(client.put("/api/settings", json={"model": {"base_url": "https://models.example/v1", "model": "fixture"}}))
    monkeypatch.setattr(app.state.provider, "complete", complete)
    require_ok(client.post("/api/chat", json={"text": "总结附件", "attachments": [ref["id"]]}))
    assert body in json.dumps(captured[0], ensure_ascii=False)
    assert app.state.store.list_messages("web:default")[0]["content"] == "总结附件"
    assert require_ok(client.get("/api/memories")) == []
    other = require_ok(client.post("/api/scopes", json={"name": "另一个聊天"}))
    forbidden = client.post("/api/chat", json={"scope_id": other["id"], "text": "引用其他聊天", "attachments": [ref["id"]]})
    assert forbidden.status_code == 403 and len(captured) == 1
    assert client.delete(f"/api/chat/attachments/{ref['id']}", params={"scope_id": other["id"]}).status_code == 403
    explicit = require_ok(client.post("/api/chat/attachments", data={"scope_id": "web:default", "purpose": "memory"},
        files={"file": ("long-term.txt", "明确保存的长期资料".encode(), "text/plain")}))
    memories = require_ok(client.get("/api/memories"))
    assert len(memories) == 1 and memories[0]["id"] == explicit["memory"]["id"]
    assert memories[0]["scope_id"] == "web:default" and memories[0]["visibility"] == "private"
    require_ok(client.delete(f"/api/chat/attachments/{ref['id']}"))
    assert client.post("/api/chat", json={"text": "引用已删除附件", "attachments": [ref["id"]]}).status_code == 400


def test_computer_disabled_and_untrusted_platform_identity_cannot_start_job(experience):
    client, app = experience
    assert client.post("/api/computer/jobs", json={"text": "生成一个测试文件"}).status_code == 403
    assert client.post("/api/chat", json={"text": "测试", "mode": "computer"}).status_code == 403
    require_ok(client.put("/api/settings", json={"computer": {"enabled": True, 'runtime': 'local'}}))
    ordinary = app.state.access.identity("onebot", "123", "9000")
    for metadata in ({"trusted_identity": ordinary, "is_admin": True, "is_owner": True},
                     {"trusted_identity": {"platform": "local", "account_id": "shiori", "proof": "forged"}}):
        response = client.portal.call(app.state.chat.handle_platform_command, "qq:private:123", "123", "/执行 生成测试文件", metadata)
        assert "只有" in response["reply"]
    assert require_ok(client.get("/api/jobs")) == []
    assert client.post("/api/computer/jobs", json={"scope_id": "qq:private:123", "text": "测试"}).status_code == 400


def test_computer_api_approval_resumes_once_and_returns_file_and_sse(experience, monkeypatch, tmp_path):
    client, app = experience
    workspace = tmp_path / "workspace"
    require_ok(client.put("/api/settings", json={"computer": {"enabled": True, 'runtime': 'local', "workspace": str(workspace), "approval_mode": "per_action"}}))
    calls = []
    async def complete(messages, **kwargs):
        calls.append(messages)
        assert kwargs["before_send"]()
        if len(calls) == 1:
            return {"content": "", "tool_calls": [{"id": "write-one", "type": "function", "function": {
                "name": "write_file", "arguments": json.dumps({"path": "result.txt", "content": "fixture generated output"})}}]}
        assert messages[-1]["role"] == "tool" and "fixture generated output" not in messages[-1]["content"]
        return {"content": "已生成 result.txt"}
    monkeypatch.setattr(app.state.provider, "complete", complete)
    job = require_ok(client.post("/api/computer/jobs", json={"text": "生成测试文件 result.txt",
        "identity": {"platform": "onebot", "account_id": "fake"}}))
    assert job["identity"]["platform"] == "local" and job["identity"]["account_id"] == "shiori"
    pending = wait_job(client, job["id"], "awaiting_approval")
    assert not (workspace / "result.txt").exists() and len(calls) == 1
    assert client.post(f"/api/jobs/{job['id']}/approve", json={"approval_id": "wrong"}).status_code == 400
    approval = pending["approval"]["id"]
    require_ok(client.post(f"/api/jobs/{job['id']}/approve", json={"approval_id": approval}))
    assert client.post(f"/api/jobs/{job['id']}/approve", json={"approval_id": approval}).status_code == 400
    completed = wait_job(client, job["id"])
    assert len(calls) == 2 and completed["result"]["reply"] == "已生成 result.txt"
    assert (workspace / "result.txt").read_text("utf-8") == "fixture generated output"
    response = client.get(f"/api/jobs/{job['id']}/files/0")
    assert response.status_code == 200 and response.text == "fixture generated output"
    stream = client.get(f"/api/jobs/{job['id']}/stream")
    assert stream.status_code == 200 and "text/event-stream" in stream.headers["content-type"]
    events = [json.loads(line[6:]) for line in stream.text.splitlines() if line.startswith("data: ")]
    types = [event["type"] for event in events]
    assert types.index("started") < types.index("approval") < types.index("approved") < types.index("completed")
    assert events[-1]["type"] == "snapshot" and events[-1]["data"]["status"] == "completed"
    resume = client.get(f"/api/jobs/{job['id']}/stream", params={"after": completed["event_cursor"]})
    assert '"type": "snapshot"' in resume.text and "id: " not in resume.text


def test_waiting_computer_job_cancellation_never_writes(experience, monkeypatch, tmp_path):
    client, app = experience
    workspace = tmp_path / "workspace"
    require_ok(client.put("/api/settings", json={"computer": {"enabled": True, 'runtime': 'local', "workspace": str(workspace), "approval_mode": "per_action"}}))
    async def complete(messages, **kwargs):
        return {"tool_calls": [{"id": "write-cancelled", "function": {
            "name": "write_file", "arguments": json.dumps({"path": "cancelled.txt", "content": "must not write"})}}]}
    monkeypatch.setattr(app.state.provider, "complete", complete)
    job = require_ok(client.post("/api/chat", json={"text": "生成文件", "mode": "computer"}))["job"]
    approval = wait_job(client, job["id"], "awaiting_approval")["approval"]["id"]
    require_ok(client.post(f"/api/jobs/{job['id']}/cancel"))
    wait_job(client, job["id"], "cancelled")
    assert client.post(f"/api/jobs/{job['id']}/approve", json={"approval_id": approval}).status_code == 400
    assert not (workspace / "cancelled.txt").exists()


def test_corpus_distillation_inventory_confirm_apply_trial_and_no_skill(experience, monkeypatch):
    client, app = experience
    upload = require_ok(client.post("/api/persona/distill/corpus", files={"file": ("corpus.zip", corpus_zip(), "application/zip")}))
    assert upload["readable_files"] == 1 and len(upload["files"]) == 2
    assert {row["name"] for row in upload["speakers"]} == {"目标", "朋友"}
    assert require_ok(client.get("/api/memories")) == []
    calls = []
    async def complete(messages, **kwargs):
        calls.append(kwargs["purpose"])
        if kwargs["purpose"] == "persona_trial":
            return {"content": "先把情况弄清楚，好吗？", "usage": {"input_tokens": 12, "output_tokens": 8}}
        assert kwargs["before_send"]()
        return {"content": "面对不确定的问题，先确认事实，再给出清晰步骤。表达简短，保留未知。", "usage": {"input_tokens": 100, "output_tokens": 40}}
    monkeypatch.setattr(app.state.provider, "complete", complete)
    payload = {"mode": "corpus", "upload_id": upload["upload_id"], "target_speaker": "目标", "name": "语料副本"}
    assert client.post("/api/persona/distill/start", json=payload).status_code == 400
    job = require_ok(client.post("/api/persona/distill/start", json={**payload, "confirmed": True}))
    completed = wait_job(client, job["id"])
    result = completed["result"]
    assert result["mode"] == "corpus" and result["artifact"] is None
    assert "资料边界" in result["prompt"] and result["applied"] is False
    assert len(require_ok(client.get("/api/personas"))) == 1
    assert client.get(f"/api/persona/distill/{job['id']}/artifact").status_code == 400
    trial = require_ok(client.post(f"/api/persona/distill/{job['id']}/trial", json={"text": "我不知道怎么办"}))
    assert trial["reply"] == "先把情况弄清楚，好吗？" and calls[-1] == "persona_trial"
    assert len(require_ok(client.get("/api/personas"))) == 1
    applied = require_ok(client.post(f"/api/persona/distill/{job['id']}/apply", json={"name": "审阅后副本"}))
    assert applied["name"] == "审阅后副本" and applied["id"] != "default"
    assert applied["system_prompt"] == result["prompt"] and applied["exploration_enabled"] is False
    assert require_ok(client.get("/api/memories")) == []


def test_character_distillation_returns_persona_and_downloadable_skill(experience, monkeypatch):
    client, app = experience
    async def search(query):
        return {"links": ["https://fixture.example/character"], "text": "fixture result"}
    async def read(url):
        return {"url": url, "title": "测试角色，测试作品", "text": "测试角色面对分歧会先倾听并确认事实。" * 20}
    async def complete(messages, **kwargs):
        assert kwargs["purpose"] == "distillation" and kwargs["before_send"]()
        return {"content": "情境：面对分歧；反应：先倾听并确认事实；证据：s1；资料不足时保留未知。", "usage": {"input_tokens": 100, "output_tokens": 50}}
    monkeypatch.setattr(app.state.distillation, "search_fn", search)
    monkeypatch.setattr(app.state.distillation, "read_fn", read)
    monkeypatch.setattr(app.state.provider, "complete", complete)
    discovery = require_ok(client.post("/api/persona/distill/character/search", json={"character": "测试角色", "work": "测试作品"}))
    assert discovery["candidates"] and len(require_ok(client.get("/api/jobs"))) == 0
    payload = {"mode": "character", "discovery_id": discovery["discovery_id"], "confirmed": True,
               "candidate_ids": [discovery["candidates"][0]["id"]], "version": "已确认原作"}
    job = require_ok(client.post("/api/persona/distill/start", json=payload))
    completed = wait_job(client, job["id"])
    assert completed["result"]["mode"] == "character" and completed["result"]["prompt"]
    response = client.get(f"/api/persona/distill/{job['id']}/artifact")
    assert response.status_code == 200 and response.headers["content-type"] == "application/zip"
    with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
        assert {"SKILL.md", "persona.md", "manifest.json", "references/sources.json"} <= set(archive.namelist())
        assert archive.read("persona.md").decode() == completed["result"]["prompt"]
    assert len(require_ok(client.get("/api/personas"))) == 1


def test_character_lookup_is_authenticated_and_does_not_start_paid_work(experience, monkeypatch):
    client, app = experience
    async def lookup(query, work):
        return {'characters': [{'id': 'bgm-22520', 'name': '和泉纱雾', '_page': {'text': 'private evidence'}}], 'failures': [], 'warnings': []}
    monkeypatch.setattr(app.state.distillation.catalog, 'lookup', lookup)
    result = require_ok(client.post('/api/persona/distill/character/lookup', json={'query': '和泉纱雾'}))
    assert result['lookup_id'] and '_page' not in result['characters'][0]
    assert require_ok(client.get('/api/jobs')) == []
    assert client.post('/api/persona/distill/character/lookup', json={'query': ''}).status_code == 400
    client.cookies.clear()
    assert client.post('/api/persona/distill/character/lookup', json={'query': '和泉纱雾'}).status_code == 401


def test_incomplete_distillation_cannot_apply_trial_or_download(experience, monkeypatch):
    client, app = experience
    async def generate(payload, progress, cancel_check, **kwargs):
        progress(stage="fixture_wait", message="等待测试停止")
        while True:
            cancel_check()
            await asyncio.sleep(.01)
    monkeypatch.setattr(app.state.distillation, "generate", generate)
    job = require_ok(client.post("/api/persona/distill/start", json={"mode": "corpus", "confirmed": True}))
    wait_job(client, job["id"], "running")
    assert client.post(f"/api/persona/distill/{job['id']}/apply", json={}).status_code == 400
    assert client.post(f"/api/persona/distill/{job['id']}/trial", json={"text": "不能试聊未完成草稿"}).status_code == 400
    assert client.get(f"/api/persona/distill/{job['id']}/artifact").status_code == 400
    require_ok(client.post(f"/api/jobs/{job['id']}/cancel"))
    wait_job(client, job["id"], "cancelled")
    assert len(require_ok(client.get("/api/personas"))) == 1


def test_real_chat_access_blocks_ordinary_qq_mcp_and_revoked_admin_approval(experience, monkeypatch):
    client, app = experience
    app.state.settings.update({"onebot": {"enabled": True, "access_token": "fixture-reverse-token", "allowed_groups": ["202"], "allowed_users": ["101", "303"]},
        "access": {"administrators": [{"platform": "onebot", "account_id": "101", "instance_id": "9000"}]},
        "mcp_servers": [{"id": "notes", "name": "Fixture notes", "url": "http://127.0.0.1:5678/mcp",
            "enabled": True, "allowed_tools": ["append_note"]}]})
    app.state.store.upsert_scope({"id": "qq:group:202", "allow_mcp": True, "allow_tools": True})
    schema = {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"], "additionalProperties": False}
    actions, external_calls = app.state.chat.tool_actions, []
    async def discover(config):
        return {"tools": [{"name": "append_note", "inputSchema": schema, "schema_hash": schema_hash(schema)}]}
    async def call(*args, **kwargs):
        external_calls.append(True)
        raise AssertionError("revoked or ordinary user must never reach MCP execution")
    monkeypatch.setattr(actions.client, "discover", discover)
    monkeypatch.setattr(actions.client, "call", call)
    require_ok(client.post("/api/mcp/discover", json={"server_id": "notes"}))
    administrator = app.state.access.identity("onebot", "101", "9000")
    ordinary = app.state.access.identity("onebot", "303", "9000")
    assert actions.available("qq:group:202", "303", ordinary) == []
    assert actions.available("qq:group:202", "303") == []
    available = actions.available("qq:group:202", "101", administrator)
    assert len(available) == 1
    alias = available[0]["function"]["name"]
    ordinary_source = app.state.store.add_message("qq:group:202", "303", "user", "保存他人的命令")
    with pytest.raises(ValueError):
        actions.propose(alias, {"text": "ordinary"}, "qq:group:202", "303", {"source_ids": [ordinary_source["id"]],
            "trusted_identity": ordinary, "is_owner": True, "is_admin": True})
    source = app.state.store.add_message("qq:group:202", "101", "user", "管理员请求保存便签")
    proposal = actions.propose(alias, {"text": "approved origin"}, "qq:group:202", "101", {
        "source_ids": [source["id"]], "trusted_identity": administrator})
    assert proposal["status"] == "pending_approval" and not external_calls
    admin_id = next(item["id"] for item in require_ok(client.get("/api/admins")) if item["account_id"] == "101")
    require_ok(client.delete("/api/admins/" + admin_id))
    assert not actions.available("qq:group:202", "101", administrator)
    result = require_ok(client.post(f"/api/tool-runs/{proposal['run_id']}/approve"))
    assert result["status"] == "cancelled" and not external_calls


def test_reference_attachment_does_not_leak_into_later_chat_history(experience, monkeypatch):
    client, app = experience
    require_ok(client.put("/api/settings", json={"model": {"base_url": "https://models.example/v1", "model": "fixture"}}))
    marker = "UNIQUE_EPHEMERAL_REFERENCE_684397"
    ref = require_ok(client.post("/api/chat/attachments", data={"purpose": "context"},
        files={"file": ("temporary.txt", marker.encode(), "text/plain")}))
    calls = []
    async def complete(messages, **kwargs):
        assert kwargs["before_send"]()
        calls.append(json.dumps(messages, ensure_ascii=False))
        return {"content": "已处理当前问题。", "usage": {"input_tokens": 20, "output_tokens": 8}}
    monkeypatch.setattr(app.state.provider, "complete", complete)
    require_ok(client.post("/api/chat", json={"text": "参考这个文件", "attachments": [ref["id"]]}))
    require_ok(client.post("/api/chat", json={"text": "另一个不需要附件的问题"}))
    assert marker in calls[0] and marker not in calls[1]
    assert marker not in json.dumps(app.state.store.list_messages("web:default"))
    assert require_ok(client.get("/api/memories")) == []


def test_attachment_deleted_before_model_send_invalidates_current_request(experience, monkeypatch):
    client, app = experience
    require_ok(client.put("/api/settings", json={"model": {"base_url": "https://models.example/v1", "model": "fixture"}}))
    ref = require_ok(client.post("/api/chat/attachments", data={"purpose": "context"},
        files={"file": ("temporary.txt", b"volatile reference", "text/plain")}))
    guards = []
    async def complete(messages, **kwargs):
        assert kwargs["before_send"]()
        (app.state.store.data_dir / "chat_attachments" / (ref["id"] + ".json")).unlink()
        try:
            current = kwargs["before_send"]()
        except (ValueError, KeyError, PermissionError):
            current = False
        guards.append(current)
        if current is not False:
            raise AssertionError("deleted attachment still passed the final send guard")
        raise ProviderError("参考附件已删除，未发送模型请求")
    monkeypatch.setattr(app.state.provider, "complete", complete)
    response = client.post("/api/chat", json={"text": "本轮参考文件", "attachments": [ref["id"]]})
    assert guards == [False]
    assert "已处理" not in response.text
    assert require_ok(client.get("/api/memories")) == []


@pytest.mark.anyio
async def test_run_close_waits_for_completion_delivery_callback(tmp_path):
    runs = RunManager(tmp_path / "runs")
    entered, release, delivered = asyncio.Event(), asyncio.Event(), []
    async def handler(ctx, payload):
        return {"reply": "finished"}
    async def on_complete(job):
        entered.set()
        await release.wait()
        delivered.append(job["id"])
    runs.register("fixture", handler)
    runs.on_complete = on_complete
    job = runs.submit("fixture", {}, identity={"platform": "local", "account_id": "shiori"})
    await asyncio.wait_for(entered.wait(), 1)
    closing = asyncio.create_task(runs.close())
    try:
        await asyncio.sleep(.02)
        assert not closing.done(), "close returned while completion still used storage"
    finally:
        release.set()
        await asyncio.wait_for(closing, 1)
    assert delivered == [job["id"]]


@pytest.mark.anyio
async def test_queued_run_cancel_is_terminal_before_handler_can_start(tmp_path):
    runs = RunManager(tmp_path / "queued")
    started = []
    async def handler(ctx, payload):
        started.append(True)
    runs.register("fixture", handler)
    job = runs.submit("fixture", {}, identity={"platform": "local", "account_id": "shiori"})
    assert runs.cancel(job["id"])["status"] == "cancelled"
    await runs.close()
    assert not started and RunManager(tmp_path / "queued").get(job["id"])["status"] == "cancelled"


def rewrite_zip(raw, name, value):
    stream = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(raw)) as source, zipfile.ZipFile(stream, "w", zipfile.ZIP_DEFLATED) as target:
        for entry in source.namelist():
            target.writestr(entry, json.dumps(value, ensure_ascii=False) if entry == name else source.read(entry))
    return stream.getvalue()


def test_backup_restores_job_results_as_readonly_without_host_file_capabilities(experience, monkeypatch):
    client, app = experience
    backup = client.get("/api/backup")
    assert backup.status_code == 200
    with zipfile.ZipFile(io.BytesIO(backup.content)) as archive:
        assert "jobs_history.json" in archive.namelist()
        manifest = json.loads(archive.read("manifest.json"))
        assert "jobs_history_read_only" in manifest["includes"]
    history = {"version": 1, "jobs": [
        {"id": "foreign-computer", "kind": "computer", "status": "completed", "scope_id": "web:default", "user_id": "owner",
            "title": "历史电脑任务", "created_at": "2026-01-01T00:00:00Z", "identity": {"proof": "must-not-restore"},
            "result": {"reply": "历史结果可以查看", "workspace": "C:/sensitive", "artifacts": [{"path": "secret.txt"}]}},
        {"id": "foreign-persona", "kind": "distillation", "status": "completed", "scope_id": "web:default", "user_id": "owner",
            "title": "历史人格任务", "created_at": "2026-01-01T00:00:00Z",
            "result": {"prompt": "历史草稿", "artifact": {"id": "a" * 32, "filename": "host-file.zip"}}},
    ]}
    restored = require_ok(client.post("/api/backup/restore", files={"file": ("backup.zip",
        rewrite_zip(backup.content, "jobs_history.json", history), "application/zip")}))
    assert restored["ok"] and app.state.settings.get()["paused"]
    assert not app.state.settings.get()["computer"]["enabled"]
    jobs = require_ok(client.get("/api/jobs"))
    assert len(jobs) == 2 and all(job["read_only"] for job in jobs)
    for job in jobs:
        assert job["identity"] == {} and job["approval"] is None
        assert not {"workspace", "artifact", "artifacts"}.intersection(job["result"])
        assert client.post(f"/api/jobs/{job['id']}/approve", json={"approval_id": "forged"}).status_code in (400, 403)
    computer_job = next(job for job in jobs if job["kind"] == "computer")
    persona_job = next(job for job in jobs if job["kind"] == "distillation")
    assert computer_job["result"]["reply"] == "历史结果可以查看"
    assert client.get(f"/api/jobs/{computer_job['id']}/files/0").status_code == 404
    assert client.get(f"/api/persona/distill/{persona_job['id']}/artifact").status_code == 400
    # A second restore is idempotent and never queues execution.
    require_ok(client.post("/api/backup/restore", files={"file": ("backup.zip",
        rewrite_zip(backup.content, "jobs_history.json", history), "application/zip")}))
    assert len(require_ok(client.get("/api/jobs"))) == 2
    assert not app.state.runs._tasks and not app.state.runs._contexts
    # Restoring rebuilds chat engines. Computer permissions must consult the
    # current social engine when the local administrator explicitly reenables it.
    async def complete(messages, **kwargs):
        assert kwargs["before_send"]()
        return {"content": "恢复后新任务正常完成"}
    monkeypatch.setattr(app.state.provider, "complete", complete)
    require_ok(client.put("/api/settings", json={"paused": False, "computer": {"enabled": True, 'runtime': 'local'}}))
    fresh = require_ok(client.post("/api/computer/jobs", json={"text": "恢复后检查新任务权限"}))
    assert wait_job(client, fresh["id"])["result"]["reply"] == "恢复后新任务正常完成"


def test_malformed_jobs_history_rejects_restore_before_current_data_changes(experience):
    client, app = experience
    memory = require_ok(client.post("/api/memories", json={"content": "恢复失败时必须保留的当前资料"}))
    backup = client.get("/api/backup")
    invalid = {"version": 1, "jobs": [{"id": "bad", "kind": "computer", "status": "running", "scope_id": "web:default"}]}
    response = client.post("/api/backup/restore", files={"file": ("bad.zip", rewrite_zip(backup.content,
        "jobs_history.json", invalid), "application/zip")})
    assert response.status_code == 400 and not app.state.settings.get()["paused"]
    assert app.state.store.get_memory(memory["id"])["status"] == "active"


def test_api_usage_keeps_unknown_price_distinct_from_real_token_usage(experience, monkeypatch):
    client, app = experience
    require_ok(client.put("/api/settings", json={"model": {"base_url": "https://models.example/v1", "model": "fixture"}}))
    real_async_client = httpx.AsyncClient
    requests = []
    def model_response(request):
        requests.append(request)
        return httpx.Response(200, json={"choices": [{"message": {"role": "assistant", "content": "连接成功"}}],
            "usage": {"prompt_tokens": 71, "completion_tokens": 9}})
    def client_factory(*args, **kwargs):
        return real_async_client(*args, **kwargs, transport=httpx.MockTransport(model_response))
    monkeypatch.setattr("qunyu.provider.httpx.AsyncClient", client_factory)
    tested = require_ok(client.post("/api/model/test", json={}))
    assert tested["usage"]["cost_known"] is False
    assert tested["usage"]["input_tokens"] == 71 and tested["usage"]["output_tokens"] == 9
    ledger = require_ok(client.get("/api/usage"))
    assert ledger[0]["cost_known"] is False and ledger[0]["cost"] == 0
    stats = require_ok(client.get("/api/bootstrap"))["stats"]
    assert stats["tokens_today"] == 80 and stats["input_tokens"] == 71 and stats["output_tokens"] == 9
    assert stats["cost_known_today"] is False and stats["unknown_cost_calls_today"] == 1
    assert len(requests) == 1 and requests[0].url.host == "models.example"
    require_ok(client.put("/api/settings", json={"model": {"input_price": 2, "output_price": 4}}))
    priced = require_ok(client.post("/api/model/test", json={}))
    assert priced["usage"]["cost_known"] is True and priced["usage"]["cost"] == pytest.approx(.000178)
    mixed = require_ok(client.get("/api/bootstrap"))["stats"]
    assert mixed["tokens_today"] == 160 and mixed["cost_today"] == pytest.approx(.000178)
    assert mixed["cost_known_today"] is False and mixed["unknown_cost_calls_today"] == 1
    # Pre-0.8 zero-cost records had no price marker; keep them visibly unknown.
    app.state.store.record_usage({"input_tokens": 3, "output_tokens": 2, "cost": 0})
    legacy = require_ok(client.get("/api/bootstrap"))["stats"]
    assert legacy["unknown_cost_calls_today"] == 2 and legacy["tokens_today"] == 165
    assert len(requests) == 2


def test_computer_close_waits_for_cancelled_thread_cleanup(experience, monkeypatch):
    client, app = experience
    require_ok(client.put("/api/settings", json={"computer": {"enabled": True, 'runtime': 'local', "approval_mode": "per_action"}}))
    entered, release, cleaned = threading.Event(), threading.Event(), threading.Event()
    async def complete(messages, **kwargs):
        return {"tool_calls": [{"id": "shell-fixture", "function": {"name": "execute_shell",
            "arguments": json.dumps({"command": "fixture only; never executes"})}}]}
    def process(ctx, root, name, value):
        entered.set()
        release.wait(2)
        cleaned.set()
        return {"ok": True, "output": "fixture worker cleaned"}
    monkeypatch.setattr(app.state.provider, "complete", complete)
    monkeypatch.setattr(app.state.computer, "_process", process)
    job = require_ok(client.post("/api/computer/jobs", json={"text": "运行一个模拟命令"}))
    approval = wait_job(client, job["id"], "awaiting_approval")["approval"]["id"]
    require_ok(client.post(f"/api/jobs/{job['id']}/approve", json={"approval_id": approval}))
    assert entered.wait(1)
    closing = client.portal.start_task_soon(app.state.runs.close)
    try:
        time.sleep(.03)
        assert not closing.done(), "shutdown returned before the command worker finished cleanup"
        assert not cleaned.is_set()
    finally:
        release.set()
        closing.result(2)
        assert cleaned.wait(1)
    assert require_ok(client.get(f"/api/jobs/{job['id']}"))["status"] == "interrupted"


def test_qq_approval_code_is_bound_to_one_step_and_originating_admin(experience, monkeypatch, tmp_path):
    client, app = experience
    app.state.settings.update({"onebot": {"enabled": True, "access_token": "fixture-reverse-token", "allowed_groups": ["202"],
        "allowed_users": ["101", "303"]}, "access": {"administrators": [
        {"platform": "onebot", "account_id": "101", "instance_id": "9000"},
        {"platform": "onebot", "account_id": "303", "instance_id": "9000"}]},
        "computer": {"enabled": True, 'runtime': 'local', "workspace": str(tmp_path / "qq-workspace"), "approval_mode": "per_action"}})
    sent, calls = [], []
    async def send(scope, text):
        sent.append((scope, text))
        return True
    async def complete(messages, **kwargs):
        calls.append(True)
        if len(calls) == 1:
            return {"tool_calls": [{"id": "write-" + name, "function": {"name": "write_file",
                "arguments": json.dumps({"path": name, "content": name})}} for name in ("first.txt", "second.txt")]}
        return {"content": "两个模拟文件已完成"}
    monkeypatch.setattr(app.state.bridge, "send", send)
    monkeypatch.setattr(app.state.provider, "complete", complete)
    identity = app.state.access.identity("onebot", "101", "9000")
    other = app.state.access.identity("onebot", "303", "9000")
    command = app.state.chat.handle_platform_command
    launched = client.portal.call(command, "qq:group:202", "101", "/执行 生成两个模拟文件", {"trusted_identity": identity})
    job_id = launched["job"]["id"]
    first = wait_job(client, job_id, "awaiting_approval")["approval"]["id"][:8]
    wrong = "00000000" if first != "00000000" else "11111111"
    for sender, account, text in ((other, "303", f"/确认 {job_id} {first}"),
        (identity, "101", f"/确认 {job_id}"), (identity, "101", f"/确认 {job_id} {wrong}")):
        client.portal.call(command, "qq:group:202", account, text, {"trusted_identity": sender})
        assert require_ok(client.get(f"/api/jobs/{job_id}"))["status"] == "awaiting_approval"
    client.portal.call(command, "qq:group:202", "101", f"/确认 {job_id} {first}", {"trusted_identity": identity})
    for _ in range(100):
        pending = require_ok(client.get(f"/api/jobs/{job_id}"))
        if pending.get("approval") and pending["approval"]["id"][:8] != first:
            break
        time.sleep(.01)
    assert pending["status"] == "awaiting_approval"
    second = pending["approval"]["id"][:8]
    assert second != first
    workspace = tmp_path / "qq-workspace"
    assert (workspace / "first.txt").exists() and not (workspace / "second.txt").exists()
    client.portal.call(command, "qq:group:202", "101", f"/确认 {job_id} {first}", {"trusted_identity": identity})
    assert require_ok(client.get(f"/api/jobs/{job_id}"))["status"] == "awaiting_approval"
    assert not (workspace / "second.txt").exists()
    client.portal.call(command, "qq:group:202", "101", f"/确认 {job_id} {second}", {"trusted_identity": identity})
    assert wait_job(client, job_id)["result"]["reply"] == "两个模拟文件已完成"
    assert len(calls) == 2
    assert any(f"/确认 {job_id} {first}" in text for _, text in sent)
    assert any(f"/确认 {job_id} {second}" in text for _, text in sent)


def test_successful_mcp_approval_creates_summary_from_actual_receipt_once(experience, monkeypatch):
    client, app = experience
    app.state.settings.update({"mcp_servers": [{"id": "notes", "name": "Fixture notes", "url": "http://127.0.0.1:5678/mcp",
        "enabled": True, "allowed_tools": ["append_note"]}]})
    app.state.store.upsert_scope({"id": "web:summary-test", "allow_mcp": True, "allow_tools": True})
    schema = {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"], "additionalProperties": False}
    actions, external_calls, summaries = app.state.chat.tool_actions, [], []
    async def discover(config):
        return {"tools": [{"name": "append_note", "inputSchema": schema, "schema_hash": schema_hash(schema)}]}
    async def call(config, name, arguments, expected_schema_hash, before_call):
        before_call()
        external_calls.append(arguments)
        return {"isError": False, "text": "fixture-receipt-3141: 实际保存一条", "content": [{"type": "text", "text": "fixture-receipt-3141: 实际保存一条"}]}
    async def complete(messages, **kwargs):
        assert kwargs["purpose"] == "tool_summary" and kwargs["before_send"]()
        summaries.append(json.loads(messages[-1]["content"]))
        return {"content": "根据 fixture-receipt-3141，已实际保存一条。"}
    monkeypatch.setattr(actions.client, "discover", discover)
    monkeypatch.setattr(actions.client, "call", call)
    monkeypatch.setattr(app.state.provider, "complete", complete)
    require_ok(client.post("/api/mcp/discover", json={"server_id": "notes"}))
    source = app.state.store.add_message("web:summary-test", "owner", "user", "把本次结论保存一条")
    alias = actions.available("web:summary-test", "owner")[0]["function"]["name"]
    proposal = actions.propose(alias, {"text": "本次结论"}, "web:summary-test", "owner", {"source_ids": [source["id"]]})
    result = require_ok(client.post(f"/api/tool-runs/{proposal['run_id']}/approve"))
    assert result["status"] == "succeeded" and result["job"]["kind"] == "tool_summary"
    completed = wait_job(client, result["job"]["id"])
    assert completed["scope_id"] == "web:summary-test"
    assert summaries == [{"request": ["把本次结论保存一条"], "tool": "append_note", "status": "succeeded",
        "result": result["result"], "error": None}]
    assert client.post(f"/api/tool-runs/{proposal['run_id']}/approve").status_code == 400
    assert external_calls == [{"text": "本次结论"}] and len(summaries) == 1
    messages = app.state.store.list_messages("web:summary-test")
    assert any(message.get("metadata", {}).get("job_id") == completed["id"] and "fixture-receipt-3141" in message["content"] for message in messages)
    assert not app.state.store.list_messages("web:default")


def test_computer_attachment_removal_revokes_waiting_action(experience, monkeypatch, tmp_path):
    client, app = experience
    workspace = tmp_path / "workspace"
    require_ok(client.put("/api/settings", json={"computer": {"enabled": True, 'runtime': 'local', "workspace": str(workspace), "approval_mode": "per_action"}}))
    ref = require_ok(client.post("/api/chat/attachments", data={"purpose": "context"},
        files={"file": ("temporary.txt", b"temporary input for this task", "text/plain")}))
    async def complete(messages, **kwargs):
        return {"tool_calls": [{"id": "write-revoked", "function": {"name": "write_file",
            "arguments": json.dumps({"path": "revoked.txt", "content": "must not write after source removal"})}}]}
    monkeypatch.setattr(app.state.provider, "complete", complete)
    job = require_ok(client.post("/api/computer/jobs", json={"text": "根据参考资料生成文件", "attachments": [ref["id"]]}))
    approval = wait_job(client, job["id"], "awaiting_approval")["approval"]["id"]
    require_ok(client.delete(f"/api/chat/attachments/{ref['id']}"))
    assert client.post(f"/api/jobs/{job['id']}/approve", json={"approval_id": approval}).status_code == 403
    wait_job(client, job["id"], "failed")
    assert not (workspace / "revoked.txt").exists()


def test_failed_distillation_reports_failure_in_chat_instead_of_a_ready_draft(experience, monkeypatch):
    client, app = experience
    upload = require_ok(client.post("/api/persona/distill/corpus", files={"file": ("corpus.zip", corpus_zip(), "application/zip")}))
    async def complete(messages, **kwargs):
        raise ProviderError("fixture-model-unavailable")
    monkeypatch.setattr(app.state.provider, "complete", complete)
    job = require_ok(client.post("/api/persona/distill/start", json={"mode": "corpus", "upload_id": upload["upload_id"],
        "target_speaker": "目标", "confirmed": True}))
    failed = wait_job(client, job["id"], "failed")
    assert failed["error"] == "fixture-model-unavailable"
    messages = app.state.store.list_messages("web:default")
    result = next(message["content"] for message in messages if message.get("metadata", {}).get("job_id") == job["id"])
    assert "fixture-model-unavailable" in result and "已生成" not in result
    assert client.post(f"/api/persona/distill/{job['id']}/apply", json={}).status_code == 400
