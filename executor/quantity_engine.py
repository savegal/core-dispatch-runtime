"""Quantity orders with durable logical slots and conservative delivery recovery."""
from __future__ import annotations

import asyncio
from copy import deepcopy
import hashlib
import logging
import time
from types import SimpleNamespace
from uuid import uuid4

from sqlalchemy import func, update
from sqlalchemy.orm import Session

from db.models import PublicationAttempt, QuantityAction, Task
from services.async_ownership import await_owned
from services.quantity_plan import quantity_plan, traffic_budget_exhausted, traffic_control_reason
from services.publication_journal import quantity_action, utcnow

logger = logging.getLogger(__name__)
INCONCLUSIVE_RECHECK_DELAYS = (2.0, 5.0, 10.0)
TASK_CONTROL_POLL_SECONDS = 1.0

# A gateway/IP-verification outage is not evidence that an account is dead.
# Keep the per-slot candidate ceiling as a hard bound, while limiting how fast
# a quantity order can consume new candidates during repeated route failures.
_PROXY_PREFLIGHT_ERRORS = frozenset({
    "proxy_authentication_required",
    "proxy_capacity_timeout",
    "proxy_coordination_unavailable",
    "residential_coordination_unavailable",
    "residential_route_already_assigned",
    "residential_exit_ip_in_use",
})


def _proxy_preflight_failure(reason, progress):
    if progress.get("publication_call_started") or not isinstance(reason, str):
        return False
    # ProxyRouteError stringifies with a "proxy_route:" prefix; the cookie gate
    # passes that string verbatim. Never require a bare error code here.
    code = reason.removeprefix("proxy_route:")
    return code in _PROXY_PREFLIGHT_ERRORS or code.startswith("residential_ip_probe_")


def _proxy_preflight_backoff_seconds(streak):
    if streak < 3:
        return 0.0
    return min(30.0, float(2 ** min(5, streak - 2)))


async def _owned_db_call(function, *args, **kwargs):
    operation = asyncio.create_task(asyncio.to_thread(function, *args, **kwargs))
    return await await_owned(operation)


def action_id(task_id, url, kind, slot, parent=""):
    return hashlib.sha256(f"{task_id}:{url}:{kind}:{slot}:{parent}".encode()).hexdigest()[:32]


def _task_stopped(task):
    return task is None or task.status == "stopped" or any(
        (task.settings or {}).get(flag) for flag in ("stopped_by_user", "cancelled_by_user"))


def _task_control_reason(engine, task_id, *, checkpointed=True):
    """Read authoritative controls in a short session, separate from HTTP work."""
    with Session(engine) as control:
        task = control.get(Task, task_id)
        if _task_stopped(task):
            return "stopped_by_user"
        return traffic_control_reason(control, task, checkpointed=checkpointed)


async def _read_task_control(engine, task_id):
    from services.traffic_meter import flush_task_traffic
    checkpointed = await flush_task_traffic(engine, task_id)
    return await _owned_db_call(_task_control_reason, engine, task_id, checkpointed=checkpointed)


def ensure_actions(db, task, url, kind, count, texts, *, parent=None):
    from sqlalchemy.dialects.sqlite import insert as sqlite_insert
    from sqlalchemy.dialects.postgresql import insert as pg_insert
    from services.spintax import expand_spintax
    if count and not texts:
        raise ValueError(f"quantity_{kind}_text_missing")
    insert = sqlite_insert if db.get_bind().dialect.name == "sqlite" else pg_insert
    for slot in range(1, count + 1):
        identity = action_id(task.id, url, kind, slot, parent.id if parent else "")
        text = expand_spintax(texts[(slot - 1) % len(texts)], seed=identity)
        db.execute(insert(QuantityAction).values(id=identity, task_id=task.id, video_url=url, kind=kind,
            slot=slot, parent_action_id=parent.id if parent else None,
            parent_id=parent.comment_id if parent else None, text=text, state="pending", candidate_count=0,
            updated_at=utcnow()).on_conflict_do_nothing(index_elements=["id"]))
    db.commit()


def claim_action(db, identity):
    owner = db.query(QuantityAction.task_id, QuantityAction.video_url).filter(QuantityAction.id == identity).first()
    if owner is None:
        return None
    db.rollback()
    # Serialize budget reservations across concurrent workers before dispatch.
    db.execute(update(Task).where(Task.id == owner.task_id).values(updated_at=utcnow()))
    task = db.get(Task, owner.task_id, populate_existing=True)
    if _task_stopped(task):
        db.commit()
        return None
    plan = quantity_plan(task.settings)
    if plan:
        reserved = db.query(func.coalesce(func.sum(QuantityAction.dispatches), 0)).filter(
            QuantityAction.task_id == owner.task_id, QuantityAction.video_url == owner.video_url).scalar()
        if reserved >= plan.max_dispatches:
            db.commit()
            return None
    token = uuid4().hex
    changed = db.execute(update(QuantityAction).where(QuantityAction.id == identity,
        QuantityAction.state.in_(["pending", "not_sent", "absent"]), QuantityAction.candidate_count < 50)
        .values(state="dispatching", token=token, candidate_count=QuantityAction.candidate_count + 1,
                dispatches=QuantityAction.dispatches + 1, comment_id=None, updated_at=utcnow())).rowcount
    db.commit()
    return token if changed else None


def _release_unstarted_claim(db, identity, token, error):
    db.rollback()
    db.execute(update(QuantityAction).where(QuantityAction.id == identity, QuantityAction.token == token,
        QuantityAction.state == "dispatching", QuantityAction.dispatches > 0)
        .values(state="not_sent", token=None, dispatches=QuantityAction.dispatches - 1,
                error_code=error, updated_at=utcnow()))
    db.commit()


def _authorize_claim(db, task_id, identity, token, account_id, *, checkpointed=True):
    from executor.http.execution_policy import valid_comment_id
    db.rollback()
    # Match the stop API's task lock before checking a selection that may have
    # taken seconds. The browser operation runs only after this transaction ends.
    db.execute(update(Task).where(Task.id == task_id).values(updated_at=Task.updated_at))
    task = db.get(Task, task_id, populate_existing=True)
    action = db.get(QuantityAction, identity, populate_existing=True)
    if action is None or action.task_id != task_id or action.token != token or action.state != "dispatching":
        db.commit()
        return "not_claimed"
    reason = "stopped" if _task_stopped(task) else None
    if reason is None:
        reason = traffic_control_reason(db, task, checkpointed=checkpointed)
    if action.kind == "reply" and reason is None:
        parent = db.get(QuantityAction, action.parent_action_id, populate_existing=True)
        if not (parent is not None and parent.kind == "main" and parent.task_id == task_id
                and parent.video_url == action.video_url and parent.state == "visible"
                and valid_comment_id(parent.comment_id) and parent.comment_id == action.parent_id):
            reason = "parent_not_public"
    if reason is not None:
        _release_unstarted_claim(db, identity, token, reason)
        return reason
    changed = db.execute(update(QuantityAction).where(QuantityAction.id == identity,
        QuantityAction.task_id == task_id, QuantityAction.token == token, QuantityAction.state == "dispatching")
        .values(account_id=account_id), execution_options={"synchronize_session": False}).rowcount
    db.commit()
    return None if changed else "not_claimed"


def _finalize_claim(db, task_id, identity, token, receipt, reason, *, entered, completed, progress):
    """A late owner may finish its own attempt, never replace a newer slot."""
    db.rollback()
    action = db.get(QuantityAction, identity, populate_existing=True)
    if action is None:
        return "not_claimed"
    if action.task_id != task_id or action.token != token or action.state in {"visible", "absent"}:
        return action.state
    state = action.state
    values = dict(updated_at=utcnow())
    if receipt is not None:
        from executor.http.execution_policy import valid_comment_id
        cid = getattr(receipt, "comment_id", None) or getattr(receipt, "reply_comment_id", None)
        if valid_comment_id(cid):
            values.update(comment_id=str(cid), state="accepted")
            state = "accepted"
    if state == "dispatching":
        proven_not_sent = (not entered or progress.get("publication_call_started") is False
            or (completed and progress.get("publication_attempted") is not True))
        state = "not_sent" if proven_not_sent else "uncertain"
        values["state"] = state
    if state == "not_sent":
        values["dispatches"] = max(0, int(action.dispatches or 0) - 1)
    from services.publication_journal import _safe_error
    values["error_code"] = _safe_error(reason)
    # Fence the read above as well: another SQL connection may advance this
    # CID while we prepare its completion. No ORM flush can bypass this CAS.
    changed = db.execute(update(QuantityAction).where(
        QuantityAction.id == identity, QuantityAction.task_id == task_id, QuantityAction.token == token,
        QuantityAction.state == action.state, QuantityAction.comment_id == action.comment_id,
        QuantityAction.dispatches == action.dispatches).values(**values),
        execution_options={"synchronize_session": False}).rowcount
    db.commit()
    return state if changed else db.get(QuantityAction, identity, populate_existing=True).state


def seed_resume_actions(db, task, url):
    slots = ((task.settings or {}).get("_quantity_resume") or {}).get(url) or []
    if not slots:
        return
    parents = {}
    for saved in [s for s in slots if s.get("kind") == "main"]:
        identity = action_id(task.id, url, "main", saved["slot"])
        row = db.get(QuantityAction, identity)
        if row is not None:
            parents[saved["id"]] = row
    for saved in sorted(slots, key=lambda s: (s["kind"] != "main", s["slot"])):
        parent = parents.get(saved.get("parent_action_id")) if saved["kind"] == "reply" else None
        if saved["kind"] == "reply":
            if parent is None or not parent.comment_id or str(parent.comment_id) != str(saved.get("parent_id")):
                continue
            ensure_actions(db, task, url, "reply", saved["slot"], [saved["text"]], parent=parent)
        identity = action_id(task.id, url, saved["kind"], saved["slot"], parent.id if parent else "")
        row = db.get(QuantityAction, identity)
        if row is None or row.state != "pending" or row.candidate_count:
            continue
        row.state = "uncertain" if saved["state"] == "dispatching" else saved["state"]
        row.comment_id = str(saved.get("comment_id")) if str(saved.get("comment_id") or "").isdigit() else None
        row.account_id = saved.get("account_id")
        if row.state not in {"pending", "not_sent"}:
            row.text = saved["text"]
        if row.state in {"visible", "accepted", "absent"} and not row.comment_id:
            row.state = "uncertain"
        if row.comment_id:
            journal_id = hashlib.sha256(f"resume:{task.id}:{row.comment_id}".encode()).hexdigest()[:32]
            if db.get(PublicationAttempt, journal_id) is None:
                previous = db.query(PublicationAttempt).filter(
                    PublicationAttempt.task_id == (task.settings or {}).get("retry_of_task_id"),
                    PublicationAttempt.comment_id == row.comment_id).first()
                db.add(PublicationAttempt(id=journal_id, task_id=task.id, action_id=row.id,
                    video_url=url, kind=row.kind, parent_id=row.parent_id, account_id=row.account_id,
                    aweme_id=previous.aweme_id if previous else None, text=row.text,
                    text_hash=hashlib.sha256(row.text.encode()).hexdigest(), status="accepted", origin="resume",
                    wire_count=0, comment_id=row.comment_id,
                    visibility=(previous.visibility if previous and previous.checked_at else
                                "visible" if row.state == "visible" else "not_found" if row.state == "absent" else "unchecked"),
                    observers=deepcopy(previous.observers) if previous else None,
                    identity_keys=previous.identity_keys if previous else None,
                    checked_at=previous.checked_at if previous else None, visible_at=previous.visible_at if previous else None,
                    started_at=previous.started_at if previous else utcnow()))
    db.commit()


async def publish_action(engine, task_id, identity, *, stop_requested, pool_owner_user_id=None, target_url=None):
    from executor.visibility_publisher import publish_visibility_http
    from executor.http.execution_policy import valid_comment_id
    from executor.user_schema import PublishedMain
    from executor.browser_pool import browser_pool_task_scope
    from services.publication_progress import publication_scope
    from services.traffic_meter import flush_task_traffic
    from services.account_service import (get_free_account_for_main_comment, get_free_account_for_reply,
                                          release_main_account_reservation, release_reply_account_reservation)
    with Session(engine, expire_on_commit=False) as db:
        task = db.get(Task, task_id)
        if task is None:
            return "not_claimed"
        if stop_requested() or _task_stopped(task):
            return "stopped"
        if traffic_budget_exhausted(db, task):
            return "quantity_traffic_budget_exhausted"
        action = db.get(QuantityAction, identity)
        if action is None or action.task_id != task_id or action.kind not in {"main", "reply"}:
            return "not_claimed"
        action_kind = action.kind
        parent_action = db.get(QuantityAction, action.parent_action_id) if action.parent_action_id else None
        if action.kind == "reply" and not (
                parent_action is not None and parent_action.kind == "main"
                and parent_action.task_id == task_id and parent_action.video_url == action.video_url
                and parent_action.state == "visible" and valid_comment_id(parent_action.comment_id)
                and parent_action.comment_id == action.parent_id):
            return "parent_not_public"
        claim = {}
        def claim_in_worker():
            claim["token"] = claim_action(db, identity)
            return claim["token"]
        try:
            token = await _owned_db_call(claim_in_worker)
        except asyncio.CancelledError:
            token = claim.get("token")
            if token:
                await _owned_db_call(_release_unstarted_claim, db, identity, token, "stopped_before_selection")
            raise
        if token is None:
            return "not_claimed"
        action = db.get(QuantityAction, identity, populate_existing=True)
        excluded = {row[0] for row in db.query(PublicationAttempt.account_id).filter(
            PublicationAttempt.task_id == task_id, PublicationAttempt.video_url == action.video_url,
            PublicationAttempt.kind == action.kind, PublicationAttempt.wire_count > 0,
            (PublicationAttempt.action_id == identity) if action.kind == "main"
                else (PublicationAttempt.parent_id == action.parent_id)).all() if row[0] is not None}
        if parent_action and parent_action.account_id:
            excluded.add(parent_action.account_id)
        selector = get_free_account_for_main_comment if action.kind == "main" else get_free_account_for_reply
        try:
            selection = dict(exclude_ids=list(excluded), pool_owner_user_id=pool_owner_user_id)
            if action.kind == "reply":
                selection["reserve"] = True
            selection_job = asyncio.create_task(asyncio.to_thread(selector, db, **selection))
            try:
                account = await await_owned(selection_job)
            except asyncio.CancelledError:
                # A worker thread cannot be cancelled. Own its reservation
                # before unwinding so it cannot acquire a lost account later.
                try:
                    account = selection_job.result()
                except Exception:
                    account = None
                selected_id = account.id if account is not None else None
                try:
                    await _owned_db_call(_release_unstarted_claim, db, identity, token, "stopped_before_publication")
                finally:
                    if selected_id is not None:
                        release = release_main_account_reservation if action_kind == "main" else release_reply_account_reservation
                        release(selected_id)
                raise
        except Exception:
            await _owned_db_call(_release_unstarted_claim, db, identity, token, "account_preparation_failed")
            logger.warning("Account selection failed for quantity action %s", identity, exc_info=True)
            return "account_preparation_failed"
        if account is None:
            await _owned_db_call(_release_unstarted_claim, db, identity, token, "no_accounts_available")
            return "no_accounts_available"
        reserved_id = account.id
        progress, receipt, reason = {}, None, None
        context_token, entered, completed = None, False, False
        try:
            checkpointed = await flush_task_traffic(engine, task_id)
            reason = await _owned_db_call(_authorize_claim, db, task_id, identity, token, reserved_id,
                                          checkpointed=checkpointed)
            if reason is not None:
                return reason
            parent = PublishedMain(text=parent_action.text, comment_id=parent_action.comment_id,
                account_id=parent_action.account_id, method="http_quantity", variant_index=parent_action.slot,
                public_confirmed=True, network_confirmed=True, local_confirmed=False, tiktok_username=None) if parent_action else None
            # claim_action rolls back/refreshes the identity map. Hydrate the
            # persistent scope before the last commit, not during browser I/O.
            _ = task.id, task.settings, action.video_url, action.text, action.slot, account.id
            await _owned_db_call(db.commit)
            context_token = quantity_action.set((identity, token))
            with browser_pool_task_scope(task_id), publication_scope(db, task, action.video_url):
                entered = True
                receipt, reason = await publish_visibility_http(db, account, target_url or action.video_url, action.text,
                    parent=parent, variant_index=action.slot, progress=progress,
                    stop_requested=stop_requested,
                    correlation_id=f"task_{task_id}_quantity_{action.kind}_{action.slot}_acc{account.id}")
                completed = True
        finally:
            if context_token is not None:
                quantity_action.reset(context_token)
            try:
                # The journal saves dispatch/CID before optional cookie cleanup.
                final_state = await _owned_db_call(_finalize_claim, db, task_id, identity, token, receipt, reason,
                    entered=entered, completed=completed, progress=progress)
            finally:
                release = release_main_account_reservation if action_kind == "main" else release_reply_account_reservation
                release(reserved_id)
        # Expose a non-publishing infrastructure failure to the scheduler.
        # Durable state still remains "not_sent" with its original error_code.
        if final_state == "not_sent" and _proxy_preflight_failure(reason, progress):
            return "proxy_preflight_unavailable"
        return final_state


async def verify_actions(engine, task_id, identities, stop_requested, *, target_url=None):
    from services.publication_recheck import observe_video, apply_observations
    from executor.browser_pool import browser_pool_task_scope
    from services.publication_progress import publication_scope
    from executor.user_schema import extract_tiktok_media_id
    if stop_requested() or await _read_task_control(engine, task_id):
        return
    with Session(engine, expire_on_commit=False) as db:
        task = db.get(Task, task_id)
        actions = db.query(QuantityAction).filter(QuantityAction.id.in_(identities),
            QuantityAction.task_id == task_id, QuantityAction.state == "accepted").all()
        if not actions or stop_requested():
            return
        projections = []
        for action in actions:
            journal = db.query(PublicationAttempt).filter(PublicationAttempt.task_id == task_id,
                PublicationAttempt.comment_id == action.comment_id).first()
            projections.append(SimpleNamespace(id=action.id, token=action.token, account_id=action.account_id,
                video_url=target_url or action.video_url,
                aweme_id=(journal.aweme_id if journal else None) or extract_tiktok_media_id(target_url or action.video_url),
                comment_id=action.comment_id, kind=action.kind, parent_id=action.parent_id,
                identity_keys=journal.identity_keys if journal else None, visible_at=None, check_count=0))
        db.commit()
        with browser_pool_task_scope(task_id), publication_scope(db, task, actions[0].video_url):
            observations = await observe_video(db, task, projections)
        apply_observations(projections, observations)
        def persist():
            db.rollback()
            for projection in projections:
                db.execute(update(QuantityAction).where(QuantityAction.id == projection.id,
                    QuantityAction.task_id == task_id, QuantityAction.token == projection.token,
                    QuantityAction.comment_id == projection.comment_id, QuantityAction.state == "accepted")
                    .values(state="visible" if projection.visibility == "visible" else (
                        "absent" if projection.visibility in {"not_found", "author_only"} else "accepted"), updated_at=utcnow()),
                    execution_options={"synchronize_session": False})
                for row in db.query(PublicationAttempt).filter(PublicationAttempt.task_id == task_id,
                        PublicationAttempt.comment_id == projection.comment_id):
                    row.visibility, row.checked_at = projection.visibility, projection.checked_at
                    row.visible_at = row.visible_at or projection.visible_at
                    row.observers = projection.observers
                    if projection.visibility == "visible":
                        row.next_check_at = None
            db.commit()
        await _owned_db_call(persist)


def build_result(db, task, url, plan, *, boost_enabled=False, boost_quantity=0, reason=None):
    from executor.user_schema import UserSchemaResult, PublishedMain, ReplyOutcome
    from services.quantity_state import serialize_actions
    # The result outlives this SQL session. Never retain ORM objects in to_dict:
    # later expiry/rollback would reopen SQL or mix a new slot with old receipts.
    plan = plan.model_copy(deep=True)
    actions = db.query(QuantityAction).filter(QuantityAction.task_id == task.id, QuantityAction.video_url == url).all()
    actions_snapshot = serialize_actions(actions)
    task_settings = deepcopy(task.settings or {})
    previous_likes = ((task_settings.get("url_results") or {}).get(url) or {}).get("reply_likes")
    previous_likes = previous_likes or (task_settings.get("_quantity_resume_likes") or {}).get(url)
    class QuantityResult(UserSchemaResult):
        def to_dict(self, *args, **kwargs):
            payload = super().to_dict(*args, **kwargs)
            visible_replies = {r.reply_comment_id for r in self.replies if getattr(r, "public_confirmed", False)}
            for item in payload["engagement"]["replies"]["items"]:
                visible = item.get("reply_comment_id") in visible_replies
                item.update(public_confirmed=visible, network_confirmed=True,
                            visibility_status="public_confirmed" if visible else "not_checked")
            total = plan.main_count * plan.replies_per_main
            payload["reply_publication_visibility"] = dict(enabled=bool(total), expected=total,
                confirmed=len(visible_replies), status="confirmed" if len(visible_replies) == total else "unconfirmed")
            complete = self.main_visibility["confirmed"] == plan.main_count and len(visible_replies) == total
            payload.update(status="success" if complete else "partial" if self.published_mains else "fail",
                           core_status="success" if complete else "partial", publication_complete=complete)
            payload["quantity_plan"] = {**plan.model_dump(), "complete": complete,
                "visible_main": self.main_visibility["confirmed"], "visible_replies": len(visible_replies)}
            payload["quantity_slots"] = deepcopy(actions_snapshot)
            if isinstance(previous_likes, dict):
                payload["reply_likes"] = deepcopy(previous_likes)
            return payload
    result = QuantityResult(expected_main_publications=plan.main_count,
        expected_reply_variants=plan.replies_per_main, boost_enabled=boost_enabled, boost_quantity=boost_quantity)
    for action in actions:
        if not action.comment_id:
            continue
        visible = action.state == "visible"
        if action.kind == "main":
            result.published_mains.append(PublishedMain(text=action.text, comment_id=action.comment_id,
                account_id=action.account_id, method="http_quantity", variant_index=action.slot,
                network_confirmed=True, public_confirmed=visible, local_confirmed=False, tiktok_username=None))
        else:
            parent = db.get(QuantityAction, action.parent_action_id)
            reply = ReplyOutcome(text=action.text, variant_index=action.slot,
                parent_comment_id=action.parent_id, matched_main_text=parent.text if parent else "",
                success=visible, method="http_quantity", error=None if visible else "public_visibility_unconfirmed",
                account_id=action.account_id, attempted=True, publication_attempted=True,
                reply_comment_id=action.comment_id, reply_confirmation="network_comment_id", counts_as_submitted=True)
            reply.public_confirmed = visible
            result.replies.append(reply)
    confirmed_main = sum(a.kind == "main" and a.state == "visible" for a in actions)
    result.main_visibility = dict(enabled=True, expected=plan.main_count, confirmed=confirmed_main,
        status="confirmed" if confirmed_main == plan.main_count else "unconfirmed", replacements=0)
    result.error = reason
    result.main_found_by_observer = confirmed_main == plan.main_count
    return result


def _snapshot_result(engine, task_id, url, plan, *, boost_enabled=False, boost_quantity=0, reason=None):
    with Session(engine, expire_on_commit=False) as work:
        current = work.get(Task, task_id)
        if current is None:
            from executor.user_schema import UserSchemaResult
            return UserSchemaResult(error="stopped_by_user")
        return build_result(work, current, url, plan, boost_enabled=boost_enabled,
                            boost_quantity=boost_quantity, reason=reason)


async def _execute_quantity_publications(db, task, video_url, text_variants, task_id, *, boost_enabled=False,
                                 boost_quantity=0, pool_owner_user_id=None, stop_requested=None, **_):
    from config.settings import get_settings
    from executor.engagement_engine import resolve_order_video_url
    from services.quantity_plan import quantity_reply_texts
    settings, plan = get_settings(), quantity_plan(task.settings)
    stop_requested = stop_requested or (lambda: False)
    started, engine = time.monotonic(), db.get_bind().engine
    source_url = video_url
    control_reason = "stopped_by_user" if stop_requested() else await _read_task_control(engine, task_id)
    if control_reason:
        return await _owned_db_call(_snapshot_result, engine, task_id, source_url, plan,
            boost_enabled=boost_enabled, boost_quantity=boost_quantity, reason=control_reason)
    if plan.replies_per_main and not quantity_reply_texts(task):
        from executor.user_schema import UserSchemaResult
        return UserSchemaResult(error="quantity_reply_text_missing")
    video_url, media_id = await resolve_order_video_url(db, video_url, pool_owner_user_id)
    if not media_id:
        from executor.user_schema import extract_tiktok_media_id, UserSchemaResult
        media_id = extract_tiktok_media_id(video_url)
        if not media_id:
            return UserSchemaResult(error="video_url_unresolved")
    with Session(engine, expire_on_commit=False) as work:
        current = work.get(Task, task_id)
        ensure_actions(work, current, source_url, "main", plan.main_count, text_variants)
        seed_resume_actions(work, current, source_url)
    from services.publication_runtime import load_publication_runtime, order_runtime
    with Session(engine) as runtime_db:
        runtime = order_runtime(task.settings, load_publication_runtime(runtime_db))
    browser_slots = max(1, int(settings.MAX_CONCURRENT_BROWSERS))
    send_limit, check_limit = runtime.limits(browser_slots)
    sends, checks = {}, {}
    checked_rounds, ready_for_check = {}, {}
    seeded_parents = set()
    next_send, selection_blocked, preparation_failed = 0.0, False, False
    proxy_preflight_fail_streak = 0
    reason = None
    try:
        while not stop_requested() and time.monotonic() - started < plan.max_seconds:
            outcomes = []
            for job in [job for job in sends if job.done()]:
                identity = sends.pop(job)
                try:
                    outcomes.append(job.result())
                except Exception as exc:
                    outcomes.append("account_preparation_failed")
                    logger.warning("Quantity action %s failed (%s)", identity, type(exc).__name__)
            if outcomes:
                selection_blocked = all(value == "no_accounts_available" for value in outcomes)
                preparation_failed = all(value == "account_preparation_failed" for value in outcomes)
                if all(value == "proxy_preflight_unavailable" for value in outcomes):
                    proxy_preflight_fail_streak += len(outcomes)
                    pause = _proxy_preflight_backoff_seconds(proxy_preflight_fail_streak)
                    if pause:
                        next_send = max(next_send, time.monotonic() + pause)
                        logger.warning(
                            "Quantity task %s: %s consecutive proxy preflight failures; "
                            "deferring new candidates %.1fs without retiring accounts",
                            task_id, proxy_preflight_fail_streak, pause,
                        )
                else:
                    proxy_preflight_fail_streak = 0
            for job in [job for job in checks if job.done()]:
                identities = checks.pop(job)
                selection_blocked = False
                try:
                    job.result()
                except Exception as exc:
                    logger.warning("Quantity visibility check failed task=%s (%s)", task_id, type(exc).__name__)
                for identity in identities:
                    round_number = checked_rounds.get(identity, 0)
                    checked_rounds[identity] = round_number + 1
                    if round_number < len(INCONCLUSIVE_RECHECK_DELAYS):
                        ready_for_check[identity] = time.monotonic() + INCONCLUSIVE_RECHECK_DELAYS[round_number]

            now = time.monotonic()
            active_ids = set(sends.values())
            checking_ids = {identity for group in checks.values() for identity in group}
            with Session(engine, expire_on_commit=False) as work:
                current = work.get(Task, task_id)
                if _task_stopped(current):
                    reason = "stopped_by_user"
                    break
                actions = work.query(QuantityAction).filter(QuantityAction.task_id == task_id,
                    QuantityAction.video_url == source_url).all()
                parents = {a.id: a for a in actions if a.kind == "main" and a.state == "visible"}
                for parent in parents.values():
                    if plan.replies_per_main and parent.id not in seeded_parents:
                        ensure_actions(work, current, source_url, "reply", plan.replies_per_main,
                                       quantity_reply_texts(current), parent=parent)
                        seeded_parents.add(parent.id)
                if parents and plan.replies_per_main:
                    actions = work.query(QuantityAction).filter(QuantityAction.task_id == task_id,
                        QuantityAction.video_url == source_url).all()
                if sum(a.state == "visible" for a in actions) >= plan.main_count * (1 + plan.replies_per_main):
                    break
                used = sum(action.dispatches for action in actions)
                if traffic_budget_exhausted(work, current):
                    reason = "quantity_traffic_budget_exhausted"
                    break
                selectable = [a for a in actions if a.id not in active_ids and a.id not in checking_ids
                    and a.state in {"pending", "not_sent", "absent"} and a.candidate_count < 50
                    and (a.kind == "main" or (a.parent_action_id in parents
                        and parents[a.parent_action_id].comment_id == a.parent_id))]
                selectable.sort(key=lambda a: (a.kind != "main", a.slot, a.id))
                batch = [a.id for a in selectable[:min(max(0, send_limit - len(sends)), max(0, plan.max_dispatches - used))]]
                waiting = []
                for action in actions:
                    if (action.state == "accepted" and action.id not in active_ids and action.id not in checking_ids
                            and checked_rounds.get(action.id, 0) <= len(INCONCLUSIVE_RECHECK_DELAYS)):
                        ready_for_check.setdefault(action.id, now + runtime.check_delay)
                        waiting.append(action.id)

            due = [identity for identity in waiting if ready_for_check[identity] <= now]
            check_capacity = max(0, check_limit - len(checks))
            if browser_slots == 1 and sends:
                check_capacity = 0
            for offset in range(0, min(len(due), check_capacity * runtime.check_batch_size),
                                runtime.check_batch_size):
                group = due[offset:offset + runtime.check_batch_size]
                job = asyncio.create_task(verify_actions(engine, task_id, group, stop_requested, target_url=video_url))
                checks[job] = group
            can_send = not selection_blocked and not preparation_failed and (browser_slots > 1 or not checks)
            if can_send and next_send <= now:
                for identity in batch[:1] if runtime.send_interval else batch:
                    # A replacement has its own verification budget.
                    checked_rounds.pop(identity, None)
                    ready_for_check.pop(identity, None)
                    job = asyncio.create_task(publish_action(engine, task_id, identity, stop_requested=stop_requested,
                        pool_owner_user_id=pool_owner_user_id, target_url=video_url))
                    sends[job] = identity
                    next_send = now + runtime.send_interval
            pending = set(sends) | set(checks)
            wake_at = [ready_for_check[identity] for identity in waiting if ready_for_check[identity] > now]
            if batch and can_send and next_send > now:
                wake_at.append(next_send)
            delay = max(0, min(wake_at) - time.monotonic()) if wake_at else None
            if pending:
                await asyncio.wait(pending, timeout=delay, return_when=asyncio.FIRST_COMPLETED)
            elif delay is not None:
                await asyncio.sleep(delay)
            else:
                if selection_blocked:
                    reason = "no_accounts_available"
                elif preparation_failed:
                    reason = "quantity_preparation_failed"
                elif used >= plan.max_dispatches:
                    reason = "quantity_dispatch_budget_exhausted"
                else:
                    reason = "quantity_visibility_or_delivery_unconfirmed"
                break
        else:
            reason = "stopped_by_user" if stop_requested() else "quantity_time_budget_exhausted"
    finally:
        owned = [*sends, *checks]
        for job in owned:
            if not job.done():
                job.cancel()
        if owned:
            await await_owned(asyncio.gather(*owned, return_exceptions=True))
    with Session(engine, expire_on_commit=False) as work:
        current = work.get(Task, task_id)
        result = build_result(work, current, source_url, plan, boost_enabled=boost_enabled,
                              boost_quantity=boost_quantity, reason=reason)
    result.resolved_video_url, result.aweme_id = video_url, media_id
    result.elapsed_seconds = time.monotonic() - started
    return result


async def execute_quantity_order(db, task, video_url, text_variants, task_id, *, boost_enabled=False,
                                 boost_quantity=0, pool_owner_user_id=None, stop_requested=None, **kwargs):
    """Bound network waits, own cancelled writes through cleanup, then project SQL receipts."""
    from executor.user_schema import _maybe_order_likes, _reply_outcome_counts_for_likes, extract_tiktok_media_id
    stop_requested = stop_requested or (lambda: False)
    plan, started = quantity_plan(task.settings), time.monotonic()
    reason, operation, watcher = None, None, None
    deadline = asyncio.timeout(plan.max_seconds)

    async def watch_stop():
        next_poll = 0.0
        while True:
            if stop_requested():
                return "stopped_by_user"
            if time.monotonic() >= next_poll:
                reason = await _read_task_control(db.get_bind().engine, task_id)
                if reason:
                    return reason
                next_poll = time.monotonic() + TASK_CONTROL_POLL_SECONDS
            await asyncio.sleep(min(.1, TASK_CONTROL_POLL_SECONDS))

    try:
        if stop_requested():
            reason = "stopped_by_user"
        else:
            async with deadline:
                operation = asyncio.create_task(_execute_quantity_publications(db, task, video_url, text_variants,
                    task_id, boost_enabled=boost_enabled, boost_quantity=boost_quantity,
                    pool_owner_user_id=pool_owner_user_id, stop_requested=stop_requested, **kwargs))
                watcher = asyncio.create_task(watch_stop())
                await asyncio.wait({operation, watcher}, return_when=asyncio.FIRST_COMPLETED)
                if operation.done():
                    result = await operation
                else:
                    reason = await watcher
    except TimeoutError:
        if not deadline.expired():
            raise
        reason = "quantity_time_budget_exhausted"
    finally:
        owned = [job for job in (operation, watcher) if job is not None]
        for job in owned:
            if not job.done():
                job.cancel()
        if owned:
            await asyncio.gather(*owned, return_exceptions=True)
    if reason:
        with Session(db.get_bind().engine, expire_on_commit=False) as work:
            current = work.get(Task, task_id)
            result = build_result(work, current, video_url, plan, boost_enabled=boost_enabled,
                                  boost_quantity=boost_quantity, reason=reason)
            journal = work.query(PublicationAttempt).filter(PublicationAttempt.task_id == task_id,
                PublicationAttempt.video_url == video_url, PublicationAttempt.aweme_id.isnot(None)).first()
            result.aweme_id = (journal.aweme_id if journal else None) or extract_tiktok_media_id(video_url)
            result.resolved_video_url = video_url if extract_tiktok_media_id(video_url) else (
                f"https://www.tiktok.com/@_/video/{result.aweme_id}" if result.aweme_id else None)
    result.elapsed_seconds = time.monotonic() - started
    if (boost_enabled and not stop_requested() and result.error not in {
            "quantity_traffic_budget_exhausted", "quantity_traffic_accounting_unavailable",
            "quantity_time_budget_exhausted", "stopped_by_user"}
            and any(_reply_outcome_counts_for_likes(outcome) for outcome in result.replies)):
        await _maybe_order_likes(result, video_url=result.resolved_video_url or video_url, task_id=task_id,
            boost_enabled=boost_enabled, boost_quantity=boost_quantity,
            target_mode=(task.settings or {}).get("boost_likes_target_mode", "single_top"))
    else:
        result.boost_enabled = False
    return result
