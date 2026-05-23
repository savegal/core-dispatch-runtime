# 🔴 УСТАНОВКА И ЗАПУСК REDIS

## Проблема

Система требует Redis для работы очереди задач. Если Redis не запущен, вы увидите ошибки:
```
ConnectionError: Error 10061 connecting to localhost:6379
```

## Решение

### Windows

#### Вариант 1: Установка через WSL (рекомендуется)

1. Установите WSL2 (если еще не установлен):
   ```powershell
   wsl --install
   ```

2. В WSL установите Redis:
   ```bash
   sudo apt update
   sudo apt install redis-server
   ```

3. Запустите Redis:
   ```bash
   sudo service redis-server start
   ```

4. Проверьте работу:
   ```bash
   redis-cli ping
   # Должно вернуть: PONG
   ```

#### Вариант 2: Установка через Docker

1. Установите Docker Desktop для Windows

2. Запустите Redis контейнер:
   ```powershell
   docker run -d -p 6379:6379 --name redis redis:latest
   ```

3. Проверьте работу:
   ```powershell
   docker exec -it redis redis-cli ping
   # Должно вернуть: PONG
   ```

#### Вариант 3: Установка через Memurai (Windows-версия Redis)

1. Скачайте Memurai с официального сайта: https://www.memurai.com/

2. Установите и запустите Memurai

3. По умолчанию Memurai использует порт 6379

### Linux / macOS

1. Установите Redis:
   ```bash
   # Ubuntu/Debian
   sudo apt install redis-server
   
   # macOS
   brew install redis
   ```

2. Запустите Redis:
   ```bash
   # Ubuntu/Debian
   sudo systemctl start redis-server
   
   # macOS
   brew services start redis
   ```

3. Проверьте работу:
   ```bash
   redis-cli ping
   # Должно вернуть: PONG
   ```

## Настройка в .env

Убедитесь, что в `.env` файле указан правильный URL Redis:

```env
REDIS_URL=redis://localhost:6379/0
```

Если Redis запущен на другом хосте/порту, измените URL соответственно.

## Проверка подключения

После запуска Redis, перезапустите приложение:

```powershell
# Остановите текущий процесс (Ctrl+C)
# Затем запустите снова:
uvicorn main:app --reload
```

Вы должны увидеть в логах:
```
Redis client initialized with connection pooling
```

## Автозапуск Redis

### Windows (WSL)

Добавьте в `/etc/systemd/system/redis.service` или используйте:
```bash
sudo systemctl enable redis-server
```

### Linux

```bash
sudo systemctl enable redis-server
```

### macOS

```bash
brew services start redis
```

## Устранение проблем

### Ошибка "Connection refused"

- Убедитесь, что Redis запущен: `redis-cli ping`
- Проверьте, что порт 6379 не занят другим процессом
- Проверьте настройки файрвола

### Ошибка "Address already in use"

- Другой процесс использует порт 6379
- Найдите и остановите процесс или измените порт Redis

### Redis не запускается

- Проверьте логи Redis: `redis-server --logfile /var/log/redis/redis-server.log`
- Убедитесь, что у Redis есть права на запись в директорию данных

---

**После запуска Redis система должна работать корректно!** ✅
