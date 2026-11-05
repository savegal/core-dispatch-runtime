"""Тесты на анонимность: resolve_video_url не утекает IP сервера.

`resolve_video_url` разрешает короткие TikTok-ссылки (vt.tiktok.com / vm.tiktok.com).

Поведение зависит от флага USE_PROXY:
  - USE_PROXY=True и proxy=None — прямой HTTP-запрос НЕ делается (иначе TikTok
    увидит IP сервера); шортлинк позже разрешит page.goto через прокси аккаунта.
  - USE_PROXY=True и proxy задан — запрос идёт через этот прокси.
  - USE_PROXY=False — шортлинк разрешается напрямую через интернет исполнителя.
"""
import pytest


def _install_pool(monkeypatch, captured, resolved_url=None, forbid=False):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from executor import browser_pool
    async def resolve(url, **kwargs):
        if forbid:
            raise AssertionError("must not be called")
        captured["count"] = captured.get("count", 0) + 1
        captured["proxy"] = kwargs.get("proxy")
        return resolved_url
    get_pool = AsyncMock(return_value=SimpleNamespace(resolve_shortlink=AsyncMock(side_effect=resolve)))
    monkeypatch.setattr(browser_pool, "get_browser_pool", get_pool)
    # These cases exercise browser fallback; never make a real HTTP request.
    import httpx
    monkeypatch.setattr(httpx, "AsyncClient", lambda **_: (_ for _ in ()).throw(httpx.ConnectError("fixture HTTP unavailable")))
    return get_pool


@pytest.fixture
def use_proxy(monkeypatch):
    from executor import engagement_engine

    def _set(value: bool):
        monkeypatch.setattr(engagement_engine.settings, "USE_PROXY", value, raising=False)

    return _set


@pytest.mark.unit
@pytest.mark.asyncio
async def test_shortlink_rejects_external_location_before_following_it(monkeypatch, use_proxy):
    import httpx
    from executor import engagement_engine

    use_proxy(False)
    original_client = httpx.AsyncClient
    get_pool = _install_pool(monkeypatch, {}, forbid=True)
    requested = []
    short = "https://www.tiktok.com/t/ZFixture/"

    def handler(request):
        requested.append(str(request.url))
        return httpx.Response(302, headers={"Location": "http://127.0.0.1:4443/private"})

    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: original_client(
        transport=httpx.MockTransport(handler), **kwargs,
    ))
    assert await engagement_engine.resolve_video_url(short) == (short, None)
    assert requested == [short]
    get_pool.assert_not_awaited()


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
async def test_resolve_shortlink_without_proxy_skips_http(monkeypatch, use_proxy):
    """USE_PROXY=True и proxy=None: прямой httpx-запрос с реального IP запрещён."""
    from executor import engagement_engine

    use_proxy(True)
    captured = {}
    _install_pool(monkeypatch, captured, forbid=True)

    out_url, out_id = await engagement_engine.resolve_video_url("https://vt.tiktok.com/ZS9w8yPEh/")
    assert captured.get("count", 0) == 0, "при USE_PROXY=True без прокси httpx запрос делаться не должен"
    assert out_url == "https://vt.tiktok.com/ZS9w8yPEh/"
    assert out_id is None


@pytest.mark.unit
@pytest.mark.asyncio
async def test_resolve_shortlink_with_proxy_normalizes_proxy_url(monkeypatch, use_proxy):
    """USE_PROXY=True и proxy задан: httpx получает нормализованный proxy_url."""
    from executor import engagement_engine

    use_proxy(True)
    captured = {}
    _install_pool(monkeypatch, captured, "https://www.tiktok.com/@erjowkaaa/video/7638676938185264404")

    out_url, out_id = await engagement_engine.resolve_video_url(
        "https://vt.tiktok.com/ZS9w8yPEh/",
        proxy="user:pass@1.2.3.4:8080",
    )
    assert captured["proxy"] == "http://user:pass@1.2.3.4:8080"
    assert out_id == "7638676938185264404"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_resolve_shortlink_with_full_proxy_url_passes_as_is(monkeypatch, use_proxy):
    """USE_PROXY=True: уже полный proxy URL (со scheme) не получает второй scheme."""
    from executor import engagement_engine

    use_proxy(True)
    captured = {}
    _install_pool(monkeypatch, captured, "https://www.tiktok.com/@u/video/123")

    await engagement_engine.resolve_video_url("https://vt.tiktok.com/abc/", proxy="socks5://u:p@h:1080")
    assert captured["proxy"] == "socks5://u:p@h:1080"


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize("given_proxy", [None, "user:pass@1.2.3.4:8080"])
async def test_resolve_shortlink_direct_when_use_proxy_disabled(monkeypatch, use_proxy, given_proxy):
    """USE_PROXY=False: шортлинк разрешается напрямую (без прокси) с первого раза."""
    from executor import engagement_engine

    use_proxy(False)
    captured = {}
    _install_pool(monkeypatch, captured, "https://www.tiktok.com/@demo/photo/7635267329407913230")

    out_url, out_id = await engagement_engine.resolve_video_url("https://vt.tiktok.com/ZS9w8yPEh/", proxy=given_proxy)
    assert captured["count"] == 1
    assert captured["proxy"] is None
    assert out_id == "7635267329407913230"
    assert out_url.endswith("/photo/7635267329407913230")


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize("private_pool", [False, True])
async def test_order_resolver_skips_bad_proxies_without_spending_three_network_attempts(
    monkeypatch, use_proxy, db_session, sample_user, private_pool,
):
    from unittest.mock import AsyncMock
    from db.models import Account, Proxy
    from executor import engagement_engine
    from services import dedicated_proxy_service, proxy_health_service
    use_proxy(True)
    monkeypatch.setattr(dedicated_proxy_service, "get_current_dedicated_proxy", lambda: None)
    owner = sample_user.id if private_pool else None
    proxies = ["http://bad1:8000", "http://bad2:8000", "http://bad3:8000"]
    db_session.add_all([
        Account(label=f"resolve-bad-{i}", status="active", proxy=proxy, owner_user_id=owner)
        for i, proxy in enumerate(proxies)
    ] + [
        Account(label="resolve-error", status="error", proxy="http://aaa-error:8000", owner_user_id=owner),
        Account(label="resolve-good", status="active", proxy="http://good:8000", owner_user_id=owner),
        Account(label="resolve-other-pool", status="active", proxy="http://aaa-other:8000",
                owner_user_id=None if private_pool else sample_user.id),
        Account(label="resolve-dedicated", status="active", proxy="http://aaa-dedicated:8000", owner_user_id=owner),
        Proxy(proxy="http://aaa-dedicated:8000", status="active", dedicated_user_id=sample_user.id),
    ])
    db_session.commit()
    monkeypatch.setattr(proxy_health_service, "is_proxy_bad", lambda proxy: proxy in proxies)
    canonical = "https://www.tiktok.com/@demo/photo/7635267329407913230"
    resolve = AsyncMock(return_value=(canonical, "7635267329407913230"))
    monkeypatch.setattr(engagement_engine, "resolve_video_url", resolve)
    assert await engagement_engine.resolve_order_video_url(
        db_session, "https://vt.tiktok.com/fixture/", owner,
    ) == (canonical, "7635267329407913230")
    resolve.assert_awaited_once_with("https://vt.tiktok.com/fixture/", proxy="http://good:8000")


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize("all_bad", [False, True])
async def test_order_resolver_network_attempts_stay_bounded_and_never_fall_back_to_direct(
    monkeypatch, use_proxy, db_session, all_bad,
):
    from unittest.mock import AsyncMock
    from db.models import Account
    from executor import engagement_engine
    from services import dedicated_proxy_service, proxy_health_service
    use_proxy(True)
    monkeypatch.setattr(dedicated_proxy_service, "get_current_dedicated_proxy", lambda: None)
    db_session.add_all([Account(label=f"resolve-budget-{i}", status="active", proxy=f"http://proxy-{i}:8000") for i in range(4)])
    db_session.commit()
    monkeypatch.setattr(proxy_health_service, "is_proxy_bad", lambda proxy: all_bad)
    short = "https://vt.tiktok.com/fixture/"
    resolve = AsyncMock(return_value=(short, None))
    monkeypatch.setattr(engagement_engine, "resolve_video_url", resolve)
    assert await engagement_engine.resolve_order_video_url(db_session, short) == (short, None)
    assert resolve.await_count == (0 if all_bad else 3)
    assert all(call.kwargs["proxy"] for call in resolve.await_args_list)
