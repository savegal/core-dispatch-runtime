from dataclasses import dataclass, field, asdict
from typing import Optional
import json
from config.settings import get_settings
from services.redis_service import get_redis_client
import logging

logger = logging.getLogger("bot.drafts")
settings = get_settings()

# ИСПРАВЛЕНО: Используем централизованный Redis клиент
try:
    redis_client = get_redis_client()
    redis_client.ping()  # Проверка подключения
    redis_available = True
except Exception as e:
    logger.warning(f"Redis недоступен для drafts, используется fallback в память: {e}")
    redis_available = False
    redis_client = None


@dataclass
class Draft:
    links: list[str] = field(default_factory=list)
    comment: Optional[str] = None
    reply_mode: str = "A"
    likes: int = 0
    delays: bool = True
    shuffle: bool = False
    rotate_accounts: bool = True
    account_pool_mode: str = "shared"
    text_variants: list[str] = field(default_factory=list)
    reply_like_target: str = "1"
    reply_like_count: int = 0
    
    def to_dict(self) -> dict:
        """Конвертирует Draft в словарь для Redis."""
        return {
            "links": json.dumps(self.links),
            "comment": self.comment or "",
            "reply_mode": self.reply_mode,
            "likes": str(self.likes),
            "delays": "1" if self.delays else "0",
            "shuffle": "1" if self.shuffle else "0",
            "rotate_accounts": "1" if self.rotate_accounts else "0",
            "account_pool_mode": self.account_pool_mode if self.account_pool_mode == "personal" else "shared",
            "text_variants": json.dumps(self.text_variants),
            "reply_like_target": str(self.reply_like_target),
            "reply_like_count": str(self.reply_like_count)
        }
    
    @classmethod
    def from_dict(cls, data: dict) -> "Draft":
        """Создаёт Draft из словаря из Redis."""
        links_data = data.get("links", "[]")
        try:
            links = json.loads(links_data) if links_data else []
        except Exception:
            links = []
        
        text_variants_data = data.get("text_variants", "[]")
        try:
            text_variants = json.loads(text_variants_data) if text_variants_data else []
        except Exception:
            text_variants = []

        return cls(
            links=links,
            comment=data.get("comment") or None,
            reply_mode=data.get("reply_mode", "A"),
            likes=int(data.get("likes", 0)),
            delays=data.get("delays") in ("1", "true", "True"),
            shuffle=data.get("shuffle") in ("1", "true", "True"),
            rotate_accounts=data.get("rotate_accounts") in ("1", "true", "True"),
            account_pool_mode="personal" if data.get("account_pool_mode") == "personal" else "shared",
            text_variants=text_variants,
            reply_like_target=str(data.get("reply_like_target", "1") or "1"),
            reply_like_count=int(data.get("reply_like_count", 0) or 0)
        )


# Fallback хранилища (в памяти, если Redis недоступен)
_drafts_fallback: dict[int, Draft] = {}
_last_comment_fallback: dict[int, str] = {}
_recent_links_fallback: dict[int, list[str]] = {}
_DRAFT_TTL_SECONDS = 86400


def _get_redis_key(key: str, user_id: int) -> str:
    """Формирует ключ Redis."""
    return f"draft:{user_id}:{key}"


def _write_draft_hash(user_id: int, draft: Draft) -> None:
    """Write a complete draft using Redis 3-compatible single-field HSETs."""
    redis_key = _get_redis_key("data", user_id)
    # redis-py turns hset(mapping=...) into the Redis 4+ multi-field HSET
    # syntax. The deployed Redis 3 server rejects it, so queue legacy-safe
    # field/value commands in one transaction.
    with redis_client.pipeline(transaction=True) as pipeline:
        for field_name, value in draft.to_dict().items():
            pipeline.hset(redis_key, field_name, value)
        pipeline.expire(redis_key, _DRAFT_TTL_SECONDS)
        pipeline.execute()


def get_draft(user_id: int) -> Draft:
    """Получает черновик пользователя из Redis или памяти."""
    if redis_available:
        try:
            draft_data = redis_client.hgetall(_get_redis_key("data", user_id))
            if draft_data:
                return Draft.from_dict(draft_data)
            # Создаём новый черновик
            draft = Draft()
            _write_draft_hash(user_id, draft)
            return draft
        except Exception as e:
            logger.error(f"Error getting draft from Redis: {e}, using fallback")
    
    # Fallback на память
    if user_id not in _drafts_fallback:
        _drafts_fallback[user_id] = Draft()
    return _drafts_fallback[user_id]


def reset_draft(user_id: int) -> None:
    """Сбрасывает черновик пользователя."""
    if redis_available:
        try:
            redis_client.delete(_get_redis_key("data", user_id))
        except Exception as e:
            logger.error(f"Error resetting draft in Redis: {e}")
    
    if user_id in _drafts_fallback:
        del _drafts_fallback[user_id]


def set_links(user_id: int, links: list[str]) -> None:
    """
    Устанавливает ссылки в черновик и сохраняет в недавние.
    
    ИСПРАВЛЕНО: Добавлен fallback сохранение в память при ошибках Redis.
    """
    draft = get_draft(user_id)
    draft.links = links
    
    # ИСПРАВЛЕНО: Всегда сохраняем в fallback для надежности
    _drafts_fallback[user_id] = draft
    
    # ИСПРАВЛЕНО: Сохраняем весь draft, а не только links, чтобы не потерять другие данные
    save_draft(user_id, draft)

    # Сохраняем в недавние (без дублей, максимум 5)
    links_text = "\n".join(links)
    if links_text:
        # ИСПРАВЛЕНО: Всегда сохраняем в fallback
        if user_id not in _recent_links_fallback:
            _recent_links_fallback[user_id] = []
        if links_text not in _recent_links_fallback[user_id]:
            _recent_links_fallback[user_id].insert(0, links_text)
            _recent_links_fallback[user_id] = _recent_links_fallback[user_id][:5]
        
        if redis_available:
            try:
                redis_key = _get_redis_key("recent_links", user_id)
                # Удаляем дубликаты
                redis_client.lrem(redis_key, 0, links_text)
                # Добавляем в начало
                redis_client.lpush(redis_key, links_text)
                # Оставляем только 5 последних
                redis_client.ltrim(redis_key, 0, 4)
                redis_client.expire(redis_key, 2592000)  # TTL 30 дней
            except Exception as e:
                logger.error(f"Error saving recent links to Redis: {e}, using fallback")
                logger.warning(f"Redis недоступен для сохранения ссылок, используется fallback в памяти")


def set_comment(user_id: int, text: str) -> None:
    """
    Устанавливает комментарий в черновик и сохраняет как последний.
    
    ИСПРАВЛЕНО: Добавлен fallback сохранение в память при ошибках Redis.
    Это предотвращает потерю комментария, если Redis упал после проверки доступности.
    """
    draft = get_draft(user_id)
    draft.comment = text
    draft.text_variants = []
    
    # ИСПРАВЛЕНО: Всегда сохраняем в fallback для надежности
    _last_comment_fallback[user_id] = text
    _drafts_fallback[user_id] = draft

    # Persist the complete object so clearing text_variants survives the next
    # callback and process boundary.
    save_draft(user_id, draft)
    
    # Сохраняем черновик в Redis (если доступен)
    if redis_available:
        try:
            # Сохраняем как последний комментарий
            redis_client.set(_get_redis_key("last_comment", user_id), text or "", ex=2592000)
        except Exception as e:
            logger.error(f"Error saving comment to Redis: {e}, using fallback")
            # ИСПРАВЛЕНО: При ошибке Redis сохраняем в fallback (уже сделано выше)
            logger.warning(f"Redis недоступен для сохранения комментария, используется fallback в памяти")
    else:
        # Redis изначально недоступен - fallback уже сохранен выше
        pass


def save_draft(user_id: int, draft: Draft) -> None:
    """
    Сохраняет весь draft в Redis.
    
    ИСПРАВЛЕНО: Добавлен fallback сохранение в память при ошибках Redis.
    """
    # ИСПРАВЛЕНО: Всегда сохраняем в fallback для надежности
    _drafts_fallback[user_id] = draft
    
    if redis_available:
        try:
            _write_draft_hash(user_id, draft)
        except Exception as e:
            logger.error(f"Error saving draft to Redis: {e}, using fallback")
            logger.warning(f"Redis недоступен для сохранения draft, используется fallback в памяти")
    else:
        # Redis изначально недоступен - fallback уже сохранен выше
        pass


def get_last_comment(user_id: int) -> Optional[str]:
    """Получает последний комментарий пользователя."""
    if redis_available:
        try:
            return redis_client.get(_get_redis_key("last_comment", user_id)) or None
        except Exception as e:
            logger.error(f"Error getting last comment from Redis: {e}")
    
    return _last_comment_fallback.get(user_id)


def get_recent_links(user_id: int) -> list[str]:
    """Получает недавние наборы ссылок."""
    if redis_available:
        try:
            redis_key = _get_redis_key("recent_links", user_id)
            return redis_client.lrange(redis_key, 0, -1) or []
        except Exception as e:
            logger.error(f"Error getting recent links from Redis: {e}")
    
    return _recent_links_fallback.get(user_id, [])


# Настройки пользователя по умолчанию (хранятся в памяти)
user_settings: dict[int, dict] = {}


def get_user_settings(user_id: int) -> dict:
    """Получает настройки пользователя по умолчанию."""
    if user_id not in user_settings:
        # Настройки по умолчанию
        user_settings[user_id] = {
            "delays": True,
            "shuffle": False,
            "rotate_accounts": False,
            "reply_mode": "A",
            "likes_enabled": True
        }
    return user_settings[user_id].copy()


def update_user_settings(user_id: int, **kwargs) -> None:
    """Обновляет настройки пользователя."""
    settings = get_user_settings(user_id)
    settings.update(kwargs)
    user_settings[user_id] = settings

