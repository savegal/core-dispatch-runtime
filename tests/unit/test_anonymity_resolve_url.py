"""Тесты на анонимность: resolve_video_url не утекает IP сервера.

`resolve_video_url` использовался для разрешения коротких TikTok-ссылок
(vt.tiktok.com / vm.tiktok.com). Раньше httpx запрос шёл БЕЗ прокси →
TikTok получал реальный IP сервера-воркера.

Фикс: без прокси не делаем HTTP-запрос вообще. page.goto в контексте
аккаунта позже разрешит шортлинк через прокси этого аккаунта.
"""
import pytest


@pytest.mark.unit
@pytest.mark.asyncio
async def test_resolve_returns_existing_id_unchanged():
    """Canonical URL с aweme_id — возвращаем как есть, без HTTP."""
    from executor.engagement_engine import resolve_video_url
    url = "https://www.tiktok.com/@user/video/7638676938185264404"
    out_url, out_id = await resolve_video_url(url)
    assert out_id == "7638676938185264404"
    assert out_url == url


@pytest.mark.unit
@pytest.mark.asyncio
async def test_resolve_passes_through_non_shortlink():
    """URL, не являющийся шортлинком — возвращаем без HTTP."""
    from executor.engagement_engine import resolve_video_url
    url = "https://example.com/some/path"
    out_url, out_id = await resolve_video_url(url)
    assert out_url == url
    assert out_id is None


@pytest.mark.unit
@pytest.mark.asyncio
async def test_resolve_shortlink_without_proxy_skips_http(monkeypatch):
    """Самое важное: без прокси НЕ делаем httpx запрос с реального IP."""
    from executor import engagement_engine

    httpx_called = {"count": 0}

    class _FakeAsyncClient:
        def __init__(self, *a, **kw):
            httpx_called["count"] += 1
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return None
        async def head(self, *a, **kw): raise AssertionError("must not be called")
        async def get(self, *a, **kw): raise AssertionError("must not be called")

    monkeypatch.setattr(engagement_engine.httpx, "AsyncClient", _FakeAsyncClient)

    out_url, out_id = await engagement_engine.resolve_video_url(
        "https://vt.tiktok.com/ZS9w8yPEh/"
    )
    assert httpx_called["count"] == 0, "без прокси httpx запрос делаться не должен"
    assert out_url == "https://vt.tiktok.com/ZS9w8yPEh/"
    assert out_id is None


@pytest.mark.unit
@pytest.mark.asyncio
async def test_resolve_shortlink_with_proxy_normalizes_proxy_url(monkeypatch):
    """С прокси — httpx получает корректный proxy_url, и попытка GET идёт."""
    from executor import engagement_engine

    captured = {}

    class _FakeResp:
        url = "https://www.tiktok.com/@erjowkaaa/video/7638676938185264404"

    class _FakeAsyncClient:
        def __init__(self, *a, **kw):
            captured["proxy"] = kw.get("proxy")
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return None
        async def head(self, *a, **kw):
            return _FakeResp()
        async def get(self, *a, **kw):
            return _FakeResp()

    monkeypatch.setattr(engagement_engine.httpx, "AsyncClient", _FakeAsyncClient)

    out_url, out_id = await engagement_engine.resolve_video_url(
        "https://vt.tiktok.com/ZS9w8yPEh/",
        proxy="user:pass@1.2.3.4:8080",
    )
    # proxy получил префикс http://
    assert captured["proxy"] == "http://user:pass@1.2.3.4:8080"
    assert out_id == "7638676938185264404"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_resolve_shortlink_with_full_proxy_url_passes_as_is(monkeypatch):
    """Уже полный proxy URL (с scheme) — не добавляем второй scheme."""
    from executor import engagement_engine

    captured = {}

    class _FakeResp:
        url = "https://www.tiktok.com/@u/video/123"

    class _FakeAsyncClient:
        def __init__(self, *a, **kw):
            captured["proxy"] = kw.get("proxy")
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return None
        async def head(self, *a, **kw):
            return _FakeResp()
        async def get(self, *a, **kw):
            return _FakeResp()

    monkeypatch.setattr(engagement_engine.httpx, "AsyncClient", _FakeAsyncClient)

    await engagement_engine.resolve_video_url(
        "https://vt.tiktok.com/abc/",
        proxy="socks5://u:p@h:1080",
    )
    assert captured["proxy"] == "socks5://u:p@h:1080"
