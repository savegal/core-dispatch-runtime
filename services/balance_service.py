"""
Сервис для работы с балансом пользователей и ценами.
Содержит как синхронные (для executor), так и асинхронные (для API/Bot) методы.
"""
from sqlalchemy.orm import Session
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from decimal import Decimal
from typing import Optional
import logging

from db.models import User, Pricing, Transaction
from config.settings import get_settings

logger = logging.getLogger("services.balance_service")
settings = get_settings()


# --- PRICING (ЦЕНЫ) ---

DEFAULT_PRICE_SUCCESS = Decimal("5.00")
DEFAULT_PRICE_FAILURE = Decimal("2.00")


def _normalize_amount(amount: Decimal | int | float | str | None) -> Decimal:
    if amount is None:
        return Decimal("0.00")
    return Decimal(str(amount))


def _validate_pricing_values(
    price_success: Decimal | None,
    price_failure: Decimal | None,
) -> tuple[Decimal | None, Decimal | None]:
    normalized_success = None if price_success is None else _normalize_amount(price_success)
    normalized_failure = None if price_failure is None else _normalize_amount(price_failure)

    if normalized_success is not None and normalized_success < 0:
        raise ValueError("price_success cannot be negative")
    if normalized_failure is not None and normalized_failure < 0:
        raise ValueError("price_failure cannot be negative")
    if (
        normalized_success is not None
        and normalized_failure is not None
        and normalized_failure > normalized_success
    ):
        raise ValueError("price_failure cannot exceed price_success")
    return normalized_success, normalized_failure


def _ensure_pricing_consistency(pricing: Pricing) -> Pricing:
    price_success, price_failure = _validate_pricing_values(
        pricing.price_success,
        pricing.price_failure,
    )
    pricing.price_success = price_success or DEFAULT_PRICE_SUCCESS
    pricing.price_failure = price_failure or DEFAULT_PRICE_FAILURE
    return pricing


def _select_singleton_pricing(db: Session) -> Pricing | None:
    rows = db.query(Pricing).order_by(Pricing.id.asc()).all()
    if not rows:
        return None
    pricing = rows[0]
    extra_rows = rows[1:]
    if extra_rows:
        logger.warning(
            "Detected %s extra pricing rows; keeping id=%s and deleting duplicates",
            len(extra_rows),
            getattr(pricing, "id", None),
        )
        for duplicate in extra_rows:
            db.delete(duplicate)
    return pricing


async def _select_singleton_pricing_async(db: AsyncSession) -> Pricing | None:
    result = await db.execute(select(Pricing).order_by(Pricing.id.asc()))
    rows = list(result.scalars().all())
    if not rows:
        return None
    pricing = rows[0]
    extra_rows = rows[1:]
    if extra_rows:
        logger.warning(
            "Detected %s extra pricing rows; keeping id=%s and deleting duplicates (async)",
            len(extra_rows),
            getattr(pricing, "id", None),
        )
        for duplicate in extra_rows:
            await db.delete(duplicate)
    return pricing

def get_pricing(db: Session) -> Pricing:
    """[Sync] Получает текущие цены."""
    rows = db.query(Pricing).order_by(Pricing.id.asc()).all()
    needs_commit = False
    if not rows:
        pricing = Pricing(
            price_success=DEFAULT_PRICE_SUCCESS,
            price_failure=DEFAULT_PRICE_FAILURE
        )
        db.add(pricing)
        needs_commit = True
    else:
        pricing = rows[0]
        extra_rows = rows[1:]
        if extra_rows:
            logger.warning(
                "Detected %s extra pricing rows; keeping id=%s and deleting duplicates",
                len(extra_rows),
                getattr(pricing, "id", None),
            )
            for duplicate in extra_rows:
                db.delete(duplicate)
            needs_commit = True
    pricing = _ensure_pricing_consistency(pricing)
    if needs_commit:
        db.commit()
        db.refresh(pricing)
    return pricing

async def get_pricing_async(db: AsyncSession) -> Pricing:
    """[Async] Получает текущие цены."""
    result = await db.execute(select(Pricing).order_by(Pricing.id.asc()))
    rows = list(result.scalars().all())
    needs_commit = False
    if not rows:
        pricing = Pricing(
            price_success=DEFAULT_PRICE_SUCCESS,
            price_failure=DEFAULT_PRICE_FAILURE
        )
        db.add(pricing)
        needs_commit = True
    else:
        pricing = rows[0]
        extra_rows = rows[1:]
        if extra_rows:
            logger.warning(
                "Detected %s extra pricing rows; keeping id=%s and deleting duplicates (async)",
                len(extra_rows),
                getattr(pricing, "id", None),
            )
            for duplicate in extra_rows:
                await db.delete(duplicate)
            needs_commit = True
    pricing = _ensure_pricing_consistency(pricing)
    if needs_commit:
        await db.commit()
        await db.refresh(pricing)
    return pricing

def update_pricing(db: Session, price_success: Decimal | None = None, price_failure: Decimal | None = None) -> Pricing:
    """[Sync] Обновляет цены."""
    pricing = get_pricing(db)
    normalized_success = pricing.price_success if price_success is None else price_success
    normalized_failure = pricing.price_failure if price_failure is None else price_failure
    normalized_success, normalized_failure = _validate_pricing_values(normalized_success, normalized_failure)
    pricing.price_success = normalized_success
    pricing.price_failure = normalized_failure
    db.commit()
    db.refresh(pricing)
    return pricing

async def update_pricing_async(db: AsyncSession, price_success: Decimal | None = None, price_failure: Decimal | None = None) -> Pricing:
    """[Async] Обновляет цены."""
    pricing = await get_pricing_async(db)
    normalized_success = pricing.price_success if price_success is None else price_success
    normalized_failure = pricing.price_failure if price_failure is None else price_failure
    normalized_success, normalized_failure = _validate_pricing_values(normalized_success, normalized_failure)
    pricing.price_success = normalized_success
    pricing.price_failure = normalized_failure
    await db.commit()
    await db.refresh(pricing)
    return pricing


# --- BALANCE (БАЛАНС) ---

def get_user_balance(db: Session, user_id: int) -> Decimal:
    """[Sync] Получает баланс пользователя."""
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        return Decimal("0.00")
    return user.balance or Decimal("0.00")

async def get_user_balance_async(db: AsyncSession, user_id: int) -> Decimal:
    """[Async] Получает баланс пользователя."""
    result = await db.execute(select(User).filter(User.id == user_id))
    user = result.scalar_one_or_none()
    if not user:
        return Decimal("0.00")
    return user.balance or Decimal("0.00")

def add_balance(db: Session, user_id: int, amount: Decimal, description: str = None, task_id: int = None) -> bool:
    """
    [Sync] Начисление средств.
    
    ИСПРАВЛЕНО: Использует SELECT FOR UPDATE для предотвращения race conditions.
    """
    try:
        # ИСПРАВЛЕНО: Используем SELECT FOR UPDATE для атомарной операции
        user = db.query(User).filter(User.id == user_id).with_for_update().first()
        if not user:
            logger.error(f"Cannot add balance: User {user_id} not found")
            return False
        
        current = user.balance or Decimal("0.00")
        user.balance = current + amount
        
        # ИСПРАВЛЕНО: Создаём запись Transaction для истории
        transaction = Transaction(
            user_id=user_id,
            task_id=task_id,
            amount=amount,
            type="deposit",
            description=description or f"Начисление средств: {amount} грн"
        )
        db.add(transaction)
        db.commit()
        logger.info(f"Added {amount} to user {user_id}. New balance: {user.balance}")
        return True
    except Exception as e:
        logger.error(f"Error adding balance (sync): {e}")
        db.rollback()
        return False

async def add_balance_async(db: AsyncSession, user_id: int, amount: Decimal, description: str = None, task_id: int = None) -> bool:
    """
    [Async] Начисление средств.
    
    ИСПРАВЛЕНО: Использует SELECT FOR UPDATE для предотвращения race conditions.
    """
    try:
        # ИСПРАВЛЕНО: Используем SELECT FOR UPDATE для атомарной операции
        result = await db.execute(
            select(User).filter(User.id == user_id).with_for_update()
        )
        user = result.scalar_one_or_none()
        if not user:
            logger.error(f"Cannot add balance: User {user_id} not found")
            return False
            
        current = user.balance or Decimal("0.00")
        user.balance = current + amount
        
        # ИСПРАВЛЕНО: Создаём запись Transaction для истории
        transaction = Transaction(
            user_id=user_id,
            task_id=task_id,
            amount=amount,
            type="deposit",
            description=description or f"Начисление средств: {amount} грн"
        )
        db.add(transaction)
        await db.commit()
        logger.info(f"Added {amount} to user {user_id}. New balance: {user.balance}")
        return True
    except Exception as e:
        logger.error(f"Error adding balance (async): {e}")
        await db.rollback()
        return False

def subtract_balance(db: Session, user_id: int, amount: Decimal, description: str = None, task_id: int = None, transaction_type: str = "withdrawal") -> bool:
    """
    [Sync] Списание средств (ранее deduct_balance).
    
    ИСПРАВЛЕНО: Использует SELECT FOR UPDATE для предотвращения race conditions.
    """
    try:
        # ИСПРАВЛЕНО: Используем SELECT FOR UPDATE для атомарной операции
        user = db.query(User).filter(User.id == user_id).with_for_update().first()
        if not user:
            return False
        
        current = user.balance or Decimal("0.00")
        if current < amount:
            logger.warning(f"User {user_id} insufficient funds: {current} < {amount}")
            return False
            
        user.balance = current - amount
        
        # ИСПРАВЛЕНО: Создаём запись Transaction для истории
        transaction = Transaction(
            user_id=user_id,
            task_id=task_id,
            amount=-amount,  # Отрицательное значение для списания
            type=transaction_type,
            description=description or f"Списание средств: {amount} грн"
        )
        db.add(transaction)
        db.commit()
        return True
    except Exception as e:
        logger.error(f"Error subtracting balance (sync): {e}")
        db.rollback()
        return False

async def subtract_balance_async(db: AsyncSession, user_id: int, amount: Decimal, description: str = None, task_id: int = None, transaction_type: str = "withdrawal") -> bool:
    """
    [Async] Списание средств (ранее deduct_balance_async).
    
    ИСПРАВЛЕНО: Использует SELECT FOR UPDATE для предотвращения race conditions.
    """
    try:
        # ИСПРАВЛЕНО: Используем SELECT FOR UPDATE для атомарной операции
        from sqlalchemy import select
        result = await db.execute(
            select(User).filter(User.id == user_id).with_for_update()
        )
        user = result.scalar_one_or_none()
        if not user:
            return False
            
        current = user.balance or Decimal("0.00")
        if current < amount:
            logger.warning(f"User {user_id} insufficient funds: {current} < {amount}")
            return False
            
        user.balance = current - amount
        
        # ИСПРАВЛЕНО: Создаём запись Transaction для истории
        transaction = Transaction(
            user_id=user_id,
            task_id=task_id,
            amount=-amount,  # Отрицательное значение для списания
            type=transaction_type,
            description=description or f"Списание средств: {amount} грн"
        )
        db.add(transaction)
        await db.commit()
        return True
    except Exception as e:
        logger.error(f"Error subtracting balance (async): {e}")
        await db.rollback()
        return False

# Алиасы для совместимости (если где-то в коде осталось старое название)
deduct_balance = subtract_balance
deduct_balance_async = subtract_balance_async


def refund_balance(
    db: Session,
    user_id: int,
    amount: Decimal,
    task_id: int = None,
    description: str = None,
    *,
    commit: bool = True,
) -> bool:
    """
    [Sync] Возврат средств (используется в executor при ошибках).
    
    ИСПРАВЛЕНО: Использует SELECT FOR UPDATE для предотвращения race conditions.
    Создаёт запись Transaction для истории.
    """
    try:
        normalized_amount = _normalize_amount(amount)
        if normalized_amount <= 0:
            logger.warning(f"Skipping refund for user {user_id}: non-positive amount {normalized_amount}")
            return False

        # ИСПРАВЛЕНО: Используем SELECT FOR UPDATE для атомарной операции
        user = db.query(User).filter(User.id == user_id).with_for_update().first()
        if not user:
            return False
        
        current = user.balance or Decimal("0.00")
        user.balance = current + normalized_amount
        
        # ИСПРАВЛЕНО: Создаём запись Transaction для истории
        transaction = Transaction(
            user_id=user_id,
            task_id=task_id,
            amount=normalized_amount,
            type="refund",
            description=description or f"Возврат средств: {normalized_amount} грн"
        )
        db.add(transaction)
        if commit:
            db.commit()
        
        msg = f"Refunded {normalized_amount} to user {user_id}"
        if task_id:
            msg += f" for task {task_id}"
        logger.info(msg)
        return True
    except Exception as e:
        logger.error(f"Error refunding balance: {e}")
        db.rollback()
        return False


# --- UTILS (УТИЛИТЫ) ---

def is_admin(user_or_id) -> bool:
    """
    ИСПРАВЛЕНО: Проверяет админа (берет ID из настроек или проверяет role).
    
    Принимает либо user_id (int), либо объект User (для обратной совместимости).
    Если передан объект User - проверяет role (admin/owner) или telegram_id.
    """
    from db.models import User
    
    # Если передан объект User - проверяем role или telegram_id
    if isinstance(user_or_id, User):
        # Проверяем role (admin/owner)
        if user_or_id.role in ("admin", "owner"):
            return True
        # Проверяем telegram_id с OWNER_TELEGRAM_ID
        if hasattr(settings, 'OWNER_TELEGRAM_ID') and settings.OWNER_TELEGRAM_ID:
            if user_or_id.telegram_id == settings.OWNER_TELEGRAM_ID:
                return True
        # Используем telegram_id для проверки ADMIN_IDS
        user_id = user_or_id.telegram_id
    elif hasattr(user_or_id, 'telegram_id'):
        # Если это объект с telegram_id (но не User) - используем его
        user_id = user_or_id.telegram_id
    elif hasattr(user_or_id, 'id'):
        # Если это объект с id - используем id
        user_id = user_or_id.id
    else:
        # Иначе считаем что это user_id (int)
        user_id = user_or_id
    
    # Проверяем OWNER_TELEGRAM_ID
    if hasattr(settings, 'OWNER_TELEGRAM_ID') and settings.OWNER_TELEGRAM_ID:
        if user_id == settings.OWNER_TELEGRAM_ID:
            return True
    
    # Проверяем ADMIN_IDS
    if hasattr(settings, 'ADMIN_IDS'):
        admin_ids = settings.ADMIN_IDS
        if isinstance(admin_ids, str):
             try:
                 return user_id in [int(x.strip()) for x in admin_ids.split(',')]
             except Exception:
                 pass
        if isinstance(admin_ids, (list, tuple, set)):
            return user_id in admin_ids
    return False


def has_full_functionality_access(user_or_id) -> bool:
    """True when a user may use full bot features despite parsing-only mode.

    Admins/owners always have full functionality. Regular workers only bypass
    parsing mode when an admin explicitly granted the per-worker override and
    the worker is not blocked.
    """
    from db.models import User

    if is_admin(user_or_id):
        return True
    if isinstance(user_or_id, User) or hasattr(user_or_id, "full_access_override"):
        return bool(
            getattr(user_or_id, "full_access_override", False)
            and not bool(getattr(user_or_id, "is_blocked", False))
        )
    return False


# Алиас для обратной совместимости (используется в executor.py и task_service.py)
balance_is_admin = is_admin

def calculate_task_cost(video_count: int, price_per_video: Decimal) -> Decimal:
    """
    Рассчитывает стоимость задачи.
    
    ИСПРАВЛЕНО: Централизованная функция для расчёта стоимости.
    Используется в api/tasks.py при создании задачи и в executor/executor.py при возврате средств.
    
    Args:
        video_count: Количество видео
        price_per_video: Цена за одно видео
        
    Returns:
        Общая стоимость задачи
    """
    return Decimal(str(video_count)) * price_per_video


def calculate_refund_amount(failed_videos: int, pricing: Pricing) -> Decimal:
    """
    Рассчитывает сумму возврата за неудачные видео.
    
    ИСПРАВЛЕНО: Централизованная функция для расчёта возврата.
    Логика: price_success - price_failure (например, 5 - 2 = 3 грн за видео)
    
    Args:
        failed_videos: Количество неудачных видео
        pricing: Объект Pricing с ценами
        
    Returns:
        Сумма возврата
    """
    pricing = _ensure_pricing_consistency(pricing)
    refund_per_video = pricing.price_success - pricing.price_failure
    if refund_per_video < 0:
        raise ValueError("Refund per video cannot be negative")
    return Decimal(str(failed_videos)) * refund_per_video
