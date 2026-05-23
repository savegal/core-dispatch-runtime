# Рекомендации по оптимизации скорости и качества

## Для 500 заказов/день на сервере

### 1. Параллелизм (КРИТИЧНО)

**Текущие настройки (для ноутбука):**
```python
MAX_THREADS = 2
MAX_CONCURRENT_BROWSERS = 1
ACCOUNT_COOLDOWN_SECONDS = 60
```

**Рекомендации для сервера (в `.env`):**
```env
# Для сервера с 8-16GB RAM:
MAX_THREADS=10
MAX_CONCURRENT_BROWSERS=6
ACCOUNT_COOLDOWN_SECONDS=30

# Для мощного сервера (16-32GB RAM):
MAX_THREADS=15
MAX_CONCURRENT_BROWSERS=10
ACCOUNT_COOLDOWN_SECONDS=20
```

**Расчёт:**
- 500 заказов/день = ~21 заказ/час
- С 10 потоками × 6 браузерами = ~30-50 заказов/час (хватит с запасом)
- Каждый заказ: ~2-3 минуты (коммент + верификация + ответы + Boost)
- Параллельно можно обрабатывать 10-15 задач

---

### 2. Оптимизация warmup (ускорение HTTP запросов)

**Проблема:** Прогрев перед каждым HTTP запросом занимает 1-2 сек + проверки.

**Решение:** Уменьшить задержки и убрать лишние проверки.

**В `executor/engagement_engine.py` → `warmup_page_for_http()`:**
- Уменьшить `sleep_time` с `1.0-2.0` до `0.5-1.0` сек
- Убрать проверку элемента видео (не критично, только замедляет)
- Проверять капчу сразу после `goto()` (до warmup)

---

### 3. Быстрый детект и пропуск капчи

**Текущая проблема:** Капча детектируется, но аккаунт не пропускается сразу.

**Решение:** Добавить проверку капчи сразу после навигации и быстрый пропуск аккаунта.

**В `executor/tiktok_client_async.py` → `post_comment()`:**
```python
# После page.goto(), ДО любых действий:
from executor.captcha_detector import detect_captcha
has_captcha, captcha_type = await detect_captcha(page, wait_ms=500)
if has_captcha:
    logger.warning(f"Task {task_id}: CAPTCHA detected for account {account.id}, skipping")
    # Помечаем аккаунт как error с причиной "captcha"
    from services.account_service import mark_soft_error
    mark_soft_error(db, account.id, f"captcha_detected:{captcha_type}")
    return CommentResult.error(
        error_type="captcha",
        details=f"CAPTCHA detected: {captcha_type}",
        fatal_for_account=True
    )
```

**В `executor/engagement_engine.py` → `execute_replies_http()`:**
- Проверять капчу перед каждым reply (если появилась — пропускать аккаунт)

---

### 4. Уменьшение задержек (для скорости)

**В `config/settings.py`:**

```python
# Было → Стало (для скорости):
BRANCH_VERIFY_DELAY_MIN = 3 → 2  # Быстрее верификация
BRANCH_VERIFY_DELAY_MAX = 8 → 5  # Быстрее верификация
PAGE_LOAD_WAIT_PROXY_MS = 800 → 500  # Меньше ожидание
PAGE_LOAD_WAIT_DIRECT_MS = 500 → 300  # Меньше ожидание
ACCOUNT_COOLDOWN_SECONDS = 60 → 30  # Быстрее повторное использование
```

**Эффект:** Каждый заказ станет на 3-5 секунд быстрее. При 500 заказах/день = экономия ~25-40 минут общего времени.

---

### 5. Мониторинг успешности

**Добавить метрики (в `services/metrics.py` или отдельный модуль):**

```python
# Процент успешных комментариев по аккаунтам
record_counter("account.success_rate", account_id=account.id, success=True)

# Процент капчи по аккаунтам
record_counter("account.captcha_rate", account_id=account.id)

# Время выполнения задачи
record_timing("task.duration_ms", duration_ms, task_id=task_id)
```

**Использование:** Отслеживать аккаунты с низким успехом (<70%) и временно исключать их.

---

### 6. Оптимизация выбора аккаунтов

**Текущая проблема:** Аккаунты выбираются последовательно, могут быть задержки.

**Решение:** 
- Кэшировать список "свободных" аккаунтов на 10-30 сек
- Использовать `SELECT FOR UPDATE` только при реальном выборе (уже есть)
- Приоритизировать аккаунты с меньшим `error_count_recent` (уже есть)

---

### 7. Оптимизация прокси

**Текущие настройки:**
```python
MAX_TASKS_PER_PROXY = 1  # Оставить (безопасность)
```

**Рекомендация:** Оставить `MAX_TASKS_PER_PROXY=1` для мобильных прокси (безопасность важнее скорости). Для резидентских можно увеличить до 2-3, но риск выше.

---

## Итоговые настройки для сервера (`.env`):

```env
# Параллелизм
MAX_THREADS=10
MAX_CONCURRENT_BROWSERS=6
ACCOUNT_COOLDOWN_SECONDS=30

# Скорость
BRANCH_VERIFY_DELAY_MIN=2
BRANCH_VERIFY_DELAY_MAX=5
PAGE_LOAD_WAIT_PROXY_MS=500
PAGE_LOAD_WAIT_DIRECT_MS=300

# Безопасность (оставить)
MAX_TASKS_PER_PROXY=1
VALIDATE_BEFORE_TASK=True
```

---

## Ожидаемый результат:

- **Скорость:** 30-50 заказов/час (вместо 10-15)
- **Качество:** Быстрый пропуск аккаунтов с капчей → меньше банов
- **Стабильность:** Мониторинг успешности → автоматическое исключение проблемных аккаунтов

---

## Приоритет внедрения:

1. **Высокий:** Параллелизм (MAX_THREADS, MAX_CONCURRENT_BROWSERS) — сразу даст 3-5x скорость
2. **Высокий:** Детект капчи с пропуском аккаунта — защита от банов
3. **Средний:** Уменьшение задержек — дополнительное ускорение
4. **Низкий:** Мониторинг — для долгосрочной оптимизации
