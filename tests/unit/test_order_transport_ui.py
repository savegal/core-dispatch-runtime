"""Order UI and transport verification with disposable databases and no live jobs."""
import asyncio
from contextlib import contextmanager
import inspect
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from httpx import AsyncClient, ASGITransport
from sqlalchemy import create_engine, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import sessionmaker
from starlette.requests import Request

from db.database import Base
from db.models import AppSettings, Task, User
from services import order_transport as transport


@pytest.fixture
async def transport_api(tmp_path, monkeypatch):
    from api import app_settings, auth
    from config.settings import get_settings
    engine = create_engine(f"sqlite:///{(tmp_path / 'orders.db').as_posix()}")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine, expire_on_commit=False)
    @contextmanager
    def db_context():
        with sessions() as session:
            yield session
    monkeypatch.setattr(app_settings, "get_sync_db_context", db_context)
    monkeypatch.setattr(auth, "verify_admin_token", lambda value: value == "local-test-admin")
    monkeypatch.setattr(get_settings(), "TIKTOK_NETWORK_IMPL_READY", True)
    monkeypatch.setattr(get_settings(), "TIKTOK_NETWORK_FIRST", False)
    app = FastAPI()
    app.include_router(app_settings.router)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://local-test") as client:
        yield client, sessions
    engine.dispose()


async def switch(client, mode):
    return await client.post("/api/settings/order_transport", headers={"X-Admin-Token": "local-test-admin"},
                       json={"order_transport_mode": mode})


async def test_real_settings_api_persists_switch_and_protects_admin(transport_api):
    client, sessions = transport_api
    for method in (client.get, client.post):
        kwargs = {"json": {"order_transport_mode": "dom"}} if method == client.post else {}
        assert (await method("/api/settings/order_transport", **kwargs)).status_code == 403
    for mode in ("http", "dom", "http"):
        response = await switch(client, mode)
        assert response.status_code == 200, response.text
        assert response.json()["order_transport_mode"] == mode
        with sessions() as db:
            assert transport.get_order_transport(db) == mode
        fresh = await client.get("/api/settings/order_transport", headers={"X-Admin-Token": "local-test-admin"})
        assert fresh.json()["order_transport_mode"] == mode
    assert (await switch(client, "fast")).status_code == 400
    with sessions() as db:
        assert transport.get_order_transport(db) == "http"


async def test_switch_does_not_report_success_on_failed_commit(transport_api, monkeypatch):
    from api import app_settings
    client, sessions = transport_api
    assert (await switch(client, "dom")).status_code == 200
    monkeypatch.setattr(app_settings, "set_order_transport", lambda *a, **k: False)
    assert (await switch(client, "http")).status_code == 500
    with sessions() as db:
        assert transport.get_order_transport(db) == "dom"


async def test_unavailable_http_is_disabled_in_api(transport_api, monkeypatch):
    from config.settings import get_settings
    client, _ = transport_api
    monkeypatch.setattr(get_settings(), "TIKTOK_NETWORK_IMPL_READY", False)
    assert (await switch(client, "http")).status_code == 409
    response = (await switch(client, "dom")).json()
    assert response["available_modes"] == ["dom"]
    assert response["http_available"] is False


async def test_fresh_read_refreshes_existing_session_identity_map(transport_api):
    client, sessions = transport_api
    await switch(client, "dom")
    with sessions() as db:
        old = db.execute(select(AppSettings).where(AppSettings.key == transport.ORDER_TRANSPORT_KEY)).scalar_one()
        assert old.value == "dom"
        await switch(client, "http")
        assert transport.get_order_transport(db) == "http"


@pytest.mark.asyncio
async def test_concurrent_orders_and_threads_keep_independent_modes():
    from executor.http.execution_policy import http_first_enabled
    settings = SimpleNamespace(TIKTOK_NETWORK_FIRST=False, TIKTOK_NETWORK_IMPL_READY=True)
    async def order(mode):
        token = transport.set_task_order_transport(mode, settings)
        try:
            await asyncio.sleep(0)
            assert http_first_enabled(settings) == (mode == "http")
            assert await asyncio.to_thread(http_first_enabled, settings) == (mode == "http")
            raise ValueError("simulated task failure")
        finally:
            transport.reset_task_order_transport(token)
    results = await asyncio.gather(order("dom"), order("http"), return_exceptions=True)
    assert all(isinstance(result, ValueError) for result in results)
    assert transport.current_order_transport() is None
    assert http_first_enabled(settings) is False


def test_http_order_cannot_silently_fall_back_when_implementation_is_disabled():
    from executor.http.execution_policy import http_first_enabled
    cfg = SimpleNamespace(TIKTOK_NETWORK_FIRST=True, TIKTOK_NETWORK_IMPL_READY=True)
    token = transport.set_task_order_transport("http", cfg)
    try:
        cfg.TIKTOK_NETWORK_IMPL_READY = False
        with pytest.raises(RuntimeError, match="unavailable"):
            http_first_enabled(cfg)
    finally:
        transport.reset_task_order_transport(token)


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["full", "engagement_only"])
async def test_real_creation_snapshots_server_policy_and_idempotency_keeps_original(tmp_path, monkeypatch, kind):
    from api import tasks
    engine = create_async_engine(f"sqlite+aiosqlite:///{(tmp_path / 'creation.db').as_posix()}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    for name, result in (("get_boost_likes_config_async", (False, 0)),
                         ("get_boost_likes_target_mode_async", "single_top"),
                         ("get_order_execution_strategy_async", "balanced")):
        monkeypatch.setattr(tasks, name, AsyncMock(return_value=result))
    monkeypatch.setattr(tasks, "reserve_task_for_enqueue_async", AsyncMock())
    monkeypatch.setattr(tasks, "finalize_reserved_enqueue_async", AsyncMock())
    monkeypatch.setattr(tasks, "claim_dedup_key", lambda *a, **k: True)
    async with async_sessionmaker(engine, expire_on_commit=False)() as db:
        db.add(User(id=1, telegram_id=7890, role="admin", is_blocked=False))
        setting = AppSettings(key=transport.ORDER_TRANSPORT_KEY, value="dom")
        db.add(setting)
        await db.commit()
        payload = tasks.TaskCreate(user_id=1, main_comment="Первый 🔥", text_variants=["Первый 🔥", "Второй", "Третий"],
            video_urls=["https://www.tiktok.com/@demo/video/1234567890123456789"], kind=kind,
            engagement_comment_text="искомый текст", replies=["Ответ 👨‍👩‍👧", "Ещё ответ"],
            settings={transport.ORDER_TRANSPORT_KEY: "http", "verify_reply_visibility": True, "template_id": 5,
                      "replies": ["Ответ 👨‍👩‍👧", "Ещё ответ"]})
        async def create(key):
            request = Request({"type": "http", "headers": [(b"x-idempotency-key", key.encode())]})
            return await inspect.unwrap(tasks.create_task)(request=request, task_data=payload, db=db, auth_user_id=1)
        first = await create("same-request")
        original = await db.get(Task, first["task_id"])
        assert first[transport.ORDER_TRANSPORT_KEY] == original.settings[transport.ORDER_TRANSPORT_KEY] == "dom"
        assert original.settings["verify_reply_visibility"] is True
        assert original.settings["replies"] == ["Ответ 👨‍👩‍👧", "Ещё ответ"]
        setting.value = "http"
        await db.commit()
        repeat = await create("same-request")
        assert repeat["task_id"] == first["task_id"]
        assert repeat[transport.ORDER_TRANSPORT_KEY] == "dom"
        second = await create("different-request")
        assert second[transport.ORDER_TRANSPORT_KEY] == "http"
        assert original.settings[transport.ORDER_TRANSPORT_KEY] == "dom"
        assert len((await db.execute(select(Task))).scalars().all()) == 2
    await engine.dispose()


@pytest.mark.asyncio
async def test_http_api_switch_then_create_full_and_reply_orders(transport_api, monkeypatch):
    """Real ASGI requests, real persistence, isolated enqueue boundary."""
    from api import tasks, auth
    from db.database import get_db
    client, sessions = transport_api
    bind = sessions.kw["bind"]
    engine = create_async_engine(str(bind.url).replace("sqlite:", "sqlite+aiosqlite:"))
    async def db_dependency():
        async with async_sessionmaker(engine, expire_on_commit=False)() as db:
            yield db
    app = client._transport.app
    app.include_router(tasks.router)
    app.state.limiter = tasks.limiter
    app.dependency_overrides[get_db] = db_dependency
    app.dependency_overrides[auth.verify_user_header] = lambda: 1
    with sessions() as db:
        db.add(User(id=1, telegram_id=2222, role="admin", is_blocked=False))
        db.commit()
    for name, value in (("get_boost_likes_config_async", (False, 0)),
                        ("get_boost_likes_target_mode_async", "single_top"),
                        ("get_order_execution_strategy_async", "balanced")):
        monkeypatch.setattr(tasks, name, AsyncMock(return_value=value))
    reserve, finalize = AsyncMock(), AsyncMock()
    monkeypatch.setattr(tasks, "reserve_task_for_enqueue_async", reserve)
    monkeypatch.setattr(tasks, "finalize_reserved_enqueue_async", finalize)
    monkeypatch.setattr(tasks, "claim_dedup_key", lambda *a, **k: True)
    for mode in ("dom", "http"):
        assert (await switch(client, mode)).status_code == 200
        for kind in ("full", "engagement_only"):
            response = await client.post("/api/tasks", json={
                "user_id": 1, "main_comment": "Текст", "text_variants": ["Первый", "Второй", "Третий"],
                "video_urls": ["https://www.tiktok.com/@u/video/1234567890123456789"],
                "kind": kind, "engagement_comment_text": "поиск",
                "settings": {"order_transport_mode": "incorrect", "replies": ["Ответ"]}})
            assert response.status_code == 200, response.text
            assert response.json()["order_transport_mode"] == mode
            with sessions() as db:
                task = db.get(Task, response.json()["task_id"])
                assert task.settings["order_transport_mode"] == mode
                assert task.kind == kind
    assert reserve.await_count == finalize.await_count == 4
    await engine.dispose()


@pytest.mark.parametrize("mode", ["dom", "http"])
def test_monitor_settings_override_template_transport(mode):
    from services.task_creation_service import _build_monitor_task_settings
    result = _build_monitor_task_settings(
        SimpleNamespace(id=1, username="demo", user_id=1), SimpleNamespace(id=2, url="https://fixture/video/3"),
        SimpleNamespace(id=5, name="Шаблон", main_comment="main", reply1=None, reply2=None, reply3=None,
                        settings={"text_variants": ["a", "b", "c"], transport.ORDER_TRANSPORT_KEY: "stale"}),
        origin="autopilot", created_from="monitor_worker", boost_likes_enabled=False,
        boost_likes_quantity=0, order_transport_mode=mode)
    assert result[transport.ORDER_TRANSPORT_KEY] == mode


def callback(data="admin:order_strategy:set:http"):
    return SimpleNamespace(data=data, from_user=SimpleNamespace(id=123, username="test"),
                           answer=AsyncMock(), message=SimpleNamespace(edit_text=AsyncMock()))


@pytest.mark.asyncio
async def test_admin_ui_renders_only_server_confirmed_mode(monkeypatch):
    from bot.handlers import admin
    cb = callback()
    monkeypatch.setattr(admin.api_client, "update_order_transport", AsyncMock(return_value={
        "order_transport_mode": "http", "available_modes": ["dom", "http"]}))
    monkeypatch.setattr(admin, "log_admin_action", AsyncMock())
    render = AsyncMock()
    monkeypatch.setattr(admin, "_safe_edit_callback_text", render)
    await admin.set_order_strategy_handler(cb)
    assert "Сейчас: *HTTP — быстрый*" in render.call_args.args[1]
    render.reset_mock()
    admin.api_client.update_order_transport.return_value = {"order_transport_mode": "dom"}
    await admin.set_order_strategy_handler(cb)
    render.assert_not_called()
    assert "Не удалось сохранить" in cb.answer.call_args.args[0]


def test_admin_keyboard_has_two_clear_modes_and_no_placeholder_strategy():
    from bot.handlers.admin import _build_order_strategy_text
    text, keyboard = _build_order_strategy_text("dom")
    assert "HTTP-запросами с браузерной сессией" in text
    assert "полных заказов" in text
    assert "«Ответы по ключевым словам» всегда выполняются через HTTP" in text
    assert [row[0].callback_data for row in keyboard.inline_keyboard[:-1]] == [
        "admin:order_strategy:set:dom", "admin:order_strategy:set:http"]
    assert "Balanced" not in text and "future" not in text


def test_copy_preserves_user_options_without_copying_execution_receipts_or_transport():
    from bot.handlers.orders import _build_recreated_order_settings
    source = {"verify_main_visibility": True, "verify_reply_visibility": True, "reply_like_target": "all",
        "reply_like_count": 3, "boost_likes_enabled": False, "url_results": {"u": {"status": "success"}},
        "order_transport_mode": "dom", "idempotency_key": "old", "mention_mode": "attempt"}
    copied = _build_recreated_order_settings(source, origin="manual_retry")
    for key in ("verify_main_visibility", "verify_reply_visibility", "reply_like_target", "reply_like_count",
                "boost_likes_enabled", "mention_mode"):
        assert copied[key] == source[key]
    for key in ("order_transport_mode", "url_results", "idempotency_key"):
        assert key not in copied


def test_template_draft_round_trip_keeps_replies_and_summary():
    from bot.services.drafts import Draft
    from bot.keyboards.composer import format_composer_summary
    draft = Draft(links=["https://fixture/video/1"], comment="Комментарий", text_variants=["a", "b", "c"],
        replies=["Первый 👨‍👩‍👧", "Второй"], template_id=7, template_name="Тест", verify_reply_visibility=True)
    restored = Draft.from_dict(draft.to_dict())
    assert restored.replies == draft.replies
    assert restored.template_id == 7 and restored.verify_reply_visibility
    text = format_composer_summary(restored, transport_label="DOM — через браузер")
    assert "Первый 👨‍👩‍👧" in text and "Второй" in text
    assert "Ответов: *2*" in text and "DOM — через браузер" in text


def test_numeric_reply_order_after_fsm_json_serialization():
    from bot.services.template_utils import replies_dict_to_list
    from bot.services.order_creation_ui import build_preview_text
    replies = {"10": "десятый", 2: "второй", "1": "первый"}
    assert replies_dict_to_list(replies) == ["первый", "второй", "десятый"]
    text = build_preview_text(links=[], text_variants=["a"], replies=replies)
    assert text.index("первый") < text.index("второй") < text.index("десятый")


@pytest.mark.asyncio
async def test_optional_mode_preview_is_bounded_when_api_stalls():
    from bot.services.order_creation_ui import current_order_transport_label
    async def stalled():
        await asyncio.Event().wait()
    started = asyncio.get_running_loop().time()
    label = await current_order_transport_label(SimpleNamespace(get_order_transport_settings=stalled))
    assert 1.8 <= asyncio.get_running_loop().time() - started < 3.0
    assert label == "Уточняется при создании"


def test_order_details_use_stored_mode_and_success_uses_server_mode():
    from bot.handlers.orders import _overview_order_plan_lines, _render_order_settings
    from bot.services.order_creation_ui import build_success_text
    task = {"id": 7, "status": "pending", "kind": "full", "settings": {"order_transport_mode": "dom"},
            "video_urls": [], "main_comment": "Текст"}
    assert "DOM — через браузер" in "\n".join(_overview_order_plan_lines(task))
    assert "DOM — через браузер" in _render_order_settings(task)
    assert "HTTP — быстрый" in build_success_text(task_id=8, links_count=1, reply_count=0,
                                                 variants_count=3, transport_mode="http")


@pytest.mark.asyncio
async def test_composer_submits_template_replies_options_and_authoritative_success(monkeypatch):
    from bot.handlers import composer
    from bot.services.drafts import Draft
    draft = Draft(links=["https://www.tiktok.com/@u/video/1234567890123456789"], comment="a",
        text_variants=["a", "b", "c"], replies=["Ответ 1", "Ответ 2", "Ответ 3", "Ответ 4"],
        template_id=7, template_name="Шаблон", verify_reply_visibility=True)
    monkeypatch.setattr(composer, "get_draft", lambda uid: draft)
    monkeypatch.setattr(composer, "reset_draft", lambda uid: None)
    monkeypatch.setattr(composer.api_client, "has_active_accounts", AsyncMock(return_value=True))
    create = AsyncMock(return_value={"task_id": 42, "order_transport_mode": "dom"})
    monkeypatch.setattr(composer.api_client, "create_task", create)
    monkeypatch.setattr(composer, "get_user_backend_id", AsyncMock(return_value=1))
    monkeypatch.setattr(composer, "_composer_likes_label", AsyncMock(return_value="выключены"))
    mention = AsyncMock(return_value=SimpleNamespace(mode="plain", request_kwargs={}))
    monkeypatch.setattr(composer, "ensure_mention_choice", mention)
    cb = callback("composer:submit")
    await composer.submit_order(cb, SimpleNamespace(clear=AsyncMock()))
    create.assert_awaited_once()
    assert create.call_args.kwargs["replies"] == draft.replies
    assert create.call_args.kwargs["settings"]["template_id"] == 7
    assert create.call_args.kwargs["settings"]["verify_reply_visibility"] is True
    assert mention.call_args.args[2] == ["a", "b", "c", *draft.replies]
    assert "DOM — через браузер" in cb.message.edit_text.call_args.args[0]
    assert "Ответов: *4*" in cb.message.edit_text.call_args.args[0]


@pytest.mark.asyncio
async def test_replies_template_entry_keeps_template_through_video_and_search(monkeypatch):
    from bot.handlers import engagement_wizard as wizard
    from bot.handlers.templates import _template_detail_keyboard
    class State:
        data = {}
        async def clear(self): self.data = {}
        async def update_data(self, **kwargs): self.data.update(kwargs)
        async def get_data(self): return dict(self.data)
        async def set_state(self, value): self.current = value
    state = State()
    template = {"id": 7, "name": "Ответы", "settings": {"template_type": "replies_only", "replies": ["Ответ"]}}
    monkeypatch.setattr(wizard.api_client, "sync_telegram", AsyncMock(return_value={"id": 1, "role": "admin"}))
    monkeypatch.setattr(wizard, "check_user_access", AsyncMock(return_value=True))
    monkeypatch.setattr(wizard.api_client, "get_task_template", AsyncMock(return_value=template))
    data = _template_detail_keyboard(7, replies_only=True).inline_keyboard[0][0].callback_data
    assert data == "engagement:from_template:7"
    await wizard.start_engagement_wizard(callback(data), state)
    assert state.data["replies"] == ["Ответ"] and state.data["template_id"] == 7
    render = AsyncMock()
    monkeypatch.setattr(wizard, "show_engagement_preview", render)
    await wizard.process_comment_text(SimpleNamespace(text="@author: полезный текст", answer=AsyncMock()), state)
    render.assert_awaited_once()
    assert state.data["username"] == "author"
    assert state.data["template_id"] == 7


@pytest.mark.asyncio
async def test_admin_middleware_rejects_global_mode_callback_from_regular_user(monkeypatch):
    from unittest.mock import Mock
    from aiogram.types import CallbackQuery
    from bot.middleware import admin_middleware
    cb = Mock(spec=CallbackQuery)
    cb.from_user = SimpleNamespace(id=987654321)
    cb.answer = AsyncMock()
    monkeypatch.setattr(admin_middleware.api_client, "sync_telegram", AsyncMock(return_value={"role": "user"}))
    handler = AsyncMock()
    await admin_middleware.AdminMiddleware()(handler, cb, {})
    handler.assert_not_called()
    assert "Доступ запрещён" in cb.answer.call_args.args[0]


def test_dense_template_previews_fit_telegram_limit_without_losing_stored_content():
    from bot.handlers.templates import _build_template_detail_text, _template_detail_keyboard
    from bot.keyboards.templates import format_template_preview
    from bot.services.order_creation_ui import build_preview_text
    replies = ["`_*[]👨‍👩‍👧" * 80 for _ in range(20)]
    template = {"id": 7, "name": "`_*[]" * 20, "settings": {
        "replies": replies, "text_variants": ["`_*[]" * 100] * 3}}
    texts = [_build_template_detail_text(template), format_template_preview(template, reply_count=20),
        build_preview_text(links=["https://fixture/" + "_" * 200] * 100,
                           text_variants=template["settings"]["text_variants"], replies=dict(enumerate(replies, 1)),
                           template_name=template["name"], transport_label="HTTP — быстрый")]
    for text in texts:
        assert len(text.encode("utf-16-le")) // 2 <= 4096
    assert len(template["settings"]["replies"]) == 20
    for row in _template_detail_keyboard(7).inline_keyboard:
        assert all(len(button.callback_data.encode()) <= 64 for button in row)


@pytest.mark.asyncio
@pytest.mark.parametrize("mode,stage", [("http", "warmup"), ("dom", "bootstrap")])
async def test_main_publish_pipeline_obeys_transport_without_cross_mode_fallback(monkeypatch, mode, stage):
    import executor.engagement_engine as engine
    monkeypatch.setattr(engine.settings, "USE_BAS", False)
    monkeypatch.setattr(engine.settings, "TIKTOK_NETWORK_IMPL_READY", True)
    monkeypatch.setattr(engine, "get_http_execution_strategy", lambda *a, **k: {"strategy": "http_first"})
    monkeypatch.setattr(engine, "create_page_for_account", AsyncMock(return_value=(SimpleNamespace(), SimpleNamespace())))
    monkeypatch.setattr(engine, "track_publication_requests", lambda *a: lambda: None)
    monkeypatch.setattr(engine, "_should_skip_http_for_account", lambda *a: None)
    warmup = AsyncMock(return_value=({}, False, {"reason_code": "test_warmup_not_ready"}))
    monkeypatch.setattr(engine, "warmup_page_for_http", warmup)
    monkeypatch.setattr(engine, "needs_enrichment", lambda account: False)
    bootstrap = AsyncMock(return_value=SimpleNamespace(status="timeout", can_attempt_publish=False, cookies={}))
    monkeypatch.setattr(engine, "bootstrap_session", bootstrap)
    monkeypatch.setattr(engine, "_register_runtime_failure", lambda *a, **k: None)
    monkeypatch.setattr(engine, "_close_attempt_resources", AsyncMock())
    token = transport.set_task_order_transport(mode)
    try:
        result = await engine._attempt_publish_on_account(SimpleNamespace(rollback=lambda: None),
            SimpleNamespace(id=7, cookies="[]"), "https://www.tiktok.com/@u/video/1234567890123456789",
            "Тест", 42, "order_mode_test", mode="main")
        assert result[2] == stage
        assert warmup.await_count == (1 if mode == "http" else 0)
        assert bootstrap.await_count == (1 if mode == "dom" else 0)
    finally:
        transport.reset_task_order_transport(token)


@pytest.mark.asyncio
async def test_template_apply_transfers_replies_to_order_draft(monkeypatch):
    from bot.handlers import templates
    from bot.services.drafts import Draft
    draft = Draft(links=["https://fixture/video/1"])
    template = {"id": 7, "name": "Шаблон", "main_comment": "a", "settings": {
        "text_variants": ["a", "b", "c"], "replies": ["Ответ 1", "Ответ 2"],
        "verify_reply_visibility": True, "reply_like_target": "all", "reply_like_count": 2}}
    monkeypatch.setattr(templates, "_load_template_for_user", AsyncMock(return_value=(1, template)))
    monkeypatch.setattr(templates, "get_draft", lambda uid: draft)
    monkeypatch.setattr(templates, "save_draft", lambda *a: None)
    monkeypatch.setattr(templates, "_format_template_composer_summary", AsyncMock(return_value="preview"))
    await templates.apply_task_template(callback("template:apply:7"))
    assert draft.replies == ["Ответ 1", "Ответ 2"]
    assert draft.template_id == 7 and draft.verify_reply_visibility is True


@pytest.mark.asyncio
async def test_explicit_dom_routes_reply_to_ui_even_for_http_ready_account(monkeypatch):
    import executor.engagement_engine as engine
    account = SimpleNamespace(id=7)
    monkeypatch.setattr(engine, "get_free_account_for_reply", lambda *a, **k: account)
    monkeypatch.setattr(engine, "get_http_execution_strategy", lambda *a, **k: {"strategy": "http_first"})
    post = AsyncMock(return_value=SimpleNamespace(success=True, comment_id="1234567890123456789"))
    monkeypatch.setattr(engine, "post_reply", post, raising=False)
    token = transport.set_task_order_transport("dom")
    try:
        result = await engine.execute_replies_http(SimpleNamespace(), "https://www.tiktok.com/@u/video/1234567890123456789",
            "9876543210987654321", ["Ответ"], task_id=99)
        assert post.await_count == 1
        assert result[0]["success"] is True and result[0]["method"] == "dom_fallback"
    finally:
        transport.reset_task_order_transport(token)
