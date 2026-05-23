# 🚀 Полное руководство по запуску TeletanSoft

## 📋 Содержание

1. [Подготовка окружения](#подготовка-окружения)
2. [Настройка базы данных](#настройка-базы-данных)
3. [Запуск сервисов (ручной режим)](#запуск-сервисов-ручной-режим)
4. [Запуск через systemd (production)](#запуск-через-systemd-production)
5. [Проверка работоспособности](#проверка-работоспособности)
6. [Устранение проблем](#устранение-проблем)

---

## 📦 Подготовка окружения

### 1. Установка зависимостей

```bash
# Перейдите в директорию проекта
cd /path/to/TeletanSoft

# Создайте виртуальное окружение (если ещё не создано)
python3 -m venv venv

# Активируйте виртуальное окружение
# Linux/Mac:
source venv/bin/activate
# Windows:
venv\Scripts\activate

# Установите зависимости
pip install -r requirements.txt

# Установите Playwright браузер
python -m playwright install chromium
```

### 2. Настройка `.env` файла

Создайте файл `.env` в корне проекта (если его ещё нет):

```env
# === ОСНОВНЫЕ НАСТРОЙКИ ===
ENV=development  # или production
DEBUG=True  # False для production

# === БАЗА ДАННЫХ ===
DB_URL=sqlite:///./dev.db
# Или для PostgreSQL:
# DB_URL=postgresql://user:password@localhost:5432/tiktok_db

# === REDIS ===
REDIS_URL=redis://localhost:6379/0
REDIS_TASK_QUEUE=queue:tasks

# === TELEGRAM БОТ ===
BOT_TOKEN=ваш_токен_от_BotFather
OWNER_TELEGRAM_ID=ваш_telegram_id

# === API НАСТРОЙКИ ===
API_BASE=http://localhost:8000
EXECUTOR_API_TOKEN=secret-token-change-me
JWT_SECRET=secret-key-change-me
JWT_ALG=HS256

# === PLAYWRIGHT ===
PLAYWRIGHT_HEADLESS=True
PLAYWRIGHT_TIMEOUT=60000
TIKTOK_BASE_URL=https://www.tiktok.com

# === МОНИТОРИНГ РЕСУРСОВ (опционально) ===
MIN_ACTIVE_ACCOUNTS=50
MIN_ACTIVE_PROXIES=20

# === SENTRY (опционально, но рекомендуется) ===
# SENTRY_DSN=https://your-sentry-dsn@sentry.io/project-id

# === ПРОКСИ ДЛЯ TELEGRAM (опционально) ===
# TELEGRAM_PROXY=http://user:pass@proxy:8080
```

**⚠️ ВАЖНО:** Замените:
- `ваш_токен_от_BotFather` - токен вашего Telegram бота
- `ваш_telegram_id` - ваш Telegram ID (можно получить у [@userinfobot](https://t.me/userinfobot))
- `secret-token-change-me` и `secret-key-change-me` - на свои секретные значения

### 3. Установка и запуск Redis

**Linux/Mac:**
```bash
# Ubuntu/Debian:
sudo apt-get install redis-server
sudo systemctl start redis-server
sudo systemctl enable redis-server

# Mac (Homebrew):
brew install redis
brew services start redis

# Проверка:
redis-cli ping
# Должно ответить: PONG
```

**Windows:**
```bash
# Скачайте Redis для Windows: https://github.com/microsoftarchive/redis/releases
# Или используйте WSL (Windows Subsystem for Linux)

# Проверка:
redis-cli ping
# Должно ответить: PONG
```

---

## 🗄️ Настройка базы данных

### 1. Создание таблиц

Запустите скрипт создания таблиц (включая новые таблицы для парсинга видео):

```bash
python db/create_all.py
```

Это создаст все необходимые таблицы:
- ✅ `users` - пользователи
- ✅ `accounts` - аккаунты TikTok
- ✅ `tasks` - задачи на комментирование
- ✅ `creator_sources` - блогеры для мониторинга (новое)
- ✅ `creator_videos` - найденные видео (новое)
- ✅ И другие таблицы...

### 2. Проверка создания таблиц

```bash
# Для SQLite:
sqlite3 dev.db ".tables"

# Должны быть видны таблицы:
# accounts, creator_sources, creator_videos, tasks, users, ...
```

### 3. Создание первого админа (если нужно)

Через бота или API создайте первого пользователя с ролью `admin` или `owner`.

---

## 🚀 Запуск сервисов (ручной режим)

### Для разработки и тестирования

Откройте **4 отдельных терминала** и запустите в каждом:

#### Терминал 1: Backend (FastAPI)

```bash
# Активируйте виртуальное окружение
source venv/bin/activate  # Linux/Mac
# или
venv\Scripts\activate  # Windows

# Запустите Backend
uvicorn main:app --reload --host 0.0.0.0 --port 8000
```

**Ожидаемый вывод:**
```
INFO:     Uvicorn running on http://0.0.0.0:8000 (Press CTRL+C to quit)
INFO:     Started reloader process
INFO:     Started server process
INFO:     Waiting for application startup.
INFO:     Application startup complete.
```

**Проверка:** Откройте http://localhost:8000/docs - должен открыться Swagger UI.

---

#### Терминал 2: Telegram Bot

```bash
# Активируйте виртуальное окружение
source venv/bin/activate  # Linux/Mac
# или
venv\Scripts\activate  # Windows

# Запустите бота
python -m bot.app
```

**Ожидаемый вывод:**
```
INFO:     Bot started successfully
INFO:     Polling started
```

**Проверка:** Откройте вашего бота в Telegram и отправьте `/start`.

---

#### Терминал 3: Executor (обработчик задач)

```bash
# Активируйте виртуальное окружение
source venv/bin/activate  # Linux/Mac
# или
venv\Scripts\activate  # Windows

# Запустите Executor
python -m executor.executor
```

**Ожидаемый вывод:**
```
INFO:     Executor started
INFO:     Waiting for tasks...
```

**⚠️ ВАЖНО:** Executor должен запускаться **отдельно**, не внутри бота. Это предотвращает блокировку event loop бота.

---

#### Терминал 4: Video Monitor (воркер мониторинга видео) ⭐ НОВОЕ

```bash
# Активируйте виртуальное окружение
source venv/bin/activate  # Linux/Mac
# или
venv\Scripts\activate  # Windows

# Запустите Video Monitor
python -m services.video_monitor_worker
```

**Ожидаемый вывод:**
```
INFO:     [VIDEO_MONITOR] Starting video monitor worker...
INFO:     [VIDEO_MONITOR] Starting video monitor worker...
```

**⚠️ ПРИМЕЧАНИЕ:** Video Monitor опционален - можно запустить позже или через systemd. Он работает отдельно и не мешает executor.

---

### Порядок запуска (рекомендуемый)

1. ✅ **Сначала:** Backend (FastAPI) - другие сервисы зависят от него
2. ✅ **Затем:** Redis - должен быть запущен
3. ✅ **Затем:** Executor - для обработки задач
4. ✅ **Затем:** Bot - для взаимодействия с пользователями
5. ✅ **В последнюю очередь:** Video Monitor - опционально

---

## 🔧 Запуск через systemd (production)

### 1. Подготовка unit файлов

Скопируйте unit файлы в `/etc/systemd/system/`:

```bash
sudo cp systemd/tiktok-backend.service /etc/systemd/system/
sudo cp systemd/tiktok-bot.service /etc/systemd/system/
sudo cp systemd/tiktok-executor.service /etc/systemd/system/
sudo cp systemd/tiktok-video-monitor.service /etc/systemd/system/
```

### 2. Настройка unit файлов

Отредактируйте каждый файл и замените пути:

```bash
sudo nano /etc/systemd/system/tiktok-backend.service
sudo nano /etc/systemd/system/tiktok-bot.service
sudo nano /etc/systemd/system/tiktok-executor.service
sudo nano /etc/systemd/system/tiktok-video-monitor.service
```

**Что изменить:**
- `User=teletan` → ваш пользователь (например, `User=ubuntu`)
- `Group=teletan` → ваша группа
- `WorkingDirectory=/opt/TeletanSoft` → путь к проекту
- `ExecStart=/opt/TeletanSoft/venv/bin/python` → путь к Python
- `EnvironmentFile=/opt/TeletanSoft/.env` → путь к `.env` файлу

**Пример:**
```ini
[Service]
User=ubuntu
Group=ubuntu
WorkingDirectory=/home/ubuntu/TeletanSoft
ExecStart=/home/ubuntu/TeletanSoft/venv/bin/python -m bot.app
EnvironmentFile=/home/ubuntu/TeletanSoft/.env
```

### 3. Загрузка и запуск сервисов

```bash
# Перезагрузите systemd
sudo systemctl daemon-reload

# Включите автозапуск (опционально)
sudo systemctl enable tiktok-backend.service
sudo systemctl enable tiktok-bot.service
sudo systemctl enable tiktok-executor.service
sudo systemctl enable tiktok-video-monitor.service

# Запустите сервисы
sudo systemctl start tiktok-backend.service
sudo systemctl start tiktok-bot.service
sudo systemctl start tiktok-executor.service
sudo systemctl start tiktok-video-monitor.service
```

### 4. Проверка статуса

```bash
# Проверьте статус всех сервисов
sudo systemctl status tiktok-backend.service
sudo systemctl status tiktok-bot.service
sudo systemctl status tiktok-executor.service
sudo systemctl status tiktok-video-monitor.service

# Или все сразу:
sudo systemctl status tiktok-*.service
```

### 5. Просмотр логов

```bash
# Логи Backend
sudo journalctl -u tiktok-backend.service -f

# Логи Bot
sudo journalctl -u tiktok-bot.service -f

# Логи Executor
sudo journalctl -u tiktok-executor.service -f

# Логи Video Monitor
sudo journalctl -u tiktok-video-monitor.service -f

# Последние 50 строк всех логов:
sudo journalctl -u tiktok-*.service -n 50
```

---

## ✅ Проверка работоспособности

### 1. Проверка Backend

```bash
# Проверка API
curl http://localhost:8000/docs

# Или откройте в браузере:
# http://localhost:8000/docs
```

**Ожидаемый результат:** Должен открыться Swagger UI с API документацией.

---

### 2. Проверка Bot

1. Откройте вашего бота в Telegram
2. Отправьте `/start`
3. Должен прийти ответ от бота

**Проверка админ-меню:**
1. Откройте админ-меню (если вы админ)
2. Должна быть кнопка "🎬 Парсинг видео"

---

### 3. Проверка Executor

```bash
# Создайте тестовую задачу через бота или API
# Executor должен подхватить задачу и начать обработку

# Проверьте логи:
tail -f logs/executor.log  # или journalctl для systemd
```

**Ожидаемый результат:** Видны логи обработки задач.

---

### 4. Проверка Video Monitor ⭐ НОВОЕ

**Через бота:**
1. Откройте админ-меню → "🎬 Парсинг видео"
2. Нажмите "➕ Добавить блогера"
3. Введите username блогера (например, `username`)
4. Блогер должен добавиться

**Проверка через API:**
```bash
# Список блогеров
curl http://localhost:8000/api/video-parsing/creators

# Должен вернуться JSON с блогерами
```

**Проверка воркера:**
```bash
# Проверьте логи Video Monitor
tail -f logs/video_monitor.log  # или journalctl для systemd
```

**Ожидаемый результат:** Видны логи мониторинга блогеров.

**Ручной запуск проверки:**
1. Через бота: "🎬 Парсинг видео" → выберите блогера → "🧪 Запуск"
2. Или через API: `POST /api/video-parsing/creators/{id}/run-now`

---

### 5. Проверка Redis

```bash
redis-cli ping
# Должно ответить: PONG

# Проверка очереди задач:
redis-cli LLEN queue:tasks
# Должно показать количество задач в очереди
```

---

### 6. Проверка базы данных

```bash
# SQLite:
sqlite3 dev.db "SELECT COUNT(*) FROM creator_sources;"
# Должно показать количество блогеров

sqlite3 dev.db "SELECT COUNT(*) FROM creator_videos;"
# Должно показать количество найденных видео
```

---

## 🔧 Устранение проблем

### Проблема 1: Backend не запускается

**Симптомы:**
```
ERROR: Could not import module 'main'
```

**Решение:**
1. Проверьте, что вы в правильной директории
2. Проверьте, что виртуальное окружение активировано
3. Проверьте, что все зависимости установлены: `pip install -r requirements.txt`

---

### Проблема 2: Bot не запускается

**Симптомы:**
```
ERROR: BOT_TOKEN not set
```

**Решение:**
1. Проверьте, что `.env` файл существует
2. Проверьте, что `BOT_TOKEN` задан в `.env`
3. Проверьте, что токен правильный (получите новый у [@BotFather](https://t.me/BotFather))

---

### Проблема 3: Executor не обрабатывает задачи

**Симптомы:**
- Задачи создаются, но не обрабатываются

**Решение:**
1. Проверьте, что Redis запущен: `redis-cli ping`
2. Проверьте, что Executor запущен
3. Проверьте логи Executor на ошибки
4. Проверьте, что есть активные аккаунты в БД

---

### Проблема 4: Video Monitor не находит видео

**Симптомы:**
```
[VIDEO_MONITOR] No videos found for @username
```

**Решение:**
1. Проверьте, что username правильный (без @)
2. Проверьте, что профиль доступен публично
3. Проверьте, что есть активные аккаунты для мониторинга
4. Проверьте логи Video Monitor на ошибки парсинга

---

### Проблема 5: База данных не создаётся

**Симптомы:**
```
ERROR: no such table: creator_sources
```

**Решение:**
1. Запустите создание таблиц: `python db/create_all.py`
2. Проверьте права доступа к файлу БД
3. Для PostgreSQL: проверьте подключение и права пользователя

---

### Проблема 6: Systemd сервис не запускается

**Симптомы:**
```
Failed to start tiktok-backend.service
```

**Решение:**
1. Проверьте логи: `sudo journalctl -u tiktok-backend.service -n 50`
2. Проверьте пути в unit файле (должны быть абсолютные)
3. Проверьте права доступа к файлам
4. Проверьте, что `.env` файл доступен для чтения

---

## 📊 Мониторинг и логи

### Просмотр логов (ручной режим)

```bash
# Backend (если логи в файл):
tail -f logs/backend.log

# Bot:
tail -f logs/bot.log

# Executor:
tail -f logs/executor.log

# Video Monitor:
tail -f logs/video_monitor.log
```

### Просмотр логов (systemd)

```bash
# Все сервисы:
sudo journalctl -u tiktok-*.service -f

# Конкретный сервис:
sudo journalctl -u tiktok-backend.service -f

# Последние 100 строк:
sudo journalctl -u tiktok-*.service -n 100
```

---

## 🎯 Быстрая команда для запуска всего

### Скрипт запуска (Linux/Mac)

Создайте файл `start_all.sh`:

```bash
#!/bin/bash

# Активируем виртуальное окружение
source venv/bin/activate

# Запускаем в фоне
nohup uvicorn main:app --host 0.0.0.0 --port 8000 > logs/backend.log 2>&1 &
nohup python -m bot.app > logs/bot.log 2>&1 &
nohup python -m executor.executor > logs/executor.log 2>&1 &
nohup python -m services.video_monitor_worker > logs/video_monitor.log 2>&1 &

echo "Все сервисы запущены!"
echo "Логи в папке logs/"
```

Сделайте исполняемым и запустите:
```bash
chmod +x start_all.sh
./start_all.sh
```

### Скрипт остановки

Создайте файл `stop_all.sh`:

```bash
#!/bin/bash

pkill -f "uvicorn main:app"
pkill -f "python -m bot.app"
pkill -f "python -m executor.executor"
pkill -f "python -m services.video_monitor_worker"

echo "Все сервисы остановлены!"
```

---

## ✅ Чеклист запуска

Перед запуском проверьте:

- [ ] Установлены все зависимости (`pip install -r requirements.txt`)
- [ ] Установлен Playwright браузер (`python -m playwright install chromium`)
- [ ] Redis запущен и доступен (`redis-cli ping`)
- [ ] Создан `.env` файл с правильными настройками
- [ ] База данных создана (`python db/create_all.py`)
- [ ] Backend запущен и доступен (http://localhost:8000/docs)
- [ ] Bot запущен и отвечает в Telegram
- [ ] Executor запущен и готов к обработке задач
- [ ] Video Monitor запущен (опционально)

---

**🎉 Готово! Все сервисы должны работать.**

Если что-то не работает - проверьте логи и следуйте инструкциям в разделе "Устранение проблем".
