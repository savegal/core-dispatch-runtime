from config.settings import get_settings


def load_config():
    """Загружает конфигурацию для бота."""
    settings = get_settings()
    try:
        admin_api_token = settings.ADMIN_API_TOKEN
    except AttributeError:
        admin_api_token = None
    return {
        "BOT_TOKEN": settings.BOT_TOKEN,
        "API_BASE": settings.API_BASE or "http://localhost:8000",
        "API_JWT": settings.API_JWT,
        "OWNER_TELEGRAM_ID": settings.OWNER_TELEGRAM_ID,
        "ADMIN_API_TOKEN": admin_api_token,
        "EXECUTOR_API_TOKEN": settings.EXECUTOR_API_TOKEN,
        "DEMO_MODE": settings.DEMO_MODE,
        "TELEGRAM_PROXY": settings.TELEGRAM_PROXY
    }
