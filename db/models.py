from sqlalchemy import Column, Integer, BigInteger, String, Text, JSON, DateTime, ForeignKey, Index, Numeric, Boolean
from sqlalchemy.sql import func
from sqlalchemy.orm import relationship
from db.database import Base
from decimal import Decimal


class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, index=True)
    telegram_id = Column(BigInteger, unique=True, index=True, nullable=False)
    username = Column(Text, nullable=True)
    role = Column(String(20), default="user", nullable=False)
    balance = Column(Numeric(10, 2), default=Decimal("0.00"), nullable=False)  # Баланс пользователя в гривнах
    is_blocked = Column(Boolean, default=False, nullable=False, index=True)  # Заблокирован ли доступ пользователя
    # True после первого ручного одобрения админом. Нужно, чтобы отличить
    # "новый работник, ждёт одобрения" (is_blocked=True, was_approved_once=False)
    # от "был активен и заблокирован за нарушения" (is_blocked=True, was_approved_once=True).
    # Дефолт False у существующих пользователей; миграция выставит True всем,
    # у кого is_blocked=False, чтобы они не считались pending.
    was_approved_once = Column(Boolean, default=False, nullable=False, server_default="0")
    # True means this worker bypasses the global parsing-only mode and sees the
    # full bot feature set without becoming admin/owner.
    full_access_override = Column(Boolean, default=False, nullable=False, server_default="0")

    # Дневной лимит видео для парсинга. Поля существуют в БД и используются
    # квотой парсинга (today_videos, last_video_date — дата последнего сброса
    # счётчика). Восстановлены, чтобы не падать с AttributeError.
    daily_video_limit = Column(Integer, default=50, nullable=False, server_default="50")
    today_videos = Column(Integer, default=0, nullable=False, server_default="0")
    last_video_date = Column(DateTime(timezone=True), nullable=True)
    video_notifications_enabled = Column(Boolean, default=True, nullable=False, server_default="1")

    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    subscriptions = relationship("Subscription", back_populates="user", cascade="all, delete-orphan")
    tasks = relationship("Task", back_populates="user", cascade="all, delete-orphan")
    task_templates = relationship("TaskTemplate", back_populates="user", cascade="all, delete-orphan")
    comment_templates = relationship("CommentTemplate", back_populates="user", cascade="all, delete-orphan")
    transactions = relationship("Transaction", back_populates="user", cascade="all, delete-orphan")


class Subscription(Base):
    __tablename__ = "subscriptions"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    tier = Column(String(50), default="standard")
    tasks_per_month = Column(Integer, nullable=True)
    expire_at = Column(DateTime(timezone=True), nullable=False, index=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    user = relationship("User", back_populates="subscriptions")


class Account(Base):
    __tablename__ = "accounts"

    id = Column(Integer, primary_key=True, index=True)
    label = Column(Text, nullable=False, unique=True, index=True)
    cookies = Column(Text, nullable=True)  # Может быть null, если cookies в файле
    cookie_file = Column(Text, nullable=True)  # Имя файла с cookies
    user_agent = Column(Text, nullable=True)  # Может быть null
    proxy = Column(Text, nullable=True)
    country_code = Column(String(2), nullable=True, index=True)  # ISO 3166-1 alpha-2 (например, "US", "UA")
    status = Column(String(20), default="active", nullable=False, index=True)
    today_main_comments = Column(Integer, default=0, nullable=False)
    today_replies = Column(Integer, default=0, nullable=False)
    last_used_at = Column(DateTime(timezone=True), nullable=True)
    # Scheduler history is separate from successful posts and monitor reads.
    last_assigned_at = Column(DateTime(timezone=True), nullable=True, index=True)
    assignment_count = Column(Integer, default=0, server_default="0", nullable=False)
    assignment_lease_until = Column(DateTime(timezone=True), nullable=True)
    assignment_lease_token = Column(String(64), nullable=True)
    last_validation_attempt_at = Column(DateTime(timezone=True), nullable=True, index=True)
    last_main_comment_at = Column(DateTime(timezone=True), nullable=True, index=True)
    last_error = Column(Text, nullable=True)
    error_count_recent = Column(Integer, default=0, nullable=False)
    validation_fail_count = Column(Integer, default=0, nullable=False)  # Счётчик неудачных валидаций подряд
    last_checked_at = Column(DateTime(timezone=True), nullable=True)  # Когда последний раз проверяли
    last_validation_ok = Column(DateTime(timezone=True), nullable=True)  # Когда последний раз валидация была OK
    # owner_user_id == NULL  -> аккаунт принадлежит общему пулу (как все аккаунты до этой фичи).
    # owner_user_id IS NOT NULL -> "личный" аккаунт работника, выбирается ТОЛЬКО когда заказ
    # создан с account_pool_mode="personal" этим же работником. Пересечения между общим и
    # личным пулами в селекторах нет — это гарантия приватности и предсказуемости лимитов.
    owner_user_id = Column(
        Integer,
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    # Per-account override глобального settings.MAX_ACTIONS_PER_ACCOUNT.
    # NULL -> использовать глобальный лимит (поведение как раньше).
    # Эти поля выставляются преимущественно для личных аккаунтов, где работник
    # хочет жёстко ограничить активность 5/10 в сутки против спам-фильтра TikTok.
    daily_main_limit = Column(Integer, nullable=True)
    daily_reply_limit = Column(Integer, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    logs = relationship("Log", back_populates="account")
    
    # ИСПРАВЛЕНО: Составные индексы для оптимизации частых запросов
    __table_args__ = (
        # Составной индекс для поиска свободных аккаунтов для основных комментариев
        # Используется в get_free_account_for_main_comment:
        # WHERE status IN ('active', 'error') AND today_main_comments < MAX AND last_main_comment_at < threshold
        Index('idx_account_main_comment', 'status', 'today_main_comments', 'last_main_comment_at'),
        
        # Составной индекс для фильтрации active/error аккаунтов с error_count_recent
        # Используется в get_free_account_for_main_comment и get_free_account_for_reply:
        # WHERE status = 'error' AND error_count_recent < ERROR_THRESHOLD
        Index('idx_account_status_error_count', 'status', 'error_count_recent'),
        
        # Составной индекс для geo-matching прокси
        # Используется в proxy_assignment_service для подбора прокси по стране:
        # WHERE status != 'dead' AND country_code = X
        Index('idx_account_status_country', 'status', 'country_code'),
    )


class Task(Base):
    __tablename__ = "tasks"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    status = Column(String(20), default="pending", nullable=False, index=True)
    main_comment = Column(Text, nullable=False)
    reply1 = Column(Text, nullable=True)
    reply2 = Column(Text, nullable=True)
    reply3 = Column(Text, nullable=True)
    likes = Column(Integer, default=0, nullable=False)
    video_urls = Column(JSON, nullable=False)  # Список URL в формате JSON для совместимости с SQLite
    settings = Column(JSON, nullable=True)
    progress = Column(Integer, default=0, nullable=False)
    attempts = Column(Integer, default=0, nullable=False)
    error_reason = Column(Text, nullable=True)
    total_cost = Column(Numeric(10, 2), nullable=True)  # Общая стоимость заказа (списанная сумма)
    refunded_cost = Column(Numeric(10, 2), nullable=True)  # Возвращенная сумма (для неудачных видео)
    video_attempts = Column(JSON, nullable=True)  # Словарь {video_url: attempt_count} для отслеживания попыток
    # HYBRID: Тип заказа (full или engagement_only)
    kind = Column(String(20), default="full", nullable=False)  # "full" или "engagement_only"
    created_at = Column(DateTime(timezone=True), server_default=func.now(), index=True)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    user = relationship("User", back_populates="tasks")
    logs = relationship("Log", back_populates="task")
    transactions = relationship("Transaction", back_populates="task", cascade="all, delete-orphan")
    
    # ИСПРАВЛЕНО: Составные индексы для оптимизации частых запросов
    __table_args__ = (
        # Составной индекс для поиска задач по статусу и пользователю
        # Используется в api/tasks.py для фильтрации задач:
        # WHERE status = X AND user_id = Y
        Index('idx_task_status_user', 'status', 'user_id'),
        
        # Составной индекс для поиска pending задач (SELECT FOR UPDATE)
        # Используется в executor/executor.py для выборки задач:
        # WHERE status = 'pending' ORDER BY updated_at
        Index('idx_task_status_updated', 'status', 'updated_at'),
    )


class Log(Base):
    __tablename__ = "logs"

    id = Column(Integer, primary_key=True, index=True)
    task_id = Column(Integer, ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False, index=True)
    account_id = Column(Integer, ForeignKey("accounts.id", ondelete="SET NULL"), nullable=True)
    message = Column(Text, nullable=False)
    timestamp = Column(DateTime(timezone=True), server_default=func.now(), index=True)

    task = relationship("Task", back_populates="logs")
    account = relationship("Account", back_populates="logs")


class TaskTemplate(Base):
    __tablename__ = "task_templates"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    name = Column(Text, nullable=False)
    main_comment = Column(Text, nullable=False)
    reply1 = Column(Text, nullable=True)
    reply2 = Column(Text, nullable=True)
    reply3 = Column(Text, nullable=True)
    likes = Column(Integer, default=0, nullable=False)
    settings = Column(JSON, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    user = relationship("User", back_populates="task_templates")


class CommentTemplate(Base):
    __tablename__ = "comment_templates"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    name = Column(Text, nullable=False)
    text = Column(Text, nullable=False)
    type = Column(String(20), nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    user = relationship("User", back_populates="comment_templates")


class Proxy(Base):
    __tablename__ = "proxies"
    
    id = Column(Integer, primary_key=True, index=True)
    proxy = Column(Text, nullable=False, unique=True, index=True)
    country_code = Column(String(2), nullable=True, index=True)  # ISO 3166-1 alpha-2 (например, "US", "UA")
    # В БД поле status есть и NOT NULL (legacy). Оставляем, но не используем в логике.
    status = Column(String(20), default="active", nullable=False, index=True)
    # В БД fail_count есть и NOT NULL (legacy). Оставляем для совместимости.
    fail_count = Column(Integer, default=0, nullable=False)
    # last_checked_at = Column(DateTime(timezone=True), nullable=True)  # УДАЛЕНО
    # last_error = Column(Text, nullable=True)  # УДАЛЕНО
    # fail_count = Column(Integer, default=0, nullable=False)  # УДАЛЕНО
    # response_time_ms = Column(Integer, nullable=True)  # УДАЛЕНО
    rotation_url = Column(Text, nullable=True)  # URL для ротации IP (для мобильных прокси)
    is_mobile = Column(String(1), default="N", nullable=False)  # "Y" или "N" - быстрая проверка типа прокси
    # NULL -> обычный общий прокси. Значение -> прокси зарезервирован только
    # для shared-заказов конкретного пользователя. TikTok-аккаунты при этом
    # остаются в общем пуле; executor подставляет этот прокси только на время
    # выполнения заказа и не сохраняет его в Account.proxy.
    dedicated_user_id = Column(
        Integer,
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
        unique=True,
        index=True,
    )
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
    
    # Индексы
    __table_args__ = (
        # Индекс для поиска мобильных прокси
        Index('idx_proxy_is_mobile', 'is_mobile'),
        # Индекс для geo-matching (по стране)
        Index('idx_proxy_country', 'country_code'),
    )


class Pricing(Base):
    __tablename__ = "pricing"

    id = Column(Integer, primary_key=True, index=True)
    price_success = Column(Numeric(10, 2), default=Decimal("5.00"), nullable=False)  # Цена за успешное комментирование (по умолчанию 5 грн)
    price_failure = Column(Numeric(10, 2), default=Decimal("2.00"), nullable=False)  # Цена за неуспешное комментирование (по умолчанию 2 грн)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class Transaction(Base):
    __tablename__ = "transactions"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    task_id = Column(Integer, ForeignKey("tasks.id", ondelete="SET NULL"), nullable=True, index=True)
    amount = Column(Numeric(10, 2), nullable=False)  # Может быть отрицательным для списаний
    type = Column(String(50), nullable=False, index=True)  # deposit, withdrawal, hold, refund
    description = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), index=True)

    user = relationship("User", back_populates="transactions")
    task = relationship("Task", back_populates="transactions")


class AdminAction(Base):
    """ИСПРАВЛЕНО: Модель для логирования критических админских операций."""
    __tablename__ = "admin_actions"

    id = Column(Integer, primary_key=True, index=True)
    admin_telegram_id = Column(BigInteger, nullable=False, index=True)  # Telegram ID админа
    admin_username = Column(Text, nullable=True)  # Username админа (для удобства)
    action_type = Column(String(50), nullable=False, index=True)  # balance_add, balance_subtract, block, unblock, delete_accounts, delete_proxies, etc.
    target_user_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True)  # ID пользователя, на которого действовали
    target_account_id = Column(Integer, ForeignKey("accounts.id", ondelete="SET NULL"), nullable=True)  # ID аккаунта (если применимо)
    details = Column(JSON, nullable=True)  # Дополнительные детали (сумма, старое/новое значение и т.д.)
    description = Column(Text, nullable=True)  # Текстовое описание действия
    created_at = Column(DateTime(timezone=True), server_default=func.now(), index=True)

    target_user = relationship("User", foreign_keys=[target_user_id])
    
    __table_args__ = (
        # Индекс для поиска действий по админу и типу
        Index('idx_admin_action_admin_type', 'admin_telegram_id', 'action_type'),
        # Индекс для поиска действий по пользователю
        Index('idx_admin_action_target_user', 'target_user_id', 'created_at'),
    )


class AppSettings(Base):
    """Настройки приложения, управляемые через админ панель."""
    __tablename__ = "app_settings"

    id = Column(Integer, primary_key=True, index=True)
    key = Column(String(100), unique=True, nullable=False, index=True)  # Ключ настройки
    value = Column(Text, nullable=True)  # Значение настройки (JSON или строка)
    description = Column(Text, nullable=True)  # Описание настройки
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
    updated_by = Column(BigInteger, nullable=True)  # Telegram ID админа, который обновил


class MonitoredBlogger(Base):
    """
    Уникальные блогеры для единого мониторинга (Proxy Optimization).
    Отделяет состояние парсинга от пользовательских подписок.

    Структура колонок и индексы должны совпадать с фактической DDL в БД
    (см. dev.db / Postgres). Часть полей используется адаптивным
    мониторингом: latency_ema_minutes, total_videos_caught, total_late_catches,
    priority_mode и override_*-интервалы.
    """
    __tablename__ = "monitored_bloggers"

    id = Column(Integer, primary_key=True)
    username = Column(String(64), nullable=False, unique=True, index=True)

    # Базовое состояние парсинга (общее для всех подписчиков)
    last_checked_at = Column(DateTime(timezone=True), nullable=True)
    last_seen_video_id = Column(String(32), nullable=True)

    # Лок воркера: lock_until — до когда занят, lock_owner — кем занят.
    lock_until = Column(DateTime(timezone=True), nullable=True, index=True)
    lock_owner = Column(String(120), nullable=True)

    # Расширенное состояние планировщика
    last_attempt_at = Column(DateTime(timezone=True), nullable=True)
    last_success_at = Column(DateTime(timezone=True), nullable=True)
    next_check_at = Column(DateTime(timezone=True), nullable=True)

    # Адаптивная аналитика и оверрайды
    posting_stats = Column(JSON, nullable=True)
    priority_mode = Column(String(20), nullable=False, default="auto", server_default="auto")
    empty_streak = Column(Integer, nullable=False, default=0, server_default="0")
    override_min_interval = Column(Integer, nullable=True)
    override_max_interval = Column(Integer, nullable=True)
    notes = Column(Text, nullable=True)
    latency_ema_minutes = Column(Numeric, nullable=True)  # REAL в SQLite
    last_detection_latency_min = Column(Integer, nullable=True)
    total_videos_caught = Column(Integer, nullable=False, default=0, server_default="0")
    total_late_catches = Column(Integer, nullable=False, default=0, server_default="0")

    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    __table_args__ = (
        Index('idx_monitored_blogger_due_lock', 'next_check_at', 'lock_until'),
        Index('idx_monitored_blogger_success_due', 'last_success_at', 'next_check_at'),
    )

    # Связь с подписками пользователей
    subscriptions = relationship("CreatorSource", back_populates="monitored_blogger")


class BloggerCheckLog(Base):
    """История проверок блогеров: каждый запуск scan'а пишет здесь строку.

    Используется в:
    - services/video_monitor_worker.py — пишет результат каждой проверки
      (с учётом задержки обнаружения, статуса, adaptive решения).
    - api/adaptive_monitoring.py — читает для отчётов и статистики
      (latency, top-блогеры).

    Таблица существовала в БД до этого момента, но соответствующая модель
    была случайно удалена при рефакторинге. Структура восстановлена точно
    по существующему DDL — никаких миграций не требуется.
    """
    __tablename__ = "blogger_check_log"

    id = Column(Integer, primary_key=True, index=True)
    blogger_id = Column(
        Integer,
        ForeignKey("monitored_bloggers.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    checked_at = Column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    found_new_count = Column(Integer, default=0, nullable=False)
    interval_used_minutes = Column(Integer, nullable=True)
    next_interval_minutes = Column(Integer, nullable=True)
    status = Column(String(20), nullable=True)
    adaptive_status = Column(String(20), nullable=True)
    reason = Column(Text, nullable=True)
    duration_ms = Column(Integer, nullable=True)
    detection_latency_min = Column(Integer, nullable=True)

    __table_args__ = (
        Index('idx_check_log_blogger_checked', 'blogger_id', 'checked_at'),
    )


class MonitorOutbox(Base):
    """Outbox-очередь событий мониторинга TikTok-блогеров.

    Используется в:
    - services/video_monitor_worker.py — пишет события (новые видео, изменения
      подписок) и обрабатывает их в фоне с механикой retry/lock_owner.
    - api/video_parsing.py — массовая очистка очереди при сбросе данных.

    Таблица существовала в БД, но соответствующая ORM-модель была случайно
    удалена при рефакторинге. Структура восстановлена точно по существующему
    DDL — никаких миграций не требуется.
    """
    __tablename__ = "monitor_outbox"

    id = Column(Integer, primary_key=True, index=True)
    event_type = Column(String(50), nullable=False)
    status = Column(String(20), nullable=False, default="pending", server_default="pending")
    dedupe_key = Column(String(255), unique=True, nullable=True)
    blogger_id = Column(
        Integer,
        ForeignKey("monitored_bloggers.id", ondelete="CASCADE"),
        nullable=True,
    )
    creator_id = Column(
        Integer,
        ForeignKey("creator_sources.id", ondelete="CASCADE"),
        nullable=True,
    )
    payload = Column(JSON, nullable=False)
    attempts = Column(Integer, nullable=False, default=0, server_default="0")
    available_at = Column(DateTime(timezone=True), nullable=True)
    locked_at = Column(DateTime(timezone=True), nullable=True)
    lock_owner = Column(String(120), nullable=True)
    last_error = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=True)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=True)

    __table_args__ = (
        Index('idx_monitor_outbox_status_available', 'status', 'available_at'),
        Index('idx_monitor_outbox_type_status', 'event_type', 'status'),
    )


class CreatorSource(Base):
    """Блогеры для мониторинга TikTok. ИСПРАВЛЕНО: Теперь персональные для каждого работника."""
    __tablename__ = "creator_sources"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=True, index=True)  # ИСПРАВЛЕНО: Добавлен user_id для персональных блогеров (nullable=True для обратной совместимости)
    username = Column(String(64), nullable=False, index=True)  # ИСПРАВЛЕНО: Убрана уникальность, теперь уникальность по (user_id, username)
    
    # Ссылка на единого мониторимого блогера (nullable=True пока не проведена миграция)
    monitored_blogger_id = Column(Integer, ForeignKey("monitored_bloggers.id", ondelete="SET NULL"), nullable=True, index=True)
    monitored_blogger = relationship("MonitoredBlogger", back_populates="subscriptions")
    
    enabled = Column(Boolean, default=True, nullable=False, index=True)

    interval_minutes = Column(Integer, default=30, nullable=False)
    mode = Column(String(20), default="manual_review", nullable=False)  # auto_comment/manual_review/notify_only

    # УЛУЧШЕНО: default_template_id для авто-комментирования (один главный шаблон)
    default_template_id = Column(Integer, ForeignKey("task_templates.id", ondelete="SET NULL"), nullable=True, index=True)
    template_ids = Column(JSON, nullable=True)  # [1,2,3] - список ID шаблонов (deprecated, используем default_template_id)
    auto_start = Column(Boolean, default=False, nullable=False)  # DEPRECATED: используем mode="auto_comment"

    # УСТАРЕЛО: Эти поля теперь живут в MonitoredBlogger
    last_checked_at = Column(DateTime(timezone=True), nullable=True)
    last_seen_video_id = Column(String(32), nullable=True)
    lock_until = Column(DateTime(timezone=True), nullable=True, index=True)  # lease для предотвращения double-check (индекс для быстрого поиска)

    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    user = relationship("User", backref="creator_sources")  # ИСПРАВЛЕНО: Добавлена связь с пользователем
    videos = relationship("CreatorVideo", back_populates="creator", cascade="all, delete-orphan")
    video_batches = relationship("VideoBatch", back_populates="creator", cascade="all, delete-orphan")  # P0 FIX: Batch для callback_data

    __table_args__ = (
        Index('idx_creator_enabled_checked', 'enabled', 'last_checked_at'),
        Index('idx_creator_user_enabled', 'user_id', 'enabled'),  # ИСПРАВЛЕНО: Добавлен индекс для фильтрации по user_id и enabled
        Index('idx_creator_user_username', 'user_id', 'username', unique=True),  # ИСПРАВЛЕНО: Уникальность username в рамках user_id
    )


class CreatorVideo(Base):
    """Найденные видео блогеров."""
    __tablename__ = "creator_videos"

    id = Column(Integer, primary_key=True)
    creator_id = Column(Integer, ForeignKey("creator_sources.id", ondelete="CASCADE"), nullable=False, index=True)

    # P0 FIX: Убрали global unique, теперь unique только в рамках creator_id
    video_id = Column(String(32), nullable=False, index=True)
    url = Column(Text, nullable=False)

    status = Column(String(20), default="new", nullable=False, index=True)  # P1 FIX: Стандартизированные статусы: new/queued/done/skipped/failed
    task_id = Column(Integer, ForeignKey("tasks.id", ondelete="SET NULL"), nullable=True, index=True)
    meta = Column(JSON, nullable=True)

    discovered_at = Column(DateTime(timezone=True), server_default=func.now(), index=True)

    creator = relationship("CreatorSource", back_populates="videos")
    task = relationship("Task")

    __table_args__ = (
        Index('idx_creator_video_status', 'status', 'discovered_at'),
        # P0 FIX: Composite unique constraint - одно видео может быть у разных подписок
        Index('idx_creator_video_unique', 'creator_id', 'video_id', unique=True),
    )


class VideoBatch(Base):
    """
    P0 FIX: Batch для хранения списка видео вместо передачи в callback_data.
    Решает проблему превышения лимита Telegram (64 байта на callback_data).
    """
    __tablename__ = "video_batches"

    id = Column(Integer, primary_key=True, index=True)
    creator_id = Column(Integer, ForeignKey("creator_sources.id", ondelete="CASCADE"), nullable=False, index=True)
    video_ids = Column(JSON, nullable=False)  # Список video_id (как JSON массив)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), index=True)
    expires_at = Column(DateTime(timezone=True), nullable=True, index=True)  # TTL для очистки старых batch'ей

    creator = relationship("CreatorSource", back_populates="video_batches")

    __table_args__ = (
        Index('idx_video_batch_creator_created', 'creator_id', 'created_at'),
    )


class PendingEnqueue(Base):
    """
    Резерв задач, которые не удалось положить в Redis очередь.
    Позволяет восстановить задачи при недоступности Redis.
    """
    __tablename__ = "pending_enqueue"

    id = Column(Integer, primary_key=True, index=True)
    task_id = Column(Integer, ForeignKey("tasks.id", ondelete="CASCADE"), unique=True, nullable=False, index=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), index=True)

    task = relationship("Task")
