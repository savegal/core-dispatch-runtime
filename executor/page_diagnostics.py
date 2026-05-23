"""
Диагностика страницы TikTok - помогает найти актуальные селекторы.

Используйте эту функцию в браузере (PLAYWRIGHT_HEADLESS=False) чтобы увидеть
что реально есть на странице и какие селекторы работают.
"""
import logging
from patchright.async_api import Page
from typing import Dict, Any, List

logger = logging.getLogger("executor.page_diagnostics")


async def diagnose_page_structure(page: Page) -> Dict[str, Any]:
    """
    Полная диагностика структуры страницы TikTok.
    
    Возвращает информацию о всех элементах, которые могут быть связаны с комментариями.
    """
    if page.is_closed():
        return {"error": "Page is closed"}
    
    try:
        result = await page.evaluate("""
            () => {
                const info = {
                    url: window.location.href,
                    title: document.title,
                    readyState: document.readyState,
                    
                    // Секция комментариев
                    commentSections: [],
                    
                    // Поля ввода
                    inputs: [],
                    
                    // Кнопки
                    buttons: [],
                    
                    // Все элементы с data-e2e
                    dataE2e: [],
                    
                    // Все элементы с contenteditable
                    contentEditable: [],
                    
                    // Все textarea
                    textareas: [],
                    
                    // Элементы с классами содержащими "comment"
                    commentClasses: [],
                };
                
                // Ищем секции комментариев
                const sectionSelectors = [
                    '[data-e2e="comment-list"]',
                    '[data-e2e="comment-container"]',
                    '[class*="CommentList"]',
                    '[class*="CommentContainer"]',
                    '[class*="comment-section"]',
                    '[class*="Comment"]',
                ];
                
                sectionSelectors.forEach(sel => {
                    try {
                        const els = document.querySelectorAll(sel);
                        if (els.length > 0) {
                            info.commentSections.push({
                                selector: sel,
                                count: els.length,
                                visible: Array.from(els).filter(el => {
                                    const rect = el.getBoundingClientRect();
                                    return rect.width > 0 && rect.height > 0;
                                }).length,
                                firstElement: {
                                    tagName: els[0].tagName,
                                    className: els[0].className,
                                    id: els[0].id || null,
                                    dataE2e: els[0].getAttribute('data-e2e') || null,
                                }
                            });
                        }
                    } catch (e) {}
                });
                
                // Ищем поля ввода
                const inputSelectors = [
                    '[data-e2e="comment-input"]',
                    'textarea[placeholder*="comment" i]',
                    'textarea[placeholder*="комментарий" i]',
                    'div[contenteditable="true"]',
                    'div[contenteditable="true"][data-e2e*="comment"]',
                    'div[contenteditable="true"][data-e2e*="input"]',
                    'textarea',
                ];
                
                inputSelectors.forEach(sel => {
                    try {
                        const els = document.querySelectorAll(sel);
                        if (els.length > 0) {
                            info.inputs.push({
                                selector: sel,
                                count: els.length,
                                visible: Array.from(els).filter(el => {
                                    const rect = el.getBoundingClientRect();
                                    return rect.width > 0 && rect.height > 0;
                                }).length,
                                elements: Array.from(els).slice(0, 3).map(el => ({
                                    tagName: el.tagName,
                                    className: el.className.substring(0, 100),
                                    id: el.id || null,
                                    dataE2e: el.getAttribute('data-e2e') || null,
                                    placeholder: el.getAttribute('placeholder') || null,
                                    contentEditable: el.getAttribute('contenteditable') || null,
                                }))
                            });
                        }
                    } catch (e) {}
                });
                
                // Ищем кнопки отправки
                const buttonSelectors = [
                    '[data-e2e="comment-post"]',
                    '[data-e2e="post-comment-button"]',
                    'button[data-e2e*="post"]',
                    'div[data-e2e="comment-post"]',
                    'button:has-text("Post")',
                    'button:has-text("Опубликовать")',
                ];
                
                buttonSelectors.forEach(sel => {
                    try {
                        const els = document.querySelectorAll(sel);
                        if (els.length > 0) {
                            info.buttons.push({
                                selector: sel,
                                count: els.length,
                                visible: Array.from(els).filter(el => {
                                    const rect = el.getBoundingClientRect();
                                    return rect.width > 0 && rect.height > 0;
                                }).length,
                                elements: Array.from(els).slice(0, 3).map(el => ({
                                    tagName: el.tagName,
                                    className: el.className.substring(0, 100),
                                    id: el.id || null,
                                    dataE2e: el.getAttribute('data-e2e') || null,
                                    text: el.textContent?.substring(0, 50) || null,
                                }))
                            });
                        }
                    } catch (e) {}
                });
                
                // Все элементы с data-e2e содержащие "comment"
                try {
                    const allE2e = document.querySelectorAll('[data-e2e]');
                    Array.from(allE2e).forEach(el => {
                        const e2e = el.getAttribute('data-e2e');
                        if (e2e && e2e.toLowerCase().includes('comment')) {
                            info.dataE2e.push({
                                value: e2e,
                                tagName: el.tagName,
                                className: el.className.substring(0, 100),
                                visible: (() => {
                                    const rect = el.getBoundingClientRect();
                                    return rect.width > 0 && rect.height > 0;
                                })(),
                            });
                        }
                    });
                } catch (e) {}
                
                // Все contenteditable элементы
                try {
                    const allContentEditable = document.querySelectorAll('[contenteditable="true"]');
                    Array.from(allContentEditable).slice(0, 10).forEach(el => {
                        info.contentEditable.push({
                            tagName: el.tagName,
                            className: el.className.substring(0, 100),
                            id: el.id || null,
                            dataE2e: el.getAttribute('data-e2e') || null,
                            placeholder: el.getAttribute('placeholder') || null,
                            visible: (() => {
                                const rect = el.getBoundingClientRect();
                                return rect.width > 0 && rect.height > 0;
                            })(),
                        });
                    });
                } catch (e) {}
                
                // Все textarea
                try {
                    const allTextareas = document.querySelectorAll('textarea');
                    Array.from(allTextareas).forEach(el => {
                        info.textareas.push({
                            className: el.className.substring(0, 100),
                            id: el.id || null,
                            dataE2e: el.getAttribute('data-e2e') || null,
                            placeholder: el.getAttribute('placeholder') || null,
                            visible: (() => {
                                const rect = el.getBoundingClientRect();
                                return rect.width > 0 && rect.height > 0;
                            })(),
                        });
                    });
                } catch (e) {}
                
                // Элементы с классами содержащими "comment" (первые 20)
                try {
                    const allWithComment = document.querySelectorAll('[class*="comment" i], [class*="Comment"]');
                    Array.from(allWithComment).slice(0, 20).forEach(el => {
                        const rect = el.getBoundingClientRect();
                        info.commentClasses.push({
                            className: el.className.substring(0, 150),
                            tagName: el.tagName,
                            dataE2e: el.getAttribute('data-e2e') || null,
                            visible: rect.width > 0 && rect.height > 0,
                        });
                    });
                } catch (e) {}
                
                return info;
            }
        """)
        
        return result
    except Exception as e:
        return {"error": str(e)}


async def log_page_diagnostics(page: Page, stage: str = "unknown"):
    """
    Логирует полную диагностику страницы.
    
    Используйте эту функцию в коде для отладки.
    """
    diag = await diagnose_page_structure(page)
    
    logger.info(f"[DIAGNOSTICS] Stage: {stage}")
    logger.info(f"[DIAGNOSTICS] URL: {diag.get('url', 'N/A')}")
    logger.info(f"[DIAGNOSTICS] ReadyState: {diag.get('readyState', 'N/A')}")
    
    # Секции комментариев
    sections = diag.get('commentSections', [])
    if sections:
        logger.info(f"[DIAGNOSTICS] Comment sections found: {len(sections)}")
        for sec in sections:
            logger.info(f"  - {sec['selector']}: {sec['count']} total, {sec['visible']} visible")
    else:
        logger.warning("[DIAGNOSTICS] No comment sections found!")
    
    # Поля ввода
    inputs = diag.get('inputs', [])
    if inputs:
        logger.info(f"[DIAGNOSTICS] Input fields found: {len(inputs)}")
        for inp in inputs:
            logger.info(f"  - {inp['selector']}: {inp['count']} total, {inp['visible']} visible")
            for el in inp.get('elements', [])[:2]:
                logger.info(f"    Element: {el['tagName']}, data-e2e={el.get('dataE2e')}, placeholder={el.get('placeholder')}")
    else:
        logger.warning("[DIAGNOSTICS] No input fields found!")
    
    # Кнопки
    buttons = diag.get('buttons', [])
    if buttons:
        logger.info(f"[DIAGNOSTICS] Post buttons found: {len(buttons)}")
        for btn in buttons:
            logger.info(f"  - {btn['selector']}: {btn['count']} total, {btn['visible']} visible")
    else:
        logger.warning("[DIAGNOSTICS] No post buttons found!")
    
    # data-e2e с "comment"
    dataE2e = diag.get('dataE2e', [])
    if dataE2e:
        logger.info(f"[DIAGNOSTICS] data-e2e with 'comment': {len(dataE2e)}")
        for item in dataE2e[:5]:
            logger.info(f"  - {item['value']} ({item['tagName']}, visible={item['visible']})")
    
    # contenteditable
    contentEditable = diag.get('contentEditable', [])
    if contentEditable:
        logger.info(f"[DIAGNOSTICS] contenteditable elements: {len(contentEditable)}")
        for el in contentEditable[:3]:
            logger.info(f"  - {el['tagName']}, data-e2e={el.get('dataE2e')}, visible={el['visible']}")
    
    return diag
