"""Utilities for proxy type filtering.
"""
from sqlalchemy import func, or_
from sqlalchemy.orm import Session, Query
from db.models import Proxy
from services.app_settings_service import get_proxy_type_filter
import logging

logger = logging.getLogger("services.proxy_filter")

def apply_proxy_type_filter(query: Query, db: Session) -> Query:
    proxy_type_filter = (get_proxy_type_filter(db) or "").strip().lower()
    normalized = func.upper(func.trim(Proxy.is_mobile))
    if proxy_type_filter == "mobile":
        query = query.filter(normalized == "Y")
        logger.debug("Filtering proxies: mobile only")
    elif proxy_type_filter == "residential":
        query = query.filter(or_(Proxy.is_mobile.is_(None), normalized != "Y"))
        logger.debug("Filtering proxies: residential only")
    return query
