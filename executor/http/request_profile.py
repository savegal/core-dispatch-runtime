from dataclasses import dataclass, field
import time
from typing import Any, Dict, Iterable, Optional
from urllib.parse import urlencode


_SIGNATURE_QUERY_KEYS = {"X-Bogus", "X-Gnarly", "a_bogus"}
COUNTRY_TO_TIMEZONE = {
    "CA": "America/Toronto",
    "DE": "Europe/Berlin",
    "FR": "Europe/Paris",
    "GB": "Europe/London",
    "IL": "Asia/Jerusalem",
    "JP": "Asia/Tokyo",
    "KZ": "Asia/Almaty",
    "NL": "Europe/Amsterdam",
    "PL": "Europe/Warsaw",
    "RU": "Europe/Moscow",
    "SG": "Asia/Singapore",
    "TR": "Europe/Istanbul",
    "UA": "Europe/Kyiv",
    "US": "America/New_York",
}


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

    ua_value = user_agent or ""
    major_version = "120"
    if "Chrome/" in ua_value:
        tail = ua_value.split("Chrome/", 1)[1]
        digits: list[str] = []
        for char in tail:
            if char.isdigit():
                digits.append(char)
            else:
                break
        if digits:
            major_version = "".join(digits)
    return f'"Google Chrome";v="{major_version}", "Chromium";v="{major_version}", "Not A(Brand";v="24"'


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
) -> None:
    if not runtime_profile:
        return

    dynamic_params = dynamic_params or {}
    primary_language = _normalize_locale(runtime_profile.get("browser_language"))
    if primary_language:
        params["app_language"] = primary_language
        params["browser_language"] = primary_language
        params["webcast_language"] = primary_language

    user_agent = runtime_profile.get("user_agent")
    if user_agent:
        params["browser_version"] = str(user_agent)

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

    proxy_country_code = str(
        dynamic_params.get("proxy_country_code")
        or runtime_profile.get("proxy_country_code")
        or ""
    ).strip().upper() or None
    mapped_timezone = COUNTRY_TO_TIMEZONE.get(proxy_country_code or "")
    timezone_name = runtime_profile.get("timezone")
    if mapped_timezone:
        params["tz_name"] = mapped_timezone
    elif timezone_name:
        params["tz_name"] = str(timezone_name)

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

    region = (
        proxy_country_code
        or runtime_profile.get("region")
        or _extract_region_from_locale(primary_language)
        or _extract_region_from_locale(runtime_profile.get("locale"))
    )
    if region:
        params["region"] = str(region)
        params["priority_region"] = str(region)

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
                "app_language": "ru-RU",
                "app_name": "tiktok_web",
                "browser_language": "ru-RU",
                "browser_name": "Mozilla",
                "browser_online": "true",
                "browser_platform": "Win32",
                "browser_version": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/143.0.0.0 Safari/537.36",
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
                "priority_region": "PL",
                "referer": "https://www.google.com/",
                "region": "SG",
                "root_referer": "https://www.google.com/",
                "screen_height": "864",
                "screen_width": "1536",
                "tz_name": "Asia/Jerusalem",
                "user_is_login": "true",
                "webcast_language": "ru-RU",
            },
            static_headers={
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
                "x-secsdk-csrf-token": "DOWNGRADE",
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
                "app_language": "ru-RU",
                "app_name": "tiktok_web",
                "browser_language": "ru-RU",
                "browser_name": "Mozilla",
                "browser_online": "true",
                "browser_platform": "Win32",
                "browser_version": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/143.0.0.0 Safari/537.36",
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
                "priority_region": "PL",
                "referer": "https://www.google.com/",
                "region": "SG",
                "root_referer": "https://www.google.com/",
                "screen_height": "864",
                "screen_width": "1536",
                "text_extra": "[]",
                "tz_name": "Asia/Jerusalem",
                "user_is_login": "true",
                "webcast_language": "ru-RU",
            },
            static_headers={
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
                "x-secsdk-csrf-token": "DOWNGRADE",
                "x-secsdk-csrf-request": "1",
                "x-secsdk-csrf-version": "1.2.22",
            },
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
                "app_language": "ru-RU",
                "app_name": "tiktok_web",
                "browser_language": "ru-RU",
                "browser_name": "Mozilla",
                "browser_online": "true",
                "browser_platform": "Win32",
                "browser_version": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/143.0.0.0 Safari/537.36",
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
                "priority_region": "SG",
                "referer": "https://www.google.com/",
                "reply_to_reply_id": "0",
                "root_referer": "https://www.google.com/",
                "screen_height": "864",
                "screen_width": "1536",
                "text_extra": "[]",
                "tz_name": "Asia/Jerusalem",
                "user_is_login": "true",
                "webcast_language": "ru-RU",
            },
            static_headers={
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
                "x-secsdk-csrf-token": "DOWNGRADE",
            },
        )

    def build_query_params(
        self,
        dynamic_params: Dict[str, Any],
        cookies: Optional[Dict[str, str]] = None,
        runtime_profile: Optional[Dict[str, Any]] = None,
        *,
        include_cookie_signatures: bool = True,
    ) -> Dict[str, str]:
        params = self.static_params.copy()
        params.update({k: str(v) for k, v in dynamic_params.items()})
        _apply_runtime_overrides(params, runtime_profile, dynamic_params)

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
            if ms_token and "msToken" not in params:
                params["msToken"] = ms_token
            if include_cookie_signatures and x_bogus and "X-Bogus" not in params:
                params["X-Bogus"] = x_bogus
            if include_cookie_signatures and x_gnarly and "X-Gnarly" not in params:
                params["X-Gnarly"] = x_gnarly
            if include_cookie_signatures and a_bogus and "a_bogus" not in params:
                params["a_bogus"] = a_bogus

        if "WebIdLastTime" not in params:
            params["WebIdLastTime"] = str(int(time.time()))

        return params

    def build_request_parts(
        self,
        dynamic_params: Dict[str, Any],
        cookies: Optional[Dict[str, str]] = None,
        runtime_profile: Optional[Dict[str, Any]] = None,
        *,
        include_cookie_signatures: bool = True,
    ) -> tuple[Dict[str, str], Dict[str, str]]:
        query_params = self.build_query_params(
            dynamic_params,
            cookies,
            runtime_profile,
            include_cookie_signatures=include_cookie_signatures,
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
    ) -> Dict[str, str]:
        _, form_data = self.build_request_parts(
            dynamic_params,
            cookies,
            runtime_profile,
            include_cookie_signatures=include_cookie_signatures,
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
        runtime_user_agent = user_agent or (runtime_profile or {}).get("user_agent")

        resolved_user_agent = str(
            runtime_user_agent
            or headers.get("user-agent")
            or self.static_params.get("browser_version")
            or ""
        ).strip()

        if runtime_profile:
            accept_language = _build_accept_language(
                runtime_profile.get("browser_language"),
                runtime_profile.get("languages"),
            )
            if accept_language:
                headers["accept-language"] = accept_language

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
                headers["x-secsdk-csrf-token"] = tt_csrf_token
            elif headers.get("x-secsdk-csrf-token") == "DOWNGRADE":
                headers.pop("x-secsdk-csrf-token", None)
        elif headers.get("x-secsdk-csrf-token") == "DOWNGRADE":
            headers.pop("x-secsdk-csrf-token", None)

        return headers
