"""
Сервис для отправки уведомлений через Telegram бота о новых видео.
"""
import asyncio
import logging
import re
from typing import List
from config.settings import get_settings
from bot.config import load_config
from aiogram import Bot
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramNetworkError
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from bot.services.formatting import markdown_handle, markdown_to_plain_text
from bot.services.video_display import format_video_markdown_line
from db.models import CreatorSource, CreatorVideo, User
from db.database import get_sync_db
from services.monitor_incident_service import (
    INCIDENT_TELEGRAM_DELIVERY,
    record_incident,
    resolve_incident,
)

logger = logging.getLogger("services.notify_service")
settings = get_settings()
config = load_config()


# Telegram ограничение на длину сообщения
MAX_TELEGRAM_MESSAGE_LENGTH = 4096
_PERMANENTLY_BLOCKED_TELEGRAM_IDS: set[int] = set()


def _build_telegram_session() -> AiohttpSession:
    timeout_seconds = getattr(settings, "TELEGRAM_API_TIMEOUT_SECONDS", 10)
    proxy = getattr(settings, "TELEGRAM_PROXY", None)
    return AiohttpSession(proxy=proxy, timeout=timeout_seconds)


def _with_ap_journal_button(keyboard: InlineKeyboardMarkup | None) -> InlineKeyboardMarkup:
    rows = []
    if keyboard is not None:
        rows = [list(row) for row in keyboard.inline_keyboard]
        if any(button.callback_data == "ap:rep" for row in rows for button in row):
            return keyboard
    rows.append([InlineKeyboardButton(text="📋 Журнал", callback_data="ap:rep")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def is_telegram_delivery_permanently_blocked(telegram_id: int | None) -> bool:
    if telegram_id is None:
        return False
    try:
        return int(telegram_id) in _PERMANENTLY_BLOCKED_TELEGRAM_IDS
    except (TypeError, ValueError):
        return False


def _disable_video_notifications_for_telegram_id(telegram_id: int) -> None:
    _PERMANENTLY_BLOCKED_TELEGRAM_IDS.add(int(telegram_id))
    db = next(get_sync_db())
    try:
        users = db.query(User).filter(User.telegram_id == telegram_id).all()
        changed = 0
        for user in users:
            if getattr(user, "video_notifications_enabled", True):
                user.video_notifications_enabled = False
                changed += 1
        if changed:
            db.commit()
            logger.warning(
                "Disabled video notifications for telegram_id=%s after Telegram Forbidden response",
                telegram_id,
            )
        else:
            db.rollback()
    except Exception as exc:
        db.rollback()
        logger.warning(
            "Failed to disable video notifications for telegram_id=%s after Telegram Forbidden: %s",
            telegram_id,
            exc,
        )
    finally:
        db.close()


async def send_notification_to_user(telegram_id: int, message: str, keyboard: InlineKeyboardMarkup | None = None) -> bool:
    """
    ИСПРАВЛЕНО: Отправляет уведомление конкретному пользователю через Telegram бота.
    
    Args:
        telegram_id: Telegram ID пользователя
        message: текст сообщения
        keyboard: опциональная клавиатура с кнопками
        
    Returns:
        True если отправлено успешно
    """
    bot_token = config.get("BOT_TOKEN")
    
    if not bot_token:
        logger.warning("BOT_TOKEN not set, cannot send notification")
        return False
    
    # ИСПРАВЛЕНО: Проверяем длину сообщения (Telegram ограничение 4096 символов)
    if len(message) > MAX_TELEGRAM_MESSAGE_LENGTH:
        logger.warning(f"Message too long ({len(message)} chars), truncating to {MAX_TELEGRAM_MESSAGE_LENGTH}")
        message = message[:MAX_TELEGRAM_MESSAGE_LENGTH - 50] + "\n\n... (сообщение обрезано)"
    
    # Создаём Bot с сессией каждый раз (уведомления редкие, переиспользование не критично)
    try:
        session = _build_telegram_session()
        bot = Bot(token=bot_token, session=session)
        
        try:
            try:
                await bot.send_message(
                    telegram_id,
                    message,
                    reply_markup=keyboard,
                    parse_mode="Markdown",
                    disable_web_page_preview=True,
                )
            except TelegramBadRequest:
                await bot.send_message(
                    telegram_id,
                    markdown_to_plain_text(message),
                    reply_markup=keyboard,
                    disable_web_page_preview=True,
                )
            logger.info(f"Notification sent to user {telegram_id}: {message[:50]}...")
            return True
        finally:
            # Закрываем сессию после использования
            await bot.session.close()
    except TelegramForbiddenError as e:
        logger.warning(
            "Telegram delivery forbidden for user %s; bot is likely blocked. Disabling video notifications: %s",
            telegram_id,
            e,
        )
        _disable_video_notifications_for_telegram_id(telegram_id)
        return False
    except TelegramNetworkError as e:
        logger.warning(f"Telegram network error sending notification to user {telegram_id}: {e}")
        return False
    except Exception as e:
        logger.error(f"Error sending notification to user {telegram_id}: {e}", exc_info=True)
        return False


async def send_notification_to_owner(
    message: str,
    keyboard: InlineKeyboardMarkup | None = None,
) -> bool:
    """
    Отправляет уведомление владельцу (OWNER_TELEGRAM_ID) через Telegram бота.
    """
    owner_id = settings.OWNER_TELEGRAM_ID
    bot_token = config.get("BOT_TOKEN")
    if not owner_id or not bot_token:
        logger.warning("OWNER_TELEGRAM_ID or BOT_TOKEN not set, cannot send owner notification")
        return False
    try:
        keyboard = _with_ap_journal_button(keyboard)
        session = _build_telegram_session()
        bot = Bot(token=bot_token, session=session)
        try:
            try:
                await bot.send_message(
                    owner_id,
                    message,
                    reply_markup=keyboard,
                    parse_mode="Markdown",
                    disable_web_page_preview=True,
                )
            except TelegramBadRequest:
                await bot.send_message(
                    owner_id,
                    markdown_to_plain_text(message),
                    reply_markup=keyboard,
                    disable_web_page_preview=True,
                )
            logger.info(f"Notification sent to owner: {message[:50]}...")
            resolve_incident(
                INCIDENT_TELEGRAM_DELIVERY,
                title="Telegram снова принимает уведомления владельцу",
                evidence="owner notification delivered",
            )
            return True
        finally:
            await bot.session.close()
    except TelegramNetworkError as e:
        logger.warning(f"Telegram network error sending notification to owner: {e}")
        record_incident(
            INCIDENT_TELEGRAM_DELIVERY,
            severity="warning",
            title="Telegram не принимает уведомления владельцу",
            cause="Telegram API временно недоступен или сеть ноутбука блокирует api.telegram.org.",
            evidence=str(e)[:300],
            auto_action="Софт продолжает работу и сохраняет инцидент локально/в Redis.",
            admin_action="Проверь VPN/интернет/TELEGRAM_PROXY. Когда Telegram ответит, инцидент очистится автоматически.",
        )
        return False
    except Exception as e:
        logger.error(f"Error sending notification to owner: {e}", exc_info=True)
        return False


def escape_markdown(text: str) -> str:
    """
    Экранирует спецсимволы Markdown, чтобы пользовательские значения не ломали разметку.
    """
    if not text:
        return ""
    # Markdown (legacy parse_mode="Markdown") в наших уведомлениях используется
    # только для простого bold/link-синтаксиса. Экранируем пользовательский текст
    # аккуратно, не затрагивая собранную нами разметку.
    return re.sub(r"([\\_*`\[\]()])", r"\\\1", text)


def normalize_video_url(url: str) -> str:
    """
    ИСПРАВЛЕНО: Нормализует URL видео TikTok, гарантируя полный формат.
    
    Args:
        url: URL видео (может быть относительным или полным)
        
    Returns:
        str: Полный URL в формате https://www.tiktok.com/@user/video/ID
    """
    if not url:
        return ""
    
    url = url.strip()
    
    # Если уже полный URL - возвращаем как есть
    if url.startswith("http://") or url.startswith("https://"):
        return url
    
    # Если относительный URL (начинается с /) - добавляем базовый домен
    if url.startswith("/@"):
        return f"https://www.tiktok.com{url}"
    
    # Если просто путь без слеша - добавляем базовый путь
    if "/video/" in url:
        return f"https://www.tiktok.com/{url}" if not url.startswith("/") else f"https://www.tiktok.com{url}"
    
    return url


async def notify_new_videos(creator: CreatorSource, videos: List[CreatorVideo]) -> bool:
    """
    ИСПРАВЛЕНО: Уведомляет владельца блогера о новых найденных видео (режим notify_only).
    Показывает ВСЕ ссылки на видео, разбивая на несколько сообщений если нужно.
    
    Args:
        creator: блогер, у которого найдены видео
        videos: список найденных видео
    """
    if not videos:
        return True
    
    # ИСПРАВЛЕНО: Проверяем, что у блогера есть владелец
    if not creator.user_id:
        logger.warning(f"[NOTIFY] Creator {creator.username} has no user_id, cannot send notification")
        return False
    
    # ИСПРАВЛЕНО: Получаем владельца блогера и его telegram_id
    db = next(get_sync_db())
    try:
        owner_user = db.query(User).filter(User.id == creator.user_id).first()
        if not owner_user:
            logger.warning(f"[NOTIFY] User {creator.user_id} not found for creator {creator.username}")
            return False
        
        telegram_id = owner_user.telegram_id
    finally:
        db.close()
    
    # ИСПРАВЛЕНО: Нормализуем все URL видео для гарантии полного формата
    normalized_videos = []
    for video in videos:
        normalized_url = normalize_video_url(video.url)
        normalized_videos.append((video, normalized_url))

    safe_username = markdown_handle(creator.username)
    
    # ИСПРАВЛЕНО: Разбиваем на части для отправки (чтобы не превысить лимит сообщения)
    # Максимум ~15 видео на сообщение (чтобы не превысить лимит 4096 символов)
    VIDEOS_PER_MESSAGE = 15
    
    all_sent = True
    for part_idx in range(0, len(normalized_videos), VIDEOS_PER_MESSAGE):
        part_videos = normalized_videos[part_idx:part_idx + VIDEOS_PER_MESSAGE]
        is_first = (part_idx == 0)
        is_last = (part_idx + VIDEOS_PER_MESSAGE >= len(normalized_videos))
        
        if is_first:
            message = f"🎬 *Новые видео*\nБлогер: {safe_username}\n\n"
            message += f"Найдено видео: {len(videos)}\n\n"
        else:
            message = f"🎬 *Продолжение списка видео*\nБлогер: {safe_username}\n\n"
            message += f"Видео {part_idx + 1}-{min(part_idx + VIDEOS_PER_MESSAGE, len(videos))} из {len(videos)}:\n\n"
        
        # ИСПРАВЛЕНО: Показываем ВСЕ ссылки на видео в этой части
        for i, (video, _normalized_url) in enumerate(part_videos, 1):
            global_idx = part_idx + i
            message += format_video_markdown_line(video, global_idx) + "\n"
        
        if not is_last:
            message += f"\n_(продолжение следует...)_"
        
        # Создаём клавиатуру с URL-кнопками для быстрого просмотра
        # Telegram ограничение: максимум 3 URL кнопки в ряд, максимум 8 кнопок всего
        keyboard_buttons = []
        
        # Добавляем URL-кнопки для первых 6 видео этой части (2 ряда по 3)
        for video, normalized_url in part_videos[:6]:
            video_id_short = video.video_id[:10] if video.video_id else f"#{video.id}"
            button_text = f"🔗 #{video_id_short[:8]}"  # Обрезаем для краткости
            keyboard_buttons.append([
                InlineKeyboardButton(text=button_text, url=normalized_url)
            ])
        
        # Кнопка перехода к списку блогеров (только в последнем сообщении)
        if is_last:
            keyboard_buttons.append([
                InlineKeyboardButton(text="📋 Мои блогеры", callback_data="vp:list")
            ])
        
        keyboard = InlineKeyboardMarkup(inline_keyboard=keyboard_buttons) if keyboard_buttons else None
        sent = await send_notification_to_user(telegram_id, message, keyboard)
        all_sent = all_sent and sent
        
        # Небольшая задержка между сообщениями (чтобы не заблокировали за спам)
        if not is_last:
            await asyncio.sleep(0.5)
    return all_sent


async def notify_pending_review(creator: CreatorSource, videos: List[CreatorVideo]) -> bool:
    """
    ПЕРЕДЕЛАНО: Уведомляет владельца блогера о новых видео (режим manual_review).
    Показывает новые видео, ссылки, стоимость комментирования и кнопки для выбора шаблона.
    
    Args:
        creator: блогер, у которого найдены видео
        videos: список видео на проверку
    """
    if not videos:
        return True
    
    # Проверяем, что у блогера есть владелец
    if not creator.user_id:
        logger.warning(f"[NOTIFY] Creator {creator.username} has no user_id, cannot send notification")
        return False
    
    # Получаем владельца блогера и его telegram_id
    db = next(get_sync_db())
    try:
        owner_user = db.query(User).filter(User.id == creator.user_id).first()
        if not owner_user:
            logger.warning(f"[NOTIFY] User {creator.user_id} not found for creator {creator.username}")
            return False
        
        telegram_id = owner_user.telegram_id
        
        # Получаем цены для расчета стоимости
        from services.balance_service import get_pricing
        pricing = get_pricing(db)
        price_per_video = pricing.price_success
        
        # P1 FIX: Получаем default_template_id и имя шаблона (до закрытия БД)
        default_template_id = creator.default_template_id
        template_name = None
        if default_template_id:
            from db.models import TaskTemplate
            template = db.query(TaskTemplate).filter(TaskTemplate.id == default_template_id).first()
            template_name = template.name if template else None
        
        # Нормализуем все URL видео для гарантии полного формата (важно - используем реальные ссылки)
        normalized_videos = []
        video_ids_list = []
        for video in videos:
            normalized_url = normalize_video_url(video.url)
            normalized_videos.append((video, normalized_url))
            video_ids_list.append(str(video.id))
        
        # P0 FIX: Создаем batch вместо передачи video_ids в callback_data
        # Это решает проблему превышения лимита Telegram (64 байта)
        from db.models import VideoBatch
        from datetime import datetime, timezone, timedelta
        
        batch = VideoBatch(
            creator_id=creator.id,
            video_ids=video_ids_list,
            expires_at=datetime.now(timezone.utc) + timedelta(days=7)
        )
        db.add(batch)
        db.flush()  # Получаем ID
        batch_id = batch.id
        db.commit()  # Коммитим batch
        
    finally:
        db.close()
    
    # Рассчитываем стоимость
    total_cost = len(videos) * float(price_per_video)
    safe_username = markdown_handle(creator.username)
    safe_template_name = escape_markdown(template_name) if template_name else None
    
    # Формируем сообщение
    message = f"🎬 *Новые видео*\nБлогер: {safe_username}\n\n"
    message += f"Найдено видео: {len(videos)}\n\n"
    
    # Показываем ссылки на видео (максимум 10 ссылок в сообщении, чтобы не превысить лимит)
    for i, (video, _normalized_url) in enumerate(normalized_videos[:10], 1):
        message += format_video_markdown_line(video, i) + "\n"
    
    if len(videos) > 10:
        message += f"\n... и ещё {len(videos) - 10} видео\n"
    
    message += f"\n💰 *Стоимость:* {total_cost:.2f} грн ({len(videos)} × {float(price_per_video):.2f} грн)\n"
    
    # P1 FIX: Используем default_template_id и template_name полученные выше (до закрытия БД)
    if default_template_id and safe_template_name:
        
        message += f"\n✅ Шаблон по умолчанию: *{safe_template_name}*\n"
        message += f"\nВыберите действие:"
        
        # P1 FIX: Добавляем кнопку "Комментировать все" если есть default template
        keyboard_buttons = [
            [InlineKeyboardButton(
                text=f"✅ Комментировать все (шаблон \"{template_name[:20]}\")",
                callback_data=f"vp:new_videos:template_selected:{creator.id}:{batch_id}:{default_template_id}"
            )],
            [InlineKeyboardButton(text="📑 Выбрать шаблон", callback_data=f"vp:new_videos:select_template:{creator.id}:{batch_id}")],
            [InlineKeyboardButton(text="➕ Создать шаблон", callback_data=f"vp:new_videos:create_template:{creator.id}:{batch_id}")]
        ]
    else:
        message += f"\nВыберите шаблон для комментирования:"
        # P0 FIX: Используем batch_id вместо списка video_ids
        keyboard_buttons = [
            [InlineKeyboardButton(text="📑 Выбрать шаблон", callback_data=f"vp:new_videos:select_template:{creator.id}:{batch_id}")],
            [InlineKeyboardButton(text="➕ Создать шаблон", callback_data=f"vp:new_videos:create_template:{creator.id}:{batch_id}")]
        ]
    
    keyboard = _with_ap_journal_button(InlineKeyboardMarkup(inline_keyboard=keyboard_buttons))
    return await send_notification_to_user(telegram_id, message, keyboard)


async def notify_auto_orders_created(creator: CreatorSource, videos: List[CreatorVideo], total_cost: float, task_ids: List[int]) -> bool:
    """
    Уведомляет владельца блогера о созданных автоматических заказах (режим auto_comment).
    
    Args:
        creator: блогер, у которого созданы заказы
        videos: список видео, для которых созданы заказы
        total_cost: общая стоимость созданных заказов
        task_ids: список ID созданных задач
    """
    if not videos:
        return True
    
    # Проверяем, что у блогера есть владелец
    if not creator.user_id:
        logger.warning(f"[NOTIFY] Creator {creator.username} has no user_id, cannot send notification")
        return False
    
    # Получаем владельца блогера и его telegram_id
    db = next(get_sync_db())
    try:
        owner_user = db.query(User).filter(User.id == creator.user_id).first()
        if not owner_user:
            logger.warning(f"[NOTIFY] User {creator.user_id} not found for creator {creator.username}")
            return False
        
        telegram_id = owner_user.telegram_id
    finally:
        db.close()
    
    # Нормализуем URL видео
    normalized_videos = []
    for video in videos:
        normalized_url = normalize_video_url(video.url)
        normalized_videos.append((video, normalized_url))

    safe_username = markdown_handle(creator.username)
    
    # Формируем сообщение
    message = f"✅ *Автоматически созданы заказы*\n\n"
    message += f"Блогер: {safe_username}\n"
    message += f"Видео: {len(videos)}\n"
    message += f"💰 Потрачено: {total_cost:.2f} грн\n\n"
    
    # Показываем ссылки на видео (максимум 10)
    message += "*Видео:*\n"
    for i, (video, _normalized_url) in enumerate(normalized_videos[:10], 1):
        message += format_video_markdown_line(video, i) + "\n"
    
    if len(videos) > 10:
        message += f"\n... и ещё {len(videos) - 10} видео\n"
    
    message += f"\n📋 Создано заказов: {len(task_ids)}"
    
    # Создаём клавиатуру: отключить авто комментирование, меню
    keyboard_buttons = [
        [InlineKeyboardButton(text="⏸ Отключить авто комментирование", callback_data=f"vp:creator:{creator.id}:auto_disable")],
        [InlineKeyboardButton(text="📋 Журнал", callback_data="ap:rep")],
        [InlineKeyboardButton(text="🏠 Главное меню", callback_data="menu:main")]
    ]
    
    keyboard = InlineKeyboardMarkup(inline_keyboard=keyboard_buttons)
    return await send_notification_to_user(telegram_id, message, keyboard)


async def notify_autopilot_paused_by_parsing_mode(creator: CreatorSource, videos: List[CreatorVideo]) -> bool:
    if not creator.user_id:
        logger.warning(f"[NOTIFY] Creator {creator.username} has no user_id, cannot send parsing-mode pause notification")
        return False

    db = next(get_sync_db())
    try:
        owner_user = db.query(User).filter(User.id == creator.user_id).first()
        if not owner_user or not owner_user.telegram_id:
            logger.warning(f"[NOTIFY] User {creator.user_id} not found for creator {creator.username}")
            return False
        telegram_id = owner_user.telegram_id
    finally:
        db.close()

    safe_username = markdown_handle(creator.username)
    preview_lines = [
        format_video_markdown_line(video, idx)
        for idx, video in enumerate(videos[:8], start=1)
    ]
    if len(videos) > 8:
        preview_lines.append(f"... и ещё {len(videos) - 8}")
    preview = "\n".join(preview_lines)
    message = (
        "⚠️ *Автопилот временно на паузе*\n\n"
        f"Блогер: {safe_username}\n"
        f"Новых видео: {len(videos)}\n\n"
        f"{preview}\n\n"
        "Сейчас софт работает в режиме парсинга, поэтому авто-заказы не создавались. "
        "Видео сохранены во входящих, их можно прокомментировать вручную после возврата полного режима."
    )
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📥 Открыть входящие", callback_data="vp:inbox")],
        [InlineKeyboardButton(text="📋 Мои блогеры", callback_data="vp:list")],
    ])
    return await send_notification_to_user(telegram_id, message, keyboard)
