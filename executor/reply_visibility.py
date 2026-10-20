"""Optional exact-ID reply checks and bounded replacement in the same branch."""
from __future__ import annotations

import asyncio
import copy
import logging
import os
import random
import re
import time
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone

from config.settings import get_settings
from executor.http.comment_discovery import fetch_reply_page, comment_read_batch
from executor.http.comment_scanner import scan_comment_ids, comment_id_key
from executor.http.execution_policy import http_first_enabled
from executor.http.execution_policy import valid_comment_id
from executor.main_visibility import verify_main_publications
from executor.visibility_support import account_identity_keys as _identity_keys, identity_is_known, identities_independent, identity_rejection_reason
from services.execution_attempts import is_pre_publish_account_failure

logger = logging.getLogger(__name__)
REPORT_KEY = "reply_publication_visibility"  # reply_visibility already describes parent discovery.


def reply_visibility_enabled(settings):
    value = (settings or {}).get("verify_reply_visibility", False)
    return value is True or (isinstance(value, str) and value.strip().lower() in {"true", "1", "yes", "on"})


def reply_id(item):
    return str(item.get("reply_comment_id") or item.get("reply_id") or "").strip()


def _legacy_reply_sections(result):
    engagement = result.get("engagement") or {}
    variants = result.get("main_variants") or engagement.get("main_variants") or []
    sections, mains = [], []
    for variant in variants:
        if not isinstance(variant, dict):
            continue
        child = variant.get("engagement") or {}
        main = dict(child.get("main") or {})
        main["comment_id"] = main.get("comment_id") or variant.get("comment_id")
        mains.append(main)
        sections.append(child.get("replies") or {})
    return sections, mains


async def scan_reply_comment_ids(page, aweme_id, parent_comment_id, comment_ids, *, max_pages=20):
    """Read fresh exact IDs from the requested parent, without DOM discovery."""
    aweme_id, parent_comment_id = str(aweme_id).strip(), comment_id_key(parent_comment_id)
    return await scan_comment_ids(
        page, aweme_id, comment_ids, max_pages=max_pages, parent_comment_id=parent_comment_id,
        read_page=lambda cursor: fetch_reply_page(page, str(aweme_id), str(parent_comment_id), cursor),
        meta_attr="_reply_list_http_meta",
    )


@dataclass
class ReplySlot:
    item: dict
    index: int
    public_confirmed: bool = False

    @property
    def comment_id(self):
        return reply_id(self.item)

    @property
    def account_id(self):
        return self.item.get("account_id")

    @property
    def text(self):
        return self.item.get("text") or ""

    @property
    def variant_index(self):
        return self.item.get("variant_index")

    @variant_index.setter
    def variant_index(self, value):
        self.item["variant_index"] = value


def save_reply_visibility_checkpoint(db, task, url_key, result):
    """Merge current settings, preserving other URLs and the original shortlink key."""
    from db.models import Task
    from sqlalchemy.orm.attributes import flag_modified
    from services.task_execution_ledger import attach_url_execution_ledger, build_task_execution_ledger
    if db is None or not isinstance(task, Task):
        return
    try:
        with db.no_autoflush:
            row = db.query(Task.settings).filter(Task.id == task.id).first()
        if row is None:
            raise RuntimeError("visibility task no longer exists")
        merged = dict(row[0] or {})
        urls = dict(merged.get("url_results") or {})
        attach_url_execution_ledger(result, url=url_key)
        urls[url_key] = copy.deepcopy(result)
        merged["url_results"] = urls
        merged["execution_ledger"] = build_task_execution_ledger(urls)
        task.settings = merged
        flag_modified(task, "settings")
        db.commit()
    except Exception:
        db.rollback()
        raise


async def verify_order_reply_visibility(db, task, video_url, result, *, pool_owner_user_id=None,
                                        stop_requested=lambda: False):
    """Post-process both full and replies-only results without repeating the main phase."""
    if not reply_visibility_enabled(getattr(task, "settings", None)):
        return result
    started = time.monotonic()
    from executor import user_schema as schema
    from executor.engagement_engine import resolve_order_video_url, _engagement_reply_outcome_to_dict, warmup_page_for_http
    from db.models import Account

    settings = get_settings()
    replies = (result.get("engagement") or {}).get("replies") or {}
    legacy_sections, legacy_mains = _legacy_reply_sections(result)
    legacy_locations = []
    if legacy_sections:
        combined = []
        planned = len(schema._reply_variants_from_task(task))
        for section in legacy_sections:
            child_items = section.get("items") or []
            combined.extend(child_items)
            legacy_locations.extend((child_items, index) for index in range(len(child_items)))
        expected_legacy = sum(int(section.get("expected_submissions") or section.get("expected")
                                  or section.get("requested_variants") or planned or len(section.get("items") or []))
                              for section in legacy_sections)
        replies.update(items=combined, total=len(combined), expected=expected_legacy, expected_submissions=expected_legacy,
                       submitted=sum(int(section.get("submitted") or int(section.get("ok") or 0)
                                         + int(section.get("submitted_unconfirmed") or 0)) for section in legacy_sections))
        result.setdefault("engagement", {})["replies"] = replies
    items = replies.get("items") or []
    slots = [ReplySlot(item, index) for index, item in enumerate(items)
             if isinstance(item, dict) and valid_comment_id(reply_id(item))]
    original_slots_all = list(slots)
    expected = int(replies.get("expected_submissions") or replies.get("expected")
                   or replies.get("requested_variants") or len(schema._reply_variants_from_task(task))
                   or replies.get("total") or len(slots))
    if not expected and not slots:
        result[REPORT_KEY] = {"enabled": True, "status": "not_requested", "expected": 0, "confirmed": 0, "replacements": 0}
        return result
    used = {slot.account_id for slot in slots if slot.account_id is not None}
    main = (result.get("engagement") or {}).get("main") or {}
    invalid_targets = []
    if result.get("mode") == "full":
        allowed = {comment_id_key(row.get("comment_id")) for row in main.get("variants_published", [])
                   if isinstance(row, dict) and valid_comment_id(row.get("comment_id"))}
        if valid_comment_id(main.get("comment_id")):
            allowed.add(comment_id_key(main["comment_id"]))
        allowed.update(comment_id_key(row.get("comment_id")) for row in legacy_mains if valid_comment_id(row.get("comment_id")))
        invalid_targets = [{"comment_id": slot.comment_id, "parent_comment_id": slot.item.get("parent_comment_id"),
                            "reason": "reply_parent_not_in_order"} for slot in slots
                           if comment_id_key(slot.item.get("parent_comment_id")) not in allowed]
        slots = [slot for slot in slots if comment_id_key(slot.item.get("parent_comment_id")) in allowed]
    used.update(row.get("account_id") for row in main.get("variants_published", []) if isinstance(row, dict))
    if main.get("account_id") is not None:
        used.add(main["account_id"])
    used.update(row["account_id"] for row in legacy_mains if row.get("account_id") is not None)
    used.discard(None)
    publisher_keys, observer_keys = set(), set()
    publisher_identities, observer_identities = [], []
    publisher_account_ids = set(used)
    identities_known = None
    result[REPORT_KEY] = {"enabled": True, "status": "checking", "expected": expected,
                          "confirmed": 0, "replacements": 0, "pending_receipts": [], "invalid_targets": invalid_targets}
    replies.update(ok=0, public_confirmed=0, visibility_complete=False)
    for item in items:
        if isinstance(item, dict):
            item["public_confirmed"] = False
    checkpoint_failed = False
    captured = []
    original_slots = list(slots)
    original_ids = {slot.comment_id for slot in original_slots_all}
    original_accounts = {slot.account_id for slot in original_slots_all}
    target = None

    def save():
        nonlocal checkpoint_failed
        try:
            save_reply_visibility_checkpoint(db, task, video_url, result)
        except Exception:
            checkpoint_failed = True
            logger.exception("[REPLY_VISIBILITY] task=%s checkpoint failed", getattr(task, "id", None))

    async def resolve_target():
        nonlocal target
        if target is None:
            cached_url = str(result.get("resolved_video_url") or "").strip()
            cached_id = str(result.get("aweme_id") or "").strip() or (extract_video_id(cached_url) if cached_url else None)
            if cached_url and cached_id:
                target = (cached_url, cached_id)
            else:
                resolved_url, resolved_id = await resolve_order_video_url(db, video_url, pool_owner_user_id)
                if resolved_url and resolved_id:
                    result["resolved_video_url"] = resolved_url
                    result["aweme_id"] = resolved_id
                target = (resolved_url, resolved_id)
        return target

    def select_account():
        account = schema.get_free_account_for_reply(
            db, exclude_ids=list(used), reserve=True,
            exclude_degraded_categories={"captcha", "page_blocked"}, pool_owner_user_id=pool_owner_user_id,
        )
        if account is not None:
            used.add(account.id)
        return account

    async def observe(pending, round_index, attempt):
        if not http_first_enabled(settings):
            return {"reason": "http_disabled", "fatal": True, "api_check_attempted": False}
        nonlocal identities_known
        if identities_known is None:
            identities_known = True
            accounts_to_preflight = []
            publisher_meta = []
            for account_id in publisher_account_ids:
                account = db.get(Account, account_id) if db is not None else None
                receipts = [slot.item for slot in original_slots_all if slot.account_id == account_id]
                username = next((item.get("author_username") for item in receipts if item.get("author_username")), None)
                uid = next((item.get("author_uid") for item in receipts if item.get("author_uid")), None)
                publisher_meta.append((account_id, account, username, uid))
                if account and not (username or uid or schema._get_cached_http_preflight_username(account) or schema._get_cached_http_preflight_user_id(account)):
                    accounts_to_preflight.append(account)
            if accounts_to_preflight:
                preflight_sem = asyncio.Semaphore(3 if settings.USE_PROXY else 2)

                async def _preflight_pub(acc):
                    async with preflight_sem:
                        await schema._preflight_cookie_http_gate(
                            db,
                            acc,
                            correlation_id=f"task_{task.id}_visibility_author_{acc.id}",
                            phase="visibility_author_identity",
                        )

                await asyncio.gather(*(_preflight_pub(acc) for acc in accounts_to_preflight))
            for account_id, account, username, uid in publisher_meta:
                keys = _identity_keys(account, username or schema._get_cached_http_preflight_username(account),
                                      user_id=uid or schema._get_cached_http_preflight_user_id(account))
                keys.add(("account_id", str(account_id)))
                publisher_keys.update(keys)
                publisher_identities.append(keys)
                identities_known = identities_known and identity_is_known(keys)
        if not identities_known:
            return {"reason": "publisher_identity_unavailable", "fatal": True, "api_check_attempted": False}
        url, media_id = await resolve_target()
        if not media_id:
            return {"reason": "video_id_unavailable", "fatal": True, "api_check_attempted": False}
        account = select_account()
        if account is None:
            return {"reason": "no_observer_accounts", "api_check_attempted": False}
        context = page = None
        api_check_attempted = False
        corr = f"task_{task.id}_reply_visibility_r{round_index}_a{attempt + 1}_acc{account.id}"
        try:
            references = publisher_identities + observer_identities
            keys = _identity_keys(account, schema._get_cached_http_preflight_username(account), user_id=schema._get_cached_http_preflight_user_id(account))
            if any(keys & reference for reference in references):
                return {"observer_id": account.id, "reason": "observer_identity_not_independent", "api_check_attempted": False, "candidate_counted": False}
            deny = await schema._preflight_cookie_http_gate(db, account, correlation_id=corr, phase="reply_visibility_pre_browser")
            if deny:
                return {"observer_id": account.id, "reason": deny, "api_check_attempted": False}
            username = schema._get_cached_http_preflight_username(account)
            keys = _identity_keys(account, username, user_id=schema._get_cached_http_preflight_user_id(account))
            rejection = identity_rejection_reason(keys, references)
            if rejection:
                return {"observer_id": account.id, "reason": rejection, "api_check_attempted": False,
                        "candidate_counted": rejection != "observer_identity_not_independent"}
            context, page = await schema.create_page_for_account(account)
            ready, deny = await schema._ensure_account_login_ready(
                db, page, account, url, timeout_ms=int(settings.PLAYWRIGHT_TIMEOUT * (2 if account.proxy else 1)), correlation_id=corr,
            )
            if not ready:
                return {"observer_id": account.id, "reason": deny or "observer_not_ready", "api_check_attempted": False}
            observer_keys.update(keys)
            observer_identities.append(keys)
            _, valid, info = await warmup_page_for_http(page, url, correlation_id=corr, account=account, db=db)
            if not valid or not schema.is_on_expected_media_url(getattr(page, "url", None), url):
                return {"observer_id": account.id, "reason": (info or {}).get("reason_code") or "observer_wrong_video", "api_check_attempted": False}
            groups = defaultdict(list)
            for slot in pending:
                groups[str(slot.item.get("parent_comment_id") or "")].append(slot.comment_id)
            branches, found, absent, complete_ids = [], [], [], []
            async with comment_read_batch(page):
                for parent, ids in groups.items():
                    api_check_attempted = True
                    evidence = await scan_reply_comment_ids(page, media_id, parent, ids, max_pages=settings.REPLY_PUBLICATION_VISIBILITY_MAX_PAGES)
                    branches.append(evidence)
                    found.extend(evidence["found_ids"])
                    absent.extend(evidence["absent_ids"])
                    if evidence["complete"]:
                        complete_ids.extend(ids)

            if str(os.getenv("AUTO_BOOST_LIKES", "1")).strip().lower() not in {"0", "false", "no", "off"} and (found or groups):
                try:
                    from executor.http.playwright_http_client import PlaywrightHttpClient
                    obs_client = await PlaywrightHttpClient.from_page(page)
                    boosted_cids = set()
                    task_settings = getattr(task, "settings", None) or {}
                    reply_like_target = str(task_settings.get("reply_like_target") or "1").strip().lower()
                    reply_like_count = int(task_settings.get("reply_like_count") or 0)

                    # 1. Boost per branch: parent comment and specified reply targets
                    branches_to_boost = [b for b in branches if b.get("parent_comment_id")]
                    if not branches_to_boost and groups:
                        branches_to_boost = [{"parent_comment_id": p, "found_ids": [cid for cid in found if cid in ids]} for p, ids in groups.items()]

                    for branch_info in branches_to_boost:
                        parent_cid = str(branch_info.get("parent_comment_id") or "").strip()
                        branch_found = [str(x).strip() for x in (branch_info.get("found_ids") or []) if valid_comment_id(str(x).strip())]

                        # 1a. Like parent comment
                        if (
                            valid_comment_id(parent_cid)
                            and parent_cid not in boosted_cids
                            and schema.can_boost_parent(parent_cid, account.id)
                        ):
                            boosted_cids.add(parent_cid)
                            try:
                                like_ok, like_err, _ = await obs_client.like_comment(
                                    aweme_id=media_id,
                                    cid=parent_cid,
                                    correlation_id=f"{corr}_boost_parent_{parent_cid[:15]}",
                                    video_url=url,
                                )
                                if like_ok:
                                    schema.record_parent_boost(parent_cid, account.id)
                                    logger.info(
                                        "[HTTP_AUTO_BOOST] Reply observer %s liked parent comment %s",
                                        account.id, parent_cid[:20]
                                    )
                                else:
                                    logger.debug("[HTTP_AUTO_BOOST] Reply observer %s like parent non-ok: %s", account.id, like_err)
                                await asyncio.sleep(random.uniform(0.6, 1.3))
                            except Exception:
                                pass

                        # 1b. Like target replies in this branch
                        if reply_like_count > 0 and reply_like_target not in {"0", "none", "off", "false"}:
                            target_reply_cids = []
                            digits = re.findall(r"\d+", reply_like_target)
                            if digits:
                                target_variant_idx = int(digits[0])
                                # 1. Поиск по известным веткам публикации (точный variant_index)
                                known_in_branch = schema._get_branch_published_replies(parent_cid)
                                for item in known_in_branch:
                                    if item.get("index") == target_variant_idx and item.get("cid") in branch_found:
                                        target_reply_cids.append(item.get("cid"))

                                # 2. Если в in-memory реестре ветки нет (напр. другой процесс/рестарт), ищем по слотам
                                if not target_reply_cids:
                                    for slot in pending:
                                        if str(slot.item.get("parent_comment_id") or "").strip() == parent_cid:
                                            slot_idx = int(slot.item.get("variant_index") or (slot.index + 1) or 0)
                                            if slot_idx == target_variant_idx and slot.comment_id in branch_found:
                                                target_reply_cids.append(slot.comment_id)

                                # 3. Резервный фолбэк по позиции в branch_found
                                if not target_reply_cids and 0 <= (target_variant_idx - 1) < len(branch_found):
                                    target_reply_cids.append(branch_found[target_variant_idx - 1])

                            elif reply_like_target == "all":
                                # При выборе 'все' таргетируются ВСЕ найденные ответы этой ветки
                                target_reply_cids.extend(branch_found)

                            for reply_cid in target_reply_cids:
                                clean_reply = str(reply_cid).strip()
                                if (
                                    valid_comment_id(clean_reply)
                                    and clean_reply not in boosted_cids
                                    and schema.can_boost_reply(clean_reply, account.id, max_likes=reply_like_count)
                                ):
                                    boosted_cids.add(clean_reply)
                                    try:
                                        like_ok, like_err, _ = await obs_client.like_comment(
                                            aweme_id=media_id,
                                            cid=clean_reply,
                                            correlation_id=f"{corr}_boost_reply_{clean_reply[:15]}",
                                            video_url=url,
                                        )
                                        if like_ok:
                                            # Передаем parent_cid для надежной регистрации в реестре
                                            schema.record_reply_boost(clean_reply, account.id, parent_id=parent_cid)
                                            logger.info(
                                                "[HTTP_AUTO_BOOST] Reply observer %s liked verified reply %s (parent=%s)",
                                                account.id, clean_reply[:20], parent_cid[:20]
                                            )
                                        else:
                                            logger.debug("[HTTP_AUTO_BOOST] Reply observer %s like reply non-ok: %s", account.id, like_err)
                                        await asyncio.sleep(random.uniform(0.6, 1.3))
                                    except Exception:
                                        pass
                except Exception as boost_err:
                    logger.debug("[HTTP_AUTO_BOOST] Reply observer boost failed (non-blocking): %s", boost_err)

            return {"observer_id": account.id, "found_ids": found, "absent_ids": absent,
                    "complete_ids": complete_ids, "complete": all(b["complete"] for b in branches),
                    "branches": branches, "reason": "branches_checked", "api_check_attempted": api_check_attempted}
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            return {"observer_id": account.id, "reason": f"observer_exception:{type(exc).__name__}",
                    "api_check_attempted": api_check_attempted}
        finally:
            try:
                await schema._safe_close(context, page, reason="reply_visibility_checked", correlation_id=corr)
            finally:
                schema.release_reply_account_reservation(account.id)

    async def replace(slot, round_index):
        if not http_first_enabled(settings):
            return None, "http_disabled"
        parent = str(slot.item.get("parent_comment_id") or "")
        if not valid_comment_id(parent) or not slot.text:
            return None, "invalid_reply_target"
        url, media_id = await resolve_target()
        if not media_id:
            return None, "video_id_unavailable"
        def capture(outcome):
            if str(outcome.parent_comment_id or "") != parent or not valid_comment_id(outcome.reply_comment_id):
                return
            item = {**slot.item, **_engagement_reply_outcome_to_dict(outcome, attempts=1),
                    "reply_comment_id": str(outcome.reply_comment_id), "reply_id": str(outcome.reply_comment_id),
                    "public_confirmed": False, "counts_as_submitted": True}
            receipt = ReplySlot(item, slot.index)
            if any(existing.comment_id == receipt.comment_id for _, existing in captured):
                return
            captured.append((slot, receipt))
            result[REPORT_KEY]["pending_receipts"].append({**item, "replaces_comment_id": slot.comment_id})
            save()  # The receipt is durable before browser cleanup can block or be cancelled.
        browser_attempts = 0
        browser_limit = max(1, int(settings.REPLY_PUBLICATION_VISIBILITY_OBSERVER_ATTEMPTS))
        for _ in range(max(1, int(settings.MAX_RETRY_ACCOUNTS))):
            if browser_attempts >= browser_limit:
                break
            if checkpoint_failed or stop_requested():
                return None, "stopped_by_user"
            account = select_account()
            if account is None:
                return None, "no_replacement_accounts"
            corr = f"task_{task.id}_reply_visibility_repair_r{round_index}_acc{account.id}"
            try:
                deny = await schema._preflight_cookie_http_gate(db, account, correlation_id=corr, phase="reply_visibility_repair_pre_browser")
                if deny:
                    continue
                keys = _identity_keys(account, schema._get_cached_http_preflight_username(account),
                                      user_id=schema._get_cached_http_preflight_user_id(account))
                if not identities_independent(keys, publisher_identities + observer_identities):
                    continue
                if checkpoint_failed or stop_requested():
                    return None, "stopped_by_user"
                candidate = schema.PublishedMain(
                    text=slot.item.get("matched_main_text") or "", comment_id=parent,
                    network_confirmed=True, local_confirmed=False, method="http_parent_id", account_id=0,
                    tiktok_username=slot.item.get("matched_parent_author") or slot.item.get("requested_parent_author"),
                    physical_parent_key=slot.item.get("physical_parent_key") or parent,
                )
                holder = {}
                browser_attempts += 1
                attempt = await schema._reply_with_variant_on_all_mains(
                    db, account, url, [candidate], slot.text, variant_index=slot.variant_index or 1,
                    correlation_id=corr, fallback_allowed=False, on_submitted_outcome=capture,
                    visibility_http_only=True, parent_search_progress_sink=holder,
                )
                publication_attempted = bool(holder.get("publication_attempted")) or any(
                    outcome.success or outcome.submitted_unconfirmed or bool(
                        outcome.attempted if outcome.publication_attempted is None else outcome.publication_attempted
                    ) for outcome in attempt.replies
                )
                for outcome in attempt.replies:
                    if outcome.success:
                        capture(outcome)
                receipt = next((new for old, new in captured if old is slot), None)
                if receipt or publication_attempted:
                    publisher_keys.update(keys)
                    publisher_identities.append(keys)
                reason = None if receipt else attempt.fallback_error or next(
                    (outcome.error for outcome in attempt.replies if outcome.error), "replacement_unconfirmed",
                )
                if receipt is None and is_pre_publish_account_failure(reason, publication_attempted=publication_attempted):
                    continue
                return receipt, reason
            finally:
                schema.release_reply_account_reservation(account.id)
        return None, "replacement_accounts_exhausted"

    save()
    report = {}
    try:
        report = await verify_main_publications(
            slots, observe=observe, replace=replace, stop_requested=lambda: checkpoint_failed or stop_requested(),
            repair_rounds=settings.REPLY_PUBLICATION_VISIBILITY_REPAIR_ROUNDS,
            observer_attempts=settings.REPLY_PUBLICATION_VISIBILITY_OBSERVER_ATTEMPTS,
            max_candidate_accounts=settings.MAX_RETRY_ACCOUNTS,
            initial_delay=settings.REPLY_PUBLICATION_VISIBILITY_INITIAL_DELAY_SECONDS,
            recheck_delay=settings.REPLY_PUBLICATION_VISIBILITY_RECHECK_DELAY_SECONDS,
            max_seconds=settings.REPLY_PUBLICATION_VISIBILITY_MAX_SECONDS, log_label="REPLY_VISIBILITY",
        )
    finally:
        # A successful POST can be captured just before cancellation during cleanup.
        interrupted = not report or report.get("reason") in {"stopped_by_user", "visibility_timeout"}
        interrupted = interrupted or str(report.get("reason") or "").startswith("verification_exception:")
        known_ids = original_ids | {slot.comment_id for slot in slots}
        known_accounts = original_accounts | {slot.account_id for slot in slots}
        for entry in report.get("history", []):
            known_ids.update(str(entry[key]) for key in ("comment_id", "replacement_comment_id", "replaced_by") if entry.get(key))
            known_accounts.update(entry[key] for key in ("account_id", "replacement_account_id") if entry.get(key) is not None)
        restored = 0
        for old, receipt in captured:
            if (interrupted and receipt.comment_id not in known_ids and receipt.account_id not in known_accounts
                    and any(slot is old for slot in slots)):
                position = next(i for i, slot in enumerate(slots) if slot is old)
                slots[position] = receipt
                known_ids.add(receipt.comment_id)
                known_accounts.add(receipt.account_id)
                restored += 1
                report.setdefault("history", []).append({
                    "comment_id": old.comment_id, "account_id": old.account_id,
                    "replaced_by": receipt.comment_id, "replacement_comment_id": receipt.comment_id,
                    "replacement_account_id": receipt.account_id, "parent_comment_id": old.item.get("parent_comment_id"),
                    "reason": "receipt_restored_after_interruption",
                })
        for slot in slots:
            slot.item["public_confirmed"] = slot.public_confirmed
            slot.item["visibility_status"] = "public_confirmed" if slot.public_confirmed else "submitted_unconfirmed"
            items[slot.index] = slot.item
            if slot.index < len(legacy_locations):
                child_items, child_index = legacy_locations[slot.index]
                child_items[child_index] = slot.item
        for section in legacy_sections:
            section["ok"] = sum(bool(item.get("public_confirmed")) for item in section.get("items", []) if isinstance(item, dict))
        confirmed = sum(slot.public_confirmed for slot in slots)
        report.update(enabled=True, expected=expected, confirmed=confirmed,
                      status="confirmed" if expected > 0 and confirmed >= expected and confirmed == len(slots) else "unconfirmed")
        report.setdefault("reason", "reply_parent_not_in_order" if invalid_targets else (
            "verification_interrupted" if not report.get("checks") else "checks_finished"))
        report["pending_receipts"] = copy.deepcopy(result[REPORT_KEY]["pending_receipts"])
        report["invalid_targets"] = invalid_targets
        report["replacements"] = int(report.get("replacements", 0)) + restored
        for entry in report.get("history", []):
            slot_number = entry.get("slot")
            if isinstance(slot_number, int) and 1 <= slot_number <= len(original_slots):
                entry["parent_comment_id"] = original_slots[slot_number - 1].item.get("parent_comment_id")
        old_items = {str(item.get("comment_id")): item for item in report.get("items", [])}
        report["items"] = [{**old_items.get(slot.comment_id, {}), "comment_id": slot.comment_id,
                            "parent_comment_id": slot.item.get("parent_comment_id"), "account_id": slot.account_id,
                            "status": "public_confirmed" if slot.public_confirmed else old_items.get(slot.comment_id, {}).get("status", "inconclusive")}
                           for slot in slots]
        report["missing"] = sum(item["status"] == "not_found" for item in report["items"])
        report["unknown"] = max(0, expected - confirmed - report["missing"])
        report["duration_seconds"] = round(time.monotonic() - started, 2)
        result[REPORT_KEY] = report
        replies.update(ok=confirmed, public_confirmed=confirmed, visibility_complete=report["status"] == "confirmed",
                       submitted_unconfirmed=max(0, int(replies.get("submitted") or len(slots)) - confirmed))
        if report["status"] != "confirmed":
            if result.get("status") in {"success", "submitted_unconfirmed"}:
                result["status"] = result["core_status"] = "submitted_unconfirmed"
            result["error"] = result.get("error") or "reply_visibility_unconfirmed"
            result.setdefault("warnings", []).append({"code": "reply_visibility_unconfirmed"})
            result["has_warnings"] = True
        timing = result.get("timing")
        if isinstance(timing, dict):
            ended = datetime.now(timezone.utc)
            timing["ended_at"] = ended.isoformat()
            try:
                beginning = datetime.fromisoformat(str(timing.get("started_at")).replace("Z", "+00:00"))
                timing["duration_ms"] = max(0, int((ended - beginning).total_seconds() * 1000))
            except (ValueError, TypeError):
                pass
        save()
        if checkpoint_failed:
            report.update(status="unconfirmed", reason="checkpoint_failed")
            replies["visibility_complete"] = False
            result["status"] = result["core_status"] = "submitted_unconfirmed"
            result["error"] = "reply_visibility_unconfirmed"
        logger.info("[REPLY_VISIBILITY] task=%s confirmed=%s/%s missing=%s unknown=%s replacements=%s seconds=%s reason=%s",
                    task.id, confirmed, expected, report["missing"], report["unknown"], report["replacements"],
                    report["duration_seconds"], report["reason"])
    return result
