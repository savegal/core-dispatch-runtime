"""
Playwright HTTP Client - HTTP-клиент на основе Playwright APIRequestContext.

Использует живые cookies и headers из browser context для выполнения запросов к TikTok API.
Это более надежный подход, чем попытка генерировать подписи вручную.
"""
import logging
import json
from typing import Optional, Dict, Any, Tuple
from datetime import datetime, timezone
from patchright.async_api import APIRequestContext, Page, BrowserContext
from executor.http.request_profile import RequestProfile
from executor.http.signature_runtime import resolve_request_signature
from executor.http.retry_policy import (
    RetryPolicy, HttpErrorType, classify_http_error,
    STANDARD_POLICY
)

logger = logging.getLogger("executor.http.playwright_client")
_SHORTLINK_HOST_MARKERS = ("vt.tiktok.com", "vm.tiktok.com")


def mask_cookies(cookies: Dict[str, str]) -> Dict[str, str]:
    """
    Маскирует чувствительные cookies для логирования.
    
    Args:
        cookies: Словарь cookies
    
    Returns:
        dict: Маскированные cookies
    """
    masked = {}
    sensitive_keys = ["sid_tt", "sessionid", "sid_guard", "tt_csrf_token"]
    
    for key, value in cookies.items():
        if any(sensitive in key.lower() for sensitive in sensitive_keys):
            if value:
                masked[key] = f"{value[:4]}...{value[-4:]}" if len(value) > 8 else "***"
            else:
                masked[key] = "***"
        else:
            masked[key] = value
    
    return masked


def _normalize_identifier(value: Any) -> Optional[str]:
    """Converts response identifier value to non-empty string."""
    if value is None:
        return None
    value_str = str(value).strip()
    return value_str or None


def _extract_comment_id(response_data: Optional[Dict[str, Any]]) -> Optional[str]:
    """
    Extracts comment ID from known TikTok response shapes.

    Supports:
    - {"comment": {"cid"/"comment_id"/"id": ...}}
    - {"data": {"comment": {...}}}
    - {"data": {"cid"/"comment_id"/"id": ...}}
    - {"cid"/"comment_id"/"id": ...}
    """
    if not isinstance(response_data, dict):
        return None

    containers: list[Dict[str, Any]] = [response_data]

    comment_data = response_data.get("comment")
    if isinstance(comment_data, dict):
        containers.append(comment_data)

    data = response_data.get("data")
    if isinstance(data, dict):
        containers.append(data)
        nested_comment = data.get("comment")
        if isinstance(nested_comment, dict):
            containers.append(nested_comment)

    for container in containers:
        for key in ("cid", "comment_id", "id"):
            comment_id = _normalize_identifier(container.get(key))
            if comment_id:
                return comment_id

    return None


def _inject_comment_id(
    response_data: Optional[Dict[str, Any]],
    comment_id: str,
    *,
    confirmation_source: str,
) -> Dict[str, Any]:
    """
    Enriches a successful publish response with recovered comment_id.

    We preserve the original response shape as much as possible and only add the
    minimum fields needed for the rest of the pipeline to treat the response as
    confirmed.
    """
    base: Dict[str, Any] = dict(response_data) if isinstance(response_data, dict) else {}
    comment_id = str(comment_id)

    data = base.get("data")
    if not isinstance(data, dict):
        data = {}
        base["data"] = data
    else:
        data = dict(data)
        base["data"] = data

    comment = data.get("comment")
    if not isinstance(comment, dict):
        comment = {}
    else:
        comment = dict(comment)

    comment.setdefault("cid", comment_id)
    data["comment"] = comment
    base.setdefault("comment", dict(comment))
    base["__confirmation_source"] = confirmation_source
    return base


async def _recover_main_comment_id_via_dom(page: Optional[Page], text: str) -> Optional[str]:
    """
    Best-effort recovery when TikTok accepted publish but omitted comment_id.

    We only use this for main comments on a live warmed page. Replies keep their
    stricter path because parent binding matters more than raw publish success.
    """
    if not page or not text or not text.strip():
        return None

    try:
        from executor.tiktok_client_async import _recover_comment_id_from_dom

        return await _recover_comment_id_from_dom(
            page=page,
            expected_text=text,
            phase="MAIN_COMMENT_HTTP",
            max_scrolls=3,
            scroll_delay_ms=250,
        )
    except Exception:
        logger.debug("[HTTP] Main comment DOM recovery helper failed", exc_info=True)
        return None


def _extract_reply_network_metadata(response_data: Optional[Dict[str, Any]]) -> Dict[str, Optional[str]]:
    """
    Extracts reply-parent metadata from known TikTok response shapes.

    We keep direct parent, reply-to-reply parent, and root parent separately so
    the caller can distinguish strong network confirmation from weaker hints.
    """
    metadata: Dict[str, Optional[str]] = {
        "direct_parent_id": None,
        "reply_to_reply_id": None,
        "root_comment_id": None,
    }
    if not isinstance(response_data, dict):
        return metadata

    containers: list[Dict[str, Any]] = [response_data]

    comment_data = response_data.get("comment")
    if isinstance(comment_data, dict):
        containers.append(comment_data)

    data = response_data.get("data")
    if isinstance(data, dict):
        containers.append(data)
        nested_comment = data.get("comment")
        if isinstance(nested_comment, dict):
            containers.append(nested_comment)

    for container in containers:
        if metadata["direct_parent_id"] is None:
            for key in ("reply_id", "parent_comment_id", "parent_id", "reply_to_comment_id"):
                parent_id = _normalize_identifier(container.get(key))
                if parent_id and parent_id != "0":
                    metadata["direct_parent_id"] = parent_id
                    break

        if metadata["reply_to_reply_id"] is None:
            reply_to_reply_id = _normalize_identifier(container.get("reply_to_reply_id"))
            if reply_to_reply_id and reply_to_reply_id != "0":
                metadata["reply_to_reply_id"] = reply_to_reply_id

        if metadata["root_comment_id"] is None:
            for key in ("root_comment_id", "root_comment_cid"):
                root_comment_id = _normalize_identifier(container.get(key))
                if root_comment_id and root_comment_id != "0":
                    metadata["root_comment_id"] = root_comment_id
                    break

    return metadata


def _classify_api_response_error(response_data: Optional[Dict[str, Any]]) -> Tuple[HttpErrorType, str]:
    if not isinstance(response_data, dict):
        return HttpErrorType.API_ERROR, "Unknown API error"

    message_parts: list[str] = []
    for key in ("status_msg", "message", "msg", "error", "description"):
        value = response_data.get(key)
        if isinstance(value, str) and value.strip():
            message_parts.append(value.strip())

    prompts = response_data.get("prompts")
    if isinstance(prompts, list):
        for prompt in prompts:
            if isinstance(prompt, str) and prompt.strip():
                message_parts.append(prompt.strip())

    message = " | ".join(message_parts) or f"API error: status_code={response_data.get('status_code')}"
    message_lower = message.lower()

    if any(marker in message_lower for marker in ("captcha", "verify", "security check", "slide", "robot", "human")):
        return HttpErrorType.CAPTCHA, message
    if any(marker in message_lower for marker in ("too fast", "too many", "rate limit", "frequent", "try again later", "too frequently")):
        return HttpErrorType.RATE_LIMIT, message
    if any(marker in message_lower for marker in ("forbidden", "denied", "blocked", "suspended", "not authorized", "not authorised")):
        return HttpErrorType.FORBIDDEN, message

    return HttpErrorType.API_ERROR, message


async def _extract_runtime_profile(page: Page) -> Dict[str, Any]:
    current_url = None
    try:
        current_url = page.url
    except Exception:
        current_url = None

    try:
        profile = await page.evaluate(
            """() => {
                const uaData = navigator.userAgentData || null;
                const brands = uaData && Array.isArray(uaData.brands)
                    ? uaData.brands.map(item => ({ brand: item.brand, version: item.version }))
                    : [];
                const metaLocale = document.querySelector('meta[property="og:locale"]')?.getAttribute("content") || null;
                const documentLocale = document.documentElement?.lang || null;
                const locale = documentLocale || metaLocale || null;
                const localeRegion = locale && locale.includes("-")
                    ? locale.split("-").slice(-1)[0].toUpperCase()
                    : null;
                let nextRegion = null;
                try {
                    const nextData = window.__NEXT_DATA__ || null;
                    nextRegion =
                        nextData?.props?.pageProps?.webappContext?.region ||
                        nextData?.props?.pageProps?.appContext?.region ||
                        nextData?.props?.pageProps?.region ||
                        null;
                } catch (error) {
                    nextRegion = null;
                }
                return {
                    current_url: window.location.href,
                    user_agent: navigator.userAgent || null,
                    browser_language: navigator.language || null,
                    languages: Array.isArray(navigator.languages) ? navigator.languages : [],
                    locale,
                    region: nextRegion || localeRegion,
                    timezone: Intl.DateTimeFormat().resolvedOptions().timeZone || null,
                    platform: navigator.platform || null,
                    sec_ch_ua_platform: uaData && uaData.platform ? uaData.platform : null,
                    sec_ch_ua_brands: brands,
                    mobile: !!(uaData && uaData.mobile),
                    screen_width: window.screen && window.screen.width ? window.screen.width : null,
                    screen_height: window.screen && window.screen.height ? window.screen.height : null,
                    viewport_width: window.innerWidth || null,
                    viewport_height: window.innerHeight || null,
                    history_length: window.history && typeof window.history.length === "number" ? window.history.length : null,
                    is_fullscreen: !!document.fullscreenElement,
                    is_page_visible: document.visibilityState === "visible",
                    focus_state: typeof document.hasFocus === "function" ? !!document.hasFocus() : null,
                    cookie_enabled: !!navigator.cookieEnabled,
                    browser_online: typeof navigator.onLine === "boolean" ? navigator.onLine : true
                };
            }"""
        )
    except Exception:
        profile = {}

    if not isinstance(profile, dict):
        profile = {}
    if current_url and not profile.get("current_url"):
        profile["current_url"] = current_url
    return profile


class PlaywrightHttpClient:
    """
    HTTP-клиент для TikTok API на основе Playwright APIRequestContext.
    
    Использует живые cookies и headers из browser context.
    """
    
    def __init__(
        self,
        request_context: APIRequestContext,
        retry_policy: Optional[RetryPolicy] = None,
        runtime_profile: Optional[Dict[str, Any]] = None,
        page: Optional[Page] = None,
    ):
        """
        Args:
            request_context: Playwright APIRequestContext (с живыми cookies)
            retry_policy: Политика повторных попыток (по умолчанию STANDARD_POLICY)
        """
        self.request_context = request_context
        self.retry_policy = retry_policy or STANDARD_POLICY
        self.runtime_profile = runtime_profile or {}
        self.page = page
        self.last_request_meta: Dict[str, Any] = {}

    @staticmethod
    def _canonical_referer_url(video_url: Optional[str], runtime_profile: Optional[Dict[str, Any]]) -> Optional[str]:
        if not video_url:
            return None
        try:
            from urllib.parse import urlparse

            host = (urlparse(video_url).netloc or "").lower()
            if any(marker in host for marker in _SHORTLINK_HOST_MARKERS):
                current_url = str((runtime_profile or {}).get("current_url") or "").strip()
                if current_url and "/video/" in current_url:
                    return current_url
        except Exception:
            return video_url
        return video_url

    def _runtime_profile_for_request(self, proxy_country_code: Optional[str] = None) -> Dict[str, Any]:
        runtime_profile = dict(self.runtime_profile or {})
        if proxy_country_code:
            runtime_profile["proxy_country_code"] = str(proxy_country_code).upper()
        return runtime_profile

    @staticmethod
    def _page_url_hint(page: Optional[Page]) -> Optional[str]:
        if page is None:
            return None
        try:
            current_url = str(page.url or "").strip()
        except Exception:
            return None
        return current_url or None

    @staticmethod
    def _should_abort_local_retries(error_type: Optional[HttpErrorType], error_msg: Optional[str]) -> bool:
        if error_type == HttpErrorType.TIMEOUT:
            return True
        if error_type != HttpErrorType.NETWORK_ERROR:
            return False

        haystack = (error_msg or "").lower()
        proxy_markers = (
            "proxy",
            "tunnel",
            "socks",
            "timed out",
            "timeout",
            "connection refused",
            "connect call failed",
            "network is unreachable",
            "connection reset",
            "tls handshake",
            "winerror 10060",
            "winerror 10061",
            "err_proxy_connection_failed",
            "err_timed_out",
            "err_name_not_resolved",
            "err_connection_reset",
            "err_network_changed",
        )
        if any(marker in haystack for marker in proxy_markers):
            return True

        suspicious_response_markers = (
            "empty json response",
            "response missing status_code field (empty body)",
            "response missing status_code field (empty json)",
            "response missing status_code field (redirected page state)",
        )
        return any(marker in haystack for marker in suspicious_response_markers)

    @staticmethod
    def _should_block_unsigned_publish(
        profile: RequestProfile,
        signature_mode: str,
        *,
        page_present: bool,
        signer_available: bool,
    ) -> bool:
        """
        Blocks sensitive publish requests when a live page-based signer failed to
        produce any usable signature.

        WHY: for comment publish/reply we already have DOM fallback. Sending a
        page-backed unsigned POST is usually lower-quality than failing fast and
        letting the orchestrator choose the safer path.
        """
        if not page_present:
            return False
        if not profile.require_signature_on_page_path:
            return False
        return signature_mode == "unsigned" and signer_available
    
    @classmethod
    async def from_page(cls, page: Page, retry_policy: Optional[RetryPolicy] = None) -> "PlaywrightHttpClient":
        """
        Создает клиент из Playwright Page.
        
        Args:
            page: Playwright Page (из которой будут извлечены cookies)
            retry_policy: Политика повторных попыток
        
        Returns:
            PlaywrightHttpClient: Экземпляр клиента
        """
        # Получаем request context из page context
        context = page.context
        request_context = context.request
        runtime_profile = await _extract_runtime_profile(page)
        return cls(request_context, retry_policy, runtime_profile=runtime_profile, page=page)
    
    @classmethod
    async def from_context(
        cls,
        browser_context: BrowserContext,
        retry_policy: Optional[RetryPolicy] = None,
        page: Optional[Page] = None,
    ) -> "PlaywrightHttpClient":
        """
        Создает клиент из Playwright BrowserContext.
        
        Args:
            browser_context: Playwright BrowserContext
            retry_policy: Политика повторных попыток
        
        Returns:
            PlaywrightHttpClient: Экземпляр клиента
        """
        request_context = browser_context.request
        runtime_profile = await _extract_runtime_profile(page) if page is not None else {}
        return cls(request_context, retry_policy, runtime_profile=runtime_profile, page=page)
    
    async def _execute_request(
        self,
        profile: RequestProfile,
        dynamic_params: Dict[str, Any],
        cookies: Optional[Dict[str, str]] = None,
        user_agent: Optional[str] = None,
        correlation_id: Optional[str] = None,
        runtime_profile_override: Optional[Dict[str, Any]] = None
    ) -> Tuple[bool, Optional[Dict[str, Any]], Optional[str], Optional[HttpErrorType]]:
        """
        Выполняет HTTP-запрос через Playwright APIRequestContext.
        
        Args:
            profile: RequestProfile с шаблоном запроса
            dynamic_params: Динамические параметры (aweme_id, cid, text и т.д.)
            cookies: Дополнительные cookies (если не указаны, используются из context)
            user_agent: User-Agent (если не указан, используется из context)
            correlation_id: ID для логирования (task_id, account_id и т.д.)
        
        Returns:
            tuple: (success, response_data, error_message, error_type)
        """
        self.last_request_meta = {
            "endpoint": profile.endpoint,
            "method": profile.method,
            "request_attempted": False,
            "attempt_count": 0,
        }
        effective_runtime_profile = runtime_profile_override or self.runtime_profile
        dynamic_params, url, signature_bundle = await resolve_request_signature(
            page=self.page,
            profile=profile,
            dynamic_params=dynamic_params,
            cookies=cookies,
            runtime_profile=effective_runtime_profile,
            allow_cookie_fallback=self.page is None,
        )
        headers = profile.build_headers(cookies, user_agent, runtime_profile=effective_runtime_profile)
        form_data = profile.build_form_data(
            dynamic_params,
            cookies,
            runtime_profile=effective_runtime_profile,
            include_cookie_signatures=False,
        )
        request_signature_meta = {
            "mode": signature_bundle.mode,
            "source": signature_bundle.source,
            "has_x_bogus": bool(signature_bundle.x_bogus),
            "has_x_gnarly": bool(signature_bundle.x_gnarly),
            "has_a_bogus": bool(signature_bundle.a_bogus),
            "has_form_body": bool(form_data),
            "signer_available": signature_bundle.signer_available,
            "notes": signature_bundle.notes,
        }
        self.last_request_meta = {
            "endpoint": profile.endpoint,
            "method": profile.method,
            "signature": dict(request_signature_meta),
            "request_attempted": False,
            "attempt_count": 0,
        }

        def _attach_request_meta(payload: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
            if not isinstance(payload, dict):
                return payload
            enriched = dict(payload)
            enriched["__request_signature"] = dict(request_signature_meta)
            enriched["__request_meta"] = dict(self.last_request_meta)
            return enriched
        
        # Маскируем cookies для логирования
        masked_cookies = mask_cookies(cookies or {})
        
        logger.debug(
            f"[HTTP] {profile.method} {profile.endpoint} | "
            f"correlation_id={correlation_id}, "
            f"dynamic_params={list(dynamic_params.keys())}, "
            f"form_keys={list(form_data.keys())}, "
            f"cookies_keys={list(masked_cookies.keys())}, "
            f"signature_mode={signature_bundle.mode}, "
            f"signature_source={signature_bundle.source}, "
            f"has_x_bogus={bool(signature_bundle.x_bogus)}, "
            f"has_x_gnarly={bool(signature_bundle.x_gnarly)}, "
            f"has_a_bogus={bool(signature_bundle.a_bogus)}"
        )

        if self._should_block_unsigned_publish(
            profile,
            signature_bundle.mode,
            page_present=self.page is not None,
            signer_available=signature_bundle.signer_available,
        ):
            error_msg = (
                "HTTP publish blocked: runtime signer did not produce a usable "
                "signature for a sensitive endpoint"
            )
            logger.warning(
                f"[HTTP] Blocking unsigned sensitive request | "
                f"correlation_id={correlation_id}, "
                f"endpoint={profile.endpoint}, "
                f"signature_source={signature_bundle.source}, "
                f"signer_available={signature_bundle.signer_available}, "
                f"notes={signature_bundle.notes}"
            )
            return False, None, error_msg, HttpErrorType.API_ERROR
        
        for attempt in range(self.retry_policy.max_retries + 1):
            try:
                self.last_request_meta["attempt_count"] = int(
                    self.last_request_meta.get("attempt_count") or 0
                ) + 1
                # ИСПРАВЛЕНО: Разные timeout для разных попыток
                # 15 сек для первого запроса (быстрее при ошибках), 30 сек для retry (может быть медленный прокси)
                base_timeout = 15000  # 15 секунд для обычных запросов
                retry_timeout = 30000  # 30 секунд для retry
                timeout_to_use = base_timeout if attempt == 0 else retry_timeout
                
                # Выполняем запрос через Playwright request context
                # Playwright автоматически использует cookies из context
                self.last_request_meta["request_attempted"] = True
                self.last_request_meta["timeout_ms"] = timeout_to_use
                response = await self.request_context.fetch(
                    url,
                    method=profile.method,
                    headers=headers,
                    form=form_data or None,
                    timeout=timeout_to_use
                )
                
                # ИСПРАВЛЕНО: Проверяем Content-Type ответа перед парсингом
                # Если TikTok вернул HTML (капча/WAF/редирект) - сразу классифицируем как FORBIDDEN/CAPTCHA
                content_type = response.headers.get("content-type", "").lower()
                status_code = response.status
                self.last_request_meta["response_status"] = status_code
                self.last_request_meta["response_content_type"] = content_type
                
                # Получаем тело ответа
                try:
                    response_body = await response.text()
                    
                    # ИСПРАВЛЕНО: Если Content-Type не JSON - это капча/WAF/редирект
                    if "application/json" not in content_type and status_code == 200:
                        # HTML ответ при 200 - это обычно капча или WAF
                        if "text/html" in content_type or not content_type:
                            error_type = HttpErrorType.CAPTCHA
                            error_msg = f"Received HTML instead of JSON (Content-Type: {content_type})"
                            logger.warning(
                                f"[HTTP] Non-JSON response | "
                                f"correlation_id={correlation_id}, "
                                f"content_type={content_type}, "
                                f"status={status_code}"
                            )
                            if self._should_abort_local_retries(error_type, error_msg):
                                return False, None, error_msg, error_type
                            if not self.retry_policy.should_retry(error_type, attempt):
                                return False, None, error_msg, error_type
                            continue
                    
                    # Парсим JSON только если Content-Type правильный
                    if "application/json" in content_type or status_code != 200:
                        response_data = json.loads(response_body) if response_body else {}
                    else:
                        # Не JSON и не 200 - это ошибка
                        response_data = {"raw": response_body[:500] if response_body else ""}
                except (json.JSONDecodeError, Exception) as parse_error:
                    # ИСПРАВЛЕНО: Если не удалось распарсить JSON - проверяем Content-Type
                    if "application/json" in content_type:
                        # Ожидали JSON, но не смогли распарсить - это ошибка
                        error_type = HttpErrorType.API_ERROR
                        error_msg = f"Failed to parse JSON response: {str(parse_error)[:100]}"
                        logger.warning(
                            f"[HTTP] JSON parse error | "
                            f"correlation_id={correlation_id}, "
                            f"content_type={content_type}, "
                            f"error={error_msg}"
                        )
                        if self._should_abort_local_retries(error_type, error_msg):
                            return False, None, error_msg, error_type
                        if not self.retry_policy.should_retry(error_type, attempt):
                            return False, None, error_msg, error_type
                        continue
                    else:
                        # Не JSON ответ - это капча/WAF
                        response_data = {"raw": response_body[:500] if response_body else ""}
                        error_type = HttpErrorType.CAPTCHA
                        error_msg = f"Non-JSON response (Content-Type: {content_type})"
                        logger.warning(
                            f"[HTTP] Non-JSON response | "
                            f"correlation_id={correlation_id}, "
                            f"content_type={content_type}"
                        )
                        if self._should_abort_local_retries(error_type, error_msg):
                            return False, None, error_msg, error_type
                        if not self.retry_policy.should_retry(error_type, attempt):
                            return False, None, error_msg, error_type
                        continue
                
                # Классифицируем ошибку
                error_type, error_msg = classify_http_error(status_code)
                
                # ИСПРАВЛЕНО: Жесткая проверка ответа HTTP
                # Успех только если: HTTP статус 200 И JSON status_code == 0
                if status_code == 200 and isinstance(response_data, dict):
                    status_code_in_body = response_data.get("status_code")
                    
                    # КРИТИЧНО: Проверяем status_code в теле ответа
                    if status_code_in_body is None:
                        # Нет status_code в ответе - считаем ошибкой
                        current_page_url = self._page_url_hint(self.page)
                        response_keys = list(response_data.keys())[:10]
                        body_preview = (response_body[:160] if response_body else "").replace("\n", " ").replace("\r", " ")
                        page_redirect_markers = ("/messages", "/login", "/signup", "/verify", "/auth", "/inbox")
                        redirected_page_state = bool(
                            current_page_url and any(marker in current_page_url.lower() for marker in page_redirect_markers)
                        )
                        logger.warning(
                            f"[HTTP] Missing status_code in response | "
                            f"correlation_id={correlation_id}, "
                            f"response_keys={response_keys}, "
                            f"content_type={content_type}, "
                            f"body_len={len(response_body) if response_body else 0}, "
                            f"page_url={current_page_url}, "
                            f"body_preview={body_preview!r}"
                        )
                        if not response_body.strip() or not response_keys:
                            error_type = HttpErrorType.FORBIDDEN
                            error_msg = (
                                "TikTok stealth-reject: empty JSON response "
                                f"(body_len={len(response_body) if response_body else 0}, "
                                f"content_type={content_type}). "
                                "Session likely invalid for this endpoint."
                            )
                            logger.warning(
                                f"[HTTP] Stealth-reject detected | "
                                f"correlation_id={correlation_id}, "
                                f"page_url={current_page_url}, "
                                f"redirected_page_state={redirected_page_state}"
                            )
                            return False, None, error_msg, error_type
                        if redirected_page_state:
                            error_type = HttpErrorType.FORBIDDEN
                            error_msg = "Response missing status_code field (redirected page state)"
                        else:
                            error_type = HttpErrorType.API_ERROR
                            error_msg = "Response missing status_code field"
                        if self._should_abort_local_retries(error_type, error_msg):
                            return False, None, error_msg, error_type
                        if not self.retry_policy.should_retry(error_type, attempt):
                            return False, None, error_msg, error_type
                        continue
                    elif status_code_in_body != 0:
                        error_type, error_msg = _classify_api_response_error(response_data)
                        logger.warning(
                            f"[HTTP] API error in response | "
                            f"correlation_id={correlation_id}, "
                            f"status_code={status_code_in_body}, "
                            f"error_type={error_type.value}, "
                            f"msg={error_msg}"
                        )
                        
                        # Check whether retry is allowed for this classified API error.
                        if self._should_abort_local_retries(error_type, error_msg):
                            return False, None, error_msg, error_type
                        if not self.retry_policy.should_retry(error_type, attempt):
                            return False, None, error_msg, error_type
                        continue
                    
                    # status_code == 0, проверяем наличие подтверждающих данных
                    # Для like: проверяем что ответ не пустой или есть подтверждение
                    # Для reply/publish: желательно есть comment_id
                    has_confirmation = False
                    if profile.endpoint == "/api/comment/digg/":
                        # Для лайка: если status_code==0, это уже подтверждение
                        has_confirmation = True
                    elif profile.endpoint == "/api/comment/publish/":
                        # For publish/reply success we require real comment_id.
                        has_confirmation = _extract_comment_id(response_data) is not None
                    
                    if has_confirmation:
                        response_data = _attach_request_meta(response_data)
                        logger.info(
                            f"[HTTP] Success (verified) | "
                            f"correlation_id={correlation_id}, "
                            f"method={profile.method}, "
                            f"endpoint={profile.endpoint}, "
                            f"status_code={status_code_in_body}, "
                            f"signature_mode={signature_bundle.mode}"
                        )
                        return True, response_data, None, None
                    else:
                        # status_code==0, но нет подтверждающих данных - подозрительно
                        recovered_comment_id: Optional[str] = None
                        is_publish_endpoint = profile.endpoint == "/api/comment/publish/"
                        is_reply_request = bool(dynamic_params.get("reply_id"))
                        if is_publish_endpoint and not is_reply_request:
                            recovered_comment_id = await _recover_main_comment_id_via_dom(
                                self.page,
                                str(dynamic_params.get("text") or ""),
                            )
                            if recovered_comment_id:
                                enriched_response = _inject_comment_id(
                                    response_data,
                                    recovered_comment_id,
                                    confirmation_source="dom_recovery_after_http",
                                )
                                enriched_response = _attach_request_meta(enriched_response)
                                logger.info(
                                    f"[HTTP] Success via DOM confirmation recovery | "
                                    f"correlation_id={correlation_id}, "
                                    f"endpoint={profile.endpoint}, "
                                    f"comment_id={recovered_comment_id[:20]}, "
                                    f"signature_mode={signature_bundle.mode}"
                                )
                                return True, enriched_response, None, None

                        error_type = HttpErrorType.API_ERROR
                        error_msg = "Response OK but missing confirmation data (comment_id)"
                        logger.warning(
                            f"[HTTP] Missing confirmation in response | "
                            f"correlation_id={correlation_id}, "
                            f"response_keys={list(response_data.keys())[:10]}, "
                            f"dom_recovered={bool(recovered_comment_id)}, "
                            f"signature_mode={signature_bundle.mode}"
                        )
                        if self._should_abort_local_retries(error_type, error_msg):
                            return False, None, error_msg, error_type
                        if not self.retry_policy.should_retry(error_type, attempt):
                            return False, None, error_msg, error_type
                        continue
                elif error_type == HttpErrorType.SUCCESS and status_code == 200:
                    # HTTP 200, но response_data не dict - ошибка парсинга
                    error_type = HttpErrorType.API_ERROR
                    error_msg = "Invalid response format"
                    logger.warning(
                        f"[HTTP] Invalid response format | "
                        f"correlation_id={correlation_id}, "
                        f"response_type={type(response_data).__name__}"
                    )
                    if self._should_abort_local_retries(error_type, error_msg):
                        return False, None, error_msg, error_type
                    if not self.retry_policy.should_retry(error_type, attempt):
                        return False, None, error_msg, error_type
                    continue
                else:
                    logger.warning(
                        f"[HTTP] Error | "
                        f"correlation_id={correlation_id}, "
                        f"status={status_code}, "
                        f"error_type={error_type.value}, "
                        f"attempt={attempt+1}/{self.retry_policy.max_retries+1}"
                    )
                    
                    # Проверяем, нужно ли повторять
                    if self._should_abort_local_retries(error_type, error_msg):
                        return False, None, error_msg, error_type
                    if not self.retry_policy.should_retry(error_type, attempt):
                        return False, None, error_msg, error_type
                    
                    # ИСПРАВЛЕНО: Задержка перед повтором с экспоненциальным backoff+jitter
                    if attempt < self.retry_policy.max_retries:
                        delay = self.retry_policy.get_delay(attempt, error_type)
                        logger.debug(
                            f"[HTTP] Retry after {delay:.2f}s | "
                            f"correlation_id={correlation_id}, "
                            f"attempt={attempt+1}/{self.retry_policy.max_retries+1}, "
                            f"error_type={error_type.value}"
                        )
                        import asyncio
                        await asyncio.sleep(delay)
                    
            except Exception as e:
                error_type, classified_error_msg = classify_http_error(None, e)
                error_msg = classified_error_msg[:200]
                
                logger.error(
                    f"[HTTP] Exception | "
                    f"correlation_id={correlation_id}, "
                    f"error={error_msg}, "
                    f"attempt={attempt+1}/{self.retry_policy.max_retries+1}",
                    exc_info=True
                )
                
                # Проверяем, нужно ли повторять
                if self._should_abort_local_retries(error_type, error_msg):
                    return False, None, error_msg, error_type
                if not self.retry_policy.should_retry(error_type, attempt):
                    return False, None, error_msg, error_type
                
                # ИСПРАВЛЕНО: Задержка перед повтором с error_type для корректного backoff
                if attempt < self.retry_policy.max_retries:
                    delay = self.retry_policy.get_delay(attempt, error_type)
                    logger.debug(
                        f"[HTTP] Retry after exception {delay:.2f}s | "
                        f"correlation_id={correlation_id}, "
                        f"attempt={attempt+1}/{self.retry_policy.max_retries+1}"
                    )
                    import asyncio
                    await asyncio.sleep(delay)
        
        return False, None, error_msg or "Max retries exceeded", error_type
    
    async def like_comment(
        self,
        aweme_id: str,
        cid: str,
        cookies: Optional[Dict[str, str]] = None,
        user_agent: Optional[str] = None,
        correlation_id: Optional[str] = None,
        digg_type: int = 1
    ) -> Tuple[bool, Optional[str], Optional[HttpErrorType]]:
        """
        Лайкает комментарий через HTTP.
        
        Args:
            aweme_id: ID видео
            cid: ID комментария
            cookies: Дополнительные cookies
            user_agent: User-Agent
            correlation_id: ID для логирования
            digg_type: Тип лайка (1 = like, 0 = unlike)
        
        Returns:
            tuple: (success, error_message, error_type)
        """
        profile = RequestProfile.like_profile()
        dynamic_params = {
            "aweme_id": aweme_id,
            "cid": cid,
            "digg_type": str(digg_type)
        }
        
        success, response_data, error_msg, error_type = await self._execute_request(
            profile=profile,
            dynamic_params=dynamic_params,
            cookies=cookies,
            user_agent=user_agent,
            correlation_id=correlation_id
        )
        
        if success:
            # Извлекаем comment_id из ответа если есть
            comment_id = None
            if isinstance(response_data, dict):
                comment_data = response_data.get("comment") or response_data.get("data", {}).get("comment", {})
                if isinstance(comment_data, dict):
                    comment_id = comment_data.get("cid") or comment_data.get("comment_id")
            
            return True, None, None
        else:
            return False, error_msg, error_type
    
    async def reply_comment(
        self,
        aweme_id: str,
        parent_comment_id: str,
        text: str,
        cookies: Optional[Dict[str, str]] = None,
        user_agent: Optional[str] = None,
        correlation_id: Optional[str] = None,
        video_url: Optional[str] = None,
        **extra_params
    ) -> Tuple[bool, Optional[str], Optional[str], Optional[HttpErrorType], Dict[str, Any]]:
        """
        Отправляет ответ на комментарий через HTTP.
        
        Args:
            aweme_id: ID видео
            parent_comment_id: ID родительского комментария (reply_id)
            text: Текст ответа
            cookies: Дополнительные cookies
            user_agent: User-Agent
            correlation_id: ID для логирования
            video_url: URL видео (для referer)
        
        Returns:
            tuple: (success, comment_id, error_message, error_type, meta)
        """
        profile = RequestProfile.publish_reply_profile()
        extra_params = dict(extra_params or {})
        proxy_country_code = extra_params.pop("proxy_country_code", None)
        dynamic_params = {
            "aweme_id": aweme_id,
            "reply_id": str(parent_comment_id),
            "text": text
        }
        
        # Дополнительные query параметры (например X-Bogus)
        if extra_params:
            dynamic_params.update(extra_params)
        if "reply_to_reply_id" in dynamic_params and dynamic_params["reply_to_reply_id"] is not None:
            dynamic_params["reply_to_reply_id"] = str(dynamic_params["reply_to_reply_id"])
        
        # ИСПРАВЛЕНО: Избегаем мутации profile.static_params (копируем если нужно изменить)
        # TikTok API использует query-параметры, поэтому referer можно установить через dynamic_params
        if video_url:
            # Устанавливаем referer через dynamic_params (не мутируем profile)
            referer_url = self._canonical_referer_url(video_url, self.runtime_profile)
            dynamic_params["referer"] = referer_url
            dynamic_params["root_referer"] = referer_url

        request_runtime_profile = self._runtime_profile_for_request(proxy_country_code)
        success, response_data, error_msg, error_type = await self._execute_request(
            profile=profile,
            dynamic_params=dynamic_params,
            cookies=cookies,
            user_agent=user_agent,
            correlation_id=correlation_id,
            runtime_profile_override=request_runtime_profile
        )
        
        if success:
            comment_id = _extract_comment_id(response_data)
            request_signature = response_data.get("__request_signature") if isinstance(response_data, dict) else None
            if not comment_id:
                return False, None, "Response OK but missing comment_id", HttpErrorType.API_ERROR, {
                    "parent_confirmed": False,
                    "response_parent_id": None,
                    "response_reply_to_reply_id": None,
                    "response_root_comment_id": None,
                    "parent_confirmation_level": "unconfirmed",
                    "request_signature": request_signature,
                    "request_meta": dict(self.last_request_meta),
                }

            expected_parent_id = _normalize_identifier(parent_comment_id)
            response_parent_id = None
            response_reply_to_reply_id = None
            response_root_comment_id = None
            parent_confirmed = not bool(expected_parent_id)
            parent_confirmation_level = "not_applicable" if not expected_parent_id else "unconfirmed"
            if expected_parent_id:
                reply_network_meta = _extract_reply_network_metadata(response_data)
                response_parent_id = reply_network_meta.get("direct_parent_id")
                response_reply_to_reply_id = reply_network_meta.get("reply_to_reply_id")
                response_root_comment_id = reply_network_meta.get("root_comment_id")

                direct_parent_candidates = {
                    value
                    for value in (response_parent_id, response_reply_to_reply_id)
                    if value
                }

                if direct_parent_candidates and expected_parent_id not in direct_parent_candidates:
                    mismatch_reason = (
                        "Reply parent mismatch: "
                        f"expected={expected_parent_id}, "
                        f"got_direct={response_parent_id}, "
                        f"got_reply_to_reply={response_reply_to_reply_id}"
                    )
                    logger.warning(
                        "[HTTP] Reply response returned a comment_id but parent mismatch was detected | "
                        "correlation_id=%s, comment_id=%s, %s. Treating as submitted-unconfirmed "
                        "to avoid duplicate DOM retry.",
                        correlation_id,
                        comment_id[:40],
                        mismatch_reason,
                    )
                    return True, comment_id, None, None, {
                        "parent_confirmed": False,
                        "response_parent_id": response_parent_id,
                        "response_reply_to_reply_id": response_reply_to_reply_id,
                        "response_root_comment_id": response_root_comment_id,
                        "parent_confirmation_level": "network_parent_mismatch",
                        "parent_mismatch": True,
                        "parent_mismatch_reason": mismatch_reason,
                        "request_signature": request_signature,
                        "request_meta": dict(self.last_request_meta),
                    }

                if expected_parent_id in direct_parent_candidates:
                    parent_confirmed = True
                    parent_confirmation_level = (
                        "network_reply_to_reply"
                        if response_reply_to_reply_id == expected_parent_id and response_parent_id != expected_parent_id
                        else "network_exact"
                    )
                elif (
                    response_root_comment_id == expected_parent_id
                    and not direct_parent_candidates
                ):
                    parent_confirmed = True
                    parent_confirmation_level = "network_root_match"

                if not parent_confirmed:
                    logger.warning(
                        f"[HTTP] Reply response missing strong parent confirmation | "
                        f"correlation_id={correlation_id}, "
                        f"expected_parent_id={expected_parent_id[:40]}, "
                        f"comment_id={comment_id[:40]}. "
                        f"direct_parent_id={str(response_parent_id)[:40] if response_parent_id else 'None'}, "
                        f"reply_to_reply_id={str(response_reply_to_reply_id)[:40] if response_reply_to_reply_id else 'None'}, "
                        f"root_comment_id={str(response_root_comment_id)[:40] if response_root_comment_id else 'None'}. "
                        f"Treating as soft success and delegating strict parent confirmation to DOM fallback/verify."
                    )

            return True, comment_id, None, None, {
                "parent_confirmed": parent_confirmed,
                "response_parent_id": response_parent_id,
                "response_reply_to_reply_id": response_reply_to_reply_id,
                "response_root_comment_id": response_root_comment_id,
                "parent_confirmation_level": parent_confirmation_level,
                "request_signature": request_signature,
                "request_meta": dict(self.last_request_meta),
            }
        else:
            return False, None, error_msg, error_type, {
                "parent_confirmed": False,
                "response_parent_id": None,
                "response_reply_to_reply_id": None,
                "response_root_comment_id": None,
                "parent_confirmation_level": "unconfirmed",
                "request_signature": self.last_request_meta.get("signature"),
                "request_meta": dict(self.last_request_meta),
            }
    
    async def publish_main_comment(
        self,
        aweme_id: str,
        text: str,
        cookies: Optional[Dict[str, str]] = None,
        user_agent: Optional[str] = None,
        correlation_id: Optional[str] = None,
        video_url: Optional[str] = None,
        **extra_params
    ) -> Tuple[bool, Optional[str], Optional[str], Optional[HttpErrorType]]:
        """
        Публикует главный комментарий через HTTP (fallback на случай если DOM не работает).
        
        Args:
            aweme_id: ID видео
            text: Текст комментария
            cookies: Дополнительные cookies
            user_agent: User-Agent
            correlation_id: ID для логирования
            video_url: URL видео (для referer)
            **extra_params: Дополнительные query параметры (X-Bogus и др.)
        
        Returns:
            tuple: (success, comment_id, error_message, error_type)
        """
        profile = RequestProfile.publish_main_profile()
        extra_params = dict(extra_params or {})
        proxy_country_code = extra_params.pop("proxy_country_code", None)
        dynamic_params = {
            "aweme_id": aweme_id,
            "text": text
        }
        
        # Добавляем любые extra_params (например, X-Bogus)
        dynamic_params.update(extra_params)
        
        # ИСПРАВЛЕНО: Избегаем мутации profile.static_params (копируем если нужно изменить)
        # TikTok API использует query-параметры, поэтому referer можно установить через dynamic_params
        if video_url:
            # Устанавливаем referer через dynamic_params (не мутируем profile)
            referer_url = self._canonical_referer_url(video_url, self.runtime_profile)
            dynamic_params["referer"] = referer_url
            dynamic_params["root_referer"] = referer_url

        request_runtime_profile = self._runtime_profile_for_request(proxy_country_code)
        success, response_data, error_msg, error_type = await self._execute_request(
            profile=profile,
            dynamic_params=dynamic_params,
            cookies=cookies,
            user_agent=user_agent,
            correlation_id=correlation_id,
            runtime_profile_override=request_runtime_profile
        )
        
        if success:
            comment_id = _extract_comment_id(response_data)
            if not comment_id:
                return False, None, "Response OK but missing comment_id", HttpErrorType.API_ERROR
            return True, comment_id, None, None
        else:
            return False, None, error_msg, error_type
