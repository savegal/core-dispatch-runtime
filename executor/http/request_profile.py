from dataclasses import dataclass, field
import time
from typing import Any, Dict, Iterable, Optional
from urllib.parse import urlencode
import re
import locale as system_locale
import sys

from config.settings import get_settings
from executor.browser_identity import desktop_user_agent, sec_ch_ua


_SIGNATURE_QUERY_KEYS = {"X-Bogus", "X-Gnarly", "a_bogus"}
COUNTRY_TO_TIMEZONE = {
    "AE": "Asia/Dubai",
    "AR": "America/Argentina/Buenos_Aires",
    "AT": "Europe/Vienna",
    "AU": "Australia/Sydney",
    "BE": "Europe/Brussels",
    "BR": "America/Sao_Paulo",
    "BY": "Europe/Minsk",
    "CA": "America/Toronto",
    "CH": "Europe/Zurich",
    "CL": "America/Santiago",
    "CN": "Asia/Shanghai",
    "CO": "America/Bogota",
    "CZ": "Europe/Prague",
    "DE": "Europe/Berlin",
    "EG": "Africa/Cairo",
    "ES": "Europe/Madrid",
    "FR": "Europe/Paris",
    "GB": "Europe/London",
    "GR": "Europe/Athens",
    "HK": "Asia/Hong_Kong",
    "HU": "Europe/Budapest",
    "ID": "Asia/Jakarta",
    "IL": "Asia/Jerusalem",
    "IN": "Asia/Kolkata",
    "IT": "Europe/Rome",
    "JP": "Asia/Tokyo",
    "KR": "Asia/Seoul",
    "KZ": "Asia/Almaty",
    "MX": "America/Mexico_City",
    "MY": "Asia/Kuala_Lumpur",
    "NL": "Europe/Amsterdam",
    "PE": "America/Lima",
    "PH": "Asia/Manila",
    "PL": "Europe/Warsaw",
    "PT": "Europe/Lisbon",
    "RO": "Europe/Bucharest",
    "RU": "Europe/Moscow",
    "SA": "Asia/Riyadh",
    "SE": "Europe/Stockholm",
    "SG": "Asia/Singapore",
    "TH": "Asia/Bangkok",
    "TR": "Europe/Istanbul",
    "TW": "Asia/Taipei",
    "UA": "Europe/Kyiv",
    "US": "America/New_York",
    "UZ": "Asia/Tashkent",
    "VN": "Asia/Ho_Chi_Minh",
    "ZA": "Africa/Johannesburg",
}
COUNTRY_TO_LOCALE = {
    "AE": "ar-AE", "AR": "es-AR", "AT": "de-AT", "AU": "en-AU",
    "BE": "nl-BE", "BR": "pt-BR", "BY": "ru-BY", "CA": "en-CA",
    "CH": "de-CH", "CL": "es-CL", "CN": "zh-CN", "CO": "es-CO",
    "CZ": "cs-CZ", "DE": "de-DE", "EG": "ar-EG", "ES": "es-ES",
    "FR": "fr-FR", "GB": "en-GB", "GR": "el-GR", "HK": "zh-HK",
    "HU": "hu-HU", "ID": "id-ID", "IL": "he-IL", "IN": "en-IN",
    "IT": "it-IT", "JP": "ja-JP", "KR": "ko-KR", "KZ": "kk-KZ",
    "MX": "es-MX", "MY": "ms-MY", "NL": "nl-NL", "PE": "es-PE",
    "PH": "en-PH", "PL": "pl-PL", "PT": "pt-PT", "RO": "ro-RO",
    "RU": "ru-RU", "SA": "ar-SA", "SE": "sv-SE", "SG": "en-SG",
    "TH": "th-TH", "TR": "tr-TR", "TW": "zh-TW", "UA": "uk-UA",
    "US": "en-US", "UZ": "uz-UZ", "VN": "vi-VN", "ZA": "en-ZA",
}


def _format_browser_version(user_agent: Optional[str] = None) -> str:
    ua = str(user_agent or desktop_user_agent()).strip()
    if ua.lower().startswith("mozilla/"):
        return ua[len("Mozilla/"):]
    return ua


def _country_code(value: Any) -> Optional[str]:
    country = str(value or "").strip().upper()
    return country if re.fullmatch(r"[A-Z]{2}", country) else None


def _executor_language() -> Optional[str]:
    if sys.platform == "win32":
        import ctypes
        buffer = ctypes.create_unicode_buffer(85)
        if ctypes.windll.kernel32.GetUserDefaultLocaleName(buffer, len(buffer)):
            return buffer.value
    return _normalize_locale(system_locale.getlocale()[0])


def _cookie_country(cookies: Dict[str, str]) -> Optional[str]:
    country = _country_code(cookies.get("store-region"))
    if country:
        return country
    # IDC identifies a data centre, so use only recognisable regional labels
    # as a last-resort hint; opaque/European labels do not identify a country.
    idc = str(cookies.get("tt-target-idc") or "").lower()
    if idc.startswith(("useast", "uswest")):
        return "US"
    if idc.startswith(("alisg", "sgp")) or idc == "sg":
        return "SG"
    country = _country_code(idc)
    return country if country in COUNTRY_TO_LOCALE else None


def resolve_request_geo(
    runtime_profile: Optional[Dict[str, Any]] = None,
    cookies: Optional[Dict[str, str]] = None,
    dynamic_params: Optional[Dict[str, Any]] = None,
) -> Dict[str, str]:
    """Resolve one geo profile for query parameters and Accept-Language."""
    runtime = runtime_profile or {}
    cookies = cookies or {}
    dynamic = dynamic_params or {}
    from services.dedicated_proxy_service import get_current_dedicated_proxy
    dedicated = get_current_dedicated_proxy()
    proxy_in_use = runtime.get("proxy_in_use")
    if proxy_in_use is None:
        proxy_in_use = get_settings().USE_PROXY or dedicated is not None
    if not proxy_in_use:
        # Browser values describe the executor. Document locale/region, account
        # country and cookies can still describe a previously used foreign IP.
        from tzlocal import get_localzone_name
        timezone_name = runtime.get("timezone") or runtime.get("tz_name") or get_localzone_name()
        # A timezone is not evidence of country or IP. Use the browser's actual
        # navigator.language, falling back to the OS when it is unavailable.
        language = _normalize_locale(runtime.get("browser_language") or _executor_language())
        region = _extract_region_from_locale(language)
        result = {"tz_name": str(timezone_name)}
        if language:
            result.update(app_language=language, browser_language=language, webcast_language=language)
        if region:
            result.update(region=region, priority_region=region)
        return result
    if dedicated and dedicated.country_code:
        runtime = {**runtime, "proxy_country_code": dedicated.country_code}
    language = _normalize_locale(
        runtime.get("browser_language") or runtime.get("locale")
        or dynamic.get("browser_language") or dynamic.get("app_language")
        or cookies.get("locale") or cookies.get("i18next") or cookies.get("lang")
    )
    runtime_region = _country_code(runtime.get("region") or dynamic.get("region"))
    locale_region = _extract_region_from_locale(language)
    proxy_region = _country_code(dynamic.get("proxy_country_code") or runtime.get("proxy_country_code"))
    fallback_region = _country_code(
        dynamic.get("account_country_code") or runtime.get("account_country_code")
    ) or _cookie_country(cookies)
    region = proxy_region or runtime_region or fallback_region or locale_region
    timezone_name = runtime.get("timezone") or runtime.get("tz_name") or dynamic.get("tz_name")
    # When stronger geo contradicts the browser's old/default country, replace
    # the entire fallback tuple rather than mixing unrelated country defaults.
    geo_override = proxy_region or (fallback_region if not runtime_region else None)
    if geo_override and geo_override != (runtime_region or locale_region):
        language = COUNTRY_TO_LOCALE.get(geo_override)
        timezone_name = COUNTRY_TO_TIMEZONE.get(geo_override)
    elif region:
        language = language or COUNTRY_TO_LOCALE.get(region)
        timezone_name = timezone_name or COUNTRY_TO_TIMEZONE.get(region)

    result = {}
    if region:
        result.update(region=region, priority_region=region)
    if language:
        result.update(app_language=language, browser_language=language, webcast_language=language)
    if timezone_name:
        result["tz_name"] = str(timezone_name)
    return result


def _normalize_locale(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    normalized = str(value).replace("_", "-").strip()
    return normalized or None


def _extract_region_from_locale(locale: Optional[str]) -> Optional[str]:
    normalized = _normalize_locale(locale)
    if not normalized or "-" not in normalized:
        return None
    region = normalized.split("-")[-1].strip().upper()
    if len(region) == 2 and region.isalpha():
        return region
    return None


def _build_accept_language(primary_language: Optional[str], languages: Optional[Iterable[str]]) -> Optional[str]:
    ordered: list[str] = []
    for candidate in [primary_language, *(languages or [])]:
        normalized = _normalize_locale(candidate)
        if normalized and normalized not in ordered:
            ordered.append(normalized)
    if not ordered:
        return None

    parts: list[str] = []
    for idx, language in enumerate(ordered[:4]):
        if idx == 0:
            parts.append(language)
        else:
            quality = max(0.1, 1.0 - (idx * 0.1))
            parts.append(f"{language};q={quality:.1f}")
    return ",".join(parts)


def _format_sec_ch_ua(runtime_profile: Optional[Dict[str, Any]], user_agent: Optional[str]) -> str:
    if runtime_profile:
        brands = runtime_profile.get("sec_ch_ua_brands")
        if isinstance(brands, list):
            formatted: list[str] = []
            for brand_info in brands:
                if not isinstance(brand_info, dict):
                    continue
                brand = str(brand_info.get("brand") or "").strip()
                version = str(brand_info.get("version") or "").strip()
                if brand and version:
                    formatted.append(f'"{brand}";v="{version}"')
            if formatted:
                return ", ".join(formatted)

    chrome = re.search(r"Chrome/(\d+\.\d+\.\d+\.\d+)", user_agent or "")
    return sec_ch_ua(chrome.group(1) if chrome else None)


def _format_sec_ch_platform(runtime_profile: Optional[Dict[str, Any]], user_agent: Optional[str]) -> Optional[str]:
    if runtime_profile:
        sec_ch_platform = runtime_profile.get("sec_ch_ua_platform")
        if sec_ch_platform:
            return f'"{sec_ch_platform}"'

        platform = runtime_profile.get("platform")
        if isinstance(platform, str) and platform.strip():
            detected_os = _detect_os(platform, user_agent)
            if detected_os == "windows":
                return '"Windows"'
            if detected_os == "mac":
                return '"macOS"'
            if detected_os == "linux":
                return '"Linux"'
            if detected_os == "android":
                return '"Android"'
            if detected_os == "ios":
                return '"iOS"'
            return f'"{platform}"'

    detected_os = _detect_os(None, user_agent)
    if detected_os == "windows":
        return '"Windows"'
    if detected_os == "mac":
        return '"macOS"'
    if detected_os == "linux":
        return '"Linux"'
    if detected_os == "android":
        return '"Android"'
    if detected_os == "ios":
        return '"iOS"'
    return None


def _detect_os(platform: Optional[str], user_agent: Optional[str]) -> Optional[str]:
    haystack = f"{platform or ''} {user_agent or ''}".lower()
    if "android" in haystack:
        return "android"
    if "iphone" in haystack or "ipad" in haystack or "ios" in haystack:
        return "ios"
    if "mac" in haystack:
        return "mac"
    if "linux" in haystack:
        return "linux"
    if "win" in haystack:
        return "windows"
    return None


def _coerce_bool_query(value: Any) -> str:
    return "true" if bool(value) else "false"


def _runtime_screen(runtime_profile: Optional[Dict[str, Any]]) -> tuple[Optional[str], Optional[str]]:
    if not runtime_profile:
        return None, None

    width = runtime_profile.get("screen_width") or runtime_profile.get("viewport_width")
    height = runtime_profile.get("screen_height") or runtime_profile.get("viewport_height")
    width_str = str(int(width)) if isinstance(width, (int, float)) else (str(width) if width else None)
    height_str = str(int(height)) if isinstance(height, (int, float)) else (str(height) if height else None)
    return width_str, height_str


def _apply_runtime_overrides(
    params: Dict[str, str],
    runtime_profile: Optional[Dict[str, Any]],
    dynamic_params: Optional[Dict[str, Any]] = None,
    cookies: Optional[Dict[str, str]] = None,
) -> None:
    params.update(resolve_request_geo(runtime_profile, cookies, dynamic_params))
    if not runtime_profile:
        return

    dynamic_params = dynamic_params or {}
    user_agent = runtime_profile.get("user_agent")
    if user_agent:
        params["browser_version"] = _format_browser_version(str(user_agent))

    platform = runtime_profile.get("platform")
    if platform:
        params["browser_platform"] = str(platform)
        detected_os = _detect_os(str(platform), str(user_agent or ""))
        if detected_os:
            params["os"] = detected_os

    screen_width, screen_height = _runtime_screen(runtime_profile)
    if screen_width:
        params["screen_width"] = screen_width
    if screen_height:
        params["screen_height"] = screen_height

    history_length = runtime_profile.get("history_length")
    if history_length is not None:
        params["history_len"] = str(history_length)

    for runtime_key, target_key in (
        ("is_fullscreen", "is_fullscreen"),
        ("is_page_visible", "is_page_visible"),
        ("focus_state", "focus_state"),
        ("cookie_enabled", "cookie_enabled"),
        ("browser_online", "browser_online"),
    ):
        if runtime_key in runtime_profile:
            params[target_key] = _coerce_bool_query(runtime_profile.get(runtime_key))

    current_url = runtime_profile.get("current_url")
    explicit_referer = (dynamic_params or {}).get("referer")
    explicit_root_referer = (dynamic_params or {}).get("root_referer")
    if current_url and not explicit_referer and params.get("referer", "").startswith("https://www.google.com"):
        params["referer"] = str(current_url)
    if current_url and not explicit_root_referer and params.get("root_referer", "").startswith("https://www.google.com"):
        params["root_referer"] = str(current_url)


@dataclass
class RequestProfile:
    endpoint: str
    method: str = "POST"
    static_params: Dict[str, str] = field(default_factory=dict)
    static_headers: Dict[str, str] = field(default_factory=dict)
    form_param_keys: tuple[str, ...] = ()
    require_signature_on_page_path: bool = False

    @classmethod
    def like_profile(cls) -> "RequestProfile":
        return cls(
            endpoint="/api/comment/digg/",
            method="POST",
            form_param_keys=("aweme_id", "cid", "digg_type"),
            require_signature_on_page_path=False,
            static_params={
                "aid": "1988",
                "app_name": "tiktok_web",
                "browser_name": "Mozilla",
                "browser_online": "true",
                "browser_platform": "Win32",
                "browser_version": _format_browser_version(desktop_user_agent()),
                "channel": "tiktok_web",
                "channel_id": "0",
                "cookie_enabled": "true",
                "data_collection_enabled": "true",
                "device_platform": "web_pc",
                "digg_type": "1",
                "focus_state": "true",
                "from_page": "video",
                "history_len": "7",
                "is_fullscreen": "false",
                "is_page_visible": "true",
                "os": "windows",
                "referer": "https://www.google.com/",
                "root_referer": "https://www.google.com/",
                "screen_height": "1080",
                "screen_width": "1920",
                "user_is_login": "true",
            },
            static_headers={
                "accept": "*/*",
                "cache-control": "no-cache",
                "content-type": "application/x-www-form-urlencoded",
                "origin": "https://www.tiktok.com",
                "pragma": "no-cache",
                "priority": "u=1, i",
                "referer": "https://www.tiktok.com/",
                "sec-ch-ua": sec_ch_ua(),
                "sec-ch-ua-mobile": "?0",
                "sec-ch-ua-platform": '"Windows"',
                "sec-fetch-dest": "empty",
                "sec-fetch-mode": "cors",
                "sec-fetch-site": "same-origin",
            },
        )

    @classmethod
    def publish_main_profile(cls) -> "RequestProfile":
        return cls(
            endpoint="/api/comment/publish/",
            method="POST",
            form_param_keys=("text", "text_extra"),
            require_signature_on_page_path=True,
            static_params={
                "aid": "1988",
                "app_name": "tiktok_web",
                "browser_name": "Mozilla",
                "browser_online": "true",
                "browser_platform": "Win32",
                "browser_version": _format_browser_version(desktop_user_agent()),
                "channel": "tiktok_web",
                "cookie_enabled": "true",
                "data_collection_enabled": "true",
                "device_platform": "web_pc",
                "focus_state": "true",
                "from_page": "video",
                "history_len": "7",
                "is_fullscreen": "false",
                "is_page_visible": "true",
                "os": "windows",
                "referer": "https://www.google.com/",
                "root_referer": "https://www.google.com/",
                "screen_height": "1080",
                "screen_width": "1920",
                "text_extra": "[]",
                "user_is_login": "true",
            },
            static_headers={
                "accept": "*/*",
                "cache-control": "no-cache",
                "content-type": "application/x-www-form-urlencoded",
                "origin": "https://www.tiktok.com",
                "pragma": "no-cache",
                "priority": "u=1, i",
                "referer": "https://www.tiktok.com/",
                "sec-ch-ua": sec_ch_ua(),
                "sec-ch-ua-mobile": "?0",
                "sec-ch-ua-platform": '"Windows"',
                "sec-fetch-dest": "empty",
                "sec-fetch-mode": "cors",
                "sec-fetch-site": "same-origin",
            },
        )

    @classmethod
    def comment_list_profile(cls) -> "RequestProfile":
        params = dict(cls.publish_main_profile().static_params)
        params.pop("text_extra", None)
        return cls(
            endpoint="/api/comment/list/", method="GET", static_params=params,
            static_headers={"accept": "application/json"},
        )

    @classmethod
    def reply_list_profile(cls) -> "RequestProfile":
        params = dict(cls.comment_list_profile().static_params)
        return cls(
            endpoint="/api/comment/list/reply/", method="GET", static_params=params,
            static_headers={"accept": "application/json"},
        )

    @classmethod
    def publish_reply_profile(cls) -> "RequestProfile":
        return cls(
            endpoint="/api/comment/publish/",
            method="POST",
            form_param_keys=("text", "text_extra", "reply_id", "reply_to_reply_id"),
            require_signature_on_page_path=True,
            static_params={
                "aid": "1988",
                "app_name": "tiktok_web",
                "browser_name": "Mozilla",
                "browser_online": "true",
                "browser_platform": "Win32",
                "browser_version": _format_browser_version(desktop_user_agent()),
                "channel": "tiktok_web",
                "cookie_enabled": "true",
                "data_collection_enabled": "true",
                "device_platform": "web_pc",
                "focus_state": "true",
                "from_page": "video",
                "history_len": "8",
                "is_fullscreen": "false",
                "is_page_visible": "true",
                "os": "windows",
                "referer": "https://www.google.com/",
                "reply_to_reply_id": "0",
                "root_referer": "https://www.google.com/",
                "screen_height": "1080",
                "screen_width": "1920",
                "text_extra": "[]",
                "user_is_login": "true",
            },
            static_headers={
                "accept": "*/*",
                "cache-control": "no-cache",
                "content-type": "application/x-www-form-urlencoded",
                "origin": "https://www.tiktok.com",
                "pragma": "no-cache",
                "priority": "u=1, i",
                "referer": "https://www.tiktok.com/",
                "sec-ch-ua": sec_ch_ua(),
                "sec-ch-ua-mobile": "?0",
                "sec-ch-ua-platform": '"Windows"',
                "sec-fetch-dest": "empty",
                "sec-fetch-mode": "cors",
                "sec-fetch-site": "same-origin",
            },
        )

    def build_query_params(
        self,
        dynamic_params: Dict[str, Any],
        cookies: Optional[Dict[str, str]] = None,
        runtime_profile: Optional[Dict[str, Any]] = None,
        *,
        include_cookie_signatures: bool = True,
        include_ms_token: bool = True,
    ) -> Dict[str, str]:
        params = self.static_params.copy()
        params.update({k: str(v) for k, v in dynamic_params.items()
                       if k not in {"proxy_country_code", "account_country_code"}})
        _apply_runtime_overrides(params, runtime_profile, dynamic_params, cookies)

        if cookies:
            device_id = cookies.get("device_id") or cookies.get("device-id")
            odin_id = cookies.get("odinId") or cookies.get("odin_tt") or cookies.get("odin-tt")
            verify_fp = cookies.get("verifyFp") or cookies.get("s_v_web_id") or cookies.get("verifyFP")
            web_id_last_time = cookies.get("WebIdLastTime") or cookies.get("web_id_last_time")
            ms_token = cookies.get("msToken") or cookies.get("ms_token")
            x_bogus = cookies.get("X-Bogus") or cookies.get("x-bogus")
            x_gnarly = cookies.get("X-Gnarly") or cookies.get("x-gnarly")
            a_bogus = cookies.get("a_bogus") or cookies.get("a-bogus")

            if device_id and ("device_id" not in params or not params.get("device_id")):
                params["device_id"] = device_id
            if odin_id and ("odinId" not in params or not params.get("odinId")):
                params["odinId"] = odin_id
            if verify_fp and ("verifyFp" not in params or not params.get("verifyFp")):
                params["verifyFp"] = verify_fp
            if web_id_last_time and "WebIdLastTime" not in params:
                params["WebIdLastTime"] = web_id_last_time
            if include_ms_token and ms_token and "msToken" not in params:
                params["msToken"] = ms_token
            if include_cookie_signatures and x_bogus and "X-Bogus" not in params:
                params["X-Bogus"] = x_bogus
            if include_cookie_signatures and x_gnarly and "X-Gnarly" not in params:
                params["X-Gnarly"] = x_gnarly
            if include_cookie_signatures and a_bogus and "a_bogus" not in params:
                params["a_bogus"] = a_bogus

        if runtime_profile:
            runtime_device_id = runtime_profile.get("device_id")
            if runtime_device_id and ("device_id" not in params or not params.get("device_id")):
                params["device_id"] = str(runtime_device_id)
            runtime_odin_id = runtime_profile.get("odin_id") or runtime_profile.get("odinId")
            if runtime_odin_id and ("odinId" not in params or not params.get("odinId")):
                params["odinId"] = str(runtime_odin_id)
            runtime_web_id_last_time = runtime_profile.get("web_id_last_time") or runtime_profile.get("WebIdLastTime")
            if runtime_web_id_last_time and "WebIdLastTime" not in params:
                params["WebIdLastTime"] = str(runtime_web_id_last_time)

        if "WebIdLastTime" not in params:
            params["WebIdLastTime"] = str(int(time.time()))

        if not include_ms_token:
            params = {key: value for key, value in params.items() if key.lower().replace("_", "") != "mstoken"}
        return params

    def build_request_parts(
        self,
        dynamic_params: Dict[str, Any],
        cookies: Optional[Dict[str, str]] = None,
        runtime_profile: Optional[Dict[str, Any]] = None,
        *,
        include_cookie_signatures: bool = True,
        include_ms_token: bool = True,
    ) -> tuple[Dict[str, str], Dict[str, str]]:
        query_params = self.build_query_params(
            dynamic_params,
            cookies,
            runtime_profile,
            include_cookie_signatures=include_cookie_signatures,
            include_ms_token=include_ms_token,
        )
        form_data: Dict[str, str] = {}
        if self.form_param_keys:
            for key in self.form_param_keys:
                if key in query_params:
                    form_data[key] = query_params.pop(key)
        return query_params, form_data

    def build_form_data(
        self,
        dynamic_params: Dict[str, Any],
        cookies: Optional[Dict[str, str]] = None,
        runtime_profile: Optional[Dict[str, Any]] = None,
        *,
        include_cookie_signatures: bool = True,
        include_ms_token: bool = True,
    ) -> Dict[str, str]:
        _, form_data = self.build_request_parts(
            dynamic_params,
            cookies,
            runtime_profile,
            include_cookie_signatures=include_cookie_signatures,
            include_ms_token=include_ms_token,
        )
        return form_data

    def build_url_from_params(self, params: Dict[str, Any]) -> str:
        query_string = urlencode(params, doseq=True)
        return f"https://www.tiktok.com{self.endpoint}?{query_string}"

    def build_url(
        self,
        dynamic_params: Dict[str, Any],
        cookies: Optional[Dict[str, str]] = None,
        runtime_profile: Optional[Dict[str, Any]] = None,
    ) -> str:
        params, _ = self.build_request_parts(
            dynamic_params,
            cookies,
            runtime_profile,
            include_cookie_signatures=True,
        )
        return self.build_url_from_params(params)

    def build_headers(
        self,
        cookies: Optional[Dict[str, str]] = None,
        user_agent: Optional[str] = None,
        runtime_profile: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, str]:
        headers = self.static_headers.copy()
        geo = resolve_request_geo(runtime_profile, cookies)
        language = geo.get("browser_language")
        runtime_languages = (runtime_profile or {}).get("languages") if language == (runtime_profile or {}).get("browser_language") else None
        accept_language = _build_accept_language(language, runtime_languages)
        if accept_language:
            headers["accept-language"] = accept_language
        runtime_user_agent = (runtime_profile or {}).get("user_agent") or user_agent

        resolved_user_agent = str(
            runtime_user_agent
            or headers.get("user-agent")
            or self.static_params.get("browser_version")
            or ""
        ).strip()

        if runtime_profile:
            current_url = runtime_profile.get("current_url")
            if current_url:
                headers["referer"] = str(current_url)

        headers["sec-ch-ua"] = _format_sec_ch_ua(runtime_profile, resolved_user_agent)
        headers["sec-ch-ua-mobile"] = "?1" if (runtime_profile or {}).get("mobile") else "?0"
        sec_ch_platform = _format_sec_ch_platform(runtime_profile, resolved_user_agent)
        if sec_ch_platform:
            headers["sec-ch-ua-platform"] = sec_ch_platform

        if cookies:
            tt_csrf_token = cookies.get("tt_csrf_token")
            if tt_csrf_token:
                headers["tt-csrf-token"] = tt_csrf_token

        return headers
