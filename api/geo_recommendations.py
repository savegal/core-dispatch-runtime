"""
API endpoints для получения рекомендаций по географии аккаунтов и прокси.
"""
from fastapi import APIRouter, Depends, Query, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from db.database import get_db, get_sync_db_context
from services.geo_recommendation_service import (
    analyze_geo_coverage,
    get_recommendation_for_task
)
import logging
from api.auth import verify_admin_header
import asyncio

router = APIRouter(prefix="/api/geo", tags=["geo"], dependencies=[Depends(verify_admin_header)])
logger = logging.getLogger(__name__)


def _analyze_geo_coverage_sync(required_accounts: int | None):
    with get_sync_db_context() as sync_db:
        return analyze_geo_coverage(sync_db, required_accounts=required_accounts)


def _get_recommendation_sync(required_accounts: int, account_type: str):
    with get_sync_db_context() as sync_db:
        return get_recommendation_for_task(sync_db, required_accounts, account_type)


@router.get("/coverage")
async def get_geo_coverage(
    required_accounts: int | None = Query(None, description="Сколько аккаунтов требуется"),
    db: AsyncSession = Depends(get_db)
):
    """
    Получает анализ покрытия географии аккаунтов и прокси.
    
    ИСПРАВЛЕНО: Переведено на async. Использует sync функцию через sync_db для обратной совместимости.
    
    Returns:
        dict с анализом по странам и рекомендациями
    """
    try:
        return await asyncio.to_thread(_analyze_geo_coverage_sync, required_accounts)
    except Exception as e:
        logger.error(f"Error getting geo coverage: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Error getting geo coverage: {str(e)[:200]}")


@router.get("/recommendation")
async def get_recommendation(
    required_accounts: int = Query(..., description="Сколько аккаунтов требуется"),
    account_type: str = Query("main_comment", description="Тип аккаунтов (main_comment/reply)"),
    db: AsyncSession = Depends(get_db)
):
    """
    Получает рекомендации для конкретной задачи.
    
    ИСПРАВЛЕНО: Переведено на async. Использует sync функцию через sync_db для обратной совместимости.
    
    Returns:
        dict с рекомендациями для выполнения задачи
    """
    try:
        return await asyncio.to_thread(_get_recommendation_sync, required_accounts, account_type)
    except Exception as e:
        logger.error(f"Error getting geo recommendation: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Error getting recommendation: {str(e)[:200]}")


