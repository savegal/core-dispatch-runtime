"""
HTTP Context - контекст для HTTP-запросов к TikTok API.

Содержит cookies, headers, proxy и account_id для выполнения HTTP-запросов.
"""
from typing import Optional, Dict, Any
from dataclasses import dataclass
from db.models import Account


@dataclass
class HttpContext:
    """Контекст для HTTP-запросов к TikTok API."""
    account_id: int
    cookies: Dict[str, str]
    headers: Dict[str, str]
    proxy: Optional[str] = None
    csrf_token: Optional[str] = None
    ms_token: Optional[str] = None
    verify_fp: Optional[str] = None
    device_id: Optional[str] = None
    odin_id: Optional[str] = None
    web_id_last_time: Optional[str] = None
    
    @classmethod
    def from_account(cls, account: Account, cookies: Optional[Dict[str, str]] = None, 
                     headers: Optional[Dict[str, str]] = None) -> "HttpContext":
        """
        Создает HttpContext из Account.
        
        Args:
            account: Аккаунт из БД
            cookies: Опциональные cookies (если не указаны, будут извлечены из account.cookies)
            headers: Опциональные headers (если не указаны, будут созданы базовые)
        """
        # Извлекаем cookies из account.cookies или cookie_file
        if cookies is None:
            from services.cookie_validator import get_cookies_for_account, parse_cookies_to_dict
            cookies_str = get_cookies_for_account(account)
            cookies_dict = parse_cookies_to_dict(cookies_str) if cookies_str else {}
        else:
            cookies_dict = cookies
        
        # Извлекаем важные токены из cookies
        csrf_token = cookies_dict.get("csrf_token") or cookies_dict.get("s_v_web_id") or cookies_dict.get("sid_tt")
        # msToken может быть несколько в cookies (берем последний)
        ms_tokens = [v for k, v in cookies_dict.items() if k == "msToken"]
        ms_token = ms_tokens[-1] if ms_tokens else None
        verify_fp = cookies_dict.get("verifyFp") or cookies_dict.get("s_v_web_id")
        device_id = cookies_dict.get("device_id")
        odin_id = cookies_dict.get("odinId") or cookies_dict.get("odin_tt")
        web_id_last_time = cookies_dict.get("WebIdLastTime")
        
        # Извлекаем дополнительные важные cookies
        # sid_tt, sessionid, sid_guard - критичные для авторизации
        # tt_csrf_token - для заголовка tt-csrf-token
        
        # Базовые headers (из реальных curl команд)
        headers_dict = headers or {
            "accept": "*/*",
            "accept-language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
            "cache-control": "no-cache",
            "content-type": "application/x-www-form-urlencoded",
            "origin": "https://www.tiktok.com",
            "pragma": "no-cache",
            "priority": "u=1, i",
            "referer": "https://www.tiktok.com/",
            "sec-ch-ua": '"Google Chrome";v="143", "Chromium";v="143", "Not A(Brand";v="24"',
            "sec-ch-ua-mobile": "?0",
            "sec-ch-ua-platform": '"Windows"',
            "sec-fetch-dest": "empty",
            "sec-fetch-mode": "cors",
            "sec-fetch-site": "same-origin",
            "user-agent": account.user_agent or "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/143.0.0.0 Safari/537.36",
            "x-secsdk-csrf-token": "DOWNGRADE",  # Для like
            "x-secsdk-csrf-request": "1",  # Для publish
            "x-secsdk-csrf-version": "1.2.22",  # Для publish
        }
        
        # Добавляем tt-csrf-token из cookies если есть
        tt_csrf_token = cookies_dict.get("tt_csrf_token")
        if tt_csrf_token:
            headers_dict["tt-csrf-token"] = tt_csrf_token
        
        return cls(
            account_id=account.id,
            cookies=cookies_dict,
            headers=headers_dict,
            proxy=account.proxy,
            csrf_token=csrf_token,
            ms_token=ms_token,
            verify_fp=verify_fp,
            device_id=device_id,
            odin_id=odin_id,
            web_id_last_time=web_id_last_time
        )
    
    def to_requests_kwargs(self, request_type: str = "like") -> Dict[str, Any]:
        """
        Преобразует HttpContext в kwargs для requests.
        
        Args:
            request_type: Тип запроса ("like", "publish", "reply") - для выбора правильных headers
        
        Returns:
            dict: kwargs для requests.get/post
        """
        # Формируем строку cookies в формате "key=value; key2=value2"
        cookies_str = "; ".join([f"{k}={v}" for k, v in self.cookies.items()])
        
        headers_dict = self.headers.copy()
        
        # Настраиваем headers в зависимости от типа запроса
        if request_type in ("like", "reply"):
            # Для like и reply используем x-secsdk-csrf-token: DOWNGRADE
            # ВАЖНО: reply использует те же headers что и like (из curl команды)
            headers_dict["x-secsdk-csrf-token"] = "DOWNGRADE"
            # Убираем publish-specific headers
            headers_dict.pop("x-secsdk-csrf-request", None)
            headers_dict.pop("x-secsdk-csrf-version", None)
        elif request_type == "publish":
            # Для publish главного комментария используем x-secsdk-csrf-request и version
            headers_dict["x-secsdk-csrf-request"] = "1"
            headers_dict["x-secsdk-csrf-version"] = "1.2.22"
            # Убираем like-specific header
            headers_dict.pop("x-secsdk-csrf-token", None)
        
        kwargs: Dict[str, Any] = {
            "headers": headers_dict,
        }
        
        # Добавляем cookies как строку в заголовок Cookie (как в curl)
        if cookies_str:
            kwargs["headers"]["Cookie"] = cookies_str
        
        # Также добавляем как dict для requests (на случай если библиотека использует)
        kwargs["cookies"] = self.cookies.copy()
        
        if self.proxy:
            kwargs["proxies"] = {
                "http": self.proxy,
                "https": self.proxy,
            }
        
        return kwargs
