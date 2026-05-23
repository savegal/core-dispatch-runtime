from __future__ import annotations

import asyncio
import copy
from dataclasses import asdict
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy import delete, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from config.settings import get_settings
from db.models import CreatorSource, CreatorVideo, MonitorOutbox, PendingEnqueue, Task, Transaction, User
from services.app_settings_service import COMMENTING_MODE_KEY, COMMENTING_MODE_OLD, normalize_commenting_mode
from services.autopilot.cache import clear_cache as clear_autopilot_cache
from services.autopilot.dashboard import collect_dashboard, get_app_setting
from services.autopilot.models import ActionResult
from services.autopilot.report import (
    TERMINAL_STATUSES,
    VISIBLE_ORIGINS,
    count_archive_candidates,
    money,
    period_filters,
    period_label,
    task_creator_id,
    task_settings,
    task_video_urls,
)
from services.balance_service import calculate_task_cost, get_pricing_async, get_user_balance_async
from services.notify_service import send_notification_to_user
from services.redis_service import get_redis_client
from services.task_service import finalize_reserved_enqueue_async, remove_tasks_from_queue, reserve_task_for_enqueue_async
from services.task_stop_signal import request_task_stop

settings = get_settings()


def _remaining_task_refund(task: Task) -> Decimal:
    return max(money(task.total_cost) - money(task.refunded_cost), Decimal("0.00"))


def _mark_video_released(video: CreatorVideo, *, task_id: int, now: datetime) -> None:
    meta = dict(video.meta or {}) if isinstance(video.meta, dict) else {}
    meta["autopilot_cancelled_at"] = now.isoformat()
    meta["autopilot_cancelled_task_id"] = task_id
    video.meta = meta
    video.status = "new"
    video.task_id = None


def _mark_video_skipped(video: CreatorVideo, *, task_id: int | None, now: datetime) -> None:
    meta = dict(video.meta or {}) if isinstance(video.meta, dict) else {}
    meta["autopilot_skipped_at"] = now.isoformat()
    meta["autopilot_skipped_by"] = "autopilot_control"
    if task_id is not None:
        meta["autopilot_skipped_task_id"] = int(task_id)
    video.meta = meta
    video.status = "skipped"
    video.task_id = None


async def _refund_pending_task(db: AsyncSession, owner: User, task: Task, *, now: datetime, reason: str) -> Decimal:
    refund = _remaining_task_refund(task)
    if refund <= 0:
        return Decimal("0.00")
    owner.balance = (owner.balance or Decimal("0.00")) + refund
    task.refunded_cost = money(task.refunded_cost) + refund
    db.add(
        Transaction(
            user_id=int(owner.id),
            task_id=task.id,
            amount=refund,
            type="refund",
            description=f"Возврат за {reason} #{task.id}",
        )
    )
    return refund


def _mark_task_stopped(task: Task, *, now: datetime, reason: str, force_close: bool = False) -> None:
    settings_dict = dict(task_settings(task))
    settings_dict["stopped_by_user"] = True
    settings_dict["stopped_at"] = now.isoformat()
    settings_dict["cancel_reason"] = reason
    if force_close:
        settings_dict["force_close_browsers"] = True
    task.status = "stopped"
    task.error_reason = "stopped_by_autopilot_control" if force_close else "cancelled_by_autopilot_control"
    task.settings = settings_dict
    task.updated_at = now


async def _cancel_outbox_video_references(
    db: AsyncSession,
    *,
    creator_id: int,
    video_id: int,
) -> int:
    rows = await db.execute(
        select(MonitorOutbox).filter(
            MonitorOutbox.creator_id == int(creator_id),
            MonitorOutbox.event_type == "autopilot_create",
            MonitorOutbox.status.in_(["pending", "failed", "processing"]),
        )
    )
    changed = 0
    for entry in rows.scalars().all():
        payload = dict(entry.payload or {}) if isinstance(entry.payload, dict) else {}
        raw_ids = payload.get("video_ids") or []
        kept_ids = []
        removed = False
        for raw_id in raw_ids:
            try:
                parsed = int(raw_id)
            except (TypeError, ValueError):
                kept_ids.append(raw_id)
                continue
            if parsed == int(video_id):
                removed = True
            else:
                kept_ids.append(raw_id)
        if not removed:
            continue
        changed += 1
        payload["video_ids"] = kept_ids
        entry.payload = payload
        if not kept_ids:
            entry.status = "cancelled"
            entry.lock_owner = None
            entry.locked_at = None
            entry.last_error = "planned_video_cancelled_by_autopilot_work"
    return changed


async def cancel_planned_video(db: AsyncSession, user_id: int, video_id: int) -> dict[str, Any]:
    now = datetime.now(timezone.utc)
    owner_row = await db.execute(select(User).filter(User.id == user_id).with_for_update())
    owner = owner_row.scalar_one_or_none()
    if not owner:
        return asdict(ActionResult(success=False, action="cancel_planned_video", code="user_not_found", message="User not found"))

    row = await db.execute(
        select(CreatorVideo, CreatorSource)
        .join(CreatorSource, CreatorVideo.creator_id == CreatorSource.id)
        .filter(CreatorVideo.id == int(video_id), CreatorSource.user_id == user_id)
        .with_for_update()
    )
    result = row.first()
    if not result:
        return asdict(ActionResult(success=False, action="cancel_planned_video", code="not_found", message="Видео не найдено"))
    video, creator = result
    if str(video.status or "") not in {"new", "queued"}:
        return asdict(
            ActionResult(
                success=False,
                action="cancel_planned_video",
                code="bad_status",
                message="Это видео уже нельзя отменить из плана",
            )
        )

    task = None
    if video.task_id:
        task_row = await db.execute(
            select(Task)
            .filter(
                Task.id == int(video.task_id),
                Task.user_id == user_id,
                Task.settings["origin"].as_string().in_(sorted(VISIBLE_ORIGINS)),
            )
            .with_for_update()
        )
        task = task_row.scalar_one_or_none()
        if task and str(task.status or "").lower() == "in_progress":
            return asdict(
                ActionResult(
                    success=False,
                    action="cancel_planned_video",
                    code="already_running",
                    message="Видео уже в работе. Остановите активный заказ во вкладке «В работе».",
                    details={"task_id": int(task.id)},
                )
            )

    outbox_changed = await _cancel_outbox_video_references(db, creator_id=int(creator.id), video_id=int(video.id))
    released_videos = 0
    refunded = Decimal("0.00")
    removed_from_queue = 0
    remove_queue_task_ids: list[int] = []
    task_id = int(task.id) if task and task.id else None

    if task and str(task.status or "").lower() == "pending":
        task_videos_result = await db.execute(
            select(CreatorVideo).filter(CreatorVideo.task_id == int(task.id)).with_for_update()
        )
        task_videos = list(task_videos_result.scalars().all())
        _mark_task_stopped(task, now=now, reason="planned_video_cancelled")
        refunded += await _refund_pending_task(db, owner, task, now=now, reason="отмену видео из плана")
        await db.execute(delete(PendingEnqueue).where(PendingEnqueue.task_id == int(task.id)))
        remove_queue_task_ids.append(int(task.id))
        for linked_video in task_videos:
            if int(linked_video.id) == int(video.id):
                _mark_video_skipped(linked_video, task_id=int(task.id), now=now)
            else:
                _mark_video_released(linked_video, task_id=int(task.id), now=now)
                released_videos += 1
    else:
        _mark_video_skipped(video, task_id=task_id, now=now)

    await db.commit()
    clear_autopilot_cache()
    if remove_queue_task_ids:
        removed_from_queue = await asyncio.to_thread(remove_tasks_from_queue, remove_queue_task_ids)
    return asdict(
        ActionResult(
            success=True,
            action="cancel_planned_video",
            message="Видео убрано из плана",
            details={
                "video_id": int(video.id),
                "task_id": task_id,
                "creator_id": int(creator.id),
                "creator_username": creator.username,
                "outbox_events_changed": outbox_changed,
                "released_other_videos": released_videos,
                "refunded_amount": float(refunded),
                "removed_from_redis_queue": int(removed_from_queue or 0),
            },
        )
    )


async def cancel_planned_task(db: AsyncSession, user_id: int, task_id: int) -> dict[str, Any]:
    now = datetime.now(timezone.utc)
    owner_row = await db.execute(select(User).filter(User.id == user_id).with_for_update())
    owner = owner_row.scalar_one_or_none()
    if not owner:
        return asdict(ActionResult(success=False, action="cancel_planned_task", code="user_not_found", message="User not found"))
    task_row = await db.execute(
        select(Task)
        .filter(
            Task.id == int(task_id),
            Task.user_id == user_id,
            Task.settings["origin"].as_string().in_(sorted(VISIBLE_ORIGINS)),
        )
        .with_for_update()
    )
    task = task_row.scalar_one_or_none()
    if not task:
        return asdict(ActionResult(success=False, action="cancel_planned_task", code="not_found", message="Заказ не найден"))
    if str(task.status or "").lower() != "pending":
        return asdict(
            ActionResult(
                success=False,
                action="cancel_planned_task",
                code="bad_status",
                message="Отменять из плана можно только ожидающий заказ",
            )
        )
    _mark_task_stopped(task, now=now, reason="planned_task_cancelled")
    refunded = await _refund_pending_task(db, owner, task, now=now, reason="отмену заказа из плана")
    video_rows = await db.execute(select(CreatorVideo).filter(CreatorVideo.task_id == int(task.id)).with_for_update())
    skipped = 0
    for video in video_rows.scalars().all():
        _mark_video_skipped(video, task_id=int(task.id), now=now)
        skipped += 1
    await db.execute(delete(PendingEnqueue).where(PendingEnqueue.task_id == int(task.id)))
    await db.commit()
    clear_autopilot_cache()
    removed = await asyncio.to_thread(remove_tasks_from_queue, [int(task.id)])
    return asdict(
        ActionResult(
            success=True,
            action="cancel_planned_task",
            message="Заказ убран из плана",
            details={
                "task_id": int(task.id),
                "skipped_videos": skipped,
                "refunded_amount": float(refunded),
                "removed_from_redis_queue": int(removed or 0),
            },
        )
    )


async def stop_work_task(db: AsyncSession, user_id: int, task_id: int) -> dict[str, Any]:
    now = datetime.now(timezone.utc)
    row = await db.execute(
        select(Task)
        .filter(
            Task.id == int(task_id),
            Task.user_id == user_id,
            Task.settings["origin"].as_string().in_(sorted(VISIBLE_ORIGINS)),
            Task.status.in_(["pending", "in_progress"]),
        )
        .with_for_update()
    )
    task = row.scalar_one_or_none()
    if not task:
        return asdict(ActionResult(success=False, action="stop_work_task", code="not_found", message="Заказ не найден или уже завершён"))
    status = str(task.status or "").lower()
    if status == "pending":
        return await cancel_planned_task(db, user_id, int(task.id))
    _mark_task_stopped(task, now=now, reason="work_task_stopped", force_close=True)
    await db.commit()
    clear_autopilot_cache()
    stop_sent = await asyncio.to_thread(request_task_stop, int(task.id))
    return asdict(
        ActionResult(
            success=True,
            action="stop_work_task",
            message=f"Заказ #{task.id} остановлен",
            details={"task_id": int(task.id), "stop_signal_sent": bool(stop_sent)},
        )
    )


async def cancel_all_pending(db: AsyncSession, user_id: int, *, include_running: bool = False) -> dict[str, Any]:
    now = datetime.now(timezone.utc)
    owner_row = await db.execute(select(User).filter(User.id == user_id).with_for_update())
    owner = owner_row.scalar_one_or_none()
    if not owner:
        return asdict(ActionResult(success=False, action="cancel_all", code="user_not_found", message="User not found"))

    creator_rows = await db.execute(select(CreatorSource.id).filter(CreatorSource.user_id == user_id))
    creator_ids = [creator_id for (creator_id,) in creator_rows.all()]
    outbox_cancelled = 0
    notification_cancelled = 0
    if creator_ids:
        outbox_rows = await db.execute(
            select(MonitorOutbox).filter(
                MonitorOutbox.creator_id.in_(creator_ids),
                MonitorOutbox.event_type.in_(["autopilot_create", "notify_auto_orders_created"]),
                MonitorOutbox.status.in_(["pending", "failed", "processing"]),
            )
        )
        for entry in outbox_rows.scalars().all():
            entry.status = "cancelled"
            entry.lock_owner = None
            entry.locked_at = None
            entry.last_error = "cancelled_by_autopilot_control"
            if entry.event_type == "autopilot_create":
                outbox_cancelled += 1
            else:
                notification_cancelled += 1

    statuses = ["pending", "in_progress"] if include_running else ["pending"]
    task_rows = await db.execute(
        select(Task)
        .filter(
            Task.user_id == user_id,
            Task.settings["origin"].as_string().in_(sorted(VISIBLE_ORIGINS)),
            Task.status.in_(statuses),
        )
        .order_by(Task.created_at.asc(), Task.id.asc())
        .with_for_update()
    )
    tasks = list(task_rows.scalars().all())
    pending_tasks = [task for task in tasks if str(task.status or "").lower() == "pending"]
    running_tasks = [task for task in tasks if str(task.status or "").lower() == "in_progress"]
    pending_ids = [int(task.id) for task in pending_tasks]
    running_ids = [int(task.id) for task in running_tasks]

    videos_by_task: dict[int, list[CreatorVideo]] = {}
    if pending_ids:
        video_rows = await db.execute(
            select(CreatorVideo)
            .join(CreatorSource, CreatorVideo.creator_id == CreatorSource.id)
            .filter(
                CreatorVideo.task_id.in_(pending_ids),
                CreatorSource.user_id == user_id,
                CreatorVideo.status == "queued",
            )
        )
        for video in video_rows.scalars().all():
            if video.task_id:
                videos_by_task.setdefault(int(video.task_id), []).append(video)

    refunded = Decimal("0.00")
    released = 0
    for task in pending_tasks:
        settings_dict = dict(task_settings(task))
        settings_dict["cancelled_by_user"] = True
        settings_dict["cancelled_at"] = now.isoformat()
        settings_dict["cancel_reason"] = "autopilot_control"
        task.status = "stopped"
        task.error_reason = "cancelled_by_autopilot_control"
        task.settings = settings_dict
        task.updated_at = now

        refund = _remaining_task_refund(task)
        if refund > 0:
            owner.balance = (owner.balance or Decimal("0.00")) + refund
            task.refunded_cost = money(task.refunded_cost) + refund
            db.add(
                Transaction(
                    user_id=user_id,
                    task_id=task.id,
                    amount=refund,
                    type="refund",
                    description=f"Возврат за отмену автокомментинга #{task.id}",
                )
            )
            refunded += refund

        for video in videos_by_task.get(int(task.id), []):
            _mark_video_released(video, task_id=int(task.id), now=now)
            released += 1

    for task in running_tasks:
        settings_dict = dict(task_settings(task))
        settings_dict["stopped_by_user"] = True
        settings_dict["stopped_at"] = now.isoformat()
        settings_dict["force_close_browsers"] = True
        settings_dict["cancel_reason"] = "autopilot_control"
        task.status = "stopped"
        task.error_reason = "stopped_by_autopilot_control"
        task.settings = settings_dict
        task.updated_at = now

    if pending_ids:
        await db.execute(delete(PendingEnqueue).where(PendingEnqueue.task_id.in_(pending_ids)))
    await db.commit()
    clear_autopilot_cache()

    removed = await asyncio.to_thread(remove_tasks_from_queue, pending_ids) if pending_ids else 0
    stop_signals = 0
    for task_id in running_ids:
        if await asyncio.to_thread(request_task_stop, task_id):
            stop_signals += 1

    return asdict(
        ActionResult(
            success=True,
            action="cancel_all",
            message="Очередь отменена",
            details={
                "cancelled_pending_tasks": len(pending_ids),
                "stopped_running_tasks": len(running_ids),
                "cancelled_outbox_events": outbox_cancelled,
                "cancelled_notifications": notification_cancelled,
                "released_videos": released,
                "refunded_amount": float(refunded),
                "removed_from_redis_queue": removed,
                "stop_signal_count": stop_signals,
            },
        )
    )


async def skip_autopilot_queue(db: AsyncSession, user_id: int) -> dict[str, Any]:
    now = datetime.now(timezone.utc)
    owner_row = await db.execute(select(User).filter(User.id == user_id).with_for_update())
    owner = owner_row.scalar_one_or_none()
    if not owner:
        return asdict(ActionResult(success=False, action="skip_queue", code="user_not_found", message="User not found"))

    creator_rows = await db.execute(select(CreatorSource.id).filter(CreatorSource.user_id == user_id))
    creator_ids = [creator_id for (creator_id,) in creator_rows.all()]

    outbox_cancelled = 0
    notification_cancelled = 0
    outbox_video_ids: set[int] = set()
    if creator_ids:
        outbox_rows = await db.execute(
            select(MonitorOutbox).filter(
                MonitorOutbox.creator_id.in_(creator_ids),
                MonitorOutbox.event_type.in_(["autopilot_create", "notify_auto_orders_created"]),
                MonitorOutbox.status.in_(["pending", "failed", "processing"]),
            )
        )
        for entry in outbox_rows.scalars().all():
            payload = entry.payload if isinstance(entry.payload, dict) else {}
            if entry.event_type == "autopilot_create":
                for video_id in payload.get("video_ids") or []:
                    try:
                        outbox_video_ids.add(int(video_id))
                    except (TypeError, ValueError):
                        continue
                outbox_cancelled += 1
            else:
                notification_cancelled += 1
            entry.status = "cancelled"
            entry.lock_owner = None
            entry.locked_at = None
            entry.last_error = "skipped_by_autopilot_control"

    task_rows = await db.execute(
        select(Task)
        .filter(
            Task.user_id == user_id,
            Task.settings["origin"].as_string().in_(sorted(VISIBLE_ORIGINS)),
            Task.status == "pending",
        )
        .order_by(Task.created_at.asc(), Task.id.asc())
        .with_for_update()
    )
    tasks = list(task_rows.scalars().all())
    pending_ids = [int(task.id) for task in tasks]

    videos_by_task: dict[int, list[CreatorVideo]] = {}
    if pending_ids:
        video_rows = await db.execute(
            select(CreatorVideo)
            .join(CreatorSource, CreatorVideo.creator_id == CreatorSource.id)
            .filter(
                CreatorVideo.task_id.in_(pending_ids),
                CreatorSource.user_id == user_id,
                CreatorVideo.status.in_(["queued", "new"]),
            )
            .with_for_update()
        )
        for video in video_rows.scalars().all():
            if video.task_id:
                videos_by_task.setdefault(int(video.task_id), []).append(video)

    outbox_videos: list[CreatorVideo] = []
    if outbox_video_ids:
        outbox_video_rows = await db.execute(
            select(CreatorVideo)
            .join(CreatorSource, CreatorVideo.creator_id == CreatorSource.id)
            .filter(
                CreatorVideo.id.in_(outbox_video_ids),
                CreatorSource.user_id == user_id,
                CreatorVideo.status.in_(["new", "queued"]),
                or_(CreatorVideo.task_id.is_(None), CreatorVideo.task_id.in_(pending_ids or [-1])),
            )
            .with_for_update()
        )
        outbox_videos = list(outbox_video_rows.scalars().all())

    refunded = Decimal("0.00")
    skipped_video_ids: set[int] = set()
    for task in tasks:
        settings_dict = dict(task_settings(task))
        settings_dict["skipped_by_user"] = True
        settings_dict["skipped_at"] = now.isoformat()
        settings_dict["cancel_reason"] = "autopilot_skip_queue"
        task.status = "stopped"
        task.error_reason = "skipped_by_autopilot_control"
        task.settings = settings_dict
        task.updated_at = now

        refund = _remaining_task_refund(task)
        if refund > 0:
            owner.balance = (owner.balance or Decimal("0.00")) + refund
            task.refunded_cost = money(task.refunded_cost) + refund
            db.add(
                Transaction(
                    user_id=user_id,
                    task_id=task.id,
                    amount=refund,
                    type="refund",
                    description=f"Возврат за пропуск очереди автокомментинга #{task.id}",
                )
            )
            refunded += refund

        for video in videos_by_task.get(int(task.id), []):
            _mark_video_skipped(video, task_id=int(task.id), now=now)
            skipped_video_ids.add(int(video.id))

    for video in outbox_videos:
        _mark_video_skipped(video, task_id=video.task_id, now=now)
        skipped_video_ids.add(int(video.id))

    if pending_ids:
        await db.execute(delete(PendingEnqueue).where(PendingEnqueue.task_id.in_(pending_ids)))
    await db.commit()
    clear_autopilot_cache()

    removed = await asyncio.to_thread(remove_tasks_from_queue, pending_ids) if pending_ids else 0
    return asdict(
        ActionResult(
            success=True,
            action="skip_queue",
            message="Очередь пропущена",
            details={
                "cancelled_pending_tasks": len(pending_ids),
                "cancelled_outbox_events": outbox_cancelled,
                "cancelled_notifications": notification_cancelled,
                "skipped_videos": len(skipped_video_ids),
                "refunded_amount": float(refunded),
                "removed_from_redis_queue": removed,
            },
        )
    )


async def stop_active_task(db: AsyncSession, user_id: int) -> dict[str, Any]:
    dashboard = await collect_dashboard(db, user_id)
    active_task = (dashboard.get("current_work") or {}).get("active_task") or {}
    task_id = active_task.get("task_id") or (dashboard.get("current_work") or {}).get("active_task_id")
    if not task_id:
        return asdict(ActionResult(success=False, action="stop_active", code="no_active_task", message="Активный заказ не найден"))

    row = await db.execute(
        select(Task).filter(
            Task.id == int(task_id),
            Task.user_id == user_id,
            Task.settings["origin"].as_string().in_(sorted(VISIBLE_ORIGINS)),
            Task.status == "in_progress",
        )
    )
    task = row.scalar_one_or_none()
    if not task:
        return asdict(ActionResult(success=False, action="stop_active", code="no_active_task", message="Активный заказ не найден"))

    now = datetime.now(timezone.utc)
    settings_dict = dict(task_settings(task))
    settings_dict["stopped_by_user"] = True
    settings_dict["stopped_at"] = now.isoformat()
    settings_dict["force_close_browsers"] = True
    settings_dict["cancel_reason"] = "autopilot_control"
    task.status = "stopped"
    task.error_reason = "stopped_by_autopilot_control"
    task.settings = settings_dict
    task.updated_at = now
    await db.commit()
    clear_autopilot_cache()
    stop_sent = await asyncio.to_thread(request_task_stop, int(task_id))
    return asdict(
        ActionResult(
            success=True,
            action="stop_active",
            message=f"Заказ #{task_id} остановлен",
            details={"task_id": int(task_id), "stop_signal_sent": bool(stop_sent)},
        )
    )


async def archive_completed(db: AsyncSession, user_id: int, period: str = "24h") -> dict[str, Any]:
    filters = period_filters(user_id, period, include_archived=False)
    filters.append(Task.status.in_(sorted(TERMINAL_STATUSES)))
    rows = await db.execute(select(Task).filter(*filters).with_for_update())
    tasks = list(rows.scalars().all())
    now = datetime.now(timezone.utc).isoformat()
    for task in tasks:
        settings_dict = dict(task_settings(task))
        settings_dict["origin"] = "autopilot_archived"
        settings_dict["autopilot_report_hidden"] = True
        settings_dict["autopilot_report_archived_at"] = now
        task.settings = settings_dict
    await db.commit()
    clear_autopilot_cache()
    return asdict(
        ActionResult(
            success=True,
            action="archive",
            message="Готовые заказы архивированы",
            details={"archived_tasks": len(tasks), "period": period, "period_label": period_label(period)},
        )
    )


async def hide_task(db: AsyncSession, user_id: int, task_id: int) -> dict[str, Any]:
    row = await db.execute(
        select(Task).filter(
            Task.id == int(task_id),
            Task.user_id == user_id,
            Task.settings["origin"].as_string().in_(["autopilot", "autopilot_archived", "manual_retry"]),
        )
    )
    task = row.scalar_one_or_none()
    if not task:
        return asdict(ActionResult(success=False, action="hide", code="not_found", message="Заказ не найден"))
    if str(task.status or "").lower() not in TERMINAL_STATUSES:
        return asdict(
            ActionResult(
                success=False,
                action="hide",
                code="bad_status",
                message="Скрыть можно только завершённый, частичный, ошибочный или остановленный заказ",
            )
        )
    settings_dict = dict(task_settings(task))
    settings_dict["origin"] = "autopilot_archived"
    settings_dict["autopilot_report_hidden"] = True
    settings_dict["autopilot_report_archived_at"] = datetime.now(timezone.utc).isoformat()
    task.settings = settings_dict
    await db.commit()
    clear_autopilot_cache()
    return asdict(ActionResult(success=True, action="hide", message=f"Заказ #{task_id} скрыт", details={"task_id": int(task_id)}))


RUNTIME_SETTING_KEYS = {
    "url_results",
    "failed_urls",
    "idempotency_key",
    "client_task_fingerprint",
    "request_id",
    "autopilot_report_hidden",
    "autopilot_report_archived_at",
    "cancelled_by_user",
    "cancelled_at",
    "cancel_reason",
    "stopped_by_user",
    "stopped_at",
    "force_close_browsers",
}


def _retry_settings(source: dict[str, Any], task_id: int) -> dict[str, Any]:
    clean = copy.deepcopy(source if isinstance(source, dict) else {})
    for key in RUNTIME_SETTING_KEYS:
        clean.pop(key, None)
    clean["origin"] = "manual_retry"
    clean["retry_of_task_id"] = int(task_id)
    return clean


RETRYABLE_ORIGINS = ["autopilot", "autopilot_archived", "manual_retry"]


def _is_free_retry_user(user: User) -> bool:
    user_is_admin = user.role in {"admin", "owner"}
    admin_ids = getattr(settings, "ADMIN_IDS", [])
    if isinstance(admin_ids, str):
        try:
            admin_ids = [int(item.strip()) for item in admin_ids.split(",") if item.strip()]
        except Exception:
            admin_ids = []
    if not user_is_admin and isinstance(admin_ids, list) and user.telegram_id:
        user_is_admin = int(user.telegram_id) in admin_ids
    return bool(settings.DEMO_MODE or user_is_admin)


async def _load_retry_context(db: AsyncSession, user_id: int, task_id: int, *, lock_user: bool = False) -> tuple[Task | None, User | None]:
    row = await db.execute(
        select(Task).filter(
            Task.id == int(task_id),
            Task.user_id == user_id,
            Task.settings["origin"].as_string().in_(RETRYABLE_ORIGINS),
        )
    )
    original = row.scalar_one_or_none()
    user_query = select(User).filter(User.id == user_id)
    if lock_user:
        user_query = user_query.with_for_update()
    user_row = await db.execute(user_query)
    user = user_row.scalar_one_or_none()
    return original, user


async def retry_task_preview(db: AsyncSession, user_id: int, task_id: int) -> dict[str, Any]:
    original, user = await _load_retry_context(db, user_id, task_id)
    if not original:
        return asdict(ActionResult(success=False, action="retry_preview", code="not_found", message="Заказ не найден"))
    if not user:
        return asdict(ActionResult(success=False, action="retry_preview", code="user_not_found", message="Пользователь не найден"))
    if str(original.status or "").lower() not in {"error", "partial"}:
        return asdict(
            ActionResult(
                success=False,
                action="retry_preview",
                code="bad_status",
                message="Повтор доступен только для ошибок и частичных заказов",
            )
        )

    video_urls = task_video_urls(original)
    pricing = await get_pricing_async(db)
    cost = Decimal("0.00") if _is_free_retry_user(user) else calculate_task_cost(len(video_urls), pricing.price_success)
    balance = await get_user_balance_async(db, user_id)
    details = {
        "task_id": int(original.id),
        "video_count": len(video_urls),
        "cost": float(cost),
        "balance": float(balance),
        "required": float(cost),
    }
    if balance < cost:
        return asdict(
            ActionResult(
                success=False,
                action="retry_preview",
                code="insufficient_funds",
                message="Недостаточно средств на балансе",
                details=details,
            )
        )
    return asdict(ActionResult(success=True, action="retry_preview", message="Повтор доступен", details=details))


async def retry_task(db: AsyncSession, user_id: int, task_id: int) -> dict[str, Any]:
    preview = await retry_task_preview(db, user_id, task_id)
    if not preview.get("success"):
        preview["action"] = "retry"
        return preview

    original, user = await _load_retry_context(db, user_id, task_id, lock_user=True)
    if not original:
        return asdict(ActionResult(success=False, action="retry", code="not_found", message="Заказ не найден"))
    if not user:
        return asdict(ActionResult(success=False, action="retry", code="user_not_found", message="Пользователь не найден"))
    if str(original.status or "").lower() not in {"error", "partial"}:
        return asdict(ActionResult(success=False, action="retry", code="bad_status", message="Повтор доступен только для ошибок и частичных заказов"))

    pricing = await get_pricing_async(db)
    video_urls = task_video_urls(original)
    cost = Decimal("0.00") if _is_free_retry_user(user) else calculate_task_cost(len(video_urls), pricing.price_success)
    balance = await get_user_balance_async(db, user_id)
    if balance < cost:
        return asdict(
            ActionResult(
                success=False,
                action="retry",
                code="insufficient_funds",
                message="Недостаточно средств на балансе",
                details={"balance": float(balance), "required": float(cost), "cost": float(cost)},
            )
        )

    if cost > 0:
        user.balance = (user.balance or Decimal("0.00")) - cost

    settings_dict = _retry_settings(task_settings(original), int(original.id))
    settings_dict["commenting_mode"] = normalize_commenting_mode(
        await get_app_setting(db, COMMENTING_MODE_KEY, COMMENTING_MODE_OLD),
        default=COMMENTING_MODE_OLD,
    )
    new_task = Task(
        user_id=user_id,
        status="pending",
        main_comment=original.main_comment,
        reply1=original.reply1,
        reply2=original.reply2,
        reply3=original.reply3,
        likes=original.likes,
        video_urls=video_urls,
        settings=settings_dict,
        kind=original.kind,
        total_cost=cost,
        refunded_cost=None,
        video_attempts={},
    )
    db.add(new_task)
    await db.flush()
    if cost > 0:
        db.add(
            Transaction(
                user_id=user_id,
                task_id=new_task.id,
                amount=-cost,
                type="hold",
                description=f"Повтор автокомментинга #{original.id}",
            )
        )
    await reserve_task_for_enqueue_async(db, int(new_task.id))
    await db.commit()
    clear_autopilot_cache()
    await finalize_reserved_enqueue_async(db, int(new_task.id))
    return asdict(
        ActionResult(
            success=True,
            action="retry",
            message=f"Создан заказ #{new_task.id}",
            details={"task_id": int(new_task.id), "retry_of_task_id": int(original.id), "cost": float(cost)},
        )
    )


def _cooldown_remaining_seconds(user_id: int) -> int:
    try:
        redis_client = get_redis_client()
        ttl = redis_client.ttl(f"notify_admin:cooldown:{user_id}")
        return max(0, int(ttl or 0))
    except Exception:
        return 0


def notify_admin_cooldown_minutes(user_id: int) -> int:
    seconds = _cooldown_remaining_seconds(user_id)
    return (seconds + 59) // 60 if seconds > 0 else 0


async def notify_admin(db: AsyncSession, user_id: int) -> dict[str, Any]:
    cooldown_seconds = _cooldown_remaining_seconds(user_id)
    if cooldown_seconds > 0:
        return asdict(
            ActionResult(
                success=False,
                action="notify_admin",
                code="cooldown",
                message="Уведомление уже отправлено",
                details={"remaining_seconds": cooldown_seconds, "remaining_minutes": (cooldown_seconds + 59) // 60},
            )
        )

    user_row = await db.execute(select(User).filter(User.id == user_id))
    user = user_row.scalar_one_or_none()
    if not user:
        return asdict(ActionResult(success=False, action="notify_admin", code="user_not_found", message="Пользователь не найден"))
    dashboard = await collect_dashboard(db, user_id)
    incidents = dashboard.get("incidents") or []
    metadata = dashboard.get("metadata") or {}
    auto_actions = [str(item.get("auto_action") or "").strip() for item in incidents if str(item.get("auto_action") or "").strip()]
    admin_actions = metadata.get("recommended_actions") or []
    incident_lines = [
        f"- {item.get('title')}: {item.get('cause')}"
        for item in incidents
    ] or ["- Активных инцидентов нет"]
    auto_lines = [f"- {item}" for item in auto_actions] or ["- Софт продолжает наблюдать и держит защитные паузы"]
    admin_lines = "\n".join(f"- {item}" for item in admin_actions) if admin_actions else "- Открыть мониторинг и проверить состояние"
    username = f"@{user.username}" if user.username else "без username"
    message = "\n".join(
        [
            "⚠️ Запрос помощи от работника",
            "",
            f"Работник: {username} (id={user.telegram_id})",
            f"Состояние автокомментинга: {dashboard.get('state_icon')} {dashboard.get('state_label')}",
            "",
            "Активные инциденты:",
            *incident_lines,
            "",
            "Что софт уже сделал:",
            *auto_lines,
            "",
            "Рекомендуемые действия:",
            admin_lines,
        ]
    )
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="Открыть мониторинг →", callback_data="admin:monitoring")]]
    )

    admin_rows = await db.execute(select(User).filter(User.role.in_(["admin", "owner"]), User.is_blocked.is_(False)))
    admins = list(admin_rows.scalars().all())
    sent = 0
    for admin in admins:
        if admin.telegram_id and await send_notification_to_user(int(admin.telegram_id), message, keyboard):
            sent += 1
    if sent:
        try:
            get_redis_client().set(f"notify_admin:cooldown:{user_id}", "1", ex=1800)
        except Exception:
            pass
    return asdict(
        ActionResult(
            success=sent > 0,
            action="notify_admin",
            message="Админ уведомлён" if sent else "Не удалось отправить уведомление",
            details={"sent": sent, "admin_count": len(admins)},
        )
    )


async def cancel_all_preview(db: AsyncSession, user_id: int) -> dict[str, Any]:
    rows = await db.execute(
        select(Task).filter(
            Task.user_id == user_id,
            Task.settings["origin"].as_string().in_(sorted(VISIBLE_ORIGINS)),
            Task.status == "pending",
        )
    )
    tasks = list(rows.scalars().all())
    refund = sum((_remaining_task_refund(task) for task in tasks), Decimal("0.00"))
    active_row = await db.execute(
        select(Task.id).filter(
            Task.user_id == user_id,
            Task.settings["origin"].as_string().in_(sorted(VISIBLE_ORIGINS)),
            Task.status == "in_progress",
        )
    )
    active_id = active_row.scalars().first()
    return {"pending_count": len(tasks), "refund": float(refund), "active_task_id": active_id}


async def skip_queue_preview(db: AsyncSession, user_id: int) -> dict[str, Any]:
    rows = await db.execute(
        select(Task).filter(
            Task.user_id == user_id,
            Task.settings["origin"].as_string().in_(sorted(VISIBLE_ORIGINS)),
            Task.status == "pending",
        )
    )
    tasks = list(rows.scalars().all())
    pending_ids = [int(task.id) for task in tasks]
    refund = sum((_remaining_task_refund(task) for task in tasks), Decimal("0.00"))

    video_ids: set[int] = set()
    if pending_ids:
        linked_rows = await db.execute(
            select(CreatorVideo.id)
            .join(CreatorSource, CreatorVideo.creator_id == CreatorSource.id)
            .filter(
                CreatorVideo.task_id.in_(pending_ids),
                CreatorSource.user_id == user_id,
                CreatorVideo.status.in_(["queued", "new"]),
            )
        )
        video_ids.update(int(video_id) for video_id in linked_rows.scalars().all())

    creator_rows = await db.execute(select(CreatorSource.id).filter(CreatorSource.user_id == user_id))
    creator_ids = [creator_id for (creator_id,) in creator_rows.all()]
    outbox_count = 0
    if creator_ids:
        outbox_rows = await db.execute(
            select(MonitorOutbox).filter(
                MonitorOutbox.creator_id.in_(creator_ids),
                MonitorOutbox.event_type == "autopilot_create",
                MonitorOutbox.status.in_(["pending", "failed", "processing"]),
            )
        )
        for entry in outbox_rows.scalars().all():
            outbox_count += 1
            payload = entry.payload if isinstance(entry.payload, dict) else {}
            for video_id in payload.get("video_ids") or []:
                try:
                    video_ids.add(int(video_id))
                except (TypeError, ValueError):
                    continue

    active_row = await db.execute(
        select(Task.id).filter(
            Task.user_id == user_id,
            Task.settings["origin"].as_string().in_(sorted(VISIBLE_ORIGINS)),
            Task.status == "in_progress",
        )
    )
    return {
        "pending_count": len(tasks),
        "outbox_count": outbox_count,
        "video_count": len(video_ids),
        "refund": float(refund),
        "active_task_id": active_row.scalars().first(),
    }


async def archive_preview(db: AsyncSession, user_id: int, period: str) -> dict[str, Any]:
    return {
        "count": await count_archive_candidates(db, user_id, period),
        "period": period,
        "period_label": period_label(period),
    }
