import asyncio
import time
from functools import wraps
from typing import Any, Awaitable, Callable, TypeVar

from sqlalchemy.ext.asyncio import AsyncSession


T = TypeVar("T")

_cache: dict[str, tuple[Any, float]] = {}
_inflight: dict[str, asyncio.Event] = {}


def clear_cache() -> None:
    _cache.clear()
    _inflight.clear()


def _key_part(value: Any) -> str:
    if isinstance(value, AsyncSession):
        try:
            bind = value.get_bind()
            url = getattr(bind, "url", None)
            if url is not None:
                return f"<AsyncSession:{url.render_as_string(hide_password=True)}>"
        except Exception:
            pass
        return "<AsyncSession>"
    return repr(value)


def _make_cache_key(func_name: str, args: tuple[Any, ...], kwargs: dict[str, Any]) -> str:
    args_key = ",".join(_key_part(arg) for arg in args)
    kwargs_key = ",".join(f"{key}={_key_part(value)}" for key, value in sorted(kwargs.items()))
    return f"{func_name}:({args_key}):({kwargs_key})"


def _purge_expired(now: float) -> None:
    for key, (_, expires_at) in list(_cache.items()):
        if now >= expires_at:
            _cache.pop(key, None)


def cached_with_ttl(ttl_seconds: float) -> Callable[[Callable[..., Awaitable[T]]], Callable[..., Awaitable[T]]]:
    def decorator(func: Callable[..., Awaitable[T]]) -> Callable[..., Awaitable[T]]:
        @wraps(func)
        async def wrapper(*args: Any, **kwargs: Any) -> T:
            key = _make_cache_key(func.__name__, args, kwargs)
            now = time.monotonic()
            _purge_expired(now)
            cached = _cache.get(key)

            if cached is not None and now < cached[1]:
                return cached[0]

            event = _inflight.get(key)
            if event is not None:
                await event.wait()
                cached = _cache.get(key)
                if cached is not None and time.monotonic() < cached[1]:
                    return cached[0]

            event = asyncio.Event()
            _inflight[key] = event
            try:
                result = await func(*args, **kwargs)
                _cache[key] = (result, time.monotonic() + ttl_seconds)
                return result
            finally:
                event.set()
                _inflight.pop(key, None)

        return wrapper

    return decorator
