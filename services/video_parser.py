"""
Сервис для парсинга видео из профилей TikTok блогеров.
"""
import re
import logging
import asyncio
import random
import hashlib
import time
from typing import List, Tuple, Optional, Set, Literal
from dataclasses import dataclass, field
from patchright.async_api import Page
from config.settings import get_settings
from db.models import Account
from services.account_service import MONITOR_SCOPE, get_http_execution_strategy
from executor.dom_actions import dismiss_cookie_banners

logger = logging.getLogger("services.video_parser")
settings = get_settings()
_EMPTY_DIAGNOSTIC_LAST_SAVED: dict[str, float] = {}
_WARMUP_CACHE: dict[str, tuple[float, bool]] = {}

VIDEO_ID_RE = re.compile(r"/video/(\d+)")
PINNED_DOM_VERIFY_LIMIT = 6
TIKTOK_API_RATE_LIMIT_STATUS_CODES = {8, 20012, 2190}
_ITEM_LIST_KEYS = (
    "itemList",
    "item_list",
    "aweme_list",
    "awemeList",
    "videoList",
    "video_list",
    "items",
    "posts",
    "creator_videos",
    "creatorVideos",
    "aweme_detail",
    "awemeDetail",
)


def _coerce_tiktok_status_code(payload: dict) -> Optional[int]:
    for key in ("status_code", "statusCode", "status"):
        value = payload.get(key)
        try:
            return int(value)
        except (TypeError, ValueError):
            continue
    return None


def _is_tiktok_api_rate_limited_payload(payload: object) -> bool:
    if not isinstance(payload, dict):
        return False
    status_code = _coerce_tiktok_status_code(payload)
    status_text = " ".join(
        str(payload.get(key) or "")
        for key in ("status_msg", "statusMsg", "message", "msg", "detail")
    ).lower()
    if status_code in TIKTOK_API_RATE_LIMIT_STATUS_CODES:
        return True
    return any(
        marker in status_text
        for marker in (
            "rate limit",
            "rate limited",
            "too many requests",
            "try again later",
            "temporarily unavailable",
        )
    )


def _extract_author_username_from_item(item: dict) -> Optional[str]:
    try:
        for author_key in ("author", "authorInfo", "author_info", "userInfo", "user"):
            author = item.get(author_key)
            if isinstance(author, dict):
                for uid_key in ("unique_id", "uniqueId", "uniqueID", "username", "user_unique_id", "userUniqueId"):
                    uid = author.get(uid_key)
                    if uid:
                        return str(uid).lower().lstrip("@")
                nested = author.get("userInfo") or author.get("user")
                if isinstance(nested, dict):
                    for uid_key in ("unique_id", "uniqueId", "username"):
                        uid = nested.get(uid_key)
                        if uid:
                            return str(uid).lower().lstrip("@")
            if isinstance(author, str):
                return author.lower().lstrip("@")
        for direct_key in ("author_name", "authorName", "unique_id", "uniqueId", "username", "nickname", "uniqueID"):
            direct = item.get(direct_key)
            if direct:
                return str(direct).lower().lstrip("@")
    except Exception:
        return None
    return None


def _payload_mentions_username(payload: object, username_lower: str, *, depth: int = 0) -> bool:
    if depth > 5:
        return False
    if isinstance(payload, dict):
        author = _extract_author_username_from_item(payload)
        if author and author == username_lower:
            return True
        for key in ("data", "body", "result", "user", "userInfo", "author", "authorInfo", "author_info"):
            value = payload.get(key)
            if isinstance(value, dict) and _payload_mentions_username(value, username_lower, depth=depth + 1):
                return True
    return False


def _author_matches_item(item: dict, username_lower: str, *, allow_missing_author: bool) -> bool:
    author_username = _extract_author_username_from_item(item)
    if author_username:
        return author_username == username_lower
    return allow_missing_author


def _get_aweme_id_from_item(item: dict) -> Optional[str]:
    if not isinstance(item, dict):
        return None
    strong_keys = ("aweme_id", "awemeId", "awemeID", "video_id", "videoId", "item_id", "itemId")
    for id_key in strong_keys:
        val = item.get(id_key)
        if val is None:
            continue
        s = str(val)
        if s.isdigit() and 16 <= len(s) <= 22:
            return s
    val = item.get("id")
    s = str(val) if val is not None else ""
    if s.isdigit() and 16 <= len(s) <= 22:
        return s
    return None


def _truthy_tiktok_flag(value: object) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    text = str(value or "").strip().lower()
    return text in {"1", "true", "yes", "pinned", "top"}


def _item_is_pinned(item: dict) -> bool:
    return any(
        _truthy_tiktok_flag(item.get(key))
        for key in ("is_pinned", "pinned", "isPinned", "isTop", "top", "is_top")
    )


def _get_create_time_from_item(item: dict) -> Optional[int]:
    if not isinstance(item, dict):
        return None
    for key in ("create_time", "createTime", "createdAt", "created_at", "publish_time"):
        val = item.get(key)
        if val is None:
            continue
        try:
            n = int(val)
        except (TypeError, ValueError):
            continue
        while n > 10_000_000_000:
            n //= 1000
        if 1_000_000_000 < n < 4_000_000_000:
            return n
    return None


def _should_save_empty_diagnostic(username: str) -> bool:
    if not getattr(settings, "VIDEO_PARSER_SAVE_HTML_ON_EMPTY", False):
        return False
    cooldown_seconds = max(60, int(getattr(settings, "VIDEO_PARSER_DIAGNOSTIC_COOLDOWN_SECONDS", 3600)))
    now_ts = time.time()
    last_saved = _EMPTY_DIAGNOSTIC_LAST_SAVED.get(username)
    if last_saved and now_ts - last_saved < cooldown_seconds:
        return False
    _EMPTY_DIAGNOSTIC_LAST_SAVED[username] = now_ts
    return True


def _warmup_cache_key(account: Account) -> str:
    return f"monitor:{account.id}"


def _warmup_ttl_seconds() -> float:
    return float(max(30, int(getattr(settings, "VIDEO_PARSING_WARMUP_TTL_SECONDS", 240))))


def _recent_monitor_warmup(account: Account) -> bool:
    cached = _WARMUP_CACHE.get(_warmup_cache_key(account))
    if not cached:
        return False
    warmed_at, suspicious = cached
    if suspicious:
        return False
    return (time.time() - warmed_at) <= _warmup_ttl_seconds()


def _remember_monitor_warmup(account: Account, *, suspicious: bool = False) -> None:
    _WARMUP_CACHE[_warmup_cache_key(account)] = (time.time(), suspicious)


def _should_collect_full_empty_diagnostics(account: Optional[Account], username: str) -> bool:
    if account:
        try:
            strategy = get_http_execution_strategy(account, MONITOR_SCOPE)
            if strategy["state_bucket"] in {"degraded", "proxy_issue", "cooldown"}:
                return True
            if strategy["session_bucket"] in {"http_degraded", "dom_only", "weak"}:
                return True
        except Exception:
            logger.debug("[VIDEO_PARSER] Could not inspect parser strategy for diagnostics", exc_info=True)

    sample_rate = max(1, int(getattr(settings, "VIDEO_PARSER_EMPTY_DIAGNOSTIC_SAMPLE_RATE", 4)))
    if sample_rate <= 1:
        return True
    digest = int(hashlib.md5(username.encode("utf-8")).hexdigest()[:8], 16)
    return (digest % sample_rate) == 0


def _has_sufficient_api_ids(
    video_ids_from_api_ordered: List[Tuple[str, bool, Optional[int]]],
    limit: int,
    stop_at_video_id: Optional[str],
) -> bool:
    if not video_ids_from_api_ordered:
        return False
    found_ids = {item[0] for item in video_ids_from_api_ordered}
    if stop_at_video_id and stop_at_video_id in found_ids:
        return True
    target = max(2, min(limit, int(getattr(settings, "VIDEO_PARSER_EARLY_API_TARGET", 6))))
    return len(found_ids) >= target


def _get_blogger_behavior_seed(username: str) -> int:
    """
    Генерирует уникальный seed для поведения на основе username.
    Это обеспечивает вариативность между разными блогерами.
    """
    hash_obj = hashlib.md5(username.encode('utf-8'))
    return int(hash_obj.hexdigest()[:8], 16) % 1000


def _get_blogger_random(username: str) -> random.Random:
    """
    Возвращает Random объект с seed на основе username.
    Это гарантирует одинаковое поведение для одного блогера, но разное для разных.
    """
    seed = _get_blogger_behavior_seed(username)
    return random.Random(seed)


class PageBlocked(Exception):
    """Исключение для заблокированных страниц (captcha/login/verify)."""
    pass


class RateLimited(Exception):
    """Исключение для rate limiting (HTTP 429)."""
    pass


@dataclass
class ScanResult:
    """
    Результат сканирования профиля блогера.
    
    P1.5: Структурированный результат с взаимоисключающими статусами.
    Статусы:
    - "OK": Найдены новые видео или anchor найден (new_videos может быть пустым)
    - "RESYNC": Anchor не найден, есть newest_normal_video_id для обновления
    - "NO_NORMAL_VIDEOS": Профиль пустой/только pinned/приватный (нет normal видео)
    - "EMPTY": Вообще ничего не получили (не загрузилось)
    - "FAIL": Ошибка парсинга
    - "RATE_LIMIT": HTTP 429
    """
    status: Literal["OK", "RESYNC", "NO_NORMAL_VIDEOS", "EMPTY", "FAIL", "RATE_LIMIT"]
    new_videos: List[Tuple[str, str]]  # (video_id, url) - только normal видео
    newest_normal_video_id: Optional[str]  # Самое свежее normal видео из canonical_videos (для RESYNC/OK)
    anchor_found: bool  # Найден ли anchor в списке
    canonical_source: Literal["API", "DOM", "JSON"]  # Источник данных
    pages_loaded: int  # Количество загруженных страниц
    diagnostics: dict  # Дополнительная диагностика (reason, profile_state, etc.)
    video_create_times: List[int] = field(default_factory=list)
    # Unix timestamp'ы non-pinned видео из API, sorted desc (newest first).
    # Пусто если canonical_source != "API" или create_time не было в ответе.
    # Используется adaptive_scheduler для расчёта интервалов проверки.
    video_create_times_map: dict = field(default_factory=dict)
    # video_id → create_time (unix int) для non-pinned API видео.
    # Используется при создании CreatorVideo для сохранения точного create_time в meta.


def extract_video_id(url: str) -> str | None:
    """
    Извлекает video_id из URL TikTok видео.
    
    Args:
        url: URL видео (например, https://www.tiktok.com/@user/video/1234567890)
        
    Returns:
        str | None: video_id или None если не найден
    """
    if not url:
        return None
    
    m = VIDEO_ID_RE.search(url)
    return m.group(1) if m else None


def is_valid_video_url(url: str, username: str) -> bool:
    """
    Проверяет, что URL является валидной ссылкой на видео нужного блогера.
    
    Args:
        url: URL для проверки
        username: username блогера (без @)
        
    Returns:
        bool: True если URL валиден
    """
    if not url or not isinstance(url, str):
        return False
    
    username_lower = username.lower().lstrip("@")
    url_lower = url.lower()
    
    if "tiktok.com" not in url_lower:
        return False
    
    if f"/@{username_lower}/" not in url_lower:
        return False
    
    return bool(re.search(rf"/@{re.escape(username_lower)}/video/\d+", url_lower))


async def warmup_before_parsing(page: Page, account: Account) -> dict[str, str | bool]:
    """
    Прогрев аккаунта перед парсингом профиля.
    Открывает FYP, прокручивает видео, имитирует человеческое поведение.
    
    Args:
        page: Playwright Page объект
        account: Аккаунт для прогрева
    """
    if not settings.VIDEO_PARSING_WARMUP_ENABLED:
        logger.debug(f"[VIDEO_PARSER] Warmup disabled, skipping for account {account.id}")
        return {"performed": False, "mode": "disabled"}

    recent_warmup = _recent_monitor_warmup(account)
    if recent_warmup:
        logger.debug(f"[VIDEO_PARSER] Reusing recent warmup for account {account.id}")
        _remember_monitor_warmup(account, suspicious=False)
        return {"performed": False, "mode": "light"}
    
    try:
        from executor.human_behavior import (
            natural_scroll,
            scroll_wheel_natural,
            random_mouse_movement,
            simulate_reading
        )
    except ImportError:
        logger.warning("[VIDEO_PARSER] human_behavior module not available, using basic warmup")
        # Fallback на базовый warmup без human_behavior
        try:
            await page.goto("https://www.tiktok.com/foryou", wait_until="domcontentloaded", timeout=30_000)
            await page.wait_for_timeout(random.randint(2000, 4000))
            for _ in range(random.randint(2, 3)):
                await page.evaluate(f"window.scrollBy(0, {random.randint(400, 700)})")
                await page.wait_for_timeout(random.randint(2000, 5000))
            _remember_monitor_warmup(account, suspicious=False)
        except Exception as e:
            logger.debug(f"[VIDEO_PARSER] Basic warmup failed: {e}")
        return {"performed": True, "mode": "fallback"}
    
    logger.debug(f"[VIDEO_PARSER] Starting warmup for account {account.id}")
    
    try:
        # 1. Открываем главную страницу (FYP)
        await page.goto(
            "https://www.tiktok.com/foryou",
            wait_until="domcontentloaded",
            timeout=30_000
        )
        
        # Случайная задержка после загрузки
        delay = random.uniform(
            settings.VIDEO_PARSING_INITIAL_LOAD_DELAY_MIN_MS / 1000,
            settings.VIDEO_PARSING_INITIAL_LOAD_DELAY_MAX_MS / 1000
        )
        await asyncio.sleep(delay)
        
        # КРИТИЧНО: Проверка валидности куки на этапе warm-up
        # Если куки мертвые, узнаем сразу, не тратя время на просмотр ленты
        account_status, reason = await quick_detect_dead_account_for_parsing(page, "foryou")
        if account_status == AccountStatus.HARD_DEAD:
            logger.warning(f"[VIDEO_PARSER] HARD_DEAD account detected during warmup for account {account.id}: {reason}")
            _remember_monitor_warmup(account, suspicious=True)
            raise PageBlocked(f"HARD_DEAD account detected during warmup: {reason}")
        elif account_status == AccountStatus.SUSPICIOUS:
            logger.warning(f"[VIDEO_PARSER] SUSPICIOUS account detected during warmup for account {account.id}: {reason}")
            _remember_monitor_warmup(account, suspicious=True)
            raise PageBlocked(f"SUSPICIOUS account detected during warmup: {reason}")
        
        # 2. Прокручиваем 2-3 видео (с вариативностью на основе аккаунта)
        # Используем комбинацию username и account.id для уникальности
        behavior_seed = _get_blogger_behavior_seed(f"{account.id}_{account.label or 'unknown'}")
        local_random = random.Random(behavior_seed)
        scrolls = local_random.randint(
            settings.VIDEO_PARSING_WARMUP_SCROLLS_MIN,
            settings.VIDEO_PARSING_WARMUP_SCROLLS_MAX
        )
        
        for i in range(scrolls):
            # Естественный скролл (используем scroll_wheel_natural для точного количества пикселей)
            scroll_amount = random.randint(
                settings.VIDEO_PARSING_SCROLL_AMOUNT_MIN,
                settings.VIDEO_PARSING_SCROLL_AMOUNT_MAX
            )
            await scroll_wheel_natural(page, scroll_amount)
            
            # Имитация просмотра видео
            watch_time = random.randint(
                settings.VIDEO_PARSING_WARMUP_WATCH_TIME_MIN_MS,
                settings.VIDEO_PARSING_WARMUP_WATCH_TIME_MAX_MS
            )
            await simulate_reading(page, duration_ms=watch_time)
            
            # Случайные движения мыши (если включено)
            if settings.RANDOM_MOUSE_MOVEMENTS and random.random() < settings.VIDEO_PARSING_MOUSE_MOVEMENT_CHANCE:
                await random_mouse_movement(page, min_moves=1, max_moves=2)
        
        logger.debug(f"[VIDEO_PARSER] Warmup completed for account {account.id}")
        _remember_monitor_warmup(account, suspicious=False)
        return {"performed": True, "mode": "full"}
        
    except Exception as e:
        logger.debug(f"[VIDEO_PARSER] Warmup failed for account {account.id}: {e}, continuing anyway")
        return {"performed": False, "mode": "failed"}


async def natural_scroll_for_videos(page: Page, username: str) -> int:
    """
    Естественный скроллинг для загрузки видео с вариативностью.
    
    Args:
        page: Playwright Page объект
        username: username блогера (для логирования)
        
    Returns:
        int: Количество найденных видео после скроллинга
    """
    try:
        from executor.human_behavior import scroll_wheel_natural, simulate_reading
    except ImportError:
        logger.warning("[VIDEO_PARSER] human_behavior module not available, using basic scrolling")
        # Fallback на базовый скроллинг
        scroll_count = random.randint(
            settings.VIDEO_PARSING_SCROLL_ITERATIONS_MIN,
            settings.VIDEO_PARSING_SCROLL_ITERATIONS_MAX
        )
        for i in range(scroll_count):
            scroll_amount = random.randint(
                settings.VIDEO_PARSING_SCROLL_AMOUNT_MIN,
                settings.VIDEO_PARSING_SCROLL_AMOUNT_MAX
            )
            await page.evaluate(f"window.scrollBy(0, {scroll_amount})")
            delay = random.uniform(
                settings.VIDEO_PARSING_SCROLL_DELAY_MIN_MS / 1000,
                settings.VIDEO_PARSING_SCROLL_DELAY_MAX_MS / 1000
            )
            await asyncio.sleep(delay)
        return 0
    
    scroll_count = random.randint(
        settings.VIDEO_PARSING_SCROLL_ITERATIONS_MIN,
        settings.VIDEO_PARSING_SCROLL_ITERATIONS_MAX
    )
    videos_found = 0
    
    for i in range(scroll_count):
        # Случайная скорость скролла
        scroll_amount = random.randint(
            settings.VIDEO_PARSING_SCROLL_AMOUNT_MIN,
            settings.VIDEO_PARSING_SCROLL_AMOUNT_MAX
        )
        await scroll_wheel_natural(page, scroll_amount)
        
        # Случайная задержка
        delay = random.uniform(
            settings.VIDEO_PARSING_SCROLL_DELAY_MIN_MS / 1000,
            settings.VIDEO_PARSING_SCROLL_DELAY_MAX_MS / 1000
        )
        await asyncio.sleep(delay)
        
        # Имитация чтения (с вероятностью)
        if random.random() < settings.VIDEO_PARSING_READ_SIMULATION_CHANCE:
            read_time = random.randint(
                settings.VIDEO_PARSING_READ_TIME_MIN_MS,
                settings.VIDEO_PARSING_READ_TIME_MAX_MS
            )
            await simulate_reading(page, duration_ms=read_time)
        
        # Иногда скролл вверх (10% шанс - как будто пересматриваешь)
        if random.random() < 0.1:
            back_scroll = -random.randint(200, 500)
            await scroll_wheel_natural(page, back_scroll)
            await asyncio.sleep(random.uniform(0.5, 1.5))
        
        # Проверяем, появились ли новые видео
        try:
            current_videos = await page.evaluate("""
                () => document.querySelectorAll('a[href*="/video/"]').length
            """)
            if current_videos > videos_found:
                videos_found = current_videos
                logger.debug(f"[VIDEO_PARSER] Found {videos_found} video links after scroll {i+1} for @{username}")
        except Exception:
            pass
    
    return videos_found


async def simulate_human_interaction(page: Page, username: str, *, light: bool = False) -> None:
    """
    Имитирует человеческое взаимодействие со страницей.
    Движения мыши, hover на элементы, случайные паузы.
    
    Args:
        page: Playwright Page объект
        username: username блогера (для логирования)
    """
    if not settings.HUMAN_LIKE_BEHAVIOR:
        return
    
    try:
        from executor.human_behavior import random_mouse_movement, bezier_mouse_move
    except ImportError:
        return
    
    # Случайные движения мыши
    if settings.RANDOM_MOUSE_MOVEMENTS and random.random() < settings.VIDEO_PARSING_MOUSE_MOVEMENT_CHANCE:
        try:
            await random_mouse_movement(page, min_moves=1, max_moves=3)
        except Exception as e:
            logger.debug(f"[VIDEO_PARSER] Mouse movement failed: {e}")
    
    # УЛУЧШЕНО: Более реалистичный hover и имитация кликов
    hover_chance = settings.VIDEO_PARSING_HOVER_CHANCE * (0.35 if light else 1.0)
    if random.random() < hover_chance:
        try:
            elements = await page.query_selector_all('a[href*="/video/"], div[class*="video"], div[class*="Video"]')
            if elements:
                # Берем первые 10 элементов для безопасности
                random_element = random.choice(elements[:min(10, len(elements))])
                box = await random_element.bounding_box()
                if box:
                    # Движение мыши к элементу по кривой Безье
                    center_x = box['x'] + box['width'] / 2
                    center_y = box['y'] + box['height'] / 2
                    await bezier_mouse_move(page, center_x + random.randint(-10, 10), center_y + random.randint(-5, 5))
                    await asyncio.sleep(random.uniform(0.15, 0.45) if light else random.uniform(0.3, 0.8))
                    await random_element.hover(timeout=2000)
                    await asyncio.sleep(random.uniform(0.25, 0.9) if light else random.uniform(0.8, 2.5))
                    
                    # Иногда имитируем клик (10% шанс) - но НЕ открываем видео, просто движение мыши
                    if random.random() < 0.1:
                        await page.mouse.move(center_x + random.randint(-5, 5), center_y + random.randint(-3, 3))
                        await asyncio.sleep(random.uniform(0.2, 0.5))
        except Exception as e:
            logger.debug(f"[VIDEO_PARSER] Hover failed: {e}")


# P0: Enum для severity детекции
from enum import Enum

class AccountStatus(Enum):
    """Статус аккаунта после детекции."""
    ALIVE = "alive"
    HARD_DEAD = "hard_dead"  # Явные признаки мертвого аккаунта (redirect, login без user indicators, ошибки)
    SUSPICIOUS = "suspicious"  # Подозрительные признаки (auth wall, login button с user indicators)
    DETECTION_ERROR = "detection_error"  # Ошибка детекции (не мертвый, но проверить не удалось)


async def quick_detect_dead_account_for_parsing(page: Page, username: str) -> tuple[AccountStatus, str]:
    """
    БЫСТРАЯ детекция мертвых аккаунтов для парсинга видео.
    P0: Двухуровневая проверка - hard dead vs suspicious.
    
    Returns:
        tuple[AccountStatus, str]: (status, reason)
        - status: AccountStatus (ALIVE, HARD_DEAD, SUSPICIOUS, DETECTION_ERROR)
        - reason: Причина (для логирования)
    """
    try:
        # 1. Проверка URL (самая быстрая) - HARD_DEAD
        current_url = page.url.lower()
        login_urls = ['/login', '/signup', '/verify', '/auth', '/register', '/sign-in']
        if any(url in current_url for url in login_urls):
            return AccountStatus.HARD_DEAD, f"HARD_DEAD: Login URL detected: {current_url[:100]}"
        
        # 2. Проверка title (быстрая) - HARD_DEAD
        try:
            page_title = await page.title()
            if page_title:
                title_lower = page_title.lower()
                if any(x in title_lower for x in ['log in', 'sign up', 'login', 'signup']):
                    return AccountStatus.HARD_DEAD, f"HARD_DEAD: Login title detected: {page_title[:50]}"
        except Exception:
            pass
        
        # 3. БЫСТРАЯ проверка через JavaScript (одна проверка вместо множества)
        try:
            await dismiss_cookie_banners(page)
        except Exception as cleanup_exc:
            logger.debug("[VIDEO_PARSER] Cookie banner cleanup before account detection failed: %s", cleanup_exc)

        dead_account_check = await page.evaluate("""
            () => {
                const checks = {
                    hasLoginButton: false,
                    hasUserIndicator: false,
                    hasError: false,
                    errorText: '',
                    loginButtonText: ''
                };
                const textOf = (el) => (
                    ((el && (el.innerText || el.textContent)) || '')
                        .replace(/\s+/g, ' ')
                        .trim()
                        .toLowerCase()
                );
                const cookieMarkers = [
                    'cookie',
                    'cookies',
                    'consent',
                    'gdpr',
                    'optional cookies',
                    'allow cookies',
                    'manage your choices',
                    'cookies policy',
                    'tiktok uses cookies',
                    'essential cookies',
                    '\u30af\u30c3\u30ad\u30fc',
                    '\ucfe0\ud0a4',
                    '\u997c\u5e72',
                    '\u96b1\u79c1',
                    '\u9690\u79c1',
                ];
                const inCookieContext = (el) => {
                    let cur = el;
                    for (let depth = 0; depth < 20 && cur && cur !== document.body && cur !== document.documentElement; depth++, cur = cur.parentElement) {
                        const label = [
                            textOf(cur),
                            cur.id || '',
                            String(cur.className || ''),
                            cur.getAttribute && (cur.getAttribute('aria-label') || ''),
                            cur.tagName || '',
                        ].join(' ').toLowerCase();
                        if (cookieMarkers.some(marker => label.includes(marker))) return true;
                    }
                    return false;
                };
                
                // Проверка 1: Кнопка "Log in" в сайдбаре или на странице
                const loginButtons = Array.from(document.querySelectorAll('button, a, div')).filter(el => {
                    if (inCookieContext(el)) return false;
                    const text = textOf(el);
                    return text.includes('log in') || text.includes('login') || text.includes('войти') || 
                           text.includes('sign in') || text === 'log in' || text === 'login';
                });
                
                if (loginButtons.length > 0) {
                    // Проверяем, что это не просто текст, а реальная кнопка
                    const visibleLoginButton = loginButtons.find(btn => {
                        const style = window.getComputedStyle(btn);
                        return style.display !== 'none' && style.visibility !== 'hidden' && 
                               (btn.offsetWidth > 0 || btn.offsetHeight > 0);
                    });
                    if (visibleLoginButton) {
                        checks.hasLoginButton = true;
                        checks.loginButtonText = (visibleLoginButton.innerText || visibleLoginButton.textContent || '').substring(0, 50);
                    }
                }
                
                // Проверка 2: User indicators (признаки залогиненного пользователя)
                // Ищем элементы, которые появляются только у залогиненных пользователей
                const userIndicators = [
                    '[data-e2e="nav-profile"]',  // Иконка профиля в навигации
                    '[data-e2e="nav-inbox"]',    // Иконка сообщений
                    'a[href*="/upload"]',        // Кнопка загрузки
                    '[data-e2e="upload-icon"]',  // Иконка загрузки
                    'div[class*="Avatar"]',      // Аватар пользователя
                    'div[class*="avatar"]',
                    '[data-e2e="user-avatar"]'
                ];
                
                for (const selector of userIndicators) {
                    try {
                        const element = document.querySelector(selector);
                        if (element) {
                            const style = window.getComputedStyle(element);
                            if (style.display !== 'none' && style.visibility !== 'hidden') {
                                checks.hasUserIndicator = true;
                                break;
                            }
                        }
                    } catch (e) {
                        // Игнорируем ошибки селекторов
                    }
                }
                
                // Проверка 3: Ошибки типа "Something went wrong"
                const errorTexts = [
                    'something went wrong',
                    'sorry about that',
                    'please try again later',
                    'произошла ошибка',
                    'что-то пошло не так',
                    'попробуйте позже',
                    'unable to load',
                    'failed to load',
                    'error loading'
                ];
                
                const allText = document.body.innerText.toLowerCase();
                for (const errorText of errorTexts) {
                    if (!allText.includes(errorText)) continue;
                    // Поиск ТОЛЬКО в маленьких видимых элементах (не body/main/section).
                    // Раньше фильтр захватывал весь body — навигация трактовалась как "error".
                    const errorElements = Array.from(document.querySelectorAll(
                        'h1, h2, h3, h4, p, span, div[class*="error" i], div[class*="empty" i], ' +
                        'div[class*="message" i], div[class*="notice" i], [data-e2e*="error" i]'
                    )).filter(el => {
                        const elText = (el.innerText || el.textContent || '').toLowerCase().trim();
                        if (!elText || elText.length > 200) return false; // отсекаем body-подобные
                        if (!elText.includes(errorText)) return false;
                        const rect = el.getBoundingClientRect();
                        if (rect.width === 0 || rect.height === 0) return false;
                        if (rect.height > 400) return false; // не корневой контейнер
                        const style = window.getComputedStyle(el);
                        if (style.display === 'none' || style.visibility === 'hidden') return false;
                        return true;
                    });
                    if (errorElements.length > 0) {
                        checks.hasError = true;
                        checks.errorText = (errorElements[0].innerText || errorElements[0].textContent || '').trim().slice(0, 150);
                        break;
                    }
                }
                
                return checks;
            }
        """)
        
        # P0: Двухуровневая проверка - hard dead vs suspicious

        # 1. HARD DEAD: Кнопка "Log in" без user indicators - явный признак мертвого аккаунта
        if dead_account_check.get('hasLoginButton') and not dead_account_check.get('hasUserIndicator'):
            login_text = dead_account_check.get('loginButtonText', '')
            return AccountStatus.HARD_DEAD, f"HARD_DEAD: Login button found without user indicators: '{login_text}'"

        # 2. SUSPICIOUS: generic TikTok error pages are often temporary and should not burn a session.
        if dead_account_check.get('hasError'):
            error_text = dead_account_check.get('errorText', '')[:100]
            return AccountStatus.SUSPICIOUS, f"SUSPICIOUS: Temporary page error detected: '{error_text}'"

        # 3. SUSPICIOUS: Кнопка "Log in" есть, но есть и user indicators (может быть переходное состояние)
        if dead_account_check.get('hasLoginButton') and dead_account_check.get('hasUserIndicator'):
            login_text = dead_account_check.get('loginButtonText', '')
            return AccountStatus.SUSPICIOUS, f"SUSPICIOUS: Login button found but user indicators also present: '{login_text}'"
        
        # P1: SUSPICIOUS только если есть признаки auth wall одновременно с отсутствием user indicators
        # НЕ считаем suspicious просто "нет user indicators" - это может быть урезанная шапка/AB-тест
        if not dead_account_check.get('hasUserIndicator'):
            # Проверяем, есть ли признаки auth wall / forced login
            has_auth_wall = False
            
            # Признаки auth wall:
            # 1. Overlay с предложением логина
            # 2. Redirect chain (уже проверено выше через URL)
            # 3. Verify prompt
            try:
                auth_wall_indicators = await page.evaluate("""
                    () => {
                        // Ищем overlay / modal с предложением логина
                        const overlays = document.querySelectorAll('[class*="overlay"], [class*="modal"], [class*="dialog"]');
                        for (const overlay of overlays) {
                            const text = (overlay.innerText || overlay.textContent || '').toLowerCase();
                            if (text.includes('log in') || text.includes('sign up') || text.includes('войти')) {
                                const style = window.getComputedStyle(overlay);
                                if (style.display !== 'none' && style.visibility !== 'hidden') {
                                    return true;
                                }
                            }
                        }
                        return false;
                    }
                """)
                has_auth_wall = auth_wall_indicators
            except Exception:
                pass
            
            # Только если есть auth wall - считаем suspicious
            if has_auth_wall:
                return AccountStatus.SUSPICIOUS, "SUSPICIOUS: No user indicators and auth wall detected"
            # Иначе - не считаем suspicious (может быть просто урезанная шапка)
        
        return AccountStatus.ALIVE, "Account appears alive"
        
    except Exception as e:
        logger.debug(f"[VIDEO_PARSER] Error in quick_detect_dead_account: {e}")
        # P0.3: При ошибке проверки - возвращаем DETECTION_ERROR (не dead!)
        return AccountStatus.DETECTION_ERROR, f"DETECTION_ERROR: {str(e)[:100]}"


async def ensure_page_ok(page: Page, username: str) -> tuple[bool, bool]:
    """
    Проверяет, что страница не заблокирована (captcha/login/verify).
    Использует БЫСТРУЮ детекцию для парсинга видео.
    
    Returns:
        tuple[bool, bool]: (has_captcha, login_required)
        - has_captcha: True если обнаружена капча
        - login_required: True если требуется логин
    
    Raises:
        PageBlocked: Если страница заблокирована (для обратной совместимости)
    """
    try:
        # КРИТИЧНО: Сначала быстрая проверка мертвого аккаунта
        account_status, reason = await quick_detect_dead_account_for_parsing(page, username)
        if account_status == AccountStatus.HARD_DEAD:
            logger.warning(f"[VIDEO_PARSER] HARD_DEAD account detected for @{username}: {reason}")
            raise PageBlocked(f"HARD_DEAD account detected: {reason}")
        elif account_status == AccountStatus.SUSPICIOUS:
            logger.warning(f"[VIDEO_PARSER] SUSPICIOUS account detected for @{username}: {reason}")
            raise PageBlocked(f"SUSPICIOUS account detected: {reason}")
        
        # Затем полная проверка через _detect_page_state
        from executor.tiktok_client_async import _detect_page_state
        has_captcha, login_required, _, _, _, _ = await _detect_page_state(page)
        
        if login_required:
            logger.warning(f"[VIDEO_PARSER] Login required detected for @{username}")
            raise PageBlocked(f"Login required for @{username}")
        
        if has_captcha:
            logger.warning(f"[VIDEO_PARSER] CAPTCHA detected for @{username}")
            raise PageBlocked(f"CAPTCHA detected for @{username}")
        
        return has_captcha, login_required
        
    except PageBlocked:
        raise
    except Exception as e:
        logger.debug(f"[VIDEO_PARSER] Error checking page state: {e}")
        # Fallback на старую логику
        url = page.url.lower()
        if any(keyword in url for keyword in ["captcha", "verify", "challenge", "login", "sign-in"]):
            raise PageBlocked(f"Page blocked for @{username}: {url}")
        return False, False


async def wait_for_video_content(page: Page, username: str, timeout: int = 15_000) -> bool:
    """
    Ждет появления видео контента на странице профиля.

    Returns:
        bool: True если контент появился, False если таймаут
    """
    combined = (
        'a[href*="/video/"], '
        'div[data-e2e*="video"], '
        '[class*="VideoItem"], '
        '[class*="video-item"], '
        'div[class*="DivItemContainer"]'
    )
    try:
        await page.wait_for_selector(combined, timeout=timeout, state="attached")
        logger.debug(f"[VIDEO_PARSER] Video content appeared for @{username}")
        return True
    except Exception as e:
        logger.debug(f"[VIDEO_PARSER] wait_for_video_content timed out for @{username}: {e}")
        return False


async def extract_videos_with_metadata(page: Page, username: str) -> List[Tuple[str, str, bool]]:
    """
    Извлекает видео с метаданными (URL, video_id, is_pinned).
    
    Returns:
        List[Tuple[str, str, bool]]: Список кортежей (video_id, url, is_pinned)
        - video_id: ID видео
        - url: URL видео
        - is_pinned: True если видео закреплено
    """
    username_lower = username.lower()
    videos_metadata = []
    
    try:
        # ИСПРАВЛЕНО: Извлекаем видео с определением pinned через JavaScript
        videos_data = await page.evaluate(f"""
            () => {{
                const username = '{username_lower}';
                const videos = [];
                const seen = new Set();
                
                // Ищем все ссылки на видео
                const videoLinks = Array.from(document.querySelectorAll('a[href*="/video/"]'));
                
                for (const link of videoLinks) {{
                    const href = link.href;
                    if (!href || !href.includes('/video/')) continue;
                    
                    // Извлекаем video_id
                    const match = href.match(/\\/video\\/(\\d+)/);
                    if (!match || !match[1]) continue;
                    
                    const videoId = match[1];
                    if (seen.has(videoId)) continue;
                    seen.add(videoId);
                    
                    // P1: Строгий детект pinned - только явные индикаторы
                    // НЕ используем "pin в классе" или позицию - это хрупко (может быть false positive)
                    let isPinned = false;
                    
                    // Проверка 1: Явная иконка pin (самый надежный признак)
                    const container = link.closest('div, section, article, [class*="item"], [class*="Item"]');
                    if (container) {{
                        // Ищем явную иконку pin (SVG или специальный элемент)
                        const pinIcon = container.querySelector(
                            'svg[class*="pin" i], ' +
                            '[class*="PinIcon" i], ' +
                            '[data-e2e*="pin" i], ' +
                            '[aria-label*="pin" i], ' +
                            '[title*="pin" i]'
                        );
                        if (pinIcon) {{
                            // Проверяем, что иконка видима
                            const style = window.getComputedStyle(pinIcon);
                            if (style.display !== 'none' && style.visibility !== 'hidden') {{
                                isPinned = true;
                            }}
                        }}
                        
                        // Проверка 2: Явный лейбл "Pinned" или "Закреплено" (только если нет иконки)
                        if (!isPinned) {{
                            const containerText = (container.innerText || container.textContent || '').toLowerCase();
                            const pinnedLabels = ['pinned', 'закреплено', 'закреплен'];
                            // Ищем лейбл рядом с видео (в пределах контейнера)
                            if (pinnedLabels.some(label => containerText.includes(label))) {{
                                // Дополнительная проверка: лейбл должен быть близко к видео
                                const labelElement = Array.from(container.querySelectorAll('*')).find(el => {{
                                    const elText = (el.innerText || el.textContent || '').toLowerCase();
                                    return pinnedLabels.some(label => elText.includes(label));
                                }});
                                if (labelElement) {{
                                    isPinned = true;
                                }}
                            }}
                        }}
                    }}
                    
                    // P1: НЕ используем позицию в списке как признак pinned
                    // Это эвристика, но не истина - может быть false positive
                    // Если API не дал is_pinned и нет явных индикаторов - считаем normal
                    const linkIndex = Array.from(document.querySelectorAll('a[href*="/video/"]')).indexOf(link);
                    
                    videos.push({{
                        videoId: videoId,
                        url: href,
                        isPinned: isPinned,
                        index: linkIndex
                    }});
                }}
                
                return videos;
            }}
        """)
        
        if isinstance(videos_data, list):
            for video_data in videos_data:
                if isinstance(video_data, dict):
                    video_id = video_data.get('videoId')
                    url = video_data.get('url')
                    is_pinned = video_data.get('isPinned', False)
                    
                    if video_id and url and is_valid_video_url(url, username):
                        videos_metadata.append((video_id, url, is_pinned))
            
            logger.debug(f"[VIDEO_PARSER] Extracted {len(videos_metadata)} videos with metadata for @{username} (pinned: {sum(1 for _, _, p in videos_metadata if p)})")
    
    except Exception as e:
        logger.debug(f"[VIDEO_PARSER] Error extracting videos with metadata: {e}")
    
    return videos_metadata


async def extract_video_links_multiple_methods(page: Page, username: str) -> List[str]:
    """
    Извлекает ссылки на видео используя несколько методов.
    
    Returns:
        List[str]: Список URL видео
    """
    all_hrefs = []
    username_lower = username.lower()

    def _enough_hrefs(hrefs: list, threshold: int = 6) -> bool:
        unique_valid = {h for h in hrefs if is_valid_video_url(h, username)}
        return len(unique_valid) >= threshold

    # Метод 1: Стандартный селектор a[href*="/video/"]
    try:
        hrefs1 = await page.evaluate("""
            () => {
                const links = Array.from(document.querySelectorAll('a[href*="/video/"]'));
                return links.map(a => a.href).filter(href => href && typeof href === 'string');
            }
        """)
        if isinstance(hrefs1, list):
            all_hrefs.extend(hrefs1)
            logger.debug(f"[VIDEO_PARSER] Method 1 (a[href*='/video/']): found {len(hrefs1)} links for @{username}")
            if _enough_hrefs(all_hrefs):
                logger.debug(f"[VIDEO_PARSER] Method 1 sufficient, skipping fallback methods for @{username}")
                # Skip to dedup/filter step
                unique_hrefs = []
                seen = set()
                for href in all_hrefs:
                    if not isinstance(href, str) or not href.strip():
                        continue
                    if not is_valid_video_url(href, username):
                        continue
                    if href not in seen:
                        seen.add(href)
                        unique_hrefs.append(href)
                logger.info(f"[VIDEO_PARSER] Total unique video links found for @{username}: {len(unique_hrefs)} (method 1 only)")
                return unique_hrefs
    except Exception as e:
        logger.debug(f"[VIDEO_PARSER] Method 1 failed for @{username}: {e}")

    # Метод 2: Поиск через data-атрибуты
    try:
        hrefs2 = await page.evaluate(f"""
            () => {{
                const links = [];
                // Ищем элементы с data-e2e содержащим video
                const videoElements = document.querySelectorAll('[data-e2e*="video" i], [data-e2e*="Video"]');
                videoElements.forEach(el => {{
                    // Ищем ближайший родительский <a> или сам элемент если это <a>
                    let linkEl = el.closest('a');
                    if (!linkEl && el.tagName === 'A') linkEl = el;
                    if (linkEl && linkEl.href && linkEl.href.includes('/video/')) {{
                        links.push(linkEl.href);
                    }}
                }});
                return [...new Set(links)];
            }}
        """)
        if isinstance(hrefs2, list):
            all_hrefs.extend(hrefs2)
            logger.debug(f"[VIDEO_PARSER] Method 2 (data-e2e): found {len(hrefs2)} links for @{username}")
    except Exception as e:
        logger.debug(f"[VIDEO_PARSER] Method 2 failed for @{username}: {e}")
    
    # Метод 3: Поиск через классы VideoItem
    try:
        hrefs3 = await page.evaluate(f"""
            () => {{
                const links = [];
                const videoItems = document.querySelectorAll('[class*="VideoItem" i], [class*="video-item" i], [class*="DivItemContainer"]');
                videoItems.forEach(item => {{
                    const linkEl = item.querySelector('a[href*="/video/"]') || item.closest('a[href*="/video/"]');
                    if (linkEl && linkEl.href) {{
                        links.push(linkEl.href);
                    }}
                }});
                return [...new Set(links)];
            }}
        """)
        if isinstance(hrefs3, list):
            all_hrefs.extend(hrefs3)
            logger.debug(f"[VIDEO_PARSER] Method 3 (VideoItem classes): found {len(hrefs3)} links for @{username}")
    except Exception as e:
        logger.debug(f"[VIDEO_PARSER] Method 3 failed for @{username}: {e}")
    
    # Метод 4: Поиск всех ссылок и фильтрация по паттерну
    try:
        hrefs4 = await page.evaluate(f"""
            () => {{
                const allLinks = Array.from(document.querySelectorAll('a'));
                const videoLinks = allLinks
                    .map(a => a.href)
                    .filter(href => href && typeof href === 'string' && href.includes('/video/'))
                    .filter(href => href.includes('@{username_lower}') || href.includes('/@{username_lower}/'));
                return [...new Set(videoLinks)];
            }}
        """)
        if isinstance(hrefs4, list):
            all_hrefs.extend(hrefs4)
            logger.debug(f"[VIDEO_PARSER] Method 4 (all links filter): found {len(hrefs4)} links for @{username}")
    except Exception as e:
        logger.debug(f"[VIDEO_PARSER] Method 4 failed for @{username}: {e}")
    
    # Метод 5: Поиск через JSON данные (SIGI_STATE или другие встроенные данные)
    try:
        # ИСПРАВЛЕНО: Используем правильную подстановку username в JavaScript
        hrefs5 = await page.evaluate(f"""
            () => {{
                const username = '{username_lower}';
                const videoLinks = [];
                // Ищем JSON данные в script тегах
                const scripts = document.querySelectorAll('script[id*="SIGI_STATE"], script[type="application/json"], script:not([src])');
                for (const script of scripts) {{
                    try {{
                        const text = script.textContent || script.innerText || '';
                        if (text.length > 1000) {{ // Только большие скрипты с данными
                            // Ищем video ID в JSON
                            const videoIdMatches = text.match(/"aweme_id":\\s*"(\\d+)"/g);
                            if (videoIdMatches) {{
                                videoIdMatches.forEach(match => {{
                                    const matchResult = match.match(/"aweme_id":\\s*"(\\d+)"/);
                                    if (matchResult && matchResult[1]) {{
                                        videoLinks.push(`https://www.tiktok.com/@${{username}}/video/${{matchResult[1]}}`);
                                    }}
                                }});
                            }}
                            // Также ищем прямые ссылки на видео
                            const urlPattern = new RegExp(`https?:\\\\/\\\\/[^"\\\\s]+\\\\/@${{username}}\\\\/video\\\\/\\\\d+`, 'gi');
                            const urlMatches = text.match(urlPattern);
                            if (urlMatches) {{
                                videoLinks.push(...urlMatches);
                            }}
                        }}
                    }} catch (e) {{
                        // Игнорируем ошибки парсинга JSON
                    }}
                }}
                return [...new Set(videoLinks)];
            }}
        """)
        if isinstance(hrefs5, list):
            all_hrefs.extend(hrefs5)
            logger.debug(f"[VIDEO_PARSER] Method 5 (JSON data): found {len(hrefs5)} links for @{username}")
    except Exception as e:
        logger.debug(f"[VIDEO_PARSER] Method 5 failed for @{username}: {e}")
    
    # Метод 6: Поиск через data-атрибуты и data-video-id
    try:
        # ИСПРАВЛЕНО: Используем правильную подстановку username в JavaScript
        hrefs6 = await page.evaluate(f"""
            () => {{
                const username = '{username_lower}';
                const videoLinks = [];
                // Ищем элементы с data-video-id или похожими атрибутами
                const videoElements = document.querySelectorAll('[data-video-id], [data-aweme-id], [data-e2e*="video"], [data-e2e*="item"]');
                videoElements.forEach(el => {{
                    const videoId = el.getAttribute('data-video-id') || 
                                   el.getAttribute('data-aweme-id') ||
                                   el.getAttribute('data-e2e')?.match(/video[_-]?(\\d+)/i)?.[1];
                    if (videoId && /^\\d+$/.test(videoId)) {{
                        videoLinks.push(`https://www.tiktok.com/@${{username}}/video/${{videoId}}`);
                    }}
                    // Также ищем ближайшую ссылку
                    const linkEl = el.closest('a[href*="/video/"]') || el.querySelector('a[href*="/video/"]');
                    if (linkEl && linkEl.href) {{
                        videoLinks.push(linkEl.href);
                    }}
                }});
                return [...new Set(videoLinks)];
            }}
        """)
        if isinstance(hrefs6, list):
            all_hrefs.extend(hrefs6)
            logger.debug(f"[VIDEO_PARSER] Method 6 (data attributes): found {len(hrefs6)} links for @{username}")
    except Exception as e:
        logger.debug(f"[VIDEO_PARSER] Method 6 failed for @{username}: {e}")
    
    # ИСПРАВЛЕНО: Удаляем дубликаты и фильтруем по username с валидацией
    unique_hrefs = []
    seen = set()
    for href in all_hrefs:
        if not isinstance(href, str) or not href.strip():
            continue
        
        # Валидация URL
        if not is_valid_video_url(href, username):
            continue
        
        if href not in seen:
            seen.add(href)
            unique_hrefs.append(href)
    
    logger.info(f"[VIDEO_PARSER] Total unique video links found for @{username}: {len(unique_hrefs)}")
    return unique_hrefs


async def fetch_latest_videos_from_profile(
    page: Page,
    username: str,
    limit: int = 12,
    stop_at_video_id: Optional[str] = None,
    account: Optional[Account] = None,
    min_video_create_time: Optional[int] = None,
) -> ScanResult:
    """
    Получает список последних видео из профиля блогера.
    
    Args:
        page: Playwright Page объект
        username: username блогера (без @)
        limit: Максимальное количество видео для получения
        stop_at_video_id: Остановиться при достижении этого video_id (оптимизация)
        account: Аккаунт для warm-up (опционально)
        
    Returns:
        List[Tuple[str, str]]: Список кортежей (video_id, url)
        
    Raises:
        PageBlocked: Если страница заблокирована (captcha/login/verify)
        RateLimited: Если TikTok вернул rate limit (HTTP 429)
    """
    profile_url = f"https://www.tiktok.com/@{username}"
    
    try:
        logger.debug(f"[VIDEO_PARSER] Fetching videos from profile: {username} (stop_at={stop_at_video_id})")
        
        # ИСПРАВЛЕНО: Warm-up перед парсингом профиля
        warmup_state = {"performed": False, "mode": "disabled"}
        if account and settings.VIDEO_PARSING_WARMUP_ENABLED:
            try:
                warmup_state = await warmup_before_parsing(page, account)
            except PageBlocked:
                # PageBlocked от warmup (мертвые куки) - пробрасываем дальше для обработки в video_monitor_worker
                raise
            except Exception as warmup_e:
                # Другие ошибки warmup (network, timeout) - логируем и продолжаем
                logger.debug(f"[VIDEO_PARSER] Warmup failed for @{username}: {warmup_e}, continuing anyway")
        
        # P0: API - канонический источник, сохраняем порядок
        username_lower = username.lower()
        video_ids_from_api_ordered: List[Tuple[str, bool, Optional[int]]] = []  # (video_id, is_pinned, create_time) в порядке API
        video_ids_from_api_set: Set[str] = set()  # Для быстрой проверки дубликатов
        api_responses_received: List[str] = []
        api_rate_limited = False
        all_requests = []

        def _build_api_diagnostics(extra: Optional[dict] = None) -> dict:
            base = {
                "api_responses_received": len(api_responses_received),
                "api_extracted_count": len(video_ids_from_api_ordered),
                "api_rate_limited": api_rate_limited,
                "api_requests_detected": len(all_requests),
            }
            if extra:
                base.update(extra)
            return base
        
        async def handle_response(response):
            """Обработчик network responses для извлечения video_id из API ответов."""
            try:
                nonlocal api_rate_limited
                url = response.url.lower()
                # TikTok API endpoints для загрузки видео профиля
                api_endpoints = [
                    "/api/user/detail",
                    "/api/post/item_list",
                    "/share/user",
                    "aweme/v1/web/general/user",
                    "/api/user/post",
                    "aweme/v1/web/general/user/post",
                    "/api/user",
                    "item_list",
                    "post/item"
                ]
                
                # ИСПРАВЛЕНО: Более широкий поиск - ищем любые запросы к TikTok API
                is_tiktok_api = any(endpoint in url for endpoint in api_endpoints) or \
                               ("tiktok.com" in url and ("api" in url or "aweme" in url))
                
                if is_tiktok_api:
                    logger.debug(f"[VIDEO_PARSER] Detected TikTok API request: {url[:100]} (status={response.status})")
                    api_responses_received.append(url[:100])
                    if response.status == 429:
                        api_rate_limited = True
                        logger.warning(f"[VIDEO_PARSER] API rate limited (HTTP 429) for @{username}: {url[:100]}")
                        return
                    if response.status == 200:
                        try:
                            # Пытаемся извлечь video_id из JSON ответа
                            response_json = await response.json()
                            logger.debug(f"[VIDEO_PARSER] Parsing API response from {url[:80]}, type={type(response_json)}")
                            
                            if isinstance(response_json, dict):
                                if _is_tiktok_api_rate_limited_payload(response_json):
                                    api_rate_limited = True
                                    status_code = _coerce_tiktok_status_code(response_json)
                                    status_msg = (
                                        response_json.get("status_msg")
                                        or response_json.get("statusMsg")
                                        or response_json.get("message")
                                        or response_json.get("msg")
                                        or ""
                                    )
                                    logger.warning(
                                        "[VIDEO_PARSER] API rate limited (body status=%s) for @%s: %s",
                                        status_code,
                                        username,
                                        str(status_msg)[:120],
                                    )
                                    return
                                # P0: Извлекаем aweme_id с сохранением порядка и pinned статуса
                                def _extract_author_username(item: dict) -> Optional[str]:
                                    try:
                                        # Try multiple author container keys; TikTok rotates between them.
                                        for author_key in ("author", "authorInfo", "author_info", "userInfo", "user"):
                                            author = item.get(author_key)
                                            if isinstance(author, dict):
                                                for uid_key in ("unique_id", "uniqueId", "uniqueID", "username", "user_unique_id", "userUniqueId"):
                                                    uid = author.get(uid_key)
                                                    if uid:
                                                        return str(uid).lower().lstrip("@")
                                                # Sometimes nested as author.userInfo.uniqueId
                                                nested = author.get("userInfo") or author.get("user")
                                                if isinstance(nested, dict):
                                                    for uid_key in ("unique_id", "uniqueId", "username"):
                                                        uid = nested.get(uid_key)
                                                        if uid:
                                                            return str(uid).lower().lstrip("@")
                                            if isinstance(author, str):
                                                return author.lower().lstrip("@")
                                        # Top-level fallbacks (some endpoints flatten)
                                        for direct_key in ("author_name", "authorName", "unique_id", "uniqueId",
                                                           "username", "nickname", "uniqueID"):
                                            direct = item.get(direct_key)
                                            if direct:
                                                return str(direct).lower().lstrip("@")
                                    except Exception:
                                        return None
                                    return None

                                def _author_matches(item: dict, allow_missing_author: bool) -> bool:
                                    return _author_matches_item(
                                        item,
                                        username_lower,
                                        allow_missing_author=allow_missing_author,
                                    )

                                def _response_matches_username(payload: dict) -> bool:
                                    return _payload_mentions_username(payload, username_lower)

                                allow_missing_author = False
                                if _response_matches_username(response_json):
                                    allow_missing_author = True
                                else:
                                    allow_missing_author = False

                                def _get_aweme_id(item: dict) -> Optional[str]:
                                    return _get_aweme_id_from_item(item)

                                def _get_create_time(item: dict) -> Optional[int]:
                                    return _get_create_time_from_item(item)

                                # Список ключей-обёрток вокруг которых TikTok прячет items
                                # data.itemList / body.itemList / result.itemList / userPost.itemList и т.д.
                                def extract_aweme_ids_ordered(obj, found_list, found_set, depth=0):
                                    """Извлекает aweme_id с сохранением порядка из API ответа."""
                                    if depth > 10:
                                        return
                                    if isinstance(obj, dict):
                                        # Сначала пробуем найти любой из известных ключей-списков на текущем уровне
                                        for list_key in _ITEM_LIST_KEYS:
                                            if list_key not in obj:
                                                continue
                                            items = obj[list_key]
                                            if isinstance(items, dict):
                                                items = [items]
                                            if not isinstance(items, list):
                                                continue
                                            for item in items:
                                                if not isinstance(item, dict):
                                                    continue
                                                if not _author_matches(item, allow_missing_author):
                                                    continue
                                                aweme_id = _get_aweme_id(item)
                                                if not aweme_id:
                                                    continue
                                                is_pinned = _item_is_pinned(item)
                                                create_time = _get_create_time(item)
                                                if aweme_id not in found_set:
                                                    found_list.append((aweme_id, is_pinned, create_time))
                                                    found_set.add(aweme_id)
                                                    logger.debug(f"[VIDEO_PARSER] Found aweme_id in {list_key}: {aweme_id} (pinned: {is_pinned})")

                                        # Legacy explicit branches (оставлены для совместимости диагностических логов)
                                        if "itemList" in obj:
                                            items = obj["itemList"]
                                            if isinstance(items, list):
                                                for item in items:
                                                    if not isinstance(item, dict):
                                                        continue
                                                    if not _author_matches(item, allow_missing_author):
                                                        continue
                                                    aweme_id = _get_aweme_id(item)
                                                    if not aweme_id:
                                                        continue
                                                    is_pinned = _item_is_pinned(item)
                                                    create_time = _get_create_time(item)
                                                    if aweme_id not in found_set:
                                                        found_list.append((aweme_id, is_pinned, create_time))
                                                        found_set.add(aweme_id)
                                                        logger.debug(f"[VIDEO_PARSER] Found aweme_id in itemList: {aweme_id} (pinned: {is_pinned})")
                                        if "item_list" in obj:
                                            items = obj["item_list"]
                                            if isinstance(items, list):
                                                for item in items:
                                                    if not isinstance(item, dict):
                                                        continue
                                                    if not _author_matches(item, allow_missing_author):
                                                        continue
                                                    aweme_id = _get_aweme_id(item)
                                                    if not aweme_id:
                                                        continue
                                                    is_pinned = _item_is_pinned(item)
                                                    create_time = _get_create_time(item)
                                                    if aweme_id not in found_set:
                                                        found_list.append((aweme_id, is_pinned, create_time))
                                                        found_set.add(aweme_id)
                                                        logger.debug(f"[VIDEO_PARSER] Found aweme_id in item_list: {aweme_id} (pinned: {is_pinned})")
                                        if "aweme_list" in obj:
                                            items = obj["aweme_list"]
                                            if isinstance(items, list):
                                                for item in items:
                                                    if not isinstance(item, dict):
                                                        continue
                                                    if not _author_matches(item, allow_missing_author):
                                                        continue
                                                    aweme_id = _get_aweme_id(item)
                                                    if not aweme_id:
                                                        continue
                                                    is_pinned = _item_is_pinned(item)
                                                    create_time = _get_create_time(item)
                                                    if aweme_id not in found_set:
                                                        found_list.append((aweme_id, is_pinned, create_time))
                                                        found_set.add(aweme_id)
                                                        logger.debug(f"[VIDEO_PARSER] Found aweme_id in aweme_list: {aweme_id} (pinned: {is_pinned})")
                                        # Если на этом уровне ничего из известных ключей-списков нет,
                                        # пробуем item-как-объект и рекурсивно спускаемся в детей.
                                        if not any(k in obj for k in _ITEM_LIST_KEYS):
                                            aweme_id = _get_aweme_id(obj)
                                            if aweme_id and _author_matches(obj, allow_missing_author):
                                                is_pinned = _item_is_pinned(obj)
                                                create_time = _get_create_time(obj)
                                                if aweme_id not in found_set:
                                                    found_list.append((aweme_id, is_pinned, create_time))
                                                    found_set.add(aweme_id)
                                                    logger.debug(f"[VIDEO_PARSER] Found aweme_id: {aweme_id} (pinned: {is_pinned})")
                                            for key, value in obj.items():
                                                if key not in ["log_pb", "extra", "status_code", "status_msg"]:
                                                    extract_aweme_ids_ordered(value, found_list, found_set, depth + 1)
                                        else:
                                            # Уже нашли список(и) на этом уровне — спускаемся только в типичные обёртки,
                                            # чтобы не разогнать рекурсию по statistic-полям items.
                                            for wrapper_key in ("data", "body", "result", "userPost", "user_post"):
                                                if wrapper_key in obj:
                                                    extract_aweme_ids_ordered(obj[wrapper_key], found_list, found_set, depth + 1)
                                    elif isinstance(obj, list):
                                        for item in obj:
                                            extract_aweme_ids_ordered(item, found_list, found_set, depth + 1)
                                
                                before_count = len(video_ids_from_api_ordered)
                                extract_aweme_ids_ordered(response_json, video_ids_from_api_ordered, video_ids_from_api_set)
                                added = len(video_ids_from_api_ordered) - before_count
                                if added > 0:
                                    logger.info(f"[VIDEO_PARSER] Extracted {added} new video IDs from API response (ordered)")
                                else:
                                    # Diagnostic: API returned 200 but yielded zero video IDs.
                                    # Log top-level keys + a sample of the first item so we can
                                    # spot wrapper / field changes without re-running with debug logging.
                                    is_post_list = any(ep in url for ep in ("/api/post/item_list", "/api/user/post"))
                                    if is_post_list and isinstance(response_json, dict):
                                        top_keys = list(response_json.keys())[:15]
                                        item_list = response_json.get("itemList") or response_json.get("item_list") or []
                                        if not isinstance(item_list, list):
                                            item_list = []
                                        first_item = item_list[0] if item_list else None
                                        first_item_keys: list = []
                                        first_item_author_keys: list = []
                                        first_item_id_sample: dict = {}
                                        if isinstance(first_item, dict):
                                            first_item_keys = list(first_item.keys())[:25]
                                            for ak in ("author", "authorInfo", "author_info", "userInfo", "user"):
                                                v = first_item.get(ak)
                                                if isinstance(v, dict):
                                                    first_item_author_keys = list(v.keys())[:15]
                                                    break
                                            for ik in ("aweme_id", "awemeId", "id", "video_id", "videoId", "item_id"):
                                                if ik in first_item:
                                                    first_item_id_sample[ik] = str(first_item[ik])[:30]
                                        logger.warning(
                                            "[VIDEO_PARSER] @%s: %s returned 200 but extracted 0 video IDs. "
                                            "top_keys=%s itemList_len=%d first_item_keys=%s "
                                            "first_item_author_keys=%s first_item_id_sample=%s status=%s",
                                            username, url[:80], top_keys, len(item_list),
                                            first_item_keys, first_item_author_keys, first_item_id_sample,
                                            response_json.get("status_code") or response_json.get("statusCode"),
                                        )
                        except Exception as json_e:
                            logger.debug(f"[VIDEO_PARSER] Could not parse API response JSON from {url[:80]}: {json_e}")
            except Exception as e:
                logger.debug(f"[VIDEO_PARSER] Error handling response: {e}")
        
        # ИСПРАВЛЕНО: Подписываемся на network responses ДО открытия страницы
        # Также подписываемся на requests для логирования
        async def handle_request(request):
            """Логируем все requests для диагностики."""
            try:
                url = request.url.lower()
                if "tiktok.com" in url and ("api" in url or "aweme" in url):
                    all_requests.append(url[:150])
                    logger.debug(f"[VIDEO_PARSER] TikTok API request detected: {url[:100]}")
            except Exception:
                pass

        page.on("request", handle_request)
        page.on("response", handle_response)
        
        try:
            # Открываем страницу профиля
            logger.debug(f"[VIDEO_PARSER] Opening profile page: {profile_url}")
            response = await page.goto(profile_url, wait_until="domcontentloaded", timeout=60_000)
            
            # P0: URL Redirect проверка - быстрая детекция мертвых куки
            final_url = page.url.lower()
            if any(redirect_url in final_url for redirect_url in ['/login', '/signup', '/sign-in', '/verify', '/auth']):
                logger.warning(f"[VIDEO_PARSER] Redirect to login detected for @{username}: {final_url[:100]}")
                raise PageBlocked(f"Redirect to login detected: {final_url[:100]}")
            
            # Проверяем HTTP статус
            if response and response.status >= 400:
                if response.status == 404:
                    logger.warning(f"[VIDEO_PARSER] Profile @{username} not found (404)")
                    return ScanResult(
                        status="EMPTY",
                        new_videos=[],
                        newest_normal_video_id=None,
                        anchor_found=False,
                        canonical_source="DOM",
                        pages_loaded=1,
                        diagnostics=_build_api_diagnostics({"reason": "profile_not_found"})
                    )
                elif response.status == 429:
                    logger.warning(f"[VIDEO_PARSER] Rate limited for @{username} (HTTP 429)")
                    raise RateLimited(f"Rate limited for @{username}")
                elif response.status >= 500:
                    logger.warning(f"[VIDEO_PARSER] Server error for @{username}: {response.status}")
                    raise PageBlocked(f"Server error for @{username}: HTTP {response.status}")
        
            # КРИТИЧНО: Минимальная задержка для загрузки базового DOM (1-2 секунды)
            await asyncio.sleep(random.uniform(1.0, 2.0))
            
            # КРИТИЧНО: БЫСТРАЯ проверка мертвого аккаунта СРАЗУ после загрузки
            account_status, reason = await quick_detect_dead_account_for_parsing(page, username)
            if account_status == AccountStatus.HARD_DEAD:
                logger.warning(f"[VIDEO_PARSER] HARD_DEAD account detected IMMEDIATELY for @{username}: {reason}")
                raise PageBlocked(f"HARD_DEAD account detected: {reason}")
            elif account_status == AccountStatus.SUSPICIOUS:
                logger.warning(f"[VIDEO_PARSER] SUSPICIOUS account detected IMMEDIATELY for @{username}: {reason}")
                raise PageBlocked(f"SUSPICIOUS account detected: {reason}")
            
            # ИСПРАВЛЕНО: Случайная задержка после проверки (меньше, т.к. уже проверили)
            if warmup_state.get("mode") == "light":
                delay = random.uniform(0.2, 0.8)
            else:
                delay = random.uniform(
                    settings.VIDEO_PARSING_INITIAL_LOAD_DELAY_MIN_MS / 1000,
                    settings.VIDEO_PARSING_INITIAL_LOAD_DELAY_MAX_MS / 1000
                )
            await asyncio.sleep(delay)
            
            # ИСПРАВЛЕНО: Ждем загрузки данных через API (увеличено время ожидания)
            try:
                # Ждем появления video_ids в API ответах
                logger.debug(f"[VIDEO_PARSER] Waiting for API responses for @{username}...")
                previous_count = 0
                stagnant_polls = 0
                for i in range(30):  # Проверяем каждые 0.5 секунды в течение 15 секунд
                    await asyncio.sleep(0.5)
                    current_count = len(video_ids_from_api_ordered)
                    if current_count == previous_count:
                        stagnant_polls += 1
                    else:
                        stagnant_polls = 0
                        previous_count = current_count

                    if _has_sufficient_api_ids(video_ids_from_api_ordered, limit, stop_at_video_id):
                        logger.info(
                            f"[VIDEO_PARSER] Collected sufficient API data for @{username}: "
                            f"{len(video_ids_from_api_ordered)} video IDs"
                        )
                        break
                    # Early stagnant exit only when we have at least MIN_STAGNANT_COUNT videos
                    # AND data hasn't changed for 3 polls (1.5s). Prevents exiting too early
                    # when TikTok sends videos in multiple API responses with small gaps.
                    min_stagnant_count = max(2, int(getattr(settings, "VIDEO_PARSER_EARLY_API_TARGET", 6)) // 2)
                    if current_count >= min_stagnant_count and stagnant_polls >= 3:
                        logger.debug(
                            f"[VIDEO_PARSER] API data stabilized early for @{username}: "
                            f"{current_count} video IDs, stopping wait loop"
                        )
                        break
                    if i == 10:  # После 5 секунд логируем промежуточный статус
                        logger.debug(f"[VIDEO_PARSER] Still waiting for API data... (received {len(api_responses_received)} API responses so far)")
                
                if api_responses_received:
                    logger.info(f"[VIDEO_PARSER] Received {len(api_responses_received)} API responses: {api_responses_received[:5]}")
                else:
                    logger.warning(f"[VIDEO_PARSER] No TikTok API responses detected for @{username}")
                    if all_requests:
                        logger.warning(f"[VIDEO_PARSER] But detected {len(all_requests)} TikTok API requests: {all_requests[:5]}")
                    else:
                        logger.warning(f"[VIDEO_PARSER] No TikTok API requests detected at all for @{username}")
                if api_rate_limited:
                    raise RateLimited(f"Rate limited for @{username} (TikTok API throttle)")
            except RateLimited:
                raise
            except Exception as wait_e:
                logger.debug(f"[VIDEO_PARSER] Error waiting for API data: {wait_e}")
        finally:
            # P1.5: Обязательно отписываемся от network responses (защита от утечек памяти)
            try:
                page.remove_listener("response", handle_response)
                page.remove_listener("request", handle_request)
            except Exception as cleanup_e:
                logger.debug(f"[VIDEO_PARSER] Error removing listeners: {cleanup_e}")
        
        # Проверяем, что страница не заблокирована
        await ensure_page_ok(page, username)
        
        # ИСПРАВЛЕНО: Имитация человеческого взаимодействия
        await simulate_human_interaction(page, username, light=warmup_state.get("mode") == "light")
        
        # P0: API - канонический источник, используем его как основной
        canonical_videos: List[Tuple[str, str, bool, Optional[int]]] = []  # (video_id, url, is_pinned, create_time)
        canonical_source_used: Literal["API", "DOM", "JSON"] = "API" if video_ids_from_api_ordered else "DOM"

        # P1: ОПТИМИЗАЦИЯ: Если API дал данные - минимальный скролл (только для маскировки)
        # Если API не дал данные - полный скролл для загрузки ленивого контента
        if video_ids_from_api_ordered:
            # API - канонический источник, используем его порядок (БЫСТРЫЙ ПУТЬ)
            logger.info(f"[VIDEO_PARSER] Using API as canonical source: {len(video_ids_from_api_ordered)} videos (fast path)")
            for video_id, is_pinned, create_time in video_ids_from_api_ordered:
                api_url = f"https://www.tiktok.com/@{username_lower}/video/{video_id}"
                if is_valid_video_url(api_url, username):
                    canonical_videos.append((video_id, api_url, is_pinned, create_time))
            api_canonical_count = len(canonical_videos)
            suspicious_api_fallback_added = 0

            # Optional DOM check for pinned in top items (anchor safety)
            if stop_at_video_id:
                try:
                    dom_meta = await extract_videos_with_metadata(page, username)
                    if dom_meta:
                        dom_pinned = {vid for vid, _, pinned in dom_meta if pinned}
                        if dom_pinned:
                            updated = []
                            for idx, (vid, url, pinned, ctime) in enumerate(canonical_videos):
                                if idx < PINNED_DOM_VERIFY_LIMIT and vid in dom_pinned:
                                    pinned = True
                                updated.append((vid, url, pinned, ctime))
                            canonical_videos = updated
                except Exception as e:
                    logger.debug(f"[VIDEO_PARSER] DOM pinned check failed for @{username}: {e}")
            
            # P1: УЛУЧШЕНО: Более реалистичный скролл даже при API (маскировка)
            # Даже если API дал данные, нужно имитировать просмотр профиля
            if settings.HUMAN_LIKE_BEHAVIOR:
                try:
                    from executor.human_behavior import scroll_wheel_natural, simulate_reading, random_mouse_movement
                    # Имитируем просмотр профиля: скроллим с настраиваемым количеством
                    scrolls = random.randint(
                        settings.VIDEO_PARSING_API_SCROLL_MIN,
                        settings.VIDEO_PARSING_API_SCROLL_MAX
                    )
                    for i in range(scrolls):
                        scroll_amount = random.randint(
                            settings.VIDEO_PARSING_SCROLL_AMOUNT_MIN // 2,  # Меньше чем при DOM парсинге
                            settings.VIDEO_PARSING_SCROLL_AMOUNT_MAX // 2
                        )
                        await scroll_wheel_natural(page, scroll_amount)
                        delay = random.uniform(
                            settings.VIDEO_PARSING_SCROLL_DELAY_MIN_MS / 1000,
                            settings.VIDEO_PARSING_SCROLL_DELAY_MAX_MS / 1000
                        )
                        await asyncio.sleep(delay)
                        
                        # Иногда имитируем чтение (40% шанс)
                        if random.random() < 0.4:
                            read_time = random.randint(800, 2000)
                            await simulate_reading(page, duration_ms=read_time)
                        
                        # Иногда движение мыши (30% шанс)
                        if settings.RANDOM_MOUSE_MOVEMENTS and random.random() < 0.3:
                            await random_mouse_movement(page, min_moves=1, max_moves=1)
                except Exception as scroll_e:
                    logger.debug(f"[VIDEO_PARSER] API path scroll failed: {scroll_e}")
                    # Fallback на простой скролл
                    try:
                        await page.evaluate(f"window.scrollBy(0, {random.randint(300, 600)})")
                        await asyncio.sleep(random.uniform(1.0, 2.0))
                    except Exception:
                        pass

            suspicious_min_videos = int(getattr(settings, "MONITOR_API_SUSPICIOUS_MIN_VIDEOS", 3))
            if (
                getattr(settings, "MONITOR_API_SUSPICIOUS_FALLBACK_ENABLED", True)
                and stop_at_video_id
                and 0 < api_canonical_count < suspicious_min_videos
            ):
                logger.warning(
                    f"[VIDEO_PARSER] Suspiciously small API result for @{username}: "
                    f"{api_canonical_count} videos; probing DOM/JSON fallback before accepting"
                )
                existing_ids = {item[0] for item in canonical_videos}
                fallback_candidates: list[tuple[str, str, bool]] = []
                try:
                    dom_meta = await extract_videos_with_metadata(page, username)
                    fallback_candidates.extend(dom_meta or [])
                except Exception as e:
                    logger.debug(f"[VIDEO_PARSER] Suspicious API DOM metadata fallback failed for @{username}: {e}")
                try:
                    hrefs = await extract_video_links_multiple_methods(page, username)
                    for href in hrefs:
                        video_id = extract_video_id(href)
                        if video_id:
                            fallback_candidates.append((video_id, href, False))
                except Exception as e:
                    logger.debug(f"[VIDEO_PARSER] Suspicious API href fallback failed for @{username}: {e}")
                try:
                    username_js = username.lower()
                    json_videos = await page.evaluate(f"""
                        () => {{
                            const username = '{username_js}';
                            const videoIds = new Set();
                            const scripts = Array.from(document.querySelectorAll('script'));
                            for (const script of scripts) {{
                                const text = script.textContent || script.innerText || '';
                                if (text.length <= 500) continue;
                                const awemeMatches = text.match(/"aweme_id"\\s*:\\s*"(\\d+)"/g);
                                if (awemeMatches) {{
                                    awemeMatches.forEach(m => {{
                                        const match = m.match(/"aweme_id"\\s*:\\s*"(\\d+)"/);
                                        if (match && match[1]) videoIds.add(match[1]);
                                    }});
                                }}
                                const urlPattern = new RegExp(`https?:\\\\/\\\\/[^"\\\\s]+\\\\/@${{username}}\\\\/video\\\\/(\\\\d+)`, 'gi');
                                const urlMatches = text.match(urlPattern);
                                if (urlMatches) {{
                                    urlMatches.forEach(url => {{
                                        const match = url.match(/\\/video\\/(\\d+)/i);
                                        if (match && match[1]) videoIds.add(match[1]);
                                    }});
                                }}
                            }}
                            return Array.from(videoIds).map(id => `https://www.tiktok.com/@${{username}}/video/${{id}}`);
                        }}
                    """)
                    if isinstance(json_videos, list):
                        for href in json_videos:
                            video_id = extract_video_id(href)
                            if video_id:
                                fallback_candidates.append((video_id, href, False))
                except Exception as e:
                    logger.debug(f"[VIDEO_PARSER] Suspicious API JSON fallback failed for @{username}: {e}")

                for video_id, url, is_pinned in fallback_candidates:
                    if video_id in existing_ids or not is_valid_video_url(url, username):
                        continue
                    canonical_videos.append((video_id, url, is_pinned, None))
                    existing_ids.add(video_id)
                    suspicious_api_fallback_added += 1
                if suspicious_api_fallback_added:
                    logger.info(
                        f"[VIDEO_PARSER] Suspicious API fallback added {suspicious_api_fallback_added} "
                        f"videos for @{username} (total={len(canonical_videos)})"
                    )
        else:
            # Fallback: DOM/JSON методы (только если API не сработал) - МЕДЛЕННЫЙ ПУТЬ
            logger.warning(f"[VIDEO_PARSER] API not available, using DOM fallback for @{username} (slow path)")
            api_canonical_count = 0
            suspicious_api_fallback_added = 0
            
            # P1: Ждем появления видео контента перед скроллингом (только для DOM)
            video_content_loaded = await wait_for_video_content(page, username, timeout=20_000)
            if not video_content_loaded:
                logger.warning(f"[VIDEO_PARSER] Video content not loaded immediately for @{username}, continuing anyway...")
            
            # P1: Полный скроллинг для загрузки ленивого контента (только для DOM)
            videos_found_after_scroll = await natural_scroll_for_videos(page, username)
            logger.debug(f"[VIDEO_PARSER] Found {videos_found_after_scroll} video links after scrolling for @{username}")
            
            hrefs = await extract_video_links_multiple_methods(page, username)
            # Извлекаем видео с метаданными из DOM
            videos_metadata = await extract_videos_with_metadata(page, username)
            for video_id, url, is_pinned in videos_metadata:
                if is_valid_video_url(url, username):
                    canonical_videos.append((video_id, url, is_pinned, None))

            # Fallback: use hrefs if metadata extraction failed
            if not canonical_videos and hrefs:
                for href in hrefs:
                    video_id = extract_video_id(href)
                    if video_id and is_valid_video_url(href, username):
                        canonical_videos.append((video_id, href, False, None))
        
        if not canonical_videos:
            # Fallback: Пробуем найти видео через JSON данные страницы (последняя попытка)
            try:
                username_js = username.lower()
                json_videos = await page.evaluate(f"""
                    () => {{
                        const username = '{username_js}';
                        const videoIds = new Set();
                        // Ищем в script тегах с JSON данными
                        const scripts = Array.from(document.querySelectorAll('script'));
                        for (const script of scripts) {{
                            const text = script.textContent || script.innerText || '';
                            if (text.length > 500) {{
                                // Ищем aweme_id в JSON
                                const awemeMatches = text.match(/"aweme_id"\\s*:\\s*"(\\d+)"/g);
                                if (awemeMatches) {{
                                    awemeMatches.forEach(m => {{
                                        const match = m.match(/"aweme_id"\\s*:\\s*"(\\d+)"/);
                                        if (match && match[1]) videoIds.add(match[1]);
                                    }});
                                }}
                                // Ищем прямые ссылки
                                const urlPattern = new RegExp(`https?:\\\\/\\\\/[^"\\\\s]+\\\\/@${{username}}\\\\/video\\\\/(\\\\d+)`, 'gi');
                                const urlMatches = text.match(urlPattern);
                                if (urlMatches) {{
                                    urlMatches.forEach(url => {{
                                        const match = url.match(/\\/video\\/(\\d+)/i);
                                        if (match && match[1]) videoIds.add(match[1]);
                                    }});
                                }}
                            }}
                        }}
                        return Array.from(videoIds).map(id => `https://www.tiktok.com/@${{username}}/video/${{id}}`);
                    }}
                """)
                if isinstance(json_videos, list) and json_videos:
                    logger.info(f"[VIDEO_PARSER] Found {len(json_videos)} videos via JSON data for @{username}")
                    # ИСПРАВЛЕНО: Преобразуем hrefs в canonical_videos формат
                    added = 0
                    for href in json_videos:
                        video_id = extract_video_id(href)
                        if video_id and is_valid_video_url(href, username):
                            canonical_videos.append((video_id, href, False, None))  # JSON не дает is_pinned
                            added += 1
                    if added > 0:
                        canonical_source_used = "JSON"
            except Exception as json_e:
                logger.debug(f"[VIDEO_PARSER] JSON extraction failed for @{username}: {json_e}")
        
        # P0: Если после всех попыток видео не найдены - возвращаем EMPTY
        if not canonical_videos:
            # Дополнительная диагностика и сохранение HTML
            try:
                collect_full_diag = _should_collect_full_empty_diagnostics(account, username)
                page_content_length = len(await page.content()) if collect_full_diag else 0
                all_links_count = await page.evaluate("() => document.querySelectorAll('a').length")
                page_text = await page.evaluate("() => document.body.innerText || ''")
                has_username = username.lower() in page_text.lower()
                
                # ИСПРАВЛЕНО: Сохраняем HTML для анализа
                if collect_full_diag and _should_save_empty_diagnostic(username):
                    try:
                        from pathlib import Path
                        import datetime

                        diagnostics_dir = Path("diagnostics")
                        diagnostics_dir.mkdir(exist_ok=True)
                        timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
                        html_file = diagnostics_dir / f"video_parser_{username}_{timestamp}.html"
                        html_content = await page.content()
                        html_file.write_text(html_content, encoding='utf-8')
                        logger.warning(f"[VIDEO_PARSER] Saved HTML for analysis: {html_file}")
                    except Exception as save_e:
                        logger.debug(f"[VIDEO_PARSER] Could not save HTML: {save_e}")
                
                # Дополнительная диагностика элементов
                try:
                    if collect_full_diag:
                        video_elements = await page.evaluate("""
                            () => {
                                return {
                                    video_tags: document.querySelectorAll('video').length,
                                    video_data_attrs: document.querySelectorAll('[data-e2e*="video" i]').length,
                                    video_classes: document.querySelectorAll('[class*="video" i], [class*="Video"]').length,
                                    scripts_with_data: document.querySelectorAll('script[id*="SIGI"], script[type="application/json"]').length
                                };
                            }
                        """)
                        logger.warning(
                            f"[VIDEO_PARSER] No video links found for @{username}. "
                            f"Diagnostics: content_length={page_content_length}, "
                            f"all_links={all_links_count}, "
                            f"has_username_in_text={has_username}, "
                            f"video_elements={video_elements}"
                        )
                    else:
                        logger.info(
                            f"[VIDEO_PARSER] No video links found for @{username} (light diagnostics). "
                            f"all_links={all_links_count}, has_username_in_text={has_username}"
                        )
                except Exception:
                    logger.warning(
                        f"[VIDEO_PARSER] No video links found for @{username}. "
                        f"Diagnostics: content_length={page_content_length}, "
                        f"all_links={all_links_count}, "
                        f"has_username_in_text={has_username}"
                    )
            except Exception as diag_e:
                logger.error(f"[VIDEO_PARSER] Diagnostic failed for @{username}: {diag_e}")
            
            # P1.5: Улучшенная диагностика EMPTY - различаем "пустой профиль" vs "не загрузилось"
            diagnostics_info = {
                "no_videos_found": True,
                "page_content_length": page_content_length if 'page_content_length' in locals() else 0,
                "all_links_count": all_links_count if 'all_links_count' in locals() else 0,
                "has_username_in_text": has_username if 'has_username' in locals() else False
            }
            
            # Пытаемся определить причину
            try:
                page_text = await page.evaluate("() => document.body.innerText || ''")
                page_text_lower = page_text.lower()
                
                if any(x in page_text_lower for x in ['private', 'приватн', 'this account is private']):
                    diagnostics_info["reason"] = "private_profile"
                elif any(x in page_text_lower for x in ['no videos', 'no posts', 'нет видео', 'нет постов']):
                    diagnostics_info["reason"] = "empty_profile"
                elif page_content_length < 1000:
                    diagnostics_info["reason"] = "page_not_loaded"
                else:
                    diagnostics_info["reason"] = "unknown"
            except Exception:
                diagnostics_info["reason"] = "unknown"
            
            return ScanResult(
                status="EMPTY",
                new_videos=[],
                newest_normal_video_id=None,
                anchor_found=False,
                canonical_source="DOM",
                pages_loaded=1,
                diagnostics=_build_api_diagnostics(diagnostics_info)
            )
        
        # P0: Anchor stop логика - идем сверху вниз, останавливаемся на anchor
        # P0: Исключаем pinned из "новых" и из якоря
                # P3: New Pinned Videos Rescue.
        # Если видео закрепили СРАЗУ после публикации, мы не должны его скипать.
        # Если create_time < 24 часов назад, притворяемся что оно не pinned,
        # чтобы оно попало в Inbox и стало новым anchor.
        _canonical_updated = []
        for _vid, _url, _pinned, _ctime in canonical_videos:
            if _pinned and _ctime is not None and (time.time() - _ctime < 86400):
                logger.info(f'[VIDEO_PARSER] Rescuing NEW pinned video: {_vid} (age < 24h, treating as normal)')
                _canonical_updated.append((_vid, _url, False, _ctime))
            else:
                _canonical_updated.append((_vid, _url, _pinned, _ctime))
        canonical_videos = _canonical_updated

        out: List[Tuple[str, str]] = []  # (video_id, url) - только normal видео
        anchor_found = False
        anchor_pinned_detected = False
        stale_skipped = 0
        anchor_numeric = None
        if stop_at_video_id and str(stop_at_video_id).isdigit():
            anchor_numeric = int(stop_at_video_id)
        
        if stop_at_video_id:
            logger.debug(f"[VIDEO_PARSER] @{username}: Using anchor stop logic (anchor={stop_at_video_id})")
        else:
            logger.debug(f"[VIDEO_PARSER] @{username}: No anchor, will return top {limit} normal videos")
        
        # REFACTOR: Исправлен баг с закрепами - anchor-stop НЕ должен срабатывать на pinned видео
        # Если anchor стал pinned - игнорируем его и продолжаем скан ниже
        for idx, (video_id, url, is_pinned, create_time) in enumerate(canonical_videos):
            # REFACTOR: Safety check - если anchor в топ-3, возможно это закреп
            # Не делаем break сразу, проверяем сначала is_pinned
            if stop_at_video_id and video_id == stop_at_video_id:
                if is_pinned:
                    # REFACTOR: Anchor оказался pinned - игнорируем его, продолжаем скан
                    logger.warning(
                        f"[VIDEO_PARSER] Anchor {video_id} is pinned! Ignoring anchor-stop, "
                        f"continuing scan for new videos below pinned content"
                    )
                    # Помечаем, что якорь закреплен; не делаем break, продолжаем искать новые видео ниже
                    anchor_found = True
                    anchor_pinned_detected = True
                else:
                    # Anchor найден и он normal - останавливаемся
                    anchor_found = True
                    logger.info(f"[VIDEO_PARSER] Anchor found (normal): {video_id}, stopping")
                    break
            
            # REFACTOR: Дополнительная защита - если anchor в топ-3 и не pinned, но мы его еще не встретили
            # Возможно это закреп который не детектился. Не делаем break на первых 3 элементах.
            if stop_at_video_id and idx < 3 and video_id == stop_at_video_id and not is_pinned:
                # Anchor в топ-3 - подозрительно, но если он точно не pinned - останавливаемся
                anchor_found = True
                logger.info(f"[VIDEO_PARSER] Anchor found in top-3 (normal): {video_id}, stopping")
                break
            
            # Пропускаем pinned видео - они не считаются "новыми"
            if is_pinned:
                logger.debug(f"[VIDEO_PARSER] Skipping pinned video: {video_id}")
                continue

            # Фильтр по create_time: дропаем видео старше cutoff (защита от ребалансировки ленты TikTok,
            # когда старое видео всплывает выше anchor). Применяется только если API дал create_time.
            if min_video_create_time is not None and create_time is not None and create_time < min_video_create_time:
                age_min = int((min_video_create_time - create_time) / 60)
                logger.info(
                    f"[VIDEO_PARSER] Skipping stale video {video_id} for @{username}: "
                    f"create_time={create_time} cutoff={min_video_create_time} (older by {age_min}m)"
                )
                stale_skipped += 1
                continue

            # CRITICAL FIX: TikTok API возвращает видео НЕ в строгом хронологическом порядке —
            # перед anchor могут идти старые видео из карусели «pinned/featured/recommended».
            # Доверять позиции в списке нельзя — единственный надёжный признак «новее» это ID.
            # TikTok aweme_id монотонно растёт со временем, поэтому строго:  int(id) > anchor.
            if stop_at_video_id:
                if anchor_numeric is not None and video_id.isdigit():
                    if int(video_id) > anchor_numeric:
                        out.append((video_id, url))
                    # else: видео старее anchor — НЕ добавляем, даже если оно идёт перед anchor в списке
                else:
                    # Anchor нечисловой (легаси) или video_id нечисловой — fallback на старую логику.
                    out.append((video_id, url))
            else:
                # Anchor не задан — просто берём первые N normal видео в порядке API.
                out.append((video_id, url))

            if len(out) >= limit:
                break
        
        # P1: Определяем источник данных
        canonical_source: Literal["API", "DOM", "JSON"] = canonical_source_used
        
        # P1: Определяем самое свежее normal видео (для RESYNC и initial sync).
        # CRITICAL FIX: API не гарантирует хронологический порядок — берём максимум по числовому ID.
        # ID TikTok монотонно растут во времени → max(ID) = самое свежее видео.
        newest_normal_video_id: Optional[str] = None
        max_numeric_id = -1
        for vid_id, _, is_pinned, _ in canonical_videos:
            if is_pinned:
                continue
            if not vid_id or not vid_id.isdigit():
                continue
            num = int(vid_id)
            if num > max_numeric_id:
                max_numeric_id = num
                newest_normal_video_id = vid_id
        # Fallback на старое поведение если ни один ID не числовой
        if newest_normal_video_id is None and canonical_videos:
            for vid_id, _, is_pinned, _ in canonical_videos:
                if not is_pinned:
                    newest_normal_video_id = vid_id
                    break
        
        # REFACTOR: RESYNC режим - если anchor не найден, отправляем top-N видео в Inbox
        if stop_at_video_id and not anchor_found:
            if newest_normal_video_id and not out:
                logger.warning(
                    f"[VIDEO_PARSER] Anchor {stop_at_video_id} missing for @{username}, "
                    f"but no newer normal videos were confirmed. Keeping old anchor."
                )
                _missing_anchor_create_times: List[int] = []
                _missing_anchor_create_times_map: dict = {}
                if canonical_source == "API":
                    for _vid_id, _url, _is_pinned, _ctime in canonical_videos:
                        if not _is_pinned and _ctime is not None:
                            _missing_anchor_create_times.append(int(_ctime))
                            _missing_anchor_create_times_map[_vid_id] = int(_ctime)
                    _missing_anchor_create_times.sort(reverse=True)
                return ScanResult(
                    status="OK",
                    new_videos=[],
                    newest_normal_video_id=newest_normal_video_id,
                    anchor_found=False,
                    canonical_source=canonical_source,
                    pages_loaded=1,
                    diagnostics=_build_api_diagnostics({
                        "anchor_missing": True,
                        "videos_found": 0,
                        "newest_normal": newest_normal_video_id,
                        "reason": "anchor_not_found_no_newer_videos_keep_anchor",
                        "canonical_videos_count": len(canonical_videos),
                        "api_video_count": api_canonical_count,
                        "api_suspicious_fallback_added": suspicious_api_fallback_added,
                        "api_response_count": len(api_responses_received),
                        "stale_skipped": stale_skipped,
                    }),
                    video_create_times=_missing_anchor_create_times,
                    video_create_times_map=_missing_anchor_create_times_map,
                )
            # REFACTOR: Если anchor не найден, но есть normal видео - отправляем их в Inbox
            if newest_normal_video_id:
                # Anchor не найден (слишком много новых видео или anchor стал pinned)
                # Отправляем top-N видео в Inbox и обновляем anchor на newest_normal
                logger.warning(
                    f"[VIDEO_PARSER] RESYNC for @{username}: anchor {stop_at_video_id} not found. "
                    f"Sending top {len(out)} videos to Inbox, updating anchor to {newest_normal_video_id}"
                )
                _resync_create_times: List[int] = []
                _resync_create_times_map: dict = {}
                if canonical_source == "API":
                    for _vid_id, _url, _is_pinned, _ctime in canonical_videos:
                        if not _is_pinned and _ctime is not None:
                            _resync_create_times.append(int(_ctime))
                            _resync_create_times_map[_vid_id] = int(_ctime)
                    _resync_create_times.sort(reverse=True)
                return ScanResult(
                    status="RESYNC",
                    new_videos=out[:limit] if out else [],  # REFACTOR: Отправляем видео в Inbox
                    newest_normal_video_id=newest_normal_video_id,  # Воркер обновит anchor
                    anchor_found=False,
                    canonical_source=canonical_source,
                    pages_loaded=1,
                    diagnostics=_build_api_diagnostics({
                        "anchor_missing": True,
                        "videos_found": len(out),
                        "newest_normal": newest_normal_video_id,
                        "reason": "anchor_not_found_sending_top_n",
                        "canonical_videos_count": len(canonical_videos),
                        "api_video_count": api_canonical_count,
                        "api_suspicious_fallback_added": suspicious_api_fallback_added,
                        "api_response_count": len(api_responses_received),
                        "stale_skipped": stale_skipped,
                    }),
                    video_create_times=_resync_create_times,
                    video_create_times_map=_resync_create_times_map,
                )
            else:
                # Anchor не найден и нет normal видео - только pinned или пустой профиль
                logger.warning(
                    f"[VIDEO_PARSER] No normal videos found for @{username}: anchor {stop_at_video_id} not found, "
                    f"only pinned or empty profile"
                )
                return ScanResult(
                    status="NO_NORMAL_VIDEOS",
                    new_videos=[],
                    newest_normal_video_id=None,  # Нет normal видео - anchor не обновляем
                    anchor_found=False,
                    canonical_source=canonical_source,
                    pages_loaded=1,
                    diagnostics=_build_api_diagnostics({
                        "anchor_missing": True,
                        "videos_found": len(out),
                        "reason": "only_pinned_or_empty",
                        "canonical_videos_count": len(canonical_videos),
                        "api_video_count": api_canonical_count,
                        "api_suspicious_fallback_added": suspicious_api_fallback_added,
                        "api_response_count": len(api_responses_received),
                    })
                )
        
        # Если anchor найден или не задан - возвращаем новые видео
        logger.info(f"[VIDEO_PARSER] Found {len(out)} new normal videos for @{username} (anchor_found={anchor_found}, newest={newest_normal_video_id})")
        
        # УЛУЧШЕНО: Имитация "возврата назад" после просмотра профиля (маскировка)
        exit_chance = getattr(settings, "VIDEO_PARSING_EXIT_BEHAVIOR_CHANCE", 0.4)
        if settings.HUMAN_LIKE_BEHAVIOR and random.random() < exit_chance:
            try:
                from executor.human_behavior import scroll_wheel_natural, random_mouse_movement
                # Небольшой скролл вверх (как будто пересматриваешь начало)
                await scroll_wheel_natural(page, -random.randint(200, 400))
                await asyncio.sleep(random.uniform(0.5, 1.2))
                # Случайное движение мыши
                if settings.RANDOM_MOUSE_MOVEMENTS:
                    await random_mouse_movement(page, min_moves=1, max_moves=1)
                await asyncio.sleep(random.uniform(0.3, 0.8))
            except Exception as exit_e:
                logger.debug(f"[VIDEO_PARSER] Exit behavior simulation failed: {exit_e}")
        
        _ok_create_times: List[int] = []
        _ok_create_times_map: dict = {}
        if canonical_source == "API":
            for _vid_id, _url, _is_pinned, _ctime in canonical_videos:
                if not _is_pinned and _ctime is not None:
                    _ok_create_times.append(int(_ctime))
                    _ok_create_times_map[_vid_id] = int(_ctime)
            _ok_create_times.sort(reverse=True)
        return ScanResult(
            status="OK",
            new_videos=out,
            newest_normal_video_id=newest_normal_video_id,  # Из canonical_videos, не из new_videos
            anchor_found=anchor_found,
            canonical_source=canonical_source,
            pages_loaded=1,
            diagnostics=_build_api_diagnostics({
                "videos_found": len(out),
                "pinned_excluded": True,
                "anchor_pinned_detected": anchor_pinned_detected,
                "canonical_videos_count": len(canonical_videos),
                "api_video_count": api_canonical_count,
                "api_suspicious_fallback_added": suspicious_api_fallback_added,
                "api_response_count": len(api_responses_received),
                "stale_skipped": stale_skipped,
            }),
            video_create_times=_ok_create_times,
            video_create_times_map=_ok_create_times_map,
        )
        
    except PageBlocked:
        raise
    except RateLimited:
        raise
    except Exception as e:
        # P1.5: Классифицируем сетевые ошибки Playwright
        error_str = str(e).lower()
        
        # Сетевые ошибки - временные проблемы (не dead, не блокировка)
        network_errors = [
            "err_http_response_code_failure",
            "net::err_",
            "connection",
            "timeout",
            "network",
            "econnrefused",
            "enotfound",
            "econnreset"
        ]
        
        is_network_error = any(net_err in error_str for net_err in network_errors)
        
        if is_network_error:
            # Сетевая ошибка - возвращаем FAIL статус с диагностикой
            logger.warning(f"[VIDEO_PARSER] Network error for @{username}: {e}")
            return ScanResult(
                status="FAIL",
                new_videos=[],
                newest_normal_video_id=None,
                anchor_found=False,
                canonical_source="DOM",
                pages_loaded=0,
                diagnostics=_build_api_diagnostics({"error_type": "network_error", "error": str(e)[:200]})
            )
        
        # Другие ошибки - пробрасываем дальше
        logger.error(f"[VIDEO_PARSER] Error fetching videos from @{username}: {e}", exc_info=True)
        raise
