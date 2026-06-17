import asyncio
import json
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import executor.publish_action as publish_action


class _FakePublishRequest:
    def __init__(self, *, url="https://www.tiktok.com/api/comment/publish/?aid=1988", method="POST", post_data="text=hello"):
        self.url = url
        self.method = method
        self.post_data = post_data


class _FakePublishResponseForRequest:
    url = "https://www.tiktok.com/api/comment/publish/?aid=1988"
    status = 200

    def __init__(self, request):
        self.request = request


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

    def __init__(self, request=None):
        if request is not None:
            self.request = request

    async def text(self):
        return json.dumps({"status_code": 0, "comment": {"cid": "cid_retry_123"}})


class _EmptyBodyResponse:
    url = "https://www.tiktok.com/api/comment/publish/?aid=1988"
    status = 200

    def __init__(self, request=None):
        if request is not None:
            self.request = request

    async def text(self):
        return ""


def _emit_publish_response(page, response_factory=_FakeResponse):
    request = _FakePublishRequest()
    request_handler = page.listeners.get("request")
    response_handler = page.listeners.get("response")
    assert request_handler is not None, "request listener was removed before fake response"
    assert response_handler is not None, "response listener was removed before fake response"
    request_handler(request)
    response_handler(response_factory(request))


@pytest.mark.unit
def test_publish_response_must_match_current_publish_request():
    captured = {}
    request = _FakePublishRequest(post_data="text=current")
    late_request = _FakePublishRequest(post_data="text=old")

    late_response = _FakePublishResponseForRequest(late_request)
    assert publish_action._publish_response_matches_current_request(captured, late_response) is False
    assert captured["ignored_response_without_request"] == 1

    assert publish_action._record_publish_request(captured, request) is True
    current_response = _FakePublishResponseForRequest(request)
    assert publish_action._publish_response_matches_current_request(captured, current_response) is True

    other_response = _FakePublishResponseForRequest(late_request)
    assert publish_action._publish_response_matches_current_request(captured, other_response) is False
    assert captured["ignored_response_post_data_mismatch"] is True


class _FakeMouse:
    def __init__(self, page):
        self.page = page

    async def click(self, *_args, **_kwargs):
        return None

    async def move(self, *_args, **_kwargs):
        return None

    async def down(self, *_args, **_kwargs):
        return None

    async def up(self, *_args, **_kwargs):
        self.page.click_count += 1
        if self.page.click_count == 2:
            _emit_publish_response(self.page)


class _RequestOnlyMouse:
    def __init__(self, page):
        self.page = page

    async def click(self, *_args, **_kwargs):
        return None

    async def move(self, *_args, **_kwargs):
        return None

    async def down(self, *_args, **_kwargs):
        return None

    async def up(self, *_args, **_kwargs):
        self.page.click_count += 1
        handler = self.page.listeners.get("request")
        if handler:
            handler(SimpleNamespace(
                url="https://www.tiktok.com/api/comment/publish/?aid=1988",
                method="POST",
            ))


class _FakeKeyboard:
    async def press(self, *_args, **_kwargs):
        return None

    async def insert_text(self, *_args, **_kwargs):
        return None

    async def type(self, *_args, **_kwargs):
        return None


class _FakePage:
    def __init__(self):
        self.listeners = {}
        self.removed = []
        self.click_count = 0
        self.synthetic_dispatch_count = 0
        self.mouse = _FakeMouse(self)
        self.keyboard = _FakeKeyboard()
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
        if "no_main_comment_input_target" in script:
            return {
                "ok": True,
                "found": True,
                "x": 100,
                "y": 200,
                "rect": {"left": 80, "top": 184, "width": 160, "height": 32},
            }
        if "([replyScoped])" in script and "placeholderMarkers" in script:
            return {"exists": True, "empty": True, "text": ""}
        if "textMatchesExpected" in script and "postDisabled" in script:
            expected_arg = _args[1] if len(_args) > 1 else [""]
            expected = expected_arg[0] if isinstance(expected_arg, list) and expected_arg else ""
            return {
                "ok": True,
                "text": expected,
                "textLen": len(str(expected)),
                "postVisible": True,
                "postDisabled": False,
            }
        return {}


@pytest.mark.unit
def test_publish_logged_out_reason_uses_visible_login_counts_even_if_state_logged_in():
    reason = publish_action._login_state_logged_out_reason(
        {
            "state": "logged_in",
            "loginCount": 1,
            "loginModalCount": 0,
            "commentLoginCtaCount": 0,
            "stateIsLoginFalse": False,
        },
        phase="input_search",
    )

    assert reason == "visible_login_ui:input_search:login=1:modal=0:comment_cta=0:state_false=False"


@pytest.mark.unit
def test_extract_comment_id_rejects_zero_sentinel():
    assert publish_action._extract_comment_id({"status_code": 0, "comment": {"cid": "0"}}) is None
    assert publish_action._extract_comment_id({"data": {"comment": {"id": 0}}}) is None
    assert publish_action._extract_comment_id({"status_code": 0, "comment": {"cid": "123"}}) == "123"


@pytest.mark.unit
def test_reply_scoped_post_click_fallback_selector_excludes_global_post():
    selector = publish_action._post_click_fallback_selector(reply_scoped=True)

    assert 'data-smart-reply-post' in selector
    assert 'data-e2e="comment-post"' not in selector


@pytest.mark.unit
@pytest.mark.asyncio
async def test_clear_post_click_blockers_accepts_icon_child_inside_post_button():
    class _PointPage(_FakePage):
        async def evaluate(self, script, *args):
            script_text = str(script)
            if "tag: el.tagName" in script_text:
                return {"tag": "path", "text": "", "e2e": None, "smart": None}
            if "top === post || post.contains(top) || top.contains(post)" in script_text:
                return True
            raise AssertionError("click-through fallback should not run for a Post button child")

    page = _PointPage()

    button, diag, clear = await publish_action._clear_post_click_blockers(
        page,
        _FakeButton(),
        max_rounds=1,
        reselect_enabled=False,
        expected_text="hello",
    )

    assert isinstance(button, _FakeButton)
    assert diag["tag"] == "path"
    assert clear is True


@pytest.mark.unit
@pytest.mark.asyncio
async def test_publish_reply_hybrid_without_parent_target_does_not_click_anything(monkeypatch):
    page = _FakePage()

    async def _unexpected(*args, **kwargs):
        raise AssertionError("reply publish without a parent target must not touch the DOM")

    monkeypatch.setattr(publish_action, "open_comment_section", _unexpected)
    monkeypatch.setattr(publish_action, "open_reply_box", _unexpected)
    monkeypatch.setattr(publish_action, "_publish_via_current_input", _unexpected)

    result = await publish_action.publish_reply_hybrid(
        page,
        "reply text",
        max_total_seconds=12.0,
        correlation_id="task_1_parallel_reply_v1_a1_acc1_to_missing",
        expected_video_url=page.url,
        comments_already_open=True,
    )

    assert result.status == "dom_error"
    assert result.error_reason == "reply_parent_target_missing"
    assert result.post_was_enabled_at_click is False


@pytest.mark.unit
@pytest.mark.asyncio
async def test_publish_reply_hybrid_comment_id_without_parent_is_unconfirmed(monkeypatch):
    page = _FakePage()

    async def _fake_none(*args, **kwargs):
        return None

    async def _fake_open_reply_box(*args, **kwargs):
        return SimpleNamespace(success=True, strategy="parent-anchor", anchor_token="pa-1")

    async def _fake_publish_current_input(*args, **kwargs):
        return publish_action.PublishResult(
            status="success",
            comment_id="reply-cid",
            network_captured=True,
            network_request_attempted=True,
            network_response_json={"status_code": 0, "comment": {"cid": "reply-cid"}},
            post_was_enabled_at_click=True,
        )

    monkeypatch.setattr(publish_action, "_captcha_result_if_present", _fake_none)
    monkeypatch.setattr(publish_action, "_logged_out_publish_result_if_visible", _fake_none)
    monkeypatch.setattr(publish_action, "_publish_url_guard_result", lambda *args, **kwargs: None)
    monkeypatch.setattr(publish_action, "open_reply_box", _fake_open_reply_box)
    monkeypatch.setattr(publish_action, "_publish_via_current_input", _fake_publish_current_input)

    result = await publish_action.publish_reply_hybrid(
        page,
        "reply text",
        parent_comment_id="parent-123",
        parent_anchor_token="pa-1",
        max_total_seconds=12.0,
        correlation_id="task_1_parallel_reply_v1_a1_acc1_to_id",
        expected_video_url=page.url,
        comments_already_open=True,
    )

    assert result.status == "failed"
    assert result.comment_id == "reply-cid"
    assert result.error_reason == "reply_parent_unconfirmed_after_submit"
    assert result.network_captured is True


@pytest.mark.unit
def test_extract_reply_parent_id_accepts_root_and_reply_to_reply_fields():
    assert publish_action._extract_reply_parent_id(
        {"data": {"comment": {"root_comment_id": "root-1"}}}
    ) == "root-1"
    assert publish_action._extract_reply_parent_id(
        {"comment": {"reply_to_reply_id": "reply-parent-1"}}
    ) == "reply-parent-1"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_reply_comments_already_open_does_not_reopen_drawer_after_reply_box_failure(monkeypatch):
    page = _FakePage()
    opened = {"called": False}

    async def _fake_none(*args, **kwargs):
        return None

    async def _fake_open_reply_box(*args, **kwargs):
        return SimpleNamespace(success=False, reason="reply_button_not_found", redirected_to=None)

    async def _unexpected_open_comment_section(*args, **kwargs):
        opened["called"] = True
        raise AssertionError("open_comment_section must not run after anchored reply-box failure")

    monkeypatch.setattr(publish_action, "_captcha_result_if_present", _fake_none)
    monkeypatch.setattr(publish_action, "_logged_out_publish_result_if_visible", _fake_none)
    monkeypatch.setattr(publish_action, "_publish_url_guard_result", lambda *args, **kwargs: None)
    monkeypatch.setattr(publish_action, "open_reply_box", _fake_open_reply_box)
    monkeypatch.setattr(publish_action, "open_comment_section", _unexpected_open_comment_section)

    result = await publish_action.publish_reply_hybrid(
        page,
        "reply text",
        parent_comment_text="parent text",
        parent_anchor_token="anchor-1",
        max_total_seconds=12.0,
        correlation_id="task_1_parallel_reply_v1_a1_acc1_to_text",
        expected_video_url=page.url,
        comments_already_open=True,
    )

    assert result.status == "dom_error"
    assert result.error_reason == "reply_box_not_open:reply_button_not_found"
    assert opened["called"] is False


@pytest.mark.unit
@pytest.mark.asyncio
async def test_reply_closed_surface_normalizes_reply_box_failure(monkeypatch):
    page = _FakePage()

    async def _fake_none(*args, **kwargs):
        return None

    async def _fake_open_comment_section(*args, **kwargs):
        return True

    async def _fake_open_reply_box(*args, **kwargs):
        return SimpleNamespace(success=False, reason="reply_button_not_found", redirected_to=None)

    monkeypatch.setattr(publish_action, "_captcha_result_if_present", _fake_none)
    monkeypatch.setattr(publish_action, "_logged_out_publish_result_if_visible", _fake_none)
    monkeypatch.setattr(publish_action, "_publish_url_guard_result", lambda *args, **kwargs: None)
    monkeypatch.setattr(publish_action, "open_comment_section", _fake_open_comment_section)
    monkeypatch.setattr(publish_action, "open_reply_box", _fake_open_reply_box)

    result = await publish_action.publish_reply_hybrid(
        page,
        "reply text",
        parent_comment_text="parent text",
        max_total_seconds=12.0,
        correlation_id="legacy_reply_path",
        expected_video_url=page.url,
        comments_already_open=False,
    )

    assert result.status == "dom_error"
    assert result.error_reason == "reply_box_not_open:reply_button_not_found"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_publish_via_current_input_uses_reply_scoped_helpers(monkeypatch):
    page = _FakePage()
    button = _FakeButton()
    calls = {"reply_input": 0, "reply_post": 0}

    async def _fake_none(*args, **kwargs):
        return None

    async def _fake_sleep(*args, **kwargs):
        return None

    async def _fake_find_reply_input_smart(*args, **kwargs):
        calls["reply_input"] += 1
        assert kwargs.get("parent_anchor_token") == "anchor-1"
        return _FakeInput(), SimpleNamespace(strategy="reply-scoped-anchor-input", elapsed_seconds=0.1, reason=None)

    async def _unexpected_find_comment_input_smart(*args, **kwargs):
        raise AssertionError("generic input search should not be used in reply_scoped mode")

    async def _fake_find_reply_post_button_smart(*args, **kwargs):
        calls["reply_post"] += 1
        return button, SimpleNamespace(strategy="reply-composer", elapsed_seconds=0.1, reason=None)

    async def _fake_wait_for_post_button_enabled(*args, **kwargs):
        assert kwargs.get("reply_scoped") is True
        return button, SimpleNamespace(strategy="reply-composer", reason=None)

    async def _fake_clear_post_click_blockers(*args, **kwargs):
        return button, {"tag": "BUTTON", "e2e": "comment-post"}, True

    async def _fake_suppress_keyboard_shortcuts_overlay(*args, **kwargs):
        return 0

    async def _fake_ensure_click_target_clear(*args, **kwargs):
        return None

    async def _fake_type_into_comment(*args, **kwargs):
        return True

    monkeypatch.setattr(publish_action.asyncio, "sleep", _fake_sleep)
    monkeypatch.setattr(publish_action, "_captcha_result_if_present", _fake_none)
    monkeypatch.setattr(publish_action, "_logged_out_publish_result_if_visible", _fake_none)
    monkeypatch.setattr(publish_action, "_publish_url_guard_result", lambda *args, **kwargs: None)
    monkeypatch.setattr(publish_action, "suppress_keyboard_shortcuts_overlay", _fake_suppress_keyboard_shortcuts_overlay)
    monkeypatch.setattr(publish_action, "find_reply_input_smart", _fake_find_reply_input_smart)
    monkeypatch.setattr(publish_action, "find_comment_input_smart", _unexpected_find_comment_input_smart)
    monkeypatch.setattr(publish_action, "find_reply_post_button_smart", _fake_find_reply_post_button_smart)
    monkeypatch.setattr(publish_action, "wait_for_post_button_enabled", _fake_wait_for_post_button_enabled)
    monkeypatch.setattr(publish_action, "_clear_post_click_blockers", _fake_clear_post_click_blockers)
    monkeypatch.setattr(publish_action, "ensure_click_target_clear", _fake_ensure_click_target_clear)
    monkeypatch.setattr(publish_action, "type_into_comment", _fake_type_into_comment)

    result = await publish_action._publish_via_current_input(
        page,
        "reply text",
        max_total_seconds=12.0,
        correlation_id="scoped_reply",
        reply_scoped=True,
        parent_anchor_token="anchor-1",
    )

    assert result.status == "success", result.error_reason
    assert result.comment_id == "cid_retry_123"
    assert calls == {"reply_input": 1, "reply_post": 1}


@pytest.mark.unit
@pytest.mark.asyncio
async def test_publish_reply_hybrid_uses_fresh_anchor_from_open_reply_box(monkeypatch):
    page = _FakePage()
    captured = {}

    async def _fake_none(*args, **kwargs):
        return None

    async def _fake_open_reply_box(*args, **kwargs):
        return SimpleNamespace(
            success=True,
            strategy="comment-id-attr",
            reason=None,
            anchor_token="fresh-anchor-token",
        )

    async def _fake_publish_via_current_input(*args, **kwargs):
        captured["parent_anchor_token"] = kwargs.get("parent_anchor_token")
        captured["reply_scoped"] = kwargs.get("reply_scoped")
        return publish_action.PublishResult(
            status="success",
            comment_id="reply_cid",
            network_captured=True,
            network_request_attempted=True,
            network_response_json={"comment": {"cid": "reply_cid", "reply_id": "parent-cid"}},
            post_was_enabled_at_click=True,
        )

    monkeypatch.setattr(publish_action, "_publish_url_guard_result", lambda *args, **kwargs: None)
    monkeypatch.setattr(publish_action, "_captcha_result_if_present", _fake_none)
    monkeypatch.setattr(publish_action, "_logged_out_publish_result_if_visible", _fake_none)
    monkeypatch.setattr(publish_action, "open_reply_box", _fake_open_reply_box)
    monkeypatch.setattr(publish_action, "_publish_via_current_input", _fake_publish_via_current_input)

    result = await publish_action.publish_reply_hybrid(
        page,
        "reply text",
        parent_comment_text="parent text",
        parent_comment_id="parent-cid",
        parent_anchor_token="stale-anchor-token",
        max_total_seconds=12.0,
        correlation_id="task_1_parallel_reply_v1_a1_acc1_to_text",
        expected_video_url=page.url,
        comments_already_open=True,
    )

    assert result.status == "success"
    assert captured == {
        "parent_anchor_token": "fresh-anchor-token",
        "reply_scoped": True,
    }


@pytest.mark.unit
@pytest.mark.asyncio
async def test_publish_reply_hybrid_parallel_reply_uses_three_open_attempts_and_longer_budget(monkeypatch):
    page = _FakePage()
    captured = {}

    async def _fake_none(*args, **kwargs):
        return None

    async def _fake_wait_for(awaitable, timeout):
        captured.setdefault("timeouts", []).append(timeout)
        return await awaitable

    async def _fake_open_reply_box(*args, **kwargs):
        captured["max_attempts"] = kwargs.get("max_attempts")
        return SimpleNamespace(
            success=True,
            strategy="comment-id-attr",
            reason=None,
            anchor_token="fresh-anchor-token",
        )

    async def _fake_publish_via_current_input(*args, **kwargs):
        captured["reply_scoped"] = kwargs.get("reply_scoped")
        captured["parent_anchor_token"] = kwargs.get("parent_anchor_token")
        return publish_action.PublishResult(
            status="success",
            comment_id="reply_cid",
            network_captured=True,
            network_request_attempted=True,
            network_response_json={"comment": {"cid": "reply_cid", "reply_id": "parent-cid"}},
            post_was_enabled_at_click=True,
        )

    monkeypatch.setattr(publish_action, "_asyncio_wait_for", _fake_wait_for)
    monkeypatch.setattr(publish_action, "_publish_url_guard_result", lambda *args, **kwargs: None)
    monkeypatch.setattr(publish_action, "_captcha_result_if_present", _fake_none)
    monkeypatch.setattr(publish_action, "_logged_out_publish_result_if_visible", _fake_none)
    monkeypatch.setattr(publish_action, "open_reply_box", _fake_open_reply_box)
    monkeypatch.setattr(publish_action, "_publish_via_current_input", _fake_publish_via_current_input)

    result = await publish_action.publish_reply_hybrid(
        page,
        "reply text",
        parent_comment_text="parent text",
        parent_comment_id="parent-cid",
        parent_anchor_token="stale-anchor-token",
        max_total_seconds=30.0,
        correlation_id="task_1_parallel_reply_v1_a1_acc1_to_text",
        expected_video_url=page.url,
        comments_already_open=True,
    )

    assert result.status == "success"
    assert captured["max_attempts"] == 3
    assert 11.0 <= captured["timeouts"][0] <= 12.0
    assert captured["reply_scoped"] is True
    assert captured["parent_anchor_token"] == "fresh-anchor-token"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_publish_reply_hybrid_recovers_open_timeout_when_scoped_input_visible(monkeypatch):
    page = _FakePage()
    captured = {}

    async def _fake_none(*args, **kwargs):
        return None

    async def _timeout_open_reply_box(*args, **kwargs):
        raise asyncio.TimeoutError()

    async def _fake_find_reply_input_smart(*args, **kwargs):
        captured["recover_anchor"] = kwargs.get("parent_anchor_token")
        return _FakeInput(), SimpleNamespace(
            strategy="reply-scoped-anchor-input",
            elapsed_seconds=0.1,
            reason=None,
        )

    async def _fake_publish_via_current_input(*args, **kwargs):
        captured["reply_scoped"] = kwargs.get("reply_scoped")
        captured["publish_anchor"] = kwargs.get("parent_anchor_token")
        captured["aggressive"] = kwargs.get("aggressive")
        return publish_action.PublishResult(status="success", comment_id="reply_cid")

    monkeypatch.setattr(publish_action, "_publish_url_guard_result", lambda *args, **kwargs: None)
    monkeypatch.setattr(publish_action, "_captcha_result_if_present", _fake_none)
    monkeypatch.setattr(publish_action, "_logged_out_publish_result_if_visible", _fake_none)
    monkeypatch.setattr(publish_action, "open_reply_box", _timeout_open_reply_box)
    monkeypatch.setattr(publish_action, "find_reply_input_smart", _fake_find_reply_input_smart)
    monkeypatch.setattr(publish_action, "_publish_via_current_input", _fake_publish_via_current_input)

    result = await publish_action.publish_reply_hybrid(
        page,
        "reply text",
        parent_comment_text="parent",
        parent_anchor_token="anchor-1",
        max_total_seconds=20.0,
        correlation_id="task_1_parallel_reply_v1_a1_acc10_to_text",
        expected_video_url=page.url,
        comments_already_open=True,
    )

    assert result.status == "success"
    assert result.comment_id == "reply_cid"
    assert captured == {
        "recover_anchor": "anchor-1",
        "reply_scoped": True,
        "publish_anchor": "anchor-1",
        "aggressive": True,
    }


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
async def test_publish_via_current_input_hard_limits_slow_input_search(monkeypatch):
    page = _FakePage()

    async def _fake_none(*args, **kwargs):
        return None

    async def _slow_find_comment_input_smart(*args, **kwargs):
        await asyncio.sleep(1.0)
        return None, SimpleNamespace(strategy=None, elapsed_seconds=1.0, reason="timeout")

    async def _fake_suppress_keyboard_shortcuts_overlay(*args, **kwargs):
        return 0

    monkeypatch.setattr(publish_action, "_captcha_result_if_present", _fake_none)
    monkeypatch.setattr(publish_action, "_logged_out_publish_result_if_visible", _fake_none)
    monkeypatch.setattr(publish_action, "suppress_keyboard_shortcuts_overlay", _fake_suppress_keyboard_shortcuts_overlay)
    monkeypatch.setattr(publish_action, "find_comment_input_smart", _slow_find_comment_input_smart)

    started = time.monotonic()
    result = await publish_action._publish_via_current_input(
        page,
        "hello",
        max_total_seconds=0.1,
        correlation_id="slow_input_search",
    )

    assert result.status == "timeout"
    assert result.error_reason == "publish_deadline_exceeded:input_search"
    assert time.monotonic() - started < 0.5


@pytest.mark.unit
@pytest.mark.asyncio
async def test_publish_via_current_input_hard_limits_input_clear(monkeypatch):
    page = _FakePage()

    async def _fake_none(*args, **kwargs):
        return None

    async def _fake_suppress_keyboard_shortcuts_overlay(*args, **kwargs):
        return 0

    async def _fake_find_comment_input_smart(*args, **kwargs):
        return _FakeInput(), SimpleNamespace(strategy="input", elapsed_seconds=0.1, reason=None)

    async def _timeout_clear(*args, **kwargs):
        raise asyncio.TimeoutError()

    async def _unexpected_type(*args, **kwargs):
        raise AssertionError("typing should not start after input clear timeout")

    monkeypatch.setattr(publish_action, "_captcha_result_if_present", _fake_none)
    monkeypatch.setattr(publish_action, "_logged_out_publish_result_if_visible", _fake_none)
    monkeypatch.setattr(publish_action, "suppress_keyboard_shortcuts_overlay", _fake_suppress_keyboard_shortcuts_overlay)
    monkeypatch.setattr(publish_action, "find_comment_input_smart", _fake_find_comment_input_smart)
    monkeypatch.setattr(publish_action, "ensure_click_target_clear", _timeout_clear)
    monkeypatch.setattr(publish_action, "type_into_comment", _unexpected_type)

    result = await publish_action._publish_via_current_input(
        page,
        "hello",
        max_total_seconds=5.0,
        correlation_id="manual_comment",
    )

    assert result.status == "timeout"
    assert result.error_reason == "publish_deadline_exceeded:input_clear"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_parallel_main_publish_soft_continues_after_input_clear_timeout(monkeypatch):
    page = _FakePage()
    calls = {"clear": 0, "type": 0}

    class _ImmediateResponseMouse:
        def __init__(self, page):
            self.page = page

        async def click(self, *_args, **_kwargs):
            return None

        async def move(self, *_args, **_kwargs):
            return None

        async def down(self, *_args, **_kwargs):
            return None

        async def up(self, *_args, **_kwargs):
            self.page.click_count += 1
            _emit_publish_response(self.page)

    page.mouse = _ImmediateResponseMouse(page)

    async def _fake_none(*args, **kwargs):
        return None

    async def _fake_suppress_keyboard_shortcuts_overlay(*args, **kwargs):
        return 0

    async def _fake_find_comment_input_smart(*args, **kwargs):
        return _FakeInput(), SimpleNamespace(strategy="input", elapsed_seconds=0.1, reason=None)

    async def _timeout_clear(*args, **kwargs):
        calls["clear"] += 1
        if calls["clear"] == 1:
            raise asyncio.TimeoutError()
        return None

    async def _fake_type(*args, **kwargs):
        calls["type"] += 1
        return True

    async def _fake_find_post_button_smart(*args, **kwargs):
        return _FakeButton(), SimpleNamespace(strategy="post")

    async def _fake_wait_for_post_button_enabled(*args, **kwargs):
        return _FakeButton(), SimpleNamespace(strategy="post", reason=None)

    async def _fake_clear_post_click_blockers(*args, **kwargs):
        return _FakeButton(), {"tag": "BUTTON"}, True

    monkeypatch.setattr(publish_action, "_captcha_result_if_present", _fake_none)
    monkeypatch.setattr(publish_action, "_logged_out_publish_result_if_visible", _fake_none)
    monkeypatch.setattr(publish_action, "_publish_url_guard_result", lambda *args, **kwargs: None)
    monkeypatch.setattr(publish_action, "suppress_keyboard_shortcuts_overlay", _fake_suppress_keyboard_shortcuts_overlay)
    monkeypatch.setattr(publish_action, "find_comment_input_smart", _fake_find_comment_input_smart)
    monkeypatch.setattr(publish_action, "ensure_click_target_clear", _timeout_clear)
    monkeypatch.setattr(publish_action, "type_into_comment", _fake_type)
    monkeypatch.setattr(publish_action, "find_post_button_smart", _fake_find_post_button_smart)
    monkeypatch.setattr(publish_action, "wait_for_post_button_enabled", _fake_wait_for_post_button_enabled)
    monkeypatch.setattr(publish_action, "_clear_post_click_blockers", _fake_clear_post_click_blockers)

    result = await publish_action._publish_via_current_input(
        page,
        "hello",
        max_total_seconds=18.0,
        correlation_id="task_1_parallel_main_v1_a1_acc2",
    )

    assert result.status == "success", result.error_reason
    assert result.comment_id == "cid_retry_123"
    assert calls == {"clear": 2, "type": 1}


@pytest.mark.unit
@pytest.mark.asyncio
async def test_publish_reply_hybrid_uses_local_deadline_helpers(monkeypatch):
    page = _FakePage()
    calls = []

    async def _fake_none(*args, **kwargs):
        return None

    async def _fake_open_comment_section(*args, **kwargs):
        calls.append(("open_comments", kwargs))
        return True

    async def _fake_open_reply_box(*args, **kwargs):
        calls.append(("open_reply_box", {"args": args, "kwargs": kwargs}))
        return SimpleNamespace(success=True, strategy="reply_button", reason=None)

    async def _fake_publish_via_current_input(*args, **kwargs):
        calls.append(("publish_input", kwargs))
        assert 0 < kwargs["max_total_seconds"] <= 3.0
        return publish_action.PublishResult(status="success", comment_id="reply_cid")

    monkeypatch.setattr(publish_action, "_publish_url_guard_result", lambda *args, **kwargs: None)
    monkeypatch.setattr(publish_action, "_captcha_result_if_present", _fake_none)
    monkeypatch.setattr(publish_action, "_logged_out_publish_result_if_visible", _fake_none)
    monkeypatch.setattr(publish_action, "open_comment_section", _fake_open_comment_section)
    monkeypatch.setattr(publish_action, "open_reply_box", _fake_open_reply_box)
    monkeypatch.setattr(publish_action, "_publish_via_current_input", _fake_publish_via_current_input)

    result = await publish_action.publish_reply_hybrid(
        page,
        "reply text",
        parent_comment_text="parent",
        max_total_seconds=3.0,
        correlation_id="reply_deadline",
    )

    assert result.status == "success"
    assert result.comment_id == "reply_cid"
    assert result.dom_strategy == "reply_button"
    assert [name for name, _payload in calls] == ["open_comments", "open_reply_box", "publish_input"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_publish_reply_hybrid_uses_open_surface_fast_path(monkeypatch):
    page = _FakePage()
    calls = []

    async def _fake_none(*args, **kwargs):
        return None

    async def _fake_open_comment_section(*args, **kwargs):
        calls.append(("open_comments", kwargs))
        return True

    async def _fake_open_reply_box(*args, **kwargs):
        calls.append(("open_reply_box", {"args": args, "kwargs": kwargs}))
        return SimpleNamespace(success=True, strategy="reply_button", reason=None)

    async def _fake_publish_via_current_input(*args, **kwargs):
        calls.append(("publish_input", kwargs))
        return publish_action.PublishResult(status="success", comment_id="reply_cid")

    monkeypatch.setattr(publish_action, "_publish_url_guard_result", lambda *args, **kwargs: None)
    monkeypatch.setattr(publish_action, "_captcha_result_if_present", _fake_none)
    monkeypatch.setattr(publish_action, "_logged_out_publish_result_if_visible", _fake_none)
    monkeypatch.setattr(publish_action, "open_comment_section", _fake_open_comment_section)
    monkeypatch.setattr(publish_action, "open_reply_box", _fake_open_reply_box)
    monkeypatch.setattr(publish_action, "_publish_via_current_input", _fake_publish_via_current_input)

    result = await publish_action.publish_reply_hybrid(
        page,
        "reply text",
        parent_comment_text="parent",
        parent_anchor_token="anchor-token-1",
        max_total_seconds=10.0,
        correlation_id="reply_fast_path",
        comments_already_open=True,
    )

    assert result.status == "success"
    assert [name for name, _payload in calls] == ["open_reply_box", "publish_input"]
    assert calls[0][1]["args"][3] == "anchor-token-1"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_publish_reply_hybrid_keeps_minimum_input_budget_after_parent_open(monkeypatch):
    page = _FakePage()
    now = [100.0]
    budgets = []

    async def _fake_none(*args, **kwargs):
        return None

    async def _fake_open_reply_box(*args, **kwargs):
        now[0] += 24.5
        return SimpleNamespace(success=True, strategy="reply_button", reason=None)

    async def _fake_publish_via_current_input(*args, **kwargs):
        budgets.append(kwargs["max_total_seconds"])
        return publish_action.PublishResult(status="success", comment_id="reply_cid")

    monkeypatch.setattr(publish_action.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(publish_action, "_publish_url_guard_result", lambda *args, **kwargs: None)
    monkeypatch.setattr(publish_action, "_captcha_result_if_present", _fake_none)
    monkeypatch.setattr(publish_action, "_logged_out_publish_result_if_visible", _fake_none)
    monkeypatch.setattr(publish_action, "open_reply_box", _fake_open_reply_box)
    monkeypatch.setattr(publish_action, "_publish_via_current_input", _fake_publish_via_current_input)

    result = await publish_action.publish_reply_hybrid(
        page,
        "reply text",
        parent_comment_text="parent",
        max_total_seconds=25.0,
        correlation_id="reply_min_budget",
        comments_already_open=True,
    )

    assert result.status == "success"
    assert budgets == [8.0]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_publish_reply_hybrid_does_not_expand_small_deadline_budget(monkeypatch):
    page = _FakePage()
    now = [200.0]
    budgets = []

    async def _fake_none(*args, **kwargs):
        return None

    async def _fake_open_reply_box(*args, **kwargs):
        now[0] += 3.5
        return SimpleNamespace(success=True, strategy="reply_button", reason=None)

    async def _fake_publish_via_current_input(*args, **kwargs):
        budgets.append(kwargs["max_total_seconds"])
        return publish_action.PublishResult(status="success", comment_id="reply_cid")

    monkeypatch.setattr(publish_action.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(publish_action, "_publish_url_guard_result", lambda *args, **kwargs: None)
    monkeypatch.setattr(publish_action, "_captcha_result_if_present", _fake_none)
    monkeypatch.setattr(publish_action, "_logged_out_publish_result_if_visible", _fake_none)
    monkeypatch.setattr(publish_action, "open_reply_box", _fake_open_reply_box)
    monkeypatch.setattr(publish_action, "_publish_via_current_input", _fake_publish_via_current_input)

    result = await publish_action.publish_reply_hybrid(
        page,
        "reply text",
        parent_comment_text="parent",
        max_total_seconds=4.0,
        correlation_id="reply_small_budget",
        comments_already_open=True,
    )

    assert result.status == "success"
    assert budgets == [0.5]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_publish_reply_hybrid_uses_smaller_parallel_minimum_input_budget(monkeypatch):
    page = _FakePage()
    now = [300.0]
    budgets = []

    async def _fake_none(*args, **kwargs):
        return None

    async def _fake_open_reply_box(*args, **kwargs):
        now[0] += 24.5
        return SimpleNamespace(success=True, strategy="reply_button", reason=None)

    async def _fake_publish_via_current_input(*args, **kwargs):
        budgets.append(kwargs["max_total_seconds"])
        return publish_action.PublishResult(status="success", comment_id="reply_cid")

    monkeypatch.setattr(publish_action.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(publish_action, "_publish_url_guard_result", lambda *args, **kwargs: None)
    monkeypatch.setattr(publish_action, "_captcha_result_if_present", _fake_none)
    monkeypatch.setattr(publish_action, "_logged_out_publish_result_if_visible", _fake_none)
    monkeypatch.setattr(publish_action, "open_reply_box", _fake_open_reply_box)
    monkeypatch.setattr(publish_action, "_publish_via_current_input", _fake_publish_via_current_input)

    result = await publish_action.publish_reply_hybrid(
        page,
        "reply text",
        parent_comment_text="parent",
        max_total_seconds=25.0,
        correlation_id="task_1_parallel_reply_v1_a1_acc10",
        comments_already_open=True,
    )

    assert result.status == "success"
    assert budgets == [publish_action._PARALLEL_REPLY_INPUT_MIN_SECONDS]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_parallel_publish_returns_after_request_without_slow_retry(monkeypatch):
    page = _FakePage()
    page.mouse = _RequestOnlyMouse(page)
    button = _FakeButton()

    async def _fake_sleep(_seconds):
        return None

    async def _fake_detect_captcha(*args, **kwargs):
        return False, None

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
        max_total_seconds=30.0,
        correlation_id="task_1_parallel_reply_v1_a1_acc10",
    )

    assert result.status == "failed"
    assert result.error_reason == "composer_cleared_after_submit"
    assert result.network_request_attempted is True
    assert result.post_was_enabled_at_click is True
    assert result.composer_clean_after_submit is True
    assert page.click_count == 1
    assert page.removed == ["request", "response"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_parallel_main_request_timeout_clears_dirty_composer_after_request(monkeypatch):
    page = _FakePage()
    page.mouse = _RequestOnlyMouse(page)
    button = _FakeButton()
    events = []

    async def _fake_sleep(_seconds):
        return None

    async def _fake_detect_captcha(*args, **kwargs):
        return False, None

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
        events.append(("type", kwargs.get("fast")))
        return True

    async def _fake_composer_cleared_state(*args, **kwargs):
        return {"exists": True, "empty": False, "text": "hello"}

    async def _fake_clear_main_comment_composer_best_effort(*args, **kwargs):
        events.append(("clear", kwargs.get("reason")))
        return True

    monkeypatch.setattr(publish_action.asyncio, "sleep", _fake_sleep)
    monkeypatch.setattr(publish_action, "detect_captcha", _fake_detect_captcha)
    monkeypatch.setattr(publish_action, "suppress_keyboard_shortcuts_overlay", _fake_suppress_keyboard_shortcuts_overlay)
    monkeypatch.setattr(publish_action, "find_comment_input_smart", _fake_find_comment_input_smart)
    monkeypatch.setattr(publish_action, "find_post_button_smart", _fake_find_post_button_smart)
    monkeypatch.setattr(publish_action, "wait_for_post_button_enabled", _fake_wait_for_post_button_enabled)
    monkeypatch.setattr(publish_action, "_clear_post_click_blockers", _fake_clear_post_click_blockers)
    monkeypatch.setattr(publish_action, "ensure_click_target_clear", _fake_ensure_click_target_clear)
    monkeypatch.setattr(publish_action, "type_into_comment", _fake_type_into_comment)
    monkeypatch.setattr(publish_action, "_composer_cleared_state", _fake_composer_cleared_state)
    monkeypatch.setattr(
        publish_action,
        "_clear_main_comment_composer_best_effort",
        _fake_clear_main_comment_composer_best_effort,
    )

    result = await publish_action._publish_via_current_input(
        page,
        "hello",
        max_total_seconds=30.0,
        correlation_id="task_1_parallel_main_v1_a1_acc10",
    )

    assert result.status == "timeout"
    assert result.error_reason == "no_network_response_after_publish_request"
    assert result.network_request_attempted is True
    assert result.post_was_enabled_at_click is True
    assert page.click_count == 1
    assert events == [
        ("clear", "main_current_input_pre_type_clean"),
        ("type", True),
        ("clear", "no_network_response_after_publish_request"),
    ]
    assert page.removed == ["request", "response"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_parallel_main_empty_body_cleans_dirty_composer_and_reports_state(monkeypatch):
    page = _FakePage()
    button = _FakeButton()
    events = []

    class _EmptyBodyMouse:
        async def move(self, *_args, **_kwargs):
            return None

        async def down(self, *_args, **_kwargs):
            return None

        async def up(self, *_args, **_kwargs):
            page.click_count += 1
            _emit_publish_response(page, _EmptyBodyResponse)

    page.mouse = _EmptyBodyMouse()

    async def _fake_sleep(_seconds):
        return None

    async def _fake_detect_captcha(*args, **kwargs):
        return False, None

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
        events.append(("type", kwargs.get("fast")))
        return True

    dirty_probe_count = {"count": 0}

    async def _fake_composer_cleared_state(*args, **kwargs):
        dirty_probe_count["count"] += 1
        return {"exists": True, "empty": False, "text": "hello"}

    async def _fake_clear_main_comment_composer_best_effort(*args, **kwargs):
        events.append(("clear", kwargs.get("reason")))
        return True

    monkeypatch.setattr(publish_action.asyncio, "sleep", _fake_sleep)
    monkeypatch.setattr(publish_action, "detect_captcha", _fake_detect_captcha)
    monkeypatch.setattr(publish_action, "suppress_keyboard_shortcuts_overlay", _fake_suppress_keyboard_shortcuts_overlay)
    monkeypatch.setattr(publish_action, "find_comment_input_smart", _fake_find_comment_input_smart)
    monkeypatch.setattr(publish_action, "find_post_button_smart", _fake_find_post_button_smart)
    monkeypatch.setattr(publish_action, "wait_for_post_button_enabled", _fake_wait_for_post_button_enabled)
    monkeypatch.setattr(publish_action, "_clear_post_click_blockers", _fake_clear_post_click_blockers)
    monkeypatch.setattr(publish_action, "ensure_click_target_clear", _fake_ensure_click_target_clear)
    monkeypatch.setattr(publish_action, "type_into_comment", _fake_type_into_comment)
    monkeypatch.setattr(publish_action, "_composer_cleared_state", _fake_composer_cleared_state)
    monkeypatch.setattr(
        publish_action,
        "_clear_main_comment_composer_best_effort",
        _fake_clear_main_comment_composer_best_effort,
    )

    result = await publish_action._publish_via_current_input(
        page,
        "hello",
        max_total_seconds=30.0,
        correlation_id="task_1_parallel_main_v1_a1_acc10",
    )

    assert result.status == "failed"
    assert result.error_reason == "stealth_reject_empty_body"
    assert result.network_captured is True
    assert result.network_request_attempted is True
    assert result.composer_clean_after_submit is True
    assert page.click_count == 1
    assert dirty_probe_count["count"] >= 1
    assert ("clear", "stealth_reject_empty_body") in events
    assert page.removed == ["request", "response"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_parallel_main_pre_type_clean_blocks_placeholder_without_trusted_clear(monkeypatch):
    page = _FakePage()
    page.mouse = _RequestOnlyMouse(page)
    button = _FakeButton()
    events = []

    async def _fake_sleep(_seconds):
        return None

    async def _fake_detect_captcha(*args, **kwargs):
        return False, None

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
        events.append(("type", kwargs.get("fast")))
        return True

    async def _fake_composer_cleared_state(*args, **kwargs):
        return {"exists": True, "empty": False, "text": "Add comment..."}

    async def _fake_clear_main_comment_composer_best_effort(*args, **kwargs):
        events.append(("clear", kwargs.get("reason")))
        return False

    monkeypatch.setattr(publish_action.asyncio, "sleep", _fake_sleep)
    monkeypatch.setattr(publish_action, "detect_captcha", _fake_detect_captcha)
    monkeypatch.setattr(publish_action, "suppress_keyboard_shortcuts_overlay", _fake_suppress_keyboard_shortcuts_overlay)
    monkeypatch.setattr(publish_action, "find_comment_input_smart", _fake_find_comment_input_smart)
    monkeypatch.setattr(publish_action, "find_post_button_smart", _fake_find_post_button_smart)
    monkeypatch.setattr(publish_action, "wait_for_post_button_enabled", _fake_wait_for_post_button_enabled)
    monkeypatch.setattr(publish_action, "_clear_post_click_blockers", _fake_clear_post_click_blockers)
    monkeypatch.setattr(publish_action, "ensure_click_target_clear", _fake_ensure_click_target_clear)
    monkeypatch.setattr(publish_action, "type_into_comment", _fake_type_into_comment)
    monkeypatch.setattr(publish_action, "_composer_cleared_state", _fake_composer_cleared_state)
    monkeypatch.setattr(
        publish_action,
        "_clear_main_comment_composer_best_effort",
        _fake_clear_main_comment_composer_best_effort,
    )

    result = await publish_action._publish_via_current_input(
        page,
        "hello",
        max_total_seconds=30.0,
        correlation_id="task_1_parallel_main_v1_a1_acc10",
    )

    assert result.status == "dom_error"
    assert result.error_reason == "main_pre_type_composer_not_clean"
    assert result.network_request_attempted is False
    assert result.post_was_enabled_at_click is False
    assert events == [("clear", "main_current_input_pre_type_clean")]
    assert page.click_count == 0


@pytest.mark.unit
@pytest.mark.asyncio
async def test_parallel_main_pre_type_dirty_composer_blocks_typing(monkeypatch):
    page = _FakePage()
    events = []

    async def _fake_sleep(_seconds):
        return None

    async def _fake_detect_captcha(*args, **kwargs):
        return False, None

    async def _fake_find_comment_input_smart(*args, **kwargs):
        return _FakeInput(), SimpleNamespace(strategy="input", elapsed_seconds=0.1, reason=None)

    async def _fake_suppress_keyboard_shortcuts_overlay(*args, **kwargs):
        return 0

    async def _unexpected_type_into_comment(*args, **kwargs):
        raise AssertionError("typing must not run when main composer is still dirty")

    async def _fake_composer_cleared_state(*args, **kwargs):
        return {"exists": True, "empty": False, "text": "old draft"}

    async def _fake_clear_main_comment_composer_best_effort(*args, **kwargs):
        events.append(("clear", kwargs.get("reason")))
        return False

    monkeypatch.setattr(publish_action.asyncio, "sleep", _fake_sleep)
    monkeypatch.setattr(publish_action, "detect_captcha", _fake_detect_captcha)
    monkeypatch.setattr(publish_action, "suppress_keyboard_shortcuts_overlay", _fake_suppress_keyboard_shortcuts_overlay)
    monkeypatch.setattr(publish_action, "find_comment_input_smart", _fake_find_comment_input_smart)
    monkeypatch.setattr(publish_action, "type_into_comment", _unexpected_type_into_comment)
    monkeypatch.setattr(publish_action, "_composer_cleared_state", _fake_composer_cleared_state)
    monkeypatch.setattr(
        publish_action,
        "_clear_main_comment_composer_best_effort",
        _fake_clear_main_comment_composer_best_effort,
    )

    result = await publish_action._publish_via_current_input(
        page,
        "hello",
        max_total_seconds=30.0,
        correlation_id="task_1_parallel_main_v1_a1_acc10",
    )

    assert result.status == "dom_error"
    assert result.error_reason == "main_pre_type_composer_not_clean"
    assert result.network_request_attempted is False
    assert result.post_was_enabled_at_click is False
    assert events == [("clear", "main_current_input_pre_type_clean")]
    assert page.click_count == 0


@pytest.mark.unit
@pytest.mark.asyncio
async def test_publish_continues_when_typing_times_out_but_text_is_visible(monkeypatch):
    page = _FakePage()
    button = _FakeButton()
    calls = {"type_fast": None, "visible_probe": 0}

    class _ImmediateResponseMouse:
        def __init__(self, page):
            self.page = page

        async def click(self, *_args, **_kwargs):
            return None

        async def move(self, *_args, **_kwargs):
            return None

        async def down(self, *_args, **_kwargs):
            return None

        async def up(self, *_args, **_kwargs):
            self.page.click_count += 1
            _emit_publish_response(self.page)

    page.mouse = _ImmediateResponseMouse(page)

    async def _fake_none(*args, **kwargs):
        return None

    async def _fake_sleep(_seconds):
        return None

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
        calls["type_fast"] = kwargs.get("fast")
        raise asyncio.TimeoutError()

    async def _fake_text_visible(*args, **kwargs):
        calls["visible_probe"] += 1
        return True, "hello"

    monkeypatch.setattr(publish_action.asyncio, "sleep", _fake_sleep)
    monkeypatch.setattr(publish_action, "_captcha_result_if_present", _fake_none)
    monkeypatch.setattr(publish_action, "_logged_out_publish_result_if_visible", _fake_none)
    monkeypatch.setattr(publish_action, "_publish_url_guard_result", lambda *args, **kwargs: None)
    monkeypatch.setattr(publish_action, "suppress_keyboard_shortcuts_overlay", _fake_suppress_keyboard_shortcuts_overlay)
    monkeypatch.setattr(publish_action, "find_comment_input_smart", _fake_find_comment_input_smart)
    monkeypatch.setattr(publish_action, "find_post_button_smart", _fake_find_post_button_smart)
    monkeypatch.setattr(publish_action, "wait_for_post_button_enabled", _fake_wait_for_post_button_enabled)
    monkeypatch.setattr(publish_action, "_clear_post_click_blockers", _fake_clear_post_click_blockers)
    monkeypatch.setattr(publish_action, "ensure_click_target_clear", _fake_ensure_click_target_clear)
    monkeypatch.setattr(publish_action, "type_into_comment", _fake_type_into_comment)
    monkeypatch.setattr(publish_action, "comment_input_contains_expected_text", _fake_text_visible)

    result = await publish_action._publish_via_current_input(
        page,
        "hello",
        max_total_seconds=18.0,
        correlation_id="task_1_parallel_main_v1_a1_acc10",
    )

    assert result.status == "success", result.error_reason
    assert result.comment_id == "cid_retry_123"
    assert calls == {"type_fast": True, "visible_probe": 1}
    assert page.click_count == 1
    assert page.removed == ["request", "response"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_parallel_main_publish_continues_when_typing_timeout_has_committed_composer_text(monkeypatch):
    page = _FakePage()
    button = _FakeButton()
    calls = {"strict_probe": 0, "loose_probe": 0}

    class _ImmediateResponseMouse:
        def __init__(self, page):
            self.page = page

        async def click(self, *_args, **_kwargs):
            return None

        async def move(self, *_args, **_kwargs):
            return None

        async def down(self, *_args, **_kwargs):
            return None

        async def up(self, *_args, **_kwargs):
            self.page.click_count += 1
            _emit_publish_response(self.page)

    page.mouse = _ImmediateResponseMouse(page)

    async def _fake_none(*args, **kwargs):
        return None

    async def _fake_sleep(_seconds):
        return None

    async def _fake_find_comment_input_smart(*args, **kwargs):
        return _FakeInput(), SimpleNamespace(strategy="trusted-smart-id-input", elapsed_seconds=0.1, reason=None)

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
        raise asyncio.TimeoutError()

    async def _fake_strict_text_visible(*args, **kwargs):
        calls["strict_probe"] += 1
        return False, ""

    async def _fake_loose_text_state(*args, **kwargs):
        calls["loose_probe"] += 1
        return {
            "ok": True,
            "text": "hello",
            "textLen": 5,
            "postVisible": True,
            "postDisabled": False,
        }

    monkeypatch.setattr(publish_action.asyncio, "sleep", _fake_sleep)
    monkeypatch.setattr(publish_action, "_captcha_result_if_present", _fake_none)
    monkeypatch.setattr(publish_action, "_logged_out_publish_result_if_visible", _fake_none)
    monkeypatch.setattr(publish_action, "_publish_url_guard_result", lambda *args, **kwargs: None)
    monkeypatch.setattr(publish_action, "suppress_keyboard_shortcuts_overlay", _fake_suppress_keyboard_shortcuts_overlay)
    monkeypatch.setattr(publish_action, "find_comment_input_smart", _fake_find_comment_input_smart)
    monkeypatch.setattr(publish_action, "find_post_button_smart", _fake_find_post_button_smart)
    monkeypatch.setattr(publish_action, "wait_for_post_button_enabled", _fake_wait_for_post_button_enabled)
    monkeypatch.setattr(publish_action, "_clear_post_click_blockers", _fake_clear_post_click_blockers)
    monkeypatch.setattr(publish_action, "ensure_click_target_clear", _fake_ensure_click_target_clear)
    monkeypatch.setattr(publish_action, "type_into_comment", _fake_type_into_comment)
    monkeypatch.setattr(publish_action, "comment_input_contains_expected_text", _fake_strict_text_visible)
    monkeypatch.setattr(publish_action, "_main_comment_input_committed_text_state", _fake_loose_text_state)

    result = await publish_action._publish_via_current_input(
        page,
        "hello",
        max_total_seconds=18.0,
        correlation_id="task_1_parallel_main_v1_a1_acc10",
    )

    assert result.status == "success", result.error_reason
    assert result.comment_id == "cid_retry_123"
    # loose_probe runs twice now: once for the typing-timeout text check and once
    # for the strict pre-Post exact-text gate that guards against doubled comments.
    assert calls == {"strict_probe": 1, "loose_probe": 2}
    assert page.click_count == 1
    assert page.removed == ["request", "response"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_parallel_main_publish_does_not_post_mismatched_committed_composer_text(monkeypatch):
    page = _FakePage()
    calls = {"strict_probe": 0, "loose_probe": 0}

    async def _fake_none(*args, **kwargs):
        return None

    async def _fake_find_comment_input_smart(*args, **kwargs):
        return _FakeInput(), SimpleNamespace(strategy="trusted-smart-id-input", elapsed_seconds=0.1, reason=None)

    async def _fake_type_into_comment(*args, **kwargs):
        raise asyncio.TimeoutError()

    async def _fake_strict_text_visible(*args, **kwargs):
        calls["strict_probe"] += 1
        return False, ""

    async def _fake_loose_text_state(*args, **kwargs):
        calls["loose_probe"] += 1
        return {
            "ok": False,
            "reason": "text_mismatch",
            "text": "old variant still in composer",
            "expected": "current variant",
        }

    async def _unexpected_post_search(*args, **kwargs):
        raise AssertionError("mismatched dirty composer text must not reach Post")

    monkeypatch.setattr(publish_action, "_captcha_result_if_present", _fake_none)
    monkeypatch.setattr(publish_action, "_logged_out_publish_result_if_visible", _fake_none)
    monkeypatch.setattr(publish_action, "_publish_url_guard_result", lambda *args, **kwargs: None)
    monkeypatch.setattr(publish_action, "suppress_keyboard_shortcuts_overlay", AsyncMock(return_value=0))
    monkeypatch.setattr(publish_action, "ensure_click_target_clear", AsyncMock(return_value=None))
    monkeypatch.setattr(publish_action, "find_comment_input_smart", _fake_find_comment_input_smart)
    monkeypatch.setattr(publish_action, "type_into_comment", _fake_type_into_comment)
    monkeypatch.setattr(publish_action, "comment_input_contains_expected_text", _fake_strict_text_visible)
    monkeypatch.setattr(publish_action, "_main_comment_input_committed_text_state", _fake_loose_text_state)
    monkeypatch.setattr(publish_action, "find_post_button_smart", _unexpected_post_search)

    result = await publish_action._publish_via_current_input(
        page,
        "current variant",
        max_total_seconds=18.0,
        correlation_id="task_1_parallel_main_v1_a1_acc10",
    )

    assert result.status == "timeout"
    assert result.error_reason == "publish_deadline_exceeded:typing"
    assert calls == {"strict_probe": 1, "loose_probe": 1}
    assert page.click_count == 0


@pytest.mark.unit
@pytest.mark.asyncio
async def test_parallel_main_publish_does_not_post_concatenated_committed_text(monkeypatch):
    page = _FakePage()
    calls = {"strict_probe": 0, "loose_probe": 0}

    async def _fake_none(*args, **kwargs):
        return None

    async def _fake_find_comment_input_smart(*args, **kwargs):
        return _FakeInput(), SimpleNamespace(strategy="trusted-smart-id-input", elapsed_seconds=0.1, reason=None)

    async def _fake_type_into_comment(*args, **kwargs):
        raise asyncio.TimeoutError()

    async def _fake_strict_text_visible(*args, **kwargs):
        calls["strict_probe"] += 1
        return False, ""

    async def _fake_loose_text_state(*args, **kwargs):
        calls["loose_probe"] += 1
        return {
            "ok": True,
            "text": "old variant current variant",
            "textLen": 27,
            "postVisible": True,
            "postDisabled": False,
        }

    async def _unexpected_post_search(*args, **kwargs):
        raise AssertionError("concatenated dirty composer text must not reach Post")

    monkeypatch.setattr(publish_action, "_captcha_result_if_present", _fake_none)
    monkeypatch.setattr(publish_action, "_logged_out_publish_result_if_visible", _fake_none)
    monkeypatch.setattr(publish_action, "_publish_url_guard_result", lambda *args, **kwargs: None)
    monkeypatch.setattr(publish_action, "suppress_keyboard_shortcuts_overlay", AsyncMock(return_value=0))
    monkeypatch.setattr(publish_action, "ensure_click_target_clear", AsyncMock(return_value=None))
    monkeypatch.setattr(publish_action, "find_comment_input_smart", _fake_find_comment_input_smart)
    monkeypatch.setattr(publish_action, "type_into_comment", _fake_type_into_comment)
    monkeypatch.setattr(publish_action, "comment_input_contains_expected_text", _fake_strict_text_visible)
    monkeypatch.setattr(publish_action, "_main_comment_input_committed_text_state", _fake_loose_text_state)
    monkeypatch.setattr(publish_action, "find_post_button_smart", _unexpected_post_search)

    result = await publish_action._publish_via_current_input(
        page,
        "current variant",
        max_total_seconds=18.0,
        correlation_id="task_1_parallel_main_v1_a1_acc10",
    )

    assert result.status == "timeout"
    assert result.error_reason == "publish_deadline_exceeded:typing"
    assert calls == {"strict_probe": 1, "loose_probe": 1}
    assert page.click_count == 0


@pytest.mark.unit
@pytest.mark.asyncio
async def test_parallel_main_publish_does_not_post_when_text_visible_but_post_disabled(monkeypatch):
    page = _FakePage()
    calls = {"strict_probe": 0, "loose_probe": 0, "clear": 0, "enabled_gate": 0}

    async def _fake_none(*args, **kwargs):
        return None

    async def _fake_find_comment_input_smart(*args, **kwargs):
        return _FakeInput(), SimpleNamespace(strategy="trusted-smart-id-input", elapsed_seconds=0.1, reason=None)

    async def _fake_type_into_comment(*args, **kwargs):
        raise asyncio.TimeoutError()

    async def _fake_strict_text_visible(*args, **kwargs):
        calls["strict_probe"] += 1
        return True, "hello"

    async def _fake_loose_text_state(*args, **kwargs):
        calls["loose_probe"] += 1
        return {
            "ok": True,
            "text": "hello",
            "textLen": 5,
            "postVisible": True,
            "postDisabled": True,
        }

    async def _fake_wait_for_post_button_enabled(*args, **kwargs):
        calls["enabled_gate"] += 1
        return None, SimpleNamespace(strategy=None, reason="post_button_input_not_ready:input_empty")

    async def _fake_clear(*args, **kwargs):
        calls["clear"] += 1
        return True

    async def _unexpected_post_search(*args, **kwargs):
        raise AssertionError("disabled Post must not be clicked or searched after typing timeout")

    monkeypatch.setattr(publish_action, "_captcha_result_if_present", _fake_none)
    monkeypatch.setattr(publish_action, "_logged_out_publish_result_if_visible", _fake_none)
    monkeypatch.setattr(publish_action, "_publish_url_guard_result", lambda *args, **kwargs: None)
    monkeypatch.setattr(publish_action, "suppress_keyboard_shortcuts_overlay", AsyncMock(return_value=0))
    monkeypatch.setattr(publish_action, "ensure_click_target_clear", AsyncMock(return_value=None))
    monkeypatch.setattr(publish_action, "find_comment_input_smart", _fake_find_comment_input_smart)
    monkeypatch.setattr(publish_action, "type_into_comment", _fake_type_into_comment)
    monkeypatch.setattr(publish_action, "comment_input_contains_expected_text", _fake_strict_text_visible)
    monkeypatch.setattr(publish_action, "_main_comment_input_committed_text_state", _fake_loose_text_state)
    monkeypatch.setattr(publish_action, "wait_for_post_button_enabled", _fake_wait_for_post_button_enabled)
    monkeypatch.setattr(publish_action, "_clear_main_comment_composer_best_effort", _fake_clear)
    monkeypatch.setattr(publish_action, "find_post_button_smart", _unexpected_post_search)

    result = await publish_action._publish_via_current_input(
        page,
        "hello",
        max_total_seconds=18.0,
        correlation_id="task_1_parallel_main_v1_a1_acc10",
    )

    assert result.status == "timeout"
    assert result.error_reason == "publish_deadline_exceeded:typing"
    assert calls == {"strict_probe": 1, "loose_probe": 1, "clear": 2, "enabled_gate": 1}
    assert page.click_count == 0


@pytest.mark.unit
@pytest.mark.asyncio
async def test_publish_surface_ready_still_resolves_real_input_handle(monkeypatch):
    page = _FakePage()
    button = _FakeButton()
    calls = {"find": [], "type_handle": None}

    class _ImmediateResponseMouse:
        def __init__(self, page):
            self.page = page

        async def click(self, *_args, **_kwargs):
            return None

        async def move(self, *_args, **_kwargs):
            return None

        async def down(self, *_args, **_kwargs):
            return None

        async def up(self, *_args, **_kwargs):
            self.page.click_count += 1
            _emit_publish_response(self.page)

    page.mouse = _ImmediateResponseMouse(page)

    async def _fake_none(*args, **kwargs):
        return None

    async def _fake_sleep(_seconds):
        return None

    async def _fake_find_comment_input_smart(*args, **kwargs):
        calls["find"].append(kwargs)
        return _FakeInput(), SimpleNamespace(strategy="surface-input", elapsed_seconds=0.1, reason=None)

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

    async def _fake_type_into_comment(_page, input_handle, *_args, **kwargs):
        calls["type_handle"] = input_handle
        return True

    async def _fake_direct_dom_unavailable(*args, **kwargs):
        return None

    monkeypatch.setattr(publish_action.asyncio, "sleep", _fake_sleep)
    monkeypatch.setattr(publish_action, "_captcha_result_if_present", _fake_none)
    monkeypatch.setattr(publish_action, "_logged_out_publish_result_if_visible", _fake_none)
    monkeypatch.setattr(publish_action, "_publish_url_guard_result", lambda *args, **kwargs: None)
    monkeypatch.setattr(publish_action, "suppress_keyboard_shortcuts_overlay", _fake_suppress_keyboard_shortcuts_overlay)
    monkeypatch.setattr(publish_action, "find_comment_input_smart", _fake_find_comment_input_smart)
    monkeypatch.setattr(publish_action, "find_post_button_smart", _fake_find_post_button_smart)
    monkeypatch.setattr(publish_action, "wait_for_post_button_enabled", _fake_wait_for_post_button_enabled)
    monkeypatch.setattr(publish_action, "_clear_post_click_blockers", _fake_clear_post_click_blockers)
    monkeypatch.setattr(publish_action, "ensure_click_target_clear", _fake_ensure_click_target_clear)
    monkeypatch.setattr(publish_action, "type_into_comment", _fake_type_into_comment)
    monkeypatch.setattr(publish_action, "_publish_main_comment_direct_dom", _fake_direct_dom_unavailable)

    result = await publish_action._publish_via_current_input(
        page,
        "hello",
        max_total_seconds=18.0,
        correlation_id="task_1_parallel_main_v1_a1_acc10",
        comment_surface_ready=True,
    )

    assert result.status == "success", result.error_reason
    assert calls["find"][0]["skip_initial_open"] is True
    assert calls["find"][0]["main_comment_aggressive"] is True
    assert calls["type_handle"] is not None


@pytest.mark.unit
@pytest.mark.asyncio
async def test_parallel_main_fast_path_skips_redundant_post_enabled_rechecks(monkeypatch):
    page = _FakePage()
    button = _FakeButton()
    calls = {"wait_enabled": 0, "clear_kwargs": None}

    class _ImmediateResponseMouse:
        def __init__(self, page):
            self.page = page

        async def click(self, *_args, **_kwargs):
            return None

        async def move(self, *_args, **_kwargs):
            return None

        async def down(self, *_args, **_kwargs):
            return None

        async def up(self, *_args, **_kwargs):
            self.page.click_count += 1
            _emit_publish_response(self.page)

    page.mouse = _ImmediateResponseMouse(page)

    async def _fake_none(*args, **kwargs):
        return None

    async def _fake_sleep(_seconds):
        return None

    async def _fake_find_comment_input_smart(*args, **kwargs):
        return _FakeInput(), SimpleNamespace(strategy="input", elapsed_seconds=0.1, reason=None)

    async def _fake_find_post_button_smart(*args, **kwargs):
        return button, SimpleNamespace(strategy="post", elapsed_seconds=0.1)

    async def _fake_wait_for_post_button_enabled(*args, **kwargs):
        calls["wait_enabled"] += 1
        return button, SimpleNamespace(strategy="enabled-check", reason=None)

    async def _fake_clear_post_click_blockers(*args, **kwargs):
        calls["clear_kwargs"] = kwargs
        return button, {"tag": "BUTTON", "e2e": "comment-post"}, True

    async def _fake_suppress_keyboard_shortcuts_overlay(*args, **kwargs):
        return 0

    async def _fake_ensure_click_target_clear(*args, **kwargs):
        return None

    async def _fake_type_into_comment(*args, **kwargs):
        return True

    monkeypatch.setattr(publish_action.asyncio, "sleep", _fake_sleep)
    monkeypatch.setattr(publish_action, "_captcha_result_if_present", _fake_none)
    monkeypatch.setattr(publish_action, "_logged_out_publish_result_if_visible", _fake_none)
    monkeypatch.setattr(publish_action, "_publish_url_guard_result", lambda *args, **kwargs: None)
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
        max_total_seconds=18.0,
        correlation_id="task_1_parallel_main_v1_a1_acc10",
    )

    assert result.status == "success", result.error_reason
    assert calls["wait_enabled"] == 1
    assert calls["clear_kwargs"]["max_rounds"] == 1
    assert calls["clear_kwargs"]["reselect_enabled"] is False


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
    # because point_diag.tag == "BODY" forces it; when the composer is proven
    # cleared afterwards, the result is an ambiguous submitted state instead of
    # a retryable timeout.
    assert result.status == "failed"
    assert result.error_reason == "composer_cleared_after_submit"
    assert result.post_was_enabled_at_click is True
    assert result.composer_clean_after_submit is True
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
async def test_publish_stops_when_post_gate_reports_uncommitted_input(monkeypatch):
    page = _FakePage()
    button = _FakeButton()
    calls = {"ensure_click_target_clear": 0}

    async def _fake_sleep(_seconds):
        return None

    async def _fake_detect_captcha(*args, **kwargs):
        return False, None

    async def _fake_find_comment_input_smart(*args, **kwargs):
        return _FakeInput(), SimpleNamespace(strategy="input", elapsed_seconds=0.1, reason=None)

    async def _fake_find_post_button_smart(*args, **kwargs):
        return button, SimpleNamespace(strategy="post")

    async def _fake_wait_for_post_button_enabled(*args, **kwargs):
        return None, SimpleNamespace(strategy=None, reason="post_button_input_not_ready:input_empty")

    async def _fake_suppress_keyboard_shortcuts_overlay(*args, **kwargs):
        return 0

    async def _fake_ensure_click_target_clear(_page, target, *args, **kwargs):
        calls["ensure_click_target_clear"] += 1
        if target is button:
            raise AssertionError("disabled input gate must stop before Post click target cleanup")
        return None

    async def _fake_type_into_comment(*args, **kwargs):
        return True

    monkeypatch.setattr(publish_action.asyncio, "sleep", _fake_sleep)
    monkeypatch.setattr(publish_action, "detect_captcha", _fake_detect_captcha)
    monkeypatch.setattr(publish_action, "suppress_keyboard_shortcuts_overlay", _fake_suppress_keyboard_shortcuts_overlay)
    monkeypatch.setattr(publish_action, "find_comment_input_smart", _fake_find_comment_input_smart)
    monkeypatch.setattr(publish_action, "find_post_button_smart", _fake_find_post_button_smart)
    monkeypatch.setattr(publish_action, "wait_for_post_button_enabled", _fake_wait_for_post_button_enabled)
    monkeypatch.setattr(publish_action, "ensure_click_target_clear", _fake_ensure_click_target_clear)
    monkeypatch.setattr(publish_action, "type_into_comment", _fake_type_into_comment)

    result = await publish_action._publish_via_current_input(
        page,
        "hello",
        max_total_seconds=10.0,
        correlation_id="input_not_ready",
    )

    assert result.status == "dom_error"
    assert result.error_reason == "post_button_input_not_ready:input_empty"
    assert result.network_request_attempted is False
    assert result.post_was_enabled_at_click is False
    assert calls["ensure_click_target_clear"] == 1
    assert page.click_count == 0
    assert page.synthetic_dispatch_count == 0


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
async def test_publish_comment_hybrid_stops_on_visible_captcha_before_open(monkeypatch):
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
    assert calls["open"] == 0


@pytest.mark.unit
@pytest.mark.asyncio
async def test_publish_comment_hybrid_bounds_comment_open_by_deadline(monkeypatch):
    page = _FakePage()

    async def _fake_wait_for(awaitable, timeout):
        close = getattr(awaitable, "close", None)
        if close:
            close()
        raise asyncio.TimeoutError()

    async def _fake_open_comment_section(*args, **kwargs):
        raise AssertionError("wait_for should cancel this awaitable in the test")

    monkeypatch.setattr(publish_action, "_publish_url_guard_result", lambda *args, **kwargs: None)
    monkeypatch.setattr(publish_action, "_logged_out_publish_result_if_visible", AsyncMock(return_value=None))
    monkeypatch.setattr(publish_action, "open_comment_section", _fake_open_comment_section)
    monkeypatch.setattr(publish_action, "_asyncio_wait_for", _fake_wait_for)

    result = await publish_action.publish_comment_hybrid(
        page,
        "hello",
        max_total_seconds=2.0,
        correlation_id="main_open_deadline",
        expected_video_url=page.url,
    )

    assert result.status == "timeout"
    assert result.error_reason == "publish_deadline_exceeded:open_comments"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_publish_comment_hybrid_reuses_already_open_comments(monkeypatch):
    page = _FakePage()
    calls = {}

    async def _unexpected_open_comment_section(*args, **kwargs):
        raise AssertionError("ready comment surface should not be reopened")

    async def _fake_publish_current_input(*args, **kwargs):
        calls["comment_surface_ready"] = kwargs.get("comment_surface_ready")
        calls["aggressive"] = kwargs.get("aggressive")
        return publish_action.PublishResult(status="success", comment_id="cid-ready")

    async def _fake_surface(_page):
        return {"comment_input_visible": True}

    monkeypatch.setattr(publish_action, "_publish_url_guard_result", lambda *args, **kwargs: None)
    monkeypatch.setattr(publish_action, "_captcha_result_if_present", AsyncMock(return_value=None))
    monkeypatch.setattr(publish_action, "_logged_out_publish_result_if_visible", AsyncMock(return_value=None))
    monkeypatch.setattr(publish_action, "get_comment_surface_state", _fake_surface)
    monkeypatch.setattr(publish_action, "open_comment_section", _unexpected_open_comment_section)
    monkeypatch.setattr(publish_action, "_publish_via_current_input", _fake_publish_current_input)

    result = await publish_action.publish_comment_hybrid(
        page,
        "hello",
        max_total_seconds=10.0,
        correlation_id="task_1_parallel_main_v1_a1_acc2_input_retry",
        expected_video_url=page.url,
        comments_already_open=True,
    )

    assert result.status == "success"
    assert calls == {"comment_surface_ready": True, "aggressive": True}


@pytest.mark.unit
@pytest.mark.asyncio
async def test_publish_comment_hybrid_continues_when_open_times_out_but_input_visible(monkeypatch):
    page = _FakePage()
    calls = {}

    async def _fake_wait_for(awaitable, timeout):
        close = getattr(awaitable, "close", None)
        if close:
            close()
        raise asyncio.TimeoutError()

    async def _fake_open_comment_section(*args, **kwargs):
        raise AssertionError("wait_for should cancel this awaitable in the test")

    async def _fake_surface(_page):
        return {
            "input_visible": True,
            "comment_input_visible": True,
            "publish_input_candidate_visible": True,
        }

    async def _fake_publish_current_input(*args, **kwargs):
        calls["comment_surface_ready"] = kwargs.get("comment_surface_ready")
        return publish_action.PublishResult(status="success", comment_id="cid-visible")

    monkeypatch.setattr(publish_action, "_publish_url_guard_result", lambda *args, **kwargs: None)
    monkeypatch.setattr(publish_action, "_captcha_result_if_present", AsyncMock(return_value=None))
    monkeypatch.setattr(publish_action, "_logged_out_publish_result_if_visible", AsyncMock(return_value=None))
    monkeypatch.setattr(publish_action, "open_comment_section", _fake_open_comment_section)
    monkeypatch.setattr(publish_action, "get_comment_surface_state", _fake_surface)
    monkeypatch.setattr(publish_action, "_publish_via_current_input", _fake_publish_current_input)
    monkeypatch.setattr(publish_action, "_asyncio_wait_for", _fake_wait_for)

    result = await publish_action.publish_comment_hybrid(
        page,
        "hello",
        max_total_seconds=10.0,
        correlation_id="task_1_parallel_main_v1_a1_acc2",
        expected_video_url=page.url,
    )

    assert result.status == "success"
    assert calls["comment_surface_ready"] is True


@pytest.mark.unit
@pytest.mark.asyncio
async def test_publish_comment_hybrid_does_not_trust_raw_input_visible_on_open_timeout(monkeypatch):
    page = _FakePage()

    async def _fake_wait_for(awaitable, timeout):
        close = getattr(awaitable, "close", None)
        if close:
            close()
        raise asyncio.TimeoutError()

    async def _fake_open_comment_section(*args, **kwargs):
        raise AssertionError("wait_for should cancel this awaitable in the test")

    async def _fake_surface(_page):
        return {
            "input": True,
            "input_visible": True,
            "comment_input": False,
            "comment_input_visible": False,
            "publish_input_candidate": False,
            "publish_input_candidate_visible": False,
        }

    async def _unexpected_publish_current_input(*args, **kwargs):
        raise AssertionError("raw input_visible must not continue into typing")

    monkeypatch.setattr(publish_action, "_publish_url_guard_result", lambda *args, **kwargs: None)
    monkeypatch.setattr(publish_action, "_captcha_result_if_present", AsyncMock(return_value=None))
    monkeypatch.setattr(publish_action, "_logged_out_publish_result_if_visible", AsyncMock(return_value=None))
    monkeypatch.setattr(publish_action, "open_comment_section", _fake_open_comment_section)
    monkeypatch.setattr(publish_action, "get_comment_surface_state", _fake_surface)
    monkeypatch.setattr(publish_action, "_publish_via_current_input", _unexpected_publish_current_input)
    monkeypatch.setattr(publish_action, "_asyncio_wait_for", _fake_wait_for)

    result = await publish_action.publish_comment_hybrid(
        page,
        "hello",
        max_total_seconds=10.0,
        correlation_id="task_1_parallel_main_v1_a1_acc2",
        expected_video_url=page.url,
    )

    assert result.status == "timeout"
    assert result.error_reason == "publish_deadline_exceeded:open_comments"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_publish_comment_hybrid_uses_aggressive_publish_for_parallel_main(monkeypatch):
    page = _FakePage()
    calls = {}

    async def _fake_open_comment_section(*args, **kwargs):
        calls["open_aggressive"] = kwargs.get("aggressive")
        calls["open_max_wait"] = kwargs.get("max_wait")
        calls["open_require_input"] = kwargs.get("require_input")
        return True

    async def _fake_publish_current_input(*args, **kwargs):
        calls["publish_aggressive"] = kwargs.get("aggressive")
        calls["comment_surface_ready"] = kwargs.get("comment_surface_ready")
        return publish_action.PublishResult(status="success", comment_id="cid123")

    monkeypatch.setattr(publish_action, "_publish_url_guard_result", lambda *args, **kwargs: None)
    monkeypatch.setattr(publish_action, "_captcha_result_if_present", AsyncMock(return_value=None))
    monkeypatch.setattr(publish_action, "_logged_out_publish_result_if_visible", AsyncMock(return_value=None))
    monkeypatch.setattr(publish_action, "open_comment_section", _fake_open_comment_section)
    monkeypatch.setattr(publish_action, "_publish_via_current_input", _fake_publish_current_input)

    result = await publish_action.publish_comment_hybrid(
        page,
        "hello",
        max_total_seconds=10.0,
        correlation_id="task_1_parallel_main_v1_a1_acc2",
        expected_video_url=page.url,
    )

    assert result.status == "success"
    assert calls["open_aggressive"] is True
    assert calls["open_require_input"] is True
    assert 5.0 <= calls["open_max_wait"] <= 10.0
    assert calls["publish_aggressive"] is True
    assert calls["comment_surface_ready"] is True


@pytest.mark.unit
@pytest.mark.asyncio
async def test_publish_comment_hybrid_does_not_mark_surface_ready_when_open_returns_false(monkeypatch):
    page = _FakePage()
    calls = {}

    async def _fake_open_comment_section(*args, **kwargs):
        calls["open_called"] = True
        return False

    async def _fake_publish_current_input(*args, **kwargs):
        calls["comment_surface_ready"] = kwargs.get("comment_surface_ready")
        return publish_action.PublishResult(status="dom_error", error_reason="input_not_found:test")

    monkeypatch.setattr(publish_action, "_publish_url_guard_result", lambda *args, **kwargs: None)
    monkeypatch.setattr(publish_action, "_captcha_result_if_present", AsyncMock(return_value=None))
    monkeypatch.setattr(publish_action, "_logged_out_publish_result_if_visible", AsyncMock(return_value=None))
    monkeypatch.setattr(publish_action, "open_comment_section", _fake_open_comment_section)
    monkeypatch.setattr(publish_action, "_publish_via_current_input", _fake_publish_current_input)

    result = await publish_action.publish_comment_hybrid(
        page,
        "hello",
        max_total_seconds=10.0,
        correlation_id="task_1_parallel_main_v1_a1_acc2",
        expected_video_url=page.url,
    )

    assert result.status == "dom_error"
    assert calls == {"open_called": True, "comment_surface_ready": False}


@pytest.mark.unit
@pytest.mark.asyncio
async def test_publish_current_input_enables_aggressive_from_parallel_correlation_id(monkeypatch):
    page = _FakePage()
    calls = {}

    async def _fake_find_comment_input_smart(*args, **kwargs):
        calls["aggressive"] = kwargs.get("aggressive")
        calls["main_comment_aggressive"] = kwargs.get("main_comment_aggressive")
        return None, SimpleNamespace(reason="comment_input_open_deadline", redirected_to=None)

    monkeypatch.setattr(publish_action, "suppress_keyboard_shortcuts_overlay", AsyncMock(return_value=0))
    monkeypatch.setattr(publish_action, "_captcha_result_if_present", AsyncMock(return_value=None))
    monkeypatch.setattr(publish_action, "_logged_out_publish_result_if_visible", AsyncMock(return_value=None))
    monkeypatch.setattr(publish_action, "_publish_url_guard_result", lambda *args, **kwargs: None)
    monkeypatch.setattr(publish_action, "find_comment_input_smart", _fake_find_comment_input_smart)

    result = await publish_action._publish_via_current_input(
        page,
        "hello",
        max_total_seconds=5.0,
        correlation_id="task_1_parallel_reply_v1_a1_acc2",
    )

    assert result.status == "dom_error"
    assert result.error_reason == "input_not_found: comment_input_open_deadline"
    assert calls["aggressive"] is True
    assert calls["main_comment_aggressive"] is False


@pytest.mark.unit
@pytest.mark.asyncio
async def test_publish_current_input_gives_parallel_main_extra_hydration_time(monkeypatch):
    page = _FakePage()
    calls = {}

    async def _fake_find_comment_input_smart(*args, **kwargs):
        calls["aggressive"] = kwargs.get("aggressive")
        calls["main_comment_aggressive"] = kwargs.get("main_comment_aggressive")
        calls["max_total_seconds"] = kwargs.get("max_total_seconds")
        return None, SimpleNamespace(reason="comment_input_open_deadline", redirected_to=None)

    async def _fake_publish_main_comment_direct_dom(*args, **kwargs):
        calls["direct_previous_reason"] = kwargs.get("previous_reason")
        return None

    monkeypatch.setattr(publish_action, "suppress_keyboard_shortcuts_overlay", AsyncMock(return_value=0))
    monkeypatch.setattr(publish_action, "_captcha_result_if_present", AsyncMock(return_value=None))
    monkeypatch.setattr(publish_action, "_logged_out_publish_result_if_visible", AsyncMock(return_value=None))
    monkeypatch.setattr(publish_action, "detect_captcha", AsyncMock(return_value=(False, None)))
    monkeypatch.setattr(publish_action, "_publish_url_guard_result", lambda *args, **kwargs: None)
    monkeypatch.setattr(publish_action, "find_comment_input_smart", _fake_find_comment_input_smart)
    monkeypatch.setattr(publish_action, "_publish_main_comment_direct_dom", _fake_publish_main_comment_direct_dom)

    result = await publish_action._publish_via_current_input(
        page,
        "hello",
        max_total_seconds=10.0,
        correlation_id="task_1_parallel_main_v1_a1_acc2",
    )

    assert result.status == "dom_error"
    assert result.error_reason == "input_not_found: comment_input_open_deadline"
    assert calls["aggressive"] is True
    assert calls["main_comment_aggressive"] is True
    assert 6.8 <= calls["max_total_seconds"] <= 7.1
    assert calls["direct_previous_reason"] == "comment_input_open_deadline"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_publish_current_input_prefers_normal_search_when_surface_ready(monkeypatch):
    page = _FakePage()
    calls = {"find": 0}

    async def _fake_find_comment_input_smart(*args, **kwargs):
        calls["find"] += 1
        calls["skip_initial_open"] = kwargs.get("skip_initial_open")
        return None, SimpleNamespace(reason="comment_input_open_deadline", redirected_to=None, strategy=None)

    async def _fake_publish_main_comment_direct_dom(*args, **kwargs):
        calls["previous_reason"] = kwargs.get("previous_reason")
        calls["text"] = args[1]
        return publish_action.PublishResult(
            status="success",
            comment_id="cid_direct_fast",
            dom_strategy="main-direct-fast-test",
        )

    monkeypatch.setattr(publish_action, "suppress_keyboard_shortcuts_overlay", AsyncMock(return_value=0))
    monkeypatch.setattr(publish_action, "_captcha_result_if_present", AsyncMock(return_value=None))
    monkeypatch.setattr(publish_action, "_logged_out_publish_result_if_visible", AsyncMock(return_value=None))
    monkeypatch.setattr(publish_action, "detect_captcha", AsyncMock(return_value=(False, None)))
    monkeypatch.setattr(publish_action, "_publish_url_guard_result", lambda *args, **kwargs: None)
    monkeypatch.setattr(publish_action, "find_comment_input_smart", _fake_find_comment_input_smart)
    monkeypatch.setattr(publish_action, "_publish_main_comment_direct_dom", _fake_publish_main_comment_direct_dom)

    result = await publish_action._publish_via_current_input(
        page,
        "hello",
        max_total_seconds=10.0,
        correlation_id="task_1_parallel_main_v1_a1_acc2",
        comment_surface_ready=True,
    )

    assert result.status == "success"
    assert result.comment_id == "cid_direct_fast"
    assert calls == {
        "find": 1,
        "skip_initial_open": True,
        "previous_reason": "comment_input_open_deadline",
        "text": "hello",
    }


@pytest.mark.unit
@pytest.mark.asyncio
async def test_publish_current_input_uses_main_direct_dom_fallback_after_input_not_found(monkeypatch):
    page = _FakePage()
    calls = {}

    async def _fake_find_comment_input_smart(*args, **kwargs):
        return None, SimpleNamespace(reason="timeout", redirected_to=None, strategy=None)

    async def _fake_publish_main_comment_direct_dom(*args, **kwargs):
        calls["previous_reason"] = kwargs.get("previous_reason")
        calls["text"] = args[1]
        return publish_action.PublishResult(
            status="success",
            comment_id="cid_direct",
            dom_strategy="main-direct-test",
        )

    monkeypatch.setattr(publish_action, "suppress_keyboard_shortcuts_overlay", AsyncMock(return_value=0))
    monkeypatch.setattr(publish_action, "_captcha_result_if_present", AsyncMock(return_value=None))
    monkeypatch.setattr(publish_action, "_logged_out_publish_result_if_visible", AsyncMock(return_value=None))
    monkeypatch.setattr(publish_action, "detect_captcha", AsyncMock(return_value=(False, None)))
    monkeypatch.setattr(publish_action, "_publish_url_guard_result", lambda *args, **kwargs: None)
    monkeypatch.setattr(publish_action, "find_comment_input_smart", _fake_find_comment_input_smart)
    monkeypatch.setattr(publish_action, "_publish_main_comment_direct_dom", _fake_publish_main_comment_direct_dom)

    result = await publish_action._publish_via_current_input(
        page,
        "hello",
        max_total_seconds=10.0,
        correlation_id="task_1_parallel_main_v1_a1_acc2",
        comment_surface_ready=False,
    )

    assert result.status == "success"
    assert result.comment_id == "cid_direct"
    assert calls == {"previous_reason": "timeout", "text": "hello"}


@pytest.mark.unit
@pytest.mark.asyncio
async def test_publish_current_input_uses_main_direct_dom_fallback_after_input_search_timeout(monkeypatch):
    page = _FakePage()
    calls = {}

    async def _fake_find_comment_input_smart(*args, **kwargs):
        raise AssertionError("fake wait_for should time this coroutine out")

    async def _fake_wait_for(awaitable, timeout):
        close = getattr(awaitable, "close", None)
        if close:
            close()
        raise asyncio.TimeoutError()

    async def _fake_publish_main_comment_direct_dom(*args, **kwargs):
        calls["previous_reason"] = kwargs.get("previous_reason")
        return publish_action.PublishResult(status="success", comment_id="cid_direct_timeout")

    monkeypatch.setattr(publish_action, "suppress_keyboard_shortcuts_overlay", AsyncMock(return_value=0))
    monkeypatch.setattr(publish_action, "_captcha_result_if_present", AsyncMock(return_value=None))
    monkeypatch.setattr(publish_action, "_logged_out_publish_result_if_visible", AsyncMock(return_value=None))
    monkeypatch.setattr(publish_action, "_publish_url_guard_result", lambda *args, **kwargs: None)
    monkeypatch.setattr(publish_action, "find_comment_input_smart", _fake_find_comment_input_smart)
    monkeypatch.setattr(publish_action, "_asyncio_wait_for", _fake_wait_for)
    monkeypatch.setattr(publish_action, "_publish_main_comment_direct_dom", _fake_publish_main_comment_direct_dom)

    result = await publish_action._publish_via_current_input(
        page,
        "hello",
        max_total_seconds=10.0,
        correlation_id="task_1_parallel_main_v1_a1_acc2",
        comment_surface_ready=False,
    )

    assert result.status == "success"
    assert result.comment_id == "cid_direct_timeout"
    assert calls["previous_reason"] == "input_search_timeout"


@pytest.mark.unit
def test_main_direct_post_click_rejects_reply_scoped_inputs():
    script = publish_action._JS_MAIN_COMMENT_DIRECT_POST_CLICK

    assert 'data-smart-reply-input' in script
    assert 'data-smart-reply-composer' in script
    assert 'data-smart-reply-anchor-token' in script
    assert '[data-smart-id="comment-input"]:not([data-smart-reply-input="1"]):not([data-smart-reply-anchor-token])' in script
    assert "if (replyScoped(raw)) continue;" in script
    assert "if (replyScoped(inner)) continue;" in script


@pytest.mark.unit
@pytest.mark.asyncio
async def test_main_direct_dom_fallback_refuses_dirty_pre_type_composer(monkeypatch):
    type_calls = {"count": 0}

    async def _fake_preclean(*args, **kwargs):
        assert kwargs["reason"] == "main_direct_pre_type_clean"
        return False

    async def _unexpected_type(*args, **kwargs):
        type_calls["count"] += 1
        raise AssertionError("direct main fallback must not type into a dirty composer")

    monkeypatch.setattr(publish_action, "_ensure_main_comment_composer_clean_before_typing", _fake_preclean)
    monkeypatch.setattr(publish_action, "_type_main_comment_without_handle", _unexpected_type)

    page = _FakePage()
    result = await publish_action._publish_main_comment_direct_dom(
        page,
        "hello",
        started=time.monotonic(),
        max_total_seconds=10.0,
        expected_video_url=page.url,
        correlation_id="task_1_parallel_main_v1_a1_acc2",
        previous_reason="timeout",
    )

    assert result is not None
    assert result.status == "dom_error"
    assert result.error_reason == "main_direct_pre_type_composer_not_clean"
    assert result.network_request_attempted is False
    assert type_calls["count"] == 0


@pytest.mark.unit
@pytest.mark.asyncio
async def test_main_direct_dom_fallback_returns_success_from_publish_response(monkeypatch):
    class _DirectPage(_FakePage):
        async def evaluate(self, script, *args):
            if script == publish_action._JS_MAIN_COMMENT_DIRECT_POST_CLICK:
                _emit_publish_response(self)
                return {
                    "clicked": True,
                    "postWasEnabled": True,
                    "strategy": "direct-test-post",
                }
            return await super().evaluate(script, *args)

    page = _DirectPage()

    async def _fake_type_main_comment_without_handle(*args, **kwargs):
        return {
            "ok": True,
            "strategy": "direct-test-type",
            "text": "hello",
        }

    monkeypatch.setattr(publish_action, "_type_main_comment_without_handle", _fake_type_main_comment_without_handle)

    result = await publish_action._publish_main_comment_direct_dom(
        page,
        "hello",
        started=time.monotonic(),
        max_total_seconds=10.0,
        expected_video_url=page.url,
        correlation_id="task_1_parallel_main_v1_a1_acc2",
        previous_reason="timeout",
    )

    assert result is not None
    assert result.status == "success"
    assert result.comment_id == "cid_retry_123"
    assert result.network_captured is True
    assert result.post_was_enabled_at_click is True
    assert set(page.removed) == {"request", "response"}


@pytest.mark.unit
@pytest.mark.asyncio
async def test_type_main_comment_without_handle_uses_smart_typing_before_legacy_keyboard(monkeypatch):
    calls = {"smart": 0, "legacy": 0}

    async def _fake_type_into_comment(*args, **kwargs):
        calls["smart"] += 1
        assert kwargs["deadline"] == 123.0
        assert kwargs["reply_scoped"] is False
        return True

    async def _fake_contains_expected(*args, **kwargs):
        return True, "hello"

    async def _unexpected_legacy_keyboard(*args, **kwargs):
        calls["legacy"] += 1
        raise AssertionError("legacy mouse/keyboard path should not run after smart typing succeeds")

    monkeypatch.setattr(publish_action, "type_into_comment", _fake_type_into_comment)
    monkeypatch.setattr(publish_action, "comment_input_contains_expected_text", _fake_contains_expected)
    monkeypatch.setattr(publish_action, "_type_main_comment_by_mouse_keyboard", _unexpected_legacy_keyboard)

    result = await publish_action._type_main_comment_without_handle(
        _FakePage(),
        "hello",
        fast=True,
        deadline=123.0,
    )

    assert result == {
        "ok": True,
        "strategy": "main-direct-smart-type",
        "text": "hello",
    }
    assert calls == {"smart": 1, "legacy": 0}


@pytest.mark.unit
@pytest.mark.asyncio
async def test_type_main_comment_without_handle_rechecks_smart_typing_before_success(monkeypatch):
    calls = {"smart": 0, "probe": 0, "legacy": 0}

    async def _fake_type_into_comment(*args, **kwargs):
        calls["smart"] += 1
        return {"ok": True, "strategy": "atomic-focus-insert-text", "last_text": "hello"}

    async def _fake_contains_expected(*args, **kwargs):
        calls["probe"] += 1
        return False, "hellohello"

    async def _fake_legacy_keyboard(*args, **kwargs):
        calls["legacy"] += 1
        return {
            "ok": True,
            "strategy": "main-direct-mouse-keyboard",
            "text": "hello",
        }

    monkeypatch.setattr(publish_action, "type_into_comment", _fake_type_into_comment)
    monkeypatch.setattr(publish_action, "comment_input_contains_expected_text", _fake_contains_expected)
    monkeypatch.setattr(publish_action, "_type_main_comment_by_mouse_keyboard", _fake_legacy_keyboard)

    result = await publish_action._type_main_comment_without_handle(
        _FakePage(),
        "hello",
        fast=True,
        deadline=123.0,
    )

    assert result == {
        "ok": True,
        "strategy": "main-direct-mouse-keyboard",
        "text": "hello",
    }
    assert calls == {"smart": 1, "probe": 1, "legacy": 1}


@pytest.mark.unit
@pytest.mark.asyncio
async def test_type_reply_comment_without_handle_uses_scoped_smart_typing_first(monkeypatch):
    calls = {"smart": 0, "scoped_probe": 0, "atomic": 0}

    async def _fake_type_into_comment(*args, **kwargs):
        calls["smart"] += 1
        assert kwargs["deadline"] == 123.0
        assert kwargs["reply_scoped"] is True
        assert kwargs["anchor_token"] == "pa-1"
        return True

    async def _fake_reply_contains(*args, **kwargs):
        calls["scoped_probe"] += 1
        return True, "reply text"

    async def _unexpected_atomic(*args, **kwargs):
        calls["atomic"] += 1
        raise AssertionError("atomic fallback should not run after scoped smart typing succeeds")

    monkeypatch.setattr(publish_action, "type_into_comment", _fake_type_into_comment)
    monkeypatch.setattr(publish_action, "_reply_scoped_input_contains_expected_text", _fake_reply_contains)
    monkeypatch.setattr(publish_action, "atomic_set_comment_text", _unexpected_atomic)

    result = await publish_action._type_reply_comment_without_handle(
        _FakePage(),
        "reply text",
        parent_anchor_token="pa-1",
        fast=True,
        deadline=123.0,
    )

    assert result == {
        "ok": True,
        "strategy": "reply-direct-smart-type",
        "text": "reply text",
    }
    assert calls == {"smart": 1, "scoped_probe": 1, "atomic": 0}


@pytest.mark.unit
@pytest.mark.asyncio
async def test_type_reply_comment_without_handle_does_not_use_raw_dom_atomic_insert(monkeypatch):
    keyboard_calls = {"press": [], "insert_text": []}
    atomic_calls = []

    class _Keyboard:
        async def press(self, key):
            keyboard_calls["press"].append(key)

        async def insert_text(self, text):
            keyboard_calls["insert_text"].append(text)

    class _Page:
        keyboard = _Keyboard()

    async def _fake_type_into_comment(*args, **kwargs):
        return {
            "ok": False,
            "reason": "atomic_primary_verify_failed",
            "last_text": "@wrong_user reply text",
            "strategy": "atomic-focus-insert-text",
            "anchor_bound": True,
        }

    async def _fake_atomic(_page, text, **kwargs):
        atomic_calls.append((text, kwargs))
        if kwargs.get("focus_only"):
            return {"ok": True, "reason": "focused", "text": "", "scoped": "reply"}
        raise AssertionError("reply fallback must not use raw-DOM atomic insert")

    async def _fake_reply_contains(*args, **kwargs):
        return False, "@wrong_user reply text"

    async def _fake_suppress(_page):
        return 0

    async def _fake_sleep(_seconds):
        return None

    monkeypatch.setattr(publish_action, "type_into_comment", _fake_type_into_comment)
    monkeypatch.setattr(publish_action, "atomic_set_comment_text", _fake_atomic)
    monkeypatch.setattr(publish_action, "_reply_scoped_input_contains_expected_text", _fake_reply_contains)
    monkeypatch.setattr(publish_action, "suppress_keyboard_shortcuts_overlay", _fake_suppress)
    monkeypatch.setattr(publish_action.asyncio, "sleep", _fake_sleep)

    result = await publish_action._type_reply_comment_without_handle(
        _Page(),
        "reply text",
        parent_anchor_token="pa-1",
        fast=True,
        deadline=123.0,
    )

    assert result == {
        "ok": False,
        "reason": "reply_verify_failed_after_trusted_insert",
        "last_text": "@wrong_user reply text",
    }
    assert [kwargs for _text, kwargs in atomic_calls] == [
        {"reply_scoped": True, "anchor_token": "pa-1", "focus_only": True},
    ]
    assert keyboard_calls == {
        "press": ["Control+A", "Delete"],
        "insert_text": ["reply text"],
    }


@pytest.mark.unit
@pytest.mark.asyncio
async def test_main_direct_dom_fallback_reports_no_response_after_publish_request(monkeypatch):
    class _RequestOnlyDirectPage(_FakePage):
        async def evaluate(self, script, *args):
            if script == publish_action._JS_MAIN_COMMENT_DIRECT_POST_CLICK:
                handler = self.listeners.get("request")
                assert handler is not None
                handler(SimpleNamespace(
                    url="https://www.tiktok.com/api/comment/publish/?aid=1988",
                    method="POST",
                ))
                return {
                    "clicked": True,
                    "postWasEnabled": True,
                    "strategy": "direct-test-post",
                }
            return await super().evaluate(script, *args)

    async def _fake_type_main_comment_without_handle(*args, **kwargs):
        return {
            "ok": True,
            "strategy": "direct-test-type",
            "text": "hello",
        }

    async def _fake_composer_cleared_state(*args, **kwargs):
        return {"exists": True, "empty": False}

    clear_calls = []

    async def _fake_clear_main_comment_composer_best_effort(*args, **kwargs):
        clear_calls.append(kwargs.get("reason"))
        return True

    page = _RequestOnlyDirectPage()
    monkeypatch.setattr(publish_action, "_type_main_comment_without_handle", _fake_type_main_comment_without_handle)
    monkeypatch.setattr(publish_action, "_composer_cleared_state", _fake_composer_cleared_state)
    monkeypatch.setattr(
        publish_action,
        "_clear_main_comment_composer_best_effort",
        _fake_clear_main_comment_composer_best_effort,
    )
    monkeypatch.setattr(publish_action, "_PARALLEL_POST_RESPONSE_GRACE_SECONDS", 0.01)

    result = await publish_action._publish_main_comment_direct_dom(
        page,
        "hello",
        started=time.monotonic(),
        max_total_seconds=10.0,
        expected_video_url=page.url,
        correlation_id="task_1_parallel_main_v1_a1_acc2",
        previous_reason="timeout",
    )

    assert result is not None
    assert result.status == "timeout"
    assert result.error_reason == "no_network_response_after_main_direct_post_click"
    assert result.network_request_attempted is True
    assert result.post_was_enabled_at_click is True
    assert clear_calls == [
        "main_direct_pre_type_clean",
        "no_network_response_after_main_direct_post_click",
    ]
    assert set(page.removed) == {"request", "response"}


@pytest.mark.unit
@pytest.mark.asyncio
async def test_main_direct_dom_fallback_composer_cleared_is_unconfirmed(monkeypatch):
    class _ClearedDirectPage(_FakePage):
        async def evaluate(self, script, *args):
            if script == publish_action._JS_MAIN_COMMENT_DIRECT_POST_CLICK:
                return {
                    "clicked": True,
                    "postWasEnabled": True,
                    "strategy": "direct-test-post",
                }
            return await super().evaluate(script, *args)

    async def _fake_type_main_comment_without_handle(*args, **kwargs):
        return {
            "ok": True,
            "strategy": "direct-test-type",
            "text": "hello",
        }

    async def _fake_composer_cleared_state(*args, **kwargs):
        return {"exists": True, "empty": True, "text": ""}

    page = _ClearedDirectPage()
    monkeypatch.setattr(publish_action, "_type_main_comment_without_handle", _fake_type_main_comment_without_handle)
    monkeypatch.setattr(publish_action, "_composer_cleared_state", _fake_composer_cleared_state)

    result = await publish_action._publish_main_comment_direct_dom(
        page,
        "hello",
        started=time.monotonic(),
        max_total_seconds=10.0,
        expected_video_url=page.url,
        correlation_id="task_1_parallel_main_v1_a1_acc2",
        previous_reason="timeout",
    )

    assert result is not None
    assert result.status == "failed"
    assert result.error_reason == "main_direct_composer_cleared_after_submit"
    assert result.network_request_attempted is False
    assert result.post_was_enabled_at_click is True


@pytest.mark.unit
@pytest.mark.asyncio
async def test_reply_direct_dom_fallback_reports_no_response_after_publish_request(monkeypatch):
    class _RequestOnlyReplyPage(_FakePage):
        async def evaluate(self, script, *args):
            if script == publish_action._JS_REPLY_DIRECT_POST_CLICK:
                handler = self.listeners.get("request")
                assert handler is not None
                handler(SimpleNamespace(
                    url="https://www.tiktok.com/api/comment/publish/?aid=1988",
                    method="POST",
                ))
                return {
                    "clicked": True,
                    "postWasEnabled": True,
                    "strategy": "reply-direct-test-post",
                }
            return await super().evaluate(script, *args)

    async def _fake_type_reply_comment_without_handle(*args, **kwargs):
        return {
            "ok": True,
            "strategy": "reply-direct-test-type",
            "text": "reply",
        }

    async def _fake_composer_cleared_state(*args, **kwargs):
        return {"exists": True, "empty": False}

    clear_calls = []

    async def _fake_clear_reply_comment_composer_best_effort(*args, **kwargs):
        clear_calls.append(kwargs.get("reason"))
        return True

    page = _RequestOnlyReplyPage()
    monkeypatch.setattr(publish_action, "_type_reply_comment_without_handle", _fake_type_reply_comment_without_handle)
    monkeypatch.setattr(publish_action, "_composer_cleared_state", _fake_composer_cleared_state)
    monkeypatch.setattr(
        publish_action,
        "_clear_reply_comment_composer_best_effort",
        _fake_clear_reply_comment_composer_best_effort,
    )
    monkeypatch.setattr(publish_action, "_PARALLEL_POST_RESPONSE_GRACE_SECONDS", 0.01)

    result = await publish_action._publish_reply_comment_direct_dom(
        page,
        "reply",
        started=time.monotonic(),
        max_total_seconds=10.0,
        expected_video_url=page.url,
        correlation_id="task_1_parallel_reply_v1_a1_acc2",
        parent_anchor_token="pa-1",
        previous_reason="timeout",
    )

    assert result is not None
    assert result.status == "timeout"
    assert result.error_reason == "no_network_response_after_publish_request"
    assert result.network_request_attempted is True
    assert result.post_was_enabled_at_click is True
    assert clear_calls == ["no_network_response_after_publish_request"]
    assert set(page.removed) == {"request", "response"}


@pytest.mark.unit
@pytest.mark.asyncio
async def test_reply_direct_dom_fallback_composer_cleared_is_unconfirmed(monkeypatch):
    class _ClearedReplyPage(_FakePage):
        async def evaluate(self, script, *args):
            if script == publish_action._JS_REPLY_DIRECT_POST_CLICK:
                return {
                    "clicked": True,
                    "postWasEnabled": True,
                    "strategy": "reply-direct-test-post",
                }
            return await super().evaluate(script, *args)

    async def _fake_type_reply_comment_without_handle(*args, **kwargs):
        return {
            "ok": True,
            "strategy": "reply-direct-test-type",
            "text": "reply",
        }

    async def _fake_composer_cleared_state(*args, **kwargs):
        return {"exists": True, "empty": True, "text": ""}

    page = _ClearedReplyPage()
    monkeypatch.setattr(publish_action, "_type_reply_comment_without_handle", _fake_type_reply_comment_without_handle)
    monkeypatch.setattr(publish_action, "_composer_cleared_state", _fake_composer_cleared_state)

    result = await publish_action._publish_reply_comment_direct_dom(
        page,
        "reply",
        started=time.monotonic(),
        max_total_seconds=10.0,
        expected_video_url=page.url,
        correlation_id="task_1_parallel_reply_v1_a1_acc2",
        parent_anchor_token="pa-1",
        previous_reason="timeout",
    )

    assert result is not None
    assert result.status == "failed"
    assert result.error_reason == "reply_direct_composer_cleared_after_submit"
    assert result.network_request_attempted is False
    assert result.post_was_enabled_at_click is True


@pytest.mark.unit
@pytest.mark.asyncio
async def test_reply_direct_dom_fallback_no_id_with_api_error_is_not_unconfirmed(monkeypatch):
    class _ErrorNoIdResponse:
        url = "https://www.tiktok.com/api/comment/publish/?aid=1988"
        status = 200

        def __init__(self, request=None):
            if request is not None:
                self.request = request

        async def text(self):
            return json.dumps({"status_code": 1001, "status_msg": "blocked"})

    class _ErrorNoIdReplyPage(_FakePage):
        async def evaluate(self, script, *args):
            if script == publish_action._JS_REPLY_DIRECT_POST_CLICK:
                _emit_publish_response(self, _ErrorNoIdResponse)
                return {
                    "clicked": True,
                    "postWasEnabled": True,
                    "strategy": "reply-direct-test-post",
                }
            return await super().evaluate(script, *args)

    async def _fake_type_reply_comment_without_handle(*args, **kwargs):
        return {
            "ok": True,
            "strategy": "reply-direct-test-type",
            "text": "reply",
        }

    page = _ErrorNoIdReplyPage()
    monkeypatch.setattr(publish_action, "_type_reply_comment_without_handle", _fake_type_reply_comment_without_handle)

    result = await publish_action._publish_reply_comment_direct_dom(
        page,
        "reply",
        started=time.monotonic(),
        max_total_seconds=10.0,
        expected_video_url=page.url,
        correlation_id="task_1_parallel_reply_v1_a1_acc2",
        parent_anchor_token="pa-1",
        previous_reason="timeout",
    )

    assert result is not None
    assert result.status == "failed"
    assert result.error_reason == "api_error_status_code=1001"
    assert result.network_request_attempted is True
    assert publish_action._extract_tiktok_status_code(result.network_response_json) == 1001


@pytest.mark.unit
@pytest.mark.asyncio
async def test_publish_current_input_uses_reply_direct_dom_fallback_after_input_not_found(monkeypatch):
    page = _FakePage()
    calls = {}

    async def _fake_find_reply_input_smart(*args, **kwargs):
        return None, SimpleNamespace(reason="timeout", redirected_to=None, strategy=None)

    async def _fake_publish_reply_comment_direct_dom(*args, **kwargs):
        calls["previous_reason"] = kwargs.get("previous_reason")
        calls["parent_anchor_token"] = kwargs.get("parent_anchor_token")
        calls["text"] = args[1]
        return publish_action.PublishResult(
            status="success",
            comment_id="cid_reply_direct",
            dom_strategy="reply-direct-test",
        )

    monkeypatch.setattr(publish_action, "suppress_keyboard_shortcuts_overlay", AsyncMock(return_value=0))
    monkeypatch.setattr(publish_action, "_captcha_result_if_present", AsyncMock(return_value=None))
    monkeypatch.setattr(publish_action, "_logged_out_publish_result_if_visible", AsyncMock(return_value=None))
    monkeypatch.setattr(publish_action, "detect_captcha", AsyncMock(return_value=(False, None)))
    monkeypatch.setattr(publish_action, "_publish_url_guard_result", lambda *args, **kwargs: None)
    monkeypatch.setattr(publish_action, "find_reply_input_smart", _fake_find_reply_input_smart)
    monkeypatch.setattr(publish_action, "_publish_reply_comment_direct_dom", _fake_publish_reply_comment_direct_dom)

    result = await publish_action._publish_via_current_input(
        page,
        "reply text",
        max_total_seconds=10.0,
        correlation_id="task_1_parallel_reply_v1_a1_acc2",
        reply_scoped=True,
        parent_anchor_token="pa-1",
    )

    assert result.status == "success"
    assert result.comment_id == "cid_reply_direct"
    assert calls == {
        "previous_reason": "timeout",
        "parent_anchor_token": "pa-1",
        "text": "reply text",
    }


@pytest.mark.unit
@pytest.mark.asyncio
async def test_publish_current_input_uses_reply_direct_dom_fallback_after_input_search_timeout(monkeypatch):
    page = _FakePage()
    calls = {}

    async def _fake_find_reply_input_smart(*args, **kwargs):
        raise AssertionError("fake wait_for should time this coroutine out")

    async def _fake_wait_for(awaitable, timeout):
        close = getattr(awaitable, "close", None)
        if close:
            close()
        raise asyncio.TimeoutError()

    async def _fake_publish_reply_comment_direct_dom(*args, **kwargs):
        calls["previous_reason"] = kwargs.get("previous_reason")
        calls["parent_anchor_token"] = kwargs.get("parent_anchor_token")
        return publish_action.PublishResult(status="success", comment_id="cid_reply_direct_timeout")

    monkeypatch.setattr(publish_action, "suppress_keyboard_shortcuts_overlay", AsyncMock(return_value=0))
    monkeypatch.setattr(publish_action, "_captcha_result_if_present", AsyncMock(return_value=None))
    monkeypatch.setattr(publish_action, "_logged_out_publish_result_if_visible", AsyncMock(return_value=None))
    monkeypatch.setattr(publish_action, "_publish_url_guard_result", lambda *args, **kwargs: None)
    monkeypatch.setattr(publish_action, "find_reply_input_smart", _fake_find_reply_input_smart)
    monkeypatch.setattr(publish_action, "_asyncio_wait_for", _fake_wait_for)
    monkeypatch.setattr(publish_action, "_publish_reply_comment_direct_dom", _fake_publish_reply_comment_direct_dom)

    result = await publish_action._publish_via_current_input(
        page,
        "reply text",
        max_total_seconds=10.0,
        correlation_id="task_1_parallel_reply_v1_a1_acc2",
        reply_scoped=True,
        parent_anchor_token="pa-2",
    )

    assert result.status == "success"
    assert result.comment_id == "cid_reply_direct_timeout"
    assert calls == {
        "previous_reason": "input_search_timeout",
        "parent_anchor_token": "pa-2",
    }


@pytest.mark.unit
@pytest.mark.asyncio
async def test_publish_current_input_does_not_use_reply_direct_fallback_without_anchor(monkeypatch):
    page = _FakePage()
    calls = {"direct": 0}

    async def _fake_find_reply_input_smart(*args, **kwargs):
        return None, SimpleNamespace(reason="timeout", redirected_to=None, strategy=None)

    async def _fake_publish_reply_comment_direct_dom(*args, **kwargs):
        calls["direct"] += 1
        return publish_action.PublishResult(status="success", comment_id="should_not_happen")

    monkeypatch.setattr(publish_action, "suppress_keyboard_shortcuts_overlay", AsyncMock(return_value=0))
    monkeypatch.setattr(publish_action, "_captcha_result_if_present", AsyncMock(return_value=None))
    monkeypatch.setattr(publish_action, "_logged_out_publish_result_if_visible", AsyncMock(return_value=None))
    monkeypatch.setattr(publish_action, "detect_captcha", AsyncMock(return_value=(False, None)))
    monkeypatch.setattr(publish_action, "_publish_url_guard_result", lambda *args, **kwargs: None)
    monkeypatch.setattr(publish_action, "find_reply_input_smart", _fake_find_reply_input_smart)
    monkeypatch.setattr(publish_action, "_publish_reply_comment_direct_dom", _fake_publish_reply_comment_direct_dom)

    result = await publish_action._publish_via_current_input(
        page,
        "reply text",
        max_total_seconds=10.0,
        correlation_id="task_1_parallel_reply_v1_a1_acc2",
        reply_scoped=True,
        parent_anchor_token=None,
    )

    assert result.status == "dom_error"
    assert result.error_reason == "input_not_found: timeout"
    assert calls["direct"] == 0


@pytest.mark.unit
@pytest.mark.asyncio
async def test_reply_direct_dom_fallback_returns_success_from_publish_response(monkeypatch):
    class _ReplyDirectPage(_FakePage):
        async def evaluate(self, script, *args):
            if script == publish_action._JS_REPLY_DIRECT_POST_CLICK:
                _emit_publish_response(self)
                return {
                    "clicked": True,
                    "postWasEnabled": True,
                    "strategy": "reply-direct-test-post",
                }
            return await super().evaluate(script, *args)

    page = _ReplyDirectPage()

    async def _fake_type_reply_comment_without_handle(*args, **kwargs):
        return {
            "ok": True,
            "strategy": "reply-direct-test-type",
            "text": "reply text",
        }

    monkeypatch.setattr(publish_action, "_type_reply_comment_without_handle", _fake_type_reply_comment_without_handle)

    result = await publish_action._publish_reply_comment_direct_dom(
        page,
        "reply text",
        started=time.monotonic(),
        max_total_seconds=10.0,
        expected_video_url=page.url,
        correlation_id="task_1_parallel_reply_v1_a1_acc2",
        parent_anchor_token="pa-3",
        previous_reason="timeout",
    )

    assert result is not None
    assert result.status == "success"
    assert result.comment_id == "cid_retry_123"
    assert result.network_captured is True
    assert result.post_was_enabled_at_click is True
    assert set(page.removed) == {"request", "response"}


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
            _emit_publish_response(page)
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
def test_manual_captcha_wait_disabled_by_default(monkeypatch):
    monkeypatch.delenv("ALLOW_MANUAL_CAPTCHA_SOLVE", raising=False)

    assert publish_action._manual_captcha_wait_enabled() is False


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
