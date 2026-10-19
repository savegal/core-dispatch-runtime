"""
BAS (Browser Automation Studio) клиент для TikTok автоматизации через BAS Remote.
Использует официальную библиотеку bas-remote-python для вызова функций из BAS скриптов.
"""
import json
import logging
import asyncio
import time
from typing import Optional, Dict, Any
from db.models import Account
from config.settings import get_settings
from executor.tiktok_client_async import parse_cookies, TikTokClientError
from services.log_sanitizer import sanitize_url

logger = logging.getLogger("executor.bas_client")
settings = get_settings()

try:
    from bas_remote import BasRemoteClient, Options
    BAS_REMOTE_AVAILABLE = True
except ImportError:
    BAS_REMOTE_AVAILABLE = False
    logger.warning("bas-remote-python not installed. Install with: pip install bas-remote-python")


class BASClientError(TikTokClientError):
    """Исключение для ошибок BAS клиента."""
    pass


class BASClient:
    """
    Клиент для работы с Browser Automation Studio через BAS Remote.
    
    BAS Remote позволяет вызывать функции из BAS скриптов напрямую из Python.
    """
    
    def __init__(
        self,
        script_name: str | None = None,
        timeout: int = 300
    ):
        """
        Инициализирует BAS клиент.
        
        Args:
            script_name: Имя скомпилированного BAS скрипта (по умолчанию из settings)
            timeout: Таймаут для операций в секундах
        """
        if not BAS_REMOTE_AVAILABLE:
            raise BASClientError(
                "bas-remote-python not installed. "
                "Install with: pip install bas-remote-python"
            )
        
        self.script_name = script_name or getattr(settings, 'BAS_SCRIPT_NAME', 'TikTokAutomation')
        self.timeout = timeout
        self._client: Optional[BasRemoteClient] = None
        
        logger.info(f"BAS Remote client initialized: script_name={self.script_name}")
    
    async def _ensure_connected(self):
        """Обеспечивает подключение к BAS Remote."""
        if self._client is None:
            try:
                options = Options(script_name=self.script_name)
                self._client = BasRemoteClient(options)
                await self._client.start()
                logger.debug(f"[BAS] Connected to script: {self.script_name}")
            except Exception as e:
                raise BASClientError(f"Failed to connect to BAS Remote: {str(e)[:200]}")
    
    async def close(self):
        """Закрывает подключение к BAS Remote."""
        if self._client:
            try:
                await self._client.close()
            except Exception:
                pass
            finally:
                self._client = None
    
    async def _call_function(
        self,
        function_name: str,
        params: Dict[str, Any]
    ) -> Dict[str, Any]:
        """
        Вызывает функцию из BAS скрипта.
        
        Args:
            function_name: Имя функции в BAS скрипте
            params: Параметры для функции
            
        Returns:
            dict: Результат выполнения функции
            
        Raises:
            BASClientError: При ошибке выполнения
        """
        await self._ensure_connected()
        
        try:
            logger.debug(f"[BAS] Calling function: {function_name}, params: {list(params.keys())}")
            
            result = await asyncio.wait_for(
                self._client.run_function(function_name, params),
                timeout=self.timeout
            )
            
            # BAS Remote возвращает результат напрямую
            if isinstance(result, dict):
                return result
            elif isinstance(result, str):
                # Если результат строка, пробуем распарсить как JSON
                try:
                    return json.loads(result)
                except Exception:
                    return {"result": result}
            else:
                return {"result": result}
                
        except asyncio.TimeoutError:
            raise BASClientError(f"BAS function timeout after {self.timeout}s: {function_name}")
        except Exception as e:
            error_msg = str(e)
            logger.error(f"[BAS] Function error: {function_name}, error: {error_msg[:200]}")
            raise BASClientError(f"BAS function error: {error_msg[:200]}")
    
    def _prepare_cookies_for_bas(self, account: Account) -> str:
        """
        Подготавливает cookies для BAS в формате JSON строки.
        
        Args:
            account: Аккаунт из БД
            
        Returns:
            str: JSON строка с cookies
        """
        try:
            cookies_list = parse_cookies(account.cookies or "")
            return json.dumps(cookies_list)
        except Exception as e:
            raise BASClientError(f"Failed to parse cookies: {str(e)[:200]}")
    
    def _prepare_proxy_for_bas(self, proxy_str: Optional[str]) -> Optional[Dict[str, str]]:
        """
        Подготавливает прокси для BAS.
        
        Args:
            proxy_str: Строка прокси (формат: http://user:pass@host:port или socks5://...)
            
        Returns:
            dict: Параметры прокси для BAS или None
        """
        if not proxy_str or not proxy_str.strip():
            return None
        
        proxy_str = proxy_str.strip()
        
        try:
            # Парсим прокси
            if "://" in proxy_str:
                protocol, rest = proxy_str.split("://", 1)
            else:
                protocol = "http"
                rest = proxy_str
            
            # Проверяем авторизацию
            if "@" in rest:
                auth_part, server_part = rest.rsplit("@", 1)
                if ":" in auth_part:
                    username, password = auth_part.split(":", 1)
                else:
                    username = auth_part
                    password = ""
            else:
                username = None
                password = None
                server_part = rest
            
            # Парсим host:port
            if ":" in server_part:
                host, port_str = server_part.rsplit(":", 1)
                try:
                    port = int(port_str)
                except ValueError:
                    port = 8080
            else:
                host = server_part
                port = 8080
            
            proxy_dict = {
                "type": protocol.lower(),
                "host": host,
                "port": port
            }
            
            if username:
                proxy_dict["username"] = username
            if password:
                proxy_dict["password"] = password
            
            return proxy_dict
            
        except Exception as e:
            logger.warning(
                "Failed to parse proxy %s: %s",
                sanitize_url(str(proxy_str or ""), max_length=80),
                e,
            )
            return None
    
    async def post_comment(
        self,
        account: Account,
        video_url: str,
        text: str,
        task_id: int | None = None
    ) -> str:
        """
        Публикует комментарий к видео через BAS.
        
        Args:
            account: Аккаунт из БД
            video_url: URL видео TikTok
            text: Текст комментария
            task_id: Опциональный ID задачи (для логирования)
            
        Returns:
            str: ID комментария (генерируется как pseudo-id)
            
        Raises:
            BASClientError: При ошибке
        """
        if not account.label:
            raise ValueError(f"Account {account.id} has no label")
        
        if not text or not text.strip():
            raise ValueError("Comment text cannot be empty")
        
        if not video_url or not video_url.strip():
            raise ValueError("Video URL cannot be empty")
        
        video_url_short = video_url[:50] + "..." if len(video_url) > 50 else video_url
        task_info = f"task_id={task_id}, " if task_id else ""
        
        logger.info(
            f"[BAS COMMENT] Starting: {task_info}account_id={account.id}, "
            f"label={account.label[:20]}, video_url={video_url_short}, comment_len={len(text)}"
        )
        
        try:
            # Подготавливаем параметры
            cookies_json = self._prepare_cookies_for_bas(account)
            proxy_dict = self._prepare_proxy_for_bas(account.proxy)
            
            params = {
                "video_url": video_url,
                "comment_text": text,
                "cookies": cookies_json,
                "user_agent": account.user_agent or None
            }
            
            if proxy_dict:
                params["proxy"] = json.dumps(proxy_dict)
            
            # Вызываем функцию из BAS скрипта
            result = await self._call_function("TikTokPostComment", params)
            
            # Генерируем comment_id (BAS может вернуть свой, или используем наш)
            comment_id = result.get("comment_id") or f"{account.id}-{int(time.time())}"
            
            logger.info(
                f"[BAS COMMENT] Success: {task_info}account_id={account.id}, "
                f"label={account.label[:20]}, comment_id={comment_id[:20]}"
            )
            
            return comment_id
            
        except BASClientError:
            raise
        except Exception as e:
            error_msg = str(e)
            logger.error(
                f"[BAS COMMENT] Error: {task_info}account_id={account.id}, "
                f"label={account.label[:20]}, error={error_msg[:200]}"
            )
            raise BASClientError(f"Failed to post comment: {error_msg}")
    
    async def post_reply(
        self,
        account: Account,
        video_url: str,
        parent_comment_id: str | None,
        text: str,
        task_id: int | None = None
    ) -> None:
        """
        Публикует ответ на комментарий через BAS.
        
        Args:
            account: Аккаунт из БД
            video_url: URL видео TikTok
            parent_comment_id: ID родительского комментария
            text: Текст ответа
            task_id: Опциональный ID задачи (для логирования)
            
        Raises:
            BASClientError: При ошибке
        """
        if not account.label:
            raise ValueError(f"Account {account.id} has no label")
        
        if not text or not text.strip():
            raise ValueError("Reply text cannot be empty")
        
        if not video_url or not video_url.strip():
            raise ValueError("Video URL cannot be empty")
        
        video_url_short = video_url[:50] + "..." if len(video_url) > 50 else video_url
        task_info = f"task_id={task_id}, " if task_id else ""
        
        logger.info(
            f"[BAS REPLY] Starting: {task_info}account_id={account.id}, "
            f"label={account.label[:20]}, video_url={video_url_short}, "
            f"parent_id={parent_comment_id[:20] if parent_comment_id else 'N/A'}, reply_len={len(text)}"
        )
        
        try:
            # Подготавливаем параметры
            cookies_json = self._prepare_cookies_for_bas(account)
            proxy_dict = self._prepare_proxy_for_bas(account.proxy)
            
            params = {
                "video_url": video_url,
                "parent_comment_id": parent_comment_id or "",
                "reply_text": text,
                "cookies": cookies_json,
                "user_agent": account.user_agent or None
            }
            
            if proxy_dict:
                params["proxy"] = json.dumps(proxy_dict)
            
            # Вызываем функцию из BAS скрипта
            await self._call_function("TikTokPostReply", params)
            
            logger.info(
                f"[BAS REPLY] Success: {task_info}account_id={account.id}, "
                f"label={account.label[:20]}"
            )
            
        except BASClientError:
            raise
        except Exception as e:
            error_msg = str(e)
            logger.error(
                f"[BAS REPLY] Error: {task_info}account_id={account.id}, "
                f"label={account.label[:20]}, error={error_msg[:200]}"
            )
            raise BASClientError(f"Failed to post reply: {error_msg}")
    
    async def like_comment(
        self,
        account: Account,
        comment_id: str,
        task_id: int | None = None,
        video_url: str | None = None
    ) -> bool:
        """
        Ставит лайк комментарию через BAS.
        
        Args:
            account: Аккаунт из БД
            comment_id: ID комментария
            task_id: Опциональный ID задачи (для логирования)
            video_url: URL видео (обязательно)
            
        Returns:
            bool: True при успехе, False при ошибке
        """
        if not account.label:
            raise ValueError(f"Account {account.id} has no label")
        
        if not video_url:
            logger.warning(
                f"[BAS LIKE] video_url not provided, cannot locate comment. "
                f"Comment_id={comment_id[:20]}, account_id={account.id}"
            )
            return False
        
        task_info = f"task_id={task_id}, " if task_id else ""
        
        logger.info(
            f"[BAS LIKE] Starting: {task_info}account_id={account.id}, "
            f"label={account.label[:20]}, comment_id={comment_id[:20]}, video_url={video_url[:50]}"
        )
        
        try:
            # Подготавливаем параметры
            cookies_json = self._prepare_cookies_for_bas(account)
            proxy_dict = self._prepare_proxy_for_bas(account.proxy)
            
            params = {
                "video_url": video_url,
                "comment_id": comment_id,
                "cookies": cookies_json,
                "user_agent": account.user_agent or None
            }
            
            if proxy_dict:
                params["proxy"] = json.dumps(proxy_dict)
            
            # Вызываем функцию из BAS скрипта
            result = await self._call_function("TikTokLikeComment", params)
            
            success = result.get("success", False)
            
            logger.info(
                f"[BAS LIKE] {'Success' if success else 'Failed'}: {task_info}account_id={account.id}, "
                f"label={account.label[:20]}"
            )
            
            return success
            
        except Exception as e:
            error_msg = str(e)
            logger.error(
                f"[BAS LIKE] Error: {task_info}account_id={account.id}, "
                f"label={account.label[:20]}, error={error_msg[:200]}"
            )
            return False
    
    async def quick_validate_account(
        self,
        account: Account,
        timeout: int | None = None
    ) -> bool:
        """
        Быстрая валидация аккаунта через BAS.
        
        Args:
            account: Аккаунт из БД
            timeout: Таймаут в секундах (по умолчанию из settings)
            
        Returns:
            bool: True если аккаунт валиден
        """
        if not account.label:
            return False
        
        logger.debug(
            f"[BAS VALIDATE] Starting: account_id={account.id}, label={account.label[:20]}"
        )
        
        try:
            # Подготавливаем параметры
            cookies_json = self._prepare_cookies_for_bas(account)
            proxy_dict = self._prepare_proxy_for_bas(account.proxy)
            
            params = {
                "cookies": cookies_json,
                "user_agent": account.user_agent or None
            }
            
            if proxy_dict:
                params["proxy"] = json.dumps(proxy_dict)
            
            # Вызываем функцию из BAS скрипта
            result = await self._call_function("TikTokValidateAccount", params)
            
            is_valid = result.get("valid", False)
            
            logger.debug(
                f"[BAS VALIDATE] Result: account_id={account.id}, "
                f"label={account.label[:20]}, valid={is_valid}"
            )
            
            return is_valid
            
        except Exception as e:
            error_msg = str(e)
            logger.warning(
                f"[BAS VALIDATE] Error: account_id={account.id}, "
                f"label={account.label[:20]}, error={error_msg[:200]}"
            )
            return False
    
    async def verify_comment_visible(
        self,
        account: Account,
        video_url: str,
        comment_text: str,
        task_id: int | None = None
    ) -> bool:
        """
        Проверяет видимость комментария через BAS.
        
        Args:
            account: Аккаунт из БД
            video_url: URL видео
            comment_text: Текст комментария для поиска
            task_id: Опциональный ID задачи (для логирования)
            
        Returns:
            bool: True если комментарий виден
        """
        if not account.label:
            return False
        
        task_info = f"task_id={task_id}, " if task_id else ""
        
        logger.debug(
            f"[BAS VERIFY] Starting: {task_info}account_id={account.id}, "
            f"label={account.label[:20]}, video_url={video_url[:50]}"
        )
        
        try:
            # Подготавливаем параметры
            cookies_json = self._prepare_cookies_for_bas(account)
            proxy_dict = self._prepare_proxy_for_bas(account.proxy)
            
            params = {
                "video_url": video_url,
                "comment_text": comment_text,
                "cookies": cookies_json,
                "user_agent": account.user_agent or None
            }
            
            if proxy_dict:
                params["proxy"] = json.dumps(proxy_dict)
            
            # Вызываем функцию из BAS скрипта
            result = await self._call_function("TikTokVerifyComment", params)
            
            is_visible = result.get("visible", False)
            
            logger.debug(
                f"[BAS VERIFY] Result: {task_info}account_id={account.id}, "
                f"label={account.label[:20]}, visible={is_visible}"
            )
            
            return is_visible
            
        except Exception as e:
            error_msg = str(e)
            logger.warning(
                f"[BAS VERIFY] Error: {task_info}account_id={account.id}, "
                f"label={account.label[:20]}, error={error_msg[:200]}"
            )
            return False


# Глобальный экземпляр клиента (singleton)
_bas_client: Optional[BASClient] = None


def get_bas_client() -> BASClient:
    """
    Получает глобальный экземпляр BAS клиента.
    
    Returns:
        BASClient: Экземпляр клиента
    """
    global _bas_client
    if _bas_client is None:
        _bas_client = BASClient()
    return _bas_client


# Экспортируем функции для совместимости с tiktok_client_async
async def post_comment(account: Account, video_url: str, text: str, task_id: int | None = None) -> str:
    """Публикует комментарий через BAS."""
    client = get_bas_client()
    return await client.post_comment(account, video_url, text, task_id)


async def post_reply(account: Account, video_url: str, parent_comment_id: str | None, text: str, task_id: int | None = None) -> None:
    """Публикует ответ на комментарий через BAS."""
    client = get_bas_client()
    return await client.post_reply(account, video_url, parent_comment_id, text, task_id)


async def like_comment(account: Account, comment_id: str, task_id: int | None = None, video_url: str | None = None) -> bool:
    """Ставит лайк комментарию через BAS."""
    client = get_bas_client()
    return await client.like_comment(account, comment_id, task_id, video_url)


async def quick_validate_account(account: Account, timeout: int | None = None) -> bool:
    """Быстрая валидация аккаунта через BAS."""
    client = get_bas_client()
    return await client.quick_validate_account(account, timeout)


async def verify_comment_visible(account: Account, video_url: str, comment_text: str, task_id: int | None = None) -> bool:
    """Проверяет видимость комментария через BAS."""
    client = get_bas_client()
    return await client.verify_comment_visible(account, video_url, comment_text, task_id)


# ---------------------------------------------------------------------------
# HTTP-клиент для bas_http_server.py (используется при USE_BAS=True)
# ---------------------------------------------------------------------------

import time as _time
from dataclasses import dataclass as _dataclass
from typing import Optional as _Optional

try:
    import httpx as _httpx
    _HTTPX_OK = True
except ImportError:
    _HTTPX_OK = False


@_dataclass
class BASResult:
    status: str
    comment_id: _Optional[str] = None
    error_reason: _Optional[str] = None
    elapsed_seconds: float = 0.0
    dom_strategy: str = "bas"
    network_captured: bool = False


async def post_comment_via_bas(
    account_id: int,
    video_url: str,
    comment_text: str,
    proxy: _Optional[str] = None,
    cookies_json: _Optional[str] = None,
    *,
    correlation_id: _Optional[str] = None,
    timeout: float = 90.0,
) -> BASResult:
    """
    Отправляет задачу на постинг комментария в bas_http_server.py.
    Сервер должен быть запущен: python bas_scripts/bas_http_server.py
    """
    if not _HTTPX_OK:
        return BASResult(status="error", error_reason="httpx_not_installed")

    started = _time.monotonic()
    bas_url = "http://127.0.0.1:8765"

    payload = {
        "account_id": account_id,
        "video_url": video_url,
        "comment_text": comment_text,
        "proxy": proxy or "",
        "cookies_json": cookies_json or "[]",
        "correlation_id": correlation_id or f"acc_{account_id}",
    }

    try:
        async with _httpx.AsyncClient(timeout=timeout + 5) as client:
            response = await client.post(f"{bas_url}/post_comment", json=payload, timeout=timeout + 5)

        elapsed = _time.monotonic() - started

        if response.status_code != 200:
            return BASResult(status="error", error_reason=f"bas_http_{response.status_code}", elapsed_seconds=elapsed)

        data = response.json()
        bas_status = data.get("status", "")

        if bas_status == "success":
            return BASResult(status="success", comment_id=str(data.get("comment_id") or ""), elapsed_seconds=elapsed, network_captured=True)
        if bas_status == "redirected":
            return BASResult(status="redirected", error_reason=data.get("error") or "bas_redirected", elapsed_seconds=elapsed)
        return BASResult(status="failed", error_reason=data.get("error") or f"bas_failed: {bas_status}", elapsed_seconds=elapsed)

    except _httpx.ConnectError:
        return BASResult(status="error", error_reason="bas_not_running", elapsed_seconds=_time.monotonic() - started)
    except _httpx.TimeoutException:
        return BASResult(status="timeout", error_reason="bas_timeout", elapsed_seconds=_time.monotonic() - started)
    except Exception as exc:
        return BASResult(status="error", error_reason=f"bas_exception: {str(exc)[:80]}", elapsed_seconds=_time.monotonic() - started)


async def post_reply_via_bas(
    account_id: int,
    video_url: str,
    reply_text: str,
    parent_comment_text: _Optional[str] = None,
    parent_comment_id: _Optional[str] = None,
    proxy: _Optional[str] = None,
    cookies_json: _Optional[str] = None,
    *,
    correlation_id: _Optional[str] = None,
    timeout: float = 90.0,
) -> BASResult:
    """
    Отправляет задачу на постинг ответа в bas_http_server.py.
    Сервер должен быть запущен: python bas_scripts/bas_http_server.py
    """
    if not _HTTPX_OK:
        return BASResult(status="error", error_reason="httpx_not_installed")

    started = _time.monotonic()
    bas_url = "http://127.0.0.1:8765"

    payload = {
        "account_id": account_id,
        "video_url": video_url,
        "reply_text": reply_text,
        "parent_comment_text": parent_comment_text or "",
        "parent_comment_id": parent_comment_id or "",
        "proxy": proxy or "",
        "cookies_json": cookies_json or "[]",
        "correlation_id": correlation_id or f"acc_{account_id}",
    }

    try:
        async with _httpx.AsyncClient(timeout=timeout + 5) as client:
            response = await client.post(f"{bas_url}/post_reply", json=payload, timeout=timeout + 5)

        elapsed = _time.monotonic() - started

        if response.status_code != 200:
            return BASResult(status="error", error_reason=f"bas_http_{response.status_code}", elapsed_seconds=elapsed)

        data = response.json()
        bas_status = data.get("status", "")

        if bas_status == "success":
            return BASResult(status="success", comment_id=str(data.get("comment_id") or ""), elapsed_seconds=elapsed, network_captured=True)
        if bas_status == "redirected":
            return BASResult(status="redirected", error_reason=data.get("error") or "bas_redirected", elapsed_seconds=elapsed)
        return BASResult(status="failed", error_reason=data.get("error") or f"bas_failed: {bas_status}", elapsed_seconds=elapsed)

    except _httpx.ConnectError:
        return BASResult(status="error", error_reason="bas_not_running", elapsed_seconds=_time.monotonic() - started)
    except _httpx.TimeoutException:
        return BASResult(status="timeout", error_reason="bas_timeout", elapsed_seconds=_time.monotonic() - started)
    except Exception as exc:
        return BASResult(status="error", error_reason=f"bas_exception: {str(exc)[:80]}", elapsed_seconds=_time.monotonic() - started)
