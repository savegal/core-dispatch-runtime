"""One HTTP replacement receipt, without composer/search/behavioral UI paths."""
import asyncio
import logging
import time

from executor.http.execution_policy import http_first_enabled, valid_comment_id
from executor.http.retry_policy import RetryPolicy
from executor.http.signature_runtime import wait_for_runtime_signer_ready
from services.proxy_normalizer import ProxyRouteError

logger = logging.getLogger(__name__)


async def _publication_readiness_error(page, aweme_id: str, *, correlation_id: str,
                                       require_comment_readiness: bool = False,
                                       runtime_profile=None) -> str | None:
    """Reuse warmup evidence, or perform a bounded read before the write."""
    from executor.http.comment_discovery import (
        COMMENT_BOOTSTRAP_TIMEOUT_SECONDS, comment_token_handshake_ready, fetch_comment_page, signed_first_comment_page,
    )
    from executor.http.page_runtime import page_document_key
    document = page_document_key(page)
    if require_comment_readiness and (
            signed_first_comment_page(page, aweme_id) is not None or comment_token_handshake_ready(page, aweme_id)):
        # The HTTP client still checks the signer at the dispatch boundary.
        return None
    signed = await wait_for_runtime_signer_ready(page, timeout_ms=200, require_signed_dispatch=True)
    if page_document_key(page) != document:
        return "url_drift_to_other_video"
    if signed and not require_comment_readiness:
        return None
    if require_comment_readiness and signed_first_comment_page(page, aweme_id) is not None:
        return None
    payload = None
    try:
        direct = require_comment_readiness and (signed or getattr(page, "_warmup_signer_ready", False) is True)
        options = {"bootstrap": True} if direct else {"retry": False} if require_comment_readiness else {}
        if require_comment_readiness and runtime_profile is not None:
            options["runtime_profile"] = runtime_profile
        payload = await asyncio.wait_for(fetch_comment_page(page, aweme_id, count=20, **options),
                                         timeout=COMMENT_BOOTSTRAP_TIMEOUT_SECONDS if direct else 6.0)
        read_error = (getattr(page, "_comment_list_http_meta", None) or {}).get("reason") or "no_result"
    except Exception as exc:
        read_error = "timeout" if isinstance(exc, TimeoutError) else f"request_exception:{type(exc).__name__}"
        read_meta = getattr(page, "_comment_list_http_meta", None)
        if read_error == "timeout" and isinstance(read_meta, dict) and read_meta.get("reason") == "not_started":
            read_meta["reason"] = "timeout"
        logger.info("[VISIBILITY_HTTP] %s: signed GET bootstrap unavailable (%s)",
                    correlation_id, type(exc).__name__)
    if page_document_key(page) != document:
        return "url_drift_to_other_video"
    if require_comment_readiness and not (isinstance(payload, dict) and isinstance(payload.get("comments"), list)):
        return (None if signed_first_comment_page(page, aweme_id) is not None or comment_token_handshake_ready(page, aweme_id)
                else f"comment_readiness_unconfirmed:{read_error}")
    if not signed and not await wait_for_runtime_signer_ready(page, timeout_ms=700, require_signed_dispatch=True):
        return "runtime_signer_not_ready"
    if page_document_key(page) != document:
        return "url_drift_to_other_video"
    if require_comment_readiness and payload is not None:
        from executor.http.comment_discovery import cache_first_comment_page
        cache_first_comment_page(page, aweme_id, payload, signed=True)
    return None


async def publish_visibility_http(db, account, video_url, text, *, correlation_id,
                                  parent=None, variant_index=1, on_published=None, progress=None, stop_requested=None,
                                  require_comment_readiness=True, recover_empty_cid=False):
    from executor import user_schema as schema
    from executor.http.comment_discovery import comment_token_handshake_ready
    scope = schema.REPLY_SCOPE if parent is not None else schema.MAIN_SCOPE
    stop_requested = stop_requested or (lambda: False)
    progress = progress if progress is not None else {}
    progress["publication_call_started"] = False
    if stop_requested():
        return None, "stopped_by_user"
    if not http_first_enabled(schema._settings):
        return None, "http_disabled"
    if parent is not None and not valid_comment_id(parent.comment_id):
        return None, "invalid_reply_target"
    context = page = None
    detach = None
    stage_started = time.monotonic()
    timings = {}

    def finish_stage(stage):
        nonlocal stage_started
        now = time.monotonic()
        timings[stage] = round((now - stage_started) * 1000)
        stage_started = now

    try:
        deny = await schema._preflight_cookie_http_gate(db, account, correlation_id=correlation_id, phase="visibility_http")
        finish_stage("passport")
        if deny:
            return None, deny
        if db is not None and db.in_transaction():
            db.commit()
        try:
            context, page = await schema.create_page_for_account(account)
        except ProxyRouteError as exc:
            return None, str(exc)
        finally:
            finish_stage("browser_acquire")
        page._publication_attempted = False
        detach = schema.track_publication_requests(page, progress)
        ready, deny = await schema._ensure_account_login_ready(
            db, page, account, video_url,
            timeout_ms=int(schema._settings.PLAYWRIGHT_TIMEOUT * (2 if account.proxy else 1)),
            correlation_id=correlation_id,
        )
        finish_stage("navigation_auth")
        if not ready:
            return None, deny or "http_auth_unconfirmed"
        if db is not None and db.in_transaction():
            db.commit()
        warmup_options = {}
        if parent is None and recover_empty_cid and require_comment_readiness:
            from services.account_service import _publish_cookie_risk
            from services.cookie_validator import get_cookies_for_account, parse_cookies_to_dict
            if not _publish_cookie_risk(parse_cookies_to_dict(get_cookies_for_account(account))):
                warmup_options["allow_verified_signer_without_list"] = True
        session = await schema._prepare_user_schema_http_session(
            page, account, video_url, scope=scope, correlation_id=correlation_id, db=db,
            require_comment_readiness=require_comment_readiness, **warmup_options,
        )
        finish_stage("warmup")
        if session is None:
            return None, schema._http_preparation_error(page, scope)
        if not schema.is_on_expected_media_url(getattr(page, "url", None), video_url):
            return None, "url_drift_to_other_video"
        expected_id = schema.extract_tiktok_media_id(video_url) or schema.extract_tiktok_media_id(getattr(page, "url", None))
        if not expected_id or str(session.aweme_id) != str(expected_id):
            return None, "http_session_media_mismatch"
        if db is not None and db.in_transaction():
            db.commit()  # No checked-out SQL connection while the browser waits/posts.
        if stop_requested():
            return None, "stopped_by_user"
        denial = None
        if getattr(session, "comment_list_deferred", False) is not True:
            denial = await _publication_readiness_error(page, str(expected_id), correlation_id=correlation_id,
                                                        require_comment_readiness=require_comment_readiness,
                                                        runtime_profile=getattr(session.client, "runtime_profile", None))
        finish_stage("comment_readiness")
        if denial:
            return None, denial
        if require_comment_readiness and comment_token_handshake_ready(page, expected_id):
            session.client.allow_comment_token_handshake = True
            logger.info("[VISIBILITY_HTTP] %s: signed token refresh observed; comment-list visibility remains unchecked",
                        correlation_id)
        # A POST with a lost/empty response may already exist on the platform.
        # Only the visibility controller may authorize another publication.
        session.client.retry_policy = RetryPolicy(max_retries=0)
        if recover_empty_cid:
            uid = schema._get_cached_http_preflight_user_id(account) or ""
            if uid.isascii() and uid.isdigit() and uid.strip("0"):
                session.client.empty_post_recovery_uid = uid
        if stop_requested():
            return None, "stopped_by_user"
        progress["publication_call_started"] = True
        if parent is None:
            published = await schema._publish_main_http_first_if_eligible(session, text, correlation_id=correlation_id)
        else:
            published = await schema._publish_reply_http_first_if_eligible(
                session, text, parent_comment_id=str(parent.comment_id).strip(), correlation_id=correlation_id,
                variant_index=variant_index,
            )
        finish_stage("publication")
        if published is None:
            return None, session.disabled_reason or "http_publish_not_eligible"
        attempted = bool(published.network_request_attempted or published.network_captured)
        progress["publication_attempted"] = attempted or bool(progress.get("publication_attempted"))
        if published.status != "success" or not published.network_captured or not valid_comment_id(published.comment_id):
            if attempted:
                recorder = schema.record_reply_attempt if parent is not None else schema.record_main_comment_attempt
                recorder(db, account.id)
                meta = getattr(session.client, "last_request_meta", None) or {}
                if (db is not None and published.status == "submitted_unconfirmed"
                        and meta.get("unconfirmed_reason") == "empty_body"):
                    from services.account_service import mark_runtime_cooldown
                    try:
                        mark_runtime_cooldown(db, account.id, "submitted_unconfirmed:empty_body",
                                              cooldown_minutes=30, category="publication_empty_response", scope=scope)
                    except Exception:
                        logger.warning("Could not save empty-response cooldown for account %s", account.id, exc_info=True)
            return None, published.error_reason or "http_publish_unconfirmed"
        if parent is None:
            author = schema._get_cached_http_preflight_username(account)
            author = author or (getattr(session.client, "last_request_meta", {}) or {}).get("comment_author")
            receipt = schema.PublishedMain(
                text=text, comment_id=str(published.comment_id).strip(), network_confirmed=True,
                local_confirmed=False, method=published.dom_strategy, account_id=account.id,
                tiktok_username=author, variant_index=variant_index,
                author_uid=schema._publication_author_identity(account, session)["author_uid"],
            )
        else:
            parent_id = str(parent.comment_id).strip()
            receipt = schema.ReplyOutcome(
                text=text, variant_index=variant_index, matched_main_text=parent.text,
                parent_comment_id=parent_id, success=True, method=published.dom_strategy,
                error=None, account_id=account.id, attempted=True, publication_attempted=True,
                parent_key=parent_id, requested_parent_author=parent.tiktok_username,
                matched_parent_author=parent.tiktok_username, parent_author_match_mode="known_comment_id",
                reply_comment_id=str(published.comment_id).strip(), reply_confirmation="network_comment_id",
                counts_as_submitted=True, physical_parent_key=parent.physical_parent_key or parent_id,
                parent_target_id=schema._main_logical_target_id(parent),
                **schema._publication_author_identity(account, session),
            )
        await schema.record_publication("main" if parent is None else "reply", receipt)
        if on_published is not None:
            await schema._call_publication_callback(on_published, receipt)  # Save the ID before any DB/cleanup await can fail.
        schema._register_runtime_success(db, account, scope=scope)
        await schema._persist_cookies_after_success(db, page, account, scope=scope, correlation_id=correlation_id)
        return receipt, None
    finally:
        if detach is not None:
            detach()
        try:
            await schema._safe_close(context, page, reason="visibility_http_finished", correlation_id=correlation_id)
        finally:
            if require_comment_readiness:
                finish_stage("finalize")
                logger.info("[QUANTITY_HTTP_TIMING] %s phase_ms=%s total_ms=%s",
                            correlation_id, timings, sum(timings.values()))
