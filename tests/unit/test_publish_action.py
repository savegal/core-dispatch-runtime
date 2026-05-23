import asyncio
import json
from types import SimpleNamespace

import pytest

import executor.publish_action as publish_action


class _FakeInput:
    async def evaluate(self, _script):
        return "hello"


class _FakeButton:
    async def bounding_box(self):
        return {"x": 100, "y": 200, "width": 80, "height": 32}

    async def scroll_into_view_if_needed(self, timeout=1500):
        return None

    async def click(self, *args, **kwargs):
        return None

    async def evaluate(self, _script):
        return None


class _FakeResponse:
    url = "https://www.tiktok.com/api/comment/publish/?aid=1988"
    status = 200

    async def text(self):
        return json.dumps({"status_code": 0, "comment": {"cid": "cid_retry_123"}})


class _FakeMouse:
    def __init__(self, page):
        self.page = page

    async def move(self, *_args, **_kwargs):
        return None

    async def down(self, *_args, **_kwargs):
        return None

    async def up(self, *_args, **_kwargs):
        self.page.click_count += 1
        if self.page.click_count == 2:
            handler = self.page.listeners.get("response")
            assert handler is not None, "response listener was removed before retry click"
            await handler(_FakeResponse())


class _FakePage:
    def __init__(self):
        self.listeners = {}
        self.removed = []
        self.click_count = 0
        self.synthetic_dispatch_count = 0
        self.mouse = _FakeMouse(self)
        self.url = "https://www.tiktok.com/@demo/video/7635267329407913230"

    def on(self, event, handler):
        self.listeners[event] = handler

    def remove_listener(self, event, handler):
        assert self.listeners.get(event) is handler
        self.removed.append(event)
        self.listeners.pop(event, None)

    def locator(self, *_args, **_kwargs):
        return SimpleNamespace(last=_FakeButton())

    async def evaluate(self, *_args, **_kwargs):
        script = str(_args[0]) if _args else ""
        if "dispatchEvent(new MouseEvent" in script:
            self.synthetic_dispatch_count += 1
            return "dispatched"
        return {}


@pytest.mark.unit
def test_extract_comment_id_rejects_zero_sentinel():
    assert publish_action._extract_comment_id({"status_code": 0, "comment": {"cid": "0"}}) is None
    assert publish_action._extract_comment_id({"data": {"comment": {"id": 0}}}) is None
    assert publish_action._extract_comment_id({"status_code": 0, "comment": {"cid": "123"}}) == "123"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_publish_keeps_response_listener_for_retry_click(monkeypatch):
    page = _FakePage()
    button = _FakeButton()

    async def _fake_sleep(_seconds):
        return None

    wait_calls = {"count": 0}

    async def _fake_wait_for(awaitable, timeout):
        wait_calls["count"] += 1
        if wait_calls["count"] == 1:
            close = getattr(awaitable, "close", None)
            if close:
                close()
            raise asyncio.TimeoutError()
        return await awaitable

    async def _fake_find_comment_input_smart(*args, **kwargs):
        return _FakeInput(), SimpleNamespace(strategy="input", elapsed_seconds=0.1, reason=None)

    async def _fake_find_post_button_smart(*args, **kwargs):
        return button, SimpleNamespace(strategy="post")

    async def _fake_wait_for_post_button_enabled(*args, **kwargs):
        return button, SimpleNamespace(strategy="post", reason=None)

    async def _fake_clear_post_click_blockers(*args, **kwargs):
        return button, {"tag": "BUTTON", "e2e": "comment-post"}, True

    async def _fake_suppress_keyboard_shortcuts_overlay(*args, **kwargs):
        return 0

    async def _fake_ensure_click_target_clear(*args, **kwargs):
        return None

    async def _fake_type_into_comment(*args, **kwargs):
        return True

    monkeypatch.setattr(publish_action.asyncio, "sleep", _fake_sleep)
    monkeypatch.setattr(publish_action.asyncio, "wait_for", _fake_wait_for)
    monkeypatch.setattr(publish_action, "suppress_keyboard_shortcuts_overlay", _fake_suppress_keyboard_shortcuts_overlay)
    monkeypatch.setattr(publish_action, "find_comment_input_smart", _fake_find_comment_input_smart)
    monkeypatch.setattr(publish_action, "find_post_button_smart", _fake_find_post_button_smart)
    monkeypatch.setattr(publish_action, "wait_for_post_button_enabled", _fake_wait_for_post_button_enabled)
    monkeypatch.setattr(publish_action, "_clear_post_click_blockers", _fake_clear_post_click_blockers)
    monkeypatch.setattr(publish_action, "ensure_click_target_clear", _fake_ensure_click_target_clear)
    monkeypatch.setattr(publish_action, "type_into_comment", _fake_type_into_comment)

    result = await publish_action._publish_via_current_input(
        page,
        "hello",
        max_total_seconds=10.0,
        correlation_id="retry_listener",
    )

    assert result.status == "success", result.error_reason
    assert result.comment_id == "cid_retry_123"
    assert result.network_captured is True
    assert page.click_count == 2
    assert page.removed == ["request", "response"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_publish_does_not_retry_click_after_synthetic_dispatch_without_network(monkeypatch):
    page = _FakePage()
    button = _FakeButton()
    wait_enabled_calls = {"count": 0}

    async def _fake_sleep(_seconds):
        return None

    async def _fake_wait_for(awaitable, timeout):
        close = getattr(awaitable, "close", None)
        if close:
            close()
        raise asyncio.TimeoutError()

    async def _fake_detect_captcha(*args, **kwargs):
        return False, None

    async def _fake_find_comment_input_smart(*args, **kwargs):
        return _FakeInput(), SimpleNamespace(strategy="input", elapsed_seconds=0.1, reason=None)

    async def _fake_find_post_button_smart(*args, **kwargs):
        return button, SimpleNamespace(strategy="post")

    async def _fake_wait_for_post_button_enabled(*args, **kwargs):
        wait_enabled_calls["count"] += 1
        return button, SimpleNamespace(strategy="post", reason=None)

    async def _fake_clear_post_click_blockers(*args, **kwargs):
        return button, {"tag": "BODY", "e2e": None}, True

    async def _fake_suppress_keyboard_shortcuts_overlay(*args, **kwargs):
        return 0

    async def _fake_ensure_click_target_clear(*args, **kwargs):
        return None

    async def _fake_type_into_comment(*args, **kwargs):
        return True

    monkeypatch.setattr(publish_action.asyncio, "sleep", _fake_sleep)
    monkeypatch.setattr(publish_action.asyncio, "wait_for", _fake_wait_for)
    monkeypatch.setattr(publish_action, "detect_captcha", _fake_detect_captcha)
    monkeypatch.setattr(publish_action, "suppress_keyboard_shortcuts_overlay", _fake_suppress_keyboard_shortcuts_overlay)
    monkeypatch.setattr(publish_action, "find_comment_input_smart", _fake_find_comment_input_smart)
    monkeypatch.setattr(publish_action, "find_post_button_smart", _fake_find_post_button_smart)
    monkeypatch.setattr(publish_action, "wait_for_post_button_enabled", _fake_wait_for_post_button_enabled)
    monkeypatch.setattr(publish_action, "_clear_post_click_blockers", _fake_clear_post_click_blockers)
    monkeypatch.setattr(publish_action, "ensure_click_target_clear", _fake_ensure_click_target_clear)
    monkeypatch.setattr(publish_action, "type_into_comment", _fake_type_into_comment)

    result = await publish_action._publish_via_current_input(
        page,
        "hello",
        max_total_seconds=10.0,
        correlation_id="synthetic_no_duplicate_retry",
    )

    # Mock returns the Post button as enabled on every wait_for_post_button_enabled
    # call, so post_was_enabled stays True. The synthetic-dispatch branch fires
    # because point_diag.tag == "BODY" forces it; with Post enabled the result
    # is the legitimate timeout (no_network_request_after_post_click), not the
    # unverified-disabled-post variant introduced for the disabled case.
    assert result.status == "timeout"
    assert result.error_reason == "no_network_request_after_post_click"
    assert result.post_was_enabled_at_click is True
    assert page.synthetic_dispatch_count == 1
    # Two probes: (1) main wait before click, (2) short pre-click re-probe after
    # blocker clearance — both legitimate.
    assert wait_enabled_calls["count"] == 2
    assert page.removed == ["request", "response"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_publish_tries_legacy_click_when_post_button_stays_disabled(monkeypatch):
    page = _FakePage()
    button = _FakeButton()

    async def _fake_sleep(_seconds):
        return None

    async def _fake_wait_for(awaitable, timeout):
        close = getattr(awaitable, "close", None)
        if close:
            close()
        raise asyncio.TimeoutError()

    async def _fake_detect_captcha(*args, **kwargs):
        return False, None

    async def _fake_find_comment_input_smart(*args, **kwargs):
        return _FakeInput(), SimpleNamespace(strategy="input", elapsed_seconds=0.1, reason=None)

    async def _fake_find_post_button_smart(*args, **kwargs):
        return button, SimpleNamespace(strategy="post")

    async def _fake_wait_for_post_button_enabled(*args, **kwargs):
        return None, SimpleNamespace(strategy=None, reason="post_button_stayed_disabled")

    async def _fake_suppress_keyboard_shortcuts_overlay(*args, **kwargs):
        return 0

    async def _fake_ensure_click_target_clear(*args, **kwargs):
        return None

    async def _fake_clear_post_click_blockers(*args, **kwargs):
        return button, {"tag": "BUTTON", "e2e": "comment-post"}, True

    async def _fake_type_into_comment(*args, **kwargs):
        return True

    monkeypatch.setattr(publish_action.asyncio, "sleep", _fake_sleep)
    monkeypatch.setattr(publish_action.asyncio, "wait_for", _fake_wait_for)
    monkeypatch.setattr(publish_action, "detect_captcha", _fake_detect_captcha)
    monkeypatch.setattr(publish_action, "suppress_keyboard_shortcuts_overlay", _fake_suppress_keyboard_shortcuts_overlay)
    monkeypatch.setattr(publish_action, "find_comment_input_smart", _fake_find_comment_input_smart)
    monkeypatch.setattr(publish_action, "find_post_button_smart", _fake_find_post_button_smart)
    monkeypatch.setattr(publish_action, "wait_for_post_button_enabled", _fake_wait_for_post_button_enabled)
    monkeypatch.setattr(publish_action, "ensure_click_target_clear", _fake_ensure_click_target_clear)
    monkeypatch.setattr(publish_action, "_clear_post_click_blockers", _fake_clear_post_click_blockers)
    monkeypatch.setattr(publish_action, "type_into_comment", _fake_type_into_comment)

    result = await publish_action._publish_via_current_input(
        page,
        "hello",
        max_total_seconds=10.0,
        correlation_id="disabled_post",
    )

    assert result.status == "failed"
    assert result.error_reason == "submitted_unverified_disabled_post"
    assert page.synthetic_dispatch_count == 0
    assert page.click_count == 1
    assert page.removed == ["request", "response"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_publish_stops_on_slider_challenge_before_typing(monkeypatch):
    page = _FakePage()
    calls = {"detect": 0}

    async def _fake_sleep(_seconds):
        return None

    async def _fake_detect_captcha(*args, **kwargs):
        calls["detect"] += 1
        return calls["detect"] >= 2, "slider"

    async def _fake_suppress_keyboard_shortcuts_overlay(*args, **kwargs):
        return 0

    async def _fake_find_comment_input_smart(*args, **kwargs):
        return _FakeInput(), SimpleNamespace(strategy="input", elapsed_seconds=0.1, reason=None)

    async def _unexpected_type_into_comment(*args, **kwargs):
        raise AssertionError("typing must not run while TikTok challenge is visible")

    async def _unexpected_ensure_click_target_clear(*args, **kwargs):
        raise AssertionError("challenge should be detected before mutating blockers")

    monkeypatch.setattr(publish_action.asyncio, "sleep", _fake_sleep)
    monkeypatch.setattr(publish_action, "_manual_captcha_wait_enabled", lambda: False)
    monkeypatch.setattr(publish_action, "detect_captcha", _fake_detect_captcha)
    monkeypatch.setattr(publish_action, "suppress_keyboard_shortcuts_overlay", _fake_suppress_keyboard_shortcuts_overlay)
    monkeypatch.setattr(publish_action, "find_comment_input_smart", _fake_find_comment_input_smart)
    monkeypatch.setattr(publish_action, "type_into_comment", _unexpected_type_into_comment)
    monkeypatch.setattr(publish_action, "ensure_click_target_clear", _unexpected_ensure_click_target_clear)

    result = await publish_action._publish_via_current_input(
        page,
        "hello",
        max_total_seconds=10.0,
        correlation_id="captcha_guard",
    )

    assert result.status == "failed"
    assert result.error_reason == "captcha_challenge:slider"
    assert calls["detect"] == 2


@pytest.mark.unit
@pytest.mark.asyncio
async def test_publish_comment_hybrid_opens_comments_before_inner_captcha_guard(monkeypatch):
    page = _FakePage()
    calls = {"open": 0}

    async def _fake_detect_captcha(*args, **kwargs):
        return True, "slider"

    async def _fake_open_comment_section(*args, **kwargs):
        calls["open"] += 1

    monkeypatch.setattr(publish_action, "_manual_captcha_wait_enabled", lambda: False)
    monkeypatch.setattr(publish_action, "detect_captcha", _fake_detect_captcha)
    monkeypatch.setattr(publish_action, "open_comment_section", _fake_open_comment_section)

    result = await publish_action.publish_comment_hybrid(
        page,
        "hello",
        max_total_seconds=10.0,
        correlation_id="captcha_preflight",
        expected_video_url=page.url,
    )

    assert result.status == "failed"
    assert result.error_reason == "captcha_challenge:slider"
    assert calls["open"] == 1


@pytest.mark.unit
@pytest.mark.asyncio
async def test_publish_reports_captcha_when_input_search_times_out(monkeypatch):
    page = _FakePage()
    calls = {"detect": 0}

    async def _fake_sleep(_seconds):
        return None

    async def _fake_detect_captcha(*args, **kwargs):
        calls["detect"] += 1
        return calls["detect"] >= 2, "slider"

    async def _fake_suppress_keyboard_shortcuts_overlay(*args, **kwargs):
        return 0

    async def _fake_find_comment_input_smart(*args, **kwargs):
        return None, SimpleNamespace(reason="timeout", redirected_to=None)

    monkeypatch.setattr(publish_action.asyncio, "sleep", _fake_sleep)
    monkeypatch.setattr(publish_action, "_manual_captcha_wait_enabled", lambda: False)
    monkeypatch.setattr(publish_action, "detect_captcha", _fake_detect_captcha)
    monkeypatch.setattr(publish_action, "suppress_keyboard_shortcuts_overlay", _fake_suppress_keyboard_shortcuts_overlay)
    monkeypatch.setattr(publish_action, "find_comment_input_smart", _fake_find_comment_input_smart)

    result = await publish_action._publish_via_current_input(
        page,
        "hello",
        max_total_seconds=10.0,
        correlation_id="captcha_after_timeout",
    )

    assert result.status == "failed"
    assert result.error_reason == "captcha_challenge:slider"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_publish_retries_input_search_after_manual_captcha_clear(monkeypatch):
    page = _FakePage()
    button = _FakeButton()
    detect_results = [
        (False, None),
        (True, "slider"),
        (False, None),
        (False, None),
        (False, None),
        (False, None),
    ]
    find_calls = {"count": 0}

    async def _fake_sleep(_seconds):
        return None

    async def _fake_detect_captcha(*args, **kwargs):
        return detect_results.pop(0) if detect_results else (False, None)

    async def _fake_open_comment_section(*args, **kwargs):
        return True

    async def _fake_find_comment_input_smart(*args, **kwargs):
        find_calls["count"] += 1
        if find_calls["count"] == 1:
            return None, SimpleNamespace(reason="timeout", redirected_to=None)
        return _FakeInput(), SimpleNamespace(strategy="input", elapsed_seconds=0.1, reason=None)

    async def _fake_find_post_button_smart(*args, **kwargs):
        return button, SimpleNamespace(strategy="post")

    async def _fake_wait_for_post_button_enabled(*args, **kwargs):
        return button, SimpleNamespace(strategy="post", reason=None)

    async def _fake_clear_post_click_blockers(*args, **kwargs):
        return button, {"tag": "BUTTON", "e2e": "comment-post"}, True

    async def _fake_suppress_keyboard_shortcuts_overlay(*args, **kwargs):
        return 0

    async def _fake_ensure_click_target_clear(*args, **kwargs):
        return None

    async def _fake_type_into_comment(*args, **kwargs):
        return True

    wait_calls = {"count": 0}

    async def _fake_wait_for(awaitable, timeout):
        wait_calls["count"] += 1
        if wait_calls["count"] == 1:
            handler = page.listeners.get("response")
            assert handler is not None
            await handler(_FakeResponse())
        return await awaitable

    monkeypatch.setattr(publish_action.asyncio, "sleep", _fake_sleep)
    monkeypatch.setattr(publish_action.asyncio, "wait_for", _fake_wait_for)
    monkeypatch.setattr(publish_action, "_manual_captcha_wait_enabled", lambda: True)
    monkeypatch.setattr(publish_action, "detect_captcha", _fake_detect_captcha)
    monkeypatch.setattr(publish_action, "open_comment_section", _fake_open_comment_section)
    monkeypatch.setattr(publish_action, "suppress_keyboard_shortcuts_overlay", _fake_suppress_keyboard_shortcuts_overlay)
    monkeypatch.setattr(publish_action, "find_comment_input_smart", _fake_find_comment_input_smart)
    monkeypatch.setattr(publish_action, "find_post_button_smart", _fake_find_post_button_smart)
    monkeypatch.setattr(publish_action, "wait_for_post_button_enabled", _fake_wait_for_post_button_enabled)
    monkeypatch.setattr(publish_action, "_clear_post_click_blockers", _fake_clear_post_click_blockers)
    monkeypatch.setattr(publish_action, "ensure_click_target_clear", _fake_ensure_click_target_clear)
    monkeypatch.setattr(publish_action, "type_into_comment", _fake_type_into_comment)

    result = await publish_action._publish_via_current_input(
        page,
        "hello",
        max_total_seconds=10.0,
        correlation_id="captcha_retry",
    )

    assert result.status == "success", result.error_reason
    assert result.comment_id == "cid_retry_123"
    assert find_calls["count"] == 2


@pytest.mark.unit
@pytest.mark.asyncio
async def test_captcha_guard_waits_for_manual_clear_in_headful(monkeypatch):
    page = _FakePage()
    detections = [(True, "slider"), (False, None), (False, None)]

    async def _fake_sleep(_seconds):
        return None

    async def _fake_detect_captcha(*args, **kwargs):
        return detections.pop(0) if detections else (False, None)

    monkeypatch.setattr(publish_action.asyncio, "sleep", _fake_sleep)
    monkeypatch.setattr(publish_action, "_manual_captcha_wait_enabled", lambda: True)
    monkeypatch.setattr(publish_action, "detect_captcha", _fake_detect_captcha)
    monkeypatch.setattr(publish_action, "MANUAL_CAPTCHA_WAIT_SECONDS", 10.0)

    result = await publish_action._captcha_result_if_present(
        page,
        0.0,
        dom_strategy="input",
    )

    assert result is None
    assert detections == []


@pytest.mark.unit
def test_point_diag_looks_like_challenge():
    assert publish_action._point_diag_looks_like_challenge(
        {"text": "Drag the slider to fit the puzzle", "aria": "", "e2e": None}
    )


@pytest.mark.unit
def test_point_diag_looks_like_wide_cookie_banner():
    assert publish_action._point_diag_looks_like_cookie_banner(
        {
            "tag": "DIV",
            "text": "Allow cookies from TikTok on this browser? Decline optional cookies Allow all",
            "aria": "",
            "e2e": None,
            "x": 0,
            "y": 904,
            "w": 1920,
            "h": 176,
        }
    )


@pytest.mark.unit
def test_point_diag_does_not_treat_top_page_container_as_cookie_banner():
    assert not publish_action._point_diag_looks_like_cookie_banner(
        {
            "tag": "DIV",
            "text": "",
            "aria": "",
            "e2e": None,
            "x": 0,
            "y": 0,
            "w": 1920,
            "h": 176,
        }
    )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_publish_reports_captcha_when_challenge_appears_before_post_button(monkeypatch):
    page = _FakePage()
    detect_results = [
        (False, None),  # initial guard
        (False, None),  # after input found
        (False, None),  # after click target clear
        (True, "slider"),  # after typing, before post button search
    ]

    async def _fake_sleep(_seconds):
        return None

    async def _fake_detect_captcha(*args, **kwargs):
        return detect_results.pop(0) if detect_results else (False, None)

    async def _fake_suppress_keyboard_shortcuts_overlay(*args, **kwargs):
        return 0

    async def _fake_find_comment_input_smart(*args, **kwargs):
        return _FakeInput(), SimpleNamespace(strategy="input", elapsed_seconds=0.1, reason=None)

    async def _fake_ensure_click_target_clear(*args, **kwargs):
        return None

    async def _fake_type_into_comment(*args, **kwargs):
        return True

    async def _unexpected_find_post_button_smart(*args, **kwargs):
        raise AssertionError("post button search should not run after captcha appears")

    monkeypatch.setattr(publish_action.asyncio, "sleep", _fake_sleep)
    monkeypatch.setattr(publish_action, "_manual_captcha_wait_enabled", lambda: False)
    monkeypatch.setattr(publish_action, "detect_captcha", _fake_detect_captcha)
    monkeypatch.setattr(publish_action, "suppress_keyboard_shortcuts_overlay", _fake_suppress_keyboard_shortcuts_overlay)
    monkeypatch.setattr(publish_action, "find_comment_input_smart", _fake_find_comment_input_smart)
    monkeypatch.setattr(publish_action, "ensure_click_target_clear", _fake_ensure_click_target_clear)
    monkeypatch.setattr(publish_action, "type_into_comment", _fake_type_into_comment)
    monkeypatch.setattr(publish_action, "find_post_button_smart", _unexpected_find_post_button_smart)

    result = await publish_action._publish_via_current_input(
        page,
        "hello",
        max_total_seconds=10.0,
        correlation_id="captcha_before_post",
    )

    assert result.status == "failed"
    assert result.error_reason == "captcha_challenge:slider"
