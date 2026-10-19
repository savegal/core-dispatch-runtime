"""One HTTP replacement receipt, without composer/search/behavioral UI paths."""
from executor.http.execution_policy import http_first_enabled, valid_comment_id
from executor.http.retry_policy import RetryPolicy
from executor.http.signature_runtime import wait_for_runtime_signer_ready


async def publish_visibility_http(db, account, video_url, text, *, correlation_id,
                                  parent=None, variant_index=1, on_published=None, progress=None):
    from executor import user_schema as schema
    scope = schema.REPLY_SCOPE if parent is not None else schema.MAIN_SCOPE
    if not http_first_enabled(schema._settings):
        return None, "http_disabled"
    if parent is not None and not valid_comment_id(parent.comment_id):
        return None, "invalid_reply_target"
    context = page = None
    detach = None
    progress = progress if progress is not None else {}
    try:
        deny = await schema._preflight_cookie_http_gate(db, account, correlation_id=correlation_id, phase="visibility_http")
        if deny:
            return None, deny
        context, page = await schema.create_page_for_account(account)
        page._publication_attempted = False
        detach = schema.track_publication_requests(page, progress)
        ready, deny = await schema._ensure_account_login_ready(
            db, page, account, video_url,
            timeout_ms=int(schema._settings.PLAYWRIGHT_TIMEOUT * (2 if account.proxy else 1)),
            correlation_id=correlation_id,
        )
        if not ready:
            return None, deny or "http_auth_unconfirmed"
        session = await schema._prepare_user_schema_http_session(
            page, account, video_url, scope=scope, correlation_id=correlation_id, db=db,
        )
        if session is None:
            return None, schema._http_preparation_error(page, scope)
        if not schema.is_on_expected_media_url(getattr(page, "url", None), video_url):
            return None, "url_drift_to_other_video"
        expected_id = schema.extract_tiktok_media_id(video_url) or schema.extract_tiktok_media_id(getattr(page, "url", None))
        if not expected_id or str(session.aweme_id) != str(expected_id):
            return None, "http_session_media_mismatch"
        if not await wait_for_runtime_signer_ready(page, timeout_ms=1200):
            return None, "runtime_signer_not_ready"
        # A POST with a lost/empty response may already exist on the platform.
        # Only the visibility controller may authorize another publication.
        session.client.retry_policy = RetryPolicy(max_retries=0)
        if parent is None:
            published = await schema._publish_main_http_first_if_eligible(session, text, correlation_id=correlation_id)
        else:
            published = await schema._publish_reply_http_first_if_eligible(
                session, text, parent_comment_id=str(parent.comment_id).strip(), correlation_id=correlation_id,
            )
        if published is None:
            return None, session.disabled_reason or "http_publish_not_eligible"
        attempted = bool(published.network_request_attempted or published.network_captured)
        progress["publication_attempted"] = attempted or bool(progress.get("publication_attempted"))
        if published.status != "success" or not published.network_captured or not valid_comment_id(published.comment_id):
            if attempted:
                recorder = schema.record_reply_attempt if parent is not None else schema.record_main_comment_attempt
                recorder(db, account.id)
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
        if on_published is not None:
            on_published(receipt)  # Save the ID before any DB/cleanup await can fail.
        schema._register_runtime_success(db, account, scope=scope)
        await schema._persist_cookies_after_success(db, page, account, scope=scope, correlation_id=correlation_id)
        return receipt, None
    finally:
        if detach is not None:
            detach()
        await schema._safe_close(context, page, reason="visibility_http_finished", correlation_id=correlation_id)
