import pytest

import executor.cookie_enricher as cookie_enricher


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
    assert calls["scroll"] >= 1


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
