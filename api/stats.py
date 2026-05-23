"""API для статистики ресурсов системы."""
from fastapi import APIRouter, Depends, HTTPException
from services.metrics import get_metrics_summary
from services.redis_service import get_executor_heartbeat, get_monitor_heartbeat
from services.task_service import get_queue_length
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func, and_
from db.database import get_db
from db.models import Account, Proxy, Task, Log
from datetime import datetime, timezone
from typing import Dict
import logging
from api.auth import verify_admin_header

router = APIRouter(prefix="/api/stats", tags=["stats"], dependencies=[Depends(verify_admin_header)])
logger = logging.getLogger(__name__)


@router.get("/resources")
async def get_resources_stats(db: AsyncSession = Depends(get_db)) -> Dict:
    """
    Возвращает статистику ресурсов системы.
    
    ИСПРАВЛЕНО: Переведено на async для соответствия async БД.
    """
    try:
        # Статистика аккаунтов
        total_result = await db.execute(select(func.count(Account.id)))
        accounts_total = total_result.scalar()
        
        active_result = await db.execute(
            select(func.count(Account.id)).filter(Account.status == "active")
        )
        accounts_active = active_result.scalar()
        
        dead_result = await db.execute(
            select(func.count(Account.id)).filter(Account.status == "dead")
        )
        accounts_dead = dead_result.scalar()
        
        # Под подозрением (error_count_recent > 0, но status != 'dead')
        suspect_result = await db.execute(
            select(func.count(Account.id)).filter(
                and_(Account.error_count_recent > 0, Account.status != "dead")
            )
        )
        accounts_suspect = suspect_result.scalar()
        
        # Статистика прокси
        # ИСПРАВЛЕНО: Убраны статусы - все прокси считаются рабочими
        proxies_total_result = await db.execute(select(func.count(Proxy.id)))
        proxies_total = proxies_total_result.scalar()
        
        # Статистика за сегодня
        today_start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
        
        comments_result = await db.execute(
            select(func.count(Log.id)).filter(
                and_(
                    Log.timestamp >= today_start,
                    Log.message.like("%COMMENT%")
                )
            )
        )
        today_comments = comments_result.scalar()
        
        likes_result = await db.execute(
            select(func.count(Log.id)).filter(
                and_(
                    Log.timestamp >= today_start,
                    Log.message.like("%LIKE%")
                )
            )
        )
        today_likes = likes_result.scalar()
        
        errors_result = await db.execute(
            select(func.count(Log.id)).filter(
                and_(
                    Log.timestamp >= today_start,
                    Log.message.like("%ERROR%")
                )
            )
        )
        today_errors = errors_result.scalar()
    
        return {
            "accounts": {
                "total": accounts_total,
                "active": accounts_active,
                "dead": accounts_dead,
                "suspect": accounts_suspect
            },
            "proxies": {
                "total": proxies_total
            },
            "today": {
                "comments": today_comments,
                "likes": today_likes,
                "errors": today_errors
            }
        }
    except Exception as e:
        logger.error(f"Error getting resources stats: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Error getting stats: {str(e)[:200]}")


@router.get("/accounts/dead")
async def get_dead_accounts(limit: int = 50, db: AsyncSession = Depends(get_db)):
    """
    Возвращает список мёртвых аккаунтов.
    
    ИСПРАВЛЕНО: Переведено на async для соответствия async БД.
    """
    try:
        result = await db.execute(
            select(Account)
            .filter(Account.status == "dead")
            .order_by(Account.updated_at.desc())
            .limit(limit)
        )
        accounts = result.scalars().all()
        
        return [
            {
                "id": acc.id,
                "label": acc.label,
                "last_error": acc.last_error,
                "error_count_recent": acc.error_count_recent,
                "last_checked_at": acc.last_checked_at.isoformat() if acc.last_checked_at else None
            }
            for acc in accounts
        ]
    except Exception as e:
        logger.error(f"Error getting dead accounts: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Error getting dead accounts: {str(e)[:200]}")


@router.get("/metrics")
async def get_metrics():
    """
    ИСПРАВЛЕНО: Возвращает метрики системы (счетчики, gauges, timings).
    
    Метрики хранятся в Redis и обновляются в реальном времени.
    """
    try:
        metrics = get_metrics_summary()
        return metrics
    except Exception as e:
        logger.error(f"Error getting metrics: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Error getting metrics: {str(e)[:200]}")


@router.get("/health/summary")
async def get_health_summary():
    """
    Расширенная сводка для админа: очередь, executor heartbeat, метрики.
    """
    try:
        alive, info = get_executor_heartbeat()
        mon_alive, mon_info = get_monitor_heartbeat()
        queue_len = 0
        queue_error = None
        try:
            queue_len = get_queue_length()
        except Exception as queue_exc:
            queue_error = str(queue_exc)
        return {
            "executor": {"alive": alive, "info": info},
            "video_monitor": {"alive": mon_alive, "info": mon_info},
            "queue": {"length": queue_len, "error": queue_error},
            "metrics": get_metrics_summary()
        }
    except Exception as e:
        logger.error(f"Error getting health summary: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Error getting health summary: {str(e)[:200]}")
