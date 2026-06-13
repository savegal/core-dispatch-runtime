from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, field_validator
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from db.database import get_db
from db.models import AppSettings, Task, Log, User
from services.app_settings_service import SOFTWARE_MODE_KEY, SOFTWARE_MODE_PARSING
from services.balance_service import has_full_functionality_access
from services.task_service import dequeue_task, requeue_deferred_task, task_queue_priority_level
from api.auth import verify_executor_header
from datetime import datetime, timezone
import logging
import asyncio

logger = logging.getLogger("api.executor_api")

router = APIRouter(prefix="/api/executor", tags=["executor"])


class UpdateTaskRequest(BaseModel):
    task_id: int
    status: str | None = None
    progress: int | None = None
    error_reason: str | None = None

    @field_validator("status")
    @classmethod
    def normalize_status(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip().lower()
        return normalized or None

    @field_validator("progress")
    @classmethod
    def validate_progress_non_negative(cls, value: int | None) -> int | None:
        if value is None:
            return None
        if value < 0:
            raise ValueError("progress must be >= 0")
        return value


class LogRequest(BaseModel):
    task_id: int
    account_id: int | None = None
    message: str




@router.get("/next_task")
async def next_task(
    timeout: int = 5,
    _: None = Depends(verify_executor_header),
    db: AsyncSession = Depends(get_db)
):
    """
    ÐŸÐ¾Ð»ÑƒÑ‡Ð°ÐµÑ‚ ÑÐ»ÐµÐ´ÑƒÑŽÑ‰ÑƒÑŽ Ð·Ð°Ð´Ð°Ñ‡Ñƒ Ð¸Ð· Ð¾Ñ‡ÐµÑ€ÐµÐ´Ð¸.
    
    Ð˜Ð¡ÐŸÐ ÐÐ’Ð›Ð•ÐÐž: ÐŸÐµÑ€ÐµÐ²ÐµÐ´ÐµÐ½Ð¾ Ð½Ð° async Ð´Ð»Ñ ÑÐ¾Ð¾Ñ‚Ð²ÐµÑ‚ÑÑ‚Ð²Ð¸Ñ async Ð‘Ð”.
    """
    # ÐŸÑ‹Ñ‚Ð°ÐµÐ¼ÑÑ Ð¿Ñ€Ð¾Ð¿ÑƒÑÑ‚Ð¸Ñ‚ÑŒ "Ð¿Ñ€Ð¾Ñ‚ÑƒÑ…ÑˆÐ¸Ðµ" ÑÐ»ÐµÐ¼ÐµÐ½Ñ‚Ñ‹ Ð¾Ñ‡ÐµÑ€ÐµÐ´Ð¸ (Ð½Ðµ pending / ÑƒÐ´Ð°Ð»ÐµÐ½Ð½Ð°Ñ Ð·Ð°Ð´Ð°Ñ‡Ð°),
    # Ñ‡Ñ‚Ð¾Ð±Ñ‹ Ð½Ðµ Ñ‚ÐµÑ€ÑÑ‚ÑŒ Ð¿Ð¾Ð»ÐµÐ·Ð½Ñ‹Ðµ Ð·Ð°Ð´Ð°Ñ‡Ð¸ Ð·Ð° Ð½Ð¸Ð¼Ð¸.
    max_dequeue_attempts = 5
    for attempt in range(max_dequeue_attempts):
        current_timeout = timeout if attempt == 0 else 0
        # dequeue_task Ð¸ÑÐ¿Ð¾Ð»ÑŒÐ·ÑƒÐµÑ‚ Ð±Ð»Ð¾ÐºÐ¸Ñ€ÑƒÑŽÑ‰Ð¸Ð¹ Redis BRPOP, Ð²Ñ‹Ð½Ð¾ÑÐ¸Ð¼ Ð² thread
        # Ñ‡Ñ‚Ð¾Ð±Ñ‹ Ð½Ðµ Ð±Ð»Ð¾ÐºÐ¸Ñ€Ð¾Ð²Ð°Ñ‚ÑŒ event loop FastAPI.
        task_id = await asyncio.to_thread(dequeue_task, timeout=current_timeout)
        if not task_id:
            return {"task_id": None}

        try:
            result = await db.execute(
                select(Task).filter(Task.id == task_id).with_for_update()
            )
        except Exception as lock_error:
            logger.warning(
                f"SELECT FOR UPDATE not supported in next_task, "
                f"falling back to regular query: {lock_error}"
            )
            result = await db.execute(select(Task).filter(Task.id == task_id))

        task = result.scalar_one_or_none()
        if not task:
            await db.rollback()
            continue

        if task.status != "pending":
            await db.rollback()
            continue

        mode_result = await db.execute(
            select(AppSettings.value).filter(AppSettings.key == SOFTWARE_MODE_KEY)
        )
        current_mode = (mode_result.scalar_one_or_none() or "").strip().lower()
        if current_mode == SOFTWARE_MODE_PARSING:
            user_result = await db.execute(select(User).filter(User.id == task.user_id))
            user = user_result.scalar_one_or_none()
            if not (user and has_full_functionality_access(user)):
                await db.rollback()
                await asyncio.to_thread(
                    requeue_deferred_task,
                    task_id,
                    priority_level=task_queue_priority_level(task),
                )
                logger.info(
                    "Deferred restricted task %s in /api/executor/next_task: software_mode=parsing",
                    task_id,
                )
                continue

        from services.task_state_machine import TaskStateTransitionError, validate_status_transition

        try:
            validate_status_transition(task.status, "in_progress")
            task.status = "in_progress"
            task.attempts += 1
            task.updated_at = datetime.now(timezone.utc)
            await db.commit()
            await db.refresh(task)
        except TaskStateTransitionError as e:
            await db.rollback()
            logger.warning(
                f"Task {task_id}: invalid status transition in next_task "
                f"{task.status} -> in_progress: {e}"
            )
            continue
        except Exception as e:
            await db.rollback()
            logger.error(f"Task {task_id}: failed to claim in next_task: {e}", exc_info=True)
            return {"task_id": None}

        return {
            "task_id": task.id,
            "user_id": task.user_id,
            "main_comment": task.main_comment,
            "reply1": task.reply1,
            "reply2": task.reply2,
            "reply3": task.reply3,
            "likes": task.likes,
            "video_urls": task.video_urls or [],
            "settings": task.settings or {}
        }

    return {"task_id": None}


@router.post("/update_task")
async def update_task(
    request: UpdateTaskRequest,
    _: None = Depends(verify_executor_header),
    db: AsyncSession = Depends(get_db)
):
    """
    ÐžÐ±Ð½Ð¾Ð²Ð»ÑÐµÑ‚ ÑÑ‚Ð°Ñ‚ÑƒÑ Ð·Ð°Ð´Ð°Ñ‡Ð¸.
    
    Ð˜Ð¡ÐŸÐ ÐÐ’Ð›Ð•ÐÐž: ÐŸÐµÑ€ÐµÐ²ÐµÐ´ÐµÐ½Ð¾ Ð½Ð° async Ð´Ð»Ñ ÑÐ¾Ð¾Ñ‚Ð²ÐµÑ‚ÑÑ‚Ð²Ð¸Ñ async Ð‘Ð”.
    Ð˜Ð¡ÐŸÐ ÐÐ’Ð›Ð•ÐÐž: Ð˜ÑÐ¿Ð¾Ð»ÑŒÐ·ÑƒÐµÐ¼ SELECT FOR UPDATE Ð´Ð»Ñ Ð¿Ñ€ÐµÐ´Ð¾Ñ‚Ð²Ñ€Ð°Ñ‰ÐµÐ½Ð¸Ñ race conditions.
    """
    # Ð˜Ð¡ÐŸÐ ÐÐ’Ð›Ð•ÐÐž: Ð˜ÑÐ¿Ð¾Ð»ÑŒÐ·ÑƒÐµÐ¼ SELECT FOR UPDATE Ð´Ð»Ñ Ð°Ñ‚Ð¾Ð¼Ð°Ñ€Ð½Ð¾Ð³Ð¾ Ð¾Ð±Ð½Ð¾Ð²Ð»ÐµÐ½Ð¸Ñ
    try:
        result = await db.execute(select(Task).filter(Task.id == request.task_id).with_for_update())
    except Exception as lock_error:
        # Ð•ÑÐ»Ð¸ Ð±Ð»Ð¾ÐºÐ¸Ñ€Ð¾Ð²ÐºÐ° Ð½Ðµ Ð¿Ð¾Ð´Ð´ÐµÑ€Ð¶Ð¸Ð²Ð°ÐµÑ‚ÑÑ, Ð¸ÑÐ¿Ð¾Ð»ÑŒÐ·ÑƒÐµÐ¼ Ð¾Ð±Ñ‹Ñ‡Ð½Ñ‹Ð¹ Ð·Ð°Ð¿Ñ€Ð¾Ñ
        logger.warning(f"SELECT FOR UPDATE not supported, falling back to regular query: {lock_error}")
        result = await db.execute(select(Task).filter(Task.id == request.task_id))
    
    task = result.scalar_one_or_none()
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")

    if str(task.status or "").lower() == "stopped":
        logger.info("Ignoring executor update for stopped task %s", task.id)
        return {"success": True, "ignored": True, "status": "stopped"}

    if request.status:
        # Validate status transition through state machine.
        from services.task_state_machine import validate_status_transition
        try:
            validate_status_transition(task.status, request.status)
        except Exception as e:
            logger.warning(f"Invalid status transition for task {task.id}: {task.status} -> {request.status}, error: {e}")
            raise HTTPException(
                status_code=400,
                detail=f"Invalid status transition: {task.status} -> {request.status}. {str(e)}"
            )

    current_progress = int(task.progress or 0)
    total_urls = len(task.video_urls or [])
    max_progress = total_urls if total_urls > 0 else 0

    if request.progress is not None:
        if request.progress > max_progress:
            raise HTTPException(
                status_code=400,
                detail=f"Invalid progress: {request.progress}. Expected range: 0..{max_progress}",
            )
        if request.progress < current_progress:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"Progress regression is not allowed: "
                    f"current={current_progress}, incoming={request.progress}"
                ),
            )
        task.progress = request.progress

    effective_progress = int(task.progress or 0)
    if request.status == "done" and total_urls > 0 and effective_progress < total_urls:
        raise HTTPException(
            status_code=400,
            detail=f"Cannot set status=done before full progress: {effective_progress}/{total_urls}",
        )

    if request.status:
        task.status = request.status
    if request.error_reason is not None:
        task.error_reason = request.error_reason

    task.updated_at = datetime.now(timezone.utc)
    
    try:
        await db.commit()
    except Exception as e:
        await db.rollback()
        logger.error(f"Error updating task {request.task_id}: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Database error: {str(e)[:200]}")

    return {"success": True}


@router.post("/log")
async def log(
    request: LogRequest,
    _: None = Depends(verify_executor_header),
    db: AsyncSession = Depends(get_db)
):
    """
    Ð¡Ð¾Ð·Ð´Ð°Ñ‘Ñ‚ Ð·Ð°Ð¿Ð¸ÑÑŒ Ð² Ð»Ð¾Ð³Ðµ.
    
    Ð˜Ð¡ÐŸÐ ÐÐ’Ð›Ð•ÐÐž: ÐŸÐµÑ€ÐµÐ²ÐµÐ´ÐµÐ½Ð¾ Ð½Ð° async Ð´Ð»Ñ ÑÐ¾Ð¾Ñ‚Ð²ÐµÑ‚ÑÑ‚Ð²Ð¸Ñ async Ð‘Ð”.
    """
    log_entry = Log(
        task_id=request.task_id,
        account_id=request.account_id,
        message=request.message
    )
    db.add(log_entry)
    
    try:
        await db.commit()
    except Exception as e:
        await db.rollback()
        logger.error(f"Error creating log entry: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Database error: {str(e)[:200]}")
    
    return {"success": True}

