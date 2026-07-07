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
def test_parent_search_scans_bounded_comment_containers_before_broad_roots():
    joined = inspect.getsource(comment_outcome.find_comments_in_list_batch)

    assert "const primaryRootSelector = [" in joined
    assert "const primaryRoots = [...scope.querySelectorAll(primaryRootSelector)].filter(visible);" in joined
    assert ").slice(0, 250);" in joined


@pytest.mark.unit
def test_parent_search_collapses_nested_wrappers_for_one_top_level_comment():
    joined = inspect.getsource(comment_outcome.find_comments_in_list_batch)

    assert "const nestedContainerAlreadySeen = topLevelOnly" in joined
    assert "existing.contains(container) || container.contains(existing)" in joined
    assert "if (nestedContainerAlreadySeen) continue;" in joined


@pytest.mark.unit
def test_batch_search_trace_reports_top_level_only_mode():
    joined = inspect.getsource(comment_outcome.find_comments_in_list_batch)

    assert '"top_level_only": top_level_only' in joined


@pytest.mark.unit
def test_batch_search_partial_return_is_enabled_for_reply_attempts():
    joined = inspect.getsource(comment_outcome.find_comments_in_list_batch)

    # Reply attempts should act on parents already visible/found instead of
    # scrolling through the full list before publishing anything. Remaining
    # parents stay pending in the reply matrix and are retried by the scheduler.
    assert "partial_extra_scrolls" not in joined
    assert "partial_found_at_scroll" not in joined
    assert "if len(found) >= len(targets):" in joined
    assert "partial_found_return" in joined


@pytest.mark.unit
@pytest.mark.asyncio
async def test_batch_search_partial_return_keeps_found_anchor_without_extra_scans(monkeypatch):
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
    assert info["partial_returned"] is True
    assert info["scrolls"] == 1


@pytest.mark.unit
@pytest.mark.asyncio
async def test_batch_search_scroll_cap_is_not_reported_as_end_of_list(monkeypatch):
    async def fake_detect_captcha(_page, wait_ms=0):
        return False, None

    class _Scope:
        def __init__(self):
            self.top = 0

        async def evaluate(self, script, *_args):
            if "scrollTop += " in script:
                self.top += 450
                return None
            if "=> ({top:" in script:
                return {"top": self.top, "height": 5000, "client": 500}
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
                    "items": 8,
                    "strong_items": 8,
                    "score": 8000,
                }
            self.scan_calls += 1
            return {
                "rows": [
                    {
                        "position": self.scan_calls,
                        "text": f"other row {self.scan_calls}",
                        "selector": '[data-e2e="comment-level-1"]',
                        "cid": None,
                        "author": "other_author",
                        "scrollTop": self.scope.top,
                        "anchorToken": f"other-{self.scan_calls}",
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
        ["target parent"],
        max_scrolls=2,
        scroll_delay_ms=0,
        ensure_open=False,
        strict_scope=True,
    )

    assert found == {}
    assert info["error"] == "scroll_limit_reached"
    assert info["scroll_limit_reached"] is True
    assert info.get("search_exhausted") is not True
    assert info["unique_rows_seen"] == 2


@pytest.mark.unit
@pytest.mark.asyncio
async def test_batch_search_accepts_expected_author_from_author_candidates(monkeypatch):
    async def fake_detect_captcha(_page, wait_ms=0):
        return False, None

    class _Scope:
        async def evaluate(self, script, *_args):
            if "=> ({top:" in script:
                return {"top": 0, "height": 1000, "client": 500}
            return None

    class _Page:
        url = "https://www.tiktok.com/@u/video/123"

        async def evaluate(self, script, *_args, **_kwargs):
            if "({selectors, strictScope})" in script:
                return {
                    "selector": '[data-smart-comment-surface-opened="1"]',
                    "scroll_container_tag": "DIV",
                    "scroll_container_scrollable": True,
                    "items": 3,
                    "strong_items": 3,
                    "score": 3000,
                }
            return {
                "rows": [
                    {
                        "position": 0,
                        "text": "main parent text",
                        "selector": '[data-e2e="comment-level-1"]',
                        "cid": None,
                        "author": "display_name",
                        "authorCandidates": ["display_name", "real_parent_author"],
                        "scrollTop": 0,
                        "anchorToken": "anchor-author-candidate",
                    }
                ],
                "scrollTop": 0,
                "scopeMissing": False,
            }

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
        ["main parent text"],
        expected_authors=["real_parent_author"],
        require_author_match=True,
        max_scrolls=1,
        scroll_delay_ms=0,
        ensure_open=False,
        strict_scope=True,
        top_level_only=True,
    )

    assert set(found) == {0}
    assert found[0]["matched_by"] == "text_author"
    assert found[0]["author_candidates"] == ["display_name", "real_parent_author"]
    assert info["found_count"] == 1


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
    assert info["partial_returned"] is True
    assert info["scrolls"] == 1


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
