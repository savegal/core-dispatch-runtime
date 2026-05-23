# Интеграция curl команд в HTTP-слой

## ✅ Обновлено с реальными curl командами

HTTP-слой обновлен на основе полных curl команд с заголовками.

## Структура curl команд

### Like Comment (curl_like.txt)
```bash
curl 'https://www.tiktok.com/api/comment/digg/?...&cid=...&digg_type=1&...&X-Bogus=...&X-Gnarly=...' \
  -X 'POST' \
  -H 'Cookie: sid_tt=...; sessionid=...; msToken=...; ...' \
  -H 'tt-csrf-token: ...' \
  -H 'x-secsdk-csrf-token: DOWNGRADE' \
  ...
```

### Publish Comment (curl_publish.txt)
```bash
curl 'https://www.tiktok.com/api/comment/publish/?...&text=...&...' \
  -X 'POST' \
  -H 'Cookie: ...' \
  -H 'x-secsdk-csrf-request: 1' \
  -H 'x-secsdk-csrf-version: 1.2.22' \
  ...
```

## Важные заголовки

### Обязательные для всех запросов:
- `Cookie:` - полная строка cookies
- `accept: */*`
- `accept-language: ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7`
- `origin: https://www.tiktok.com`
- `referer: https://www.tiktok.com/...`
- `user-agent: Mozilla/5.0...`
- `sec-ch-ua`, `sec-ch-ua-mobile`, `sec-ch-ua-platform`
- `sec-fetch-dest`, `sec-fetch-mode`, `sec-fetch-site`

### Специфичные для Like:
- `x-secsdk-csrf-token: DOWNGRADE`
- `tt-csrf-token: ...` (из cookies)

### Специфичные для Publish/Reply:
- `x-secsdk-csrf-request: 1`
- `x-secsdk-csrf-version: 1.2.22`

## Параметры в URL

### X-Bogus и X-Gnarly
Эти параметры находятся в **URL query params**, а не в headers:
```
?X-Bogus=DFSzsIVufnTANGtPCzORfhNnXzYK&X-Gnarly=MFUGCz3OoAIf...
```

### Важные параметры из cookies:
- `msToken` - из cookies (может быть несколько, берем последний)
- `verifyFp` - из cookies (s_v_web_id)
- `device_id` - из cookies
- `odinId` - из cookies (odin_tt)
- `WebIdLastTime` - timestamp

## Обновленные файлы

### 1. `executor/http/http_context.py`
- ✅ Извлечение всех cookies из account
- ✅ Правильное извлечение msToken (последний если несколько)
- ✅ Все заголовки из curl команд
- ✅ Динамическая настройка headers в зависимости от типа запроса

### 2. `executor/http/tiktok_http_client.py`
- ✅ Использование правильных headers для каждого типа запроса
- ✅ Передача X-Bogus и X-Gnarly в URL (если есть)

### 3. `executor/http/tiktok_endpoints.py`
- ✅ Все параметры из curl команд
- ✅ Стандартные параметры (WebIdLastTime, history_len и т.д.)

## TODO

### X-Bogus и X-Gnarly генерация
Эти параметры требуют криптографии. Пока:
- Извлекаются из cookies если есть
- Передаются в URL если найдены
- Если нет - запрос может работать без них (но не гарантировано)

**Варианты реализации:**
1. Использовать библиотеку `tiktok-signature` или аналогичную
2. Реализовать собственную генерацию (сложно)

## Тестирование

После обновления нужно протестировать:
1. ✅ Правильное извлечение cookies из account
2. ✅ Формирование Cookie header строки
3. ✅ Правильные headers для like запросов
4. ✅ Правильные headers для publish/reply запросов
5. ⏳ Работа с X-Bogus и X-Gnarly (если есть в cookies)
6. ⏳ Работа без X-Bogus и X-Gnarly (может работать для некоторых запросов)

## Готово

HTTP-слой обновлен с полными заголовками из curl команд. Все заголовки настроены правильно в зависимости от типа запроса.
