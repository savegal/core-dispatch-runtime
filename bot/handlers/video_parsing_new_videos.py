"""
Обработчики для нового функционала уведомлений о новых видео.
"""
from aiogram import Router, F
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.fsm.context import FSMContext
from aiogram.exceptions import TelegramBadRequest
from bot.services.api_client import api_client
from bot.services.user_utils import get_user_backend_id
from bot.keyboards.video_parsing import get_video_parsing_menu_keyboard
from bot.services.formatting import Emoji, escape_markdown, markdown_inline_code
from bot.services.template_utils import filter_full_templates
from bot.services.video_display import format_video_markdown_list
from config.settings import get_settings
import logging
from bot.services.order_utils import get_task_creation_error_message

logger = logging.getLogger("bot.video_parsing.new_videos")
router = Router()

settings = get_settings()


def _new_videos_preview(videos: list[dict], limit: int = 5) -> str:
    lines = format_video_markdown_list(videos, limit=limit)
    return "\n".join(lines)


async def render_template_picker(
    message,
    state: FSMContext,
    *,
    user_id: int,
    creator_id: int,
    batch_id: int,
    page: int = 0,
    cancel_callback: str | None = None,
    notify=None,
) -> bool:
    """Показывает экран выбора шаблона для пачки видео.

    Это переиспользуемый renderer: его можно звать как из callback-handler
    `new_videos_select_template`, так и из inbox-flow, не плодя FakeCallback
    хаков. ``notify(text, alert=True)`` — необязательный коллбэк для toast'ов
    (если есть исходный CallbackQuery, передавайте ``callback.answer``).
    ``cancel_callback`` — что класть в кнопку «Отменить»; по умолчанию ведёт
    в стандартный новый-видео cancel-handler.

    Возвращает True если экран успешно показан, False если выйти пришлось
    из-за устаревшего batch / отсутствующих видео.
    """
    cancel_cb = cancel_callback or f"vp:new_videos:cancel:{creator_id}:{batch_id}"

    async def _toast(text: str, alert: bool = True) -> None:
        if notify is None:
            return
        try:
            await notify(text, show_alert=alert)
        except TelegramBadRequest:
            pass
        except Exception:
            pass

    batch_data = await api_client.get_video_batch(batch_id, user_id)
    if not batch_data:
        await _toast("❌ Batch не найден или истек")
        return False

    batch_creator_id = int(batch_data.get("creator_id") or 0)
    if batch_creator_id and batch_creator_id != creator_id:
        logger.warning(
            "Template picker creator mismatch: param=%s batch=%s batch_id=%s user_id=%s",
            creator_id,
            batch_creator_id,
            batch_id,
            user_id,
        )
        await _toast("Старое уведомление. Откройте список видео заново.")
        return False

    video_ids = [int(vid) for vid in batch_data.get("video_ids", []) if vid]
    if not video_ids:
        await _toast("❌ Не найдены видео")
        return False

    videos = await api_client.get_videos_by_ids(video_ids, user_id)
    if videos:
        available_videos = [
            video
            for video in videos
            if str(video.get("status") or "").strip().lower() == "new" and not video.get("task_id")
        ]
        if not available_videos:
            await _toast("Эти видео уже поставлены в очередь или обработаны.")
            return False
        videos = available_videos
        video_ids = [int(video.get("id")) for video in videos if video.get("id")]
    if not videos:
        await _toast("❌ Видео не найдены")
        return False

    pricing = await api_client.get_pricing()
    price_per_video = float(pricing.get("price_success", 5.0))
    total_cost = len(videos) * price_per_video

    await state.update_data(
        creator_id=creator_id,
        batch_id=batch_id,
        video_ids=video_ids,
        video_urls=[v.get("url") for v in videos],
        total_cost=total_cost,
        price_per_video=price_per_video,
    )

    templates = filter_full_templates(await api_client.list_task_templates(user_id))

    if not templates:
        await message.edit_text(
            f"❌ *Нет шаблонов*\n\n"
            f"🎬 Видео: *{len(videos)}*\n"
            f"💰 Стоимость: *{total_cost:.2f} грн*\n\n"
            f"{_new_videos_preview(videos)}\n\n"
            f"_Создайте шаблон для комментирования._",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="➕ Создать шаблон", callback_data=f"vp:new_videos:create_template:{creator_id}:{batch_id}")],
                [InlineKeyboardButton(text="❌ Отменить", callback_data=cancel_cb)],
            ]),
            parse_mode="Markdown",
            disable_web_page_preview=True,
        )
        return True

    per_page = 8
    total_pages = max(1, (len(templates) + per_page - 1) // per_page)
    page = max(0, min(page, total_pages - 1))
    start = page * per_page

    keyboard_rows = []
    for template in templates[start:start + per_page]:
        template_id = template.get("id")
        template_name = template.get("name", f"Шаблон #{template_id}")
        if len(template_name) > 30:
            template_name = template_name[:27] + "..."
        keyboard_rows.append([
            InlineKeyboardButton(
                text=template_name,
                callback_data=f"vp:new_videos:template_selected:{creator_id}:{batch_id}:{template_id}",
            )
        ])

    if total_pages > 1:
        nav = []
        if page > 0:
            nav.append(InlineKeyboardButton(text="◀️", callback_data=f"vp:new_videos:select_template:{creator_id}:{batch_id}:{page - 1}"))
        if page < total_pages - 1:
            nav.append(InlineKeyboardButton(text="▶️", callback_data=f"vp:new_videos:select_template:{creator_id}:{batch_id}:{page + 1}"))
        if nav:
            keyboard_rows.append(nav)

    keyboard_rows.append([
        InlineKeyboardButton(text="➕ Создать шаблон", callback_data=f"vp:new_videos:create_template:{creator_id}:{batch_id}"),
        InlineKeyboardButton(text="❌ Отменить", callback_data=cancel_cb),
    ])

    page_line = f"Страница *{page + 1}*/{total_pages}\n\n" if total_pages > 1 else ""
    text = (
        f"📑 *Выберите шаблон*\n\n"
        f"🎬 Видео: *{len(videos)}*\n"
        f"💰 Стоимость: *{total_cost:.2f} грн*  _({len(videos)} × {price_per_video:.2f} грн)_\n\n"
        f"{_new_videos_preview(videos)}\n\n"
        f"{page_line}"
        f"_Выберите шаблон для комментирования:_"
    )

    await message.edit_text(
        text,
        reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard_rows),
        parse_mode="Markdown",
        disable_web_page_preview=True,
    )
    return True


@router.callback_query(F.data.startswith("vp:new_videos:select_template:"))
async def new_videos_select_template(callback: CallbackQuery, state: FSMContext):
    """Обработчик выбора шаблона для новых видео (тонкая обёртка над renderer)."""
    try:
        await callback.answer()
    except TelegramBadRequest as e:
        if "query is too old" in str(e).lower() or "timeout" in str(e).lower():
            logger.warning(f"Callback query expired: {callback.data[:50]}")
        else:
            raise

    try:
        if not callback.message:
            return

        parts = callback.data.split(":")
        creator_id = int(parts[3])
        batch_id = int(parts[4])
        page = int(parts[5]) if len(parts) > 5 else 0

        user_id = await get_user_backend_id(callback.from_user.id)
        if not user_id:
            try:
                await callback.answer("❌ Пользователь не найден", show_alert=True)
            except TelegramBadRequest:
                pass
            return

        await render_template_picker(
            callback.message,
            state,
            user_id=user_id,
            creator_id=creator_id,
            batch_id=batch_id,
            page=page,
            notify=callback.answer,
        )

    except Exception as e:
        logger.error(f"Error selecting template for new videos: {e}", exc_info=True)
        try:
            await callback.answer("❌ Ошибка", show_alert=True)
        except TelegramBadRequest:
            # Callback уже истек - это нормально, не критично
            pass


@router.callback_query(F.data.startswith("vp:new_videos:template_selected:"))
async def new_videos_template_selected(callback: CallbackQuery, state: FSMContext):
    """Обработчик выбора конкретного шаблона для новых видео - показывает карточку заказа."""
    # ИСПРАВЛЕНО: Отвечаем на callback СРАЗУ
    try:
        await callback.answer()
    except TelegramBadRequest as e:
        if "query is too old" in str(e).lower() or "timeout" in str(e).lower():
            logger.warning(f"Callback query expired: {callback.data[:50]}")
        else:
            raise
    
    try:
        if not callback.message:
            return
        
        # P0 FIX: Парсим callback_data: vp:new_videos:template_selected:{creator_id}:{batch_id}:{template_id}
        parts = callback.data.split(":")
        creator_id = int(parts[3])
        batch_id = int(parts[4])
        template_id = int(parts[5])
        
        # Получаем user_id
        user_id = await get_user_backend_id(callback.from_user.id)
        if not user_id:
            try:
                await callback.answer("❌ Пользователь не найден", show_alert=True)
            except TelegramBadRequest:
                pass
            return
        
        # Получаем batch и извлекаем video_ids
        batch_data = await api_client.get_video_batch(batch_id, user_id)
        if not batch_data:
            try:
                await callback.answer("❌ Batch не найден или истек", show_alert=True)
            except TelegramBadRequest:
                pass
            return

        batch_creator_id = int(batch_data.get("creator_id") or 0)
        if batch_creator_id and batch_creator_id != creator_id:
            logger.warning(
                "New videos template callback creator mismatch: callback=%s batch=%s batch_id=%s user_id=%s",
                creator_id,
                batch_creator_id,
                batch_id,
                user_id,
            )
            try:
                await callback.answer("Старое уведомление. Откройте список видео заново.", show_alert=True)
            except TelegramBadRequest:
                pass
            return
        
        video_ids = [int(vid) for vid in batch_data.get("video_ids", []) if vid]
        
        # Получаем шаблон
        templates = filter_full_templates(await api_client.list_task_templates(user_id))
        template = next((t for t in templates if t.get("id") == template_id), None)
        
        if not template:
            try:
                await callback.answer("❌ Шаблон не найден", show_alert=True)
            except TelegramBadRequest:
                pass
            return
        
        # Получаем видео
        videos = await api_client.get_videos_by_ids(video_ids, user_id)
        skipped_stale_count = 0
        if videos:
            available_videos = []
            for video in videos:
                if str(video.get("status") or "").strip().lower() == "new" and not video.get("task_id"):
                    available_videos.append(video)
                else:
                    skipped_stale_count += 1
            if not available_videos:
                try:
                    await callback.answer("Эти видео уже поставлены в очередь или обработаны.", show_alert=True)
                except TelegramBadRequest:
                    pass
                return
            videos = available_videos
            video_ids = [int(video.get("id")) for video in videos if video.get("id")]
        if not videos:
            try:
                await callback.answer("❌ Видео не найдены", show_alert=True)
            except TelegramBadRequest:
                pass
            return
        
        # Получаем цены
        pricing = await api_client.get_pricing()
        # ИСПРАВЛЕНО: Конвертируем Decimal в float для форматирования
        price_per_video = float(pricing.get("price_success", 5.0))
        total_cost = len(videos) * price_per_video
        
        # P0 FIX: Сохраняем batch_id вместо video_ids_str
        await state.update_data(
            creator_id=creator_id,
            batch_id=batch_id,
            video_ids=video_ids,
            video_urls=[v.get("url") for v in videos],
            template_id=template_id,
            total_cost=total_cost
        )
        
        # Формируем карточку заказа
        template_name = escape_markdown(template.get("name", "Без названия"))
        main_comment_raw = str(template.get("main_comment", "") or "").replace("\n", " ")
        main_comment = markdown_inline_code(main_comment_raw[:100] + ("..." if len(main_comment_raw) > 100 else ""))
        from bot.services.template_utils import template_replies
        template_reply_list = template_replies(template)
        reply_count = min(len(template_reply_list), settings.MAX_REPLIES)
        
        # Карточка заказа: каждая строка — пара «*label:* value». Лейблы — жирные
        # для быстрого скана глазом, значения — обычные/моноширные где это
        # семантически данные (текст коммента, имя шаблона).
        message = (
            f"📋 *Карточка заказа*\n\n"
            f"🎬 *Видео:* *{len(videos)}*\n"
            f"💰 *Стоимость:* *{total_cost:.2f} грн*  _({len(videos)} × {price_per_video:.2f} грн)_\n"
            f"📑 *Шаблон:* {template_name}\n"
            f"💬 *Комментарий:* {main_comment}\n"
            f"↩️ *Ответов:* *{reply_count}*\n\n"
            f"{_new_videos_preview(videos, limit=7)}\n\n"
            f"_После запуска будет создан один заказ на все выбранные видео._"
        )

        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🚀 Запустить", callback_data=f"vp:new_videos:confirm_order:{creator_id}:{batch_id}:{template_id}")],
            [InlineKeyboardButton(text="⬅️ Назад", callback_data=f"vp:new_videos:select_template:{creator_id}:{batch_id}")]
        ])
        
        await callback.message.edit_text(
            message,
            reply_markup=keyboard,
            parse_mode="Markdown",
            disable_web_page_preview=True,
        )
        
    except Exception as e:
        logger.error(f"Error showing order card: {e}", exc_info=True)
        try:
            await callback.answer("❌ Ошибка", show_alert=True)
        except TelegramBadRequest:
            pass


@router.callback_query(F.data.startswith("vp:new_videos:confirm_order:"))
async def new_videos_confirm_order(callback: CallbackQuery, state: FSMContext):
    """Обработчик подтверждения заказа - создает один заказ на выбранные видео."""
    # ИСПРАВЛЕНО: Отвечаем на callback СРАЗУ
    try:
        await callback.answer()
    except TelegramBadRequest as e:
        if "query is too old" in str(e).lower() or "timeout" in str(e).lower():
            logger.warning(f"Callback query expired: {callback.data[:50]}")
        else:
            raise
    
    try:
        if not callback.message:
            return
        
        # P0 FIX: Парсим callback_data: vp:new_videos:confirm_order:{creator_id}:{batch_id}:{template_id}
        parts = callback.data.split(":")
        creator_id = int(parts[3])
        batch_id = int(parts[4])
        template_id = int(parts[5])
        
        # Получаем user_id
        user_id = await get_user_backend_id(callback.from_user.id)
        if not user_id:
            try:
                await callback.answer("❌ Пользователь не найден", show_alert=True)
            except TelegramBadRequest:
                pass
            return
        
        # Получаем batch и извлекаем video_ids
        batch_data = await api_client.get_video_batch(batch_id, user_id)
        if not batch_data:
            try:
                await callback.answer("❌ Batch не найден или истек", show_alert=True)
            except TelegramBadRequest:
                pass
            return
        
        batch_creator_id = int(batch_data.get("creator_id") or 0)
        if batch_creator_id and batch_creator_id != creator_id:
            logger.warning(
                "New videos confirm creator mismatch: callback=%s batch=%s batch_id=%s user_id=%s",
                creator_id,
                batch_creator_id,
                batch_id,
                user_id,
            )
            try:
                await callback.answer("Старое уведомление. Откройте список видео заново.", show_alert=True)
            except TelegramBadRequest:
                pass
            return

        video_ids = [int(vid) for vid in batch_data.get("video_ids", []) if vid]
        
        # Получаем шаблон
        templates = filter_full_templates(await api_client.list_task_templates(user_id))
        template = next((t for t in templates if t.get("id") == template_id), None)
        
        if not template:
            try:
                await callback.answer("❌ Шаблон не найден", show_alert=True)
            except TelegramBadRequest:
                pass
            return
        
        # Получаем видео
        videos = await api_client.get_videos_by_ids(video_ids, user_id)
        skipped_stale_count = 0
        if videos:
            available_videos = []
            for video in videos:
                if str(video.get("status") or "").strip().lower() == "new" and not video.get("task_id"):
                    available_videos.append(video)
                else:
                    skipped_stale_count += 1
            if not available_videos:
                try:
                    await callback.answer("Эти видео уже поставлены в очередь или обработаны.", show_alert=True)
                except TelegramBadRequest:
                    pass
                return
            videos = available_videos
            video_ids = [int(video.get("id")) for video in videos if video.get("id")]
        if not videos:
            try:
                await callback.answer("❌ Видео не найдены", show_alert=True)
            except TelegramBadRequest:
                pass
            return
        
        # Получаем настройки шаблона
        template_settings = template.get("settings", {})
        from bot.services.template_utils import template_replies
        template_reply_list = template_replies(template)
        try:
            requested_reply_count = int(template_settings.get("reply_count", len(template_reply_list)) or 0)
        except (TypeError, ValueError):
            requested_reply_count = len(template_reply_list)
        reply_count = min(requested_reply_count, len(template_reply_list), settings.MAX_REPLIES)
        selected_replies = template_reply_list[:reply_count]
        
        # Формируем settings для заказа
        task_settings = {
            "reply_mode": template_settings.get("reply_mode", "A"),
            "reply_count": reply_count,
            "replies": selected_replies,
            "delays": template_settings.get("delays", True),
            "shuffle": template_settings.get("shuffle", False),
            "rotate_accounts": template_settings.get("rotate_accounts", True),
            # Привязка к шаблону — нужна для отображения "Шаблон: X" в деталях
            # заказа и при последующих копиях.
            "template_id": template_id,
            "template_name": template.get("name") or f"Шаблон #{template_id}",
        }
        
        # P1 FIX: Создаем ОДИН заказ на все видео (вместо N задач)
        creator_usernames = sorted({
            str(video.get("creator_username") or "").strip().lstrip("@")
            for video in videos
            if str(video.get("creator_username") or "").strip()
        })
        if creator_usernames:
            if len(creator_usernames) == 1:
                task_settings["creator_username"] = creator_usernames[0]
            else:
                task_settings["creator_username"] = ", ".join(f"@{name}" for name in creator_usernames[:3])
        task_settings["creator_source_id"] = creator_id

        video_urls = []
        video_ids_list = []
        errors = []
        
        # Собираем все URL видео
        for video in videos:
            video_id = video.get("id")
            video_url = video.get("url")
            if not video_url:
                errors.append(f"Видео {video_id}: нет URL")
                continue
            video_urls.append(video_url)
            video_ids_list.append(video_id)
        
        if not video_urls:
            await callback.message.edit_text(
                "❌ *Заказ не создан*\n\n"
                "В выбранной пачке нет видео с валидной ссылкой на TikTok.",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="⬅️ Выбрать шаблон", callback_data=f"vp:new_videos:select_template:{creator_id}:{batch_id}")],
                    [InlineKeyboardButton(text="🏠 Главное меню", callback_data="menu:main")],
                ]),
                parse_mode="Markdown",
                disable_web_page_preview=True,
            )
            return
        
        main_comment = template.get("main_comment", "")
        if not main_comment:
            await callback.message.edit_text(
                "❌ *Заказ не создан*\n\n"
                "В выбранном шаблоне нет основного комментария.",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="⬅️ Выбрать шаблон", callback_data=f"vp:new_videos:select_template:{creator_id}:{batch_id}")],
                    [InlineKeyboardButton(text="🏠 Главное меню", callback_data="menu:main")],
                ]),
                parse_mode="Markdown",
                disable_web_page_preview=True,
            )
            return

        # Берём варианты комментария из шаблона (не reply-тексты!)
        from bot.services.template_utils import template_text_variants
        text_variants = template_text_variants(template)
        if len(text_variants) < 3:
            await callback.message.edit_text(
                "❌ *Заказ не создан*\n\n"
                "В шаблоне должно быть минимум *3* варианта комментария.",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="⬅️ Выбрать шаблон", callback_data=f"vp:new_videos:select_template:{creator_id}:{batch_id}")],
                    [InlineKeyboardButton(text="🏠 Главное меню", callback_data="menu:main")],
                ]),
                parse_mode="Markdown",
                disable_web_page_preview=True,
            )
            return

        # Показываем прогресс только после локальной валидации, чтобы при
        # ошибке не оставлять пользователя на вечном «создании заказа».
        await callback.message.edit_text(f"{Emoji.PENDING} Создание заказа...")
        
        try:
            # Создаем ОДИН заказ на все видео
            # REFACTOR: Устанавливаем origin для new_videos
            if "origin" not in task_settings:
                task_settings["origin"] = "new_videos"
            
            task = await api_client.create_task(
                user_id=user_id,
                main_comment=main_comment,
                text_variants=text_variants[:10],  # Ограничиваем максимум 10 вариантами
                video_urls=video_urls,  # Все видео в одном заказе
                reply1=template.get("reply1"),
                reply2=template.get("reply2"),
                reply3=template.get("reply3"),
                replies=selected_replies,
                likes=template.get("likes", 0),
                settings=task_settings
            )
            
            # REFACTOR: API возвращает {"task_id": ...} согласно create_task endpoint
            task_id = task.get("task_id")
            if not task_id:
                raise RuntimeError("API не вернул ID созданного заказа")
        
            # Обновляем статус видео в БД через специальный API endpoint
            link_warning = ""
            if task_id and video_ids_list:
                try:
                    # Создаем пары (video_id, task_id) для всех видео
                    video_task_pairs = [(vid, task_id) for vid in video_ids_list]
                    await api_client.link_videos_to_tasks(user_id, video_task_pairs)
                    logger.info(f"Linked {len(video_task_pairs)} videos to task {task_id} for user {user_id}")
                except Exception as e:
                    logger.error(f"Error linking videos to task: {e}", exc_info=True)
                    link_warning = "Заказ создан, но входящие видео не удалось связать с ним автоматически."
        
            await state.clear()

            # Итоговое сообщение: главное — task_id (моноширный = удобно
            # скопировать, плюс в кнопке ниже прямой переход к деталям).
            message = f"✅ *Заказ создан*\n\n"
            message += f"📋 Заказ: `#{task_id}`\n"
            message += f"🎬 Видео: *{len(video_urls)}*\n"
            message += _new_videos_preview(videos, limit=5) + "\n"
            if skipped_stale_count:
                message += f"\n⚠️ Пропущено уже обработанных видео: *{skipped_stale_count}*\n"

            if errors:
                message += f"\n⚠️ Ошибок: *{len(errors)}* (некоторые видео пропущены)\n"
            if link_warning:
                message += f"\n⚠️ {escape_markdown(link_warning)}\n"
            order_created_ok = True
        except Exception as e:
            await state.clear()
            # Обработка ошибок при создании задачи
            logger.error(f"Error creating task for videos: {e}", exc_info=True)
            error_text, _ = get_task_creation_error_message(e)
            short_error = escape_markdown(error_text.split("\n")[0].replace("❌ ", "").replace("*", ""))
            message = f"❌ *Ошибка создания заказа*\n\n{short_error}\n\n"
            if errors:
                message += f"_Дополнительные ошибки:_\n" + "\n".join(escape_markdown(err) for err in errors[:3])
            order_created_ok = False
            task_id = None

        # Сразу после создания заказа удобно открыть его детали — там виден
        # прогресс выполнения. Раньше юзер получал «Мои заказы» и должен был
        # искать свой свежесозданный среди списка. Прямая ссылка короче.
        keyboard_rows: list[list[InlineKeyboardButton]] = []
        if order_created_ok and task_id:
            keyboard_rows.append([
                InlineKeyboardButton(text=f"👁 Открыть заказ #{task_id}", callback_data=f"order:detail:{task_id}")
            ])
        keyboard_rows.append([
            InlineKeyboardButton(text="📋 Мои заказы", callback_data="menu:orders"),
            InlineKeyboardButton(text="🏠 Главное меню", callback_data="menu:main"),
        ])
        keyboard = InlineKeyboardMarkup(inline_keyboard=keyboard_rows)
        
        await callback.message.edit_text(
            message,
            reply_markup=keyboard,
            parse_mode="Markdown",
            disable_web_page_preview=True,
        )
        
    except Exception as e:
        logger.error(f"Error confirming order for new videos: {e}", exc_info=True)
        try:
            await callback.answer("❌ Ошибка создания заказов", show_alert=True)
        except TelegramBadRequest:
            pass
        if callback.message:
            try:
                await callback.message.edit_text(
                    f"❌ *Ошибка*\n\n{escape_markdown(str(e)[:200])}",
                    reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                        [InlineKeyboardButton(text="🏠 Главное меню", callback_data="menu:main")]
                    ]),
                    parse_mode="Markdown"
                )
            except TelegramBadRequest:
                pass


@router.callback_query(F.data.startswith("vp:new_videos:create_template:"))
async def new_videos_create_template(callback: CallbackQuery, state: FSMContext):
    """Обработчик создания шаблона для новых видео - перенаправляет на создание шаблона."""
    # ИСПРАВЛЕНО: Отвечаем на callback СРАЗУ
    try:
        await callback.answer()
    except TelegramBadRequest as e:
        if "query is too old" in str(e).lower() or "timeout" in str(e).lower():
            logger.warning(f"Callback query expired: {callback.data[:50]}")
        else:
            raise
    
    try:
        if not callback.message:
            return
        
        # P0 FIX: Парсим callback_data: vp:new_videos:create_template:{creator_id}:{batch_id}
        parts = callback.data.split(":")
        creator_id = int(parts[3])
        batch_id = int(parts[4])
        
        # Получаем user_id
        user_id = await get_user_backend_id(callback.from_user.id)
        if not user_id:
            try:
                await callback.answer("❌ Пользователь не найден", show_alert=True)
            except TelegramBadRequest:
                pass
            return
        
        # Получаем batch и извлекаем video_ids
        batch_data = await api_client.get_video_batch(batch_id, user_id)
        if not batch_data:
            try:
                await callback.answer("❌ Batch не найден или истек", show_alert=True)
            except TelegramBadRequest:
                pass
            return

        batch_creator_id = int(batch_data.get("creator_id") or 0)
        if batch_creator_id and batch_creator_id != creator_id:
            logger.warning(
                "New videos create-template callback creator mismatch: callback=%s batch=%s batch_id=%s user_id=%s",
                creator_id,
                batch_creator_id,
                batch_id,
                user_id,
            )
            try:
                await callback.answer("Старое уведомление. Откройте список видео заново.", show_alert=True)
            except TelegramBadRequest:
                pass
            return
        
        video_ids = [int(vid) for vid in batch_data.get("video_ids", []) if vid]
        
        # P0 FIX: Сохраняем batch_id вместо video_ids_str
        await state.update_data(
            new_videos_creator_id=creator_id,
            new_videos_batch_id=batch_id,
            return_to_new_videos=True,
            creating_template_via_quick_order=True  # Маркер для правильной обработки
        )
        
        # ИСПРАВЛЕНО: Кнопка "Назад" возвращает к выбору шаблонов, а не в меню
        # Переходим к созданию шаблона через стандартный flow
        from bot.state.order_states import OrderStates
        await callback.message.edit_text(
            "💬 *Создание шаблона*\n\n"
            "Отправьте основной комментарий для шаблона:",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="⬅️ Назад", callback_data=f"vp:new_videos:select_template:{creator_id}:{batch_id}")]
            ]),
            parse_mode="Markdown"
        )
        await state.set_state(OrderStates.NEW_TEMPLATE_MAIN_COMMENT)
        
    except Exception as e:
        logger.error(f"Error redirecting to template creation: {e}", exc_info=True)
        try:
            await callback.answer("❌ Ошибка", show_alert=True)
        except TelegramBadRequest:
            pass


@router.callback_query(F.data.startswith("vp:new_videos:cancel:"))
async def new_videos_cancel_selection(callback: CallbackQuery, state: FSMContext):
    """
    ИСПРАВЛЕНО: Отмена выбора шаблона для новых видео.
    Показывает сообщение с возможностью вернуться к списку блогеров или в меню.
    """
    try:
        await callback.answer()
    except TelegramBadRequest as e:
        if "query is too old" in str(e).lower() or "timeout" in str(e).lower():
            logger.warning(f"Callback query expired: {callback.data[:50]}")
        else:
            raise
    
    try:
        if not callback.message:
            return
        
        # Очищаем state
        await state.clear()
        
        # Парсим callback_data для получения информации о блогере
        parts = callback.data.split(":")
        creator_id = int(parts[3]) if len(parts) > 3 else None
        
        await callback.message.edit_text(
            "❌ *Выбор отменён*\n\n"
            "Вы можете вернуться к новым видео позже через список блогеров.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="📋 Мои блогеры", callback_data="vp:list")],
                [InlineKeyboardButton(text="🏠 Главное меню", callback_data="menu:main")]
            ]),
            parse_mode="Markdown"
        )
        
    except Exception as e:
        logger.error(f"Error cancelling new videos selection: {e}", exc_info=True)
        try:
            await callback.answer("❌ Ошибка", show_alert=True)
        except TelegramBadRequest:
            pass


@router.callback_query(F.data.startswith("vp:creator:") & F.data.contains(":auto_disable"))
async def disable_auto_commenting(callback: CallbackQuery):
    """Обработчик отключения авто комментирования."""
    # ИСПРАВЛЕНО: Отвечаем на callback СРАЗУ
    try:
        await callback.answer()
    except TelegramBadRequest as e:
        if "query is too old" in str(e).lower() or "timeout" in str(e).lower():
            logger.warning(f"Callback query expired: {callback.data[:50]}")
        else:
            raise
    
    try:
        if not callback.message:
            return
        
        # Парсим callback_data: vp:creator:{creator_id}:auto_disable
        parts = callback.data.split(":")
        creator_id = int(parts[2])
        
        # Получаем user_id
        user_id = await get_user_backend_id(callback.from_user.id)
        if not user_id:
            try:
                await callback.answer("❌ Пользователь не найден", show_alert=True)
            except TelegramBadRequest:
                pass
            return
        
        # Отключаем авто комментирование (меняем режим на manual_review)
        await api_client.update_video_creator(creator_id, user_id, mode="manual_review", auto_start=False)
        
        await callback.message.edit_text(
            f"✅ *Авто комментирование отключено*\n\n"
            f"Блогер переведён в режим ручной проверки.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="📋 Мои блогеры", callback_data="vp:list")],
                [InlineKeyboardButton(text="🏠 Главное меню", callback_data="menu:main")]
            ]),
            parse_mode="Markdown"
        )
        
    except Exception as e:
        logger.error(f"Error disabling auto commenting: {e}", exc_info=True)
        try:
            await callback.answer("❌ Ошибка", show_alert=True)
        except TelegramBadRequest:
            pass
