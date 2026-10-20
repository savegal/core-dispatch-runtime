from __future__ import annotations

from typing import Optional

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from bot.services.formatting import Emoji, markdown_inline_code
from bot.utils.markdown_escape import escape_markdown_simple


def _safe_preview(text: Optional[str], limit: int) -> str:
    raw = (text or "").strip().replace("\n", " ")
    if not raw:
        return markdown_inline_code("не указано")
    shortened = raw[:limit]
    if len(raw) > limit:
        shortened += "..."
    return markdown_inline_code(shortened)


def _reply_items(replies: dict[int, str], *, limit: int = 3) -> list[str]:
    items: list[str] = []
    for idx in sorted(replies.keys())[:limit]:
        preview = _safe_preview(replies.get(idx), 72)
        items.append(f"{idx}. {preview}")
    return items


def _link_items(links: list[str], *, limit: int = 6) -> list[str]:
    items: list[str] = []
    for idx, link in enumerate((links or [])[:limit], start=1):
        items.append(f"{idx}. {escape_markdown_simple(link)}")
    if len(links) > limit:
        items.append(f"... и ещё {len(links) - limit}")
    return items


def build_cancel_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=f"{Emoji.CANCEL} Отмена", callback_data="menu:main")]
        ]
    )


def build_links_prompt_text() -> str:
    return (
        "*Новый заказ*\n\n"
        "Отправьте ссылки на TikTok-видео, по одной на строку."
    )

def build_template_choice_text(
    links_count: int,
    *,
    page: Optional[int] = None,
    total_pages: Optional[int] = None,
) -> str:
    page_text = ""
    if page is not None and total_pages and total_pages > 1:
        page_text = f"\nСтраница: *{page + 1}/{total_pages}*"

    return (
        "*Выберите шаблон*\n\n"
        f"Ссылок: *{links_count}*{page_text}\n\n"
        "Выберите готовый шаблон или продолжите без него."
    )


def build_main_comment_variant_prompt_text(
    *,
    links_count: int,
    variant_index: int,
    total_variants: int = 3,
) -> str:
    examples = {
        1: "Пример: Отличное видео, спасибо за контент 👍",
        2: "Пример: Очень круто получилось, лайк за подачу 🔥",
        3: "Пример: Сильный ролик, досмотрел до конца 👏",
    }
    return (
        f"*Вариант комментария {variant_index} из {total_variants}*\n\n"
        "Введите текст, который будет опубликован под видео.\n"
        "Все варианты должны отличаться по смыслу.\n\n"
        f"{escape_markdown_simple(examples.get(variant_index, 'Пример: Живой комментарий без копипаста.'))}"
    )


def build_replies_prompt_text(variants_count: int, max_replies: int) -> str:
    return (
        "*Ответы под комментарием*\n\n"
        f"Необязательно. Добавьте до {max_replies} ответов по одному сообщению.\n"
        "Или пропустите этот шаг."
    )


def build_template_name_prompt_text() -> str:
    return (
        "*Сохранить шаблон*\n\n"
        "Введите короткое название шаблона.\n"
        "Если сохранять не нужно, пропустите этот шаг."
    )


def build_template_name_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="⏭ Не сохранять", callback_data="wizard:create_order")],
            [InlineKeyboardButton(text=f"{Emoji.CANCEL} Отмена", callback_data="menu:main")],
        ]
    )


def build_preview_text(
    *,
    links: list[str],
    text_variants: list[str],
    replies: dict[int, str],
    likes_label: str = "выключены",
    reply_likes_label: Optional[str] = None,
    template_name: Optional[str] = None,
    include_template_prompt: bool = False,
    estimated_cost_label: Optional[str] = None,
    balance_label: Optional[str] = None,
) -> str:
    variant_lines: list[str] = []
    for idx, variant in enumerate(text_variants[:4], start=1):
        variant_lines.append(f"{idx}. {_safe_preview(variant, 120)}")
    if len(text_variants) > 4:
        variant_lines.append(f"... и ещё {len(text_variants) - 4}")

    reply_lines = _reply_items(replies, limit=3)
    if len(replies) > 3:
        reply_lines.append(f"... и ещё {len(replies) - 3}")

    link_lines = _link_items(links, limit=6)

    text = (
        "*Проверьте заказ*\n\n"
        f"{Emoji.LINK} Видео: *{len(links)}*\n"
        f"{Emoji.COMMENT} Комментарий: *{len(text_variants)} вариант(а)*\n"
        f"{Emoji.REPLY} Ответов: *{len(replies)}*\n"
        f"{Emoji.LIKES} Лайки: *{escape_markdown_simple(likes_label)}*"
        + (f"\n❤️ Лайки на ответ: *{escape_markdown_simple(reply_likes_label)}*" if (replies and reply_likes_label) else "")
    )

    if estimated_cost_label:
        text += f"\n💸 Списание: *{escape_markdown_simple(estimated_cost_label)}*"
    if balance_label:
        text += f"\n👛 Баланс: *{escape_markdown_simple(balance_label)}*"
    if template_name:
        text += f"\n📑 Шаблон: *{escape_markdown_simple(template_name)}*"

    text += "\n\n*Видео*\n" + ("\n".join(link_lines) if link_lines else "Не добавлены")
    text += "\n\n*Комментарий*\n" + "\n".join(variant_lines or [f"1. {markdown_inline_code('не указано')}"])

    if reply_lines:
        text += "\n\n*Ответы*\n" + "\n".join(reply_lines)
    else:
        text += "\n\n*Ответы*\nНе добавлены"

    if include_template_prompt:
        text += "\n\nЕсли шаблон пригодится ещё раз, его можно сохранить перед запуском."

    return text


def build_manual_preview_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🚀 Создать заказ", callback_data="wizard:create_order")],
            [
                InlineKeyboardButton(text="🔗 Изменить ссылки", callback_data="wizard:edit_links"),
                InlineKeyboardButton(text="💬 Изменить текст", callback_data="wizard:edit_comment"),
            ],
            [
                InlineKeyboardButton(text="↩️ Изменить ответы", callback_data="wizard:edit_replies"),
                InlineKeyboardButton(text="💾 Сохранить шаблон", callback_data="wizard:save_template"),
            ],
            [InlineKeyboardButton(text=f"{Emoji.CANCEL} Отмена", callback_data="menu:main")],
        ]
    )


def build_template_preview_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🚀 Создать заказ", callback_data="wizard:confirm_with_template")],
            [
                InlineKeyboardButton(text="📑 Другой шаблон", callback_data="wizard:change_template"),
                InlineKeyboardButton(text="✍️ Без шаблона", callback_data="wizard:manual_setup"),
            ],
            [InlineKeyboardButton(text="🔗 Изменить ссылки", callback_data="wizard:edit_links")],
            [InlineKeyboardButton(text=f"{Emoji.CANCEL} Отмена", callback_data="menu:main")],
        ]
    )


def build_success_text(
    *,
    task_id: int,
    links_count: int,
    reply_count: int,
    variants_count: int,
    likes: int = 0,
    likes_label: Optional[str] = None,
    reply_likes_label: Optional[str] = None,
    template_name: Optional[str] = None,
    pool_label: Optional[str] = None,
) -> str:
    visible_likes = likes_label
    if visible_likes is None:
        visible_likes = str(likes) if likes else "выключены"
    text = (
        f"{Emoji.SUCCESS} Заказ #{task_id} принят\n\n"
        f"{Emoji.LINK} Видео: *{links_count}*\n"
        f"{Emoji.COMMENT} Комментарий: *{variants_count} вариант(а)*\n"
        f"{Emoji.REPLY} Ответов: *{reply_count}*\n"
        f"{Emoji.LIKES} Лайки: *{escape_markdown_simple(str(visible_likes))}*"
        + (f"\n❤️ Лайки на ответ: *{escape_markdown_simple(reply_likes_label)}*" if (reply_count > 0 and reply_likes_label) else "")
    )
    if pool_label:
        text += f"\nПул: *{escape_markdown_simple(pool_label)}*"
    if template_name:
        text += f"\n📑 Шаблон: *{escape_markdown_simple(template_name)}*"
    text += "\n\nДобавлен в очередь."
    return text
