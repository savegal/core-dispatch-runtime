"""Tests for executor.cookie_health.

Покрываем три вещи:
1. probe_cookie_health правильно мапит QuickLoginCheck → CookieVerdict.
2. register_dead_session растёт счётчик и вызывает mark_needs_reauth
   только после COOKIE_DEAD_RETIRE_THRESHOLD провалов подряд.
3. register_ambiguous_session не двигает счётчик.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest


@pytest.mark.unit
@pytest.mark.asyncio
async def test_probe_cookie_health_ready(monkeypatch):
    from executor import cookie_health
    from executor.session_bootstrap import QuickLoginCheck

    async def fake_check(_page):
        return QuickLoginCheck(
            state="ready",
            on_video=True,
            has_login_button=False,
            has_logged_in_marker=True,
            current_url="https://www.tiktok.com/@u/video/1",
            reason="ok",
        )

    monkeypatch.setattr(cookie_health, "quick_video_login_check", fake_check)
    result = await cookie_health.probe_cookie_health(object())
    assert result.verdict == cookie_health.CookieVerdict.READY
    assert result.is_ready
    assert not result.is_dead


@pytest.mark.unit
@pytest.mark.asyncio
async def test_probe_cookie_health_dead(monkeypatch):
    from executor import cookie_health
    from executor.session_bootstrap import QuickLoginCheck

    async def fake_check(_page):
        return QuickLoginCheck(
            state="dead",
            on_video=False,
            has_login_button=True,
            has_logged_in_marker=False,
            current_url="https://www.tiktok.com/login",
            reason="redirected_to_login",
        )

    monkeypatch.setattr(cookie_health, "quick_video_login_check", fake_check)
    result = await cookie_health.probe_cookie_health(object())
    assert result.is_dead
    assert result.reason == "redirected_to_login"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_probe_cookie_health_ambiguous(monkeypatch):
    from executor import cookie_health
    from executor.session_bootstrap import QuickLoginCheck

    async def fake_check(_page):
        return QuickLoginCheck(
            state="unknown",
            on_video=True,
            has_login_button=False,
            has_logged_in_marker=False,
            current_url="https://www.tiktok.com/@u/video/1",
            reason="ambiguous_ui",
        )

    monkeypatch.setattr(cookie_health, "quick_video_login_check", fake_check)
    result = await cookie_health.probe_cookie_health(object())
    assert result.verdict == cookie_health.CookieVerdict.AMBIGUOUS


@pytest.mark.unit
@pytest.mark.asyncio
async def test_probe_cookie_health_waits_for_captcha_then_switches_account(monkeypatch):
    from executor import cookie_health
    from executor.session_bootstrap import QuickLoginCheck

    class _Page:
        url = "https://www.tiktok.com/@u/video/1"

    async def fake_check(_page):
        return QuickLoginCheck(
            state="ready",
            on_video=True,
            has_login_button=False,
            has_logged_in_marker=True,
            current_url="https://www.tiktok.com/@u/video/1",
            reason="strong_marker:state:isLogin:true",
        )

    async def fake_detect_captcha(_page, wait_ms=0):
        return True, "slider"

    monkeypatch.setattr(cookie_health, "quick_video_login_check", fake_check)
    monkeypatch.setattr(cookie_health, "detect_captcha", fake_detect_captcha)
    monkeypatch.setattr(cookie_health, "_manual_captcha_wait_enabled", lambda: False)

    result = await cookie_health.probe_cookie_health(_Page())

    assert result.verdict == cookie_health.CookieVerdict.CAPTCHA
    assert result.reason == "captcha_challenge:slider"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_probe_cookie_health_empty_video_dom_is_ambiguous(monkeypatch):
    from executor import cookie_health
    from executor.session_bootstrap import QuickLoginCheck

    calls = {"check": 0}

    async def fake_check(_page):
        calls["check"] += 1
        return QuickLoginCheck(
            state="ready",
            on_video=True,
            has_login_button=False,
            has_logged_in_marker=False,
            current_url="https://www.tiktok.com/@u/video/1",
            reason="on_video_empty_dom",
        )

    async def fake_sleep(_seconds):
        return None

    monkeypatch.setattr(cookie_health, "quick_video_login_check", fake_check)
    monkeypatch.setattr(cookie_health.asyncio, "sleep", fake_sleep)

    result = await cookie_health.probe_cookie_health(object())

    assert result.verdict == cookie_health.CookieVerdict.AMBIGUOUS
    assert result.reason == "on_video_empty_dom_after_settle"
    assert calls["check"] == 2


@pytest.mark.unit
@pytest.mark.asyncio
async def test_probe_cookie_health_continues_after_manual_captcha_clear(monkeypatch):
    from executor import cookie_health
    from executor.session_bootstrap import QuickLoginCheck

    class _Page:
        url = "https://www.tiktok.com/@u/video/1"

    async def fake_check(_page):
        return QuickLoginCheck(
            state="ready",
            on_video=True,
            has_login_button=False,
            has_logged_in_marker=True,
            current_url="https://www.tiktok.com/@u/video/1",
            reason="strong_marker:state:isLogin:true",
        )

    states = [(True, "slider"), (False, None), (False, None)]

    async def fake_detect_captcha(_page, wait_ms=0):
        return states.pop(0) if states else (False, None)

    async def fake_sleep(_seconds):
        return None

    monkeypatch.setattr(cookie_health, "quick_video_login_check", fake_check)
    monkeypatch.setattr(cookie_health, "detect_captcha", fake_detect_captcha)
    monkeypatch.setattr(cookie_health, "_manual_captcha_wait_enabled", lambda: True)
    monkeypatch.setattr(cookie_health.asyncio, "sleep", fake_sleep)

    result = await cookie_health.probe_cookie_health(_Page())

    assert result.verdict == cookie_health.CookieVerdict.READY
    assert result.reason == "strong_marker:state:isLogin:true"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_probe_cookie_health_weak_video_ui_is_ambiguous(monkeypatch):
    from executor import cookie_health
    from executor.session_bootstrap import QuickLoginCheck

    async def fake_check(_page):
        return QuickLoginCheck(
            state="unknown",
            on_video=True,
            has_login_button=False,
            has_logged_in_marker=False,
            current_url="https://www.tiktok.com/@u/video/1",
            reason="on_video_no_login_btn:video",
        )

    monkeypatch.setattr(cookie_health, "quick_video_login_check", fake_check)

    result = await cookie_health.probe_cookie_health(object())

    assert result.verdict == cookie_health.CookieVerdict.AMBIGUOUS
    assert result.reason == "on_video_no_login_btn:video"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_probe_cookie_health_visible_login_ui_is_dead(monkeypatch):
    from executor import cookie_health

    async def fake_login_state(_page):
        return {
            "state": "logged_out",
            "loginCount": 1,
            "loginModalCount": 0,
            "commentLoginCtaCount": 0,
            "stateIsLoginFalse": False,
        }

    async def unexpected_quick(_page):
        raise AssertionError("quick check should not run after visible logged-out UI")

    class _Page:
        url = "https://www.tiktok.com/@u/video/1"

    monkeypatch.setattr(cookie_health, "get_browser_login_state", fake_login_state)
    monkeypatch.setattr(cookie_health, "quick_video_login_check", unexpected_quick)

    result = await cookie_health.probe_cookie_health(_Page())

    assert result.verdict == cookie_health.CookieVerdict.DEAD
    assert result.reason.startswith("visible_login_ui:")


@pytest.mark.unit
def test_register_dead_session_does_not_retire_on_first_failure(monkeypatch):
    """Первый DEAD → только soft_error, аккаунт остаётся в работе."""
    from executor import cookie_health

    # Делаем threshold = 2
    monkeypatch.setattr(cookie_health._settings, "COOKIE_DEAD_RETIRE_THRESHOLD", 2)

    account = SimpleNamespace(id=10, validation_fail_count=0)

    calls = {"soft": 0, "reauth": 0}

    def fake_mark_soft_error(_db, account_id, reason):
        calls["soft"] += 1
        assert account_id == 10
        assert "dead_session" in reason

    def fake_mark_needs_reauth(_db, account_id, reason):
        calls["reauth"] += 1

    class FakeQuery:
        def filter(self, *_a, **_k): return self
        def with_for_update(self): return self
        def first(self):
            return account

    class FakeDb:
        def query(self, _model): return FakeQuery()
        def commit(self): pass
        def rollback(self): pass

    monkeypatch.setattr(cookie_health, "mark_soft_error", fake_mark_soft_error)
    monkeypatch.setattr(cookie_health, "mark_needs_reauth", fake_mark_needs_reauth)

    retired = cookie_health.register_dead_session(FakeDb(), account, "redirected_to_login")
    assert retired is False
    assert account.validation_fail_count == 1
    assert calls["soft"] == 1
    assert calls["reauth"] == 0


@pytest.mark.unit
def test_register_dead_session_retires_on_second_consecutive_failure(monkeypatch):
    """Второй подряд DEAD → mark_needs_reauth, аккаунт выходит из ротации."""
    from executor import cookie_health

    monkeypatch.setattr(cookie_health._settings, "COOKIE_DEAD_RETIRE_THRESHOLD", 2)

    # уже один провал был раньше
    account = SimpleNamespace(id=11, validation_fail_count=1)

    calls = {"soft": 0, "reauth": 0}
    seen_reasons = []

    def fake_mark_soft_error(_db, account_id, reason):
        calls["soft"] += 1
        seen_reasons.append(("soft", reason))

    def fake_mark_needs_reauth(_db, account_id, reason):
        calls["reauth"] += 1
        seen_reasons.append(("reauth", reason))

    class FakeQuery:
        def filter(self, *_a, **_k): return self
        def with_for_update(self): return self
        def first(self):
            return account

    class FakeDb:
        def query(self, _model): return FakeQuery()
        def commit(self): pass
        def rollback(self): pass

    monkeypatch.setattr(cookie_health, "mark_soft_error", fake_mark_soft_error)
    monkeypatch.setattr(cookie_health, "mark_needs_reauth", fake_mark_needs_reauth)

    retired = cookie_health.register_dead_session(FakeDb(), account, "logged_out_ui")
    assert retired is True
    assert account.validation_fail_count == 2
    assert calls["reauth"] == 1
    assert calls["soft"] == 0  # ушли в reauth, soft не дёргаем


@pytest.mark.unit
def test_register_ambiguous_session_does_not_bump_retire_counter(monkeypatch):
    """AMBIGUOUS не наказывает аккаунт по счётчику."""
    from executor import cookie_health

    account = SimpleNamespace(id=15, validation_fail_count=0)

    calls = {"soft": 0, "reauth": 0}

    def fake_mark_soft_error(_db, account_id, reason):
        calls["soft"] += 1
        assert "login_ambiguous" in reason

    def fake_mark_needs_reauth(_db, account_id, reason):
        calls["reauth"] += 1

    monkeypatch.setattr(cookie_health, "mark_soft_error", fake_mark_soft_error)
    monkeypatch.setattr(cookie_health, "mark_needs_reauth", fake_mark_needs_reauth)

    cookie_health.register_ambiguous_session(object(), account, "slow_proxy")
    # счётчик НЕ растёт
    assert account.validation_fail_count == 0
    assert calls["soft"] == 1
    assert calls["reauth"] == 0


@pytest.mark.unit
def test_register_captcha_session_cooldowns_without_bumping_dead_counter(monkeypatch):
    from executor import cookie_health

    account = SimpleNamespace(id=16, validation_fail_count=0)
    calls = {"soft": 0, "reauth": 0}
    reasons = []

    def fake_mark_soft_error(_db, account_id, reason):
        calls["soft"] += 1
        reasons.append(reason)

    def fake_mark_needs_reauth(_db, account_id, reason):
        calls["reauth"] += 1

    monkeypatch.setattr(cookie_health, "mark_soft_error", fake_mark_soft_error)
    monkeypatch.setattr(cookie_health, "mark_needs_reauth", fake_mark_needs_reauth)

    cookie_health.register_captcha_session(object(), account, "captcha_challenge:slider")

    assert account.validation_fail_count == 0
    assert calls == {"soft": 1, "reauth": 0}
    assert reasons == ["captcha_challenge:slider"]


@pytest.mark.unit
def test_register_blocked_session_retires_after_repeated_blocked_ui(monkeypatch):
    from executor import cookie_health

    monkeypatch.setattr(cookie_health._settings, "COOKIE_BLOCKED_RETIRE_THRESHOLD", 2, raising=False)
    account = SimpleNamespace(id=17, validation_fail_count=1)
    calls = {"soft": 0, "reauth": 0}
    reasons = []

    def fake_mark_soft_error(_db, account_id, reason):
        calls["soft"] += 1
        reasons.append(("soft", reason))

    def fake_mark_needs_reauth(_db, account_id, reason):
        calls["reauth"] += 1
        reasons.append(("reauth", reason))

    class FakeQuery:
        def filter(self, *_a, **_k): return self
        def with_for_update(self): return self
        def first(self): return account

    class FakeDb:
        def query(self, _model): return FakeQuery()
        def commit(self): pass
        def rollback(self): pass

    monkeypatch.setattr(cookie_health, "mark_soft_error", fake_mark_soft_error)
    monkeypatch.setattr(cookie_health, "mark_needs_reauth", fake_mark_needs_reauth)

    retired = cookie_health.register_blocked_session(
        FakeDb(),
        account,
        "blocked_ui:on_video_empty_dom_after_settle",
    )

    assert retired is True
    assert account.validation_fail_count == 2
    assert calls == {"soft": 0, "reauth": 1}
    assert reasons[0][0] == "reauth"
    assert "blocked_ui_repeat" in reasons[0][1]


@pytest.mark.unit
def test_register_alive_session_does_not_reset_counter(monkeypatch):
    """alive probe сама счётчик не обнуляет. Только успешная публикация это
    делает (через record_main_comment_success / record_reply_success).
    """
    from executor import cookie_health

    account = SimpleNamespace(id=20, validation_fail_count=1)
    # вообще не должно дёргать БД
    cookie_health.register_alive_session(None, account)
    assert account.validation_fail_count == 1
