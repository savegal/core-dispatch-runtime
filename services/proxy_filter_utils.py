"""
Утилиты для фильтрации прокси по типу.
Централизованная логика для избежания дублирования кода.
"""
from sqlalchemy.orm import Session
from sqlalchemy.orm import Query
from db.models import Proxy
from services.app_settings_service import get_proxy_type_filter
import logging

logger = logging.getLogger("services.proxy_filter")


def apply_proxy_type_filter(query: Query, db: Session) -> Query:
    """
    Применяет фильтр типа прокси к запросу.
    
    Args:
        query: SQLAlchemy запрос для прокси
        db: SQLAlchemy сессия для получения настроек
        
    Returns:
        Модифицированный запрос с применённым фильтром
    """
    proxy_type_filter = get_proxy_type_filter(db)
    if proxy_type_filter:
        if proxy_type_filter.lower() == "mobile":
            query = query.filter(Proxy.is_mobile == "Y")
            logger.debug("Filtering proxies: mobile only")
        elif proxy_type_filter.lower() == "residential":
            query = query.filter(Proxy.is_mobile != "Y")
            logger.debug("Filtering proxies: residential only")
    
    return query

