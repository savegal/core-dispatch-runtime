from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from config.settings import get_settings
from db.models import AppSettings, CreatorSource, CreatorVideo, Task, TaskTemplate, Transaction, User
from services.balance_service import get_pricing, get_pricing_async
from services.task_service import (
    finalize_reserved_enqueue,
    finalize_reserved_enqueue_async,
    reserve_task_for_enqueue,
    reserve_task_for_enqueue_async,
)
from services.likes_config import (
    BOOST_LIKES_DEFAULT,
    BOOST_LIKES_ENABLED_KEY,
    BOOST_LIKES_QUANTITY_KEY,
    coerce_bool,
    coerce_likes_quantity,
    provider_available as likes_provider_available,
)

logger = logging.getLogger("services.task_creation")
settings = get_settings()

@dataclass
class MonitoredTaskCreationResult:
    status: str
    task_id: int | None = None
    cost: Decimal = Decimal("0.00")
    reason: str | None = None


@dataclass
class MonitoredTaskBatchResult:
    status: str
    created_video_ids: list[int]
    task_ids: list[int]
    total_cost: Decimal = Decimal("0.00")
    reason: str | None = None
    skipped_video_ids: list[int] = field(default_factory=list)


def _parse_setting_value(raw_value: Any) -> Any:
    if raw_value is None:
        return None
    if not isinstance(raw_value, str):
        return raw_value
    try:
        return json.loads(raw_value)
    except (TypeError, ValueError):
        return raw_value


def _coerce_bool(value: Any, default: bool) -> bool:
    return coerce_bool(value, default)


def _coerce_likes_quantity(value: Any, default: int) -> int:
    return coerce_likes_quantity(value, default)


def _is_admin_user(user: User) -> bool:
    if user.role in ("admin", "owner"):
        return True
    admin_ids = getattr(settings, "ADMIN_IDS", [])
    if isinstance(admin_ids, str):
        try:
            admin_ids = [int(item.strip()) for item in admin_ids.split(",") if item.strip()]
        except Exception:
            admin_ids = []
    return bool(user.telegram_id and isinstance(admin_ids, list) and user.telegram_id in admin_ids)


def _build_text_variants(template: TaskTemplate) -> list[str]:
    template_settings = template.settings or {}
    variants = [
        str(item).strip()
        for item in (template_settings.get("text_variants") or [])
        if str(item).strip()
    ]
    if not variants:
        variants = [template.main_comment.strip()]
    return variants[:10]


def _template_reply_texts(template: TaskTemplate) -> list[str]:
    template_settings = template.settings or {}
    settings_replies = template_settings.get("replies")
    if isinstance(settings_replies, list):
        raw_replies = settings_replies
    else:
        raw_replies = [template.reply1, template.reply2, template.reply3]
    return [str(reply).strip() for reply in raw_replies if str(reply or "").strip()]


def _build_monitor_task_settings(
    creator: CreatorSource,
    video: CreatorVideo,
    template: TaskTemplate,
    origin: str,
    created_from: str,
    boost_likes_enabled: bool,
    boost_likes_quantity: int,
    text_variant_offset: int = 0,
) -> dict[str, Any]:
    task_settings = dict(template.settings or {})
    text_variants = _build_text_variants(template)
    task_settings["text_variants"] = text_variants
    task_settings["text_variant_offset"] = text_variant_offset % len(text_variants) if text_variants else 0
    task_settings["origin"] = origin
    task_settings["creator_username"] = creator.username
    task_settings["creator_source_id"] = creator.id
    task_settings["creator_video_id"] = video.id
    task_settings["created_from"] = created_from
    task_settings["boost_likes_enabled"] = boost_likes_enabled
    task_settings["likes_count"] = boost_likes_quantity if boost_likes_enabled else 0
    # Связь с шаблоном: используется UI orders для отображения "Шаблон: X",
    # для повторов "Сделать копию" и для аналитики monitoring.
    task_settings["template_id"] = template.id
    task_settings["template_name"] = template.name or f"Шаблон #{template.id}"
    return task_settings


def _validate_monitor_text_variants(task_settings: dict[str, Any]) -> str | None:
    variants = [str(item).strip() for item in (task_settings.get("text_variants") or []) if str(item).strip()]
    if len(variants) < 3:
        return "template_requires_3_text_variants"
    return None


def _selected_main_comment(task_settings: dict[str, Any]) -> str:
    variants = [str(item).strip() for item in (task_settings.get("text_variants") or []) if str(item).strip()]
    if not variants:
        return ""
    try:
        offset = int(task_settings.get("text_variant_offset") or 0)
    except (TypeError, ValueError):
        offset = 0
    return variants[offset % len(variants)]


def get_boost_likes_config_sync(db: Session) -> tuple[bool, int]:
    provider_available = likes_provider_available(settings.BOOST_PROJECT_API_KEY)
    default_enabled = provider_available
    default_quantity = _coerce_likes_quantity(
        settings.BOOST_PROJECT_LIKES_QUANTITY,
        BOOST_LIKES_DEFAULT,
    )

    try:
        rows = db.query(AppSettings).filter(
            AppSettings.key.in_([BOOST_LIKES_ENABLED_KEY, BOOST_LIKES_QUANTITY_KEY])
        ).all()
        values = {row.key: _parse_setting_value(row.value) for row in rows}
        enabled = provider_available and _coerce_bool(values.get(BOOST_LIKES_ENABLED_KEY), default_enabled)
        quantity = _coerce_likes_quantity(values.get(BOOST_LIKES_QUANTITY_KEY), default_quantity)
        return enabled, quantity
    except Exception as e:
        logger.warning(f"Failed to read boost likes settings, using defaults: {e}")
        return default_enabled, default_quantity


async def get_boost_likes_config_async(db: AsyncSession) -> tuple[bool, int]:
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


def create_monitored_task_sync(
    db: Session,
    creator: CreatorSource,
    video_id: int,
    template: TaskTemplate,
    *,
    origin: str,
    created_from: str,
) -> MonitoredTaskCreationResult:
    video = db.query(CreatorVideo).filter(CreatorVideo.id == video_id).with_for_update().first()
    if not video:
        return MonitoredTaskCreationResult(status="missing_video", reason="video_not_found")
    if video.status != "new":
        return MonitoredTaskCreationResult(status="skipped", reason=f"video_status={video.status}")
    if not creator.user_id:
        return MonitoredTaskCreationResult(status="skipped", reason="creator_has_no_owner")

    owner_user = db.query(User).filter(User.id == creator.user_id).with_for_update().first()
    if not owner_user:
        return MonitoredTaskCreationResult(status="missing_user", reason="owner_not_found")

    is_admin_user = _is_admin_user(owner_user)
    if settings.DEMO_MODE or is_admin_user:
        cost = Decimal("0.00")
    else:
        pricing = get_pricing(db)
        cost = Decimal(str(pricing.price_success))
        current_balance = owner_user.balance or Decimal("0.00")
        if current_balance < cost:
            return MonitoredTaskCreationResult(status="insufficient_funds", cost=cost)

    boost_likes_enabled, boost_likes_quantity = get_boost_likes_config_sync(db)
    task_boost_likes_enabled = boost_likes_enabled and bool(_template_reply_texts(template))
    likes_for_task = boost_likes_quantity if task_boost_likes_enabled else 0
    task_settings = _build_monitor_task_settings(
        creator,
        video,
        template,
        origin,
        created_from,
        task_boost_likes_enabled,
        boost_likes_quantity,
    )
    invalid_text_reason = _validate_monitor_text_variants(task_settings)
    if invalid_text_reason:
        return MonitoredTaskCreationResult(status="invalid_template", reason=invalid_text_reason, cost=cost)

    task = Task(
        user_id=creator.user_id,
        status="pending",
        main_comment=_selected_main_comment(task_settings),
        reply1=template.reply1,
        reply2=template.reply2,
        reply3=template.reply3,
        likes=likes_for_task,
        video_urls=[video.url],
        settings=task_settings,
        kind="full",
        total_cost=cost,
        refunded_cost=None,
        video_attempts={},
    )
    db.add(task)
    try:
        db.flush()

        if cost > 0:
            owner_user.balance = (owner_user.balance or Decimal("0.00")) - cost
            db.add(
                Transaction(
                    user_id=creator.user_id,
                    task_id=task.id,
                    amount=-cost,
                    type="hold",
                    description=f"Monitoring order for video #{video.id} from @{creator.username}",
                )
            )

        video.status = "queued"
        video.task_id = task.id
        reserve_task_for_enqueue(db, task.id)
        db.commit()
    except Exception as e:
        db.rollback()
        logger.error(f"Failed to commit monitored task for video {video.id}: {e}", exc_info=True)
        return MonitoredTaskCreationResult(status="db_error", reason=str(e)[:200], cost=cost)

    finalize_reserved_enqueue(db, task.id)
    return MonitoredTaskCreationResult(status="created", task_id=task.id, cost=cost)


async def create_monitored_task_async(
    db: AsyncSession,
    creator: CreatorSource,
    video_id: int,
    template: TaskTemplate,
    *,
    origin: str,
    created_from: str,
) -> MonitoredTaskCreationResult:
    video_result = await db.execute(
        select(CreatorVideo).filter(CreatorVideo.id == video_id).with_for_update()
    )
    video = video_result.scalar_one_or_none()
    if not video:
        return MonitoredTaskCreationResult(status="missing_video", reason="video_not_found")
    if video.status != "new":
        return MonitoredTaskCreationResult(status="skipped", reason=f"video_status={video.status}")
    if not creator.user_id:
        return MonitoredTaskCreationResult(status="skipped", reason="creator_has_no_owner")

    user_result = await db.execute(
        select(User).filter(User.id == creator.user_id).with_for_update()
    )
    owner_user = user_result.scalar_one_or_none()
    if not owner_user:
        return MonitoredTaskCreationResult(status="missing_user", reason="owner_not_found")

    is_admin_user = _is_admin_user(owner_user)
    if settings.DEMO_MODE or is_admin_user:
        cost = Decimal("0.00")
    else:
        pricing = await get_pricing_async(db)
        cost = Decimal(str(pricing.price_success))
        current_balance = owner_user.balance or Decimal("0.00")
        if current_balance < cost:
            return MonitoredTaskCreationResult(status="insufficient_funds", cost=cost)

    boost_likes_enabled, boost_likes_quantity = await get_boost_likes_config_async(db)
    task_boost_likes_enabled = boost_likes_enabled and bool(_template_reply_texts(template))
    likes_for_task = boost_likes_quantity if task_boost_likes_enabled else 0
    task_settings = _build_monitor_task_settings(
        creator,
        video,
        template,
        origin,
        created_from,
        task_boost_likes_enabled,
        boost_likes_quantity,
    )
    invalid_text_reason = _validate_monitor_text_variants(task_settings)
    if invalid_text_reason:
        return MonitoredTaskCreationResult(status="invalid_template", reason=invalid_text_reason, cost=cost)

    task = Task(
        user_id=creator.user_id,
        status="pending",
        main_comment=_selected_main_comment(task_settings),
        reply1=template.reply1,
        reply2=template.reply2,
        reply3=template.reply3,
        likes=likes_for_task,
        video_urls=[video.url],
        settings=task_settings,
        kind="full",
        total_cost=cost,
        refunded_cost=None,
        video_attempts={},
    )
    db.add(task)
    try:
        await db.flush()

        if cost > 0:
            owner_user.balance = (owner_user.balance or Decimal("0.00")) - cost
            db.add(
                Transaction(
                    user_id=creator.user_id,
                    task_id=task.id,
                    amount=-cost,
                    type="hold",
                    description=f"Monitoring order for video #{video.id} from @{creator.username}",
                )
            )

        video.status = "queued"
        video.task_id = task.id
        await reserve_task_for_enqueue_async(db, task.id)
        await db.commit()
    except Exception as e:
        await db.rollback()
        logger.error(f"Failed to commit monitored task for video {video.id}: {e}", exc_info=True)
        return MonitoredTaskCreationResult(status="db_error", reason=str(e)[:200], cost=cost)

    await finalize_reserved_enqueue_async(db, task.id)
    return MonitoredTaskCreationResult(status="created", task_id=task.id, cost=cost)


def create_monitored_tasks_batch_sync(
    db: Session,
    creator: CreatorSource,
    video_ids: list[int],
    template: TaskTemplate,
    *,
    origin: str,
    created_from: str,
) -> MonitoredTaskBatchResult:
    """
    Creates monitoring tasks in one transactional batch.

    WHY:
    - reads pricing/settings/user only once
    - locks videos/user once
    - commits once
    - finalizes enqueue after the transaction
    """
    if not video_ids:
        return MonitoredTaskBatchResult(status="skipped", created_video_ids=[], task_ids=[], reason="no_videos")
    if not creator.user_id:
        return MonitoredTaskBatchResult(status="skipped", created_video_ids=[], task_ids=[], reason="creator_has_no_owner")

    unique_video_ids = list(dict.fromkeys(int(video_id) for video_id in video_ids))
    videos = (
        db.query(CreatorVideo)
        .filter(CreatorVideo.id.in_(unique_video_ids), CreatorVideo.creator_id == creator.id)
        .with_for_update()
        .all()
    )
    video_map = {video.id: video for video in videos}
    ordered_videos = [video_map[video_id] for video_id in unique_video_ids if video_id in video_map]
    candidate_videos = [
        video
        for video in ordered_videos
        if video.status == "new" and video.task_id is None
    ]

    if not candidate_videos:
        return MonitoredTaskBatchResult(status="skipped", created_video_ids=[], task_ids=[], reason="no_new_videos")

    owner_user = db.query(User).filter(User.id == creator.user_id).with_for_update().first()
    if not owner_user:
        return MonitoredTaskBatchResult(status="missing_user", created_video_ids=[], task_ids=[], reason="owner_not_found")

    is_admin_user = _is_admin_user(owner_user)
    if settings.DEMO_MODE or is_admin_user:
        cost_per_task = Decimal("0.00")
    else:
        pricing = get_pricing(db)
        cost_per_task = Decimal(str(pricing.price_success))

    available_balance = owner_user.balance or Decimal("0.00")
    skipped_for_balance: list[int] = []
    if cost_per_task > 0:
        affordable_count = int(available_balance // cost_per_task)
        if affordable_count <= 0:
            return MonitoredTaskBatchResult(
                status="insufficient_funds",
                created_video_ids=[],
                task_ids=[],
                total_cost=Decimal("0.00"),
                reason="insufficient_funds",
                skipped_video_ids=[video.id for video in candidate_videos],
            )
        if affordable_count < len(candidate_videos):
            skipped_for_balance = [video.id for video in candidate_videos[affordable_count:]]
        candidate_videos = candidate_videos[:affordable_count]

    boost_likes_enabled, boost_likes_quantity = get_boost_likes_config_sync(db)
    task_boost_likes_enabled = boost_likes_enabled and bool(_template_reply_texts(template))
    likes_for_task = boost_likes_quantity if task_boost_likes_enabled else 0

    created_pairs: list[tuple[CreatorVideo, Task, Decimal]] = []
    total_cost = Decimal("0.00")

    for video_index, video in enumerate(candidate_videos):
        task_settings = _build_monitor_task_settings(
            creator,
            video,
            template,
            origin,
            created_from,
            task_boost_likes_enabled,
            boost_likes_quantity,
            text_variant_offset=video_index,
        )
        invalid_text_reason = _validate_monitor_text_variants(task_settings)
        if invalid_text_reason:
            return MonitoredTaskBatchResult(
                status="invalid_template",
                created_video_ids=[],
                task_ids=[],
                total_cost=Decimal("0.00"),
                reason=invalid_text_reason,
                skipped_video_ids=[video.id for video in candidate_videos],
            )
        task = Task(
            user_id=creator.user_id,
            status="pending",
            main_comment=_selected_main_comment(task_settings),
            reply1=template.reply1,
            reply2=template.reply2,
            reply3=template.reply3,
            likes=likes_for_task,
            video_urls=[video.url],
            settings=task_settings,
            kind="full",
            total_cost=cost_per_task,
            refunded_cost=None,
            video_attempts={},
        )
        db.add(task)
        created_pairs.append((video, task, cost_per_task))
        total_cost += cost_per_task

    task_ids: list[int] = []
    created_video_ids: list[int] = []

    try:
        db.flush()

        if total_cost > 0:
            owner_user.balance = (owner_user.balance or Decimal("0.00")) - total_cost

        for video, task, task_cost in created_pairs:
            if task_cost > 0:
                db.add(
                    Transaction(
                        user_id=creator.user_id,
                        task_id=task.id,
                        amount=-task_cost,
                        type="hold",
                        description=f"Monitoring order for video #{video.id} from @{creator.username}",
                    )
                )

            video.status = "queued"
            video.task_id = task.id
            reserve_task_for_enqueue(db, task.id)
            task_ids.append(task.id)
            created_video_ids.append(video.id)

        db.commit()
    except Exception as e:
        db.rollback()
        logger.error(
            f"Failed to commit monitoring task batch for creator {creator.id}: {e}",
            exc_info=True,
        )
        return MonitoredTaskBatchResult(
            status="db_error",
            created_video_ids=[],
            task_ids=[],
            total_cost=total_cost,
            reason=str(e)[:200],
        )

    for task_id in task_ids:
        finalize_reserved_enqueue(db, task_id)

    return MonitoredTaskBatchResult(
        status="created",
        created_video_ids=created_video_ids,
        task_ids=task_ids,
        total_cost=total_cost,
        reason="partial_insufficient_funds" if skipped_for_balance else None,
        skipped_video_ids=skipped_for_balance,
    )
