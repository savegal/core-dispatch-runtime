import asyncio
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, Mock

import pytest

from executor.static_asset_cache import StaticAssetCache
from executor.http.comment_scanner import scan_comment_ids
from executor.http import mentions as mention_mod
from executor import engagement_engine as engine, keyword_replies as keyword, user_schema as schema


@pytest.mark.unit
def test_static_asset_cache_strips_cdn_trace_and_timing_headers():
    cache = StaticAssetCache(max_total_bytes=1024 * 1024)
    url = "https://lf16-tiktok-web.tiktokcdn-us.com/obj/tiktok-web/sdk.js"
    body = b"console.log('tiktok sdk loaded successfully for test');" * 3
    headers = {
        "Content-Type": "application/javascript",
        "X-TT-Logid": "20261102000111222333",
        "X-TT-Trace-Host": "0123456789abcdef",
        "X-Cache": "TCP_MISS from a23-45-67-89.deploy.akamaitechnologies.com",
        "CF-Ray": "89abcdef0123-FRA",
        "Server-Timing": "cdn-cache; desc=MISS, edge; dur=42",
        "Date": "Mon, 02 Nov 2026 12:00:00 GMT",
        "Age": "12",
        "ETag": '"abc123"',
    }
    assert cache.put(url, 200, headers, body) is True
    entry = cache.get(url)
    assert entry is not None
    stored_keys = {k.lower() for k in entry.headers}
    for forbidden in (
        "x-tt-logid", "x-tt-trace-host", "x-cache", "cf-ray",
        "server-timing", "date", "age", "etag", "set-cookie",
    ):
        assert forbidden not in stored_keys
    assert entry.headers["access-control-allow-origin"] == "*"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_shared_browser_context_skips_os_window_resize(monkeypatch):
    from executor import device_profiles

    sent_commands = []

    class DummySession:
        async def send(self, method, params=None):
            sent_commands.append((method, params))
            if method == "Browser.getWindowForTarget":
                return {"windowId": 1}
            return {}

    context = SimpleNamespace(
        _account_device_profile=device_profiles.build_device_profile(
            SimpleNamespace(id=101, device_seed="a" * 64),
            None,
            "135.0.0.0",
        ),
        _shared_browser_process=True,
        new_cdp_session=AsyncMock(return_value=DummySession()),
    )
    page = SimpleNamespace(context=context)
    await device_profiles.prepare_device_page(page)
    methods = [m for m, _ in sent_commands]
    assert "Emulation.setUserAgentOverride" in methods
    assert "Browser.setWindowBounds" not in methods


@pytest.mark.unit
@pytest.mark.asyncio
async def test_mention_resolver_falls_back_to_disk_cache_on_transient_error(monkeypatch):
    username = "kuvg54"
    cached_identity = {
        "user_id": "7123456789012345678",
        "sec_uid": "MS4wLjABAAAA_kuvg54_valid_sec_uid_token",
        "unique_id": "kuvg54",
    }
    mention_mod._cache_put(username, cached_identity)
    page = SimpleNamespace(
        is_closed=lambda: False,
        evaluate=AsyncMock(return_value={"status": "transient_error", "reason": "http_429"}),
    )
    from services.mention_policy import set_task_mention_mode, reset_task_mention_mode

    token = set_task_mention_mode("clickable")
    try:
        resolved = await mention_mod.resolve_mention_user(page, username, timeout_ms=1000)
        assert resolved == cached_identity
        # Subsequent account in the same order shares the bound identity without another evaluate
        resolved_again = await mention_mod.resolve_mention_user(page, username, timeout_ms=1000)
        assert resolved_again == cached_identity
        assert page.evaluate.await_count == 1
    finally:
        reset_task_mention_mode(token)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_early_comment_list_interceptor_captures_first_page():
    listeners = {}

    def on(event, handler):
        listeners[event] = handler

    page = SimpleNamespace(
        url="https://www.tiktok.com/@u/video/7123456789012345678",
        on=on,
    )
    engine.install_early_comment_list_interceptor(
        page, "https://www.tiktok.com/@u/video/7123456789012345678"
    )
    assert "response" in listeners

    payload = {
        "status_code": 0,
        "comments": [{"cid": "7999888777666555444", "text": "Где?"}],
        "cursor": 20,
        "has_more": 1,
    }
    response = SimpleNamespace(
        url="https://www.tiktok.com/api/comment/list/?aweme_id=7123456789012345678&cursor=0&count=20",
        status=200,
        text=AsyncMock(return_value=json.dumps(payload)),
    )
    listeners["response"](response)
    await asyncio.sleep(0.05)
    assert getattr(page, "_cached_first_comment_page", None) == payload
    assert getattr(page, "_cached_first_comment_aweme_id", None) == "7123456789012345678"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_comment_missing_from_partial_window_is_inconclusive():
    wanted_cid = "7999000000000000999"
    pages_data = {
        0: {
            "comments": [
                {"cid": f"71000000000000000{i:02d}", "aweme_id": "7123456789012345678", "reply_id": "0"}
                for i in range(1, 21)
            ],
            "has_more": 1,
            "cursor": 20,
        },
        20: {
            "comments": [
                {"cid": f"72000000000000000{i:02d}", "aweme_id": "7123456789012345678", "reply_id": "0"}
                for i in range(1, 21)
            ],
            "has_more": 1,
            "cursor": 40,
        },
        40: {
            "comments": [
                {"cid": f"73000000000000000{i:02d}", "aweme_id": "7123456789012345678", "reply_id": "0"}
                for i in range(1, 21)
            ],
            "has_more": 1,
            "cursor": 60,
        },
    }

    async def read_page(cursor):
        return pages_data.get(cursor)

    report = await scan_comment_ids(
        SimpleNamespace(), "7123456789012345678", [wanted_cid],
        read_page=read_page, max_pages=3,
    )
    assert report["scanned"] == 60
    assert report["complete"] is False
    assert report["reason"] == "page_limit"
    assert report["absent_ids"] == []


@pytest.mark.unit
@pytest.mark.asyncio
async def test_auto_engagement_parent_picks_highest_liked_top_comment(monkeypatch):
    from executor.http import comment_discovery as discovery

    monkeypatch.setattr(engine, "http_first_enabled", lambda _: True)
    account = SimpleNamespace(id=1, proxy=None)
    monkeypatch.setattr(engine, "get_free_account_for_reply", Mock(return_value=account))
    monkeypatch.setattr(schema, "_preflight_cookie_http_gate", AsyncMock(return_value=None))
    monkeypatch.setattr(schema, "_ensure_account_login_ready", AsyncMock(return_value=(True, None)))
    monkeypatch.setattr(engine, "warmup_page_for_http", AsyncMock(return_value=({}, True, {})))
    page = SimpleNamespace(url="https://www.tiktok.com/@u/video/7123456789012345678")
    monkeypatch.setattr(engine, "create_page_for_account", AsyncMock(return_value=(object(), page)))
    monkeypatch.setattr(schema, "_safe_close", AsyncMock())
    monkeypatch.setattr(schema, "release_reply_account_reservation", Mock())

    comments = [
        {"cid": "7111111111111111111", "text": "Обычный коммент", "digg_count": 2, "user": {"unique_id": "u1"}},
        {"cid": "7222222222222222222", "text": "Где?", "digg_count": 53, "user": {"unique_id": "its_nothing66"}},
        {"cid": "7333333333333333333", "text": "Ещё коммент", "digg_count": 11, "user": {"unique_id": "u3"}},
    ]
    monkeypatch.setattr(discovery, "fetch_comment_page", AsyncMock(return_value={"comments": comments}))
    selected, err = await engine._resolve_auto_engagement_parent(
        None, "https://www.tiktok.com/@u/video/7123456789012345678"
    )
    assert err is None
    assert selected == {
        "cid": "7222222222222222222",
        "text": "Где?",
        "author": "its_nothing66",
    }


@pytest.mark.unit
def test_keyword_select_targets_captures_digg_count():
    rows = [
        {"cid": "7111111111111111111", "text": "Где?", "digg_count": 53, "user": {"unique_id": "its_nothing66"}},
        {"cid": "7222222222222222222", "text": "Где купить?", "digg_count": 5, "user": {"unique_id": "u2"}},
    ]
    targets = keyword.select_targets(rows, ["где"], set())
    assert len(targets) == 2
    assert targets[0]["digg_count"] == 53
    assert targets[1]["digg_count"] == 5


@pytest.mark.unit
@pytest.mark.asyncio
async def test_boost_target_replies_via_http_pool_reaches_target_likes(monkeypatch):
    url = "https://www.tiktok.com/@u/video/7123456789012345678"
    schema._BRANCH_PUBLISHED_REPLIES.clear()
    schema._BRANCH_PARENT_LIKED_ACCOUNTS.clear()

    parent_cid = "7999000000000000001"
    reply_cid = "7999000000000000002"
    schema._record_branch_published_reply(parent_cid, 1, reply_cid)
    # Initial 1 like from observer
    schema.record_reply_boost(reply_cid, 10, parent_id=parent_cid)

    accounts = [SimpleNamespace(id=20 + i, proxy="http://u:p@127.0.0.1:8080") for i in range(4)]
    monkeypatch.setattr(schema, "get_free_account_for_reply", Mock(side_effect=accounts + [None]))
    monkeypatch.setattr(schema, "_preflight_cookie_http_gate", AsyncMock(return_value=None))
    monkeypatch.setattr(schema, "create_page_for_account", AsyncMock(return_value=(object(), SimpleNamespace(url=url))))
    monkeypatch.setattr(schema, "_ensure_account_login_ready", AsyncMock(return_value=(True, None)))
    monkeypatch.setattr(engine, "warmup_page_for_http", AsyncMock(return_value=({}, True, {})))
    monkeypatch.setattr(schema, "_safe_close", AsyncMock())
    monkeypatch.setattr(schema, "release_reply_account_reservation", Mock())
    monkeypatch.setattr(schema.asyncio, "sleep", AsyncMock())

    fake_client = SimpleNamespace(like_comment=AsyncMock(return_value=(True, None, None)))
    from executor.http import playwright_http_client
    monkeypatch.setattr(playwright_http_client.PlaywrightHttpClient, "from_page", AsyncMock(return_value=fake_client))

    likes_map = await schema.boost_target_replies_via_http_pool(
        MagicMock(),
        url,
        "7123456789012345678",
        targets=[{"cid": reply_cid, "parent_id": parent_cid}],
        max_likes=4,
        exclude_account_ids={10},
    )
    assert likes_map[reply_cid] == 4
    assert schema.can_boost_reply(reply_cid, None, max_likes=4) is False
    assert [call.kwargs["cid"] for call in fake_client.like_comment.await_args_list] == [reply_cid] * 3


@pytest.mark.unit
def test_high_like_count_buttons_present_in_wizard_and_templates():
    from bot.handlers.order_wizard import _build_wizard_likes_keyboard
    from bot.handlers.templates import _build_template_likes_keyboard

    kb_w = _build_wizard_likes_keyboard(current_target="all", current_count=25)
    texts_w = [b.text for row in kb_w.inline_keyboard for b in row]
    assert any("✅ 25 шт" in t for t in texts_w)
    assert any("10 шт" in t for t in texts_w)
    assert any("15 шт" in t for t in texts_w)

    kb_t = _build_template_likes_keyboard(template_id=7, current_target="1", current_count=15)
    texts_t = [b.text for row in kb_t.inline_keyboard for b in row]
    assert any("✅ 15 шт" in t for t in texts_t)
    assert any("25 шт" in t for t in texts_t)


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize("wrong_parent", [False, True])
async def test_keyword_replies_supports_sort_by_likes_and_multiple_replies_per_target(monkeypatch, tmp_path, wrong_parent):
    from sqlalchemy import create_engine, event
    from sqlalchemy.orm import Session
    from db.database import Base
    from db.models import KeywordReplyTarget, Task, User
    from services.order_transport import set_task_order_transport, reset_task_order_transport

    db_engine = create_engine(
        f"sqlite:///{(tmp_path / 'kw_multi.db').as_posix()}",
        connect_args={"check_same_thread": False},
    )

    @event.listens_for(db_engine, "connect")
    def pragmas(connection, record):
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA foreign_keys=ON")

    Base.metadata.create_all(db_engine)
    url = "https://www.tiktok.com/@fixture/video/7123456789012345678"
    media = "7123456789012345678"

    with Session(db_engine, expire_on_commit=False) as db:
        db.add(User(id=1, telegram_id=9991, role="admin"))
        db.flush()
        task = Task(
            user_id=1,
            kind="keyword_replies",
            status="in_progress",
            main_comment="keyword",
            video_urls=[url],
            settings={
                "keywords": ["где"],
                "replies": ["узнал @kuvg54", "а вот и @kuvg54", "респект @kuvg54"],
                "reply_count": 3,
                "sort_targets_by_likes": True,
                "max_targets": 1,
                "replies_per_target": 3,
            },
        )
        db.add(task)
        db.commit()

        monkeypatch.setattr(engine, "resolve_order_video_url", AsyncMock(return_value=(url, media)))
        discovered = [
            {"cid": "7700000000000000001", "text": "Где купить?", "author": "low_likes", "digg_count": 2},
            {"cid": "7700000000000000002", "text": "Где?", "author": "its_nothing66", "digg_count": 53},
        ]
        monkeypatch.setattr(
            keyword,
            "discover_keyword_targets",
            AsyncMock(return_value=(discovered, {"complete": True, "scanned": 20, "error": None})),
        )
        sent_calls = []

        async def fake_send(*args, **kwargs):
            sent_calls.append(kwargs)
            cid = kwargs["comment_id"]
            outcomes = [
                {
                    "success": True,
                    "method": "http_user_schema",
                    "reply_id": str(int(cid) + 100 + idx),
                    "parent_comment_id": cid,
                    "network_confirmed": True,
                    "parent_confirmed": True,
                    "text": text,
                }
                for idx, text in enumerate(kwargs["replies"])
            ]
            if wrong_parent:
                outcomes[1]["response_parent_id"] = "7700000000000000001"
            return outcomes, cid, kwargs.get("comment_author")

        monkeypatch.setattr(engine, "execute_engagement_only_replies_via_full_flow", AsyncMock(side_effect=fake_send))
        token = set_task_order_transport("http", SimpleNamespace(TIKTOK_NETWORK_IMPL_READY=True))
        try:
            res = await keyword.execute_keyword_replies(db, task, url, task_id=task.id)
            recovered = await keyword.execute_keyword_replies(db, task, url, task_id=task.id)
        finally:
            reset_task_order_transport(token)

        assert len(sent_calls) == 1
        # Highest-liked target ("Где?" with 53 likes) was selected and received all 3 replies
        assert sent_calls[0]["comment_id"] == "7700000000000000002"
        assert sent_calls[0]["replies"] == ["узнал @kuvg54", "а вот и @kuvg54", "респект @kuvg54"]
        assert res.expected_reply_variants == 3
        assert res.matched_targets == 1
        assert [reply["variant_index"] for reply in res.replies] == [1, 2, 3]
        assert [reply["success"] for reply in res.replies] == [True, not wrong_parent, True]
        assert recovered.replies == res.replies
        if wrong_parent:
            assert res.replies[1]["error"] == "keyword_parent_mismatch"
            assert "reply_id" not in res.replies[1]
        persisted = db.query(KeywordReplyTarget).one()
        assert persisted.status == ("unconfirmed" if wrong_parent else "success")
    db_engine.dispose()


@pytest.mark.unit
def test_reply_account_soft_cooldown_deprioritizes_recently_used_account(db_session, monkeypatch):
    from db.models import Account
    from services import account_service

    now = datetime.now(timezone.utc)
    cookies_hot = json.dumps({
        "sessionid": "hot_sess_1",
        "sid_tt": "hot_sid_1",
        "sid_guard": "hot_guard_1",
        "ttwid": "hot_ttwid_1",
        "tt_csrf_token": "hot_csrf_1",
        "msToken": "hot_ms_1",
    })
    acc_recent = Account(
        label="recent_reply_acc",
        status="active",
        cookies=cookies_hot,
        proxy="http://u:p@127.0.0.1:8001",
        last_used_at=now - timedelta(seconds=30),
    )
    acc_rested = Account(
        label="rested_reply_acc",
        status="active",
        cookies=cookies_hot,
        proxy="http://u:p@127.0.0.1:8002",
        last_used_at=now - timedelta(seconds=600),
    )
    db_session.add_all([acc_recent, acc_rested])
    db_session.commit()

    monkeypatch.setattr(account_service.settings, "USE_PROXY", False)
    monkeypatch.setattr(account_service.settings, "REPLY_ACCOUNT_COOLDOWN_SECONDS", 300)

    picked = account_service.get_free_account_for_reply(db_session, reserve=False)
    assert picked is not None
    assert picked.id == acc_rested.id


@pytest.mark.unit
def test_device_identity_js_locks_touchpoints_colordepth_and_webgpu():
    import subprocess

    node_script = """
    const fs = require('fs');
    const code = fs.readFileSync('executor/device_identity.js', 'utf8');
    eval(code);

    const navProto = Object.getPrototypeOf(navigator);
    Object.defineProperty(navProto, 'maxTouchPoints', {
        get() { return 10; },
        configurable: true,
    });
    globalThis.window = globalThis;
    Object.defineProperty(globalThis, 'outerWidth', { get() { return 800; }, configurable: true });
    Object.defineProperty(globalThis, 'outerHeight', { get() { return 600; }, configurable: true });

    function Screen() {}
    Object.defineProperty(Screen.prototype, 'availWidth', { get() { return 800; }, configurable: true });
    Object.defineProperty(Screen.prototype, 'availHeight', { get() { return 600; }, configurable: true });
    Object.defineProperty(Screen.prototype, 'colorDepth', { get() { return 30; }, configurable: true });
    Object.defineProperty(Screen.prototype, 'pixelDepth', { get() { return 30; }, configurable: true });
    globalThis.Screen = Screen;
    globalThis.screen = new Screen();

    function GPUAdapter() {}
    Object.defineProperty(GPUAdapter.prototype, 'info', {
        get() {
            return { vendor: 'intel', architecture: 'gen-12lp', device: 'Intel Iris Xe', description: 'Intel Iris Xe' };
        },
        configurable: true,
    });
    globalThis.GPUAdapter = GPUAdapter;

    installAccountIdentity({
        family: 'windows',
        platform: 'Win32',
        canvasSeed: 12345,
        audioSeed: 67890,
        hardware: {
            width: 2560,
            height: 1440,
            hardwareConcurrency: 12,
            deviceMemory: 8,
            webglVendor: 'Google Inc. (AMD)',
            webglRenderer: 'ANGLE (AMD, AMD Radeon RX 6700 XT Direct3D11 vs_5_0 ps_5_0, D3D11)',
        },
        geometry: {
            availWidth: 2560,
            availHeight: 1350,
            outerWidth: 2560,
            outerHeight: 1440,
        },
    });

    const adapter = new GPUAdapter();
    console.log(JSON.stringify({
        maxTouchPoints: navigator.maxTouchPoints,
        colorDepth: screen.colorDepth,
        pixelDepth: screen.pixelDepth,
        availWidth: screen.availWidth,
        gpuVendor: adapter.info.vendor,
        gpuArch: adapter.info.architecture,
        gpuDesc: adapter.info.description,
    }));
    """
    res = subprocess.run(["node", "-e", node_script], capture_output=True, text=True, check=True)
    data = json.loads(res.stdout.strip())
    assert data["maxTouchPoints"] == 0
    assert data["colorDepth"] == 24
    assert data["pixelDepth"] == 24
    assert data["availWidth"] == 2560
    assert data["gpuVendor"] == "amd"
    assert data["gpuArch"] == "rdna-2"
    assert data["gpuDesc"] == "AMD Radeon RX 6700 XT"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_playwright_http_client_from_context_inherits_account_hardware_and_geo():
    from executor.device_profiles import build_device_profile
    from executor.http.playwright_http_client import PlaywrightHttpClient
    from executor.http.request_profile import RequestProfile

    account = SimpleNamespace(
        id=4040,
        label="ctx_acc",
        cookies=json.dumps({"sessionid": "sess_4040_abcdef1234567890", "device_id": "7400000000000004040"}),
        cookie_file=None,
        country_code="DE",
    )
    device_profile = build_device_profile(account, {}, "145.0.7632.6")
    context = SimpleNamespace(
        request=SimpleNamespace(),
        _pool_account_id=4040,
        _pool_account_obj=account,
        _account_device_profile=device_profile,
        _account_locale="de-DE",
        _account_proxy_in_use=True,
        _account_proxy_route=SimpleNamespace(country_code="DE"),
    )
    client = await PlaywrightHttpClient.from_context(context, page=None)
    hw = device_profile["identity"]["hardware"]
    assert client.runtime_profile["screen_width"] == hw["width"]
    assert client.runtime_profile["screen_height"] == hw["height"]
    assert client.runtime_profile["device_id"] == "7400000000000004040"

    params = RequestProfile.publish_main_profile().build_query_params(
        {"aweme_id": "7123456789012345678", "text": "hi"},
        runtime_profile=client.runtime_profile,
    )
    assert params["screen_width"] == str(hw["width"])
    assert params["screen_height"] == str(hw["height"])
    assert params["region"] == "DE"
    assert params["browser_language"] == "de-DE"


@pytest.mark.unit
def test_mention_resolver_js_includes_full_web_pc_telemetry_params():
    from executor.http import mentions

    for key in (
        "browser_version",
        "browser_online",
        "cookie_enabled",
        "screen_width",
        "screen_height",
        "history_len",
        "priority_region",
        "os: 'windows'",
    ):
        assert key in mentions._RESOLVE_USER_JS


@pytest.mark.unit
def test_recreated_order_settings_preserves_competitor_keyword_and_like_fields():
    from bot.handlers.orders import _build_recreated_order_settings

    src = {
        "mention_mode": "attempt",
        "reply_like_target": "all",
        "reply_like_count": 25,
        "sort_targets_by_likes": True,
        "max_targets": 1,
        "replies_per_target": 3,
        "url_results": {"old": {}},
    }
    clean = _build_recreated_order_settings(src, origin="keyword_replies")
    assert clean["reply_like_target"] == "all"
    assert clean["reply_like_count"] == 25
    assert clean["sort_targets_by_likes"] is True
    assert clean["max_targets"] == 1
    assert clean["replies_per_target"] == 3
    assert "url_results" not in clean


@pytest.mark.unit
@pytest.mark.asyncio
async def test_http_first_video_parser_extracts_ordered_aweme_ids_and_probes_item_list():
    from services import video_parser

    payload = {
        "statusCode": 0,
        "itemList": [
            {"id": "7400000000000000010", "createTime": 1700000010, "isPinnedItem": True, "author": {"uniqueId": "test_author"}},
            {"id": "7400000000000000099", "createTime": 1700000099, "author": {"uniqueId": "test_author"}},
            {"id": "7400000000000000050", "createTime": 1700000050, "author": {"uniqueId": "test_author"}},
        ],
    }
    ordered: list[tuple[str, bool, int | None]] = []
    seen: set[str] = set()
    added = video_parser._extract_aweme_ids_ordered_from_payload(payload, "test_author", ordered, seen)
    assert added == 3
    assert ordered[0] == ("7400000000000000010", True, 1700000010)
    assert ordered[1] == ("7400000000000000099", False, 1700000099)
    assert ordered[2] == ("7400000000000000050", False, 1700000050)

    page = SimpleNamespace(
        evaluate=AsyncMock(
            return_value={
                "secUid": "MS4wLjABAAAA_test_sec_uid",
                "profileReason": None,
                "statusCode": 0,
                "videoCount": 12,
                "itemListPayload": payload,
            }
        )
    )
    probe = await video_parser._probe_profile_videos_http_first(page, "test_author", limit=15)
    assert probe["secUid"] == "MS4wLjABAAAA_test_sec_uid"
    assert probe["profileReason"] is None
    assert probe["itemListPayload"] == payload


@pytest.mark.unit
@pytest.mark.asyncio
async def test_engagement_wizard_reply_likes_disabled_for_keyword_orders(monkeypatch):
    from aiogram.fsm.context import FSMContext
    from aiogram.fsm.storage.base import StorageKey
    from aiogram.fsm.storage.memory import MemoryStorage
    from bot.handlers import engagement_wizard as wizard
    from bot.state.engagement_states import EngagementStates

    storage = MemoryStorage()
    state = FSMContext(storage, StorageKey(bot_id=1, chat_id=555, user_id=555))
    user = SimpleNamespace(id=555, username="tester")
    msg = SimpleNamespace(chat=SimpleNamespace(id=555), message_id=10, edit_text=AsyncMock(), answer=AsyncMock())
    cb = SimpleNamespace(data="engagement:edit_reply_likes", from_user=user, message=msg, answer=AsyncMock())

    monkeypatch.setattr(wizard, "_boost_likes_ui_config", AsyncMock(return_value=(False, 0)))
    monkeypatch.setattr(wizard, "current_order_transport_label", AsyncMock(return_value="HTTP"))
    monkeypatch.setattr(wizard, "get_user_backend_id", AsyncMock(return_value=1))

    await state.set_state(EngagementStates.CONFIRM_ORDER)
    await state.update_data(
        order_kind="engagement_only",
        video_url="https://www.tiktok.com/@u/video/7123456789012345678",
        comment_text="где",
        replies=["Первый", "Второй"],
        account_pool_mode="shared",
    )
    await wizard.engagement_edit_reply_likes(cb, state)
    cb.data = "engagement:set_like_target:2"
    await wizard.engagement_set_like_target(cb, state)
    cb.data = "engagement:set_like_count:15"
    await wizard.engagement_set_like_count(cb, state)

    data_eng = await state.get_data()
    assert data_eng["reply_like_target"] == "2"
    assert data_eng["reply_like_count"] == 15

    # Now switch to keyword_replies and verify confirm_engagement_order strips reply_like_target / reply_like_count
    create_mock = AsyncMock(return_value={"task_id": 77, "order_transport_mode": "http"})
    monkeypatch.setattr(wizard.api_client, "create_task", create_mock)
    await state.update_data(order_kind="keyword_replies", keywords=["где"])
    for option in ("replies", "limit", "sort"):
        cb.data = f"engagement:keyword_options:{option}"
        await wizard.update_keyword_options(cb, state)
    cb.data = "engagement:confirm"
    await wizard.confirm_engagement_order(cb, state)
    sent_settings = create_mock.await_args.kwargs["settings"]
    assert "reply_like_target" not in sent_settings
    assert "reply_like_count" not in sent_settings
    assert sent_settings["replies_per_target"] == 2
    assert sent_settings["max_targets"] == 1
    assert sent_settings["sort_targets_by_likes"] is True
    await storage.close()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_keyword_templates_full_lifecycle_utils_api_and_wizard(monkeypatch):
    from aiogram.fsm.context import FSMContext
    from aiogram.fsm.storage.base import StorageKey
    from aiogram.fsm.storage.memory import MemoryStorage
    from api.templates import _normalize_template_reply_payload
    from bot.handlers import engagement_wizard as wizard
    from bot.services.template_utils import (
        KEYWORDS_MAIN_PLACEHOLDER,
        TEMPLATE_TYPE_KEYWORDS,
        filter_keyword_templates,
        is_keywords_template,
        template_keywords,
        template_type,
    )
    from bot.state.engagement_states import EngagementStates

    # 1. API & template_utils normalization
    settings, replies = _normalize_template_reply_payload(
        {"template_type": "keywords", "keywords": ["Где купить", "цена", "доставка"]},
        [None, None, None],
    )
    assert replies == []
    assert settings["template_type"] == TEMPLATE_TYPE_KEYWORDS
    assert settings["keywords"] == ["Где купить", "цена", "доставка"]

    tpl_obj = {"id": 42, "name": "Ключи магазин", "main_comment": KEYWORDS_MAIN_PLACEHOLDER, "settings": settings}
    assert is_keywords_template(tpl_obj) is True
    assert template_type(tpl_obj) == TEMPLATE_TYPE_KEYWORDS
    assert template_keywords(tpl_obj) == ["Где купить", "цена", "доставка"]
    assert filter_keyword_templates([tpl_obj, {"id": 99, "main_comment": "Обычный", "settings": {}}]) == [tpl_obj]

    # 2. Selecting keyword template in engagement_wizard
    storage = MemoryStorage()
    state = FSMContext(storage, StorageKey(bot_id=1, chat_id=777, user_id=777))
    user = SimpleNamespace(id=777, username="kw_user")
    msg = SimpleNamespace(chat=SimpleNamespace(id=777), message_id=20, edit_text=AsyncMock(), answer=AsyncMock())
    cb = SimpleNamespace(data="engagement:choose_keyword_template", from_user=user, message=msg, answer=AsyncMock())

    monkeypatch.setattr(wizard, "get_user_backend_id", AsyncMock(return_value=1))
    monkeypatch.setattr(wizard.api_client, "list_task_templates", AsyncMock(return_value=[tpl_obj]))
    monkeypatch.setattr(wizard.api_client, "get_task_template", AsyncMock(return_value=tpl_obj))

    await state.set_state(EngagementStates.INPUT_COMMENT_TEXT)
    await state.update_data(order_kind="keyword_replies", video_url="https://www.tiktok.com/@u/video/7123456789012345678")
    await wizard.choose_keyword_template(cb, state)
    assert await state.get_state() == EngagementStates.CHOOSE_KEYWORD_TEMPLATE.state

    cb.data = "engagement:keyword_template_select:42"
    await wizard.select_keyword_template(cb, state)
    data = await state.get_data()
    assert data["keywords"] == ["Где купить", "цена", "доставка"]
    assert data["keyword_template_id"] == 42
    assert await state.get_state() == EngagementStates.CHOOSE_REPLY_SOURCE.state
    await storage.close()
