from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from db.models import Account, CreatorSource, CreatorVideo, MonitorOutbox, Task
from services.autopilot.cache import cached_with_ttl
from services.autopilot.models import (
    CreatorReportRow,
    FinanceSnapshot,
    SummarySnapshot,
    TaskCard,
    TaskListResult,
    TaskPreview,
)

VISIBLE_ORIGINS = {"autopilot", "manual_retry"}
REPORT_ORIGINS = {"autopilot", "autopilot_archived", "manual_retry"}
TERMINAL_STATUSES = {"done", "partial", "error", "stopped"}
STATUS_LABELS = {
    "pending": "В очереди",
    "in_progress": "В работе",
    "done": "Готово",
    "partial": "Частично",
    "error": "Ошибка",
    "stopped": "Остановлен",
}
ERROR_SUMMARIES = {
    "captcha": "Аккаунты получили captcha. Похоже на временное ограничение TikTok.",
    "captcha_challenge": "Аккаунты получили captcha. Похоже на временное ограничение TikTok.",
    "input_not_found": "TikTok не показал поле ввода комментария. Часто это временная проблема страницы.",
    "comment_list_not_open": "Не удалось открыть список комментариев, поэтому ответы не смогли найти главный комментарий.",
    "stealth_reject": "TikTok принял действие не полностью. Обычно это рейт-лимит или скрытая модерация.",
    "stealth_reject_persistent": "TikTok несколько раз отклонил публикацию. Обычно это рейт-лимит или скрытая модерация.",
    "no_main_published": "Главный комментарий не был опубликован, поэтому ответы не запускались.",
    "reply_mains_not_found": "Ответчики не нашли главные комментарии на странице. Возможно TikTok скрыл публикации или список комментариев не успел прогрузиться.",
    "no_main_found_by_reply_account": "Reply-аккаунт не нашёл главные комментарии на странице.",
    "reply_publication_unconfirmed": "Ответы отправлены в TikTok, но публичное подтверждение не получено.",
    "submitted_unconfirmed": "Действия отправлены в TikTok, но публичное подтверждение не получено.",
    "enrichment_failed": "Не удалось подготовить cookies аккаунта. Аккаунт нужно перевалидировать.",
    "critical_cookies_not_generated": "TikTok не вернул критичные cookies для аккаунта.",
    "dead_session": "Куки аккаунта умерли: TikTok показывает экран логина.",
    "visible_login_button": "Куки аккаунта умерли: TikTok показывает экран логина.",
    "parallel_main_incomplete": "Один или несколько главных слотов не смогли опубликовать комментарии.",
    "parallel_main_no_publications_after_two_batches": "Главные комментарии не публикуются после двух заходов. Похоже на блок TikTok, проблему видео или аккаунтов.",
    "in_progress": "Заказ завис в обработке. Обычно это значит, что воркер был остановлен или упал во время заказа.",
    "задача зависла": "Заказ завис в обработке. Обычно это значит, что воркер был остановлен или упал во время заказа.",
    "cancelled_by_autopilot_control": "Заказ отменён через Автокомментинг.",
    "stopped_by_autopilot_control": "Заказ остановлен через Автокомментинг.",
}


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def iso_or_none(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def decimal_to_float(value: Decimal | int | float | str | None) -> float:
    return float(Decimal(str(value or 0)))


def money(value: Decimal | int | float | str | None) -> Decimal:
    return Decimal(str(value or 0))


def task_settings(task: Task) -> dict[str, Any]:
    return task.settings if isinstance(task.settings, dict) else {}


def task_video_urls(task: Task) -> list[str]:
    raw_urls = task.video_urls or []
    if isinstance(raw_urls, str):
        raw_urls = [raw_urls]
    if not isinstance(raw_urls, list):
        return []
    return [str(url).strip() for url in raw_urls if str(url or "").strip()]


def _first_present(data: dict[str, Any], keys: tuple[str, ...]) -> Any:
    for key in keys:
        value = data.get(key)
        if value is not None and value != "":
            return value
    return None


def _safe_int(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def _is_unconfirmed_publication(item: dict[str, Any]) -> bool:
    return bool(
        item.get("submitted_unconfirmed")
        or item.get("visibility_status") == "submitted_unconfirmed"
        or item.get("core_status") == "submitted_unconfirmed"
        or item.get("status") == "submitted_unconfirmed"
    )


def _is_confirmed_publication(item: dict[str, Any], *, id_keys: tuple[str, ...]) -> bool:
    if _is_unconfirmed_publication(item):
        return False
    return bool(
        _first_present(item, id_keys)
        or item.get("success")
        or item.get("posted")
        or item.get("network_confirmed")
        or item.get("local_confirmed")
        or item.get("verified")
    )


def _publication_account(item: dict[str, Any], account_labels: dict[int, str] | None = None) -> str:
    direct = _first_present(item, ("account_username", "username", "account_label", "account"))
    if direct:
        return str(direct).lstrip("@")
    try:
        account_id = int(item.get("account_id"))
    except (TypeError, ValueError):
        return ""
    if account_labels and account_id in account_labels:
        return str(account_labels[account_id]).lstrip("@")
    return f"acc#{account_id}"


def _publication_row(
    item: dict[str, Any],
    *,
    kind: str,
    url: str,
    account_labels: dict[int, str] | None = None,
) -> dict[str, Any]:
    id_keys = ("reply_id", "comment_id") if kind == "reply" else ("comment_id",)
    unconfirmed = _is_unconfirmed_publication(item)
    confirmed = _is_confirmed_publication(item, id_keys=id_keys)
    return {
        "kind": kind,
        "status": "unconfirmed" if unconfirmed else ("confirmed" if confirmed else "unknown"),
        "confirmed": confirmed,
        "unconfirmed": unconfirmed,
        "comment_id": _first_present(item, id_keys),
        "parent_comment_id": item.get("parent_comment_id") or item.get("parent_id"),
        "account_id": item.get("account_id"),
        "account_username": _publication_account(item, account_labels),
        "text": str(item.get("text") or item.get("text_choice") or "")[:120],
        "url": url,
    }


def task_publications(task: Task, account_labels: dict[int, str] | None = None) -> dict[str, Any]:
    settings = task_settings(task)
    url_results = settings.get("url_results")
    mains: list[dict[str, Any]] = []
    replies: list[dict[str, Any]] = []
    if not isinstance(url_results, dict):
        url_results = {}

    for url, row in url_results.items():
        if not isinstance(row, dict):
            continue
        engagement = row.get("engagement") if isinstance(row.get("engagement"), dict) else {}
        main = engagement.get("main") if isinstance(engagement.get("main"), dict) else {}
        variants = main.get("variants_published") if isinstance(main.get("variants_published"), list) else []
        main_sources = [item for item in variants if isinstance(item, dict)]
        if not main_sources and (
            main.get("comment_id")
            or main.get("submitted_unconfirmed")
            or main.get("posted")
            or main.get("success")
            or main.get("network_confirmed")
            or main.get("local_confirmed")
        ):
            main_sources = [main]
        main_unconfirmed = _is_unconfirmed_publication(main)
        for item in main_sources:
            source = dict(item)
            if main_unconfirmed and not source.get("network_confirmed") and not source.get("verified"):
                source["submitted_unconfirmed"] = True
            publication = _publication_row(source, kind="main", url=str(url), account_labels=account_labels)
            if publication["confirmed"] or publication["unconfirmed"]:
                mains.append(publication)

        reply_data = engagement.get("replies") if isinstance(engagement.get("replies"), dict) else {}
        reply_items = reply_data.get("items") if isinstance(reply_data.get("items"), list) else []
        replies_before = len(replies)
        for item in reply_items:
            if not isinstance(item, dict):
                continue
            publication = _publication_row(item, kind="reply", url=str(url), account_labels=account_labels)
            if publication["confirmed"] or publication["unconfirmed"]:
                replies.append(publication)
        url_replies = replies[replies_before:]
        confirmed_seen = sum(1 for item in url_replies if item["confirmed"])
        unconfirmed_seen = sum(1 for item in url_replies if item["unconfirmed"])
        aggregate_confirmed = _safe_int(reply_data.get("ok"))
        aggregate_unconfirmed = _safe_int(reply_data.get("submitted_unconfirmed"))
        for _ in range(max(0, aggregate_confirmed - confirmed_seen)):
            replies.append(_publication_row({"success": True}, kind="reply", url=str(url), account_labels=account_labels))
        for _ in range(max(0, aggregate_unconfirmed - unconfirmed_seen)):
            replies.append(_publication_row({"submitted_unconfirmed": True}, kind="reply", url=str(url), account_labels=account_labels))

    mains_confirmed = sum(1 for item in mains if item["confirmed"])
    mains_unconfirmed = sum(1 for item in mains if item["unconfirmed"])
    replies_confirmed = sum(1 for item in replies if item["confirmed"])
    replies_unconfirmed = sum(1 for item in replies if item["unconfirmed"])
    status = str(task.status or "").lower()
    return {
        "mains": mains,
        "replies": replies,
        "summary": {
            "mains_confirmed": mains_confirmed,
            "mains_unconfirmed": mains_unconfirmed,
            "replies_confirmed": replies_confirmed,
            "replies_unconfirmed": replies_unconfirmed,
            "needs_review": bool(
                mains_unconfirmed
                or replies_unconfirmed
                or status in {"partial", "error"}
            ),
        },
    }


def task_creator_id(task: Task) -> int | None:
    try:
        return int(task_settings(task).get("creator_source_id"))
    except (TypeError, ValueError):
        return None


def period_cutoff(period: str) -> datetime | None:
    period = str(period or "24h").lower()
    now = now_utc()
    if period == "1h":
        return now - timedelta(hours=1)
    if period == "24h":
        return now - timedelta(hours=24)
    if period == "7d":
        return now - timedelta(days=7)
    if period == "30d":
        return now - timedelta(days=30)
    if period == "all":
        return None
    return now - timedelta(hours=24)


def period_label(period: str) -> str:
    return {
        "1h": "1 час",
        "24h": "24 часа",
        "7d": "7 дней",
        "30d": "30 дней",
        "all": "всё время",
    }.get(str(period or "24h"), "24 часа")


def origin_filter(include_archived: bool = False):
    expr = Task.settings["origin"].as_string()
    return expr.in_(sorted(REPORT_ORIGINS)) if include_archived else expr.in_(sorted(VISIBLE_ORIGINS))


def period_filters(user_id: int, period: str, *, include_archived: bool = False) -> list[Any]:
    filters = [Task.user_id == user_id, origin_filter(include_archived)]
    cutoff = period_cutoff(period)
    if cutoff is not None:
        filters.append(Task.created_at >= cutoff)
    return filters


def human_error_reason(reason: str | None) -> str:
    raw = str(reason or "").strip()
    if not raw:
        return ""
    lowered = raw.lower()
    for marker, text in ERROR_SUMMARIES.items():
        if marker in lowered:
            return text
    first_line = next((line.strip() for line in raw.splitlines() if line.strip()), raw)
    return first_line.replace("_", " ")[:160]


def _row_engagement_counts(row: dict[str, Any]) -> tuple[int, int, int | None, int | None]:
    engagement = row.get("engagement") if isinstance(row, dict) else None
    if not isinstance(engagement, dict):
        return 0, 0, None, None

    main = engagement.get("main") if isinstance(engagement.get("main"), dict) else {}
    variants = main.get("variants_published") if isinstance(main.get("variants_published"), list) else []
    main_count = 0
    if variants:
        main_count = sum(
            1
            for item in variants
            if isinstance(item, dict)
            and (item.get("comment_id") or item.get("submitted_unconfirmed") or item.get("network_confirmed") or item.get("local_confirmed"))
        )
    elif main.get("comment_id") or main.get("submitted_unconfirmed") or main.get("posted") or main.get("success"):
        main_count = 1

    replies = engagement.get("replies") if isinstance(engagement.get("replies"), dict) else {}
    reply_items = replies.get("items") if isinstance(replies.get("items"), list) else []
    reply_count = sum(
        1
        for item in reply_items
        if isinstance(item, dict) and (item.get("success") or item.get("submitted_unconfirmed") or item.get("reply_id"))
    )
    if not reply_items:
        reply_count = int(replies.get("ok") or replies.get("submitted") or 0)

    replies_expected = None
    try:
        replies_expected = int(replies.get("total") or replies.get("requested_variants") or 0)
    except (TypeError, ValueError):
        replies_expected = None
    try:
        mains_expected = int(main.get("expected") or main.get("expected_publications") or 0)
    except (TypeError, ValueError):
        mains_expected = 0
    if not mains_expected:
        mains_expected = None
    return main_count, reply_count, mains_expected, replies_expected


def task_publication_counts(task: Task) -> dict[str, int | None]:
    settings = task_settings(task)
    url_results = settings.get("url_results")
    if not isinstance(url_results, dict) or not url_results:
        return {
            "mains_published": None,
            "replies_published": None,
            "mains_expected": None,
            "replies_expected": None,
        }

    mains = replies = 0
    mains_expected = replies_expected = 0
    exact_mains_expected = True
    exact_replies_expected = True
    for row in url_results.values():
        if not isinstance(row, dict):
            continue
        row_mains, row_replies, row_mains_expected, row_replies_expected = _row_engagement_counts(row)
        mains += row_mains
        replies += row_replies
        if row_mains_expected is None:
            exact_mains_expected = False
        else:
            mains_expected += row_mains_expected
        if row_replies_expected is None:
            exact_replies_expected = False
        else:
            replies_expected += row_replies_expected
    return {
        "mains_published": mains,
        "replies_published": replies,
        "mains_expected": mains_expected if exact_mains_expected else None,
        "replies_expected": replies_expected if exact_replies_expected else None,
    }


def summary_publication_counts(tasks: list[Task]) -> tuple[int | None, int | None]:
    mains = replies = 0
    for task in tasks:
        counts = task_publication_counts(task)
        if counts["mains_published"] is None or counts["replies_published"] is None:
            return None, None
        mains += int(counts["mains_published"] or 0)
        replies += int(counts["replies_published"] or 0)
    return mains, replies


def task_preview(task: Task) -> dict[str, Any]:
    settings = task_settings(task)
    counts = task_publication_counts(task)
    publications = task_publications(task)
    summary = publications["summary"]
    return asdict(
        TaskPreview(
            task_id=int(task.id),
            creator_id=task_creator_id(task),
            creator_username=str(settings.get("creator_username") or ""),
            template_name=str(settings.get("template_name") or ""),
            status=str(task.status or ""),
            progress=int(task.progress or 0),
            cost=decimal_to_float(task.total_cost),
            mains_published=counts["mains_published"],
            mains_expected=counts["mains_expected"],
            replies_published=counts["replies_published"],
            replies_expected=counts["replies_expected"],
            mains_confirmed=int(summary["mains_confirmed"]),
            mains_unconfirmed=int(summary["mains_unconfirmed"]),
            replies_confirmed=int(summary["replies_confirmed"]),
            replies_unconfirmed=int(summary["replies_unconfirmed"]),
            needs_review=bool(summary["needs_review"]),
            created_at=task.created_at,
            updated_at=task.updated_at,
            video_urls=task_video_urls(task)[:3],
        )
    )


def task_card_payload(
    task: Task,
    *,
    linked_video: dict[str, Any] | None = None,
    account_labels: dict[int, str] | None = None,
) -> dict[str, Any]:
    settings = task_settings(task)
    counts = task_publication_counts(task)
    publications = task_publications(task, account_labels=account_labels)
    publication_summary = publications["summary"]
    created_at = task.created_at
    updated_at = task.updated_at
    duration_seconds = None
    if created_at and updated_at and str(task.status or "").lower() in TERMINAL_STATUSES:
        duration_seconds = max(0, int((updated_at - created_at).total_seconds()))
    cost = money(task.total_cost)
    refunded = money(task.refunded_cost)
    urls = task_video_urls(task)
    url = str((linked_video or {}).get("url") or (urls[0] if urls else ""))
    creator_id = task_creator_id(task)
    creator_username = str(settings.get("creator_username") or (linked_video or {}).get("creator_username") or "")
    return asdict(
        TaskCard(
            task_id=int(task.id),
            status=str(task.status or ""),
            creator_id=creator_id,
            creator_username=creator_username,
            template_name=str(settings.get("template_name") or ""),
            url=url,
            created_at=created_at,
            updated_at=updated_at,
            duration_seconds=duration_seconds,
            mains_published=counts["mains_published"],
            mains_expected=counts["mains_expected"],
            replies_published=counts["replies_published"],
            replies_expected=counts["replies_expected"],
            mains_confirmed=int(publication_summary["mains_confirmed"]),
            mains_unconfirmed=int(publication_summary["mains_unconfirmed"]),
            replies_confirmed=int(publication_summary["replies_confirmed"]),
            replies_unconfirmed=int(publication_summary["replies_unconfirmed"]),
            needs_review=bool(publication_summary["needs_review"]),
            publications={"mains": publications["mains"], "replies": publications["replies"]},
            cost=decimal_to_float(cost),
            refunded=decimal_to_float(refunded),
            net_cost=decimal_to_float(max(cost - refunded, Decimal("0.00"))),
            error_reason=str(task.error_reason or ""),
            error_summary=human_error_reason(task.error_reason),
        )
    )


async def _load_linked_videos(db: AsyncSession, user_id: int, task_ids: list[int]) -> dict[int, dict[str, Any]]:
    if not task_ids:
        return {}
    result = await db.execute(
        select(CreatorVideo, CreatorSource.username)
        .join(CreatorSource, CreatorVideo.creator_id == CreatorSource.id)
        .filter(CreatorVideo.task_id.in_(task_ids), CreatorSource.user_id == user_id)
        .order_by(CreatorVideo.discovered_at.desc(), CreatorVideo.id.desc())
    )
    linked: dict[int, dict[str, Any]] = {}
    for video, username in result.all():
        if video.task_id and int(video.task_id) not in linked:
            linked[int(video.task_id)] = {
                "id": video.id,
                "creator_id": video.creator_id,
                "creator_username": username,
                "video_id": video.video_id,
                "url": video.url,
                "status": video.status,
                "discovered_at": iso_or_none(video.discovered_at),
            }
    return linked


def _publication_account_ids(task: Task) -> set[int]:
    ids: set[int] = set()
    for section in ("mains", "replies"):
        for item in task_publications(task).get(section, []):
            try:
                ids.add(int(item.get("account_id")))
            except (TypeError, ValueError):
                continue
    return ids


async def _load_account_labels(db: AsyncSession, account_ids: set[int]) -> dict[int, str]:
    if not account_ids:
        return {}
    result = await db.execute(select(Account.id, Account.label).filter(Account.id.in_(account_ids)))
    return {int(account_id): str(label or f"acc#{account_id}") for account_id, label in result.all()}


@cached_with_ttl(30)
async def collect_summary(db: AsyncSession, user_id: int, period: str = "24h") -> dict[str, Any]:
    filters = period_filters(user_id, period, include_archived=False)
    rows = await db.execute(select(Task).filter(*filters))
    tasks = list(rows.scalars().all())
    status_counts = Counter(str(task.status or "unknown") for task in tasks)
    charged = sum((money(task.total_cost) for task in tasks), Decimal("0.00"))
    refunded = sum((money(task.refunded_cost) for task in tasks), Decimal("0.00"))
    mains, replies = summary_publication_counts(tasks)

    creator_counter: dict[int, dict[str, Any]] = {}
    for task in tasks:
        creator_id = task_creator_id(task) or 0
        settings = task_settings(task)
        row = creator_counter.setdefault(
            creator_id,
            {
                "creator_id": creator_id,
                "username": str(settings.get("creator_username") or "unknown"),
                "total": 0,
                "done": 0,
                "partial": 0,
                "error": 0,
            },
        )
        row["total"] += 1
        status = str(task.status or "")
        if status in {"done", "partial", "error"}:
            row[status] += 1

    error_counter = Counter(str(task.error_reason or "").split(":", 1)[0] for task in tasks if task.error_reason)
    snapshot = SummarySnapshot(
        period=period,
        total=len(tasks),
        status_counts=dict(status_counts),
        published_mains=mains,
        published_replies=replies,
        cost=decimal_to_float(charged),
        refunded=decimal_to_float(refunded),
        net_cost=decimal_to_float(max(charged - refunded, Decimal("0.00"))),
    )
    payload = asdict(snapshot)
    payload["top_creators"] = sorted(creator_counter.values(), key=lambda row: row["total"], reverse=True)[:3]
    payload["top_errors"] = [{"reason": reason, "count": count} for reason, count in error_counter.most_common(3) if reason]
    return payload


@cached_with_ttl(15)
async def list_tasks(
    db: AsyncSession,
    user_id: int,
    period: str = "24h",
    *,
    status: str = "all",
    page: int = 0,
    limit: int = 5,
    creator_id: int | None = None,
) -> dict[str, Any]:
    filters = period_filters(user_id, period, include_archived=False)
    if status and status not in {"all", "review"}:
        filters.append(Task.status == status)
    if creator_id:
        filters.append(
            or_(
                Task.settings["creator_source_id"].as_integer() == int(creator_id),
                Task.settings["creator_source_id"].as_string() == str(creator_id),
            )
        )

    if status == "review":
        rows = await db.execute(select(Task).filter(*filters).order_by(Task.created_at.desc(), Task.id.desc()))
        all_tasks = list(rows.scalars().all())
        linked = await _load_linked_videos(db, user_id, [int(task.id) for task in all_tasks])
        all_items = [task_card_payload(task, linked_video=linked.get(int(task.id))) for task in all_tasks]
        review_items = [item for item in all_items if item.get("needs_review")]
        total = len(review_items)
        pages = max(1, (total + limit - 1) // limit)
        page = max(0, min(int(page or 0), pages - 1))
        payload = asdict(TaskListResult(items=[], total=total, page=page, pages=pages, limit=limit))
        payload["items"] = review_items[page * limit : page * limit + limit]
        payload["filters"] = {"status": status or "all", "creator_id": creator_id, "period": period}
        return payload

    total_result = await db.execute(select(func.count(Task.id)).filter(*filters))
    total = int(total_result.scalar_one() or 0)
    pages = max(1, (total + limit - 1) // limit)
    page = max(0, min(int(page or 0), pages - 1))
    rows = await db.execute(
        select(Task)
        .filter(*filters)
        .order_by(Task.created_at.desc(), Task.id.desc())
        .offset(page * limit)
        .limit(limit)
    )
    tasks = list(rows.scalars().all())
    linked = await _load_linked_videos(db, user_id, [int(task.id) for task in tasks])
    result = TaskListResult(
        items=[],
        total=total,
        page=page,
        pages=pages,
        limit=limit,
    )
    payload = asdict(result)
    payload["items"] = [task_card_payload(task, linked_video=linked.get(int(task.id))) for task in tasks]
    payload["filters"] = {"status": status or "all", "creator_id": creator_id, "period": period}
    return payload


@cached_with_ttl(15)
async def list_recent_tasks(db: AsyncSession, user_id: int, limit: int = 3) -> list[dict[str, Any]]:
    safe_limit = max(1, min(int(limit or 3), 5))
    rows = await db.execute(
        select(Task)
        .filter(
            Task.user_id == user_id,
            origin_filter(include_archived=False),
            Task.status.in_(sorted(TERMINAL_STATUSES)),
        )
        .order_by(Task.updated_at.desc(), Task.created_at.desc(), Task.id.desc())
        .limit(safe_limit)
    )
    tasks = list(rows.scalars().all())
    linked = await _load_linked_videos(db, user_id, [int(task.id) for task in tasks])
    return [task_card_payload(task, linked_video=linked.get(int(task.id))) for task in tasks]


async def get_task_card(db: AsyncSession, user_id: int, task_id: int) -> dict[str, Any] | None:
    row = await db.execute(
        select(Task).filter(
            Task.id == int(task_id),
            Task.user_id == user_id,
            Task.settings["origin"].as_string().in_(sorted(REPORT_ORIGINS)),
        )
    )
    task = row.scalar_one_or_none()
    if not task:
        return None
    linked = await _load_linked_videos(db, user_id, [int(task.id)])
    account_labels = await _load_account_labels(db, _publication_account_ids(task))
    payload = task_card_payload(task, linked_video=linked.get(int(task.id)), account_labels=account_labels)
    settings = task_settings(task)
    payload["origin"] = settings.get("origin") or "manual"
    origin = settings.get("origin")
    if origin == "autopilot":
        payload["origin_label"] = "Создан автоматически из мониторинга"
    elif origin == "manual_retry":
        payload["origin_label"] = f"Создан заново из заказа #{settings.get('retry_of_task_id')}"
    else:
        payload["origin_label"] = "Создан вручную"
    payload["settings"] = settings
    payload["status_label"] = STATUS_LABELS.get(payload["status"], payload["status"])
    return payload


@cached_with_ttl(30)
async def list_by_creators(
    db: AsyncSession,
    user_id: int,
    period: str = "24h",
    *,
    page: int = 0,
    limit: int = 5,
) -> dict[str, Any]:
    filters = period_filters(user_id, period, include_archived=False)
    rows = await db.execute(select(Task).filter(*filters).order_by(Task.created_at.desc(), Task.id.desc()))
    tasks = list(rows.scalars().all())
    creator_ids = {task_creator_id(task) for task in tasks if task_creator_id(task)}
    creators: dict[int, CreatorSource] = {}
    if creator_ids:
        creator_rows = await db.execute(select(CreatorSource).filter(CreatorSource.id.in_(creator_ids), CreatorSource.user_id == user_id))
        creators = {int(creator.id): creator for creator in creator_rows.scalars().all()}

    grouped: dict[int, list[Task]] = defaultdict(list)
    for task in tasks:
        grouped[task_creator_id(task) or 0].append(task)

    rows_payload: list[dict[str, Any]] = []
    for creator_id, creator_tasks in grouped.items():
        statuses = Counter(str(task.status or "unknown") for task in creator_tasks)
        charged = sum((money(task.total_cost) for task in creator_tasks), Decimal("0.00"))
        refunded = sum((money(task.refunded_cost) for task in creator_tasks), Decimal("0.00"))
        creator = creators.get(creator_id)
        last_task = max(creator_tasks, key=lambda item: item.created_at or datetime.min.replace(tzinfo=timezone.utc))
        fail_streak = 0
        for task in sorted(creator_tasks, key=lambda item: item.created_at or datetime.min.replace(tzinfo=timezone.utc), reverse=True):
            if str(task.status or "") == "error":
                fail_streak += 1
            else:
                break
        settings = task_settings(last_task)
        rows_payload.append(
            asdict(
                CreatorReportRow(
                    creator_id=creator_id,
                    username=(creator.username if creator else str(settings.get("creator_username") or "unknown")),
                    mode=str(creator.mode if creator else ""),
                    enabled=bool(creator.enabled) if creator else False,
                    total=len(creator_tasks),
                    done=int(statuses.get("done", 0)),
                    partial=int(statuses.get("partial", 0)),
                    error=int(statuses.get("error", 0)),
                    stopped=int(statuses.get("stopped", 0)),
                    cost=decimal_to_float(charged),
                    refunded=decimal_to_float(refunded),
                    last_at=last_task.created_at,
                    last_status=str(last_task.status or ""),
                    fail_streak=fail_streak,
                )
            )
        )

    rows_payload.sort(key=lambda row: row["total"], reverse=True)
    total = len(rows_payload)
    pages = max(1, (total + limit - 1) // limit)
    page = max(0, min(int(page or 0), pages - 1))
    return {
        "items": rows_payload[page * limit : page * limit + limit],
        "total": total,
        "page": page,
        "pages": pages,
        "limit": limit,
    }


@cached_with_ttl(60)
async def collect_finance(db: AsyncSession, user_id: int, period: str = "24h") -> dict[str, Any]:
    filters = period_filters(user_id, period, include_archived=False)
    rows = await db.execute(select(Task).filter(*filters).order_by(Task.created_at.desc()))
    tasks = list(rows.scalars().all())
    charged = sum((money(task.total_cost) for task in tasks), Decimal("0.00"))
    refunded = sum((money(task.refunded_cost) for task in tasks), Decimal("0.00"))
    mains, replies = summary_publication_counts(tasks)
    publication_count = None if mains is None or replies is None else mains + replies
    avg_publication_cost = None
    if publication_count:
        avg_publication_cost = decimal_to_float(max(charged - refunded, Decimal("0.00")) / Decimal(publication_count))

    daily: dict[str, dict[str, Any]] = defaultdict(lambda: {"charged": Decimal("0.00"), "refunded": Decimal("0.00"), "publications": 0})
    if period in {"7d", "30d", "all"}:
        for task in tasks:
            day = (task.created_at or now_utc()).strftime("%d.%m")
            daily[day]["charged"] += money(task.total_cost)
            daily[day]["refunded"] += money(task.refunded_cost)
            counts = task_publication_counts(task)
            if counts["mains_published"] is not None and counts["replies_published"] is not None:
                daily[day]["publications"] += int(counts["mains_published"] or 0) + int(counts["replies_published"] or 0)

    refund_reasons: dict[str, Decimal] = defaultdict(lambda: Decimal("0.00"))
    for task in tasks:
        refunded_amount = money(task.refunded_cost)
        if refunded_amount <= 0:
            continue
        reason = str(task.error_reason or "другие").split(":", 1)[0] or "другие"
        refund_reasons[reason] += refunded_amount

    snapshot = FinanceSnapshot(
        period=period,
        charged=decimal_to_float(charged),
        refunded=decimal_to_float(refunded),
        cost=decimal_to_float(max(charged - refunded, Decimal("0.00"))),
        task_count=len(tasks),
        publication_count=publication_count,
        avg_publication_cost=avg_publication_cost,
        daily=[
            {
                "day": day,
                "charged": decimal_to_float(row["charged"]),
                "refunded": decimal_to_float(row["refunded"]),
                "publications": int(row["publications"]),
            }
            for day, row in sorted(daily.items(), reverse=True)[:10]
        ],
        refund_reasons={reason: decimal_to_float(value) for reason, value in refund_reasons.items()},
    )
    return asdict(snapshot)


async def count_archive_candidates(db: AsyncSession, user_id: int, period: str = "24h") -> int:
    filters = period_filters(user_id, period, include_archived=False)
    filters.append(Task.status.in_(sorted(TERMINAL_STATUSES)))
    result = await db.execute(select(func.count(Task.id)).filter(*filters))
    return int(result.scalar_one() or 0)
