from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup


def get_admin_menu_keyboard() -> InlineKeyboardMarkup:
    """Main admin dashboard keyboard."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="🛰 Мониторинг", callback_data="admin:monitoring"),
                InlineKeyboardButton(text="📋 Очередь", callback_data="admin:queue"),
            ],
            [
                InlineKeyboardButton(text="🔑 Аккаунты", callback_data="admin:accounts"),
                InlineKeyboardButton(text="🧠 Runtime", callback_data="admin:accounts:health"),
            ],
            [
                InlineKeyboardButton(text="🌐 Прокси", callback_data="admin:proxies"),
                InlineKeyboardButton(text="📊 Ресурсы", callback_data="admin:resources"),
            ],
            [
                InlineKeyboardButton(text="👥 Пользователи", callback_data="admin:users"),
                InlineKeyboardButton(text="🌍 Гео", callback_data="admin:geo"),
            ],
            [
                InlineKeyboardButton(text="⚙️ Прокси-режим", callback_data="admin:proxy_settings"),
                InlineKeyboardButton(text="💰 Цены", callback_data="admin:pricing"),
            ],
            [
                InlineKeyboardButton(text="❤️ Лайки", callback_data="admin:boost_likes"),
                InlineKeyboardButton(text="🔄 Обновить", callback_data="admin:main"),
            ],
            [
                InlineKeyboardButton(text="🎛 Режим софта", callback_data="admin:software_mode"),
            ],
            [
                InlineKeyboardButton(text="💬 Режим комментинга", callback_data="admin:commenting_mode"),
            ],
            [
                InlineKeyboardButton(text="🎚 Стратегия заказов", callback_data="admin:order_strategy"),
            ],
            [
                InlineKeyboardButton(text="◀️ Меню", callback_data="menu:main"),
            ],
        ]
    )


def get_monitor_parallel_settings_keyboard(current: int) -> InlineKeyboardMarkup:
    """Выбор параллелизма Playwright для video_monitor_worker (1–8)."""

    def label(i: int) -> str:
        return f"✓ {i}" if i == current else str(i)

    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text=label(i), callback_data=f"admin:monitoring:parallel:set:{i}")
                for i in range(1, 5)
            ],
            [
                InlineKeyboardButton(text=label(i), callback_data=f"admin:monitoring:parallel:set:{i}")
                for i in range(5, 9)
            ],
            [
                InlineKeyboardButton(text="🔄 Обновить", callback_data="admin:monitoring:parallel"),
            ],
            [
                InlineKeyboardButton(text="◀️ К мониторингу", callback_data="admin:monitoring"),
            ],
        ]
    )


def get_monitoring_menu_keyboard() -> InlineKeyboardMarkup:
    """Monitoring admin menu."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="🧠 Адаптивный", callback_data="admin:adaptive"),
                InlineKeyboardButton(text="🔄 Обновить", callback_data="admin:monitoring"),
            ],
            [
                InlineKeyboardButton(text="🖥 Параллель браузеров", callback_data="admin:monitoring:parallel"),
            ],
            [
                InlineKeyboardButton(text="🗑 Удалить всех блогеров", callback_data="admin:monitoring:delete_all"),
            ],
            [
                InlineKeyboardButton(text="◀️ Назад", callback_data="admin:main"),
            ],
        ]
    )


def get_adaptive_settings_keyboard(adaptive_enabled: bool) -> InlineKeyboardMarkup:
    """Главная панель адаптивного алгоритма — только ключевые действия."""
    toggle_text = "🔴 Отключить авто-интервалы" if adaptive_enabled else "🟢 Включить авто-интервалы"
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=toggle_text, callback_data="admin:adaptive:toggle")],
            [
                InlineKeyboardButton(text="👤 Блогеры", callback_data="admin:adaptive:bloggers"),
                InlineKeyboardButton(text="📊 Сводка", callback_data="admin:adaptive:summary"),
            ],
            [
                InlineKeyboardButton(text="📈 Отчёт самообучения", callback_data="admin:adaptive:learning"),
            ],
            [
                InlineKeyboardButton(text="🛡 Лимиты", callback_data="admin:adaptive:cat:limits"),
                InlineKeyboardButton(text="🎯 Настройки обучения", callback_data="admin:adaptive:cat:feedback"),
            ],
            [
                InlineKeyboardButton(text="🔧 Тонкая настройка", callback_data="admin:adaptive:advanced"),
            ],
            [
                InlineKeyboardButton(text="🔄 Переобучить по истории", callback_data="admin:adaptive:backfill"),
            ],
            [InlineKeyboardButton(text="◀️ Назад", callback_data="admin:monitoring")],
        ]
    )


def get_adaptive_advanced_keyboard() -> InlineKeyboardMarkup:
    """Подменю технических категорий адаптивного алгоритма."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="⚡ Когда чаще", callback_data="admin:adaptive:cat:acceleration"),
                InlineKeyboardButton(text="🐌 Когда реже", callback_data="admin:adaptive:cat:deceleration"),
            ],
            [
                InlineKeyboardButton(text="📐 Как считает ритм", callback_data="admin:adaptive:cat:base"),
                InlineKeyboardButton(text="⏰ Часы активности", callback_data="admin:adaptive:cat:hours"),
            ],
            [
                InlineKeyboardButton(text="💾 История и память", callback_data="admin:adaptive:cat:history"),
            ],
            [InlineKeyboardButton(text="◀️ К настройкам", callback_data="admin:adaptive")],
        ]
    )


def get_adaptive_category_keyboard(category: str) -> InlineKeyboardMarkup:
    """Клавиатура подменю конкретной категории настроек адаптивного алгоритма."""
    CATEGORY_BUTTONS = {
        "acceleration": [
            [
                InlineKeyboardButton(text="После видео: интервал", callback_data="admin:adaptive:edit:active_phase_interval"),
                InlineKeyboardButton(text="После видео: сколько часов", callback_data="admin:adaptive:edit:active_phase_hours"),
            ],
            [
                InlineKeyboardButton(text="Серия: видео за сутки", callback_data="admin:adaptive:edit:burst_threshold_24h"),
                InlineKeyboardButton(text="Серия: насколько чаще", callback_data="admin:adaptive:edit:burst_multiplier"),
            ],
            [
                InlineKeyboardButton(text="Серия: держать часов", callback_data="admin:adaptive:edit:burst_duration_hours"),
                InlineKeyboardButton(text="Серия: быстрый темп", callback_data="admin:adaptive:edit:use_p25_for_burst"),
            ],
            [
                InlineKeyboardButton(text="Серия: относительно обычного", callback_data="admin:adaptive:edit:burst_relative_factor"),
            ],
            [
                InlineKeyboardButton(text="Прогноз: уверенность", callback_data="admin:adaptive:edit:prediction_min_confidence"),
                InlineKeyboardButton(text="Прогноз: запас минут", callback_data="admin:adaptive:edit:prediction_buffer_minutes"),
            ],
            [
                InlineKeyboardButton(text="Прогноз: сильная уверенность", callback_data="admin:adaptive:edit:prediction_high_confidence"),
            ],
        ],
        "deceleration": [
            [
                InlineKeyboardButton(text="Когда считать тихим", callback_data="admin:adaptive:edit:silence_multiplier"),
                InlineKeyboardButton(text="Тихий: максимум реже", callback_data="admin:adaptive:edit:silence_max_factor"),
            ],
            [
                InlineKeyboardButton(text="Тихие часы: насколько реже", callback_data="admin:adaptive:edit:nighttime_multiplier"),
            ],
            [
                InlineKeyboardButton(text="Пустые проверки: сколько", callback_data="admin:adaptive:edit:empty_streak_threshold"),
                InlineKeyboardButton(text="Пустые: насколько реже", callback_data="admin:adaptive:edit:empty_streak_multiplier"),
            ],
        ],
        "base": [
            [
                InlineKeyboardButton(text="Ритм → интервал", callback_data="admin:adaptive:edit:gap_divisor"),
                InlineKeyboardButton(text="Видео для старта", callback_data="admin:adaptive:edit:min_samples"),
            ],
            [
                InlineKeyboardButton(text="Первый сбор видео", callback_data="admin:adaptive:edit:initial_limit"),
                InlineKeyboardButton(text="Вес свежих видео", callback_data="admin:adaptive:edit:recent_gap_weight"),
            ],
        ],
        "feedback": [
            [
                InlineKeyboardButton(text="Самообучение вкл/выкл", callback_data="admin:adaptive:edit:feedback_enabled"),
            ],
            [
                InlineKeyboardButton(text="Цель: поймать за N мин", callback_data="admin:adaptive:edit:feedback_target_latency_min"),
                InlineKeyboardButton(text="Скорость реакции", callback_data="admin:adaptive:edit:feedback_ema_alpha"),
            ],
            [
                InlineKeyboardButton(text="Не ускорять сильнее", callback_data="admin:adaptive:edit:feedback_min_factor"),
                InlineKeyboardButton(text="Не замедлять сильнее", callback_data="admin:adaptive:edit:feedback_max_factor"),
            ],
            [
                InlineKeyboardButton(text="Что считать опозданием", callback_data="admin:adaptive:edit:feedback_late_threshold"),
            ],
        ],
        "hours": [
            [
                InlineKeyboardButton(text="Видео для карты часов", callback_data="admin:adaptive:edit:hour_histogram_min_total"),
            ],
            [
                InlineKeyboardButton(text="Активный час", callback_data="admin:adaptive:edit:active_hour_threshold"),
                InlineKeyboardButton(text="Тихий час", callback_data="admin:adaptive:edit:dead_hour_threshold"),
            ],
            [
                InlineKeyboardButton(text="Активный час: чаще", callback_data="admin:adaptive:edit:active_hour_multiplier"),
            ],
        ],
        "limits": [
            [
                InlineKeyboardButton(text="Минимум между проверками", callback_data="admin:adaptive:edit:min_interval"),
                InlineKeyboardButton(text="Максимум между проверками", callback_data="admin:adaptive:edit:max_interval"),
            ],
            [
                InlineKeyboardButton(text="Аварийный предел", callback_data="admin:adaptive:edit:sanity_cap_factor"),
            ],
        ],
        "history": [
            [
                InlineKeyboardButton(text="Сколько видео помнить", callback_data="admin:adaptive:edit:max_keep"),
                InlineKeyboardButton(text="Сколько дней помнить", callback_data="admin:adaptive:edit:max_history_days"),
            ],
            [
                InlineKeyboardButton(text="История проверок", callback_data="admin:adaptive:edit:check_log_retention_days"),
            ],
        ],
    }
    _ADVANCED_CATS = {"acceleration", "deceleration", "base", "hours", "history"}
    if category in _ADVANCED_CATS:
        back_text, back_cb = "◀️ К тонкой настройке", "admin:adaptive:advanced"
    else:
        back_text, back_cb = "◀️ К настройкам", "admin:adaptive"
    rows = CATEGORY_BUTTONS.get(category, [])
    rows = rows + [[InlineKeyboardButton(text=back_text, callback_data=back_cb)]]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def get_adaptive_bloggers_keyboard(page: int, total_pages: int) -> InlineKeyboardMarkup:
    """Клавиатура пагинации списка блогеров."""
    nav_row = []
    if page > 0:
        nav_row.append(InlineKeyboardButton(text="◀️", callback_data=f"admin:adaptive:bloggers:page:{page - 1}"))
    nav_row.append(InlineKeyboardButton(text=f"{page + 1}/{total_pages}", callback_data=f"admin:adaptive:bloggers:page:{page}"))
    if page < total_pages - 1:
        nav_row.append(InlineKeyboardButton(text="▶️", callback_data=f"admin:adaptive:bloggers:page:{page + 1}"))
    return InlineKeyboardMarkup(
        inline_keyboard=[
            nav_row,
            [InlineKeyboardButton(text="◀️ К настройкам", callback_data="admin:adaptive")],
        ]
    )


def get_blogger_detail_keyboard(blogger_id: int, page: int, current_mode: str = "auto") -> InlineKeyboardMarkup:
    """Клавиатура карточки блогера. Активный режим помечается ✅."""
    def _btn(label: str, mode: str) -> InlineKeyboardButton:
        prefix = "✅ " if mode == current_mode else ""
        return InlineKeyboardButton(
            text=f"{prefix}{label}",
            callback_data=f"admin:adaptive:blogger:{blogger_id}:priority:{mode}:{page}",
        )

    return InlineKeyboardMarkup(
        inline_keyboard=[
            [_btn("🟢 Авто", "auto"), _btn("🔴 VIP чаще", "vip")],
            [_btn("🟡 Экономить", "lazy"), _btn("⏸️ Пауза", "paused")],
            [
                InlineKeyboardButton(text="🚀 Проверить сейчас", callback_data=f"admin:adaptive:blogger:{blogger_id}:recheck:{page}"),
                InlineKeyboardButton(text="📋 История", callback_data=f"admin:adaptive:blogger:{blogger_id}:log:{page}"),
            ],
            [
                InlineKeyboardButton(text="📊 Лог обнаружения", callback_data=f"admin:adaptive:blogger:{blogger_id}:videos:{page}"),
            ],
            [
                InlineKeyboardButton(text="⬇️ Минимум для него", callback_data=f"admin:adaptive:blogger:{blogger_id}:override_min:{page}"),
                InlineKeyboardButton(text="⬆️ Максимум для него", callback_data=f"admin:adaptive:blogger:{blogger_id}:override_max:{page}"),
            ],
            [InlineKeyboardButton(text="✏️ Новый ник", callback_data=f"admin:adaptive:blogger:{blogger_id}:rename_prompt:{page}")],
            [InlineKeyboardButton(text="🗑 Удалить блогера", callback_data=f"admin:adaptive:blogger:{blogger_id}:delete_confirm:{page}")],
            [InlineKeyboardButton(text="◀️ К списку", callback_data=f"admin:adaptive:bloggers:page:{page}")],
        ]
    )


def get_blogger_delete_confirm_keyboard(blogger_id: int, page: int) -> InlineKeyboardMarkup:
    """Клавиатура подтверждения удаления блогера."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="✅ Да, удалить", callback_data=f"admin:adaptive:blogger:{blogger_id}:delete:{page}"),
                InlineKeyboardButton(text="❌ Отмена", callback_data=f"admin:adaptive:blogger:{blogger_id}:page:{page}"),
            ],
        ]
    )


def get_accounts_menu_keyboard() -> InlineKeyboardMarkup:
    """Accounts admin menu."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="🧠 Детальный runtime", callback_data="admin:accounts:health"),
                InlineKeyboardButton(text="🔍 Проверить все", callback_data="admin:accounts:validate"),
            ],
            [
                InlineKeyboardButton(text="🌐 Назначить прокси", callback_data="admin:accounts:assign_proxies"),
                InlineKeyboardButton(text="🔄 Перепроверить мёртвые", callback_data="admin:accounts:recheck_dead"),
            ],
            [
                InlineKeyboardButton(text="🧪 Тестовый режим", callback_data="admin:accounts:test_mode"),
                InlineKeyboardButton(text="📊 Пул аккаунтов", callback_data="admin:accounts:pool_diag"),
            ],
            [
                InlineKeyboardButton(text="📋 Список", callback_data="admin:accounts:list"),
                InlineKeyboardButton(text="🔎 Поиск", callback_data="admin:accounts:search"),
            ],
            [
                InlineKeyboardButton(text="📥 Импорт", callback_data="admin:accounts:import"),
                InlineKeyboardButton(text="📤 Экспорт", callback_data="admin:accounts:export"),
            ],
            [
                InlineKeyboardButton(text="✅ Активировать все", callback_data="admin:accounts:make_all_active"),
                InlineKeyboardButton(text="🔄 Сбросить cooldown", callback_data="admin:accounts:reset_cooldowns"),
            ],
            [
                InlineKeyboardButton(text="⚙️ Ещё", callback_data="admin:accounts:more"),
                InlineKeyboardButton(text="🔄 Обновить", callback_data="admin:accounts"),
            ],
            [
                InlineKeyboardButton(text="◀️ Назад", callback_data="admin:main"),
            ],
        ]
    )


def get_import_confirm_keyboard(has_accounts_without_proxy: bool = False) -> InlineKeyboardMarkup:
    keyboard = [
        [
            InlineKeyboardButton(text="✅ Импортировать", callback_data="admin:import:confirm"),
            InlineKeyboardButton(text="🔍 Показать ошибки", callback_data="admin:import:check"),
        ]
    ]
    if has_accounts_without_proxy:
        keyboard.append(
            [InlineKeyboardButton(text="✅ Импортировать и назначить прокси", callback_data="admin:import:confirm_and_assign")]
        )
    keyboard.append([InlineKeyboardButton(text="❌ Отмена", callback_data="admin:accounts")])
    return InlineKeyboardMarkup(inline_keyboard=keyboard)


def get_proxies_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="🔍 Проверить прокси", callback_data="admin:proxies:validate"),
                InlineKeyboardButton(text="📋 Список", callback_data="admin:proxies:list"),
            ],
            [
                InlineKeyboardButton(text="📥 Импорт", callback_data="admin:proxies:import"),
                InlineKeyboardButton(text="⚙️ Режим прокси", callback_data="admin:proxy_settings"),
            ],
            [
                InlineKeyboardButton(text="🗑 Удалить все", callback_data="admin:proxies:delete_all"),
                InlineKeyboardButton(text="🔄 Обновить", callback_data="admin:proxies"),
            ],
            [
                InlineKeyboardButton(text="◀️ Назад", callback_data="admin:main"),
            ],
        ]
    )


def get_proxy_import_confirm_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="✅ Сохранить", callback_data="admin:proxies:import:confirm")],
            [InlineKeyboardButton(text="❌ Отмена", callback_data="admin:proxies")],
        ]
    )


def get_resources_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="💀 Мёртвые аккаунты", callback_data="admin:resources:dead:accounts"),
                InlineKeyboardButton(text="🗑 Удалить мёртвые", callback_data="admin:resources:clean:accounts"),
            ],
            [
                InlineKeyboardButton(text="🛰 Мониторинг", callback_data="admin:monitoring"),
                InlineKeyboardButton(text="🔑 Аккаунты", callback_data="admin:accounts"),
            ],
            [
                InlineKeyboardButton(text="◀️ Назад", callback_data="admin:main"),
            ],
        ]
    )


def get_geo_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="📊 Статистика", callback_data="admin:geo:stats"),
                InlineKeyboardButton(text="📈 Покрытие", callback_data="admin:geo:coverage"),
            ],
            [
                InlineKeyboardButton(text="💡 Рекомендации", callback_data="admin:geo:recommendation"),
                InlineKeyboardButton(text="📋 Сводка", callback_data="admin:geo:summary"),
            ],
            [
                InlineKeyboardButton(text="🌍 Определить гео", callback_data="admin:geo:detect"),
                InlineKeyboardButton(text="⚙️ Настройки", callback_data="admin:geo:settings"),
            ],
            [
                InlineKeyboardButton(text="🔄 Обновить", callback_data="admin:geo"),
                InlineKeyboardButton(text="◀️ Назад", callback_data="admin:main"),
            ],
        ]
    )


def get_proxy_list_keyboard(page: int, has_prev: bool, has_next: bool) -> InlineKeyboardMarkup:
    """Пагинация списка прокси."""
    nav_row = []
    if has_prev:
        nav_row.append(InlineKeyboardButton(text="◀️", callback_data=f"admin:proxies:list:page:{page - 1}"))
    nav_row.append(InlineKeyboardButton(text=f"стр. {page + 1}", callback_data="admin:proxies:list:noop"))
    if has_next:
        nav_row.append(InlineKeyboardButton(text="▶️", callback_data=f"admin:proxies:list:page:{page + 1}"))
    return InlineKeyboardMarkup(
        inline_keyboard=[
            nav_row,
            [InlineKeyboardButton(text="◀️ К прокси", callback_data="admin:proxies")],
        ]
    )
