"""Thread-safe, local SQLite storage for mikubot.

JSON records retain forward-compatible fields; indexed columns enforce the
boundaries used by chat, memory retrieval and the reminder scheduler.
"""

from __future__ import annotations

import json
import logging
import traceback
import hashlib
import math
import os
from pathlib import Path
import re
import sqlite3
import threading
import unicodedata
from collections import OrderedDict
from datetime import date, datetime, timedelta, timezone
from typing import Any
from uuid import uuid4
from .timezones import config_zone, ingress_times


UTC = timezone.utc
HONG_KONG = timezone(timedelta(hours=8))
MEMORY_KINDS = {"fact", "preference", "event", "note"}
MEMORY_STATUSES = {"active", "pending", "rejected", "archived", "superseded", "deleted"}
JARGON_STATUSES = {"pending", "approved", "rejected", "disabled", "deleted"}
TASK_STATUSES = {"pending", "running", "completed", "cancelled", "failed"}
BLOCKED_SOURCE_CACHE_SCOPES = 128
SECRET_FIELDS = {
    "api_key", "access_token", "password", "secret", "authorization",
    "client_secret", "refresh_token", "auth_token", "bootstrap_token",
    "app_secret",
    "token",
}
TABLES = (
    "scopes", "personas", "messages", "memories", "memory_versions",
    "jargon", "jargon_versions", "tasks", "usage", "events",
)


def _memory_property(record: dict) -> str:
    """Conservative single-value identities for legacy explicit self-facts."""
    raw = str(record.get("property") or "").strip()
    aliases = {"居住城市": "residence_city", "居住地": "residence_city", "现居城市": "residence_city",
               "residence_city": "residence_city", "生日": "birthday", "birthday": "birthday",
               "偏好称呼": "preferred_name", "称呼偏好": "preferred_name", "preferred_name": "preferred_name"}
    if raw:
        return aliases.get(unicodedata.normalize("NFKC", raw).casefold(), raw)
    if record.get("kind") not in {"fact", "preference"}:
        return ""
    text = str(record.get("content") or "").strip()
    if any(marker in text for marker in ("?", "？", "如果", "假如", "可能", "也许", "听说", "朋友", "同事", "以前", "曾经", "不是", "不住", "不在", "哪", "吗")):
        return ""
    if re.fullmatch(r"我(?:现在|目前)?(?:住在|居住在|住|搬到)(?:了)?[^\n。！？；;，,]{1,40}[。！]?", text):
        return "residence_city"
    if re.fullmatch(r"我(?:现在|目前)?的?生日(?:是|在)[^\n。！？；;，,]{1,40}[。！]?", text):
        return "birthday"
    if re.fullmatch(r"(?:我)?希望被称呼为[：:\s]*[^\n。！？；;，,]{1,30}[。！]?", text):
        return "preferred_name"
    return ""


def _memory_view(record: dict) -> dict:
    # Reading old databases need not mutate their version history just to gain
    # a conservative property label for conflict checks and the admin UI.
    record = dict(record)
    record['pinned'] = record.get('pinned') is True
    for key, default in (('importance', 0.5), ('confidence', 0.5)):
        value = record.get(key)
        record[key] = float(value) if type(value) in (int, float) and math.isfinite(value) and 0 <= value <= 1 else default
    for key, default in (('content', ''), ('subject', ''), ('updated_at', ''), ('created_at', '')):
        if not isinstance(record.get(key), str): record[key] = default
    for key, default in (('source_ids', []), ('dependencies', {})):
        if not isinstance(record.get(key), type(default)): record[key] = default
    record.setdefault('version', 1)
    if record.get("status") != "deleted":
        property_name = _memory_property(record)
        if property_name:
            record["property"] = property_name
    return record


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


def message_fingerprint(record: dict) -> str:
    """Fingerprint source material without storing another copy of its text."""
    payload = {key: record.get(key) for key in ("id", "scope_id", "user_id", "role", "content", "created_at", "metadata")}
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _iso(value: Any, field: str, *, nullable: bool = True) -> str | None:
    if value is None or value == "":
        if nullable:
            return None
        raise ValueError(f"{field} 不能为空")
    if not isinstance(value, str):
        raise ValueError(f"{field} 必须为 ISO 时间字符串")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return parsed.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")
    except (ValueError, OverflowError) as exc:
        raise ValueError(f"{field} 不是有效时间") from exc


def _text(value: Any, field: str, *, required: bool = True) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field} 必须为字符串")
    result = value.strip()
    if required and not result:
        raise ValueError(f"{field} 不能为空")
    return result


def _choice(value: Any, allowed: set[str], field: str) -> str:
    if not isinstance(value, str) or value not in allowed:
        raise ValueError(f"{field} 必须为 {' / '.join(sorted(allowed))}")
    return value


def _boolean(value: Any, field: str) -> bool:
    if not isinstance(value, bool):
        raise ValueError(f"{field} 必须为布尔值")
    return value


def _number(value: Any, field: str, *, minimum: float = 0, maximum: float | None = None) -> float:
    if isinstance(value, bool) or not isinstance(value, (float, int)):
        raise ValueError(f"{field} 必须为数字")
    if not math.isfinite(value) or value < minimum or (maximum is not None and value > maximum):
        raise ValueError(f"{field} 超出有效范围")
    return float(value)


def _strings(value: Any, field: str) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ValueError(f"{field} 必须为字符串列表")
    return list(dict.fromkeys(item.strip() for item in value if item.strip()))


def _json(value: Any) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
    except (TypeError, ValueError) as exc:
        raise ValueError("记录必须为可序列化 JSON") from exc


def _data(value: Any) -> dict:
    if not isinstance(value, dict):
        raise ValueError("记录必须为对象")
    return json.loads(_json(value))


def _redact(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _redact(item) for key, item in value.items() if key.casefold() not in SECRET_FIELDS}
    if isinstance(value, list):
        return [_redact(item) for item in value]
    return value


def _limit(value: Any, maximum: int = 1000) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError("limit 必须为正整数")
    return min(value, maximum)


class Store:
    """A single connection serialized with an RLock, safe across API threads."""

    def __init__(self, data_dir: Path):
        self._timezone_settings = None
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.path = self.data_dir / "qunyu.db"
        self.created = not self.path.exists()
        self.db_path = self.path
        self._lock = threading.RLock()
        self._closed = False
        self._memory_search = None
        self._memory_search_dirty = set()
        self._memory_revision = 0
        self._memory_search_warning = ''
        self._blocked_source_cache = OrderedDict()
        self._blocked_source_data_version = None
        self._conn = sqlite3.connect(self.path, check_same_thread=False, timeout=30)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.execute("PRAGMA busy_timeout=30000")
        self._conn.executescript("""
            CREATE TABLE IF NOT EXISTS scopes (id TEXT PRIMARY KEY, data TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS personas (id TEXT PRIMARY KEY, data TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS messages (
                id TEXT PRIMARY KEY, scope_id TEXT NOT NULL, user_id TEXT NOT NULL,
                role TEXT NOT NULL, created_at TEXT NOT NULL, data TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS messages_scope_time ON messages(scope_id, created_at);
            CREATE INDEX IF NOT EXISTS messages_time ON messages(created_at);
            CREATE INDEX IF NOT EXISTS messages_extraction_cursor ON messages(scope_id,user_id,role,created_at);
            CREATE INDEX IF NOT EXISTS messages_origin_time ON messages(scope_id,user_id,role,
                COALESCE(json_extract(data,'$.sent_at'),json_extract(data,'$.received_at'),created_at));
            CREATE INDEX IF NOT EXISTS messages_origin_day ON messages(scope_id,user_id,role,
                julianday(COALESCE(json_extract(data,'$.sent_at'),json_extract(data,'$.received_at'),created_at)));
            CREATE TABLE IF NOT EXISTS memories (
                id TEXT PRIMARY KEY, scope_id TEXT NOT NULL, user_id TEXT NOT NULL,
                kind TEXT NOT NULL, status TEXT NOT NULL, visibility TEXT NOT NULL,
                valid_from TEXT, valid_to TEXT, version INTEGER NOT NULL,
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL, data TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS memories_scope ON memories(scope_id, status, user_id);
            CREATE INDEX IF NOT EXISTS memories_preference_rank ON memories(
                scope_id, user_id, kind, status, (json_extract(data,'$.pinned') IS 1) DESC,
                (CASE WHEN json_type(data,'$.importance') IN ('integer','real')
                  AND json_extract(data,'$.importance') BETWEEN 0 AND 1
                  THEN json_extract(data,'$.importance') ELSE 0.5 END) DESC, updated_at DESC, id DESC);
            CREATE TABLE IF NOT EXISTS memory_versions (
                memory_id TEXT NOT NULL, version INTEGER NOT NULL, created_at TEXT NOT NULL,
                data TEXT NOT NULL, PRIMARY KEY(memory_id, version));
            CREATE TABLE IF NOT EXISTS jargon (
                id TEXT PRIMARY KEY, scope_id TEXT NOT NULL, term TEXT NOT NULL,
                status TEXT NOT NULL, version INTEGER NOT NULL, created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL, data TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS jargon_scope ON jargon(scope_id, status, term);
            CREATE TABLE IF NOT EXISTS jargon_versions (
                jargon_id TEXT NOT NULL, version INTEGER NOT NULL, created_at TEXT NOT NULL,
                data TEXT NOT NULL, PRIMARY KEY(jargon_id, version));
            CREATE TABLE IF NOT EXISTS tasks (
                id TEXT PRIMARY KEY, scope_id TEXT NOT NULL, status TEXT NOT NULL,
                due_at TEXT, idempotency_key TEXT UNIQUE, created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL, data TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS tasks_due ON tasks(status, due_at);
            CREATE TABLE IF NOT EXISTS usage (
                id TEXT PRIMARY KEY, created_at TEXT NOT NULL, input_tokens INTEGER NOT NULL,
                output_tokens INTEGER NOT NULL, cost REAL NOT NULL, data TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS usage_time ON usage(created_at);
            CREATE TABLE IF NOT EXISTS events (
                id TEXT PRIMARY KEY, created_at TEXT NOT NULL, kind TEXT NOT NULL, data TEXT NOT NULL);
        """)
        self._conn.commit()
        self._conn.execute("PRAGMA user_version=1")
        self._seed()
        self._init_blocked_source_tracking()

    def _init_blocked_source_tracking(self) -> None:
        """Transaction-aware, connection-local invalidation; no backup migration.

        Only scopes in the bounded LRU are tracked. TEMP triggers also cover
        direct SQL on this connection, and their counters roll back with the
        main write. Other connections are covered by PRAGMA data_version.
        """
        self._blocked_source_cache.clear()
        self._blocked_source_data_version = None
        self._conn.execute("CREATE TEMP TABLE IF NOT EXISTS blocked_source_revisions "
                           "(scope_id TEXT PRIMARY KEY, revision INTEGER NOT NULL)")
        self._conn.execute("DELETE FROM temp.blocked_source_revisions")
        for table in ('memories', 'jargon'):
            for operation in ('INSERT', 'UPDATE', 'DELETE'):
                name = f'blocked_source_{table}_{operation.lower()}'
                self._conn.execute(f'DROP TRIGGER IF EXISTS temp.{name}')
                scope = ('scope_id IN (OLD.scope_id, NEW.scope_id)' if operation == 'UPDATE'
                         else 'scope_id=' + ('OLD' if operation == 'DELETE' else 'NEW') + '.scope_id')
                self._conn.execute(f"""CREATE TEMP TRIGGER {name}
                    AFTER {operation} ON main.{table} BEGIN
                    UPDATE blocked_source_revisions SET revision=revision+1 WHERE {scope};
                    END""")
        self._conn.commit()

    def _seed(self) -> None:
        # Keep the migration marker in an existing table so schema-1 backups
        # remain readable and restoring an old snapshot runs this once again.
        legacy = [
            {"id": "default", "name": "温柔陪伴", "description": "认真听你说话的日常伙伴",
             "system_prompt": "你是群屿，一个温暖自然的聊天伙伴。认真理解上下文，简洁真诚，不编造记忆。", "color": "#52735b"},
            {"id": "quiet", "name": "务实助手", "description": "安静可靠，帮助整理与完成任务",
             "system_prompt": "你是群屿的务实助手。表达简明，关注事实、任务和结果，不确定时说明。", "color": "#607484"},
            {"id": "lively", "name": "群聊搭子", "description": "轻松开朗，懂得适时接话",
             "system_prompt": "你是群屿的群聊伙伴。自然开朗，适时接话，尊重每个人，不强行玩梗或刷屏。", "color": "#bd9055"},
        ]
        default = {"id": "default", "name": "初音未来", "description": "喜欢音乐、温暖活泼的日常伙伴",
                   "system_prompt": "你以初音未来的人格与大家交流：温暖、活泼、体贴，喜欢音乐与创作。自然理解语境，尊重每个人的边界，不强行卖萌或刷屏。对事实认真核实，不编造经历、记忆或已完成的事情；不确定时坦诚说明。需要时能安静倾听，也能帮助整理和完成任务。", "color": "#39c5bb",
                   "interests": ["音乐", "动画"], "exploration_enabled": False}
        marker = "mikubot_persona_seed_v1"
        with self._lock, self._conn:
            if not self._conn.execute("SELECT 1 FROM events WHERE kind=?", (marker,)).fetchone():
                removed = []
                for persona in legacy:
                    try:
                        current = self.get_persona(persona["id"])
                    except KeyError:
                        continue
                    # Exact equality intentionally preserves even small user
                    # changes, custom extensions and user-created personas.
                    if current != persona:
                        continue
                    if persona["id"] == "default":
                        self._write("personas", default, ())
                    else:
                        self._conn.execute("DELETE FROM personas WHERE id=?", (persona["id"],))
                        removed.append(persona["id"])
                for scope in self.list_scopes():
                    if scope.get("persona_id") in removed:
                        self._write("scopes", {**scope, "persona_id": "default"}, ())
                self.add_event(marker, "默认人格模板已迁移；保留自建及改写人格", removed_personas=removed)
                self._conn.execute("INSERT OR IGNORE INTO personas(id,data) VALUES (?,?)", (default["id"], _json(default)))
            if self._conn.execute("SELECT 1 FROM scopes WHERE id=?", ("web:default",)).fetchone() is None:
                self.upsert_scope({"id": "web:default"})

    def _get(self, table: str, record_id: str) -> dict:
        with self._lock:
            row = self._conn.execute(f"SELECT data FROM {table} WHERE id=?", (record_id,)).fetchone()
            if row is None:
                raise KeyError(record_id)
            return json.loads(row["data"])

    def _list(self, table: str, clauses: list[str] | None = None, params: list | None = None, order: str = "rowid") -> list[dict]:
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        with self._lock:
            rows = self._conn.execute(f"SELECT data FROM {table}{where} ORDER BY {order}", params or []).fetchall()
            return [json.loads(row["data"]) for row in rows]

    def _write(self, table: str, record: dict, columns: tuple[str, ...]) -> dict:
        names = ("id", *columns, "data")
        values = (record["id"], *(record.get(key) for key in columns), _json(record))
        placeholders = ",".join("?" for _ in names)
        updates = ",".join(f"{name}=excluded.{name}" for name in names if name != "id")
        self._conn.execute(
            f"INSERT INTO {table}({','.join(names)}) VALUES ({placeholders}) ON CONFLICT(id) DO UPDATE SET {updates}", values
        )
        if table == 'memories':
            self._memory_revision += 1
            if self._memory_search_dirty is not None:
                self._memory_search_dirty.add(record['id'])
                if len(self._memory_search_dirty) > 4096:
                    self._memory_search_dirty = None
        return json.loads(values[-1])

    def configure_timezone(self, settings):
        self._timezone_settings = settings

    def stats(self) -> dict:
        with self._lock:
            result = {}
            for name in ("messages", "memories", "jargon", "tasks"):
                condition = " WHERE status!='deleted'" if name in {"memories", "jargon"} else ""
                result[name] = self._conn.execute(f"SELECT COUNT(*) FROM {name}{condition}").fetchone()[0]
            result["sessions"] = self._conn.execute("SELECT COUNT(*) FROM scopes").fetchone()[0]
            totals = self._conn.execute("SELECT COALESCE(SUM(input_tokens),0),COALESCE(SUM(output_tokens),0) FROM usage").fetchone()
            result["input_tokens"], result["output_tokens"] = totals
            today = self.usage_totals()
            result["cost_today"] = today["cost"]
            result["tokens_today"] = today["tokens"]
            zone = config_zone(self._timezone_settings)
            start = datetime.combine(datetime.now(zone).date(), datetime.min.time(), tzinfo=zone)
            unknown = self._conn.execute(
                "SELECT COUNT(*) FROM usage WHERE created_at>=? AND created_at<? "
                "AND (json_extract(data,'$.cost_known')=0 OR "
                "(json_extract(data,'$.cost_known') IS NULL AND cost=0))",
                (_iso(start.isoformat(), "day"), _iso((start + timedelta(days=1)).isoformat(), "day")),
            ).fetchone()[0]
            result["unknown_cost_calls_today"] = unknown
            result["cost_known_today"] = unknown == 0
            return result

    def list_scopes(self) -> list[dict]:
        resolve = getattr(self, 'scope_resolver', lambda value: value)
        return [resolve(value) for value in self._list("scopes")]

    def get_scope(self, scope_id: str) -> dict:
        scope_id = _text(scope_id, "scope_id")
        with self._lock:
            try:
                return getattr(self, 'scope_resolver', lambda value: value)(self._get("scopes", scope_id))
            except KeyError:
                return self.upsert_scope({"id": scope_id})

    def discover_scope(self, scope_id: str, name: str = '') -> dict:
        """Collect chat metadata without persisting rejected message content."""
        with self._lock:
            try:
                self._get('scopes', scope_id)
            except KeyError:
                binding = {'profile_id': '', 'profile_overrides': []} if getattr(self, 'scope_resolver', None) else {}
                display = str(name).strip()[:100]
                if any(ord(char) < 32 for char in display):
                    display = ''
                return self.upsert_scope({'id': scope_id, 'discovered_at': utc_now(),
                                          **({'name': display} if display else {}), **binding})
            return self.get_scope(scope_id)

    def default_persona_id(self) -> str:
        personas = self.list_personas()
        return 'default' if any(p['id'] == 'default' for p in personas) else (personas[0]['id'] if personas else '')

    def inherit_scope(self, scope_id: str) -> dict:
        from .profiles import FIELDS
        with self._lock, self._conn:
            record = self._get('scopes', scope_id)
            for key in list(record):
                if key in FIELDS or key in ('bot_name', 'bot_aliases', 'output', 'quiet_start', 'quiet_end',
                                           'cooldown_seconds', 'followup_seconds', 'merge_window_ms',
                                           'daily_tokens', 'semantic_gate') or key.startswith(('output_', 'planner_')):
                    record.pop(key)
            record.update(profile_id='', profile_overrides=[], settings={})
            self._write('scopes', record, ())
            return self.upsert_scope({'id': scope_id})

    def upsert_scope(self, data: dict) -> dict:
        incoming = _data(data)
        scope_id = _text(incoming.get("id", incoming.get("scope_id", "web:default")), "id")
        with self._lock, self._conn:
            try:
                previous = self._get("scopes", scope_id)
            except KeyError:
                previous = {}
            default_name = "本地试玩"
            if scope_id.startswith("qq:group:"):
                default_name = f"QQ 群 {scope_id.removeprefix('qq:group:')}"
            elif scope_id.startswith("qq:private:"):
                default_name = f"QQ 私聊 {scope_id.removeprefix('qq:private:')}"
            elif scope_id.startswith("qqofficial:group:"):
                default_name = f"QQ 官方群 {scope_id.removeprefix('qqofficial:group:')}"
            elif scope_id.startswith("qqofficial:private:"):
                default_name = f"QQ 官方私聊 {scope_id.removeprefix('qqofficial:private:')}"
            if getattr(self, 'scope_resolver', None):
                from .profiles import FIELDS
                if not previous:
                    incoming.setdefault('profile_id', '')
                    incoming.setdefault('profile_overrides', [k for k in incoming if k in FIELDS])
                elif 'profile_id' in previous and 'profile_overrides' not in incoming:
                    incoming['profile_overrides'] = sorted(set(previous.get('profile_overrides', [])) | (set(incoming) & FIELDS))
                if 'profile_overrides' in incoming and (not isinstance(incoming['profile_overrides'], list) or any(k not in FIELDS for k in incoming['profile_overrides'])):
                    raise ValueError('会话覆盖字段无效')
            record = {"id": scope_id, "name": default_name, "enabled": True, "persona_id": self.default_persona_id(),
                      "activity": "balanced", "dictionary_understand": True, "dictionary_use": False,
                      "auto_memory": True, "quiet_start": "", "quiet_end": "", "allow_tools": True,
                      "plan_enabled": None, "trigger_at": None, "trigger_reply": None, "trigger_name": None, "proactive_reply": None,
                      "always_reply": False, "expression_learn": False, "expression_use": False, "context_compress": False,
                      "memory_space": "", "use_persona_knowledge": True,
                      "planner_min_interval_ms": None, "planner_daily_calls": None,
                      "planner_daily_tokens": None, "planner_max_age_ms": None,
                      "settings": {}, **previous, **incoming}
            record["id"] = scope_id
            if "settings" in incoming:
                if not isinstance(incoming["settings"], dict):
                    raise ValueError("settings 必须为对象")
                record["settings"] = {**previous.get("settings", {}), **incoming["settings"]}
            record["name"] = _text(record["name"], "name")
            record['model_id'] = _text(record.get('model_id',''), 'model_id', required=False)
            if len(record['model_id']) > 64:
                raise ValueError('会话模型ID不能超过64字')
            record["memory_space"] = _text(record["memory_space"], "memory_space", required=False)
            if len(record["memory_space"]) > 80 or any(ord(char) < 32 for char in record["memory_space"]):
                raise ValueError("memory_space 必须为最多 80 字的共享空间名称")
            self.get_persona(_text(record["persona_id"], "persona_id", required=False))
            _choice(record["activity"], {"quiet", "balanced", "active"}, "activity")
            for field in ("enabled", "dictionary_understand", "dictionary_use", "auto_memory", "allow_tools", "always_reply", "expression_learn", "expression_use", "context_compress", "use_persona_knowledge"):
                _boolean(record[field], field)
            for field in ("trigger_at", "trigger_reply", "trigger_name", "plan_enabled", "proactive_reply", "debounce_enabled"):
                for container in (record, record['settings']):
                    if container.get(field) is not None:
                        _boolean(container[field], field)
            if record.get('bot_name') is not None and (not isinstance(record['bot_name'], str) or len(record['bot_name']) > 40):
                raise ValueError('bot_name 最多40字')
            if record.get('bot_aliases') is not None:
                if not isinstance(record['bot_aliases'], list) or len(record['bot_aliases']) > 12 or any(not isinstance(x, str) or not 1 <= len(x.strip()) <= 30 for x in record['bot_aliases']):
                    raise ValueError('bot_aliases 最多12个，每个1—30字')
            for container in (record, record['settings']):
                for field, low, high in (('debounce_seconds', .1, 60), ('debounce_max_wait_seconds', 1, 300)):
                    value = container.get(field)
                    if value is not None and (type(value) not in (int, float) or not math.isfinite(value) or not low <= value <= high):
                        raise ValueError(field + ' 超出范围')
                if (container.get('debounce_seconds') is not None and container.get('debounce_max_wait_seconds') is not None
                        and container['debounce_seconds'] > container['debounce_max_wait_seconds']):
                    raise ValueError('消息防抖最长等待时间不能少于等待秒数')
                for field, (lower, upper) in {
                    'planner_min_interval_ms': (0, 60000), 'planner_daily_calls': (0, 100000),
                    'planner_daily_tokens': (0, 10000000), 'planner_max_age_ms': (1000, 120000),
                }.items():
                    value = container.get(field)
                    if value is not None and (type(value) is not int or not lower <= value <= upper):
                        raise ValueError(field + ' 超出范围')
                local_output = container.get('output')
                if local_output is not None and not isinstance(local_output, dict):
                    raise ValueError('output 必须为对象')
                local_output = {**(local_output or {}), **{k.removeprefix('output_'):v for k,v in container.items() if k.startswith('output_')}}
                for key,value in local_output.items():
                    if value is None: continue
                    if key in {'enabled', 'remove_parentheses'}:
                        _boolean(value, 'output.' + key)
                    elif key == 'mode':
                        if value not in ('model','length'): raise ValueError('output.mode 无效')
                    elif key in {'target_chars','max_chars','interval_ms','max_parts','platform_max_chars','reply_max_chars','message_max_chars'}:
                        lower,upper = {'target_chars':(20,1000),'max_chars':(40,4000),'interval_ms':(0,5000),'max_parts':(1,20),'platform_max_chars':(40,20000),
                                       'reply_max_chars':(0,20000),'message_max_chars':(0,4000)}[key]
                        if type(value) is not int or not lower <= value <= upper:
                            raise ValueError('output.' + key + ' 超出范围')
                    else:
                        raise ValueError('未知output字段')
                if local_output.get('target_chars') is not None and local_output.get('max_chars') is not None and local_output['target_chars'] > local_output['max_chars']:
                    raise ValueError('输出目标长度不能大于单段最大长度')
            for field in ("quiet_start", "quiet_end"):
                if not isinstance(record[field], str) or (record[field] and not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", record[field])):
                    raise ValueError(f"{field} 必须为 HH:mm 或空字符串")
            resolved = getattr(self, 'scope_resolver', lambda value: value)(record)
            self._write("scopes", record, ())
            return resolved

    def list_personas(self) -> list[dict]:
        return self._list("personas")

    def get_persona(self, id: str) -> dict:
        if id == '':
            return {'id': '', 'name': '未使用人格', 'description': '', 'color': '#39c5bb',
                    'system_prompt': '你是 mikubot。自然、清楚地回应，不编造记忆或已经完成的事情。'}
        return self._get("personas", id)

    def save_persona(self, data: dict) -> dict:
        incoming = _data(data)
        record_id = _text(incoming.get("id") or str(uuid4()), "id")
        with self._lock, self._conn:
            try:
                previous = self.get_persona(record_id)
            except KeyError:
                previous = {}
            record = {"id": record_id, "name": "新角色", "description": "", "system_prompt": "", "color": "#52735b", **previous, **incoming}
            for field in ("name", "system_prompt"):
                record[field] = _text(record[field], field)
            record["description"] = _text(record["description"], "description", required=False)
            if not isinstance(record["color"], str) or not re.fullmatch(r"#[0-9a-fA-F]{6}", record["color"]):
                raise ValueError("color 必须为十六进制颜色")
            return self._write("personas", record, ())

    def delete_persona(self, id: str) -> dict:
        with self._lock, self._conn:
            record = self.get_persona(id)
            remaining = [p for p in self.list_personas() if p['id'] != id]
            replacement = next((p['id'] for p in remaining if p['id'] == 'default'), remaining[0]['id'] if remaining else '')
            for scope in self._list('scopes'):
                if scope.get("persona_id") == id:
                    scope["persona_id"] = replacement
                    self._write("scopes", scope, ())
            self._conn.execute("DELETE FROM personas WHERE id=?", (id,))
            return record

    def add_message(self, scope_id: str, user_id: str, role: str, content: str, **metadata: Any) -> dict:
        scope_id = _text(scope_id, "scope_id")
        user_id = _text(user_id, "user_id")
        _choice(role, {"user", "assistant", "system", "tool"}, "role")
        content = _text(content, "content", required=False)
        metadata = _data(metadata)
        if not content and not metadata:
            raise ValueError("消息不能为空")
        # created_at remains the local insertion cursor for old databases.
        # Platform send and local receipt are distinct provenance, not event dates.
        times = ingress_times(metadata.get('sent_at'), received_at=metadata.get('received_at'))
        metadata.update(times)
        record = {"id": str(uuid4()), "scope_id": scope_id, "user_id": user_id, "role": role,
                  "content": content, "created_at": utc_now(), **times, "metadata": metadata}
        with self._lock, self._conn:
            self.get_scope(scope_id)
            return self._write("messages", record, ("scope_id", "user_id", "role", "created_at"))

    def list_messages(self, scope_id: str, limit: int = 50, before: str | None = None) -> list[dict]:
        limit = _limit(limit)
        clauses, params = ["scope_id=?"], [scope_id]
        with self._lock:
            if before:
                cursor = self._conn.execute("SELECT created_at,rowid FROM messages WHERE scope_id=? AND id=?", (scope_id, before)).fetchone()
                if cursor:
                    clauses.append("(created_at<? OR (created_at=? AND rowid<?))")
                    params.extend((cursor[0], cursor[0], cursor[1]))
                else:
                    clauses.append("created_at<?")
                    params.append(_iso(before, "before", nullable=False))
            rows = self._conn.execute(
                f"SELECT data FROM messages WHERE {' AND '.join(clauses)} ORDER BY created_at DESC,rowid DESC LIMIT ?", (*params, limit)
            ).fetchall()
            return [json.loads(row[0]) for row in reversed(rows)]

    def clear_messages(self, scope_id: str) -> dict:
        with self._lock, self._conn:
            cursor = self._conn.execute("DELETE FROM messages WHERE scope_id=?", (scope_id,))
            return {"scope_id": scope_id, "deleted": cursor.rowcount}

    def get_message(self, id: str) -> dict:
        return self._get("messages", id)

    def ordered_messages(self, ids, *, scope_id):
        """Resolve canonical sources in the same order as list_messages."""
        keys = list(dict.fromkeys(ids))
        if not keys:
            return []
        with self._lock:
            rows = self._conn.execute(
                "SELECT data FROM messages WHERE scope_id=? AND id IN "
                "(SELECT value FROM json_each(?)) ORDER BY created_at,rowid",
                (scope_id, json.dumps(keys))).fetchall()
        return [json.loads(row[0]) for row in rows]

    def _prepare_memory(self, record: dict) -> dict:
        for field in ("scope_id", "user_id", "content"):
            record[field] = _text(record[field], field)
        record["subject"] = _text(record.get("subject", ""), "subject", required=False)
        if "property" in record:
            record["property"] = _text(record["property"], "property", required=False)
        property_name = _memory_property(record)
        if property_name:
            record["property"] = property_name
        for field in ("event_at", "occurred_at", "event_time_start"):
            if record.get(field):
                record[field] = _iso(record[field], field)
        _choice(record["kind"], MEMORY_KINDS, "kind")
        _choice(record["status"], MEMORY_STATUSES, "status")
        _choice(record["visibility"], {"private", "group"}, "visibility")
        record["source_ids"] = _strings(record.get("source_ids", []), "source_ids")
        record["blocked_source_ids"] = _strings(record.get("blocked_source_ids", []), "blocked_source_ids")
        record["confidence"] = _number(record["confidence"], "confidence", maximum=1)
        record["importance"] = _number(record["importance"], "importance", maximum=1)
        _boolean(record["pinned"], "pinned")
        record["shareable"] = _boolean(record.get("shareable", False), "shareable")
        record["valid_from"] = _iso(record.get("valid_from"), "valid_from")
        record["valid_to"] = _iso(record.get("valid_to"), "valid_to")
        if record["valid_from"] and record["valid_to"] and record["valid_from"] >= record["valid_to"]:
            raise ValueError("valid_to 必须晚于 valid_from")
        dependencies = record.get("dependencies", {})
        if not isinstance(dependencies, dict) or any(
            not isinstance(key, str) or not key.strip() or isinstance(value, bool) or not isinstance(value, int) or value < 1
            for key, value in dependencies.items()
        ):
            raise ValueError("dependencies 必须为记忆 ID 到正整数版本的映射")
        record["dependencies"] = dependencies
        if record.get("property") == "memory_relation":
            for field in ("from_id", "to_id", "label"):
                record[field] = _text(record.get(field), field)
            if record["from_id"] == record["to_id"] or len(record["label"]) > 160:
                raise ValueError("关系必须连接两条不同记忆，说明最多 160 字")
            _choice(record.get("relation_type"), {"person_relation", "event_link", "related"}, "relation_type")
            if not {record["from_id"], record["to_id"]} <= set(dependencies):
                raise ValueError("关系必须引用两个端点的当前版本")
        supersedes = record.get("supersedes")
        if supersedes is not None and not isinstance(supersedes, (str, list)):
            raise ValueError("supersedes 必须为记忆 ID、ID 列表或空")
        if isinstance(supersedes, list):
            record["supersedes"] = _strings(supersedes, "supersedes")
        return record

    def _save_memory(self, record: dict) -> dict:
        result = self._write("memories", record, (
            "scope_id", "user_id", "kind", "status", "visibility", "valid_from", "valid_to", "version", "created_at", "updated_at",
        ))
        self._conn.execute("INSERT INTO memory_versions(memory_id,version,created_at,data) VALUES (?,?,?,?)",
                           (record["id"], record["version"], record["updated_at"], _json(record)))
        return result

    def create_memory(self, data: dict) -> dict:
        incoming, now = _data(data), utc_now()
        scope_id = incoming.get("scope_id", "web:default")
        default_visibility = "group" if str(scope_id).startswith(("qq:group:", "qqofficial:group:")) else "private"
        record = {"scope_id": scope_id, "user_id": "owner", "kind": "fact", "subject": "",
                  "source_ids": [], "status": "active", "visibility": default_visibility,
                  "confidence": 1.0, "importance": 0.5, "pinned": False, "shareable": False, "valid_from": None,
                  "valid_to": None, "supersedes": None, "dependencies": {}, **incoming,
                  "id": _text(incoming.get("id") or str(uuid4()), "id"), "version": 1,
                  "created_at": now, "updated_at": now}
        self._prepare_memory(record)
        if record["status"] == "deleted":
            raise ValueError("新记忆不能为 deleted")
        with self._lock, self._conn:
            if self._conn.execute("SELECT 1 FROM memories WHERE id=?", (record["id"],)).fetchone():
                raise ValueError("记忆 ID 已存在")
            self.get_scope(record["scope_id"])
            return self._save_memory(record)

    def list_memories(self, scope_id: str | None = None, q: str = "", status: str | None = None) -> list[dict]:
        clauses, params = self._filters(scope_id, status, MEMORY_STATUSES)
        if status is None:
            clauses.append("status!='deleted'")
        records = self._list("memories", clauses, params, "updated_at DESC,rowid DESC")
        return self._search_text([_memory_view(record) for record in records], q, ("content", "subject", "kind", "property"))

    def get_memory(self, id: str) -> dict:
        return _memory_view(self._get("memories", id))

    def user_memory_records(self, scope_id: str, user_id: str, *, limit=1000, offset=0) -> list[dict]:
        """Bound maintenance reads in SQL before decoding memory bodies."""
        limit = _limit(limit, 1000)
        if type(offset) is not int or offset < 0:
            raise ValueError('offset须为非负整数')
        with self._lock:
            rows = self._conn.execute("""SELECT data FROM memories
                WHERE scope_id=? AND user_id=? AND status='active'
                AND COALESCE(json_extract(data,'$.property'),'') NOT IN ('memory_relation','conversation_digest')
                ORDER BY updated_at DESC,rowid DESC LIMIT ? OFFSET ?""",
                (scope_id, str(user_id), limit, offset)).fetchall()
        return [_memory_view(json.loads(row[0])) for row in rows]

    def user_memory_versions(self, scope_id: str, user_id: str) -> dict:
        """Dependency snapshot without loading unrelated users or text/JSON."""
        with self._lock:
            return dict(self._conn.execute("SELECT id,version FROM memories WHERE scope_id=? AND user_id=? AND status='active'",
                                           (scope_id, str(user_id))).fetchall())

    def blocked_memory_sources(self, scope_id: str) -> set[str]:
        """Return current source tombstones without rescanning on every token.

        No result read inside a transaction can enter the cross-call cache:
        SQLite reuses a revision after rollback, and total_changes does not
        roll back. The TEMP revision instead follows committed changes to the
        relevant scope, including writes which bypass Store methods.
        """
        with self._lock:
            if self._conn.in_transaction:
                return self._query_blocked_memory_sources(scope_id)
            for _ in range(3):
                data_version = self._conn.execute('PRAGMA data_version').fetchone()[0]
                if data_version != self._blocked_source_data_version:
                    self._blocked_source_cache.clear()
                    with self._conn:
                        self._conn.execute('DELETE FROM temp.blocked_source_revisions')
                    self._blocked_source_data_version = data_version
                if scope_id not in self._blocked_source_cache:
                    with self._conn:
                        if len(self._blocked_source_cache) >= BLOCKED_SOURCE_CACHE_SCOPES:
                            expired, _ = self._blocked_source_cache.popitem(last=False)
                            self._conn.execute('DELETE FROM temp.blocked_source_revisions WHERE scope_id=?', (expired,))
                        self._conn.execute('INSERT OR IGNORE INTO temp.blocked_source_revisions VALUES (?,0)', (scope_id,))
                    self._blocked_source_cache[scope_id] = None
                revision = self._conn.execute(
                    'SELECT revision FROM temp.blocked_source_revisions WHERE scope_id=?', (scope_id,)).fetchone()[0]
                cached = self._blocked_source_cache[scope_id]
                result = (cached[1] if cached is not None and cached[0] == revision
                          else frozenset(self._query_blocked_memory_sources(scope_id)))
                # A second connection can commit between the initial version
                # check and the scan/cache read. Do not publish that old result.
                if self._conn.execute('PRAGMA data_version').fetchone()[0] != data_version:
                    continue
                self._blocked_source_cache[scope_id] = (revision, result)
                self._blocked_source_cache.move_to_end(scope_id)
                return set(result)
            # Continuous external writers must not make this authorization
            # check spin indefinitely. A fresh SELECT retains the old method's
            # snapshot semantics and is deliberately not cached.
            return self._query_blocked_memory_sources(scope_id)

    def _query_blocked_memory_sources(self, scope_id: str) -> set[str]:
        rows = self._conn.execute("""SELECT j.value FROM memories m,
            json_each(m.data,'$.blocked_source_ids') j WHERE m.scope_id=? AND j.type='text'
            UNION SELECT j.value FROM jargon m,
            json_each(m.data,'$.blocked_source_ids') j WHERE m.scope_id=? AND j.type='text'""",
            (scope_id, scope_id)).fetchall()
        return {row[0] for row in rows}

    def memory_property_records(self, scope_id, property_name, status=None, *, include_shared=False):
        if include_shared:
            scope = self.get_scope(scope_id)
            space = scope.get('memory_space')
            origins = {scope_id} | {item['id'] for item in self.list_scopes()
                                   if space and item.get('memory_space') == space and item.get('enabled', True)}
        else:
            origins = None
        clauses, params = self._filters(scope_id, status, MEMORY_STATUSES)
        if origins is not None:
            clauses, params = self._filters(None, status, MEMORY_STATUSES)
            clauses.append('scope_id IN (' + ','.join('?' for _ in origins) + ')')
            params.extend(sorted(origins))
        if status is None: clauses.append("status!='deleted'")
        clauses.append("json_extract(data,'$.property')=?")
        params.append(property_name)
        return [_memory_view(record) for record in self._list('memories', clauses, params)]

    def memory_review_counts(self):
        with self._lock:
            counts = dict(self._conn.execute('SELECT status,COUNT(*) FROM memories GROUP BY status').fetchall())
            digests = self._conn.execute("SELECT COUNT(*) FROM memories WHERE status='active' AND json_extract(data,'$.property')='conversation_digest'").fetchone()[0]
            jargon = self._conn.execute("""SELECT
                SUM(CASE WHEN json_extract(data,'$.entry_type')='expression' THEN 1 ELSE 0 END),
                SUM(CASE WHEN COALESCE(json_extract(data,'$.entry_type'),'jargon')!='expression' THEN 1 ELSE 0 END)
                FROM jargon WHERE status='pending'""").fetchone()
        return {'active': counts.get('active', 0), 'pending': counts.get('pending', 0), 'digests': digests,
                'expression_pending': jargon[0] or 0, 'jargon_pending': jargon[1] or 0}

    def memory_page(self, scope_id=None, q='', status=None, *, limit=50, offset=0):
        limit = _limit(limit, 200)
        if type(offset) is not int or offset < 0:
            raise ValueError('offset 须为非负整数')
        q = _text(q, 'q', required=False)
        if len(q) > 1000: raise ValueError('搜索文字不超过1000字')
        clauses, params = self._filters(scope_id, status, MEMORY_STATUSES)
        if status is None: clauses.append("status!='deleted'")
        with self._lock:
            if q:
                # Preserve legacy casefold/property normalization semantics.
                self._conn.create_function('memory_query', 2, lambda raw, query: bool(self._search_text(
                    [_memory_view(json.loads(raw))], query, ('content', 'subject', 'kind', 'property'))))
                clauses.append('memory_query(data,?)')
                params.append(q)
            where = ' WHERE ' + ' AND '.join(clauses) if clauses else ''
            total = self._conn.execute('SELECT COUNT(*) FROM memories' + where, params).fetchone()[0]
            rows = self._conn.execute('SELECT data FROM memories' + where + ' ORDER BY updated_at DESC,rowid DESC LIMIT ? OFFSET ?',
                                      [*params, limit, offset]).fetchall()
        items = [_memory_view(json.loads(row[0])) for row in rows]
        return {'items': items, 'total': total, 'offset': offset, 'limit': limit,
                'has_more': offset + len(items) < total, 'next_offset': offset + len(items)}

    def list_memory_versions(self, id: str) -> list[dict]:
        with self._lock:
            self.get_memory(id)
            rows = self._conn.execute("SELECT data FROM memory_versions WHERE memory_id=? ORDER BY version", (id,)).fetchall()
            return [json.loads(row[0]) for row in rows]

    def update_memory(self, id: str, patch: dict) -> dict:
        patch = _data(patch)
        with self._lock, self._conn:
            old = self.get_memory(id)
            if old["status"] == "deleted":
                raise ValueError("已删除记忆不能恢复，请创建新记忆")
            if patch.get("status") == "deleted":
                return self.delete_memory(id)
            record = {**old, **patch, "id": old["id"], "version": old["version"] + 1,
                      "created_at": old["created_at"], "updated_at": utc_now()}
            record["blocked_source_ids"] = list(dict.fromkeys(old.get("blocked_source_ids", [])
                + _strings(patch.get("blocked_source_ids", []), "blocked_source_ids")))
            if "content" in patch and patch["content"] != old["content"]:
                record["blocked_source_ids"] = list(dict.fromkeys(
                    record["blocked_source_ids"] + old.get("source_ids", [])
                ))
            self._prepare_memory(record)
            self.get_scope(record["scope_id"])
            return self._save_memory(record)

    def delete_memory(self, id: str) -> dict:
        with self._lock, self._conn:
            record = self.get_memory(id)
            if record["status"] == "deleted":
                return record
            record = {"id": record["id"], "scope_id": record["scope_id"], "user_id": record["user_id"],
                      "kind": record["kind"], "content": "", "subject": "", "source_ids": [],
                      "blocked_source_ids": list(dict.fromkeys(record.get("blocked_source_ids", []) + record.get("source_ids", []))),
                      "status": "deleted", "visibility": record["visibility"], "confidence": 0.0,
                      "importance": 0.0, "pinned": False, "valid_from": None, "valid_to": None,
                      "supersedes": None, "dependencies": {}, "version": record["version"] + 1,
                      "created_at": record["created_at"], "updated_at": utc_now()}
            self._conn.execute("DELETE FROM memory_versions WHERE memory_id=?", (id,))
            for event in self._list("events", ["kind='memory_organization'",
                    "EXISTS (SELECT 1 FROM json_each(events.data,'$.details.memory_ids') WHERE value=?)"], [id]):
                event["details"].pop("before", None)
                event["details"].update(status="invalidated", invalidated_at=utc_now())
                self._write("events", event, ("created_at", "kind"))
            return self._save_memory(record)

    def _allowed_memories(self, scope_id: str, user_id: str, *, scope_allowed=None, roots=None) -> list[dict]:
        now = utc_now()
        scope = self.get_scope(scope_id)
        if not scope.get("enabled", True):
            return []
        scopes = {record["id"]: record for record in self.list_scopes()}
        space = scope.get("memory_space", "")
        origin_permissions = {}

        def in_boundary(record):
            origin = record.get("scope_id")
            if scope_allowed is not None:
                if origin not in origin_permissions:
                    origin_permissions[origin] = bool(scope_allowed(origin))
                if not origin_permissions[origin]:
                    return False
            if origin == scope_id:
                return True
            source = scopes.get(origin, {})
            return bool(space and source.get("enabled", True) and source.get("memory_space", "") == space
                        and record.get("shareable") is True and record.get("visibility") == "group"
                        and record.get("kind") in MEMORY_KINDS and record.get("property") != "conversation_digest")

        # Sources retain their original scope. Sharing is a projection with
        # explicit per-record consent, never a copy or historical merge.
        origins = [scope_id] + [key for key, value in scopes.items()
                               if space and value.get('memory_space') == space and value.get('enabled', True)]
        clauses = ["status='active'", 'scope_id IN (' + ','.join('?' for _ in origins) + ')']
        records = [_memory_view(record) for record in (roots if roots is not None else self._list('memories', clauses, origins))
                   if record.get('status') == 'active' and in_boundary(record)]
        selected_ids = {record['id'] for record in records}
        by_id = {record["id"]: record for record in records}
        if roots is not None:
            # Load only the dependency closure, never the whole memory table.
            pending = list(records)
            while pending:
                current = pending.pop()
                dependencies = list(current.get('dependencies', {}))
                if current.get('property') == 'memory_relation':
                    dependencies += [current.get('from_id'), current.get('to_id')]
                for key in dependencies:
                    if not key or key in by_id: continue
                    try: dependency = self.get_memory(key)
                    except KeyError: continue
                    if dependency.get('status') == 'active' and in_boundary(dependency):
                        by_id[key] = dependency
                        records.append(dependency)
                        pending.append(dependency)
        memo: dict[str, bool] = {}
        # Digests are disposable projections, not independent facts. This uses
        # JSON fields only, so old databases and backups retain schema version 1.
        digests = any(record.get("property") == "conversation_digest" for record in records)
        relation_origins = {record["scope_id"] for record in records if record.get("property") == "memory_relation"}
        relation_blocked = {}
        for origin in relation_origins:
            relation_blocked[origin] = self.blocked_memory_sources(origin)
        blocked = set()
        jargon = {}
        if digests:
            blocked = self.blocked_memory_sources(scope_id)
            jargon = {item["id"]: item for item in self._list("jargon", ["scope_id=?"], [scope_id])}

        def allowed(record: dict, visiting: set[str]) -> bool:
            record_id = record["id"]
            if record_id in memo:
                return memo[record_id]
            if record_id in visiting:
                return False
            if not in_boundary(record):
                memo[record_id] = False
                return False
            public_group = scope_id.startswith(("qq:group:", "qqofficial:group:"))
            if record.get("visibility") == "private" and (public_group or record.get("user_id") != user_id):
                memo[record_id] = False
                return False
            if (record.get("valid_from") and record["valid_from"] > now) or (record.get("valid_to") and record["valid_to"] <= now):
                memo[record_id] = False
                return False
            if record.get("property") == "memory_relation":
                if set(record.get("source_ids", [])) & relation_blocked.get(record["scope_id"], set()):
                    memo[record_id] = False
                    return False
                endpoints = record.get("endpoint_identities", {})
                for endpoint_id in (record.get("from_id"), record.get("to_id")):
                    endpoint = by_id.get(endpoint_id)
                    if not endpoint or endpoint.get("property") == "memory_relation" or endpoints.get(endpoint_id) != [endpoint.get("scope_id"), endpoint.get("user_id")]:
                        memo[record_id] = False
                        return False
                fingerprints = record.get("source_fingerprints", {})
                for source_id in record.get("source_ids", []):
                    try:
                        source = self.get_message(source_id)
                    except KeyError:
                        memo[record_id] = False
                        return False
                    if source.get("scope_id") != record["scope_id"] or fingerprints.get(source_id) != message_fingerprint(source):
                        memo[record_id] = False
                        return False
            if record.get("property") == "conversation_digest":
                if not scope.get("enabled", True) or not (scope.get("context_compress", False) or record.get("manual_digest")):
                    memo[record_id] = False
                    return False
                sources = record.get("source_ids", [])
                fingerprints = record.get("source_fingerprints", {})
                if not sources or not isinstance(fingerprints, dict) or set(sources) & blocked:
                    memo[record_id] = False
                    return False
                for source_id in sources:
                    try:
                        source = self.get_message(source_id)
                    except KeyError:
                        memo[record_id] = False
                        return False
                    if source.get("scope_id") != scope_id or fingerprints.get(source_id) != message_fingerprint(source):
                        memo[record_id] = False
                        return False
                    if not public_group and source.get("role") == "user" and source.get("user_id") != user_id:
                        memo[record_id] = False
                        return False
                jargon_dependencies = record.get("jargon_dependencies", {})
                for dependency_id, version in jargon_dependencies.items():
                    dependency = jargon.get(dependency_id)
                    enabled = (scope.get("expression_use", False) and dependency and dependency.get("use_allowed", False)) if dependency and dependency.get("entry_type", "jargon") == "expression" else scope.get("dictionary_understand", True)
                    if not enabled or dependency is None or dependency.get("status") != "approved" or dependency.get("version") != version:
                        memo[record_id] = False
                        return False
            visiting = visiting | {record_id}
            for dependency_id, version in record.get("dependencies", {}).items():
                dependency = by_id.get(dependency_id)
                if dependency is None or dependency["version"] != version or not allowed(dependency, visiting):
                    memo[record_id] = False
                    return False
            memo[record_id] = True
            return True

        return [record for record in records if record['id'] in selected_ids and allowed(record, set())]

    def profile_preferences(self, scope_id: str, user_id: str, limit: int = 3) -> list[dict]:
        """Bounded chat projection; no full profile or summary allocation."""
        limit = _limit(limit, 50)
        with self._lock:
            scope = self.get_scope(scope_id)
            space = scope.get('memory_space')
            origins = list(dict.fromkeys([scope_id] + [s['id'] for s in self.list_scopes()
                if space and s.get('memory_space') == space and s.get('enabled', True)]))
            marks = ','.join('?' for _ in origins)
            offset, result = 0, []
            while len(result) < limit:
                rows = self._conn.execute(f"""SELECT data FROM memories
                    WHERE scope_id IN ({marks}) AND user_id=? AND kind='preference' AND status='active'
                    AND (valid_from IS NULL OR valid_from<=?) AND (valid_to IS NULL OR valid_to>?)
                    AND (scope_id=? OR (visibility='group' AND json_extract(data,'$.shareable') IS 1))
                    AND (visibility!='private' OR ?=0)
                    ORDER BY (json_extract(data,'$.pinned') IS 1) DESC,
                    CASE WHEN json_type(data,'$.importance') IN ('integer','real')
                      AND json_extract(data,'$.importance') BETWEEN 0 AND 1
                      THEN json_extract(data,'$.importance') ELSE 0.5 END DESC,
                    updated_at DESC, id DESC LIMIT 32 OFFSET ?""", (*origins, user_id, utc_now(), utc_now(), scope_id,
                        int(scope_id.startswith(('qq:group:', 'qqofficial:group:'))), offset)).fetchall()
                if not rows: break
                result.extend(self._allowed_memories(scope_id, user_id, roots=[json.loads(row['data']) for row in rows]))
                offset += len(rows)
            return result[:limit]

    def visible_memories(self, scope_id: str, user_id: str, *, scope_allowed=None, record_ids=None) -> list[dict]:
        """Authoritative current projection shared by lexical/semantic readers."""
        with self._lock:
            roots = None
            if record_ids is not None:
                keys = list(dict.fromkeys(record_ids))
                roots = []
                for start in range(0, len(keys), 400):
                    part = keys[start:start + 400]
                    roots.extend(self._list('memories', ['id IN (' + ','.join('?' for _ in part) + ')'], part))
            return self._allowed_memories(scope_id, user_id, scope_allowed=scope_allowed, roots=roots)

    def _memory_candidate_boundary(self, scope_id, user_id, scope_allowed=None):
        from .memory_search import Boundary
        scope = self.get_scope(scope_id)
        if not scope.get('enabled', True):
            origins = ()
        else:
            space = scope.get('memory_space')
            origins = tuple(dict.fromkeys([scope_id] + [item['id'] for item in self.list_scopes()
                if space and item.get('memory_space') == space and item.get('enabled', True)]))
            if scope_allowed is not None:
                origins = tuple(origin for origin in origins if scope_allowed(origin))
        return Boundary(scope_id, user_id, origins,
                        scope_id.startswith(('qq:group:', 'qqofficial:group:')), utc_now())

    def memory_candidate_ids(self, scope_id: str, user_id: str, query: str, limit: int = 192, *, scope_allowed=None) -> list[str]:
        """Bound lexical candidates in a disposable index, then recheck visible_memories.

        The first call rebuilds the old database in batches. Hot calls decode
        modified records only. Metadata filtering is not dependency validation.
        """
        from .memory_search import MemorySearchIndex, sql_fallback
        limit = _limit(limit, 512)
        with self._lock:
            boundary = self._memory_candidate_boundary(scope_id, user_id, scope_allowed)
            if not boundary.origins:
                return []
            for attempt in range(2):
                try:
                    if self._memory_search is None:
                        self._memory_search = MemorySearchIndex(self.data_dir / 'memory_search.db')
                    self._memory_search.sync(self._conn, self._memory_search_dirty)
                    # A query inside an uncommitted transaction must remain
                    # dirty in case the caller subsequently rolls back.
                    if not self._conn.in_transaction:
                        self._memory_search_dirty = set()
                    result = self._memory_search.candidates(boundary, query, limit)
                    self._memory_search_warning = ''
                    return result
                except (sqlite3.DatabaseError, OSError):
                    logging.getLogger(__name__).warning('记忆候选索引读取失败，将重建派生索引；主记忆数据库不受影响。')
                    if self._memory_search is not None:
                        self._memory_search.close()
                        self._memory_search = None
                    self._memory_search_dirty = None
                    if attempt == 0:
                        path = self.data_dir / 'memory_search.db'
                        try:
                            for suffix in ('', '-wal', '-shm', '-journal'):
                                path.with_name(path.name + suffix).unlink(missing_ok=True)
                        except OSError:
                            break
            if not self._memory_search_warning:
                self._memory_search_warning = '记忆候选索引不可用，已降级为主库SQL检索；大量记忆检索可能变慢。'
                logging.getLogger(__name__).warning(self._memory_search_warning)
            return sql_fallback(self._conn, boundary, query, limit)

    def filter_memory_candidate_ids(self, scope_id: str, user_id: str, record_ids, *, scope_allowed=None) -> list[str]:
        """Fresh coarse SQL boundary for cached vectors; no body decoding.

        Dependencies, source withdrawal and settings remain the responsibility
        of visible_memories. Only this bounded projection may precede ranking.
        """
        keys = list(dict.fromkeys(record_ids))
        current = self.memory_candidate_versions(scope_id, user_id, keys, scope_allowed=scope_allowed)
        return [key for key in keys if key in current]

    def memory_candidate_versions(self, scope_id: str, user_id: str, record_ids, *, scope_allowed=None) -> dict[str, int]:
        """Current coarse-boundary versions for rejecting stale vector references.

        SQL reads IDs and versions only. Dependency and source authorization
        still need visible_memories after selecting the bounded candidates.
        """
        with self._lock:
            boundary = self._memory_candidate_boundary(scope_id, user_id, scope_allowed)
            if not boundary.origins:
                return {}
            where, params = boundary.sql(alias='m')
            keys = list(dict.fromkeys(record_ids))
            result = {}
            for start in range(0, len(keys), 64):
                part = keys[start:start + 64]
                rows = self._conn.execute('SELECT m.id,m.version FROM memories m WHERE m.id IN ('
                    + ','.join('?' for _ in part) + f') AND {where}', (*part, *params))
                result.update((row[0], int(row[1])) for row in rows)
            return result

    def memory_search_status(self):
        with self._lock:
            search = self._memory_search
            return {'mode': search.mode if search else ('sql_fallback' if self._memory_search_warning else 'uninitialized'),
                    'warning': self._memory_search_warning or (search.warning if search else ''),
                    'initialized': bool(search and search.initialized), 'revision': self._memory_revision}

    def message_sources_current(self, scope_id: str, source_ids) -> bool:
        """Check message existence/scope without decoding every historical body."""
        keys = list(dict.fromkeys(source_ids))
        if any(not isinstance(key, str) for key in keys):
            return False
        with self._lock:
            for start in range(0, len(keys), 400):
                part = keys[start:start + 400]
                count = self._conn.execute('SELECT COUNT(*) FROM messages WHERE scope_id=? AND id IN ('
                                           + ','.join('?' for _ in part) + ')', (scope_id, *part)).fetchone()[0]
                if count != len(part):
                    return False
        return True

    def memory_accessible(self, scope_id: str, user_id: str, record_id: str, version: int | None = None, *, scope_allowed=None) -> bool:
        """Recheck consent, membership, privacy and dependency closure now."""
        with self._lock:
            try: candidate = self.get_memory(record_id)
            except KeyError: return False
            return any(record["id"] == record_id and (version is None or record["version"] == version)
                       for record in self._allowed_memories(scope_id, user_id, roots=[candidate], scope_allowed=scope_allowed))

    def allowed_memory_sources(self, scope_id: str, user_id: str, record_id: str) -> list[str]:
        """Shared facts may carry citations, never another chat's raw history."""
        with self._lock:
            try: candidate = self.get_memory(record_id)
            except KeyError: return []
            record = next(iter(self._allowed_memories(scope_id, user_id, roots=[candidate])), None)
            if record is None or record["scope_id"] != scope_id:
                return []
            result = []
            for source_id in record.get("source_ids", []):
                try:
                    source = self.get_message(source_id)
                except KeyError:
                    continue
                if source.get("scope_id") == scope_id:
                    result.append(source_id)
            return result

    def recall(self, scope_id: str, user_id: str, query: str, limit: int = 6) -> list[dict]:
        limit = _limit(limit, 50)
        query = _text(query, "query", required=False)
        with self._lock:
            records = [record for record in self._allowed_memories(scope_id, user_id)
                       if record.get("property") != "memory_relation"]
        from .memory import lexical_recall
        return lexical_recall(records, query, str(user_id), limit=limit, per_memory=1)

    def _profile_candidates(self, scope_id, user_id, *, kinds, limit, offset=0, events=False):
        """Page in SQL before JSON decoding; authoritative consent follows."""
        scope = self.get_scope(scope_id)
        space = scope.get('memory_space')
        origins = list(dict.fromkeys([scope_id] + [s['id'] for s in self.list_scopes()
            if space and s.get('memory_space') == space and s.get('enabled', True)]))
        marks = ','.join('?' for _ in origins)
        kinds_sql = ','.join('?' for _ in kinds)
        order = ("COALESCE(json_extract(data,'$.event_at'),json_extract(data,'$.occurred_at'),created_at) DESC, id DESC" if events else
            """(json_extract(data,'$.pinned') IS 1) DESC,
            CASE WHEN json_type(data,'$.importance') IN ('integer','real')
              AND json_extract(data,'$.importance') BETWEEN 0 AND 1
              THEN json_extract(data,'$.importance') ELSE 0.5 END DESC, updated_at DESC, id DESC""")
        stamp = utc_now()
        rows = self._conn.execute(f"""SELECT data FROM memories
            WHERE scope_id IN ({marks}) AND user_id=? AND kind IN ({kinds_sql}) AND status='active'
            AND (valid_from IS NULL OR valid_from<=?) AND (valid_to IS NULL OR valid_to>?)
            AND (scope_id=? OR (visibility='group' AND json_extract(data,'$.shareable') IS 1))
            AND (visibility!='private' OR ?=0)
            ORDER BY {order} LIMIT ? OFFSET ?""", (*origins,user_id,*kinds,stamp,stamp,scope_id,
                int(scope_id.startswith(('qq:group:','qqofficial:group:'))),limit+1,offset)).fetchall()
        return [json.loads(row['data']) for row in rows[:limit]], len(rows)>limit

    def profile(self, scope_id: str, user_id: str, *, limit=50, offset=0, include_summary=True) -> dict:
        limit = _limit(limit, 100)
        if type(offset) is not int or not 0 <= offset <= 1000000:
            raise ValueError('画像分页位置须为0—1000000的整数')
        with self._lock:
            candidates, more = self._profile_candidates(scope_id,user_id,kinds=('fact','preference'),limit=limit,offset=offset)
            records = self._allowed_memories(scope_id,user_id,roots=candidates)
            event_rows, _ = self._profile_candidates(scope_id,user_id,kinds=('event',),limit=12,events=True)
            events = self._allowed_memories(scope_id,user_id,roots=event_rows)
        summary = '\n'.join(record['content'] for record in records)[:20000] if include_summary else ''
        return {"scope_id": scope_id, "user_id": user_id,
                "offset":offset,"limit":limit,"has_more":more,"next_offset":offset+limit if more else None,
                "facts": [record for record in records if record["kind"] == "fact"],
                "preferences": [record for record in records if record["kind"] == "preference"],
                "memories": records, "summary": summary,
                "recent_events": events[:12],
                "structured_preferences": [{"memory_id": record["id"], "subject": record["subject"] or user_id,
                    "property": record.get("property", ""), "value": record["content"], "source_ids": record["source_ids"],
                    "confidence": record["confidence"], "version": record["version"]}
                    for record in records if record["kind"] == "preference"],
                "evidence": [{"memory_id": record["id"], "version": record["version"],
                    "source_ids": record["source_ids"], "confidence": record["confidence"],
                    "valid_from": record.get("valid_from"), "valid_to": record.get("valid_to")}
                    for record in records + events[:12]]}

    def memory_conflicts(self, id: str) -> list[dict]:
        """Only explicit subject/property identity defines a factual conflict."""
        from .memory import normalized
        with self._lock:
            candidate = self.get_memory(id)
            property_name = normalized(str(candidate.get("property", "")))
            if not property_name or candidate["kind"] not in {"fact", "preference"}:
                return []
            subject = normalized(candidate.get("subject") or candidate["user_id"])
            return [record for record in self.list_memories(candidate["scope_id"], status="active")
                    if record["id"] != id and record["kind"] in {"fact", "preference"}
                    and normalized(record.get("subject") or record["user_id"]) == subject
                    and normalized(str(record.get("property", ""))) == property_name
                    and normalized(record["content"]) != normalized(candidate["content"])]

    def approve_memory(self, id: str, *, replace_conflicts: bool = False) -> dict:
        """Explicitly approve a candidate and atomically supersede conflicts."""
        _boolean(replace_conflicts, "replace_conflicts")
        with self._lock, self._conn:
            candidate = self.get_memory(id)
            if candidate["status"] == "active":
                return candidate
            if candidate["status"] != "pending":
                raise ValueError("只能确认待审核记忆")
            conflicts = self.memory_conflicts(id)
            if conflicts and not replace_conflicts:
                raise ValueError("检测到同对象同属性的事实冲突，请明确确认替代旧事实")
            now = utc_now()
            replaced_ids = {record["id"] for record in conflicts}
            # A replacement is a new claim backed by source messages. Its old
            # conflicting claim cannot remain a derivation dependency.
            candidate["dependencies"] = {key: value for key, value in candidate.get("dependencies", {}).items()
                                          if key not in replaced_ids}
            for old in conflicts:
                old["status"] = "superseded"
                old["version"] += 1
                old["updated_at"] = now
                old["superseded_by"] = id
                old["valid_to"] = now
                old["blocked_source_ids"] = list(dict.fromkeys(old.get("blocked_source_ids", []) + old.get("source_ids", [])))
                self._save_memory(old)
            previous = candidate.get("supersedes") or []
            previous = [previous] if isinstance(previous, str) else previous
            candidate.update(status="active", version=candidate["version"] + 1, updated_at=now,
                             supersedes=list(dict.fromkeys(previous + sorted(replaced_ids))))
            return self._save_memory(candidate)

    def reject_memory(self, id: str) -> dict:
        with self._lock, self._conn:
            record = self.get_memory(id)
            if record["status"] == "rejected":
                return record
            if record["status"] != "pending":
                raise ValueError("只能拒绝待审核记忆")
            record.update(status="rejected", version=record["version"] + 1, updated_at=utc_now())
            record["blocked_source_ids"] = list(dict.fromkeys(record.get("blocked_source_ids", []) + record.get("source_ids", [])))
            return self._save_memory(record)

    def ingest_memory_candidates(self, records: list[dict], *, scope_id: str, user_id: str,
                                 source_ids: list[str]) -> list[dict]:
        """Accept model suggestions only against actual, authorized user input."""
        if not isinstance(records, list) or len(records) > 30:
            raise ValueError("候选记忆必须为列表且每批最多 30 条")
        scope_id, user_id = _text(scope_id, "scope_id"), _text(user_id, "user_id")
        sources = _strings(source_ids, "source_ids")
        if not sources:
            raise ValueError("候选记忆必须附真实用户消息来源")
        with self._lock, self._conn:
            for source_id in sources:
                try:
                    source = self._get("messages", source_id)
                except KeyError as exc:
                    raise ValueError("候选记忆来源消息不存在") from exc
                if source["scope_id"] != scope_id or source["user_id"] != user_id or source["role"] != "user":
                    raise ValueError("来源不属于当前聊天中的当前用户")
            existing = self.list_memories(scope_id)
            blocked = {source_id for item in existing for source_id in item.get("blocked_source_ids", [])}
            # Tombstones are intentionally absent from normal management lists.
            blocked.update(source_id for item in self.list_memories(scope_id, status="deleted")
                           for source_id in item.get("blocked_source_ids", []))
            prepared = []
            for raw in records:
                item = _data(raw)
                if item.get("scope_id", scope_id) != scope_id or item.get("user_id", user_id) != user_id:
                    raise ValueError("候选记忆不得扩大来源作用域或更换来源用户")
                refs = _strings(item.get("source_ids", sources), "source_ids")
                if not refs or not set(refs).issubset(sources):
                    raise ValueError("候选来源必须属于已授权消息")
                if set(refs) & blocked:
                    continue
                # Model-generated candidates do not inherit arbitrary memory
                # dependencies, approval flags or ownership from their payload.
                item.update(scope_id=scope_id, user_id=user_id, source_ids=refs,
                            status="pending", subject=item.get("subject") or user_id,
                            dependencies={}, blocked_source_ids=[], shareable=False)
                if not scope_id.startswith(("qq:group:", "qqofficial:group:")):
                    item["visibility"] = "private"
                item.pop("id", None)
                item.pop("supersedes", None)
                probe = {"kind": "fact", "subject": user_id, "visibility": "group" if scope_id.startswith(("qq:group:", "qqofficial:group:")) else "private",
                         "confidence": 0.5, "importance": 0.5, "pinned": False, **item}
                self._prepare_memory(probe)
                prepared.append(probe)
            result = []
            for item in prepared:
                duplicate = next((old for old in existing if old["content"] == item["content"]
                                  and old["source_ids"] == item["source_ids"] and old.get("property", "") == item.get("property", "")), None)
                if duplicate:
                    result.append(duplicate)
                else:
                    created = self.create_memory(item)
                    existing.append(created)
                    result.append(created)
            return result

    def _prepare_jargon(self, record: dict) -> dict:
        for field in ("scope_id", "term", "meaning"):
            record[field] = _text(record[field], field)
        for field in ("topic", "example", "created_by"):
            record[field] = _text(record[field], field, required=False)
        _choice(record["status"], JARGON_STATUSES, "status")
        _boolean(record["use_allowed"], "use_allowed")
        record["source_ids"] = _strings(record["source_ids"], "source_ids")
        record["blocked_source_ids"] = _strings(record.get("blocked_source_ids", []), "blocked_source_ids")
        return record

    def _save_jargon(self, record: dict) -> dict:
        result = self._write("jargon", record, ("scope_id", "term", "status", "version", "created_at", "updated_at"))
        self._conn.execute("INSERT INTO jargon_versions(jargon_id,version,created_at,data) VALUES (?,?,?,?)",
                           (record["id"], record["version"], record["updated_at"], _json(record)))
        return result

    def create_jargon(self, data: dict) -> dict:
        incoming, now = _data(data), utc_now()
        record = {"scope_id": "web:default", "topic": "", "example": "", "source_ids": [],
                  "status": "pending", "use_allowed": False, "created_by": "owner", **incoming,
                  "id": _text(incoming.get("id") or str(uuid4()), "id"), "version": 1,
                  "created_at": now, "updated_at": now}
        self._prepare_jargon(record)
        if record["status"] == "deleted":
            raise ValueError("新词条不能为 deleted")
        with self._lock, self._conn:
            if self._conn.execute("SELECT 1 FROM jargon WHERE id=?", (record["id"],)).fetchone():
                raise ValueError("词条 ID 已存在")
            self.get_scope(record["scope_id"])
            return self._save_jargon(record)

    def list_jargon(self, scope_id: str | None = None, q: str = "", status: str | None = None) -> list[dict]:
        clauses, params = self._filters(scope_id, status, JARGON_STATUSES)
        if status is None:
            clauses.append("status!='deleted'")
        return self._search_text(self._list("jargon", clauses, params, "updated_at DESC,rowid DESC"), q, ("term", "meaning", "topic", "example"))

    def get_jargon(self, id: str) -> dict:
        return self._get("jargon", id)

    def update_jargon(self, id: str, patch: dict) -> dict:
        patch = _data(patch)
        with self._lock, self._conn:
            old = self.get_jargon(id)
            if old["status"] == "deleted":
                raise ValueError("已删除词条不能恢复，请创建新词条")
            if patch.get("status") == "deleted":
                return self.delete_jargon(id)
            record = {**old, **patch, "id": id, "version": old["version"] + 1,
                      "created_at": old["created_at"], "updated_at": utc_now()}
            record["blocked_source_ids"] = list(dict.fromkeys(old.get("blocked_source_ids", [])
                + _strings(patch.get("blocked_source_ids", []), "blocked_source_ids")))
            if (("meaning" in patch and patch["meaning"] != old["meaning"])
                    or patch.get("status") in {"disabled", "rejected"}):
                record["blocked_source_ids"] = list(dict.fromkeys(
                    record["blocked_source_ids"] + old.get("source_ids", [])
                ))
            self._prepare_jargon(record)
            self.get_scope(record["scope_id"])
            return self._save_jargon(record)

    def delete_jargon(self, id: str) -> dict:
        with self._lock, self._conn:
            old = self.get_jargon(id)
            if old["status"] == "deleted":
                return old
            record = {"id": id, "scope_id": old["scope_id"], "term": "", "meaning": "", "topic": "", "example": "",
                      "entry_type": old.get("entry_type", "jargon"),
                      "source_ids": [], "status": "deleted", "use_allowed": False, "created_by": old["created_by"],
                      "blocked_source_ids": list(dict.fromkeys(old.get("blocked_source_ids", []) + old.get("source_ids", []))),
                      "version": old["version"] + 1, "created_at": old["created_at"], "updated_at": utc_now()}
            self._conn.execute("DELETE FROM jargon_versions WHERE jargon_id=?", (id,))
            return self._save_jargon(record)

    def jargon_visible(self, scope_id: str, record: dict) -> bool:
        if record.get('scope_id') == scope_id:
            return True
        config = self._timezone_settings.get() if self._timezone_settings else {}
        if not config or config.get('language', {}).get('group_isolation', False):
            return False
        origin = record.get('scope_id', '')
        if ':group:' not in scope_id or ':group:' not in origin or record.get('entry_type', 'jargon') != 'jargon':
            return False
        try:
            return self.get_scope(origin).get('enabled', True) and self.get_scope(scope_id).get('enabled', True)
        except KeyError:
            return False

    def match_jargon(self, scope_id: str, text: str, limit: int = 6) -> list[dict]:
        limit, text = _limit(limit, 50), _text(text, "text", required=False)
        matches = []
        with self._lock:
            rows = self._conn.execute("SELECT data FROM jargon WHERE status='approved' AND term<>'' AND instr(lower(?),lower(term))>0 ORDER BY (scope_id=?) DESC,updated_at DESC LIMIT 200",(text,scope_id)).fetchall()
        for row in rows:
            record = json.loads(row[0])
            if not self.jargon_visible(scope_id, record):
                continue
            if record.get("entry_type", "jargon") == "expression":
                continue
            term = record["term"]
            # ASCII letters/numbers need word boundaries; Chinese phrases match
            # naturally without requiring spaces between words.
            left = r"(?<![A-Za-z0-9_])" if term[0].isascii() and (term[0].isalnum() or term[0] == "_") else ""
            right = r"(?![A-Za-z0-9_])" if term[-1].isascii() and (term[-1].isalnum() or term[-1] == "_") else ""
            match = re.search(left + re.escape(term) + right, text, flags=re.IGNORECASE)
            if match:
                matches.append((match.start(), -len(term), record))
        matches.sort(key=lambda item: (item[0], item[1], item[2]["id"]))
        return [record for _, _, record in matches[:limit]]

    def _prepare_task(self, record: dict) -> dict:
        from .timeparse import validate_calendar
        for field in ("scope_id", "user_id", "title"):
            record[field] = _text(record[field], field)
        _choice(record["kind"], {"todo", "reminder"}, "kind")
        _choice(record["status"], TASK_STATUSES, "status")
        _choice(record["repeat"], {"none", "daily", "weekly"}, "repeat")
        _choice(record["delivery_status"], {"pending", "delivered", "failed"}, "delivery_status")
        record["due_at"] = _iso(record.get("due_at"), "due_at")
        if record["kind"] == "reminder" and record["due_at"] is None:
            raise ValueError("提醒必须设置 due_at")
        if record["repeat"] != "none" and record["due_at"] is None:
            raise ValueError("重复任务必须设置 due_at")
        if record['due_at']:
            validate_calendar(record, self._timezone_settings)
        key = record.get("idempotency_key")
        record["idempotency_key"] = _text(key, "idempotency_key", required=False) or None if key is not None else None
        record["error"] = _text(record.get("error", ""), "error", required=False)
        return record

    def create_task(self, data: dict) -> dict:
        incoming, now = _data(data), utc_now()
        key = incoming.get("idempotency_key")
        if key is not None:
            key = _text(key, "idempotency_key", required=False) or None
        with self._lock, self._conn:
            if key:
                existing = self._conn.execute("SELECT data FROM tasks WHERE idempotency_key=?", (key,)).fetchone()
                if existing:
                    return json.loads(existing[0])
            record = {"scope_id": "web:default", "user_id": "owner", "kind": "todo", "status": "pending",
                      "due_at": None, "repeat": "none", "delivery_status": "pending", "result": "", "error": "",
                      **incoming, "idempotency_key": key, "id": _text(incoming.get("id") or str(uuid4()), "id"),
                      "created_at": now, "updated_at": now}
            self._prepare_task(record)
            if self._conn.execute("SELECT 1 FROM tasks WHERE id=?", (record["id"],)).fetchone():
                raise ValueError("任务 ID 已存在")
            self.get_scope(record["scope_id"])
            return self._write("tasks", record, ("scope_id", "status", "due_at", "idempotency_key", "created_at", "updated_at"))

    def list_tasks(self, scope_id: str | None = None, status: str | None = None) -> list[dict]:
        clauses, params = self._filters(scope_id, status, TASK_STATUSES)
        return self._list("tasks", clauses, params, "created_at DESC,rowid DESC")

    def get_task(self, id: str) -> dict:
        return self._get("tasks", id)

    def update_task(self, id: str, patch: dict) -> dict:
        patch = _data(patch)
        with self._lock, self._conn:
            old = self.get_task(id)
            record = {**old, **patch, "id": id, "created_at": old["created_at"], "updated_at": utc_now()}
            if record.get('due_at') and (('due_at' in patch and patch['due_at'] != old.get('due_at'))
                                          or ('timezone' in patch and patch['timezone'] != old.get('timezone'))):
                from .timeparse import calendar_fields
                calendar = calendar_fields(record['due_at'], self._timezone_settings, timezone_name=record.get('timezone'))
                for key, value in calendar.items():
                    if key not in patch:
                        record[key] = value
            self._prepare_task(record)
            self.get_scope(record["scope_id"])
            try:
                return self._write("tasks", record, ("scope_id", "status", "due_at", "idempotency_key", "created_at", "updated_at"))
            except sqlite3.IntegrityError as exc:
                raise ValueError("idempotency_key 已被其他任务使用") from exc

    def due_tasks(self, now_iso: str) -> list[dict]:
        now = _iso(now_iso, "now_iso", nullable=False)
        records = self._list("tasks", ["due_at IS NOT NULL", "due_at<=?", "status IN ('pending','running')"], [now], "due_at,created_at,rowid")
        return [record for record in records if record["kind"] == "reminder" and record["delivery_status"] != "delivered"]

    @staticmethod
    def _filters(scope_id: str | None, status: str | None, statuses: set[str]) -> tuple[list[str], list]:
        clauses, params = [], []
        if scope_id is not None:
            clauses.append("scope_id=?")
            params.append(scope_id)
        if status is not None:
            _choice(status, statuses, "status")
            clauses.append("status=?")
            params.append(status)
        return clauses, params

    @staticmethod
    def _search_text(records: list[dict], q: str, fields: tuple[str, ...]) -> list[dict]:
        query = _text(q, "q", required=False).casefold()
        return records if not query else [record for record in records if any(query in str(record.get(field, "")).casefold() for field in fields)]

    def record_usage(self, data: dict) -> dict:
        incoming = _redact(_data(data))
        input_tokens = incoming.get("input_tokens", incoming.get("prompt_tokens", 0))
        output_tokens = incoming.get("output_tokens", incoming.get("completion_tokens", 0))
        for name, value in (("input_tokens", input_tokens), ("output_tokens", output_tokens)):
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{name} 必须为非负整数")
        record = {**incoming, "id": str(uuid4()), "created_at": _iso(incoming.get("created_at") or utc_now(), "created_at", nullable=False),
                  "input_tokens": input_tokens, "output_tokens": output_tokens,
                  "cost": _number(incoming.get("cost", 0.0), "cost")}
        with self._lock, self._conn:
            return self._write("usage", record, ("created_at", "input_tokens", "output_tokens", "cost"))

    def list_usage(self, limit: int = 100) -> list[dict]:
        limit = _limit(limit, 10000)
        with self._lock:
            rows = self._conn.execute("SELECT data FROM usage ORDER BY created_at DESC,rowid DESC LIMIT ?", (limit,)).fetchall()
            return [json.loads(row[0]) for row in rows]

    def usage_totals(self, scope_id: str | None = None, day: date | datetime | str | None = None) -> dict:
        """Aggregate the configured calendar day, without list limits.

        The default remains UTC+8; aware datetimes use the selected timezone.
        """
        zone = config_zone(self._timezone_settings)
        if day is None:
            selected = datetime.now(zone).date()
        elif isinstance(day, datetime):
            selected = (day.replace(tzinfo=zone) if day.tzinfo is None else day.astimezone(zone)).date()
        elif isinstance(day, date):
            selected = day
        elif isinstance(day, str):
            try:
                if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", day):
                    raise ValueError()
                selected = date.fromisoformat(day)
            except ValueError as exc:
                raise ValueError("day 必须为有效的 YYYY-MM-DD 日期") from exc
        else:
            raise ValueError("day 必须为日期、时间或 YYYY-MM-DD 字符串")
        try:
            start = datetime.combine(selected, datetime.min.time(), tzinfo=zone)
            start_iso = _iso(start.isoformat(), "day")
            end_iso = _iso((start + timedelta(days=1)).isoformat(), "day")
        except (OverflowError, ValueError) as exc:
            raise ValueError("day 超出可聚合日期范围") from exc
        clauses, params = ["created_at>=?", "created_at<?"], [start_iso, end_iso]
        if scope_id is not None:
            clauses.append("json_extract(data,'$.scope_id')=?")
            params.append(_text(scope_id, "scope_id"))
        with self._lock:
            row = self._conn.execute(
                "SELECT COALESCE(SUM(input_tokens),0),COALESCE(SUM(output_tokens),0),COALESCE(SUM(cost),0) "
                "FROM usage WHERE " + " AND ".join(clauses), params,
            ).fetchone()
            input_tokens, output_tokens, cost = int(row[0]), int(row[1]), float(row[2])
            return {"input_tokens": input_tokens, "output_tokens": output_tokens,
                    "tokens": input_tokens + output_tokens, "cost": cost}

    def add_event(self, kind: str, message: str, **details: Any) -> dict:
        kind, message = _text(kind, "kind"), _text(message, "message", required=False)
        record = {"id": str(uuid4()), "kind": kind, "message": message, "details": _redact(_data(details)), "created_at": utc_now()}
        with self._lock, self._conn:
            return self._write("events", record, ("created_at", "kind"))

    def add_exception(self, kind: str, message: str, exc: BaseException, **details) -> dict:
        error_id = uuid4().hex[:12]
        clean = getattr(self, 'exception_redactor', None)
        if clean is None:
            # A standalone bridge may not have installed the settings redactor.
            # Keep frame locations, but never persist raw source lines/messages.
            trace = '\n'.join(f'  File "{f.filename}", line {f.lineno}, in {f.name}'
                              for f in traceback.extract_tb(exc.__traceback__)) + '\n' + type(exc).__name__
            clean = lambda value: '[异常内容未记录：脱敏器尚未初始化]'
        else:
            trace = clean(''.join(traceback.format_exception(type(exc), exc, exc.__traceback__)))
        details.update(error=type(exc).__name__, error_id=error_id,
                       error_message=clean(str(exc))[:1000], traceback=trace[-18000:])
        logging.getLogger('mikubot.runtime').error('%s [%s]\n%s', message, error_id, trace[-18000:])
        return self.add_event(kind, message, **details)

    def list_events(self, limit: int = 50) -> list[dict]:
        limit = _limit(limit)
        with self._lock:
            rows = self._conn.execute("SELECT data FROM events ORDER BY created_at DESC,rowid DESC LIMIT ?", (limit,)).fetchall()
            return [json.loads(row[0]) for row in rows]

    def memory_events(self, kind: str, scope_id: str, user_id: str | None = None, limit: int = 200) -> list[dict]:
        """Bounded derived metadata in schema-1's existing events table."""
        if kind not in {"memory_use", "memory_organization"}:
            raise ValueError("不支持的记忆事件类型")
        clauses, params = ["kind=?", "json_extract(data,'$.details.scope_id')=?"], [kind, scope_id]
        if user_id is not None:
            clauses.append("json_extract(data,'$.details.user_id')=?")
            params.append(user_id)
        with self._lock:
            return self._list("events", clauses, params, f"rowid DESC LIMIT {_limit(limit, 1000)}")

    def record_memory_use(self, scope_id: str, user_id: str, versions: dict[str, int], request_id: str, *, scope_allowed=None) -> dict:
        """Reinforce actual use without editing facts or their dependency IDs."""
        if not isinstance(versions, dict) or len(versions) > 32 or any(not isinstance(key, str) or type(value) is not int or value < 1 for key, value in versions.items()):
            raise ValueError("记忆引用最多 32 条，须含实际版本")
        request_id = _text(request_id, "request_id")
        if len(request_id) > 240:
            raise ValueError("request_id 过长")
        request_hash = hashlib.sha256(request_id.encode()).hexdigest()
        now, used = utc_now(), []
        with self._lock, self._conn:
            candidates = []
            for key in versions:
                try: candidates.append(self.get_memory(key))
                except KeyError: pass
            visible = {record["id"]: record for record in self._allowed_memories(scope_id, user_id, scope_allowed=scope_allowed, roots=candidates)}
            for record_id, version in versions.items():
                current = visible.get(record_id)
                if not current or current["version"] != version:
                    continue
                identity = _json([scope_id, user_id, record_id, version])
                event_id = "memory-use:" + hashlib.sha256(identity.encode()).hexdigest()
                try:
                    event = self._get("events", event_id)
                    previous = event["details"]
                except KeyError:
                    event, previous = {"id": event_id, "created_at": now}, {}
                requests = previous.get("requests", [])
                if request_hash in requests:
                    continue
                details = {"scope_id": scope_id, "user_id": user_id, "memory_id": record_id, "memory_version": version,
                           "count": min(1000000, int(previous.get("count", 0)) + 1), "last_used_at": now,
                           "requests": (requests + [request_hash])[-64:]}
                self._write("events", {**event, "kind": "memory_use", "message": "已使用当前授权记忆", "details": details}, ("created_at", "kind"))
                used.append(record_id)
        return {"used": used, "count": len(used)}

    def memory_use_stats(self, scope_id: str, user_id: str) -> dict[str, dict]:
        result = {}
        for event in self.memory_events("memory_use", scope_id, user_id, limit=512):
            details = event["details"]
            # The query is newest-first: never let an older fact version win.
            result.setdefault(details["memory_id"], {key: details[key] for key in ("memory_version", "count", "last_used_at")})
        return result

    def backup(self, destination: Path, include_messages: bool = True) -> Path:
        destination = Path(destination)
        if destination.is_dir():
            destination = destination / "qunyu.db"
        if destination.resolve() == self.path.resolve():
            raise ValueError("备份不能覆盖正在使用的数据库")
        _boolean(include_messages, "include_messages")
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_name(destination.name + f".{uuid4().hex}.tmp")
        try:
            with self._lock:
                target = sqlite3.connect(temporary)
                try:
                    self._conn.backup(target)
                    with target:
                        if not include_messages:
                            target.execute("DELETE FROM messages")
                        for table in TABLES:
                            for rowid, payload in target.execute(f"SELECT rowid,data FROM {table}").fetchall():
                                cleaned = _json(_redact(json.loads(payload)))
                                if cleaned != payload:
                                    target.execute(f"UPDATE {table} SET data=? WHERE rowid=?", (cleaned, rowid))
                    target.execute("PRAGMA journal_mode=DELETE")
                    target.execute("VACUUM")
                finally:
                    target.close()
            os.replace(temporary, destination)
            return destination
        finally:
            temporary.unlink(missing_ok=True)

    def close(self) -> None:
        with self._lock:
            if not self._closed:
                if self._memory_search is not None:
                    self._memory_search.close()
                    self._memory_search = None
                self._conn.close()
                self._closed = True

    def restore_from(self, candidate: Path, *, validate_only=False) -> dict:
        """Validate an exact-schema snapshot, then restore into this live object.

        The caller stops workers and writes its pre-restore backup. Validation
        happens entirely before SQLite's online backup touches the live DB.
        """
        candidate = Path(candidate)
        if candidate.resolve() == self.path.resolve() or not candidate.is_file():
            raise ValueError("恢复文件无效")
        source = None
        try:
            source = sqlite3.connect(candidate.resolve().as_uri() + "?mode=ro", uri=True, timeout=10)
            with self._lock:
                if source.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                    raise ValueError("备份数据库完整性校验失败")
                if source.execute("PRAGMA user_version").fetchone()[0] != self._conn.execute("PRAGMA user_version").fetchone()[0]:
                    raise ValueError("备份数据库版本不兼容")
                objects = source.execute("SELECT type,name,sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' ORDER BY type,name").fetchall()
                current = self._conn.execute("SELECT type,name,sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' ORDER BY type,name").fetchall()
                if any(row[0] in {"trigger", "view"} for row in objects):
                    raise ValueError("备份不得含有触发器或视图")
                signature = lambda rows: [(row[0], row[1], " ".join((row[2] or "").split())) for row in rows]
                # Only these additive performance indexes may be absent from
                # older schema-1 backups. Any present index must still match
                # its exact trusted definition; no arbitrary schema is allowed.
                history_indexes = {'messages_origin_day', 'messages_extraction_cursor'}
                missing = history_indexes - {row[1] for row in objects if row[0] == 'index'}
                expected = [row for row in current if not (row[0] == 'index' and row[1] in missing)]
                if signature(objects) != signature(expected):
                    raise ValueError("备份数据库结构不兼容")
                tables = {row[1] for row in objects if row[0] == "table"}
                if tables != set(TABLES):
                    raise ValueError("备份包含未允许的表")
                for table in TABLES:
                    columns = [row[1] for row in source.execute(f"PRAGMA table_info({table})")]
                    for row in source.execute(f"SELECT * FROM {table}"):
                        payload = json.loads(row[columns.index("data")])
                        if not isinstance(payload, dict):
                            raise ValueError("备份记录格式无效")
                        for column, value in zip(columns, row):
                            if column == "created_at" and table.endswith("_versions"):
                                expected = payload.get("updated_at")
                            else:
                                expected = payload.get(column)
                            if column != "data" and column not in {"memory_id", "jargon_id"} and expected != value:
                                raise ValueError("备份记录索引与正文不一致")
                if validate_only:
                    return {'valid': True}
                source.backup(self._conn)
                for row in current:
                    if row[0] == 'index' and row[1] in missing:
                        self._conn.execute(row[2])
                self._conn.execute("PRAGMA journal_mode=WAL")
                self._memory_search_dirty = None
                self._memory_revision += 1
                self._init_blocked_source_tracking()
                self._seed()
                return self.stats()
        except (sqlite3.DatabaseError, json.JSONDecodeError, TypeError) as exc:
            raise ValueError("无法读取有效的mikubot数据库备份") from exc
        finally:
            if source is not None:
                source.close()
