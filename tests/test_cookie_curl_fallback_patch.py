"""Exercise actual curl_cffi selection and the aiohttp backup without live network."""
import asyncio
from types import SimpleNamespace

import pytest
from curl_cffi.requests import impersonate
from executor import browser_identity
from services import cookie_validator as validator
from services import proxy_normalizer
from executor.http import request_profile


@pytest.mark.parametrize("curl_raises", [False, True])
def test_curl_passport_and_aiohttp_backup(monkeypatch, curl_raises):
    calls = {"curl": 0, "aiohttp": 0}
    profiles = []

    class CurlResponse:
        status_code = 200
        headers = {}

        def json(self):
            return {"message": "success", "data": {"username": "demo", "user_id_str": "456"}}

    class CurlSession:
        def __init__(self, **kwargs):
            profiles.append(kwargs["impersonate"])

        async def __aenter__(self):
            calls["curl"] += 1
            if curl_raises:
                raise OSError("simulated curl transport error")
            return self

        async def __aexit__(self, *exc):
            return None

        async def get(self, *args, **kwargs):
            return CurlResponse()

    class AioResponse:
        status = 200
        headers = {}

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return None

        async def json(self, **kwargs):
            return {"message": "success", "data": {"username": "demo", "user_id_str": "456"}}

    class AioSession:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return None

        def get(self, *args, **kwargs):
            calls["aiohttp"] += 1
            return AioResponse()

    monkeypatch.setenv("VALIDATOR_TEST_CURL_CFFI", "1")
    monkeypatch.setattr(browser_identity, "_running_version", "144.0.0.0")
    available_profiles = [SimpleNamespace(value=name) for name in ("chrome131", "chrome142", "chrome145")]
    monkeypatch.setattr(impersonate, "BrowserType", available_profiles)
    monkeypatch.setattr(validator, "_CurlAsyncSession", CurlSession)
    monkeypatch.setattr(validator.aiohttp, "ClientSession", AioSession)
    monkeypatch.setattr(validator, "_ORIGINAL_AIOHTTP_CLIENT_SESSION", AioSession)
    monkeypatch.setattr(validator, "get_cookies_for_account", lambda _: "sessionid=123")
    monkeypatch.setattr(validator, "parse_cookies_to_dict", lambda _: {"sessionid": "123"})
    monkeypatch.setattr(proxy_normalizer, "account_proxy_route", lambda _: None)
    monkeypatch.setattr(request_profile, "resolve_request_geo", lambda **_: {"browser_language": "en-US"})
    monkeypatch.setattr(request_profile, "_build_accept_language", lambda *args: "en-US")

    result = asyncio.run(validator._validate_account_http_tri_state(SimpleNamespace(country_code="US")))
    assert result["valid"] is True
    assert profiles == ["chrome142"]
    assert calls["curl"] == 1
    assert calls["aiohttp"] == int(curl_raises)
