from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from patchright.async_api import Page
from executor.dom_actions import dismiss_cookie_banners

logger = logging.getLogger("executor.session_bootstrap")

GENERATED_COOKIES = {
    "ttwid",
    "tt_csrf_token",
    "tt_chain_token",
    "passport_csrf_token",
    "odin_tt",
    "cmpl_token",
}

AUTH_COOKIES = {
    "sid_guard",
    "sessionid",
    "sessionid_ss",
    "sid_tt",
    "msToken",
}
LOGIN_AUTH_COOKIES = {
    "sid_guard",
    "sessionid",
    "sessionid_ss",
    "sid_tt",
}

DEAD_URL_MARKERS = ("/login", "/signup", "/verify", "/messages", "/inbox", "/auth")
TIKTOK_MEDIA_PATH_MARKERS = ("/video/", "/photo/")


def is_tiktok_media_page_url(url: Optional[str]) -> bool:
    normalized = (url or "").lower()
    return any(marker in normalized for marker in TIKTOK_MEDIA_PATH_MARKERS)


@dataclass
class BootstrapResult:
    status: str
    dead_reason: Optional[str] = None
    cookies: Dict[str, str] = field(default_factory=dict)
    generated_present: List[str] = field(default_factory=list)
    auth_present: List[str] = field(default_factory=list)
    final_url: Optional[str] = None
    elapsed_seconds: float = 0.0
    browser_login_state: str = "unknown"
    tiktok_username: Optional[str] = None

    @property
    def is_ready(self) -> bool:
        return self.status == "ready"

    @property
    def can_attempt_publish(self) -> bool:
        has_login_auth = any(name in self.cookies for name in LOGIN_AUTH_COOKIES)
        return self.is_ready and has_login_auth and "ttwid" in self.cookies


@dataclass
class QuickLoginCheck:
    """Cheap (<= 2s) visual probe used before publishing.

    state:
      - ready: видим logged_in индикаторы И стоим на /video/
      - dead: видим кнопку Login или редирект на /login,/messages и т.п.
      - unknown: ничего конкретного, но и не login. Звонит вызывающему «решай сам».
    """
    state: str
    on_video: bool
    has_login_button: bool
    has_logged_in_marker: bool
    current_url: Optional[str] = None
    reason: Optional[str] = None


async def extract_tiktok_username(page: Page) -> Optional[str]:
    """Возвращает реальный TikTok username залогиненного аккаунта.

    Ищется в нескольких источниках (в порядке надёжности):
      1. window.__UNIVERSAL_DATA_FOR_REHYDRATION__ или __NEXT_DATA__
         (uniqueId/secUid живут в user store)
      2. Header avatar link href "/@username" (если рендерилась шапка)
      3. Cookie passport_csrf_token / sid_guard НЕ содержат username, поэтому
         не используем.

    Возвращает username БЕЗ ведущего @, либо None если не удалось вытащить.
    """
    try:
        username = await page.evaluate(
            r"""
            () => {
                const fromState = () => {
                    try {
                        const universal = window.__UNIVERSAL_DATA_FOR_REHYDRATION__ || null;
                        const next = window.__NEXT_DATA__ || null;
                        const state = universal || next;
                        if (!state) return null;
                        // typical paths in TikTok SPA state
                        const candidates = [
                            state?.default?.['webapp.user-detail']?.userInfo?.user?.uniqueId,
                            state?.default?.['webapp.app-context']?.user?.uniqueId,
                            state?.['webapp.app-context']?.user?.uniqueId,
                            state?.props?.pageProps?.serverCode?.user?.uniqueId,
                            state?.props?.pageProps?.userInfo?.user?.uniqueId,
                            state?.props?.initialProps?.$user?.uniqueId,
                            state?.props?.pageProps?.appContext?.user?.uniqueId,
                            state?.props?.pageProps?.webappContext?.user?.uniqueId,
                        ];
                        for (const c of candidates) {
                            if (typeof c === 'string' && c.trim()) return c.trim();
                        }
                    } catch (_) {}
                    return null;
                };

                const fromAvatar = () => {
                    const selectors = [
                        'a[data-e2e="nav-profile"][href*="/@"]',
                        'a[data-e2e="profile-icon"][href*="/@"]',
                        'a[href*="/@"][data-e2e*="profile"]',
                        'a[href*="/@"][role="link"]'
                    ];
                    for (const sel of selectors) {
                        const el = document.querySelector(sel);
                        if (!el) continue;
                        const href = el.getAttribute('href') || '';
                        const m = href.match(/\/@([^/?#]+)/);
                        if (m && m[1]) return m[1];
                    }
                    return null;
                };

                return fromState() || fromAvatar() || null;
            }
            """
        )
        if isinstance(username, str):
            cleaned = username.strip().lstrip("@")
            return cleaned or None
    except Exception:
        return None
    return None


async def quick_video_login_check(
    page: Page,
    *,
    expect_video_path: bool = True,
    timeout_ms: int = 1500,
) -> QuickLoginCheck:
    """Быстрый визуальный чек перед публикацией.

    Не делает goto и не ждёт длинных таймаутов. Достаточно ~1 evaluate-вызова
    после того, как страница уже открыта на видео.

    Возвращает QuickLoginCheck со state ∈ {ready, dead, unknown}:
      - ready  → можно публиковать
      - dead   → сразу следующий аккаунт (cookies невалидны / не пускают)
      - unknown→ пока неясно, дать ещё немного времени (но не как сейчас 22с)
    """
    current_url = ""
    try:
        current_url = (page.url or "").lower()
    except Exception:
        pass

    on_video = is_tiktok_media_page_url(current_url)
    if expect_video_path and not on_video:
        for marker in DEAD_URL_MARKERS:
            if marker in current_url:
                return QuickLoginCheck(
                    state="dead",
                    on_video=False,
                    has_login_button=False,
                    has_logged_in_marker=False,
                    current_url=current_url,
                    reason=f"redirected_to_{marker.strip('/')}",
                )
        return QuickLoginCheck(
            state="unknown",
            on_video=False,
            has_login_button=False,
            has_logged_in_marker=False,
            current_url=current_url,
            reason="not_on_video_page",
        )

    try:
        probe = await page.evaluate(
            r"""
            () => {
                const visible = (el) => {
                    if (!el || !el.getBoundingClientRect) return false;
                    const r = el.getBoundingClientRect();
                    const st = getComputedStyle(el);
                    return r.width > 0 && r.height > 0
                        && st.display !== 'none'
                        && st.visibility !== 'hidden'
                        && st.opacity !== '0';
                };

                const text = (el) => ((el && (el.innerText || el.textContent)) || '').trim().toLowerCase();
                const compactText = (el) => text(el).replace(/\s+/g, ' ').trim();

                // STRONG logged-in markers — это то, что точно отрисовывается
                // только когда пользователь авторизован.
                const strongLoggedInSelectors = [
                    '[data-e2e="profile-icon"]',
                    '[data-e2e="nav-profile"]',
                    '[data-e2e="inbox-icon"]',
                    '[data-e2e="message-icon"]',
                    'a[href*="/@"][data-e2e="nav-profile"]',
                    // Дополнительные сильные маркеры (для разных layout'ов TikTok):
                    '[data-e2e="upload-icon"]',
                    '[data-e2e="nav-upload"]',
                    'a[href="/upload"]',
                    'a[href="/messages"]',
                ];
                let strongLoggedIn = false;
                let strongMarkerFound = null;
                for (const s of strongLoggedInSelectors) {
                    const el = document.querySelector(s);
                    if (visible(el)) {
                        strongLoggedIn = true;
                        strongMarkerFound = s;
                        break;
                    }
                }

                // WEAK markers — это TikTok UI вообще загрузился. Сами по себе
                // НЕ доказывают login (есть и у гостя), но в комбинации с
                // отсутствием visible login button они дают «вероятно залогинен».
                if (strongMarkerFound && [
                    '[data-e2e="profile-icon"]',
                    '[data-e2e="nav-profile"]',
                    '[data-e2e="inbox-icon"]',
                    '[data-e2e="message-icon"]',
                    'a[href*="/@"][data-e2e="nav-profile"]',
                    '[data-e2e="upload-icon"]',
                    '[data-e2e="nav-upload"]',
                    'a[href="/upload"]',
                    'a[href="/messages"]',
                ].includes(strongMarkerFound)) {
                    strongLoggedIn = false;
                    strongMarkerFound = null;
                }
                if (!strongLoggedIn) {
                    const stateText = [
                        document.getElementById('SIGI_STATE')?.textContent || '',
                        document.getElementById('__UNIVERSAL_DATA_FOR_REHYDRATION__')?.textContent || '',
                        document.body?.innerHTML?.slice(0, 250000) || '',
                    ].join('\n');
                    if (stateText.includes('"isLogin":true') || stateText.includes('"is_login":true')) {
                        strongLoggedIn = true;
                        strongMarkerFound = 'state:isLogin:true';
                    }
                }
                if (!strongLoggedIn) {
                    for (const el of [...document.querySelectorAll('[data-e2e="profile-icon"], [data-e2e="nav-profile"]')]) {
                        if (!visible(el)) continue;
                        const link = el.closest('a[href^="/@"]') || el.querySelector('a[href^="/@"]');
                        const href = ((link && link.getAttribute('href')) || '').toLowerCase();
                        if (href.startsWith('/@') && !href.includes('/login')) {
                            strongLoggedIn = true;
                            strongMarkerFound = 'profile-link:' + href.slice(0, 40);
                            break;
                        }
                    }
                }

                const weakUiSelectors = [
                    '[data-e2e="comment-icon"]',
                    '[data-e2e="like-icon"]',
                    '[data-e2e="share-icon"]',
                    '[data-e2e="comments"]',
                    'button[data-e2e="comments"]',
                    'video',
                ];
                let weakUiPresent = false;
                let weakMarkerFound = null;
                for (const s of weakUiSelectors) {
                    const el = document.querySelector(s);
                    if (visible(el)) {
                        weakUiPresent = true;
                        weakMarkerFound = s;
                        break;
                    }
                }

                // explicit login buttons (only count if logged-in markers absent).
                // Локали: en, ru, uk, es, fr, de, pt, it, tr, id, vi, th, ar, hi,
                // zh-Hans/Hant, ja, ko, pl. Это покрывает все TikTok-регионы из
                // их официального языкового списка.
                //
                // ВНИМАНИЕ: массив нормализуется через .toLowerCase() при сравнении,
                // потому что некоторые языки (vi, tr) имеют буквы с разным
                // case-mapping (Đ → đ, İ → i̇), которые могут сломать прямой
                // indexOf без нормализации с обеих сторон.
                const loginLabelsRaw = [
                    'log in', 'login', 'sign in', 'sign-in',
                    'войти',              // ru
                    'увійти',        // uk
                    'iniciar sesión', 'iniciar sesion',       // es
                    'se connecter',                                 // fr
                    'anmelden', 'einloggen',                        // de
                    'entrar', 'iniciar sessão',                // pt
                    'accedi',                                       // it
                    'oturum aç',                               // tr
                    'masuk',                                        // id
                    'Đăng nhập',                     // vi
                    'เข้าสู่ระบบ',  // th
                    'تسجيل الدخول', // ar
                    'लॉग इन',              // hi
                    '登录',                                 // zh-Hans
                    '登入', '登錄',                 // zh-Hant
                    'ログイン',                     // ja
                    '로그인',                           // ko
                    'zaloguj się', 'zaloguj',                  // pl
                ];
                const loginLabels = loginLabelsRaw.map(s => s.toLowerCase());
                // Точное совпадение текста — для коротких кнопок ("Log in").
                // startsWith — для кнопок с продолжением ("Log in to TikTok").
                // Длина >= 2 нужна чтобы не словить случайное короткое слово.
                const matchesLoginLabel = (t) => {
                    if (!t) return false;
                    for (const label of loginLabels) {
                        if (label.length < 2) continue;
                        if (t === label) return true;
                        if (t.startsWith(label + ' ') || t.startsWith(label + '\n')) return true;
                    }
                    return false;
                };
                let loginBtn = false;
                let loginBtnReason = null;
                const nodes = document.querySelectorAll([
                    'button',
                    'a',
                    '[role="button"]',
                    '[data-e2e*="login" i]',
                    '[href*="/login" i]',
                    '[href*="/signup" i]',
                    'div[tabindex]',
                    'div[class*="Button"]',
                    'div[class*="button"]',
                    'span[class*="Button"]',
                    'span[class*="button"]',
                    'div',
                    'span',
                    'p',
                ].join(','));
                for (const n of nodes) {
                    if (!visible(n)) continue;
                    const t = compactText(n);
                    if (!t && !n.getAttribute('data-e2e') && !n.getAttribute('href') && !n.getAttribute('aria-label')) {
                        continue;
                    }
                    if (t.length > 90) {
                        continue;
                    }
                    const dataE2e = (n.getAttribute('data-e2e') || '').toLowerCase();
                    const ariaLabel = ((n.getAttribute && n.getAttribute('aria-label')) || '').toLowerCase().trim();
                    const href = ((n.getAttribute && n.getAttribute('href')) || '').toLowerCase();
                    // 1) Любой data-e2e содержащий "login" (login-button, top-login-button,
                    //    login-button-v2, и т.п.).
                    if (dataE2e.includes('login')) {
                        loginBtn = true;
                        loginBtnReason = 'data-e2e:' + dataE2e;
                        break;
                    }
                    // 2) Ссылка на /login (только в шапке/topbar — у нас уже фильтр visible).
                    if (href.includes('/login') || href.includes('/signup')) {
                        loginBtn = true;
                        loginBtnReason = 'href:' + href.slice(0, 30);
                        break;
                    }
                    // 3) innerText точно равен или начинается с login-label.
                    if (matchesLoginLabel(t)) {
                        loginBtn = true;
                        loginBtnReason = 'text:' + t.slice(0, 30);
                        break;
                    }
                    // 4) aria-label начинается с login-label (для кнопок-иконок без текста).
                    if (matchesLoginLabel(ariaLabel)) {
                        loginBtn = true;
                        loginBtnReason = 'aria:' + ariaLabel.slice(0, 30);
                        break;
                    }
                }

                return {
                    strongLoggedIn,
                    loginBtn,
                    loginBtnReason,
                    weakUiPresent,
                    strongMarkerFound,
                    weakMarkerFound,
                };
            }
            """
        )
    except Exception as exc:
        return QuickLoginCheck(
            state="unknown",
            on_video=on_video,
            has_login_button=False,
            has_logged_in_marker=False,
            current_url=current_url,
            reason=f"probe_failed:{str(exc)[:80]}",
        )

    if not isinstance(probe, dict):
        return QuickLoginCheck(
            state="unknown",
            on_video=on_video,
            has_login_button=False,
            has_logged_in_marker=False,
            current_url=current_url,
            reason="probe_no_dict",
        )

    strong_logged_in = bool(probe.get("strongLoggedIn") or probe.get("loggedIn"))
    login_btn = bool(probe.get("loginBtn"))
    login_btn_reason = probe.get("loginBtnReason") or ""
    weak_ui = bool(probe.get("weakUiPresent"))
    strong_marker = probe.get("strongMarkerFound")
    weak_marker = probe.get("weakMarkerFound")

    # 1) Login button виден → точно logged out, аккаунт надо ротировать.
    if login_btn:
        return QuickLoginCheck(
            state="dead",
            on_video=on_video,
            has_login_button=True,
            has_logged_in_marker=strong_logged_in,
            current_url=current_url,
            reason=f"visible_login_button:{login_btn_reason}" if login_btn_reason else "visible_login_button",
        )

    # 2) Сильный маркер залогинения (profile-icon / inbox / upload) → 100% ready.
    if strong_logged_in:
        return QuickLoginCheck(
            state="ready",
            on_video=on_video,
            has_login_button=False,
            has_logged_in_marker=True,
            current_url=current_url,
            reason=f"strong_marker:{strong_marker}" if strong_marker else "strong_marker",
        )

    # 3) Login button НЕ виден, мы на странице видео, TikTok UI отрендерился.
    # Это больше НЕ является доказательством залогиненной сессии: guest-страница
    # TikTok тоже показывает video/comment/like/share UI, а login CTA может
    # появиться позднее или только после открытия comments. Caller обязан
    # подтвердить real composer через action-ready probe.
    if on_video and weak_ui:
        return QuickLoginCheck(
            state="unknown",
            on_video=on_video,
            has_login_button=False,
            has_logged_in_marker=False,
            current_url=current_url,
            reason=f"on_video_no_login_btn:{weak_marker}" if weak_marker else "on_video_no_login_btn",
        )

    # 4) URL уже /video/, явной login кнопки нет, но TikTok ещё не отдал ни
    # strong-, ни weak-маркеры. Это тоже только unconfirmed auth; cookie_health
    # сделает короткий settle/retry, а user_schema при необходимости откроет
    # composer перед публикацией.
    if on_video and not login_btn:
        return QuickLoginCheck(
            state="unknown",
            on_video=True,
            has_login_button=False,
            has_logged_in_marker=False,
            current_url=current_url,
            reason="on_video_no_login_btn_unconfirmed" if weak_ui else "on_video_empty_dom",
        )

    # 5) Ничего не понятно (рано вызвали, страница пустая).
    return QuickLoginCheck(
        state="unknown",
        on_video=on_video,
        has_login_button=False,
        has_logged_in_marker=strong_logged_in,
        current_url=current_url,
        reason="ambiguous_ui",
    )


async def get_browser_login_state(page: Page) -> dict:
    """Return TikTok login state based on visible UI, not only cookies.

    Cookies can be present in the browser context while TikTok still renders a
    logged-out page. For publishing, the visible UI is the source of truth.
    """
    try:
        state = await page.evaluate(
            r"""
            () => {
                const visible = (el) => {
                    if (!el || !el.getBoundingClientRect) return false;
                    const r = el.getBoundingClientRect();
                    const st = getComputedStyle(el);
                    return r.width > 0 && r.height > 0
                        && st.display !== 'none'
                        && st.visibility !== 'hidden'
                        && st.opacity !== '0';
                };

                const textOf = (el) => (
                    ((el && (el.innerText || el.textContent)) || '')
                        .replace(/\s+/g, ' ')
                        .trim()
                        .toLowerCase()
                );

                const labelOf = (el) => [
                    textOf(el),
                    (el.getAttribute('aria-label') || '').toLowerCase(),
                    (el.getAttribute('data-e2e') || '').toLowerCase(),
                    (el.getAttribute('href') || '').toLowerCase(),
                ].join(' ');

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

                const loginNodes = [
                    ...document.querySelectorAll([
                        'button',
                        'a',
                        '[role="button"]',
                        '[data-e2e*="login" i]',
                        '[href*="/login" i]',
                        '[href*="/signup" i]',
                        'div[tabindex]',
                        'div[class*="Button"]',
                        'div[class*="button"]',
                        'span[class*="Button"]',
                        'span[class*="button"]',
                        'div',
                        'span',
                        'p',
                    ].join(','))
                ].filter(visible).filter((el) => {
                    if (inCookieContext(el)) return false;
                    const label = labelOf(el);
                    const visibleText = textOf(el);
                    const dataE2e = (el.getAttribute('data-e2e') || '').toLowerCase();
                    const aria = (el.getAttribute('aria-label') || '').toLowerCase();
                    if (!visibleText && !dataE2e && !aria && !(el.getAttribute('href') || '')) return false;
                    if (visibleText.length > 90) return false;
                    const loginLabels = [
                        'log in',
                        'login',
                        'sign in',
                        'sign-in',
                        '\u0432\u043e\u0439\u0442\u0438',
                        '\u0443\u0432\u0456\u0439\u0442\u0438',
                        '\u767b\u5f55',
                        '\u767b\u5165',
                        '\u30ed\u30b0\u30a4\u30f3',
                        '\ub85c\uadf8\uc778',
                    ];
                    const matches = (value) => loginLabels.some((needle) => (
                        value === needle || value.startsWith(needle + ' ') || value.startsWith(needle + '\n')
                    ));
                    return matches(visibleText)
                        || matches(aria)
                        || dataE2e.includes('login-button')
                        || dataE2e === 'login'
                        || label.includes('/login')
                        || label.includes('/signup');
                });

                const stateText = [
                    document.getElementById('SIGI_STATE')?.textContent || '',
                    document.getElementById('__UNIVERSAL_DATA_FOR_REHYDRATION__')?.textContent || '',
                    document.body?.innerHTML?.slice(0, 250000) || '',
                ].join('\n').toLowerCase();
                const stateIsLoginTrue = stateText.includes('"islogin":true')
                    || stateText.includes('"is_login":true')
                    || stateText.includes('"isLogin":true'.toLowerCase());
                const stateIsLoginFalse = stateText.includes('"islogin":false')
                    || stateText.includes('"is_login":false')
                    || stateText.includes('"isLogin":false'.toLowerCase());

                const dialogNodes = [
                    ...document.querySelectorAll('[role="dialog"], [aria-modal="true"], div[class*="Modal"], div[class*="modal"]')
                ].filter(visible).filter((el) => {
                    if (inCookieContext(el)) return false;
                    const label = labelOf(el);
                    return label.includes('log in to tiktok')
                        || label.includes('sign up for tiktok')
                        || label.includes('continue with google')
                        || label.includes('continue with facebook')
                        || label.includes('continue with qr')
                        || label.includes('use phone')
                        || label.includes('email / username')
                        || label.includes('phone / email')
                        || label.includes('\u0432\u043e\u0439\u0442\u0438');
                });

                const commentLoginNodes = [
                    ...document.querySelectorAll('button, a, [role="button"], div, span, p')
                ].filter(visible).filter((el) => {
                    if (inCookieContext(el)) return false;
                    const t = textOf(el);
                    if (!t || t.length > 120) return false;
                    return t.includes('log in to comment')
                        || t.includes('login to comment')
                        || t.includes('log in to view comments')
                        || t.includes('log in to reply')
                        || t.includes('log in to like')
                        || t.includes('sign in to comment');
                });

                const loggedInSelectors = [
                    '[data-e2e="inbox-icon"]',
                    '[data-e2e="nav-message"]',
                    '[data-e2e="message-icon"]',
                    '[aria-label*="Open settings menu" i]',
                ];

                const selectorSignals = [];
                for (const selector of loggedInSelectors) {
                    const el = document.querySelector(selector);
                    if (visible(el)) selectorSignals.push(selector);
                }
                for (const el of document.querySelectorAll('[data-e2e="profile-icon"], [data-e2e="nav-profile"]')) {
                    if (!visible(el)) continue;
                    const link = el.closest('a[href^="/@"]') || el.querySelector('a[href^="/@"]');
                    const href = ((link && link.getAttribute('href')) || '').toLowerCase();
                    if (href.startsWith('/@') && !href.includes('/login')) {
                        selectorSignals.push('profile-link:' + href.slice(0, 40));
                        break;
                    }
                }

                const navSignals = [];
                for (const el of document.querySelectorAll('a, button, [role="button"]')) {
                    if (!visible(el)) continue;
                    const text = textOf(el);
                    if (text === 'messages' || text.includes('notifications')) {
                        navSignals.push(text.slice(0, 40));
                    }
                }

                const loggedInCount = selectorSignals.length + navSignals.length;
                const loginCount = loginNodes.length;
                const loginModalCount = dialogNodes.length;
                const commentLoginCtaCount = commentLoginNodes.length;
                let state = 'unknown';
                if ((stateIsLoginTrue || loggedInCount > 0) && !stateIsLoginFalse) state = 'logged_in';
                else if (loginCount > 0 || loginModalCount > 0 || commentLoginCtaCount > 0 || stateIsLoginFalse) state = 'logged_out';

                return {
                    state,
                    loginCount,
                    loginModalCount,
                    commentLoginCtaCount,
                    loggedInCount,
                    stateIsLoginTrue,
                    stateIsLoginFalse,
                    selectorSignals: selectorSignals.slice(0, 6),
                    navSignals: navSignals.slice(0, 6),
                    pathname: window.location.pathname,
                };
            }
            """
        )
        if isinstance(state, dict):
            return state
    except Exception as exc:
        logger.debug("[BOOTSTRAP] Browser login state check failed: %s", exc)
    return {"state": "unknown", "loginCount": 0, "loggedInCount": 0}


async def _apply_browser_login_state(
    page: Page,
    result: BootstrapResult,
    *,
    correlation_id: Optional[str],
    started: float,
) -> bool:
    try:
        await dismiss_cookie_banners(page)
    except Exception as exc:
        logger.debug("[BOOTSTRAP] Cookie banner cleanup before login-state failed: %s", exc)
    await asyncio.sleep(0.3)
    state = await get_browser_login_state(page)
    result.browser_login_state = str(state.get("state") or "unknown")
    if result.browser_login_state == "logged_out":
        result.status = "dead_session"
        result.dead_reason = "browser_login_ui_visible"
        result.final_url = getattr(page, "url", None)
        result.elapsed_seconds = time.monotonic() - started
        logger.info(
            "[BOOTSTRAP] Dead session detected for %s: visible login UI despite auth cookies | state=%s",
            correlation_id or "unknown",
            state,
        )
        return False
    return True


async def bootstrap_session(
    page: Page,
    video_url: str,
    *,
    max_total_seconds: float = 22.0,
    correlation_id: Optional[str] = None,
) -> BootstrapResult:
    started = time.monotonic()
    result = BootstrapResult(status="timeout")

    navigation_ok = False
    try:
        await page.goto(
            video_url,
            wait_until="domcontentloaded",
            timeout=15000,
        )
        navigation_ok = True
    except Exception as exc:
        exc_str = str(exc).lower()
        if "timeout" in exc_str or "net::" in exc_str:
            logger.debug(
                "[BOOTSTRAP] Navigation timeout for %s (continuing): %s",
                correlation_id or "unknown",
                str(exc)[:80],
            )
        else:
            result.status = "navigation_failed"
            result.dead_reason = f"goto_failed: {str(exc)[:100]}"
            result.elapsed_seconds = time.monotonic() - started
            logger.warning(
                "[BOOTSTRAP] Navigation failed for %s: %s",
                correlation_id or "unknown",
                result.dead_reason,
            )
            return result

    # TikTok is a React SPA. Give it time to render and generate client cookies.
    await asyncio.sleep(2.5)

    # DEBUG: проверяем что stealth работает
    try:
        webdriver_flag = await page.evaluate("() => navigator.webdriver")
        has_chrome = await page.evaluate("() => !!window.chrome")
        plugins_count = await page.evaluate("() => navigator.plugins.length")
        logger.info(
            "[STEALTH_CHECK] webdriver=%s chrome=%s plugins=%d",
            webdriver_flag, has_chrome, plugins_count,
        )
    except Exception:
        pass

    try:
        current_url = (page.url or "").lower()
    except Exception:
        current_url = ""
    for marker in DEAD_URL_MARKERS:
        if marker in current_url:
            result.status = "dead_session"
            result.dead_reason = f"redirected_to_{marker.strip('/')}"
            result.final_url = page.url
            result.elapsed_seconds = time.monotonic() - started
            logger.info(
                "[BOOTSTRAP] Dead session detected (%s) for %s in %.1fs",
                marker,
                correlation_id or "unknown",
                result.elapsed_seconds,
            )
            return result

    deadline = started + max_total_seconds
    last_generated_count = -1
    stable_cycles = 0

    while time.monotonic() < deadline:
        try:
            current_url = (page.url or "").lower()
            for marker in DEAD_URL_MARKERS:
                if marker in current_url:
                    result.status = "dead_session"
                    result.dead_reason = f"redirected_to_{marker.strip('/')}_during_load"
                    result.final_url = page.url
                    result.elapsed_seconds = time.monotonic() - started
                    logger.info(
                        "[BOOTSTRAP] Dead session detected during load for %s: %s",
                        correlation_id or "unknown",
                        marker,
                    )
                    return result
        except Exception:
            break

        try:
            cookies_list = await page.context.cookies()
        except Exception:
            break

        cookies = {c["name"]: c["value"] for c in cookies_list}
        generated = [name for name in GENERATED_COOKIES if name in cookies]
        auth = [name for name in AUTH_COOKIES if name in cookies]

        if auth and "ttwid" in cookies and "tt_csrf_token" in cookies:
            result.status = "ready"
            result.cookies = cookies
            result.generated_present = generated
            result.auth_present = auth
            result.final_url = page.url
            result.elapsed_seconds = time.monotonic() - started
            if not await _apply_browser_login_state(
                page,
                result,
                correlation_id=correlation_id,
                started=started,
            ):
                return result
            try:
                result.tiktok_username = await extract_tiktok_username(page)
            except Exception:
                result.tiktok_username = None
            logger.info(
                "[BOOTSTRAP] Session ready for %s in %.1fs | auth=%s | generated=%s | browser=%s | username=%s",
                correlation_id or "unknown",
                result.elapsed_seconds,
                auth,
                generated,
                result.browser_login_state,
                result.tiktok_username or "?",
            )
            return result

        if len(generated) == last_generated_count:
            stable_cycles += 1
        else:
            stable_cycles = 0
        last_generated_count = len(generated)

        if stable_cycles >= 3 and auth:
            result.status = "ready"
            result.cookies = cookies
            result.generated_present = generated
            result.auth_present = auth
            result.final_url = page.url
            result.elapsed_seconds = time.monotonic() - started
            if not await _apply_browser_login_state(
                page,
                result,
                correlation_id=correlation_id,
                started=started,
            ):
                return result
            logger.info(
                "[BOOTSTRAP] Session settled (partial) for %s in %.1fs | auth=%s | generated=%s | ttwid=%s csrf=%s | browser=%s",
                correlation_id or "unknown",
                result.elapsed_seconds,
                auth,
                generated,
                "ttwid" in cookies,
                "tt_csrf_token" in cookies,
                result.browser_login_state,
            )
            return result

        try:
            await page.evaluate("window.scrollBy(0, 150)")
        except Exception:
            pass

        await asyncio.sleep(0.4)

    try:
        cookies_list = await page.context.cookies()
        cookies = {c["name"]: c["value"] for c in cookies_list}
        result.cookies = cookies
        result.generated_present = [name for name in GENERATED_COOKIES if name in cookies]
        result.auth_present = [name for name in AUTH_COOKIES if name in cookies]
    except Exception:
        pass

    result.elapsed_seconds = time.monotonic() - started
    result.final_url = getattr(page, "url", None)
    logger.warning(
        "[BOOTSTRAP] Timeout for %s after %.1fs | auth=%s | generated=%s",
        correlation_id or "unknown",
        result.elapsed_seconds,
        result.auth_present,
        result.generated_present,
    )
    return result
