# План реализации HYBRID-КОММЕНТИРОВАНИЯ

## ✅ Выполнено

1. ✅ HTTP-слой создан:
   - `executor/http/http_context.py` - контекст для HTTP-запросов
   - `executor/http/tiktok_endpoints.py` - централизованные эндпоинты
   - `executor/http/retry_policy.py` - политика повторных попыток
   - `executor/http/tiktok_http_client.py` - HTTP-клиент для TikTok API

2. ✅ Engagement Engine создан:
   - `executor/engagement_engine.py` - единый движок для HYBRID и ENGAGEMENT_ONLY
   - Реализованы функции:
     - `execute_hybrid_full()` - вариант 3 (DOM + HTTP)
     - `execute_engagement_only()` - вариант 4 (только engagement)
     - `find_comment_for_engagement()` - поиск комментария
     - `verify_comment_dom()` - DOM верификация
     - `execute_replies_http()` - replies через HTTP
     - `execute_likes_http()` - likes через HTTP (фиксировано 15)

3. ✅ Модели БД обновлены:
   - Добавлено поле `Task.kind` (full/engagement_only)
   - Обновлен `origin` в settings

4. ✅ API обновлен:
   - `api/tasks.py` - добавлена поддержка `kind`, `engagement_comment_id`, `engagement_username`, `engagement_comment_text`
   - Валидация для engagement_only заказов

5. ✅ Executor обновлен:
   - Добавлена проверка `task.kind`
   - Для `engagement_only` используется `engagement_engine`
   - Для `full` заказов replies/likes переведены на HTTP (HYBRID)

## 🔄 Осталось выполнить

### 6. Бот handlers для engagement_only wizard

Нужно создать:
- `bot/handlers/engagement_wizard.py` - wizard для создания engagement_only заказов
- Добавить кнопку в меню создания заказа: "⚡ Только ответы + лайки"
- Flow:
  1. Выбор видео (url)
  2. Выбор способа найти комментарий:
     - A) Ввести comment_id
     - B) Ввести username + часть текста
  3. Ввод replies (1-3 текста)
  4. Подтверждение
  5. Запуск

### 7. Обновить отображение в "Мои заказы"

Нужно обновить:
- `bot/handlers/orders.py` - добавить отображение:
  - Тип заказа (FULL / ENGAGEMENT)
  - Origin с иконками (🤖 autopilot / ✍️ manual / ⚡ engagement)
  - Кратко: replies OK / likes OK
  - Пример: `#1842 ✅ DONE • FULL • 🤖 autopilot • 💬1 💬r3 ❤️15`

### 8. Исправить баги

- [ ] `post_reply()` - результат игнорируется → ИСПРАВИТЬ (уже исправлено через engagement_engine)
- [ ] `like_comment()` - bool игнорируется → ИСПРАВИТЬ (уже исправлено через engagement_engine)
- [ ] Сохранять реальный `text_choice`, а не `task.main_comment` → ИСПРАВИТЬ
- [ ] Replies/likes ТОЛЬКО по DOM-verified ветке → ИСПРАВЛЕНО
- [ ] `network_confirmed` НЕ считается достаточным для engagement → ИСПРАВЛЕНО

## 📝 Важные замечания

1. **HTTP эндпоинты**: В `tiktok_endpoints.py` указаны TODO - нужно заменить на реальные эндпоинты после анализа network_probe.

2. **Cookies extraction**: В `http_context.py` нужно реализовать извлечение cookies из `account.cookies` или `cookie_file`.

3. **Миграция БД**: Нужно создать миграцию для добавления поля `kind` в таблицу `tasks`:
   ```sql
   ALTER TABLE tasks ADD COLUMN kind VARCHAR(20) DEFAULT 'full' NOT NULL;
   ```

4. **Тестирование**: После реализации нужно протестировать:
   - Создание engagement_only заказа через бота
   - Поиск комментария по comment_id
   - Поиск комментария по username + text
   - Выполнение replies через HTTP
   - Выполнение likes через HTTP (15 штук)
   - Отображение в "Мои заказы"

## 🚀 Следующие шаги

1. Создать `bot/handlers/engagement_wizard.py`
2. Обновить `bot/handlers/orders.py` для отображения kind и origin
3. Создать миграцию БД для поля `kind`
4. Протестировать все сценарии
