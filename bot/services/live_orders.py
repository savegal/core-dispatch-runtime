"""Bounded live message subscriptions, cancelled before any navigation edits."""
import asyncio
from contextlib import suppress
from html import escape
import logging
import time

from aiogram import BaseMiddleware
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter
from bot.services.order_presentation import count, number, excerpt, utc_time, progress_bar, percent, publication_links, NO_LINK_PREVIEWS

logger = logging.getLogger(__name__)
PHASES = {"preparing": "Подготовка", "main": "Главные комментарии", "reply": "Ответы",
          "checking": "Проверка видимости", "reading": "Поиск комментариев"}
STATUSES = {"accepted": "принято с CID", "visible": "видно наблюдателю", "unknown": "исход отправки неизвестен",
            "sending": "запрос отправляется", "failed": "ошибка отправки", "not_sent": "не отправлено",
            "preparing": "подготовка"}
TASK_STATUSES = {"pending": "В очереди", "in_progress": "В работе", "running": "В работе",
                 "done": "Завершён", "success": "Завершён", "completed": "Завершён",
                 "partial": "Частично выполнен", "error": "Ошибка", "failed": "Ошибка", "stopped": "Остановлен"}
TASK_STATUSES["queued"] = "В очереди"
ACTIVE_STATUSES = {"pending", "in_progress", "running", "queued"}


def _publication_error_label(row):
    from services.user_facing_errors import humanize_error_text

    reason = str(row.get("error_code") or "")
    if not reason:
        return ""
    if reason.endswith("empty_body") and row.get("wire_count"):
        return "Пустой ответ на POST, исход неизвестен"
    if reason.startswith("dead_session:"):
        return "Сессия истекла или недействительна"
    if reason.startswith(("proxy_route:", "residential_ip_probe_")):
        return "Ошибка маршрута прокси"
    return {
        "http_preflight:inconclusive:passport_auth_unconfirmed": "Passport не подтвердил авторизацию",
        "http_auth_unconfirmed:browser_login_state": "Браузер не подтвердил авторизацию",
        "comment_readiness_unconfirmed:timeout": "Таймаут чтения комментариев",
        "comment_readiness_unconfirmed:empty_body": "Пустой ответ списка комментариев",
    }.get(reason) or humanize_error_text(reason, short=True) or ""


def render_live(activity):
    status = str(activity.get("status") or "pending")
    active = status in ACTIVE_STATUSES
    videos, success = count(activity.get("videos_total")), count(activity.get("videos_success"))
    goal_complete = bool(activity.get("plan") and activity.get("videos_total", 0) > 0
                         and activity.get("videos_success", 0) >= activity["videos_total"])
    icon = "✅" if goal_complete else "◉" if active else "⏹" if status == "stopped" else "◌"
    lines = [f"<b>{icon} Заказ #{count(activity.get('task_id'))}</b>",
             "Цель подтверждена" if goal_complete else excerpt(TASK_STATUSES.get(status, status), 80), ""]
    if active:
        interval = activity.get("live_interval")
        interval = interval if isinstance(interval, (int, float)) else 3
        lines.extend([f"<b>Сейчас</b> · {PHASES.get(activity.get('phase'), 'Подготовка')}",
                      f"Интервал обновления · {interval:g} с", ""])
    if videos:
        lines.extend([f"<b>Цель на видео</b> · {number(success)} / {number(videos)}",
                      f"<code>{progress_bar(success, videos)}</code>",
                      f"Обработано видео: {number(activity.get('progress'))}/{number(videos)}", ""])
    if not active and not goal_complete and activity.get("error_reason"):
        from services.user_facing_errors import humanize_error_text
        lines.extend([f"<b>Причина</b> · {excerpt(humanize_error_text(activity['error_reason'], short=True), 220, encoded_limit=350)}", ""])
    plan = activity.get("plan") or {}
    if plan:
        lines.extend([f"На видео: <b>{number(plan.get('main_count'))} главных × {number(plan.get('replies_per_main'))} ответов</b>",
                      "Ответы — под каждым публично подтверждённым главным.", ""])
        execution = activity.get("execution") or {}
        if execution.get("browser_slots") == 1:
            lines.extend(["Один браузерный слот · HTTP-отправка и проверка чередуются", ""])
        elif execution:
            idle_senders = execution.get("idle_reader_senders")
            extra = (f" · без проверок до {number(idle_senders)} отправок"
                     if idle_senders and idle_senders != execution.get("effective_senders") else "")
            lines.extend([f"Лимит параллельности · HTTP-отправки {number(execution.get('effective_senders'))}, "
                          f"проверки {number(execution.get('effective_readers'))}{extra}", ""])
    for key, title in (("main", "Главные комментарии"), ("replies", "Ответы")):
        value = activity.get(key) or {}
        visible = value.get("visible", 0)
        target = count(plan.get("main_count")) * videos * (count(plan.get("replies_per_main")) if key == "replies" else 1)
        lines.extend([f"<b>{title}</b>",
            f"Публично подтверждено: <b>{number(visible)}" + (f" / {number(target)}" if plan else "") + "</b>",
            f"HTTP-отправок: {number(value.get('dispatched'))} · принято с CID: {number(value.get('accepted'))}",
            f"Готовятся: {number(value.get('preparing'))} · отправляются: {number(value.get('sending'))}",
            f"Только автору: <b>{number(value.get('author_only'))}</b> · не найдены наблюдателями: {number(value.get('not_found'))}",
            f"Ждут проверки: {number(value.get('pending_checks'))} · исход неизвестен: {number(value.get('uncertain'))}",
            f"Ошибок: {number(value.get('failed'))} · отсев до POST: "
            f"{number(value.get('preflight_rejected', value.get('not_sent')))}",
            f"Видно / отправок: {percent(value.get('visible_percent'))}"])
        if value.get("ever_visible", visible) != visible:
            lines.append(f"Когда-либо подтверждено: {number(value.get('ever_visible'))}")
        lines.append("")
    traffic = activity.get("traffic") or {}
    total_bytes = int(traffic.get("received_bytes") or 0) + int(traffic.get("sent_bytes") or 0)
    if total_bytes:
        lines.append(f"Трафик: ≈ {total_bytes / 1048576:.2f} МБ" +
                     (f" / {number(plan.get('traffic_limit_mb'))} МБ" if plan.get("traffic_limit_mb") else ""))
    job = activity.get("recheck") or {}
    if job:
        label = {"pending": "в очереди", "running": "в процессе", "done": "раунд завершён", "error": "ошибка"}
        lines.append(f"Перепроверка: {escape(label.get(job.get('status'), '—'))}")
        stats = job.get("result") or {}
        if stats.get("processed"):
            lines.append(f"Обработано: {number(stats['processed'])} · с результатом: {number(stats.get('checked'))} "
                         f"· публично: {number(stats.get('visible'))} · только автору: {number(stats.get('author_only'))}")
    latest = activity.get("latest") or []
    if latest:
        lines.extend(["", "<b>Последние действия</b>"])
    for row in latest[:4]:
        state = {"visible": "видно наблюдателю", "author_only": "видно только автору",
                 "not_found": "не найден наблюдателями", "inconclusive": "проверка не завершена"}.get(
                     row.get("visibility"), STATUSES.get(row.get("status"), "ожидается"))
        label = "ответ" if row.get("kind") == "reply" else "комментарий"
        timestamp = next((utc_time(row.get(key)) for key in ("checked_at", "finished_at", "dispatched_at", "started_at") if utc_time(row.get(key))), None)
        clock = (timestamp.split(" · ")[-1] + " · ") if timestamp else ""
        account = f" · акк. #{number(row['account_id'])}" if row.get("account_id") else ""
        reason = _publication_error_label(row)
        if row.get("wire_count") == 0 and row.get("status") == "not_sent":
            state = "пропуск до POST"
        if row.get("wire_count") and str(row.get("error_code") or "").endswith("empty_body"):
            state = "POST отправлен"
        detail = f"\n{excerpt(reason, 80, encoded_limit=160)}" if reason else ""
        lines.append(f"<code>{clock}</code>{label}{account} · {escape(state)}{detail}\n"
                     f"{excerpt(row.get('text'), 80, encoded_limit=200)}")
        links = publication_links(row)
        if links:
            lines.append(links)
    if activity.get("pending_rechecks") and not active:
        lines.extend(["", "Поздние проверки продолжаются. Новые POST не отправляются."])
    generated, updated = utc_time(activity.get("generated_at")), utc_time(activity.get("updated_at"))
    if generated:
        lines.extend(["", f"<i>Данные: {generated}</i>"])
    if updated:
        lines.append(f"<i>Последнее событие: {updated}</i>")
    lines.append("<i>«Только автору» — автор нашёл CID. Две полные независимые проверки его не нашли.</i>")
    return "\n".join(lines)


def render_text_statistics(activity, page=0):
    texts = activity.get("texts") or []
    pages = max(1, (len(texts) + 3) // 4)
    page = max(0, min(int(page), pages - 1))
    lines = [f"<b>◈ Заказ #{count(activity.get('task_id'))} · тексты</b>",
             "Публичный результат / реальные HTTP-отправки.",
             "Принятый CID учитывается отдельно от публичной видимости.", ""]
    for index, row in enumerate(texts[page * 4:page * 4 + 4], page * 4 + 1):
        lines.extend([f"<b>#{index} · {'Ответ' if row.get('kind') == 'reply' else 'Главный комментарий'}</b>",
            f"<blockquote>{excerpt(row.get('text'), 180, encoded_limit=320)}</blockquote>",
            f"Видно: {number(row.get('visible'))} · отправок: {number(row.get('dispatched'))}",
            f"Конверсия: <b>{percent(row.get('visible_percent'))}</b>",
            f"CID: {number(row.get('accepted'))} · ждут проверки: {number(row.get('pending_checks'))}",
            f"Только автору: {number(row.get('author_only'))} · не найдены: {number(row.get('not_found'))}",
            f"Исход неизвестен: {number(row.get('uncertain'))} · ошибок: {number(row.get('failed'))}", ""])
    if not texts:
        lines.append("Отправок ещё нет. Статистика появится в ходе работы.")
    lines.append(f"Страница {page + 1}/{pages}. Полный отчёт доступен в TXT и CSV.")
    generated = utc_time(activity.get("generated_at"))
    if generated:
        lines.append(f"<i>Данные: {generated}</i>")
    return "\n".join(lines)


class LiveOrderViews:
    def __init__(self, interval=3.0, max_views=128, lifetime=3600):
        self.interval, self.max_views, self.lifetime = interval, max_views, lifetime
        self.views = {}

    async def stop(self, chat_id, message_id):
        task = self.views.pop((chat_id, message_id), None)
        if task:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task

    async def watch(self, message, loader, renderer, initial_text):
        key = (message.chat.id, message.message_id)
        await self.stop(*key)
        if len(self.views) >= self.max_views:
            oldest = next(iter(self.views))
            await self.stop(*oldest)

        async def refresh():
            previous, failures, started = initial_text, 0, time.monotonic()
            previous_keyboard = getattr(message, "reply_markup", None)
            interval = self.interval

            async def pause_notice(reason):
                with suppress(Exception):
                    await message.edit_text(previous +
                        f"\n\n<i>Автообновление приостановлено: {reason} Нажмите «Обновить».</i>",
                        reply_markup=previous_keyboard, parse_mode="HTML", link_preview_options=NO_LINK_PREVIEWS)

            try:
                while time.monotonic() - started < self.lifetime:
                    await asyncio.sleep(interval)
                    try:
                        activity = await loader()
                        if isinstance(activity.get("live_interval"), (int, float)):
                            interval = max(2, min(30, activity["live_interval"]))
                        text, keyboard = renderer(activity)
                        if text != previous or keyboard != previous_keyboard:
                            await message.edit_text(text, reply_markup=keyboard, parse_mode="HTML", link_preview_options=NO_LINK_PREVIEWS)
                            previous, previous_keyboard = text, keyboard
                        failures = 0
                        if activity.get("status") not in {"pending", "in_progress", "running", "queued"} and (
                                (activity.get("recheck") or {}).get("status") not in {"pending", "running"}) and not activity.get("pending_rechecks"):
                            return
                    except TelegramRetryAfter as exc:
                        await asyncio.sleep(min(60, max(1, exc.retry_after)))
                    except TelegramForbiddenError:
                        return
                    except TelegramBadRequest as exc:
                        if "message is not modified" not in str(exc).lower():
                            return
                    except Exception:
                        failures += 1
                        if failures >= 5:
                            logger.warning("Live order view stopped after repeated load failures")
                            await pause_notice("сервер временно недоступен.")
                            return
                await pause_notice("истёк час просмотра.")
            finally:
                if self.views.get(key) is asyncio.current_task():
                    self.views.pop(key, None)
        self.views[key] = asyncio.create_task(refresh())

    async def close(self):
        for key in list(self.views):
            await self.stop(*key)


live_order_views = LiveOrderViews()


class StopLiveViewOnNavigation(BaseMiddleware):
    async def __call__(self, handler, event, data):
        message = getattr(event, "message", None)
        if message:
            await live_order_views.stop(message.chat.id, message.message_id)
        return await handler(event, data)
