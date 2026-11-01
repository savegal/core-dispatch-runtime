import asyncio
import base64
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from urllib.parse import parse_qs, urlencode, urlparse

import pytest

from executor import engagement_engine as engine, user_schema as schema
from executor.http import comment_discovery as discovery, playwright_http_client as client_module
from executor.http.request_profile import RequestProfile, generate_fallback_device_id
from executor.http.signature_runtime import wait_for_runtime_signer_ready
from services import geo_service
from services.proxy_normalizer import ProxyRoute
from services.log_sanitizer import sanitize_url


@pytest.mark.asyncio
async def test_worker_blogger_search_empty_result_renders_safe_query(monkeypatch):
    from bot.handlers import admin
    message = SimpleNamespace(text="absent_*`", answer=AsyncMock())
    state = SimpleNamespace(get_data=AsyncMock(return_value={"worker_blogger_user_id": 42}),
                            update_data=AsyncMock(), clear=AsyncMock())
    monkeypatch.setattr(admin.api_client, "list_video_creators", AsyncMock(return_value=[]))
    monkeypatch.setattr(admin.api_client, "get_user", AsyncMock(return_value={"username": "owner"}))
    await admin.search_worker_bloggers_input(message, state)
    text = message.answer.await_args.args[0]
    assert "ничего не найдено" in text
    assert admin.markdown_inline_code(message.text) in text
    assert message.answer.await_args.kwargs["reply_markup"].inline_keyboard
    state.clear.assert_not_awaited()


@pytest.mark.asyncio
async def test_composer_missing_variants_shows_keyboard_without_creating_task(monkeypatch):
    from bot.handlers import composer
    from bot.services.drafts import Draft
    draft = Draft(links=["https://www.tiktok.com/@user/video/123"], comment="text",
                  account_pool_mode="shared", text_variants=["text"])
    callback = SimpleNamespace(from_user=SimpleNamespace(id=123), answer=AsyncMock(),
                               message=SimpleNamespace(edit_text=AsyncMock()))
    monkeypatch.setattr(composer, "get_draft", lambda _: draft)
    monkeypatch.setattr(composer, "get_user_backend_id", AsyncMock(return_value=42))
    monkeypatch.setattr(composer.api_client, "has_active_accounts", AsyncMock(return_value=True))
    create_task = AsyncMock()
    monkeypatch.setattr(composer.api_client, "create_task", create_task)
    await composer.submit_order(callback, SimpleNamespace())
    answer = callback.message.edit_text.await_args
    assert "Нужно 3 варианта" in answer.args[0]
    callbacks = [button.callback_data for row in answer.kwargs["reply_markup"].inline_keyboard for button in row]
    assert "menu:new_order" in callbacks
    create_task.assert_not_awaited()

VIDEO = "7635267329407913230"
CID = "7641761192486194454"
URL = f"https://www.tiktok.com/@fixture/video/{VIDEO}"


@pytest.mark.parametrize("raw,password", [
    ("http://u:pa#?ss@localhost:8080", "pa#?ss"),
    ("socks5://u:pa#?ss@localhost:8080", "pa#?ss"),
    ("u:pa#?ss@localhost:8080", "pa#?ss"),
    ("localhost:8080:u:pa#?ss", "pa#?ss"),
    ("localhost:8080:u:pa@ss", "pa@ss"),
    ("http://u:pa%2523ss@localhost:8080", "pa%23ss"),
])
def test_proxy_password_survives_import_roundtrip_and_is_redacted(raw, password):
    from bot.handlers.admin_resources import parse_proxy_line
    route = ProxyRoute.parse(raw)
    assert route.password == password
    assert ProxyRoute.parse(route.url) == route
    parsed = parse_proxy_line(raw + " | https://rotation.fixture/change")
    assert parsed["proxy"] == route.url and parsed["is_mobile"] == "Y"
    assert password not in sanitize_url(raw)


@pytest.mark.parametrize("raw", ["http://localhost:8080/path", "localhost:0", "http://localhost:8080?x=1", "ftp://u:p@host:21"])
def test_invalid_proxy_is_rejected_by_bot_and_route(raw):
    from bot.handlers.admin_resources import parse_proxy_line
    assert parse_proxy_line(raw) is None


def test_fallback_identity_is_stable_persisted_and_native_ssr_supersedes_it(db_session):
    from db.models import Account
    from services.cookie_persistence_service import save_warmed_cookies_for_account, _inject_web_identity_records
    from services.cookie_validator import parse_cookies_to_dict
    account = Account(label="fallback-fixture", cookies='{"sessionid":"fixture"}')
    db_session.add(account); db_session.commit()
    identity = client_module.seed_web_identity_from_account(account)
    assert len(identity["device_id"]) == 19 and identity["device_id"].isdigit()
    assert identity["device_id"] == generate_fallback_device_id(account.device_seed)
    query = RequestProfile.comment_list_profile().build_query_params({}, runtime_profile=identity)
    assert query["device_id"] == identity["device_id"] and query["verifyFp"].startswith("verify_")
    assert save_warmed_cookies_for_account(account, {}, db_session)
    assert parse_cookies_to_dict(account.cookies)["device_id"] == identity["device_id"]
    client_module._ACCOUNT_WEB_IDENTITY_CACHE.pop(account.id, None)
    assert client_module.seed_web_identity_from_account(account)["device_id"] == identity["device_id"]
    native = {"device_id": "7612345678901234567", "web_id_last_time": "1712345678", "_web_id_last_time_explicit": True}
    updated = client_module.cache_web_identity(account.id, native, account=account)
    assert updated["device_id"] == native["device_id"]
    assert account.device_seed
    client_module.cache_web_identity(account.id, {"device_id": "7698765432109876543"}, account=account)
    rows, mapping = _inject_web_identity_records(
        [{"name": "WebIdLastTime", "value": "stale"}], account=account,
        previous={"device_id": identity["device_id"], "WebIdLastTime": "stale"})
    assert "WebIdLastTime" not in mapping and not any(row["name"] == "WebIdLastTime" for row in rows)


async def test_sdk_ready_does_not_claim_observed_signed_dispatch():
    page = SimpleNamespace(evaluate=AsyncMock(return_value={"sdk_present": True, "signed_dispatch_seen": False}))
    assert await wait_for_runtime_signer_ready(page, timeout_ms=6000)
    page.evaluate.assert_awaited_once()
    assert page._http_signer_state["signed_dispatch_seen"] is False


def test_reused_account_id_with_a_new_seed_does_not_reuse_old_web_identity():
    first = SimpleNamespace(id=-241026, device_seed="a" * 64, cookies=None)
    second = SimpleNamespace(id=first.id, device_seed="b" * 64, cookies=None)
    old = client_module.seed_web_identity_from_account(first)
    new = client_module.get_cached_web_identity(second.id, account=second)
    assert new["device_id"] == generate_fallback_device_id(second.device_seed)
    assert new["device_id"] != old["device_id"]


def test_new_fallback_does_not_reuse_an_unpaired_old_creation_time():
    account = SimpleNamespace(id=-241028, device_seed="c" * 64, cookies='{"WebIdLastTime":"1710000000"}')
    identity = client_module.seed_web_identity_from_account(account)
    assert "web_id_last_time" not in identity
    profile = RequestProfile.comment_list_profile()
    query = profile.build_query_params({}, {"WebIdLastTime": "1710000000"}, identity)
    assert "WebIdLastTime" not in query
    query = profile.build_query_params({}, {"WebIdLastTime": "1710000000"}, {**identity, "web_id_last_time": "1720000000"})
    assert query["WebIdLastTime"] == "1720000000"


@pytest.mark.parametrize("definitive", [False, True])
def test_reauthentication_requires_definitive_authentication_evidence(monkeypatch, definitive):
    from executor import cookie_health
    account = SimpleNamespace(id=-241027, validation_fail_count=10)
    monkeypatch.setattr(cookie_health, "_bump_validation_fail_count", lambda *args: 11)
    soft, retire = Mock(), Mock()
    monkeypatch.setattr(cookie_health, "mark_soft_error", soft)
    monkeypatch.setattr(cookie_health, "mark_needs_reauth", retire)
    retired = cookie_health.register_dead_session(object(), account, "session expired", definitive=definitive)
    assert retired is definitive and retire.call_count == int(definitive)
    assert soft.call_count == int(not definitive)


@pytest.mark.parametrize("signature", ["X-Bogus", "a_bogus"])
async def test_one_supported_signature_is_enough_for_get_without_retry(monkeypatch, signature):
    monkeypatch.setattr(discovery, "_extract_runtime_profile", AsyncMock(return_value={}))
    sleep = AsyncMock()
    monkeypatch.setattr(discovery.asyncio, "sleep", sleep)
    async def evaluate(script, url, **kwargs):
        return {"status": 200, "text": '{"status_code":0,"comments":[],"has_more":0}', "url": url + "&" + urlencode({signature: "fixture"})}
    page = SimpleNamespace(context=SimpleNamespace(cookies=AsyncMock(return_value=[])), evaluate=AsyncMock(side_effect=evaluate))
    assert await discovery.fetch_comment_page(page, VIDEO, fresh=True) is not None
    assert page.evaluate.await_count == 1 and sleep.await_count == 0


@pytest.mark.parametrize("foreign", [False, True])
async def test_header_token_rotation_reaches_next_cursor_without_touching_ads_cookie(monkeypatch, foreign):
    monkeypatch.setattr(discovery, "_extract_runtime_profile", AsyncMock(return_value={}))
    rows = [{"name": "msToken", "value": "old", "domain": ".tiktok.com", "path": "/"},
            {"name": "msToken", "value": "ads", "domain": "ads.tiktok.com", "path": "/"}]
    async def cookies(): return [dict(row) for row in rows]
    async def add_cookies(updated):
        assert all(row["domain"] != "ads.tiktok.com" for row in updated)
        for update in updated:
            for row in rows:
                if row["name"] == update["name"] and row["domain"] == update["domain"]:
                    row.update(update)
    seen = []
    async def evaluate(script, url, **kwargs):
        seen.append(parse_qs(urlparse(url).query)["msToken"][0])
        actual = url.replace("www.tiktok.com", "foreign.invalid") if foreign else url
        return {"status": 200, "text": '{"status_code":0,"comments":[],"has_more":0}',
                "url": actual + "&X-Bogus=fixture", "headers": {"X-Ms-Token": "rotated"}}
    context = SimpleNamespace(cookies=AsyncMock(side_effect=cookies), add_cookies=AsyncMock(side_effect=add_cookies))
    page = SimpleNamespace(context=context, evaluate=AsyncMock(side_effect=evaluate))
    async with discovery.comment_read_batch(page):
        await discovery.fetch_comment_page(page, VIDEO, cursor=0, fresh=True)
        await discovery.fetch_comment_page(page, VIDEO, cursor=20, fresh=True)
    assert seen == ["old", "old" if foreign else "rotated"]
    assert rows[1]["value"] == "ads"
    assert context.add_cookies.await_count == (0 if foreign else 2)


async def test_geo_ip_lookup_and_fallbacks_all_use_authenticated_http_proxy(monkeypatch):
    requests = []
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_): pass
        def do_GET(self):
            requests.append((self.path, self.headers.get("Proxy-Authorization")))
            self.send_response(503 if "geo-first" in self.path else 200)
            self.send_header("Content-Type", "application/json"); self.end_headers()
            data = {"ip": "198.51.100.7"} if "ip.fixture" in self.path else {"country_code": "DE"}
            self.wfile.write(json.dumps(data).encode())
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
    monkeypatch.setattr(geo_service, "PROXY_PUBLIC_IP_URL", "http://ip.fixture/json")
    monkeypatch.setattr(geo_service, "GEO_API_URLS", ["http://geo-first.fixture/{ip}", "http://geo-second.fixture/{ip}"])
    try:
        assert await geo_service.detect_proxy_country(f"http://u:pa#?ss@127.0.0.1:{server.server_port}") == "DE"
        assert len(requests) == 3
        assert [urlparse(url).hostname for url, _ in requests] == ["ip.fixture", "geo-first.fixture", "geo-second.fixture"]
        expected = "Basic " + base64.b64encode(b"u:pa#?ss").decode()
        assert all(auth == expected for _, auth in requests)
    finally:
        await asyncio.to_thread(server.shutdown); server.server_close(); thread.join(timeout=2)


async def test_auto_parent_scans_past_thirty_preflight_refusals_and_cleans_up(monkeypatch):
    monkeypatch.setattr(engine.settings, "MAX_RETRY_ACCOUNTS", 3)
    monkeypatch.setattr(engine.settings, "MAX_ACCOUNT_CANDIDATES", 50)
    monkeypatch.setattr(engine, "http_first_enabled", lambda _: True)
    accounts = [SimpleNamespace(id=value) for value in range(1, 37)]
    select = Mock(side_effect=accounts)
    monkeypatch.setattr(engine, "get_free_account_for_reply", select)
    async def preflight(db, account, **kwargs): return "http_preflight:dead" if account.id < 36 else None
    monkeypatch.setattr(schema, "_preflight_cookie_http_gate", preflight)
    monkeypatch.setattr(schema, "_ensure_account_login_ready", AsyncMock(return_value=(True, None)))
    monkeypatch.setattr(engine, "warmup_page_for_http", AsyncMock(return_value=({}, True, {})))
    page = SimpleNamespace(url=URL)
    create = AsyncMock(return_value=(object(), page)); monkeypatch.setattr(engine, "create_page_for_account", create)
    close = AsyncMock(); release = Mock()
    monkeypatch.setattr(schema, "_safe_close", close); monkeypatch.setattr(schema, "release_reply_account_reservation", release)
    fetch = AsyncMock(return_value={"comments": [{"cid": "bad"}, {"cid": CID, "text": "parent", "user": {"unique_id": "fixture"}}]})
    monkeypatch.setattr(discovery, "fetch_comment_page", fetch)
    selected, error = await engine._resolve_auto_engagement_parent(None, URL)
    assert selected["cid"] == CID and error is None
    assert select.call_count == 36 and create.await_count == 1
    assert release.call_count == close.await_count == 36
    fetch.assert_awaited_once_with(page, VIDEO, count=5)


def test_api_auto_target_does_not_use_reply_text_as_parent():
    from api.tasks import TaskCreate
    task = TaskCreate(user_id=1, kind="engagement_only", text_variants=["reply1", "reply2", "reply3"], video_urls=[URL])
    assert task.settings["engagement_target_mode"] == "auto_top"


async def test_parent_like_follows_successful_reply_once_and_can_be_disabled(monkeypatch):
    monkeypatch.setenv("LIKE_PARENT_BEFORE_REPLY", "1")
    monkeypatch.setattr(schema.asyncio, "sleep", AsyncMock())
    events = []
    async def like(**kwargs): events.append("like"); return True, None, None
    async def reply(**kwargs):
        events.append("reply")
        return True, "7641761192486194499", None, None, {"parent_confirmed": True, "parent_confirmation_level": "http_parent_id"}
    client = SimpleNamespace(like_comment=AsyncMock(side_effect=like), reply_comment=AsyncMock(side_effect=reply), last_request_meta={})
    session = SimpleNamespace(scope=schema.REPLY_SCOPE, disabled_reason=None, account_id=-241024, aweme_id=VIDEO, cookies={}, video_url=URL, client=client)
    schema._BRANCH_PARENT_LIKED_ACCOUNTS.pop(CID, None)
    for _ in range(2):
        assert (await schema._publish_reply_http_first_if_eligible(session, "reply", parent_comment_id=CID, correlation_id="fixture")).status == "success"
    assert events == ["reply", "like", "reply"]
    monkeypatch.setenv("LIKE_PARENT_BEFORE_REPLY", "0")
    await schema._publish_reply_http_first_if_eligible(session, "reply", parent_comment_id="7641761192486194455", correlation_id="fixture")
    assert client.like_comment.await_count == 1
