from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession

from api.auth import ensure_user_scope, verify_user_header
from db.database import get_db
from services.autopilot.report import collect_finance, collect_summary, get_task_card, list_by_creators, list_tasks

router = APIRouter(prefix="/api/autopilot/report", tags=["autopilot"])


@router.get("/summary")
async def report_summary(
    user_id: int = Query(..., description="Owner user ID"),
    period: str = Query("24h"),
    db: AsyncSession = Depends(get_db),
    auth_user_id: int = Depends(verify_user_header),
) -> dict:
    ensure_user_scope(user_id, auth_user_id)
    if not isinstance(period, str):
        period = "24h"
    return await collect_summary(db, user_id, period)


@router.get("/tasks")
async def report_tasks(
    user_id: int = Query(..., description="Owner user ID"),
    period: str = Query("24h"),
    status: str = Query("all"),
    page: int = Query(0, ge=0),
    limit: int = Query(5, ge=1, le=20),
    creator_id: int | None = Query(None, ge=1),
    db: AsyncSession = Depends(get_db),
    auth_user_id: int = Depends(verify_user_header),
) -> dict:
    ensure_user_scope(user_id, auth_user_id)
    if not isinstance(period, str):
        period = "24h"
    if not isinstance(status, str):
        status = "all"
    if not isinstance(page, int):
        page = 0
    if not isinstance(limit, int):
        limit = 5
    if not isinstance(creator_id, int):
        creator_id = None
    return await list_tasks(db, user_id, period, status=status, page=page, limit=limit, creator_id=creator_id)


@router.get("/task/{task_id}")
async def report_task(
    task_id: int,
    user_id: int = Query(..., description="Owner user ID"),
    db: AsyncSession = Depends(get_db),
    auth_user_id: int = Depends(verify_user_header),
) -> dict:
    ensure_user_scope(user_id, auth_user_id)
    payload = await get_task_card(db, user_id, task_id)
    if payload is None:
        raise HTTPException(status_code=404, detail="Task not found")
    return payload


@router.get("/creators")
async def report_creators(
    user_id: int = Query(..., description="Owner user ID"),
    period: str = Query("24h"),
    page: int = Query(0, ge=0),
    limit: int = Query(5, ge=1, le=20),
    db: AsyncSession = Depends(get_db),
    auth_user_id: int = Depends(verify_user_header),
) -> dict:
    ensure_user_scope(user_id, auth_user_id)
    if not isinstance(period, str):
        period = "24h"
    if not isinstance(page, int):
        page = 0
    if not isinstance(limit, int):
        limit = 5
    return await list_by_creators(db, user_id, period, page=page, limit=limit)


@router.get("/finance")
async def report_finance(
    user_id: int = Query(..., description="Owner user ID"),
    period: str = Query("24h"),
    db: AsyncSession = Depends(get_db),
    auth_user_id: int = Depends(verify_user_header),
) -> dict:
    ensure_user_scope(user_id, auth_user_id)
    if not isinstance(period, str):
        period = "24h"
    return await collect_finance(db, user_id, period)
