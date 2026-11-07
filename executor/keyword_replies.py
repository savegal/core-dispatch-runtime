"""Discover matching parents and durably fence reply dispatch per branch."""
import asyncio
from collections import Counter
from copy import deepcopy
import hashlib
import logging
from urllib.parse import urlparse

from sqlalchemy.orm.attributes import flag_modified
from sqlalchemy import func

from config.settings import get_settings
from db.models import KeywordReplyTarget
from executor.engagement_engine import EngagementResult, extract_video_id
from executor.http.execution_policy import http_first_enabled, valid_comment_id
from executor.http.page_runtime import target_page_matches
from services.keyword_replies import KEYWORD_ORDER_KIND, matches_keywords, normalize_keywords, replies_per_keyword_target
from services.likes_config import coerce_bool
from services.visibility_checkpoint import run_visibility_checkpoint


settings = get_settings()
logger = logging.getLogger(__name__)
_RESOURCE_DEFERRAL_ERRORS = {"no_free_reply_accounts", "no_accounts_available",
                             "candidate_budget_exhausted", "parallel_browser_capacity_unavailable"}


def _can_defer_without_delivery_risk(outcome):
    """A failure label alone must never erase a durable dispatch claim."""
    return bool(
        outcome.get("error") in _RESOURCE_DEFERRAL_ERRORS
        and outcome.get("publication_attempted") is False
        and not any(outcome.get(flag) for flag in (
            "success", "network_confirmed", "submitted_unconfirmed", "counts_as_submitted",
            "network_request_attempted", "network_captured", "post_was_enabled_at_click",
            "reply_id", "reply_comment_id", "comment_id",
        ))
        and outcome.get("visibility_status") != "submitted_unconfirmed"
    )


def video_key(url):
    return hashlib.sha256((extract_video_id(url) or url).encode()).hexdigest()


def _valid_video_binding(value, source_url):
    """Accept only internally consistent TikTok media/storage identities."""
    if not isinstance(value, dict):
        return None
    resolved = str(value.get("resolved_video_url") or "").strip()
    media_id = extract_video_id(resolved)
    source_id = extract_video_id(str(source_url))
    if not media_id or str(value.get("aweme_id") or "") != media_id or (source_id and source_id != media_id):
        return None
    key = value.get("storage_key")
    storage_url = str(value.get("storage_url") or "").strip()
    if not storage_url:
        storage_url = resolved if key == video_key(resolved) else str(source_url)
    storage_media_id = extract_video_id(storage_url)
    if storage_media_id and storage_media_id != media_id:
        return None
    for url in (resolved, storage_url):
        parsed = urlparse(url)
        host = (parsed.hostname or "").lower()
        if parsed.scheme != "https" or not (host == "tiktok.com" or host.endswith(".tiktok.com")):
            return None
    if key != video_key(storage_url):
        return None
    return {"storage_key": key, "storage_url": storage_url,
            "resolved_video_url": resolved, "aweme_id": media_id}


def _bound_video(task_settings, source_url, media_id=None):
    bindings = task_settings.get("keyword_video_bindings")
    if not isinstance(bindings, dict):
        return None
    direct = _valid_video_binding(bindings.get(source_url), source_url)
    if direct:
        return direct
    if media_id:
        for original, value in bindings.items():
            binding = _valid_video_binding(value, original)
            if binding and binding["aweme_id"] == str(media_id):
                return binding
    return None


def _valid_comment_row(row, aweme_id=None):
    return bool(isinstance(row, dict) and valid_comment_id(row.get("cid"))
                and (row.get("text") is None or isinstance(row["text"], str))
                and (aweme_id is None or row.get("aweme_id") is None or str(row["aweme_id"]) == str(aweme_id)))


def select_targets(rows, keywords, seen, *, aweme_id=None):
    selected = []
    for row in rows:
        if not _valid_comment_row(row, aweme_id):
            continue
        cid = str(row["cid"]).strip()
        if cid in seen:
            continue
        seen.add(cid)
        if str(row.get("reply_id") or "0") != "0" or row.get("is_deleted") in (True, 1, "1"):
            continue
        if not matches_keywords(str(row.get("text") or ""), keywords):
            continue
        user = row.get("user") if isinstance(row.get("user"), dict) else {}
        try:
            digg_count = max(0, int(row.get("digg_count") or 0))
        except (TypeError, ValueError):
            digg_count = 0
        selected.append({"cid": cid, "text": str(row.get("text") or ""),
                         "author": str(user.get("unique_id") or "").strip() or None,
                         "digg_count": digg_count})
    return selected


def has_more(value):
    if value in (False, 0, "0"):
        return False
    if value in (True, 1, "1"):
        return True
    raise ValueError("keyword_invalid_pagination")


async def collect_http_targets(page, aweme_id, keywords, *, stop_requested=None):
    from executor.http.comment_discovery import comment_read_batch, fetch_comment_page
    from executor.http.comment_scanner import reports_unseen_comments
    seen, cursors, targets = set(), set(), []
    cursor = 0
    complete, error = False, None
    async with comment_read_batch(page):
        for _ in range(max(1, settings.KEYWORD_REPLY_MAX_SCAN_PAGES)):
            if stop_requested and stop_requested():
                error = "stopped_by_user"
                break
            payload = await fetch_comment_page(page, aweme_id, cursor=cursor, count=50)
            if not isinstance(payload, dict) or not isinstance(payload.get("comments"), list):
                error = "keyword_comment_list_unavailable"
                break
            targets.extend(select_targets(payload["comments"], keywords, seen, aweme_id=aweme_id))
            if not all(_valid_comment_row(row, aweme_id) for row in payload["comments"]):
                # Keep the valid matches, but malformed rows cannot prove that
                # every accessible comment was examined successfully.
                error = "keyword_comment_list_unavailable"
                break
            try:
                if not has_more(payload.get("has_more")):
                    complete = not reports_unseen_comments(payload, len(seen))
                    if not complete:
                        error = "keyword_comment_list_unavailable"
                    break
                next_cursor = int(payload["cursor"])
                if next_cursor <= int(cursor) or next_cursor in cursors:
                    raise ValueError
            except (KeyError, TypeError, ValueError):
                error = "keyword_pagination_stalled"
                break
            cursors.add(int(cursor))
            cursor = next_cursor
        else:
            error = "keyword_scan_limit"
    return targets, {"complete": complete, "scanned": len(seen), "error": error}


async def discover_keyword_targets(db, video_url, keywords, *, task_id, pool_owner_user_id=None, stop_requested=None):
    from executor import engagement_engine as engine, user_schema as schema
    from services.execution_attempts import candidate_scan_limit
    from executor.visibility_support import account_identity_keys, identity_rejection_reason
    from services.publication_journal import record_phase
    await record_phase("reading")
    if not http_first_enabled(settings):
        return [], {"complete": False, "scanned": 0, "error": "keyword_http_only"}
    excluded, found = [], {}
    browser_attempts = 0
    browser_limit = max(1, min(6, int(settings.MAX_RETRY_ACCOUNTS)))
    best_scan = None
    negative_readers = []
    expected_media_id = engine.extract_video_id(video_url)
    for _ in range(candidate_scan_limit(settings)):
        if stop_requested and stop_requested():
            break
        if browser_attempts >= browser_limit:
            break
        account = engine.get_free_account_for_reply(db, exclude_ids=excluded, reserve=True,
                                                   pool_owner_user_id=pool_owner_user_id)
        if account is None:
            break
        excluded.append(account.id)
        page = context = None
        corr = f"task_{task_id}_keyword_discovery_acc{account.id}"
        try:
            if await schema._preflight_cookie_http_gate(db, account, correlation_id=corr, phase="keyword_discovery"):
                continue
            identity = account_identity_keys(account, schema._get_cached_http_preflight_username(account),
                                             user_id=schema._get_cached_http_preflight_user_id(account))
            if negative_readers and identity_rejection_reason(identity, negative_readers) == "observer_identity_not_independent":
                continue
            browser_attempts += 1
            context, page = await engine.create_page_for_account(account)
            ready, _ = await schema._ensure_account_login_ready(db, page, account, video_url,
                                                                timeout_ms=5000, correlation_id=corr)
            if not ready:
                continue
            _, ready, _ = await engine.warmup_page_for_http(page, video_url, correlation_id=corr, account=account, db=db)
            # Warmup owns navigation. Bind discovery to its final media rather
            # than retaining an ID observed before that navigation completed.
            media_id = engine.extract_video_id(page.url)
            if ready and media_id and target_page_matches(page, expected_media_id or media_id):
                expected_media_id = media_id
                targets, scan = await collect_http_targets(page, media_id, keywords, stop_requested=stop_requested)
                for target in targets:
                    found.setdefault(target["cid"], target)
                best_scan = {**scan, "scanned": max(int(scan.get("scanned") or 0),
                             int((best_scan or {}).get("scanned") or 0), len(found)),
                             "resolved_video_url": str(page.url), "aweme_id": media_id}
                if scan.get("complete") and not found:
                    # A restricted account may receive a plausible empty list.
                    # Require a second independent reader before saying no match.
                    if identity_rejection_reason(identity, negative_readers) is None:
                        negative_readers.append(identity)
                    best_scan["negative_confirmations"] = len(negative_readers)
                    if len(negative_readers) < 2:
                        best_scan.update(complete=False, error="keyword_negative_scan_unconfirmed")
                        continue
                if scan.get("error") in {"keyword_comment_list_unavailable", "keyword_pagination_stalled"}:
                    engine._record_http_account_result(account.id, "soft_fail")
                if scan.get("complete") or scan.get("error") not in {
                    "keyword_comment_list_unavailable", "keyword_pagination_stalled",
                }:
                    return list(found.values()), best_scan
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("Keyword discovery failed account=%s: %s", account.id, type(exc).__name__)
        finally:
            try:
                await schema._safe_close(context, page, reason="keyword_discovery_done", correlation_id=corr)
            finally:
                schema.release_reply_account_reservation(account.id)
    scan = best_scan or {"complete": False, "scanned": 0, "error": "keyword_discovery_unavailable"}
    if stop_requested and stop_requested():
        scan = {**scan, "complete": False, "error": "stopped_by_user"}
    return list(found.values()), scan


def target_dict(row):
    result = dict(row.result or {})
    if row.status in {"dispatching", "unconfirmed"}:
        result.update(success=False, submitted_unconfirmed=True, dispatch_claimed=True,
                      visibility_status="submitted_unconfirmed", error="keyword_delivery_unconfirmed")
    result.setdefault("success", False)
    result.setdefault("text", row.reply_text)
    result.update(parent_comment_id=row.comment_id, variant_index=row.position,
                  matched_main_text=row.parent_text)
    result.setdefault("matched_parent_author", row.parent_author)
    if row.status == "pending":
        result["error"] = "not_attempted"
    return result


def _validate_receipt(outcome, parent_id):
    observed_parents = [
        str(outcome[field]).strip()
        for field in ("parent_comment_id", "response_parent_id", "response_root_comment_id")
        if valid_comment_id(outcome.get(field))
    ]
    if any(parent != parent_id for parent in observed_parents):
        observed_reply = outcome.pop("reply_id", None) or outcome.get("reply_comment_id")
        outcome.pop("reply_comment_id", None)
        outcome.update(
            success=False, submitted_unconfirmed=True, network_confirmed=False,
            error="keyword_parent_mismatch", observed_parent_comment_ids=observed_parents,
            observed_reply_id=observed_reply,
        )
    elif outcome.get("success") and not (
        valid_comment_id(outcome.get("reply_id")) and outcome.get("network_confirmed")
        and str(outcome.get("method") or "").startswith("http")
    ):
        outcome.update(success=False, submitted_unconfirmed=True, error="keyword_delivery_unconfirmed")
    return outcome


def _target_slots(row, replies_per_target):
    if replies_per_target == 1:
        return [target_dict(row)]
    aggregate = target_dict(row)
    outcomes = aggregate.get("branch_outcomes") or []
    slots = []
    for index in range(replies_per_target):
        if index < len(outcomes) and isinstance(outcomes[index], dict):
            item = _validate_receipt(dict(outcomes[index]), row.comment_id)
        elif index == 0 and not outcomes:
            item = dict(aggregate)
        else:
            item = {"success": False, "error": aggregate.get("error") or "not_attempted"}
            if row.status in {"dispatching", "unconfirmed"}:
                item.update(submitted_unconfirmed=True, visibility_status="submitted_unconfirmed")
        item.update(parent_comment_id=row.comment_id, matched_main_text=row.parent_text,
                    variant_index=(row.position - 1) * replies_per_target + index + 1)
        item.setdefault("matched_parent_author", row.parent_author)
        slots.append(item)
    return slots


def _targets(db, task_id, key):
    return db.query(KeywordReplyTarget).filter_by(task_id=task_id, video_key=key).order_by(
        KeywordReplyTarget.position).populate_existing().all()


def _checkpoint(db, task, url, operation, data, box):
    key = data.pop("_video_key", None) or video_key(url)
    if operation == "plan":
        rows = _targets(db, task.id, key)
        existing = {row.comment_id for row in rows}
        position = max((row.position for row in rows), default=0)
        for target in data["targets"]:
            if target["cid"] in existing:
                continue
            position += 1
            db.add(KeywordReplyTarget(task_id=task.id, video_key=key, comment_id=target["cid"],
                position=position, parent_text=target["text"], parent_author=target.get("author"),
                reply_text=data["replies"][(position - 1) % len(data["replies"])], status="pending"))
            existing.add(target["cid"])
    else:
        row = db.get(KeywordReplyTarget, (task.id, key, data["cid"]))
        if operation == "claim":
            stopped = task.status == "stopped" or (task.settings or {}).get("stopped_by_user") or (task.settings or {}).get("cancelled_by_user")
            box["claimed"] = bool(row and row.status == "pending" and not stopped)
            if box["claimed"]:
                row.status = "dispatching"
            else:
                db.commit()
                return
        elif row is not None and row.status == "dispatching":
            row.result, row.status = data["result"], data["status"]
        else:
            db.commit()
            return
    db.flush()
    query = db.query(KeywordReplyTarget).filter_by(task_id=task.id, video_key=key)
    counts = Counter(dict(query.with_entities(KeywordReplyTarget.status, func.count()).group_by(
        KeywordReplyTarget.status).all()))
    total = sum(counts.values())
    task_settings = dict(task.settings or {})
    scans = dict(task_settings.get("keyword_scans") or {})
    if operation == "plan":
        scans[key] = dict(data["scan"])
        if data.get("binding"):
            stored_bindings = task_settings.get("keyword_video_bindings")
            bindings = dict(stored_bindings) if isinstance(stored_bindings, dict) else {}
            bindings[url] = dict(data["binding"])
            task_settings["keyword_video_bindings"] = bindings
    scan = scans.get(key, {})
    progress = {**scan, "matched": total, "replied": counts["success"],
                "unconfirmed": counts["dispatching"] + counts["unconfirmed"], "failed": counts["failed"]}
    task_settings["keyword_scans"] = scans
    url_results = dict(task_settings.get("url_results") or {})
    from services.publication_progress import merge_items
    previous = dict(url_results.get(url) or {})
    engagement = dict(previous.get("engagement") or {})
    reply_info = dict(engagement.get("replies") or {})
    per_target = replies_per_keyword_target(task_settings.get("replies_per_target"))
    receipts = [item for target in query.order_by(KeywordReplyTarget.position).all()
                if target.status in {"success", "dispatching", "unconfirmed", "failed"}
                for item in _target_slots(target, per_target)]
    items = merge_items(reply_info.get("items") or [], receipts)
    engagement["replies"] = {**reply_info, "ok": sum(bool(item.get("success")) for item in items),
        "expected": total * per_target,
        "submitted_unconfirmed": sum(bool(item.get("submitted_unconfirmed")) for item in items), "items": items}
    engagement.setdefault("main", {})
    url_results[url] = {**previous, "status": "in_progress", "mode": KEYWORD_ORDER_KIND,
                        "keyword_replies": progress, "engagement": engagement}
    task_settings["url_results"] = url_results
    task.settings = task_settings
    flag_modified(task, "settings")
    db.commit()


async def checkpoint(db, task, url, operation, *, storage_key=None, **data):
    box = {}
    if storage_key:
        data["_video_key"] = storage_key
    await run_visibility_checkpoint(db, task, _checkpoint, url, operation, deepcopy(data), box)
    return box.get("claimed", False)


class KeywordReplyResult(EngagementResult):
    def __init__(self, scan):
        super().__init__()
        self.scan = scan
        self.matched_targets = 0
        self.boost_enabled, self.boost_quantity = False, 0

    def to_dict(self, **kwargs):
        kwargs["mode"] = KEYWORD_ORDER_KIND
        result = super().to_dict(**kwargs)
        result["keyword_replies"] = {**self.scan, "matched": self.matched_targets,
                                     "replied": result["engagement"]["replies"]["ok"],
                                     "unconfirmed": result["engagement"]["replies"]["submitted_unconfirmed"]}
        if not self.scan.get("complete"):
            result["status"] = result["core_status"] = "partial" if result["engagement"]["replies"]["submitted"] else "fail"
            result["error"] = self.scan.get("error") or "keyword_scan_incomplete"
        elif self.scan.get("error") and result["status"] != "success":
            result["error"] = self.scan["error"]
        result["publication_complete"] = result["status"] == "success"
        return result


async def execute_keyword_replies(db, task, video_url, *, task_id, pool_owner_user_id=None, stop_requested=None):
    """Own one browser-pool phase across every matched parent on this video."""
    if not http_first_enabled(settings):
        raise ValueError("keyword_http_only")
    # The executor owns this pool through visibility checks and later URLs.
    return await _execute_keyword_replies(db, task, video_url, task_id=task_id,
        pool_owner_user_id=pool_owner_user_id, stop_requested=stop_requested)


async def _execute_keyword_replies(db, task, video_url, *, task_id, pool_owner_user_id=None, stop_requested=None):
    from executor import engagement_engine as engine
    if not http_first_enabled(settings):
        raise ValueError("keyword_http_only")
    keywords = normalize_keywords((task.settings or {}).get("keywords"))
    replies = engine._reply_variants_from_task(task)
    if not replies:
        raise ValueError("keyword_reply_text_missing")
    task_settings = task.settings or {}
    binding = _bound_video(task_settings, video_url)
    if binding:
        resolved_url, media_id = binding["resolved_video_url"], binding["aweme_id"]
    else:
        resolved_url, media_id = await engine.resolve_order_video_url(db, video_url, pool_owner_user_id)
        binding = _bound_video(task_settings, video_url, media_id)
    key = binding["storage_key"] if binding else video_key(resolved_url)
    scans = task_settings.get("keyword_scans") or {}
    original_key = video_key(video_url)
    legacy_storage = False
    if not binding and original_key != key and (original_key in scans or _targets(db, task.id, original_key)):
        # Older tasks could have committed a raw-shortlink namespace. Keep its
        # claims/receipts in place instead of changing primary keys on recovery.
        key, legacy_storage = original_key, True
    elif not binding and key == original_key and (key in scans or _targets(db, task.id, key)):
        legacy_storage = True
    scan = scans.get(key, {})
    if not isinstance(scan, dict):
        scan = {}
    targets = None
    if not scan.get("complete") or not media_id:
        targets, scan = await discover_keyword_targets(db, resolved_url, keywords, task_id=task_id,
            pool_owner_user_id=pool_owner_user_id, stop_requested=stop_requested)
        discovered_url = str(scan.get("resolved_video_url") or "")
        discovered_id = extract_video_id(discovered_url)
        if discovered_id and str(scan.get("aweme_id") or "") == discovered_id:
            if media_id and str(media_id) != discovered_id:
                targets, scan = [], {"complete": False, "scanned": 0, "error": "keyword_discovery_unavailable"}
            else:
                resolved_url, media_id = discovered_url, discovered_id
                alias_binding = _bound_video(task.settings or {}, video_url, media_id)
                if alias_binding:
                    binding, key = alias_binding, alias_binding["storage_key"]
                elif not legacy_storage:
                    key = video_key(resolved_url)
        logger.info("[KEYWORD_REPLY] task=%s scanned=%s matched=%s complete=%s reason=%s", task_id,
                    scan.get("scanned"), len(targets), scan.get("complete"), scan.get("error") or "ok")
    if media_id:
        binding = _valid_video_binding({"storage_key": key,
            "storage_url": binding["storage_url"] if binding else video_url if legacy_storage else resolved_url,
            "resolved_video_url": resolved_url, "aweme_id": str(media_id)}, video_url)
    if not media_id:
        scan = {**scan, "complete": False, "error": scan.get("error") or "keyword_discovery_unavailable"}
    if targets:
        current_settings = getattr(task, "settings", None) or {}
        if coerce_bool(current_settings.get("sort_targets_by_likes"), current_settings.get("target_sort") == "likes"):
            targets = sorted(targets, key=lambda item: int(item.get("digg_count") or 0), reverse=True)
        try:
            max_targets = max(0, int(current_settings.get("max_targets") or 0))
        except (TypeError, ValueError, OverflowError):
            max_targets = 0
        if max_targets > 0:
            existing_cids = {row.comment_id for row in _targets(db, task.id, key)}
            targets = [item for item in targets if item["cid"] not in existing_cids][
                :max(0, max_targets - len(existing_cids))
            ]
    if targets is not None or (binding and _bound_video(task.settings or {}, video_url) is None):
        await checkpoint(db, task, video_url, "plan", targets=targets or [], scan=scan,
                         replies=replies, storage_key=key, binding=binding)
    rows = _targets(db, task.id, key)
    prepublish_failed_account_ids = set()
    replies_per_target = replies_per_keyword_target(task_settings.get("replies_per_target"))
    for row in rows:
        if not media_id:
            break
        if stop_requested and stop_requested():
            scan = {**scan, "error": "stopped_by_user"}
            break
        if row.status != "pending" or not await checkpoint(db, task, video_url, "claim", cid=row.comment_id, storage_key=key):
            continue
        logger.info("[KEYWORD_REPLY] task=%s parent=%s position=%s/%s dispatch_claimed", task_id, row.comment_id, row.position, len(rows))
        try:
            if replies_per_target > 1:
                target_replies = [
                    replies[(max(1, int(row.position or 1)) - 1 + idx) % len(replies)]
                    for idx in range(replies_per_target)
                ]
            else:
                target_replies = [row.reply_text]
            outcomes, _, _ = await engine.execute_engagement_only_replies_via_full_flow(
                db, resolved_url, comment_text=row.parent_text, comment_id=row.comment_id,
                comment_author=row.parent_author, replies=target_replies, task_id=task_id,
                pool_owner_user_id=pool_owner_user_id, stop_requested=stop_requested, parent_phrase_match=False,
                close_pool_after_phase=False, prepublish_failed_account_ids=prepublish_failed_account_ids,
                task_settings=getattr(task, "settings", None) if task is not None else None)
            by_index = {
                receipt.get("variant_index", index): _validate_receipt(dict(receipt), row.comment_id)
                for index, receipt in enumerate(outcomes, 1) if isinstance(receipt, dict)
            }
            uncertain_receipt = {"success": False, "error": "keyword_delivery_unconfirmed",
                                 "submitted_unconfirmed": True}
            if replies_per_target == 1 and len(outcomes) != 1:
                by_index.clear()
            receipts = [
                by_index.get(index, uncertain_receipt).copy()
                for index in range(1, replies_per_target + 1)
            ]
            can_defer = all(_can_defer_without_delivery_risk(receipt) for receipt in receipts)
            for receipt in receipts:
                if not can_defer and receipt.get("error") in _RESOURCE_DEFERRAL_ERRORS:
                    receipt.update(success=False, submitted_unconfirmed=True)
            outcome = dict(next((receipt for receipt in receipts if receipt.get("error")), receipts[0]))
            outcome["success"] = all(receipt.get("success") for receipt in receipts)
            uncertain = any(receipt.get("submitted_unconfirmed")
                            or receipt.get("visibility_status") == "submitted_unconfirmed" for receipt in receipts)
            if replies_per_target > 1:
                outcome["branch_outcomes"] = receipts
                outcome["branch_replies_ok"] = sum(bool(receipt.get("success")) for receipt in receipts)
            if can_defer:
                outcome["deferred_reason"] = outcome["error"]
                status = "pending"
                scan = {**scan, "error": outcome["error"]}
            else:
                partially_sent = not outcome["success"] and any(receipt.get("success") for receipt in receipts)
                status = "unconfirmed" if uncertain or partially_sent else "success" if outcome["success"] else "failed"
        except asyncio.CancelledError:
            raise  # The durable dispatch marker prevents a blind retry.
        except Exception:
            outcome, status = {"error": "keyword_delivery_unconfirmed", "submitted_unconfirmed": True}, "unconfirmed"
        await checkpoint(db, task, video_url, "finish", cid=row.comment_id, result=outcome, status=status, storage_key=key)
        logger.info("[KEYWORD_REPLY] task=%s parent=%s status=%s reason=%s", task_id, row.comment_id, status, outcome.get("error") or "ok")
        if status == "pending" or any(code in str(outcome.get("error") or "") for code in ("no_free_reply_accounts", "no_accounts_available", "stopped_by_user")):
            break
    result = KeywordReplyResult(scan)
    rows = _targets(db, task.id, key)
    result.comment_found = bool(rows)
    result.matched_targets = len(rows)
    result.expected_reply_variants = len(rows) * replies_per_target
    result.replies = [slot for row in rows for slot in _target_slots(row, replies_per_target)]
    result.resolved_video_url, result.aweme_id = resolved_url, media_id
    if not rows:
        result.main_comment["error"] = scan.get("error") or "keyword_no_matches"
    return result
