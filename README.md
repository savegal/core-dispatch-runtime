# TikTok Comment Service

Приватный сервис для автоматизации комментариев в TikTok через Telegram-бота.

## 🚀 Быстрый старт

**Новичок?** Начните с [QUICK_START.md](QUICK_START.md) (5 минут)

**Нужна подробная инструкция?** Читайте [LAUNCH_GUIDE.md](LAUNCH_GUIDE.md)

### Кратко:

1. Создайте `.env` файл (см. пример ниже)
2. Установите зависимости: `pip install -r requirements.txt`
3. Установите Playwright браузер: `python -m playwright install chromium`
4. Установите и запустите Redis
5. Создайте БД: `python db/create_all.py`
6. Запустите в 4 терминалах (Executor теперь отдельный процесс):
   - `uvicorn main:app --reload` (Backend)
   - `python -m bot.app` (Bot)
   - `python -m executor.executor` (Executor — отдельный процесс, НЕ внутри backend)
   - `python -m services.video_monitor_worker` (Video Monitor - опционально)
   
   ⚠️ **ВАЖНО:** Executor запускается **отдельно** и публикует heartbeat в Redis. Backend его не стартует.
   
   ⚠️ **Video Monitor:** Воркер мониторинга видео работает отдельно и не мешает executor. Можно запустить позже или через systemd.
   
   📖 **Подробная инструкция:** См. [STARTUP_GUIDE.md](STARTUP_GUIDE.md) для полного руководства по запуску всех сервисов.

### Минимальный `.env`:

```env
DEMO_MODE=1
DB_URL=sqlite:///./dev.db
REDIS_URL=redis://localhost:6379/0
BOT_TOKEN=ваш_токен_от_BotFather
OWNER_TELEGRAM_ID=ваш_telegram_id
API_BASE=http://localhost:8000
EXECUTOR_API_TOKEN=secret-token
JWT_SECRET=secret-key

# Мониторинг ошибок (опционально, но рекомендуется)
# SENTRY_DSN=https://your-sentry-dsn@sentry.io/project-id

# Прокси для Telegram (опционально, если Telegram заблокирован)
# TELEGRAM_PROXY=http://user:pass@proxy:8080
```

### Получение токена бота:

1. Найдите [@BotFather](https://t.me/BotFather) в Telegram
2. Отправьте `/newbot` и следуйте инструкциям
3. Скопируйте токен в `.env`

### Получение Telegram ID:

1. Найдите [@userinfobot](https://t.me/userinfobot) в Telegram
2. Отправьте `/start`
3. Скопируйте ваш ID в `.env`

## Структура проекта

- `/config` - настройки приложения
- `/db` - модели БД и миграции
- `/api` - FastAPI роутеры
- `/services` - бизнес-логика
- `/executor` - обработчик задач
- `/bot` - Telegram-бот

## Особенности

- Максимум 50 основных комментариев в день на аккаунт
- Ротация аккаунтов с cooldown
- Удобный композер заказов через Telegram
- **Умный импорт аккаунтов** с автоматическим распознаванием форматов
- **Автоматическое распределение прокси** из пула активных прокси
- **Мониторинг ресурсов** с уведомлениями владельцу
- **🎬 Парсинг видео (мониторинг блогеров TikTok)** - автоматический поиск новых видео и создание заказов
- Управление подписками и доступом
- Автоматизация через Playwright (реальная работа с TikTok)
- **Тексты комментариев:** минимум 3 варианта на заказ; при массовой тени по тексту оставшиеся URL останавливаются, требуется новый текст.
- **Верификация:** успех фиксируется только при сетевом подтверждении с реальным comment_id и публичной проверке; псевдо-ID не считается успешным.

## Работа с TikTok через Playwright

Проект использует Playwright для автоматизации работы с TikTok. `tiktok_client` напрямую взаимодействует с веб-интерфейсом TikTok через браузер Chromium.

### Установка Playwright

После установки зависимостей (`pip install -r requirements.txt`) установите браузер:

```bash
python -m playwright install chromium
```

### Формат cookies для аккаунтов

Cookies аккаунтов хранятся в базе данных (поле `cookies`) в формате JSON. Поддерживаются два формата:

**Формат 1: JSON-массив объектов**
```json
[
  {
    "name": "sessionid",
    "value": "abc123...",
    "domain": ".tiktok.com",
    "path": "/",
    "httpOnly": true,
    "secure": true
  },
  {
    "name": "sid_tt",
    "value": "xyz789...",
    "domain": ".tiktok.com",
    "path": "/"
  }
]
```

**Формат 2: JSON-объект (ключ-значение)**
```json
{
  "sessionid": "abc123...",
  "sid_tt": "xyz789...",
  "uid_tt": "123456"
}
```

При использовании формата 2 система автоматически преобразует его в массив cookie-объектов с дефолтными значениями для `domain`, `path`, `httpOnly`, `secure`.

### Настройки Playwright в `.env`

```env
PLAYWRIGHT_HEADLESS=True      # Запуск браузера в headless режиме (True/False)
PLAYWRIGHT_TIMEOUT=60000      # Таймаут ожидания элементов (в миллисекундах)
TIKTOK_BASE_URL=https://www.tiktok.com
```

### Работа с прокси

Если у аккаунта указан `proxy` в БД, Playwright будет использовать его при работе:

- Формат: `login:pass@ip:port` или `ip:port`
- Пример: `user123:pass456@proxy.example.com:8080`

### Принцип работы

1. Executor получает задачу из очереди
2. Выбирает свободный аккаунт с учётом лимитов
3. `tiktok_client` запускает браузер Chromium через Playwright
4. Загружает cookies аккаунта в контекст браузера
5. Открывает страницу TikTok и выполняет действие (комментарий/ответ/лайк)
6. Закрывает браузер и возвращает результат

### Отладка

Для отладки можно отключить headless режим:

```env
PLAYWRIGHT_HEADLESS=False
```

Браузер будет открываться визуально, что позволяет видеть процесс работы.

## Управление аккаунтами и прокси

### Импорт аккаунтов

Бот поддерживает гибкий импорт аккаунтов из файла:

**Поддерживаемые форматы:**
- `label;cookies;user_agent;proxy` - полный формат
- `label;cookie_file.txt;user_agent;proxy` - с файлом cookies
- `cookies;proxy` - минимальный формат
- `cookies` - только cookies (label генерируется автоматически)

**Разделители:** `;`, `,`, `|`, табуляция, множественные пробелы

**Cookies:** JSON-строка `{"sessionid":"...","sid_tt":"..."}` или inline формат

После импорта система автоматически предложит раздать прокси из пула, если у аккаунтов нет прокси.

### Автоматическое распределение прокси

1. **Через админ-меню**: Кнопка "🔄 Распределить прокси" в разделе "Аккаунты"
2. **При импорте**: Автоматическое предложение или кнопка "Раздать прокси сразу"
3. **Через API**: `POST /api/accounts/assign_proxies` (требует админ-токен)

Система автоматически:
- Выбирает аккаунты без прокси
- Распределяет свободные активные прокси из пула
- Показывает статистику распределения

### Мониторинг ресурсов

Для автоматических уведомлений о недостатке ресурсов запустите:

```bash
python -m services.monitoring_task
```

Скрипт будет:
- Проверять количество активных аккаунтов и прокси каждые 5 минут
- Сравнивать с порогами `MIN_ACTIVE_ACCOUNTS` и `MIN_ACTIVE_PROXIES`
- Отправлять уведомления владельцу (не чаще раза в час)

Настройки в `.env`:
```env
MIN_ACTIVE_ACCOUNTS=50
MIN_ACTIVE_PROXIES=20
OWNER_TELEGRAM_ID=ваш_telegram_id
```

