"""Telegram order screens report actual evidence and remain valid bounded HTML."""
import asyncio
from html.parser import HTMLParser
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from bot.services.live_orders import LiveOrderViews, render_live, render_text_statistics
from bot.services.order_presentation import excerpt, progress_bar, utc_time


def activity(**changes):
    result = dict(task_id=12, status="in_progress", phase="checking", videos_total=1,
        videos_success=0, progress=0, generated_at="2026-11-06T18:00:01+00:00",
        updated_at="2026-11-06T17:59:58+00:00", plan=dict(main_count=20, replies_per_main=2),
        main=dict(dispatched=10, accepted=3, visible=1, ever_visible=1, pending_checks=2,
                  uncertain=1, failed=6, visible_percent=10),
        replies=dict(dispatched=0, accepted=0, visible=0, visible_percent=None))
    result.update(changes)
    return result


class ValidTelegramHTML(HTMLParser):
    def __init__(self):
        super().__init__()
        self.stack = []

    def handle_starttag(self, tag, attrs):
        assert tag in {"b", "i", "code", "blockquote", "a"}
        if tag == "a":
            assert dict(attrs)["href"].startswith("https://www.tiktok.com/share/video/")
        self.stack.append(tag)

    def handle_endtag(self, tag):
        assert self.stack.pop() == tag


def validate_card(text):
    parser = ValidTelegramHTML()
    parser.feed(text)
    assert not parser.stack
    # Raw escaped HTML bound is stricter than Telegram's 4096-character text bound.
    assert len(text.encode("utf-16-le")) // 2 < 4096


def test_processed_or_accepted_does_not_turn_into_a_completed_goal():
    text = render_live(activity(status="done", progress=1, main=dict(accepted=20, dispatched=20, visible=0)))
    assert "Цель подтверждена" not in text
    assert "▰" not in text and "▱" * 10 in text
    assert "Публично подтверждено: <b>0 / 20</b>" in text
    assert "принято с CID: 20" in text
    assert "Видно / отправок: —" in text
    validate_card(text)


def test_only_video_goal_evidence_completes_the_video_bar():
    text = render_live(activity(videos_success=1))
    assert "Цель подтверждена" in text and "▰" * 10 in text
    assert progress_bar(99, 100) == "▰" * 9 + "▱"


def test_refresh_and_event_timestamps_come_from_the_snapshot():
    snapshot = activity(live_interval=7, execution=dict(browser_slots=4, effective_senders=3, effective_readers=1))
    text = render_live(snapshot)
    assert render_live(snapshot) == text
    assert "Интервал обновления · 7 с" in text
    assert "Лимит параллельности · HTTP-отправки 3, проверки 1" in text
    assert "Данные: 06.11 · 18:00:01 UTC" in text
    assert "Последнее событие: 06.11 · 17:59:58 UTC" in text
    assert utc_time("2026-11-06T20:00:01+02:00") == "06.11 · 18:00:01 UTC"
    assert utc_time("unavailable") is None
    assert "Данные:" not in render_live(activity(generated_at=None, updated_at=None))


def test_latest_events_are_escaped_and_retain_their_evidence_state():
    text = render_live(activity(latest=[dict(kind="reply", status="unknown", visibility="unchecked",
        text="<b>private & text</b>", dispatched_at="2026-11-06T18:00:00Z")]))
    assert "&lt;b&gt;private &amp; text&lt;/b&gt;" in text
    assert "исход отправки неизвестен" in text and "18:00:00 UTC" in text
    assert "на модерации" not in text.lower()
    validate_card(text)


def test_late_verification_and_stopped_states_are_explicit():
    text = render_live(activity(status="stopped", pending_rechecks=2))
    assert "Остановлен" in text and "Поздние проверки продолжаются" in text
    assert "Интервал обновления" not in text
    assert "Цель подтверждена" not in text


def test_worst_case_text_libraries_and_events_fit_telegram_html():
    from bot.handlers.quantity_orders import preview_text
    attack = '<>&"' * 1000
    text = preview_text(dict(publication_plan=dict(main_count=500, replies_per_main=20),
        texts=[attack] * 50, replies=[attack] * 20, links=["url"] * 100,
        template_name=attack, cost_label=attack, balance_label=attack))
    validate_card(text)
    assert "Ещё вариантов: 47" in text and "Ещё вариантов: 17" in text
    assert "оценка" in text and "фактическая конверсия" in text
    assert "На весь заказ: 50 000 главных · 1 000 000 ответов" in text
    latest = [dict(kind="reply", status="accepted", text=attack, aweme_id="7704037684280446216",
                   comment_id="7704740811904828192", parent_id="7704740811904828100")] * 12
    validate_card(render_live(activity(latest=latest, error_reason=attack, status="error")))
    texts = [dict(kind="main", text=attack, dispatched=20, visible=2, visible_percent=10)] * 50
    validate_card(render_text_statistics(activity(texts=texts), page=12))
    clipped = excerpt("&" * 200, encoded_limit=13)
    assert clipped == "&amp;&amp;…"


def test_live_keyboard_has_explicit_refresh_and_state_appropriate_controls():
    from bot.handlers.orders import _build_order_detail_keyboard
    task = dict(id=12, status="in_progress", settings=dict(publication_plan=dict(main_count=20,
        replies_per_main=2)), video_urls=["https://www.tiktok.com/@fixture/video/123"])
    keyboard = _build_order_detail_keyboard(task, "live", 0, "all")
    buttons = [button for row in keyboard.inline_keyboard for button in row]
    assert any(button.text == "↻ Обновить" and button.callback_data == "order:detail:12:live:0:all" for button in buttons)
    assert any("Остановить" in button.text for button in buttons)
    assert not any("Перепроверить" in button.text for button in buttons)
    task["status"] = "done"
    buttons = [button for row in _build_order_detail_keyboard(task, "live", 0, "all").inline_keyboard for button in row]
    assert any("Перепроверить" in button.text for button in buttons)
    assert not any("Остановить" in button.text for button in buttons)
    assert all(len(button.callback_data.encode()) <= 64 for button in buttons)


@pytest.mark.asyncio
async def test_keyboard_only_changes_are_pushed_to_the_live_message():
    previous = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Before", callback_data="old")]])
    changed = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="After", callback_data="new")]])
    message = SimpleNamespace(chat=SimpleNamespace(id=10), message_id=5,
                              reply_markup=previous, edit_text=AsyncMock())
    views = LiveOrderViews(interval=.001)
    try:
        await views.watch(message, AsyncMock(return_value={"status": "in_progress"}), lambda _: ("same", changed), "same")
        for _ in range(100):
            if message.edit_text.await_count:
                break
            await asyncio.sleep(.002)
        assert message.edit_text.await_count == 1
        call = message.edit_text.await_args
        assert call.args == ("same",) and call.kwargs["reply_markup"] == changed
        assert call.kwargs["parse_mode"] == "HTML" and call.kwargs["link_preview_options"].is_disabled
    finally:
        await views.close()


@pytest.mark.asyncio
async def test_backend_failure_does_not_leave_a_false_auto_update_promise():
    message = SimpleNamespace(chat=SimpleNamespace(id=10), message_id=6, edit_text=AsyncMock())
    views = LiveOrderViews(interval=.001)
    try:
        await views.watch(message, AsyncMock(side_effect=RuntimeError("backend unavailable")), lambda _: ("new", None), "Last known result")
        for _ in range(100):
            if not views.views:
                break
            await asyncio.sleep(.002)
        assert not views.views
        assert "Автообновление приостановлено" in message.edit_text.await_args.args[0]
        assert "Last known result" in message.edit_text.await_args.args[0]
    finally:
        await views.close()
