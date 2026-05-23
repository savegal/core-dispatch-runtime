"""
API endpoints для статистики по географии.
"""
from fastapi import APIRouter, Depends, Query, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func, and_, or_
from db.database import get_db
from db.models import Account, Proxy, Task, Log
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional
import logging
from api.auth import verify_admin_header

router = APIRouter(prefix="/api/geo", tags=["geo"], dependencies=[Depends(verify_admin_header)])
logger = logging.getLogger(__name__)


@router.get("/stats")
async def get_geo_stats(
    days: int = Query(7, description="Количество дней для анализа"),
    db: AsyncSession = Depends(get_db)
) -> Dict:
    """
    Получает статистику эффективности по странам за период.
    
    Returns:
        dict со статистикой:
        {
            "countries": [
                {
                    "country_code": "US",
                    "country_name": "США",
                    "accounts_count": 20,
                    "proxies_count": 18,
                    "tasks_success": 100,
                    "tasks_total": 120,
                    "success_rate": 0.83,
                    "captcha_rate": 0.02,  # Процент капчи
                    "avg_response_time": 1500,  # Среднее время выполнения
                    "status": "optimal"
                }
            ],
            "summary": {
                "best_country": "US",
                "worst_country": "RU",
                "total_countries": 5,
                "avg_success_rate": 0.85
            }
        }
    """
    try:
        period_start = datetime.now(timezone.utc) - timedelta(days=days)
        
        # Получаем статистику по странам
        countries_stats = []
        
        # Получаем все страны из аккаунтов и прокси (объединяем)
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
        
        # Объединяем страны из аккаунтов и прокси
        all_countries = account_countries | proxy_countries
    
        country_names = {
            "US": "США", "UA": "Украина", "GB": "Великобритания", "DE": "Германия",
            "FR": "Франция", "IT": "Италия", "ES": "Испания", "PL": "Польша",
            "RU": "Россия", "CA": "Канада", "AU": "Австралия", "JP": "Япония",
            "KR": "Корея", "CN": "Китай", "BR": "Бразилия", "MX": "Мексика",
            "IN": "Индия", "TR": "Турция", "NL": "Нидерланды", "BE": "Бельгия",
            "SE": "Швеция", "NO": "Норвегия", "DK": "Дания", "FI": "Финляндия",
        }
        
        best_country = None
        worst_country = None
        best_success_rate = 0
        worst_success_rate = 1.0
        total_success_rate = 0
        
        for country_code in all_countries:
            if not country_code:
                continue
            
            # Количество аккаунтов
            accounts_result = await db.execute(
                select(func.count(Account.id)).filter(
                    and_(
                        Account.country_code == country_code,
                        Account.status != "dead"
                    )
                )
            )
            accounts_count = accounts_result.scalar() or 0
            
            # Количество прокси
            proxies_result = await db.execute(
                select(func.count(Proxy.id)).filter(
                    and_(
                        Proxy.country_code == country_code,
                        Proxy.id.isnot(None)  # Все прокси
                    )
                )
            )
            proxies_count = proxies_result.scalar() or 0
            
            # Статистика задач (приблизительная - через логи)
            # Ищем логи с упоминанием этой страны
            logs_result = await db.execute(
                select(Log)
                .join(Account)
                .filter(
                    and_(
                        Account.country_code == country_code,
                        Log.timestamp >= period_start,
                        or_(
                            Log.message.like("%SUCCESS%"),
                            Log.message.like("%COMMENT%"),
                            Log.message.like("%ERROR%"),
                            Log.message.like("%CAPTCHA%")
                        )
                    )
                )
            )
            tasks_logs = logs_result.scalars().all()
            
            # Простой подсчет успешных/неуспешных
            success_count = sum(1 for log in tasks_logs if "SUCCESS" in log.message.upper() or ("COMMENT" in log.message.upper() and "ERROR" not in log.message.upper()))
            error_count = sum(1 for log in tasks_logs if "ERROR" in log.message.upper())
            captcha_count = sum(1 for log in tasks_logs if "CAPTCHA" in log.message.upper())
            total_count = success_count + error_count
            
            success_rate = success_count / total_count if total_count > 0 else 0
            captcha_rate = captcha_count / total_count if total_count > 0 else 0
            
            # Определяем статус
            if proxies_count >= accounts_count:
                status = "optimal"
            elif proxies_count >= accounts_count * 0.5:
                status = "warning"
            else:
                status = "critical"
            
            country_stat = {
                "country_code": country_code,
                "country_name": country_names.get(country_code, country_code),
                "accounts_count": accounts_count,
                "proxies_count": proxies_count,
                "tasks_success": success_count,
                "tasks_total": total_count,
                "success_rate": round(success_rate, 3),
                "captcha_rate": round(captcha_rate, 3),
                "status": status
            }
            
            countries_stats.append(country_stat)
            
            # Обновляем лучшие/худшие
            if total_count >= 10:  # Только для стран с достаточной статистикой
                if success_rate > best_success_rate:
                    best_success_rate = success_rate
                    best_country = country_code
                if success_rate < worst_success_rate:
                    worst_success_rate = success_rate
                    worst_country = country_code
            
            total_success_rate += success_rate
        
        # Сортируем по успешности
        countries_stats.sort(key=lambda x: x["success_rate"], reverse=True)
    
        avg_success_rate = total_success_rate / len(countries_stats) if countries_stats else 0
        
        return {
            "countries": countries_stats,
            "summary": {
                "best_country": best_country,
                "worst_country": worst_country,
                "total_countries": len(countries_stats),
                "avg_success_rate": round(avg_success_rate, 3),
                "period_days": days
            }
        }
    except Exception as e:
        logger.error(f"Error getting geo stats: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Error getting geo stats: {str(e)[:200]}")



