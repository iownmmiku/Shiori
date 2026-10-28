"""Read-only local diagnostics. No connection probes or credential echoing."""
from __future__ import annotations

import inspect
from urllib.parse import urlsplit, urlunsplit

from .db import SECRET_FIELDS, utc_now


def _settings(settings):
    return settings if isinstance(settings, dict) else settings.get()


def _secret_values(value):
    found = set()
    if isinstance(value, dict):
        for key, child in value.items():
            if str(key).casefold() in SECRET_FIELDS and isinstance(child, str) and child:
                found.add(child)
            else:
                found.update(_secret_values(child))
    elif isinstance(value, list):
        for child in value:
            found.update(_secret_values(child))
    return found


def _safe(value, secrets):
    if isinstance(value, dict):
        return {str(key): _safe(child, secrets) for key, child in value.items()
                if str(key).casefold() not in SECRET_FIELDS and str(key).casefold() not in {"headers", "request_body", "response_body", "messages"}}
    if isinstance(value, (list, tuple)):
        return [_safe(child, secrets) for child in value]
    if isinstance(value, str):
        for secret in sorted(secrets, key=len, reverse=True):
            value = value.replace(secret, "[已隐藏]")
        if value.startswith(("http://", "https://", "ws://", "wss://")):
            try:
                parts = urlsplit(value)
                hostname = parts.hostname or ""
                if ":" in hostname:
                    hostname = "[" + hostname + "]"
                authority = hostname + (":" + str(parts.port) if parts.port else "")
                return urlunsplit((parts.scheme, authority, parts.path, "", ""))
            except ValueError:
                return "[无效地址]"
        return value[:2000]
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return type(value).__name__


def _status(target):
    if target is None or not callable(getattr(target, "status", None)):
        return {"available": False}
    try:
        result = target.status()
        if inspect.isawaitable(result):
            if inspect.iscoroutine(result):
                result.close()
            return {"available": False, "reason": "status_requires_async"}
        return result if isinstance(result, dict) else {"available": False}
    except Exception as exc:
        return {"available": False, "error": type(exc).__name__}


def build_diagnostics(store, settings, provider, bridge):
    """Return local facts, not a claim that a configured service was contacted."""
    cfg = _settings(settings)
    secrets = _secret_values(cfg)
    token = getattr(settings, "token", None)
    if token:
        secrets.add(str(token))
    model_cfg = cfg.get("model", {})
    raw_status, raw_bridge = _status(provider), _status(bridge)
    secrets.update(_secret_values(raw_status))
    secrets.update(_secret_values(raw_bridge))
    status = _safe(raw_status, secrets)
    bridge_status = _safe(raw_bridge, secrets)
    totals = store.usage_totals()
    limits = cfg.get("budget", {})
    tokens_limit = int(limits.get("daily_tokens") or 0)
    cost_limit = float(limits.get("daily_cost") or 0)
    exhausted = bool(tokens_limit and totals["tokens"] >= tokens_limit or cost_limit and totals["cost"] >= cost_limit)
    budget = {**totals, "timezone": "Asia/Hong_Kong", "daily_tokens": tokens_limit, "daily_cost": cost_limit,
              "tokens_remaining": max(0, tokens_limit - totals["tokens"]) if tokens_limit else None,
              "cost_remaining": max(0, cost_limit - totals["cost"]) if cost_limit else None,
              "exhausted": exhausted}
    memory = {**store.memory_review_counts(),
              "semantic_enabled": bool(cfg.get("memory", {}).get("semantic_enabled")),
              "embedding_model_set": bool(cfg.get("memory", {}).get("embedding_model"))}
    scopes = store.list_scopes()
    context = {"enabled": bool(cfg.get("context", {}).get("enabled")),
               "enabled_scopes": sum(bool(scope.get("enabled", True) and scope.get("context_compress")) for scope in scopes),
               "digests": memory.pop('digests')}
    adapter = cfg.get("onebot", {}).get("adapter", "napcat")
    official = adapter == "official"
    qq_cfg = cfg.get("qq_official" if official else "onebot", {})
    qq = {"adapter": adapter, "enabled": bool(qq_cfg.get("enabled")), "status": bridge_status,
          "chat_policy": qq_cfg.get('chat_policy', 'whitelist'),
          "allowed_groups": len(qq_cfg.get("allowed_groups") or []),
          "allowed_users": len(set(map(str, qq_cfg.get('allowed_users') or [])) |
                               (set(map(str, qq_cfg.get('owner_ids') or [])) if not official else set()))}
    resolver = getattr(provider, "resolve", None)
    chat_model, embedding_model = model_cfg, {"model": cfg.get("memory", {}).get("embedding_model", "")}
    if callable(resolver):
        try:
            chat_model, embedding_model = resolver("chat"), resolver("embedding")
        except Exception:
            chat_model, embedding_model = {}, {}
    configured = bool(chat_model.get("enabled", True) and chat_model.get("base_url") and chat_model.get("model"))
    memory["embedding_model_set"] = bool(embedding_model.get("enabled", True) and embedding_model.get("model"))
    providers, routes = status.get("providers", []), status.get("routes", {})
    model = {"configured": bool(configured), "model": str(chat_model.get("model") or ""),
             "api_key_set": bool(chat_model.get("api_key")), "network_tested": False,
             "status": status, "providers": providers, "routes": routes}
    pending = memory["pending"] + memory["jargon_pending"] + memory["expression_pending"]
    checks = [
        {"id": "runtime", "label": "运行状态", "status": "warning" if cfg.get("paused") else "ok", "message": "当前已暂停" if cfg.get("paused") else "本地服务正在运行"},
        {"id": "model", "label": "模型配置", "status": "ok" if configured else "warning", "message": "已配置；本次未测试网络" if configured else "尚未配置可用模型路由"},
        {"id": "qq", "label": "QQ 连接", "status": "info" if not qq["enabled"] else "ok" if bridge_status.get("connected") else "warning", "message": "未启用 QQ" if not qq["enabled"] else "已连接" if bridge_status.get("connected") else "已启用但当前未连接"},
        {"id": "budget", "label": "今日预算", "status": "warning" if exhausted else "ok", "message": "今日预算已达到限额" if exhausted else "按香港自然日聚合全部用量"},
        {"id": "review", "label": "待审核记录", "status": "info" if pending else "ok", "message": f"有 {pending} 条记忆、词条或表达待审"},
        {"id": "context", "label": "长对话整理", "status": "ok" if context["enabled"] else "info", "message": "全局已启用，仍须单会话开启" if context["enabled"] else "自动整理默认关闭；手动整理需明确触发"},
    ]
    if qq['enabled']:
        last_message = bridge_status.get('last_message_result') or {}
        checks.append({'id': 'qq_messages', 'label': 'QQ 消息处理',
            'status': 'warning' if last_message.get('stage') in ('blocked', 'failed') else 'ok' if last_message.get('code') == 'sent' else 'info',
            'message': bridge_status.get('diagnostic') or '请发送一条测试消息，并在连接页查看是否收到事件及回复。'})
    return _safe({"ok": not any(check["status"] == "error" for check in checks), "checks": checks,
                  "model": model, "providers": providers, "routes": routes, "qq": qq, "budget": budget,
                  "memory": memory, "context": context, "paused": bool(cfg.get("paused")), "generated_at": utc_now(),
                  "network_tested": False}, secrets)
