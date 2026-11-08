"""Short-lived metadata handoff and input activation for a single document."""
import asyncio
from copy import deepcopy
import time
from executor.browser_reads import await_browser_read
from services.tiktok_urls import media_id_from_url, parse_tiktok_url


def target_page_matches(page, aweme_id):
    """Canonical URLs and query changes may still refer to the same video."""
    closed = getattr(page, "is_closed", None)
    return not (callable(closed) and closed() is True) and target_url_matches(getattr(page, "url", ""), aweme_id)


def target_url_matches(url, aweme_id):
    return media_id_from_url(url) == str(aweme_id)


def _media_document_key(url):
    media_id = media_id_from_url(url)
    if media_id:
        parsed = parse_tiktok_url(url)
        return parsed.scheme, parsed.hostname, media_id
    return None


def page_document_key(page):
    """Bind a probe to one document and media identity, allowing canonical URLs."""
    url = str(getattr(page, "url", "") or "")
    return document_generation(page), _media_document_key(url) or url


def document_generation(page):
    if not getattr(page, "_http_document_tracking", False):
        page._http_document_tracking = True
        page._http_document_generation = 0
        page._http_document_url = getattr(page, "url", "")
        page._http_document_navigation_pending = False

        def invalidate(*_):
            page._http_document_generation += 1
            page._http_input_activated = None
            page._http_document_dom_ready = None
            page._http_clear_captcha_probe = None
            page._http_auth_verified_at = None
            page._http_signer_state = {}
            page._http_signer_probe = None
            page._http_runtime_profile_probe = None
            page._cached_first_comment_page = None
            page._cached_first_comment_aweme_id = None
            page._signed_comment_page_document = None
            page._signed_comment_page_observed_at = None
            page._http_confirmed_signed_get = None

        def dom_ready(*_):
            # Completion of the document already being observed is not a new
            # document. Reloads invalidate on their main-frame request instead.
            page._http_document_dom_ready = (page._http_document_generation, time.monotonic())
            page._http_document_navigation_pending = False

        def requested(request):
            if (getattr(request, "is_navigation_request", lambda: False)()
                    and getattr(request, "frame", None) == getattr(page, "main_frame", None)):
                page._http_document_navigation_pending = True
                invalidate()

        def navigated(frame):
            if frame == getattr(page, "main_frame", None):
                previous = page._http_document_url
                current = getattr(page, "url", "")
                page._http_document_url = current
                # Same-document history updates must not invalidate readiness.
                # Full reloads are observed via main-frame document requests.
                key = _media_document_key(previous)
                # A real document request also invalidates at commit: probes
                # started against the old document during navigation must not
                # transfer their authentication/signing proof to the new one.
                # Warmup can explicitly rebase after rechecking the same video.
                if (page._http_document_navigation_pending
                        or key is None or key != _media_document_key(current)):
                    invalidate()
                page._http_document_navigation_pending = False

        if callable(getattr(page, "on", None)):
            page.on("request", requested)
            page.on("framenavigated", navigated)
            page.on("domcontentloaded", dom_ready)
            page.on("close", invalidate)
    return page._http_document_generation


def prepared_runtime_profile(page, profile):
    """Reuse only an explicit initialization snapshot, never cache by URL alone."""
    if not isinstance(profile, dict) or profile.get("_snapshot_reusable") is not True:
        return None
    captured = profile.get("_captured_at")
    if (type(captured) not in (int, float) or not 0 <= time.monotonic() - captured <= 10.0
            or profile.get("current_url") != page.url
            or profile.get("_document_generation") != document_generation(page)):
        return None
    context = getattr(page, "context", None)
    account = getattr(page, "_pool_account_obj", None) or getattr(context, "_pool_account_obj", None)
    if account is not None:
        from executor.device_profiles import _account_seed_key
        if profile.get("device_seed") != _account_seed_key(account):
            return None
    return deepcopy(profile)


async def read_warmup_runtime_profile(page, *, allow_extension=False, timeout_seconds=5.0, grace_seconds=4.0):
    """Allow one slow cold-start read to finish without issuing another RPC."""
    from executor.http.playwright_http_client import _extract_runtime_profile
    generation = document_generation(page)
    probe = getattr(page, "_http_runtime_profile_probe", None)
    if not (isinstance(probe, tuple) and len(probe) == 2 and probe[0] == generation
            and isinstance(probe[1], asyncio.Future)):
        task = asyncio.create_task(_extract_runtime_profile(page))
        task.add_done_callback(lambda done: None if done.cancelled() else done.exception())
        probe = (generation, task)
        page._http_runtime_profile_probe = probe
    task = probe[1]
    extended = False
    try:
        try:
            profile = await await_browser_read(task, timeout=timeout_seconds)
        except asyncio.TimeoutError:
            if task.done():
                profile = task.result()
            elif allow_extension and document_generation(page) == generation:
                extended = True
                profile = await await_browser_read(task, timeout=grace_seconds)
            else:
                raise
        return profile, extended
    finally:
        if task.done() and getattr(page, "_http_runtime_profile_probe", None) is probe:
            page._http_runtime_profile_probe = None


async def activate_page_input(page):
    """Send one browser mouse move per document; failures do not fake events."""
    key = (getattr(page, "url", None), document_generation(page))
    if getattr(page, "_http_input_activated", None) == key:
        return True
    move = getattr(getattr(page, "mouse", None), "move", None)
    if not callable(move):
        return False
    try:
        await await_browser_read(lambda: move(120, 160), timeout=0.5)
    except Exception:
        return False
    page._http_input_activated = key
    return True
