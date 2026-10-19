from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, field_validator
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from db.database import get_db
from db.models import TaskTemplate, CommentTemplate
from typing import List
import logging
from config.limiter import limiter
from config.settings import get_settings
from api.auth import verify_user_header, ensure_user_scope

router = APIRouter(prefix="/api/templates", tags=["templates"])
logger = logging.getLogger(__name__)
settings = get_settings()


def _preserve_text(value: str | None) -> str | None:
    if value is None:
        return None
    return str(value).replace("\r\n", "\n").replace("\r", "\n")


def _meaningful_text(value: str | None) -> str | None:
    preserved = _preserve_text(value)
    if preserved is None or not preserved.strip():
        return None
    return preserved


def _normalize_text_list(values: list[str] | None) -> list[str]:
    normalized: list[str] = []
    for value in values or []:
        text = _meaningful_text(value)
        if text:
            normalized.append(text)
    return normalized


def _normalize_reply_list(values: list | tuple | None) -> list[str]:
    replies: list[str] = []
    for value in values or []:
        text = _meaningful_text(value)
        if not text:
            continue
        if len(text) > settings.MAX_REPLY_LENGTH:
            raise HTTPException(
                status_code=422,
                detail=f"Reply too long (max {settings.MAX_REPLY_LENGTH} characters)",
            )
        replies.append(text)
        if len(replies) >= settings.MAX_REPLIES:
            break
    return replies


def _normalize_template_reply_payload(
    raw_settings: dict | None,
    top_level_replies: list[str | None],
    *,
    prefer_settings_replies: bool = True,
) -> tuple[dict, list[str]]:
    payload = dict(raw_settings or {})
    settings_replies = payload.get("replies")
    if prefer_settings_replies and isinstance(settings_replies, list) and settings_replies:
        raw_replies = settings_replies
    else:
        raw_replies = [
            *top_level_replies,
            *[
                payload.get(f"reply{idx}")
                for idx in range(4, settings.MAX_REPLIES + 1)
            ],
        ]

    replies = _normalize_reply_list(raw_replies)
    requested_reply_count = payload.get("reply_count")
    if isinstance(requested_reply_count, int) and requested_reply_count >= 0:
        reply_count = min(requested_reply_count, len(replies), settings.MAX_REPLIES)
    else:
        reply_count = len(replies)

    for idx in range(4, settings.MAX_REPLIES + 1):
        payload.pop(f"reply{idx}", None)
    payload["replies"] = replies
    payload["reply_count"] = reply_count
    for idx, reply_text in enumerate(replies[3:], start=4):
        payload[f"reply{idx}"] = reply_text
    return payload, replies


def _extract_template_replies(template: TaskTemplate) -> list[str]:
    settings_payload = template.settings or {}
    replies = _normalize_text_list(settings_payload.get("replies"))
    if replies:
        return replies[: settings.MAX_REPLIES]

    fallback: list[str] = []
    for idx in range(1, settings.MAX_REPLIES + 1):
        value = getattr(template, f"reply{idx}", None) if idx <= 3 else settings_payload.get(f"reply{idx}")
        text = _meaningful_text(value)
        if text:
            fallback.append(text)
    return fallback


def _extract_template_variants(template: TaskTemplate) -> list[str]:
    settings_payload = template.settings or {}
    variants = _normalize_text_list(settings_payload.get("text_variants"))
    if variants:
        while len(variants) < 3:
            variants.append(variants[-1])
        return variants

    main_comment = _meaningful_text(template.main_comment)
    if not main_comment:
        return []
    return [main_comment] * 3


def _is_archived_template(template: TaskTemplate) -> bool:
    """Return whether a task template is intentionally hidden from normal lists."""
    settings_payload = template.settings or {}
    return bool(settings_payload.get("archived"))


class TaskTemplateCreate(BaseModel):
    """Модель для создания шаблона задачи."""
    user_id: int
    name: str
    main_comment: str
    reply1: str | None = None
    reply2: str | None = None
    reply3: str | None = None
    likes: int = 0
    settings: dict | None = None
    
    # ИСПРАВЛЕНО: Валидация входных данных
    @field_validator('name')
    @classmethod
    def validate_name(cls, v: str) -> str:
        """Валидирует имя шаблона."""
        if not v or not v.strip():
            raise ValueError('name cannot be empty')
        if len(v) > 100:
            raise ValueError('name too long (max 100 characters)')
        return v.strip()
    
    @field_validator('main_comment')
    @classmethod
    def validate_main_comment(cls, v: str) -> str:
        """Валидирует основной комментарий."""
        preserved = _meaningful_text(v)
        if not preserved:
            raise ValueError('main_comment cannot be empty')
        if len(preserved) > settings.MAX_COMMENT_LENGTH:
            raise ValueError(f'main_comment too long (max {settings.MAX_COMMENT_LENGTH} characters)')
        return preserved
    
    @field_validator('reply1', 'reply2', 'reply3')
    @classmethod
    def validate_replies(cls, v: str | None) -> str | None:
        """Валидирует ответы."""
        preserved = _meaningful_text(v)
        if preserved is None:
            return None
        if len(preserved) > settings.MAX_REPLY_LENGTH:
            raise ValueError(f'Reply too long (max {settings.MAX_REPLY_LENGTH} characters)')
        return preserved

    @field_validator('likes')
    @classmethod
    def validate_likes(cls, v: int) -> int:
        if v < 0:
            raise ValueError('likes cannot be negative')
        return v


class TaskTemplateResponse(BaseModel):
    id: int
    user_id: int
    name: str
    main_comment: str
    reply1: str | None
    reply2: str | None
    reply3: str | None
    likes: int
    settings: dict | None
    created_at: str

    @classmethod
    def from_orm(cls, template: TaskTemplate):
        return cls(
            id=template.id,
            user_id=template.user_id,
            name=template.name,
            main_comment=template.main_comment,
            reply1=template.reply1,
            reply2=template.reply2,
            reply3=template.reply3,
            likes=template.likes,
            settings=template.settings,
            created_at=template.created_at.isoformat() if template.created_at else ""
        )


class CommentTemplateCreate(BaseModel):
    user_id: int
    name: str
    text: str
    type: str  # main | reply1 | reply2 | reply3


class CommentTemplateResponse(BaseModel):
    id: int
    user_id: int
    name: str
    text: str
    type: str
    created_at: str

    @classmethod
    def from_orm(cls, template: CommentTemplate):
        return cls(
            id=template.id,
            user_id=template.user_id,
            name=template.name,
            text=template.text,
            type=template.type,
            created_at=template.created_at.isoformat() if template.created_at else ""
        )


@router.post("/task", response_model=TaskTemplateResponse)
@limiter.limit("30/minute")  # ИСПРАВЛЕНО: Rate limiting
async def create_task_template(
    request: Request,
    template_data: TaskTemplateCreate,
    db: AsyncSession = Depends(get_db),
    auth_user_id: int = Depends(verify_user_header),
):
    """
    Создаёт шаблон задачи.
    
    ИСПРАВЛЕНО: Переведено на async для соответствия async БД.
    """
    try:
        ensure_user_scope(template_data.user_id, auth_user_id)
        template_settings, template_replies = _normalize_template_reply_payload(
            template_data.settings,
            [template_data.reply1, template_data.reply2, template_data.reply3],
        )
        template = TaskTemplate(
            user_id=template_data.user_id,
            name=template_data.name,
            main_comment=template_data.main_comment,
            reply1=template_replies[0] if len(template_replies) >= 1 else None,
            reply2=template_replies[1] if len(template_replies) >= 2 else None,
            reply3=template_replies[2] if len(template_replies) >= 3 else None,
            likes=template_data.likes,
            settings=template_settings,
        )
        db.add(template)
        await db.commit()
        await db.refresh(template)
        logger.info(f"Created task template {template.id} for user {template_data.user_id}")
        return TaskTemplateResponse.from_orm(template)
    except HTTPException:
        raise
    except Exception as e:
        await db.rollback()
        logger.error(f"Error creating task template: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Database error")


@router.get("/task", response_model=List[TaskTemplateResponse])
@limiter.limit("60/minute")  # ИСПРАВЛЕНО: Rate limiting
async def list_task_templates(
    request: Request,
    user_id: int = Query(...),
    include_archived: bool = Query(False),
    db: AsyncSession = Depends(get_db),
    auth_user_id: int = Depends(verify_user_header),
):
    """Получает список шаблонов задач пользователя."""
    try:
        ensure_user_scope(user_id, auth_user_id)
        result = await db.execute(
            select(TaskTemplate).filter(
                TaskTemplate.user_id == user_id
            ).order_by(TaskTemplate.created_at.desc())
        )
        templates = result.scalars().all()
        if not include_archived:
            templates = [template for template in templates if not _is_archived_template(template)]
        return [TaskTemplateResponse.from_orm(t) for t in templates]
    except Exception as e:
        logger.error(f"Error listing task templates for user {user_id}: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Database error")


@router.post("/comment", response_model=CommentTemplateResponse)
@limiter.limit("30/minute")  # ИСПРАВЛЕНО: Rate limiting
async def create_comment_template(
    request: Request,
    template_data: CommentTemplateCreate,
    db: AsyncSession = Depends(get_db),
    auth_user_id: int = Depends(verify_user_header),
):
    """
    Создаёт шаблон комментария.
    
    ИСПРАВЛЕНО: Переведено на async для соответствия async БД.
    """
    try:
        ensure_user_scope(template_data.user_id, auth_user_id)
        template = CommentTemplate(
            user_id=template_data.user_id,
            name=template_data.name,
            text=template_data.text,
            type=template_data.type
        )
        db.add(template)
        await db.commit()
        await db.refresh(template)
        logger.info(f"Created comment template {template.id} for user {template_data.user_id}")
        return CommentTemplateResponse.from_orm(template)
    except Exception as e:
        await db.rollback()
        logger.error(f"Error creating comment template: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Database error")


@router.get("/comment", response_model=List[CommentTemplateResponse])
@limiter.limit("60/minute")  # ИСПРАВЛЕНО: Rate limiting
async def list_comment_templates(
    request: Request,
    user_id: int = Query(...),
    type: str | None = Query(None),
    db: AsyncSession = Depends(get_db),
    auth_user_id: int = Depends(verify_user_header),
):
    """
    Получает список шаблонов комментариев пользователя.
    
    ИСПРАВЛЕНО: Переведено на async для соответствия async БД.
    """
    try:
        ensure_user_scope(user_id, auth_user_id)
        query = select(CommentTemplate).filter(CommentTemplate.user_id == user_id)
        if type:
            query = query.filter(CommentTemplate.type == type)
        query = query.order_by(CommentTemplate.created_at.desc())
        result = await db.execute(query)
        templates = result.scalars().all()
        return [CommentTemplateResponse.from_orm(t) for t in templates]
    except Exception as e:
        logger.error(f"Error listing comment templates for user {user_id}: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Database error")


class TaskTemplateUpdate(BaseModel):
    name: str | None = None
    main_comment: str | None = None
    reply1: str | None = None
    reply2: str | None = None
    reply3: str | None = None
    likes: int | None = None
    settings: dict | None = None

    @field_validator('name')
    @classmethod
    def validate_name(cls, v: str | None) -> str | None:
        if v is None:
            return None
        if not v.strip():
            raise ValueError('name cannot be empty')
        if len(v) > 100:
            raise ValueError('name too long (max 100 characters)')
        return v.strip()

    @field_validator('main_comment')
    @classmethod
    def validate_main_comment(cls, v: str | None) -> str | None:
        if v is None:
            return None
        preserved = _meaningful_text(v)
        if not preserved:
            raise ValueError('main_comment cannot be empty')
        if len(preserved) > settings.MAX_COMMENT_LENGTH:
            raise ValueError(f'main_comment too long (max {settings.MAX_COMMENT_LENGTH} characters)')
        return preserved

    @field_validator('reply1', 'reply2', 'reply3')
    @classmethod
    def validate_replies(cls, v: str | None) -> str | None:
        if v is None:
            return None
        preserved = _meaningful_text(v)
        if not preserved:
            return None
        if len(preserved) > settings.MAX_REPLY_LENGTH:
            raise ValueError(f'Reply too long (max {settings.MAX_REPLY_LENGTH} characters)')
        return preserved

    @field_validator('likes')
    @classmethod
    def validate_likes(cls, v: int | None) -> int | None:
        if v is None:
            return None
        if v < 0:
            raise ValueError('likes cannot be negative')
        return v


@router.get("/task/{template_id}", response_model=TaskTemplateResponse)
async def get_task_template(
    template_id: int,
    user_id: int = Query(...),
    db: AsyncSession = Depends(get_db),
    auth_user_id: int = Depends(verify_user_header),
):
    """
    Получает шаблон задачи по ID.
    
    ИСПРАВЛЕНО: Переведено на async для соответствия async БД.
    """
    try:
        ensure_user_scope(user_id, auth_user_id)
        result = await db.execute(
            select(TaskTemplate).filter(
                TaskTemplate.id == template_id,
                TaskTemplate.user_id == user_id
            )
        )
        template = result.scalar_one_or_none()
        if not template:
            raise HTTPException(status_code=404, detail="Template not found")
        return TaskTemplateResponse.from_orm(template)
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error getting task template {template_id}: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Database error")


@router.put("/task/{template_id}", response_model=TaskTemplateResponse)
async def update_task_template(
    template_id: int,
    template_data: TaskTemplateUpdate,
    user_id: int = Query(...),
    db: AsyncSession = Depends(get_db),
    auth_user_id: int = Depends(verify_user_header),
):
    """
    Обновляет шаблон задачи.
    
    ИСПРАВЛЕНО: Переведено на async для соответствия async БД.
    """
    try:
        ensure_user_scope(user_id, auth_user_id)
        result = await db.execute(
            select(TaskTemplate).filter(
                TaskTemplate.id == template_id,
                TaskTemplate.user_id == user_id
            )
        )
        template = result.scalar_one_or_none()
        if not template:
            raise HTTPException(status_code=404, detail="Template not found")
        
        # Различаем "поле не передано" и "передано null".
        # Это позволяет удалять reply1..reply3 через null.
        fields_set = template_data.model_fields_set

        if "name" in fields_set:
            if template_data.name is None:
                raise HTTPException(status_code=422, detail="name cannot be null")
            template.name = template_data.name
        if "main_comment" in fields_set:
            if template_data.main_comment is None:
                raise HTTPException(status_code=422, detail="main_comment cannot be null")
            template.main_comment = template_data.main_comment
        if "reply1" in fields_set:
            template.reply1 = template_data.reply1
        if "reply2" in fields_set:
            template.reply2 = template_data.reply2
        if "reply3" in fields_set:
            template.reply3 = template_data.reply3
        if "likes" in fields_set:
            if template_data.likes is None:
                raise HTTPException(status_code=422, detail="likes cannot be null")
            template.likes = template_data.likes
        if "settings" in fields_set:
            template.settings = template_data.settings

        if {"settings", "reply1", "reply2", "reply3"} & fields_set:
            template.settings, template_replies = _normalize_template_reply_payload(
                template.settings,
                [template.reply1, template.reply2, template.reply3],
                prefer_settings_replies="settings" in fields_set,
            )
            template.reply1 = template_replies[0] if len(template_replies) >= 1 else None
            template.reply2 = template_replies[1] if len(template_replies) >= 2 else None
            template.reply3 = template_replies[2] if len(template_replies) >= 3 else None
        
        await db.commit()
        await db.refresh(template)
        logger.info(f"Updated task template {template_id}")
        return TaskTemplateResponse.from_orm(template)
    except HTTPException:
        raise
    except Exception as e:
        await db.rollback()
        logger.error(f"Error updating task template {template_id}: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Database error")


@router.delete("/task/{template_id}", response_model=dict)
async def delete_task_template(
    template_id: int,
    user_id: int = Query(...),
    db: AsyncSession = Depends(get_db),
    auth_user_id: int = Depends(verify_user_header),
):
    """
    Удаляет шаблон задачи.
    
    ИСПРАВЛЕНО: Переведено на async для соответствия async БД.
    """
    try:
        ensure_user_scope(user_id, auth_user_id)
        result = await db.execute(
            select(TaskTemplate).filter(
                TaskTemplate.id == template_id,
                TaskTemplate.user_id == user_id
            )
        )
        template = result.scalar_one_or_none()
        if not template:
            raise HTTPException(status_code=404, detail="Template not found")
        
        await db.delete(template)
        await db.commit()
        logger.info(f"Deleted task template {template_id}")
        return {"deleted": True, "id": template_id}
    except HTTPException:
        raise
    except Exception as e:
        await db.rollback()
        logger.error(f"Error deleting task template {template_id}: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Database error")
