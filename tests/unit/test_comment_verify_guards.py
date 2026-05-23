from types import SimpleNamespace

import pytest

import executor.comment_verification as comment_verification
import executor.comment_outcome as comment_outcome
import executor.reply_finder as reply_finder
import executor.tiktok_client_async as tiktok_client_async


@pytest.mark.unit
def test_comment_row_signature_ignores_position_and_prefers_cid():
    assert comment_outcome._comment_row_signature("Hello", cid="123", author="demo") == "cid:123"
    assert (
        comment_outcome._comment_row_signature("  Hello   World  ", author="@Demo")
        == "text:hello world|author:@demo"
    )
    assert comment_outcome._comment_row_signature("", author="demo") is None


class _FakeTextElement:
    def __init__(self, text: str):
        self._text = text

    async def evaluate(self, _script):
        return self._text


class _FakeCommentRoot:
    def __init__(self, comment_id: str, text: str):
        self._comment_id = comment_id
        self._text = text

    async def get_attribute(self, name: str):
        if name in {"data-cid", "data-id", "data-comment-id", "id"}:
            return self._comment_id
        return None

    async def query_selector(self, selector: str):
        if "username" in selector or "author" in selector or "/@" in selector:
            return None
        return _FakeTextElement(self._text)

    async def is_visible(self):
        return True

    async def scroll_into_view_if_needed(self):
        return None


class _FakeVerifyPage:
    url = "https://www.tiktok.com/@demo/video/123"

    def __init__(self, comment_root):
        self._comment_root = comment_root

    async def goto(self, *args, **kwargs):
        return None

    async def wait_for_timeout(self, *args, **kwargs):
        return None

    async def query_selector_all(self, selector: str):
        return [self._comment_root]

    async def reload(self, *args, **kwargs):
        return None

    async def close(self):
        return None


@pytest.mark.unit
@pytest.mark.asyncio
async def test_verify_with_retries_does_not_succeed_on_comment_id_mismatch(monkeypatch):
    fake_root = _FakeCommentRoot(comment_id="wrong_id", text="hello world")
    fake_page = _FakeVerifyPage(fake_root)

    async def _fake_create_page_for_account(_account):
        return object(), fake_page

    async def _fake_find_comment_in_list(*args, **kwargs):
        return True, {
            "position": 0,
            "comment_id": "wrong_id",
            "text_normalized": "hello world",
        }

    monkeypatch.setattr(comment_verification, "create_page_for_account", _fake_create_page_for_account)
    monkeypatch.setattr(comment_verification, "find_comment_in_list", _fake_find_comment_in_list)

    account = SimpleNamespace(id=1, proxy=None)
    found, info = await comment_verification.verify_with_retries(
        verify_account=account,
        video_url="https://www.tiktok.com/@demo/video/123",
        main_comment_text="hello world",
        task_id=1,
        timeout=0.01,
        interval=0,
        expected_comment_id="expected_id",
        verify_mode="standard",
        max_scrolls=1,
        scroll_delay_ms=0,
    )

    assert found is False
    assert bool(info.get("comment_id_mismatch") or info.get("moderation_timeout"))


@pytest.mark.unit
@pytest.mark.asyncio
async def test_verify_with_retries_can_reject_text_only_author_missing(monkeypatch):
    fake_root = _FakeCommentRoot(comment_id="", text="hello world")
    fake_page = _FakeVerifyPage(fake_root)

    async def _fake_create_page_for_account(_account):
        return object(), fake_page

    async def _fake_find_comment_in_list(*args, **kwargs):
        return True, {
            "position": 0,
            "comment_id": None,
            "text_normalized": "hello world",
        }

    monkeypatch.setattr(comment_verification, "create_page_for_account", _fake_create_page_for_account)
    monkeypatch.setattr(comment_verification, "find_comment_in_list", _fake_find_comment_in_list)

    account = SimpleNamespace(id=1, proxy=None)
    found, info = await comment_verification.verify_with_retries(
        verify_account=account,
        video_url="https://www.tiktok.com/@demo/video/123",
        main_comment_text="hello world",
        task_id=1,
        timeout=0.01,
        interval=0,
        expected_author="expected_author",
        verify_mode="standard",
        max_scrolls=1,
        scroll_delay_ms=0,
        require_author_match=True,
    )

    assert found is False
    assert info.get("moderation_timeout") is True


class _FakeCommentListContainer:
    """Imitates the comment-list container that find_comment_by_text_and_author
    requires (after strict-scope fix). query_selector_all returns the supplied
    fake comment roots so the parent-id check still runs."""

    def __init__(self, roots):
        self._roots = roots

    async def is_visible(self):
        return True

    async def query_selector_all(self, _selector):
        return self._roots

    async def evaluate(self, _script):
        return None


class _FalseVisibleSmartScope:
    async def is_visible(self):
        return False

    async def evaluate(self, _script):
        return None


class _FakeBatchScopePage:
    url = "https://www.tiktok.com/@demo/video/123"

    def __init__(self):
        self.evaluate_calls = 0

    async def evaluate(self, _script, _arg=None):
        self.evaluate_calls += 1
        if self.evaluate_calls == 1:
            return {
                "selector": '[class*="CommentContainer"]',
                "scroll_container_tag": "DIV",
                "scroll_container_scrollable": False,
                "items": 1,
                "strong_items": 1,
                "score": 1010,
            }
        return {
            "rows": [
                {
                    "position": 0,
                    "text": "target parent comment",
                    "selector": '[data-e2e="comment-level-1"]',
                    "cid": "parent_1",
                    "author": "demo",
                    "scrollTop": 0,
                }
            ],
            "scrollTop": 0,
            "scopeMissing": False,
        }

    async def query_selector(self, selector: str):
        if selector in {
            '[data-smart-comment-list-batch-scope="1"]',
            '[data-smart-comment-list-batch-scroll="1"]',
        }:
            return _FalseVisibleSmartScope()
        return None


@pytest.mark.unit
@pytest.mark.asyncio
async def test_batch_parent_search_keeps_js_visible_scope_when_playwright_visibility_lies(monkeypatch):
    async def _no_sleep(_seconds):
        return None

    monkeypatch.setattr(comment_outcome.asyncio, "sleep", _no_sleep)

    found, info = await comment_outcome.find_comments_in_list_batch(
        _FakeBatchScopePage(),
        ["target parent comment"],
        max_scrolls=1,
        scroll_delay_ms=0,
        strict_scope=True,
        ensure_open=False,
    )

    assert info["found_count"] == 1
    assert found[0]["comment_id"] == "parent_1"


class _FakeReplyPage:
    def __init__(self, container=None):
        self.url = "https://www.tiktok.com/@demo/video/123"
        self._container = container

    async def query_selector(self, selector: str):
        # Only respond to the list-container probe; the rest stays None so the
        # implementation falls through to the container we provided.
        if any(
            tok in selector
            for tok in (
                "comment-list",
                "CommentList",
                "DivCommentListContainer",
                "CommentContainer",
            )
        ):
            return self._container
        return None


@pytest.mark.unit
@pytest.mark.asyncio
async def test_reply_finder_requires_exact_parent_id_when_expected_comment_id_provided(monkeypatch):
    fake_root = _FakeCommentRoot(comment_id="different_id", text="same parent text")
    fake_container = _FakeCommentListContainer([fake_root])
    fake_page = _FakeReplyPage(container=fake_container)

    async def _fake_check_page_alive(_page):
        return True, None

    monkeypatch.setattr(reply_finder, "check_page_alive", _fake_check_page_alive)

    element, info = await reply_finder.find_comment_by_text_and_author(
        page=fake_page,
        comment_text="same parent text",
        expected_author=None,
        expected_comment_id="expected_parent_id",
        using_proxy=False,
        max_scrolls=1,
    )

    assert element is None
    assert info["found"] is False
    assert "expected ID" in info["error"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_reply_finder_returns_comment_list_not_open_when_no_container(monkeypatch):
    """When no comment-list container is visible, the strict-scope finder must
    return comment_list_not_open and refuse a page-wide fallback. This guards
    against false matches against sidebar feed cards (cid=one-column-item-*)."""
    fake_page = _FakeReplyPage(container=None)

    async def _fake_check_page_alive(_page):
        return True, None

    monkeypatch.setattr(reply_finder, "check_page_alive", _fake_check_page_alive)

    element, info = await reply_finder.find_comment_by_text_and_author(
        page=fake_page,
        comment_text="some parent",
        expected_author=None,
        expected_comment_id=None,
        using_proxy=False,
        max_scrolls=1,
    )

    assert element is None
    assert info["found"] is False
    assert info["error"] == "comment_list_not_open"


class _FakeAlivePage:
    async def wait_for_timeout(self, *args, **kwargs):
        return None


@pytest.mark.unit
@pytest.mark.asyncio
async def test_fast_verify_reply_visibility_rejects_mismatched_comment_id(monkeypatch):
    async def _fake_check_page_alive(_page):
        return True, None

    async def _fake_find_comment_in_list(*args, **kwargs):
        return True, {
            "comment_id": "wrong_reply_id",
            "position": 0,
            "text_selector": "span[data-e2e=\"comment-level-2\"]",
        }

    monkeypatch.setattr(tiktok_client_async, "check_page_alive", _fake_check_page_alive)
    monkeypatch.setattr(comment_outcome, "find_comment_in_list", _fake_find_comment_in_list)

    found, info = await tiktok_client_async._fast_verify_reply_visibility(
        page=_FakeAlivePage(),
        reply_text="test reply",
        expected_comment_id="expected_reply_id",
    )

    assert found is False
    assert info["comment_id"] == "wrong_reply_id"
    assert info["comment_id_mismatch"] is True
