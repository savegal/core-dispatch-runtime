"""
API для работы с VideoBatch (P0 FIX: решение проблемы callback_data с video_ids).
"""
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from db.database import get_db
from db.models import VideoBatch, CreatorSource, AppSettings, User
from services.balance_service import has_full_functionality_access
from typing import List
import logging
from datetime import datetime, timezone, timedelta
from api.auth import verify_user_header, ensure_user_scope
from services.app_settings_service import SOFTWARE_MODE_KEY, SOFTWARE_MODE_PARSING

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/video-batch", tags=["video_batch"])


def _as_utc_datetime(value: datetime | None) -> datetime | None:
    """SQLite may return timezone=True columns as naive datetimes."""
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


class BatchCreateRequest(BaseModel):
    """Модель для создания batch."""
    creator_id: int
    video_ids: List[str]


@router.post("/create")
async def create_batch(
    request: BatchCreateRequest,
    user_id: int = Query(..., description="ID пользователя, владельца batch"),
    db: AsyncSession = Depends(get_db),
    auth_user_id: int = Depends(verify_user_header),
) -> dict:
    """
    P0 FIX: Создает batch для хранения списка video_ids.
    Возвращает batch_id для использования в callback_data.
    """
    ensure_user_scope(user_id, auth_user_id)
    # Block in parsing-mode — batch creation leads to comment/order flows
    _mode_row = await db.execute(select(AppSettings.value).filter(AppSettings.key == SOFTWARE_MODE_KEY))
    if (_mode_row.scalar_one_or_none() or "").strip().lower() == SOFTWARE_MODE_PARSING:
        _user_row = await db.execute(select(User).filter(User.id == user_id))
        _user_obj = _user_row.scalar_one_or_none()
        if not (_user_obj and has_full_functionality_access(_user_obj)):
            raise HTTPException(
                status_code=403,
                detail="Software is in parsing mode: batch creation is disabled",
            )
    # Проверяем, что creator существует
    result = await db.execute(
        select(CreatorSource).filter(
            CreatorSource.id == request.creator_id,
            CreatorSource.user_id == user_id
        )
    )
    creator = result.scalar_one_or_none()
    if not creator:
        raise HTTPException(status_code=404, detail="Creator not found or access denied")
    
    # Keep notification buttons usable through weekends and short pauses.
    expires_at = datetime.now(timezone.utc) + timedelta(days=7)
    batch = VideoBatch(
        creator_id=request.creator_id,
        video_ids=request.video_ids,
        expires_at=expires_at
    )
    db.add(batch)
    await db.flush()  # Получаем ID
    await db.commit()
    
    logger.info(f"Created video batch {batch.id} for creator {request.creator_id} with {len(request.video_ids)} videos")
    
    return {
        "batch_id": batch.id,
        "creator_id": request.creator_id,
        "video_count": len(request.video_ids),
        "expires_at": _as_utc_datetime(batch.expires_at).isoformat() if batch.expires_at else None
    }


@router.get("/{batch_id}")
async def get_batch(
    batch_id: int,
    user_id: int = Query(..., description="ID пользователя, владельца batch"),
    db: AsyncSession = Depends(get_db),
    auth_user_id: int = Depends(verify_user_header),
) -> dict:
    """
    P0 FIX: Получает batch по ID и возвращает список video_ids.
    """
    ensure_user_scope(user_id, auth_user_id)
    result = await db.execute(
        select(VideoBatch)
        .join(CreatorSource, VideoBatch.creator_id == CreatorSource.id)
        .filter(
            VideoBatch.id == batch_id,
            CreatorSource.user_id == user_id
        )
    )
    batch = result.scalar_one_or_none()
    
    if not batch:
        raise HTTPException(status_code=404, detail="Batch not found or access denied")
    
    # Проверяем TTL. SQLite can strip tzinfo even for DateTime(timezone=True).
    expires_at = _as_utc_datetime(batch.expires_at)
    if expires_at and expires_at < datetime.now(timezone.utc):
        raise HTTPException(status_code=410, detail="Batch expired")
    
    created_at = _as_utc_datetime(batch.created_at)
    return {
        "batch_id": batch.id,
        "creator_id": batch.creator_id,
        "video_ids": batch.video_ids,
        "created_at": created_at.isoformat() if created_at else None,
        "expires_at": expires_at.isoformat() if expires_at else None
    }
