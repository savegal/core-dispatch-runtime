"""
Сервис для работы с настройками приложения из базы данных.
Настройки кэшируются для производительности и обновляются при изменении.
"""
import json
import logging
from typing import Optional, Any
from sqlalchemy.orm import Session
from sqlalchemy import select
from db.models import AppSettings
from datetime import datetime, timezone
from services.likes_config import (
    BOOST_LIKES_DEFAULT,
    BOOST_LIKES_ENABLED_KEY,
    BOOST_LIKES_MAX,
    BOOST_LIKES_MIN,
    BOOST_LIKES_QUANTITY_KEY,
    BOOST_LIKES_TARGET_MODE_KEY,
    BOOST_LIKES_TARGET_SINGLE_TOP,
    coerce_bool,
    coerce_likes_quantity,
    coerce_likes_target_mode,
)

logger = logging.getLogger("services.app_settings")

# Кэш настроек (TTL 60 секунд)
_settings_cache: dict[str, tuple[Any, float]] = {}
_settings_stale: dict[str, Any] = {}   # last-known-good values, never expires — used when DB is down
CACHE_TTL = 60.0  # секунды

ACCOUNT_PROXY_TEST_MODE_KEY = "account_proxy_test_mode"
SOFTWARE_MODE_KEY = "software_mode"
SOFTWARE_MODE_FULL = "full"
SOFTWARE_MODE_PARSING = "parsing"
SOFTWARE_MODES = {SOFTWARE_MODE_FULL, SOFTWARE_MODE_PARSING}
COMMENTING_MODE_KEY = "commenting_mode"
COMMENTING_MODE_OLD = "old"
COMMENTING_MODE_PARALLEL = "parallel"
COMMENTING_MODE_PARALLEL_6X1 = "parallel_6x1"
COMMENTING_MODES = {
    COMMENTING_MODE_OLD,
    COMMENTING_MODE_PARALLEL,
    COMMENTING_MODE_PARALLEL_6X1,
}
ORDER_EXECUTION_STRATEGY_KEY = "order_execution_strategy"
ORDER_EXECUTION_STRATEGY_BALANCED = "balanced"
ORDER_EXECUTION_STRATEGY_FAST = "fast"
ORDER_EXECUTION_STRATEGY_CAREFUL = "careful"
ORDER_EXECUTION_STRATEGY_VERIFY = "verify"
ORDER_EXECUTION_STRATEGIES = {
    ORDER_EXECUTION_STRATEGY_BALANCED,
    ORDER_EXECUTION_STRATEGY_FAST,
    ORDER_EXECUTION_STRATEGY_CAREFUL,
    ORDER_EXECUTION_STRATEGY_VERIFY,
}

AUTOPILOT_CONTROL_MODE_KEY = "autopilot_control_mode"
AUTOPILOT_CONTROL_ACTIVE = "active"
AUTOPILOT_CONTROL_PAUSED = "paused"
AUTOPILOT_CONTROL_DRAIN = "drain"
AUTOPILOT_CONTROL_MODES = {
    AUTOPILOT_CONTROL_ACTIVE,
    AUTOPILOT_CONTROL_PAUSED,
    AUTOPILOT_CONTROL_DRAIN,
}
AUTOPILOT_MAX_CREATE_PER_TICK_KEY = "autopilot_max_create_per_tick"
AUTOPILOT_MAX_CREATE_PER_CREATOR_KEY = "autopilot_max_create_per_creator"
AUTOPILOT_MAX_CREATE_PER_HOUR_KEY = "autopilot_max_create_per_hour"
AUTOPILOT_DEFAULT_MAX_CREATE_PER_TICK = 20
AUTOPILOT_DEFAULT_MAX_CREATE_PER_CREATOR = 3
AUTOPILOT_DEFAULT_MAX_CREATE_PER_HOUR = 120

MONITOR_MAX_PARALLEL_BROWSERS_KEY = "monitor_max_parallel_browsers"
MONITOR_MAX_PARALLEL_MIN = 1
MONITOR_MAX_PARALLEL_MAX = 8


def get_setting(db: Session, key: str, default: Any = None) -> Any:
    """
    Получает значение настройки из БД с кэшированием.
    
    Args:
        db: SQLAlchemy сессия
        key: ключ настройки
        default: значение по умолчанию, если настройка не найдена
        
    Returns:
        Значение настройки (десериализованное из JSON если нужно)
    """
    import time
    current_time = time.time()
    
    # Проверяем кэш
    if key in _settings_cache:
        cached_value, cache_time = _settings_cache[key]
        if current_time - cache_time < CACHE_TTL:
            return cached_value
        else:
            # Кэш устарел
            del _settings_cache[key]
    
    # Загружаем из БД
    try:
        result = db.execute(select(AppSettings).filter(AppSettings.key == key))
        setting = result.scalar_one_or_none()
        
        if setting is None:
            # Настройка не найдена - возвращаем default и кэшируем
            _settings_cache[key] = (default, current_time)
            return default
        
        # Десериализуем значение (может быть JSON или строка)
        value = setting.value
        if value:
            try:
                # Пробуем распарсить как JSON
                value = json.loads(value)
            except (json.JSONDecodeError, TypeError):
                # Если не JSON - оставляем как строку
                pass
        
        # Кэшируем и обновляем stale-копию
        _settings_cache[key] = (value, current_time)
        _settings_stale[key] = value
        return value

    except Exception as e:
        error_str = str(e).lower()
        if "no such table" in error_str or "does not exist" in error_str:
            logger.warning(f"Table app_settings does not exist, returning default for {key}")
            _settings_cache[key] = (default, current_time)
            return default
        else:
            logger.error(f"Error getting setting {key}: {e}", exc_info=True)
            # Возвращаем последнее известное значение вместо жёсткого дефолта
            if key in _settings_stale:
                logger.warning(f"DB unavailable — returning stale value for '{key}'")
                return _settings_stale[key]
            return default


def set_setting(db: Session, key: str, value: Any, description: str | None = None, updated_by: int | None = None) -> bool:
    """
    Устанавливает значение настройки в БД и обновляет кэш.
    
    Args:
        db: SQLAlchemy сессия
        key: ключ настройки
        value: значение настройки (будет сериализовано в JSON если не строка)
        description: описание настройки (опционально)
        updated_by: Telegram ID админа, который обновил (опционально)
        
    Returns:
        True если успешно, False если ошибка
    """
    import time
    try:
        # Сериализуем значение
        if isinstance(value, str):
            value_str = value
        else:
            value_str = json.dumps(value)
        
        # Ищем существующую настройку
        result = db.execute(select(AppSettings).filter(AppSettings.key == key))
        setting = result.scalar_one_or_none()
        
        if setting is None:
            # Создаём новую настройку
            setting = AppSettings(
                key=key,
                value=value_str,
                description=description,
                updated_by=updated_by
            )
            db.add(setting)
        else:
            # Обновляем существующую
            setting.value = value_str
            if description is not None:
                setting.description = description
            if updated_by is not None:
                setting.updated_by = updated_by
            setting.updated_at = datetime.now(timezone.utc)
        
        db.commit()
        db.refresh(setting)
        
        # Обновляем кэш и stale-копию
        current_time = time.time()
        _settings_cache[key] = (value, current_time)
        _settings_stale[key] = value
        
        logger.info(f"Setting {key} updated to {value_str[:50]}")
        return True
        
    except Exception as e:
        # ИСПРАВЛЕНО: Обрабатываем ошибку отсутствия таблицы gracefully
        error_str = str(e).lower()
        if "no such table" in error_str or "does not exist" in error_str:
            logger.warning(f"Table app_settings does not exist, cannot set {key}")
            db.rollback()
            return False
        else:
            logger.error(f"Error setting {key}: {e}", exc_info=True)
            db.rollback()
            return False


def get_proxy_type_filter(db: Session) -> Optional[str]:
    """Получает фильтр типа прокси из БД."""
    return get_setting(db, "proxy_type_filter", None)


def set_proxy_type_filter(db: Session, value: Optional[str], updated_by: int | None = None) -> bool:
    """Устанавливает фильтр типа прокси в БД."""
    valid_values = [None, "mobile", "residential"]
    if value not in valid_values:
        logger.warning(f"Invalid proxy_type_filter value: {value}, must be one of {valid_values}")
        return False
    return set_setting(
        db, 
        "proxy_type_filter", 
        value, 
        description="Фильтр типа прокси: mobile (только мобильные), residential (только резидентские), None (все)",
        updated_by=updated_by
    )


def get_proxy_prefer_mobile(db: Session) -> bool:
    """Получает настройку предпочтения мобильных прокси из БД."""
    return get_setting(db, "proxy_prefer_mobile", False)


def set_proxy_prefer_mobile(db: Session, value: bool, updated_by: int | None = None) -> bool:
    """Устанавливает настройку предпочтения мобильных прокси в БД."""
    return set_setting(
        db,
        "proxy_prefer_mobile",
        value,
        description="Предпочитать мобильные прокси при назначении (если proxy_type_filter=None)",
        updated_by=updated_by
    )


def clear_cache():
    """Очищает кэш настроек (используется при обновлении настроек)."""
    global _settings_cache
    _settings_cache.clear()
    logger.debug("Settings cache cleared")


def normalize_software_mode(value: Any, default: str = SOFTWARE_MODE_FULL) -> str:
    normalized = str(value or "").strip().lower()
    if normalized in SOFTWARE_MODES:
        return normalized
    return default if default in SOFTWARE_MODES else SOFTWARE_MODE_FULL


def get_software_mode(db: Session, default: str = SOFTWARE_MODE_FULL) -> str:
    raw = get_setting(db, SOFTWARE_MODE_KEY, default)
    return normalize_software_mode(raw, default=default)


def set_software_mode(db: Session, mode: str, updated_by: int | None = None) -> bool:
    normalized = normalize_software_mode(mode, default="")
    if normalized not in SOFTWARE_MODES:
        logger.warning(f"Invalid software_mode value: {mode}")
        return False
    return set_setting(
        db,
        SOFTWARE_MODE_KEY,
        normalized,
        description="Global software mode: full exposes all bot features, parsing exposes video monitoring notifications only.",
        updated_by=updated_by,
    )


def is_parsing_mode(db: Session) -> bool:
    return get_software_mode(db) == SOFTWARE_MODE_PARSING


def normalize_commenting_mode(value: Any, default: str = COMMENTING_MODE_OLD) -> str:
    normalized = str(value or "").strip().lower()
    if normalized in COMMENTING_MODES:
        return normalized
    return default if default in COMMENTING_MODES else COMMENTING_MODE_OLD


def get_commenting_mode(db: Session, default: str = COMMENTING_MODE_OLD) -> str:
    raw = get_setting(db, COMMENTING_MODE_KEY, default)
    return normalize_commenting_mode(raw, default=default)


def set_commenting_mode(db: Session, mode: str, updated_by: int | None = None) -> bool:
    normalized = normalize_commenting_mode(mode, default="")
    if normalized not in COMMENTING_MODES:
        logger.warning(f"Invalid commenting_mode value: {mode}")
        return False
    return set_setting(
        db,
        COMMENTING_MODE_KEY,
        normalized,
        description=(
            "Commenting workflow mode: old keeps the current user-schema flow; "
            "parallel publishes up to three main comments in parallel and replies "
            "in batches of up to three accounts without reply-side fallback mains; "
            "parallel_6x1 publishes two strict waves of three one-comment accounts "
            "with a mandatory mobile-IP rotation between waves and reuses the "
            "parallel reply workflow."
        ),
        updated_by=updated_by,
    )


def normalize_order_execution_strategy(
    value: Any,
    default: str = ORDER_EXECUTION_STRATEGY_BALANCED,
) -> str:
    normalized = str(value or "").strip().lower()
    if normalized in ORDER_EXECUTION_STRATEGIES:
        return normalized
    return default if default in ORDER_EXECUTION_STRATEGIES else ORDER_EXECUTION_STRATEGY_BALANCED


def get_order_execution_strategy(
    db: Session,
    default: str = ORDER_EXECUTION_STRATEGY_BALANCED,
) -> str:
    raw = get_setting(db, ORDER_EXECUTION_STRATEGY_KEY, default)
    return normalize_order_execution_strategy(raw, default=default)


def set_order_execution_strategy(
    db: Session,
    strategy: str,
    updated_by: int | None = None,
) -> bool:
    normalized = normalize_order_execution_strategy(strategy, default="")
    if normalized not in ORDER_EXECUTION_STRATEGIES:
        logger.warning(f"Invalid order_execution_strategy value: {strategy}")
        return False
    return set_setting(
        db,
        ORDER_EXECUTION_STRATEGY_KEY,
        normalized,
        description=(
            "Order execution strategy: balanced keeps current behavior; fast/careful/verify "
            "are explicit operator modes for future retry and verification policy tuning."
        ),
        updated_by=updated_by,
    )


def normalize_autopilot_control_mode(value: Any, default: str = AUTOPILOT_CONTROL_ACTIVE) -> str:
    normalized = str(value or "").strip().lower()
    if normalized in AUTOPILOT_CONTROL_MODES:
        return normalized
    return default if default in AUTOPILOT_CONTROL_MODES else AUTOPILOT_CONTROL_ACTIVE


def get_autopilot_control_mode(
    db: Session,
    default: str = AUTOPILOT_CONTROL_ACTIVE,
) -> str:
    raw = get_setting(db, AUTOPILOT_CONTROL_MODE_KEY, default)
    return normalize_autopilot_control_mode(raw, default=default)


def set_autopilot_control_mode(
    db: Session,
    mode: str,
    updated_by: int | None = None,
) -> bool:
    normalized = normalize_autopilot_control_mode(mode, default="")
    if normalized not in AUTOPILOT_CONTROL_MODES:
        logger.warning(f"Invalid autopilot_control_mode value: {mode}")
        return False
    return set_setting(
        db,
        AUTOPILOT_CONTROL_MODE_KEY,
        normalized,
        description="Autopilot order creation mode: active, paused, or drain.",
        updated_by=updated_by,
    )


def get_autopilot_limits(db: Session) -> dict[str, int]:
    return {
        "max_create_per_tick": _coerce_int(
            get_setting(db, AUTOPILOT_MAX_CREATE_PER_TICK_KEY, AUTOPILOT_DEFAULT_MAX_CREATE_PER_TICK),
            AUTOPILOT_DEFAULT_MAX_CREATE_PER_TICK,
            0,
            1000,
        ),
        "max_create_per_creator": _coerce_int(
            get_setting(db, AUTOPILOT_MAX_CREATE_PER_CREATOR_KEY, AUTOPILOT_DEFAULT_MAX_CREATE_PER_CREATOR),
            AUTOPILOT_DEFAULT_MAX_CREATE_PER_CREATOR,
            0,
            1000,
        ),
        "max_create_per_hour": _coerce_int(
            get_setting(db, AUTOPILOT_MAX_CREATE_PER_HOUR_KEY, AUTOPILOT_DEFAULT_MAX_CREATE_PER_HOUR),
            AUTOPILOT_DEFAULT_MAX_CREATE_PER_HOUR,
            0,
            10000,
        ),
    }


def set_autopilot_limits(
    db: Session,
    *,
    max_create_per_tick: int | None = None,
    max_create_per_creator: int | None = None,
    max_create_per_hour: int | None = None,
    updated_by: int | None = None,
) -> bool:
    updates: list[tuple[str, int, str]] = []
    if max_create_per_tick is not None:
        updates.append(
            (
                AUTOPILOT_MAX_CREATE_PER_TICK_KEY,
                _coerce_int(max_create_per_tick, AUTOPILOT_DEFAULT_MAX_CREATE_PER_TICK, 0, 1000),
                "Maximum autopilot orders created from monitor outbox per monitor pass. 0 disables new creation.",
            )
        )
    if max_create_per_creator is not None:
        updates.append(
            (
                AUTOPILOT_MAX_CREATE_PER_CREATOR_KEY,
                _coerce_int(max_create_per_creator, AUTOPILOT_DEFAULT_MAX_CREATE_PER_CREATOR, 0, 1000),
                "Maximum autopilot orders created for one creator from one outbox event. 0 disables new creation.",
            )
        )
    if max_create_per_hour is not None:
        updates.append(
            (
                AUTOPILOT_MAX_CREATE_PER_HOUR_KEY,
                _coerce_int(max_create_per_hour, AUTOPILOT_DEFAULT_MAX_CREATE_PER_HOUR, 0, 10000),
                "Maximum autopilot orders created globally per rolling hour. 0 disables new creation.",
            )
        )

    ok = True
    for key, value, description in updates:
        ok = set_setting(db, key, value, description=description, updated_by=updated_by) and ok
    return ok


def _coerce_bool(value: Any, default: bool) -> bool:
    return coerce_bool(value, default)


def get_account_proxy_test_mode(db: Session, default: bool = False) -> bool:
    """Возвращает, включён ли тестовый режим аккаунтов/прокси."""
    raw = get_setting(db, ACCOUNT_PROXY_TEST_MODE_KEY, default)
    return _coerce_bool(raw, default)


def set_account_proxy_test_mode(db: Session, value: bool, updated_by: int | None = None) -> bool:
    """Сохраняет флаг тестового режима аккаунтов/прокси."""
    return set_setting(
        db,
        ACCOUNT_PROXY_TEST_MODE_KEY,
        bool(value),
        description="Тестовый режим аккаунтов/прокси: ослабляет proxy-фильтры и разрешает временно шарить residential proxy.",
        updated_by=updated_by,
    )


def _coerce_int(value: Any, default: int, min_value: int, max_value: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return max(min_value, min(max_value, parsed))


# ── User-schema workflow toggle ───────────────────────────────────────────────

USER_SCHEMA_ENABLED_KEY = "user_schema_enabled"


def get_user_schema_enabled(db: Session, default: bool | None = None) -> bool:
    """Возвращает флаг включения user-schema workflow.

    Источник истины: app_settings таблица (управляется из админ-панели).
    Если в БД ничего нет — берём дефолт из config.settings.USER_SCHEMA_ENABLED
    (можно подменить через .env). Это даёт переход с env → DB без потери
    поведения.
    """
    if default is None:
        from config.settings import get_settings as _gs

        default = bool(getattr(_gs(), "USER_SCHEMA_ENABLED", True))
    raw = get_setting(db, USER_SCHEMA_ENABLED_KEY, default)
    return _coerce_bool(raw, default)


def set_user_schema_enabled(db: Session, value: bool, updated_by: int | None = None) -> bool:
    """Включает/выключает user-schema workflow через админ-панель."""
    return set_setting(
        db,
        USER_SCHEMA_ENABLED_KEY,
        bool(value),
        description=(
            "Включить новый workflow по схеме владельца: один аккаунт пишет "
            "все варианты главного коммента подряд, reply-аккаунты ищут любой "
            "из них и отвечают по матрице, лайки на реальный TikTok username. "
            "При выключении возвращается старая логика execute_hybrid_full."
        ),
        updated_by=updated_by,
    )


def get_boost_likes_enabled(db: Session, default: bool = True) -> bool:
    """Возвращает флаг, включены ли лайки на комментарии."""
    raw = get_setting(db, BOOST_LIKES_ENABLED_KEY, default)
    return _coerce_bool(raw, default)


def set_boost_likes_enabled(db: Session, value: bool, updated_by: int | None = None) -> bool:
    """Сохраняет флаг включения лайков на комментарии."""
    return set_setting(
        db,
        BOOST_LIKES_ENABLED_KEY,
        bool(value),
        description="Включать лайки на комментарии",
        updated_by=updated_by,
    )


def get_boost_likes_quantity(db: Session, default: int = BOOST_LIKES_DEFAULT) -> int:
    """Возвращает количество лайков на один заказ (с нормализацией диапазона)."""
    raw = get_setting(db, BOOST_LIKES_QUANTITY_KEY, default)
    return coerce_likes_quantity(raw, default)


def set_boost_likes_quantity(db: Session, value: int, updated_by: int | None = None) -> bool:
    """Сохраняет количество лайков на один заказ."""
    normalized = coerce_likes_quantity(value, BOOST_LIKES_DEFAULT)
    return set_setting(
        db,
        BOOST_LIKES_QUANTITY_KEY,
        normalized,
        description=(
            "Количество лайков на комментарий. "
            f"Диапазон: {BOOST_LIKES_MIN}-{BOOST_LIKES_MAX}"
        ),
        updated_by=updated_by,
    )


def get_boost_likes_target_mode(db: Session, default: str = BOOST_LIKES_TARGET_SINGLE_TOP) -> str:
    """Returns how parallel comment likes choose Boost targets."""
    raw = get_setting(db, BOOST_LIKES_TARGET_MODE_KEY, default)
    return coerce_likes_target_mode(raw, default)


def set_boost_likes_target_mode(db: Session, value: str, updated_by: int | None = None) -> bool:
    """Stores how parallel comment likes choose Boost targets."""
    normalized = coerce_likes_target_mode(value, BOOST_LIKES_TARGET_SINGLE_TOP)
    return set_setting(
        db,
        BOOST_LIKES_TARGET_MODE_KEY,
        normalized,
        description=(
            "How Boost comment likes choose targets in parallel commenting: "
            "single_top or each_found."
        ),
        updated_by=updated_by,
    )


def get_monitor_max_parallel_browsers(db: Session) -> int:
    """Максимум одновременных браузеров Playwright в video_monitor_worker."""
    from config.settings import get_settings as _gs

    default = int(_gs().MONITOR_MAX_PARALLEL_BROWSERS)
    raw = get_setting(db, MONITOR_MAX_PARALLEL_BROWSERS_KEY, default)
    return _coerce_int(raw, default, MONITOR_MAX_PARALLEL_MIN, MONITOR_MAX_PARALLEL_MAX)


def set_monitor_max_parallel_browsers(db: Session, value: int, updated_by: int | None = None) -> bool:
    from config.settings import get_settings as _gs

    default = int(_gs().MONITOR_MAX_PARALLEL_BROWSERS)
    normalized = _coerce_int(value, default, MONITOR_MAX_PARALLEL_MIN, MONITOR_MAX_PARALLEL_MAX)
    return set_setting(
        db,
        MONITOR_MAX_PARALLEL_BROWSERS_KEY,
        normalized,
        description=(
            "Максимум одновременных браузеров в video_monitor_worker (проверка блогеров). "
            f"Диапазон {MONITOR_MAX_PARALLEL_MIN}-{MONITOR_MAX_PARALLEL_MAX}. Применяется без перезапуска (кэш настроек ~60 с)."
        ),
        updated_by=updated_by,
    )


# ── Адаптивный мониторинг ─────────────────────────────────────────────────────

MONITOR_ADAPTIVE_ENABLED_KEY               = "monitor_adaptive_enabled"
MONITOR_ADAPTIVE_MIN_INTERVAL_KEY          = "monitor_adaptive_min_interval"
MONITOR_ADAPTIVE_MAX_INTERVAL_KEY          = "monitor_adaptive_max_interval"
MONITOR_ADAPTIVE_SANITY_CAP_FACTOR_KEY     = "monitor_adaptive_sanity_cap_factor"
MONITOR_ADAPTIVE_ACTIVE_PHASE_INT_KEY      = "monitor_adaptive_active_phase_interval"
MONITOR_ADAPTIVE_ACTIVE_PHASE_HRS_KEY      = "monitor_adaptive_active_phase_hours"
MONITOR_ADAPTIVE_FOUND_NEW_RESET_KEY       = "monitor_adaptive_found_new_reset_factor"
MONITOR_ADAPTIVE_BURST_THRESHOLD_KEY       = "monitor_adaptive_burst_threshold_24h"
MONITOR_ADAPTIVE_BURST_MULTIPLIER_KEY      = "monitor_adaptive_burst_multiplier"
MONITOR_ADAPTIVE_BURST_DURATION_KEY        = "monitor_adaptive_burst_duration_hours"
MONITOR_ADAPTIVE_SILENCE_MULT_KEY          = "monitor_adaptive_silence_multiplier"
MONITOR_ADAPTIVE_SILENCE_MAX_FACTOR_KEY    = "monitor_adaptive_silence_max_factor"
MONITOR_ADAPTIVE_NIGHTTIME_MULT_KEY        = "monitor_adaptive_nighttime_multiplier"
MONITOR_ADAPTIVE_EMPTY_STREAK_THRESH_KEY   = "monitor_adaptive_empty_streak_threshold"
MONITOR_ADAPTIVE_EMPTY_STREAK_MULT_KEY     = "monitor_adaptive_empty_streak_multiplier"
MONITOR_ADAPTIVE_GAP_DIVISOR_KEY           = "monitor_adaptive_gap_divisor"
MONITOR_ADAPTIVE_USE_P25_BURST_KEY         = "monitor_adaptive_use_p25_for_burst"
MONITOR_ADAPTIVE_HOUR_MIN_TOTAL_KEY        = "monitor_adaptive_hour_histogram_min_total"
MONITOR_ADAPTIVE_ACTIVE_HOUR_THRESH_KEY    = "monitor_adaptive_active_hour_threshold"
MONITOR_ADAPTIVE_DEAD_HOUR_THRESH_KEY      = "monitor_adaptive_dead_hour_threshold"
MONITOR_ADAPTIVE_ACTIVE_HOUR_MULT_KEY      = "monitor_adaptive_active_hour_multiplier"
MONITOR_ADAPTIVE_MAX_KEEP_KEY              = "monitor_adaptive_max_keep"
MONITOR_ADAPTIVE_MAX_HISTORY_DAYS_KEY      = "monitor_adaptive_max_history_days"
MONITOR_ADAPTIVE_MIN_SAMPLES_KEY           = "monitor_adaptive_min_samples"
MONITOR_ADAPTIVE_INITIAL_LIMIT_KEY         = "monitor_adaptive_initial_limit"
MONITOR_ADAPTIVE_CHECK_LOG_RETENTION_KEY   = "monitor_adaptive_check_log_retention_days"
MONITOR_ADAPTIVE_BURST_RELATIVE_KEY        = "monitor_adaptive_burst_relative_factor"
MONITOR_ADAPTIVE_PRED_MIN_CONF_KEY         = "monitor_adaptive_prediction_min_confidence"
MONITOR_ADAPTIVE_PRED_BUFFER_MIN_KEY       = "monitor_adaptive_prediction_buffer_minutes"
MONITOR_ADAPTIVE_PRED_HIGH_CONF_KEY         = "monitor_adaptive_prediction_high_confidence"
MONITOR_ADAPTIVE_RECENT_GAP_WEIGHT_KEY      = "monitor_adaptive_recent_gap_weight"
MONITOR_ADAPTIVE_FEEDBACK_ENABLED_KEY       = "monitor_adaptive_feedback_enabled"
MONITOR_ADAPTIVE_FEEDBACK_TARGET_KEY        = "monitor_adaptive_feedback_target_latency_min"
MONITOR_ADAPTIVE_FEEDBACK_ALPHA_KEY         = "monitor_adaptive_feedback_ema_alpha"
MONITOR_ADAPTIVE_FEEDBACK_MIN_FACTOR_KEY    = "monitor_adaptive_feedback_min_factor"
MONITOR_ADAPTIVE_FEEDBACK_MAX_FACTOR_KEY    = "monitor_adaptive_feedback_max_factor"
MONITOR_ADAPTIVE_FEEDBACK_LATE_THRESH_KEY   = "monitor_adaptive_feedback_late_threshold"


def _coerce_float(value: Any, default: float, min_value: float, max_value: float) -> float:
    try:
        result = float(value)
        return max(min_value, min(result, max_value))
    except (TypeError, ValueError):
        return default


def get_adaptive_enabled(db: Session, default: bool = True) -> bool:
    return _coerce_bool(get_setting(db, MONITOR_ADAPTIVE_ENABLED_KEY, default), default)


def set_adaptive_enabled(db: Session, value: bool, updated_by: int | None = None) -> bool:
    return set_setting(
        db, MONITOR_ADAPTIVE_ENABLED_KEY, bool(value),
        description="Включить адаптивный интервал мониторинга (на основе истории постов)",
        updated_by=updated_by,
    )


def get_adaptive_min_interval(db: Session, default: int = 5) -> int:
    return _coerce_int(get_setting(db, MONITOR_ADAPTIVE_MIN_INTERVAL_KEY, default), default, 1, 1440)


def set_adaptive_min_interval(db: Session, value: int, updated_by: int | None = None) -> bool:
    return set_setting(
        db, MONITOR_ADAPTIVE_MIN_INTERVAL_KEY, _coerce_int(value, 5, 1, 1440),
        description="Минимальный интервал проверки блогера (минуты)",
        updated_by=updated_by,
    )


def get_adaptive_max_interval(db: Session, default: int = 300) -> int:
    return _coerce_int(get_setting(db, MONITOR_ADAPTIVE_MAX_INTERVAL_KEY, default), default, 1, 1440)


def set_adaptive_max_interval(db: Session, value: int, updated_by: int | None = None) -> bool:
    return set_setting(
        db, MONITOR_ADAPTIVE_MAX_INTERVAL_KEY, _coerce_int(value, 300, 1, 1440),
        description="Максимальный интервал проверки блогера (минуты)",
        updated_by=updated_by,
    )


def get_adaptive_active_phase_interval(db: Session, default: int = 20) -> int:
    return _coerce_int(get_setting(db, MONITOR_ADAPTIVE_ACTIVE_PHASE_INT_KEY, default), default, 1, 1440)


def set_adaptive_active_phase_interval(db: Session, value: int, updated_by: int | None = None) -> bool:
    return set_setting(
        db, MONITOR_ADAPTIVE_ACTIVE_PHASE_INT_KEY, _coerce_int(value, 20, 1, 1440),
        description="Интервал проверки сразу после нового поста (активная фаза, минуты)",
        updated_by=updated_by,
    )


def get_adaptive_active_phase_hours(db: Session, default: float = 2.0) -> float:
    return _coerce_float(get_setting(db, MONITOR_ADAPTIVE_ACTIVE_PHASE_HRS_KEY, default), default, 0.1, 72.0)


def set_adaptive_active_phase_hours(db: Session, value: float, updated_by: int | None = None) -> bool:
    return set_setting(
        db, MONITOR_ADAPTIVE_ACTIVE_PHASE_HRS_KEY, _coerce_float(value, 2.0, 0.1, 72.0),
        description="Длительность активной фазы после поста (часы)",
        updated_by=updated_by,
    )


def get_adaptive_silence_multiplier(db: Session, default: float = 4.5) -> float:
    return _coerce_float(get_setting(db, MONITOR_ADAPTIVE_SILENCE_MULT_KEY, default), default, 1.0, 20.0)


def set_adaptive_silence_multiplier(db: Session, value: float, updated_by: int | None = None) -> bool:
    return set_setting(
        db, MONITOR_ADAPTIVE_SILENCE_MULT_KEY, _coerce_float(value, 4.5, 1.0, 20.0),
        description="Порог тишины: кратность медианного интервала → переключение в silent",
        updated_by=updated_by,
    )


def get_adaptive_nighttime_multiplier(db: Session, default: float = 2.0) -> float:
    return _coerce_float(get_setting(db, MONITOR_ADAPTIVE_NIGHTTIME_MULT_KEY, default), default, 1.0, 10.0)


def set_adaptive_nighttime_multiplier(db: Session, value: float, updated_by: int | None = None) -> bool:
    return set_setting(
        db, MONITOR_ADAPTIVE_NIGHTTIME_MULT_KEY, _coerce_float(value, 2.0, 1.0, 10.0),
        description="Ночной множитель интервала (вне активных часов блогера)",
        updated_by=updated_by,
    )


def get_adaptive_min_samples(db: Session, default: int = 5) -> int:
    return _coerce_int(get_setting(db, MONITOR_ADAPTIVE_MIN_SAMPLES_KEY, default), default, 2, 100)


def set_adaptive_min_samples(db: Session, value: int, updated_by: int | None = None) -> bool:
    return set_setting(
        db, MONITOR_ADAPTIVE_MIN_SAMPLES_KEY, _coerce_int(value, 5, 2, 100),
        description="Минимальное количество видео для активации адаптивного расчёта",
        updated_by=updated_by,
    )


def get_adaptive_initial_limit(db: Session, default: int = 30) -> int:
    return _coerce_int(get_setting(db, MONITOR_ADAPTIVE_INITIAL_LIMIT_KEY, default), default, 6, 50)


def set_adaptive_initial_limit(db: Session, value: int, updated_by: int | None = None) -> bool:
    return set_setting(
        db, MONITOR_ADAPTIVE_INITIAL_LIMIT_KEY, _coerce_int(value, 30, 6, 50),
        description="Количество видео для начального парсинга (для набора базы timestamp'ов)",
        updated_by=updated_by,
    )


# ── New settings (v2) ─────────────────────────────────────────────────────────

def get_adaptive_sanity_cap_factor(db: Session, default: float = 2.0) -> float:
    return _coerce_float(get_setting(db, MONITOR_ADAPTIVE_SANITY_CAP_FACTOR_KEY, default), default, 1.0, 10.0)

def set_adaptive_sanity_cap_factor(db: Session, value: float, updated_by: int | None = None) -> bool:
    return set_setting(db, MONITOR_ADAPTIVE_SANITY_CAP_FACTOR_KEY, _coerce_float(value, 2.0, 1.0, 10.0),
        description="Аварийный cap: интервал не может быть > max_interval × этого числа", updated_by=updated_by)


def get_adaptive_found_new_reset_factor(db: Session, default: float = 0.25) -> float:
    return _coerce_float(get_setting(db, MONITOR_ADAPTIVE_FOUND_NEW_RESET_KEY, default), default, 0.1, 1.0)

def set_adaptive_found_new_reset_factor(db: Session, value: float, updated_by: int | None = None) -> bool:
    return set_setting(db, MONITOR_ADAPTIVE_FOUND_NEW_RESET_KEY, _coerce_float(value, 0.25, 0.1, 1.0),
        description="После нового видео: интервал = median × этот фактор", updated_by=updated_by)


def get_adaptive_burst_threshold_24h(db: Session, default: int = 3) -> int:
    return _coerce_int(get_setting(db, MONITOR_ADAPTIVE_BURST_THRESHOLD_KEY, default), default, 2, 20)

def set_adaptive_burst_threshold_24h(db: Session, value: int, updated_by: int | None = None) -> bool:
    return set_setting(db, MONITOR_ADAPTIVE_BURST_THRESHOLD_KEY, _coerce_int(value, 3, 2, 20),
        description="Постов за 24ч для активации burst-режима", updated_by=updated_by)


def get_adaptive_burst_multiplier(db: Session, default: float = 0.5) -> float:
    return _coerce_float(get_setting(db, MONITOR_ADAPTIVE_BURST_MULTIPLIER_KEY, default), default, 0.1, 1.0)

def set_adaptive_burst_multiplier(db: Session, value: float, updated_by: int | None = None) -> bool:
    return set_setting(db, MONITOR_ADAPTIVE_BURST_MULTIPLIER_KEY, _coerce_float(value, 0.5, 0.1, 1.0),
        description="Множитель базового интервала в burst-режиме (< 1 = быстрее)", updated_by=updated_by)


def get_adaptive_burst_duration_hours(db: Session, default: float = 24.0) -> float:
    return _coerce_float(get_setting(db, MONITOR_ADAPTIVE_BURST_DURATION_KEY, default), default, 1.0, 168.0)

def set_adaptive_burst_duration_hours(db: Session, value: float, updated_by: int | None = None) -> bool:
    return set_setting(db, MONITOR_ADAPTIVE_BURST_DURATION_KEY, _coerce_float(value, 24.0, 1.0, 168.0),
        description="Длительность burst-режима после последнего поста (часы)", updated_by=updated_by)


def get_adaptive_silence_max_factor(db: Session, default: float = 2.0) -> float:
    return _coerce_float(get_setting(db, MONITOR_ADAPTIVE_SILENCE_MAX_FACTOR_KEY, default), default, 1.0, 10.0)

def set_adaptive_silence_max_factor(db: Session, value: float, updated_by: int | None = None) -> bool:
    return set_setting(db, MONITOR_ADAPTIVE_SILENCE_MAX_FACTOR_KEY, _coerce_float(value, 2.0, 1.0, 10.0),
        description="Максимальный множитель замедления в режиме тишины", updated_by=updated_by)


def get_adaptive_empty_streak_threshold(db: Session, default: int = 5) -> int:
    return _coerce_int(get_setting(db, MONITOR_ADAPTIVE_EMPTY_STREAK_THRESH_KEY, default), default, 1, 100)

def set_adaptive_empty_streak_threshold(db: Session, value: int, updated_by: int | None = None) -> bool:
    return set_setting(db, MONITOR_ADAPTIVE_EMPTY_STREAK_THRESH_KEY, _coerce_int(value, 5, 1, 100),
        description="Пустых проверок подряд до доп. замедления", updated_by=updated_by)


def get_adaptive_empty_streak_multiplier(db: Session, default: float = 1.3) -> float:
    return _coerce_float(get_setting(db, MONITOR_ADAPTIVE_EMPTY_STREAK_MULT_KEY, default), default, 1.0, 5.0)

def set_adaptive_empty_streak_multiplier(db: Session, value: float, updated_by: int | None = None) -> bool:
    return set_setting(db, MONITOR_ADAPTIVE_EMPTY_STREAK_MULT_KEY, _coerce_float(value, 1.3, 1.0, 5.0),
        description="Множитель при превышении порога пустых проверок", updated_by=updated_by)


def get_adaptive_gap_divisor(db: Session, default: float = 30.0) -> float:
    return _coerce_float(get_setting(db, MONITOR_ADAPTIVE_GAP_DIVISOR_KEY, default), default, 1.5, 100.0)

def set_adaptive_gap_divisor(db: Session, value: float, updated_by: int | None = None) -> bool:
    return set_setting(db, MONITOR_ADAPTIVE_GAP_DIVISOR_KEY, _coerce_float(value, 30.0, 1.5, 100.0),
        description="Делитель: base_interval = median_gap / этого числа", updated_by=updated_by)


def get_adaptive_use_p25_for_burst(db: Session, default: bool = True) -> bool:
    return _coerce_bool(get_setting(db, MONITOR_ADAPTIVE_USE_P25_BURST_KEY, default), default)

def set_adaptive_use_p25_for_burst(db: Session, value: bool, updated_by: int | None = None) -> bool:
    return set_setting(db, MONITOR_ADAPTIVE_USE_P25_BURST_KEY, bool(value),
        description="Использовать P25 вместо медианы для burst-блогеров", updated_by=updated_by)


def get_adaptive_hour_histogram_min_total(db: Session, default: int = 10) -> int:
    return _coerce_int(get_setting(db, MONITOR_ADAPTIVE_HOUR_MIN_TOTAL_KEY, default), default, 5, 100)

def set_adaptive_hour_histogram_min_total(db: Session, value: int, updated_by: int | None = None) -> bool:
    return set_setting(db, MONITOR_ADAPTIVE_HOUR_MIN_TOTAL_KEY, _coerce_int(value, 10, 5, 100),
        description="Мин. постов в гистограмме для включения часового весового коэффициента", updated_by=updated_by)


def get_adaptive_active_hour_threshold(db: Session, default: float = 0.5) -> float:
    return _coerce_float(get_setting(db, MONITOR_ADAPTIVE_ACTIVE_HOUR_THRESH_KEY, default), default, 0.1, 1.0)

def set_adaptive_active_hour_threshold(db: Session, value: float, updated_by: int | None = None) -> bool:
    return set_setting(db, MONITOR_ADAPTIVE_ACTIVE_HOUR_THRESH_KEY, _coerce_float(value, 0.5, 0.1, 1.0),
        description="Доля от пика для признания часа активным", updated_by=updated_by)


def get_adaptive_dead_hour_threshold(db: Session, default: float = 0.15) -> float:
    return _coerce_float(get_setting(db, MONITOR_ADAPTIVE_DEAD_HOUR_THRESH_KEY, default), default, 0.0, 0.5)

def set_adaptive_dead_hour_threshold(db: Session, value: float, updated_by: int | None = None) -> bool:
    return set_setting(db, MONITOR_ADAPTIVE_DEAD_HOUR_THRESH_KEY, _coerce_float(value, 0.15, 0.0, 0.5),
        description="Доля от пика для признания часа мёртвым (ночной множитель)", updated_by=updated_by)


def get_adaptive_active_hour_multiplier(db: Session, default: float = 0.7) -> float:
    return _coerce_float(get_setting(db, MONITOR_ADAPTIVE_ACTIVE_HOUR_MULT_KEY, default), default, 0.1, 1.0)

def set_adaptive_active_hour_multiplier(db: Session, value: float, updated_by: int | None = None) -> bool:
    return set_setting(db, MONITOR_ADAPTIVE_ACTIVE_HOUR_MULT_KEY, _coerce_float(value, 0.7, 0.1, 1.0),
        description="Множитель в активные часы блогера (< 1 = быстрее)", updated_by=updated_by)


def get_adaptive_max_keep(db: Session, default: int = 30) -> int:
    return _coerce_int(get_setting(db, MONITOR_ADAPTIVE_MAX_KEEP_KEY, default), default, 5, 200)

def set_adaptive_max_keep(db: Session, value: int, updated_by: int | None = None) -> bool:
    return set_setting(db, MONITOR_ADAPTIVE_MAX_KEEP_KEY, _coerce_int(value, 30, 5, 200),
        description="Размер скользящего окна timestamp'ов в posting_stats", updated_by=updated_by)


def get_adaptive_max_history_days(db: Session, default: int = 90) -> int:
    return _coerce_int(get_setting(db, MONITOR_ADAPTIVE_MAX_HISTORY_DAYS_KEY, default), default, 7, 365)

def set_adaptive_max_history_days(db: Session, value: int, updated_by: int | None = None) -> bool:
    return set_setting(db, MONITOR_ADAPTIVE_MAX_HISTORY_DAYS_KEY, _coerce_int(value, 90, 7, 365),
        description="Не учитывать timestamp'ы старше N дней", updated_by=updated_by)


def get_adaptive_check_log_retention_days(db: Session, default: int = 30) -> int:
    return _coerce_int(get_setting(db, MONITOR_ADAPTIVE_CHECK_LOG_RETENTION_KEY, default), default, 1, 180)

def set_adaptive_check_log_retention_days(db: Session, value: int, updated_by: int | None = None) -> bool:
    return set_setting(db, MONITOR_ADAPTIVE_CHECK_LOG_RETENTION_KEY, _coerce_int(value, 30, 1, 180),
        description="Дней хранения blogger_check_log", updated_by=updated_by)


def get_adaptive_burst_relative_factor(db: Session, default: float = 1.5) -> float:
    return _coerce_float(get_setting(db, MONITOR_ADAPTIVE_BURST_RELATIVE_KEY, default), default, 0.5, 10.0)

def set_adaptive_burst_relative_factor(db: Session, value: float, updated_by: int | None = None) -> bool:
    return set_setting(db, MONITOR_ADAPTIVE_BURST_RELATIVE_KEY, _coerce_float(value, 1.5, 0.5, 10.0),
        description="Burst: активен если постов/24ч >= ожидаемых × этот фактор", updated_by=updated_by)


def get_adaptive_prediction_min_confidence(db: Session, default: float = 0.4) -> float:
    return _coerce_float(get_setting(db, MONITOR_ADAPTIVE_PRED_MIN_CONF_KEY, default), default, 0.0, 1.0)

def set_adaptive_prediction_min_confidence(db: Session, value: float, updated_by: int | None = None) -> bool:
    return set_setting(db, MONITOR_ADAPTIVE_PRED_MIN_CONF_KEY, _coerce_float(value, 0.4, 0.0, 1.0),
        description="Минимальная уверенность предсказания для сдвига следующей проверки", updated_by=updated_by)


def get_adaptive_prediction_buffer_minutes(db: Session, default: int = 15) -> int:
    return _coerce_int(get_setting(db, MONITOR_ADAPTIVE_PRED_BUFFER_MIN_KEY, default), default, 1, 120)

def set_adaptive_prediction_buffer_minutes(db: Session, value: int, updated_by: int | None = None) -> bool:
    return set_setting(db, MONITOR_ADAPTIVE_PRED_BUFFER_MIN_KEY, _coerce_int(value, 15, 1, 120),
        description="Проверить за N минут до предсказанного поста", updated_by=updated_by)


def get_adaptive_prediction_high_confidence(db: Session, default: float = 0.65) -> float:
    return _coerce_float(get_setting(db, MONITOR_ADAPTIVE_PRED_HIGH_CONF_KEY, default), default, 0.0, 1.0)

def set_adaptive_prediction_high_confidence(db: Session, value: float, updated_by: int | None = None) -> bool:
    return set_setting(db, MONITOR_ADAPTIVE_PRED_HIGH_CONF_KEY, _coerce_float(value, 0.65, 0.0, 1.0),
        description="Высокая уверенность: позволить прогнозу также удлинять интервал", updated_by=updated_by)


def get_adaptive_recent_gap_weight(db: Session, default: float = 0.3) -> float:
    return _coerce_float(get_setting(db, MONITOR_ADAPTIVE_RECENT_GAP_WEIGHT_KEY, default), default, 0.0, 1.0)

def set_adaptive_recent_gap_weight(db: Session, value: float, updated_by: int | None = None) -> bool:
    return set_setting(db, MONITOR_ADAPTIVE_RECENT_GAP_WEIGHT_KEY, _coerce_float(value, 0.3, 0.0, 1.0),
        description="Вес медианы последних 5 интервалов vs полная история (0=игнор, 1=только свежие)", updated_by=updated_by)


# ── Self-learning feedback loop (latency-based per-blogger tuning) ────────────

def get_adaptive_feedback_enabled(db: Session, default: bool = True) -> bool:
    return _coerce_bool(get_setting(db, MONITOR_ADAPTIVE_FEEDBACK_ENABLED_KEY, default), default)

def set_adaptive_feedback_enabled(db: Session, value: bool, updated_by: int | None = None) -> bool:
    return set_setting(db, MONITOR_ADAPTIVE_FEEDBACK_ENABLED_KEY, bool(value),
        description="Само-подстройка интервала под фактическую задержку обнаружения", updated_by=updated_by)


def get_adaptive_feedback_target_latency(db: Session, default: int = 10) -> int:
    return _coerce_int(get_setting(db, MONITOR_ADAPTIVE_FEEDBACK_TARGET_KEY, default), default, 1, 240)

def set_adaptive_feedback_target_latency(db: Session, value: int, updated_by: int | None = None) -> bool:
    return set_setting(db, MONITOR_ADAPTIVE_FEEDBACK_TARGET_KEY, _coerce_int(value, 10, 1, 240),
        description="Целевая задержка обнаружения (минут). Подстройка стремится к этому значению", updated_by=updated_by)


def get_adaptive_feedback_ema_alpha(db: Session, default: float = 0.3) -> float:
    return _coerce_float(get_setting(db, MONITOR_ADAPTIVE_FEEDBACK_ALPHA_KEY, default), default, 0.05, 1.0)

def set_adaptive_feedback_ema_alpha(db: Session, value: float, updated_by: int | None = None) -> bool:
    return set_setting(db, MONITOR_ADAPTIVE_FEEDBACK_ALPHA_KEY, _coerce_float(value, 0.3, 0.05, 1.0),
        description="Коэффициент EMA для задержки (выше=быстрее реакция, ниже=плавнее)", updated_by=updated_by)


def get_adaptive_feedback_min_factor(db: Session, default: float = 0.5) -> float:
    return _coerce_float(get_setting(db, MONITOR_ADAPTIVE_FEEDBACK_MIN_FACTOR_KEY, default), default, 0.1, 1.0)

def set_adaptive_feedback_min_factor(db: Session, value: float, updated_by: int | None = None) -> bool:
    return set_setting(db, MONITOR_ADAPTIVE_FEEDBACK_MIN_FACTOR_KEY, _coerce_float(value, 0.5, 0.1, 1.0),
        description="Нижняя граница множителя самонастройки (защита от чрезмерного ускорения)", updated_by=updated_by)


def get_adaptive_feedback_max_factor(db: Session, default: float = 2.0) -> float:
    return _coerce_float(get_setting(db, MONITOR_ADAPTIVE_FEEDBACK_MAX_FACTOR_KEY, default), default, 1.0, 5.0)

def set_adaptive_feedback_max_factor(db: Session, value: float, updated_by: int | None = None) -> bool:
    return set_setting(db, MONITOR_ADAPTIVE_FEEDBACK_MAX_FACTOR_KEY, _coerce_float(value, 2.0, 1.0, 5.0),
        description="Верхняя граница множителя самонастройки (защита от чрезмерного замедления)", updated_by=updated_by)


def get_adaptive_feedback_late_threshold(db: Session, default: float = 2.0) -> float:
    return _coerce_float(get_setting(db, MONITOR_ADAPTIVE_FEEDBACK_LATE_THRESH_KEY, default), default, 1.0, 10.0)

def set_adaptive_feedback_late_threshold(db: Session, value: float, updated_by: int | None = None) -> bool:
    return set_setting(db, MONITOR_ADAPTIVE_FEEDBACK_LATE_THRESH_KEY, _coerce_float(value, 2.0, 1.0, 10.0),
        description="Опоздание = задержка > target × этого множителя (для счётчика опозданий)", updated_by=updated_by)


def get_all_adaptive_settings(db: Session) -> dict:
    """Возвращает все adaptive-настройки (все ~26 параметров) для API."""
    return {
        # Main switch
        "adaptive_enabled":              get_adaptive_enabled(db),
        # Boundaries
        "min_interval_minutes":          get_adaptive_min_interval(db),
        "max_interval_minutes":          get_adaptive_max_interval(db),
        "sanity_cap_factor":             get_adaptive_sanity_cap_factor(db),
        # Acceleration
        "active_phase_interval_minutes": get_adaptive_active_phase_interval(db),
        "active_phase_hours":            get_adaptive_active_phase_hours(db),
        "found_new_reset_factor":        get_adaptive_found_new_reset_factor(db),
        "burst_threshold_24h":           get_adaptive_burst_threshold_24h(db),
        "burst_multiplier":              get_adaptive_burst_multiplier(db),
        "burst_duration_hours":          get_adaptive_burst_duration_hours(db),
        # Deceleration
        "silence_multiplier":            get_adaptive_silence_multiplier(db),
        "silence_max_factor":            get_adaptive_silence_max_factor(db),
        "nighttime_multiplier":          get_adaptive_nighttime_multiplier(db),
        "empty_streak_threshold":        get_adaptive_empty_streak_threshold(db),
        "empty_streak_multiplier":       get_adaptive_empty_streak_multiplier(db),
        # Base calculation
        "gap_divisor":                   get_adaptive_gap_divisor(db),
        "use_p25_for_burst":             get_adaptive_use_p25_for_burst(db),
        "min_samples":                   get_adaptive_min_samples(db),
        # Hour-of-day
        "hour_histogram_min_total":      get_adaptive_hour_histogram_min_total(db),
        "active_hour_threshold":         get_adaptive_active_hour_threshold(db),
        "dead_hour_threshold":           get_adaptive_dead_hour_threshold(db),
        "active_hour_multiplier":        get_adaptive_active_hour_multiplier(db),
        # History
        "max_keep":                      get_adaptive_max_keep(db),
        "max_history_days":              get_adaptive_max_history_days(db),
        "initial_limit":                 get_adaptive_initial_limit(db),
        "check_log_retention_days":      get_adaptive_check_log_retention_days(db),
        # Burst (relative) + Prediction
        "burst_relative_factor":         get_adaptive_burst_relative_factor(db),
        "prediction_min_confidence":     get_adaptive_prediction_min_confidence(db),
        "prediction_buffer_minutes":     get_adaptive_prediction_buffer_minutes(db),
        "prediction_high_confidence":    get_adaptive_prediction_high_confidence(db),
        "recent_gap_weight":             get_adaptive_recent_gap_weight(db),
        # Self-learning feedback loop
        "feedback_enabled":              get_adaptive_feedback_enabled(db),
        "feedback_target_latency_min":   get_adaptive_feedback_target_latency(db),
        "feedback_ema_alpha":            get_adaptive_feedback_ema_alpha(db),
        "feedback_min_factor":           get_adaptive_feedback_min_factor(db),
        "feedback_max_factor":           get_adaptive_feedback_max_factor(db),
        "feedback_late_threshold":       get_adaptive_feedback_late_threshold(db),
    }
