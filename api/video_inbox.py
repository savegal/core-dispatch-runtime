"""
API для работы с Inbox (единый экран для новых видео).

УЛУЧШЕНО: Единый вход для всех новых видео вместо множества разделов.
"""
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func
from db.database import get_db
from db.models import CreatorVideo, CreatorSource, Task, User
from typing import List, Optional
import logging
from datetime import datetime, timezone
from api.auth import verify_user_header, ensure_user_scope
from services.user_facing_errors import build_video_status_summary
from services.video_time_utils import create_time_from_meta, published_at_iso_from_create_time

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/video-inbox", tags=["video_inbox"])


@router.get("/videos")
async def get_inbox_videos(
    user_id: int = Query(..., description="ID пользователя"),
    status: Optional[str] = Query(None, description="Фильтр по статусу: new/queued/skipped/done"),
    limit: int = Query(20, le=50, description="Лимит видео"),
    offset: int = Query(0, ge=0, description="Смещение"),
    db: AsyncSession = Depends(get_db),
    auth_user_id: int = Depends(verify_user_header),
) -> dict:
    """
    УЛУЧШЕНО: Получает видео для Inbox с группировкой по статусам.
    """
    ensure_user_scope(user_id, auth_user_id)
    # Получаем все подписки пользователя
    creators_result = await db.execute(
        select(CreatorSource.id).filter(CreatorSource.user_id == user_id)
    )
    creator_ids = [row[0] for row in creators_result.all()]
    
    if not creator_ids:
        return {
            "total": 0,
            "by_status": {"new": 0, "queued": 0, "skipped": 0, "failed": 0, "done": 0},
            "videos": []
        }
    
    # Базовый запрос. Task.status подтягиваем outer-join'ом, чтобы UI мог
    # показать «⏹ Остановить» только для in_progress и «📋 Заказ #N» с
    # актуальным статусом — без N+1 запросов из бота.
    query = (
        select(CreatorVideo, CreatorSource.username, Task.status)
        .join(CreatorSource, CreatorVideo.creator_id == CreatorSource.id)
        .outerjoin(Task, CreatorVideo.task_id == Task.id)
        .filter(CreatorVideo.creator_id.in_(creator_ids))
    )

    # Фильтр по статусу
    if status:
        query = query.filter(CreatorVideo.status == status)
    
    # Подсчет по статусам
    count_query = select(
        CreatorVideo.status,
        func.count(CreatorVideo.id).label("count")
    ).filter(CreatorVideo.creator_id.in_(creator_ids)).group_by(CreatorVideo.status)
    
    count_result = await db.execute(count_query)
    status_counts = {row[0]: row[1] for row in count_result.all()}
    
    by_status = {
        "new": status_counts.get("new", 0),
        "queued": status_counts.get("queued", 0),
        "skipped": status_counts.get("skipped", 0),
        "failed": status_counts.get("failed", 0),
        "done": status_counts.get("done", 0)
    }
    
    # Получаем видео
    query = query.order_by(CreatorVideo.discovered_at.desc()).limit(limit).offset(offset)
    result = await db.execute(query)
    videos_data = result.all()
    
    videos = []
    for video, username, task_status in videos_data:
        video_summary = build_video_status_summary(video.status, video.meta)
        create_time = create_time_from_meta(video.meta)
        videos.append({
            "id": video.id,
            "creator_id": video.creator_id,
            "creator_username": username,
            "video_id": video.video_id,
            "url": video.url,
            "status": video.status,
            "task_id": video.task_id,
            "task_status": task_status,
            "discovered_at": video.discovered_at.isoformat() if video.discovered_at else None,
            "create_time": create_time,
            "published_at": published_at_iso_from_create_time(create_time),
            "meta": video.meta,
            "display_status": video_summary.get("display_status"),
            "status_label": video_summary.get("status_label"),
            "status_badge": video_summary.get("status_badge"),
            "human_reason": video_summary.get("display_reason"),
            "warnings": video_summary.get("warnings", []),
            "has_warnings": video_summary.get("has_warnings", False),
        })
    
    total = sum(status_counts.values())
    
    return {
        "total": total,
        "by_status": by_status,
        "videos": videos
    }


# ── Batch request model ──────────────────────────────────────────────────────

class BatchSkipRequest(BaseModel):
    """Модель для пропуска/отметки нескольких видео."""
    video_ids: List[int]


def _mark_video_done_manually(video) -> None:
    """Помечает видео как откомментированное вручную (status=done, meta.commented_manually=true)."""
    video.status = "done"
    meta = dict(video.meta or {})
    meta["commented_manually"] = True
    meta["commented_manually_at"] = datetime.now(timezone.utc).isoformat()
    video.meta = meta


def _ensure_batch_videos_owned(videos_data: list[tuple[CreatorVideo, int]], user_id: int) -> None:
    if any(video_owner_id != user_id for _, video_owner_id in videos_data):
        raise HTTPException(status_code=403, detail="You can only update your own videos")


# ── Static / batch routes MUST be declared before {video_id} routes ───────

@router.post("/videos/batch/skip")
async def skip_videos_batch(
    request: BatchSkipRequest,
    user_id: int = Query(..., description="ID пользователя"),
    db: AsyncSession = Depends(get_db),
    auth_user_id: int = Depends(verify_user_header),
) -> dict:
    """
    УЛУЧШЕНО: Пропускает несколько видео пачкой.
    """
    ensure_user_scope(user_id, auth_user_id)
    # Проверяем права на все видео
    result = await db.execute(
        select(CreatorVideo, CreatorSource.user_id).join(
            CreatorSource, CreatorVideo.creator_id == CreatorSource.id
        ).filter(CreatorVideo.id.in_(request.video_ids))
    )
    videos_data = result.all()
    
    if len(videos_data) != len(request.video_ids):
        raise HTTPException(status_code=404, detail="Some videos not found")
    _ensure_batch_videos_owned(videos_data, user_id)
    
    skipped_count = 0
    for video, _ in videos_data:
        video.status = "skipped"
        skipped_count += 1
    
    await db.commit()
    
    return {"ok": True, "skipped": skipped_count}


@router.post("/videos/batch/mark-commented")
async def mark_videos_commented_batch(
    request: BatchSkipRequest,
    user_id: int = Query(..., description="ID пользователя"),
    db: AsyncSession = Depends(get_db),
    auth_user_id: int = Depends(verify_user_header),
) -> dict:
    """Помечает несколько видео как откомментированные вручную пачкой."""
    ensure_user_scope(user_id, auth_user_id)
    result = await db.execute(
        select(CreatorVideo, CreatorSource.user_id).join(
            CreatorSource, CreatorVideo.creator_id == CreatorSource.id
        ).filter(CreatorVideo.id.in_(request.video_ids))
    )
    videos_data = result.all()

    if len(videos_data) != len(request.video_ids):
        raise HTTPException(status_code=404, detail="Some videos not found")
    _ensure_batch_videos_owned(videos_data, user_id)

    marked_count = 0
    for video, _ in videos_data:
        _mark_video_done_manually(video)
        marked_count += 1

    await db.commit()
    return {"ok": True, "marked": marked_count}


@router.post("/videos/batch/skip-new")
async def skip_all_new_videos(
    user_id: int = Query(..., description="ID пользователя"),
    db: AsyncSession = Depends(get_db),
    auth_user_id: int = Depends(verify_user_header),
) -> dict:
    """Пропускает все новые видео пользователя без лимита страницы."""
    ensure_user_scope(user_id, auth_user_id)
    result = await db.execute(
        select(CreatorVideo)
        .join(CreatorSource, CreatorVideo.creator_id == CreatorSource.id)
        .filter(CreatorSource.user_id == user_id, CreatorVideo.status == "new")
    )
    videos = list(result.scalars().all())
    for video in videos:
        video.status = "skipped"

    await db.commit()
    return {"ok": True, "skipped": len(videos)}


# ── Parameterized {video_id} routes (AFTER batch routes) ─────────────────

@router.post("/videos/{video_id}/skip")
async def skip_video(
    video_id: int,
    user_id: int = Query(..., description="ID пользователя"),
    db: AsyncSession = Depends(get_db),
    auth_user_id: int = Depends(verify_user_header),
) -> dict:
    """
    УЛУЧШЕНО: Пропускает видео (помечает как skipped).
    """
    ensure_user_scope(user_id, auth_user_id)
    result = await db.execute(
        select(CreatorVideo, CreatorSource.user_id).join(
            CreatorSource, CreatorVideo.creator_id == CreatorSource.id
        ).filter(CreatorVideo.id == video_id)
    )
    row = result.first()
    
    if not row:
        raise HTTPException(status_code=404, detail="Video not found")
    
    video, video_owner_id = row
    
    if video_owner_id != user_id:
        raise HTTPException(status_code=403, detail="You can only skip your own videos")
    
    video.status = "skipped"
    await db.commit()
    
    return {"ok": True, "message": "Video skipped"}


@router.post("/videos/{video_id}/unskip")
async def unskip_video(
    video_id: int,
    user_id: int = Query(..., description="ID пользователя"),
    db: AsyncSession = Depends(get_db),
    auth_user_id: int = Depends(verify_user_header),
) -> dict:
    """Возвращает пропущенное видео в статус ``new``.

    Используется из inbox-таба «Пропущенные» (кнопка «↩️ Вернуть в новые»).
    Безопасно: применимо только к видео в статусе skipped, иначе 409 — чтобы
    случайно не «оживить» уже выполненное или зависшее в работе.
    """
    ensure_user_scope(user_id, auth_user_id)
    result = await db.execute(
        select(CreatorVideo, CreatorSource.user_id).join(
            CreatorSource, CreatorVideo.creator_id == CreatorSource.id
        ).filter(CreatorVideo.id == video_id)
    )
    row = result.first()
    if not row:
        raise HTTPException(status_code=404, detail="Video not found")

    video, video_owner_id = row
    if video_owner_id != user_id:
        raise HTTPException(status_code=403, detail="You can only update your own videos")

    if video.status != "skipped":
        raise HTTPException(
            status_code=409,
            detail=f"Only skipped videos can be unskipped, got status={video.status!r}",
        )

    previous_task_id = video.task_id
    meta = dict(video.meta or {})
    if previous_task_id:
        meta["unskipped_from_task_id"] = previous_task_id
    meta["unskipped_at"] = datetime.now(timezone.utc).isoformat()

    video.status = "new"
    video.task_id = None
    video.meta = meta
    await db.commit()
    return {"ok": True, "message": "Video returned to new", "previous_task_id": previous_task_id}


@router.post("/videos/{video_id}/reset-to-new")
async def reset_video_to_new(
    video_id: int,
    user_id: int = Query(..., description="ID пользователя"),
    db: AsyncSession = Depends(get_db),
    auth_user_id: int = Depends(verify_user_header),
) -> dict:
    """Сбрасывает упавшее видео в статус ``new`` для повторной публикации.

    Используется из inbox-таба «Ошибки» (кнопка «🔁 Повторить»). Очищает
    привязку к старой задаче (task_id=None), чтобы новый заказ не делил
    идентификатор с провалившимся, и проставляет в meta служебную пометку
    ``retry_of_task_id``, чтобы можно было аудитом отследить повторы.
    Применимо только к failed-видео — на других статусах 409.
    """
    ensure_user_scope(user_id, auth_user_id)
    result = await db.execute(
        select(CreatorVideo, CreatorSource.user_id).join(
            CreatorSource, CreatorVideo.creator_id == CreatorSource.id
        ).filter(CreatorVideo.id == video_id)
    )
    row = result.first()
    if not row:
        raise HTTPException(status_code=404, detail="Video not found")

    video, video_owner_id = row
    if video_owner_id != user_id:
        raise HTTPException(status_code=403, detail="You can only update your own videos")

    if video.status != "failed":
        raise HTTPException(
            status_code=409,
            detail=f"Only failed videos can be reset to new, got status={video.status!r}",
        )

    previous_task_id = video.task_id
    meta = dict(video.meta or {})
    if previous_task_id:
        meta["retry_of_task_id"] = previous_task_id
    meta["reset_to_new_at"] = datetime.now(timezone.utc).isoformat()

    video.status = "new"
    video.task_id = None
    video.meta = meta
    await db.commit()
    return {"ok": True, "message": "Video reset to new", "previous_task_id": previous_task_id}


@router.post("/videos/{video_id}/mark-commented")
async def mark_video_commented(
    video_id: int,
    user_id: int = Query(..., description="ID пользователя"),
    db: AsyncSession = Depends(get_db),
    auth_user_id: int = Depends(verify_user_header),
) -> dict:
    """Помечает видео как откомментированное вручную (status=done)."""
    ensure_user_scope(user_id, auth_user_id)
    result = await db.execute(
        select(CreatorVideo, CreatorSource.user_id).join(
            CreatorSource, CreatorVideo.creator_id == CreatorSource.id
        ).filter(CreatorVideo.id == video_id)
    )
    row = result.first()
    if not row:
        raise HTTPException(status_code=404, detail="Video not found")

    video, video_owner_id = row
    if video_owner_id != user_id:
        raise HTTPException(status_code=403, detail="You can only mark your own videos")

    _mark_video_done_manually(video)
    await db.commit()
    return {"ok": True, "message": "Video marked as commented"}


@router.get("/stats")
async def get_inbox_stats(
    user_id: int = Query(..., description="ID пользователя"),
    db: AsyncSession = Depends(get_db),
    auth_user_id: int = Depends(verify_user_header),
) -> dict:
    """
    УЛУЧШЕНО: Получает статистику Inbox (количество по статусам).
    """
    ensure_user_scope(user_id, auth_user_id)
    creators_result = await db.execute(
        select(CreatorSource.id).filter(CreatorSource.user_id == user_id)
    )
    creator_ids = [row[0] for row in creators_result.all()]
    
    if not creator_ids:
        return {
            "new": 0,
            "queued": 0,
            "skipped": 0,
            "failed": 0,
            "done": 0,
            "total": 0
        }
    
    count_query = select(
        CreatorVideo.status,
        func.count(CreatorVideo.id).label("count")
    ).filter(CreatorVideo.creator_id.in_(creator_ids)).group_by(CreatorVideo.status)
    
    count_result = await db.execute(count_query)
    status_counts = {row[0]: row[1] for row in count_result.all()}
    
    return {
        "new": status_counts.get("new", 0),
        "queued": status_counts.get("queued", 0),
        "skipped": status_counts.get("skipped", 0),
        "failed": status_counts.get("failed", 0),
        "done": status_counts.get("done", 0),
        "total": sum(status_counts.values())
    }
