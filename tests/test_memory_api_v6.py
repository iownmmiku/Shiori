"""Authenticated management routes preserve review and restore boundaries."""
import io
import json
import zipfile

import pytest
from fastapi.testclient import TestClient

from shiori.api import create_app
from auth_helpers import password_login


@pytest.fixture
def client(tmp_path):
    app = create_app(tmp_path / "memory-api-v6", start_workers=False)
    with TestClient(app) as connection:
        password_login(connection, app)
        yield connection, app


def fact(connection, content="pianoclub 音乐社资料", **fields):
    response = connection.post("/api/memories", json={"content": content, **fields})
    assert response.status_code == 200, response.text
    return response.json()


def propose(connection, left, right, **fields):
    return connection.post("/api/memory/relations", json={"from_id": left["id"], "to_id": right["id"],
        "relation_type": "event_link", "label": "共同创作活动", **fields})


def review_relation(connection, edge, action, **fields):
    return connection.post(f"/api/memory/relations/{edge['id']}/review", json={"action": action, **fields})


def organization(connection, operation="merge_duplicate"):
    response = connection.post("/api/memory/organization", json={})
    assert response.status_code == 200, response.text
    return next(item for item in response.json()["suggestions"] if item["details"]["operation"] == operation)


def review_organization(connection, suggestion, action, **fields):
    return connection.post(f"/api/memory/organization/{suggestion['id']}/review", json={"action": action, **fields})


def rewrite_backup(raw, name, value):
    buffer = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(raw)) as source, zipfile.ZipFile(buffer, "w") as target:
        for entry in source.namelist():
            target.writestr(entry, json.dumps(value) if entry == name else source.read(entry))
    return buffer.getvalue()


def test_relation_routes_need_admin_session_and_preserve_review_metadata(client):
    connection, app = client
    left = fact(connection)
    right = fact(connection, "星期六下午排练", kind="event")
    source = app.state.store.add_message("web:default", "owner", "user", "在音乐社进行周六排练")
    response = propose(connection, left, right, source_ids=[source["id"]], status="active", shareable=True)
    assert response.status_code == 200, response.text
    edge = response.json()
    assert edge["status"] == "pending" and edge["shareable"] is False
    assert edge["dependencies"] == {left["id"]: 1, right["id"]: 1}
    assert edge["source_fingerprints"][source["id"]]
    assert edge["endpoint_identities"][left["id"]] == ["web:default", "owner"]
    assert connection.get("/api/memory/relations", params={"status": "pending"}).json()[0]["id"] == edge["id"]
    assert not connection.get("/api/memory/search", params={"q": "pianoclub"}).json()["related"]
    assert review_relation(connection, edge, "approve").status_code == 200
    result = connection.get("/api/memory/search", params={"q": "pianoclub"}).json()
    linked = next(item for item in result["items"] if item["id"] == right["id"])
    assert linked["related"] and linked["memory_versions"][edge["id"]] == 2
    assert linked["path_ids"] == [left["id"], edge["id"], right["id"]]
    assert review_relation(connection, edge, "wait").status_code == 400
    connection.cookies.clear()
    assert connection.get("/api/memory/relations").status_code == 401
    assert review_relation(connection, edge, "reject").status_code == 401


def test_relation_source_validation_rejects_cross_scope_other_user_and_missing_ids(client):
    connection, app = client
    left, right = fact(connection), fact(connection, "星期六排练", kind="event")
    other_scope = app.state.store.add_message("web:other", "owner", "user", "另一个聊天的内容")
    other_user = app.state.store.add_message("web:default", "someone-else", "user", "他人的私聊内容")
    for source in (other_scope, other_user):
        result = propose(connection, left, right, source_ids=[source["id"]])
        assert result.status_code == 403, result.text
    assert propose(connection, left, right, source_ids=["missing-source"]).status_code == 404
    assert propose(connection, left, right, source_ids="not-a-list").status_code == 400
    assert propose(connection, left, right, source_ids=["id"] * 21).status_code == 400
    assert connection.get("/api/memory/relations").json() == []


def test_relation_wrong_scope_or_private_user_cannot_review_and_endpoint_edit_wins(client):
    connection, app = client
    left, right = fact(connection), fact(connection, "星期六排练", kind="event")
    edge = propose(connection, left, right).json()
    assert review_relation(connection, edge, "approve", user_id="other").status_code == 403
    assert review_relation(connection, edge, "reject", scope_id="web:other").status_code == 403
    assert app.state.store.get_memory(edge["id"])["status"] == "pending"
    assert connection.put(f"/api/memories/{left['id']}", json={"content": "更正后的社团资料"}).status_code == 200
    assert review_relation(connection, edge, "approve").status_code == 400
    assert connection.get("/api/memory/relations").json() == []


def test_relation_rejection_is_terminal_and_clearing_source_blocks_approval(client):
    connection, app = client
    left, right = fact(connection), fact(connection, "星期六排练", kind="event")
    edge = propose(connection, left, right).json()
    assert review_relation(connection, edge, "reject").json()["status"] == "rejected"
    assert review_relation(connection, edge, "approve").status_code == 400
    assert connection.get("/api/memory/relations", params={"status": "rejected"}).json()[0]["id"] == edge["id"]
    assert propose(connection, left, right).json()["id"] == edge["id"]
    source = app.state.store.add_message("web:default", "owner", "user", "新的明确关联来源")
    other_edge = propose(connection, left, right, relation_type="related", source_ids=[source["id"]]).json()
    assert connection.delete("/api/messages").status_code == 200
    assert review_relation(connection, other_edge, "approve").status_code == 404
    assert app.state.store.get_memory(other_edge["id"])["status"] == "pending"


def test_relation_analysis_route_creates_pending_only_using_available_ids(client, monkeypatch):
    connection, app = client
    left, right = fact(connection), fact(connection, "星期六排练", kind="event")
    calls = []

    async def analyze(messages, **kwargs):
        calls.append(kwargs)
        assert kwargs["before_send"]()
        return {"content": json.dumps([{"from_id": left["id"], "to_id": right["id"], "relation_type": "event_link", "label": "明确的活动关联"},
            {"from_id": "fabricated", "to_id": right["id"], "label": "不应保存"}])}

    monkeypatch.setattr(app.state.provider, "complete", analyze)
    response = connection.post("/api/memory/relations/analyze", json={})
    assert response.status_code == 200, response.text
    assert calls[0]["purpose"] == "memory_extract"
    assert response.json()["created"] == 1
    assert response.json()["candidates"][0]["status"] == "pending"
    assert connection.get("/api/memory/relations", params={"status": "active"}).json() == []


def test_organization_routes_enforce_review_ownership_and_revert_once(client, monkeypatch):
    connection, app = client
    # Equal clock values deliberately make the second inserted ID win the
    # deterministic tie-break. Apply the candidate's reviewed keep_id.
    monkeypatch.setattr("qunyu.db.utc_now", lambda: "2026-01-01T00:00:00.000000Z")
    first = fact(connection, id="z-created-first")
    second = fact(connection, id="a-created-second")
    suggestion = organization(connection)
    keep_id = suggestion["details"]["keep_id"]
    archived_id = next(item["id"] for item in (first, second) if item["id"] != keep_id)
    assert keep_id == second["id"]
    assert connection.get("/api/memory/organization").json()[0]["details"]["status"] == "pending"
    assert app.state.store.get_memory(second["id"])["status"] == "active"
    assert review_organization(connection, suggestion, "approve", user_id="other").status_code == 403
    assert review_organization(connection, suggestion, "approve", scope_id="web:other").status_code == 403
    assert review_organization(connection, suggestion, "wait").status_code == 400
    approved = review_organization(connection, suggestion, "approve")
    assert approved.status_code == 200, approved.text
    assert approved.json()["details"]["result_versions"][archived_id] == 2
    assert app.state.store.get_memory(archived_id)["merged_into"] == keep_id
    assert app.state.store.get_memory(keep_id)["version"] == 1
    assert review_organization(connection, suggestion, "approve").status_code == 400
    assert review_organization(connection, suggestion, "revert").json()["details"]["status"] == "reverted"
    assert app.state.store.get_memory(archived_id)["version"] == 3
    assert app.state.store.get_memory(archived_id)["status"] == "active"
    assert app.state.store.get_memory(keep_id)["version"] == 1
    assert review_organization(connection, suggestion, "revert").status_code == 400


def test_organization_routes_reject_without_changes_and_hide_deleted_preview(client):
    connection, app = client
    first, second = fact(connection), fact(connection)
    suggestion = organization(connection)
    assert review_organization(connection, suggestion, "reject").json()["details"]["status"] == "rejected"
    assert all(app.state.store.get_memory(item["id"])["status"] == "active" for item in (first, second))
    assert connection.post("/api/memory/organization", json={}).json()["created"] == 0
    assert connection.delete(f"/api/memories/{first['id']}").status_code == 200
    assert connection.get("/api/memory/organization").json() == []
    assert "before" not in app.state.store._get("events", suggestion["id"])["details"]


def test_partial_persona_nested_edits_preserve_existing_overrides_and_allow_inheritance(client):
    connection, app = client
    saved = connection.put("/api/personas/default", json={"system_prompt": "自定义人格需要保留", "interests": ["合成音乐"],
        "exploration": {"interval_hours": 48, "daily_searches": 0, "valid_days": 7}, "excluded_topics": ["剧情剧透"]})
    assert saved.status_code == 200, saved.text
    edited = connection.put("/api/personas/default", json={"exploration": {"interval_hours": 24}})
    assert edited.status_code == 200, edited.text
    data = edited.json()
    assert data["name"] == "初音未来" and data["system_prompt"] == "自定义人格需要保留"
    assert data["interests"] == ["合成音乐"] and data["excluded_topics"] == ["剧情剧透"]
    assert data["exploration"] == {"interval_hours": 24, "daily_searches": 0, "valid_days": 7}
    inherited = connection.put("/api/personas/default", json={"exploration": {"daily_searches": None}})
    assert inherited.status_code == 200
    assert inherited.json()["exploration"] == {"interval_hours": 24, "daily_searches": None, "valid_days": 7}
    before = app.state.store.get_persona("default")
    for payload in ({"exploration": {"valid_days": True}}, {"exploration": {"unknown": 3}}, {"excluded_topics": [123]}):
        assert connection.put("/api/personas/default", json=payload).status_code == 400
        assert app.state.store.get_persona("default") == before


def test_backup_restores_review_state_graph_dependencies_and_reversible_organization(client):
    connection, app = client
    left = fact(connection)
    right = fact(connection, "星期六排练", kind="event")
    source = app.state.store.add_message("web:default", "owner", "user", "社团的排练计划")
    edge = propose(connection, left, right, source_ids=[source["id"]]).json()
    assert review_relation(connection, edge, "approve").status_code == 200
    # Consolidate an unrelated pair: archiving a graph endpoint would correctly
    # revoke its edge, so this restore test must not depend on which UUID wins.
    duplicate_pair = [fact(connection, "独立的重复课程笔记") for _ in range(2)]
    suggestion = organization(connection)
    archived_id = next(item["id"] for item in duplicate_pair if item["id"] != suggestion["details"]["keep_id"])
    assert review_organization(connection, suggestion, "approve").status_code == 200
    backup = connection.get("/api/backup")
    with zipfile.ZipFile(io.BytesIO(backup.content)) as archive:
        assert "tool_history.json" in archive.namelist()
        assert "tool_history_read_only" in json.loads(archive.read("manifest.json"))["includes"]
    fact(connection, "恢复前新建、备份中不存在的记忆")
    restored = connection.post("/api/backup/restore", files={"file": ("memory-v6.zip", backup.content, "application/zip")})
    assert restored.status_code == 200, restored.text
    assert app.state.settings.get()["paused"]
    assert app.state.store.get_memory(edge["id"])["source_fingerprints"][source["id"]]
    assert app.state.store.get_memory(archived_id)["status"] == "archived"
    assert connection.put("/api/settings", json={"paused": False}).status_code == 200
    linked = connection.get("/api/memory/search", params={"q": "pianoclub"}).json()["related"]
    assert linked and linked[0]["memory_versions"][edge["id"]] == 2
    assert connection.get("/api/memory/organization").json()[0]["details"]["status"] == "approved"
    assert review_organization(connection, suggestion, "revert").status_code == 200
    assert app.state.store.get_memory(archived_id)["status"] == "active"


def test_invalid_tool_history_precheck_keeps_live_memory_settings_and_review_state(client):
    connection, app = client
    first, second = fact(connection), fact(connection)
    suggestion = organization(connection)
    backup = connection.get("/api/backup").content
    live = fact(connection, "备份之后的新记忆")
    settings_before = app.state.settings.get()
    malformed = rewrite_backup(backup, "tool_history.json", {"version": 1, "runs": [{"id": "bad", "status": "execute-now"}]})
    response = connection.post("/api/backup/restore", files={"file": ("malformed.zip", malformed, "application/zip")})
    assert response.status_code == 400, response.text
    assert app.state.settings.get() == settings_before
    assert app.state.store.get_memory(live["id"])["content"] == live["content"]
    assert app.state.store._get("events", suggestion["id"])["details"]["status"] == "pending"
