"""Execution ledger helpers for task result reporting.

The ledger is intentionally derived from existing ``settings["url_results"]``
payloads.  It gives the UI and tests a stable, privacy-safe view of what was
requested, attempted, submitted, verified, and skipped without changing the
publishing flow itself.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from hashlib import sha1
from typing import Any, Mapping


LEDGER_VERSION = 2
_EVENT_LIMIT = 80
_PREVIEW_LIMIT = 90
_VIDEO_SUCCESS_STATUSES = {"success", "submitted_unconfirmed", "done"}


def _clean(value: Any) -> str:
    return str(value or "").strip()


def _lower(value: Any) -> str:
    return _clean(value).lower()


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        if value is None or value == "":
            return default
        return int(value)
    except (TypeError, ValueError):
        return default


def _preview(value: Any, limit: int = _PREVIEW_LIMIT) -> str | None:
    text = " ".join(_clean(value).split())
    if not text:
        return None
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 3)].rstrip() + "..."


def _hash_key(value: Any) -> str:
    raw = _clean(value)
    if not raw:
        return "unknown"
    return sha1(raw.encode("utf-8", errors="ignore")).hexdigest()[:12]


def _status_is_submitted(item: Mapping[str, Any]) -> bool:
    visibility_status = _lower(item.get("visibility_status"))
    return bool(
        item.get("success")
        or item.get("submitted_unconfirmed")
        or visibility_status in {"submitted_unconfirmed", "confirmed_local", "network_confirmed"}
    )


def _status_is_confirmed(item: Mapping[str, Any]) -> bool:
    visibility_status = _lower(item.get("visibility_status"))
    return bool(
        item.get("success")
        or visibility_status in {"confirmed_local", "network_confirmed"}
        or item.get("local_visible") is True
    )


def _reply_attempted(item: Mapping[str, Any]) -> bool:
    return bool(
        item.get("attempted")
        or item.get("success")
        or item.get("submitted_unconfirmed")
        or item.get("error")
        or item.get("method")
    )


def _error_category(reason: Any) -> str | None:
    text = _lower(reason)
    if not text:
        return None
    if "captcha" in text:
        return "captcha"
    if any(token in text for token in ("cookie", "login", "passport", "session", "enrichment")):
        return "login_or_cookie"
    if any(token in text for token in ("stealth_reject", "empty_body")):
        return "stealth_reject"
    if any(token in text for token in ("comment_list_not_open", "comment_ui_not_found", "input_not_found", "surface", "empty_dom")):
        return "comment_surface"
    if any(token in text for token in ("parent_not_found", "main_not_found", "target_not_found", "not_found_for_text")):
        return "parent_missing"
    if any(token in text for token in ("proxy", "goto_failed", "net::", "err_", "timeout", "timed out")):
        return "network_or_proxy"
    return "other_errors"


def _add_event(events: list[dict[str, Any]], event: dict[str, Any]) -> None:
    if len(events) >= _EVENT_LIMIT:
        return
    clean_event = {key: value for key, value in event.items() if value not in (None, "", [])}
    events.append(clean_event)


def _new_account_signal(account_id: int) -> dict[str, Any]:
    return {
        "account_id": account_id,
        "attempted": 0,
        "submitted": 0,
        "confirmed": 0,
        "unconfirmed": 0,
        "failed": 0,
        "categories": {},
        "last_error": None,
    }


def _record_account_signal(
    signals: dict[int, dict[str, Any]],
    account_id: Any,
    *,
    attempted: bool = False,
    submitted: bool = False,
    confirmed: bool = False,
    unconfirmed: bool = False,
    error: Any = None,
) -> None:
    account_int = _safe_int(account_id)
    if account_int <= 0:
        return
    signal = signals.setdefault(account_int, _new_account_signal(account_int))
    if attempted:
        signal["attempted"] += 1
    if submitted:
        signal["submitted"] += 1
    if confirmed:
        signal["confirmed"] += 1
    if unconfirmed:
        signal["unconfirmed"] += 1
    category = _error_category(error)
    if category:
        signal["failed"] += 1
        signal["last_error"] = _preview(error, 120)
        categories = dict(signal.get("categories") or {})
        categories[category] = _safe_int(categories.get(category)) + 1
        signal["categories"] = categories


def _target_key(item: Mapping[str, Any]) -> str:
    aliases = _target_aliases(item)
    if aliases:
        return aliases[0]
    return "unknown"


def _text_alias(value: Any) -> str | None:
    text = _lower(value)
    if not text:
        return None
    return f"text:{_hash_key(text)}"


def _cid_alias(value: Any) -> str | None:
    cid = _clean(value)
    if not cid:
        return None
    return f"cid:{_hash_key(cid)}"


def _target_aliases(item: Mapping[str, Any]) -> list[str]:
    aliases: list[str] = []
    for value in (item.get("parent_comment_id"), item.get("comment_id")):
        alias = _cid_alias(value)
        if alias and alias not in aliases:
            aliases.append(alias)
    for value in (item.get("parent_key"), item.get("matched_main_text")):
        alias = _text_alias(value)
        if alias and alias not in aliases:
            aliases.append(alias)
    return aliases


def _target_label(item: Mapping[str, Any]) -> str:
    author = _clean(item.get("matched_parent_author") or item.get("requested_parent_author")).lstrip("@")
    text = _preview(item.get("matched_main_text") or item.get("parent_key") or item.get("parent_comment_id"), 50)
    if author and text:
        return f"@{author}: {text}"
    if author:
        return f"@{author}"
    return text or "unknown parent"


def _seed_parent_targets_from_main(main: Mapping[str, Any]) -> tuple[dict[str, dict[str, Any]], dict[str, str]]:
    variants = main.get("variants_published") if isinstance(main.get("variants_published"), list) else []
    if not variants and main.get("posted"):
        variants = [
            {
                "text": main.get("text_choice"),
                "comment_id": main.get("comment_id"),
                "account_id": main.get("account_id"),
            }
        ]

    seed_rows: list[tuple[str, dict[str, Any], list[str]]] = []
    alias_counts: Counter[str] = Counter()
    for index, variant in enumerate(variants, start=1):
        if not isinstance(variant, Mapping):
            continue
        text = variant.get("text")
        cid = variant.get("comment_id")
        account_id = _safe_int(variant.get("account_id"))
        aliases = [
            alias
            for alias in (
                _cid_alias(cid),
                _text_alias(text),
            )
            if alias
        ]
        for alias in aliases:
            alias_counts[alias] += 1

        text_identity = _lower(text)
        if cid:
            identity = cid
        elif account_id or text_identity:
            identity = f"{account_id}:{text_identity}"
        else:
            identity = f"index:{index}"
        key = f"main:{index}:{_hash_key(identity)}"
        label = _preview(text, 50) or (f"comment {index}" if not cid else f"comment {index} ({_preview(cid, 18)})")
        seed_rows.append(
            (
                key,
                {
                    "key": key,
                    "label": label,
                    "requested_author": None,
                    "matched_authors": [],
                    "attempted": 0,
                    "submitted": 0,
                    "confirmed": 0,
                    "unconfirmed": 0,
                    "failed": 0,
                    "author_fallback_used": False,
                    "last_error": None,
                    "status": "missing",
                    "expected": True,
                },
                aliases,
            )
        )

    per_target = {key: target for key, target, _aliases in seed_rows}
    alias_to_target: dict[str, str] = {}
    for key, _target, aliases in seed_rows:
        for alias in aliases:
            if alias_counts[alias] == 1:
                alias_to_target[alias] = key
    return per_target, alias_to_target


def _build_main_summary(main: Mapping[str, Any], events: list[dict[str, Any]], account_signals: dict[int, dict[str, Any]]) -> dict[str, Any]:
    variants = main.get("variants_published") if isinstance(main.get("variants_published"), list) else []
    expected = _safe_int(main.get("expected"))
    posted = bool(main.get("posted") or main.get("success"))
    submitted = len(variants) if variants else (1 if posted else 0)

    verified = 0
    network_confirmed = 0
    local_confirmed = 0
    for index, variant in enumerate(variants, start=1):
        if not isinstance(variant, Mapping):
            continue
        variant_confirmed = bool(variant.get("network_confirmed") or variant.get("local_confirmed"))
        verified += 1 if variant_confirmed else 0
        network_confirmed += 1 if variant.get("network_confirmed") else 0
        local_confirmed += 1 if variant.get("local_confirmed") else 0
        _record_account_signal(
            account_signals,
            variant.get("account_id"),
            attempted=True,
            submitted=True,
            confirmed=variant_confirmed,
            unconfirmed=not variant_confirmed,
            error=variant.get("error"),
        )
        _add_event(
            events,
            {
                "type": "main_submitted",
                "index": index,
                "status": "verified" if variant_confirmed else "submitted_unconfirmed",
                "method": variant.get("method"),
                "account_id": variant.get("account_id"),
                "comment_id": variant.get("comment_id"),
                "text_preview": _preview(variant.get("text")),
            },
        )

    if not variants and posted:
        verified = 1 if main.get("verified") else 0
        network_confirmed = 1 if main.get("network_confirmed") else 0
        local_confirmed = 1 if main.get("local_confirmed") else 0

    if expected <= 0:
        expected = submitted

    unconfirmed = max(0, submitted - verified)
    failed = max(0, expected - submitted) if expected else 0
    status = "verified" if submitted and verified >= submitted else ("submitted_unconfirmed" if submitted else "not_submitted")
    if failed and submitted:
        status = "partial"
    elif failed:
        status = "failed"

    return {
        "expected": expected,
        "submitted": submitted,
        "verified": verified,
        "unconfirmed": unconfirmed,
        "failed": failed,
        "network_confirmed": network_confirmed,
        "local_confirmed": local_confirmed,
        "status": status,
        "method": _clean(main.get("method")) or None,
        "fallback_via_reply": bool(main.get("fallback_via_reply")),
    }


def _build_reply_summary(
    replies: Mapping[str, Any],
    main: Mapping[str, Any],
    events: list[dict[str, Any]],
    account_signals: dict[int, dict[str, Any]],
) -> tuple[dict[str, Any], dict[str, Any]]:
    items = replies.get("items") if isinstance(replies.get("items"), list) else []
    requested = _safe_int(replies.get("requested_variants")) or _safe_int(replies.get("total")) or len(items)
    variant_outcomes = _safe_int(replies.get("variant_outcomes"))
    if variant_outcomes <= 0:
        variant_outcomes = len({item.get("variant_index") for item in items if isinstance(item, Mapping) and item.get("variant_index")})
    variants_not_started = _safe_int(replies.get("variants_not_started"))
    if requested and variant_outcomes:
        variants_not_started = max(variants_not_started, requested - variant_outcomes)

    aggregate_attempted = _safe_int(replies.get("attempted"))
    aggregate_submitted = _safe_int(replies.get("submitted"))
    aggregate_confirmed = _safe_int(replies.get("ok"))
    aggregate_unconfirmed = _safe_int(replies.get("submitted_unconfirmed"))
    aggregate_failed = _safe_int(replies.get("failed"))
    item_attempted_count = 0
    item_submitted_count = 0
    item_confirmed_count = 0
    item_unconfirmed_count = 0
    item_failed_count = 0

    per_target, alias_to_expected_target = _seed_parent_targets_from_main(main)
    author_fallbacks = 0
    text_only_matches = 0
    exact_author_matches = 0
    parent_missing = 0
    variant_rows: list[dict[str, Any]] = []

    for item in items:
        if not isinstance(item, Mapping):
            continue
        item_attempted = _reply_attempted(item)
        item_submitted = _status_is_submitted(item)
        item_confirmed = _status_is_confirmed(item)
        item_unconfirmed = bool(item_submitted and not item_confirmed)
        item_attempted_count += 1 if item_attempted else 0
        item_submitted_count += 1 if item_submitted else 0
        item_confirmed_count += 1 if item_confirmed else 0
        item_unconfirmed_count += 1 if item_unconfirmed else 0
        item_failed_count += 1 if item_attempted and not item_submitted else 0

        author_fallback = bool(item.get("author_fallback_used"))
        match_mode = _lower(item.get("parent_author_match_mode") or item.get("matched_by"))
        item_author_match = "unknown"
        if author_fallback:
            author_fallbacks += 1
            text_only_matches += 1
            item_author_match = "fallback"
        elif "author" in match_mode or (
            _clean(item.get("requested_parent_author"))
            and _lower(item.get("requested_parent_author")) == _lower(item.get("matched_parent_author"))
        ):
            exact_author_matches += 1
            item_author_match = "exact"
        elif item_submitted:
            text_only_matches += 1

        category = _error_category(item.get("error"))
        if category == "parent_missing":
            parent_missing += 1

        target_key = next(
            (alias_to_expected_target[alias] for alias in _target_aliases(item) if alias in alias_to_expected_target),
            _target_key(item),
        )
        target = per_target.setdefault(
            target_key,
            {
                "key": target_key,
                "label": _target_label(item),
                "requested_author": _clean(item.get("requested_parent_author")).lstrip("@") or None,
                "matched_authors": [],
                "attempted": 0,
                "submitted": 0,
                "confirmed": 0,
                "unconfirmed": 0,
                "failed": 0,
                "author_fallback_used": False,
                "last_error": None,
                "status": "pending",
                "expected": False,
            },
        )
        matched_author = _clean(item.get("matched_parent_author")).lstrip("@")
        requested_author = _clean(item.get("requested_parent_author")).lstrip("@")
        if requested_author and not target.get("requested_author"):
            target["requested_author"] = requested_author
        if matched_author and matched_author not in target["matched_authors"]:
            target["matched_authors"].append(matched_author)
        target["attempted"] += 1 if item_attempted else 0
        target["submitted"] += 1 if item_submitted else 0
        target["confirmed"] += 1 if item_confirmed else 0
        target["unconfirmed"] += 1 if item_unconfirmed else 0
        target["failed"] += 1 if item_attempted and not item_submitted else 0
        target["author_fallback_used"] = bool(target["author_fallback_used"] or author_fallback)
        if item.get("error"):
            target["last_error"] = _preview(item.get("error"), 120)

        variant_status = "submitted_unconfirmed" if item_unconfirmed else ("confirmed" if item_confirmed else ("failed" if item_attempted else "not_started"))
        variant_rows.append(
            {
                "variant_index": _safe_int(item.get("variant_index")),
                "status": variant_status,
                "target_key": target_key,
                "author_match": item_author_match,
            }
        )
        _record_account_signal(
            account_signals,
            item.get("account_id"),
            attempted=item_attempted,
            submitted=item_submitted,
            confirmed=item_confirmed,
            unconfirmed=item_unconfirmed,
            error=item.get("error") if item_attempted else None,
        )
        _add_event(
            events,
            {
                "type": "reply_result",
                "variant_index": item.get("variant_index"),
                "status": variant_status,
                "target": _target_label(item),
                "method": item.get("method"),
                "account_id": item.get("account_id"),
                "author_match": None if item_author_match == "unknown" else item_author_match,
                "error_category": category,
            },
        )

    for target in per_target.values():
        if target["submitted"]:
            target["status"] = "submitted"
        elif target["attempted"]:
            target["status"] = "attempted_not_submitted"
        else:
            target["status"] = "missing"

    final_attempted = aggregate_attempted or item_attempted_count
    final_submitted = aggregate_submitted or item_submitted_count
    final_confirmed = aggregate_confirmed or item_confirmed_count
    final_unconfirmed = aggregate_unconfirmed or item_unconfirmed_count
    final_failed = aggregate_failed or item_failed_count

    if requested and final_submitted > requested:
        requested = final_submitted

    summary = {
        "requested": requested,
        "attempted": final_attempted,
        "submitted": final_submitted,
        "confirmed": final_confirmed,
        "submitted_unconfirmed": final_unconfirmed,
        "failed": final_failed,
        "variants_recorded": variant_outcomes,
        "variants_not_started": variants_not_started,
        "author_exact_matches": exact_author_matches,
        "author_fallbacks": author_fallbacks,
        "text_only_matches": text_only_matches,
        "parent_missing": parent_missing,
    }
    coverage = {
        "parent_targets": list(per_target.values()),
        "reply_variants": variant_rows,
    }
    return summary, coverage


def _build_likes_summary(likes: Mapping[str, Any], events: list[dict[str, Any]]) -> dict[str, Any]:
    orders = likes.get("orders") if isinstance(likes.get("orders"), list) else []
    targets: list[dict[str, Any]] = []
    ok_count = 0
    failed_count = 0
    for order in orders:
        if not isinstance(order, Mapping):
            continue
        username = _clean(order.get("username")).lstrip("@")
        if not username:
            continue
        ok = bool(order.get("ok"))
        ok_count += 1 if ok else 0
        failed_count += 0 if ok else 1
        target = {
            "username": username,
            "quantity": _safe_int(order.get("quantity")),
            "reply_count": _safe_int(order.get("reply_count")),
            "ok": ok,
            "error": _preview(order.get("error"), 120),
            "parent_text": _preview(order.get("parent_text"), 160),
            "selection_reason": _clean(order.get("selection_reason")) or None,
        }
        targets.append(target)
        _add_event(
            events,
            {
                "type": "likes_order",
                "status": "ok" if ok else "failed",
                "username": username,
                "quantity": target["quantity"],
                "reply_count": target["reply_count"],
                "error": target["error"],
            },
        )

    return {
        "enabled": bool(likes.get("enabled")),
        "requested": bool(likes.get("requested")),
        "quantity": _safe_int(likes.get("quantity")),
        "target_mode": _clean(likes.get("target_mode")) or None,
        "orders_total": len(targets),
        "orders_ok": ok_count,
        "orders_failed": failed_count,
        "targets": targets,
        "error": _preview(likes.get("error"), 120),
    }


def build_url_execution_ledger(result: Mapping[str, Any] | None, *, url: str | None = None) -> dict[str, Any]:
    """Builds a ledger for a single URL result."""
    result = result if isinstance(result, Mapping) else {}
    engagement = result.get("engagement") if isinstance(result.get("engagement"), Mapping) else {}
    events: list[dict[str, Any]] = []
    account_signals: dict[int, dict[str, Any]] = {}

    main = engagement.get("main") if isinstance(engagement.get("main"), Mapping) else {}
    replies = engagement.get("replies") if isinstance(engagement.get("replies"), Mapping) else {}
    likes = engagement.get("likes") if isinstance(engagement.get("likes"), Mapping) else {}

    main_summary = _build_main_summary(main, events, account_signals)
    reply_summary, coverage = _build_reply_summary(replies, main, events, account_signals)
    likes_summary = _build_likes_summary(likes, events)

    error_counter: Counter[str] = Counter(
        str(event.get("error_category"))
        for event in events
        if event.get("error_category") and not event.get("account_id")
    )
    for signal in account_signals.values():
        for category, count in (signal.get("categories") or {}).items():
            error_counter[category] += _safe_int(count)

    status = _lower(result.get("status")) or "unknown"
    return {
        "version": LEDGER_VERSION,
        "scope": "url",
        "url_hash": _hash_key(url) if url else None,
        "status": status,
        "success_like": status in _VIDEO_SUCCESS_STATUSES,
        "summary": {
            "main": main_summary,
            "replies": reply_summary,
            "likes": likes_summary,
        },
        "coverage": coverage,
        "health_signals": {
            "accounts_total": len(account_signals),
            "accounts_with_errors": sum(1 for item in account_signals.values() if item.get("failed")),
            "error_categories": dict(error_counter),
            "accounts": list(account_signals.values())[:20],
        },
        "events": events,
    }


def attach_url_execution_ledger(result: dict[str, Any], *, url: str | None = None) -> dict[str, Any]:
    """Mutates and returns ``result`` with an ``execution_ledger`` key."""
    if not isinstance(result, dict):
        return result
    result["execution_ledger"] = build_url_execution_ledger(result, url=url)
    return result


def attach_url_execution_ledgers(url_results: dict[str, Any]) -> dict[str, Any]:
    """Attaches missing/updated ledgers to every URL row in a url_results dict."""
    if not isinstance(url_results, dict):
        return url_results
    for url, row in list(url_results.items()):
        if isinstance(row, dict):
            attach_url_execution_ledger(row, url=str(url))
    return url_results


def _sum_into(target: dict[str, int], source: Mapping[str, Any], keys: tuple[str, ...]) -> None:
    for key in keys:
        target[key] = _safe_int(target.get(key)) + _safe_int(source.get(key))


def build_task_execution_ledger(url_results: Mapping[str, Any] | None) -> dict[str, Any]:
    """Builds an aggregate task ledger from all URL results."""
    if not isinstance(url_results, Mapping):
        url_results = {}

    videos = {"total": len(url_results), "success_like": 0, "failed": 0, "with_warnings": 0}
    main_total: dict[str, int] = defaultdict(int)
    reply_total: dict[str, int] = defaultdict(int)
    likes_total: dict[str, int] = defaultdict(int)
    likes_targets: list[dict[str, Any]] = []
    error_counter: Counter[str] = Counter()
    parent_targets_total = 0
    parent_targets_submitted = 0
    parent_targets_fallback = 0
    account_ids_with_errors: set[int] = set()
    url_ledgers: list[dict[str, Any]] = []

    for url, row in url_results.items():
        if not isinstance(row, Mapping):
            continue
        ledger = row.get("execution_ledger")
        if not isinstance(ledger, Mapping) or _safe_int(ledger.get("version")) != LEDGER_VERSION:
            ledger = build_url_execution_ledger(row, url=str(url))
        url_ledgers.append(
            {
                "url_hash": ledger.get("url_hash") or _hash_key(url),
                "status": ledger.get("status"),
                "summary": ledger.get("summary", {}),
            }
        )
        if ledger.get("success_like"):
            videos["success_like"] += 1
        else:
            videos["failed"] += 1
        warnings = row.get("warnings") if isinstance(row.get("warnings"), list) else []
        if warnings:
            videos["with_warnings"] += 1

        summary = ledger.get("summary") if isinstance(ledger.get("summary"), Mapping) else {}
        main = summary.get("main") if isinstance(summary.get("main"), Mapping) else {}
        replies = summary.get("replies") if isinstance(summary.get("replies"), Mapping) else {}
        likes = summary.get("likes") if isinstance(summary.get("likes"), Mapping) else {}
        _sum_into(main_total, main, ("expected", "submitted", "verified", "unconfirmed", "failed"))
        _sum_into(
            reply_total,
            replies,
            (
                "requested",
                "attempted",
                "submitted",
                "confirmed",
                "submitted_unconfirmed",
                "failed",
                "variants_not_started",
                "author_exact_matches",
                "author_fallbacks",
                "text_only_matches",
                "parent_missing",
            ),
        )
        _sum_into(likes_total, likes, ("quantity", "orders_total", "orders_ok", "orders_failed"))
        for target in likes.get("targets") or []:
            if isinstance(target, Mapping):
                likes_targets.append(dict(target))

        coverage = ledger.get("coverage") if isinstance(ledger.get("coverage"), Mapping) else {}
        parent_targets = coverage.get("parent_targets") if isinstance(coverage.get("parent_targets"), list) else []
        counted_parent_targets = [
            target
            for target in parent_targets
            if isinstance(target, Mapping) and target.get("expected")
        ] or [
            target
            for target in parent_targets
            if isinstance(target, Mapping)
        ]
        for target in counted_parent_targets:
            if not isinstance(target, Mapping):
                continue
            parent_targets_total += 1
            if _safe_int(target.get("submitted")) > 0:
                parent_targets_submitted += 1
            if target.get("author_fallback_used"):
                parent_targets_fallback += 1

        health = ledger.get("health_signals") if isinstance(ledger.get("health_signals"), Mapping) else {}
        for category, count in (health.get("error_categories") or {}).items():
            error_counter[str(category)] += _safe_int(count)
        for account in health.get("accounts") or []:
            if not isinstance(account, Mapping):
                continue
            if _safe_int(account.get("failed")) > 0:
                account_id = _safe_int(account.get("account_id"))
                if account_id > 0:
                    account_ids_with_errors.add(account_id)

    return {
        "version": LEDGER_VERSION,
        "scope": "task",
        "videos": videos,
        "summary": {
            "main": dict(main_total),
            "replies": dict(reply_total),
            "likes": {
                **dict(likes_total),
                "targets": likes_targets,
            },
        },
        "coverage": {
            "parent_targets_total": parent_targets_total,
            "parent_targets_submitted": parent_targets_submitted,
            "parent_targets_fallback": parent_targets_fallback,
        },
        "health_signals": {
            "accounts_with_errors": len(account_ids_with_errors),
            "error_categories": dict(error_counter),
        },
        "urls": url_ledgers,
    }
