# Итоговый аудит HTTP-only

## Результат

Исправлены несоответствия по шести требованиям. При активных TIKTOK_NETWORK_FIRST и TIKTOK_NETWORK_IMPL_READY заказы публикуют комментарии и ответы через window.fetch живой страницы. Отказ аккаунта или HTTP-запроса не включает DOM-кликер, DOM-поиск/скролл или отдельные аккаунты-наблюдатели.

Уточнения к заданию: браузер всё ещё нужен для загрузки SDK и native fetch. Обязательный ttwid ожидается до 6 секунд с шагом 0.25 секунды. Отсутствие tt_csrf_token не является самостоятельным отказом: его может выставить ответ первого comment/list после появления ttwid. API-подтверждение не гарантирует последующую публичную видимость после модерации TikTok.

## Найденные несоответствия и исправления

1. **Строгий HTTP-маршрут.** В .env было PLAYWRIGHT_HEADLESS=False. PARALLEL_COMMENTING_DOM_FIRST мог перекрывать активный HTTP; неполные mains запускали observers, legacy HYBRID FULL запускал DOM-верификацию, а execute_replies_http мог выбрать DOM по стратегии аккаунта/отсутствию ID. Неоднозначная проверка авторизации могла открывать шторку комментариев. Теперь общая политика отдаёт приоритет HTTP, эти ветки закрыты; низкоуровневые DOM-публикаторы имеют дополнительную защиту. BAS не выбирается вместо активного page-backed HTTP.
2. **ttwid и навигация (без page.reload).** Из `warmup_page_for_http` полностью убрана резкая перезагрузка страницы (`page.reload`), из-за которой в логах падали попытки ответов с `Page.reload: net::ERR_ABORTED`. Скрипт спокойно опрашивает cookies живой страницы до получения `ttwid` (и при повторной попытке продолжает ожидание на целевой странице без `page.reload`), после чего выполняет запрос `comment/list`. Если первичная навигация (`page.goto`) или редирект короткой ссылки прерываются `ERR_ABORTED`, выполняется пауза и чистый повторный `page.goto`. Явные login/captcha отсекаются до POST.
3. **Короткие ссылки и photo.** При прокси-режиме ранний резолв мог пропускаться; резолв для блокировки URL не передавал результат исполнителю. Добавлен общий резолв до запуска работников через прямое соединение либо разрешённый прокси пула владельца/выделенный прокси. Повторного сетевого резолва канонической ссылки нет. Неразрешённый shortlink завершает подготовку без расходования аккаунтов. Навигация с about:blank и других немедиа-страниц обязательна. /photo/ сохраняется.
4. **Cookies и заголовки.** Для ttwid не было приоритета последней живой записи при двух реальных сроках действия. Теперь он выбирается так же, как msToken/tt_csrf_token: веб-домен, реальный срок вместо синтетического 2038, последняя запись. Уже действовавшие очистка старых антифрод-кук, восстановление sessionid/sid_tt/sessionid_ss из sid_guard, обновление cookies между попытками и whitelist native fetch без Cookie/x-secsdk-* проверены тестами.
5. **Рабочие попытки.** request_attempted выставлялся до page.evaluate; ошибка JS-контекста могла списать попытку без POST. Добавлено наблюдение за реальным запросом публикации и передача этого признака через отмены/таймауты работника. Явный publication_attempted=False имеет приоритет над текстом ошибки. Для legacy-вызывателей распознаются warmup_exception, parent_api_unavailable и http_session_not_ready. До-публикационная капча больше не обрывает legacy main-серию после первого аккаунта. Общий MAX_RETRY_ACCOUNTS остаётся ограничителем замен.
6. **Подтверждение результата.** HTTP status_code=0 без CID мог превращаться в успех после DOM-recovery; принимались строковые заглушки CID, неоднозначный корневой id и JSON false вместо числового 0. Агрегаторы могли доверять success без ID, повторно учитывать один ID или принять неполную/не ту матрицу ответов. Теперь нужны числовой status_code=0 и валидный API comment_id; DOM-recovery исключён из HTTP-запроса. Пустое тело, включая ответы без JSON Content-Type, является неподтверждённым stealth-reject. Проверяются отдельные ID, подтверждения ответов, плановые пары parent/variant и ожидаемое число вариантов. Локальные признаки не подменяют API-подтверждение.

## Конфигурация запуска

```dotenv
TIKTOK_NETWORK_FIRST=True
TIKTOK_NETWORK_IMPL_READY=True
PARALLEL_COMMENTING_DOM_FIRST=False
PLAYWRIGHT_HEADLESS=True
USE_PROXY=False
```

По сравнению с исходной .env изменён только PLAYWRIGHT_HEADLESS. Прямое соединение соответствует текущему тестовому режиму пользователя.

## Проверки

Проверено **1257 уникальных тестов**: ошибок 0, падений 0, пропусков 0. Включены 24 теста в реальном Chromium; сетевые ответы в них локальные. Повторные результаты одного теста учитываются по последнему прогону.

Проверки включают последовательные/параллельные заказы, 6×1, main-only, full и replies-only, замену мёртвых аккаунтов, флаги авторизации, synthetic cookies, короткие ссылки/владельцев прокси, native fetch, отсутствие POST при ошибке JS до fetch, сохранение факта POST после отмены, пустые ответы, неправильные ID и неполные матрицы. Старые DOM-тесты явно отключают HTTP в своих fixtures; HTTP-тесты включают строгую политику отдельно.

[Сводный JUnit XML](<C:/Users/daboo/Desktop/TeletanSoft — новая версия стараюсь сделать на веб запросах/output/http-audit-combined.xml>)

| Модуль проверок | Уникальных тестов |
|---|---:|
| `tests.integration.test_http_browser_transport` | 24 |
| `tests.unit.test_account_service` | 2 |
| `tests.unit.test_account_service.TestAccountService` | 71 |
| `tests.unit.test_account_validation_fairness` | 12 |
| `tests.unit.test_browser_cookie_pipeline` | 11 |
| `tests.unit.test_browser_pool_page_reuse` | 41 |
| `tests.unit.test_comment_reply_flow` | 15 |
| `tests.unit.test_cookie_enricher` | 10 |
| `tests.unit.test_cookie_validator` | 70 |
| `tests.unit.test_device_profiles` | 20 |
| `tests.unit.test_engagement_engine_runtime` | 72 |
| `tests.unit.test_engagement_phrase_search` | 42 |
| `tests.unit.test_executor_multi_main_variants` | 14 |
| `tests.unit.test_http_final_audit` | 42 |
| `tests.unit.test_http_first_navigation` | 30 |
| `tests.unit.test_http_first_readiness` | 29 |
| `tests.unit.test_http_initialization` | 18 |
| `tests.unit.test_http_order_modes` | 37 |
| `tests.unit.test_http_signature_runtime` | 30 |
| `tests.unit.test_http_warmup_recovery` | 39 |
| `tests.unit.test_parallel_6x1_mode` | 8 |
| `tests.unit.test_parent_comment_api` | 50 |
| `tests.unit.test_publish_action` | 138 |
| `tests.unit.test_request_profile_http_readiness` | 32 |
| `tests.unit.test_task_execution_ledger` | 10 |
| `tests.unit.test_user_schema` | 16 |
| `tests.unit.test_user_schema_internals` | 374 |

## Быстрая повторная проверка шести требований

```powershell
python -m pytest tests/unit/test_http_final_audit.py tests/unit/test_http_warmup_recovery.py tests/unit/test_cookie_validator.py tests/integration/test_http_browser_transport.py -q
```

## Изменённые исходные файлы

- [.env](<C:/Users/daboo/Desktop/TeletanSoft — новая версия стараюсь сделать на веб запросах/.env>)
- [config/settings.py](<C:/Users/daboo/Desktop/TeletanSoft — новая версия стараюсь сделать на веб запросах/config/settings.py>)
- [executor/http/execution_policy.py](<C:/Users/daboo/Desktop/TeletanSoft — новая версия стараюсь сделать на веб запросах/executor/http/execution_policy.py>)
- [executor/http/playwright_http_client.py](<C:/Users/daboo/Desktop/TeletanSoft — новая версия стараюсь сделать на веб запросах/executor/http/playwright_http_client.py>)
- [executor/user_schema.py](<C:/Users/daboo/Desktop/TeletanSoft — новая версия стараюсь сделать на веб запросах/executor/user_schema.py>)
- [executor/engagement_engine.py](<C:/Users/daboo/Desktop/TeletanSoft — новая версия стараюсь сделать на веб запросах/executor/engagement_engine.py>)
- [executor/publish_action.py](<C:/Users/daboo/Desktop/TeletanSoft — новая версия стараюсь сделать на веб запросах/executor/publish_action.py>)
- [executor/executor.py](<C:/Users/daboo/Desktop/TeletanSoft — новая версия стараюсь сделать на веб запросах/executor/executor.py>)
- [services/cookie_validator.py](<C:/Users/daboo/Desktop/TeletanSoft — новая версия стараюсь сделать на веб запросах/services/cookie_validator.py>)
- [services/execution_attempts.py](<C:/Users/daboo/Desktop/TeletanSoft — новая версия стараюсь сделать на веб запросах/services/execution_attempts.py>)

## Изменённые тестовые файлы

- [tests/unit/test_http_final_audit.py](<C:/Users/daboo/Desktop/TeletanSoft — новая версия стараюсь сделать на веб запросах/tests/unit/test_http_final_audit.py>)
- [tests/unit/test_http_order_modes.py](<C:/Users/daboo/Desktop/TeletanSoft — новая версия стараюсь сделать на веб запросах/tests/unit/test_http_order_modes.py>)
- [tests/unit/test_http_first_readiness.py](<C:/Users/daboo/Desktop/TeletanSoft — новая версия стараюсь сделать на веб запросах/tests/unit/test_http_first_readiness.py>)
- [tests/unit/test_http_warmup_recovery.py](<C:/Users/daboo/Desktop/TeletanSoft — новая версия стараюсь сделать на веб запросах/tests/unit/test_http_warmup_recovery.py>)
- [tests/unit/test_http_initialization.py](<C:/Users/daboo/Desktop/TeletanSoft — новая версия стараюсь сделать на веб запросах/tests/unit/test_http_initialization.py>)
- [tests/unit/test_cookie_validator.py](<C:/Users/daboo/Desktop/TeletanSoft — новая версия стараюсь сделать на веб запросах/tests/unit/test_cookie_validator.py>)
- [tests/unit/test_comment_reply_flow.py](<C:/Users/daboo/Desktop/TeletanSoft — новая версия стараюсь сделать на веб запросах/tests/unit/test_comment_reply_flow.py>)
- [tests/unit/test_http_signature_runtime.py](<C:/Users/daboo/Desktop/TeletanSoft — новая версия стараюсь сделать на веб запросах/tests/unit/test_http_signature_runtime.py>)
- [tests/unit/test_user_schema.py](<C:/Users/daboo/Desktop/TeletanSoft — новая версия стараюсь сделать на веб запросах/tests/unit/test_user_schema.py>)
- [tests/unit/test_user_schema_internals.py](<C:/Users/daboo/Desktop/TeletanSoft — новая версия стараюсь сделать на веб запросах/tests/unit/test_user_schema_internals.py>)
- [tests/unit/test_engagement_engine_runtime.py](<C:/Users/daboo/Desktop/TeletanSoft — новая версия стараюсь сделать на веб запросах/tests/unit/test_engagement_engine_runtime.py>)
- [tests/unit/test_engagement_phrase_search.py](<C:/Users/daboo/Desktop/TeletanSoft — новая версия стараюсь сделать на веб запросах/tests/unit/test_engagement_phrase_search.py>)
- [tests/unit/test_publish_action.py](<C:/Users/daboo/Desktop/TeletanSoft — новая версия стараюсь сделать на веб запросах/tests/unit/test_publish_action.py>)
- [tests/unit/test_parallel_6x1_mode.py](<C:/Users/daboo/Desktop/TeletanSoft — новая версия стараюсь сделать на веб запросах/tests/unit/test_parallel_6x1_mode.py>)
- [tests/integration/test_http_browser_transport.py](<C:/Users/daboo/Desktop/TeletanSoft — новая версия стараюсь сделать на веб запросах/tests/integration/test_http_browser_transport.py>)

## Запуск и проверка реального заказа

Перезапустить исполнитель из папки проекта:

```powershell
python -m executor.executor
```

Затем создать один небольшой HYBRID FULL заказ: 1 главный комментарий и 1 ответ. Отдельным заказом проверить ENGAGEMENT ONLY на известном родительском comment_id. Текущий USE_PROXY=False использует ваш интернет.

В логах нужны transport=window.fetch, POST /api/comment/publish/, status=200, непустое тело и API status_code=0 с comment_id для каждой плановой публикации. При empty body, captcha или мёртвой сессии заказ не должен становиться success; прогрев без POST не расходует рабочий лимит варианта. Реальные комментарии в рамках этого аудита не отправлялись. Гарантировать принятие и видимость комментариев сторонним сервисом на 100% невозможно.
