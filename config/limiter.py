"""
Централизованная конфигурация rate limiter для API.
Вынесено в отдельный модуль для избежания циклических импортов.
"""
from slowapi import Limiter
from slowapi.util import get_remote_address

def rate_limit_key_func(request):
    """
    Формирует ключ rate-limit.
    Приоритет:
    1) X-RateLimit-Key (например user:<id> от bot API client)
    2) X-Forwarded-For (первый IP)
    3) remote address
    """
    if request is None:
        return "anonymous"

    header_key = request.headers.get("X-RateLimit-Key")
    if header_key:
        # Ограничиваем длину и убираем лишние пробелы
        safe_key = header_key.strip()[:128]
        if safe_key:
            return f"rk:{safe_key}"

    query_user_id = request.query_params.get("user_id")
    if query_user_id:
        safe_user_id = query_user_id.strip()[:64]
        if safe_user_id:
            return f"uid:{safe_user_id}"

    forwarded_for = request.headers.get("X-Forwarded-For")
    if forwarded_for:
        first_ip = forwarded_for.split(",")[0].strip()
        if first_ip:
            return f"ip:{first_ip}"

    remote_ip = get_remote_address(request) or "anonymous"
    return f"ip:{remote_ip}"


# ИСПРАВЛЕНО: Создаём limiter в отдельном модуле для избежания циклических импортов
limiter = Limiter(key_func=rate_limit_key_func)



