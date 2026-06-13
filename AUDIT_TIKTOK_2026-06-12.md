# Глубокий аудит реализации TikTok-автоматизации (12.06.2026)

Статус: ЧЕРНОВИК — пополняется по ходу аудита. Только анализ, без правок кода.

## Карта объёмов (что разрослось)

| Файл | Строк | Роль |
|---|---|---|
| executor/user_schema.py | 10 132 | оркестрация задач, login-пайплайн, параллельные слоты |
| executor/dom_actions.py | 6 700 | поиск инпута, ввод текста, кнопка Post |
| executor/engagement_engine.py | ~4 300 | engagement-only, лайки, reply-пул |
| executor/publish_action.py | 2 699 | submit + network confirm |
| executor/comment_outcome.py | ~2 300 | верификация публикации, batch-поиск родителей |

Фиксированных `asyncio.sleep()` в executor/: **84 штуки** (макс 2.5s, в основном 0.1–1.0s).
Это НЕ главный источник тормозов — главный источник: каскады таймаут-бюджетов и цепочки fallback-проб (см. ниже).

---

## КРИТИЧНЫЕ находки (корень «кривой работы»)

### K1. JS-fallback ввода текста создаёт «успех» при пустом React-стейте → серая кнопка Post
`executor/dom_actions.py:6440-6462` (`_js_insert_text`): если execCommand не сработал —
делает `real.textContent = expected` + синтетические события. **textContent НЕ обновляет
React-стейт** (TikTok-композер на Lexical/Draft). Дальше:
- верификация `_page_text_contains_expected()` видит текст в DOM → type_into_comment возвращает True;
- `wait_for_post_button_enabled` честно ждёт весь бюджет (до 8s) — кнопка серая, ведь React пустой;
- затем `publish_action.py:1516` «Trying legacy force-click anyway» кликает по DISABLED
  кнопке / шлёт synthetic dispatch → сети нет → `no_network_request_after_post_click`.
Это ровно симптом из логов пользователя («текст вроде введён, кнопка серая»).
**Вердикт: fallback `textContent=` хуже отсутствия fallback — он маскирует провал и сжигает
бюджет.** Цепочка после insert_text-primary должна либо повторять ТОЛЬКО trusted-ввод
(клик в редактор + keyboard.insert_text), либо сразу отдавать failure для смены аккаунта.

### K2. Верификация «текст введён» принимает ЧАСТИЧНЫЙ ввод
`executor/dom_actions.py:6255-6257, 5985` (и в `_JS_COMMENT_INPUT_TEXT_MATCH`):
`txt === expected || txt.includes(expected) || expected.includes(txt)`.
`expected.includes(txt)` означает: в редакторе лежит «How» от «How much is it?» —
верификация ПРОЙДЕНА. Можно запостить обрубок комментария. Для дедупа/поиска родителя
обрубок потом не матчится с ожидаемым текстом → reply-фаза его не найдёт.
**Должно быть строгое равенство после нормализации пробелов.**

### K3. Слепой ввод с клавиатуры после непроверенного фокуса
`executor/dom_actions.py:6556-6560` (`_atomic_primary`): после focus-evaluate сразу
`Control+A` → `Backspace` → `insert_text`. Между evaluate и нажатиями фокус может
увести оверлей/ре-рендер. Хуже: fallback-попытки фокуса `attempt==3: Tab` и
`attempt==4: Shift+Tab` (6631-6634) **переводят фокус на случайный элемент**, после чего
Ctrl+A + Backspace + текст уходят туда (поиск, логин-поле и т.п.). Верификация это
поймает постфактум, но состояние страницы уже испорчено (например, текст в поиске →
дроп-даун поиска перекрывает композер).

### K4. Условие готовности Post-кнопки фильтрует по геометрии: `r.top < innerHeight*0.25 → отбросить`
`executor/dom_actions.py:5720` (`clickTargetFor`): кандидат отбрасывается, если он в
верхней четверти вьюпорта. На нестандартных размерах окна (а browser_pool открывает
окна сеткой/смещением) reply-композер у конкретного коммента может оказаться выше
четверти экрана → «button_not_found» при живой кнопке. Геометрическая эвристика вместо
скоупинга от композера — хрупко.

### K5. Девять слоёв проб поиска инпута, дублирующих друг друга
`executor/dom_actions.py:4214-5400` (`find_comment_input_smart`):
`_oneshot_surface_probe` → `_trusted_existing_input_probe` → `_direct_existing_input_probe`
→ `_fast_existing_input_probe` → `_unified_input_resolver_probe` → `_old_style_handle_probe`
→ `_legacy_js_input_probe` → цикл `_JS_FIND_COMMENT_INPUT` + `_resolve_legacy_js_input`
+ диагностика. Каждый слой — своя копия одних и тех же JS-эвристик. Подсчёт дублей в
dom_actions.py: **`labelOf` ×19, `badMarkers` ×20, `isCommentScoped` ×8,
`placeholderMarkers` ×5** — списки маркеров уже разъехались (где-то есть 'login',
где-то нет 'verification'). Это археология фиксов: каждый новый «фикс» добавлял пробу
ПЕРЕД старыми, не удаляя их. Когда композер реально не виден, цепочка честно сжигает
сумму бюджетов всех слоёв.
**Нужен один резолвер с одной JS-функцией и одним набором маркеров.**

### K6. Полный re-bootstrap браузера на каждый ретрай аккаунта
`executor/user_schema.py:8095-8206` (`_run_main_slot_attempt`) → `_try_main_account:3617`:
каждая попытка = новый аккаунт → `create_page_for_account` (новый context) → navigation
→ login-пробы → (возможно) cookie-enrichment с заходом на /foryou и ВОЗВРАТОМ на видео
(`goto` + sleep 1.0, :3741) → idle-пауза 2–5s → warmup-лайк → публикация. Стоимость
неудачной попытки ≈ 20–40s+. При плохом прокси/куках задача легко уходит в минуты,
т.к. ретраи последовательно оплачивают полный bootstrap. Контекст закрывается в finally
(`_safe_close:3877`) — переиспользования живой залогиненной страницы между вариантами
текста нет НА УРОВНЕ ретраев (внутри одной попытки варианты публикуются на одной странице — ок).

### K7. wait_for_post_button_enabled молотит весь бюджет при ПУСТОМ инпуте
`executor/dom_actions.py:5816-5822, 5881-5888`: при `reason=input_empty` или
`placeholder_leak` цикл продолжает поллить каждые 0.3s до конца max_wait (до 8s),
хотя пустой инпут сам не наполнится — ввод уже провален. Нужен fail-fast после 2-3
стабильных подряд `input_empty`.

---

## СЕРЬЁЗНЫЕ находки (архитектура/скорость)

### S1. user_schema.py — бог-файл на 10k строк
`_try_main_account` + `_publish_all_variants_on_account` + login-пайплайн + параллельный
шедулер + reply-фаза + observer + прокси-логика в одном модуле. 198 def'ов. Половина
функций — замыкания внутри гигантской функции запуска (вложенность 3-4 уровня:
`_run_main_slot_attempt` определён внутри функции, использует 10+ переменных из
окружения). Невозможно юнит-тестить иначе как через getsource-хаки (что тесты и делают).

### S2. Login-readiness: до 4 последовательных проб одного и того же
`_ensure_account_login_ready:2372`: `_open_video_and_check` (полная проба) →
`_ready_health_still_needs_action_probe`? → `_accept_action_ready_surface` (проба
поверхности) → `_confirm_comment_action_ready` (ещё проба + open_comment_section с
таймаутом) → при AMBIGUOUS: `_fast_cookie_health_probe` → reload → ещё probe →
`probe_cookie_health`. Каждая проба сама делает evaluate+ожидания. Худший случай —
минуты на одном аккаунте ДО первого действия. HTTP-preflight (`_preflight_cookie_http_gate`)
частично решает (fast-accept), но только для parallel-correlation и валидного паспорта.

### S3. Таймаут-стекинг: внешние wait_for оборачивают внутренние ретраи
Паттерн повсеместный: `publish_action._publish_via_current_input` имеет свой `_remaining()`
бюджет, внутри — `type_into_comment` со своим deadline, внутри которого «6 попыток фокуса
×(клик+0.22s)» + JS-fallback, поверх — `_typing_phase_timeout`, поверх — retry с НОВЫМ
поиском инпута (`:1357-1414`) и вторым type_into_comment. Аналогично в user_schema:
`asyncio.wait_for(find_comments_in_list_batch, timeout=...)` поверх внутренних max_scrolls
× scroll_delay. Реальную длительность операции по коду предсказать нельзя — она зависит
от того, какой слой первым исчерпает свой бюджет.

### S4. Подмена смыслов: «published» при недоказанной публикации
`publish_action.py:1736-1751`: Enter-fast-path считается submitted если «композер
очистился», затем ждёт network max 2s — если сеть не пришла, статус всё равно идёт
вверх как submitted/unconfirmed. Дальше comment_outcome должен доказывать публикацию
скроллом списка. При этом очистка композера бывает и при ошибке (TikTok сбрасывает
поле при reject). Цепочка «submitted_unconfirmed → DOM-верификация» рабочая, но
дорогая: до 10 скроллов × delay на каждый недоказанный коммент.

### S5. Selectors-модуль декоративный
`executor/tiktok_selectors.py` существует, но dom_actions/comment_outcome используют его
точечно (COMMENT_ROOT, COMMENT_TEXT_SPAN), а 90% селекторов захардкожены в JS-строках
по всему файлу. Половина селекторов в tiktok_selectors.py мертва (`POST_COMMENT_BUTTON`
со старыми `button[data-e2e=...]`, `VIEW_COMMENTS_BUTTON` с :has-text — Playwright-only
синтаксис в поле, которое нигде не передаётся в locator). `get_comment_input_selectors()`
ставит первым приоритетом ГОЛЫЙ `div[contenteditable="true"]` — без скоупинга это
поиск/любой редактор; защищает только то, что эту функцию почти никто не вызывает.

### S6. Прокси-ротация: точечные триггеры вместо централизованной политики
- `mark_proxy_bad` вызывается из user_schema в 2-3 местах по маркерам строк ошибок
  (`_PROXY_FAILURE_MARKERS` — список подстрок; ERR_EMPTY_RESPONSE добавили только сегодня).
- Ротация мобильного IP (`ensure_proxy_ip_rotated`) до сегодняшнего фикса дёргалась
  ТОЛЬКО на edge-403 (`_handle_main_edge_access_denied`), теперь ещё на сетевых сбоях —
  но только в main-фазе. **Reply-фаза и engagement-only ротацию IP не триггерят вообще.**
- Классификация ошибок размазана: `_looks_like_proxy_failure`, `_looks_like_edge_access_denied`,
  `_is_navigation_ambiguous` — три независимых string-match функции, каждая со своим списком.

### S7. Дублирование «двух миров»: user_schema vs engagement_engine
engagement_engine имеет СВОЙ выбор аккаунтов, СВОИ cooldown'ы, СВОЙ HTTP-vs-DOM выбор
(`http_first/http_cautious`, `PlaywrightHttpClient`), частично дублирующий login-пайплайн
user_schema. `execute_engagement_only_replies_via_full_flow` уже признал это и
делегирует в full-flow — правильное направление, но старый HTTP-путь и слой
tiktok_client_async.post_comment остаются как третья параллельная реализация публикации.

---

## ЗАМЕТКИ ПО СКОРОСТИ (типичный счастливый путь, parallel main)

Сумма по коду (типичные значения, не худший случай):
1. create context + goto видео: 3–8s (зависит от прокси)
2. login-probe + settle (`PARALLEL_INITIAL_SHELL_READY_SECONDS` теперь 4.0): ~4–6s
3. (если bucket dom_only) enrichment /foryou + возврат: +8–20s
4. idle-пауза «человечности»: 2–5s
5. warmup-лайк (probability 0.5): 0–8s
6. открытие комментов + поиск инпута (fast-path при surface_ready): 0.5–3s
7. ввод (insert_text): ~1s; Post-gate: 0.3–1s; клик+network: 1–6s
Итого: **12–25s на здоровом аккаунте/прокси без enrichment**, 25–50s с enrichment.
× ретраи при сбоях (каждый с полным bootstrap) — отсюда минуты.
Пункты 4-5 — осознанный анти-детект, его трогать нельзя без решения владельца,
но 4+5 могли бы идти ПАРАЛЛЕЛЬНО открытию комментов (лайк-warmup уже внутри drawer).

---

## Находки в фиксах текущего чата (самопроверка)

- focusOnly+insert_text primary (K3) — правильное направление (trusted input),
  но без проверки фокуса перед Ctrl+A — см. K3.
- `surface_ready_skip_search` fast-path в publish_action — корректен, режет 5-15s.
- Прокси-фикс сегодня: ротация добавлена в main-фазу; reply/engagement — нет (S6).
- comment_outcome: ранний partial-return убран верно; capture-стоп на капче — ок.

## Дочитанные блоки (вторая итерация)

### S8. open_comment_section — 620 строк на «кликнуть иконку комментов»
`executor/dom_actions.py:3202-3822`. Внутри: подавление оверлеев, куки-баннеры,
captcha-чек, hydrate-wait с «nudge»-скроллом, скоринговый `_JS_OPEN_COMMENTS` (:1205,
~360 строк JS со скорингом кандидатов `e2e==='comment-icon' score+=4`...), три режима
готовности (loose/input/list_with_items), JS-event fallback по иконке. Сама задача —
клик по `[data-e2e="comment-icon"]` и ожидание панели. Остальное — обвязка против
прошлых багов, которая теперь сама источник задержек: каждый вызов платит
suppress+dismiss+captcha-evaluate даже когда панель уже открыта.
Вызывается из 5+ мест (find_comment_input_smart, comment_outcome, user_schema...),
иногда ДВАЖДЫ на одну попытку (initial open + strict retry).

### S9. Reply-фаза: тот же полный bootstrap на каждый вариант × каждый ретрай
`user_schema.py:5871+` (`_reply_with_variant_on_all_mains`): preflight → новый context
(`:5962`) → `_ensure_account_login_ready` (полный пайплайн как у main) → enrichment
(как у main) → поиск родителей → ответы. 3 варианта ответов × N ретраев = каждый раз
новый браузер+логин+enrichment. Бюджеты поиска родителей: 18s (обычный) / 28s (deep,
≥6 кандидатов) НА КАЖДУЮ попытку аккаунта.
Плюс (хорошее): `shared_parent_cache` шарит найденные якоря между вариантами,
batch-поиск ищет все тексты за один проход.

### S10. Reply-фаза не триггерит ротацию мобильного прокси
`_handle_main_edge_access_denied` есть только в main-фазе. В reply-фазе аналогичный
обработчик отсутствует: при edge-403/сетевых ошибках reply-аккаунты просто меняются,
оставаясь на том же IP. (См. S6.)

---

## ЧТО СДЕЛАНО ПРАВИЛЬНО (чтобы не сломать при рефакторинге)

1. **Strict gate реплаев** (`user_schema.py:8665`) — реплаи не стартуют, пока не
   опубликованы ВСЕ ожидаемые мейны. На месте, работает.
2. **Network-confirm публикации**: listener на `/api/comment/publish/` вешается ДО
   клика, статус-коды парсятся (`_extract_publish_response_fields`), cid вытаскивается.
3. **Anchor-токены** (`data-smart-parent-anchor`) для привязки реплая к конкретному
   DOM-узлу родителя — правильная идея против дублей текстов в параллельном режиме.
   Риск: TikTok виртуализирует длинные списки комментов, узлы пересоздаются и атрибут
   теряется — код имеет text/cid fallback, ок.
4. **Trusted input через keyboard.insert_text** (фикс этого чата) — корректное решение
   для React-композера; эмодзи через insert_text (surrogate pairs) учтены.
5. **Reply-scoped guard**: «ответ не должен уйти как main-коммент» — последовательно
   протянут через publish_action/find_reply_input_smart (отдельные маркеры
   data-smart-reply-input/post, запрет fallback на глобальный композер).
6. **HTTP-preflight куки до браузера** (`_preflight_cookie_http_gate`) — дёшево
   отсеивает мёртвые аккаунты без запуска браузера.
7. **Капча-гейты** перед каждой дорогой фазой + manual-clear ожидание.
8. **shared_parent_cache** и batch-поиск родителей за один проход листа.

---

## ПРИОРИТЕТНЫЙ ПЛАН (если чинить — в этом порядке)

1. **K1+K2**: убрать `textContent=` fallback и `expected.includes(txt)` из верификации.
   Ввод = только trusted-пути (insert_text / type), верификация = строгое равенство
   нормализованного текста. Это убирает «серую кнопку» и обрубки.
2. **K7**: fail-fast в `wait_for_post_button_enabled` при стабильном `input_empty` (2-3 полла).
3. **K3**: перед Ctrl+A проверять `document.activeElement` внутри композера; убрать
   Tab/Shift+Tab из фокус-fallback'ов.
4. **K5+S8**: схлопнуть 9 проб инпута в 1-2 (oneshot_surface + один resolver),
   вынести labelOf/badMarkers/isCommentScoped в ОДИН переиспользуемый JS-сниппет
   (константа-префикс для evaluate). Сократит и время, и расхождения маркеров.
5. **K6/S9**: переиспользование живой залогиненной сессии для ретраев того же
   аккаунта (не закрывать context при retriable-ошибках типа input_not_found);
   ограничить enrichment одним разом на аккаунт за задачу (кэш «enriched_at»).
6. **S6/S10**: единый ProxyFailureClassifier + ротация мобильного IP из reply/engagement.
7. **K4**: убрать геометрический фильтр `top < 25%` — скоупить кнопку от композера.
8. **S1**: распил user_schema.py на модули (login_pipeline / main_phase / reply_phase /
   proxy_policy) — без этого каждое следующее изменение будет добавлять баги.

## Вердикт

Софт не «безнадёжно кривой» — каркас здравый (network-confirm, strict gate, anchor-токены,
preflight). Кривизна — в методе роста: каждый баг чинился ДОБАВЛЕНИЕМ нового слоя
(проба №9, fallback №4, sleep №84) вместо замены сломанного. Из-за этого:
(а) время = сумма бюджетов мёртвых слоёв; (б) fallback'и маскируют реальные провалы
(textContent, частичный матч) и превращают их в загадочные «no_network_request» позже;
(в) одна правка в 10k-строчном файле непредсказуемо задевает соседние слои.
Понимание устройства TikTok в коде в целом верное (contenteditable+React, data-e2e,
virtualized list, trust score/pointer-events) — проблема не в знаниях, а в наслоениях.
