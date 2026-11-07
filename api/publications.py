"""Owner-scoped publication progress and read-only result exports."""
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from api.auth import ensure_user_scope, verify_user_header
from db.database import get_db
from db.models import Task
from services.publication_reports import activity_snapshot, export_report, rolling_statistics

router = APIRouter(prefix="/api/publications", tags=["publications"])


async def owned_task(db, task_id, user_id, auth_user_id):
    ensure_user_scope(user_id, auth_user_id)
    task = (await db.execute(select(Task).where(Task.id == task_id, Task.user_id == user_id))).scalar_one_or_none()
    if task is None:
        raise HTTPException(404, "Task not found")
    return task


@router.get("/tasks/{task_id}/activity")
async def activity(task_id: int, user_id: int, include_texts: bool = False, db: AsyncSession = Depends(get_db),
                   auth_user_id: int = Depends(verify_user_header)):
    task = await owned_task(db, task_id, user_id, auth_user_id)
    return await db.run_sync(lambda sync: activity_snapshot(sync, task, include_texts=include_texts))


@router.get("/tasks/{task_id}/report")
async def report(task_id: int, user_id: int, format: Literal["txt", "csv", "links"] = "txt",
                 db: AsyncSession = Depends(get_db), auth_user_id: int = Depends(verify_user_header)):
    task = await owned_task(db, task_id, user_id, auth_user_id)
    content = await db.run_sync(lambda sync: export_report(sync, task, format))
    suffix = "csv" if format == "csv" else "txt"
    return dict(filename=f"{'links' if format == 'links' else 'report'}-{task_id}.{suffix}", content=content)


@router.get("/statistics")
async def statistics(user_id: int, hours: int = Query(24, ge=1, le=168),
                     db: AsyncSession = Depends(get_db), auth_user_id: int = Depends(verify_user_header)):
    ensure_user_scope(user_id, auth_user_id)
    return await db.run_sync(lambda sync: rolling_statistics(sync, user_id, hours=hours))


@router.post("/tasks/{task_id}/recheck")
async def recheck(task_id: int, user_id: int, db: AsyncSession = Depends(get_db),
                  auth_user_id: int = Depends(verify_user_header)):
    task = await owned_task(db, task_id, user_id, auth_user_id)
    if task.status in {"pending", "in_progress", "running", "queued"}:
        raise HTTPException(409, "Дождитесь завершения отправки публикаций")
    from services.publication_recheck import request_recheck
    try:
        result = await db.run_sync(lambda sync: request_recheck(sync, task.id))
    except ValueError as exc:
        raise HTTPException(409, "Исчерпан лимит трафика заказа для перепроверок") from exc
    await db.commit()
    return result
