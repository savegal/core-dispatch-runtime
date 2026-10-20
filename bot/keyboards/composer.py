from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from bot.services.drafts import Draft
from bot.services.formatting import markdown_inline_code


def get_composer_keyboard(draft: Draft) -> InlineKeyboardMarkup:
    """Клавиатура композера заказа."""
    keyboard = []

    keyboard.append([
        InlineKeyboardButton(text="🔗 Изменить ссылки", callback_data="composer:input_links"),
        InlineKeyboardButton(text="💬 Изменить текст", callback_data="composer:comment:input")
    ])

    pool_button = "👤 Пул: мои" if draft.account_pool_mode == "personal" else "🌐 Пул: общий"
    keyboard.append([
        InlineKeyboardButton(text=pool_button, callback_data="composer:toggle_pool")
    ])

    keyboard.append([
        InlineKeyboardButton(text="🚀 Создать заказ", callback_data="composer:submit")
    ])
    keyboard.append([
        InlineKeyboardButton(text="💾 Сохранить шаблон", callback_data="composer:save_template"),
        InlineKeyboardButton(text="❌ Отмена", callback_data="composer:cancel")
    ])

    return InlineKeyboardMarkup(inline_keyboard=keyboard)


def format_composer_summary(draft: Draft, *, likes_label: str = "выключены") -> str:
    """Форматирует сводку композера."""
    links_count = len(draft.links)
    variants_count = len(draft.text_variants) if draft.text_variants else (1 if draft.comment else 0)
    pool_label = "👤 Мои аккаунты" if draft.account_pool_mode == "personal" else "🌐 Общий пул"

    reply_likes_str = "выключены"
    if draft.reply_like_count > 0 and str(draft.reply_like_target).lower() not in {"none", "0", "off"}:
        target_display = "все" if draft.reply_like_target == "all" else f"ответ {draft.reply_like_target}"
        reply_likes_str = f"{target_display} ({draft.reply_like_count} шт)"

    text = (
        "*Новый заказ*\n\n"
        f"🔗 Видео: *{links_count}*\n"
        f"💬 Вариантов комментария: *{variants_count}*\n"
        f"❤️ Лайки: *{likes_label}*\n"
        f"Пул: *{pool_label}*"
    )
    if reply_likes_str != "выключены":
        text += f"\n❤️ Лайки на ответ: *{reply_likes_str}*"

    if draft.comment:
        raw_preview = draft.comment.replace("\n", " ")
        preview = raw_preview[:80] + "..." if len(raw_preview) > 80 else raw_preview
        text += f"\n\n*Текст*\n{markdown_inline_code(preview)}"

    return text
