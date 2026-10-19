"""Helpers for turning raw technical errors into short user-facing messages."""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

from services.task_execution_ledger import build_task_execution_ledger, LEDGER_VERSION, visibility_checks_complete


_SUMMARY_RE = re.compile(r"успешно:\s*(\d+)\s*/\s*(\d+)", re.IGNORECASE)

_MESSAGES: dict[str, tuple[str, str]] = {
    "reply_visibility_unconfirmed": (
        "Видимость ответов не подтверждена",
        "Ответы отправлены, но проверка с других аккаунтов не подтвердила видимость всех ответов.",
    ),
    "main_visibility_unconfirmed": (
        "Видимость главных не подтверждена",
        "Не удалось подтвердить видимость всех главных комментариев. Ответы не запускались.",
    ),
    "captcha": (
        "Капча TikTok",
        "TikTok запросил капчу. Нужен другой аккаунт или пауза перед повтором.",
    ),
    "rate_limit": (
        "Лимит TikTok",
        "TikTok временно ограничил действия. Нужна пауза перед повтором.",
    ),
    "login_required": (
        "Нужен вход",
        "Аккаунт разлогинен или сессия истекла. Нужен повторный вход.",
    ),
    "account_banned": (
        "Аккаунт заблокирован",
        "Аккаунт заблокирован или ограничен TikTok. Нужен другой аккаунт.",
    ),
    "comment_disabled": (
        "Комментарии отключены",
        "У этого видео отключены комментарии.",
    ),
    "reply_target": (
        "Не найден родительский комментарий",
        "Не удалось найти нужный комментарий, под который надо отправить ответ.",
    ),
    "layout": (
        "Страница TikTok изменилась",
        "Не удалось надежно найти нужные элементы на странице TikTok. Нужен повтор или обновление селекторов.",
    ),
    "network": (
        "Проблема с сетью или прокси",
        "Ошибка сети, прокси или соединения. Обычно помогает повтор с другим ресурсом.",
    ),
    "video_unavailable": (
        "Видео недоступно",
        "Видео удалено, приватное или временно недоступно.",
    ),
    "post_not_confirmed": (
        "TikTok не подтвердил публикацию",
        "TikTok не подтвердил публикацию комментария или ответа. Нужен повтор.",
    ),
    "duplicate": (
        "Похоже на дубликат",
        "Похожий комментарий уже отправлялся под это видео. Лучше изменить текст.",
    ),
    "shadowban": (
        "Текст попал в тень",
        "Текст вероятно попал в тень. Лучше остановить заказ и сменить текст.",
    ),
    "task_timeout": (
        "Лимит времени заказа",
        "Заказ остановлен по лимиту 30 минут. Всё, что успело отправиться, сохранено в карточке.",
    ),
    "no_accounts": (
        "Нет готовых аккаунтов",
        "Сейчас нет живых аккаунтов для выполнения заказа.",
    ),
    "cooldown": (
        "Аккаунты на паузе",
        "Свободных аккаунтов сейчас нет: они в паузе после прошлых ошибок.",
    ),
    "no_text": (
        "Нет текста комментария",
        "Для заказа не передан корректный текст комментария.",
    ),
    "insufficient_funds": (
        "Недостаточно баланса",
        "Недостаточно средств для запуска заказа.",
    ),
    "stopped_by_user": (
        "Остановлено пользователем",
        "Заказ остановлен пользователем. Новые видео по этому заказу обрабатываться не будут.",
    ),
    "likes_failed": (
        "Лайки не запустились",
        "Комментарий опубликован, но лайки не удалось запустить.",
    ),
    "internal": (
        "Внутренняя ошибка",
        "Внутренняя ошибка сервиса. Нужен повтор или ручная проверка.",
    ),
}

_VIDEO_SUCCESS_STATUSES = {"success", "submitted_unconfirmed"}


def _clean_text(raw: Any) -> str:
    if raw is None:
        return ""
    text = str(raw).strip()
    if not text:
        return ""
    return re.sub(r"\s+", " ", text)


def _extract_first_error_line(text: str) -> str | None:
    if "ошибки:" not in text.lower():
        return None
    parts = re.split(r"ошибки:\s*", text, flags=re.IGNORECASE, maxsplit=1)
    if len(parts) < 2:
        return None
    remainder = parts[1]
    for line in remainder.splitlines():
        candidate = _clean_text(line)
        if candidate and not candidate.startswith("..."):
            return candidate
    candidate = _clean_text(remainder)
    return candidate or None


def _detect_error_code(text: str) -> str | None:
    lower = text.lower()
    if "reply_visibility_unconfirmed" in lower:
        return "reply_visibility_unconfirmed"
    if "main_visibility_unconfirmed" in lower:
        return "main_visibility_unconfirmed"

    if any(token in lower for token in ("boost_failed", "likes were not created", "лайки не удалось", "comment_author_username empty")):
        return "likes_failed"
    if any(token in lower for token in ("insufficient_funds", "недостаточно средств", "недостаточно баланса")):
        return "insufficient_funds"
    if any(token in lower for token in ("stopped_by_user", "stopped by user", "остановлено пользователем", "остановлен пользователем")):
        return "stopped_by_user"
    if any(token in lower for token in ("нет активных аккаунтов", "no active accounts", "no accounts available")):
        return "no_accounts"
    if any(token in lower for token in ("cooldown", "коулдаун", "в паузе")):
        return "cooldown"
    if "captcha" in lower:
        return "captcha"
    if any(token in lower for token in ("rate_limit", "too many requests", "429", "limit exceeded")):
        return "rate_limit"
    if any(token in lower for token in ("login_required", "login required", "session expired", "разлог")):
        return "login_required"
    if any(token in lower for token in ("account_banned", "blocked by tiktok", "banned", "suspended", "аккаунт заблок")):
        return "account_banned"
    if any(token in lower for token in ("comment_disabled", "comments are turned off", "комментарии отключены")):
        return "comment_disabled"
    if any(token in lower for token in ("reply_parent_not_found", "expected_parent", "родительский комментарий", "не найден комментарий")):
        return "reply_target"
    if any(
        token in lower
        for token in (
            "navigation_error",
            "layout_changed",
            "comment_input_not_found",
            "comment_button_not_found",
            "reply_button_not_found",
            "reply_input_not_found",
            "anchor_not_found",
            "selector",
            "page not loaded",
        )
    ):
        return "layout"
    if any(
        token in lower
        for token in (
            "timeout обработки задачи",
            "таймаут обработки задачи",
            "task processing timeout",
            "task_timeout",
            "task_timeout_30m",
            "task_processing_timeout",
        )
    ):
        return "task_timeout"
    if any(
        token in lower
        for token in (
            "proxy",
            "network_error",
            "connection refused",
            "connection error",
            "timed out",
            "timeout",
            "socks",
            "read error",
        )
    ):
        return "network"
    if any(token in lower for token in ("private", "deleted", "video not found", "not_found", "unavailable", "private_profile")):
        return "video_unavailable"
    if any(
        token in lower
        for token in (
            "comment_backend_error",
            "failed to post",
            "main_public_confirmation",
            "reply_publication_incomplete",
            "not_publicly_confirmed",
            "не подтвердил публикацию",
        )
    ):
        return "post_not_confirmed"
    if any(token in lower for token in ("duplicate_order", "duplicate_prevented", "дубликат")):
        return "duplicate"
    if any(token in lower for token in ("shadow", "текст в тени", "текст вероятно в тени")):
        return "shadowban"
    if any(token in lower for token in ("нет доступных текстов", "no text variants", "text_variants")):
        return "no_text"
    if any(token in lower for token in ("критическая ошибка", "critical error", "internal error")):
        return "internal"
    return None


def _fallback_message(text: str, short: bool) -> str:
    cleaned = text.replace("`", "").replace("*", "").replace("_", " ")
    if short and len(cleaned) > 80:
        return cleaned[:77] + "..."
    if not short and len(cleaned) > 220:
        return cleaned[:217] + "..."
    return cleaned


def humanize_error_text(raw: Any, short: bool = False) -> str | None:
    text = _clean_text(raw)
    if not text:
        return None

    summary_match = _SUMMARY_RE.search(text)
    if summary_match:
        success_count, total_count = summary_match.groups()
        prefix = (
            f"Обработано {success_count} из {total_count} видео."
            if not short
            else f"{success_count}/{total_count} видео"
        )
        primary_error = _extract_first_error_line(text)
        if primary_error:
            human_primary = humanize_error_text(primary_error, short=short)
            if human_primary:
                return human_primary if short else f"{prefix} Основная причина: {human_primary}"
        return prefix

    error_code = _detect_error_code(text)
    if error_code and error_code in _MESSAGES:
        short_message, long_message = _MESSAGES[error_code]
        return short_message if short else long_message

    return _fallback_message(text, short=short)


def humanize_warning(warning: Any, short: bool = False) -> str | None:
    if isinstance(warning, Mapping):
        code = _clean_text(warning.get("code"))
        message = _clean_text(warning.get("message"))
        if code == "boost_failed":
            return humanize_error_text("boost_failed", short=short)
        if code in {"main_visibility_unconfirmed", "reply_visibility_unconfirmed"}:
            return humanize_error_text(code, short=short)
        if code == "main_submitted_unconfirmed":
            if short:
                return "Комментарий отправлен, но пока не подтвержден"
            return "Комментарий отправлен, но независимая проверка пока не подтвердила его."
        if code == "reply_submitted_unconfirmed":
            if short:
                return "Ответы отправлены, но пока не подтверждены"
            return "Ответы отправлены, но независимая проверка пока не подтвердила их публичную видимость."
        if code == "main_variant_partial":
            if short:
                return "Часть вариантов комментария не отправилась"
            return "Часть вариантов главного комментария не удалось отправить. Остальные варианты продолжили выполнение."
        if code == "parallel_main_partial":
            submitted = _safe_int(warning.get("submitted"))
            expected = _safe_int(warning.get("expected"))
            if submitted and expected:
                if short:
                    return f"Главные комментарии: отправлено {submitted} из {expected}"
                return (
                    f"Главные комментарии: отправлено {submitted} из {expected}. "
                    "Система сохранила то, что удалось отправить."
                )
            if short:
                return "Часть главных комментариев не отправилась"
            return (
                "Часть главных комментариев не удалось отправить. "
                "Система сохранила то, что удалось отправить."
            )
        if code == "parallel_main_partial_replies_continued":
            submitted = _safe_int(warning.get("submitted"))
            expected = _safe_int(warning.get("expected"))
            if submitted and expected:
                if short:
                    return f"\u041e\u0442\u0432\u0435\u0442\u044b \u0437\u0430\u043f\u0443\u0449\u0435\u043d\u044b \u043f\u043e \u0433\u043b\u0430\u0432\u043d\u044b\u043c {submitted}/{expected}"
                return (
                    f"\u0413\u043b\u0430\u0432\u043d\u044b\u0435 \u043a\u043e\u043c\u043c\u0435\u043d\u0442\u0430\u0440\u0438\u0438 \u043e\u0442\u043f\u0440\u0430\u0432\u043b\u0435\u043d\u044b \u0447\u0430\u0441\u0442\u0438\u0447\u043d\u043e: {submitted} \u0438\u0437 {expected}. "
                    "\u041e\u0442\u0432\u0435\u0442\u044b \u043f\u0440\u043e\u0434\u043e\u043b\u0436\u0438\u043b\u0438 \u0440\u0430\u0431\u043e\u0442\u0443 \u043f\u043e \u043e\u0442\u043f\u0440\u0430\u0432\u043b\u0435\u043d\u043d\u044b\u043c \u0433\u043b\u0430\u0432\u043d\u044b\u043c."
                )
            if short:
                return "\u041e\u0442\u0432\u0435\u0442\u044b \u0437\u0430\u043f\u0443\u0449\u0435\u043d\u044b \u043f\u043e \u043e\u0442\u043f\u0440\u0430\u0432\u043b\u0435\u043d\u043d\u044b\u043c \u0433\u043b\u0430\u0432\u043d\u044b\u043c"
            return "\u041e\u0442\u0432\u0435\u0442\u044b \u043f\u0440\u043e\u0434\u043e\u043b\u0436\u0438\u043b\u0438 \u0440\u0430\u0431\u043e\u0442\u0443 \u043f\u043e \u0442\u0435\u043c \u0433\u043b\u0430\u0432\u043d\u044b\u043c \u043a\u043e\u043c\u043c\u0435\u043d\u0442\u0430\u0440\u0438\u044f\u043c, \u043a\u043e\u0442\u043e\u0440\u044b\u0435 \u0443\u0434\u0430\u043b\u043e\u0441\u044c \u043e\u0442\u043f\u0440\u0430\u0432\u0438\u0442\u044c."
        if code == "parallel_main_partial_legacy_unused":
            submitted = _safe_int(warning.get("submitted"))
            expected = _safe_int(warning.get("expected"))
            if submitted and expected:
                if short:
                    return f"Главные комментарии: отправлено {submitted} из {expected}"
                return (
                    f"Главные комментарии: отправлено {submitted} из {expected}. "
                    "Ответы продолжили работу по тем комментариям, которые удалось отправить."
                )
            if short:
                return "Часть главных комментариев не отправилась"
            return "Часть главных комментариев не удалось отправить. Ответы продолжили работу по опубликованным комментариям."
        if code in {"parallel_main_incomplete_replies_skipped", "parallel_main_incomplete_replies_blocked"}:
            submitted = _safe_int(warning.get("submitted"))
            expected = _safe_int(warning.get("expected"))
            if submitted and expected:
                if short:
                    return f"Ответы не запускались: главных {submitted}/{expected}"
                return (
                    f"Ответы не запускались: главных комментариев отправлено {submitted} из {expected}. "
                    "Это защищает reply-аккаунты от бессмысленного поиска неполного набора parent-комментариев."
                )
            if short:
                return "Ответы не запускались: главные отправлены не полностью"
            return "Ответы не запускались, потому что главные комментарии отправлены не полностью."
        if code == "engagement_parent_author_fallback":
            requested = _clean_text(warning.get("requested_author"))
            matched = _clean_text(warning.get("matched_author"))
            requested_label = f"@{requested.lstrip('@')}" if requested else "указанный автор"
            matched_label = f"@{matched.lstrip('@')}" if matched else "автор не распознан"
            if short:
                return f"{requested_label} не найден, ответили по точному тексту"
            return (
                f"Автор {requested_label} не найден. "
                f"Ответ отправлен под точно совпавший текст у {matched_label}."
            )
        return humanize_error_text(message or code, short=short)
    return humanize_error_text(warning, short=short)


def _extract_settings(task_or_settings: Mapping[str, Any] | None) -> Mapping[str, Any]:
    if not isinstance(task_or_settings, Mapping):
        return {}
    settings = task_or_settings.get("settings")
    if isinstance(settings, Mapping):
        return settings
    return task_or_settings


def collect_task_warnings(task_or_settings: Mapping[str, Any] | None, short: bool = False) -> list[str]:
    settings = _extract_settings(task_or_settings)
    url_results = settings.get("url_results", {})
    if not isinstance(url_results, Mapping):
        return []

    warnings: list[str] = []
    seen: set[str] = set()
    for result in url_results.values():
        if not isinstance(result, Mapping):
            continue
        for warning in result.get("warnings", []) or []:
            human_warning = humanize_warning(warning, short=short)
            if human_warning and human_warning not in seen:
                warnings.append(human_warning)
                seen.add(human_warning)
    return warnings


def _format_timeout_limit_phrase(settings: Mapping[str, Any]) -> str:
    try:
        seconds = max(1, int(settings.get("timeout_seconds") or 1800))
    except (TypeError, ValueError):
        seconds = 1800
    if seconds >= 60 and seconds % 60 == 0:
        return f"{seconds // 60} минут"
    if seconds >= 60:
        minutes = seconds // 60
        tail_seconds = seconds % 60
        return f"{minutes} мин {tail_seconds} сек"
    return f"{seconds} сек"


def _format_task_timeout_reason(settings: Mapping[str, Any], short: bool = False) -> str:
    limit = _format_timeout_limit_phrase(settings)
    if short:
        return f"Лимит времени: {limit}"
    return f"Заказ остановлен по лимиту {limit}. Все, что успело отправиться, сохранено в карточке."


def build_task_display_reason(task: Mapping[str, Any] | None, short: bool = False) -> str | None:
    if not isinstance(task, Mapping):
        return None

    settings = _extract_settings(task)
    url_results = settings.get("url_results", {})
    only_successful_results = False
    if isinstance(url_results, Mapping) and url_results:
        result_statuses = [
            _clean_text(result.get("status")).lower() in _VIDEO_SUCCESS_STATUSES and visibility_checks_complete(result)
            for result in url_results.values()
            if isinstance(result, Mapping)
        ]
        only_successful_results = bool(result_statuses) and all(result_statuses)

    status = _clean_text(task.get("status")).lower()
    error_code = _detect_error_code(_clean_text(task.get("error_reason")))
    if settings.get("timeout_stopped") or error_code == "task_timeout":
        return _format_task_timeout_reason(settings, short=short)

    if status == "stopped" or settings.get("timeout_stopped"):
        reason = humanize_error_text(task.get("error_reason"), short=short)
        if reason:
            return reason

    if not only_successful_results:
        reason = humanize_error_text(task.get("error_reason"), short=short)
        if reason:
            return reason

    if isinstance(url_results, Mapping):
        for result in url_results.values():
            if not isinstance(result, Mapping):
                continue
            if _clean_text(result.get("status")).lower() in _VIDEO_SUCCESS_STATUSES and visibility_checks_complete(result):
                continue
            human_reason = humanize_error_text(result.get("error"), short=short)
            if human_reason:
                return human_reason

    warnings = collect_task_warnings(task, short=short)
    if warnings:
        return warnings[0]
    return None


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _compress_method(methods: set[str]) -> str | None:
    normalized = {str(item).strip() for item in methods if item}
    if not normalized:
        return None
    if len(normalized) == 1:
        return next(iter(normalized))
    return "mixed"


def build_task_execution_summary(task: Mapping[str, Any] | None) -> dict[str, Any]:
    if not isinstance(task, Mapping):
        return {
            "display_status": "unknown",
            "status_label": "Неизвестно",
            "status_badge": "neutral",
            "display_reason": None,
            "warnings": [],
            "has_warnings": False,
            "videos": {"total": 0, "success": 0, "failed": 0, "with_warnings": 0, "pending": 0},
            "main": {},
            "replies": {},
            "likes": {},
            "ledger": {},
            "coverage": {},
            "health": {},
        }

    status = (_clean_text(task.get("status")) or "unknown").lower()
    video_urls = task.get("video_urls") or []
    total_urls = len(video_urls) if isinstance(video_urls, list) else 0
    settings = _extract_settings(task)
    url_results = settings.get("url_results", {})
    if not isinstance(url_results, Mapping):
        url_results = {}
    stored_ledger = settings.get("execution_ledger")
    if isinstance(stored_ledger, Mapping) and _safe_int(stored_ledger.get("version")) == LEDGER_VERSION:
        task_ledger = dict(stored_ledger)
    else:
        task_ledger = build_task_execution_ledger(url_results)

    warnings = collect_task_warnings(task, short=False)
    display_reason = build_task_display_reason(task, short=False)

    video_success = 0
    video_failed = 0
    video_with_warnings = 0

    main_methods: set[str] = set()
    main_comment_id = None
    main_verified = False
    main_posted = False
    main_verify_mode = None
    main_expected = 0
    main_submitted = 0
    main_verified_count = 0

    replies_ok = 0
    replies_total = 0
    replies_requested = 0
    replies_submitted = 0
    reply_attempted = 0
    reply_failed = 0
    reply_variant_outcomes = 0
    reply_variants_not_started = 0
    reply_parent_confirmed = 0
    reply_parent_ui_confirmed = 0
    reply_visibility_confirmed = 0
    reply_submitted_unconfirmed = 0
    reply_methods: set[str] = set()

    likes_enabled = False
    likes_requested = False
    likes_quantity = 0
    likes_success_urls = 0
    likes_failed_urls = 0
    likes_errors: list[str] = []
    likes_target_mode = None
    likes_orders_total = 0
    likes_orders_ok = 0
    likes_orders_failed = 0
    likes_targets: list[dict[str, Any]] = []

    for result in url_results.values():
        if not isinstance(result, Mapping):
            continue

        result_status = _clean_text(result.get("status"))
        if result_status in _VIDEO_SUCCESS_STATUSES and visibility_checks_complete(result):
            video_success += 1
        else:
            video_failed += 1

        result_warnings = result.get("warnings") or []
        if isinstance(result_warnings, list) and result_warnings:
            video_with_warnings += 1

        engagement = result.get("engagement", {})
        if not isinstance(engagement, Mapping):
            continue

        main = engagement.get("main", {})
        if isinstance(main, Mapping):
            method = _clean_text(main.get("method"))
            if method:
                main_methods.add(method)
            if not main_comment_id:
                main_comment_id = _clean_text(main.get("comment_id"))
            main_verified = main_verified or bool(main.get("verified"))
            main_posted = main_posted or bool(main.get("posted"))
            main_verify_mode = main_verify_mode or _clean_text(main.get("verify_mode"))
            expected = _safe_int(main.get("expected"))
            variants = main.get("variants_published") or []
            variant_count = len(variants) if isinstance(variants, list) else 0
            submitted_count = variant_count or (1 if main.get("posted") else 0)
            verified_count = 0
            if variant_count > 0:
                for variant in variants:
                    if not isinstance(variant, Mapping):
                        continue
                    if (bool(variant["public_confirmed"]) if "public_confirmed" in variant
                            else bool(variant.get("network_confirmed") or variant.get("local_confirmed"))):
                        verified_count += 1
            elif main.get("verified"):
                verified_count = submitted_count
            main_expected += expected if expected > 0 else submitted_count
            main_submitted += submitted_count
            main_verified_count += verified_count

        replies = engagement.get("replies", {})
        if isinstance(replies, Mapping):
            replies_ok += _safe_int(replies.get("ok"))
            replies_total += _safe_int(replies.get("total"))
            replies_requested += (
                _safe_int(replies.get("expected_submissions"))
                or _safe_int(replies.get("expected"))
                or _safe_int(replies.get("requested_variants"))
                or _safe_int(replies.get("total"))
            )
            reply_attempted += _safe_int(replies.get("attempted"))
            reply_failed += _safe_int(replies.get("failed"))
            reply_variant_outcomes += _safe_int(replies.get("variant_outcomes"))
            reply_variants_not_started += _safe_int(replies.get("variants_not_started"))
            explicit_submitted = _safe_int(replies.get("submitted"))
            aggregate_unconfirmed = _safe_int(replies.get("submitted_unconfirmed"))
            item_unconfirmed = 0
            for item in replies.get("items") or []:
                if not isinstance(item, Mapping):
                    continue
                reply_method = _clean_text(item.get("method"))
                if reply_method:
                    reply_methods.add(reply_method)
                if item.get("parent_confirmed"):
                    reply_parent_confirmed += 1
                elif _clean_text(item.get("parent_confirmation_level")) == "ui_author_only":
                    reply_parent_ui_confirmed += 1
                visibility_status = _clean_text(item.get("visibility_status"))
                if (bool(item["public_confirmed"]) if "public_confirmed" in item
                        else visibility_status in {"confirmed_local", "network_confirmed", "public_confirmed"}):
                    reply_visibility_confirmed += 1
                elif visibility_status == "submitted_unconfirmed" or item.get("submitted_unconfirmed"):
                    item_unconfirmed += 1
            unconfirmed_count = aggregate_unconfirmed or item_unconfirmed
            reply_submitted_unconfirmed += unconfirmed_count
            replies_submitted += explicit_submitted or (_safe_int(replies.get("ok")) + unconfirmed_count)

        likes = engagement.get("likes", {})
        if isinstance(likes, Mapping):
            likes_enabled = likes_enabled or bool(likes.get("enabled"))
            likes_requested = likes_requested or bool(likes.get("requested"))
            likes_quantity = max(likes_quantity, _safe_int(likes.get("quantity")))
            likes_target_mode = likes_target_mode or _clean_text(likes.get("target_mode"))
            if likes.get("requested"):
                if likes.get("ok"):
                    likes_success_urls += 1
                else:
                    likes_failed_urls += 1
                    error_text = humanize_error_text(likes.get("error"), short=False) or _clean_text(likes.get("error"))
                    if error_text:
                        likes_errors.append(error_text)
            like_orders = likes.get("orders") if isinstance(likes.get("orders"), list) else []
            for order in like_orders:
                if not isinstance(order, Mapping):
                    continue
                username = _clean_text(order.get("username")).lstrip("@")
                if not username:
                    continue
                ok = bool(order.get("ok"))
                likes_orders_total += 1
                if ok:
                    likes_orders_ok += 1
                else:
                    likes_orders_failed += 1
                likes_targets.append(
                    {
                        "username": username,
                        "quantity": _safe_int(order.get("quantity")),
                        "ok": ok,
                        "reply_count": _safe_int(order.get("reply_count")),
                        "parent_text": _clean_text(order.get("parent_text")),
                        "selection_reason": _clean_text(order.get("selection_reason")) or None,
                        "error": humanize_error_text(order.get("error"), short=True) or _clean_text(order.get("error")),
                    }
                )

    known_video_count = video_success + video_failed
    video_pending = max(0, total_urls - known_video_count)
    effective_status = status
    only_successful_video_results = known_video_count > 0 and video_failed == 0 and video_pending == 0
    if status in {"error", "partial"} and only_successful_video_results:
        effective_status = "done"

    if effective_status == "done" and warnings:
        display_status = "done_warning"
        status_label = "Выполнено, но есть предупреждения"
        status_badge = "warning"
    elif effective_status == "done":
        display_status = "done"
        status_label = "Выполнено"
        status_badge = "success"
    elif effective_status == "partial":
        display_status = "partial"
        status_label = "Частично выполнено"
        status_badge = "warning"
    elif effective_status == "stopped":
        display_status = "stopped"
        status_label = "Остановлено"
        status_badge = "neutral"
    elif effective_status == "error":
        display_status = "failed"
        status_label = "С ошибкой"
        status_badge = "error"
    elif effective_status == "in_progress":
        display_status = "in_progress"
        status_label = "Выполняется"
        status_badge = "progress"
    elif effective_status == "pending":
        display_status = "pending"
        status_label = "Ожидает"
        status_badge = "pending"
    else:
        display_status = effective_status
        status_label = status or "Неизвестно"
        status_badge = "neutral"

    return {
        "display_status": display_status,
        "status_label": status_label,
        "status_badge": status_badge,
        "display_reason": display_reason,
        "warnings": warnings,
        "has_warnings": bool(warnings),
        "videos": {
            "total": total_urls,
            "success": video_success,
            "failed": video_failed,
            "with_warnings": video_with_warnings,
            "pending": video_pending,
        },
        "main": {
            "posted": main_posted,
            "verified": main_verified,
            "method": _compress_method(main_methods),
            "comment_id": main_comment_id,
            "verify_mode": main_verify_mode,
            "expected": main_expected,
            "submitted": main_submitted,
            "verified_count": main_verified_count,
        },
        "replies": {
            "ok": replies_ok,
            "total": replies_total,
            "requested": replies_requested,
            "submitted": replies_submitted,
            "attempted": reply_attempted,
            "failed": reply_failed,
            "variant_outcomes": reply_variant_outcomes,
            "variants_not_started": reply_variants_not_started,
            "parent_confirmed": reply_parent_confirmed,
            "parent_ui_confirmed": reply_parent_ui_confirmed,
            "visibility_confirmed": reply_visibility_confirmed,
            "submitted_unconfirmed": reply_submitted_unconfirmed,
            "method": _compress_method(reply_methods),
        },
        "likes": {
            "enabled": likes_enabled,
            "requested": likes_requested,
            "quantity": likes_quantity,
            "successful_urls": likes_success_urls,
            "failed_urls": likes_failed_urls,
            "error": likes_errors[0] if likes_errors else None,
            "target_mode": likes_target_mode,
            "orders_total": likes_orders_total,
            "orders_ok": likes_orders_ok,
            "orders_failed": likes_orders_failed,
            "targets": likes_targets,
        },
        "ledger": task_ledger,
        "coverage": task_ledger.get("coverage", {}) if isinstance(task_ledger, Mapping) else {},
        "health": task_ledger.get("health_signals", {}) if isinstance(task_ledger, Mapping) else {},
    }


def build_video_status_summary(status: str | None, meta: Mapping[str, Any] | None) -> dict[str, Any]:
    normalized_status = _clean_text(status) or "unknown"
    reason = explain_video_status(normalized_status, meta, short=False)
    warning_messages = [humanize_warning(item, short=False) for item in ((meta or {}).get("warnings") or [])]
    warning_messages = [message for message in warning_messages if message]

    if normalized_status == "done" and warning_messages:
        display_status = "done_warning"
        status_label = "Выполнено с предупреждением"
        status_badge = "warning"
    elif normalized_status == "done":
        display_status = "done"
        status_label = "Выполнено"
        status_badge = "success"
    elif normalized_status == "failed":
        display_status = "failed"
        status_label = "Ошибка"
        status_badge = "error"
    elif normalized_status == "queued":
        display_status = "queued"
        status_label = "В очереди"
        status_badge = "progress"
    elif normalized_status == "skipped":
        display_status = "skipped_manual"
        status_label = "Пропущено вручную"
        status_badge = "neutral"
    elif normalized_status == "new":
        display_status = "new"
        status_label = "Ждет решения"
        status_badge = "pending"
    else:
        display_status = normalized_status
        status_label = normalized_status or "Неизвестно"
        status_badge = "neutral"

    return {
        "display_status": display_status,
        "status_label": status_label,
        "status_badge": status_badge,
        "display_reason": reason,
        "warnings": warning_messages,
        "has_warnings": bool(warning_messages),
    }


def explain_video_status(status: str | None, meta: Mapping[str, Any] | None, short: bool = False) -> str | None:
    meta = meta if isinstance(meta, Mapping) else {}
    last_error = meta.get("last_error")
    warning_messages = [humanize_warning(item, short=short) for item in (meta.get("warnings") or [])]
    warning_messages = [message for message in warning_messages if message]

    if status == "failed":
        return humanize_error_text(last_error, short=short) or (warning_messages[0] if warning_messages else "Ошибка обработки")
    if status == "queued":
        return "Видео поставлено в очередь." if not short else "В очереди"
    if status == "skipped":
        return "Видео пропущено вручную." if not short else "Пропущено вручную"
    if status == "done" and warning_messages:
        return warning_messages[0]
    if status == "done":
        return "Видео обработано." if not short else "Готово"
    if status == "new":
        return "Ожидает решения." if not short else "Ожидает"
    return humanize_error_text(last_error, short=short)
