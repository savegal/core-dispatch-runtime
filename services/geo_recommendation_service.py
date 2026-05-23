"""
Сервис для анализа и рекомендаций по географии аккаунтов и прокси.
Помогает оптимизировать использование ресурсов и снизить вероятность капчи.
"""
import logging
from datetime import datetime, timedelta, timezone
from sqlalchemy.orm import Session
from sqlalchemy import func, and_, or_
from db.models import Account, Proxy
from typing import Dict, List, Optional, Tuple
from collections import defaultdict
from config.settings import get_settings

settings = get_settings()
logger = logging.getLogger("services.geo_recommendation")


class GeoRecommendation:
    """Рекомендация по географии для задачи."""
    
    def __init__(
        self,
        country_code: str,
        available_accounts: int,
        available_proxies: int,
        recommended_proxies: int,
        status: str,  # "optimal", "warning", "critical"
        message: str,
        account_ids: List[int],
        proxy_ids: List[int]
    ):
        self.country_code = country_code
        self.available_accounts = available_accounts
        self.available_proxies = available_proxies
        self.recommended_proxies = recommended_proxies
        self.status = status
        self.message = message
        self.account_ids = account_ids
        self.proxy_ids = proxy_ids


def analyze_geo_coverage(
    db: Session,
    required_accounts: Optional[int] = None
) -> Dict[str, any]:
    """
    Анализирует покрытие географии аккаунтов и прокси.
    
    Args:
        db: SQLAlchemy сессия
        required_accounts: сколько аккаунтов требуется для задачи (опционально)
        
    Returns:
        dict с анализом:
        {
            "countries": [
                {
                    "country_code": "US",
                    "available_accounts": 15,
                    "available_proxies": 10,
                    "optimal_proxies_needed": 15,  # Рекомендуется для оптимальной работы
                    "status": "optimal" | "warning" | "critical",
                    "message": "Описание",
                    "accounts": [1, 2, 3],
                    "proxies": [4, 5, 6]
                },
                ...
            ],
            "summary": {
                "total_accounts": 100,
                "total_proxies": 50,
                "countries_with_full_coverage": 5,
                "countries_needing_proxies": 3,
                "recommendations": ["Рекомендация 1", "Рекомендация 2"]
            }
        }
    """
    # Получаем активные аккаунты с country_code
    accounts_query = db.query(Account).filter(
        and_(
            Account.status.in_(["active", "error"]),
            Account.country_code.isnot(None),
            Account.country_code != ""
        )
    )
    
    accounts = accounts_query.all()
    
    # Получаем активные прокси с country_code
    proxies_query = db.query(Proxy).filter(
        and_(
            Proxy.id.isnot(None),  # Все прокси
            Proxy.country_code.isnot(None),
            Proxy.country_code != ""
        )
    )
    
    proxies = proxies_query.all()
    
    # Группируем по странам
    accounts_by_country = defaultdict(list)
    proxies_by_country = defaultdict(list)
    
    for account in accounts:
        if account.country_code:
            accounts_by_country[account.country_code].append(account)
    
    for proxy in proxies:
        if proxy.country_code:
            proxies_by_country[proxy.country_code].append(proxy)
    
    # Формируем анализ для каждой страны
    countries_analysis = []
    countries_with_full_coverage = 0
    countries_needing_proxies = []
    
    all_countries = set(accounts_by_country.keys()) | set(proxies_by_country.keys())
    
    for country_code in sorted(all_countries):
        country_accounts = accounts_by_country.get(country_code, [])
        country_proxies = proxies_by_country.get(country_code, [])
        
        available_accounts = len(country_accounts)
        available_proxies = len(country_proxies)
        
        # Определяем оптимальное количество прокси (обычно 1:1, но можно с запасом)
        # Для задач с большим количеством действий нужен запас
        optimal_proxies_needed = available_accounts  # 1 прокси на аккаунт оптимально
        
        # Определяем статус
        if available_proxies >= optimal_proxies_needed:
            status = "optimal"
            message = f"✅ Оптимальное покрытие: {available_proxies} прокси для {available_accounts} аккаунтов"
        elif available_proxies >= optimal_proxies_needed * 0.5:  # Есть хотя бы 50%
            status = "warning"
            message = f"⚠️ Частичное покрытие: {available_proxies} прокси для {available_accounts} аккаунтов (рекомендуется {optimal_proxies_needed})"
            countries_needing_proxies.append(country_code)
        else:
            status = "critical"
            message = f"🔴 Недостаточно прокси: {available_proxies} прокси для {available_accounts} аккаунтов (рекомендуется {optimal_proxies_needed})"
            countries_needing_proxies.append(country_code)
        
        if status == "optimal":
            countries_with_full_coverage += 1
        
        countries_analysis.append({
            "country_code": country_code,
            "country_name": _get_country_name(country_code),
            "available_accounts": available_accounts,
            "available_proxies": available_proxies,
            "optimal_proxies_needed": optimal_proxies_needed,
            "status": status,
            "message": message,
            "accounts": [acc.id for acc in country_accounts],
            "proxies": [proxy.id for proxy in country_proxies]
        })
    
    # Формируем рекомендации
    recommendations = []
    
    if countries_needing_proxies:
        for country_code in countries_needing_proxies[:5]:  # Показываем топ-5
            accounts_count = len(accounts_by_country.get(country_code, []))
            proxies_count = len(proxies_by_country.get(country_code, []))
            needed = accounts_count - proxies_count
            country_name = _get_country_name(country_code)
            recommendations.append(
                f"Рекомендуется докупить {needed} прокси для {country_name} "
                f"({accounts_count} аккаунтов, {proxies_count} прокси)"
            )
    
    # Если требуется определенное количество аккаунтов
    if required_accounts:
        # Проверяем, можно ли выполнить задачу оптимально
        total_optimal_accounts = sum(
            ca["available_accounts"] 
            for ca in countries_analysis 
            if ca["status"] == "optimal"
        )
        
        if total_optimal_accounts < required_accounts:
            needed = required_accounts - total_optimal_accounts
            recommendations.insert(0, 
                f"⚠️ Для оптимального выполнения задачи (требуется {required_accounts} аккаунтов) "
                f"рекомендуется докупить прокси для покрытия {needed} дополнительных аккаунтов"
            )
    
    summary = {
        "total_accounts": len(accounts),
        "total_proxies": len(proxies),
        "total_countries": len(all_countries),
        "countries_with_full_coverage": countries_with_full_coverage,
        "countries_needing_proxies": len(countries_needing_proxies),
        "recommendations": recommendations
    }
    
    return {
        "countries": countries_analysis,
        "summary": summary
    }


def get_recommendation_for_task(
    db: Session,
    required_accounts: int,
    account_type: str = "main_comment"  # "main_comment" или "reply"
) -> Dict[str, any]:
    """
    Получает рекомендации для конкретной задачи.
    
    Args:
        db: SQLAlchemy сессия
        required_accounts: сколько аккаунтов требуется для задачи
        account_type: тип аккаунтов ("main_comment" или "reply")
        
    Returns:
        dict с рекомендациями:
        {
            "can_execute": bool,  # Можно ли выполнить задачу
            "optimal_execution": bool,  # Можно ли выполнить оптимально
            "recommendations": ["Рекомендация 1", ...],
            "best_countries": [
                {
                    "country_code": "US",
                    "available_accounts": 10,
                    "available_proxies": 10,
                    "status": "optimal",
                    "message": "..."
                },
                ...
            ],
            "warnings": ["Предупреждение 1", ...]
        }
    """
    # Получаем аккаунты с учетом типа и лимитов
    now = datetime.now(timezone.utc)
    cooldown_threshold = now - timedelta(seconds=settings.ACCOUNT_COOLDOWN_SECONDS)
    
    if account_type == "main_comment":
        accounts_query = db.query(Account).filter(
            and_(
                Account.status.in_(["active", "error"]),
                Account.today_main_comments < settings.MAX_ACTIONS_PER_ACCOUNT,
                or_(
                    Account.last_main_comment_at.is_(None),
                    Account.last_main_comment_at < cooldown_threshold
                )
            )
        )
    else:  # reply
        accounts_query = db.query(Account).filter(
            Account.status.in_(["active", "error"])
        )
    
    accounts = accounts_query.all()
    
    # Группируем по странам
    accounts_by_country = defaultdict(list)
    for account in accounts:
        if account.country_code:
            accounts_by_country[account.country_code].append(account)
        else:
            accounts_by_country[None].append(account)
    
    # Получаем прокси по странам
    proxies_by_country = defaultdict(list)
    # ИСПРАВЛЕНО: Убрана проверка Proxy.status - все прокси считаются активными
    proxies = db.query(Proxy).all()
    for proxy in proxies:
        if proxy.country_code:
            proxies_by_country[proxy.country_code].append(proxy)
        else:
            proxies_by_country[None].append(proxy)
    
    # Анализируем возможности выполнения
    can_execute = len(accounts) >= required_accounts
    
    recommendations = []
    warnings = []
    best_countries = []
    
    # Сортируем страны по доступности (оптимальные сначала)
    country_stats = []
    for country_code, country_accounts in accounts_by_country.items():
        if country_code is None:
            continue  # Пропускаем аккаунты без country_code
        
        country_proxies = proxies_by_country.get(country_code, [])
        available_accounts = len(country_accounts)
        available_proxies = len(country_proxies)
        
        # Определяем статус
        if available_proxies >= available_accounts:
            status = "optimal"
        elif available_proxies >= available_accounts * 0.5:
            status = "warning"
        else:
            status = "critical"
        
        country_stats.append({
            "country_code": country_code,
            "country_name": _get_country_name(country_code),
            "available_accounts": available_accounts,
            "available_proxies": available_proxies,
            "status": status,
            "message": _get_status_message(country_code, available_accounts, available_proxies)
        })
    
    # Сортируем: optimal -> warning -> critical
    country_stats.sort(key=lambda x: {"optimal": 0, "warning": 1, "critical": 2}[x["status"]])
    best_countries = country_stats[:10]  # Топ-10 стран
    
    # Формируем рекомендации
    optimal_execution = False
    
    if can_execute:
        # Проверяем оптимальность: достаточно ли аккаунтов со статусом "optimal" для выполнения задачи
        optimal_accounts = sum(
            cs["available_accounts"] 
            for cs in country_stats 
            if cs["status"] == "optimal"
        )
        
        # Оптимальное выполнение: достаточно оптимальных аккаунтов для задачи
        optimal_execution = optimal_accounts >= required_accounts
        
        if not optimal_execution:
            needed = required_accounts - optimal_accounts
            warnings.append(
                f"⚠️ Для оптимального выполнения рекомендуется докупить прокси "
                f"для покрытия {needed} дополнительных аккаунтов"
            )
            
            # Конкретные рекомендации по странам (с приоритетом на warning/critical)
            for cs in country_stats:
                if cs["status"] in ["warning", "critical"] and cs["available_accounts"] > 0:
                    needed_proxies = max(0, cs["available_accounts"] - cs["available_proxies"])
                    if needed_proxies > 0:
                        recommendations.append(
                            f"📌 {cs['country_name']}: {cs['available_accounts']} аккаунтов, "
                            f"{cs['available_proxies']} прокси (желательно докупить {needed_proxies} прокси)"
                        )
    else:
        warnings.append(
            f"⚠️ Недостаточно аккаунтов: требуется {required_accounts}, доступно {len(accounts)}"
        )
    
    return {
        "can_execute": can_execute,
        "optimal_execution": optimal_execution,
        "recommendations": recommendations[:5],  # Топ-5 рекомендаций
        "best_countries": best_countries,
        "warnings": warnings,
        "total_available_accounts": len(accounts),
        "total_available_proxies": len(proxies)
    }


def _get_country_name(country_code: str) -> str:
    """Возвращает название страны по коду."""
    country_names = {
        "US": "США",
        "UA": "Украина",
        "GB": "Великобритания",
        "DE": "Германия",
        "FR": "Франция",
        "IT": "Италия",
        "ES": "Испания",
        "PL": "Польша",
        "RU": "Россия",
        "CA": "Канада",
        "AU": "Австралия",
        "JP": "Япония",
        "KR": "Корея",
        "CN": "Китай",
        "BR": "Бразилия",
        "MX": "Мексика",
        "IN": "Индия",
        "TR": "Турция",
        "NL": "Нидерланды",
        "BE": "Бельгия",
        "SE": "Швеция",
        "NO": "Норвегия",
        "DK": "Дания",
        "FI": "Финляндия",
    }
    return country_names.get(country_code, country_code)


def _get_status_message(country_code: str, accounts: int, proxies: int) -> str:
    """Формирует сообщение о статусе для страны."""
    country_name = _get_country_name(country_code)
    
    if proxies >= accounts:
        return f"✅ {country_name}: {accounts} аккаунтов, {proxies} прокси (оптимально)"
    elif proxies >= accounts * 0.5:
        needed = accounts - proxies
        return f"⚠️ {country_name}: {accounts} аккаунтов, {proxies} прокси (желательно докупить {needed})"
    else:
        needed = accounts - proxies
        return f"🔴 {country_name}: {accounts} аккаунтов, {proxies} прокси (рекомендуется докупить {needed})"










