"""
Сервис для мониторинга ресурсов и отправки уведомлений владельцу.
"""
import logging
from sqlalchemy.orm import Session
from db.models import Account, Proxy
from config.settings import get_settings
from datetime import datetime, timedelta, timezone
from typing import Dict, Optional

logger = logging.getLogger("services.monitoring")
settings = get_settings()

# Кэш для предотвращения спама уведомлений
_last_notification: Dict[str, Optional[datetime]] = {
    "low_accounts": None,
    "low_proxies": None
}

NOTIFICATION_COOLDOWN_HOURS = 1  # Не чаще раза в час


def check_resources_status(db: Session) -> Dict[str, any]:
    """
    Проверяет статус ресурсов и возвращает информацию о проблемах.
    
    Returns:
        dict с информацией о ресурсах и проблемах
    """
    active_accounts = db.query(Account).filter(Account.status == "active").count()
    # ИСПРАВЛЕНО: Убрана проверка Proxy.status - все прокси считаются активными
    active_proxies = db.query(Proxy).count()
    
    issues = []
    
    if active_accounts < settings.MIN_ACTIVE_ACCOUNTS:
        issues.append({
            "type": "low_accounts",
            "current": active_accounts,
            "minimum": settings.MIN_ACTIVE_ACCOUNTS,
            "message": f"Активных аккаунтов мало: {active_accounts} (минимум: {settings.MIN_ACTIVE_ACCOUNTS})"
        })
    
    if active_proxies < settings.MIN_ACTIVE_PROXIES:
        issues.append({
            "type": "low_proxies",
            "current": active_proxies,
            "minimum": settings.MIN_ACTIVE_PROXIES,
            "message": f"Активных прокси мало: {active_proxies} (минимум: {settings.MIN_ACTIVE_PROXIES})"
        })
    
    return {
        "active_accounts": active_accounts,
        "active_proxies": active_proxies,
        "issues": issues,
        "all_ok": len(issues) == 0
    }


def should_send_notification(issue_type: str) -> bool:
    """
    Проверяет, нужно ли отправлять уведомление (с учётом cooldown).
    
    Args:
        issue_type: тип проблемы ("low_accounts" или "low_proxies")
    
    Returns:
        True если нужно отправить уведомление
    """
    last_time = _last_notification.get(issue_type)
    
    if last_time is None:
        return True
    
    time_since = datetime.now(timezone.utc) - last_time
    return time_since >= timedelta(hours=NOTIFICATION_COOLDOWN_HOURS)


def mark_notification_sent(issue_type: str) -> None:
    """Отмечает, что уведомление было отправлено."""
    _last_notification[issue_type] = datetime.now(timezone.utc)


def get_notification_message(status: Dict) -> Optional[str]:
    """
    Формирует сообщение для владельца на основе статуса ресурсов.
    
    Returns:
        Текст сообщения или None, если уведомление не нужно
    """
    if status["all_ok"]:
        return None
    
    messages = []
    for issue in status["issues"]:
        issue_type = issue["type"]
        if should_send_notification(issue_type):
            messages.append(issue["message"])
            mark_notification_sent(issue_type)
    
    if messages:
        return "⚠️ Внимание: ресурсы системы\n\n" + "\n".join(messages) + "\n\nПора добавить новые ресурсы."
    
    return None

