from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import field_validator, ValidationError, Field
from functools import lru_cache
import os


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        extra="ignore",
        env_file=".env",
        env_file_encoding="utf-8"
    )

    # ИСПРАВЛЕНО: Production настройки по умолчанию
    ENV: str = "production"  # По умолчанию production, можно переопределить через .env
    DEBUG: bool = False  # По умолчанию False для безопасности

    # База данных
    DB_URL: str = "sqlite:///./dev.db"

    # Redis
    REDIS_URL: str = "redis://localhost:6379/0"
    REDIS_TASK_QUEUE: str = "queue:tasks"

    # Ограничения
    MAX_ACTIONS_PER_ACCOUNT: int = 999999  # ИСПРАВЛЕНО: Убран лимит на комментирование (было 50)
    MAX_THREADS: int = 2  # КРИТИЧНО: Снижено до 2 для работы на ноутбуке
    MAX_CONCURRENT_BROWSERS: int = 1  # КРИТИЧНО: Снижено до 1 для работы на ноутбуке (минимум нагрузки)
    # NEW: Лимит задач на один прокси (1-2 для мобильных, 2-3 для серверных)
    # 1 - самый безопасный режим (стабильная ротация, макс. скорость)
    MAX_TASKS_PER_PROXY: int = 1
    # Рекомендации по MAX_CONCURRENT_BROWSERS:
    # - 4GB RAM: MAX_CONCURRENT_BROWSERS = 4-5
    # - 8GB RAM: MAX_CONCURRENT_BROWSERS = 8-10
    # - 16GB RAM: MAX_CONCURRENT_BROWSERS = 15-20
    # - 32GB RAM: MAX_CONCURRENT_BROWSERS = 30-40
    RANDOM_DELAY: bool = True
    ACCOUNT_COOLDOWN_SECONDS: int = 60  # Для ноутбука: 60. Для сервера (стабильные аккаунты): 30-20
    # Prefer real browser auth (sessionid/sid_tt), but allow sid_guard-only
    # accounts as a last resort. They still must pass browser action-ready and
    # enrichment gates before any DOM publish/search is attempted.
    ALLOW_SID_GUARD_ONLY_PUBLISH_LAST_RESORT: bool = True
    # Жизненный цикл по дате создания записи в БД (created_at):
    # True — режим «сжигаемых» аккаунтов: старше ~72ч без свежей валидации → hard_expired, аккаунт
    #   исключается из main/reply/monitor (как на ферме одноразовых логинов).
    # False (по умолчанию) — возраст строки в БД не отключает аккаунт; подходит для долгоживущих
    #   импортированных cookie-pool (иначе через несколько дней все аккаунты уходят в retire).
    ACCOUNT_SHORT_LIVED_LIFECYCLE_BY_CREATED_AT: bool = False

    # Дефолтные лимиты, которые проставляются на каждый аккаунт при импорте в личный пул работника.
    # Работник может перезаписать их в карточке аккаунта или через раздел "Лимиты".
    # Для общего пула эти значения НЕ применяются — там по-прежнему действует MAX_ACTIONS_PER_ACCOUNT.
    PERSONAL_POOL_DEFAULT_DAILY_MAIN_LIMIT: int = 5
    PERSONAL_POOL_DEFAULT_DAILY_REPLY_LIMIT: int = 10
    # Жёсткие границы, которые работник не сможет превысить из бота — защита от случайного
    # выставления абсурдных значений вроде 9999.
    PERSONAL_POOL_MAX_DAILY_MAIN_LIMIT: int = 50
    PERSONAL_POOL_MAX_DAILY_REPLY_LIMIT: int = 100
    # Сколько личных аккаунтов один работник может держать одновременно.
    PERSONAL_POOL_MAX_ACCOUNTS_PER_USER: int = 200

    # ОПТИМИЗИРОВАНО: Настройки для ускорения процесса (сбалансированы скорость и надежность)
    BRANCH_VERIFY_DELAY_MIN: int = 3  # Для скорости: 2 сек. Текущее: 3 сек (баланс)
    BRANCH_VERIFY_DELAY_MAX: int = 8  # Для скорости: 5 сек. Текущее: 8 сек (баланс)
    BRANCH_DELAY_BASE: int = 30  # ОПТИМИЗИРОВАНО: Уменьшена базовая задержка между ветками (было 45)
    BRANCH_DELAY_MIN: int = 15  # ОПТИМИЗИРОВАНО: Уменьшена минимальная задержка между ветками (было 20)
    BRANCH_DELAY_VERIFIED_MIN: int = 10  # Минимальная задержка между ветками если первая verified
    BRANCH_DELAY_VERIFIED_MAX: int = 20  # Максимальная задержка между ветками если первая verified
    BRANCH_DELAY_NOT_VERIFIED_MIN: int = 20  # Минимальная задержка между ветками если первая не verified
    BRANCH_DELAY_NOT_VERIFIED_MAX: int = 40  # Максимальная задержка между ветками если первая не verified
    REQUIRED_SUCCESSFUL_BRANCHES: int = 2  # Требуется успешных веток на URL
    MAX_ACCOUNTS_PER_URL: int = 10  # Максимум аккаунтов на одну ссылку
    
    # Защита от DoS через API (видео-парсинг)
    MAX_CREATORS_PER_USER: int = 500  # Максимум активных блогеров на одного пользователя (кроме админа)
    MAX_UNIQUE_CREATORS_TOTAL: int = 2000  # Жёсткий потолок уникальных блогеров на весь сервис
    MAX_FAILED_IN_ROW: int = 5  # Максимум неудач подряд перед остановкой
    TASK_PROCESSING_TIMEOUT: int = 1800  # Таймаут обработки задачи (секунды, 30 минут)
    TASK_TIMEOUT_COOLDOWN_SECONDS: int = 60  # Пауза после принудительной остановки по таймауту
    STUCK_TASKS_RECOVERY_INTERVAL: int = 300  # Интервал восстановления зависших задач (секунды, 5 минут)
    
    # Лимиты автокомментирования (autopilot режим)
    AUTOPILOT_MAX_VIDEOS_PER_TICK: int = 10  # Максимум видео за один цикл проверки блогера
    AUTOPILOT_MAX_TASKS_PER_HOUR: int = 30   # Максимум задач в час на одного блогера (защита от спама)

    # Video monitor worker — настроены под ноутбук (мало RAM, нет сервера).
    # Потолок параллельных проверок блогеров. Воркер дополнительно ограничивает реальную
    # параллельность количеством рабочих прокси (mobile + residential) — нет смысла запускать
    # 2 воркера на 1 мобильный прокси (один из них будет постоянно ждать lock).
    MONITOR_BLOGGER_CONCURRENCY: int = 2
    # Если выключить — concurrency будет ровно MONITOR_BLOGGER_CONCURRENCY независимо от прокси.
    MONITOR_ADAPTIVE_CONCURRENCY_BY_PROXIES: bool = True
    MONITOR_MAX_PARALLEL_BROWSERS: int = 2  # макс. одновременных браузеров (каждый ~300MB RAM)
    MONITOR_MAX_BLOGGERS_PER_TICK: int = 10  # сколько due-блогеров брать в один цикл воркера
    MONITOR_THROTTLE_WHILE_COMMENTING: bool = True
    MONITOR_COMMENTING_ACTIVE_CONCURRENCY: int = 1
    MONITOR_COMMENTING_ACTIVE_MAX_BLOGGERS_PER_TICK: int = 1
    MONITOR_COMMENTING_ACTIVE_BALANCED_CONCURRENCY: int = 1
    MONITOR_COMMENTING_ACTIVE_BALANCED_MAX_BLOGGERS_PER_TICK: int = 2
    MONITOR_COMMENTING_ACTIVE_OUTBOX_BATCH_LIMIT: int = 2
    MONITOR_COMMENTING_ACTIVE_DEFER_AUTOPILOT_OUTBOX: bool = True
    MONITOR_COMMENTING_ACTIVE_PAUSE_ON_HIGH_LOAD: bool = True
    MONITOR_COMMENTING_ACTIVE_LIGHT_CPU_PERCENT: float = 70.0
    MONITOR_COMMENTING_ACTIVE_LIGHT_MEMORY_PERCENT: float = 75.0
    MONITOR_COMMENTING_ACTIVE_PAUSE_CPU_PERCENT: float = 85.0
    MONITOR_COMMENTING_ACTIVE_PAUSE_MEMORY_PERCENT: float = 85.0
    MONITOR_COMMENTING_ACTIVE_PAUSE_OUTBOX_BATCH_LIMIT: int = 1
    MONITOR_COMMENTING_ACTIVE_PAUSE_SLEEP_SECONDS: int = 90
    MONITOR_BACKLOG_SLEEP_SECONDS: int = 5  # пауза между циклами (ноут успевает остыть)
    SHARED_BLOGGER_MIN_SUBS: int = 2  # с какого числа подписок блогер считается shared
    SHARED_BLOGGER_MIN_INTERVAL_MINUTES: int = 15  # нижняя граница ручного интервала для shared-блогеров
    MONITOR_ACCOUNT_LOW_THRESHOLD: int = 8
    MONITOR_ACCOUNT_CRITICAL_THRESHOLD: int = 3
    MONITOR_ACCOUNT_MIN_ELIGIBLE_PER_DUE: float = 2.0
    MONITOR_ACCOUNT_MIN_ELIGIBLE_RATIO: float = 0.35
    MONITOR_DEAD_SESSION_ALERT_THRESHOLD: int = 5
    MONITOR_DEAD_SESSION_ALERT_RATIO: float = 0.25
    MONITOR_OK_WINDOW_MIN_RATIO: float = 0.35
    MONITOR_BACKLOG_ATTENTION_OVERDUE_MINUTES: int = 30
    MONITOR_ACCOUNT_ALERT_COOLDOWN_MINUTES: int = 120
    MONITOR_NO_ACCOUNTS_ALERT_COOLDOWN_MINUTES: int = 30
    MONITOR_TIMEOUT_WINDOW_MINUTES: int = 15
    MONITOR_TIMEOUT_ALERT_THRESHOLD: int = 2
    MONITOR_TIMEOUT_WINDOW_ALERT_THRESHOLD: int = 3
    MONITOR_NAVIGATION_TIMEOUT_WINDOW_MINUTES: int = 5
    MONITOR_NAVIGATION_TIMEOUT_STORM_THRESHOLD: int = 4
    MONITOR_NAVIGATION_TIMEOUT_DEFER_MINUTES: int = 5
    MONITOR_NAVIGATION_TIMEOUT_STORM_DEFER_MINUTES: int = 5
    MONITOR_BROWSER_INFRA_STORM_WINDOW_MINUTES: int = 5
    MONITOR_BROWSER_INFRA_STORM_THRESHOLD: int = 3
    MONITOR_BROWSER_INFRA_RESET_COOLDOWN_MINUTES: int = 10
    GLOBAL_NETWORK_PROBE_HOSTS: str = "1.1.1.1:443,8.8.8.8:53"
    GLOBAL_NETWORK_PROBE_CACHE_SECONDS: int = 60
    GLOBAL_NETWORK_PROBE_TIMEOUT_SECONDS: float = 3.0
    
    # Задержки для обработки URL
    URL_PROCESSING_DELAY_MIN: float = 0.5  # Минимальный множитель для задержки между URL
    URL_PROCESSING_DELAY_MAX: float = 1.5  # Максимальный множитель для задержки между URL

    # Compatibility defaults for settings referenced across API/bot/worker code.
    ADMIN_IDS: str = ""
    BOT_RATE_LIMIT_MESSAGES_PER_MINUTE: int = 30
    BOT_RATE_LIMIT_CALLBACKS_PER_MINUTE: int = 60
    BOT_RATE_LIMIT_BLOCK_DURATION_MINUTES: int = 5
    TELEGRAM_API_TIMEOUT_SECONDS: int = 120
    REQUIRE_WORKER_APPROVAL: bool = True
    DEFAULT_PROXY_SCHEME: str = "socks5"
    PROXY_SCHEME_DETECT_TIMEOUT: float = 1.5
    PROXY_BAD_TTL_SECONDS: int = 120
    PROXY_BAD_TTL_MAX_SECONDS: int = 1800
    PROXY_BAD_TTL_WINDOW_SECONDS: int = 3600
    MONITOR_OUTBOX_BATCH_LIMIT: int = 20
    MONITOR_IDLE_SLEEP_SECONDS: int = 180
    MONITOR_HEALTH_WINDOW_MINUTES: int = 15
    MONITOR_HEALTH_LOG_INTERVAL_SECONDS: int = 300
    MONITOR_INCIDENT_TTL_MINUTES: int = 60
    MONITOR_ACCOUNT_CANDIDATE_LIMIT: int = 250
    MONITOR_PROXY_FAIL_OPEN_MAX_UNIQUE_PROXIES: int = 8
    MONITOR_MARK_PROXY_BAD_ON_TIMEOUT: bool = False
    MONITOR_MARK_PROXY_BAD_ON_RATE_LIMIT: bool = False
    MONITOR_MARK_MOBILE_PROXY_BAD_ON_NETWORK: bool = False
    MONITOR_MOBILE_PROXY_HARD_FAILURE_BAD_THRESHOLD: int = 3
    MONITOR_MOBILE_PROXY_HARD_FAILURE_WINDOW_SECONDS: int = 600
    MONITOR_API_SUSPICIOUS_FALLBACK_ENABLED: bool = True
    MONITOR_API_SUSPICIOUS_MIN_VIDEOS: int = 3
    PROXY_AUTO_REBALANCE_INTERVAL_SECONDS: int = 600
    PROXY_REBALANCE_MOBILE_WEIGHT: int = 5
    BLOGGER_CHECK_TIMEOUT_SECONDS: int = 180
    BLOGGER_FAILURE_ALERT_THRESHOLD: int = 3
    MONITOR_MOBILE_PROXY_BUSY_DEFER_MINUTES: int = 1
    MONITOR_PROXY_PRESSURE_DEFER_THRESHOLD: int = 3
    MONITOR_PROXY_PRESSURE_DEFER_RATIO: float = 0.4
    MONITOR_PROXY_PRESSURE_GUARD_SECONDS: int = 600
    MONITOR_PROXY_PRESSURE_CONCURRENCY_CAP: int = 1
    MONITOR_PROXY_PRESSURE_ALERT_COOLDOWN_MINUTES: int = 30
    INITIAL_PARSE_TIMEOUT_SECONDS: int = 180
    NO_ACCOUNTS_COOLDOWN_MINUTES: int = 5
    SHARED_BLOGGER_INTERVAL_MINUTES: int = 15
    VIDEO_CREATOR_RUN_NOW_COOLDOWN_MINUTES: int = 3
    VALIDATE_CREATOR_PROFILE_ON_ADD: bool = True
    VIDEO_PARSER_SAVE_HTML_ON_EMPTY: bool = False
    VIDEO_PARSER_DIAGNOSTIC_COOLDOWN_SECONDS: int = 3600
    VIDEO_PARSER_EMPTY_DIAGNOSTIC_SAMPLE_RATE: int = 4
    VIDEO_PARSER_EARLY_API_TARGET: int = 6
    VIDEO_PARSING_WARMUP_TTL_SECONDS: int = 240
    VIDEO_PARSING_EXIT_BEHAVIOR_CHANCE: float = 0.4
    VIDEO_PARSING_API_SCROLL_MIN: int = 4
    VIDEO_PARSING_API_SCROLL_MAX: int = 9
    COMMENT_IDEMPOTENCY_WINDOW_SECONDS: int = 600
    REPLY_IDEMPOTENCY_WINDOW_SECONDS: int = 600
    COMMENT_INPUT_STAGE_RETRIES: int = 2
    COMMENT_SUBMIT_STAGE_RETRIES: int = 2
    REPLY_BUTTON_STAGE_RETRIES: int = 2
    REPLY_INPUT_STAGE_RETRIES: int = 2
    REPLY_SUBMIT_STAGE_RETRIES: int = 2
    REPLY_BROWSER_TRUST_WINDOW_SECONDS: int = 300
    REPLY_BROWSER_ACTIVITY_WINDOW_SECONDS: int = 900
    STAGE_METRICS_ENABLED: bool = True
    
    # Задержки для retry
    RETRY_DELAY_MIN: int = 5  # Минимальная задержка перед retry (секунды)
    RETRY_DELAY_MAX: int = 10  # Максимальная задержка перед retry (секунды)
    
    # Задержки для worker
    WORKER_POLL_INTERVAL: float = 0.1  # Интервал проверки задач в воркере (секунды)
    WORKER_POLL_COUNT: int = 10  # Количество проверок перед паузой
    WORKER_ERROR_DELAY: int = 5  # Задержка при ошибке воркера (секунды)
    
    # ИСПРАВЛЕНО: Настройки для надёжной верификации комментариев (рекомендации ChatGPT)
    COMMENT_APPEARANCE_TIMEOUT: int = 90  # Максимальное время ожидания появления комментария (секунды, 90 секунд)
    COMMENT_VERIFY_CHECK_INTERVAL: int = 8  # Интервал между проверками видимости комментария (секунды, 8 секунд)
    COMMENT_LOCAL_VERIFY_DELAY: int = 3  # Задержка перед локальной проверкой (автор видит свой комментарий, секунды)
    COMMENT_LOCAL_VERIFY_TIMEOUT: int = 10  # Таймаут для локальной проверки (секунды)

    # Backend/executor security
    # ИСПРАВЛЕНО: Убраны дефолтные значения секретов - должны быть заданы через .env
    EXECUTOR_API_TOKEN: str  # Обязательное поле, должно быть задано в .env
    JWT_SECRET: str  # Обязательное поле, должно быть задано в .env
    JWT_ALG: str = "HS256"
    PLAYWRIGHT_DESKTOP_VIEWPORT_WIDTH: int = 1920
    PLAYWRIGHT_DESKTOP_VIEWPORT_HEIGHT: int = 960
    PLAYWRIGHT_PROXYLESS_VIEWPORT_WIDTH: int = 1280
    PLAYWRIGHT_PROXYLESS_VIEWPORT_HEIGHT: int = 720
    PLAYWRIGHT_SYNC_WINDOW_WITH_VIEWPORT: bool = True
    PLAYWRIGHT_WINDOW_EXTRA_HEIGHT: int = 90
    
    # ИСПРАВЛЕНО: Валидация обязательных секретов
    @field_validator('EXECUTOR_API_TOKEN', 'JWT_SECRET')
    @classmethod
    def validate_secrets(cls, v: str) -> str:
        """Валидирует, что секреты заданы и не используют дефолтные значения."""
        if not v or not v.strip():
            raise ValueError(
                f"Secret must be set via environment variable or .env file. "
                f"Current value is empty."
            )
        
        # Проверяем, что это не дефолтные значения
        forbidden_values = [
            'secret-executor-token',
            'supersecret',
            'your-secret-here',
            'change-me',
            'default',
            'secret'
        ]
        
        if v.strip().lower() in forbidden_values:
            raise ValueError(
                f"Secret must not use default/example value. "
                f"Please set a secure value via environment variable or .env file."
            )
        
        # Проверяем минимальную длину
        if len(v.strip()) < 16:
            raise ValueError(
                f"Secret must be at least 16 characters long for security. "
                f"Current length: {len(v.strip())}"
            )
        
        return v.strip()

    # DEMO режим
    # REFACTOR: DEMO_MODE управляется через .env, по умолчанию False (реальные списания)
    DEMO_MODE: bool = False

    # Browser Automation Studio (BAS) настройки
    USE_BAS: bool = False  # Использовать BAS вместо Playwright (False = Playwright, True = BAS)
    BAS_SCRIPT_NAME: str = "TikTokAutomation"  # Имя скомпилированного BAS скрипта (для BAS Remote)
    BAS_TIMEOUT: int = 300  # Таймаут для BAS операций в секундах (5 минут)
    BAS_MAX_RETRIES: int = 3  # Количество повторов при ошибках BAS
    BAS_SCRIPTS_DIR: str = "executor/bas_scripts"  # Папка с BAS скриптами
    
    # Playwright настройки (используются если USE_BAS=False)
    COOKIES_DIR: str = "cookies"    # папка, в которой лежат txt-файлы с cookies для аккаунтов в формате Netscape

    # Playwright настройки
    PLAYWRIGHT_HEADLESS: bool = True  # Запуск браузера в headless режиме (False = видимый браузер для отладки)
    PLAYWRIGHT_TIMEOUT: int = 60000  # Таймаут в миллисекундах (60 секунд) - для резидентских прокси
    PLAYWRIGHT_TIMEOUT_MOBILE: int = 150000  # Мобильные прокси медленнее
    
    # Retry настройки для executor
    COMMENT_POST_MAX_RETRIES: int = 2  # Максимум попыток публикации комментария на одном аккаунте
    VERIFICATION_POST_MAX_RETRIES: int = 2  # Максимум попыток публикации при верификации
    
    # Element search настройки для TikTok client
    ELEMENT_SEARCH_MAX_ATTEMPTS: int = 4  # Максимум попыток поиска элемента (общий)
    COMMENT_SECTION_SEARCH_ATTEMPTS: int = 4  # Попытки поиска секции комментариев
    SUBMIT_BUTTON_SEARCH_ATTEMPTS: int = 3  # Попытки поиска кнопки отправки
    TIKTOK_NETWORK_PROBE: bool = False  # Логирование network endpoints (debug)
    TIKTOK_NETWORK_FIRST: bool = False  # Включить network-first publish/verify (hybrid)
    TIKTOK_NETWORK_IMPL_READY: bool = False  # Safety: включать только когда network-логика реально реализована
    
    # Delays и timeouts
    POST_COMMENT_TIMEOUT: int = 600  # ИСПРАВЛЕНО: Увеличен таймаут для операции post_comment (секунды, 10 минут) - для медленных прокси
    
    # Задержки для загрузки страницы
    PAGE_LOAD_WAIT_PROXY_MS: int = 800  # Для скорости: 500 мс. Текущее: 800 мс (баланс)
    PAGE_LOAD_WAIT_DIRECT_MS: int = 500  # Для скорости: 300 мс. Текущее: 500 мс (баланс)
    
    # Задержки для закрытия оверлеев
    OVERLAY_CLOSE_DELAY_MS: int = 300  # Задержка после закрытия оверлея (мс)
    OVERLAY_CLOSE_ESCAPE_DELAY_MS: int = 200  # Задержка после нажатия ESC (мс)
    
    # Задержки для Rate Limit
    RATE_LIMIT_WAIT_MIN: int = 30  # Минимальная задержка при rate limit (секунды)
    RATE_LIMIT_WAIT_MAX: int = 60  # Максимальная задержка при rate limit (секунды)
    
    # Задержки для проверки авторизации
    AUTH_CHECK_DELAY_PROXY_MS: int = 2000  # Задержка перед проверкой авторизации с прокси (мс)
    AUTH_CHECK_DELAY_DIRECT_MS: int = 1000  # Задержка перед проверкой авторизации без прокси (мс)
    
    # Задержки для загрузки видео
    VIDEO_INIT_WAIT_PROXY_MS: int = 5000  # Ожидание инициализации видео с прокси (мс)
    VIDEO_INIT_WAIT_DIRECT_MS: int = 2000  # Ожидание инициализации видео без прокси (мс)
    
    # Задержки для прокрутки
    SCROLL_WAIT_MS: int = 2000  # Задержка после прокрутки (мс)
    
    # Задержки для комментариев
    COMMENT_INPUT_WAIT_MS: int = 1000  # Задержка перед вводом комментария (мс)
    COMMENT_SUBMIT_WAIT_MS: int = 2000  # Задержка после отправки комментария (мс)
    COMMENT_RETRY_DELAY_MS: int = 1000  # Задержка между попытками отправки (мс)
    
    # Задержки для верификации
    VERIFICATION_DELAY_BEFORE_RETRY_MS: int = 1000  # Задержка перед повторной проверкой (мс)
    VERIFICATION_FINAL_WAIT_MS: int = 2000  # Финальное ожидание после верификации (мс)
    PROXY_MAX_RESPONSE_TIME_MS: int = 30000  # ИСПРАВЛЕНО: Увеличено максимальное время ответа резидентских прокси (30 секунд)
    PROXY_MAX_RESPONSE_TIME_MS_MOBILE: int = 90000  # ИСПРАВЛЕНО: Увеличено максимальное время ответа мобильных прокси (90 секунд) - они очень медленные
    TIKTOK_BASE_URL: str = "https://www.tiktok.com"
    PLAYWRIGHT_SCREENSHOTS_ON_ERROR: bool = True  # Сохранять скриншоты при ошибках
    PLAYWRIGHT_SCREENSHOTS_DIR: str = "screenshots"  # Папка для сохранения скриншотов
    BLOCK_IMAGES: bool = True  # Блокировать загрузку картинок для экономии трафика (аватары, превью)
    BLOCK_MEDIA: bool = False  # ИСПРАВЛЕНО: НЕ блокировать видео - важно для маскировки (TikTok может проверять загрузку)
    BLOCK_FONTS: bool = True  # Блокировать загрузку шрифтов для экономии трафика (не критично)
    BLOCK_STYLESHEETS: bool = False  # НЕ блокировать стили (нужны для корректного отображения UI)
    USE_PROXY: bool = True  # Использовать прокси для аккаунтов (обязательно True - работа только с прокси)
    PROXY_GEO_MATCHING: bool = True  # Подбирать прокси по географии аккаунта (снижает вероятность капчи)
    PROXY_GEO_FALLBACK: bool = True  # Если нет прокси из той же страны, использовать любой доступный
    
    # Настройки мобильных прокси с ротацией IP
    MOBILE_PROXY_ROTATION_DELAY: int = 5  # Задержка после запроса ротации IP (секунды). Большинство провайдеров (proxy-seller и пр.) поднимают IP быстрее 10с.
    MOBILE_PROXY_MAX_RETRIES: int = 3  # Максимум попыток ротации IP если IP не изменился
    MOBILE_PROXY_LOCK_TTL: int = 120  # TTL блокировки прокси в Redis (секунды) - защита от зависания
    MOBILE_PROXY_LOCK_EXTRA_SECONDS: int = 90  # Запас lock сверх BLOGGER_CHECK_TIMEOUT_SECONDS
    MOBILE_PROXY_CHECK_IP_URL: str = "https://api.ipify.org?format=json"  # URL для проверки текущего IP
    # Cooldown переиспользования IP без ротации. При нехватке мобильных прокси воркер теряет
    # ~25–30 с на каждой ротации, что блокирует параллельных воркеров. Если IP уже свежий
    # (моложе cooldown), повторно ротировать нет смысла. 0 = всегда ротировать (старое поведение).
    MOBILE_PROXY_ROTATION_COOLDOWN_SECONDS: int = 180  # 3 минуты по умолчанию (значение для 1 мобильного прокси)
    # Адаптивный cooldown: воркер сам подбирает значение исходя из количества мобильных прокси
    # (1 прокси → ~180с, 2 → ~90с, 3+ → 60с). Жёстко выставленный MOBILE_PROXY_ROTATION_COOLDOWN_SECONDS
    # выступает дефолтом, если адаптация выключена или ещё не успела отработать.
    MOBILE_PROXY_ADAPTIVE_COOLDOWN_ENABLED: bool = True
    MOBILE_PROXY_ROTATION_COOLDOWN_MIN_SECONDS: int = 60   # нижняя граница для адаптивного режима
    MOBILE_PROXY_ROTATION_COOLDOWN_MAX_SECONDS: int = 240  # верхняя граница для адаптивного режима
    
    # Настройки человеческого поведения (для снижения вероятности капчи)
    HUMAN_LIKE_BEHAVIOR: bool = True  # Включить эмуляцию человеческого поведения
    VIDEO_WATCH_MIN_SECONDS: float = 8.0  # ИСПРАВЛЕНО: Минимальное время просмотра видео (увеличено для реалистичности)
    VIDEO_WATCH_MAX_SECONDS: float = 18.0  # ИСПРАВЛЕНО: Максимальное время просмотра видео (увеличено для реалистичности)
    TYPING_SPEED_MIN_MS: int = 100  # Минимальная задержка между символами при вводе (мс) - немного медленнее
    TYPING_SPEED_MAX_MS: int = 250  # Максимальная задержка между символами при вводе (мс)
    RANDOM_MOUSE_MOVEMENTS: bool = True  # Случайные движения мыши для имитации активности
    SIMULATE_PAGE_INTERACTION: bool = True  # Имитировать взаимодействие со страницей (прокрутки, клики)
    MIN_DELAY_BETWEEN_ACTIONS_MS: int = 500  # Минимальная задержка между действиями (мс)
    MAX_DELAY_BETWEEN_ACTIONS_MS: int = 2000  # Максимальная задержка между действиями (мс)
    
    # Настройки парсинга видео (для снижения риска капчи)
    VIDEO_PARSING_WARMUP_ENABLED: bool = True  # Включить warm-up перед парсингом профиля
    VIDEO_PARSING_WARMUP_SCROLLS_MIN: int = 2  # Минимальное количество скроллов на FYP
    VIDEO_PARSING_WARMUP_SCROLLS_MAX: int = 3  # Максимальное количество скроллов на FYP
    VIDEO_PARSING_WARMUP_WATCH_TIME_MIN_MS: int = 2000  # Минимальное время просмотра видео на FYP (мс)
    VIDEO_PARSING_WARMUP_WATCH_TIME_MAX_MS: int = 5000  # Максимальное время просмотра видео на FYP (мс)
    VIDEO_PARSING_SCROLL_ITERATIONS_MIN: int = 5  # Минимальное количество скроллов при парсинге
    VIDEO_PARSING_SCROLL_ITERATIONS_MAX: int = 10  # Максимальное количество скроллов при парсинге
    VIDEO_PARSING_SCROLL_AMOUNT_MIN: int = 300  # Минимальная величина скролла (px)
    VIDEO_PARSING_SCROLL_AMOUNT_MAX: int = 800  # Максимальная величина скролла (px)
    VIDEO_PARSING_SCROLL_DELAY_MIN_MS: int = 1200  # Минимальная задержка между скроллами (мс)
    VIDEO_PARSING_SCROLL_DELAY_MAX_MS: int = 2500  # Максимальная задержка между скроллами (мс)
    VIDEO_PARSING_READ_SIMULATION_CHANCE: float = 0.7  # Вероятность имитации чтения после скролла (0.0-1.0)
    VIDEO_PARSING_READ_TIME_MIN_MS: int = 1000  # Минимальное время имитации чтения (мс)
    VIDEO_PARSING_READ_TIME_MAX_MS: int = 3000  # Максимальное время имитации чтения (мс)
    VIDEO_PARSING_INITIAL_LOAD_DELAY_MIN_MS: int = 2000  # Минимальная задержка после загрузки страницы (мс)
    VIDEO_PARSING_INITIAL_LOAD_DELAY_MAX_MS: int = 5000  # Максимальная задержка после загрузки страницы (мс)
    VIDEO_PARSING_MOUSE_MOVEMENT_CHANCE: float = 0.5  # Вероятность случайных движений мыши (0.0-1.0)
    VIDEO_PARSING_HOVER_CHANCE: float = 0.3  # Вероятность hover на элементы (0.0-1.0)
    
    # Валидация аккаунтов и прокси
    VALIDATE_BEFORE_TASK: bool = True  # Быстрая проверка куков перед использованием аккаунта
    VALIDATE_PROXY_BEFORE_TASK: bool = True  # Быстрая проверка прокси перед использованием (если USE_PROXY=True)
    VALIDATION_RETRY_COUNT: int = 2  # Сколько раз пробовать найти рабочий аккаунт если текущий не прошёл проверку
    VALIDATION_FAST_MODE: bool = True  # Быстрый режим валидации (меньше ожиданий, оптимизированные проверки)
    VALIDATION_MAX_CONCURRENT: int = 12  # Максимальное количество параллельных валидаций (для скорости, увеличено с 8)
    VALIDATION_TIMEOUT_SECONDS: int = 45  # ОПТИМИЗИРОВАНО: Уменьшен базовый таймаут для валидации (было 60)
    VALIDATION_TIMEOUT_MOBILE_SECONDS: int = 90  # ОПТИМИЗИРОВАНО: Уменьшен таймаут для мобильных прокси (было 120)
    VALIDATION_WAIT_AFTER_LOAD_MS: int = 2000  # ОПТИМИЗИРОВАНО: Уменьшено время ожидания после загрузки (было 3000)
    VALIDATION_RETRY_ON_TIMEOUT: bool = True  # Повторять валидацию при таймауте (один раз)
    
    # ИСПРАВЛЕНО: Фильтрация прокси по типу теперь управляется через БД (AppSettings)
    # Настройки PROXY_TYPE_FILTER и PROXY_PREFER_MOBILE удалены - используйте админ панель
    
    # Валидация длины текста
    MAX_COMMENT_LENGTH: int = 2200  # Максимальная длина комментария (лимит TikTok)
    MAX_REPLY_LENGTH: int = 2200  # Максимальная длина ответа (лимит TikTok)
    TEXT_STATS_TTL_HOURS: int = 12  # TTL статистики по текстам (для детекта теней)
    TEXT_SHADOW_THRESHOLD: int = 3  # Порог подряд теневых/невидимых публикаций для авто-стопа
    
    # Мониторинг (Sentry)
    SENTRY_DSN: str | None = None  # DSN для Sentry (если не задан, мониторинг отключён)
    APP_VERSION: str = "1.0.0"  # Версия приложения для Sentry

    # Telegram
    BOT_TOKEN: str | None = None
    API_BASE: str | None = None
    API_JWT: str | None = None
    OWNER_TELEGRAM_ID: int | None = None
    # Опционально: X-Admin-Token для API. Если не задан, принимается EXECUTOR_API_TOKEN (как у bot api_client).
    ADMIN_API_TOKEN: str | None = None
    TELEGRAM_PROXY: str | None = None  # Прокси для Telegram бота (например: http://user:pass@proxy:8080 или socks5://proxy:1080)

    # Мониторинг и пороги
    PROXY_TEST_URL: str = "https://api.ipify.org?format=json"
    MIN_ACTIVE_ACCOUNTS: int = 50
    MIN_ACTIVE_PROXIES: int = 20
    DEFAULT_LIKES_FOR_FAST_ORDER: int = 100  # Legacy поле; фактическое количество берётся из настроек лайков
    MAX_TEMPLATES_PER_USER: int = 10
    MAX_REPLIES: int = 20  # Максимальное количество ответов на комментарий (настраивается в админке)
    
    # Boost Project API — лайки на комментарии TikTok (внешний сервис)
    BOOST_PROJECT_API_KEY: str = ""  # API ключ (из .env). Если пусто — заказ лайков через Boost отключён
    BOOST_PROJECT_BASE_URL: str = "https://boost-project.pro/api/v1"
    BOOST_PROJECT_TIKTOK_LIKES_SERVICE_ID: int = 5  # TikTok | Лайки на комментарии
    BOOST_PROJECT_LIKES_QUANTITY: int = 100  # Количество лайков по умолчанию (диапазон 50-5000)
    
    # Пороги для обработки ошибок
    ERROR_THRESHOLD: int = 5  # Сколько ошибок подряд нужно для статуса "error" аккаунта
    PROXY_FAIL_THRESHOLD: int = 3  # Сколько неудач нужно для статуса "bad" прокси
    
    # Цена подписки (для отображения пользователям)
    SUBSCRIPTION_PRICE: str = "1500 грн/месяц"  # Текст с ценой подписки
    
    # Лимиты на задачи
    MAX_VIDEOS_PER_USER_PER_DAY: int = 50  # Максимальное количество видео в день на одного пользователя (настраивается в админке)
    
    # Настройки executor
    MAX_RETRY_ACCOUNTS: int = 3  # Максимум попыток с разными аккаунтами для одной задачи
    PARALLEL_COMMENTING_MAX_MAIN_ATTEMPTS: int = 6
    PARALLEL_COMMENTING_MAX_REPLY_ATTEMPTS: int = 6
    PARALLEL_DYNAMIC_WORKER_POOL_ENABLED: bool = True
    PARALLEL_COMMENTING_TARGET_BROWSERS: int = 4
    PARALLEL_COMMENTING_MAX_BROWSERS: int = 4
    PARALLEL_MAIN_PUBLISH_MAX_SECONDS: float = 32.0
    PARALLEL_MAIN_INPUT_RETRY_MAX_SECONDS: float = 18.0
    PARALLEL_MAIN_OPEN_COMMENTS_MAX_SECONDS: float = 10.0
    PARALLEL_MAIN_INPUT_SEARCH_MAX_SECONDS: float = 6.0
    PARALLEL_REPLY_PUBLISH_MAX_SECONDS: float = 30.0
    PARALLEL_REPLY_START_DELAY_MIN_SECONDS: float = 2.0
    PARALLEL_REPLY_START_DELAY_MAX_SECONDS: float = 5.0
    PARALLEL_MAIN_READY_IDLE_MIN_SECONDS: float = 0.2
    PARALLEL_MAIN_READY_IDLE_MAX_SECONDS: float = 0.6
    PARALLEL_REPLY_READY_IDLE_MIN_SECONDS: float = 0.2
    PARALLEL_REPLY_READY_IDLE_MAX_SECONDS: float = 0.6
    PARALLEL_INITIAL_SHELL_READY_SECONDS: float = 12.0
    PARALLEL_PAGE_LOADING_EXTRA_WAIT_SECONDS: float = 18.0
    PARALLEL_MAIN_WARMUP_PROBABILITY: float = 0.0
    PARALLEL_MAIN_WARMUP_TIMEOUT_SECONDS: float = 3.0
    PARALLEL_PROXY_DIVERSIFY_BY_SOURCE_HOST: bool = True
    PARALLEL_COMMENTING_DOM_FIRST: bool = True
    PARALLEL_COMMENTING_FAST_AUTH_GATE: bool = True
    ACCOUNT_VALIDATION_CACHE_TTL_MINUTES: int = 2  # TTL кэша валидации аккаунтов (минуты)
    ACCOUNT_VALIDATION_MAX_AGE_MINUTES: int = 10  # Максимальный возраст валидации аккаунта (минуты)

    # USER_SCHEMA_ENABLED включает новый workflow по схеме владельца:
    # один аккаунт пишет все 3 варианта главного коммента подряд; reply-аккаунты
    # ищут любой из них в DOM и отвечают по матрице (один аккаунт = один вариант
    # ответа на все найденные main-комменты). При неудаче — fallback на reply-
    # аккаунт пишущий главные. Если выключить, выполняется старая логика
    # execute_hybrid_full.
    USER_SCHEMA_ENABLED: bool = True

    # Сколько раз подряд аккаунт может выдать «dead session» при визуальной
    # проверке login, после чего он помечается reauth_needed и больше не
    # берётся в работу до повторной валидации/перелогина. Делается per-account
    # на базе Account.validation_fail_count, которое уже есть в БД и
    # сбрасывается mark_success при первом же успешном комментарии.
    COOKIE_DEAD_RETIRE_THRESHOLD: int = 2
    COOKIE_BLOCKED_RETIRE_THRESHOLD: int = 3
    
    # Настройки бота
    ORDERS_PER_PAGE: int = 15  # Количество заказов на странице в боте
    
    # ИСПРАВЛЕНО: Настройки логирования
    USE_JSON_LOGGING: bool = Field(
        default=False,
        description="Использовать structured logging (JSON) вместо обычного текстового формата"
    )
    EXECUTOR_LOG_FILE: str = Field(
        default="logs/executor.log",
        description="Executor log file path. Use a separate file for long diagnostic runs if needed.",
    )
    LOG_FILE_MAX_MB: int = Field(
        default=50,
        ge=1,
        le=2048,
        description="Maximum size of one rotating log file in megabytes.",
    )
    LOG_FILE_BACKUP_COUNT: int = Field(
        default=20,
        ge=1,
        le=200,
        description="Number of rotated executor log files to keep.",
    )


@lru_cache()
def get_settings() -> Settings:
    return Settings()
