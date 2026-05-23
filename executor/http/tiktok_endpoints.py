"""
TikTok API Endpoints.

Централизованное хранение всех эндпоинтов TikTok API.
Все эндпоинты должны быть определены здесь, без хардкодов в бизнес-логике.

ОБНОВЛЕНО: Реальные эндпоинты на основе curl команд.
"""
from typing import Optional, Dict, Any
from urllib.parse import urlencode


class TikTokEndpoints:
    """Класс с эндпоинтами TikTok API."""
    
    BASE_URL = "https://www.tiktok.com"
    API_BASE = "https://www.tiktok.com/api"
    
    @staticmethod
    def comment_publish(
        aweme_id: str,
        text: str,
        reply_id: Optional[str] = None,
        reply_to_reply_id: str = "0",
        video_url: Optional[str] = None,
        **extra_params
    ) -> str:
        """
        Эндпоинт для публикации комментария (главного или ответа).
        
        Args:
            aweme_id: ID видео (aweme_id)
            text: Текст комментария
            reply_id: ID родительского комментария (если это ответ)
            reply_to_reply_id: ID ответа на ответ (по умолчанию "0")
            **extra_params: Дополнительные query параметры
            
        Returns:
            str: URL эндпоинта с query параметрами
        """
        params = {
            "aid": "1988",
            "app_language": "ru-RU",
            "app_name": "tiktok_web",
            "aweme_id": aweme_id,
            "browser_language": "ru-RU",
            "browser_name": "Mozilla",
            "browser_online": "true",
            "browser_platform": "Win32",
            "browser_version": "5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/143.0.0.0 Safari/537.36",
            "channel": "tiktok_web",
            "cookie_enabled": "true",
            "data_collection_enabled": "true",
            "device_platform": "web_pc",
            "focus_state": "true",
            "from_page": "video",
            "is_fullscreen": "false",
            "is_page_visible": "true",
            "os": "windows",
            "priority_region": "US",
            "referer": "https://www.tiktok.com/",
            "region": "US",
            "root_referer": "https://www.tiktok.com/",
            "text": text,
            "text_extra": "[]",
            "tz_name": "Asia/Jerusalem",
            "user_is_login": "true",
            "webcast_language": "ru-RU",
        }
        
        # Устанавливаем referer - если есть video_url, используем его, иначе базовый URL
        if video_url:
            params["referer"] = video_url
        else:
            params["referer"] = "https://www.tiktok.com/"
        
        # Если это ответ - добавляем reply_id
        # ВАЖНО: reply_id должен быть строкой, не числом
        if reply_id:
            params["reply_id"] = str(reply_id)
            params["reply_to_reply_id"] = str(reply_to_reply_id)
        else:
            params["reply_id"] = "0"
            params["reply_to_reply_id"] = "0"
        
        # Добавляем дополнительные параметры (msToken, X-Bogus, X-Gnarly, verifyFp и т.д.)
        # ВАЖНО: X-Bogus и X-Gnarly должны быть в query params, а не в headers
        params.update(extra_params)
        
        # Добавляем стандартные параметры если их нет
        if "WebIdLastTime" not in params:
            import time
            params["WebIdLastTime"] = str(int(time.time()))
        if "history_len" not in params:
            params["history_len"] = "8"
        if "screen_height" not in params:
            params["screen_height"] = "864"
        if "screen_width" not in params:
            params["screen_width"] = "1536"
        
        query_string = urlencode(params, doseq=True)
        return f"{TikTokEndpoints.API_BASE}/comment/publish/?{query_string}"
    
    @staticmethod
    def comment_digg(
        aweme_id: str,
        cid: str,
        digg_type: int = 1,
        **extra_params
    ) -> str:
        """
        Эндпоинт для лайка комментария.
        
        Args:
            aweme_id: ID видео (aweme_id)
            cid: ID комментария (cid)
            digg_type: Тип лайка (1 = like, 0 = unlike)
            **extra_params: Дополнительные query параметры
            
        Returns:
            str: URL эндпоинта с query параметрами
        """
        params = {
            "aid": "1988",
            "app_language": "ru-RU",
            "app_name": "tiktok_web",
            "aweme_id": aweme_id,
            "browser_language": "ru-RU",
            "browser_name": "Mozilla",
            "browser_online": "true",
            "browser_platform": "Win32",
            "browser_version": "5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/143.0.0.0 Safari/537.36",
            "channel": "tiktok_web",
            "channel_id": "0",
            "cid": cid,
            "cookie_enabled": "true",
            "data_collection_enabled": "true",
            "device_id": "",  # Будет заполнено из cookies
            "device_platform": "web_pc",
            "digg_type": str(digg_type),
            "focus_state": "true",
            "from_page": "video",
            "is_fullscreen": "false",
            "is_page_visible": "true",
            "odinId": "",  # Будет заполнено из cookies
            "os": "windows",
            "priority_region": "US",
            "referer": "https://www.tiktok.com/",
            "region": "US",
            "root_referer": "https://www.tiktok.com/",
            "tz_name": "Asia/Jerusalem",
            "user_is_login": "true",
            "verifyFp": "",  # Будет заполнено из cookies
            "webcast_language": "ru-RU",
        }
        
        # Добавляем дополнительные параметры (msToken, X-Bogus, X-Gnarly и т.д.)
        # ВАЖНО: X-Bogus и X-Gnarly должны быть в query params, а не в headers
        params.update(extra_params)
        
        # Добавляем стандартные параметры если их нет
        if "WebIdLastTime" not in params:
            import time
            params["WebIdLastTime"] = str(int(time.time()))
        if "history_len" not in params:
            params["history_len"] = "7"
        if "screen_height" not in params:
            params["screen_height"] = "864"
        if "screen_width" not in params:
            params["screen_width"] = "1536"
        if "channel_id" not in params:
            params["channel_id"] = "0"
        
        query_string = urlencode(params, doseq=True)
        return f"{TikTokEndpoints.API_BASE}/comment/digg/?{query_string}"
    
    @staticmethod
    def comment_list(aweme_id: str, cursor: int = 0, count: int = 20) -> str:
        """
        Эндпоинт для получения списка комментариев.
        
        Args:
            aweme_id: ID видео
            cursor: Курсор для пагинации
            count: Количество комментариев
            
        Returns:
            str: URL эндпоинта
        """
        # TODO: Добавить полный набор параметров как в других эндпоинтах
        return f"{TikTokEndpoints.API_BASE}/comment/list/?aweme_id={aweme_id}&cursor={cursor}&count={count}"
