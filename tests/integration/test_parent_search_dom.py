"""Parent search regressions against a local DOM; all network access is blocked."""

import pytest
from patchright.async_api import async_playwright

from executor.comment_outcome import find_comments_in_list_batch


pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


@pytest.fixture
async def parent_page():
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        try:
            page = await browser.new_page(viewport={"width": 1280, "height": 900})
            await page.route("**/*", lambda route: route.abort())
            await page.set_content("""
                <style>
                  #comments { height: 400px; width: 520px; overflow-y: auto; }
                  .row { height: 100px; box-sizing: border-box; }
                  p { margin: 0; }
                </style>
                <section id="comments" data-e2e="comment-list"></section>
            """)
            yield page
        finally:
            await browser.close()


async def scan(page, texts, **kwargs):
    return await find_comments_in_list_batch(
        page, texts, ensure_open=False, strict_scope=True, top_level_only=True,
        return_partial_on_found=True, min_match_ratio=1.0,
        scroll_delay_ms=0, **kwargs,
    )


async def test_visible_parent_after_first_250_loaded_rows_is_scanned(parent_page):
    await parent_page.evaluate("""() => {
        const list = document.querySelector('#comments');
        list.innerHTML = Array.from({length: 320}, (_, i) =>
            `<article class="row" data-comment-id="c${i}">
              <p data-e2e="comment-level-1">parent ${i}</p>
            </article>`).join('');
        list.scrollTop = 28000;
    }""")
    found, info = await scan(parent_page, ["parent 280"], max_scrolls=1)
    assert found, info
    assert found[0]["comment_id"] == "c280"


async def test_mixed_row_markup_does_not_hide_parent(parent_page):
    await parent_page.locator('#comments').evaluate("""el => el.innerHTML = `
        <article class="row"><p data-e2e="comment-level-1">first parent</p></article>
        <div class="DivCommentContent row"><p data-e2e="comment-level-1">second parent</p></div>
    `""")
    found, info = await scan(parent_page, ["first parent", "second parent"], max_scrolls=1)
    assert set(found) == {0, 1}, info


async def test_group_wrapper_does_not_swallow_sibling_parents(parent_page):
    await parent_page.locator('#comments').evaluate("""el => el.innerHTML = `
        <div class="CommentItemGroup">
          <article class="row" data-comment-id="c1"><p data-e2e="comment-level-1">first parent</p></article>
          <article class="row" data-comment-id="c2"><p data-e2e="comment-level-1">second parent</p></article>
        </div>
    `""")
    found, info = await scan(parent_page, ["first parent", "second parent"], max_scrolls=1)
    assert [found[i]["comment_id"] for i in sorted(found)] == ["c1", "c2"], info


async def test_nested_aliases_and_replies_are_not_additional_parents(parent_page):
    await parent_page.locator('#comments').evaluate("""el => el.innerHTML = `
        <article data-comment-id="c1">
          <div class="CommentItem"><p data-e2e="comment-level-1"><span data-e2e="comment-level-1">same text</span></p></div>
          <div class="reply-container">
            <article data-comment-id="r1"><p data-e2e="comment-level-2">same text</p></article>
          </div>
        </article>
    `""")
    found, info = await scan(parent_page, ["same text", "same text"], max_scrolls=1)
    assert len(found) == 1, info
    assert found[0]["comment_id"] == "c1"


async def test_continuation_reaches_next_parent_without_returning_to_top(parent_page):
    await parent_page.evaluate("""() => {
        const list = document.querySelector('#comments');
        list.innerHTML = Array.from({length: 40}, (_, i) =>
            `<article class="row" data-comment-id="c${i}">
              <p data-e2e="comment-level-1">parent ${i}</p>
            </article>`).join('');
        list.scrollTop = 2000;
    }""")
    found, info = await scan(
        parent_page, ["parent 27"], max_scrolls=3, continue_from_current_position=True,
    )
    assert found, info
    assert found[0]["comment_id"] == "c27"
    assert found[0]["scroll_top"] >= 2000


async def test_parent_does_not_inherit_nested_reply_id(parent_page):
    await parent_page.locator('#comments').evaluate("""el => el.innerHTML = `
        <article>
          <p data-e2e="comment-level-1">parent without id</p>
          <div class="reply-container"><article data-comment-id="reply-id">
            <p data-e2e="comment-level-2">nested reply</p>
          </article></div>
        </article>
    `""")
    found, info = await scan(
        parent_page, ["parent without id"], expected_comment_ids=["parent-id"], max_scrolls=1,
    )
    assert found, info
    assert found[0]["comment_id"] is None


async def test_hidden_text_and_username_do_not_hide_visible_comment_text(parent_page):
    await parent_page.locator('#comments').evaluate("""el => el.innerHTML = `
        <article data-comment-id="c1">
          <p style="display:none" data-e2e="comment-level-1">old hidden text</p>
          <a href="/@author"><p>Display name</p></a>
          <p data-e2e="comment-level-1">visible parent</p>
        </article>
    `""")
    found, info = await scan(parent_page, ["visible parent"], max_scrolls=1)
    assert found, info
    assert found[0]["author"] == "author"


async def test_delayed_virtual_row_is_read_before_scrolling_past_it(parent_page):
    await parent_page.evaluate("""() => {
        const list = document.querySelector('#comments');
        list.innerHTML = '<div style="height:6000px"><article class="row" id="row">' +
            '<p data-e2e="comment-level-1">initial unrelated row</p></article></div>';
        let timer;
        list.addEventListener('scroll', () => {
            clearTimeout(timer);
            const row = document.querySelector('#row');
            const top = list.scrollTop;
            row.style.transform = `translateY(${top}px)`;
            row.querySelector('p').textContent = '';
            timer = setTimeout(() => {
                row.querySelector('p').textContent = top < 700 ? 'delayed parent' : 'later unrelated row';
            }, 450);
        });
    }""")
    found, info = await scan(
        parent_page, ["delayed parent"], max_scrolls=6, continue_from_current_position=True,
    )
    assert found, info
    assert found[0]["scroll_top"] < 700


async def test_same_text_requires_correct_parent_author(parent_page):
    await parent_page.locator('#comments').evaluate("""el => el.innerHTML = `
        <article><a href="/@wrong">Wrong</a><p data-e2e="comment-level-1">same text</p></article>
        <article><a href="/@correct">Correct</a><p data-e2e="comment-level-1">same text</p></article>
    `""")
    found, info = await scan(
        parent_page, ["same text"], expected_authors=["correct"], require_author_match=True, max_scrolls=1,
    )
    assert found, info
    assert found[0]["author"] == "correct"
