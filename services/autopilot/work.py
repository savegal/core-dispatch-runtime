from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from db.models import CreatorSource, CreatorVideo, MonitorOutbox, Task
from services.autopilot.cache import cached_with_ttl
from services.autopilot.report import (
    TERMINAL_STATUSES,
    VISIBLE_ORIGINS,
    decimal_to_float,
    human_error_reason,
    money,
    task_card_payload,
    task_publication_counts,
    task_settings,
    task_video_urls,
)


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def _page_payload(items: list[dict[str, Any]], *, page: int, limit: int) -> dict[str, Any]:
    safe_limit = max(1, min(int(limit or 10), 20))
    total = len(items)
    pages = max(1, (total + safe_limit - 1) // safe_limit)
    safe_page = max(0, min(int(page or 0), pages - 1))
    return {
        "items": items[safe_page * safe_limit : safe_page * safe_limit + safe_limit],
        "total": total,
        "page": safe_page,
        "pages": pages,
        "limit": safe_limit,
    }


def _stage_for_task(task: Task, counts: dict[str, int | None] | None = None) -> str:
    status = str(task.status or "").lower()
    if status == "pending":
        return "Ждёт очереди executor"
    if status == "in_progress":
        counts = counts or task_publication_counts(task)
        mains = counts.get("mains_published")
        mains_expected = counts.get("mains_expected")
        replies = counts.get("replies_published")
        replies_expected = counts.get("replies_expected")
        if mains is None and replies is None:
            progress = int(task.progress or 0)
            total = len(task_video_urls(task)) or 1
            return f"Обрабатывает видео {min(progress, total)}/{total}"
        if mains_expected is not None and int(mains or 0) < int(mains_expected or 0):
            return "Публикует главные комментарии"
        if replies_expected is not None and int(replies or 0) < int(replies_expected or 0):
            return "Публикует ответы"
        return "Финализирует результат"
    if status in TERMINAL_STATUSES:
        return "Завершён"
    return "Состояние уточняется"


def _task_work_payload(task: Task) -> dict[str, Any]:
    payload = task_card_payload(task)
    counts = task_publication_counts(task)
    payload["progress_percent"] = int(task.progress or 0)
    payload["stage"] = _stage_for_task(task, counts)
    payload["video_count"] = len(task_video_urls(task)) or (1 if payload.get("url") else 0)
    payload["updated_at"] = _iso(task.updated_at)
    payload["created_at"] = _iso(task.created_at)
    return payload


def _task_refund_preview(task: Task) -> float:
    return decimal_to_float(max(money(task.total_cost) - money(task.refunded_cost), money(0)))


def _planned_from_video(
    video: CreatorVideo,
    creator: CreatorSource,
    *,
    source: str,
    task: Task | None = None,
    outbox: MonitorOutbox | None = None,
) -> dict[str, Any]:
    return {
        "kind": "video",
        "video_id": int(video.id),
        "task_id": int(task.id) if task and task.id else (int(video.task_id) if video.task_id else None),
        "outbox_id": int(outbox.id) if outbox and outbox.id else None,
        "source": source,
        "status": str(video.status or ""),
        "creator_id": int(creator.id) if creator.id else None,
        "creator_username": str(creator.username or ""),
        "url": str(video.url or ""),
        "video_external_id": str(video.video_id or ""),
        "discovered_at": _iso(video.discovered_at),
        "created_at": _iso((task.created_at if task else None) or (outbox.created_at if outbox else None) or video.discovered_at),
        "label": "Заказ уже создан" if task else "Ждёт создания заказа",
        "can_cancel": True,
    }


def _planned_from_task_url(task: Task, url: str, index: int) -> dict[str, Any]:
    settings = task_settings(task)
    return {
        "kind": "task",
        "video_id": None,
        "task_id": int(task.id),
        "outbox_id": None,
        "source": "task",
        "status": str(task.status or ""),
        "creator_id": settings.get("creator_source_id"),
        "creator_username": str(settings.get("creator_username") or ""),
        "url": str(url or ""),
        "video_external_id": "",
        "discovered_at": None,
        "created_at": _iso(task.created_at),
        "label": f"Заказ уже создан · видео {index}",
        "can_cancel": True,
    }


@cached_with_ttl(5)
async def collect_planned_work(db: AsyncSession, user_id: int, page: int = 0, limit: int = 10) -> dict[str, Any]:
    items: list[dict[str, Any]] = []
    seen_video_ids: set[int] = set()

    task_rows = await db.execute(
        select(Task)
        .filter(
            Task.user_id == user_id,
            Task.settings["origin"].as_string().in_(sorted(VISIBLE_ORIGINS)),
            Task.status == "pending",
        )
        .order_by(Task.created_at.asc(), Task.id.asc())
        .limit(200)
    )
    pending_tasks = list(task_rows.scalars().all())
    task_map = {int(task.id): task for task in pending_tasks}
    task_ids = list(task_map)
    videos_by_task: dict[int, list[tuple[CreatorVideo, CreatorSource]]] = {}
    if task_ids:
        video_rows = await db.execute(
            select(CreatorVideo, CreatorSource)
            .join(CreatorSource, CreatorVideo.creator_id == CreatorSource.id)
            .filter(CreatorVideo.task_id.in_(task_ids), CreatorSource.user_id == user_id)
            .order_by(CreatorVideo.discovered_at.asc(), CreatorVideo.id.asc())
        )
        for video, creator in video_rows.all():
            if video.task_id:
                videos_by_task.setdefault(int(video.task_id), []).append((video, creator))

    for task in pending_tasks:
        linked = videos_by_task.get(int(task.id), [])
        if linked:
            for video, creator in linked:
                seen_video_ids.add(int(video.id))
                items.append(_planned_from_video(video, creator, source="task", task=task))
            continue
        for index, url in enumerate(task_video_urls(task), start=1):
            items.append(_planned_from_task_url(task, url, index))

    creator_rows = await db.execute(select(CreatorSource.id).filter(CreatorSource.user_id == user_id))
    creator_ids = [int(creator_id) for (creator_id,) in creator_rows.all()]
    if creator_ids:
        outbox_rows = await db.execute(
            select(MonitorOutbox, CreatorSource)
            .join(CreatorSource, CreatorSource.id == MonitorOutbox.creator_id)
            .filter(
                MonitorOutbox.creator_id.in_(creator_ids),
                MonitorOutbox.event_type == "autopilot_create",
                MonitorOutbox.status.in_(["pending", "failed", "processing"]),
            )
            .order_by(MonitorOutbox.available_at.asc().nulls_first(), MonitorOutbox.created_at.asc(), MonitorOutbox.id.asc())
            .limit(200)
        )
        outbox_pairs = list(outbox_rows.all())
        outbox_video_ids: list[int] = []
        for entry, _creator in outbox_pairs:
            payload = entry.payload if isinstance(entry.payload, dict) else {}
            for raw_id in payload.get("video_ids") or []:
                try:
                    video_id = int(raw_id)
                except (TypeError, ValueError):
                    continue
                if video_id not in seen_video_ids:
                    outbox_video_ids.append(video_id)
        video_map: dict[int, CreatorVideo] = {}
        if outbox_video_ids:
            videos = await db.execute(select(CreatorVideo).filter(CreatorVideo.id.in_(outbox_video_ids)))
            video_map = {int(video.id): video for video in videos.scalars().all()}
        for entry, creator in outbox_pairs:
            payload = entry.payload if isinstance(entry.payload, dict) else {}
            for raw_id in payload.get("video_ids") or []:
                try:
                    video_id = int(raw_id)
                except (TypeError, ValueError):
                    continue
                if video_id in seen_video_ids:
                    continue
                video = video_map.get(video_id)
                if not video or str(video.status or "") not in {"new", "queued"}:
                    continue
                seen_video_ids.add(video_id)
                items.append(_planned_from_video(video, creator, source="outbox", outbox=entry))

    payload = _page_payload(items, page=page, limit=limit)
    payload["tab"] = "plan"
    return payload


@cached_with_ttl(5)
async def collect_running_work(db: AsyncSession, user_id: int) -> dict[str, Any]:
    rows = await db.execute(
        select(Task)
        .filter(
            Task.user_id == user_id,
            Task.settings["origin"].as_string().in_(sorted(VISIBLE_ORIGINS)),
            Task.status == "in_progress",
        )
        .order_by(Task.updated_at.desc(), Task.created_at.asc(), Task.id.asc())
    )
    tasks = list(rows.scalars().all())
    return {
        "items": [_task_work_payload(task) for task in tasks],
        "total": len(tasks),
        "page": 0,
        "pages": 1,
        "limit": len(tasks) or 10,
        "tab": "run",
    }


@cached_with_ttl(15)
async def collect_processed_work(db: AsyncSession, user_id: int, page: int = 0, limit: int = 10) -> dict[str, Any]:
    safe_limit = max(1, min(int(limit or 10), 20))
    filters = [
        Task.user_id == user_id,
        Task.settings["origin"].as_string().in_(sorted(VISIBLE_ORIGINS)),
        Task.status.in_(sorted(TERMINAL_STATUSES)),
    ]
    total_result = await db.execute(select(func.count(Task.id)).filter(*filters))
    total = int(total_result.scalar_one() or 0)
    pages = max(1, (total + safe_limit - 1) // safe_limit)
    safe_page = max(0, min(int(page or 0), pages - 1))
    rows = await db.execute(
        select(Task)
        .filter(*filters)
        .order_by(Task.updated_at.desc(), Task.created_at.desc(), Task.id.desc())
        .offset(safe_page * safe_limit)
        .limit(safe_limit)
    )
    tasks = list(rows.scalars().all())
    items = [_task_work_payload(task) for task in tasks]
    for item in items:
        if item.get("error_reason") and not item.get("error_summary"):
            item["error_summary"] = human_error_reason(item.get("error_reason"))
    return {
        "items": items,
        "total": total,
        "page": safe_page,
        "pages": pages,
        "limit": safe_limit,
        "tab": "done",
    }


async def get_planned_video_preview(db: AsyncSession, user_id: int, video_id: int) -> dict[str, Any]:
    row = await db.execute(
        select(CreatorVideo, CreatorSource)
        .join(CreatorSource, CreatorVideo.creator_id == CreatorSource.id)
        .filter(CreatorVideo.id == int(video_id), CreatorSource.user_id == user_id)
    )
    result = row.first()
    if not result:
        return {}
    video, creator = result
    task = None
    if video.task_id:
        task_row = await db.execute(
            select(Task).filter(
                Task.id == int(video.task_id),
                Task.user_id == user_id,
                Task.settings["origin"].as_string().in_(sorted(VISIBLE_ORIGINS)),
            )
        )
        task = task_row.scalar_one_or_none()
    item = _planned_from_video(video, creator, source="task" if task else "outbox", task=task)
    if task:
        settings = task_settings(task)
        item.update(
            {
                "task_status": str(task.status or ""),
                "template_name": str(settings.get("template_name") or ""),
                "cost": decimal_to_float(task.total_cost),
                "refunded": decimal_to_float(task.refunded_cost),
                "refund_preview": _task_refund_preview(task),
            }
        )
    return item


async def get_planned_task_preview(db: AsyncSession, user_id: int, task_id: int) -> dict[str, Any]:
    row = await db.execute(
        select(Task).filter(
            Task.id == int(task_id),
            Task.user_id == user_id,
            Task.settings["origin"].as_string().in_(sorted(VISIBLE_ORIGINS)),
        )
    )
    task = row.scalar_one_or_none()
    if not task:
        return {}
    payload = _task_work_payload(task)
    payload["refund_preview"] = _task_refund_preview(task)
    payload["video_urls"] = task_video_urls(task)[:10]
    return payload


async def collect_work(db: AsyncSession, user_id: int, tab: str = "plan", page: int = 0, limit: int = 10) -> dict[str, Any]:
    normalized = str(tab or "plan").lower()
    if normalized in {"run", "running", "current"}:
        return await collect_running_work(db, user_id)
    if normalized in {"done", "processed"}:
        return await collect_processed_work(db, user_id, page=page, limit=limit)
    return await collect_planned_work(db, user_id, page=page, limit=limit)
