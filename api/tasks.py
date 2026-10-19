from fastapi import APIRouter, Depends, HTTPException, Query, Request, Header
from pydantic import BaseModel, TypeAdapter, field_validator, model_validator
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import delete, select
from db.database import get_db
from db.models import CreatorSource, CreatorVideo, PendingEnqueue, Task, Transaction, User, AppSettings
from db.constants import TaskStatus
from services.task_service import (
    claim_dedup_key,
    finalize_reserved_enqueue_async,
    promote_task_to_high_priority,
    remove_tasks_from_queue,
    reserve_task_for_enqueue_async,
)
from services.task_stop_signal import request_task_stop
from services.balance_service import (
    get_pricing_async, calculate_task_cost, subtract_balance_async, 
    has_full_functionality_access, get_user_balance_async
)
from services.app_settings_service import (
    SOFTWARE_MODE_KEY,
    SOFTWARE_MODE_PARSING,
)
from config.settings import get_settings
from typing import List
from datetime import datetime, timezone, timedelta
from decimal import Decimal
import json
import logging
import asyncio
from config.limiter import limiter
from api.auth import verify_user_header, verify_admin_header, ensure_user_scope
from services.task_creation_service import (
    get_boost_likes_config_async,
    get_boost_likes_target_mode_async,
    get_order_execution_strategy_async,
)
from services.user_facing_errors import build_task_display_reason, build_task_execution_summary, collect_task_warnings
from services.likes_config import (
    BOOST_LIKES_DEFAULT,
    BOOST_LIKES_ENABLED_KEY,
    BOOST_LIKES_MAX,
    BOOST_LIKES_QUANTITY_KEY,
    BOOST_LIKES_TARGET_SINGLE_TOP,
    coerce_likes_target_mode,
    coerce_bool,
    coerce_likes_quantity,
    provider_available as likes_provider_available,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/tasks", tags=["tasks"])
settings = get_settings()
ACCOUNT_POOL_MODE_SHARED = "shared"
ACCOUNT_POOL_MODE_PERSONAL = "personal"


def _settings_dict(task: Task) -> dict:
    return dict(task.settings or {}) if isinstance(task.settings, dict) else {}


def _money(value) -> Decimal:
    try:
        return Decimal(str(value or "0.00"))
    except Exception:
        return Decimal("0.00")


def _remaining_task_refund(task: Task) -> Decimal:
    return max(_money(task.total_cost) - _money(task.refunded_cost), Decimal("0.00"))


def _mark_creator_video_released(video: CreatorVideo, *, task_id: int, now: datetime) -> None:
    meta = dict(video.meta or {}) if isinstance(video.meta, dict) else {}
    meta["queue_cancelled_at"] = now.isoformat()
    meta["queue_cancelled_task_id"] = int(task_id)
    video.meta = meta
    video.status = "new"
    video.task_id = None


def _normalize_account_pool_mode_from_settings(task_settings: dict | None) -> str:
    if not isinstance(task_settings, dict):
        return ACCOUNT_POOL_MODE_SHARED
    mode = str(task_settings.get("account_pool_mode") or ACCOUNT_POOL_MODE_SHARED).strip().lower()
    if mode == ACCOUNT_POOL_MODE_PERSONAL:
        return ACCOUNT_POOL_MODE_PERSONAL
    return ACCOUNT_POOL_MODE_SHARED


def _active_duplicate_matches_pool(existing_task: Task, new_pool_mode: str) -> bool:
    existing_pool_mode = _normalize_account_pool_mode_from_settings(existing_task.settings or {})
    return existing_pool_mode == new_pool_mode


def _preserve_text(value: str | None) -> str | None:
    """Нормализует пользовательский текст для хранения в БД.

    - Приводит переводы строк к LF (Telegram присылает CRLF от Windows-клиента).
    - Обрезает ведущие/трейлинговые whitespace (включая «потерявшийся \\n»):
      если пользователь случайно скопировал текст с пустой строкой —
      она не пойдёт в TikTok как пустой коммент-перевод-строка.
    - Сохраняет всё остальное как есть: эмодзи, multi-line внутри, спецсимволы.
    """
    if value is None:
        return None
    return str(value).replace("\r\n", "\n").replace("\r", "\n").strip()


def _meaningful_text(value: str | None) -> str | None:
    preserved = _preserve_text(value)
    if preserved is None or not preserved:
        return None
    return preserved


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


def _normalize_task_reply_payload(
    raw_settings: dict | None,
    top_level_replies: list[str | None],
) -> tuple[dict, list[str]]:
    task_settings = dict(raw_settings or {})
    settings_replies = task_settings.get("replies")
    if isinstance(settings_replies, list) and settings_replies:
        raw_replies = settings_replies
    else:
        raw_replies = [
            *top_level_replies,
            *[
                task_settings.get(f"reply{idx}")
                for idx in range(4, settings.MAX_REPLIES + 1)
            ],
        ]

    replies = _normalize_reply_list(raw_replies)
    requested_reply_count = task_settings.get("reply_count")
    if isinstance(requested_reply_count, int) and requested_reply_count >= 0:
        reply_count = min(requested_reply_count, len(replies), settings.MAX_REPLIES)
    else:
        reply_count = len(replies)

    for idx in range(4, settings.MAX_REPLIES + 1):
        task_settings.pop(f"reply{idx}", None)
    task_settings["replies"] = replies
    task_settings["reply_count"] = reply_count
    for idx, reply_text in enumerate(replies[3:], start=4):
        task_settings[f"reply{idx}"] = reply_text
    return task_settings, replies


class TaskCreate(BaseModel):
    """ÐœÐ¾Ð´ÐµÐ»ÑŒ Ð´Ð»Ñ ÑÐ¾Ð·Ð´Ð°Ð½Ð¸Ñ Ð·Ð°Ð´Ð°Ñ‡Ð¸."""
    user_id: int
    # Ð”Ð»Ñ engagement_only Ð¼Ð¾Ð¶ÐµÑ‚ Ð½Ðµ Ð¿ÐµÑ€ÐµÐ´Ð°Ð²Ð°Ñ‚ÑŒÑÑ: Ñ„Ð°ÐºÑ‚Ð¸Ñ‡ÐµÑÐºÐ¸Ð¹ main_comment Ð±ÐµÑ€ÐµÐ¼ Ð¸Ð· text_variants[0].
    main_comment: str | None = None
    text_variants: list[str]
    reply1: str | None = None
    reply2: str | None = None
    reply3: str | None = None
    likes: int = 0  # Legacy поле, фактическое значение задаётся app settings
    video_urls: list[str]
    settings: dict | None = None
    # HYBRID: Ð¢Ð¸Ð¿ Ð·Ð°ÐºÐ°Ð·Ð°
    kind: str = "full"  # "full" Ð¸Ð»Ð¸ "engagement_only"
    # HYBRID: Ð”Ð»Ñ engagement_only - Ð¿Ð°Ñ€Ð°Ð¼ÐµÑ‚Ñ€Ñ‹ Ð¿Ð¾Ð¸ÑÐºÐ° ÐºÐ¾Ð¼Ð¼ÐµÐ½Ñ‚Ð°Ñ€Ð¸Ñ
    engagement_comment_id: str | None = None  # ID ÐºÐ¾Ð¼Ð¼ÐµÐ½Ñ‚Ð°Ñ€Ð¸Ñ (Ð¿Ñ€Ð¸Ð¾Ñ€Ð¸Ñ‚ÐµÑ‚)
    engagement_username: str | None = None  # Ð˜Ð¼Ñ Ð°Ð²Ñ‚Ð¾Ñ€Ð° ÐºÐ¾Ð¼Ð¼ÐµÐ½Ñ‚Ð°Ñ€Ð¸Ñ
    engagement_comment_text: str | None = None  # Ð¢ÐµÐºÑÑ‚ ÐºÐ¾Ð¼Ð¼ÐµÐ½Ñ‚Ð°Ñ€Ð¸Ñ Ð´Ð»Ñ Ð¿Ð¾Ð¸ÑÐºÐ°
    
    # Ð˜Ð¡ÐŸÐ ÐÐ’Ð›Ð•ÐÐž: Ð’Ð°Ð»Ð¸Ð´Ð°Ñ†Ð¸Ñ Ð²Ñ…Ð¾Ð´Ð½Ñ‹Ñ… Ð´Ð°Ð½Ð½Ñ‹Ñ…
    @field_validator('settings')
    @classmethod
    def validate_visibility_setting(cls, value):
        if value is not None:
            value = dict(value)
            for key in ("verify_main_visibility", "verify_reply_visibility"):
                if key in value:
                    value[key] = TypeAdapter(bool).validate_python(value[key])
        return value

    @field_validator('video_urls')
    @classmethod
    def validate_video_urls(cls, v: list[str]) -> list[str]:
        """Ð’Ð°Ð»Ð¸Ð´Ð¸Ñ€ÑƒÐµÑ‚ ÑÐ¿Ð¸ÑÐ¾Ðº URL Ð²Ð¸Ð´ÐµÐ¾."""
        if not v:
            raise ValueError('video_urls cannot be empty')
        if len(v) > 100:
            raise ValueError('Too many URLs (max 100)')
        # Ð’Ð°Ð»Ð¸Ð´Ð¸Ñ€ÑƒÐµÐ¼ Ð¸ Ð´ÐµÐ´ÑƒÐ¿Ð»Ð¸Ñ†Ð¸Ñ€ÑƒÐµÐ¼ URLs, Ñ‡Ñ‚Ð¾Ð±Ñ‹ Ð½Ðµ ÑÐ¾Ð·Ð´Ð°Ð²Ð°Ñ‚ÑŒ/Ð¾Ð¿Ð»Ð°Ñ‡Ð¸Ð²Ð°Ñ‚ÑŒ Ð´ÑƒÐ±Ð»Ð¸ÐºÐ°Ñ‚Ñ‹.
        from bot.services.validators import validate_video_url, normalize_url

        normalized_unique: list[str] = []
        seen: set[str] = set()

        for raw_url in v:
            normalized_url = normalize_url(raw_url)
            if not validate_video_url(normalized_url):
                raise ValueError(f'Invalid TikTok URL: {str(raw_url)[:50]}')
            if normalized_url in seen:
                continue
            seen.add(normalized_url)
            normalized_unique.append(normalized_url)

        if not normalized_unique:
            raise ValueError('video_urls cannot be empty')

        return normalized_unique
    
    @field_validator('text_variants')
    @classmethod
    def validate_text_variants(cls, v: list[str]) -> list[str]:
        """Ð¢Ñ€ÐµÐ±ÑƒÐµÐ¼ Ð¼Ð¸Ð½Ð¸Ð¼ÑƒÐ¼ 3 Ð²Ð°Ñ€Ð¸Ð°Ð½Ñ‚Ð° Ñ‚ÐµÐºÑÑ‚Ð° Ð´Ð»Ñ Ð±Ð¾Ñ€ÑŒÐ±Ñ‹ Ñ Ñ‚ÐµÐ½ÑŒÑŽ."""
        if not v or len(v) < 3:
            raise ValueError('text_variants must contain at least 3 non-empty items')
        normalized = []
        for item in v:
            item = _meaningful_text(item)
            if not item:
                continue
            if len(item) > settings.MAX_COMMENT_LENGTH:
                raise ValueError(f'text variant too long (max {settings.MAX_COMMENT_LENGTH})')
            normalized.append(item)
        if len(normalized) < 3:
            raise ValueError('Provide at least 3 non-empty text variants')
        return normalized
    
    @field_validator('main_comment')
    @classmethod
    def validate_main_comment(cls, v: str | None) -> str:
        """Normalizes and bounds main comment length."""
        normalized = _preserve_text(v) or ""
        if len(normalized) > settings.MAX_COMMENT_LENGTH:
            raise ValueError(f'main_comment too long (max {settings.MAX_COMMENT_LENGTH} characters)')
        return normalized
    
    @field_validator('reply1', 'reply2', 'reply3')
    @classmethod
    def validate_replies(cls, v: str | None) -> str | None:
        """Ð’Ð°Ð»Ð¸Ð´Ð¸Ñ€ÑƒÐµÑ‚ Ð¾Ñ‚Ð²ÐµÑ‚Ñ‹."""
        preserved = _meaningful_text(v)
        if preserved is None:
            return None
        if len(preserved) > settings.MAX_REPLY_LENGTH:
            raise ValueError(f'Reply too long (max {settings.MAX_REPLY_LENGTH} characters)')
        return preserved
    
    @field_validator('likes')
    @classmethod
    def validate_likes(cls, v: int) -> int:
        """Legacy-валидация: поле не источник истины, но должно быть валидным."""
        if v < 0:
            raise ValueError("likes cannot be negative")
        if v > BOOST_LIKES_MAX:
            raise ValueError(f"likes too large (max {BOOST_LIKES_MAX})")
        return v
    
    @field_validator('kind')
    @classmethod
    def validate_kind(cls, v: str) -> str:
        """Ð’Ð°Ð»Ð¸Ð´Ð¸Ñ€ÑƒÐµÑ‚ Ñ‚Ð¸Ð¿ Ð·Ð°ÐºÐ°Ð·Ð°."""
        if v not in ("full", "engagement_only"):
            raise ValueError('kind must be "full" or "engagement_only"')
        return v

    @model_validator(mode="after")
    def validate_kind_specific_fields(self):
        """Cross-field validation for full/engagement_only orders."""
        if self.kind != "full" and (self.settings or {}).get("verify_main_visibility"):
            raise ValueError("verify_main_visibility is available only for full orders")
        if self.kind == "full":
            if not self.main_comment:
                raise ValueError("main_comment cannot be empty for full orders")
            return self

        has_comment_id = bool(self.engagement_comment_id)
        has_comment_text = bool(self.engagement_comment_text)
        if not has_comment_id and not has_comment_text:
            raise ValueError(
                "For engagement_only orders, provide either engagement_comment_id or engagement_comment_text"
            )
        return self

    @field_validator('engagement_comment_id')
    @classmethod
    def validate_engagement_comment_id(cls, v: str | None) -> str | None:
        """ÐÐ¾Ñ€Ð¼Ð°Ð»Ð¸Ð·ÑƒÐµÑ‚ Ð¸ Ð¾Ð³Ñ€Ð°Ð½Ð¸Ñ‡Ð¸Ð²Ð°ÐµÑ‚ ID ÐºÐ¾Ð¼Ð¼ÐµÐ½Ñ‚Ð°Ñ€Ð¸Ñ Ð´Ð»Ñ engagement_only."""
        if v is None:
            return None
        normalized = v.strip()
        if not normalized:
            return None
        if len(normalized) > 128:
            raise ValueError("engagement_comment_id too long (max 128)")
        return normalized

    @field_validator('engagement_username')
    @classmethod
    def validate_engagement_username(cls, v: str | None) -> str | None:
        """ÐÐ¾Ñ€Ð¼Ð°Ð»Ð¸Ð·ÑƒÐµÑ‚ username Ð°Ð²Ñ‚Ð¾Ñ€Ð° ÐºÐ¾Ð¼Ð¼ÐµÐ½Ñ‚Ð°Ñ€Ð¸Ñ."""
        if v is None:
            return None
        normalized = v.strip().lstrip("@")
        if not normalized:
            return None
        if len(normalized) > 64:
            raise ValueError("engagement_username too long (max 64)")
        return normalized

    @field_validator('engagement_comment_text')
    @classmethod
    def validate_engagement_comment_text(cls, v: str | None) -> str | None:
        """ÐžÐ³Ñ€Ð°Ð½Ð¸Ñ‡Ð¸Ð²Ð°ÐµÑ‚ Ñ€Ð°Ð·Ð¼ÐµÑ€ Ñ‚ÐµÐºÑÑ‚Ð° ÐºÐ¾Ð¼Ð¼ÐµÐ½Ñ‚Ð°Ñ€Ð¸Ñ Ð´Ð»Ñ Ð¿Ð¾Ð¸ÑÐºÐ°."""
        normalized = _meaningful_text(v)
        if not normalized:
            return None
        if len(normalized) > settings.MAX_COMMENT_LENGTH:
            raise ValueError(
                f"engagement_comment_text too long (max {settings.MAX_COMMENT_LENGTH} characters)"
            )
        return normalized


class TaskTextsUpdate(BaseModel):
    text_variants: list[str]

    @field_validator('text_variants')
    @classmethod
    def validate_text_variants(cls, v: list[str]) -> list[str]:
        if not v or len(v) < 3:
            raise ValueError('Provide at least 3 text variants')
        normalized = []
        for item in v:
            item = _meaningful_text(item)
            if not item:
                continue
            if len(item) > settings.MAX_COMMENT_LENGTH:
                raise ValueError(f"text too long (max {settings.MAX_COMMENT_LENGTH})")
            normalized.append(item)
        if len(normalized) < 3:
            raise ValueError('Provide at least 3 non-empty text variants')
        return normalized

class TaskResponse(BaseModel):
    id: int
    user_id: int
    status: str
    main_comment: str
    reply1: str | None
    reply2: str | None
    reply3: str | None
    likes: int
    video_urls: list[str]
    settings: dict | None
    progress: int
    attempts: int
    error_reason: str | None
    created_at: str
    updated_at: str
    # REFACTOR: Ð”Ð¾Ð±Ð°Ð²Ð»ÐµÐ½Ñ‹ Ð¿Ð¾Ð»Ñ Ð´Ð»Ñ Ð¿Ñ€Ð¾Ð·Ñ€Ð°Ñ‡Ð½Ð¾Ð¹ Ð¾Ð¿Ð»Ð°Ñ‚Ñ‹
    total_cost: Decimal | None = None  # Ð¡Ð¿Ð¸ÑÐ°Ð½Ð¾ Ð¸Ð·Ð½Ð°Ñ‡Ð°Ð»ÑŒÐ½Ð¾
    refunded_cost: Decimal | None = None  # Ð’Ð¾Ð·Ð²Ñ€Ð°Ñ‰ÐµÐ½Ð¾ Ð·Ð° Ð½ÐµÑƒÐ´Ð°Ñ‡Ð½Ñ‹Ðµ
    final_cost: Decimal | None = None  # Ð˜Ñ‚Ð¾Ð³Ð¾Ð²Ð°Ñ ÑÑ‚Ð¾Ð¸Ð¼Ð¾ÑÑ‚ÑŒ (total_cost - refunded_cost)
    origin: str | None = None  # Ð˜ÑÑ‚Ð¾Ñ‡Ð½Ð¸Ðº Ð·Ð°ÐºÐ°Ð·Ð°: manual/composer/wizard/autopilot/new_videos/notification
    kind: str = "full"  # HYBRID: Ð¢Ð¸Ð¿ Ð·Ð°ÐºÐ°Ð·Ð° (full/engagement_only)
    display_status: str | None = None
    display_reason: str | None = None
    warnings: list[str] = []
    execution_summary: dict | None = None

    @classmethod
    def from_orm(cls, task: Task):
        # Ð’Ñ‹Ñ‡Ð¸ÑÐ»ÑÐµÐ¼ final_cost (total_cost - refunded_cost)
        final_cost = None
        if task.total_cost is not None:
            refunded = task.refunded_cost or Decimal("0.00")
            final_cost = task.total_cost - refunded
        
        # ÐŸÐ¾Ð»ÑƒÑ‡Ð°ÐµÐ¼ origin Ð¸Ð· settings
        origin = None
        if task.settings and isinstance(task.settings, dict):
            origin = task.settings.get("origin")
        
        # ÐŸÐ¾Ð»ÑƒÑ‡Ð°ÐµÐ¼ kind Ð¸Ð· Ð¼Ð¾Ð´ÐµÐ»Ð¸ (Ð¿Ð¾ ÑƒÐ¼Ð¾Ð»Ñ‡Ð°Ð½Ð¸ÑŽ "full" Ð´Ð»Ñ Ð¾Ð±Ñ€Ð°Ñ‚Ð½Ð¾Ð¹ ÑÐ¾Ð²Ð¼ÐµÑÑ‚Ð¸Ð¼Ð¾ÑÑ‚Ð¸)
        kind = getattr(task, "kind", "full") or "full"
        task_payload = {
            "status": task.status,
            "progress": task.progress,
            "error_reason": task.error_reason,
            "video_urls": task.video_urls or [],
            "settings": task.settings if isinstance(task.settings, dict) else {},
        }
        execution_summary = build_task_execution_summary(task_payload)
        display_reason = build_task_display_reason(task_payload, short=False)
        warnings = collect_task_warnings(task_payload, short=False)
        
        return cls(
            id=task.id,
            user_id=task.user_id,
            status=task.status,
            main_comment=task.main_comment,
            reply1=task.reply1,
            reply2=task.reply2,
            reply3=task.reply3,
            likes=task.likes,
            video_urls=task.video_urls or [],
            settings=task.settings,
            progress=task.progress,
            attempts=task.attempts,
            error_reason=task.error_reason,
            created_at=task.created_at.isoformat() if task.created_at else "",
            updated_at=task.updated_at.isoformat() if task.updated_at else "",
            total_cost=task.total_cost,
            refunded_cost=task.refunded_cost,
            final_cost=final_cost,
            origin=origin,
            kind=kind,
            display_status=execution_summary.get("display_status"),
            display_reason=display_reason,
            warnings=warnings,
            execution_summary=execution_summary,
        )


def _normalize_header_key(value: str | None) -> str | None:
    """ÐÐ¾Ñ€Ð¼Ð°Ð»Ð¸Ð·ÑƒÐµÑ‚ ÐºÐ»ÑŽÑ‡ Ð¸Ð· HTTP Ð·Ð°Ð³Ð¾Ð»Ð¾Ð²ÐºÐ° (idempotency/rate-limit metadata)."""
    if not value:
        return None
    normalized = value.strip()
    if not normalized:
        return None
    # ÐžÐ³Ñ€Ð°Ð½Ð¸Ñ‡Ð¸Ð²Ð°ÐµÐ¼ Ð´Ð»Ð¸Ð½Ñƒ Ð´Ð»Ñ Ð±ÐµÐ·Ð¾Ð¿Ð°ÑÐ½Ð¾Ð³Ð¾ Ñ…Ñ€Ð°Ð½ÐµÐ½Ð¸Ñ Ð² settings
    return normalized[:128]


def _parse_setting_value(raw_value: str | None):
    if raw_value is None:
        return None
    try:
        return json.loads(raw_value)
    except Exception:
        return raw_value


def _coerce_bool(value, default: bool) -> bool:
    return coerce_bool(value, default)


def _coerce_likes_quantity(value, default: int) -> int:
    return coerce_likes_quantity(value, default)


async def _get_boost_likes_config(db: AsyncSession) -> tuple[bool, int]:
    provider_available = likes_provider_available(settings.BOOST_PROJECT_API_KEY)
    default_enabled = provider_available
    default_quantity = _coerce_likes_quantity(
        settings.BOOST_PROJECT_LIKES_QUANTITY,
        BOOST_LIKES_DEFAULT,
    )

    try:
        result = await db.execute(
            select(AppSettings).filter(
                AppSettings.key.in_([BOOST_LIKES_ENABLED_KEY, BOOST_LIKES_QUANTITY_KEY])
            )
        )
        rows = result.scalars().all()
        values = {row.key: _parse_setting_value(row.value) for row in rows}

        enabled = provider_available and _coerce_bool(values.get(BOOST_LIKES_ENABLED_KEY), default_enabled)
        quantity = _coerce_likes_quantity(values.get(BOOST_LIKES_QUANTITY_KEY), default_quantity)
        return enabled, quantity
    except Exception as e:
        logger.warning(f"Failed to read boost likes settings, using defaults: {e}")
        return default_enabled, default_quantity


@router.post("", response_model=dict)
@limiter.limit("10/minute")  # Ð˜Ð¡ÐŸÐ ÐÐ’Ð›Ð•ÐÐž: Rate limiting Ð´Ð»Ñ ÑÐ¾Ð·Ð´Ð°Ð½Ð¸Ñ Ð·Ð°Ð´Ð°Ñ‡
async def create_task(
    request: Request,
    task_data: TaskCreate, 
    db: AsyncSession = Depends(get_db),
    auth_user_id: int = Depends(verify_user_header),
):
    """
    Ð¡Ð¾Ð·Ð´Ð°Ñ‘Ñ‚ Ð½Ð¾Ð²ÑƒÑŽ Ð·Ð°Ð´Ð°Ñ‡Ñƒ.
    
    Ð˜Ð¡ÐŸÐ ÐÐ’Ð›Ð•ÐÐž: Ð’Ð°Ð»Ð¸Ð´Ð°Ñ†Ð¸Ñ Ð²Ñ‹Ð¿Ð¾Ð»Ð½ÑÐµÑ‚ÑÑ Ñ‡ÐµÑ€ÐµÐ· Pydantic validators.
    """
    # Ð’Ð°Ð»Ð¸Ð´Ð°Ñ†Ð¸Ñ ÑƒÐ¶Ðµ Ð²Ñ‹Ð¿Ð¾Ð»Ð½ÐµÐ½Ð° Ñ‡ÐµÑ€ÐµÐ· Pydantic validators
    ensure_user_scope(task_data.user_id, auth_user_id)

    # ÐŸÑ€Ð¾Ð²ÐµÑ€ÐºÐ° ÑÑƒÑ‰ÐµÑÑ‚Ð²Ð¾Ð²Ð°Ð½Ð¸Ñ Ð¿Ð¾Ð»ÑŒÐ·Ð¾Ð²Ð°Ñ‚ÐµÐ»Ñ
    result = await db.execute(select(User).filter(User.id == task_data.user_id))
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    
    # ÐŸÑ€Ð¾Ð²ÐµÑ€ÐºÐ° Ð±Ð»Ð¾ÐºÐ¸Ñ€Ð¾Ð²ÐºÐ¸ Ð¿Ð¾Ð»ÑŒÐ·Ð¾Ð²Ð°Ñ‚ÐµÐ»Ñ
    if user.is_blocked:
        raise HTTPException(status_code=403, detail="User is blocked. Contact administrator.")

    # Mode gating: in parsing mode all comment/full orders are disabled,
    # regardless of whether the request comes from bot, web UI or direct API.
    # The bot UI already hides these flows, but a stale callback or a
    # direct API caller must not be able to bypass it.
    # Exception: admin/owner users and workers with full_access_override have full mode.
    mode_row = await db.execute(
        select(AppSettings.value).filter(AppSettings.key == SOFTWARE_MODE_KEY)
    )
    current_mode = (mode_row.scalar_one_or_none() or "").strip().lower()
    if current_mode == SOFTWARE_MODE_PARSING and not has_full_functionality_access(user):
        logger.info(
            "Rejected /api/tasks POST: software_mode=parsing (user_id=%s, kind=%s)",
            task_data.user_id, task_data.kind,
        )
        raise HTTPException(
            status_code=403,
            detail="Software is in parsing mode: comment orders are disabled",
        )

    normalized_task_settings, normalized_replies = _normalize_task_reply_payload(
        task_data.settings,
        [task_data.reply1, task_data.reply2, task_data.reply3],
    )
    
    idempotency_key = _normalize_header_key(
        request.headers.get("X-Idempotency-Key") or request.headers.get("Idempotency-Key")
    )
    request_id = _normalize_header_key(request.headers.get("X-Request-ID"))

    # 1) Ð˜Ð´ÐµÐ¼Ð¿Ð¾Ñ‚ÐµÐ½Ñ‚Ð½Ð¾ÑÑ‚ÑŒ Ð¿Ð¾ ÐºÐ»ÑŽÑ‡Ñƒ: ÐµÑÐ»Ð¸ Ñ‚Ð°ÐºÐ¾Ð¹ ÐºÐ»ÑŽÑ‡ ÑƒÐ¶Ðµ Ð±Ñ‹Ð» Ñƒ Ð½ÐµÐ´Ð°Ð²Ð½ÐµÐ¹ Ð·Ð°Ð´Ð°Ñ‡Ð¸ Ð¿Ð¾Ð»ÑŒÐ·Ð¾Ð²Ð°Ñ‚ÐµÐ»Ñ â€” Ð²Ð¾Ð·Ð²Ñ€Ð°Ñ‰Ð°ÐµÐ¼ ÐµÑ‘
    # Ð—Ð°Ñ‰Ð¸Ñ‰Ð°ÐµÑ‚ Ð¾Ñ‚ Ð¿Ð¾Ð²Ñ‚Ð¾Ñ€Ð½Ð¾Ð¹ Ð¾Ñ‚Ð¿Ñ€Ð°Ð²ÐºÐ¸ Ð¿Ð¾ÑÐ»Ðµ Ñ‚Ð°Ð¹Ð¼Ð°ÑƒÑ‚Ð°/ÑÐ±Ð¾ÐµÐ² ÑÐµÑ‚Ð¸.
    if idempotency_key:
        idempotency_window_start = datetime.now(timezone.utc) - timedelta(hours=24)
        idem_result = await db.execute(
            select(Task).filter(
                Task.user_id == task_data.user_id,
                Task.created_at >= idempotency_window_start
            ).order_by(Task.created_at.desc()).limit(200)
        )
        for existing_task in idem_result.scalars().all():
            existing_settings = existing_task.settings or {}
            existing_key = _normalize_header_key(
                existing_settings.get("idempotency_key") or existing_settings.get("client_task_fingerprint")
            )
            if existing_key and existing_key == idempotency_key:
                logger.info(
                    f"Idempotency hit for user={task_data.user_id}: "
                    f"task_id={existing_task.id}, status={existing_task.status}"
                )
                return {
                    "task_id": existing_task.id,
                    "status": existing_task.status,
                    "duplicate": True,
                    "idempotency_reuse": True
                }

    # Identical content is allowed in separate full and reply orders.
    new_account_pool_mode = _normalize_account_pool_mode_from_settings(normalized_task_settings)

    # REFACTOR: Ð˜ÑÐ¿Ð¾Ð»ÑŒÐ·ÑƒÐµÐ¼ text_variants[0] ÐºÐ°Ðº Ð¾ÑÐ½Ð¾Ð²Ð½Ð¾Ð¹ Ñ‚ÐµÐºÑÑ‚ Ð²ÐµÐ·Ð´Ðµ
    # WHY: Ð“Ð°Ñ€Ð°Ð½Ñ‚Ð¸Ñ€ÑƒÐµÐ¼ ÐµÐ´Ð¸Ð½Ð¾Ð¾Ð±Ñ€Ð°Ð·Ð¸Ðµ - Ð¾ÑÐ½Ð¾Ð²Ð½Ð¾Ð¹ Ñ‚ÐµÐºÑÑ‚ Ð²ÑÐµÐ³Ð´Ð° Ð¿ÐµÑ€Ð²Ñ‹Ð¹ Ð²Ð°Ñ€Ð¸Ð°Ð½Ñ‚
    task_main_text = task_data.text_variants[0] if task_data.text_variants else task_data.main_comment

    # ÐŸÑ€Ð¾Ð²ÐµÑ€ÐºÐ° Ð±Ð°Ð»Ð°Ð½ÑÐ° (Ð°Ð´Ð¼Ð¸Ð½Ñ‹ Ð½Ðµ Ð¿Ñ€Ð¾Ð²ÐµÑ€ÑÑŽÑ‚ÑÑ)
    # REFACTOR: Ð˜ÑÐ¿Ñ€Ð°Ð²Ð»ÐµÐ½ admin-check - Ð¸ÑÐ¿Ð¾Ð»ÑŒÐ·ÑƒÐµÐ¼ user.role Ð¸Ð»Ð¸ user.telegram_id Ð²Ð¼ÐµÑÑ‚Ð¾ is_admin(user-object)
    video_count = len(task_data.video_urls)
    user_is_admin = user.role in ("admin", "owner")
    if not user_is_admin and hasattr(settings, 'ADMIN_IDS'):
        # ÐŸÑ€Ð¾Ð²ÐµÑ€ÑÐµÐ¼ Ñ‚Ð°ÐºÐ¶Ðµ Ð¿Ð¾ telegram_id Ð² ADMIN_IDS
        admin_ids = settings.ADMIN_IDS
        if isinstance(admin_ids, str):
            try:
                admin_ids = [int(x.strip()) for x in admin_ids.split(',')]
            except (ValueError, TypeError):
                admin_ids = []
        if isinstance(admin_ids, list) and user.telegram_id:
            user_is_admin = user.telegram_id in admin_ids

    # Only replay of the same explicitly identified request is deduplicated.
    # Separate orders may have identical video URLs, comments and replies.
    if idempotency_key and not claim_dedup_key(
        task_data.user_id, "", [], ttl_seconds=30,
        scope=f"idempotency={idempotency_key}",
    ):
        raise HTTPException(
            status_code=409,
            detail="ORDER_REQUEST_IN_PROGRESS: This order request is already being created. Retry with the same key.",
        )

    hold_transaction = None

    if not settings.DEMO_MODE and not user_is_admin:
        # ÐŸÐ¾Ð»ÑƒÑ‡Ð°ÐµÐ¼ Ñ†ÐµÐ½Ñ‹
        pricing = await get_pricing_async(db)
        # Ð¡Ñ‚Ð¾Ð¸Ð¼Ð¾ÑÑ‚ÑŒ Ñ€Ð°ÑÑÑ‡Ð¸Ñ‚Ñ‹Ð²Ð°ÐµÑ‚ÑÑ Ð¿Ð¾ Ñ†ÐµÐ½Ðµ ÑƒÑÐ¿ÐµÑˆÐ½Ð¾Ð³Ð¾ ÐºÐ¾Ð¼Ð¼ÐµÐ½Ñ‚Ð¸Ñ€Ð¾Ð²Ð°Ð½Ð¸Ñ (Ð±ÑƒÐ´ÐµÑ‚ Ð¿ÐµÑ€ÐµÑÑ‡Ð¸Ñ‚Ð°Ð½Ð¾ Ð¿Ð¾ÑÐ»Ðµ Ð²Ñ‹Ð¿Ð¾Ð»Ð½ÐµÐ½Ð¸Ñ)
        cost = calculate_task_cost(video_count, pricing.price_success)
        
        # ÐŸÑ€Ð¾Ð²ÐµÑ€ÑÐµÐ¼ Ð±Ð°Ð»Ð°Ð½Ñ
        current_balance = await get_user_balance_async(db, task_data.user_id)
        if current_balance < cost:
            raise HTTPException(
                status_code=402,
                detail=f"Insufficient balance. Required: {cost} грн, Available: {current_balance} грн"
            )
        
        # Ð˜Ð¡ÐŸÐ ÐÐ’Ð›Ð•ÐÐž: Hold Ð±Ð°Ð»Ð°Ð½ÑÐ° Ð¸ ÑÐ¾Ð·Ð´Ð°Ð½Ð¸Ðµ Task Ð² Ð¾Ð´Ð½Ð¾Ð¹ Ñ‚Ñ€Ð°Ð½Ð·Ð°ÐºÑ†Ð¸Ð¸
        # Ð­Ñ‚Ð¾ Ð¿Ñ€ÐµÐ´Ð¾Ñ‚Ð²Ñ€Ð°Ñ‰Ð°ÐµÑ‚ "Ð²Ð¸ÑÑÑ‰Ð¸Ðµ hold'Ñ‹" Ð¿Ñ€Ð¸ Ð¾ÑˆÐ¸Ð±ÐºÐµ Ð¼ÐµÐ¶Ð´Ñƒ hold Ð¸ ÑÐ¾Ð·Ð´Ð°Ð½Ð¸ÐµÐ¼ Task
        try:
            # Ð‘Ð»Ð¾ÐºÐ¸Ñ€ÑƒÐµÐ¼ Ð¿Ð¾Ð»ÑŒÐ·Ð¾Ð²Ð°Ñ‚ÐµÐ»Ñ Ð´Ð»Ñ Ð°Ñ‚Ð¾Ð¼Ð°Ñ€Ð½Ð¾Ð¹ Ð¾Ð¿ÐµÑ€Ð°Ñ†Ð¸Ð¸
            result = await db.execute(
                select(User).filter(User.id == task_data.user_id).with_for_update()
            )
            user = result.scalar_one_or_none()
            if not user:
                raise HTTPException(status_code=404, detail="User not found")
            
            # ÐŸÑ€Ð¾Ð²ÐµÑ€ÑÐµÐ¼ Ð±Ð°Ð»Ð°Ð½Ñ ÐµÑ‰Ðµ Ñ€Ð°Ð· (Ð½Ð° ÑÐ»ÑƒÑ‡Ð°Ð¹ Ð¸Ð·Ð¼ÐµÐ½ÐµÐ½Ð¸Ñ Ð¼ÐµÐ¶Ð´Ñƒ Ð¿Ñ€Ð¾Ð²ÐµÑ€ÐºÐ°Ð¼Ð¸)
            current_balance = user.balance or Decimal("0.00")
            if current_balance < cost:
                await db.rollback()
                raise HTTPException(
                    status_code=402,
                    detail=f"Insufficient balance. Required: {cost} грн, Available: {current_balance} грн"
                )
            
            # Ð¡Ð¿Ð¸ÑÑ‹Ð²Ð°ÐµÐ¼ Ð±Ð°Ð»Ð°Ð½Ñ (Ñ…Ð¾Ð»Ð´Ð¸Ñ€ÑƒÐµÐ¼ ÑÑ€ÐµÐ´ÑÑ‚Ð²Ð°) - Ð‘Ð•Ð— commit
            user.balance = current_balance - cost
            
            # Ð¡Ð¾Ð·Ð´Ð°Ñ‘Ð¼ Ð·Ð°Ð¿Ð¸ÑÑŒ Transaction Ð´Ð»Ñ Ð¸ÑÑ‚Ð¾Ñ€Ð¸Ð¸ (Ð‘Ð•Ð— commit)
            from db.models import Transaction
            hold_transaction = Transaction(
                user_id=task_data.user_id,
                amount=-cost,  # ÐžÑ‚Ñ€Ð¸Ñ†Ð°Ñ‚ÐµÐ»ÑŒÐ½Ð¾Ðµ Ð·Ð½Ð°Ñ‡ÐµÐ½Ð¸Ðµ Ð´Ð»Ñ ÑÐ¿Ð¸ÑÐ°Ð½Ð¸Ñ
                type="hold",
                description=f"Списание за заказ на {video_count} видео"
            )
            db.add(hold_transaction)
            
            # ÐÐ• Ð´ÐµÐ»Ð°ÐµÐ¼ commit Ð·Ð´ÐµÑÑŒ - Ð¾Ð½ Ð±ÑƒÐ´ÐµÑ‚ Ð¿Ð¾ÑÐ»Ðµ ÑÐ¾Ð·Ð´Ð°Ð½Ð¸Ñ Task
        except HTTPException:
            raise
        except Exception as e:
            await db.rollback()
            logger.error(f"Error holding balance: {e}", exc_info=True)
            raise HTTPException(status_code=500, detail="Error processing payment")
    else:
        cost = Decimal("0.00")  # Ð”Ð»Ñ Ð°Ð´Ð¼Ð¸Ð½Ð¾Ð² Ð¸ DEMO_MODE

    boost_likes_enabled, boost_likes_quantity = await get_boost_likes_config_async(db)
    boost_likes_target_mode = await get_boost_likes_target_mode_async(db)
    order_execution_strategy = await get_order_execution_strategy_async(db)

    # REFACTOR: Ð¡Ð¾Ñ…Ñ€Ð°Ð½ÑÐµÐ¼ origin Ð² settings (Ð´Ð»Ñ Ñ„Ð¸Ð»ÑŒÑ‚Ñ€Ð°Ñ†Ð¸Ð¸ Ð¸ Ð¾Ñ‚Ð¾Ð±Ñ€Ð°Ð¶ÐµÐ½Ð¸Ñ)
    # WHY: Ð¥Ñ€Ð°Ð½Ð¸Ð¼ Ð² settings, Ð½Ð¾ Ð¼Ð¾Ð¶Ð½Ð¾ Ð»ÐµÐ³ÐºÐ¾ Ñ„Ð¸Ð»ÑŒÑ‚Ñ€Ð¾Ð²Ð°Ñ‚ÑŒ Ñ‡ÐµÑ€ÐµÐ· JSONB queries Ð² Ð±ÑƒÐ´ÑƒÑ‰ÐµÐ¼
    task_settings = dict(normalized_task_settings)
    task_settings.setdefault("verify_main_visibility", False)
    task_settings.setdefault("verify_reply_visibility", False)
    task_settings["text_variants"] = task_data.text_variants
    task_settings["account_pool_mode"] = new_account_pool_mode
    # Ð£ÑÑ‚Ð°Ð½Ð°Ð²Ð»Ð¸Ð²Ð°ÐµÐ¼ origin ÐµÑÐ»Ð¸ Ð½Ðµ ÑƒÐºÐ°Ð·Ð°Ð½ (Ð´Ð»Ñ Ð¾Ð±Ñ€Ð°Ñ‚Ð½Ð¾Ð¹ ÑÐ¾Ð²Ð¼ÐµÑÑ‚Ð¸Ð¼Ð¾ÑÑ‚Ð¸)
    if "origin" not in task_settings:
        task_settings["origin"] = "manual"  # ÐŸÐ¾ ÑƒÐ¼Ð¾Ð»Ñ‡Ð°Ð½Ð¸ÑŽ manual
    if idempotency_key:
        task_settings["client_task_fingerprint"] = idempotency_key
        task_settings["idempotency_key"] = idempotency_key
    if request_id:
        task_settings["request_id"] = request_id
    requested_boost_likes = task_settings.get("boost_likes_enabled")
    task_boost_likes_enabled = boost_likes_enabled
    if requested_boost_likes is not None:
        task_boost_likes_enabled = boost_likes_enabled and _coerce_bool(
            requested_boost_likes,
            boost_likes_enabled,
        )
    if task_data.kind == "full" and not normalized_replies:
        task_boost_likes_enabled = False
    likes_for_task = boost_likes_quantity if task_boost_likes_enabled else 0
    task_settings["boost_likes_enabled"] = task_boost_likes_enabled
    task_settings["likes_count"] = likes_for_task
    task_settings["boost_likes_target_mode"] = coerce_likes_target_mode(
        boost_likes_target_mode,
        BOOST_LIKES_TARGET_SINGLE_TOP,
    )
    task_settings["order_execution_strategy"] = order_execution_strategy
    
    # HYBRID: Ð”Ð»Ñ engagement_only ÑÐ¾Ñ…Ñ€Ð°Ð½ÑÐµÐ¼ Ð¿Ð°Ñ€Ð°Ð¼ÐµÑ‚Ñ€Ñ‹ Ð¿Ð¾Ð¸ÑÐºÐ° ÐºÐ¾Ð¼Ð¼ÐµÐ½Ñ‚Ð°Ñ€Ð¸Ñ
    if task_data.kind == "engagement_only":
        if task_data.engagement_comment_id:
            task_settings["engagement_comment_id"] = task_data.engagement_comment_id
        if task_data.engagement_username:
            task_settings["engagement_username"] = task_data.engagement_username
        if task_data.engagement_comment_text:
            task_settings["engagement_comment_text"] = task_data.engagement_comment_text
        
        # Ð’Ð°Ð»Ð¸Ð´Ð°Ñ†Ð¸Ñ: Ð´Ð¾Ð»Ð¶ÐµÐ½ Ð±Ñ‹Ñ‚ÑŒ comment_id Ð¸Ð»Ð¸ (username + text)
        if not task_data.engagement_comment_id and not task_data.engagement_comment_text:
            raise HTTPException(
                status_code=400,
                detail="For engagement_only orders, provide either engagement_comment_id or engagement_comment_text"
            )
        
    
    # Ð˜Ð¡ÐŸÐ ÐÐ’Ð›Ð•ÐÐž: Ð¡Ð¾Ð·Ð´Ð°Ð½Ð¸Ðµ Ð·Ð°Ð´Ð°Ñ‡Ð¸ Ð² Ñ‚Ð¾Ð¹ Ð¶Ðµ Ñ‚Ñ€Ð°Ð½Ð·Ð°ÐºÑ†Ð¸Ð¸
    task = Task(
        user_id=task_data.user_id,
        status="pending",
        main_comment=task_main_text,  # REFACTOR: Ð’ÑÐµÐ³Ð´Ð° Ð¸ÑÐ¿Ð¾Ð»ÑŒÐ·ÑƒÐµÐ¼ text_variants[0]
        reply1=normalized_replies[0] if len(normalized_replies) >= 1 else None,
        reply2=normalized_replies[1] if len(normalized_replies) >= 2 else None,
        reply3=normalized_replies[2] if len(normalized_replies) >= 3 else None,
        likes=likes_for_task,
        video_urls=task_data.video_urls,
        settings=task_settings,
        kind=task_data.kind,  # HYBRID: Ð¢Ð¸Ð¿ Ð·Ð°ÐºÐ°Ð·Ð°
        total_cost=cost,  # WHY: Ð¡Ð¿Ð¸ÑÐ°Ð½Ð¾ Ð¸Ð·Ð½Ð°Ñ‡Ð°Ð»ÑŒÐ½Ð¾ (Ð¿ÐµÑ€ÐµÑÑ‡Ð¸Ñ‚Ñ‹Ð²Ð°ÐµÑ‚ÑÑ Ð¿Ð¾ÑÐ»Ðµ Ð²Ñ‹Ð¿Ð¾Ð»Ð½ÐµÐ½Ð¸Ñ)
        refunded_cost=None,  # WHY: Ð‘ÑƒÐ´ÐµÑ‚ ÑƒÑÑ‚Ð°Ð½Ð¾Ð²Ð»ÐµÐ½Ð¾ Ð¿Ð¾ÑÐ»Ðµ Ð²Ð¾Ð·Ð²Ñ€Ð°Ñ‚Ð° Ð·Ð° Ð½ÐµÑƒÐ´Ð°Ñ‡Ð½Ñ‹Ðµ Ð²Ð¸Ð´ÐµÐ¾
        video_attempts={}  # Ð˜Ð½Ð¸Ñ†Ð¸Ð°Ð»Ð¸Ð·Ð¸Ñ€ÑƒÐµÐ¼ ÑÐ»Ð¾Ð²Ð°Ñ€ÑŒ Ð´Ð»Ñ Ð¾Ñ‚ÑÐ»ÐµÐ¶Ð¸Ð²Ð°Ð½Ð¸Ñ Ð¿Ð¾Ð¿Ñ‹Ñ‚Ð¾Ðº
    )
    db.add(task)
    
    # Ð˜Ð¡ÐŸÐ ÐÐ’Ð›Ð•ÐÐž: Commit Ð¾Ð´Ð½Ð¾Ð¹ Ñ‚Ñ€Ð°Ð½Ð·Ð°ÐºÑ†Ð¸ÐµÐ¹ - hold Ð±Ð°Ð»Ð°Ð½ÑÐ° Ð¸ ÑÐ¾Ð·Ð´Ð°Ð½Ð¸Ðµ Task Ð°Ñ‚Ð¾Ð¼Ð°Ñ€Ð½Ð¾
    try:
        await db.flush()
        if hold_transaction is not None:
            hold_transaction.task_id = task.id
        await reserve_task_for_enqueue_async(db, task.id)
        await db.commit()
        await db.refresh(task)
        logger.info(f"Task {task.id} created for user {task_data.user_id}. Cost: {cost} грн")
        
    except Exception as e:
        await db.rollback()
        # Ð˜Ð¡ÐŸÐ ÐÐ’Ð›Ð•ÐÐž: ÐŸÑ€Ð¸ Ð¾ÑˆÐ¸Ð±ÐºÐµ commit Ñ‚Ñ€Ð°Ð½Ð·Ð°ÐºÑ†Ð¸Ñ Ð¾Ñ‚ÐºÐ°Ñ‚Ñ‹Ð²Ð°ÐµÑ‚ÑÑ Ð¿Ð¾Ð»Ð½Ð¾ÑÑ‚ÑŒÑŽ,
        # Ð¿Ð¾ÑÑ‚Ð¾Ð¼Ñƒ Ð±Ð°Ð»Ð°Ð½Ñ Ð°Ð²Ñ‚Ð¾Ð¼Ð°Ñ‚Ð¸Ñ‡ÐµÑÐºÐ¸ Ð²Ð¾Ð·Ð²Ñ€Ð°Ñ‰Ð°ÐµÑ‚ÑÑ (Ð½Ðµ Ð½ÑƒÐ¶Ð½Ð¾ Ð¾Ñ‚Ð´ÐµÐ»ÑŒÐ½Ð¾ Ð²Ð¾Ð·Ð²Ñ€Ð°Ñ‰Ð°Ñ‚ÑŒ)
        logger.error(f"Error creating task: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Database error: {str(e)[:200]}")

    # Ð”Ð¾Ð±Ð°Ð²Ð»ÐµÐ½Ð¸Ðµ Ð² Ð¾Ñ‡ÐµÑ€ÐµÐ´ÑŒ Ð¿Ð¾ÑÐ»Ðµ Ñ„Ð¸ÐºÑÐ°Ñ†Ð¸Ð¸ reserve Ð² Ð‘Ð”
    await finalize_reserved_enqueue_async(db, task.id)

    return {"task_id": task.id, "status": task.status}


@router.get("", response_model=List[TaskResponse])
@limiter.limit("60/minute")  # Ð˜Ð¡ÐŸÐ ÐÐ’Ð›Ð•ÐÐž: Rate limiting Ð´Ð»Ñ ÑÐ¿Ð¸ÑÐºÐ° Ð·Ð°Ð´Ð°Ñ‡
async def list_tasks(
    request: Request,
    user_id: int | None = Query(None),
    status: str | None = Query(None),
    limit: int = Query(50, le=100),
    offset: int = Query(0, ge=0),
    db: AsyncSession = Depends(get_db),
    _: None = Depends(verify_admin_header),
    x_user_id: int | None = Header(default=None),
):
    """
    ÐŸÐ¾Ð»ÑƒÑ‡Ð°ÐµÑ‚ ÑÐ¿Ð¸ÑÐ¾Ðº Ð·Ð°Ð´Ð°Ñ‡ Ñ Ñ„Ð¸Ð»ÑŒÑ‚Ñ€Ð°Ñ†Ð¸ÐµÐ¹.
    
    Ð˜Ð¡ÐŸÐ ÐÐ’Ð›Ð•ÐÐž: Rate limiting Ð¸ N+1 query Ð¾Ð¿Ñ‚Ð¸Ð¼Ð¸Ð·Ð°Ñ†Ð¸Ñ.
    ÐšÐ Ð˜Ð¢Ð˜Ð§ÐÐž: Ð•ÑÐ»Ð¸ user_id Ð½Ðµ Ð¿ÐµÑ€ÐµÐ´Ð°Ð½, Ð²Ð¾Ð·Ð²Ñ€Ð°Ñ‰Ð°ÐµÐ¼ Ð¿ÑƒÑÑ‚Ð¾Ð¹ ÑÐ¿Ð¸ÑÐ¾Ðº (Ð±ÐµÐ·Ð¾Ð¿Ð°ÑÐ½Ð¾ÑÑ‚ÑŒ).
    """
    try:
        # Ð•ÑÐ»Ð¸ user_id Ð½Ðµ Ð¿ÐµÑ€ÐµÐ´Ð°Ð½, ÑÑ‚Ð¾ Ð°Ð´Ð¼Ð¸Ð½ÑÐºÐ¸Ð¹ Ð¾Ð±Ð·Ð¾Ñ€ Ð¾Ñ‡ÐµÑ€ÐµÐ´Ð¸.
        # Ð•ÑÐ»Ð¸ user_id Ð¿ÐµÑ€ÐµÐ´Ð°Ð½ â€” Ð´Ð¾Ð¿Ð¾Ð»Ð½Ð¸Ñ‚ÐµÐ»ÑŒÐ½Ð¾ Ð¿Ñ€Ð¾Ð²ÐµÑ€ÑÐµÐ¼ X-User-Id (ÐµÑÐ»Ð¸ Ð¿Ñ€Ð¸ÑˆÐµÐ»).
        if user_id is None and x_user_id is not None:
            user_id = x_user_id
        if user_id is not None and x_user_id is not None:
            ensure_user_scope(user_id, x_user_id)
        
        # Ð˜Ð¡ÐŸÐ ÐÐ’Ð›Ð•ÐÐž: Ð˜ÑÐ¿Ð¾Ð»ÑŒÐ·ÑƒÐµÐ¼ async Ð·Ð°Ð¿Ñ€Ð¾ÑÑ‹ Ñ joinedload Ð´Ð»Ñ Ð¿Ñ€ÐµÐ´Ð¾Ñ‚Ð²Ñ€Ð°Ñ‰ÐµÐ½Ð¸Ñ N+1
        from sqlalchemy.orm import joinedload
        
        query = select(Task).options(joinedload(Task.user))
        
        if user_id is not None:
            query = query.filter(Task.user_id == user_id)
        
        if status:
            query = query.filter(Task.status == status)
        
        # ИСПРАВЛЕНО: добавлен вторичный сорт по Task.id.desc() для детерминированного
        # порядка, когда несколько задач созданы в одну и ту же миллисекунду (батч).
        # Без этого PostgreSQL может вернуть их в произвольном порядке.
        query = query.order_by(Task.created_at.desc(), Task.id.desc()).offset(offset).limit(limit)
        result = await db.execute(query)
        tasks = result.unique().scalars().all()
        
        logger.debug(f"[API] list_tasks: user_id={user_id}, status={status}, found {len(tasks)} tasks")
        return [TaskResponse.from_orm(task) for task in tasks]
    except Exception as e:
        logger.error(f"Error listing tasks: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Database error: {str(e)[:200]}")


@router.post("/cancel_pending")
@limiter.limit("20/minute")
async def cancel_pending_tasks(
    request: Request,
    user_id: int = Query(..., description="ID пользователя, владельца задач"),
    db: AsyncSession = Depends(get_db),
    auth_user_id: int = Depends(verify_user_header),
):
    """Отменяет все pending-заказы пользователя и снимает их из Redis-очереди."""
    try:
        ensure_user_scope(user_id, auth_user_id)
        now = datetime.now(timezone.utc)

        user_result = await db.execute(select(User).filter(User.id == user_id).with_for_update())
        owner = user_result.scalar_one_or_none()
        if not owner:
            raise HTTPException(status_code=404, detail="User not found")

        try:
            result = await db.execute(
                select(Task)
                .filter(Task.user_id == user_id, Task.status == TaskStatus.PENDING.value)
                .with_for_update()
            )
        except Exception as lock_error:
            logger.warning(
                "SELECT FOR UPDATE not supported in cancel_pending_tasks, falling back to regular query: %s",
                lock_error,
            )
            result = await db.execute(
                select(Task).filter(Task.user_id == user_id, Task.status == TaskStatus.PENDING.value)
            )

        pending_tasks = list(result.scalars().all())
        pending_ids = [int(task.id) for task in pending_tasks]
        if not pending_ids:
            return {
                "success": True,
                "cancelled": 0,
                "removed_from_queue": 0,
                "released_videos": 0,
                "refunded_amount": 0.0,
            }

        videos_by_task: dict[int, list[CreatorVideo]] = {}
        videos_result = await db.execute(
            select(CreatorVideo)
            .join(CreatorSource, CreatorVideo.creator_id == CreatorSource.id)
            .filter(
                CreatorVideo.task_id.in_(pending_ids),
                CreatorSource.user_id == user_id,
                CreatorVideo.status == "queued",
            )
        )
        for video in videos_result.scalars().all():
            if video.task_id:
                videos_by_task.setdefault(int(video.task_id), []).append(video)

        refunded_amount = Decimal("0.00")
        released_videos = 0
        for task in pending_tasks:
            task_settings = _settings_dict(task)
            task_settings["cancelled_by_user"] = True
            task_settings["cancelled_at"] = now.isoformat()
            task_settings["cancel_reason"] = "orders_queue_cancel"
            task.status = TaskStatus.STOPPED.value
            task.error_reason = "cancelled_by_user"
            task.settings = task_settings
            task.updated_at = now

            refund = _remaining_task_refund(task)
            if refund > 0:
                owner.balance = (owner.balance or Decimal("0.00")) + refund
                task.refunded_cost = _money(task.refunded_cost) + refund
                db.add(
                    Transaction(
                        user_id=user_id,
                        task_id=task.id,
                        amount=refund,
                        type="refund",
                        description=f"Возврат за отмену заказа #{task.id} из очереди",
                    )
                )
                refunded_amount += refund

            for video in videos_by_task.get(int(task.id), []):
                _mark_creator_video_released(video, task_id=int(task.id), now=now)
                released_videos += 1

        await db.execute(delete(PendingEnqueue).where(PendingEnqueue.task_id.in_(pending_ids)))
        await db.commit()

        removed_from_queue = await asyncio.to_thread(remove_tasks_from_queue, pending_ids)
        logger.info(
            "Cancelled pending queue for user_id=%s: tasks=%s redis_removed=%s released_videos=%s refunded=%s",
            user_id,
            len(pending_ids),
            removed_from_queue,
            released_videos,
            refunded_amount,
        )
        return {
            "success": True,
            "cancelled": len(pending_ids),
            "removed_from_queue": removed_from_queue,
            "released_videos": released_videos,
            "refunded_amount": float(refunded_amount),
        }
    except HTTPException:
        raise
    except Exception as e:
        await db.rollback()
        logger.error(f"Error cancelling pending tasks for user {user_id}: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Database error: {str(e)[:200]}")


@router.get("/{task_id}", response_model=TaskResponse)
@limiter.limit("60/minute")  # Ð˜Ð¡ÐŸÐ ÐÐ’Ð›Ð•ÐÐž: Rate limiting
async def get_task(
    request: Request,
    task_id: int, 
    user_id: int = Query(..., description="ID пользователя, владельца задачи"),
    db: AsyncSession = Depends(get_db),
    auth_user_id: int = Depends(verify_user_header),
):
    """ÐŸÐ¾Ð»ÑƒÑ‡Ð°ÐµÑ‚ Ð·Ð°Ð´Ð°Ñ‡Ñƒ Ð¿Ð¾ ID."""
    try:
        ensure_user_scope(user_id, auth_user_id)
        # Ð˜Ð¡ÐŸÐ ÐÐ’Ð›Ð•ÐÐž: Ð˜ÑÐ¿Ð¾Ð»ÑŒÐ·ÑƒÐµÐ¼ async Ð·Ð°Ð¿Ñ€Ð¾Ñ
        result = await db.execute(
            select(Task).filter(
                Task.id == task_id,
                Task.user_id == user_id
            )
        )
        task = result.scalar_one_or_none()
        if not task:
            raise HTTPException(status_code=404, detail="Task not found")
        return TaskResponse.from_orm(task)
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error getting task {task_id}: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Database error: {str(e)[:200]}")


@router.post("/{task_id}/priority")
@limiter.limit("30/minute")
async def prioritize_task(
    request: Request,
    task_id: int,
    user_id: int = Query(..., description="ID пользователя, владельца задачи"),
    db: AsyncSession = Depends(get_db),
    auth_user_id: int = Depends(verify_user_header),
):
    """Повышает pending-заказ до верхнего приоритета очереди."""
    try:
        ensure_user_scope(user_id, auth_user_id)

        try:
            result = await db.execute(
                select(Task)
                .filter(Task.id == task_id, Task.user_id == user_id)
                .with_for_update()
            )
        except Exception as lock_error:
            logger.warning(
                "SELECT FOR UPDATE not supported in prioritize_task, falling back to regular query: %s",
                lock_error,
            )
            result = await db.execute(select(Task).filter(Task.id == task_id, Task.user_id == user_id))

        task = result.scalar_one_or_none()
        if not task:
            raise HTTPException(status_code=404, detail="Task not found")
        if str(task.status or "").lower() != TaskStatus.PENDING.value:
            raise HTTPException(status_code=409, detail="Task can be prioritized only while pending")

        now = datetime.now(timezone.utc)
        task_settings = _settings_dict(task)
        already_high = str(task_settings.get("queue_priority") or "").strip().lower() == "high"
        task_settings["queue_priority"] = "high"
        task_settings["priority_set_by_user"] = True
        task_settings["priority_set_at"] = now.isoformat()
        task.settings = task_settings
        task.updated_at = now
        await db.commit()
        await db.refresh(task)

        try:
            queue_state = await asyncio.to_thread(promote_task_to_high_priority, task.id)
        except Exception as queue_error:
            logger.warning(
                "Task %s priority was saved but Redis requeue failed; reserving enqueue: %s",
                task.id,
                queue_error,
            )
            await reserve_task_for_enqueue_async(db, task.id)
            await db.commit()
            queue_state = {
                "removed": 0,
                "enqueue_state": "pending_reserve",
                "error": str(queue_error)[:160],
            }
        logger.info(
            "Task %s prioritized by user %s (already_high=%s, queue_state=%s)",
            task.id,
            user_id,
            already_high,
            queue_state,
        )
        return {
            "success": True,
            "task_id": task.id,
            "status": task.status,
            "queue_priority": "high",
            "already_high": already_high,
            "queue": queue_state,
        }
    except HTTPException:
        raise
    except Exception as e:
        await db.rollback()
        logger.error(f"Error prioritizing task {task_id}: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Database error: {str(e)[:200]}")


@router.post("/{task_id}/stop")
@limiter.limit("30/minute")
async def stop_task(
    request: Request,
    task_id: int,
    user_id: int = Query(..., description="ID пользователя, владельца задачи"),
    db: AsyncSession = Depends(get_db),
    auth_user_id: int = Depends(verify_user_header),
):
    """Останавливает заказ, который уже выполняется."""
    try:
        ensure_user_scope(user_id, auth_user_id)

        try:
            result = await db.execute(
                select(Task).filter(
                    Task.id == task_id,
                    Task.user_id == user_id,
                ).with_for_update()
            )
        except Exception as lock_error:
            logger.warning(
                "SELECT FOR UPDATE not supported in stop_task, falling back to regular query: %s",
                lock_error,
            )
            result = await db.execute(
                select(Task).filter(
                    Task.id == task_id,
                    Task.user_id == user_id,
                )
            )

        task = result.scalar_one_or_none()
        if not task:
            raise HTTPException(status_code=404, detail="Task not found")

        current_status = str(task.status or "").lower()
        if current_status != TaskStatus.IN_PROGRESS.value:
            raise HTTPException(
                status_code=409,
                detail="Task can be stopped only while in_progress",
            )

        from services.task_state_machine import validate_status_transition
        validate_status_transition(current_status, TaskStatus.STOPPED.value)

        now = datetime.now(timezone.utc)
        task_settings = dict(task.settings or {}) if isinstance(task.settings, dict) else {}
        task_settings["stopped_by_user"] = True
        task_settings["stopped_at"] = now.isoformat()
        task_settings["force_close_browsers"] = True
        task.status = TaskStatus.STOPPED.value
        task.error_reason = "stopped_by_user"
        task.settings = task_settings
        task.updated_at = now
        await db.commit()
        await db.refresh(task)

        stop_signal_sent = await asyncio.to_thread(request_task_stop, task.id)

        logger.info(
            "Task %s stopped by user %s (stop_signal_sent=%s)",
            task.id,
            user_id,
            stop_signal_sent,
        )
        return {
            "success": True,
            "task_id": task.id,
            "status": task.status,
            "progress": task.progress,
            "stop_signal_sent": stop_signal_sent,
        }
    except HTTPException:
        raise
    except Exception as e:
        await db.rollback()
        logger.error(f"Error stopping task {task_id}: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Database error: {str(e)[:200]}")


@router.post("/{task_id}/texts")
@limiter.limit("30/minute")
async def update_task_texts(
    request: Request,
    task_id: int,
    payload: TaskTextsUpdate,
    user_id: int = Query(..., description="ID пользователя, владельца задачи"),
    db: AsyncSession = Depends(get_db),
    auth_user_id: int = Depends(verify_user_header),
):
    """
    ÐžÐ±Ð½Ð¾Ð²Ð»ÑÐµÑ‚ Ð²Ð°Ñ€Ð¸Ð°Ð½Ñ‚Ñ‹ Ñ‚ÐµÐºÑÑ‚Ð° Ð´Ð»Ñ pending Ð·Ð°Ð´Ð°Ñ‡ (Ð´Ð»Ñ Ð¾ÑÑ‚Ð°Ð²ÑˆÐ¸Ñ…ÑÑ URL).
    """
    try:
        ensure_user_scope(user_id, auth_user_id)
        result = await db.execute(
            select(Task).filter(
                Task.id == task_id,
                Task.user_id == user_id
            )
        )
        task = result.scalar_one_or_none()
        if not task:
            raise HTTPException(status_code=404, detail="Task not found")
        if task.status not in ("pending",):
            raise HTTPException(status_code=400, detail="Task must be pending to update texts")

        variants = payload.text_variants
        task.main_comment = variants[0]
        settings_dict = task.settings or {}
        settings_dict["text_variants"] = variants
        task.settings = settings_dict
        task.updated_at = datetime.now(timezone.utc)
        await db.commit()
        return {"success": True, "task_id": task.id, "text_variants": variants}
    except HTTPException:
        raise
    except Exception as e:
        await db.rollback()
        logger.error(f"Error updating task texts {task_id}: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Database error: {str(e)[:200]}")
