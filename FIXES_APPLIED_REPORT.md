# 🔧 Отчет об исправлениях: "Мои заказы" + Парсинг видео + Закрепы

**Дата:** 2025-01-XX  
**Основание:** Анализ ChatGPT о проблемах в проекте  

---

## ✅ Исправленные проблемы

### 1. **Список заказов - добавлены origin, стоимость, количество видео**

**Файл:** `bot/handlers/orders.py`

**Изменения:**
- Обновлена функция `format_order_line()` для отображения:
  - **Origin** с эмодзи (🤖 autopilot, ✍️ manual, 🧙 wizard, 📝 composer, 📥 new_videos, 🔔 notification)
  - **Стоимость** (HOLD для pending/in_progress, итоговая для done, возвраты для failed)
  - **Количество видео** (уже было, проверено)

**Пример отображения:**
```
✅ #1842 • 4/4 • 19:10 • 🤖 autopilot • 💸 20.00
🟡 #1841 • 2/5 • 19:02 • 📥 new_videos • 💸 HOLD 25.00
🔴 #1840 • 0/3 • 18:51 • ✍️ manual • 💸 15.00 → REF 9.00
```

---

### 2. **DEMO_MODE исправлен (default=False, управляется через .env)**

**Файл:** `config/settings.py`

**Изменения:**
- `DEMO_MODE: bool = False` (было `True`)
- Добавлен комментарий о том, что управляется через `.env`

**Важно:** Теперь по умолчанию реальные списания. Для тестов нужно явно установить `DEMO_MODE=true` в `.env`.

---

### 3. **Autopilot: добавлен origin="autopilot" и учет DEMO_MODE**

**Файл:** `services/video_monitor_worker.py`

**Изменения в `auto_create_tasks()`:**

1. **Добавлен origin в settings:**
```python
task_settings["origin"] = "autopilot"
task_settings["creator_username"] = creator.username
task_settings["creator_source_id"] = creator.id
task_settings["created_from"] = "monitor_worker"
```

2. **Добавлен учет DEMO_MODE:**
```python
if settings.DEMO_MODE:
    total_cost = Decimal("0.00")
    logger.info(f"[AUTOPILOT] DEMO_MODE enabled, skipping balance deduction")
else:
    # Проверка баланса и списание только если не DEMO_MODE
    ...
```

**Результат:**
- Autopilot заказы теперь имеют `origin="autopilot"` в списке заказов
- DEMO_MODE работает одинаково для ручных и автозаказов
- Метаданные блогера сохраняются в settings для отладки

---

### 4. **text_variants исправлен (минимум 3 варианта)**

**Файл:** `bot/services/api_client.py`

**Изменения в `create_task()`:**

```python
# REFACTOR: text_variants должно быть минимум 3 (API требует)
if text_variants:
    if len(text_variants) < 3:
        text_variants = text_variants + [text_variants[0]] * (3 - len(text_variants))
else:
    text_variants = [main_comment, main_comment, main_comment]
```

**Результат:**
- Всегда отправляется минимум 3 варианта текста
- API не будет возвращать 422 ошибку из-за недостаточного количества вариантов

---

### 5. **Критический баг с закрепами исправлен**

**Файл:** `services/video_parser.py`

**Проблема:**
- Anchor-stop срабатывал на pinned видео → новые видео ниже не смотрелись
- Если `last_seen_video_id` случайно стал закрепом → система "слепнет"

**Исправления:**

1. **Anchor-stop НЕ срабатывает на pinned видео:**
```python
if stop_at_video_id and video_id == stop_at_video_id:
    if is_pinned:
        # REFACTOR: Anchor оказался pinned - игнорируем его, продолжаем скан
        logger.warning(f"Anchor {video_id} is pinned! Ignoring anchor-stop, continuing scan")
        # НЕ делаем break, продолжаем искать новые видео ниже
    else:
        # Anchor найден и он normal - останавливаемся
        anchor_found = True
        break
```

2. **Safety check для топ-3:**
```python
# Дополнительная защита - если anchor в топ-3 и не pinned
if stop_at_video_id and idx < 3 and video_id == stop_at_video_id and not is_pinned:
    anchor_found = True
    break
```

3. **RESYNC теперь отправляет top-N видео в Inbox:**
```python
# REFACTOR: Если anchor не найден, отправляем top-N видео в Inbox
if stop_at_video_id and not anchor_found:
    if newest_normal_video_id:
        return ScanResult(
            status="RESYNC",
            new_videos=out[:limit] if out else [],  # Отправляем видео в Inbox
            newest_normal_video_id=newest_normal_video_id,
            ...
        )
```

**Результат:**
- Закрепы больше не блокируют поиск новых видео
- Если anchor не найден → отправляются top-N видео в Inbox (не теряются)
- Anchor обновляется на `newest_normal_video_id` (не pinned)

---

### 6. **Воркер обновлен для обработки RESYNC с видео**

**Файл:** `services/video_monitor_worker.py`

**Изменения:**
- При RESYNC статусе воркер теперь обрабатывает `scan_result.new_videos` (не выходит сразу)
- Anchor обновляется на `newest_normal_video_id`
- Видео отправляются в Inbox для обработки

---

## 📋 Проверка origin во всех местах создания заказов

| Место создания | Origin | Статус |
|----------------|--------|--------|
| `bot/handlers/composer.py` | `"composer"` | ✅ |
| `bot/handlers/order_wizard.py` | `"wizard"` | ✅ |
| `bot/handlers/video_parsing_new_videos.py` | `"new_videos"` | ✅ |
| `bot/handlers/quick_order.py` | `"manual"` | ✅ |
| `services/video_monitor_worker.py` | `"autopilot"` | ✅ (исправлено) |
| `api/tasks.py` (default) | `"manual"` | ✅ |

---

## 🧪 Инструкции по тестированию

### Тест 1: Проверка origin в списке заказов

```
1. Создать заказ через Composer → проверить список заказов
2. Создать заказ через Wizard → проверить список заказов
3. Создать заказ из новых видео → проверить список заказов
4. Дождаться автопилота → проверить список заказов
5. ✅ Ожидаемый результат: В списке видно origin (📝 composer, 🧙 wizard, 📥 new_videos, 🤖 autopilot)
```

### Тест 2: Проверка стоимости в списке заказов

```
1. Создать заказ (не DEMO_MODE)
2. Открыть список заказов
3. ✅ Ожидаемый результат:
   - Для pending/in_progress: "💸 HOLD X.XX"
   - Для done: "💸 X.XX"
   - Для failed: "💸 X.XX → REF Y.YY"
```

### Тест 3: Проверка DEMO_MODE

```
1. Установить DEMO_MODE=true в .env
2. Создать заказ
3. Проверить баланс - не должен измениться
4. ✅ Ожидаемый результат: Баланс не списывается, заказ создается с total_cost=0.00
```

### Тест 4: Проверка бага с закрепами

```
1. Найти блогера с закрепленным видео
2. Убедиться что last_seen_video_id = ID закрепленного видео
3. Дождаться следующей проверки воркером
4. ✅ Ожидаемый результат:
   - Воркер находит новые видео ниже закрепа
   - Anchor обновляется на newest_normal_video_id (не pinned)
   - Новые видео появляются в Inbox
```

### Тест 5: Проверка RESYNC (слишком много новых видео)

```
1. Найти блогера, у которого anchor не найден (слишком много новых видео)
2. Дождаться проверки воркером
3. ✅ Ожидаемый результат:
   - Воркер отправляет top-N видео в Inbox
   - Anchor обновляется на newest_normal_video_id
   - Видео не теряются
```

---

## 📁 Измененные файлы

1. `bot/handlers/orders.py` - Обновлен `format_order_line()` для отображения origin, стоимости
2. `config/settings.py` - `DEMO_MODE = False` (было `True`)
3. `bot/services/api_client.py` - Исправлен `text_variants` (минимум 3)
4. `services/video_monitor_worker.py` - Добавлен origin="autopilot", учет DEMO_MODE
5. `services/video_parser.py` - Исправлен баг с закрепами (anchor-stop на pinned)

---

## ✅ Итоговый статус

- [x] Список заказов показывает origin, стоимость, количество видео
- [x] DEMO_MODE исправлен (default=False)
- [x] Autopilot создает заказы с origin="autopilot"
- [x] Autopilot учитывает DEMO_MODE
- [x] text_variants всегда минимум 3
- [x] Баг с закрепами исправлен (anchor-stop не срабатывает на pinned)
- [x] RESYNC отправляет top-N видео в Inbox (не теряет их)

---

**Все критические проблемы исправлены!**
