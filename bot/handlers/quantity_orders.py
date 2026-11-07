"""Order composition by publication quantities, using the existing template library."""
from html import escape
import asyncio
from decimal import Decimal
import math
import logging
import re
from uuid import uuid4

from aiogram import F, Router
from aiogram.filters import StateFilter
from aiogram.dispatcher.event.bases import SkipHandler
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import InlineKeyboardButton as Button, InlineKeyboardMarkup as Markup

from bot.services.api_client import api_client, APIRequestError
from bot.services.user_utils import get_user_backend_id
from bot.services.template_utils import template_text_variants, template_replies, filter_full_templates, is_full_template
from bot.services.validators import normalize_url, validate_video_url
from services.quantity_plan import QuantityPlan, quantity_plan
from services.publication_runtime import (
    MAX_SEND_WORKERS, ORDER_OPTIONS_KEY, OrderPublicationOptions, PublicationRuntime,
    default_runtime, order_runtime, publication_capacity,
)
from bot.services.order_presentation import excerpt, number

router = Router(name="quantity_orders")
logger = logging.getLogger(__name__)


class QuantityStates(StatesGroup):
    links = State()
    texts = State()
    main_count = State()
    reply_count = State()
    replies = State()
    name = State()
    limits = State()
    speed = State()
    preview = State()
    template_counts = State()
    template_texts = State()
    pending_texts = State()


def keyboard(*rows):
    return Markup(inline_keyboard=[[Button(text=label, callback_data=callback) for label, callback in row] for row in rows])


CANCEL = keyboard([("🏠 Меню", "menu:main")])


def parse_links(text):
    links = []
    for raw in re.findall(r"https?://[^\s<>]+", text or ""):
        url = normalize_url(raw.rstrip(".,);]"))
        if validate_video_url(url) and url not in links:
            links.append(url)
    if not links or len(links) > 100:
        raise ValueError("Пришлите от 1 до 100 ссылок на TikTok-видео.")
    return links


def parse_texts(text, *, limit, max_length):
    values = [line.strip() for line in (text or "").splitlines() if line.strip()]
    if not values or len(values) > limit or any(len(value) > max_length for value in values):
        raise ValueError(f"Отправьте от 1 до {limit} текстов, каждый с новой строки, до {max_length} символов.")
    return values


def make_plan(main_count, replies_per_main, previous=None):
    total = main_count * (1 + replies_per_main)
    if previous:
        return QuantityPlan.model_validate({**previous, "main_count": main_count, "replies_per_main": replies_per_main,
            "max_dispatches": max(total, previous.get("max_dispatches") or total * 10)}).model_dump()
    return QuantityPlan(main_count=main_count, replies_per_main=replies_per_main,
                        max_seconds=min(21600, max(1800, total * 100))).model_dump()


def draft_runtime(data):
    snapshot = data.get("publication_defaults") or {}
    defaults = PublicationRuntime.model_validate({key: snapshot[key] for key in PublicationRuntime.model_fields
                                                 if key in snapshot}) if snapshot else default_runtime()
    return order_runtime(data, defaults), snapshot.get("browser_slots")


async def load_preview_runtime(state, data, *, refresh=False):
    if "publication_defaults" in data and not refresh:
        return
    try:
        response = await api_client.get_publication_runtime()
        values = {key: response[key] for key in PublicationRuntime.model_fields if key in response}
        defaults = PublicationRuntime.model_validate(values).model_dump(mode="json")
        snapshot = {**defaults, "browser_slots": max(1, int(response["browser_slots"]))}
    except (APIRequestError, KeyError, TypeError, ValueError):
        snapshot = data.get("publication_defaults") or {}
    data["publication_defaults"] = snapshot
    await state.update_data(publication_defaults=snapshot)


def speed_lines(data):
    runtime, slots = draft_runtime(data)
    if slots == 1:
        return ["Один браузерный слот · HTTP-отправка и проверка чередуются"]
    if slots is None:
        return [f"Профиль · HTTP-отправки <b>{runtime.send_workers}</b>, проверки {runtime.check_workers}",
                "Лимит исполнителя пока недоступен"]
    senders, readers = runtime.limits(slots)
    return [f"Лимит параллельности · HTTP-отправки <b>{senders}</b>, проверки <b>{readers}</b>"]


def preview_text(data):
    plan = QuantityPlan.model_validate(data["publication_plan"])
    videos = len(data.get("links") or [])
    total = plan.main_count * (1 + plan.replies_per_main)
    title = "Новый шаблон" if data.get("template_only") else "Новый заказ"
    lines = [f"<b>◈ {title} · проверка</b>",
        "Сохраните тексты и параметры для следующих заказов." if data.get("template_only") else "Настройте цель и запустите выполнение.", "",
        "<b>План публикаций</b>", f"Видео: <b>{videos}</b>",
        f"Главных комментариев на видео: <b>{plan.main_count}</b>",
        f"Ответов под каждым главным: <b>{plan.replies_per_main}</b>",
        f"Всего ответов на видео: <b>{plan.main_count * plan.replies_per_main}</b>",
        f"На весь заказ: {number(plan.main_count * videos)} главных · {number(plan.main_count * plan.replies_per_main * videos)} ответов",
        f"Пул аккаунтов: {'личный' if data.get('pool') == 'personal' else 'общий'}", "",
        "<b>Библиотека текстов</b>",
        f"Вариантов главного текста: {len(data.get('texts') or [])}",
        f"Вариантов ответа: {len(data.get('replies') or [])}"]
    if data.get("template_name"):
        lines.append(f"Шаблон: {excerpt(data['template_name'], 70)}")
    lines.append("Тексты чередуются; их количество не меняет цель заказа.")
    if data.get("cost_label"):
        lines.extend(["", "<b>Стоимость</b>", f"Списание за заказ: {excerpt(data['cost_label'], 90)}",
                      f"Баланс: {excerpt(data.get('balance_label') or '—', 50)}"])
    lines.extend(["", "<b>Ресурсы и лимиты</b>",
        *speed_lines(data),
        f"Ориентир при конверсии 10–18%: ≈ {number(math.ceil(total / .18))}–{number(total * 10)} отправок на видео.",
        "Это оценка, фактическая конверсия появится по результатам.",
        f"Лимит отправок на видео: {number(plan.max_dispatches)}",
        f"Лимит времени на видео: {plan.max_seconds // 60} мин",
        f"Лимит трафика задания: {str(plan.traffic_limit_mb) + ' МБ' if plan.traffic_limit_mb else 'не задан'}",
        "", "<b>Подтверждение результата</b>",
        "Цель учитывает публикации, найденные независимым аккаунтом.",
        "Ответы размещаются под каждым подтверждённым главным.",
        "Неопределённая отправка сохраняется для проверки.", "", "<b>Главные тексты · превью</b>"])
    lines.extend(f"{index}. {excerpt(text, 80, encoded_limit=190)}" for index, text in enumerate((data.get("texts") or [])[:3], 1))
    if len(data.get("texts") or []) > 3:
        lines.append(f"Ещё вариантов: {len(data['texts']) - 3}")
    if data.get("replies"):
        lines.extend(["", "<b>Ответы · превью</b>"])
        lines.extend(f"{index}. {excerpt(text, 80, encoded_limit=190)}" for index, text in enumerate(data["replies"][:3], 1))
        if len(data["replies"]) > 3:
            lines.append(f"Ещё вариантов: {len(data['replies']) - 3}")
    return "\n".join(lines)


async def show_preview(message, state, *, edit=False):
    data = await state.get_data()
    if not data.get("publication_plan") or not data.get("texts"):
        await message.answer("Редактор заказа устарел. Откройте создание заказа заново.", reply_markup=CANCEL)
        return
    user_id = data.get("backend_user_id")
    if user_id and not data.get("template_only"):
        try:
            pricing, user, _ = await asyncio.gather(api_client.get_pricing(), api_client.get_user(user_id),
                                                     load_preview_runtime(state, data))
            amount = Decimal(str(pricing["price_success"])) * len(data.get("links") or [])
            from config.settings import get_settings
            if user.get("role") in {"admin", "owner"} or get_settings().DEMO_MODE:
                amount = Decimal("0")
            data["cost_label"] = f"{amount:.2f} грн"
            data["balance_label"] = f"{Decimal(str(user.get('balance') or 0)):.2f} грн"
        except APIRequestError:
            data["cost_label"] = "временно не удалось получить цену"
    rows = [[("🚀 Создать заказ", "qo:create")]] if not data.get("template_only") else []
    rows.extend([[('Главные · количество', 'qo:edit:main'), ('Ответов под каждым', 'qo:edit:reply')],
                 [('✎ Главные тексты', 'qo:edit:texts'), ('✎ Тексты ответов', 'qo:edit:replies')],
                 [('⚙ Лимиты', 'qo:limits'), ('👥 Пул аккаунтов', 'qo:pool')],
                 [('Потоки и скорость', 'qo:speed')],
                 [('💾 Сохранить шаблон', 'qo:save')], [('🏠 Меню', 'menu:main')]])
    method = message.edit_text if edit else message.answer
    await method(preview_text(data), reply_markup=keyboard(*rows), parse_mode="HTML")
    await state.set_state(QuantityStates.preview)


async def copy_full_order(message, state, task, user_id):
    """Copy libraries and quantities; discard receipts and execution-only state."""
    from bot.handlers.orders import _order_replies, _order_text_variants, _build_recreated_order_settings
    from services.quantity_plan import ensure_quantity_plan
    source = task.get("settings") or {}
    replies = _order_replies(task)
    texts = _order_text_variants(task) or [task.get("main_comment") or "Комментарий"]
    settings = ensure_quantity_plan(_build_recreated_order_settings(source, origin="quantity_copy"), replies)
    await state.clear()
    await state.update_data(links=list(task.get("video_urls") or []), texts=texts, replies=replies,
        publication_plan=settings["publication_plan"], pool=source.get("account_pool_mode") or "shared",
        template_id=source.get("template_id"), template_name=source.get("template_name"),
        template_settings=settings, publication_options=settings.get(ORDER_OPTIONS_KEY) or {},
        backend_user_id=user_id, idempotency_key=uuid4().hex)
    await show_preview(message, state, edit=True)


async def choose_template(message, state, user_id, *, edit=False):
    templates = filter_full_templates(await api_client.list_task_templates(user_id))
    rows = [[(str(t.get("name") or "Шаблон")[:55], f"qo:template:{t['id']}")] for t in templates[:30]]
    rows.extend([[('✍️ Ввести тексты', 'qo:manual')], [('🏠 Меню', 'menu:main')]])
    method = message.edit_text if edit else message.answer
    await method("◈ Новый заказ · тексты\n\nВыберите сохранённый шаблон или введите новую библиотеку комментариев.\nКоличество публикаций задаётся отдельно от числа текстов.", reply_markup=keyboard(*rows))


async def start(message, state, *, edit=False, links=None, template_only=False, actor_id=None):
    await state.clear()
    if actor_id:
        from bot.services.drafts import reset_draft, set_links
        reset_draft(actor_id)
        if links:
            set_links(actor_id, links)
    await state.update_data(links=links or [], pool="shared", replies=[],
                            template_only=template_only, idempotency_key=uuid4().hex,
                            backend_user_id=await get_user_backend_id(actor_id or message.from_user.id))
    if template_only:
        await state.set_state(QuantityStates.texts)
        await message.edit_text("Пришлите тексты главных комментариев. Каждый вариант — с новой строки.", reply_markup=CANCEL)
    elif links:
        await choose_template(message, state, await get_user_backend_id(actor_id or message.from_user.id), edit=edit)
    else:
        await state.set_state(QuantityStates.links)
        method = message.edit_text if edit else message.answer
        await method("Пришлите ссылки на видео. Можно несколько, каждую с новой строки.", reply_markup=CANCEL)


@router.callback_query(F.data == "menu:new_order")
async def start_new_order(callback, state):
    await callback.answer()
    if callback.message:
        await start(callback.message, state, edit=True, actor_id=callback.from_user.id)


@router.callback_query(F.data.in_({"quick:show_templates", "menu:new_order:back", "composer:submit"}))
async def resume_template_picker(callback, state):
    await callback.answer()
    from bot.services.drafts import get_draft
    links = list(get_draft(callback.from_user.id).links or [])
    await start(callback.message, state, edit=True, links=links, actor_id=callback.from_user.id)


@router.callback_query(F.data.in_({"wizard:confirm", "wizard:create_order", "wizard:submit_order", "wizard:confirm_with_template", "new_template:confirm"}))
async def reopen_legacy_composer(callback, state):
    await callback.answer()
    from bot.services.drafts import get_draft
    links = list(get_draft(callback.from_user.id).links or [])
    await start(callback.message, state, edit=True, links=links, actor_id=callback.from_user.id)


@router.callback_query(F.data == "template:create_full")
async def start_full_template(callback, state):
    await callback.answer()
    if callback.message:
        await start(callback.message, state, template_only=True, actor_id=callback.from_user.id)


@router.message(StateFilter(QuantityStates.links))
async def input_links(message, state):
    try:
        links = parse_links(message.text)
    except ValueError as exc:
        await message.answer(str(exc))
        return
    await state.update_data(links=links)
    from bot.services.drafts import set_links
    set_links(message.from_user.id, links)
    data = await state.get_data()
    if data.get("texts"):
        if data.get("publication_plan"):
            await show_preview(message, state)
        else:
            await state.set_state(QuantityStates.main_count)
            await message.answer("Сколько главных комментариев оставить под каждым видео? От 1 до 500.", reply_markup=CANCEL)
    else:
        await choose_template(message, state, await get_user_backend_id(message.from_user.id))


@router.message(StateFilter(None), F.text.regexp(r"https?://(?:www\.|vm\.|vt\.)?tiktok\.com/"))
async def pasted_links(message, state):
    try:
        links = parse_links(message.text)
    except ValueError as exc:
        await message.answer(str(exc))
        return
    await start(message, state, links=links, actor_id=message.from_user.id)


@router.callback_query(F.data.startswith("qo:template:"))
@router.callback_query(F.data.startswith("template:apply:"))
@router.callback_query(F.data.startswith("template:select:"))
@router.callback_query(F.data.startswith("quicktpl:confirm:"))
async def use_template(callback, state):
    await callback.answer()
    user_id = await get_user_backend_id(callback.from_user.id)
    template = await api_client.get_task_template(int(callback.data.split(":")[2]), user_id)
    if not is_full_template(template):
        raise SkipHandler()
    settings = dict(template.get("settings") or {})
    texts, replies = template_text_variants(template), list(template_replies(template))
    if isinstance(template_replies(template), dict):
        replies = list(template_replies(template).values())
    plan = quantity_plan(settings)
    data = await state.get_data()
    if not data.get("links"):
        from bot.services.drafts import get_draft
        draft_links = list(get_draft(callback.from_user.id).links or [])
        if draft_links:
            data["links"] = draft_links
            await state.update_data(links=draft_links)
    await state.update_data(texts=texts or [template["main_comment"]], replies=replies,
        template_id=template["id"], template_name=template.get("name"), template_settings=settings,
        idempotency_key=uuid4().hex, pool=data.get("pool") or "shared", editing=False, template_only=False,
        main_count=None, publication_plan=plan.model_dump() if plan else None,
        publication_options=settings.get(ORDER_OPTIONS_KEY) or {})
    await state.update_data(backend_user_id=user_id)
    if plan:
        await state.update_data(publication_plan=plan.model_dump())
    if not data.get("links"):
        await state.set_state(QuantityStates.links)
        await callback.message.edit_text("Пришлите ссылки на видео для выбранного шаблона.", reply_markup=CANCEL)
    elif plan:
        await show_preview(callback.message, state, edit=True)
    else:
        await state.set_state(QuantityStates.main_count)
        await callback.message.edit_text("Сколько главных комментариев оставить под каждым видео? Введите число от 1 до 500.", reply_markup=CANCEL)


@router.callback_query(F.data == "qo:manual")
async def manual_texts(callback, state):
    await callback.answer()
    await state.update_data(template_id=None, template_name=None, template_settings={})
    await state.set_state(QuantityStates.texts)
    await callback.message.edit_text("◈ Новый заказ · главные тексты\n\nПришлите варианты главного комментария, каждый с новой строки.\nТексты будут чередоваться в пределах заданного количества публикаций.", reply_markup=CANCEL)


@router.message(StateFilter(QuantityStates.texts))
async def input_texts(message, state):
    from config.settings import get_settings
    try:
        texts = parse_texts(message.text, limit=50, max_length=get_settings().MAX_COMMENT_LENGTH)
    except ValueError as exc:
        await message.answer(str(exc))
        return
    data = await state.get_data()
    await state.update_data(texts=texts, idempotency_key=uuid4().hex)
    if data.get("editing"):
        await state.update_data(editing=False)
        await show_preview(message, state)
    else:
        await state.set_state(QuantityStates.main_count)
        await message.answer("◈ Новый заказ · количество\n\nСколько главных комментариев оставить под каждым видео? От 1 до 500.\nНапример: 20 комментариев из вашей библиотеки текстов.", reply_markup=CANCEL)


@router.message(StateFilter(QuantityStates.main_count))
async def input_main_count(message, state):
    raw = (message.text or "").strip()
    if not raw.isdecimal() or not 1 <= int(raw) <= 500:
        await message.answer("Введите целое число от 1 до 500.")
        return
    data = await state.get_data()
    previous = data.get("publication_plan") or {}
    await state.update_data(main_count=int(raw), idempotency_key=uuid4().hex)
    if data.get("editing"):
        await state.update_data(publication_plan=make_plan(int(raw), previous.get("replies_per_main", 0), previous), editing=False)
        await show_preview(message, state)
    else:
        await state.set_state(QuantityStates.reply_count)
        await message.answer("◈ Новый заказ · ответы\n\nСколько ответов разместить под каждым публично подтверждённым главным комментарием? От 0 до 20.\nНапример: 20 главных × 2 ответа = 40 ответов на видео.\n0 — только главные комментарии.", reply_markup=CANCEL)


@router.message(StateFilter(QuantityStates.reply_count))
async def input_reply_count(message, state):
    raw = (message.text or "").strip()
    if not raw.isdecimal() or not 0 <= int(raw) <= 20:
        await message.answer("Введите целое число от 0 до 20.")
        return
    data = await state.get_data()
    count = int(raw)
    await state.update_data(publication_plan=make_plan(data.get("main_count") or data["publication_plan"]["main_count"], count,
                            data.get("publication_plan")),
                            editing=False, idempotency_key=uuid4().hex)
    if count and not data.get("replies"):
        await state.set_state(QuantityStates.replies)
        await message.answer("◈ Новый заказ · тексты ответов\n\nПришлите варианты ответов, каждый с новой строки.\nИх количество не меняет число ответов под каждым главным.", reply_markup=CANCEL)
    else:
        await show_preview(message, state)


@router.message(StateFilter(QuantityStates.replies))
async def input_replies(message, state):
    from config.settings import get_settings
    try:
        texts = parse_texts(message.text, limit=20, max_length=get_settings().MAX_REPLY_LENGTH)
    except ValueError as exc:
        await message.answer(str(exc))
        return
    await state.update_data(replies=texts, editing=False, idempotency_key=uuid4().hex)
    await show_preview(message, state)


@router.callback_query(F.data.startswith("qo:edit:"), StateFilter(QuantityStates.preview))
async def edit_plan(callback, state):
    await callback.answer()
    target = callback.data.rsplit(":", 1)[1]
    fields = {"main": (QuantityStates.main_count, "Сколько главных комментариев на видео? От 1 до 500."),
              "reply": (QuantityStates.reply_count, "Сколько ответов под каждым главным? От 0 до 20."),
              "texts": (QuantityStates.texts, "Новые тексты главных комментариев, каждый с новой строки."),
              "replies": (QuantityStates.replies, "Новые тексты ответов, каждый с новой строки.")}
    selected, prompt = fields[target]
    await state.update_data(editing=True)
    await state.set_state(selected)
    await callback.message.edit_text(prompt, reply_markup=CANCEL)


@router.callback_query(F.data == "qo:pool", StateFilter(QuantityStates.preview))
async def toggle_pool(callback, state):
    await callback.answer()
    data = await state.get_data()
    await state.update_data(pool="shared" if data.get("pool") == "personal" else "personal", idempotency_key=uuid4().hex)
    await show_preview(callback.message, state, edit=True)


@router.callback_query(F.data == "qo:limits", StateFilter(QuantityStates.preview))
async def edit_limits(callback, state):
    await callback.answer()
    await state.set_state(QuantityStates.limits)
    await callback.message.edit_text("Введите три числа через пробел: лимит отправок на видео, время в минутах и лимит трафика задания в МБ. Например: 1000 120 500. Для трафика 0 означает без лимита.", reply_markup=CANCEL)


@router.message(StateFilter(QuantityStates.limits))
async def input_limits(message, state):
    data = await state.get_data()
    try:
        sends, minutes, mb = map(int, (message.text or "").split())
        plan = QuantityPlan.model_validate({**data["publication_plan"], "max_dispatches": sends,
                                           "max_seconds": minutes * 60, "traffic_limit_mb": mb or None})
    except (ValueError, TypeError):
        await message.answer("Проверьте значения. Отправок — до 20000 и не меньше числа публикаций. Время — от 1 до 360 минут. Трафик — от 0 до 100000 МБ.")
        return
    await state.update_data(publication_plan=plan.model_dump(), idempotency_key=uuid4().hex)
    await show_preview(message, state)


@router.callback_query(F.data == "qo:speed", StateFilter(QuantityStates.preview))
async def edit_speed(callback, state):
    await callback.answer()
    data = await state.get_data()
    await load_preview_runtime(state, data, refresh=True)
    runtime, slots = draft_runtime(data)
    if slots is None:
        senders, readers = runtime.send_workers, runtime.check_workers
        presets = [1]
        selection_text = "Выберите одну отправку или введите своё число. Исполнитель применит свой лимит."
    else:
        max_senders, _ = publication_capacity(slots, runtime.check_workers)
        senders, readers = runtime.limits(slots)
        presets = sorted({1, (max_senders + 1) // 2, max_senders})
        selection_text = f"Лимит отправки при текущем числе проверок · {max_senders}. Выберите вариант или введите число."
    text = ["<b>Потоки и скорость заказа</b>", "", *speed_lines(data),
            f"Интервал запуска отправок · {runtime.send_interval:g} с",
            f"Пауза перед первой проверкой · {runtime.check_delay:g} с",
            f"Обновление карточки · {runtime.live_interval:g} с", "",
            selection_text,
            "Для полной настройки отправьте пять чисел через пробел. По порядку идут отправки, проверки, "
            "интервал отправок, пауза перед проверкой и интервал карточки.",
            f"<code>{senders} {readers} {runtime.send_interval:g} "
            f"{runtime.check_delay:g} {runtime.live_interval:g}</code>"]
    await state.set_state(QuantityStates.speed)
    await callback.message.edit_text("\n".join(text), parse_mode="HTML", reply_markup=keyboard(
        [(str(value), f"qo:speed:{value}") for value in presets],
        [("По умолчанию", "qo:speed:reset"), ("Назад", "qo:speed:back")]))


@router.callback_query(F.data.regexp(r"^qo:speed:(?:[0-9]{1,2}|reset|back)$"), StateFilter(QuantityStates.speed))
async def choose_speed(callback, state):
    choice = callback.data.rsplit(":", 1)[1]
    if choice == "reset":
        await state.update_data(publication_options={}, idempotency_key=uuid4().hex)
    elif choice != "back":
        data = await state.get_data()
        runtime, slots = draft_runtime(data)
        limit = publication_capacity(slots, runtime.check_workers)[0] if slots is not None else MAX_SEND_WORKERS
        senders = int(choice)
        if not 1 <= senders <= limit:
            await callback.answer(f"Допустимое число отправок при текущем числе проверок · {limit}.", show_alert=True)
            return
        options = {**(data.get(ORDER_OPTIONS_KEY) or {}), "send_workers": senders}
        await state.update_data(publication_options=options, idempotency_key=uuid4().hex)
    await callback.answer()
    await show_preview(callback.message, state, edit=True)


@router.message(StateFilter(QuantityStates.speed), F.text)
async def input_speed(message, state):
    data = await state.get_data()
    runtime, slots = draft_runtime(data)
    values = message.text.replace(",", ".").split()
    try:
        options = dict(data.get(ORDER_OPTIONS_KEY) or {})
        if len(values) == 1:
            options["send_workers"] = int(values[0])
        elif len(values) == 5:
            options.update(send_workers=int(values[0]), check_workers=int(values[1]), send_interval=float(values[2]),
                           check_delay=float(values[3]), live_interval=float(values[4]))
        else:
            raise ValueError("Expected one or five values")
        options = OrderPublicationOptions.model_validate(options).model_dump(exclude_none=True)
    except ValueError:
        await message.answer("Введите число отправок или пять чисел через пробел. Интервал отправок 0–30 с, "
                             "пауза проверки 0–60 с, обновление карточки 2–30 с.")
        return
    if slots is not None:
        requested_checks = options.get("check_workers", runtime.check_workers)
        max_senders, readers = publication_capacity(slots, requested_checks)
        if len(values) == 5 and requested_checks > readers:
            await message.answer(f"Лимит проверок на заказ · {readers}.")
            return
        if options["send_workers"] > max_senders:
            await message.answer(f"Лимит отправки при таком числе проверок · {max_senders}.")
            return
    await state.update_data(publication_options=options, idempotency_key=uuid4().hex)
    await show_preview(message, state)


def order_settings(data):
    settings = dict(data.get("template_settings") or {})
    settings.update(publication_plan=data["publication_plan"], account_pool_mode=data.get("pool", "shared"),
                    template_id=data.get("template_id"), template_name=data.get("template_name"),
                    origin="quantity_order", replies=data.get("replies") or [])
    settings.pop(ORDER_OPTIONS_KEY, None)
    if data.get(ORDER_OPTIONS_KEY):
        settings[ORDER_OPTIONS_KEY] = OrderPublicationOptions.model_validate(data[ORDER_OPTIONS_KEY]).model_dump(exclude_none=True)
    return settings


@router.callback_query(F.data == "qo:create", StateFilter(QuantityStates.preview))
async def create_order(callback, state):
    await callback.answer()
    data = await state.get_data()
    if not data.get("links") or not data.get("texts"):
        await callback.message.answer("Заполните ссылки и тексты заказа.")
        return
    from bot.services.mention_choice import ensure_mention_choice
    user_id = await get_user_backend_id(callback.from_user.id)
    choice = await ensure_mention_choice(callback.message, state,
        [*data["texts"], *(data.get("replies") or [])], user_id=user_id, resume="qo:create",
        scope={key: data.get(key) for key in ("links", "publication_plan", ORDER_OPTIONS_KEY, "pool", "idempotency_key")})
    if choice is None:
        return
    settings = order_settings(data)
    settings["mention_mode"] = choice.mode
    try:
        task = await api_client.create_task(user_id=user_id, main_comment=data["texts"][0],
            text_variants=data["texts"], video_urls=data["links"], replies=data.get("replies") or [],
            settings=settings, idempotency_key=data["idempotency_key"])
    except APIRequestError as exc:
        await callback.message.answer(escape(str(exc)[:1000]) + "\nДанные сохранены. Можно повторить создание с той же кнопки.", parse_mode="HTML")
        return
    await state.clear()
    from bot.services.drafts import reset_draft
    reset_draft(callback.from_user.id)
    task_id = int(task.get("task_id") or task.get("id"))
    await callback.message.edit_text(f"Заказ #{task_id} создан. {len(data['links'])} видео, по {data['publication_plan']['main_count']} главных комментариев и {data['publication_plan']['replies_per_main']} ответов под каждым.",
        reply_markup=keyboard([("🔴 Ход выполнения", f"order:detail:{task_id}:live:0:all:0")], [("Мои заказы", "menu:orders")]))
    try:
        from bot.handlers.orders import _build_order_detail_keyboard
        from bot.services.live_orders import live_order_views, render_live
        task_view, activity = await asyncio.gather(api_client.get_task(task_id, user_id),
                                                   api_client.get_publication_activity(task_id, user_id))
        initial = render_live(activity)
        def renderer(current):
            view = {**task_view, "status": current["status"], "progress": current["progress"]}
            return render_live(current), _build_order_detail_keyboard(view, "live", 0, "all")
        await callback.message.edit_text(initial, reply_markup=renderer(activity)[1], parse_mode="HTML")
        await live_order_views.watch(callback.message,
            lambda: api_client.get_publication_activity(task_id, user_id), renderer, initial)
    except Exception:
        logger.debug("Created order live card unavailable for task %s", task_id, exc_info=True)


@router.callback_query(F.data == "qo:save", StateFilter(QuantityStates.preview))
async def save_template_prompt(callback, state):
    await callback.answer()
    await state.set_state(QuantityStates.name)
    await callback.message.edit_text("Введите название шаблона. Сохранятся тексты, количества публикаций, лимиты и выбранная скорость.", reply_markup=CANCEL)


@router.message(StateFilter(QuantityStates.name))
async def save_template(message, state):
    name = (message.text or "").strip()
    if not name or len(name) > 100:
        await message.answer("Название — от 1 до 100 символов.")
        return
    data = await state.get_data()
    template = await api_client.create_task_template(user_id=await get_user_backend_id(message.from_user.id),
        name=name, main_comment=data["texts"][0], text_variants=data["texts"], replies=data.get("replies") or [],
        settings=order_settings(data))
    await state.update_data(template_id=template["id"], template_name=name)
    await message.answer("Шаблон сохранён.")
    await show_preview(message, state)


@router.callback_query(F.data.startswith("template:counts:"))
async def template_counts_prompt(callback, state):
    await callback.answer()
    template_id = int(callback.data.rsplit(":", 1)[1])
    await api_client.get_task_template(template_id, await get_user_backend_id(callback.from_user.id))
    await state.update_data(edit_quantity_template_id=template_id)
    await state.set_state(QuantityStates.template_counts)
    await callback.message.edit_text("Введите два числа через пробел: главных комментариев на видео и ответов под каждым. Например: 60 2. Ответов может быть 0.", reply_markup=CANCEL)


@router.message(StateFilter(QuantityStates.template_counts))
async def update_template_counts(message, state):
    data = await state.get_data()
    try:
        main, replies = map(int, (message.text or "").split())
        make_plan(main, replies)
    except (ValueError, TypeError):
        await message.answer("Главных комментариев — от 1 до 500, ответов под каждым — от 0 до 20.")
        return
    user_id = await get_user_backend_id(message.from_user.id)
    template_id = data["edit_quantity_template_id"]
    template = await api_client.get_task_template(template_id, user_id)
    if replies and not template_replies(template):
        await message.answer("Сначала добавьте тексты ответов в шаблон или укажите 0 ответов.")
        return
    settings = dict(template.get("settings") or {})
    settings["publication_plan"] = make_plan(main, replies, settings.get("publication_plan"))
    await api_client.update_task_template(template_id, user_id, settings=settings)
    await state.clear()
    await message.answer("Количество публикаций в шаблоне обновлено.",
        reply_markup=keyboard([("Открыть шаблон", f"template:detail:{template_id}")]))


@router.callback_query(F.data.startswith("template:edit_comment:"))
async def template_texts_prompt(callback, state):
    await callback.answer()
    template_id = int(callback.data.rsplit(":", 1)[1])
    await api_client.get_task_template(template_id, await get_user_backend_id(callback.from_user.id))
    await state.update_data(edit_quantity_template_id=template_id)
    await state.set_state(QuantityStates.template_texts)
    await callback.message.edit_text("Пришлите новый набор главных текстов. Каждый вариант — с новой строки, от 1 до 50 вариантов.", reply_markup=CANCEL)


@router.message(StateFilter(QuantityStates.template_texts))
async def update_template_texts(message, state):
    from config.settings import get_settings
    try:
        texts = parse_texts(message.text, limit=50, max_length=get_settings().MAX_COMMENT_LENGTH)
    except ValueError as exc:
        await message.answer(str(exc))
        return
    data = await state.get_data()
    user_id = await get_user_backend_id(message.from_user.id)
    template_id = data["edit_quantity_template_id"]
    template = await api_client.get_task_template(template_id, user_id)
    settings = {**(template.get("settings") or {}), "text_variants": texts}
    await api_client.update_task_template(template_id, user_id, main_comment=texts[0], settings=settings)
    await state.clear()
    await message.answer("Тексты обновлены. Количество публикаций в шаблоне сохранено.",
        reply_markup=keyboard([("Открыть шаблон", f"template:detail:{template_id}")]))


@router.callback_query(F.data.startswith("order:texts:"))
async def pending_texts_prompt(callback, state):
    await callback.answer()
    task_id = int(callback.data.rsplit(":", 1)[1])
    task = await api_client.get_task(task_id, await get_user_backend_id(callback.from_user.id))
    if task.get("status") != "pending":
        await callback.message.answer("Тексты можно менять у заказа в очереди.")
        return
    await state.update_data(quantity_replace_task_id=task_id,
                            quantity_replace_legacy=not bool((task.get("settings") or {}).get("publication_plan")))
    await state.set_state(QuantityStates.pending_texts)
    await callback.message.edit_text("Пришлите новые главные тексты, каждый вариант с новой строки. Количество публикаций останется прежним.", reply_markup=CANCEL)


@router.message(StateFilter(QuantityStates.pending_texts))
async def replace_pending_texts(message, state):
    from config.settings import get_settings
    data = await state.get_data()
    try:
        texts = parse_texts(message.text, limit=50, max_length=get_settings().MAX_COMMENT_LENGTH)
        if data.get("quantity_replace_legacy") and len(texts) < 3:
            raise ValueError("Для сохранённого заказа старого формата нужны три варианта текста.")
    except ValueError as exc:
        await message.answer(str(exc))
        return
    task_id = data["quantity_replace_task_id"]
    await api_client.update_task_texts(task_id, texts, await get_user_backend_id(message.from_user.id))
    await state.clear()
    await message.answer("Тексты заказа обновлены.",
        reply_markup=keyboard([("Открыть заказ", f"order:detail:{task_id}:overview:0:all:0")]))


@router.callback_query(F.data.startswith("qo:"))
async def stale_composer(callback):
    await callback.answer("Этот редактор устарел. Откройте новый заказ через меню.", show_alert=True)
