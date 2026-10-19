import logging
import re
from typing import Optional, Dict, Any

from patchright.async_api import Page
from urllib.parse import urlparse, urlencode, parse_qsl, urlunparse

from executor.http.playwright_http_client import PlaywrightHttpClient
from executor.http.request_profile import RequestProfile
from executor.http.signature_runtime import build_unsigned_request_url

logger = logging.getLogger("executor.tiktok.network_client")


class NetworkClientError(Exception):
    pass


async def generate_x_bogus(page: Page, url: str) -> Optional[str]:
    """
    Пытается сгенерировать X-Bogus подпись для URL через JS-инъекцию в контекст страницы TikTok.
    """
    try:
        # Простой JS-скрипт, который пытается найти функцию sign в window.byted_acrawler
        js_code = """
        (url) => {
            try {
                if (window.byted_acrawler && typeof window.byted_acrawler.sign === 'function') {
                    // Возвращаем X-Bogus строку
                    return window.byted_acrawler.sign({url: url}) || null;
                }
                return null;
            } catch (e) {
                return null;
            }
        }
        """
        x_bogus = await page.evaluate(js_code, url)
        if x_bogus:
            logger.debug(f"[X-BOGUS] Успешно сгенерирована подпись через JS: {x_bogus[:10]}...")
            return x_bogus
        else:
            logger.warning("[X-BOGUS] Функция window.byted_acrawler.sign не найдена на странице.")
            return None
    except Exception as e:
        logger.warning(f"[X-BOGUS] Ошибка при инъекции JS: {e}")
        return None


def _cookies_list_to_dict(cookies_list) -> Dict[str, str]:
    """
    Преобразует список cookies Playwright в dict name->value.
    """
    cookies_dict: Dict[str, str] = {}
    for c in cookies_list or []:
        name = c.get("name")
        value = c.get("value")
        if name and value is not None:
            cookies_dict[name] = value
    return cookies_dict


async def publish_comment_via_network(page: Page, text: str, artifacts: Dict[str, Any]) -> Optional[str]:
    """
    Network-first publish. Использует PlaywrightHttpClient и динамическую генерацию X-Bogus.
    
    Args:
        page: Playwright Page (откуда берется контекст и куда внедряется X-Bogus)
        text: Текст комментария
        artifacts: Артефакты (aweme_id, csrf, msToken и т.д.)
        
    Returns:
        Optional[str]: ID комментария при успехе, иначе None
    """
    try:
        if not artifacts or "aweme_id" not in artifacts:
            logger.warning("[NETWORK] Недостаточно артефактов для HTTP-публикации.")
            return None
        
        aweme_id = artifacts["aweme_id"]
        video_url = artifacts.get("video_url")

        # Шаг 1: собираем cookies из контекста для консистентного URL
        cookies_list = await page.context.cookies()
        cookies_dict = _cookies_list_to_dict(cookies_list)

        # Делегируем подпись POST-запроса (query + form body) нативному window.fetch страницы
        logger.debug("[NETWORK] Инициализация PlaywrightHttpClient из page...")
        http_client = await PlaywrightHttpClient.from_page(page)

        success, comment_id, error_msg, error_type = await http_client.publish_main_comment(
            aweme_id=aweme_id,
            text=text,
            video_url=video_url,
            cookies=cookies_dict,
        )
        
        if success and comment_id:
            logger.info(f"[NETWORK] Комментарий успешно опубликован через HTTP! ID: {comment_id}")
            return str(comment_id)
        else:
            logger.warning(f"[NETWORK] Ошибка публикации через HTTP: {error_msg} (Type: {error_type})")
            return None
            
    except Exception as e:
        logger.warning(f"[NETWORK] Критическая ошибка publish_comment_via_network: {e}")
        return None


async def fetch_comment_list(page, video_id: str, artifacts: Dict[str, Any]):
    """
    Network fetch comments (stub).
    """
    # TODO: implement after endpoint discovery
    return None


async def like_comment_via_network(page, comment_id: str, artifacts: Dict[str, Any]):
    """
    Network like (stub).
    """
    return None


async def reply_comment_via_network(page, parent_comment_id: str, text: str, artifacts: Dict[str, Any]):
    """
    Network reply (stub).
    """
    try:
        if not artifacts or "aweme_id" not in artifacts:
            logger.warning("[NETWORK] Недостаточно артефактов для HTTP-ответа.")
            return None

        aweme_id = artifacts["aweme_id"]
        video_url = artifacts.get("video_url")

        cookies_list = await page.context.cookies()
        cookies_dict = _cookies_list_to_dict(cookies_list)

        http_client = await PlaywrightHttpClient.from_page(page)
        success, comment_id, error_msg, error_type, _reply_meta = await http_client.reply_comment(
            aweme_id=aweme_id,
            parent_comment_id=parent_comment_id,
            text=text,
            video_url=video_url,
            cookies=cookies_dict,
        )

        if success and comment_id:
            logger.info(f"[NETWORK] Ответ опубликован через HTTP. ID: {comment_id}")
            return str(comment_id)
        else:
            logger.warning(f"[NETWORK] Ошибка HTTP-ответа: {error_msg} (Type: {error_type})")
            return None

    except Exception as e:
        logger.warning(f"[NETWORK] Критическая ошибка reply_comment_via_network: {e}")
        return None
