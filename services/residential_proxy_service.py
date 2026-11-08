"""Account-owned residential routes and bounded, cached exit-IP checks."""
import asyncio
import hashlib
import json
from ipaddress import ip_address

import httpx

from config.settings import get_settings
from services.proxy_normalizer import ProxyRouteError

_CLAIM_IP = """
local owner = redis.call('GET', KEYS[1])
if owner and owner ~= ARGV[1] then return 0 end
redis.call('SET', KEYS[1], ARGV[1], 'EX', ARGV[3])
if ARGV[5] == '1' then
  redis.call('SET', KEYS[2], ARGV[2], 'EX', ARGV[4])
end
return 1
"""
_CLAIM_ROUTE = """
local owner = redis.call('GET', KEYS[1])
if owner and owner ~= ARGV[1] then return 0 end
redis.call('SET', KEYS[1], ARGV[1], 'EX', ARGV[2])
return 1
"""


async def _probe_exit_ip(route) -> str:
    from services.proxy_service import PROXY_IP_CHECK_URL

    async with asyncio.timeout(10), httpx.AsyncClient(
        proxy=route.url, trust_env=False, follow_redirects=False,
        timeout=httpx.Timeout(8.0, connect=4.0),
    ) as client:
        async with client.stream("GET", PROXY_IP_CHECK_URL, headers={"Accept": "application/json"}) as response:
            response.raise_for_status()
            body = bytearray()
            async for chunk in response.aiter_bytes(chunk_size=1024):
                body.extend(chunk)
                if len(body) > 1024:
                    raise ProxyRouteError("residential_ip_probe_invalid")
    try:
        result = ip_address(json.loads(body)["ip"])
        if not result.is_global:
            raise ValueError("Non-public exit IP")
        return str(result)
    except (ValueError, TypeError, KeyError):
        raise ProxyRouteError("residential_ip_probe_invalid") from None


async def ensure_residential_exit_ip(account, route, *, lease=None) -> str | None:
    """Fail before TikTok I/O when two accounts use the same route or observed IP."""
    if route is None or route.is_mobile:
        return None
    account_id = getattr(account, "id", None)
    if account_id is None:
        raise ProxyRouteError("residential_account_id_missing")
    from services.proxy_concurrency_service import _lease_client

    ttl = get_settings().RESIDENTIAL_IP_CACHE_TTL_SECONDS
    digest = hashlib.sha256(route.url.encode()).hexdigest()
    route_key = f"proxy:residential:route:{digest}"
    cache_key = f"proxy:residential:checked:{account_id}:{digest}"
    owner = str(account_id)
    try:
        client = _lease_client()
        claimed = await asyncio.to_thread(client.eval, _CLAIM_ROUTE, 1, route_key, owner, ttl + 60)
        if not claimed:
            raise ProxyRouteError("residential_route_already_assigned")
        cached = await asyncio.to_thread(client.get, cache_key)
        observed = str(cached) if cached else await _probe_exit_ip(route)
        observed = str(ip_address(observed))
        ip_key = f"proxy:residential:ip:{observed}"
        claimed = await asyncio.to_thread(
            client.eval, _CLAIM_IP, 2, ip_key, cache_key,
            owner, observed, ttl + 60, ttl, "0" if cached else "1",
        )
        if not claimed:
            raise ProxyRouteError("residential_exit_ip_in_use")
        if lease is not None:
            lease.identity_owner = owner
            lease.identity_ttl = ttl + 60
            lease.identity_keys = (route_key, ip_key)
    except ProxyRouteError:
        raise
    except httpx.ProxyError as exc:
        if str(exc).split(" ", 1)[0] == "402":
            raise ProxyRouteError("residential_ip_probe_payment_required") from None
        if "407" in str(exc):
            raise ProxyRouteError("proxy_authentication_required") from None
        raise ProxyRouteError("residential_ip_probe_proxy_error") from None
    except httpx.HTTPStatusError as exc:
        code = exc.response.status_code
        if code == 402:
            raise ProxyRouteError("residential_ip_probe_payment_required") from None
        if code == 407:
            raise ProxyRouteError("proxy_authentication_required") from None
        if code == 429:
            raise ProxyRouteError("residential_ip_probe_rate_limited") from None
        if 500 <= code <= 599:
            raise ProxyRouteError("residential_ip_probe_service_error") from None
        raise ProxyRouteError("residential_ip_probe_http_error") from None
    except (httpx.TimeoutException, TimeoutError):
        raise ProxyRouteError("residential_ip_probe_timeout") from None
    except httpx.ConnectError:
        raise ProxyRouteError("residential_ip_probe_connect_error") from None
    except httpx.HTTPError:
        raise ProxyRouteError("residential_ip_probe_transport_error") from None
    except (ValueError, TypeError):
        raise ProxyRouteError("residential_ip_probe_invalid") from None
    except Exception:
        raise ProxyRouteError("residential_coordination_unavailable") from None
    account.proxy_exit_ip = observed
    return observed
