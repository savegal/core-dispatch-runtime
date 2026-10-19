import asyncio
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from services.comment_search import contains_search_phrase, normalize_search_phrase


@pytest.mark.unit
@pytest.mark.parametrize(("query", "comment", "expected"), [
    ("где", "Подскажите, ГДЕ это? 😂", True),
    ("где", "нигде не нашёл", False),
    ("где", "гдето тут", False),
    ("где посмотреть 😂", "Подскажите ГДЕ 🔥 посмотреть?", True),
    ("где посмотреть", "где\n\u00a0посмотреть", True),
    ("где посмотреть", "где можно посмотреть", False),
    ("где посмотреть", "посмотреть где", False),
    ("где посмотреть", "где", False),
    ("где 👨‍👩‍👧 посмотреть 👍🏿", "ГДЕ❤️посмотреть", True),
    ("ГДЕ 🇺🇦 посмотреть", "где посмотреть", True),
    ("г\u200bде", "ГДЕ", True),
    ("café", "CAFE\u0301 ☕", True),
    ("що", "ЩО це?", True),
    ("Straße", "die STRASSE dort", True),
    ("1️⃣ где", "где", True),
    ("👍🏿", "любой комментарий", False),
    ("1️⃣", "1", False),
    ("?!", "любой комментарий", False),
    ("где ™️ℹ️🈁 посмотреть", "ГДЕ посмотреть", True),
    ("™️ℹ️🈁", "любой комментарий", False),
    ("", "любой комментарий", False),
])
def test_phrase_matching(query, comment, expected):
    assert contains_search_phrase(query, comment) is expected


@pytest.mark.unit
def test_phrase_matching_does_not_weaken_publication_verification():
    from executor.comment_outcome import is_comment_match

    assert not is_comment_match("где", "подскажите где посмотреть", min_ratio=1.0)
    assert is_comment_match("где", "подскажите где посмотреть", min_ratio=1.0, phrase_match=True)
    assert not is_comment_match("👍🏻", "👍🏿", min_ratio=1.0)
    assert not normalize_search_phrase("👨‍👩‍👧 🇺🇦 ❤️")


class _CommentPage:
    url = "https://www.tiktok.com/@u/video/123"

    def __init__(self, comments):
        self.comments = comments
        self.scan_calls = 0

    async def query_selector(self, selector):
        if selector in {
            '[data-smart-comment-list-batch-scope="1"]',
            '[data-smart-comment-list-batch-scroll="1"]',
        }:
            return SimpleNamespace(evaluate=AsyncMock(return_value={"top": 0, "height": 1000, "client": 500}))
        return None

    async def evaluate(self, script, *_args, **_kwargs):
        if "({selectors, strictScope})" in script:
            return {
                "selector": '[data-smart-comment-surface-opened="1"]',
                "scroll_container_tag": "DIV",
                "scroll_container_scrollable": True,
                "items": len(self.comments),
                "strong_items": len(self.comments),
                "score": 4000,
            }
        self.scan_calls += 1
        return {
            "rows": [
                dict(position=index, text=text, author=author, cid=None,
                     selector='[data-e2e="comment-level-1"]', scrollTop=0, anchorToken=f"row-{index}")
                for index, (text, author) in enumerate(self.comments)
            ],
            "scrollTop": 0,
            "scopeMissing": False,
        }


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize(("author", "position"), [(None, 1), ("target.user", 2)])
async def test_batch_phrase_search_returns_first_matching_row_and_keeps_full_anchor(monkeypatch, author, position):
    from executor import comment_outcome

    long_text = "Очень длинный комментарий " * 15 + "ГДЕ ❤️ посмотреть?"
    page = _CommentPage([
        ("Нигде посмотреть", "target.user"),
        ("А ГДЕ 😂 посмотреть?", "other.user"),
        (long_text, "target.user"),
        ("где посмотреть", "target.user"),
    ])
    monkeypatch.setattr(comment_outcome, "detect_captcha", AsyncMock(return_value=(False, None)))
    found, _info = await comment_outcome.find_comments_in_list_batch(
        page, ["где посмотреть 👍"], expected_authors=[author],
        require_author_match=bool(author), phrase_match=True, min_match_ratio=1.0,
        max_scrolls=1, ensure_open=False, strict_scope=True, top_level_only=True,
    )
    assert page.scan_calls == 1
    assert found[0]["position"] == position
    assert found[0]["anchor_token"] == f"row-{position}"
    assert found[0]["raw_text"] == page.comments[position][0]


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize("batch", [True, False])
async def test_phrase_mode_reaches_batch_and_single_anchor_search(monkeypatch, batch):
    from executor import user_schema

    main = user_schema.PublishedMain("где", None, False, True, "engagement_only_anchor", 0, None)
    hit = {"raw_text": "Подскажите, ГДЕ посмотреть 😂", "anchor_token": "parent-row"}
    finder = AsyncMock(return_value=({0: hit}, {}) if batch else (True, hit))
    monkeypatch.setattr(user_schema, "find_comments_in_list_batch" if batch else "find_comment_in_list", finder)
    page = SimpleNamespace(query_selector=AsyncMock(return_value=None))
    if batch:
        anchors, _info = await user_schema._find_main_anchors_batch(page, [main], phrase_match=True)
        anchor = anchors[0]
    else:
        anchor = await user_schema._find_main_anchor(page, [main], phrase_match=True)
    assert finder.call_args.kwargs["phrase_match"] is True
    assert anchor.open_text == hit["raw_text"]
    assert anchor.matched_text == "где"
    assert anchor.anchor_token == "parent-row"


@pytest.fixture
def engagement_runtime(db_session, monkeypatch):
    import db.database as database
    from db.models import Account
    from executor import browser_pool, engagement_engine, user_schema

    @contextmanager
    def db_context():
        yield db_session

    accounts = [Account(id=910 + i, label=f"reply-{i}", status="active", cookies='{"sessionid":"x"}') for i in range(8)]

    def select(_db, exclude_ids=None, **_kwargs):
        return next((account for account in accounts if account.id not in (exclude_ids or [])), None)

    monkeypatch.setattr(database, "get_sync_db_context", db_context)
    monkeypatch.setattr(engagement_engine, "get_free_account_for_reply", select)
    monkeypatch.setattr(browser_pool, "ensure_thread_browser_pool_capacity", lambda _: None)
    monkeypatch.setattr(browser_pool, "close_browser_pool", AsyncMock())
    monkeypatch.setattr(engagement_engine.settings, "PARALLEL_COMMENTING_TARGET_BROWSERS", 1)
    monkeypatch.setattr(engagement_engine.settings, "MAX_RETRY_ACCOUNTS", 8)
    monkeypatch.setattr(engagement_engine, "ENGAGEMENT_ONLY_REPLY_MAX_ATTEMPTS", 8)
    monkeypatch.setattr(engagement_engine.random, "uniform", lambda *_: 0)
    return engagement_engine, user_schema


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize("dynamic", [True, False])
@pytest.mark.parametrize("eventual_success", [True, False])
async def test_dead_accounts_do_not_spend_publish_attempt_but_replacement_is_bounded(
    db_session, monkeypatch, engagement_runtime, dynamic, eventual_success,
):
    engine, schema = engagement_runtime
    monkeypatch.setattr(engine.settings, "PARALLEL_DYNAMIC_WORKER_POOL_ENABLED", dynamic)
    monkeypatch.setattr(engine.settings, "MAX_RETRY_ACCOUNTS", 3)
    monkeypatch.setattr(engine, "ENGAGEMENT_ONLY_REPLY_MAX_ATTEMPTS", 1)
    seen = []

    async def reply(_db, account, url, parents, text, *, variant_index, **kwargs):
        seen.append(url)
        kwargs["on_resolved_video_url"]("https://www.tiktok.com/@demo/video/7635267329407913230")
        success = eventual_success and len(seen) == 3
        return schema.ReplyAttemptResult(replies=[schema.ReplyOutcome(
            text, variant_index, parents[0].text if success else None,
            parents[0].comment_id if success else None, success, "http_user_schema" if success else None,
            None if success else "dead_session:visible_login_ui", account.id,
            attempted=success, publication_attempted=success, counts_as_submitted=success,
            reply_comment_id="7641761192486194454" if success else None,
            reply_confirmation="network_comment_id" if success else None,
        )], found_mains=parents if success else [])

    monkeypatch.setattr(schema, "_reply_with_variant_on_all_mains", reply)
    results, _, _ = await engine.execute_engagement_only_replies_via_full_flow(
        db_session, "https://vt.tiktok.com/test/", comment_text="main", comment_id="7635267329407913230",
        comment_author=None, replies=["reply"], task_id=123,
    )
    assert len(seen) == 3
    assert seen[1:] == ["https://www.tiktok.com/@demo/video/7635267329407913230"] * 2
    assert results[0]["success"] is eventual_success
    if eventual_success:
        assert results[0]["attempts"] == 1


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize("dynamic", [True, False])
@pytest.mark.parametrize("found_between_misses", [False, True])
async def test_four_bounded_search_misses_stop_order_and_finding_parent_resets_count(
    db_session, monkeypatch, engagement_runtime, dynamic, found_between_misses,
):
    engine, schema = engagement_runtime
    attempts = []
    monkeypatch.setattr(engine.settings, "PARALLEL_DYNAMIC_WORKER_POOL_ENABLED", dynamic)

    async def reply(_db, account, _url, parents, text, *, variant_index, **kwargs):
        attempts.append(account.id)
        assert kwargs["parent_phrase_match"] is True
        found = found_between_misses and len(attempts) == 3
        return schema.ReplyAttemptResult(
            replies=[schema.ReplyOutcome(text, variant_index, parents[0].text, None, False, None,
                                         "reply_box_open_timeout", account.id, attempted=True)] if found else [],
            found_mains=parents if found else [], searched=True, saw_comment_rows=True,
            confident_empty_search=False, parent_search_max_seen_rows=12,
            parent_search_unique_rows_seen=12, parent_search_exhausted=False,
            parent_search_scroll_container_scrollable=True,
        )

    monkeypatch.setattr(schema, "_reply_with_variant_on_all_mains", reply)
    results, _, _ = await asyncio.wait_for(engine.execute_engagement_only_replies_via_full_flow(
        db_session, "https://www.tiktok.com/@demo/video/123", comment_text="где",
        comment_id=None, comment_author=None, replies=["ответ"], task_id=999,
    ), timeout=5)
    assert len(attempts) == (7 if found_between_misses else 4)
    assert any(row.get("error") == "remaining_main_comments_not_visible_after_four_accounts" for row in results)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_next_account_keeps_user_phrase_not_previous_observed_author_or_full_text(
    db_session, monkeypatch, engagement_runtime,
):
    engine, schema = engagement_runtime
    seen = []

    async def reply(_db, account, _url, parents, text, *, variant_index, **kwargs):
        seen.append((parents[0].text, parents[0].comment_id, parents[0].tiktok_username))
        assert kwargs["require_parent_author_match"] is False
        return schema.ReplyAttemptResult(
            replies=[schema.ReplyOutcome(text, variant_index, "А ГДЕ посмотреть?", "12345678901",
                                         True, "http_user_schema", None, account.id, attempted=True,
                                         matched_parent_author="observed.user",
                                         reply_comment_id=str(7641761192486194454 + variant_index),
                                         reply_confirmation="network_comment_id")],
            found_mains=parents, searched=True,
        )

    monkeypatch.setattr(schema, "_reply_with_variant_on_all_mains", reply)
    results, _, _ = await engine.execute_engagement_only_replies_via_full_flow(
        db_session, "https://www.tiktok.com/@demo/video/123", comment_text="где",
        comment_id=None, comment_author=None, replies=["ответ 1", "ответ 2"], task_id=999,
    )
    assert seen == [("где", None, None), ("где", None, None)]
    assert all(row["success"] for row in results)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_emoji_only_query_does_not_select_accounts(db_session, monkeypatch):
    from executor import engagement_engine

    def forbidden(*_args, **_kwargs):
        pytest.fail("emoji-only queries must not open accounts")

    monkeypatch.setattr(engagement_engine, "get_free_account_for_reply", forbidden)
    results, _, _ = await engagement_engine.execute_engagement_only_replies_via_full_flow(
        db_session, "https://www.tiktok.com/@demo/video/123", comment_text="😂",
        comment_id=None, comment_author=None, replies=["ответ"], task_id=999,
    )
    assert results[0]["error"] == "empty_search_phrase"


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize("error", ["captcha_challenge:slider", "login_not_ready", "proxy_timeout"])
async def test_pre_search_failure_does_not_consume_four_search_budget(db_session, monkeypatch, engagement_runtime, error):
    engine, schema = engagement_runtime
    calls = []

    async def reply(_db, account, _url, parents, text, *, variant_index, **kwargs):
        calls.append(account.id)
        if len(calls) == 3:
            return schema.ReplyAttemptResult(
                replies=[schema.ReplyOutcome(text, variant_index, None, None, False, None, error, account.id)],
                searched=False,
            )
        return schema.ReplyAttemptResult(searched=True, saw_comment_rows=True)

    monkeypatch.setattr(schema, "_reply_with_variant_on_all_mains", reply)
    results, _, _ = await engine.execute_engagement_only_replies_via_full_flow(
        db_session, "https://www.tiktok.com/@demo/video/123", comment_text="где",
        comment_id=None, comment_author=None, replies=["ответ"], task_id=999,
    )
    assert len(calls) == 5  # Four searches plus the pre-search failure.
    assert any(row.get("error") == "remaining_main_comments_not_visible_after_four_accounts" for row in results)


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize("query", ["😂", "@target.user: 👍🏿", "@target.user: Где посмотреть 😂"])
async def test_bot_rejects_emoji_only_query_but_preserves_real_phrase(monkeypatch, query):
    from bot.handlers import engagement_wizard as wizard

    message = SimpleNamespace(text=query, answer=AsyncMock())
    state = SimpleNamespace(update_data=AsyncMock())
    ask = AsyncMock()
    monkeypatch.setattr(wizard, "ask_for_reply_source", ask)
    await wizard.process_comment_text(message, state)
    if "Где" in query:
        assert state.update_data.call_args.kwargs["comment_text"] == "Где посмотреть 😂"
        assert state.update_data.call_args.kwargs["username"] == "target.user"
        ask.assert_awaited_once()
    else:
        state.update_data.assert_not_awaited()
        ask.assert_not_awaited()
        assert "смайликов" in message.answer.call_args.args[0]
