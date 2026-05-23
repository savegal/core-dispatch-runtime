# 🎬 Парсинг видео (мониторинг блогеров TikTok)

Модуль для автоматического мониторинга TikTok блогеров и парсинга новых видео.

## Функциональность

- **Мониторинг блогеров**: Автоматическая проверка профилей блогеров каждые 30 минут (настраивается)
- **Три режима работы**:
  - 🤖 **auto_comment**: Автоматически создаёт задачи при обнаружении новых видео
  - 👀 **manual_review**: Отправляет видео на проверку работнику/админу
  - 🔔 **notify_only**: Только уведомляет о новых видео
- **Дедупликация**: Не создаёт дубли видео
- **Управление через бота**: Полное управление через Telegram-бот
- **Отдельный воркер**: Не мешает executor, работает независимо

## Установка и настройка

### 1. Создание таблиц БД

Модуль использует новые таблицы `creator_sources` и `creator_videos`. Они создаются автоматически при запуске `python db/create_all.py` или при старте FastAPI (через `Base.metadata.create_all`).

Если база уже создана, можно запустить:
```bash
python db/create_all.py
```

### 2. Запуск воркера

#### Ручной запуск (для тестирования)

```bash
python -m services.video_monitor_worker
```

#### Systemd (для production)

1. Скопируйте unit файл:
```bash
sudo cp systemd/tiktok-video-monitor.service /etc/systemd/system/
```

2. Отредактируйте пути и пользователя в файле:
```bash
sudo nano /etc/systemd/system/tiktok-video-monitor.service
```

Измените:
- `User=teletan` → ваш пользователь
- `Group=teletan` → ваша группа
- `WorkingDirectory=/opt/TeletanSoft` → путь к проекту
- `ExecStart=/opt/TeletanSoft/venv/bin/python` → путь к Python

3. Запустите сервис:
```bash
sudo systemctl daemon-reload
sudo systemctl enable tiktok-video-monitor.service
sudo systemctl start tiktok-video-monitor.service
```

4. Проверьте статус:
```bash
sudo systemctl status tiktok-video-monitor.service
sudo journalctl -u tiktok-video-monitor.service -f
```

## Использование через бота

1. Откройте админ-меню в боте
2. Нажмите "🎬 Парсинг видео"
3. Добавьте блогера через "➕ Добавить блогера"
4. Настройте режим работы, интервал проверки, шаблоны
5. Включите мониторинг

### Управление блогером

- ⏯ **Включить/Выключить**: Временно отключает мониторинг
- ✏️ **Режим**: Выбор режима работы (auto_comment/manual_review/notify_only)
- ⏱ **Интервал**: Настройка интервала проверки (в минутах)
- 🧩 **Шаблоны**: Привязка шаблонов комментариев к блогеру
- 🧪 **Запуск**: Немедленная проверка блогера (не ждёт интервала)
- 🗑 **Удалить**: Удаление блогера

### Очередь на проверку

Если выбран режим `manual_review`, новые видео попадают в очередь на проверку:
- Просмотр очереди: "🧾 Очередь на проверку"
- ✅ **Комментить**: Создаёт задачу и добавляет в очередь executor
- ❌ **Пропустить**: Помечает видео как отклонённое

## API Endpoints

Модуль также предоставляет REST API для управления:

- `GET /api/video-parsing/creators` - Список блогеров
- `POST /api/video-parsing/creators` - Создание блогера
- `PATCH /api/video-parsing/creators/{id}` - Обновление настроек
- `DELETE /api/video-parsing/creators/{id}` - Удаление блогера
- `POST /api/video-parsing/creators/{id}/run-now` - Запуск проверки
- `GET /api/video-parsing/review-queue` - Очередь на проверку
- `POST /api/video-parsing/review/{video_id}/approve` - Подтверждение видео
- `POST /api/video-parsing/review/{video_id}/reject` - Отклонение видео
- `GET /api/video-parsing/videos` - Список видео с фильтрацией

## Алгоритм работы

1. **Воркер каждые 60 секунд** проверяет список блогеров (`CHECK_TICK = 60`)
2. **Для каждого блогера, у которого прошёл интервал проверки:**
   - Выбирает аккаунт для мониторинга через `get_monitor_account()` (приоритет аккаунтам без комментов)
   - Открывает страницу профиля `https://www.tiktok.com/@{username}` через Playwright
   - **Парсит видео используя множественные методы:**
     - Перехватывает network requests от TikTok API (`/api/user/detail`, `/api/post/item_list`, и т.д.)
     - Извлекает `aweme_id` из JSON ответов API
     - Ищет ссылки в DOM через 6 разных методов (селекторы, data-атрибуты, JSON данные)
     - Агрессивный скроллинг для загрузки lazy content (8 скроллов)
   - Извлекает `video_id` из URL (формат: `https://www.tiktok.com/@{username}/video/{video_id}`)
   - Фильтрует видео новее `last_seen_video_id` (оптимизация - не парсит старые)
   - Сохраняет новые видео в БД (`CreatorVideo`) с дедупликацией по `(creator_id, video_id)`
   - **Раздаёт видео всем активным подпискам** (`CreatorSource`) на этого блогера
   - По режиму каждой подписки:
     - `auto_comment`: Создаёт Task и добавляет в очередь executor
     - `manual_review`: Помечает как `pending_review` и уведомляет
     - `notify_only`: Только уведомляет о новых видео
3. Обновляет `last_checked_at` и `last_seen_video_id` у блогера

## Требования

- Аккаунты с валидными cookies для мониторинга
- Запущенный Redis (для очереди задач)
- Запущенный executor (для выполнения задач, если используется auto_comment)

## Логи

Воркер логирует все операции:
```bash
# Просмотр логов (systemd)
sudo journalctl -u tiktok-video-monitor.service -f

# Или через Python
tail -f logs/video_monitor.log  # если настроен файловый логгер
```

Примеры логов:
```
[VIDEO_MONITOR] Creator username: found 3 new videos
[VIDEO_MONITOR] Creator username: created 3 tasks from 3 videos
[VIDEO_MONITOR] Processing creator: @username (account_id=5)
```

## Устранение проблем

### Воркер не запускается

1. Проверьте что БД создана: `python db/create_all.py`
2. Проверьте что Redis запущен: `redis-cli ping`
3. Проверьте логи: `sudo journalctl -u tiktok-video-monitor.service -n 50`

### Нет аккаунтов для мониторинга

Воркер выберет аккаунт автоматически из доступных. Если нет доступных аккаунтов:
- Проверьте что есть аккаунты со статусом `active` или `error` (с низким `error_count_recent`)
- Убедитесь что у аккаунтов есть валидные cookies

### Видео не находятся

- Проверьте что username правильный (без @)
- Проверьте что профиль доступен публично
- Проверьте логи воркера на ошибки парсинга
- **Проверьте сохранённые HTML файлы** в `diagnostics/` - парсер автоматически сохраняет HTML при проблемах
- Парсер использует **6 методов поиска видео** + перехват API запросов, но TikTok может блокировать или изменить структуру
- Убедитесь что аккаунт для мониторинга имеет валидные cookies и не заблокирован

### Задачи не создаются (auto_comment)

- Проверьте что у блогера настроены `template_ids`
- Проверьте что шаблоны существуют в БД
- Проверьте что есть системный пользователь (owner) в БД
- Проверьте логи воркера на ошибки

## Производительность

- Параллелизм: до `MAX_CREATORS_PER_TICK` блогеров за раз (по умолчанию 1, можно увеличить)
- Интервал проверки: каждые 60 секунд проверка due блогеров
- Лимит видео: максимум `VIDEOS_LIMIT = 12` видео за парсинг
- Дедупликация: по `(creator_id, video_id)` - уникальный constraint в БД
- Lease: 3 минуты для предотвращения double-check
- Оптимизация: парсер останавливается при достижении `last_seen_video_id` (не парсит старые видео)

## Безопасность

- Воркер не использует аккаунты для комментирования (только просмотр профилей)
- Приоритет аккаунтам без комментов (меньше нагрузка на аккаунты)
- Ограничения ресурсов через systemd (MemoryLimit, CPUQuota)
