"""Единый модуль для аутентификации и авторизации API."""
from fastapi import HTTPException, Header
from typing import Annotated
from services.auth_service import verify_admin_token, verify_executor_token
from services.user_scope_signature import verify_user_scope_signature
from config.settings import get_settings


def verify_admin_header(x_admin_token: Annotated[str | None, Header()] = None) -> None:
    """Проверяет административный токен."""
    if not x_admin_token or not verify_admin_token(x_admin_token):
        raise HTTPException(status_code=403, detail="Access denied")


def verify_executor_header(x_internal_token: Annotated[str | None, Header()] = None) -> None:
    """Проверяет токен executor'а."""
    if not x_internal_token or not verify_executor_token(x_internal_token):
        raise HTTPException(status_code=403, detail="Access denied")


def verify_user_header(
    x_admin_token: Annotated[str | None, Header()] = None,
    x_user_id: Annotated[int | None, Header()] = None,
    x_user_signature: Annotated[str | None, Header()] = None,
    x_user_ts: Annotated[int | None, Header()] = None,
) -> int:
    """
    Проверяет токен API и возвращает user_id из заголовка.
    Используется для user-endpoint'ов, чтобы привязать запрос к конкретному пользователю.
    """
    if not x_admin_token or not verify_admin_token(x_admin_token):
        raise HTTPException(status_code=403, detail="Access denied")
    if x_user_id is None:
        raise HTTPException(status_code=403, detail="Missing X-User-Id header")
    if x_user_ts is None:
        raise HTTPException(status_code=403, detail="Missing X-User-Ts header")
    if not verify_user_scope_signature(
        user_id=x_user_id,
        timestamp=x_user_ts,
        signature=x_user_signature,
        secret=get_settings().JWT_SECRET,
    ):
        raise HTTPException(status_code=403, detail="Invalid user scope signature")
    return x_user_id


def ensure_user_scope(expected_user_id: int, authenticated_user_id: int) -> None:
    """Гарантирует, что запрос выполняется только в рамках пользователя из заголовка."""
    if expected_user_id != authenticated_user_id:
        raise HTTPException(status_code=403, detail="User scope mismatch")









