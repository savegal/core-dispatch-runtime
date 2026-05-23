"""
API endpoints для простой статистики по географии (аккаунты и прокси по странам).
"""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func, and_, or_
from db.database import get_db
from db.models import Account, Proxy
from api.auth import verify_admin_header
from typing import Dict, List
import logging

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/geo", tags=["geo"], dependencies=[Depends(verify_admin_header)])


# Глобальное состояние для отслеживания прогресса определения гео
_geo_detection_progress = {
    "running": False,
    "total_accounts": 0,
    "total_proxies": 0,
    "checked_accounts": 0,
    "checked_proxies": 0,
    "detected_accounts": 0,
    "detected_proxies": 0,
    "current_item": None
}


@router.get("/detect/progress")
async def get_geo_detection_progress():
    """Возвращает текущий прогресс определения гео."""
    return _geo_detection_progress.copy()


@router.post("/detect")
async def detect_all_geo(
    db: AsyncSession = Depends(get_db),
    _: None = Depends(verify_admin_header)
) -> Dict:
    """
    Определяет гео для всех аккаунтов и прокси, у которых гео не определено.
    
    Returns:
        dict: {
            "accounts": {"total": 10, "detected": 5, "failed": 2},
            "proxies": {"total": 15, "detected": 12, "failed": 1}
        }
    """
    from services.geo_service import detect_proxy_country
    from services.geo_detection_service import detect_account_country
    import asyncio
    
    global _geo_detection_progress
    
    # Проверяем, не запущена ли уже проверка
    if _geo_detection_progress["running"]:
        return {
            "error": "Detection already in progress",
            "progress": _geo_detection_progress.copy()
        }
    
    # Получаем все аккаунты без гео
    accounts_result = await db.execute(
        select(Account).filter(
            and_(
                Account.status != "dead",
                or_(
                    Account.country_code.is_(None),
                    Account.country_code == ""
                )
            )
        )
    )
    accounts_without_geo = accounts_result.scalars().all()
    
    # Получаем все прокси без гео
    proxies_result = await db.execute(
        select(Proxy).filter(
            or_(
                Proxy.country_code.is_(None),
                Proxy.country_code == ""
            )
        )
    )
    proxies_without_geo = proxies_result.scalars().all()
    
    total_accounts = len(accounts_without_geo)
    total_proxies = len(proxies_without_geo)
    
    # Инициализируем прогресс
    _geo_detection_progress = {
        "running": True,
        "total_accounts": total_accounts,
        "total_proxies": total_proxies,
        "checked_accounts": 0,
        "checked_proxies": 0,
        "detected_accounts": 0,
        "detected_proxies": 0,
        "current_item": None
    }
    
    detected_accounts = 0
    detected_proxies = 0
    failed_accounts = 0
    failed_proxies = 0
    
    # Семафор для ограничения параллельных запросов
    semaphore = asyncio.Semaphore(5)  # Максимум 5 одновременно
    
    async def detect_account_geo(account):
        """Определяет гео для одного аккаунта."""
        nonlocal detected_accounts, failed_accounts
        
        async with semaphore:
            _geo_detection_progress["current_item"] = f"Account: {account.label[:30]}"
            
            try:
                # Пробуем определить из cookies
                detected_country = detect_account_country(account)
                
                if not detected_country and account.proxy:
                    # Если не определилось из cookies, пробуем через прокси
                    detected_country = await detect_proxy_country(account.proxy, timeout=10.0)
                
                if detected_country:
                    account.country_code = detected_country
                    detected_accounts += 1
                    _geo_detection_progress["detected_accounts"] = detected_accounts
                    logger.info(f"[GEO] Detected country {detected_country} for account {account.id} ({account.label[:30]})")
                else:
                    failed_accounts += 1
            except Exception as e:
                failed_accounts += 1
                logger.debug(f"[GEO] Could not detect country for account {account.id}: {e}")
            finally:
                _geo_detection_progress["checked_accounts"] += 1
    
    async def detect_proxy_geo(proxy):
        """Определяет гео для одного прокси."""
        nonlocal detected_proxies, failed_proxies
        
        async with semaphore:
            proxy_short = proxy.proxy[:30] + "..." if len(proxy.proxy) > 30 else proxy.proxy
            _geo_detection_progress["current_item"] = f"Proxy: {proxy_short}"
            
            try:
                detected_country = await detect_proxy_country(proxy.proxy, timeout=10.0)
                if detected_country:
                    proxy.country_code = detected_country
                    detected_proxies += 1
                    _geo_detection_progress["detected_proxies"] = detected_proxies
                    logger.info(f"[GEO] Detected country {detected_country} for proxy {proxy.id} ({proxy_short})")
                else:
                    failed_proxies += 1
            except Exception as e:
                failed_proxies += 1
                logger.debug(f"[GEO] Could not detect country for proxy {proxy.id}: {e}")
            finally:
                _geo_detection_progress["checked_proxies"] += 1
    
    try:
        # Запускаем определение гео параллельно
        tasks = []
        for account in accounts_without_geo:
            tasks.append(detect_account_geo(account))
        for proxy in proxies_without_geo:
            tasks.append(detect_proxy_geo(proxy))
        
        await asyncio.gather(*tasks)
        
        # Сохраняем изменения
        try:
            await db.commit()
        except Exception as e:
            await db.rollback()
            logger.error(f"Error committing geo detection results: {e}", exc_info=True)
            raise HTTPException(status_code=500, detail=f"Database error: {str(e)[:200]}")
        
    finally:
        _geo_detection_progress["running"] = False
        _geo_detection_progress["current_item"] = None
    
    logger.info(f"Geo detection complete: {detected_accounts} accounts, {detected_proxies} proxies")
    
    return {
        "accounts": {
            "total": total_accounts,
            "detected": detected_accounts,
            "failed": failed_accounts
        },
        "proxies": {
            "total": total_proxies,
            "detected": detected_proxies,
            "failed": failed_proxies
        }
    }


@router.get("/coverage_summary")
async def get_geo_coverage(
    db: AsyncSession = Depends(get_db)
) -> Dict:
    """
    Возвращает простую статистику по географии: сколько аккаунтов и прокси в каждой стране.
    
    Returns:
        dict: {
            "countries": [
                {
                    "country_code": "US",
                    "country_name": "США",
                    "accounts_total": 20,
                    "accounts_active": 18,
                    "accounts_dead": 2,
                    "proxies_total": 25,
                    "proxies_active": 22,
                    "proxies_slow": 2,
                    "proxies_bad": 1,
                    "coverage_status": "optimal"  # optimal, warning, critical
                }
            ],
            "summary": {
                "total_countries": 5,
                "total_accounts": 100,
                "total_proxies": 120,
                "countries_with_proxies": 4,
                "countries_without_proxies": 1
            }
        }
    """
    try:
        # Получаем все уникальные страны из аккаунтов и прокси
        accounts_result = await db.execute(
            select(Account.country_code)
            .filter(
                and_(
                    Account.country_code.isnot(None),
                    Account.country_code != ""
                )
            )
            .distinct()
        )
        account_countries = set(accounts_result.scalars().all())
        
        proxies_result = await db.execute(
            select(Proxy.country_code)
            .filter(
                and_(
                    Proxy.country_code.isnot(None),
                    Proxy.country_code != ""
                )
            )
            .distinct()
        )
        proxy_countries = set(proxies_result.scalars().all())
        
        all_countries = account_countries | proxy_countries
        
        country_names = {
            "US": "США", "UA": "Украина", "GB": "Великобритания", "DE": "Германия",
            "FR": "Франция", "IT": "Италия", "ES": "Испания", "PL": "Польша",
            "RU": "Россия", "CA": "Канада", "AU": "Австралия", "JP": "Япония",
            "KR": "Корея", "CN": "Китай", "BR": "Бразилия", "MX": "Мексика",
            "IN": "Индия", "TR": "Турция", "NL": "Нидерланды", "BE": "Бельгия",
            "SE": "Швеция", "NO": "Норвегия", "DK": "Дания", "FI": "Финляндия",
        }
        
        countries_data = []
        total_accounts = 0
        total_proxies = 0
        countries_with_proxies = 0
        countries_without_proxies = 0
        
        for country_code in sorted(all_countries):
            if not country_code:
                continue
            
            # Статистика аккаунтов
            accounts_total_result = await db.execute(
                select(func.count(Account.id)).filter(
                    Account.country_code == country_code
                )
            )
            accounts_total = accounts_total_result.scalar() or 0
            
            accounts_active_result = await db.execute(
                select(func.count(Account.id)).filter(
                    and_(
                        Account.country_code == country_code,
                        Account.status == "active"
                    )
                )
            )
            accounts_active = accounts_active_result.scalar() or 0
            
            accounts_dead_result = await db.execute(
                select(func.count(Account.id)).filter(
                    and_(
                        Account.country_code == country_code,
                        Account.status == "dead"
                    )
                )
            )
            accounts_dead = accounts_dead_result.scalar() or 0
            
            # Статистика прокси
            proxies_total_result = await db.execute(
                select(func.count(Proxy.id)).filter(
                    Proxy.country_code == country_code
                )
            )
            proxies_total = proxies_total_result.scalar() or 0
            
            # УБРАНО: Все упоминания Proxy.status - теперь считаем все прокси
            proxies_result = await db.execute(
                select(func.count(Proxy.id)).filter(
                    Proxy.country_code == country_code
                )
            )
            proxies_total = proxies_result.scalar() or 0
            proxies_active = proxies_total  # Все прокси считаются активными
            proxies_slow = 0  # Убрано
            proxies_bad = 0  # Убрано
            
            # Определяем статус покрытия
            if accounts_total == 0:
                coverage_status = "no_accounts"
            elif proxies_total >= accounts_active:
                coverage_status = "optimal"
            elif proxies_total >= accounts_active * 0.5:
                coverage_status = "warning"
            else:
                coverage_status = "critical"
            
            if proxies_total > 0:
                countries_with_proxies += 1
            else:
                countries_without_proxies += 1
            
            total_accounts += accounts_total
            total_proxies += proxies_total
            
            countries_data.append({
                "country_code": country_code,
                "country_name": country_names.get(country_code, country_code),
                "accounts_total": accounts_total,
                "accounts_active": accounts_active,
                "accounts_dead": accounts_dead,
                "proxies_total": proxies_total,
                "proxies_active": proxies_active,
                "proxies_slow": proxies_slow,
                "proxies_bad": proxies_bad,
                "coverage_status": coverage_status
            })
        
        # Сортируем по количеству аккаунтов (по убыванию)
        countries_data.sort(key=lambda x: x["accounts_total"], reverse=True)
        
        return {
            "countries": countries_data,
            "summary": {
                "total_countries": len(countries_data),
                "total_accounts": total_accounts,
                "total_proxies": total_proxies,
                "countries_with_proxies": countries_with_proxies,
                "countries_without_proxies": countries_without_proxies
            }
        }
    except Exception as e:
        logger.error(f"Error getting geo coverage: {e}", exc_info=True)
        await db.rollback()
        raise HTTPException(status_code=500, detail=f"Database error: {str(e)[:200]}")



