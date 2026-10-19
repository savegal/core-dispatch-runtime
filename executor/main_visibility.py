"""Independent HTTP visibility checks and bounded replacement of missing mains."""
from __future__ import annotations

import asyncio
import logging
import time

from executor.http.comment_discovery import fetch_comment_page
from executor.http.execution_policy import valid_comment_id
from executor.http.comment_scanner import scan_comment_ids, comment_id_key

logger = logging.getLogger(__name__)
_PENDING_CLEANUP = set()


def _cleanup_finished(task):
    _PENDING_CLEANUP.discard(task)
    if not task.cancelled():
        task.exception()


def main_visibility_enabled(settings) -> bool:
    value = (settings or {}).get("verify_main_visibility", False)
    return value is True or (isinstance(value, str) and value.strip().lower() in {"true", "1", "yes", "on"})


async def scan_main_comment_ids(page, aweme_id, comment_ids, *, max_pages=20):
    """Read fresh exact top-level IDs; incomplete traversals never prove absence."""
    aweme_id = str(aweme_id).strip()
    return await scan_comment_ids(
        page, aweme_id, comment_ids, max_pages=max_pages,
        read_page=lambda cursor: fetch_comment_page(page, str(aweme_id), cursor, fresh=True),
    )


class _Stopped(Exception):
    pass


async def verify_main_publications(
    mains, *, observe, replace, stop_requested=lambda: False,
    repair_rounds=2, observer_attempts=6, max_candidate_accounts=30, initial_delay=10.0,
    recheck_delay=10.0, max_seconds=300.0,
    log_label="MAIN_VISIBILITY",
):
    """Update logical main slots, retaining every superseded receipt in history.

    observe() must use a different account from every publisher and returns
    exact-ID scan evidence plus observer_id. Replacement requires two distinct
    observers to finish the full list without that ID. Unknown outcomes never
    authorize another write. Refusals before a scan set api_check_attempted=False:
    they use the per-round candidate budget, not the API-check budget.
    replace() returns (new_main_or_None, reason).
    Two fully absent batches (originals, then all first replacements) stop any
    further writes. This is an operational limit, not a moderation diagnosis.
    """
    report = {"enabled": True, "status": "checking", "checks": [], "history": [],
              "replacements": 0, "expected": len(mains), "items": [],
              "observer_candidates": 0, "observer_attempts": 0,
              "observer_identity_skips": 0, "observer_selections": 0,
              "round_summaries": []}
    deadline = time.monotonic() + max(1.0, float(max_seconds))
    all_ids = {comment_id_key(m.comment_id) for m in mains}
    absent_by_id = {}
    blocked_slots = set()
    publisher_ids = {m.account_id for m in mains}
    first_replacement_batch = None

    def checkpoint():
        if stop_requested():
            raise _Stopped()
        if time.monotonic() >= deadline:
            raise asyncio.TimeoutError()

    async def wait(seconds):
        until = time.monotonic() + max(0.0, seconds)
        while time.monotonic() < until:
            checkpoint()
            await asyncio.sleep(min(1.0, until - time.monotonic(), max(0.001, deadline - time.monotonic())))
        checkpoint()

    async def bounded(call):
        checkpoint()
        operation = asyncio.ensure_future(call())
        try:
            while True:
                remaining = max(0.001, deadline - time.monotonic())
                done, _ = await asyncio.wait({operation}, timeout=min(0.25, remaining))
                if done:
                    return operation.result()
                checkpoint()
        finally:
            if not operation.done():
                operation.cancel()
                done, _ = await asyncio.wait({operation}, timeout=0.5)
                if not done:
                    # Browser shutdown must not defeat the order deadline.
                    # Keep cleanup owned until it releases its account lease.
                    report["cleanup_pending"] = True
                    _PENDING_CLEANUP.add(operation)
                    operation.add_done_callback(_cleanup_finished)
            if operation.done() and not operation.cancelled():
                operation.exception()

    for main in mains:
        main.public_confirmed = False
    input_error = ("duplicate_comment_ids" if len(all_ids) != len(mains)
                   else ("invalid_comment_ids" if any(not valid_comment_id(m.comment_id) for m in mains) else None))
    round_limit = max(0, min(3, int(repair_rounds)))
    if input_error:
        report["reason"] = input_error
    try:
        for round_index in range(0 if input_error else round_limit + 1):
            if not mains or all(m.public_confirmed for m in mains):
                break
            await wait(initial_delay)
            previous_read = False
            working_attempts = 0
            working_limit = max(2, min(12, int(observer_attempts)))
            candidate_limit = max(1, int(max_candidate_accounts))
            candidates_used = 0
            # Duplicate imports are cheap exclusions, with a separate hard bound.
            for candidate_index in range(candidate_limit * 4):
                if working_attempts >= working_limit or candidates_used >= candidate_limit:
                    break
                pending = [m for m in mains if not m.public_confirmed
                           and len(absent_by_id.get(comment_id_key(m.comment_id), set())) < 2
                           and valid_comment_id(m.comment_id)]
                if not pending:
                    break
                if previous_read:
                    await wait(recheck_delay)
                evidence = await bounded(lambda: observe(pending, round_index, candidate_index))
                evidence = dict(evidence or {})
                evidence["round"] = round_index
                candidate_counted = evidence.get("candidate_counted") is not False
                candidates_used += int(candidate_counted)
                evidence["candidate"] = candidates_used
                evidence["selection"] = candidate_index + 1
                # Adapters explicitly mark refusals before a comment API scan.
                # Old/custom observers without this field retain their budget semantics.
                counted = evidence.get("api_check_attempted") is not False
                working_attempts += int(counted)
                report["observer_candidates"] += int(candidate_counted)
                report["observer_identity_skips"] += int(not candidate_counted)
                report["observer_selections"] += 1
                report["observer_attempts"] += int(counted)
                evidence["observer_attempt"] = working_attempts if counted else None
                report["checks"].append(evidence)
                observer_id = evidence.get("observer_id")
                # A same-author read can prove only local visibility.
                independent = observer_id is not None and observer_id not in publisher_ids
                found = set(map(comment_id_key, evidence.get("found_ids") or [])) if independent else set()
                absent = set(map(comment_id_key, evidence.get("absent_ids") or [])) if independent else set()
                if not evidence.get("complete"):
                    # Branch checks can finish some parents while another is unavailable.
                    absent &= set(map(comment_id_key, evidence.get("complete_ids") or []))
                previous_read = bool(found or absent)
                for main in pending:
                    cid = comment_id_key(main.comment_id)
                    if cid in found:
                        main.public_confirmed = True
                    elif cid in absent:
                        absent_by_id.setdefault(cid, set()).add(observer_id)
                logger.info("[%s] round=%s candidate=%s/%s check=%s/%s api_check_attempted=%s observer=%s found=%s absent=%s reason=%s",
                            log_label, round_index, candidates_used, candidate_limit, working_attempts,
                            working_limit, counted, observer_id, len(found), len(absent), evidence.get("reason"))
                if evidence.get("reason") == "no_observer_accounts" or evidence.get("fatal"):
                    report["reason"] = evidence.get("reason")
                    break
            if working_attempts < working_limit and any(
                    not main.public_confirmed and len(absent_by_id.get(comment_id_key(main.comment_id), set())) < 2
                    for main in mains
                ) and not report.get("reason"):
                if candidates_used >= candidate_limit:
                    report["reason"] = "observer_candidate_limit"
                elif candidate_index + 1 >= candidate_limit * 4:
                    report["reason"] = "observer_selection_limit"
            current_ids = {comment_id_key(main.comment_id) for main in mains}
            confirmed_count = sum(bool(main.public_confirmed) for main in mains)
            missing_count = sum(not main.public_confirmed and len(
                absent_by_id.get(comment_id_key(main.comment_id), set())) >= 2 for main in mains)
            report["round_summaries"].append({
                "round": round_index, "expected": len(mains), "confirmed": confirmed_count,
                "missing": missing_count, "unknown": len(mains) - confirmed_count - missing_count,
                "observer_attempts": working_attempts,
            })
            fully_absent = len(mains) >= 2 and missing_count == len(mains)
            if round_index == 1 and fully_absent and current_ids == first_replacement_batch:
                report["reason"] = "repeated_full_absence"
                logger.warning(
                    "[%s] stopping further publications: originals and the full first replacement "
                    "batch were not found by independent observers; expected=%s reason=%s",
                    log_label, len(mains), report["reason"],
                )
                break
            if round_index >= round_limit:
                break
            repaired = False
            retryable_selection_failure = False
            replacements_before = report["replacements"]
            for index, main in enumerate(list(mains)):
                checkpoint()
                if (main.public_confirmed or index in blocked_slots
                        or len(absent_by_id.get(comment_id_key(main.comment_id), set())) < 2):
                    continue
                new_main, reason = await bounded(lambda: replace(main, round_index + 1))
                if new_main is None and reason == "stopped_by_user":
                    raise _Stopped()
                entry = {"slot": index + 1, "comment_id": main.comment_id,
                         "account_id": main.account_id, "text": main.text,
                         "replacement_round": round_index + 1, "reason": reason,
                         "absent_observer_ids": sorted(absent_by_id[comment_id_key(main.comment_id)])}
                if new_main is not None:
                    entry["replacement_comment_id"] = new_main.comment_id
                    entry["replacement_account_id"] = new_main.account_id
                if (new_main is None or not valid_comment_id(new_main.comment_id)
                        or comment_id_key(new_main.comment_id) in all_ids or new_main.account_id in publisher_ids):
                    entry["replacement_failed"] = True
                    report["history"].append(entry)
                    # Only account-selection failures prove that no write occurred.
                    if new_main is None and reason in {
                        "no_replacement_accounts", "no_independent_replacement_accounts", "replacement_accounts_exhausted",
                    }:
                        retryable_selection_failure = True
                    else:
                        blocked_slots.add(index)
                    continue
                entry["replaced_by"] = new_main.comment_id
                report["history"].append(entry)
                all_ids.add(comment_id_key(new_main.comment_id))
                publisher_ids.add(new_main.account_id)
                new_main.public_confirmed = False
                new_main.variant_index = main.variant_index
                mains[index] = new_main
                report["replacements"] += 1
                repaired = True
            if round_index == 0 and fully_absent and report["replacements"] - replacements_before == len(mains):
                first_replacement_batch = {comment_id_key(main.comment_id) for main in mains}
            if not repaired and not retryable_selection_failure:
                break
    except _Stopped:
        report["reason"] = "stopped_by_user"
    except asyncio.TimeoutError:
        report["reason"] = "visibility_timeout"
    except Exception as exc:
        # Preserve receipts even when the verification service itself fails.
        logger.exception("[%s] verification failed", log_label)
        report["reason"] = f"verification_exception:{type(exc).__name__}"
    for index, main in enumerate(mains):
        absent = sorted(absent_by_id.get(comment_id_key(main.comment_id), set()))
        report["items"].append({"slot": index + 1, "comment_id": main.comment_id,
                                "account_id": main.account_id, "text": main.text,
                                "status": "public_confirmed" if main.public_confirmed else (
                                    "not_found" if len(absent) >= 2 else "inconclusive"),
                                "absent_observer_ids": absent})
    report["confirmed"] = sum(bool(m.public_confirmed) for m in mains)
    report["missing"] = sum(i["status"] == "not_found" for i in report["items"])
    report["unknown"] = len(mains) - report["confirmed"] - report["missing"]
    report["status"] = "confirmed" if mains and report["confirmed"] == len(mains) else "unconfirmed"
    return report
