import inspect

import pytest

from executor.session_bootstrap import (
    BootstrapResult,
    QuickLoginCheck,
    extract_tiktok_username,
    get_browser_login_state,
    is_tiktok_media_page_url,
    quick_video_login_check,
)


@pytest.mark.unit
def test_bootstrap_publish_requires_login_cookie_not_only_mstoken():
    result = BootstrapResult(
        status="ready",
        cookies={"msToken": "ms", "ttwid": "wid", "tt_csrf_token": "csrf"},
        auth_present=["msToken"],
    )

    assert result.can_attempt_publish is False


@pytest.mark.unit
def test_bootstrap_publish_allows_login_cookie_with_ttwid():
    result = BootstrapResult(
        status="ready",
        cookies={"sid_guard": "guard", "msToken": "ms", "ttwid": "wid"},
        auth_present=["sid_guard", "msToken"],
    )

    assert result.can_attempt_publish is True


@pytest.mark.unit
@pytest.mark.asyncio
async def test_get_browser_login_state_returns_logged_out_from_visible_login_ui():
    class _Page:
        async def evaluate(self, _script):
            return {"state": "logged_out", "loginCount": 1, "loggedInCount": 0}

    state = await get_browser_login_state(_Page())

    assert state["state"] == "logged_out"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_get_browser_login_state_returns_unknown_when_dom_check_fails():
    class _Page:
        async def evaluate(self, _script):
            raise RuntimeError("page closed")

    state = await get_browser_login_state(_Page())

    assert state["state"] == "unknown"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_quick_video_login_check_dead_when_redirected_away_from_video():
    class _Page:
        url = "https://www.tiktok.com/login"

    check = await quick_video_login_check(_Page())
    assert isinstance(check, QuickLoginCheck)
    assert check.state == "dead"
    assert check.on_video is False
    assert check.reason == "redirected_to_login"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_quick_video_login_check_unknown_when_shortlink_not_resolved():
    class _Page:
        url = "https://vt.tiktok.com/zsxnkgd5j/"

    check = await quick_video_login_check(_Page())
    assert isinstance(check, QuickLoginCheck)
    assert check.state == "unknown"
    assert check.on_video is False
    assert check.reason == "not_on_video_page"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_quick_video_login_check_ready_when_logged_in_marker_visible():
    class _Page:
        url = "https://www.tiktok.com/@user/video/123"

        async def evaluate(self, _script):
            return {"strongLoggedIn": True, "loginBtn": False}

    check = await quick_video_login_check(_Page())
    assert check.state == "ready"
    assert check.has_logged_in_marker is True
    assert check.has_login_button is False


@pytest.mark.unit
@pytest.mark.asyncio
async def test_quick_video_login_check_keeps_backward_compat_logged_in_key():
    class _Page:
        url = "https://www.tiktok.com/@user/video/123"

        async def evaluate(self, _script):
            return {"loggedIn": True, "loginBtn": False}

    check = await quick_video_login_check(_Page())
    assert check.state == "ready"
    assert check.has_logged_in_marker is True


@pytest.mark.unit
@pytest.mark.asyncio
async def test_quick_video_login_check_dead_when_login_button_visible():
    class _Page:
        url = "https://www.tiktok.com/@user/video/123"

        async def evaluate(self, _script):
            return {"loggedIn": False, "loginBtn": True}

    check = await quick_video_login_check(_Page())
    assert check.state == "dead"
    assert check.has_login_button is True


@pytest.mark.unit
@pytest.mark.asyncio
async def test_quick_video_login_check_unknown_when_video_page_has_no_login_button_yet():
    """URL на /video/, login btn нет, но weak UI отрендерился → ready (uncomfirmed)."""
    class _Page:
        url = "https://www.tiktok.com/@user/video/123"

        async def evaluate(self, _script):
            return {"strongLoggedIn": False, "loginBtn": False, "weakUiPresent": True}

    check = await quick_video_login_check(_Page())
    assert check.state == "unknown"
    assert check.reason.startswith("on_video_no_login_btn")


@pytest.mark.unit
@pytest.mark.asyncio
async def test_quick_video_login_check_accepts_photo_posts_as_media_pages():
    class _Page:
        url = "https://www.tiktok.com/@user/photo/7639468151566011680"

        async def evaluate(self, _script):
            return {"strongLoggedIn": False, "loginBtn": False, "weakUiPresent": True}

    check = await quick_video_login_check(_Page())
    assert check.state == "unknown"
    assert check.on_video is True
    assert check.reason.startswith("on_video_no_login_btn")


@pytest.mark.unit
def test_is_tiktok_media_page_url_accepts_video_and_photo_paths():
    assert is_tiktok_media_page_url("https://www.tiktok.com/@u/video/123") is True
    assert is_tiktok_media_page_url("https://www.tiktok.com/@u/photo/123") is True
    assert is_tiktok_media_page_url("https://www.tiktok.com/@u") is False


@pytest.mark.unit
@pytest.mark.asyncio
async def test_quick_video_login_check_empty_dom_when_no_ui_markers_on_video():
    """URL на /video/, но TikTok вообще не отрендерил ни weak, ни strong маркеры —
    признак ограниченного рендеринга. quick_check возвращает ready с особым reason,
    а probe_cookie_health поверх делает retry + ambiguous."""
    class _Page:
        url = "https://www.tiktok.com/@user/video/123"

        async def evaluate(self, _script):
            return {"strongLoggedIn": False, "loginBtn": False, "weakUiPresent": False}

    check = await quick_video_login_check(_Page())
    assert check.state == "unknown"
    assert check.reason == "on_video_empty_dom"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_quick_video_login_check_unknown_when_probe_fails_on_video():
    class _Page:
        url = "https://www.tiktok.com/@user/video/123"

        async def evaluate(self, _script):
            raise RuntimeError("page closed")

    check = await quick_video_login_check(_Page())
    assert check.state == "unknown"
    assert check.reason.startswith("probe_failed:")


@pytest.mark.unit
@pytest.mark.asyncio
async def test_extract_tiktok_username_from_state_uniqueid():
    class _Page:
        async def evaluate(self, _script):
            return "real_tiktok_user"

    assert await extract_tiktok_username(_Page()) == "real_tiktok_user"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_extract_tiktok_username_returns_none_when_no_data():
    class _Page:
        async def evaluate(self, _script):
            return None

    assert await extract_tiktok_username(_Page()) is None


@pytest.mark.unit
@pytest.mark.asyncio
async def test_extract_tiktok_username_strips_leading_at_sign():
    class _Page:
        async def evaluate(self, _script):
            return "@some_user"

    assert await extract_tiktok_username(_Page()) == "some_user"


@pytest.mark.unit
def test_bootstrap_result_carries_tiktok_username_field():
    res = BootstrapResult(status="ready", tiktok_username="erjowkaaa")
    assert res.tiktok_username == "erjowkaaa"


@pytest.mark.unit
def test_login_state_ignores_deep_cookie_banner_login_context():
    source = inspect.getsource(get_browser_login_state)

    assert "depth < 20" in source
    assert "\\u9690\\u79c1" in source
    assert "'sign in'" in source
    assert "'iniciar sesion'" in source
    assert "'se connecter'" in source
    assert "'anmelden'" in source
    assert "'entrar'" in source
    assert "'accedi'" in source
    assert "'zaloguj'" in source
    assert "\\u0432\\u043e\\u0439\\u0442\\u0438" in source
    assert "\\u767b\\u5f55" in source
    assert "\\u30ed\\u30b0\\u30a4\\u30f3" in source
    assert "\\ub85c\\uadf8\\uc778" in source
    assert "text === 'activity'" not in source
    assert '[data-e2e="nav-activity"]' not in source
    assert '[href*="/login" i]' in source
    assert "startsWith(needle + ' ')" in source
    assert '[aria-label*="Profile" i][role="button"]' not in source
    assert "&& !stateIsLoginFalse" in source
    assert "const hasVisibleLoginUi" in source
    assert source.find("if (hasVisibleLoginUi) state = 'logged_out'") < source.find(
        "else if ((stateIsLoginTrue || loggedInCount > 0) && !stateIsLoginFalse)"
    )


@pytest.mark.unit
def test_quick_video_login_check_downgrades_message_icons_from_strong_login():
    source = inspect.getsource(quick_video_login_check)

    downgrade_block = source[source.find("if (strongMarkerFound && ["):source.find("if (!strongLoggedIn)")]
    assert '[data-e2e="inbox-icon"]' in downgrade_block
    assert '[data-e2e="message-icon"]' in downgrade_block
    assert 'a[href="/messages"]' in downgrade_block
