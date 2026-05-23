# BAS Functions для TikTok автоматизации

Эта папка содержит документацию по функциям BAS для автоматизации TikTok.

**Важно:** Используется **BAS Remote** - функции вызываются напрямую из Python, а не через HTTP API.

---

## 📋 Необходимые функции в BAS

В BAS Studio нужно создать **Functions** (через Function Manager), а не отдельные скрипты.

### 1. Функция `TikTokPostComment`

**Назначение:** Публикация комментариев к видео TikTok.

**Параметры:**
- `video_url` (string) - URL видео TikTok
- `comment_text` (string) - Текст комментария
- `cookies` (string) - JSON строка с cookies
- `proxy` (string, optional) - JSON строка с параметрами прокси
- `user_agent` (string, optional) - User agent

**Возвращает:**
```json
{
  "comment_id": "123-4567890",
  "success": true
}
```

**Логика:**
1. Получить параметры
2. Загрузить cookies в браузер
3. Настроить прокси (если есть)
4. Открыть TikTok видео
5. Найти поле ввода комментария
6. Ввести текст
7. Отправить комментарий
8. Вернуть результат

---

### 2. Функция `TikTokPostReply`

**Назначение:** Публикация ответов на комментарии.

**Параметры:**
- `video_url` (string) - URL видео TikTok
- `parent_comment_id` (string) - ID родительского комментария
- `reply_text` (string) - Текст ответа
- `cookies` (string) - JSON строка с cookies
- `proxy` (string, optional) - JSON строка с параметрами прокси
- `user_agent` (string, optional) - User agent

**Возвращает:**
```json
{
  "success": true
}
```

---

### 3. Функция `TikTokLikeComment`

**Назначение:** Лайки комментариев.

**Параметры:**
- `video_url` (string) - URL видео TikTok
- `comment_id` (string) - ID комментария
- `cookies` (string) - JSON строка с cookies
- `proxy` (string, optional) - JSON строка с параметрами прокси
- `user_agent` (string, optional) - User agent

**Возвращает:**
```json
{
  "success": true
}
```

---

### 4. Функция `TikTokValidateAccount`

**Назначение:** Быстрая валидация аккаунта TikTok.

**Параметры:**
- `cookies` (string) - JSON строка с cookies
- `proxy` (string, optional) - JSON строка с параметрами прокси
- `user_agent` (string, optional) - User agent

**Возвращает:**
```json
{
  "valid": true
}
```

**Логика:**
1. Загрузить cookies
2. Настроить прокси (если есть)
3. Открыть TikTok главную страницу
4. Проверить наличие элементов авторизации
5. Проверить наличие элементов профиля
6. Вернуть результат

---

### 5. Функция `TikTokVerifyComment`

**Назначение:** Проверка видимости комментария.

**Параметры:**
- `video_url` (string) - URL видео TikTok
- `comment_text` (string) - Текст комментария для поиска
- `cookies` (string) - JSON строка с cookies
- `proxy` (string, optional) - JSON строка с параметрами прокси
- `user_agent` (string, optional) - User agent

**Возвращает:**
```json
{
  "visible": true
}
```

---

## 📝 Формат cookies

Cookies передаются в формате JSON массива:
```json
[
  {"name": "sid_tt", "value": "xxx", "domain": ".tiktok.com", "path": "/"},
  {"name": "sessionid", "value": "yyy", "domain": ".tiktok.com", "path": "/"}
]
```

---

## 📝 Формат proxy

Прокси передается в формате JSON объекта:
```json
{
  "type": "http",
  "host": "proxy.example.com",
  "port": 8080,
  "username": "user",
  "password": "pass"
}
```

Или для SOCKS5:
```json
{
  "type": "socks5",
  "host": "proxy.example.com",
  "port": 1080,
  "username": "user",
  "password": "pass"
}
```

---

## 🎯 Как создать функции в BAS

### Шаг 1: Открыть Function Manager

1. В BAS Studio найдите **"Менеджер Модулей"** (Module Manager) или **Function Manager**
2. Создайте новую функцию

### Шаг 2: Создать функцию

1. **Название функции:** `TikTokPostComment` (точно так, как указано выше)
2. **Параметры:** Добавьте все параметры, указанные выше
3. **Логика:** Реализуйте логику комментирования
4. **Возврат:** Верните JSON с результатом

### Шаг 3: Повторить для всех функций

Создайте все 5 функций:
- `TikTokPostComment`
- `TikTokPostReply`
- `TikTokLikeComment`
- `TikTokValidateAccount`
- `TikTokVerifyComment`

### Шаг 4: Скомпилировать скрипт

1. Нажмите **"Скомпилировать"** (Compile)
2. Дайте имя: **`TikTokAutomation`** (или другое, укажите в `.env`)
3. Сохраните

### Шаг 5: Включить Remote Function Execution

1. Зайдите в **кабинет Bablosoft** (онлайн)
2. Найдите ваш скрипт
3. Включите **"allow remote function execution"**
4. Сохраните

---

## ✅ Проверка

После создания функций:

1. **Установите библиотеку:**
   ```bash
   pip install bas-remote-python
   ```

2. **Настройте `.env`:**
   ```env
   USE_BAS=true
   BAS_SCRIPT_NAME=TikTokAutomation
   ```

3. **Протестируйте:**
   ```python
   import asyncio
   from executor.bas_client import BASClient
   
   async def test():
       client = BASClient()
       try:
           # Тест валидации
           from db.models import Account
           account = Account(id=1, label="test", cookies='[{"name":"sid_tt","value":"test"}]')
           result = await client.quick_validate_account(account)
           print(f"Validation result: {result}")
       finally:
           await client.close()
   
   asyncio.run(test())
   ```

---

**Подробнее:** См. `BAS_REMOTE_SETUP.md` в корне проекта.
