"""
Автоматическая валидация аккаунтов раз в полчаса.
Проверяет аккаунты на валидность и обновляет их статус.
"""
import logging
import asyncio
import sys
from datetime import datetime, timezone, timedelta
from sqlalchemy.orm import Session
from sqlalchemy import select
from db.database import get_sync_db
from db.models import Account
from services.cookie_validator import validate_accounts_batch
from services.account_assignment import available_assignment_filter
from services.account_service import _read_runtime_state
from config.settings import get_settings

logger = logging.getLogger("services.auto_account_validator")
settings = get_settings()

# Глобальное состояние для отслеживания последней проверки
_last_validation_time = None
_validation_running = False


def _get_validation_accounts(db, now, limit=50):
    threshold = now - timedelta(minutes=30)
    return db.execute(select(Account).where(
        Account.status.in_(["active", "error"]),
        (Account.last_validation_attempt_at.is_(None)) | (Account.last_validation_attempt_at < threshold),
        available_assignment_filter(now),
    ).order_by(Account.last_validation_attempt_at.asc().nulls_first(), Account.id.asc()).limit(limit)).scalars().all()


def get_event_loop():
    """Получает или создает event loop для текущего потока."""
    import threading
    thread_local = threading.local()
    if not hasattr(thread_local, 'loop') or thread_local.loop.is_closed():
        if sys.platform == 'win32':
            try:
                policy = asyncio.get_event_loop_policy()
                if not isinstance(policy, asyncio.WindowsProactorEventLoopPolicy):
                    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
            except Exception:
                try:
                    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
                except Exception:
                    pass
        thread_local.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(thread_local.loop)
    return thread_local.loop


def validate_accounts_sync(accounts: list[Account]) -> list[dict]:
    """Run HTTP tri-state validation for a batch of accounts."""
    async def _run():
        return await validate_accounts_batch(accounts, max_concurrent=10)

    if sys.platform == 'win32':
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            return loop.run_until_complete(_run())
        finally:
            loop.close()
            asyncio.set_event_loop(None)
    try:
        running_loop = asyncio.get_running_loop()
    except RuntimeError:
        running_loop = None
    if running_loop and running_loop.is_running():
        return [
            {
                "account_id": account.id,
                "label": account.label,
                "valid": False,
                "status": "inconclusive",
                "error": "auto_validator_called_from_running_loop",
                "definitive": False,
            }
            for account in accounts
        ]
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        return loop.run_until_complete(_run())
    finally:
        loop.close()
        asyncio.set_event_loop(None)


def auto_validate_accounts():
    """
    Автоматически проверяет аккаунты на валидность.
    Проверяет только активные и error аккаунты, которые не проверялись в последние 30 минут.
    """
    global _last_validation_time, _validation_running
    
    # Проверяем, не запущена ли уже валидация
    if _validation_running:
        logger.debug("Auto validation already running, skipping")
        return
    
    # Проверяем, прошло ли 30 минут с последней проверки
    now = datetime.now(timezone.utc)
    if _last_validation_time:
        time_since_last = (now - _last_validation_time).total_seconds()
        if time_since_last < 1800:  # 30 минут = 1800 секунд
            logger.debug(f"Auto validation skipped (last check was {time_since_last/60:.1f} minutes ago)")
            return
    
    _validation_running = True
    _last_validation_time = now
    
    try:
        db: Session = next(get_sync_db())
        try:
            # Получаем аккаунты для проверки:
            # 1. Статус active или error
            # 2. Не проверялись в последние 30 минут (или вообще не проверялись)
            accounts = _get_validation_accounts(db, now)
            
            if not accounts:
                logger.debug("No accounts need validation (all checked recently)")
                return
            
            logger.info(f"Auto-validating {len(accounts)} accounts...")
            
            valid_count = 0
            invalid_count = 0
            inconclusive_count = 0
            error_count = 0

            checked_sources = {account.id: (account.cookies, account.cookie_file, account.proxy) for account in accounts}
            validation_results = validate_accounts_sync(accounts)
            results_by_id = {item.get("account_id"): item for item in validation_results if isinstance(item, dict)}

            for account in accounts:
                try:
                    # An order/import may have changed this account while HTTP was in flight.
                    db.refresh(account)
                    if account.status not in ("active", "error"):
                        continue
                    if (account.cookies, account.cookie_file, account.proxy) != checked_sources[account.id]:
                        continue
                    lease = account.assignment_lease_until
                    if lease is not None:
                        if lease.tzinfo is None:
                            lease = lease.replace(tzinfo=timezone.utc)
                        if lease > datetime.now(timezone.utc):
                            continue
                    label = (account.label or "")[:20]
                    logger.debug(f"Validating account {account.id} ({label})...")

                    validation = results_by_id.get(account.id) or {
                        "valid": False,
                        "status": "inconclusive",
                        "error": "missing_validation_result",
                        "definitive": False,
                    }
                    now_check = datetime.now(timezone.utc)
                    account.last_validation_attempt_at = now_check

                    status = str(validation.get("status") or "").lower()
                    error = str(validation.get("error") or "")[:200]
                    is_valid = bool(validation.get("valid") or status == "valid")
                    is_dead = status == "dead" and bool(validation.get("definitive", True))

                    if is_valid:
                        valid_count += 1
                        state = _read_runtime_state(account, now_check)
                        account.validation_fail_count = 0
                        account.last_validation_ok = now_check
                        # HTTP validation has its own timestamp; preserve the runtime
                        # timestamp used to measure existing CAPTCHA/network cooldowns.
                        if not state["active_cooldown"]:
                            account.status = "active"
                            account.error_count_recent = 0
                            account.last_error = None
                            logger.info(f"Account {account.id} ({label}) restored to active")
                    elif is_dead:
                        invalid_count += 1
                        account.last_error = f"Auto-validation failed - {error or 'login required'}"

                        from services.account_validation_policy import handle_validation_failure
                        marked_dead = handle_validation_failure(db, account)

                        if not marked_dead:
                            account.status = "error"
                            account.error_count_recent = (account.error_count_recent or 0) + 1
                            logger.warning(
                                f"Account {account.id} ({label}) marked as error "
                                f"({account.validation_fail_count}/3): {error}"
                            )
                    else:
                        inconclusive_count += 1
                        # Do not erase runtime/CAPTCHA reasons, extend their cooldown,
                        # or mark good cookies dead because an HTTP check was ambiguous.
                        logger.info(
                            f"Account {account.id} ({label}) auto-validation inconclusive; "
                            "not bumping validation_fail_count"
                        )

                    db.commit()

                except Exception as e:
                    error_count += 1
                    logger.error(f"Error validating account {account.id}: {e}", exc_info=True)
                    try:
                        db.rollback()
                    except Exception:
                        pass

            logger.info(
                f"Auto-validation complete: {valid_count} valid, {invalid_count} invalid, "
                f"{inconclusive_count} inconclusive, {error_count} errors ({len(accounts)} total)"
            )
            
        except Exception as e:
            logger.error(f"Error in auto_validate_accounts: {e}", exc_info=True)
            try:
                db.rollback()
            except Exception:
                pass
        finally:
            db.close()
            
    finally:
        _validation_running = False


def auto_validation_loop():
    """Основной цикл автоматической валидации."""
    logger.info("Auto account validation loop started (checking every 5 minutes)")
    
    while True:
        try:
            auto_validate_accounts()
        except Exception as e:
            logger.error(f"Error in auto validation loop: {e}", exc_info=True)
        
        # Проверяем каждые 5 минут
        import time
        time.sleep(300)  # 5 минут


if __name__ == "__main__":
    auto_validation_loop()



