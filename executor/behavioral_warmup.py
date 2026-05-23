"""
ИСПРАВЛЕНО: Поведенческие улучшения для имитации человеческого поведения.

Advanced Layer: "Doom Scrolling" - лайки чужих комментариев перед своим для повышения Trust Score.

КРИТИЧНО: Реальный пользователь не сразу пишет комментарий - он сначала:
1. Скроллит вниз (читает другие комментарии)
2. Лайкает пару чужих комментариев (это сильно повышает траст)
3. Возвращается наверх или пишет внизу

Это "смешивает" аккаунт с толпой и снижает вероятность детекции.
"""
import random
import logging
from typing import Optional, Tuple
from patchright.async_api import Page
from executor.comment_utils import log_step, check_page_alive
from executor.tiktok_selectors import selectors

logger = logging.getLogger("executor.behavioral_warmup")


async def like_random_comment_before_action(
    page: Page,
    account_id: int,
    using_proxy: bool = False,
    probability: float = 0.3
) -> Tuple[bool, Optional[str]]:
    """
    ИСПРАВЛЕНО: Лайкает случайный чужой комментарий перед написанием своего (Advanced Layer).
    
    Это имитирует "Doom Scrolling" - поведение реального пользователя, который
    сначала читает и лайкает чужие комментарии, а потом пишет свой.
    
    Args:
        page: Страница Playwright
        account_id: ID аккаунта (для логирования)
        using_proxy: True если используется прокси
        probability: Вероятность выполнения (0.3 = 30%)
        
    Returns:
        tuple[bool, str | None]: (успешно ли лайкнули, comment_id или None)
    """
    # Проверяем вероятность
    if random.random() > probability:
        logger.debug(f"[BEHAVIORAL] Skipping random like (probability check failed)")
        return False, None
    
    if not page or page.is_closed():
        return False, None
    
    try:
        log_step("BEHAVIORAL_WARMUP_START", {"action": "like_random_comment"})
        
        # Проверяем, что страница жива
        is_alive, error = await check_page_alive(page)
        if not is_alive:
            return False, None
        
        # Ищем корневые контейнеры комментариев
        comment_roots = await page.query_selector_all(selectors.COMMENT_ROOT)
        if not comment_roots or len(comment_roots) == 0:
            logger.debug(f"[BEHAVIORAL] No comments found for random like")
            return False, None
        
        # Выбираем случайный комментарий (не первый, чтобы не лайкать автору видео)
        if len(comment_roots) <= 1:
            logger.debug(f"[BEHAVIORAL] Only 1 comment found, skipping random like")
            return False, None
        
        # Выбираем случайный комментарий из первых 5-10 (чтобы не скроллить слишком много)
        max_index = min(len(comment_roots), 10)
        random_index = random.randint(1, max_index - 1)  # Пропускаем первый (обычно автор)
        comment_root = comment_roots[random_index]
        
        # Проверяем видимость
        is_visible = await comment_root.is_visible()
        if not is_visible:
            # Скроллим к комментарию
            try:
                await comment_root.scroll_into_view_if_needed()
                await page.wait_for_timeout(500)
                is_visible = await comment_root.is_visible()
            except Exception:
                return False, None
        
        if not is_visible:
            return False, None
        
        # Ищем кнопку лайка внутри комментария
        like_button = await comment_root.query_selector(selectors.LIKE_BUTTON)
        if not like_button:
            # Пробуем fallback селектор
            like_button = await comment_root.query_selector(selectors.LIKE_BUTTON_FALLBACK)
        
        if not like_button:
            logger.debug(f"[BEHAVIORAL] Like button not found in random comment")
            return False, None
        
        # Проверяем, не лайкнут ли уже
        is_liked = await like_button.evaluate("""
            el => {
                const ariaPressed = el.getAttribute('aria-pressed');
                const className = el.className || '';
                if (ariaPressed === 'true' || className.includes('liked') || className.includes('active')) {
                    return true;
                }
                return false;
            }
        """)
        
        if is_liked:
            logger.debug(f"[BEHAVIORAL] Random comment already liked")
            return False, None
        
        # Лайкаем комментарий
        try:
            await like_button.click(timeout=3000)
            await page.wait_for_timeout(500)  # Небольшая задержка для обработки
            
            # Проверяем успешность
            is_liked_now = await like_button.evaluate("""
                el => {
                    const ariaPressed = el.getAttribute('aria-pressed');
                    const className = el.className || '';
                    if (ariaPressed === 'true' || className.includes('liked') || className.includes('active')) {
                        return true;
                    }
                    return false;
                }
            """)
            
            if is_liked_now:
                # Извлекаем comment_id для логирования
                comment_id = await comment_root.get_attribute("data-cid") or \
                           await comment_root.get_attribute("data-id") or \
                           await comment_root.get_attribute("data-comment-id")
                
                log_step("BEHAVIORAL_WARMUP_SUCCESS", {
                    "action": "like_random_comment",
                    "comment_id": comment_id[:20] if comment_id else None,
                    "index": random_index
                })
                logger.debug(f"[BEHAVIORAL] Successfully liked random comment {random_index} for account {account_id}")
                return True, str(comment_id) if comment_id else None
            else:
                logger.debug(f"[BEHAVIORAL] Like failed (bounced) for random comment")
                return False, None
                
        except Exception as like_error:
            logger.debug(f"[BEHAVIORAL] Error liking random comment: {like_error}")
            return False, None
        
    except Exception as e:
        logger.debug(f"[BEHAVIORAL] Error in behavioral warmup: {e}")
        return False, None


async def scroll_comments_before_action(
    page: Page,
    using_proxy: bool = False
) -> bool:
    """
    ИСПРАВЛЕНО: Скроллит комментарии вниз перед написанием своего (Advanced Layer).
    
    Имитирует поведение реального пользователя, который сначала читает комментарии.
    
    Args:
        page: Страница Playwright
        using_proxy: True если используется прокси
        
    Returns:
        bool: True если скролл выполнен успешно
    """
    try:
        log_step("BEHAVIORAL_SCROLL_START", {"action": "scroll_comments_down"})
        
        # Находим контейнер комментариев
        comment_list = await page.query_selector('[data-e2e="comment-list"], [class*="CommentList"]')
        if comment_list:
            # Скроллим контейнер комментариев
            await comment_list.evaluate("""
                el => {
                    el.scrollTop += el.clientHeight * 0.5;
                }
            """)
        else:
            # Fallback: скроллим страницу
            await page.evaluate("window.scrollBy(0, 500)")
        
        # Небольшая задержка для загрузки новых комментариев
        await page.wait_for_timeout(800 if using_proxy else 500)
        
        log_step("BEHAVIORAL_SCROLL_SUCCESS")
        return True
        
    except Exception as e:
        logger.debug(f"[BEHAVIORAL] Error scrolling comments: {e}")
        return False
