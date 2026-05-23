"""
Сервис для автоматической замены медленных/проблемных прокси у аккаунтов.

Логика:
- Прокси помечается как "bad" только после НЕСКОЛЬКИХ неудач (PROXY_FAIL_THRESHOLD)
- До этого прокси помечается как "slow" 
- Аккаунты не теряют статус "active" при временных ошибках прокси
- При замене прокси учитывается география аккаунта (если включено в настройках)
"""
import logging
from sqlalchemy.orm import Session
from sqlalchemy import and_, or_
from db.models import Account, Proxy
from typing import Optional
from config.settings import get_settings

settings = get_settings()
logger = logging.getLogger("services.proxy_replacement")

# Используем настройку из settings
PROXY_FAIL_THRESHOLD = settings.PROXY_FAIL_THRESHOLD


def replace_slow_proxy_for_account(
    db: Session,
    account_id: int,
    exclude_proxy_strings: Optional[set[str]] = None,
    use_geo_matching: bool | None = None
) -> bool:
    """
    Заменяет прокси у аккаунта на лучший доступный прокси.
    При включённом geo-matching предпочитает прокси из той же страны что и аккаунт.
    
    Args:
        db: SQLAlchemy сессия
        account_id: ID аккаунта
        exclude_proxy_strings: набор строк прокси для исключения
        use_geo_matching: использовать подбор по гео (если None - берётся из settings.PROXY_GEO_MATCHING)
        
    Returns:
        True если прокси был заменён, False если не удалось найти подходящий прокси
    """
    if use_geo_matching is None:
        use_geo_matching = settings.PROXY_GEO_MATCHING
    
    # ИСПРАВЛЕНО: Используем SELECT FOR UPDATE для предотвращения race conditions
    account = db.query(Account).filter(Account.id == account_id).with_for_update().first()
    if not account:
        logger.warning(f"Account {account_id} not found")
        return False
    
    # ОПТИМИЗАЦИЯ: Получаем только proxy строки вместо полных объектов Account
    proxy_results = db.query(Account.proxy).filter(
        and_(Account.proxy.isnot(None), Account.proxy != "")
    ).distinct().all()
    used_proxy_strings = {p[0] for p in proxy_results if p[0]}
    
    if exclude_proxy_strings:
        used_proxy_strings.update(exclude_proxy_strings)
    
    # ИСПРАВЛЕНО: Используем только рабочие прокси по статусу
    # Строим базовый запрос
    base_query = db.query(Proxy).filter(
        Proxy.status.in_(["active", "slow"]),
        ~Proxy.proxy.in_(used_proxy_strings) if used_proxy_strings else True
    )
    
    # ИСПРАВЛЕНО: Используем централизованную функцию фильтрации
    from services.proxy_filter_utils import apply_proxy_type_filter
    base_query = apply_proxy_type_filter(base_query, db)
    
    new_proxy = None
    matched_by_geo = False
    
    # Если включен geo-matching и у аккаунта есть country_code, ищем прокси из той же страны
    if use_geo_matching and account.country_code:
        # ИСПРАВЛЕНО: Убрана сортировка по response_time_ms - поля нет
        geo_matched_proxy = base_query.filter(
            Proxy.country_code == account.country_code
        ).first()
        
        if geo_matched_proxy:
            new_proxy = geo_matched_proxy
            matched_by_geo = True
    
    # ИСПРАВЛЕНО: Убрана сортировка по response_time_ms - поля нет
    # Если не нашли по гео, берём любой доступный
    if not new_proxy:
        if settings.PROXY_GEO_FALLBACK or not use_geo_matching:
            new_proxy = base_query.first()
    
    if not new_proxy:
        logger.warning(f"No free active proxy available for account {account_id}")
        return False
    
    old_proxy = account.proxy
    account.proxy = new_proxy.proxy
    
    # УЛУЧШЕНИЕ: Если у аккаунта нет country_code, но у нового прокси есть - устанавливаем из прокси
    if not account.country_code and new_proxy.country_code:
        account.country_code = new_proxy.country_code
        logger.info(
            f"[GEO] Auto-set country_code {new_proxy.country_code} for account {account_id} "
            f"({account.label[:20]}) from replacement proxy"
        )
    
    try:
        db.commit()
    except Exception as e:
        db.rollback()
        logger.error(f"Error committing proxy replacement for account {account_id}: {e}", exc_info=True)
        raise
    
    geo_info = ""
    if matched_by_geo:
        geo_info = f" (geo-matched: {account.country_code})"
    elif use_geo_matching and account.country_code:
        geo_info = f" (fallback: account {account.country_code} -> proxy {new_proxy.country_code or 'unknown'})"
    
    logger.info(
        f"Auto-replaced proxy for account {account_id} ({account.label[:20]}): "
        f"{old_proxy[:30] if old_proxy else None} -> {new_proxy.proxy[:30]}{geo_info}"
    )
    
    return True


def mark_proxy_as_slow_and_replace(
    db: Session,
    account_id: int,
    proxy_string: str,
    timeout_error: bool = True
) -> bool:
    """
    Заменяет прокси у аккаунта при ошибках.
    
    ИСПРАВЛЕНО: Прокси больше не имеют статусов и счетчиков ошибок.
    Все прокси считаются рабочими, поэтому просто заменяем прокси у аккаунта.
    
    Args:
        db: SQLAlchemy сессия
        account_id: ID аккаунта
        proxy_string: строка прокси
        timeout_error: True если это ошибка таймаута (медленный прокси)
        
    Returns:
        True если прокси был заменён
    """
    # ИСПРАВЛЕНО: Просто логируем ошибку прокси, но не меняем его статус
    error_type = "timeout" if timeout_error else "error"
    logger.info(f"Replacing proxy {proxy_string[:30]} for account {account_id} due to {error_type}")
    
    # Заменяем прокси у аккаунта (с учётом гео если включено)
    return replace_slow_proxy_for_account(
        db,
        account_id,
        exclude_proxy_strings={proxy_string},
        use_geo_matching=settings.PROXY_GEO_MATCHING
    )


def reset_proxy_fail_count(db: Session, proxy_string: str) -> None:
    """
    ИСПРАВЛЕНО: Функция оставлена для обратной совместимости, но ничего не делает,
    так как прокси больше не имеют счетчиков ошибок.
    """
    # Прокси больше не имеют fail_count и status, поэтому просто логируем
    logger.debug(f"reset_proxy_fail_count called for {proxy_string[:30]} (no-op, proxies have no status)")
    pass
