"""Compatibility helpers backed by the canonical RequestProfile.

POST callers must use *_parts to receive both URL and form body. URL-only
helpers omit form fields; dispatch/signing belongs to the live-page transport.
"""
from typing import Optional, Dict, Any
from executor.http.request_profile import RequestProfile


class TikTokEndpoints:
    BASE_URL = "https://www.tiktok.com"
    API_BASE = "https://www.tiktok.com/api"

    @staticmethod
    def comment_publish_parts(
        aweme_id: str, text: str, reply_id: Optional[str] = None,
        reply_to_reply_id: str = "0", video_url: Optional[str] = None,
        runtime_profile: Optional[Dict[str, Any]] = None,
        cookies: Optional[Dict[str, str]] = None, **extra_params,
    ) -> tuple[str, Dict[str, str]]:
        reply = bool(reply_id and str(reply_id) != "0")
        profile = RequestProfile.publish_reply_profile() if reply else RequestProfile.publish_main_profile()
        dynamic = {"aweme_id": aweme_id, "text": text,
                   "referer": video_url or (runtime_profile or {}).get("current_url") or TikTokEndpoints.BASE_URL + "/"}
        if reply:
            dynamic.update(reply_id=str(reply_id), reply_to_reply_id=str(reply_to_reply_id))
        dynamic.update(extra_params)
        query, form = profile.build_request_parts(dynamic, cookies, runtime_profile, include_cookie_signatures=False)
        return profile.build_url_from_params(query), form

    @staticmethod
    def comment_publish(*args, **kwargs) -> str:
        """Return only the URL; use comment_publish_parts for POST dispatch."""
        return TikTokEndpoints.comment_publish_parts(*args, **kwargs)[0]

    @staticmethod
    def comment_digg_parts(
        aweme_id: str, cid: str, digg_type: int = 1,
        runtime_profile: Optional[Dict[str, Any]] = None,
        cookies: Optional[Dict[str, str]] = None, **extra_params,
    ) -> tuple[str, Dict[str, str]]:
        # Old callers documented 0=unlike; normalize to the client wire value.
        digg_type = 2 if digg_type == 0 else digg_type
        if digg_type not in (1, 2):
            raise ValueError("digg_type must be 1 (like) or 2 (unlike)")
        profile = RequestProfile.like_profile()
        query, form = profile.build_request_parts(
            {"aweme_id": aweme_id, "cid": cid, "digg_type": digg_type, **extra_params},
            cookies, runtime_profile, include_cookie_signatures=False,
        )
        return profile.build_url_from_params(query), form

    @staticmethod
    def comment_digg(*args, **kwargs) -> str:
        """Return only the URL; use comment_digg_parts for POST dispatch."""
        return TikTokEndpoints.comment_digg_parts(*args, **kwargs)[0]

    @staticmethod
    def comment_list(aweme_id: str, cursor: int = 0, count: int = 20, *,
                     runtime_profile=None, cookies=None, **extra_params) -> str:
        profile = RequestProfile.comment_list_profile()
        query, _ = profile.build_request_parts(
            {"aweme_id": aweme_id, "cursor": cursor, "count": count, **extra_params},
            cookies, runtime_profile, include_cookie_signatures=False,
        )
        return profile.build_url_from_params(query)
