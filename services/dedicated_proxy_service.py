"""Strict per-user proxy routing for orders that use the shared account pool."""
from __future__ import annotations

from contextvars import ContextVar, Token
from dataclasses import dataclass
import logging

from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import set_committed_value

from db.models import Account, Proxy
from services.log_sanitizer import sanitize_url
from config.settings import get_settings

logger = logging.getLogger("services.dedicated_proxy")


@dataclass(frozen=True)
class DedicatedProxyRoute:
    user_id: int
    proxy_id: int
    proxy: str
    country_code: str | None
    is_mobile: bool
    rotation_url: str | None


_CURRENT_ROUTE: ContextVar[DedicatedProxyRoute | None] = ContextVar(
    "dedicated_proxy_route",
    default=None,
)


def get_current_dedicated_proxy() -> DedicatedProxyRoute | None:
    return _CURRENT_ROUTE.get()


def activate_dedicated_proxy_for_user(
    db: Session,
    user_id: int,
) -> Token[DedicatedProxyRoute | None] | None:
    """Activate a strict route for one shared-pool order, if configured."""
    if not get_settings().USE_PROXY:
        return None
    proxy_obj = (
        db.query(Proxy)
        .filter(Proxy.dedicated_user_id == int(user_id))
        .order_by(Proxy.id.asc())
        .first()
    )
    if proxy_obj is None:
        return None

    proxy_value = str(proxy_obj.proxy or "").strip()
    if not proxy_value:
        raise RuntimeError(f"Dedicated proxy for user_id={user_id} is empty")

    route = DedicatedProxyRoute(
        user_id=int(user_id),
        proxy_id=int(proxy_obj.id),
        proxy=proxy_value,
        country_code=(str(proxy_obj.country_code).strip().upper() or None)
        if proxy_obj.country_code
        else None,
        is_mobile=str(proxy_obj.is_mobile or "").upper() == "Y",
        rotation_url=str(proxy_obj.rotation_url or "").strip() or None,
    )
    logger.info(
        "Strict dedicated proxy route enabled for user_id=%s proxy_id=%s proxy=%s",
        route.user_id,
        route.proxy_id,
        sanitize_url(route.proxy, max_length=80),
    )
    return _CURRENT_ROUTE.set(route)


def reset_dedicated_proxy(token: Token[DedicatedProxyRoute | None] | None) -> None:
    if token is not None:
        _CURRENT_ROUTE.reset(token)


def apply_dedicated_proxy(account: Account) -> Account:
    """Apply the active proxy to an ORM object without dirtying Account.proxy."""
    route = get_current_dedicated_proxy()
    if route is not None:
        if hasattr(account, "_sa_instance_state"):
            set_committed_value(account, "proxy", route.proxy)
        else:
            account.proxy = route.proxy
        account.proxy_country_code = route.country_code
        return account
    if not get_settings().USE_PROXY:
        # Runtime override only: cookies/statistics commits must keep the saved assignment.
        if hasattr(account, "_sa_instance_state"):
            set_committed_value(account, "proxy", None)
        else:
            account.proxy = None
        return account
    return account
