# Обновление для Reply Comment

## ✅ Обнаружено из curl команды

Получена полная curl команда для **reply comment** (ответ на комментарий).

### Ключевые отличия:

#### 1. Headers для Reply
**ВАЖНО:** Reply использует те же headers что и **like**, а не как publish главного комментария:

```
x-secsdk-csrf-token: DOWNGRADE  ✅ (как для like)
tt-csrf-token: ...              ✅
```

**НЕ использует:**
```
x-secsdk-csrf-request: 1       ❌ (только для publish главного комментария)
x-secsdk-csrf-version: 1.2.22  ❌ (только для publish главного комментария)
```

#### 2. URL параметры для Reply
```
reply_id=7589677087624495927   ✅ ID комментария, на который отвечаем
reply_to_reply_id=0            ✅ Если отвечаем на reply, то ID того reply, иначе 0
text=ggg                       ✅ Текст ответа
text_extra=[]                  ✅ Дополнительный параметр для текста
X-Bogus=...                    ✅ В URL query params
X-Gnarly=...                   ✅ В URL query params
```

#### 3. Дополнительные заголовки
Из curl команды видно дополнительные заголовки:
- `tt-ticket-guard-client-data: ...`
- `tt-ticket-guard-iteration-version: 0`
- `tt-ticket-guard-public-key: ...`
- `tt-ticket-guard-version: 2`
- `tt-ticket-guard-web-version: 1`

Эти заголовки могут быть опциональными, но для полной совместимости их можно добавить.

## ✅ Обновления в коде

### 1. `executor/http/http_context.py`
- ✅ Обновлена логика определения типа запроса
- ✅ Reply теперь использует headers как like (`x-secsdk-csrf-token: DOWNGRADE`)

### 2. `executor/http/tiktok_http_client.py`
- ✅ Обновлена логика определения типа запроса в `_make_request`
- ✅ Проверка наличия `reply_id` в URL для определения reply vs publish

### 3. `executor/http/tiktok_endpoints.py`
- ✅ `reply_id` и `reply_to_reply_id` теперь всегда строки (как в curl)

## 📋 Типы запросов и их headers

| Тип запроса | x-secsdk-csrf-token | x-secsdk-csrf-request | x-secsdk-csrf-version |
|------------|---------------------|----------------------|----------------------|
| **Like** | `DOWNGRADE` ✅ | ❌ | ❌ |
| **Reply** | `DOWNGRADE` ✅ | ❌ | ❌ |
| **Publish** (главный) | ❌ | `1` ✅ | `1.2.22` ✅ |

## ⚠️ TODO (опционально)

### Добавить дополнительные заголовки для полной совместимости:
- `tt-ticket-guard-client-data`
- `tt-ticket-guard-iteration-version`
- `tt-ticket-guard-public-key`
- `tt-ticket-guard-version`
- `tt-ticket-guard-web-version`

Эти заголовки могут быть опциональными, но для максимальной совместимости их можно добавить.

## ✅ Готово

Код обновлен для правильной обработки reply запросов с правильными headers.
