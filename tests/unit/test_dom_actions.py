import pytest
import asyncio
import inspect
import time

import executor.dom_actions as dom_actions


@pytest.mark.unit
@pytest.mark.asyncio
async def test_dismiss_cookie_banners_runs_targeted_cookie_cleanup(monkeypatch):
    calls = {"evaluate": 0, "slept": 0}

    class _Page:
        async def evaluate(self, script):
            calls["evaluate"] += 1
            assert "allow cookies" in script.lower()
            return {"count": 1, "action": "cookie-accept-clicked"}

    async def _fake_sleep(_seconds):
        calls["slept"] += 1

    monkeypatch.setattr(dom_actions.asyncio, "sleep", _fake_sleep)

    assert await dom_actions.dismiss_cookie_banners(_Page()) == 1
    assert calls == {"evaluate": 1, "slept": 1}


@pytest.mark.unit
@pytest.mark.asyncio
async def test_find_comment_input_smart_hard_limits_initial_open(monkeypatch):
    async def _slow_open(*_args, **_kwargs):
        await dom_actions.asyncio.sleep(1.0)
        return False

    async def _fake_cookie(_page):
        raise AssertionError("cookie cleanup should not run after opener consumes the budget")

    monkeypatch.setattr(dom_actions, "open_comment_section", _slow_open)
    monkeypatch.setattr(dom_actions, "dismiss_cookie_banners", _fake_cookie)

    started = time.monotonic()
    input_handle, result = await dom_actions.find_comment_input_smart(
        object(),
        max_total_seconds=0.05,
    )

    assert input_handle is None
    assert result.success is False
    assert result.reason == "comment_input_open_deadline"
    assert time.monotonic() - started < 0.5


@pytest.mark.unit
@pytest.mark.asyncio
async def test_find_comment_input_smart_continues_after_initial_open_timeout(monkeypatch):
    calls = {"open": 0, "cookie": 0, "evaluate": [], "query": []}

    class _Handle:
        pass

    class _Page:
        async def evaluate(self, script):
            calls["evaluate"].append(script)
            if script == dom_actions._JS_DIAGNOSE_PAGE:
                return {
                    "pathname": "/@u/video/1",
                    "editableCount": 0,
                    "textareaCount": 0,
                    "dataE2eCommentElements": [],
                    "commentTriggerCandidates": [],
                    "bodyHeight": 1200,
                }
            if script == dom_actions._JS_FIND_COMMENT_INPUT:
                return {"found": True, "strategy": "js-probe"}
            return {}

        async def query_selector(self, selector):
            calls["query"].append(selector)
            if selector == '[data-smart-id="comment-input"]':
                return _Handle()
            return None

    async def _timeout_open(*_args, **_kwargs):
        calls["open"] += 1
        raise dom_actions.asyncio.TimeoutError("initial-open")

    async def _fake_cookie(_page):
        calls["cookie"] += 1
        return 0

    monkeypatch.setattr(dom_actions, "open_comment_section", _timeout_open)
    monkeypatch.setattr(dom_actions, "dismiss_cookie_banners", _fake_cookie)

    input_handle, result = await dom_actions.find_comment_input_smart(
        _Page(),
        max_total_seconds=1.5,
    )

    assert input_handle is not None
    assert result.success is True
    assert result.strategy == "js-probe"
    assert calls["open"] == 1
    assert calls["cookie"] == 0


@pytest.mark.unit
@pytest.mark.asyncio
async def test_find_comment_input_smart_treats_cookie_cleanup_timeout_as_best_effort(monkeypatch):
    calls = {"cookie": 0, "probe": 0}

    class _NullHandle:
        def as_element(self):
            return None

    class _Page:
        async def query_selector(self, _selector):
            return None

        async def evaluate_handle(self, _script):
            return _NullHandle()

        async def evaluate(self, script, *args):
            if script == "el => el === null":
                return True
            if script == dom_actions._JS_FIND_COMMENT_INPUT:
                calls["probe"] += 1
                return {"found": False, "reason": "not_found"}
            if script == dom_actions._JS_DIAGNOSE_PAGE:
                return {
                    "pathname": "/@u/video/1",
                    "editableCount": 0,
                    "textareaCount": 0,
                    "dataE2eCommentElements": [],
                    "commentTriggerCandidates": [],
                    "bodyHeight": 1200,
                }
            return {}

    async def _unexpected_open(*_args, **_kwargs):
        raise AssertionError("skip_initial_open should not reopen comments")

    async def _hung_cookie(_page):
        calls["cookie"] += 1
        await asyncio.Event().wait()

    monkeypatch.setattr(dom_actions, "open_comment_section", _unexpected_open)
    monkeypatch.setattr(dom_actions, "dismiss_cookie_banners", _hung_cookie)

    input_handle, result = await dom_actions.find_comment_input_smart(
        _Page(),
        max_total_seconds=1.2,
        aggressive=True,
        skip_initial_open=True,
    )

    assert input_handle is None
    assert result.success is False
    assert result.reason != "comment_input_cookie_cleanup_deadline"
    assert calls["cookie"] >= 1
    assert calls["probe"] > 0


@pytest.mark.unit
@pytest.mark.asyncio
async def test_find_comment_input_smart_skip_initial_open_uses_existing_surface(monkeypatch):
    calls = {"open": 0, "cookie": 0}

    class _Handle:
        pass

    class _Page:
        async def evaluate(self, script):
            if script == dom_actions._JS_DIAGNOSE_PAGE:
                return {
                    "pathname": "/@u/video/1",
                    "editableCount": 1,
                    "textareaCount": 0,
                    "dataE2eCommentElements": [],
                    "commentTriggerCandidates": [],
                    "bodyHeight": 1200,
                }
            if script == dom_actions._JS_FIND_COMMENT_INPUT:
                return {"found": True, "strategy": "existing-surface-js"}
            return {}

        async def query_selector(self, selector):
            if selector == '[data-smart-id="comment-input"]':
                return _Handle()
            return None

    async def _unexpected_open(*_args, **_kwargs):
        calls["open"] += 1
        raise AssertionError("initial opener should be skipped when surface is already ready")

    async def _fake_cookie(_page):
        calls["cookie"] += 1
        return 0

    monkeypatch.setattr(dom_actions, "open_comment_section", _unexpected_open)
    monkeypatch.setattr(dom_actions, "dismiss_cookie_banners", _fake_cookie)

    input_handle, result = await dom_actions.find_comment_input_smart(
        _Page(),
        max_total_seconds=1.5,
        skip_initial_open=True,
    )

    assert input_handle is not None
    assert result.success is True
    assert result.strategy == "trusted-smart-id-input"
    assert calls == {"open": 0, "cookie": 0}


@pytest.mark.unit
@pytest.mark.asyncio
async def test_find_comment_input_smart_uses_trusted_wrapper_before_broad_probes(monkeypatch):
    calls = {"open": 0, "cookie": 0, "handle": 0, "query": []}

    class _Handle:
        async def bounding_box(self):
            return {"width": 200, "height": 0}

    class _Wrapper:
        async def query_selector(self, _selector):
            return _Handle()

        async def click(self, **_kwargs):
            return None

    class _Page:
        async def evaluate(self, script, *args):
            if script.startswith("el => el.setAttribute"):
                return None
            raise AssertionError("already-open trusted wrapper should avoid broad JS")

        async def query_selector(self, selector):
            calls["query"].append(selector)
            if selector == '[data-smart-id="comment-input"]':
                return None
            if selector.startswith('[data-e2e="comment-input"]'):
                return _Wrapper()
            return None

        async def evaluate_handle(self, _script):
            calls["handle"] += 1
            raise AssertionError("trusted wrapper should return before broad handle scan")

    async def _unexpected_open(*_args, **_kwargs):
        calls["open"] += 1
        raise AssertionError("already-open legacy fast path must not reopen comments")

    async def _unexpected_cookie(_page):
        calls["cookie"] += 1
        raise AssertionError("legacy fast path should return before cookie cleanup")

    monkeypatch.setattr(dom_actions, "open_comment_section", _unexpected_open)
    monkeypatch.setattr(dom_actions, "dismiss_cookie_banners", _unexpected_cookie)

    input_handle, result = await dom_actions.find_comment_input_smart(
        _Page(),
        max_total_seconds=2.0,
        aggressive=True,
        skip_initial_open=True,
    )

    assert input_handle is not None
    assert result.success is True
    assert result.strategy == "trusted-comment-input-inner"
    assert calls["open"] == 0
    assert calls["cookie"] == 0
    assert calls["handle"] == 0


@pytest.mark.unit
@pytest.mark.asyncio
async def test_find_comment_input_smart_skip_initial_open_uses_direct_resolver_before_unified(monkeypatch):
    calls = {"open": 0, "cookie": 0, "handle": 0}

    class _Handle:
        pass

    class _Page:
        async def evaluate_handle(self, script):
            calls["handle"] += 1
            assert 'data-smart-id="comment-input"' in script
            return _Handle()

        async def evaluate(self, script, *args):
            if script == "el => el === null":
                return False
            return {}

    async def _unexpected_open(*_args, **_kwargs):
        calls["open"] += 1
        raise AssertionError("already-open input fast path must not reopen comments")

    async def _unexpected_cookie(_page):
        calls["cookie"] += 1
        raise AssertionError("direct input fast path should return before cookie cleanup")

    monkeypatch.setattr(dom_actions, "open_comment_section", _unexpected_open)
    monkeypatch.setattr(dom_actions, "dismiss_cookie_banners", _unexpected_cookie)

    input_handle, result = await dom_actions.find_comment_input_smart(
        _Page(),
        max_total_seconds=1.5,
        skip_initial_open=True,
    )

    assert input_handle is not None
    assert result.success is True
    assert result.strategy == "direct-existing-input"
    assert calls == {"open": 0, "cookie": 0, "handle": 1}


@pytest.mark.unit
@pytest.mark.asyncio
async def test_find_comment_input_smart_surface_ready_fast_resolvers_get_real_budgets(monkeypatch):
    calls = {"timeouts": []}

    class _Handle:
        pass

    class _Page:
        async def evaluate_handle(self, _script):
            return _Handle()

        async def evaluate(self, script, *args):
            if script == "el => el === null":
                return False
            return {}

    async def _fake_wait_for(coro, *, timeout):
        calls["timeouts"].append(timeout)
        return await coro

    async def _unexpected_open(*_args, **_kwargs):
        raise AssertionError("already-ready surface should not reopen comments")

    async def _unexpected_cookie(_page):
        raise AssertionError("already-ready surface should not clean cookies before first grab")

    monkeypatch.setattr(dom_actions.asyncio, "wait_for", _fake_wait_for)
    monkeypatch.setattr(dom_actions, "open_comment_section", _unexpected_open)
    monkeypatch.setattr(dom_actions, "dismiss_cookie_banners", _unexpected_cookie)

    input_handle, result = await dom_actions.find_comment_input_smart(
        _Page(),
        max_total_seconds=6.0,
        aggressive=True,
        skip_initial_open=True,
    )

    assert input_handle is not None
    assert result.success is True
    assert result.strategy == "direct-existing-input"
    assert calls["timeouts"]
    # Index 0 is the new cheap one-shot surface probe (runs first); the
    # trusted-existing and surface-ready-direct probes keep their real budgets.
    assert 1.0 <= calls["timeouts"][0] <= 1.2
    assert 0.3 <= calls["timeouts"][1] <= 0.4
    assert 0.5 <= calls["timeouts"][2] <= 0.8


@pytest.mark.unit
@pytest.mark.asyncio
async def test_find_comment_input_smart_main_aggressive_prefers_old_js_handle(monkeypatch):
    calls = {"handle": 0, "surface": 0}

    class _Handle:
        async def evaluate(self, _script):
            return "main-aggressive-old-js"

    class _Page:
        async def evaluate_handle(self, script):
            calls["handle"] += 1
            assert script == dom_actions._JS_FIND_COMMENT_INPUT_HANDLE
            return _Handle()

        async def evaluate(self, script, *args):
            if script == dom_actions._JS_COMMENT_SURFACE_STATE:
                calls["surface"] += 1
                raise AssertionError("main aggressive path should avoid surface-state first")
            return {}

    async def _unexpected_open(*_args, **_kwargs):
        raise AssertionError("main aggressive handle path must not reopen comments")

    async def _unexpected_cookie(_page):
        raise AssertionError("main aggressive handle path must not clean cookies first")

    monkeypatch.setattr(dom_actions, "open_comment_section", _unexpected_open)
    monkeypatch.setattr(dom_actions, "dismiss_cookie_banners", _unexpected_cookie)

    input_handle, result = await dom_actions.find_comment_input_smart(
        _Page(),
        max_total_seconds=4.0,
        aggressive=True,
        skip_initial_open=True,
        main_comment_aggressive=True,
    )

    assert input_handle is not None
    assert result.success is True
    assert result.strategy == "main-aggressive-old-js"
    assert calls == {"handle": 1, "surface": 0}


@pytest.mark.unit
@pytest.mark.asyncio
async def test_find_comment_input_smart_main_aggressive_uses_trusted_existing_before_old_js(monkeypatch):
    calls = {"query": [], "handle": 0, "surface": 0}

    class _Handle:
        async def evaluate(self, _script):
            return None

    class _Page:
        async def query_selector(self, selector):
            calls["query"].append(selector)
            if selector == '[data-smart-id="comment-input"]':
                return _Handle()
            return None

        async def evaluate_handle(self, _script):
            calls["handle"] += 1
            raise AssertionError("trusted existing input should win before old JS")

        async def evaluate(self, script, *args):
            if script == dom_actions._JS_COMMENT_SURFACE_STATE:
                calls["surface"] += 1
                raise AssertionError("main aggressive trusted path should avoid surface-state")
            return {}

    async def _unexpected_open(*_args, **_kwargs):
        raise AssertionError("main aggressive trusted path must not reopen comments")

    async def _unexpected_cookie(_page):
        raise AssertionError("main aggressive trusted path must not clean cookies first")

    monkeypatch.setattr(dom_actions, "open_comment_section", _unexpected_open)
    monkeypatch.setattr(dom_actions, "dismiss_cookie_banners", _unexpected_cookie)

    input_handle, result = await dom_actions.find_comment_input_smart(
        _Page(),
        max_total_seconds=4.0,
        aggressive=True,
        skip_initial_open=True,
        main_comment_aggressive=True,
    )

    assert input_handle is not None
    assert result.success is True
    assert result.strategy == "trusted-smart-id-input"
    assert calls["handle"] == 0
    assert calls["surface"] == 0


@pytest.mark.unit
@pytest.mark.asyncio
async def test_find_comment_input_smart_skip_initial_open_falls_back_to_unified_resolver(monkeypatch):
    calls = {"open": 0, "cookie": 0, "handle": 0, "query": 0}

    class _NullHandle:
        def as_element(self):
            return None

    class _Handle:
        pass

    class _Page:
        async def query_selector(self, _selector):
            calls["query"] += 1
            return None

        async def evaluate_handle(self, script):
            calls["handle"] += 1
            if "data-smart-input-resolver" in script:
                return _Handle()
            return _NullHandle()

        async def evaluate(self, script, *args):
            if script == "el => el === null":
                return True
            return {}

    async def _unexpected_open(*_args, **_kwargs):
        calls["open"] += 1
        raise AssertionError("already-open input path must not reopen comments")

    async def _unexpected_cookie(_page):
        calls["cookie"] += 1
        raise AssertionError("unified fallback should run before cookie cleanup")

    monkeypatch.setattr(dom_actions, "open_comment_section", _unexpected_open)
    monkeypatch.setattr(dom_actions, "dismiss_cookie_banners", _unexpected_cookie)

    input_handle, result = await dom_actions.find_comment_input_smart(
        _Page(),
        max_total_seconds=3.0,
        aggressive=True,
        skip_initial_open=True,
    )

    assert input_handle is not None
    assert result.success is True
    assert result.strategy == "unified-comment-input"
    assert calls["open"] == 0
    assert calls["cookie"] == 0
    assert calls["handle"] >= 2


@pytest.mark.unit
@pytest.mark.asyncio
async def test_find_comment_input_smart_skip_initial_open_never_retry_opens(monkeypatch):
    calls = {"open": 0, "cookie": 0}

    class _NullHandle:
        def as_element(self):
            return None

    class _Page:
        async def query_selector(self, _selector):
            return None

        async def evaluate_handle(self, _script):
            return _NullHandle()

        async def evaluate(self, script, *args):
            if script == "el => el === null":
                return True
            if script == dom_actions._JS_DIAGNOSE_PAGE:
                return {
                    "pathname": "/@u/video/1",
                    "editableCount": 0,
                    "textareaCount": 0,
                    "dataE2eCommentElements": [],
                    "commentTriggerCandidates": [],
                    "bodyHeight": 1000,
                }
            if script == dom_actions._JS_FIND_COMMENT_INPUT:
                return {"found": False, "reason": "not_found"}
            return {}

    async def _unexpected_open(*_args, **_kwargs):
        calls["open"] += 1
        raise AssertionError("already-open retry path must not reopen comments")

    async def _fake_cookie(_page):
        calls["cookie"] += 1
        return 0

    monkeypatch.setattr(dom_actions, "open_comment_section", _unexpected_open)
    monkeypatch.setattr(dom_actions, "dismiss_cookie_banners", _fake_cookie)

    input_handle, result = await dom_actions.find_comment_input_smart(
        _Page(),
        max_total_seconds=0.8,
        aggressive=True,
        skip_initial_open=True,
    )

    assert input_handle is None
    assert result.success is False
    assert calls["open"] == 0


@pytest.mark.unit
@pytest.mark.asyncio
async def test_open_comment_section_short_circuits_when_captcha_visible(monkeypatch):
    from executor import captcha_detector

    calls = {"evaluate": 0}

    class _Page:
        async def evaluate(self, _script):
            calls["evaluate"] += 1
            raise AssertionError("opener must not run while captcha is visible")

    async def _fake_detect(_page, wait_ms=0):
        return True, "slider"

    async def _fake_suppress(_page):
        return 0

    async def _fake_cookie(_page):
        return 0

    monkeypatch.setattr(captcha_detector, "detect_captcha", _fake_detect)
    monkeypatch.setattr(dom_actions, "suppress_keyboard_shortcuts_overlay", _fake_suppress)
    monkeypatch.setattr(dom_actions, "dismiss_cookie_banners", _fake_cookie)

    assert await dom_actions.open_comment_section(_Page(), require_input=True) is False
    assert calls == {"evaluate": 0}


@pytest.mark.unit
@pytest.mark.asyncio
async def test_open_comment_section_dismisses_cookie_banner_before_open(monkeypatch):
    calls = []

    class _Page:
        async def evaluate(self, _script):
            calls.append("open")
            return {"already_open": True}

    async def _fake_suppress(_page):
        calls.append("suppress")
        return 0

    async def _fake_cookie(_page):
        calls.append("cookie")
        return 1

    monkeypatch.setattr(dom_actions, "suppress_keyboard_shortcuts_overlay", _fake_suppress)
    monkeypatch.setattr(dom_actions, "dismiss_cookie_banners", _fake_cookie)

    assert await dom_actions.open_comment_section(_Page()) is True
    assert calls == ["suppress", "cookie", "open"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_open_comment_section_requires_input_when_publishing(monkeypatch):
    class _Page:
        async def evaluate(self, _script):
            return {"list_visible": True, "input_pending": True}

    async def _fake_suppress(_page):
        return 0

    async def _fake_cookie(_page):
        return 0

    monkeypatch.setattr(dom_actions, "suppress_keyboard_shortcuts_overlay", _fake_suppress)
    monkeypatch.setattr(dom_actions, "dismiss_cookie_banners", _fake_cookie)

    assert await dom_actions.open_comment_section(_Page(), max_wait=0.2, require_input=True) is False
    assert await dom_actions.open_comment_section(_Page(), max_wait=0.2, require_input=False) is True


@pytest.mark.unit
@pytest.mark.asyncio
async def test_open_comment_section_does_not_accept_already_open_without_comment_input(monkeypatch):
    class _Page:
        async def evaluate(self, script):
            if "comment_input_visible" in script:
                return {
                    "input_visible": True,
                    "comment_input_visible": False,
                    "surface_visible": True,
                }
            return {"already_open": True}

    async def _fake_suppress(_page):
        return 0

    async def _fake_cookie(_page):
        return 0

    monkeypatch.setattr(dom_actions, "suppress_keyboard_shortcuts_overlay", _fake_suppress)
    monkeypatch.setattr(dom_actions, "dismiss_cookie_banners", _fake_cookie)

    assert await dom_actions.open_comment_section(_Page(), max_wait=0.2, require_input=True) is False


@pytest.mark.unit
@pytest.mark.asyncio
async def test_open_comment_section_uses_real_mouse_click_for_trigger(monkeypatch):
    calls = {"click": None, "selector": None}

    class _Mouse:
        async def click(self, x, y):
            calls["click"] = (x, y)

    class _Page:
        mouse = _Mouse()

        async def evaluate(self, _script):
            if "comment_input_visible" in _script:
                return {
                    "input_visible": True,
                    "comment_input_visible": True,
                    "surface_visible": True,
                }
            return {"click": True, "x": 10, "y": 20, "w": 30, "h": 40}

        async def wait_for_selector(self, selector, **_kwargs):
            calls["selector"] = selector
            return object()

    async def _fake_suppress(_page):
        return 0

    async def _fake_cookie(_page):
        return 0

    monkeypatch.setattr(dom_actions, "suppress_keyboard_shortcuts_overlay", _fake_suppress)
    monkeypatch.setattr(dom_actions, "dismiss_cookie_banners", _fake_cookie)

    assert await dom_actions.open_comment_section(_Page(), require_input=True) is True
    assert calls["click"] == (25, 40)
    assert '[data-e2e="comment-list"]' not in calls["selector"]
    assert '[data-e2e="comment-input"]' in calls["selector"]


@pytest.mark.unit
def test_cookie_banner_cleanup_is_persistent_but_not_attribute_noisy():
    script = dom_actions._JS_DISMISS_COOKIE_BANNERS

    assert "depth < 20" in script
    assert "attributes: false" in script
    assert "}, 1500)" in script
    assert "\\u9690\\u79c1" in script


@pytest.mark.unit
def test_open_comment_section_searches_non_e2e_comment_buttons():
    script = dom_actions._JS_OPEN_COMMENTS

    assert "commentTriggerMarkers" in script
    assert "commentTabPatterns" in script
    assert "commentTabLabelCandidates" in script
    assert "isCommentTabLabel" in script
    assert "exactCommentTabChild" in script
    assert "comment_tab" in script
    assert "rawLabel.includes('you may like')" not in script
    assert "aria-label" in script
    assert "\\u043a\\u043e\\u043c\\u0435\\u043d\\u0442\\u0430\\u0440" in script
    assert "\\u0434\\u043e\\u0434\\u0430\\u0442\\u0438 \\u043a\\u043e\\u043c\\u0435\\u043d\\u0442\\u0430\\u0440" in script
    assert "комментарии" in script
    assert "оставить комментар" in script
    assert "button, [role=\"button\"]" in script
    assert "comment-open-trigger" in script
    assert "input_pending: true" in script


@pytest.mark.unit
def test_open_comment_section_main_fast_recognizes_cyrillic_comment_tabs():
    script = dom_actions._JS_OPEN_COMMENTS_MAIN_FAST

    assert "\\u043a\\u043e\\u043c\\u043c\\u0435\\u043d\\u0442\\u0430\\u0440" in script
    assert "\\u043a\\u043e\\u043c\\u0435\\u043d\\u0442\\u0430\\u0440" in script
    assert "main_fast" in script


@pytest.mark.unit
def test_comment_surface_state_detects_open_comment_panel_without_input():
    script = dom_actions._JS_COMMENT_SURFACE_STATE

    assert "comment_items_visible" in script
    assert "comment_input_visible" in script
    assert "commentInputLike" in script
    assert "surface_visible" in script
    assert "comment-username" in script
    assert "DivCommentObject" in script
    assert "comment-list-container" in script
    assert "scrollable" in script
    assert "scroll_height" in script
    assert "data-smart-comment-surface-opened" in script
    assert "data-smart-comment-scroll-opened" in script


@pytest.mark.unit
def test_comment_surface_state_rejects_auth_textboxes_as_comment_input():
    script = dom_actions._JS_COMMENT_SURFACE_STATE

    assert "badMarkers" in script
    assert "'log in'" in script
    assert "'email / username'" in script
    assert "badMarkers.some(marker => label.includes(marker))" in script
    assert "badMarkers.some(marker => text.includes(marker))" in script


@pytest.mark.unit
def test_comment_input_auth_filter_does_not_match_author_substrings():
    scripts = [
        dom_actions._JS_FIND_COMMENT_INPUT,
        dom_actions._JS_RESOLVE_COMMENT_INPUT_HANDLE,
        dom_actions._JS_COMMENT_SURFACE_STATE,
    ]

    for script in scripts:
        assert "'auth'" not in script
        assert "authTokenLike" in script
        assert "/(^|[^a-z])auth([^a-z]|$)/" in script


@pytest.mark.unit
def test_shortcuts_suppressor_covers_feed_navigation_onboarding():
    script = dom_actions._JS_SUPPRESS_KEYBOARD_SHORTCUTS_OVERLAY

    assert "browse your feed" in script
    assert "arrows on the right" in script
    assert "feed_navigation_guide_seen" in script


@pytest.mark.unit
def test_comment_surface_ready_requires_real_comment_input_for_publish():
    generic_textbox_surface = {
        "input_visible": True,
        "comment_input_visible": False,
    }
    safe_publish_candidate_surface = {
        "input_visible": True,
        "comment_input_visible": False,
        "publish_input_candidate_visible": True,
    }
    comment_input_surface = {
        "input_visible": True,
        "comment_input_visible": True,
    }

    assert dom_actions._comment_surface_ready(
        generic_textbox_surface,
        require_input=True,
    ) is False
    assert dom_actions._comment_surface_ready(
        safe_publish_candidate_surface,
        require_input=True,
    ) is False
    assert dom_actions._comment_surface_ready(
        safe_publish_candidate_surface,
        require_input=True,
        aggressive=True,
    ) is True
    assert dom_actions._comment_surface_ready(
        comment_input_surface,
        require_input=True,
    ) is True


@pytest.mark.unit
def test_strict_comment_surface_ready_requires_strong_comment_rows():
    weak_placeholder_surface = {
        "list_visible": True,
        "comment_items_visible": 3,
        "list_source": {"strong_items": 0},
    }
    real_comment_surface = {
        "list_visible": True,
        "comment_items_visible": 3,
        "list_source": {"strong_items": 1},
    }

    assert dom_actions._comment_surface_ready(
        weak_placeholder_surface,
        require_list_with_items=True,
    ) is False
    assert dom_actions._comment_surface_ready(
        real_comment_surface,
        require_list_with_items=True,
    ) is True


@pytest.mark.unit
def test_page_diagnostic_reports_comment_trigger_candidates():
    script = dom_actions._JS_DIAGNOSE_PAGE

    assert "commentTriggerCandidates" in script
    assert "aria-label" in script
    assert "button, [role=\"button\"]" in script


@pytest.mark.unit
def test_comment_input_guard_avoids_overbroad_verify_markers():
    script = dom_actions._JS_FIND_COMMENT_INPUT

    assert "'verify'," not in script
    assert "'robot'," not in script
    assert "'verification'," in script


@pytest.mark.unit
def test_comment_input_guard_excludes_search_inputs():
    script = dom_actions._JS_FIND_COMMENT_INPUT

    assert "looksLikeSearchInput" in script
    assert "name === 'q'" in script
    assert "'search'" in script
    assert "\\u043f\\u043e\\u0438\\u0441\\u043a" in script
    assert "&& !looksLikeSearchInput(el, raw)" in script


@pytest.mark.unit
def test_unified_comment_input_resolver_keeps_safe_guards():
    script = dom_actions._JS_RESOLVE_COMMENT_INPUT_HANDLE

    assert "looksLikeSearchOrAuthInput" in script
    assert "commentSurfaceVisible" in script
    assert "pickEditor" in script
    assert "data-smart-input-resolver" in script
    assert "'search'" in script
    assert "'captcha'" in script
    assert "'login'" in script
    assert "'password'" in script


@pytest.mark.unit
def test_reply_input_activation_guard_has_reply_mode_markers():
    script = dom_actions._JS_REPLY_INPUT_ACTIVE

    assert "reply_input_not_activated" in script
    assert "mention_prefill" in script
    assert "\\u043e\\u0442\\u0432\\u0435\\u0442" in script
    assert "\\ub2f5\\uae00" in script


@pytest.mark.unit
@pytest.mark.asyncio
async def test_open_comment_section_aggressive_accepts_safe_publish_candidate(monkeypatch):
    class _Page:
        url = "https://www.tiktok.com/@u/video/1"

        async def evaluate(self, script):
            if "publish_input_candidate_visible" in script:
                return {
                    "input_visible": True,
                    "comment_input_visible": False,
                    "publish_input_candidate_visible": True,
                    "surface_visible": True,
                }
            return {"already_open": True}

    async def _fake_suppress(_page):
        return 0

    async def _fake_cookie(_page):
        return 0

    monkeypatch.setattr(dom_actions, "suppress_keyboard_shortcuts_overlay", _fake_suppress)
    monkeypatch.setattr(dom_actions, "dismiss_cookie_banners", _fake_cookie)

    assert await dom_actions.open_comment_section(_Page(), max_wait=0.2, require_input=True) is False
    assert await dom_actions.open_comment_section(_Page(), max_wait=0.2, require_input=True, aggressive=True) is True


@pytest.mark.unit
@pytest.mark.asyncio
async def test_open_comment_section_main_aggressive_accepts_fast_existing_input(monkeypatch):
    calls = {"evaluate": []}

    class _Page:
        url = "https://www.tiktok.com/@u/video/1"

        async def evaluate(self, script):
            calls["evaluate"].append(script)
            if script == dom_actions._JS_OPEN_COMMENTS_MAIN_FAST:
                return {"already_open": True, "input_pending": True}
            if script == dom_actions._JS_COMMENT_SURFACE_STATE:
                raise AssertionError("main aggressive opener should not need surface-state confirmation")
            return {}

    async def _fake_suppress(_page):
        return 0

    async def _fake_cookie(_page):
        return 0

    monkeypatch.setattr(dom_actions, "suppress_keyboard_shortcuts_overlay", _fake_suppress)
    monkeypatch.setattr(dom_actions, "dismiss_cookie_banners", _fake_cookie)

    assert await dom_actions.open_comment_section(
        _Page(),
        max_wait=0.2,
        require_input=True,
        aggressive=True,
        main_comment_aggressive=True,
    ) is True
    assert calls["evaluate"] == [dom_actions._JS_OPEN_COMMENTS_MAIN_FAST]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_open_comment_section_main_aggressive_waits_only_comment_input_selectors(monkeypatch):
    calls = {"selectors": []}

    class _Page:
        url = "https://www.tiktok.com/@u/video/1"

        async def evaluate(self, script):
            if script == dom_actions._JS_OPEN_COMMENTS_MAIN_FAST:
                return {"input_pending": True, "list_visible": True, "main_fast": True}
            if script == dom_actions._JS_COMMENT_SURFACE_STATE:
                return {}
            return {}

        async def wait_for_selector(self, selector, **_kwargs):
            calls["selectors"].append(selector)
            raise TimeoutError("not ready")

    async def _fake_suppress(_page):
        return 0

    async def _fake_cookie(_page):
        return 0

    async def _fake_fallback(*_args, **_kwargs):
        return False

    monkeypatch.setattr(dom_actions, "suppress_keyboard_shortcuts_overlay", _fake_suppress)
    monkeypatch.setattr(dom_actions, "dismiss_cookie_banners", _fake_cookie)
    monkeypatch.setattr(dom_actions, "_click_comment_trigger_fallback", _fake_fallback)

    assert await dom_actions.open_comment_section(
        _Page(),
        max_wait=0.2,
        require_input=True,
        aggressive=True,
        main_comment_aggressive=True,
    ) is False
    assert calls["selectors"]
    assert all('[data-e2e="comment-input"]' in selector for selector in calls["selectors"])
    assert all('[contenteditable="true"], textarea, [role="textbox"]' not in selector for selector in calls["selectors"])


@pytest.mark.unit
@pytest.mark.asyncio
async def test_open_comment_section_accepts_ready_final_state(monkeypatch):
    calls = {"fallback": 0, "state": 0}

    class _Page:
        url = "https://www.tiktok.com/@u/video/1"

        async def evaluate(self, _script):
            return {"click": False, "input_pending": False, "list_visible": False}

    async def _fake_suppress(_page):
        return 0

    async def _fake_cookie(_page):
        return 0

    async def _fake_fallback(*_args, **_kwargs):
        calls["fallback"] += 1
        return False

    async def _fake_state(_page):
        calls["state"] += 1
        return {
            "input_visible": True,
            "comment_input_visible": True,
            "publish_input_candidate_visible": False,
            "surface_visible": True,
        }

    monkeypatch.setattr(dom_actions, "suppress_keyboard_shortcuts_overlay", _fake_suppress)
    monkeypatch.setattr(dom_actions, "dismiss_cookie_banners", _fake_cookie)
    monkeypatch.setattr(dom_actions, "_click_comment_trigger_fallback", _fake_fallback)
    monkeypatch.setattr(dom_actions, "_comment_surface_state", _fake_state)

    assert await dom_actions.open_comment_section(_Page(), max_wait=0.2, require_input=True) is True
    assert calls["fallback"] >= 1
    assert calls["state"] >= 1


@pytest.mark.unit
def test_like_parent_comment_script_is_scoped_to_parent_row():
    script = dom_actions._JS_LIKE_PARENT_COMMENT

    assert "data-smart-parent-anchor" in script
    assert "comment-like" in script
    assert "like_button_not_found_in_parent" in script
    assert "CSS.escape" in script
    assert "candidate.closest && candidate.closest('button, [role=\"button\"], div[tabindex=\"0\"]')" in script
    assert "querySelectorAll('button, [role=\"button\"], div[tabindex=\"0\"]')" in script


@pytest.mark.unit
@pytest.mark.asyncio
async def test_like_parent_comment_if_safe_is_best_effort():
    calls = {"args": None}

    class _Page:
        async def evaluate(self, script, args):
            assert script == dom_actions._JS_LIKE_PARENT_COMMENT
            calls["args"] = args
            return {"success": False, "reason": "like_button_not_found_in_parent", "strategy": "parent-anchor"}

    result = await dom_actions.like_parent_comment_if_safe(
        _Page(),
        parent_comment_text="main text",
        parent_comment_id="123",
        parent_anchor_token="anchor-1",
        max_total_seconds=0.5,
    )

    assert result.success is False
    assert result.reason == "like_button_not_found_in_parent"
    assert calls["args"] == ["123", "main text", "anchor-1"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_open_reply_box_requires_bonded_reply_composer(monkeypatch):
    class _Page:
        async def evaluate(self, script, *_args):
            if script == dom_actions._JS_OPEN_REPLY:
                return {"clicked": True, "strategy": "parent-text-match"}
            if script == dom_actions._JS_FIND_REPLY_INPUT:
                return {"found": False, "reason": "reply_composer_triple_not_found"}
            raise AssertionError("unexpected script")

        async def wait_for_selector(self, *_args, **_kwargs):
            return None

    async def _fake_sleep(_seconds):
        return None

    monkeypatch.setattr(dom_actions.asyncio, "sleep", _fake_sleep)

    result = await dom_actions.open_reply_box(_Page(), parent_comment_text="parent")

    assert result.success is False
    assert result.reason == "reply_composer_triple_not_found"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_open_reply_box_accepts_bonded_composer_after_click(monkeypatch):
    class _Page:
        async def evaluate(self, script, args=None):
            if script == dom_actions._JS_OPEN_REPLY:
                return {"clicked": True, "strategy": "parent-anchor-reply", "anchorToken": "anchor-1"}
            if script == dom_actions._JS_FIND_REPLY_INPUT:
                assert args == ["anchor-1"]
                return {
                    "found": True,
                    "strategy": "reply-composer-between_parent_and_next_row",
                    "score": 19000,
                    "anchorToken": "anchor-1",
                }
            raise AssertionError("unexpected script")

        async def wait_for_selector(self, *_args, **_kwargs):
            return None

    async def _fake_sleep(_seconds):
        return None

    monkeypatch.setattr(dom_actions.asyncio, "sleep", _fake_sleep)

    result = await dom_actions.open_reply_box(
        _Page(),
        parent_comment_text="parent",
        parent_anchor_token="anchor-1",
    )

    assert result.success is True
    assert result.anchor_token == "anchor-1"
    assert result.strategy == "parent-anchor-reply+reply-composer-between_parent_and_next_row"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_find_reply_input_smart_uses_bonded_target_handles(monkeypatch):
    calls = {"handle": 0, "state": 0}

    class _Handle:
        def __init__(self, kind="input"):
            self.kind = kind

        async def evaluate(self, _script):
            return {"strategy": f"reply-{self.kind}", "score": "19000"}

    class _Page:
        async def evaluate_handle(self, script, args=None):
            calls["handle"] += 1
            if script == dom_actions._JS_FIND_REPLY_INPUT_HANDLE:
                assert args == ["anchor-1"]
                return _Handle("input")
            if "data-smart-reply-composer" in script:
                return _Handle("composer")
            if "data-smart-reply-post" in script:
                return _Handle("post")
            raise AssertionError("unexpected handle script")

        async def evaluate(self, _script):
            calls["state"] += 1
            return {
                "found": True,
                "strategy": "reply-composer-between_parent_and_next_row",
                "score": 19000,
                "anchorToken": "anchor-1",
            }

    input_handle, result = await dom_actions.find_reply_input_smart(
        _Page(),
        parent_anchor_token="anchor-1",
        max_total_seconds=1.0,
    )

    assert input_handle is not None
    assert result.success is True
    assert result.strategy == "reply-composer-between_parent_and_next_row"
    assert calls == {"handle": 3, "state": 1}


@pytest.mark.unit
@pytest.mark.asyncio
async def test_find_reply_post_button_smart_uses_handle_without_selector_roundtrip():
    calls = {"handle": 0, "query": 0}

    class _Handle:
        async def evaluate(self, _script):
            return {"strategy": "reply-composer", "targetTag": "BUTTON"}

    class _Page:
        async def evaluate_handle(self, script):
            calls["handle"] += 1
            assert script == dom_actions._JS_FIND_REPLY_POST_BUTTON_HANDLE
            return _Handle()

        async def query_selector(self, _selector):
            calls["query"] += 1
            raise AssertionError("reply post handle path should not query_selector after found")

        async def evaluate(self, _script):
            raise AssertionError("reply post handle path should not need state lookup after found")

    post_button, result = await dom_actions.find_reply_post_button_smart(
        _Page(),
        max_total_seconds=1.0,
    )

    assert post_button is not None
    assert result.success is True
    assert result.strategy == "reply-composer"
    assert calls == {"handle": 1, "query": 0}


@pytest.mark.unit
def test_open_reply_box_parent_text_search_is_broad_and_normalized():
    script = dom_actions._JS_OPEN_REPLY

    assert "normalizedNeedle" in script
    assert "\\u200b-\\u200d\\ufeff" in script
    assert "commentContainerSelector" in script
    assert "[data-e2e*=\"comment-level\"]" in script
    assert "[class*=\"DivComment\"]" in script
    assert "parent-text-match-parent" in script


@pytest.mark.unit
def test_open_reply_box_exact_anchor_does_not_require_full_parent_text_match():
    script = dom_actions._JS_OPEN_REPLY
    anchor_block = script.split("if (targetAnchorToken) {", 1)[1].split("// Strategy 1", 1)[0]

    assert "rootMatchesNeedle(el)" not in anchor_block
    assert "depth < 8" in anchor_block
    assert "parent-anchor-parent" in anchor_block
    assert "parent_anchor_not_found" in anchor_block


@pytest.mark.unit
def test_open_reply_box_prefers_parent_dom_anchor_before_text_search():
    script = dom_actions._JS_OPEN_REPLY

    assert "parentAnchorToken" in script
    assert "data-smart-parent-anchor" in script
    assert "parent-anchor" in script


@pytest.mark.unit
def test_reply_input_finder_requires_live_parent_anchor_scope():
    script = dom_actions._JS_FIND_REPLY_INPUT

    assert "parent_anchor_token_missing" in script
    assert "reply_composer_triple_not_found" in script
    assert "parent_anchor_not_found" in script
    assert "input_without_post" in script
    assert "global_main_composer" in script
    assert "data-smart-reply-anchor-token" in script
    assert "editor.setAttribute('data-smart-id', 'comment-input')" not in script
    assert "editor.removeAttribute('data-smart-id')" in script
    assert "editor.removeAttribute('data-smart-main-input')" in script


@pytest.mark.unit
def test_reply_input_finder_accepts_disabled_post_candidate_before_typing():
    script = dom_actions._JS_FIND_REPLY_INPUT

    assert "allowDisabled" in script
    assert "if (!allowDisabled && disabled(el)) continue" in script
    assert "if (!allowDisabled && st.pointerEvents === 'none') continue" in script
    assert "findPostInside(cur, inputRect, {allowDisabled: true})" in script
    assert "postInitiallyDisabled" in script
    assert "data-smart-reply-post-initial-disabled" in script


@pytest.mark.unit
def test_reply_post_finder_does_not_fallback_to_global_comment_markers():
    script = dom_actions._JS_FIND_REPLY_POST_BUTTON
    handle_script = "".join(dom_actions._JS_FIND_REPLY_POST_BUTTON_HANDLE)
    enabled_source = inspect.getsource(dom_actions.wait_for_post_button_enabled)
    optimistic_source = inspect.getsource(dom_actions.find_reply_send_candidate_for_optimistic_click)

    assert 'document.querySelector(\'[data-smart-reply-input="1"]\')' in script
    assert 'document.querySelector(\'[data-smart-id="comment-input"]\')' not in script
    assert '[data-smart-reply-post="1"]' in handle_script
    assert '[data-smart-id=\\"comment-post\\"]' not in handle_script
    assert "document.querySelector('[data-smart-reply-post=\"1\"]')" in enabled_source
    assert "const replyPostCandidates = () =>" in enabled_source
    assert "composer.querySelectorAll(selector)" in enabled_source
    assert "reply-enabled-refind" in enabled_source
    assert "let sawDisabled = null" in enabled_source
    assert "if (sawDisabled) return sawDisabled" in enabled_source
    assert "const isAccessoryReplyIcon = (raw) =>" in enabled_source
    assert "label.includes('emoji')" in enabled_source
    assert "label.includes('mention')" in enabled_source
    assert "compact === '@'" in enabled_source
    assert "const usableInputRect = (input) =>" in enabled_source
    assert "const inputRect = usableInputRect(input)" in enabled_source
    assert enabled_source.index("raw.closest('button')") < enabled_source.index("raw,")
    assert "document.querySelector('[data-smart-reply-input=\"1\"]')" in optimistic_source
    assert "document.querySelector('[data-smart-reply-composer=\"1\"]')" in optimistic_source
    assert "inputText !== expected" in optimistic_source
    assert "reply-optimistic-send" in optimistic_source
    assert "compact === '@'" in optimistic_source
    assert "label.includes('mention')" in optimistic_source
    assert "st.pointerEvents === 'none'" in optimistic_source
    assert "raw.closest && raw.closest('button')" in optimistic_source


# ---------------------------------------------------------------------------
# Atomic set-text primitive + type_into_comment primary path
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_atomic_set_comment_text_js_preserves_reply_scoping():
    """The atomic primitive must scope replies via the reply anchor mechanism
    and must NEVER fall back to the global composer in reply mode."""
    script = dom_actions._JS_ATOMIC_SET_COMMENT_TEXT

    # Reply path is driven by the embedded reply finder, not globalPick.
    assert "const replyProbe =" in script
    assert "data-smart-reply-input" in script
    assert "reply_marked_input_missing" in script
    # The embedded reply finder keeps its bonded-composer guard reasons.
    assert "reply_composer_triple_not_found" in script
    assert "input_without_post" in script
    assert "global_main_composer" in script
    assert "parent_anchor_not_found" in script
    assert "real.removeAttribute('data-smart-id')" in script
    assert "real.removeAttribute('data-smart-main-input')" in script
    assert script.index("real.removeAttribute('data-smart-id')") < script.index(
        "real.setAttribute('data-smart-id', 'comment-input')"
    )
    assert "real.setAttribute('data-smart-main-input', '1')" in script
    # globalPick is only reachable in the non-reply branch (after replyProbe).
    assert "el = globalPick();" in script
    assert script.index("const replyProbe =") < script.index("el = globalPick();")
    # Still does the proven execCommand insert + placeholder-leak rejection.
    assert "document.execCommand('insertText'" in script
    assert "startsWithPlaceholder" in script


@pytest.mark.unit
def test_comment_typing_verification_rejects_partial_and_fake_dom_text():
    source = inspect.getsource(dom_actions.type_into_comment)

    assert "expected.includes(txt)" not in dom_actions._JS_COMMENT_INPUT_TEXT_MATCH
    assert "expected.includes(txt)" not in dom_actions._JS_ATOMIC_SET_COMMENT_TEXT
    assert "expected.includes(txt)" not in source
    assert "real.textContent = expected" not in source
    assert "contenteditable_js_fallback_disabled" not in source
    assert "document.execCommand('insertText'" in source
    assert 'page.keyboard.press("Tab")' not in source
    assert 'page.keyboard.press("Shift+Tab")' not in source


@pytest.mark.unit
def test_main_comment_input_paths_prefer_main_marker():
    wait_source = inspect.getsource(dom_actions.wait_for_post_button_enabled)
    typing_source = inspect.getsource(dom_actions.type_into_comment)

    assert "data-smart-main-input" in dom_actions._JS_FIND_COMMENT_INPUT
    assert "data-smart-main-input" in dom_actions._JS_COMMENT_INPUT_TEXT_MATCH
    assert "document.querySelector('[data-smart-main-input=\"1\"]')" in wait_source
    assert "'[data-smart-main-input=\"1\"]'" in typing_source


@pytest.mark.unit
def test_raw_dom_comment_insert_does_not_dispatch_second_insert_text_event():
    script = dom_actions._JS_ATOMIC_SET_COMMENT_TEXT
    atomic_after_exec = script.split(
        "document.execCommand('insertText', false, insertText);",
        1,
    )[1].split("// Leave caret", 1)[0]

    assert "fireInputEvents(real, insertText)" not in atomic_after_exec
    assert "fireCommitEvents(real)" in atomic_after_exec

    source = inspect.getsource(dom_actions.type_into_comment)
    fallback_after_exec = source.split(
        "document.execCommand('insertText', false, expected);",
        1,
    )[1].split("const range = document.createRange();", 1)[0]

    assert "fireInputEvents(real, expected)" not in fallback_after_exec
    assert "fireCommitEvents(real)" in fallback_after_exec


@pytest.mark.unit
@pytest.mark.asyncio
async def test_atomic_set_comment_text_forwards_reply_flags():
    captured = {}

    class _Page:
        async def evaluate(self, script, *args):
            captured["script"] = script
            captured["args"] = args[0] if args else None
            return {"ok": False, "reason": "reply_input_not_scoped_to_parent", "scoped": "reply"}

    result = await dom_actions.atomic_set_comment_text(
        _Page(),
        "hello world",
        reply_scoped=True,
        anchor_token="pa-123",
    )

    assert captured["script"] == dom_actions._JS_ATOMIC_SET_COMMENT_TEXT
    # [insertText, expected, anchorToken, replyScoped, focusOnly, clearOnly]
    assert captured["args"] == ["hello world", "hello world", "pa-123", True, False, False]
    assert result == {"ok": False, "reason": "reply_input_not_scoped_to_parent", "scoped": "reply"}


@pytest.mark.unit
@pytest.mark.asyncio
async def test_atomic_set_comment_text_holds_back_last_char_when_requested():
    captured = {}

    class _Page:
        async def evaluate(self, script, *args):
            captured["args"] = args[0] if args else None
            return {"ok": True, "reason": "inserted", "scoped": "global"}

    result = await dom_actions.atomic_set_comment_text(
        _Page(),
        "hello",
        insert_text="hell",
    )

    # insert_text is what lands in-page; expected stays the full string.
    assert captured["args"] == ["hell", "hello", "", False, False, False]
    assert result["ok"] is True


@pytest.mark.unit
@pytest.mark.asyncio
async def test_atomic_set_comment_text_can_verify_clear_only():
    captured = {}

    class _Page:
        async def evaluate(self, script, *args):
            captured["script"] = script
            captured["args"] = args[0] if args else None
            return {"ok": True, "reason": "cleared", "text": "", "scoped": "global"}

    result = await dom_actions.atomic_set_comment_text(
        _Page(),
        "",
        clear_only=True,
    )

    assert captured["script"] == dom_actions._JS_ATOMIC_SET_COMMENT_TEXT
    assert captured["args"] == ["", "", "", False, False, True]
    assert result["ok"] is True
    assert result["reason"] == "cleared"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_type_into_comment_reply_uses_trusted_full_insert_first(monkeypatch):
    """Reply path starts with scoped focus + trusted full text insertion."""
    keyboard_calls = {"type": [], "insert_text": [], "press": []}

    class _Keyboard:
        async def type(self, *args, **_kwargs):
            keyboard_calls["type"].append(args[0] if args else "")

        async def insert_text(self, text):
            keyboard_calls["insert_text"].append(text)

        async def press(self, key):
            keyboard_calls["press"].append(key)

    class _Page:
        keyboard = _Keyboard()

        def __init__(self):
            self.scripts = []
            self.atomic_calls = []

        async def evaluate(self, script, *args):
            self.scripts.append(script)
            if script == dom_actions._JS_ATOMIC_SET_COMMENT_TEXT:
                payload = args[0] if args else None
                self.atomic_calls.append(payload)
                return {
                    "ok": True,
                    "reason": "focused",
                    "text": payload[0],
                    "scoped": "reply",
                }
            if "document.activeElement" in str(script):
                return True
            return {"ok": True, "text": "hello"}

    async def _fake_suppress(_page):
        return 0

    async def _fake_sleep(_seconds):
        return None

    monkeypatch.setattr(dom_actions, "suppress_keyboard_shortcuts_overlay", _fake_suppress)
    monkeypatch.setattr(dom_actions.asyncio, "sleep", _fake_sleep)

    page = _Page()
    result = await dom_actions.type_into_comment(
        page,
        object(),
        "hello",
        fast=True,
        reply_scoped=True,
        anchor_token="pa-123",
    )

    assert result["ok"] is True
    assert result["strategy"] == "atomic-focus-insert-text"
    assert result["anchor_bound"] is True
    assert dom_actions._JS_ATOMIC_SET_COMMENT_TEXT in page.scripts
    # Reply path: scoped focus (atomic focus_only), then a TRUSTED Ctrl+A+Delete to
    # reset React/Lexical EditorState (no DOM-only clear_only round-trip), then the
    # trusted insert. The trusted clear mirrors the main-composer fix but stays
    # scoped because focus is already confirmed inside the reply editor.
    assert page.atomic_calls == [
        ["", "hello", "pa-123", True, True, False],
    ]
    assert keyboard_calls["insert_text"] == ["hello"]
    assert keyboard_calls["press"] == ["Control+A", "Delete"]
    assert keyboard_calls["type"] == []


@pytest.mark.unit
@pytest.mark.asyncio
async def test_type_into_comment_main_prefers_focus_insert_over_prefix_tail(monkeypatch):
    keyboard_calls = {"insert_text": [], "press": []}

    class _Keyboard:
        async def insert_text(self, text):
            keyboard_calls["insert_text"].append(text)

        async def press(self, key):
            keyboard_calls["press"].append(key)

        async def type(self, *_args, **_kwargs):
            raise AssertionError("main fast path should not reach slow keyboard.type")

    class _Page:
        keyboard = _Keyboard()

        def __init__(self):
            self.atomic_calls = []

        async def evaluate(self, script, *args):
            if script == dom_actions._JS_ATOMIC_SET_COMMENT_TEXT:
                payload = args[0] if args else None
                self.atomic_calls.append(payload)
                return {"ok": True, "reason": "focused", "scoped": "global"}
            return {"ok": True, "text": "hello"}

    async def _fake_suppress(_page):
        return 0

    async def _fake_sleep(_seconds):
        return None

    monkeypatch.setattr(dom_actions, "suppress_keyboard_shortcuts_overlay", _fake_suppress)
    monkeypatch.setattr(dom_actions.asyncio, "sleep", _fake_sleep)

    page = _Page()
    result = await dom_actions.type_into_comment(page, object(), "hello", fast=True)

    assert result["ok"] is True
    assert result["strategy"] == "atomic-focus-insert-text"
    # MAIN composer now clears via a TRUSTED select-all+delete (resets React/Lexical
    # EditorState) instead of an atomic clear_only that only wipes the DOM.
    assert page.atomic_calls == [
        ["", "hello", "", False, True, False],
    ]
    assert keyboard_calls["press"] == ["Control+A", "Delete"]
    assert keyboard_calls["insert_text"] == ["hello"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_type_into_comment_main_uses_atomic_prefix_tail_after_keyboard_miss(monkeypatch):
    keyboard_calls = {"insert_text": [], "press": []}
    atomic_calls = []

    class _Keyboard:
        async def insert_text(self, text):
            keyboard_calls["insert_text"].append(text)

        async def press(self, key):
            keyboard_calls["press"].append(key)

        async def type(self, *_args, **_kwargs):
            raise AssertionError("main fallback should not reach slow keyboard.type")

    class _Page:
        keyboard = _Keyboard()

        async def evaluate(self, script, *args):
            if "document.activeElement" in str(script):
                return True
            if keyboard_calls["insert_text"] == ["hello", "o"]:
                return {"ok": True, "text": "hello"}
            return {"ok": False, "text": ""}

    async def _fake_atomic(_page, text, **kwargs):
        atomic_calls.append((text, kwargs))
        if kwargs.get("focus_only"):
            return {"ok": True, "reason": "focused", "scoped": "global"}
        if kwargs.get("clear_only"):
            return {"ok": True, "reason": "cleared", "text": "", "scoped": "global"}
        if kwargs.get("insert_text") == "hell":
            return {"ok": False, "reason": "verify_failed", "text": "hell", "scoped": "global"}
        return {"ok": False, "reason": "unexpected_atomic_call", "text": "", "scoped": "global"}

    async def _fake_suppress(_page):
        return 0

    async def _fake_sleep(_seconds):
        return None

    monkeypatch.setattr(dom_actions, "atomic_set_comment_text", _fake_atomic)
    monkeypatch.setattr(dom_actions, "suppress_keyboard_shortcuts_overlay", _fake_suppress)
    monkeypatch.setattr(dom_actions.asyncio, "sleep", _fake_sleep)

    result = await dom_actions.type_into_comment(
        _Page(),
        object(),
        "hello",
        fast=True,
        allow_main_dom_fallback=True,
    )

    assert result["ok"] is True
    assert result["strategy"] == "atomic-prefix-trusted-tail"
    # MAIN composer clears via trusted Ctrl+A+Delete (no atomic clear_only call).
    assert [kwargs for _text, kwargs in atomic_calls] == [
        {"reply_scoped": False, "anchor_token": None, "focus_only": True},
        {"reply_scoped": False, "anchor_token": None, "insert_text": "hell"},
    ]
    assert keyboard_calls["press"] == ["Control+A", "Delete"]
    assert keyboard_calls["insert_text"] == ["hello", "o"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_type_into_comment_main_keeps_typing_when_clear_verify_is_uncertain(monkeypatch):
    keyboard_calls = {"insert_text": [], "press": []}
    atomic_calls = []

    class _Keyboard:
        async def insert_text(self, text):
            keyboard_calls["insert_text"].append(text)

        async def press(self, key):
            keyboard_calls["press"].append(key)

        async def type(self, *_args, **_kwargs):
            raise AssertionError("main path should use insert_text, not slow keyboard.type")

    class _Page:
        keyboard = _Keyboard()

        async def evaluate(self, script, *args):
            if "document.activeElement" in str(script):
                return True
            if keyboard_calls["insert_text"] == ["hello"]:
                return {"ok": True, "text": "hello"}
            return {"ok": False, "text": ""}

    async def _fake_atomic(_page, text, **kwargs):
        atomic_calls.append((text, kwargs))
        if kwargs.get("focus_only"):
            return {"ok": True, "reason": "focused", "scoped": "global"}
        if kwargs.get("clear_only"):
            return {"ok": False, "reason": "clear_verify_failed", "text": "Add comment...", "scoped": "global"}
        return {"ok": False, "reason": "unexpected_atomic_call", "text": "", "scoped": "global"}

    async def _fake_suppress(_page):
        return 0

    async def _fake_sleep(_seconds):
        return None

    monkeypatch.setattr(dom_actions, "atomic_set_comment_text", _fake_atomic)
    monkeypatch.setattr(dom_actions, "suppress_keyboard_shortcuts_overlay", _fake_suppress)
    monkeypatch.setattr(dom_actions.asyncio, "sleep", _fake_sleep)

    result = await dom_actions.type_into_comment(_Page(), object(), "hello", fast=True)

    assert result["ok"] is True
    assert result["strategy"] == "atomic-focus-insert-text"
    # MAIN composer always resets EditorState with a trusted Ctrl+A+Delete before
    # the trusted insert, so no atomic clear_only round-trip is used.
    assert [kwargs for _text, kwargs in atomic_calls] == [
        {"reply_scoped": False, "anchor_token": None, "focus_only": True},
    ]
    assert keyboard_calls["press"] == ["Control+A", "Delete"]
    assert keyboard_calls["insert_text"] == ["hello"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_type_into_comment_main_uses_contenteditable_js_fallback_after_keyboard_miss(monkeypatch):
    keyboard_calls = {"insert_text": [], "press": [], "type": []}
    evaluate_calls = []

    class _Keyboard:
        async def insert_text(self, text):
            keyboard_calls["insert_text"].append(text)

        async def press(self, key):
            keyboard_calls["press"].append(key)

        async def type(self, text, **_kwargs):
            keyboard_calls["type"].append(text)

    class _Element:
        async def evaluate(self, script, *args):
            if "textContent || el.innerText" in str(script):
                return ""
            return None

        async def click(self, **_kwargs):
            return None

    class _Page:
        keyboard = _Keyboard()

        async def query_selector(self, _selector):
            return None

        async def evaluate(self, script, *args):
            script_text = str(script)
            evaluate_calls.append(script_text)
            if "document.activeElement" in script_text:
                return True
            if "const pickEditor" in script_text and "fireInputEvents" in script_text:
                return {"ok": True, "reason": "inserted", "text": args[0], "placeholder_leak": False}
            return {"ok": False, "text": ""}

    async def _fake_atomic(_page, text, **kwargs):
        return {"ok": False, "reason": "no_editor", "text": "", "scoped": "global"}

    async def _fake_suppress(_page):
        return 0

    async def _fake_sleep(_seconds):
        return None

    monkeypatch.setattr(dom_actions, "atomic_set_comment_text", _fake_atomic)
    monkeypatch.setattr(dom_actions, "suppress_keyboard_shortcuts_overlay", _fake_suppress)
    monkeypatch.setattr(dom_actions.asyncio, "sleep", _fake_sleep)

    result = await dom_actions.type_into_comment(
        _Page(),
        _Element(),
        "hello",
        fast=True,
        allow_main_dom_fallback=True,
    )

    assert result["ok"] is True
    assert result["strategy"].startswith("js-fallback:")
    assert keyboard_calls["insert_text"] == ["hello"]
    assert keyboard_calls["type"] == []
    assert any("const pickEditor" in call and "document.execCommand('insertText'" in call for call in evaluate_calls)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_type_into_comment_main_skips_raw_dom_fallback_by_default(monkeypatch):
    keyboard_calls = {"insert_text": [], "press": [], "type": []}
    evaluate_calls = []

    class _Keyboard:
        async def insert_text(self, text):
            keyboard_calls["insert_text"].append(text)

        async def press(self, key):
            keyboard_calls["press"].append(key)

        async def type(self, text, **_kwargs):
            keyboard_calls["type"].append(text)

    class _Element:
        async def evaluate(self, script, *args):
            if "textContent || el.innerText" in str(script):
                return ""
            return None

        async def click(self, **_kwargs):
            return None

    class _Page:
        keyboard = _Keyboard()

        async def query_selector(self, _selector):
            return None

        async def evaluate(self, script, *args):
            script_text = str(script)
            evaluate_calls.append(script_text)
            if "document.activeElement" in script_text:
                return True
            if "const pickEditor" in script_text and "document.execCommand('insertText'" in script_text:
                raise AssertionError("raw JS fallback must stay disabled for main publish by default")
            return {"ok": False, "text": ""}

    async def _fake_atomic(_page, text, **kwargs):
        return {"ok": False, "reason": "no_editor", "text": "", "scoped": "global"}

    async def _fake_suppress(_page):
        return 0

    async def _fake_sleep(_seconds):
        return None

    monkeypatch.setattr(dom_actions, "atomic_set_comment_text", _fake_atomic)
    monkeypatch.setattr(dom_actions, "suppress_keyboard_shortcuts_overlay", _fake_suppress)
    monkeypatch.setattr(dom_actions.asyncio, "sleep", _fake_sleep)

    result = await dom_actions.type_into_comment(_Page(), _Element(), "hello", fast=True)

    assert result["ok"] is False
    assert result["reason"] == "main_raw_dom_fallback_disabled"
    assert result["strategy"] in {"keyboard-type", "keyboard-focus"}
    assert keyboard_calls["insert_text"] == ["hello"]
    assert keyboard_calls["type"] == []
    assert not any("const pickEditor" in call and "document.execCommand('insertText'" in call for call in evaluate_calls)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_type_into_comment_reply_skips_raw_dom_atomic_fallback_after_keyboard_miss(monkeypatch):
    keyboard_calls = {"insert_text": [], "press": []}
    atomic_calls = []

    class _Keyboard:
        async def insert_text(self, text):
            keyboard_calls["insert_text"].append(text)

        async def press(self, key):
            keyboard_calls["press"].append(key)

        async def type(self, *_args, **_kwargs):
            raise AssertionError("reply fallback should not reach slow keyboard.type")

    class _Page:
        keyboard = _Keyboard()

        async def evaluate(self, script, *args):
            script_text = str(script)
            if "document.activeElement" in script_text:
                return True
            if 'data-smart-reply-input="1"' in script_text:
                return {"ok": False, "text": ""}
            return {"ok": False, "text": ""}

    async def _fake_atomic(_page, text, **kwargs):
        atomic_calls.append((text, kwargs))
        if kwargs.get("insert_text") == "hell":
            return {"ok": False, "reason": "verify_failed", "text": "he", "scoped": "reply"}
        if kwargs.get("focus_only"):
            return {"ok": True, "reason": "focused", "scoped": "reply"}
        if kwargs.get("clear_only"):
            return {"ok": True, "reason": "cleared", "text": "", "scoped": "reply"}
        return {"ok": True, "reason": "inserted", "text": text, "scoped": "reply"}

    async def _fake_suppress(_page):
        return 0

    async def _fake_sleep(_seconds):
        return None

    monkeypatch.setattr(dom_actions, "atomic_set_comment_text", _fake_atomic)
    monkeypatch.setattr(dom_actions, "suppress_keyboard_shortcuts_overlay", _fake_suppress)
    monkeypatch.setattr(dom_actions.asyncio, "sleep", _fake_sleep)

    result = await dom_actions.type_into_comment(
        _Page(),
        None,
        "hello",
        fast=True,
        reply_scoped=True,
        anchor_token="pa-777",
    )

    assert result["ok"] is False
    assert result["reason"] == "editor_missing_after_atomic_attempts"
    assert result["strategy"] == "resolve-editor"
    assert result["anchor_bound"] is True
    # Reply primary now resets EditorState with a trusted Ctrl+A+Delete (no atomic
    # clear_only round-trip). If the trusted insert does not verify, reply mode
    # must NOT fall through to raw-DOM atomic prefix/full insert because that is
    # the path that can desync React/Lexical state.
    assert [kwargs for _text, kwargs in atomic_calls] == [
        {"reply_scoped": True, "anchor_token": "pa-777", "focus_only": True},
    ]
    assert keyboard_calls["press"] == ["Control+A", "Delete"]
    assert keyboard_calls["insert_text"] == ["hello"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_type_into_comment_reply_scoped_forwards_anchor_to_atomic(monkeypatch):
    captured = {"atomic_args": []}

    class _Keyboard:
        async def insert_text(self, *_args, **_kwargs):
            return None

        async def press(self, *_args, **_kwargs):
            return None

    class _Page:
        keyboard = _Keyboard()

        async def evaluate(self, script, *args):
            if script == dom_actions._JS_ATOMIC_SET_COMMENT_TEXT:
                captured["atomic_args"].append(args[0])
                return {
                    "ok": True,
                    "reason": "focused",
                    "text": args[0][0],
                    "scoped": "reply",
                }
            return {"ok": True, "text": "hi there"}

    async def _fake_suppress(_page):
        return 0

    async def _fake_sleep(_seconds):
        return None

    monkeypatch.setattr(dom_actions, "suppress_keyboard_shortcuts_overlay", _fake_suppress)
    monkeypatch.setattr(dom_actions.asyncio, "sleep", _fake_sleep)

    result = await dom_actions.type_into_comment(
        _Page(),
        object(),
        "hi there",
        fast=True,
        reply_scoped=True,
        anchor_token="pa-xyz",
    )

    assert result["ok"] is True
    assert result["strategy"] == "atomic-focus-insert-text"
    assert result["anchor_bound"] is True
    # replyScoped True and the anchor token are forwarded into the atomic JS for
    # the scoped focus. The clear before typing is now a TRUSTED Ctrl+A+Delete
    # (keyboard), not an atomic clear_only round-trip, so when the trusted insert
    # verifies on the first try there is only the single scoped focus atomic call.
    assert captured["atomic_args"][0][2] == "pa-xyz"
    assert captured["atomic_args"][0][3] is True
    assert captured["atomic_args"][0][4] is True  # scoped focus before trusted clear + insert


@pytest.mark.unit
def test_reply_typing_stays_scoped_and_skips_unscoped_js_fallback():
    source = inspect.getsource(dom_actions.type_into_comment)

    assert "document.querySelector('[data-smart-reply-input=\"1\"]')" in source
    assert "Scoped reply typing did not verify; skipping unscoped JS fallback" in source
    assert "anchor_token=anchor_token if reply_scoped else None" in source


@pytest.mark.unit
@pytest.mark.asyncio
async def test_find_comment_input_smart_oneshot_returns_handle_first(monkeypatch):
    """The cheap one-shot surface detector returns a handle immediately and the
    heavy open/probe chain is never started."""

    class _Handle:
        pass

    class _Page:
        async def evaluate(self, script, *args):
            if script == dom_actions._JS_COMMENT_SURFACE_STATE:
                return {"comment_input_visible": True}
            raise AssertionError("one-shot success must not run further evaluates")

        async def query_selector(self, selector):
            if selector == '[data-smart-id="comment-input"]':
                return _Handle()
            return None

    async def _no_open(*_args, **_kwargs):
        raise AssertionError("open_comment_section must not run when one-shot succeeds")

    async def _no_cookie(_page):
        raise AssertionError("cookie cleanup must not run when one-shot succeeds")

    monkeypatch.setattr(dom_actions, "open_comment_section", _no_open)
    monkeypatch.setattr(dom_actions, "dismiss_cookie_banners", _no_cookie)

    handle, result = await dom_actions.find_comment_input_smart(
        _Page(),
        max_total_seconds=3.0,
    )

    assert handle is not None
    assert result.success is True
    assert result.strategy == "oneshot-surface-input"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_wait_for_post_button_enabled_fails_fast_when_composer_stays_empty(monkeypatch):
    calls = {"evaluate": 0, "sleep": 0}

    class _Page:
        async def evaluate(self, *_args, **_kwargs):
            calls["evaluate"] += 1
            return {"ready": False, "reason": "input_empty", "inputText": ""}

    async def _fake_sleep(_seconds):
        calls["sleep"] += 1

    monkeypatch.setattr(dom_actions.asyncio, "sleep", _fake_sleep)

    button, result = await dom_actions.wait_for_post_button_enabled(
        _Page(),
        max_wait_seconds=10.0,
    )

    assert button is None
    assert result.success is False
    assert result.reason == "post_button_input_not_ready:input_empty"
    assert calls["evaluate"] == 2
    assert calls["sleep"] == 1


@pytest.mark.unit
@pytest.mark.asyncio
async def test_wait_for_post_button_enabled_fails_fast_when_composer_text_mismatches_expected(monkeypatch):
    calls = {"evaluate": 0, "sleep": 0}

    class _Page:
        async def evaluate(self, _script, args):
            calls["evaluate"] += 1
            assert args[1] == "fresh text"
            return {
                "ready": False,
                "reason": "text_mismatch",
                "inputText": "old text",
                "expectedText": "fresh text",
            }

    async def _fake_sleep(_seconds):
        calls["sleep"] += 1

    monkeypatch.setattr(dom_actions.asyncio, "sleep", _fake_sleep)

    button, result = await dom_actions.wait_for_post_button_enabled(
        _Page(),
        max_wait_seconds=10.0,
        expected_text="fresh text",
    )

    assert button is None
    assert result.success is False
    assert result.reason == "post_button_input_not_ready:text_mismatch"
    assert calls["evaluate"] == 2
    assert calls["sleep"] == 1


@pytest.mark.unit
def test_post_button_enabled_probe_keeps_empty_input_distinct_from_placeholder():
    source = inspect.getsource(dom_actions.wait_for_post_button_enabled)

    assert "if (!txt) return false;" in source
    assert "if (!txt) return true;" not in source


@pytest.mark.unit
def test_exact_comment_post_button_is_not_rejected_by_upper_viewport_geometry():
    source = inspect.getsource(dom_actions.wait_for_post_button_enabled)

    assert "r.top < window.innerHeight * 0.25" not in source
    assert "clickTargetFor(dataE2e, {allowUpperViewport: true})" in dom_actions._JS_FIND_POST_BUTTON
