"""Incident state and admin-facing diagnostics for the video monitor."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any

from config.settings import get_settings
from services.redis_service import get_redis_client

logger = logging.getLogger("services.monitor_incident_service")
settings = get_settings()

INCIDENT_KEY_PREFIX = "videomonitor:incident:"

INCIDENT_NAVIGATION_TIMEOUT_STORM = "navigation_timeout_storm"
INCIDENT_HARD_PROXY_FAILURE = "hard_proxy_failure"
INCIDENT_LOCAL_BROWSER_INFRA = "local_browser_infra"
INCIDENT_ACCOUNT_POOL_LOW = "account_pool_low"
INCIDENT_TELEGRAM_DELIVERY = "telegram_delivery_issue"
INCIDENT_MONITOR_BACKLOG = "monitor_backlog"
INCIDENT_MOBILE_PROXY_PRESSURE = "mobile_proxy_pressure"
INCIDENT_TIKTOK_REGION_DEGRADATION = "tiktok_region_degradation"
INCIDENT_RECOVERED = "recovered"

_fallback_incidents: dict[str, dict[str, Any]] = {}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _ttl_seconds(default_minutes: int = 60) -> int:
    try:
        minutes = int(getattr(settings, "MONITOR_INCIDENT_TTL_MINUTES", default_minutes))
    except (TypeError, ValueError):
        minutes = default_minutes
    return max(60, minutes * 60)


def _redis():
    try:
        return get_redis_client()
    except Exception as exc:
        logger.debug("Incident Redis unavailable, using in-memory fallback: %s", exc)
        return None


def _coerce_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        return [str(item) for item in value if str(item).strip()]
    text = str(value).strip()
    return [text] if text else []


def _incident_key(kind: str) -> str:
    return f"{INCIDENT_KEY_PREFIX}{kind}"


def classify_monitor_error(error_text: str, *, error_type: str | None = None) -> str:
    """Return a stable incident class for common monitor failures."""
    text = f"{error_type or ''} {error_text or ''}".lower()
    if "telegramnetworkerror" in text or "api.telegram.org" in text:
        return INCIDENT_TELEGRAM_DELIVERY
    if (
        "target page, context or browser has been closed" in text
        or "target closed" in text
        or "failed to create browser context" in text
        or "browser has been closed" in text
    ):
        return INCIDENT_LOCAL_BROWSER_INFRA
    if (
        "err_proxy_connection_failed" in text
        or "err_tunnel_connection_failed" in text
        or "proxy authentication" in text
        or "407" in text
    ):
        return INCIDENT_HARD_PROXY_FAILURE
    if "page.goto" in text and "err_timed_out" in text:
        return INCIDENT_NAVIGATION_TIMEOUT_STORM
    if (
        "temporary page error" in text
        or "something went wrong" in text
        or "please try again later" in text
        or "unable to load" in text
        or "failed to load" in text
    ):
        return INCIDENT_TIKTOK_REGION_DEGRADATION
    return "unknown"


def build_admin_alert(
    *,
    severity: str,
    title: str,
    what: str,
    why: str,
    auto_action: str,
    admin_action: str,
    next_check: str | None = None,
    affected: list[str] | tuple[str, ...] | None = None,
) -> str:
    """Build one consistent, readable owner notification."""
    severity_label = {
        "critical": "CRITICAL",
        "warning": "WARN",
        "info": "INFO",
        "recovered": "OK",
    }.get((severity or "warning").lower(), "WARN")
    lines = [
        f"[{severity_label}] {title}",
        "",
        "Что происходит:",
        what.strip() or "Нет деталей.",
        "",
        "Почему софт так решил:",
        why.strip() or "Нет данных.",
    ]
    affected_items = _coerce_list(affected)
    if affected_items:
        lines.extend(["", "Затронуто:", ", ".join(affected_items[:8])])
    lines.extend(
        [
            "",
            "Что софт уже сделал:",
            auto_action.strip() or "Пока только записал инцидент.",
            "",
            "Что нужно админу:",
            admin_action.strip() or "Пока ничего, софт наблюдает.",
        ]
    )
    if next_check:
        lines.extend(["", "Следующая проверка:", next_check.strip()])
    return "\n".join(lines)


def record_incident(
    kind: str,
    *,
    severity: str = "warning",
    title: str | None = None,
    cause: str | None = None,
    evidence: list[str] | tuple[str, ...] | str | None = None,
    auto_action: str | None = None,
    admin_action: str | None = None,
    affected: list[str] | tuple[str, ...] | str | None = None,
    cooldown_until: datetime | str | None = None,
    state: str = "active",
    ttl_minutes: int | None = None,
) -> dict[str, Any]:
    now = _now_iso()
    if isinstance(cooldown_until, datetime):
        cooldown_until_value = cooldown_until.isoformat()
    else:
        cooldown_until_value = cooldown_until
    payload: dict[str, Any] = {
        "type": kind,
        "severity": severity,
        "title": title or kind,
        "cause": cause or "",
        "evidence": _coerce_list(evidence),
        "auto_action": auto_action or "",
        "admin_action": admin_action or "",
        "affected": _coerce_list(affected),
        "cooldown_until": cooldown_until_value,
        "state": state,
        "created_at": now,
        "updated_at": now,
    }

    existing = get_incident(kind)
    if existing and existing.get("created_at"):
        payload["created_at"] = existing["created_at"]

    ttl = max(60, int(ttl_minutes * 60 if ttl_minutes is not None else _ttl_seconds()))
    redis_client = _redis()
    if redis_client:
        try:
            redis_client.set(_incident_key(kind), json.dumps(payload, ensure_ascii=False), ex=ttl)
        except Exception as exc:
            logger.debug("Failed to write monitor incident %s to Redis: %s", kind, exc)
            _fallback_incidents[kind] = payload
    else:
        _fallback_incidents[kind] = payload
    return payload


def get_incident(kind: str) -> dict[str, Any] | None:
    redis_client = _redis()
    if redis_client:
        try:
            raw = redis_client.get(_incident_key(kind))
            if raw:
                return json.loads(raw)
        except Exception as exc:
            logger.debug("Failed to read monitor incident %s from Redis: %s", kind, exc)
    return _fallback_incidents.get(kind)


def clear_incident(kind: str) -> bool:
    existed = get_incident(kind) is not None
    redis_client = _redis()
    if redis_client:
        try:
            redis_client.delete(_incident_key(kind))
        except Exception as exc:
            logger.debug("Failed to clear monitor incident %s from Redis: %s", kind, exc)
    _fallback_incidents.pop(kind, None)
    return existed


def resolve_incident(
    kind: str,
    *,
    title: str = "Монитор восстановился",
    evidence: list[str] | tuple[str, ...] | str | None = None,
    auto_action: str | None = None,
) -> bool:
    previous = get_incident(kind)
    if not previous:
        return False
    clear_incident(kind)
    record_incident(
        INCIDENT_RECOVERED,
        severity="recovered",
        title=title,
        cause=f"resolved:{kind}",
        evidence=evidence or previous.get("evidence"),
        auto_action=auto_action or "Инцидент очищен, монитор вернулся к обычному режиму.",
        admin_action="Ничего делать не нужно.",
        affected=previous.get("affected"),
        state="recovered",
        ttl_minutes=15,
    )
    return True


def list_monitor_incidents(*, include_recovered: bool = True) -> list[dict[str, Any]]:
    incidents: dict[str, dict[str, Any]] = dict(_fallback_incidents)
    redis_client = _redis()
    if redis_client:
        try:
            for key in redis_client.scan_iter(f"{INCIDENT_KEY_PREFIX}*"):
                raw = redis_client.get(key)
                if not raw:
                    continue
                item = json.loads(raw)
                incidents[str(item.get("type") or key)] = item
        except Exception as exc:
            logger.debug("Failed to scan monitor incidents from Redis: %s", exc)

    values = [
        item
        for item in incidents.values()
        if include_recovered or item.get("state") != "recovered"
    ]
    severity_rank = {"critical": 0, "warning": 1, "recovered": 2, "info": 3}
    values.sort(
        key=lambda item: (
            severity_rank.get(str(item.get("severity", "warning")), 9),
            str(item.get("updated_at", "")),
        )
    )
    return values


def get_automation_state(incidents: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    active = [i for i in (incidents if incidents is not None else list_monitor_incidents()) if i.get("state") == "active"]
    by_type = {str(item.get("type")): item for item in active}
    if INCIDENT_NAVIGATION_TIMEOUT_STORM in by_type:
        incident = by_type[INCIDENT_NAVIGATION_TIMEOUT_STORM]
        return {
            "mode": "network_pause",
            "reason": "TikTok/сеть не отвечает, проверки профилей временно отложены.",
            "pause_until": incident.get("cooldown_until"),
        }
    if INCIDENT_LOCAL_BROWSER_INFRA in by_type:
        incident = by_type[INCIDENT_LOCAL_BROWSER_INFRA]
        return {
            "mode": "browser_recovering",
            "reason": "Playwright/browser pool дал сбой, софт чистит браузерные ресурсы.",
            "pause_until": incident.get("cooldown_until"),
        }
    if INCIDENT_TIKTOK_REGION_DEGRADATION in by_type:
        incident = by_type[INCIDENT_TIKTOK_REGION_DEGRADATION]
        return {
            "mode": "tiktok_degraded_pause",
            "reason": "TikTok profile pages are temporarily degraded; monitor checks are being deferred without account penalties.",
            "pause_until": incident.get("cooldown_until"),
        }
    if INCIDENT_ACCOUNT_POOL_LOW in by_type:
        return {
            "mode": "account_pool_low",
            "reason": "Готовых monitor-аккаунтов мало или они в cooldown.",
            "pause_until": None,
        }
    return {"mode": "normal", "reason": "Критичных инцидентов нет.", "pause_until": None}


def build_recommended_actions(incidents: list[dict[str, Any]] | None = None) -> list[str]:
    active = [i for i in (incidents if incidents is not None else list_monitor_incidents()) if i.get("state") == "active"]
    actions: list[str] = []
    seen: set[str] = set()
    for incident in active:
        action = str(incident.get("admin_action") or "").strip()
        if action and action not in seen:
            seen.add(action)
            actions.append(action)
    if not actions:
        actions.append("Срочных действий нет: софт сам держит защитные паузы и продолжает наблюдать.")
    return actions[:5]


def build_monitor_digest(
    *,
    health_snapshot: dict[str, Any] | None,
    runtime_snapshot: dict[str, Any] | None,
    incidents: list[dict[str, Any]] | None = None,
) -> str:
    health_snapshot = health_snapshot or {}
    runtime_snapshot = runtime_snapshot or {}
    incidents = incidents if incidents is not None else list_monitor_incidents(include_recovered=False)
    state = get_automation_state(incidents)
    active = [item for item in incidents if item.get("state") == "active"]
    status = "OK" if not active else ("CRITICAL" if any(i.get("severity") == "critical" for i in active) else "WARN")
    incident_lines = [
        f"- {item.get('title', item.get('type'))}: {item.get('cause', '')}".strip()
        for item in active[:5]
    ] or ["- Активных инцидентов нет."]
    action_lines = [f"- {action}" for action in build_recommended_actions(incidents)]
    return "\n".join(
        [
            f"[{status}] Дайджест монитора",
            "",
            "Состояние:",
            f"Режим: {state.get('mode')}",
            f"Причина: {state.get('reason')}",
            "",
            "Цифры:",
            f"Блогеры: {int(health_snapshot.get('bloggers', 0) or 0)}",
            f"Due backlog: {int(runtime_snapshot.get('due_backlog', health_snapshot.get('due', 0)) or 0)}",
            f"Oldest overdue: {int(runtime_snapshot.get('oldest_overdue_minutes', health_snapshot.get('oldest_overdue_minutes', 0)) or 0)}m",
            f"OK за окно: {int(health_snapshot.get('ok_last_window', 0) or 0)}",
            f"Аккаунты: {int(health_snapshot.get('accounts_eligible', 0) or 0)}/{int(health_snapshot.get('accounts_total', 0) or 0)} ready",
            "",
            "Инциденты:",
            *incident_lines,
            "",
            "Что делать админу:",
            *action_lines,
        ]
    )


__all__ = [
    "INCIDENT_ACCOUNT_POOL_LOW",
    "INCIDENT_HARD_PROXY_FAILURE",
    "INCIDENT_LOCAL_BROWSER_INFRA",
    "INCIDENT_MOBILE_PROXY_PRESSURE",
    "INCIDENT_MONITOR_BACKLOG",
    "INCIDENT_NAVIGATION_TIMEOUT_STORM",
    "INCIDENT_RECOVERED",
    "INCIDENT_TELEGRAM_DELIVERY",
    "INCIDENT_TIKTOK_REGION_DEGRADATION",
    "build_admin_alert",
    "build_monitor_digest",
    "build_recommended_actions",
    "classify_monitor_error",
    "clear_incident",
    "get_automation_state",
    "get_incident",
    "list_monitor_incidents",
    "record_incident",
    "resolve_incident",
]
