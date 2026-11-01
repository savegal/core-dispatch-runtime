"""Real Chromium HTTP/JSON pagination; every network request is locally fulfilled.

The fixture's dummy signature wrapper exercises the existing read contract,
not TikTok's SDK. Publication is replaced by an explicit local HTTP adapter.
"""
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock
from urllib.parse import parse_qs, urlparse

import pytest
from patchright.async_api import async_playwright

from executor import engagement_engine as engagement, keyword_replies as keyword
from services.order_transport import set_task_order_transport, reset_task_order_transport
from tests.unit.test_keyword_replies import keyword_db, URL, MEDIA, CIDS, comment


pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


@pytest.fixture
async def browser_http():
    requests, posts = [], []
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        context = await browser.new_context()
        await context.add_cookies([{"name": name, "value": "local-fixture", "domain": ".tiktok.com", "path": "/"}
                                   for name in ("sessionid", "ttwid", "tt_csrf_token", "msToken")])
        async def route(route):
            request = route.request
            parsed = urlparse(request.url)
            if parsed.path.rstrip("/") == "/api/comment/list":
                cursor = int(parse_qs(parsed.query).get("cursor", [0])[0])
                requests.append(cursor)
                rows = [comment(0, "ЦЕНА?"), comment(1), comment(2, "не подходит")] if cursor == 0 else [comment(0), comment(3, "цена")]
                payload = {"status_code": 0, "comments": rows, "cursor": 20 if cursor == 0 else 40, "has_more": 1 if cursor == 0 else 0}
                await route.fulfill(status=200, content_type="application/json", body=json.dumps(payload))
            elif parsed.path.rstrip("/") == "/api/comment/publish":
                payload = json.loads(request.post_data)
                posts.append(payload)
                await route.fulfill(status=200, content_type="application/json", body=json.dumps({
                    "cid": str(7700000000000010000 + len(posts)), "parent": payload["parent"]}))
            else:
                await route.fulfill(status=200, content_type="text/html", body="""<html><body>Local fixture<script>
                    window.byted_acrawler={sign:()=>''};
                    const original=window.fetch.bind(window);
                    window.fetch=(input, init)=>{
                        const url=new URL(input,location.href);
                        if(url.pathname.startsWith('/api/comment/')) {
                            url.searchParams.set('X-Bogus','local-fixture');
                            url.searchParams.set('X-Gnarly','local-fixture');
                        }
                        return original(url.toString(), init);
                    };
                    </script></body></html>""")
        await context.route("**/*", route)
        page = await context.new_page()
        await page.goto(URL)
        token = set_task_order_transport("http", SimpleNamespace(TIKTOK_NETWORK_IMPL_READY=True))
        try:
            yield page, requests, posts
        finally:
            reset_task_order_transport(token)
            await context.close()
            await playwright.stop()


async def test_real_comment_fetch_paginates_and_returns_every_matching_parent(browser_http):
    page, requests, posts = browser_http
    targets, scan = await keyword.collect_http_targets(page, MEDIA, ["цена", "где купить"])
    assert scan["complete"] and [target["cid"] for target in targets] == [CIDS[0], CIDS[1], CIDS[3]]
    assert requests == [0, 20] and not posts
    assert page._comment_list_http_meta["reason"] == "ok"


async def test_keyword_execution_posts_once_per_parent_and_does_not_repeat_after_resume(browser_http, keyword_db, monkeypatch):
    page, requests, posts = browser_http
    db, task, _ = keyword_db
    task.settings = {**task.settings, "keywords": ["цена", "где купить"]}
    db.commit()
    async def discovery(*args, **kwargs):
        return await keyword.collect_http_targets(page, MEDIA, task.settings["keywords"])
    monkeypatch.setattr(keyword, "discover_keyword_targets", discovery)
    monkeypatch.setattr(engagement, "resolve_order_video_url", AsyncMock(return_value=(URL, MEDIA)))
    async def local_publish(*args, **kwargs):
        assert kwargs["parent_phrase_match"] is False
        parent = kwargs["comment_id"]
        response = await page.evaluate("""async payload => {
            const response=await fetch('/api/comment/publish/', {method:'POST',body:JSON.stringify(payload)});
            return await response.json();
        }""", {"parent": parent, "text": kwargs["replies"][0]}, isolated_context=False)
        return [{"success": True, "method": "http", "network_confirmed": True,
                 "reply_id": response["cid"], "parent_comment_id": response["parent"]}], parent, None
    monkeypatch.setattr(engagement, "execute_engagement_only_replies_via_full_flow", local_publish)
    result = (await keyword.execute_keyword_replies(db, task, URL, task_id=task.id)).to_dict()
    assert result["status"] == "success" and result["engagement"]["replies"]["ok"] == 3
    assert [post["parent"] for post in posts] == [CIDS[0], CIDS[1], CIDS[3]]
    assert [post["text"] for post in posts] == ["Первый", "Второй", "Первый"]
    await keyword.execute_keyword_replies(db, task, URL, task_id=task.id)
    assert len(posts) == 3 and requests == [0, 20]


@pytest.mark.parametrize("cached", [False, True])
async def test_signed_keyword_batch_preserves_requested_count_without_native_ui_click(browser_http, cached):
    from executor.http.comment_discovery import fetch_comment_page

    page, cursors, posts = browser_http
    page._warmup_signer_ready = True
    if cached:
        page._cached_first_comment_page = (MEDIA, {"comments": [comment(0)], "cursor": 20, "has_more": 1})
    counts = []

    def requested(request):
        parsed = urlparse(request.url)
        if parsed.path.rstrip("/") == "/api/comment/list":
            counts.append(parse_qs(parsed.query)["count"][0])

    page.on("request", requested)
    await page.evaluate("""media => {
        window.fixtureCommentClicks = 0;
        const button = document.createElement('button');
        button.setAttribute('data-e2e', 'comment-icon');
        button.onclick = () => {
            window.fixtureCommentClicks++;
            fetch('/api/comment/list/?aweme_id=' + media + '&cursor=0&count=20');
        };
        document.body.append(button);
    }""", MEDIA, isolated_context=False)
    try:
        payload = await fetch_comment_page(page, MEDIA, cursor=0, count=50)
        assert payload is not None
        assert counts == ["50"] and cursors == [0]
        assert await page.evaluate("window.fixtureCommentClicks", isolated_context=False) == 0
        assert not posts
    finally:
        page.remove_listener("request", requested)
