from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from typing import Dict, List

from config.settings import get_settings

from bot.services.formatting import markdown_inline_code
from bot.services.main_visibility_ui import visibility_keyboard
from bot.services.template_utils import is_replies_only_template, template_replies, template_text_variants
from bot.utils.markdown_escape import escape_markdown_simple


TEMPLATES_PER_PAGE = 8
settings = get_settings()


def _mono_preview(text: object, limit: int) -> str:
    raw = ("" if text is None else str(text)).strip().replace("\n", " ")
    shortened = raw[:limit]
    if len(raw) > limit:
        shortened += "..."
    return markdown_inline_code(shortened or "—")


def get_template_choice_keyboard(
    templates: List[Dict],
    page: int = 0,
    max_templates: int | None = None
) -> tuple[InlineKeyboardMarkup, int, int]:
    """
    Клавиатура выбора шаблона комментария с пагинацией.
    """
    if max_templates is None:
        max_templates = len(templates)

    total_pages = (len(templates) + TEMPLATES_PER_PAGE - 1) // TEMPLATES_PER_PAGE if templates else 1
    current_page = max(0, min(page, total_pages - 1))

    keyboard = []

    start_idx = current_page * TEMPLATES_PER_PAGE
    end_idx = min(start_idx + TEMPLATES_PER_PAGE, len(templates))
    shown_templates = templates[start_idx:end_idx]

    for template in shown_templates:
        template_id = template.get("id")
        template_name = template.get("name", f"Шаблон #{template_id}")
        if len(template_name) > 30:
            template_name = template_name[:27] + "..."
        if is_replies_only_template(template):
            template_name = f"↩️ {template_name}"

        keyboard.append([
            InlineKeyboardButton(
                text=template_name,
                callback_data=f"template:select:{template_id}"
            )
        ])

    if total_pages > 1:
        nav_buttons = []
        if current_page > 0:
            nav_buttons.append(
                InlineKeyboardButton(
                    text="◀️",
                    callback_data=f"template:page:{current_page - 1}"
                )
            )
        if current_page < total_pages - 1:
            nav_buttons.append(
                InlineKeyboardButton(
                    text="▶️",
                    callback_data=f"template:page:{current_page + 1}"
                )
            )
        if nav_buttons:
            keyboard.append(nav_buttons)

    keyboard.append([InlineKeyboardButton(text="✍️ Без шаблона", callback_data="template:create_new")])
    keyboard.append([InlineKeyboardButton(text="❌ Отмена", callback_data="template:cancel")])

    return InlineKeyboardMarkup(inline_keyboard=keyboard), current_page, total_pages


def get_template_preview_keyboard(template_id: int, reply_count: int = 3, is_editing: bool = False,
                                  verify_main_visibility: bool = False,
                                  verify_reply_visibility: bool = False) -> InlineKeyboardMarkup:
    """
    Клавиатура предпросмотра шаблона.
    """
    keyboard = []

    if not is_editing:
        keyboard.append([
            InlineKeyboardButton(text="🚀 Создать заказ", callback_data=f"quicktpl:confirm:{template_id}:{reply_count}")
        ])
        keyboard.append([
            InlineKeyboardButton(text="Изменить текст", callback_data=f"quicktpl:edit_comment:{template_id}:{reply_count}"),
            InlineKeyboardButton(text="Изменить ответы", callback_data=f"quicktpl:edit_replies:{template_id}:{reply_count}")
        ])

        reply_buttons = []
        if reply_count > 0:
            reply_buttons.append(
                InlineKeyboardButton(
                    text="− Ответов меньше",
                    callback_data=f"quicktpl:set_replies:{template_id}:{reply_count - 1}"
                )
            )
        if reply_count < settings.MAX_REPLIES:
            reply_buttons.append(
                InlineKeyboardButton(
                    text="+ Ответов больше",
                    callback_data=f"quicktpl:set_replies:{template_id}:{reply_count + 1}"
                )
            )
        if reply_buttons:
            keyboard.append(reply_buttons)

        keyboard.append([InlineKeyboardButton(text="◀️ Назад", callback_data="quicktpl:back_to_choice")])

    markup = InlineKeyboardMarkup(inline_keyboard=keyboard)
    return markup if is_editing else visibility_keyboard(markup, verify_main_visibility, verify_reply_visibility)


def format_template_preview(
    template: Dict,
    reply_count: int = 3,
    edited_comment: str | None = None,
    edited_text_variants: list[str] | None = None,
    edited_replies: dict | None = None,
    likes_label: str = "выключены",
) -> str:
    """
    Форматирует предпросмотр шаблона.
    """
    name = template.get("name") or "Без названия"
    safe_name = escape_markdown_simple(str(name))
    text_variants = edited_text_variants or template_text_variants(template)
    if edited_comment is not None and not edited_text_variants:
        text_variants = [edited_comment] if edited_comment else []

    main_comment = text_variants[0] if text_variants else template.get("main_comment", "")
    replies_source = template_replies(template)
    if edited_replies is not None:
        replies_source = [edited_replies[idx] for idx in sorted(edited_replies.keys()) if edited_replies.get(idx)]

    comment_lines = []
    for idx, variant in enumerate(text_variants[:3], start=1):
        comment_lines.append(f"{idx}. {_mono_preview(variant, 120)}")

    text = (
        "*Проверьте заказ*\n\n"
        f"📑 Шаблон: *{safe_name}*\n"
        f"💬 Комментарий: *{len(text_variants) if text_variants else 1} вариант(а)*\n"
        f"↩️ Ответов: *{reply_count}*\n"
        f"❤️ Лайки: *{escape_markdown_simple(likes_label)}*\n\n"
        "*Комментарий*\n"
        f"{chr(10).join(comment_lines) if comment_lines else _mono_preview(main_comment, 120)}"
    )

    replies = []
    for idx, reply in enumerate(replies_source[:reply_count], start=1):
        replies.append(f"{idx}. {_mono_preview(reply, 80)}")

    if replies:
        text += "\n\n*Ответы*\n" + "\n".join(replies)
    else:
        text += "\n\n*Ответы*\nНе добавлены"

    return text
