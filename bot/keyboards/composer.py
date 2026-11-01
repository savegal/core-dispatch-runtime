from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from bot.services.drafts import Draft


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


def format_composer_summary(draft: Draft, *, likes_label: str = "выключены", transport_label: str | None = None) -> str:
    """Форматирует сводку композера."""
    from bot.services.order_creation_ui import build_preview_text
    from bot.services.likes_ui import format_reply_likes_label
    text = build_preview_text(
        links=draft.links, text_variants=draft.text_variants or ([draft.comment] if draft.comment else []),
        replies=dict(enumerate(draft.replies, 1)), likes_label=likes_label,
        reply_likes_label=format_reply_likes_label(draft.reply_like_target, draft.reply_like_count),
        template_name=draft.template_name, transport_label=transport_label,
    )
    text += "\n\nПул: *" + ("Мои аккаунты" if draft.account_pool_mode == "personal" else "Общий пул") + "*"
    return text
