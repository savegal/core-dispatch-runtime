import pytest
import inspect

from executor.comment_outcome import _extract_publish_response_fields
import executor.comment_outcome as comment_outcome


@pytest.mark.unit
def test_extract_publish_fields_top_level_comment():
    payload = {
        "status_code": 0,
        "status_msg": "ok",
        "comment": {
            "cid": "c123",
            "reply_id": "p456",
        },
    }

    extracted = _extract_publish_response_fields(payload)

    assert extracted["status_code"] == 0
    assert extracted["status_msg"] == "ok"
    assert extracted["comment_id"] == "c123"
    assert extracted["parent_comment_id"] == "p456"


@pytest.mark.unit
def test_extract_publish_fields_nested_data_comment():
    payload = {
        "data": {
            "status_code": 0,
            "status_msg": "sent",
            "comment": {
                "comment_id": "cid_nested",
                "parent_comment_id": "parent_nested",
            },
        }
    }

    extracted = _extract_publish_response_fields(payload)

    assert extracted["status_code"] == 0
    assert extracted["status_msg"] == "sent"
    assert extracted["comment_id"] == "cid_nested"
    assert extracted["parent_comment_id"] == "parent_nested"


@pytest.mark.unit
def test_extract_publish_fields_keeps_status_without_comment_id():
    payload = {
        "status_code": 4,
        "status_msg": "rate limited",
        "data": {"reply_id": "0"},
    }

    extracted = _extract_publish_response_fields(payload)

    assert extracted["status_code"] == 4
    assert extracted["status_msg"] == "rate limited"
    assert extracted["comment_id"] is None
    assert extracted["parent_comment_id"] is None


@pytest.mark.unit
def test_parent_search_uses_data_id_only_when_it_looks_like_tiktok_cid():
    joined = inspect.getsource(comment_outcome.find_comments_in_list_batch)

    assert "const dataId = el.getAttribute('data-id');" in joined
    assert "/^\\\\d{10,30}$/.test" in joined
    assert "const cidSelector = '[data-cid], [data-comment-id], [id^=\"comment-\"], [data-id]';" in joined


@pytest.mark.unit
def test_parent_search_wheel_nudges_selected_scroll_target_first():
    joined = inspect.getsource(comment_outcome.find_comments_in_list_batch)

    assert "nudge_target = scroll_target if scroll_target is not None else comment_list" in joined


@pytest.mark.unit
def test_parent_search_marks_rows_with_dom_anchor_tokens():
    joined = inspect.getsource(comment_outcome.find_comments_in_list_batch)

    assert "data-smart-parent-anchor" in joined
    assert "getAttribute('data-smart-parent-anchor')" in joined
    assert '"anchor_token": row.get("anchorToken")' in joined


@pytest.mark.unit
def test_batch_search_partial_return_waits_for_all_targets():
    joined = inspect.getsource(comment_outcome.find_comments_in_list_batch)

    # The premature partial-return shortcut (which abandoned mains further down the
    # list after only ~2 extra scrolls) must be gone, so repliers no longer skip
    # main comments.
    assert "partial_extra_scrolls" not in joined
    assert "partial_found_at_scroll" not in joined
    # The batch search now terminates early only once EVERY target has been found.
    assert "if len(found) >= len(targets):" in joined


@pytest.mark.unit
@pytest.mark.asyncio
async def test_batch_search_partial_return_keeps_found_anchor_after_extra_scans(monkeypatch):
    async def fake_detect_captcha(_page, wait_ms=0):
        return False, None

    class _Scope:
        def __init__(self):
            self.top = 0

        async def evaluate(self, script, *_args):
            if "scrollTop += " in script:
                self.top += 100
                return None
            if "=> ({top:" in script:
                return {"top": self.top, "height": 1000, "client": 500}
            return None

    class _Page:
        url = "https://www.tiktok.com/@u/video/123"

        def __init__(self):
            self.scope = _Scope()
            self.scan_calls = 0

        async def evaluate(self, script, *_args, **_kwargs):
            if "({selectors, strictScope})" in script:
                return {
                    "selector": '[data-smart-comment-surface-opened="1"]',
                    "scroll_container_tag": "DIV",
                    "scroll_container_scrollable": True,
                    "items": 4,
                    "strong_items": 4,
                    "score": 4000,
                }
            self.scan_calls += 1
            return {
                "rows": [
                    {
                        "position": 0,
                        "text": "main one",
                        "selector": '[data-e2e="comment-level-1"]',
                        "cid": None,
                        "author": None,
                        "scrollTop": self.scope.top,
                        "anchorToken": f"anchor-{self.scan_calls}",
                    }
                ],
                "scrollTop": self.scope.top,
                "scopeMissing": False,
            }

        async def query_selector(self, selector):
            if selector in {
                '[data-smart-comment-list-batch-scope="1"]',
                '[data-smart-comment-list-batch-scroll="1"]',
            }:
                return self.scope
            return None

    monkeypatch.setattr(comment_outcome, "detect_captcha", fake_detect_captcha)

    found, info = await comment_outcome.find_comments_in_list_batch(
        _Page(),
        ["main one", "main two"],
        max_scrolls=5,
        scroll_delay_ms=0,
        ensure_open=False,
        strict_scope=True,
        return_partial_on_found=True,
    )

    assert set(found) == {0}
    assert found[0]["text_normalized"] == "main one"
    assert found[0]["anchor_token"] == "anchor-1"
    assert info["found_count"] == 1
    assert info["missing_count"] == 1
    assert info["matched_indexes"] == [0]
    assert info["missing_indexes"] == [1]
    assert info["missing_previews"][0]["text_preview"] == "main two"
    # No early partial-return: the search now scans the full budget looking for the
    # still-missing target instead of bailing out after a couple of extra scrolls.
    assert info["scrolls"] == 5


@pytest.mark.unit
@pytest.mark.asyncio
async def test_batch_search_returns_partial_for_mid_sized_missing_set(monkeypatch):
    async def fake_detect_captcha(_page, wait_ms=0):
        return False, None

    class _Scope:
        def __init__(self):
            self.top = 0

        async def evaluate(self, script, *_args):
            if "scrollTop += " in script:
                self.top += 100
                return None
            if "=> ({top:" in script:
                return {"top": self.top, "height": 1000, "client": 500}
            return None

    class _Page:
        url = "https://www.tiktok.com/@u/video/123"

        def __init__(self):
            self.scope = _Scope()
            self.scan_calls = 0

        async def evaluate(self, script, *_args, **_kwargs):
            if "({selectors, strictScope})" in script:
                return {
                    "selector": '[data-smart-comment-surface-opened="1"]',
                    "scroll_container_tag": "DIV",
                    "scroll_container_scrollable": True,
                    "items": 6,
                    "strong_items": 6,
                    "score": 6000,
                }
            self.scan_calls += 1
            return {
                "rows": [
                    {
                        "position": 0,
                        "text": "main one",
                        "selector": '[data-e2e="comment-level-1"]',
                        "cid": None,
                        "author": None,
                        "scrollTop": self.scope.top,
                        "anchorToken": f"anchor-one-{self.scan_calls}",
                    },
                    {
                        "position": 1,
                        "text": "main two",
                        "selector": '[data-e2e="comment-level-1"]',
                        "cid": None,
                        "author": None,
                        "scrollTop": self.scope.top,
                        "anchorToken": f"anchor-two-{self.scan_calls}",
                    },
                ],
                "scrollTop": self.scope.top,
                "scopeMissing": False,
            }

        async def query_selector(self, selector):
            if selector in {
                '[data-smart-comment-list-batch-scope="1"]',
                '[data-smart-comment-list-batch-scroll="1"]',
            }:
                return self.scope
            return None

    monkeypatch.setattr(comment_outcome, "detect_captcha", fake_detect_captcha)

    found, info = await comment_outcome.find_comments_in_list_batch(
        _Page(),
        ["main one", "main two", "main three"],
        max_scrolls=5,
        scroll_delay_ms=0,
        ensure_open=False,
        strict_scope=True,
        return_partial_on_found=True,
    )

    assert set(found) == {0, 1}
    assert info["found_count"] == 2
    assert info["missing_count"] == 1
    assert info["matched_indexes"] == [0, 1]
    assert info["missing_indexes"] == [2]
    assert info["missing_previews"][0]["text_preview"] == "main three"
    # No early partial-return: the search scans the full budget for the missing
    # target instead of returning a partial set prematurely.
    assert info["scrolls"] == 5


@pytest.mark.unit
@pytest.mark.asyncio
async def test_batch_search_stops_when_captcha_is_visible(monkeypatch):
    async def fake_detect_captcha(_page, wait_ms=0):
        return True, "slider"

    class _Page:
        url = "https://www.tiktok.com/@u/video/123"

        async def evaluate(self, *_args, **_kwargs):
            raise AssertionError("comment scan must not run under captcha")

    monkeypatch.setattr(comment_outcome, "detect_captcha", fake_detect_captcha)

    found, info = await comment_outcome.find_comments_in_list_batch(
        _Page(),
        ["target text"],
        ensure_open=False,
        strict_scope=True,
    )

    assert found == {}
    assert info["error"] == "captcha_challenge:slider"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_batch_search_reports_parse_empty_when_strong_scope_has_no_rows(monkeypatch):
    async def fake_detect_captcha(_page, wait_ms=0):
        return False, None

    class _Scope:
        async def evaluate(self, script, *_args):
            if "=> ({top:" in script:
                return {"top": 0, "height": 1000, "client": 500}
            return None

    class _Page:
        url = "https://www.tiktok.com/@u/video/123"

        def __init__(self):
            self.calls = 0

        async def evaluate(self, *_args, **_kwargs):
            self.calls += 1
            if self.calls == 1:
                return {
                    "selector": '[data-smart-comment-surface-opened="1"]',
                    "scroll_container_tag": "DIV",
                    "scroll_container_scrollable": True,
                    "items": 12,
                    "strong_items": 6,
                    "score": 6000,
                }
            return {"rows": [], "scrollTop": 0, "scopeMissing": False}

        async def query_selector(self, selector):
            if selector in {
                '[data-smart-comment-list-batch-scope="1"]',
                '[data-smart-comment-list-batch-scroll="1"]',
            }:
                return _Scope()
            return None

    monkeypatch.setattr(comment_outcome, "detect_captcha", fake_detect_captcha)

    found, info = await comment_outcome.find_comments_in_list_batch(
        _Page(),
        ["target text"],
        max_scrolls=1,
        scroll_delay_ms=0,
        ensure_open=False,
        strict_scope=True,
    )

    assert found == {}
    assert info["error"] == "comment_surface_parse_empty"
    assert info["selected_strong_items"] == 6


@pytest.mark.unit
@pytest.mark.asyncio
async def test_single_search_stops_when_captcha_is_visible(monkeypatch):
    async def fake_detect_captcha(_page, wait_ms=0):
        return True, "slider"

    class _Page:
        url = "https://www.tiktok.com/@u/video/123"

        async def evaluate(self, *_args, **_kwargs):
            raise AssertionError("single comment scan must not run under captcha")

    monkeypatch.setattr(comment_outcome, "detect_captcha", fake_detect_captcha)

    found, info = await comment_outcome.find_comment_in_list(
        _Page(),
        "target text",
        ensure_open=False,
        strict_scope=True,
    )

    assert found is False
    assert info["error"] == "captcha_challenge:slider"
