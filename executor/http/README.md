# HTTP-слой для TikTok API

## Обновлено с реальными эндпоинтами

HTTP-слой обновлен на основе реальных curl команд из:
- `ответы.txt` - POST запрос для reply
- `просто коменты.txt` - POST запрос для main comment
- `лайки.txt` - POST запрос для like

## Эндпоинты

### 1. Comment Publish (главный комментарий или ответ)
- **URL**: `https://www.tiktok.com/api/comment/publish/`
- **Method**: POST
- **Параметры в URL**:
  - `aweme_id` - ID видео
  - `text` - текст комментария
  - `reply_id` - ID родительского комментария (если это ответ, иначе "0")
  - `reply_to_reply_id` - ID ответа на ответ (обычно "0")
  - Множество других параметров (см. `tiktok_endpoints.py`)

### 2. Comment Digg (лайк)
- **URL**: `https://www.tiktok.com/api/comment/digg/`
- **Method**: POST
- **Параметры в URL**:
  - `aweme_id` - ID видео
  - `cid` - ID комментария
  - `digg_type` - 1 для like, 0 для unlike

## Важные заголовки и параметры

### Обязательные параметры из cookies:
- `msToken` - токен из cookies
- `verifyFp` - fingerprint из cookies
- `device_id` - ID устройства из cookies
- `odinId` - ID из cookies
- `WebIdLastTime` - timestamp из cookies

### Заголовки (TODO):
- `X-Bogus` - криптографический заголовок (требует генерации)
- `X-Gnarly` - криптографический заголовок (требует генерации)

**ВАЖНО**: X-Bogus и X-Gnarly требуют сложной криптографии. Пока оставлены как TODO.
TikTok может работать без них для некоторых запросов, но для полной совместимости нужно реализовать генерацию.

## Структура ответов

### Comment Publish Response:
```json
{
  "comment": {
    "cid": "7594448243254117142",  // ID нового комментария
    "aweme_id": "7588903996258110733",
    "text": "текст комментария",
    ...
  },
  "status_code": 0,
  "status_msg": "Comment sent successfully"
}
```

### Comment Digg Response:
```json
{
  "status_code": 0,
  "status_msg": ""
}
```

## Использование

```python
from executor.http.http_context import HttpContext
from executor.http.tiktok_http_client import TikTokHttpClient

# Создаем контекст из аккаунта
context = HttpContext.from_account(account)

# Создаем клиент
client = TikTokHttpClient(context)

# Отправляем reply
success, comment_id, error = client.reply_comment(
    parent_comment_id="7589377062021726998",
    video_id="7588903996258110733",
    text="текст ответа"
)

# Ставим лайк
success, error = client.like_comment(
    comment_id="7590510720192545538",
    video_id="7588903996258110733"
)
```

## TODO

1. ✅ Реальные эндпоинты - реализовано
2. ✅ Извлечение cookies - реализовано
3. ⏳ Генерация X-Bogus - требует криптографии
4. ⏳ Генерация X-Gnarly - требует криптографии
5. ⏳ Генерация msToken - можно извлечь из cookies или сгенерировать

## Примечания

- Все параметры передаются в URL (query string), body обычно пустой
- Cookies извлекаются автоматически из `account.cookies` или `account.cookie_file`
- Retry policy обрабатывает 429 (rate limit) и другие ошибки
- Фатальные ошибки (403, captcha) не ретраятся
