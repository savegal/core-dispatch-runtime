from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from api.auth import ensure_user_scope, verify_user_header
from db.database import get_db
from services.autopilot.actions import (
    archive_completed,
    archive_preview,
    cancel_all_pending,
    cancel_all_preview,
    cancel_planned_task,
    cancel_planned_video,
    hide_task,
    notify_admin,
    retry_task,
    retry_task_preview,
    skip_autopilot_queue,
    skip_queue_preview,
    stop_active_task,
    stop_work_task,
)

router = APIRouter(prefix="/api/autopilot/actions", tags=["autopilot"])


class ActionRequest(BaseModel):
    task_id: int | None = Field(None, ge=1)
    video_id: int | None = Field(None, ge=1)
    period: str = "24h"
    include_running: bool = False


@router.post("/cancel-all")
async def action_cancel_all(
    payload: ActionRequest,
    user_id: int = Query(..., description="Owner user ID"),
    db: AsyncSession = Depends(get_db),
    auth_user_id: int = Depends(verify_user_header),
) -> dict:
    ensure_user_scope(user_id, auth_user_id)
    return await cancel_all_pending(db, user_id, include_running=payload.include_running)


@router.get("/cancel-all/preview")
async def action_cancel_all_preview(
    user_id: int = Query(..., description="Owner user ID"),
    db: AsyncSession = Depends(get_db),
    auth_user_id: int = Depends(verify_user_header),
) -> dict:
    ensure_user_scope(user_id, auth_user_id)
    return await cancel_all_preview(db, user_id)


@router.post("/skip-queue")
async def action_skip_queue(
    user_id: int = Query(..., description="Owner user ID"),
    db: AsyncSession = Depends(get_db),
    auth_user_id: int = Depends(verify_user_header),
) -> dict:
    ensure_user_scope(user_id, auth_user_id)
    return await skip_autopilot_queue(db, user_id)


@router.get("/skip-queue/preview")
async def action_skip_queue_preview(
    user_id: int = Query(..., description="Owner user ID"),
    db: AsyncSession = Depends(get_db),
    auth_user_id: int = Depends(verify_user_header),
) -> dict:
    ensure_user_scope(user_id, auth_user_id)
    return await skip_queue_preview(db, user_id)


@router.post("/stop-active")
async def action_stop_active(
    user_id: int = Query(..., description="Owner user ID"),
    db: AsyncSession = Depends(get_db),
    auth_user_id: int = Depends(verify_user_header),
) -> dict:
    ensure_user_scope(user_id, auth_user_id)
    return await stop_active_task(db, user_id)


@router.post("/stop-task")
async def action_stop_task(
    payload: ActionRequest,
    user_id: int = Query(..., description="Owner user ID"),
    db: AsyncSession = Depends(get_db),
    auth_user_id: int = Depends(verify_user_header),
) -> dict:
    ensure_user_scope(user_id, auth_user_id)
    if payload.task_id is None:
        raise HTTPException(status_code=422, detail="task_id is required")
    return await stop_work_task(db, user_id, int(payload.task_id))


@router.post("/cancel-planned-task")
async def action_cancel_planned_task(
    payload: ActionRequest,
    user_id: int = Query(..., description="Owner user ID"),
    db: AsyncSession = Depends(get_db),
    auth_user_id: int = Depends(verify_user_header),
) -> dict:
    ensure_user_scope(user_id, auth_user_id)
    if payload.task_id is None:
        raise HTTPException(status_code=422, detail="task_id is required")
    return await cancel_planned_task(db, user_id, int(payload.task_id))


@router.post("/cancel-planned-video")
async def action_cancel_planned_video(
    payload: ActionRequest,
    user_id: int = Query(..., description="Owner user ID"),
    db: AsyncSession = Depends(get_db),
    auth_user_id: int = Depends(verify_user_header),
) -> dict:
    ensure_user_scope(user_id, auth_user_id)
    if payload.video_id is None:
        raise HTTPException(status_code=422, detail="video_id is required")
    return await cancel_planned_video(db, user_id, int(payload.video_id))


@router.post("/archive")
async def action_archive(
    payload: ActionRequest,
    user_id: int = Query(..., description="Owner user ID"),
    db: AsyncSession = Depends(get_db),
    auth_user_id: int = Depends(verify_user_header),
) -> dict:
    ensure_user_scope(user_id, auth_user_id)
    return await archive_completed(db, user_id, payload.period)


@router.get("/archive/preview")
async def action_archive_preview(
    user_id: int = Query(..., description="Owner user ID"),
    period: str = Query("24h"),
    db: AsyncSession = Depends(get_db),
    auth_user_id: int = Depends(verify_user_header),
) -> dict:
    ensure_user_scope(user_id, auth_user_id)
    return await archive_preview(db, user_id, period)


@router.post("/retry")
async def action_retry(
    payload: ActionRequest,
    user_id: int = Query(..., description="Owner user ID"),
    db: AsyncSession = Depends(get_db),
    auth_user_id: int = Depends(verify_user_header),
) -> dict:
    ensure_user_scope(user_id, auth_user_id)
    if payload.task_id is None:
        raise HTTPException(status_code=422, detail="task_id is required")
    return await retry_task(db, user_id, int(payload.task_id))


@router.get("/retry/preview")
async def action_retry_preview(
    user_id: int = Query(..., description="Owner user ID"),
    task_id: int = Query(..., ge=1),
    db: AsyncSession = Depends(get_db),
    auth_user_id: int = Depends(verify_user_header),
) -> dict:
    ensure_user_scope(user_id, auth_user_id)
    return await retry_task_preview(db, user_id, int(task_id))


@router.post("/hide")
async def action_hide(
    payload: ActionRequest,
    user_id: int = Query(..., description="Owner user ID"),
    db: AsyncSession = Depends(get_db),
    auth_user_id: int = Depends(verify_user_header),
) -> dict:
    ensure_user_scope(user_id, auth_user_id)
    if payload.task_id is None:
        raise HTTPException(status_code=422, detail="task_id is required")
    return await hide_task(db, user_id, int(payload.task_id))


@router.post("/notify-admin")
async def action_notify_admin(
    user_id: int = Query(..., description="Owner user ID"),
    db: AsyncSession = Depends(get_db),
    auth_user_id: int = Depends(verify_user_header),
) -> dict:
    ensure_user_scope(user_id, auth_user_id)
    return await notify_admin(db, user_id)
