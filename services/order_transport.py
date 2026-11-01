"""Global order publication mode and isolated per-task execution scope."""
from contextvars import ContextVar
import json

from sqlalchemy import select

from config.settings import get_settings
from db.models import AppSettings
from services.app_settings_service import set_setting


ORDER_TRANSPORT_KEY = "order_transport_mode"
DOM = "dom"
HTTP = "http"
ORDER_TRANSPORT_MODES = {DOM, HTTP}
_task_transport = ContextVar("order_transport_mode", default=None)


def _enabled(value):
    return value.strip().lower() in {"1", "true", "yes", "on"} if isinstance(value, str) else bool(value)


def http_transport_available(settings=None):
    return _enabled(getattr(settings or get_settings(), "TIKTOK_NETWORK_IMPL_READY", False))


def default_order_transport(settings=None):
    settings = settings or get_settings()
    return HTTP if (_enabled(getattr(settings, "TIKTOK_NETWORK_FIRST", False))
                    and http_transport_available(settings)) else DOM


def normalize_order_transport(value, default=None):
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            pass
    normalized = str(value or "").strip().lower()
    return normalized if normalized in ORDER_TRANSPORT_MODES else default


def order_transport_label(value):
    return {DOM: "DOM — через браузер", HTTP: "HTTP — быстрый"}.get(
        normalize_order_transport(value), "По настройке администратора")


def get_order_transport(db):
    # Fresh reads matter across bot/API/executor processes; the generic
    # settings cache can otherwise show a previous mode for up to a minute.
    row = db.execute(select(AppSettings).where(AppSettings.key == ORDER_TRANSPORT_KEY)
                     .execution_options(populate_existing=True)).scalar_one_or_none()
    return normalize_order_transport(row.value if row is not None else None, default_order_transport())


async def get_order_transport_async(db):
    result = await db.execute(select(AppSettings).where(AppSettings.key == ORDER_TRANSPORT_KEY)
                              .execution_options(populate_existing=True))
    row = result.scalar_one_or_none()
    return normalize_order_transport(row.value if row is not None else None, default_order_transport())


def set_order_transport(db, mode, *, updated_by=None):
    mode = normalize_order_transport(mode)
    if mode is None:
        raise ValueError("Выберите режим DOM или HTTP")
    if mode == HTTP and not http_transport_available():
        raise ValueError("HTTP-транспорт недоступен в конфигурации сервера")
    return set_setting(db, ORDER_TRANSPORT_KEY, mode,
                       description="Глобальный режим публикации новых заказов: DOM или HTTP",
                       updated_by=updated_by)


def current_order_transport():
    return _task_transport.get()


def set_task_order_transport(mode, settings=None):
    normalized = normalize_order_transport(mode, default_order_transport(settings))
    if normalized == HTTP and not http_transport_available(settings):
        raise ValueError("HTTP-транспорт заказа недоступен в конфигурации сервера")
    return _task_transport.set(normalized)


def reset_task_order_transport(token):
    if token is not None:
        _task_transport.reset(token)
