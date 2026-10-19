# Хотфикс 9: cookies и заголовки native fetch

Дата: 16 октября 2026 года.

## Что изменено

1. `normalize_cookies_for_browser` очищает стартовые антифрод-cookies по умолчанию. BrowserPool явно передаёт `drop_stale_antifraud=True`; `parse_cookies` уже передавал его и раньше. Поэтому утверждение ревью, что флаг нигде не включался, было неточным, но граница BrowserPool и значение по умолчанию теперь также исправлены.
2. При стартовой загрузке удаляется `msToken`, если срок `expires`/`expirationDate`/`expiration` после нормализации не менее `2000000000`. Это выбранная в задании альтернатива проверке возраста валидации. Токены с реальным сроком и сессионные токены не удаляются только из-за отсутствия срока.
3. Синтетический срок до 2038 года имеет меньший приоритет, чем реальные/сессионные записи. Для `msToken` и `tt_csrf_token` приоритетны веб-домены, затем несинтетический срок и последняя запись. Выбор `sid_guard` по внутреннему timestamp сохранён. Порядок выбранных записей сохраняется и при повторной нормализации.
4. Сохранение прогретого контекста явно использует `drop_stale_antifraud=False`: свежие `tt_csrf_token` и прочие полученные от страницы cookies не выбрасываются при записи в БД.
5. Профили не навязывают `x-secsdk-csrf-request`, `x-secsdk-csrf-version` и `x-secsdk-csrf-token: DOWNGRADE`. `build_headers` использует cookie только для `tt-csrf-token`, а не для SDK-токена.
6. Браузерный транспорт пропускает только `accept`, `accept-language`, `tt-csrf-token`, `content-type`. Для POST задаётся `application/x-www-form-urlencoded; charset=UTF-8`. Сторонние/старые `x-secsdk-*` исключаются независимо от регистра имени.
7. Для native fetch комментариев `msToken` не вставляется в исходный URL из cookies, static или dynamic params. Это применяется и к GET поиска, и к POST публикации. Токен и подписи добавляет сама страница. Транспорт без `page` сохраняет прежнюю возможность использовать cookie `msToken`.

## Проверка на живой странице

Проверка выполнена через штатный BrowserPool без прокси, со свежим загруженным кодом. Аккаунты: 2445 и 2507. Источник доказательств: `output/playwright/hotfix9-live-verification.jsonl`.

На обоих аккаунтах прогрев завершился успешно. Для четырёх POST — главный комментарий и ответ на каждом аккаунте — подтверждено:

- транспорт `window.fetch`;
- страница добавила `msToken`, `X-Bogus`, `X-Gnarly`;
- `msToken` отличается от сохранённого токена аккаунта в БД;
- в окончательном POST отсутствуют `x-secsdk-csrf-request` и `x-secsdk-csrf-version`;
- CSRF-заголовок SDK присутствует и отличается от `DOWNGRADE` и от cookie `tt_csrf_token`;
- текст формы, ID родителя и сессионные cookies передаются;
- служебный HEAD получил HTTP 200 с `x-ware-csrf-token`;
- cookies аккаунта в БД не изменены диагностикой.

Все четыре POST получили локальный диагностический ответ без ID и не отправлялись в TikTok. Это подтверждает подготовку транспорта и работу перехватчика, но не успешность настоящего POST на сервере.

GET списка аккаунта 2445 вернул 48 комментариев. У аккаунта 2507 при этой проверке GET по-прежнему вернул HTTP 200 с пустым телом. Поэтому исчезновение всех серверных отказов этим хотфиксом не заявляется. Публичная видимость новых публикаций этим запуском не проверялась.

## Регрессионные сценарии

Добавлены и обновлены проверки: synthetic expiry против реального; смена срока и порядка токенов; повторная нормализация; очистка на границе BrowserPool с обходом очистки парсера; сохранение прогретых cookies; обновление между retry/вариантами; запрет SDK-заголовков от вызывающего кода; добавление токенов перехватчиком в настоящем Chromium; отсутствие cookie `msToken` в исходных GET/POST URL; совместимость APIRequestContext без `page`.

Общий прогон: **861 passed**, 312,55 секунды, в том числе **21 тест в настоящем Chromium с локальными ответами**. Результат сохранён в `hotfix9-regression.log`. Ранее ожидавшие принудительную подстановку SDK-заголовка и cookie `msToken` тесты обновлены под новый контракт; отдельно проверено, что транспорт APIRequestContext без `page` сохраняет cookie `msToken`.

## Изменённые файлы

Рабочий код и диагностика:

- `services/cookie_validator.py`
- `services/cookie_persistence_service.py`
- `executor/browser_pool.py`
- `executor/http/request_profile.py`
- `executor/http/signature_runtime.py`
- `executor/http/playwright_http_client.py`
- `executor/http/comment_discovery.py`
- `scripts/verify_http_pipeline.py`

Тесты:

- `tests/unit/test_cookie_validator.py`
- `tests/unit/test_request_profile_http_readiness.py`
- `tests/unit/test_http_initialization.py`
- `tests/unit/test_parent_comment_api.py`
- `tests/unit/test_http_signature_runtime.py`
- `tests/integration/test_http_browser_transport.py`

Документация: этот отчёт и `HTTP_FIRST_TEST_RUN.md`. После замены кода нужен перезапуск старого executor; автоматической публикации или перезапуска в ходе проверки не выполнялось. Архив для ревью, созданный до этого хотфикса, содержит предыдущий снимок исходников.
