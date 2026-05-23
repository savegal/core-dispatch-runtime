import asyncio
import logging
import sys
import os
from aiogram import Bot, Dispatcher
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.client.session.aiohttp import AiohttpSession
from bot.config import load_config
from bot.handlers import menu, composer, orders, admin, templates, quick_order, admin_resources, admin_geo, settings, video_parsing, video_inbox, engagement_wizard, personal_accounts
from bot.handlers import autopilot
from config.logging_config import setup_logging
from config.settings import get_settings
from config.sentry_config import init_sentry

# Настройка логирования (вызывается один раз)
# ИСПРАВЛЕНО: Используем JSON logging если включено в настройках
from config.settings import get_settings
settings_for_logging = get_settings()
setup_logging(level="INFO", use_json=settings_for_logging.USE_JSON_LOGGING)
logger = logging.getLogger("bot.app")

# Инициализируем Sentry для мониторинга ошибок
init_sentry()


async def check_redis_connection() -> bool:
    """Проверяет доступность Redis (асинхронно, не блокирует event loop)."""
    try:
        import redis.asyncio as aioredis
        from config.settings import get_settings
        
        settings = get_settings()
        
        # Увеличиваем таймауты для надежности (Memurai может быть медленнее при старте)
        redis_client = aioredis.from_url(
            settings.REDIS_URL,
            decode_responses=True,
            socket_connect_timeout=10,  # Увеличено до 10 секунд для Memurai
            socket_timeout=10,  # Увеличено до 10 секунд
            retry_on_timeout=True,  # Повтор при таймауте
            health_check_interval=30  # Проверка здоровья соединения
        )
        
        try:
            # Пробуем подключиться
            result = await asyncio.wait_for(redis_client.ping(), timeout=10.0)
            await redis_client.aclose()
            return result
        except asyncio.TimeoutError:
            logger.warning("Redis ping timeout, trying again...")
            await redis_client.aclose()
            # Повторная попытка с новым соединением
            redis_client = aioredis.from_url(
                settings.REDIS_URL,
                decode_responses=True,
                socket_connect_timeout=10,
                socket_timeout=10,
                retry_on_timeout=True
            )
            try:
                result = await asyncio.wait_for(redis_client.ping(), timeout=10.0)
                await redis_client.aclose()
                return result
            except Exception as retry_error:
                logger.error(f"Redis retry failed: {retry_error}")
                await redis_client.aclose()
                return False
            
    except (aioredis.ConnectionError, aioredis.TimeoutError, OSError, ConnectionRefusedError) as e:
        logger.error(f"Redis connection error: {e}")
        logger.error(f"Redis URL: {settings.REDIS_URL}")
        return False
    except Exception as e:
        logger.error(f"Unexpected error checking Redis: {e}", exc_info=True)
        return False


async def main():
    """Главная функция запуска бота."""
    config = load_config()

    bot_token = config.get("BOT_TOKEN")
    if not bot_token:
        logger.error("BOT_TOKEN not set in config")
        return

    # Проверка Redis (асинхронно)
    logger.info("Checking Redis connection...")
    if not await check_redis_connection():
        logger.error("=" * 50)
        logger.error("❗ Redis не запущен. Запусти Redis.")
        logger.error("=" * 50)
        logger.error("Инструкции по установке Redis см. в README.md или LAUNCH_GUIDE.md")
        logger.error("Для проверки: redis-cli ping (должно вернуть PONG)")
        sys.exit(1)
    
    logger.info("✅ Redis connection OK")

    # Настройка прокси для Telegram
    telegram_proxy = config.get("TELEGRAM_PROXY")
    
    # Если прокси не указан в .env, проверяем системные переменные
    if not telegram_proxy:
        telegram_proxy = os.environ.get("HTTP_PROXY") or os.environ.get("HTTPS_PROXY") or os.environ.get("http_proxy") or os.environ.get("https_proxy")
        if telegram_proxy:
            logger.info(f"Используется системный прокси для Telegram: {telegram_proxy.split('@')[-1] if '@' in telegram_proxy else telegram_proxy}")
    
    # Создаем AiohttpSession с прокси (если указан)
    # В aiogram 3.x AiohttpSession автоматически создает ClientSession с нужными настройками
    from aiogram.client.session.aiohttp import AiohttpSession
    
    # ИСПРАВЛЕНО: AiohttpSession принимает timeout как число (секунды) и прокси через параметр proxy
    # ИСПРАВЛЕНО: Таймаут вынесен в settings для возможности настройки
    from config.settings import get_settings
    app_settings = get_settings()  # ИСПРАВЛЕНО: переименовано, чтобы не переопределять модуль settings из bot.handlers
    timeout_seconds = getattr(app_settings, 'TELEGRAM_API_TIMEOUT_SECONDS', 120)  # Общий таймаут в секундах
    
    if telegram_proxy:
        logger.info(f"Используется прокси для Telegram: {telegram_proxy.split('@')[-1] if '@' in telegram_proxy else telegram_proxy}")
        # Передаём прокси и timeout как число
        session = AiohttpSession(proxy=telegram_proxy, timeout=timeout_seconds)
    else:
        logger.warning("⚠️  Прокси не настроен!")
        logger.warning("Если Telegram заблокирован, подключение НЕВОЗМОЖНО без прокси.")
        logger.warning("Добавьте TELEGRAM_PROXY в .env файл или настройте VPN.")
        # Передаём только timeout как число
        session = AiohttpSession(timeout=timeout_seconds)
    
    bot = Bot(token=bot_token, session=session)
    
    # ИСПРАВЛЕНО (Bugfix #2): Используем Redis storage с расширенным TTL
    # MemoryStorage теряет state при перезапуске или долгом бездействии
    try:
        from aiogram.fsm.storage.redis import RedisStorage
        from aiogram.fsm.storage.redis import RedisStorage
        import redis.asyncio as aioredis
        
        # ИСПРАВЛЕНО: Для Aiogram нужен асинхронный клиент (redis.asyncio)
        # get_redis_client() возвращает синхронный клиент, что вызывает ошибку TypeError
        # ИСПРАВЛЕНО: Используем app_settings чтобы не конфликтовать с импортированным модулем settings
        from config.settings import get_settings
        app_settings = get_settings()
        
        redis_client = aioredis.from_url(
            app_settings.REDIS_URL, 
            decode_responses=True,
            socket_connect_timeout=5,
            socket_timeout=30
        )
        storage = RedisStorage(
            redis=redis_client,
            state_ttl=3600,  # 1 час
            data_ttl=3600    # 1 час
        )
        dp = Dispatcher(storage=storage)
        logger.info(f"✅ Bot using RedisStorage (state_ttl=3600s)")
    except Exception as e:
        logger.warning(f"Redis не доступен, используется MemoryStorage (fallback): {e}")
        dp = Dispatcher(storage=MemoryStorage())


    # Регистрируем роутеры (важен порядок)
    from bot.handlers import order_wizard

    # Команды и команды меню ДОЛЖНЫ быть первыми (Command() фильтры должны обрабатываться раньше)
    dp.include_router(menu.router)      # Команды (/start и т.д.) - ПЕРВЫМ!

    # Затем специфичные callback обработчики
    # ИСПРАВЛЕНО: Применяем AdminMiddleware к админ-роутерам для централизованной проверки доступа
    # ВАЖНО: video_parsing доступен всем работникам, поэтому middleware НЕ применяем
    from bot.middleware.admin_middleware import AdminMiddleware
    admin_middleware = AdminMiddleware()
    admin.router.message.middleware(admin_middleware)
    admin.router.callback_query.middleware(admin_middleware)
    admin_resources.router.message.middleware(admin_middleware)
    admin_resources.router.callback_query.middleware(admin_middleware)
    admin_geo.router.message.middleware(admin_middleware)
    admin_geo.router.callback_query.middleware(admin_middleware)
    # video_parsing доступен всем - убрали admin_middleware

    # SoftwareModeMiddleware: в parsing-режиме блокирует коментинг-флоу.
    # Применяем точечно к роутерам, которые относятся ИСКЛЮЧИТЕЛЬНО к коментингу:
    # orders/templates/composer-визарды/настройки/личные аккаунты/quick-order.
    # На menu/video_parsing/video_inbox/admin* НЕ навешиваем — иначе в parsing-режиме
    # пользователь не сможет ни вернуться в главное меню, ни попасть в админку.
    from bot.middleware.software_mode_middleware import SoftwareModeMiddleware
    # Админ/owner всегда остаётся в full-режиме; parsing ограничивает только
    # обычных пользователей.
    comment_mode_guard = SoftwareModeMiddleware(
        allow_video_monitoring=False,
        respect_admin_bypass=True,
    )

    dp.include_router(admin.router)     # Админка
    dp.include_router(admin_resources.router)  # Обработчики ресурсов и прокси
    dp.include_router(admin_geo.router)  # Гео-аналитика и управление
    dp.include_router(autopilot.router)  # Автокомментинг
    dp.include_router(video_parsing.router)  # Парсинг видео
    dp.include_router(video_inbox.router)  # УЛУЧШЕНО: Inbox для новых видео

    for commenting_router in (
        orders.router,
        templates.router,
        order_wizard.router,
        engagement_wizard.router,
        personal_accounts.router,
        settings.router,
        quick_order.router,
    ):
        commenting_router.message.middleware(comment_mode_guard)
        commenting_router.callback_query.middleware(comment_mode_guard)

    dp.include_router(orders.router)    # Заказы
    dp.include_router(templates.router) # Шаблоны
    dp.include_router(order_wizard.router)  # Новый мастер создания заказа (menu:new_order здесь)
    dp.include_router(engagement_wizard.router)  # HYBRID: Engagement only wizard (menu:engagement_only)
    dp.include_router(personal_accounts.router)  # Личные аккаунты работника (menu: pa:menu)
    dp.include_router(settings.router)  # Настройки
    # Старый композер отключён, чтобы избежать некорректных заказов без вариаций текста
    dp.include_router(quick_order.router)  # Обработчик ссылок - последним (перехватывает все текстовые сообщения)

    logger.info("Bot starting...")
    try:
        # ИСПРАВЛЕНО: Добавляем retry для подключения к Telegram API
        max_retries = 3
        retry_delay = 5  # секунд
        
        for attempt in range(max_retries):
            try:
                await dp.start_polling(bot)
                break  # Успешно подключились
            except Exception as polling_error:
                error_msg = str(polling_error).lower()
                if "timeout" in error_msg or "connection" in error_msg or "telegram" in error_msg:
                    if attempt < max_retries - 1:
                        logger.warning(f"Попытка подключения {attempt + 1}/{max_retries} не удалась. Повтор через {retry_delay} секунд...")
                        await asyncio.sleep(retry_delay)
                        retry_delay *= 2  # Увеличиваем задержку с каждой попыткой
                        continue
                raise  # Если не timeout/connection ошибка - пробрасываем дальше
    except Exception as e:
        error_msg = str(e)
        logger.error(f"Bot error: {error_msg}")
        
        # Проверяем тип ошибки
        if "Cannot connect to host" in error_msg or "api.telegram.org" in error_msg:
            logger.error("=" * 60)
            logger.error("❌ ОШИБКА ПОДКЛЮЧЕНИЯ К TELEGRAM API")
            logger.error("=" * 60)
            logger.error("Возможные причины:")
            logger.error("1. Нет подключения к интернету")
            logger.error("2. Telegram API заблокирован (проверьте VPN/прокси)")
            logger.error("3. Проблемы с файрволом/антивирусом")
            logger.error("4. Проблемы с SSL сертификатами")
            logger.error("=" * 60)
            logger.error("РЕШЕНИЕ:")
            logger.error("")
            logger.error("1. Настройте прокси (РЕКОМЕНДУЕТСЯ):")
            logger.error("   Добавьте в .env файл:")
            logger.error("   TELEGRAM_PROXY=http://user:pass@proxy:8080")
            logger.error("   или")
            logger.error("   TELEGRAM_PROXY=socks5://user:pass@proxy:1080")
            logger.error("")
            logger.error("2. Или используйте системные переменные окружения:")
            logger.error("   set HTTP_PROXY=http://user:pass@proxy:8080")
            logger.error("   (в PowerShell: $env:HTTP_PROXY='...')")
            logger.error("")
            logger.error("3. Проверьте интернет-соединение")
            logger.error("4. Проверьте настройки файрвола/антивируса")
            logger.error("")
            logger.error("Подробности: см. TELEGRAM_CONNECTION_ANALYSIS.md")
            logger.error("=" * 60)
        else:
            logger.error(f"Неизвестная ошибка: {error_msg}")
        raise


if __name__ == "__main__":
    asyncio.run(main())

