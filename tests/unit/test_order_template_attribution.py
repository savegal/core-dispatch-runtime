"""Проверяем привязку шаблона к заказу.

После создания заказа из шаблона (любым путём — wizard / quick_order /
new_videos / monitor) в task.settings должны лежать template_id и
template_name. UI orders должен отображать их в детальном экране заказа.
"""
import inspect

import pytest


# ─── monitor: _build_monitor_task_settings ─────────────────────────────────

@pytest.mark.unit
def test_monitor_task_settings_carries_template_id_and_name():
    """Auto-comment монитора кладёт template_id/template_name в settings."""
    from services.task_creation_service import _build_monitor_task_settings
    from types import SimpleNamespace

    creator = SimpleNamespace(id=10, username="erjowkaaa", user_id=42)
    video = SimpleNamespace(id=999, url="https://www.tiktok.com/@u/video/123")
    template = SimpleNamespace(
        id=7,
        name="Промо-шаблон 🔥",
        main_comment="привет",
        reply1=None, reply2=None, reply3=None,
        settings={"text_variants": ["вариант 1", "вариант 2", "вариант 3"]},
    )

    out = _build_monitor_task_settings(
        creator, video, template,
        origin="autopilot",
        created_from="monitor_worker",
        boost_likes_enabled=True,
        boost_likes_quantity=100,
    )
    assert out["template_id"] == 7
    assert out["template_name"] == "Промо-шаблон 🔥"
    assert out["creator_username"] == "erjowkaaa"
    assert out["creator_source_id"] == 10
    assert out["origin"] == "autopilot"


@pytest.mark.unit
def test_monitor_task_settings_template_name_fallback_when_empty():
    """Если у шаблона нет name, ставим читаемый fallback вместо None."""
    from services.task_creation_service import _build_monitor_task_settings
    from types import SimpleNamespace

    creator = SimpleNamespace(id=1, username="x", user_id=2)
    video = SimpleNamespace(id=3, url="u")
    template = SimpleNamespace(
        id=42,
        name=None,
        main_comment="m",
        reply1=None, reply2=None, reply3=None,
        settings={"text_variants": ["a", "b", "c"]},
    )

    out = _build_monitor_task_settings(
        creator, video, template,
        origin="autopilot", created_from="x",
        boost_likes_enabled=False, boost_likes_quantity=0,
    )
    assert out["template_name"] == "Шаблон #42"


# ─── orders UI: рендер настроек ────────────────────────────────────────────

@pytest.mark.unit
def test_render_order_settings_shows_template_block_when_set():
    """Если task.settings.template_id/name есть — они появляются в экране."""
    from bot.handlers.orders import _render_order_settings

    task = {
        "id": 100,
        "settings": {
            "origin": "wizard_template",
            "template_id": 7,
            "template_name": "Промо 🔥",
            "delays": True, "shuffle": False, "rotate_accounts": True,
        },
    }
    text = _render_order_settings(task)
    assert "Шаблон" in text
    assert "Промо" in text
    assert "#7" in text


@pytest.mark.unit
def test_render_order_settings_omits_template_when_missing():
    """Без template_id строка про шаблон отсутствует."""
    from bot.handlers.orders import _render_order_settings

    task = {
        "id": 100,
        "settings": {
            "origin": "manual",
            "delays": True, "shuffle": False, "rotate_accounts": True,
        },
    }
    text = _render_order_settings(task)
    # В тексте есть «Источник», но не «Шаблон»
    assert "Источник" in text
    assert "Шаблон" not in text


@pytest.mark.unit
def test_render_order_settings_shows_creator_username_for_monitor_orders():
    """Для заказов из автомониторинга показываем блогера-источника."""
    from bot.handlers.orders import _render_order_settings

    task = {
        "id": 100,
        "settings": {
            "origin": "autopilot",
            "creator_username": "erjowkaaa",
            "delays": True, "shuffle": False, "rotate_accounts": True,
        },
    }
    text = _render_order_settings(task)
    assert "Блогер" in text
    assert "erjowkaaa" in text


# ─── _origin_label ─────────────────────────────────────────────────────────

@pytest.mark.unit
def test_origin_label_covers_all_real_origins():
    """В UI должны быть человекочитаемые подписи всех реально используемых
    origin-значений (а не raw-строка вроде 'wizard_template').
    """
    from bot.handlers.orders import _origin_label

    seen_origins = [
        "manual", "composer", "wizard", "wizard_template",
        "quick_order", "autopilot", "manual_review",
        "new_videos", "monitor_auto_comment", "monitor_manual_review",
        "notification", "engagement_only", "retry_failed",
    ]
    for origin in seen_origins:
        label = _origin_label(origin)
        assert label, f"empty label for origin={origin}"
        # Должен быть осмысленный текст, не сама raw-строка.
        assert label != origin, f"origin {origin!r} не переведён, остался raw"


# ─── handlers фиксы: что код реально содержит ─────────────────────────────

@pytest.mark.unit
def test_order_wizard_create_with_template_attaches_template_id():
    """Wizard при confirm_with_template кладёт template_id в task_settings."""
    from bot.handlers import order_wizard
    src = inspect.getsource(order_wizard)
    # Ищем блок task_settings где template_id записан
    assert "\"template_id\": template_id" in src
    assert "\"template_name\":" in src
    assert "wizard_template" in src


@pytest.mark.unit
def test_order_wizard_confirm_template_defines_template_id_before_use():
    """Регрессия-гард: template_id должен быть определён ДО task_settings,
    иначе NameError на runtime. Раньше handler ссылался на template_id, не
    получая его ни параметром, ни из state — заказы из шаблона падали с
    'name template_id is not defined'."""
    import inspect
    from bot.handlers import order_wizard

    src = inspect.getsource(order_wizard.wizard_confirm_with_template)
    assignment_pos = src.find("template_id = state_data.get")
    usage_pos = src.find('"template_id": template_id')
    assert assignment_pos != -1, (
        "template_id должен присваиваться внутри wizard_confirm_with_template"
    )
    assert usage_pos != -1, (
        "task_settings обязан содержать \"template_id\": template_id"
    )
    assert assignment_pos < usage_pos, (
        "template_id используется ДО объявления — NameError на runtime"
    )


@pytest.mark.unit
def test_quick_order_confirm_template_attaches_template_id():
    from bot.handlers import quick_order
    src = inspect.getsource(quick_order)
    assert "'template_id': template_id" in src
    assert "'template_name'" in src


@pytest.mark.unit
def test_video_parsing_new_videos_attaches_template_id():
    from bot.handlers import video_parsing_new_videos
    src = inspect.getsource(video_parsing_new_videos)
    assert "\"template_id\": template_id" in src
    assert "\"template_name\":" in src
    assert "task_settings[\"creator_username\"]" in src
    assert "task_settings[\"creator_source_id\"] = creator_id" in src
