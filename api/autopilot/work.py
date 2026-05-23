from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from api.auth import ensure_user_scope, verify_user_header
from db.database import get_db
from services.autopilot.work import collect_work, get_planned_task_preview, get_planned_video_preview

router = APIRouter(prefix="/api/autopilot", tags=["autopilot"])


@router.get("/work")
async def get_work(
    user_id: int = Query(..., description="Owner user ID"),
    tab: str = Query("plan"),
    page: int = Query(0, ge=0),
    limit: int = Query(10, ge=1, le=20),
    db: AsyncSession = Depends(get_db),
    auth_user_id: int = Depends(verify_user_header),
) -> dict:
    ensure_user_scope(user_id, auth_user_id)
    return await collect_work(db, user_id, tab=tab, page=page, limit=limit)


@router.get("/work/planned-video/{video_id}")
async def get_work_planned_video(
    video_id: int,
    user_id: int = Query(..., description="Owner user ID"),
    db: AsyncSession = Depends(get_db),
    auth_user_id: int = Depends(verify_user_header),
) -> dict:
    ensure_user_scope(user_id, auth_user_id)
    return await get_planned_video_preview(db, user_id, video_id)


@router.get("/work/planned-task/{task_id}")
async def get_work_planned_task(
    task_id: int,
    user_id: int = Query(..., description="Owner user ID"),
    db: AsyncSession = Depends(get_db),
    auth_user_id: int = Depends(verify_user_header),
) -> dict:
    ensure_user_scope(user_id, auth_user_id)
    return await get_planned_task_preview(db, user_id, task_id)
