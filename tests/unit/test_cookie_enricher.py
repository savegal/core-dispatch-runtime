import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import executor.cookie_enricher as cookie_enricher
from services.cookie_validator import parse_cookies_to_dict


@pytest.mark.unit
@pytest.mark.asyncio
async def test_enricher_dismisses_cookie_banner_before_collecting_cookies(monkeypatch):
    calls = {"dismiss": 0, "scroll": 0}

    class _Context:
        async def cookies(self):
            return [
                {"name": "ttwid", "value": "wid"},
                {"name": "tt_csrf_token", "value": "csrf"},
            ]

    class _Page:
        url = "https://www.tiktok.com/foryou"
        context = _Context()
        route = AsyncMock()
        unroute = AsyncMock()

        async def goto(self, *_args, **_kwargs):
            return None

        async def evaluate(self, _script):
            calls["scroll"] += 1

    async def _fake_sleep(_seconds):
        return None

    async def _fake_dismiss(_page):
        calls["dismiss"] += 1
        return 1

    monkeypatch.setattr(cookie_enricher.asyncio, "sleep", _fake_sleep)
    monkeypatch.setattr(cookie_enricher, "dismiss_cookie_banners", _fake_dismiss)

    result = await cookie_enricher.enrich_cookies_via_browser(
        _Page(),
        {"sid_guard": "guard"},
        timeout_seconds=1.0,
    )

    assert result.success is True
    assert calls["dismiss"] >= 1
    assert calls["scroll"] == 0


@pytest.mark.unit
@pytest.mark.asyncio
async def test_enricher_does_not_succeed_without_auth_cookies(monkeypatch):
    class _Context:
        async def cookies(self):
            return [
                {"name": "ttwid", "value": "wid"},
                {"name": "tt_csrf_token", "value": "csrf"},
            ]

    class _Page:
        url = "https://www.tiktok.com/foryou"
        context = _Context()
        route = AsyncMock()
        unroute = AsyncMock()

        async def goto(self, *_args, **_kwargs):
            return None

        async def evaluate(self, _script):
            return None

    async def _fake_sleep(_seconds):
        return None

    async def _fake_dismiss(_page):
        return 0

    monkeypatch.setattr(cookie_enricher.asyncio, "sleep", _fake_sleep)
    monkeypatch.setattr(cookie_enricher, "dismiss_cookie_banners", _fake_dismiss)

    result = await cookie_enricher.enrich_cookies_via_browser(
        _Page(),
        {},
        timeout_seconds=1.0,
    )

    assert result.success is False
    assert result.strength_after == "dead"
    assert result.error == "auth_cookies_not_restored"


@pytest.mark.unit
def test_serialization_preserves_domains_duplicates_and_explicit_flags():
    records = [
        {"name": "msToken", "value": "web", "domain": "www.tiktok.com", "path": "/",
         "secure": False, "httpOnly": False, "sameSite": "Lax", "expires": 2000000000},
        {"name": "msToken", "value": "ads", "domain": "ads.tiktok.com", "path": "/api",
         "secure": True, "httpOnly": True},
        {"name": "sid_guard", "value": "a" * 32 + "%7C1700000000%7C3600", "domain": "www.tiktok.com",
         "secure": True, "httpOnly": True},
    ]
    cookies = parse_cookies_to_dict(json.dumps(records))
    serialized = json.loads(cookie_enricher._serialize_cookies_to_json(cookies))

    assert serialized[0] == records[0]
    assert serialized[1] == records[1]
    for name in ("sessionid", "sid_tt", "sessionid_ss"):
        alias = next(c for c in serialized if c["name"] == name)
        assert alias["domain"] == "www.tiktok.com"
        assert alias["httpOnly"] is True
        assert alias["value"] == "a" * 32
    assert "path" not in cookies.records[2]  # Serialization must not mutate the input.


@pytest.mark.unit
def test_serialization_defaults_for_cookies_without_metadata():
    serialized = json.loads(cookie_enricher._serialize_cookies_to_json({"sessionid": "sid", "ttwid": "wid"}))
    assert all(c["domain"] == ".tiktok.com" and c["secure"] for c in serialized)
    assert serialized[0]["httpOnly"] is True
    assert serialized[1]["httpOnly"] is False


@pytest.mark.unit
@pytest.mark.asyncio
async def test_enricher_stops_when_critical_cookies_appear_and_restores_routes(monkeypatch):
    existing = parse_cookies_to_dict(json.dumps([
        {"name": "sid_guard", "value": "guard", "domain": ".tiktok.com"},
        {"name": "msToken", "value": "web", "domain": "www.tiktok.com", "secure": False},
        {"name": "msToken", "value": "old-ads", "domain": "ads.tiktok.com"},
    ]))
    fresh = [
        {"name": "msToken", "value": "new-ads", "domain": "ads.tiktok.com"},
        {"name": "ttwid", "value": "wid", "domain": ".tiktok.com", "httpOnly": True},
        {"name": "tt_csrf_token", "value": "csrf", "domain": "www.tiktok.com", "httpOnly": False},
    ]
    context = SimpleNamespace(cookies=AsyncMock(side_effect=[[], fresh]))
    page = SimpleNamespace(
        url="https://www.tiktok.com/foryou", context=context,
        route=AsyncMock(), unroute=AsyncMock(), goto=AsyncMock(), evaluate=AsyncMock(),
    )
    monkeypatch.setattr(cookie_enricher.asyncio, "sleep", AsyncMock())
    monkeypatch.setattr(cookie_enricher, "dismiss_cookie_banners", AsyncMock())
    result = await cookie_enricher.enrich_cookies_via_browser(page, existing)

    assert result.success is True
    page.evaluate.assert_awaited_once()
    assert context.cookies.await_count == 2
    pattern, handler = page.route.call_args.args
    page.unroute.assert_awaited_once_with(pattern, handler)
    for resource in ("image", "media", "font", "script", "xhr", "document", "fetch", "stylesheet"):
        route = SimpleNamespace(request=SimpleNamespace(resource_type=resource), abort=AsyncMock(), fallback=AsyncMock())
        await handler(route)
        assert route.abort.await_count == int(resource in {"image", "media", "font"})
        assert route.fallback.await_count == int(resource not in {"image", "media", "font"})
    saved = json.loads(result.all_cookies_json)
    tokens = {c["domain"]: c["value"] for c in saved if c["name"] == "msToken"}
    assert tokens == {"www.tiktok.com": "web", "ads.tiktok.com": "new-ads"}
    assert next(c for c in saved if c["name"] == "ttwid")["httpOnly"] is True
    assert parse_cookies_to_dict(result.all_cookies_json)["msToken"] == "web"


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["navigation", "redirect", "cancelled"])
async def test_enricher_removes_only_its_route_on_early_exit(monkeypatch, failure):
    page = SimpleNamespace(
        url="https://www.tiktok.com/login", route=AsyncMock(), unroute=AsyncMock(), goto=AsyncMock(),
    )
    monkeypatch.setattr(cookie_enricher.asyncio, "sleep", AsyncMock())
    monkeypatch.setattr(cookie_enricher, "dismiss_cookie_banners", AsyncMock())
    if failure == "navigation":
        page.goto.side_effect = RuntimeError("page closed")
    elif failure == "cancelled":
        page.goto.side_effect = cookie_enricher.asyncio.CancelledError()

    if failure == "cancelled":
        with pytest.raises(cookie_enricher.asyncio.CancelledError):
            await cookie_enricher.enrich_cookies_via_browser(page, {})
    else:
        result = await cookie_enricher.enrich_cookies_via_browser(page, {})
        assert result.success is False
    page.unroute.assert_awaited_once_with(*page.route.call_args.args)


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize("read_fails", [False, True])
async def test_enricher_timeout_keeps_metadata_and_rejects_empty_critical_cookies(monkeypatch, read_fails):
    existing = parse_cookies_to_dict(json.dumps([
        {"name": "sid_guard", "value": "guard", "domain": "www.tiktok.com", "httpOnly": True},
    ]))
    context = SimpleNamespace(cookies=AsyncMock(return_value=[
        {"name": "ttwid", "value": "", "domain": ".tiktok.com"},
        {"name": "tt_csrf_token", "value": "csrf", "domain": ".tiktok.com"},
    ]))
    if read_fails:
        context.cookies.side_effect = RuntimeError("cookies unavailable")
    page = SimpleNamespace(
        url="https://www.tiktok.com/foryou", context=context,
        route=AsyncMock(), unroute=AsyncMock(), goto=AsyncMock(), evaluate=AsyncMock(),
    )
    monkeypatch.setattr(cookie_enricher.asyncio, "sleep", AsyncMock())
    monkeypatch.setattr(cookie_enricher, "dismiss_cookie_banners", AsyncMock())

    result = await cookie_enricher.enrich_cookies_via_browser(page, existing, timeout_seconds=0)

    assert result.success is False
    assert result.error == "critical_cookies_not_generated"
    assert json.loads(result.all_cookies_json)[0]["domain"] == "www.tiktok.com"
    context.cookies.assert_awaited_once()
    page.evaluate.assert_not_awaited()
