"""Проверяем getter/setter user_schema_enabled в app_settings_service.

Это и есть «выключатель из админ-панели»: executor читает значение каждые
~60 с (TTL кеша) и переключает workflow без перезапуска воркеров.
"""
from __future__ import annotations

from contextlib import contextmanager

import pytest
from fastapi import HTTPException

from services.app_settings_service import (
    COMMENTING_MODE_KEY,
    COMMENTING_MODE_OLD,
    COMMENTING_MODE_PARALLEL,
    ORDER_EXECUTION_STRATEGY_BALANCED,
    ORDER_EXECUTION_STRATEGY_FAST,
    ORDER_EXECUTION_STRATEGY_KEY,
    USER_SCHEMA_ENABLED_KEY,
    clear_cache,
    get_commenting_mode,
    get_order_execution_strategy,
    get_user_schema_enabled,
    normalize_commenting_mode,
    normalize_order_execution_strategy,
    set_commenting_mode,
    set_order_execution_strategy,
    set_user_schema_enabled,
)


class _Storage:
    """Имитация AppSettings-сессии: хранит k→v в памяти."""

    def __init__(self) -> None:
        self._data: dict = {}

    def get(self, key: str):
        return self._data.get(key)

    def set(self, key: str, value: str) -> None:
        self._data[key] = value


@pytest.mark.unit
def test_user_schema_enabled_defaults_from_env(monkeypatch):
    """Если в БД ничего нет, читаем значение из config.settings.USER_SCHEMA_ENABLED."""
    clear_cache()
    # Подменяем settings
    from config import settings as cfg
    monkeypatch.setattr(cfg.get_settings(), "USER_SCHEMA_ENABLED", True, raising=False)

    class FakeDb:
        def execute(self, _q):
            class _R:
                def scalar_one_or_none(self):
                    return None
            return _R()

    assert get_user_schema_enabled(FakeDb()) is True


@pytest.mark.unit
def test_user_schema_enabled_reads_stored_value(monkeypatch):
    """Если в БД лежит "false" — get вернёт False вне зависимости от env."""
    from services import app_settings_service as svc
    clear_cache()

    # эмулируем get_setting -> "false"
    def fake_get_setting(_db, key, default=None):
        if key == USER_SCHEMA_ENABLED_KEY:
            return "false"
        return default

    monkeypatch.setattr(svc, "get_setting", fake_get_setting)
    assert svc.get_user_schema_enabled(None, default=True) is False


@pytest.mark.unit
def test_user_schema_enabled_coerces_various_truthy_inputs(monkeypatch):
    from services import app_settings_service as svc
    clear_cache()

    for raw in ("1", 1, True, "true", "YES", "on"):
        def _fake(raw_v):
            def inner(_db, key, default=None):
                return raw_v
            return inner
        monkeypatch.setattr(svc, "get_setting", _fake(raw))
        assert svc.get_user_schema_enabled(None, default=False) is True, (
            f"raw={raw!r} should coerce to True"
        )


@pytest.mark.unit
def test_user_schema_enabled_coerces_various_falsy_inputs(monkeypatch):
    from services import app_settings_service as svc
    clear_cache()

    for raw in ("0", 0, False, "false", "NO", "off"):
        def _fake(raw_v):
            def inner(_db, key, default=None):
                return raw_v
            return inner
        monkeypatch.setattr(svc, "get_setting", _fake(raw))
        assert svc.get_user_schema_enabled(None, default=True) is False, (
            f"raw={raw!r} should coerce to False"
        )


@pytest.mark.unit
def test_set_user_schema_enabled_persists(monkeypatch):
    """set_user_schema_enabled должен вызывать set_setting с правильным ключом."""
    from services import app_settings_service as svc

    captured: dict = {}

    def fake_set_setting(_db, key, value, description=None, updated_by=None):
        captured["key"] = key
        captured["value"] = value
        captured["description"] = description
        captured["updated_by"] = updated_by
        return True

    monkeypatch.setattr(svc, "set_setting", fake_set_setting)
    ok = svc.set_user_schema_enabled(None, True, updated_by=42)
    assert ok is True
    assert captured["key"] == USER_SCHEMA_ENABLED_KEY
    assert captured["value"] is True
    assert captured["updated_by"] == 42
    assert "workflow" in (captured["description"] or "")


@pytest.mark.unit
def test_commenting_mode_defaults_to_old(monkeypatch):
    from services import app_settings_service as svc
    clear_cache()

    monkeypatch.setattr(svc, "get_setting", lambda _db, _key, default=None: default)

    assert get_commenting_mode(None) == COMMENTING_MODE_OLD


@pytest.mark.unit
def test_commenting_mode_reads_parallel(monkeypatch):
    from services import app_settings_service as svc
    clear_cache()

    def fake_get_setting(_db, key, default=None):
        if key == COMMENTING_MODE_KEY:
            return COMMENTING_MODE_PARALLEL
        return default

    monkeypatch.setattr(svc, "get_setting", fake_get_setting)

    assert get_commenting_mode(None) == COMMENTING_MODE_PARALLEL


@pytest.mark.unit
def test_commenting_mode_rejects_invalid_value():
    assert normalize_commenting_mode("unknown") == COMMENTING_MODE_OLD


@pytest.mark.unit
def test_set_commenting_mode_persists(monkeypatch):
    from services import app_settings_service as svc

    captured: dict = {}

    def fake_set_setting(_db, key, value, description=None, updated_by=None):
        captured["key"] = key
        captured["value"] = value
        captured["description"] = description
        captured["updated_by"] = updated_by
        return True

    monkeypatch.setattr(svc, "set_setting", fake_set_setting)
    ok = set_commenting_mode(None, COMMENTING_MODE_PARALLEL, updated_by=42)

    assert ok is True
    assert captured["key"] == COMMENTING_MODE_KEY
    assert captured["value"] == COMMENTING_MODE_PARALLEL
    assert captured["updated_by"] == 42
    assert "parallel" in (captured["description"] or "")


@pytest.mark.unit
def test_order_execution_strategy_defaults_to_balanced(monkeypatch):
    from services import app_settings_service as svc
    clear_cache()

    monkeypatch.setattr(svc, "get_setting", lambda _db, _key, default=None: default)

    assert get_order_execution_strategy(None) == ORDER_EXECUTION_STRATEGY_BALANCED


@pytest.mark.unit
def test_order_execution_strategy_reads_fast(monkeypatch):
    from services import app_settings_service as svc
    clear_cache()

    def fake_get_setting(_db, key, default=None):
        if key == ORDER_EXECUTION_STRATEGY_KEY:
            return ORDER_EXECUTION_STRATEGY_FAST
        return default

    monkeypatch.setattr(svc, "get_setting", fake_get_setting)

    assert get_order_execution_strategy(None) == ORDER_EXECUTION_STRATEGY_FAST


@pytest.mark.unit
def test_order_execution_strategy_rejects_invalid_value():
    assert normalize_order_execution_strategy("wild") == ORDER_EXECUTION_STRATEGY_BALANCED


@pytest.mark.unit
def test_set_order_execution_strategy_persists(monkeypatch):
    from services import app_settings_service as svc

    captured: dict = {}

    def fake_set_setting(_db, key, value, description=None, updated_by=None):
        captured["key"] = key
        captured["value"] = value
        captured["description"] = description
        captured["updated_by"] = updated_by
        return True

    monkeypatch.setattr(svc, "set_setting", fake_set_setting)
    ok = set_order_execution_strategy(None, ORDER_EXECUTION_STRATEGY_FAST, updated_by=42)

    assert ok is True
    assert captured["key"] == ORDER_EXECUTION_STRATEGY_KEY
    assert captured["value"] == ORDER_EXECUTION_STRATEGY_FAST
    assert captured["updated_by"] == 42


@pytest.mark.unit
@pytest.mark.asyncio
async def test_commenting_mode_api_get_returns_current_mode(monkeypatch):
    from api import app_settings as api_settings

    @contextmanager
    def fake_db_context():
        yield object()

    monkeypatch.setattr(api_settings, "get_sync_db_context", fake_db_context)
    monkeypatch.setattr(
        api_settings,
        "get_commenting_mode",
        lambda _db, default=COMMENTING_MODE_OLD: COMMENTING_MODE_PARALLEL,
    )

    response = await api_settings.get_commenting_mode_settings(_=None)

    assert response.commenting_mode == COMMENTING_MODE_PARALLEL
    assert response.is_parallel_mode is True


@pytest.mark.unit
@pytest.mark.asyncio
async def test_commenting_mode_api_update_persists_mode(monkeypatch):
    from api import app_settings as api_settings

    captured = {}

    @contextmanager
    def fake_db_context():
        yield object()

    def fake_set_commenting_mode(_db, mode):
        captured["mode"] = mode
        return True

    monkeypatch.setattr(api_settings, "get_sync_db_context", fake_db_context)
    monkeypatch.setattr(api_settings, "set_commenting_mode", fake_set_commenting_mode)
    monkeypatch.setattr(
        api_settings,
        "get_commenting_mode",
        lambda _db, default=COMMENTING_MODE_OLD: captured["mode"],
    )

    response = await api_settings.update_commenting_mode_settings(
        api_settings.CommentingModeSettingsUpdate(commenting_mode=" PARALLEL "),
        _=None,
    )

    assert captured["mode"] == COMMENTING_MODE_PARALLEL
    assert response.commenting_mode == COMMENTING_MODE_PARALLEL
    assert response.is_parallel_mode is True


@pytest.mark.unit
@pytest.mark.asyncio
async def test_commenting_mode_api_rejects_invalid_mode():
    from api import app_settings as api_settings

    with pytest.raises(HTTPException) as exc:
        await api_settings.update_commenting_mode_settings(
            api_settings.CommentingModeSettingsUpdate(commenting_mode="invalid"),
            _=None,
        )

    assert exc.value.status_code == 400


@pytest.mark.unit
@pytest.mark.asyncio
async def test_order_execution_strategy_api_get_returns_current_strategy(monkeypatch):
    from api import app_settings as api_settings

    @contextmanager
    def fake_db_context():
        yield object()

    monkeypatch.setattr(api_settings, "get_sync_db_context", fake_db_context)
    monkeypatch.setattr(
        api_settings,
        "get_order_execution_strategy",
        lambda _db, default=ORDER_EXECUTION_STRATEGY_BALANCED: ORDER_EXECUTION_STRATEGY_FAST,
    )

    response = await api_settings.get_order_execution_strategy_settings(_=None)

    assert response.order_execution_strategy == ORDER_EXECUTION_STRATEGY_FAST
    assert ORDER_EXECUTION_STRATEGY_BALANCED in response.available_strategies


@pytest.mark.unit
@pytest.mark.asyncio
async def test_order_execution_strategy_api_update_persists_strategy(monkeypatch):
    from api import app_settings as api_settings

    captured = {}

    @contextmanager
    def fake_db_context():
        yield object()

    def fake_set_strategy(_db, strategy):
        captured["strategy"] = strategy
        return True

    monkeypatch.setattr(api_settings, "get_sync_db_context", fake_db_context)
    monkeypatch.setattr(api_settings, "set_order_execution_strategy", fake_set_strategy)
    monkeypatch.setattr(
        api_settings,
        "get_order_execution_strategy",
        lambda _db, default=ORDER_EXECUTION_STRATEGY_BALANCED: captured["strategy"],
    )

    response = await api_settings.update_order_execution_strategy_settings(
        api_settings.OrderExecutionStrategyUpdate(order_execution_strategy=" FAST "),
        _=None,
    )

    assert captured["strategy"] == ORDER_EXECUTION_STRATEGY_FAST
    assert response.order_execution_strategy == ORDER_EXECUTION_STRATEGY_FAST


@pytest.mark.unit
@pytest.mark.asyncio
async def test_order_execution_strategy_api_rejects_invalid_strategy():
    from api import app_settings as api_settings

    with pytest.raises(HTTPException) as exc:
        await api_settings.update_order_execution_strategy_settings(
            api_settings.OrderExecutionStrategyUpdate(order_execution_strategy="invalid"),
            _=None,
        )

    assert exc.value.status_code == 400
