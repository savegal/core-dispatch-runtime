"""Cookie/bootstrap regressions using local route fixtures, with no TikTok traffic."""
import asyncio
import json
import time
from unittest.mock import AsyncMock
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse

import pytest

from executor import engagement_engine
from executor.http import comment_discovery as discovery
from services.cookie_validator import normalize_cookies_for_browser
from tests.integration.test_http_browser_transport import browser_page as browser_page
from tests.unit.test_cookie_route_audit_20261025 import export_records


pytestmark = [pytest.mark.integration, pytest.mark.asyncio]
VIDEO = "7635267329407913230"
URL = f"https://www.tiktok.com/@fixture/video/{VIDEO}"


async def test_selected_root_guard_is_sent_to_www_in_chromium(browser_page):
    sent = []

    async def serve(route):
        sent.append(await route.request.header_value("cookie") or "")
        await route.fulfill(content_type="text/html", body="<html>Local fixture</html>")

    await browser_page.route("**/*", serve)
    records = export_records()
    original = json.dumps(records)
    await browser_page.context.add_cookies(normalize_cookies_for_browser(records))
    await browser_page.goto(URL)
    assert "sid_guard=" + "b" * 32 in sent[0]
    assert "sessionid=" + "b" * 32 in sent[0]
    assert "sid_guard=" + "a" * 32 not in sent[0]
    assert json.dumps(records) == original


async def test_delayed_native_activation_is_reused_and_fresh_read_does_not_click(browser_page):
    requests = []

    async def serve(route):
        if urlparse(route.request.url).path == "/api/comment/list/":
            requests.append(route.request.url)
            await route.fulfill(content_type="application/json", body='{"status_code":0,"comments":[],"has_more":0}')
        else:
            await route.fulfill(content_type="text/html", body="<button data-e2e='comment-icon'>comments</button>")

    await browser_page.route("**/*", serve)
    await browser_page.goto(URL)
    await browser_page.evaluate("""video => {
        window.clicks = 0;
        const nativeFetch = window.fetch;
        window.fetch = (input, options) => {
            const url = new URL(input, location.href);
            url.searchParams.set('X-Bogus', 'fixture');
            return nativeFetch(url.href, options);
        };
        document.querySelector('button').onclick = () => {
            window.clicks++;
            setTimeout(() => window.fetch('/api/comment/list/?aweme_id=' + video + '&cursor=0&count=20'), 450);
        };
    }""", VIDEO, isolated_context=False)
    assert await discovery.fetch_comment_page(browser_page, VIDEO) is not None
    assert browser_page._comment_list_http_meta["activation_response_reused"] is True
    assert len(requests) == 1
    # Even if the UI marker was reset, a fresh observer read must not activate it.
    await browser_page.evaluate("delete window.__teletanCommentActivation", isolated_context=False)
    assert await discovery.fetch_comment_page(browser_page, VIDEO, fresh=True) is not None
    assert len(requests) == 2
    assert await browser_page.evaluate("window.clicks", isolated_context=False) == 1
    assert all(parse_qs(urlparse(url).query)["X-Bogus"] == ["fixture"] for url in requests)


@pytest.mark.parametrize("warmup_ready", [True, False])
async def test_native_activation_slower_than_one_second_does_not_race_manual_get(browser_page, warmup_ready):
    requests = []
    async def serve(route):
        if urlparse(route.request.url).path == "/api/comment/list/":
            requests.append(route.request.url)
            await route.fulfill(content_type="application/json", body='{"status_code":0,"comments":[],"has_more":0}')
        else:
            await route.fulfill(content_type="text/html", body="<button data-e2e='comment-icon'>comments</button>")
    await browser_page.route("**/*", serve)
    await browser_page.goto(URL)
    browser_page._warmup_signer_ready = warmup_ready
    await browser_page.evaluate("""video => {
        const fetch = window.fetch;
        window.fetch = (input, options) => {
            const url = new URL(input, location.href);
            url.searchParams.set('X-Bogus', 'fixture');
            return fetch(url.href, options);
        };
        document.querySelector('button').onclick = () => setTimeout(() => {
            window.fetch('/api/comment/list/?aweme_id=' + video + '&cursor=0&count=20&native=1');
        }, 1400);
    }""", VIDEO, isolated_context=False)
    payload = await discovery.fetch_comment_page(browser_page, VIDEO)
    assert payload is not None
    assert browser_page._comment_list_http_meta.get("activation_response_reused") is True
    assert len(requests) == 1 and parse_qs(urlparse(requests[0]).query)["native"] == ["1"]


async def test_activation_without_dispatch_does_not_send_manual_get_when_runtime_unready(browser_page, monkeypatch):
    requests = []
    async def serve(route):
        if urlparse(route.request.url).path.startswith("/api/"):
            requests.append(route.request.url)
            await route.fulfill(content_type="application/json", body="")
        else:
            await route.fulfill(content_type="text/html", body="<button data-e2e='comment-icon'>comments</button>")
    await browser_page.route("**/*", serve)
    await browser_page.goto(URL)
    browser_page._warmup_signer_ready = False
    monkeypatch.setattr(discovery, "_COMMENT_LIST_NATIVE_TIMEOUT_SECONDS", 0.1)
    monkeypatch.setattr("executor.http.signature_runtime.wait_for_runtime_signer_ready", AsyncMock(return_value=False))
    assert await discovery.fetch_comment_page(browser_page, VIDEO) is None
    assert requests == []
    assert browser_page._comment_list_http_meta["activation_dispatch_timeout"]
    assert browser_page._comment_list_http_meta["reason"] == "signer_not_ready"


@pytest.mark.parametrize("signed", [True, False])
async def test_empty_body_does_not_imply_an_unsigned_dispatch(browser_page, monkeypatch, signed):
    async def serve(route):
        if urlparse(route.request.url).path.startswith("/api/"):
            await route.fulfill(content_type="application/json", body="")
        else:
            await route.fulfill(content_type="text/html", body="<html>Local fixture</html>")
    await browser_page.route("**/*", serve)
    await browser_page.goto(URL)
    if signed:
        await browser_page.evaluate("""() => {
            const fetch = window.fetch;
            window.fetch = (input, options) => {
                const url = new URL(input, location.href);
                url.searchParams.set('X-Bogus', 'fixture');
                return fetch(url.href, options);
            };
        }""", isolated_context=False)
    monkeypatch.setattr(discovery, "_COMMENT_LIST_MAX_RETRIES", 0)
    assert await discovery.fetch_comment_page(browser_page, VIDEO) is None
    meta = browser_page._comment_list_http_meta
    assert meta["reason"] == "empty_body" and meta["security_params_source"] == "request_event"
    assert meta["unsigned_dispatch_observed"] is (not signed)


@pytest.mark.parametrize("csrf_received", [True, False])
async def test_minimal_import_bootstrap_requires_observed_tokens_before_publish(browser_page, csrf_received):
    requests = []

    async def serve(route):
        requests.append((route.request.method, urlparse(route.request.url).path))
        if urlparse(route.request.url).path == "/api/comment/list/":
            headers = {"x-ms-token": "fresh-native-token"}
            if csrf_received:
                headers["set-cookie"] = "tt_csrf_token=fresh-csrf; Domain=.tiktok.com; Path=/; Secure; SameSite=Lax"
            await route.fulfill(content_type="application/json", headers=headers,
                                body='{"status_code":0,"comments":[],"has_more":0}')
        else:
            await route.fulfill(content_type="text/html", body="""<html><script>
                document.cookie = 'ttwid=native-link; domain=.tiktok.com; path=/';
                window.byted_acrawler = {sign: () => ({})};
                const nativeFetch = window.fetch;
                window.fetch = (input, options) => {
                    const url = new URL(input, location.href);
                    url.searchParams.set('X-Bogus', 'fixture');
                    return nativeFetch(url.href, options);
                };
            </script></html>""")

    await browser_page.route("**/*", serve)
    await browser_page.context.add_cookies(normalize_cookies_for_browser(export_records()))
    await browser_page.goto(URL)
    browser_page._http_auth_verified_at = time.monotonic()
    account = SimpleNamespace(id=None, device_seed="e" * 64, cookies=json.dumps(export_records()),
                              country_code=None, _http_preflight_requires_browser_warmup=True)
    cookies, valid, info = await engagement_engine.warmup_page_for_http(
        browser_page, URL, account=account, max_retries=1,
    )
    assert valid is csrf_received
    assert info["comment_api_probed"] is True
    assert cookies["msToken"] == "fresh-native-token"
    assert requests.count(("GET", "/api/comment/list/")) == 1
    assert all(method != "POST" for method, _ in requests)
    if csrf_received:
        assert cookies["tt_csrf_token"] == "fresh-csrf"
        assert account._http_preflight_requires_browser_warmup is False
    else:
        assert info["reason_code"] == "missing_tt_csrf_token"
        assert account._http_preflight_requires_browser_warmup is True


async def test_retry_does_not_reactivate_comment_button_after_empty_response(browser_page, monkeypatch):
    requests = []

    async def serve(route):
        if urlparse(route.request.url).path == "/api/comment/list/":
            requests.append(route.request.url)
            await route.fulfill(content_type="application/json", body="" if len(requests) == 1 else '{"status_code":0,"comments":[]}')
        else:
            await route.fulfill(content_type="text/html", body="<button data-e2e='comment-icon'>comments</button>")

    await browser_page.route("**/*", serve)
    await browser_page.goto(URL)
    await browser_page.evaluate("""() => {
        window.clicks = 0;
        document.querySelector('button').onclick = () => {window.clicks++;};
        const nativeFetch = window.fetch;
        window.fetch = (input, options) => {
            const url = new URL(input, location.href);
            url.searchParams.set('X-Bogus', 'fixture');
            delete window.__teletanCommentActivation;
            return nativeFetch(url.href, options);
        };
    }""", isolated_context=False)
    monkeypatch.setattr(discovery, "_COMMENT_LIST_RETRY_DELAY_SECONDS", 0)
    assert await discovery.fetch_comment_page(browser_page, VIDEO) is not None
    assert len(requests) == 2
    assert await browser_page.evaluate("window.clicks", isolated_context=False) == 1

