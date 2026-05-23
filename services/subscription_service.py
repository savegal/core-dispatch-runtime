from datetime import datetime, timedelta, timezone
from sqlalchemy.orm import Session
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import and_, select
from db.models import Subscription, User
from config.settings import get_settings
import logging

settings = get_settings()
logger = logging.getLogger(__name__)


def user_has_active_subscription(db: Session, user_id: int) -> bool:
    """
    Проверяет наличие активной подписки у пользователя.
    
    Owner и admin всегда имеют доступ.
    """
    if settings.DEMO_MODE:
        return True

    # Проверяем, является ли пользователь owner/admin
    user = db.query(User).filter(User.id == user_id).first()
    if user:
        is_owner = settings.OWNER_TELEGRAM_ID and user.telegram_id == settings.OWNER_TELEGRAM_ID
        if user.role in ("admin", "owner") or is_owner:
            return True

    now = datetime.now(timezone.utc)
    subscription = db.query(Subscription).filter(
        and_(
            Subscription.user_id == user_id,
            Subscription.expire_at > now
        )
    ).first()

    return subscription is not None


def grant_subscription(db: Session, user_id: int, days: int = 30) -> Subscription:
    """Выдаёт или продлевает подписку пользователю."""
    try:
        # Проверяем существующую подписку
        existing = db.query(Subscription).filter(
            Subscription.user_id == user_id
        ).order_by(Subscription.expire_at.desc()).first()

        now = datetime.now(timezone.utc)

        if existing and existing.expire_at > now:
            # Продлеваем существующую
            existing.expire_at = existing.expire_at + timedelta(days=days)
            db.commit()
            db.refresh(existing)
            return existing
        else:
            # Создаём новую
            if existing:
                # Продлеваем от текущего момента
                expire_at = now + timedelta(days=days)
            else:
                expire_at = now + timedelta(days=days)

            subscription = Subscription(
                user_id=user_id,
                tier="standard",
                expire_at=expire_at
            )
            db.add(subscription)
            db.commit()
            db.refresh(subscription)
            return subscription
    except Exception as e:
        db.rollback()
        logger = logging.getLogger(__name__)
        logger.error(f"Error granting subscription for user {user_id}: {e}", exc_info=True)
        raise


def revoke_subscription(db: Session, user_id: int) -> bool:
    """Отзывает подписку (устанавливает expire_at = now())."""
    try:
        now = datetime.now(timezone.utc)
        subscriptions = db.query(Subscription).filter(
            and_(
                Subscription.user_id == user_id,
                Subscription.expire_at > now
            )
        ).all()

        for sub in subscriptions:
            sub.expire_at = now

        db.commit()
        return len(subscriptions) > 0
    except Exception as e:
        db.rollback()
        logger.error(f"Error revoking subscription for user {user_id}: {e}", exc_info=True)
        raise


def get_user_subscription(db: Session, user_id: int) -> Subscription | None:
    """Возвращает активную подписку пользователя (синхронная версия для executor)."""
    now = datetime.now(timezone.utc)
    return db.query(Subscription).filter(
        and_(
            Subscription.user_id == user_id,
            Subscription.expire_at > now
        )
    ).order_by(Subscription.expire_at.desc()).first()


async def get_user_subscription_async(db: AsyncSession, user_id: int) -> Subscription | None:
    """ИСПРАВЛЕНО: Async версия - возвращает активную подписку пользователя."""
    now = datetime.now(timezone.utc)
    result = await db.execute(
        select(Subscription)
        .filter(
            and_(
                Subscription.user_id == user_id,
                Subscription.expire_at > now
            )
        )
        .order_by(Subscription.expire_at.desc())
    )
    return result.scalar_one_or_none()


async def user_has_active_subscription_async(db: AsyncSession, user_id: int) -> bool:
    """
    ИСПРАВЛЕНО: Async версия - проверяет наличие активной подписки у пользователя.
    
    Owner и admin всегда имеют доступ.
    """
    if settings.DEMO_MODE:
        return True

    # Проверяем, является ли пользователь owner/admin
    result = await db.execute(select(User).filter(User.id == user_id))
    user = result.scalar_one_or_none()
    
    if user:
        is_owner = settings.OWNER_TELEGRAM_ID and user.telegram_id == settings.OWNER_TELEGRAM_ID
        if user.role in ("admin", "owner") or is_owner:
            return True

    now = datetime.now(timezone.utc)
    result = await db.execute(
        select(Subscription)
        .filter(
            and_(
                Subscription.user_id == user_id,
                Subscription.expire_at > now
            )
        )
    )
    subscription = result.scalar_one_or_none()

    return subscription is not None

