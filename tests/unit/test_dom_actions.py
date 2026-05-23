import pytest

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
async def test_open_comment_section_uses_real_mouse_click_for_trigger(monkeypatch):
    calls = {"click": None, "selector": None}

    class _Mouse:
        async def click(self, x, y):
            calls["click"] = (x, y)

    class _Page:
        mouse = _Mouse()

        async def evaluate(self, _script):
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
    assert "aria-label" in script
    assert "комментарии" in script
    assert "оставить комментар" in script
    assert "button, [role=\"button\"]" in script
    assert "comment-open-trigger" in script
    assert "input_pending: true" in script


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
def test_reply_input_activation_guard_has_reply_mode_markers():
    script = dom_actions._JS_REPLY_INPUT_ACTIVE

    assert "reply_input_not_activated" in script
    assert "mention_prefill" in script
    assert "\\u043e\\u0442\\u0432\\u0435\\u0442" in script
    assert "\\ub2f5\\uae00" in script


@pytest.mark.unit
@pytest.mark.asyncio
async def test_open_reply_box_requires_reply_input_activation(monkeypatch):
    class _Page:
        async def evaluate(self, script, *_args):
            if script == dom_actions._JS_OPEN_REPLY:
                return {"clicked": True, "strategy": "parent-text-match"}
            if script == dom_actions._JS_REPLY_INPUT_ACTIVE:
                return {"active": False, "reason": "reply_input_not_activated"}
            raise AssertionError("unexpected script")

        async def wait_for_selector(self, *_args, **_kwargs):
            return None

    async def _fake_sleep(_seconds):
        return None

    monkeypatch.setattr(dom_actions.asyncio, "sleep", _fake_sleep)

    result = await dom_actions.open_reply_box(_Page(), parent_comment_text="parent")

    assert result.success is False
    assert result.reason == "reply_input_not_activated"


@pytest.mark.unit
def test_open_reply_box_parent_text_search_is_broad_and_normalized():
    script = dom_actions._JS_OPEN_REPLY

    assert "normalizedNeedle" in script
    assert "\\u200b-\\u200d\\ufeff" in script
    assert "commentContainerSelector" in script
    assert "[data-e2e*=\"comment-level\"]" in script
    assert "[class*=\"DivComment\"]" in script
    assert "parent-text-match-parent" in script
