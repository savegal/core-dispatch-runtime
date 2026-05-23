from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, field_validator
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import and_, func, or_, select
from sqlalchemy.exc import IntegrityError
from db.database import get_db
from db.models import (
    Account,
    AdminAction,
    CommentTemplate,
    CreatorSource,
    CreatorVideo,
    Subscription,
    Task,
    TaskTemplate,
    Transaction,
    User,
)
from services.balance_service import (
    add_balance_async, subtract_balance_async, get_user_balance_async
)
from api.auth import verify_admin_header
from config.settings import get_settings
from datetime import datetime, timezone
from decimal import Decimal
import logging
from config.limiter import limiter

router = APIRouter(prefix="/api/users", tags=["users"], dependencies=[Depends(verify_admin_header)])
settings = get_settings()
logger = logging.getLogger(__name__)


def _iso_or_none(value) -> str | None:
    return value.isoformat() if value else None


def _float_or_zero(value) -> float:
    if value is None:
        return 0.0
    return float(value)


def _safe_video_count(video_urls) -> int:
    if isinstance(video_urls, list):
        return len(video_urls)
    return 0


def _task_origin(settings_data) -> str:
    if not isinstance(settings_data, dict):
        return "manual"
    origin = settings_data.get("origin") or settings_data.get("source")
    if origin:
        return str(origin)
    if settings_data.get("creator_username") or settings_data.get("monitoring_source"):
        return "monitoring"
    return "manual"


def _is_future_datetime(value) -> bool:
    if not value:
        return False
    try:
        dt = value
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt > datetime.now(timezone.utc)
    except Exception:
        return False


class SyncTelegramRequest(BaseModel):
    telegram_id: int
    username: str | None = None


class SyncTelegramResponse(BaseModel):
    id: int
    role: str
    is_blocked: bool = False
    full_access_override: bool = False
    full_access: bool = False
    # True ровно один раз — в ответе на первый sync_telegram, который
    # фактически создал запись в БД. Бот использует это, чтобы решить,
    # отправлять ли владельцу уведомление о новом работнике.
    is_new: bool = False
    # True, если пользователь ждёт ручного одобрения админом.
    # Считаем пендингом: is_blocked=True AND was_approved_once=False
    # AND role не admin/owner. После первого unblock админом — навсегда False.
    is_pending_approval: bool = False


@router.post("/sync_telegram", response_model=SyncTelegramResponse)
@limiter.limit("30/minute")  # ИСПРАВЛЕНО: Rate limiting
async def sync_telegram(
    request: Request,
    sync_request: SyncTelegramRequest, 
    db: AsyncSession = Depends(get_db)
):
    """Синхронизирует или создаёт пользователя по Telegram ID."""
    try:
        # ИСПРАВЛЕНО: Используем async запросы
        result = await db.execute(select(User).filter(User.telegram_id == sync_request.telegram_id))
        user = result.scalar_one_or_none()

        is_new_user = False

        if not user:
            # Создаём нового пользователя
            role = "owner" if settings.OWNER_TELEGRAM_ID == sync_request.telegram_id else "user"
            requires_approval = bool(getattr(settings, "REQUIRE_WORKER_APPROVAL", True))
            preapproved = role in ("admin", "owner")
            user = User(
                telegram_id=sync_request.telegram_id,
                username=sync_request.username,
                role=role,
                is_blocked=requires_approval and not preapproved,
                daily_video_limit=50,
                today_videos=0,
                # admin/owner сразу считаются одобренными;
                # обычный новый юзер — нет, ждёт ручного одобрения.
                was_approved_once=preapproved,
            )
            db.add(user)
            try:
                await db.commit()
                await db.refresh(user)
                is_new_user = True
                logger.info(
                    "Created new user: telegram_id=%s, role=%s, is_blocked=%s",
                    sync_request.telegram_id,
                    role,
                    user.is_blocked,
                )
            except IntegrityError:
                # Параллельные sync_telegram для того же telegram_id: второй INSERT ловит UNIQUE.
                await db.rollback()
                result = await db.execute(
                    select(User).filter(User.telegram_id == sync_request.telegram_id)
                )
                user = result.scalar_one_or_none()
                if user is None:
                    raise
                logger.info(
                    "User already exists (concurrent sync): telegram_id=%s, id=%s",
                    sync_request.telegram_id,
                    user.id,
                )

        if user and sync_request.username and user.username != sync_request.username:
            user.username = sync_request.username
            if settings.OWNER_TELEGRAM_ID == sync_request.telegram_id:
                user.role = "owner"
                # Если owner вдруг создавался до того, как был выставлен OWNER_TELEGRAM_ID,
                # сразу подтягиваем флаг одобрения, чтобы бот не показывал ему экран ожидания.
                if not bool(getattr(user, "was_approved_once", False)):
                    user.was_approved_once = True
            await db.commit()
            await db.refresh(user)
            logger.info(f"Updated user {user.id}: username={sync_request.username}")

        # Безопасный getattr на случай старой БД без миграции —
        # бот тогда увидит is_pending_approval=False и просто пойдёт по старому пути.
        was_approved_once = bool(getattr(user, "was_approved_once", True))
        is_pending = (
            bool(user.is_blocked)
            and not was_approved_once
            and user.role not in ("admin", "owner")
        )

        return SyncTelegramResponse(
            id=user.id,
            role=user.role,
            is_blocked=bool(user.is_blocked),
            full_access_override=bool(getattr(user, "full_access_override", False)),
            full_access=(
                user.role in ("admin", "owner")
                or (
                    bool(getattr(user, "full_access_override", False))
                    and not bool(user.is_blocked)
                )
            ),
            is_new=is_new_user,
            is_pending_approval=is_pending,
        )
    except Exception as e:
        await db.rollback()
        logger.error(f"Error syncing telegram user {sync_request.telegram_id}: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Database error: {str(e)[:200]}")


@router.get("")
@limiter.limit("60/minute")  # ИСПРАВЛЕНО: Rate limiting
async def list_users(
    request: Request,
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
    status: str = Query("all", pattern="^(all|active|blocked|pending|admins)$"),
    q: str | None = Query(None, max_length=80),
    db: AsyncSession = Depends(get_db)
):
    """Получает список пользователей с информацией о балансе."""
    try:
        # ИСПРАВЛЕНО: Используем async запросы
        filters = []
        if status == "active":
            filters.append(User.is_blocked.is_(False))
        elif status == "blocked":
            filters.append(
                and_(
                    User.is_blocked.is_(True),
                    User.was_approved_once.is_(True),
                    User.role.notin_(("admin", "owner")),
                )
            )
        elif status == "pending":
            filters.append(
                and_(
                    User.is_blocked.is_(True),
                    User.was_approved_once.is_(False),
                    User.role.notin_(("admin", "owner")),
                )
            )
        elif status == "admins":
            filters.append(User.role.in_(("admin", "owner")))

        query_text = (q or "").strip()
        if query_text:
            search_filters = [func.lower(User.username).like(f"%{query_text.lower()}%")]
            if query_text.isdigit():
                numeric_query = int(query_text)
                search_filters.extend([
                    User.id == numeric_query,
                    User.telegram_id == numeric_query,
                ])
            filters.append(or_(*search_filters))

        total_stmt = select(func.count(User.id))
        if filters:
            total_stmt = total_stmt.where(*filters)
        total = int(await db.scalar(total_stmt) or 0)

        if total > 0 and offset >= total:
            offset = ((total - 1) // limit) * limit

        users_stmt = (
            select(User)
            .order_by(User.created_at.desc(), User.id.desc())
            .offset(offset)
            .limit(limit)
        )
        if filters:
            users_stmt = users_stmt.where(*filters)

        users_result = await db.execute(users_stmt)
        users = users_result.scalars().all()
        
        result = []
        
        for user in users:
            balance = await get_user_balance_async(db, user.id)
            was_approved_once = bool(getattr(user, "was_approved_once", True))
            is_pending = (
                bool(user.is_blocked)
                and not was_approved_once
                and user.role not in ("admin", "owner")
            )
            result.append({
                "id": user.id,
                "telegram_id": user.telegram_id,
                "username": user.username,
                "role": user.role,
                "balance": float(balance),
                "is_blocked": user.is_blocked,
                "was_approved_once": was_approved_once,
                "is_pending_approval": is_pending,
                "bot_access": not bool(user.is_blocked),
                "full_access_override": bool(getattr(user, "full_access_override", False)),
                "full_access": (
                    user.role in ("admin", "owner")
                    or (
                        bool(getattr(user, "full_access_override", False))
                        and not bool(user.is_blocked)
                    )
                ),
                "created_at": user.created_at.isoformat() if user.created_at else None
            })
        
        async def _count_users(*where_clauses) -> int:
            stmt = select(func.count(User.id))
            if where_clauses:
                stmt = stmt.where(*where_clauses)
            return int(await db.scalar(stmt) or 0)

        counts = {
            "total": await _count_users(),
            "active": await _count_users(User.is_blocked.is_(False)),
            "blocked": await _count_users(
                User.is_blocked.is_(True),
                User.was_approved_once.is_(True),
                User.role.notin_(("admin", "owner")),
            ),
            "pending": await _count_users(
                User.is_blocked.is_(True),
                User.was_approved_once.is_(False),
                User.role.notin_(("admin", "owner")),
            ),
            "admins": await _count_users(User.role.in_(("admin", "owner"))),
            "positive_balance": await _count_users(User.balance > 0),
        }

        total_pages = max(1, (total + limit - 1) // limit)
        page = (offset // limit) + 1 if total else 1

        return {
            "items": result,
            "total": total,
            "limit": limit,
            "offset": offset,
            "page": page,
            "total_pages": total_pages,
            "filters": {"status": status, "q": query_text},
            "counts": counts,
        }
    except Exception as e:
        logger.error(f"Error listing users: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Database error: {str(e)[:200]}")




@router.get("/{user_id}")
@limiter.limit("60/minute")  # ИСПРАВЛЕНО: Rate limiting
async def get_user(
    request: Request,
    user_id: int, 
    db: AsyncSession = Depends(get_db)
):
    """Получает информацию о пользователе, включая баланс и статус блокировки."""
    try:
        result = await db.execute(select(User).filter(User.id == user_id))
        user = result.scalar_one_or_none()
        
        if not user:
            raise HTTPException(status_code=404, detail="User not found")
        
        balance = await get_user_balance_async(db, user_id)
        
        was_approved_once = bool(getattr(user, "was_approved_once", True))
        is_pending = (
            bool(user.is_blocked)
            and not was_approved_once
            and user.role not in ("admin", "owner")
        )

        task_status_rows = await db.execute(
            select(Task.status, func.count(Task.id))
            .where(Task.user_id == user_id)
            .group_by(Task.status)
        )
        task_by_status = {str(row[0] or "unknown"): int(row[1] or 0) for row in task_status_rows.all()}
        task_total = sum(task_by_status.values())
        active_task_count = sum(task_by_status.get(status, 0) for status in ("pending", "in_progress"))

        task_cost_row = (
            await db.execute(
                select(
                    func.coalesce(func.sum(Task.total_cost), 0),
                    func.coalesce(func.sum(Task.refunded_cost), 0),
                ).where(Task.user_id == user_id)
            )
        ).one()
        total_cost = _float_or_zero(task_cost_row[0])
        refunded_cost = _float_or_zero(task_cost_row[1])

        recent_tasks_result = await db.execute(
            select(Task)
            .where(Task.user_id == user_id)
            .order_by(Task.created_at.desc(), Task.id.desc())
            .limit(5)
        )
        recent_tasks = []
        for task in recent_tasks_result.scalars().all():
            settings_data = task.settings if isinstance(task.settings, dict) else {}
            recent_tasks.append({
                "id": task.id,
                "status": task.status,
                "kind": task.kind,
                "progress": int(task.progress or 0),
                "video_count": _safe_video_count(task.video_urls),
                "total_cost": _float_or_zero(task.total_cost),
                "refunded_cost": _float_or_zero(task.refunded_cost),
                "final_cost": _float_or_zero(task.total_cost) - _float_or_zero(task.refunded_cost),
                "origin": _task_origin(settings_data),
                "template_id": settings_data.get("template_id"),
                "template_name": settings_data.get("template_name"),
                "creator_username": settings_data.get("creator_username"),
                "error_reason": task.error_reason,
                "created_at": _iso_or_none(task.created_at),
            })

        transaction_rows = await db.execute(
            select(Transaction.type, func.count(Transaction.id), func.coalesce(func.sum(Transaction.amount), 0))
            .where(Transaction.user_id == user_id)
            .group_by(Transaction.type)
        )
        transactions_by_type = {
            str(row[0] or "unknown"): {
                "count": int(row[1] or 0),
                "amount": _float_or_zero(row[2]),
            }
            for row in transaction_rows.all()
        }
        latest_transactions_result = await db.execute(
            select(Transaction)
            .where(Transaction.user_id == user_id)
            .order_by(Transaction.created_at.desc(), Transaction.id.desc())
            .limit(5)
        )
        latest_transactions = [
            {
                "id": transaction.id,
                "task_id": transaction.task_id,
                "amount": _float_or_zero(transaction.amount),
                "type": transaction.type,
                "description": transaction.description,
                "created_at": _iso_or_none(transaction.created_at),
            }
            for transaction in latest_transactions_result.scalars().all()
        ]

        subscription_result = await db.execute(
            select(Subscription)
            .where(Subscription.user_id == user_id)
            .order_by(Subscription.expire_at.desc(), Subscription.id.desc())
            .limit(1)
        )
        subscription = subscription_result.scalar_one_or_none()

        template_counts_row = (
            await db.execute(
                select(
                    select(func.count(TaskTemplate.id)).where(TaskTemplate.user_id == user_id).scalar_subquery(),
                    select(func.count(CommentTemplate.id)).where(CommentTemplate.user_id == user_id).scalar_subquery(),
                )
            )
        ).one()
        recent_templates_result = await db.execute(
            select(TaskTemplate)
            .where(TaskTemplate.user_id == user_id)
            .order_by(TaskTemplate.created_at.desc(), TaskTemplate.id.desc())
            .limit(3)
        )
        recent_templates = [
            {
                "id": template.id,
                "name": template.name,
                "likes": int(template.likes or 0),
                "reply_count": len([
                    reply
                    for reply in (template.reply1, template.reply2, template.reply3)
                    if reply
                ]),
                "created_at": _iso_or_none(template.created_at),
            }
            for template in recent_templates_result.scalars().all()
        ]
        recent_comment_templates_result = await db.execute(
            select(CommentTemplate)
            .where(CommentTemplate.user_id == user_id)
            .order_by(CommentTemplate.created_at.desc(), CommentTemplate.id.desc())
            .limit(3)
        )
        recent_comment_templates = [
            {
                "id": template.id,
                "name": template.name,
                "type": template.type,
                "created_at": _iso_or_none(template.created_at),
            }
            for template in recent_comment_templates_result.scalars().all()
        ]

        creator_count_rows = await db.execute(
            select(CreatorSource.mode, func.count(CreatorSource.id))
            .where(CreatorSource.user_id == user_id)
            .group_by(CreatorSource.mode)
        )
        creators_by_mode = {str(row[0] or "unknown"): int(row[1] or 0) for row in creator_count_rows.all()}
        creators_total = sum(creators_by_mode.values())
        creators_enabled = int(await db.scalar(
            select(func.count(CreatorSource.id)).where(
                CreatorSource.user_id == user_id,
                CreatorSource.enabled.is_(True),
            )
        ) or 0)
        video_status_rows = await db.execute(
            select(CreatorVideo.status, func.count(CreatorVideo.id))
            .join(CreatorSource, CreatorVideo.creator_id == CreatorSource.id)
            .where(CreatorSource.user_id == user_id)
            .group_by(CreatorVideo.status)
        )
        videos_by_status = {str(row[0] or "unknown"): int(row[1] or 0) for row in video_status_rows.all()}
        latest_creators_result = await db.execute(
            select(CreatorSource)
            .where(CreatorSource.user_id == user_id)
            .order_by(CreatorSource.updated_at.desc(), CreatorSource.id.desc())
            .limit(5)
        )
        latest_creators = [
            {
                "id": creator.id,
                "username": creator.username,
                "enabled": bool(creator.enabled),
                "mode": creator.mode,
                "interval_minutes": int(creator.interval_minutes or 0),
                "default_template_id": creator.default_template_id,
                "created_at": _iso_or_none(creator.created_at),
                "updated_at": _iso_or_none(creator.updated_at),
            }
            for creator in latest_creators_result.scalars().all()
        ]

        account_status_rows = await db.execute(
            select(Account.status, func.count(Account.id))
            .where(Account.owner_user_id == user_id)
            .group_by(Account.status)
        )
        personal_accounts_by_status = {str(row[0] or "unknown"): int(row[1] or 0) for row in account_status_rows.all()}
        personal_accounts_total = sum(personal_accounts_by_status.values())
        personal_accounts_activity = (
            await db.execute(
                select(
                    func.coalesce(func.sum(Account.today_main_comments), 0),
                    func.coalesce(func.sum(Account.today_replies), 0),
                ).where(Account.owner_user_id == user_id)
            )
        ).one()
        latest_personal_accounts_result = await db.execute(
            select(Account)
            .where(Account.owner_user_id == user_id)
            .order_by(Account.updated_at.desc(), Account.id.desc())
            .limit(5)
        )
        latest_personal_accounts = [
            {
                "id": account.id,
                "label": account.label,
                "status": account.status,
                "country_code": account.country_code,
                "today_main_comments": int(account.today_main_comments or 0),
                "today_replies": int(account.today_replies or 0),
                "last_validation_ok": _iso_or_none(account.last_validation_ok),
                "last_error": account.last_error,
                "updated_at": _iso_or_none(account.updated_at),
            }
            for account in latest_personal_accounts_result.scalars().all()
        ]

        admin_actions_result = await db.execute(
            select(AdminAction)
            .where(AdminAction.target_user_id == user_id)
            .order_by(AdminAction.created_at.desc(), AdminAction.id.desc())
            .limit(5)
        )
        recent_admin_actions = [
            {
                "id": action.id,
                "admin_telegram_id": action.admin_telegram_id,
                "admin_username": action.admin_username,
                "action_type": action.action_type,
                "description": action.description,
                "created_at": _iso_or_none(action.created_at),
            }
            for action in admin_actions_result.scalars().all()
        ]

        return {
            "id": user.id,
            "telegram_id": user.telegram_id,
            "username": user.username,
            "role": user.role,
            "balance": float(balance),
            "is_blocked": user.is_blocked,
            "was_approved_once": was_approved_once,
            "is_pending_approval": is_pending,
            "bot_access": not bool(user.is_blocked),
            "full_access_override": bool(getattr(user, "full_access_override", False)),
            "full_access": (
                user.role in ("admin", "owner")
                or (
                    bool(getattr(user, "full_access_override", False))
                    and not bool(user.is_blocked)
                )
            ),
            "daily_video_limit": user.daily_video_limit,
            "today_videos": user.today_videos,
            "last_video_date": _iso_or_none(user.last_video_date),
            "video_notifications_enabled": bool(getattr(user, "video_notifications_enabled", True)),
            "created_at": _iso_or_none(user.created_at),
            "updated_at": _iso_or_none(user.updated_at),
            "summary": {
                "subscription": {
                    "tier": subscription.tier if subscription else None,
                    "expire_at": _iso_or_none(subscription.expire_at) if subscription else None,
                    "active": _is_future_datetime(subscription.expire_at) if subscription else False,
                },
                "orders": {
                    "total": task_total,
                    "active": active_task_count,
                    "by_status": task_by_status,
                    "total_cost": total_cost,
                    "refunded_cost": refunded_cost,
                    "final_cost": total_cost - refunded_cost,
                    "recent": recent_tasks,
                },
                "finance": {
                    "balance": float(balance),
                    "transactions_by_type": transactions_by_type,
                    "latest_transactions": latest_transactions,
                },
                "templates": {
                    "task_templates": int(template_counts_row[0] or 0),
                    "comment_templates": int(template_counts_row[1] or 0),
                    "recent_task_templates": recent_templates,
                    "recent_comment_templates": recent_comment_templates,
                },
                "monitoring": {
                    "creators_total": creators_total,
                    "creators_enabled": creators_enabled,
                    "creators_disabled": max(0, creators_total - creators_enabled),
                    "creators_by_mode": creators_by_mode,
                    "videos_by_status": videos_by_status,
                    "latest_creators": latest_creators,
                    "video_notifications_enabled": bool(getattr(user, "video_notifications_enabled", True)),
                    "daily_video_limit": int(user.daily_video_limit or 0),
                    "today_videos": int(user.today_videos or 0),
                    "last_video_date": _iso_or_none(user.last_video_date),
                },
                "personal_accounts": {
                    "total": personal_accounts_total,
                    "by_status": personal_accounts_by_status,
                    "today_main_comments": int(personal_accounts_activity[0] or 0),
                    "today_replies": int(personal_accounts_activity[1] or 0),
                    "recent": latest_personal_accounts,
                },
                "admin_actions": {
                    "recent": recent_admin_actions,
                },
            }
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error getting user {user_id}: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Database error: {str(e)[:200]}")


class UpdateBalanceRequest(BaseModel):
    """Модель для обновления баланса."""
    amount: float  # Сумма для добавления/вычитания (может быть отрицательной)
    
    # ИСПРАВЛЕНО: Валидация входных данных
    @field_validator('amount')
    @classmethod
    def validate_amount(cls, v: float) -> float:
        """Валидирует сумму."""
        if abs(v) > 1000000:
            raise ValueError('Amount too large (max 1,000,000)')
        return v


@router.post("/{user_id}/balance/add", response_model=dict)
@limiter.limit("20/minute")  # ИСПРАВЛЕНО: Rate limiting
async def add_user_balance(
    request: Request,
    user_id: int,
    balance_request: UpdateBalanceRequest,
    db: AsyncSession = Depends(get_db),
    _: None = Depends(verify_admin_header)
):
    """Добавляет баланс пользователю (только для админов)."""
    try:
        if balance_request.amount <= 0:
            raise HTTPException(status_code=400, detail="amount must be > 0")
        
        result = await db.execute(select(User).filter(User.id == user_id))
        user = result.scalar_one_or_none()
        
        if not user:
            raise HTTPException(status_code=404, detail="User not found")
        
        amount = Decimal(str(balance_request.amount))
        success = await add_balance_async(
            db, 
            user_id, 
            amount,
            description=f"Пополнение баланса администратором: {amount} грн",
            task_id=None
        )
        
        if not success:
            raise HTTPException(status_code=500, detail="Failed to add balance")
        
        # Получаем обновленный баланс
        new_balance = await get_user_balance_async(db, user_id)
        
        logger.info(f"Added balance {amount} to user {user_id}. New balance: {new_balance}")
        
        return {
            "user_id": user_id,
            "added_amount": float(amount),
            "new_balance": float(new_balance)
        }
    except HTTPException:
        raise
    except Exception as e:
        await db.rollback()
        logger.error(f"Error adding balance to user {user_id}: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Database error: {str(e)[:200]}")


@router.post("/{user_id}/balance/subtract", response_model=dict)
@limiter.limit("20/minute")  # ИСПРАВЛЕНО: Rate limiting
async def subtract_user_balance(
    request: Request,
    user_id: int,
    balance_request: UpdateBalanceRequest,
    db: AsyncSession = Depends(get_db),
    _: None = Depends(verify_admin_header)
):
    """Вычитает баланс у пользователя (только для админов)."""
    try:
        if balance_request.amount <= 0:
            raise HTTPException(status_code=400, detail="amount must be > 0")
        
        result = await db.execute(select(User).filter(User.id == user_id))
        user = result.scalar_one_or_none()
        
        if not user:
            raise HTTPException(status_code=404, detail="User not found")
        
        amount = Decimal(str(balance_request.amount))
        current_balance = await get_user_balance_async(db, user_id)
        
        if current_balance < amount:
            raise HTTPException(
                status_code=400, 
                detail=f"Insufficient balance. Current: {float(current_balance)}, Requested: {float(amount)}"
            )
        
        success = await subtract_balance_async(
            db, 
            user_id, 
            amount,
            description=f"Списание баланса администратором: {amount} грн",
            transaction_type="withdrawal"
        )
        
        if not success:
            raise HTTPException(status_code=500, detail="Failed to subtract balance")
        
        # Получаем обновленный баланс
        new_balance = await get_user_balance_async(db, user_id)
        
        logger.info(f"Subtracted balance {amount} from user {user_id}. New balance: {new_balance}")
        
        return {
            "user_id": user_id,
            "subtracted_amount": float(amount),
            "new_balance": float(new_balance)
        }
    except HTTPException:
        raise
    except Exception as e:
        await db.rollback()
        logger.error(f"Error subtracting balance from user {user_id}: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Database error: {str(e)[:200]}")


@router.post("/{user_id}/block", response_model=dict)
@limiter.limit("20/minute")  # ИСПРАВЛЕНО: Rate limiting
async def block_user(
    request: Request,
    user_id: int,
    db: AsyncSession = Depends(get_db),
    _: None = Depends(verify_admin_header)
):
    """Блокирует пользователя (только для админов)."""
    try:
        result = await db.execute(select(User).filter(User.id == user_id))
        user = result.scalar_one_or_none()
        
        if not user:
            raise HTTPException(status_code=404, detail="User not found")
        
        user.is_blocked = True
        user.full_access_override = False
        await db.commit()
        await db.refresh(user)
        
        logger.info(f"Blocked user {user_id}")
        
        return {
            "user_id": user.id,
            "is_blocked": user.is_blocked
        }
    except HTTPException:
        raise
    except Exception as e:
        await db.rollback()
        logger.error(f"Error blocking user {user_id}: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Database error: {str(e)[:200]}")


@router.post("/{user_id}/unblock", response_model=dict)
@limiter.limit("20/minute")  # ИСПРАВЛЕНО: Rate limiting
async def unblock_user(
    request: Request,
    user_id: int,
    db: AsyncSession = Depends(get_db),
    _: None = Depends(verify_admin_header)
):
    """Разблокирует пользователя (только для админов)."""
    try:
        result = await db.execute(select(User).filter(User.id == user_id))
        user = result.scalar_one_or_none()
        
        if not user:
            raise HTTPException(status_code=404, detail="User not found")
        
        user.is_blocked = False
        # Первый успешный unblock одновременно играет роль "одобрить нового
        # работника". Дальше is_blocked может прыгать как угодно, а
        # was_approved_once останется True — бот перестанет показывать экран
        # ожидания и будет показывать обычный "вы заблокированы".
        if not bool(getattr(user, "was_approved_once", False)):
            user.was_approved_once = True
        await db.commit()
        await db.refresh(user)
        
        logger.info(f"Unblocked user {user_id}")
        
        return {
            "user_id": user.id,
            "is_blocked": user.is_blocked
        }
    except HTTPException:
        raise
    except Exception as e:
        await db.rollback()
        logger.error(f"Error unblocking user {user_id}: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Database error: {str(e)[:200]}")


@router.post("/{user_id}/full-access/grant", response_model=dict)
@limiter.limit("20/minute")
async def grant_user_full_access(
    request: Request,
    user_id: int,
    db: AsyncSession = Depends(get_db),
    _: None = Depends(verify_admin_header)
):
    """Выдаёт работнику полный доступ к боту без повышения до admin."""
    try:
        result = await db.execute(select(User).filter(User.id == user_id))
        user = result.scalar_one_or_none()

        if not user:
            raise HTTPException(status_code=404, detail="User not found")

        user.is_blocked = False
        user.full_access_override = True
        if not bool(getattr(user, "was_approved_once", False)):
            user.was_approved_once = True
        await db.commit()
        await db.refresh(user)

        logger.info("Granted full access to user %s", user_id)
        return {
            "user_id": user.id,
            "id": user.id,
            "telegram_id": user.telegram_id,
            "username": user.username,
            "role": user.role,
            "is_blocked": bool(user.is_blocked),
            "full_access_override": bool(user.full_access_override),
            "full_access": True,
        }
    except HTTPException:
        raise
    except Exception as e:
        await db.rollback()
        logger.error(f"Error granting full access to user {user_id}: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Database error: {str(e)[:200]}")


@router.post("/{user_id}/full-access/revoke", response_model=dict)
@limiter.limit("20/minute")
async def revoke_user_full_access(
    request: Request,
    user_id: int,
    db: AsyncSession = Depends(get_db),
    _: None = Depends(verify_admin_header)
):
    """Убирает полный доступ у работника без изменения роли."""
    try:
        result = await db.execute(select(User).filter(User.id == user_id))
        user = result.scalar_one_or_none()

        if not user:
            raise HTTPException(status_code=404, detail="User not found")

        if user.role in ("admin", "owner"):
            raise HTTPException(status_code=400, detail="Cannot revoke full access from admin/owner")

        user.full_access_override = False
        if not bool(getattr(user, "was_approved_once", False)):
            user.was_approved_once = True
        await db.commit()
        await db.refresh(user)

        logger.info("Revoked full access from user %s", user_id)
        return {
            "user_id": user.id,
            "id": user.id,
            "telegram_id": user.telegram_id,
            "username": user.username,
            "role": user.role,
            "is_blocked": bool(user.is_blocked),
            "full_access_override": bool(user.full_access_override),
            "full_access": False,
        }
    except HTTPException:
        raise
    except Exception as e:
        await db.rollback()
        logger.error(f"Error revoking full access from user {user_id}: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Database error: {str(e)[:200]}")
