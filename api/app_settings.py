"""
API endpoints для управления настройками приложения через админ панель.
"""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from api.auth import verify_admin_header
from services.app_settings_service import (
    get_proxy_type_filter,
    set_proxy_type_filter,
    get_proxy_prefer_mobile,
    set_proxy_prefer_mobile,
    get_boost_likes_enabled,
    set_boost_likes_enabled,
    get_boost_likes_quantity,
    set_boost_likes_quantity,
    get_software_mode,
    set_software_mode,
    get_commenting_mode,
    set_commenting_mode,
    get_monitor_max_parallel_browsers,
    set_monitor_max_parallel_browsers,
    get_user_schema_enabled,
    set_user_schema_enabled,
    SOFTWARE_MODE_FULL,
    SOFTWARE_MODE_PARSING,
    SOFTWARE_MODES,
    COMMENTING_MODE_OLD,
    COMMENTING_MODE_PARALLEL,
    COMMENTING_MODES,
    MONITOR_MAX_PARALLEL_MIN,
    MONITOR_MAX_PARALLEL_MAX,
)
from services.likes_config import (
    BOOST_LIKES_DEFAULT,
    BOOST_LIKES_MAX,
    BOOST_LIKES_MIN,
    coerce_likes_quantity,
    provider_available as likes_provider_available,
)
from db.database import get_sync_db_context
from config.settings import get_settings
import logging

logger = logging.getLogger("api.app_settings")
router = APIRouter(prefix="/api/settings", tags=["settings"])
app_cfg = get_settings()


class ProxySettingsResponse(BaseModel):
    proxy_type_filter: str | None
    proxy_prefer_mobile: bool


class ProxySettingsUpdate(BaseModel):
    proxy_type_filter: str | None = None
    proxy_prefer_mobile: bool | None = None


class BoostLikesSettingsResponse(BaseModel):
    boost_likes_enabled: bool
    boost_likes_quantity: int
    boost_provider_available: bool


class BoostLikesSettingsUpdate(BaseModel):
    boost_likes_enabled: bool | None = None
    boost_likes_quantity: int | None = Field(
        default=None,
        ge=BOOST_LIKES_MIN,
        le=BOOST_LIKES_MAX,
    )


class SoftwareModeSettingsResponse(BaseModel):
    software_mode: str
    is_parsing_mode: bool


class SoftwareModeSettingsUpdate(BaseModel):
    software_mode: str


class CommentingModeSettingsResponse(BaseModel):
    commenting_mode: str
    is_parallel_mode: bool


class CommentingModeSettingsUpdate(BaseModel):
    commenting_mode: str


class MonitorParallelBrowsersResponse(BaseModel):
    monitor_max_parallel_browsers: int
    allowed_min: int = MONITOR_MAX_PARALLEL_MIN
    allowed_max: int = MONITOR_MAX_PARALLEL_MAX
    env_default: int


class MonitorParallelBrowsersUpdate(BaseModel):
    monitor_max_parallel_browsers: int = Field(
        ge=MONITOR_MAX_PARALLEL_MIN,
        le=MONITOR_MAX_PARALLEL_MAX,
    )


@router.get("/proxy", response_model=ProxySettingsResponse)
async def get_proxy_settings(
    _: None = Depends(verify_admin_header)
):
    """Получает настройки фильтрации прокси."""
    try:
        # ИСПРАВЛЕНО: Используем context manager для безопасной работы с sync сессией
        with get_sync_db_context() as sync_db:
            proxy_type_filter = get_proxy_type_filter(sync_db)
            proxy_prefer_mobile = get_proxy_prefer_mobile(sync_db)
            
            return ProxySettingsResponse(
                proxy_type_filter=proxy_type_filter,
                proxy_prefer_mobile=proxy_prefer_mobile
            )
    except Exception as e:
        logger.error(f"Error getting proxy settings: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Error getting proxy settings: {str(e)[:200]}")


@router.post("/proxy")
async def update_proxy_settings(
    settings: ProxySettingsUpdate,
    _: None = Depends(verify_admin_header)
):
    """Обновляет настройки фильтрации прокси."""
    try:
        # ИСПРАВЛЕНО: Используем context manager для безопасной работы с sync сессией
        with get_sync_db_context() as sync_db:
            updated = False
            
            fields_set = getattr(settings, "model_fields_set", None)
            if fields_set is None:
                fields_set = getattr(settings, "__fields_set__", set())

            if "proxy_type_filter" in fields_set:
                # Валидация значения
                proxy_type_filter = settings.proxy_type_filter
                if isinstance(proxy_type_filter, str) and proxy_type_filter.lower() in {"", "none", "all"}:
                    proxy_type_filter = None
                valid_values = [None, "mobile", "residential"]
                if proxy_type_filter not in valid_values:
                    raise HTTPException(
                        status_code=400,
                        detail=f"Invalid proxy_type_filter. Must be one of: {valid_values}"
                    )
                if set_proxy_type_filter(sync_db, proxy_type_filter):
                    updated = True
            
            if settings.proxy_prefer_mobile is not None:
                if set_proxy_prefer_mobile(sync_db, settings.proxy_prefer_mobile):
                    updated = True
            
            if not updated:
                return {"message": "No changes made"}
            
            # Возвращаем обновленные настройки
            return ProxySettingsResponse(
                proxy_type_filter=get_proxy_type_filter(sync_db),
                proxy_prefer_mobile=get_proxy_prefer_mobile(sync_db)
            )
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error updating proxy settings: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Error updating proxy settings: {str(e)[:200]}")


@router.get("/boost_likes", response_model=BoostLikesSettingsResponse)
async def get_boost_likes_settings(
    _: None = Depends(verify_admin_header)
):
    """Возвращает настройки лайков на комментарии."""
    try:
        with get_sync_db_context() as sync_db:
            provider_available = likes_provider_available(app_cfg.BOOST_PROJECT_API_KEY)
            default_enabled = provider_available
            default_quantity = coerce_likes_quantity(
                app_cfg.BOOST_PROJECT_LIKES_QUANTITY,
                BOOST_LIKES_DEFAULT,
            )

            stored_enabled = get_boost_likes_enabled(sync_db, default=default_enabled)
            return BoostLikesSettingsResponse(
                boost_likes_enabled=provider_available and stored_enabled,
                boost_likes_quantity=get_boost_likes_quantity(sync_db, default=default_quantity),
                boost_provider_available=provider_available,
            )
    except Exception as e:
        logger.error(f"Error getting boost likes settings: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Error getting boost likes settings: {str(e)[:200]}")


@router.post("/boost_likes", response_model=BoostLikesSettingsResponse)
async def update_boost_likes_settings(
    settings: BoostLikesSettingsUpdate,
    _: None = Depends(verify_admin_header)
):
    """Обновляет настройки лайков на комментарии."""
    try:
        with get_sync_db_context() as sync_db:
            provider_available = likes_provider_available(app_cfg.BOOST_PROJECT_API_KEY)
            default_enabled = provider_available
            default_quantity = coerce_likes_quantity(
                app_cfg.BOOST_PROJECT_LIKES_QUANTITY,
                BOOST_LIKES_DEFAULT,
            )

            if settings.boost_likes_enabled is not None:
                set_boost_likes_enabled(sync_db, settings.boost_likes_enabled)

            if settings.boost_likes_quantity is not None:
                set_boost_likes_quantity(sync_db, settings.boost_likes_quantity)

            stored_enabled = get_boost_likes_enabled(sync_db, default=default_enabled)
            return BoostLikesSettingsResponse(
                boost_likes_enabled=provider_available and stored_enabled,
                boost_likes_quantity=get_boost_likes_quantity(sync_db, default=default_quantity),
                boost_provider_available=provider_available,
            )
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error updating boost likes settings: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Error updating boost likes settings: {str(e)[:200]}")


@router.get("/software_mode", response_model=SoftwareModeSettingsResponse)
async def get_software_mode_settings(
    _: None = Depends(verify_admin_header)
):
    """Returns the current global software mode."""
    try:
        with get_sync_db_context() as sync_db:
            mode = get_software_mode(sync_db, default=SOFTWARE_MODE_FULL)
            return SoftwareModeSettingsResponse(
                software_mode=mode,
                is_parsing_mode=mode == SOFTWARE_MODE_PARSING,
            )
    except Exception as e:
        logger.error(f"Error getting software mode settings: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Error getting software mode settings: {str(e)[:200]}")


@router.post("/software_mode", response_model=SoftwareModeSettingsResponse)
async def update_software_mode_settings(
    settings: SoftwareModeSettingsUpdate,
    _: None = Depends(verify_admin_header)
):
    """Updates the global software mode."""
    try:
        mode = (settings.software_mode or "").strip().lower()
        if mode not in SOFTWARE_MODES:
            raise HTTPException(
                status_code=400,
                detail=f"Invalid software_mode. Must be one of: {[SOFTWARE_MODE_FULL, SOFTWARE_MODE_PARSING]}",
            )

        with get_sync_db_context() as sync_db:
            set_software_mode(sync_db, mode)
            updated_mode = get_software_mode(sync_db, default=SOFTWARE_MODE_FULL)
            return SoftwareModeSettingsResponse(
                software_mode=updated_mode,
                is_parsing_mode=updated_mode == SOFTWARE_MODE_PARSING,
            )
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error updating software mode settings: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Error updating software mode settings: {str(e)[:200]}")


@router.get("/commenting_mode", response_model=CommentingModeSettingsResponse)
async def get_commenting_mode_settings(
    _: None = Depends(verify_admin_header)
):
    """Returns the current commenting workflow mode."""
    try:
        with get_sync_db_context() as sync_db:
            mode = get_commenting_mode(sync_db, default=COMMENTING_MODE_OLD)
            return CommentingModeSettingsResponse(
                commenting_mode=mode,
                is_parallel_mode=mode == COMMENTING_MODE_PARALLEL,
            )
    except Exception as e:
        logger.error(f"Error getting commenting mode settings: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Error getting commenting mode settings: {str(e)[:200]}")


@router.post("/commenting_mode", response_model=CommentingModeSettingsResponse)
async def update_commenting_mode_settings(
    settings: CommentingModeSettingsUpdate,
    _: None = Depends(verify_admin_header)
):
    """Updates the commenting workflow mode."""
    try:
        mode = (settings.commenting_mode or "").strip().lower()
        if mode not in COMMENTING_MODES:
            raise HTTPException(
                status_code=400,
                detail=f"Invalid commenting_mode. Must be one of: {[COMMENTING_MODE_OLD, COMMENTING_MODE_PARALLEL]}",
            )

        with get_sync_db_context() as sync_db:
            set_commenting_mode(sync_db, mode)
            updated_mode = get_commenting_mode(sync_db, default=COMMENTING_MODE_OLD)
            return CommentingModeSettingsResponse(
                commenting_mode=updated_mode,
                is_parallel_mode=updated_mode == COMMENTING_MODE_PARALLEL,
            )
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error updating commenting mode settings: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Error updating commenting mode settings: {str(e)[:200]}")


@router.get("/monitor_parallel_browsers", response_model=MonitorParallelBrowsersResponse)
async def get_monitor_parallel_browsers_settings(
    _: None = Depends(verify_admin_header),
):
    """Сколько Playwright-профилей video_monitor_worker может открыть одновременно (кэш настроек ~60 с)."""
    try:
        with get_sync_db_context() as sync_db:
            v = get_monitor_max_parallel_browsers(sync_db)
            return MonitorParallelBrowsersResponse(
                monitor_max_parallel_browsers=v,
                env_default=int(app_cfg.MONITOR_MAX_PARALLEL_BROWSERS),
            )
    except Exception as e:
        logger.error(f"Error getting monitor parallel browsers: {e}", exc_info=True)
        raise HTTPException(
            status_code=500,
            detail=f"Error getting monitor parallel browsers: {str(e)[:200]}",
        )


@router.post("/monitor_parallel_browsers", response_model=MonitorParallelBrowsersResponse)
async def update_monitor_parallel_browsers_settings(
    body: MonitorParallelBrowsersUpdate,
    _: None = Depends(verify_admin_header),
):
    try:
        with get_sync_db_context() as sync_db:
            if not set_monitor_max_parallel_browsers(sync_db, body.monitor_max_parallel_browsers):
                raise HTTPException(
                    status_code=500,
                    detail="Failed to persist monitor_max_parallel_browsers (database error)",
                )
            v = get_monitor_max_parallel_browsers(sync_db)
            return MonitorParallelBrowsersResponse(
                monitor_max_parallel_browsers=v,
                env_default=int(app_cfg.MONITOR_MAX_PARALLEL_BROWSERS),
            )
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error updating monitor parallel browsers: {e}", exc_info=True)
        raise HTTPException(
            status_code=500,
            detail=f"Error updating monitor parallel browsers: {str(e)[:200]}",
        )


# ── User-schema workflow toggle ───────────────────────────────────────────────

class UserSchemaSettingsResponse(BaseModel):
    user_schema_enabled: bool
    env_default: bool


class UserSchemaSettingsUpdate(BaseModel):
    user_schema_enabled: bool


@router.get("/user_schema", response_model=UserSchemaSettingsResponse)
async def get_user_schema_settings(
    _: None = Depends(verify_admin_header),
):
    """Текущее состояние нового workflow (user-schema)."""
    try:
        env_default = bool(getattr(app_cfg, "USER_SCHEMA_ENABLED", True))
        with get_sync_db_context() as sync_db:
            return UserSchemaSettingsResponse(
                user_schema_enabled=get_user_schema_enabled(sync_db, default=env_default),
                env_default=env_default,
            )
    except Exception as e:
        logger.error(f"Error getting user_schema settings: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Error getting user_schema settings: {str(e)[:200]}")


@router.post("/user_schema", response_model=UserSchemaSettingsResponse)
async def update_user_schema_settings(
    body: UserSchemaSettingsUpdate,
    _: None = Depends(verify_admin_header),
):
    """Включить / выключить новый workflow из админ-панели.

    Принимает { "user_schema_enabled": true|false } и сохраняет в app_settings.
    Изменение видно executor'у в течение ~60 секунд (TTL кеша настроек) без
    перезапуска воркеров.
    """
    try:
        env_default = bool(getattr(app_cfg, "USER_SCHEMA_ENABLED", True))
        with get_sync_db_context() as sync_db:
            if not set_user_schema_enabled(sync_db, body.user_schema_enabled):
                raise HTTPException(
                    status_code=500,
                    detail="Failed to persist user_schema_enabled (database error)",
                )
            return UserSchemaSettingsResponse(
                user_schema_enabled=get_user_schema_enabled(sync_db, default=env_default),
                env_default=env_default,
            )
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error updating user_schema settings: {e}", exc_info=True)
        raise HTTPException(
            status_code=500,
            detail=f"Error updating user_schema settings: {str(e)[:200]}",
        )
