# Как найти актуальные селекторы для TikTok

Если софт не находит элементы на странице, используйте эту инструкцию.

## Шаг 1: Включите видимый браузер

В `.env` или `config/settings.py`:
```python
PLAYWRIGHT_HEADLESS=False
```

## Шаг 2: Запустите задачу

Запустите executor и создайте задачу. Браузер откроется и вы увидите что происходит.

## Шаг 3: Откройте DevTools в браузере

1. В открывшемся браузере нажмите `F12` или `Ctrl+Shift+I`
2. Перейдите на вкладку **Console**
3. Вставьте и выполните этот код:

```javascript
// Диагностика секции комментариев
console.log("=== COMMENT SECTIONS ===");
const sections = document.querySelectorAll('[data-e2e*="comment"], [class*="Comment"], [id*="comment" i]');
sections.forEach((el, i) => {
    const rect = el.getBoundingClientRect();
    console.log(`${i+1}. Tag: ${el.tagName}, data-e2e: ${el.getAttribute('data-e2e')}, class: ${el.className.substring(0, 100)}, visible: ${rect.width > 0 && rect.height > 0}`);
});

// Диагностика полей ввода
console.log("\n=== INPUT FIELDS ===");
const inputs = document.querySelectorAll('[data-e2e*="input"], [data-e2e*="comment"], textarea, div[contenteditable="true"]');
inputs.forEach((el, i) => {
    const rect = el.getBoundingClientRect();
    console.log(`${i+1}. Tag: ${el.tagName}, data-e2e: ${el.getAttribute('data-e2e')}, placeholder: ${el.getAttribute('placeholder')}, contenteditable: ${el.getAttribute('contenteditable')}, visible: ${rect.width > 0 && rect.height > 0}`);
});

// Диагностика кнопок отправки
console.log("\n=== POST BUTTONS ===");
const buttons = document.querySelectorAll('[data-e2e*="post"], button[data-e2e*="comment"], div[data-e2e*="post"]');
buttons.forEach((el, i) => {
    const rect = el.getBoundingClientRect();
    console.log(`${i+1}. Tag: ${el.tagName}, data-e2e: ${el.getAttribute('data-e2e')}, text: ${el.textContent?.substring(0, 50)}, visible: ${rect.width > 0 && rect.height > 0}`);
});

// Все элементы с data-e2e
console.log("\n=== ALL data-e2e ATTRIBUTES ===");
const allE2e = document.querySelectorAll('[data-e2e]');
const e2eValues = new Set();
allE2e.forEach(el => {
    const val = el.getAttribute('data-e2e');
    if (val && val.toLowerCase().includes('comment')) {
        e2eValues.add(val);
    }
});
console.log(Array.from(e2eValues).sort());
```

## Шаг 4: Скопируйте результаты

Скопируйте вывод из консоли и отправьте мне. Я обновлю селекторы.

## Шаг 5: Альтернатива - используйте встроенную диагностику

Софт теперь автоматически логирует диагностику при ошибках. Смотрите логи:

```
[DIAGNOSTICS] Stage: comment_section_not_found
[DIAGNOSTICS] Comment sections found: ...
[DIAGNOSTICS] Input fields found: ...
```

## Что искать:

1. **Секция комментариев:**
   - Элемент с `data-e2e="comment-list"` или похожим
   - Элемент с классом содержащим "Comment" или "comment"

2. **Поле ввода:**
   - `textarea` с placeholder содержащим "comment"
   - `div[contenteditable="true"]` с `data-e2e*="comment"` или `data-e2e*="input"`
   - Любой элемент с `data-e2e="comment-input"`

3. **Кнопка отправки:**
   - `div[data-e2e="comment-post"]` или `button[data-e2e*="post"]`
   - Элемент с текстом "Post" или "Опубликовать"

## Примеры актуальных селекторов (обновляйте по мере изменения TikTok):

```python
# Секция комментариев
COMMENT_SECTION = '[data-e2e="comment-list"]'

# Поле ввода
COMMENT_INPUT = 'div[contenteditable="true"][data-e2e="comment-input"]'

# Кнопка отправки
POST_BUTTON = 'div[data-e2e="comment-post"][role="button"]'
```
