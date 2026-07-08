"""
Unit тесты для account_service.
Тестируют логику выбора и управления аккаунтами.
"""
import json
import pytest
from datetime import datetime, timezone, timedelta
from sqlalchemy.orm import Session
import services.account_service as account_service_module
from services.account_service import (
    MAIN_SCOPE,
    REPLY_SCOPE,
    get_account_runtime_health,
    get_browser_validation_policy,
    get_free_account_for_main_comment,
    get_free_account_for_reply,
    get_monitor_account,
    get_monitor_pool_stats,
    mark_error,
    mark_runtime_cooldown,
    mark_soft_error,
    mark_success,
    mark_monitor_use,
    mark_dead,
    mark_needs_reauth,
    increment_main_comment
)
from db.models import Account, Proxy
from config.settings import get_settings

settings = get_settings()


@pytest.fixture(autouse=True)
def reset_account_service_runtime_state(monkeypatch: pytest.MonkeyPatch):
    account_service_module._MAIN_ACCOUNT_RESERVED_UNTIL.clear()
    account_service_module._REPLY_ACCOUNT_RESERVED_UNTIL.clear()
    monkeypatch.setattr(account_service_module.settings, "USE_PROXY", False, raising=False)
    yield
    account_service_module._MAIN_ACCOUNT_RESERVED_UNTIL.clear()
    account_service_module._REPLY_ACCOUNT_RESERVED_UNTIL.clear()


@pytest.mark.unit
def test_proxy_selection_diag_masks_credentials():
    proxy = Proxy(id=7, proxy="http://user:secret@example.com:8000", is_mobile="Y", country_code="PL")

    diag = account_service_module._safe_proxy_selection_diag(proxy.proxy, proxy)

    assert "secret" not in diag
    assert "user:secret" not in diag
    assert "example.com:8000" in diag
    assert "id=7" in diag
    assert "mobile=Y" in diag
    assert "country=PL" in diag
    assert "sig=" in diag


@pytest.mark.unit
def test_account_selection_diagnostics_exposes_cookie_keys_not_values():
    now = datetime(2026, 5, 22, 12, 0, tzinfo=timezone.utc)
    account = Account(
        id=41,
        label="diag",
        status="active",
        cookies=json.dumps({"sessionid": "secret-session", "ttwid": "secret-ttwid"}),
        proxy="host.example:9000:user:pass",
        created_at=now - timedelta(hours=2),
        last_used_at=now - timedelta(minutes=35),
        last_main_comment_at=now - timedelta(minutes=50),
        last_validation_ok=now - timedelta(minutes=20),
        today_main_comments=2,
        today_replies=3,
        error_count_recent=1,
    )

    diag = account_service_module._account_selection_diagnostics(account, now)

    assert "sessionid" in diag
    assert "ttwid" in diag
    assert "secret-session" not in diag
    assert "secret-ttwid" not in diag
    assert "host.example:9000" in diag
    assert "age_min=120" in diag
    assert "last_used_min=35" in diag
    assert "last_main_min=50" in diag
    assert "last_valid_min=20" in diag
    assert "today_main=2" in diag
    assert "today_replies=3" in diag
    assert "errors=1" in diag
    assert "runtime_health=risky" in diag


@pytest.mark.unit
class TestAccountService:
    """Тесты для account_service."""
    
    def test_get_free_account_active(self, db_session: Session, sample_account: Account):
        """Тест получения свободного активного аккаунта."""
        account = get_free_account_for_main_comment(db_session)
        assert account is not None
        assert account.id == sample_account.id
        assert account.status == "active"
    
    def test_get_free_account_excludes_dead(self, db_session: Session):
        """Тест исключения dead аккаунтов."""
        # Создаём dead аккаунт
        dead_account = Account(
            label="dead_account",
            status="dead",
            cookies='{"sessionid":"dead"}',
            today_main_comments=0
        )
        db_session.add(dead_account)
        
        # Создаём active аккаунт
        active_account = Account(
            label="active_account",
            status="active",
            cookies='{"sessionid":"active"}',
            today_main_comments=0
        )
        db_session.add(active_account)
        db_session.commit()
        
        # Должен вернуть active, а не dead
        account = get_free_account_for_main_comment(db_session)
        assert account is not None
        assert account.status == "active"
        assert account.id == active_account.id
    
    def test_get_free_account_respects_daily_limit(self, db_session: Session):
        """Тест уважения дневного лимита комментариев."""
        # Создаём аккаунт с достигнутым лимитом
        limited_account = Account(
            label="limited_account",
            status="active",
            cookies='{"sessionid":"limited"}',
            today_main_comments=settings.MAX_ACTIONS_PER_ACCOUNT  # Достигнут лимит
        )
        db_session.add(limited_account)
        
        # Создаём аккаунт без лимита
        free_account = Account(
            label="free_account",
            status="active",
            cookies='{"sessionid":"free"}',
            today_main_comments=0
        )
        db_session.add(free_account)
        db_session.commit()
        
        # Должен вернуть аккаунт без лимита
        account = get_free_account_for_main_comment(db_session)
        assert account is not None
        assert account.id == free_account.id
    
    def test_get_free_account_respects_cooldown(self, db_session: Session):
        """Тест уважения cooldown между использованиями."""
        # Создаём аккаунт, который недавно использовался
        recently_used = Account(
            label="recently_used",
            status="active",
            cookies='{"sessionid":"recent"}',
            today_main_comments=0,
            last_main_comment_at=datetime.now(timezone.utc)  # Только что использован
        )
        db_session.add(recently_used)
        
        # Создаём аккаунт, который давно не использовался
        old_account = Account(
            label="old_account",
            status="active",
            cookies='{"sessionid":"old"}',
            today_main_comments=0,
            last_main_comment_at=datetime.now(timezone.utc) - timedelta(seconds=settings.ACCOUNT_COOLDOWN_SECONDS + 30)
        )
        db_session.add(old_account)
        db_session.commit()
        
        # Должен вернуть аккаунт, который давно не использовался
        account = get_free_account_for_main_comment(db_session)
        assert account is not None
        assert account.id == old_account.id
    
    def test_mark_error(self, db_session: Session, sample_account: Account):
        """Тест пометки аккаунта с ошибкой."""
        initial_error_count = sample_account.error_count_recent or 0
        # mark_error принимает account_id (int)
        mark_error(db_session, sample_account.id)
        
        db_session.refresh(sample_account)
        assert sample_account.error_count_recent == initial_error_count + 1
    
    def test_mark_success(self, db_session: Session, sample_account: Account):
        """Тест пометки успешного использования аккаунта."""
        # Устанавливаем ошибки
        sample_account.error_count_recent = 5
        sample_account.last_error = "Previous error"
        sample_account.status = "error"
        db_session.commit()
        
        # mark_success принимает account_id (int)
        mark_success(db_session, sample_account.id)
        
        db_session.refresh(sample_account)
        assert sample_account.error_count_recent == 0
        assert sample_account.status == "active"
    
    def test_increment_main_comment(self, db_session: Session, sample_account: Account):
        """Тест увеличения счётчика основных комментариев."""
        initial_count = sample_account.today_main_comments
        increment_main_comment(db_session, sample_account.id)
        
        db_session.refresh(sample_account)
        assert sample_account.today_main_comments == initial_count + 1
        assert sample_account.last_main_comment_at is not None

    def test_get_monitor_account_skips_active_monitor_cooldown(self, db_session: Session):
        """Monitor selection should skip accounts that are still in monitor cooldown."""
        cooldown_until = (datetime.now(timezone.utc) + timedelta(minutes=20)).isoformat()
        cooled = Account(
            label="monitor_cooldown",
            status="active",
            cookies="sessionid=abc",
            proxy="http://proxy-one",
            last_error=json.dumps(
                {
                    "scope": "monitor",
                    "category": "captcha",
                    "reason": "CAPTCHA detected during video parsing",
                    "cooldown_until": cooldown_until,
                }
            ),
        )
        healthy = Account(
            label="monitor_healthy",
            status="active",
            cookies="sessionid=def",
            proxy="http://proxy-two",
            last_validation_ok=datetime.now(timezone.utc),
        )
        db_session.add_all([cooled, healthy])
        db_session.commit()

        account = get_monitor_account(db_session)
        assert account is not None
        assert account.id == healthy.id

    def test_mark_soft_error_serializes_monitor_parsing_error(self, db_session: Session):
        """Monitor parsing errors should be stored as structured JSON for cooldown logic."""
        account = Account(
            label="monitor_structured_error",
            status="active",
            cookies="sessionid=xyz",
            proxy="http://proxy-three",
        )
        db_session.add(account)
        db_session.commit()

        mark_soft_error(db_session, account.id, "CAPTCHA detected during video parsing")

        db_session.refresh(account)
        payload = json.loads(account.last_error)
        assert payload["scope"] == "monitor"
        assert payload["category"] == "captcha"
        assert payload["cooldown_until"]

    def test_mark_soft_error_does_not_classify_local_browser_crash_as_network(self, db_session: Session):
        account = Account(
            label="monitor_browser_driver_error",
            status="active",
            cookies="sessionid=driver",
            proxy="http://proxy-driver",
        )
        db_session.add(account)
        db_session.commit()

        mark_soft_error(
            db_session,
            account.id,
            "BrowserType.launch: Connection closed while reading from the driver",
        )

        db_session.refresh(account)
        payload = json.loads(account.last_error)
        assert payload["category"] == "unknown"

    def test_get_monitor_pool_stats_counts_monitor_buckets(self, db_session: Session):
        """Pool stats should expose healthy/cooldown/dead_session buckets."""
        cooldown_until = (datetime.now(timezone.utc) + timedelta(minutes=15)).isoformat()
        accounts = [
            Account(
                label="monitor_stats_healthy",
                status="active",
                cookies="sessionid=h1",
                proxy="http://proxy-a",
                last_validation_ok=datetime.now(timezone.utc),
            ),
            Account(
                label="monitor_stats_cooldown",
                status="active",
                cookies="sessionid=h2",
                proxy="http://proxy-b",
                last_error=json.dumps(
                    {
                        "scope": "monitor",
                        "category": "rate_limit",
                        "reason": "Rate limited during video parsing",
                        "cooldown_until": cooldown_until,
                    }
                ),
            ),
            Account(
                label="monitor_stats_dead",
                status="dead",
                cookies="sessionid=h3",
                proxy="http://proxy-c",
            ),
        ]
        db_session.add_all(accounts)
        db_session.commit()

        stats = get_monitor_pool_stats(db_session)
        assert stats["total_candidates"] == 3
        assert stats["healthy"] == 1
        assert stats["cooldown"] == 1
        assert stats["dead_session"] == 1
        assert stats["eligible_now"] == 1

    def test_mark_soft_error_serializes_runtime_captcha_error(self, db_session: Session):
        """Execution errors should be structured too, so selectors can apply cooldowns."""
        account = Account(
            label="runtime_structured_error",
            status="active",
            cookies="sessionid=runtime1",
        )
        db_session.add(account)
        db_session.commit()

        mark_soft_error(db_session, account.id, "CAPTCHA during comment publish")

        db_session.refresh(account)
        payload = json.loads(account.last_error)
        assert payload["scope"] == "runtime"
        assert payload["category"] == "captcha"
        assert payload["cooldown_until"]
        assert account.last_used_at is not None

    def test_mark_soft_error_serializes_blocked_comment_ui_error(self, db_session: Session):
        """Ready accounts with no usable comment UI should cool down as page_blocked."""
        account = Account(
            label="runtime_blocked_ui_error",
            status="active",
            cookies="sessionid=runtime_blocked",
        )
        db_session.add(account)
        db_session.commit()

        mark_soft_error(db_session, account.id, "publish_failed:input_not_found: comment_input_not_visible")

        db_session.refresh(account)
        payload = json.loads(account.last_error)
        assert payload["scope"] == "runtime"
        assert payload["category"] == "page_blocked"
        assert payload["cooldown_until"]

    def test_mark_runtime_cooldown_applies_explicit_cookie_enrichment_cooldown(self, db_session: Session):
        account = Account(
            label="runtime_cookie_enrichment_cooldown",
            status="active",
            cookies="sessionid=runtime_enrich",
        )
        db_session.add(account)
        db_session.commit()

        mark_runtime_cooldown(
            db_session,
            account.id,
            "enrichment_failed:critical_cookies_not_generated",
            cooldown_minutes=360,
            category=account_service_module.RUNTIME_CATEGORY_COOKIE_ENRICHMENT,
        )

        db_session.refresh(account)
        payload = json.loads(account.last_error)
        cooldown_until = datetime.fromisoformat(payload["cooldown_until"])
        assert payload["scope"] == "runtime"
        assert payload["category"] == account_service_module.RUNTIME_CATEGORY_COOKIE_ENRICHMENT
        assert payload["reason"] == "enrichment_failed:critical_cookies_not_generated"
        assert cooldown_until > datetime.now(timezone.utc) + timedelta(minutes=350)
        assert account.last_used_at is not None

    def test_mark_needs_reauth_updates_last_used_for_rotation(self, db_session: Session):
        account = Account(
            label="runtime_reauth_rotation",
            status="active",
            cookies="sessionid=runtime_reauth",
        )
        db_session.add(account)
        db_session.commit()

        mark_needs_reauth(db_session, account.id, "dead_session_detected")

        db_session.refresh(account)
        assert account.status == "reauth_needed"
        assert account.last_used_at is not None

    def test_get_free_account_for_reply_skips_login_captcha_runtime_cooldown(self, db_session: Session):
        cooldown_until = (datetime.now(timezone.utc) + timedelta(minutes=20)).isoformat()
        cooled = Account(
            label="reply_login_captcha_cooldown",
            status="active",
            cookies="sessionid=reply-captcha",
            last_error=json.dumps(
                {
                    "scope": "runtime",
                    "category": "captcha",
                    "reason": "captcha_challenge:slider",
                    "cooldown_until": cooldown_until,
                }
            ),
        )
        healthy = Account(
            label="reply_login_captcha_healthy",
            status="active",
            cookies="sessionid=reply-healthy",
            last_validation_ok=datetime.now(timezone.utc),
        )
        db_session.add_all([cooled, healthy])
        db_session.commit()

        account = get_free_account_for_reply(db_session)
        assert account is not None
        assert account.id == healthy.id

    def test_get_free_account_for_reply_can_skip_degraded_captcha_category(self, db_session: Session):
        degraded = Account(
            label="reply_degraded_captcha",
            status="active",
            cookies="sessionid=reply-degraded-captcha",
            last_error=json.dumps(
                {
                    "scope": "runtime",
                    "category": "captcha",
                    "reason": "captcha_challenge:slider",
                }
            ),
        )
        healthy = Account(
            label="reply_degraded_captcha_healthy",
            status="active",
            cookies="sessionid=reply-healthy-after-degraded",
            last_validation_ok=datetime.now(timezone.utc),
        )
        db_session.add_all([degraded, healthy])
        db_session.commit()

        account = get_free_account_for_reply(
            db_session,
            exclude_degraded_categories={"captcha", "page_blocked"},
        )

        assert account is not None
        assert account.id == healthy.id

    def test_get_free_account_for_reply_uses_excluded_degraded_as_last_resort(self, db_session: Session):
        degraded = Account(
            label="reply_degraded_captcha_last_resort",
            status="active",
            cookies="sessionid=reply-degraded-captcha-last-resort",
            last_error=json.dumps(
                {
                    "scope": "runtime",
                    "category": "captcha",
                    "reason": "captcha_challenge:slider",
                }
            ),
        )
        db_session.add(degraded)
        db_session.commit()

        account = get_free_account_for_reply(
            db_session,
            exclude_degraded_categories={"captcha", "page_blocked"},
        )

        assert account is not None
        assert account.id == degraded.id

    def test_main_selector_keeps_no_proxy_account_when_excluding_other_proxy(self, db_session: Session):
        account = Account(
            label="main_no_proxy_with_exclude_proxy",
            status="active",
            cookies=json.dumps(
                {
                    "sessionid": "main-no-proxy-session",
                    "sid_tt": "main-no-proxy-sid",
                    "s_v_web_id": "main-no-proxy-verify",
                    "ttwid": "main-no-proxy-wid",
                    "msToken": "main-no-proxy-ms",
                }
            ),
            proxy=None,
            last_validation_ok=datetime.now(timezone.utc),
        )
        db_session.add(account)
        db_session.commit()

        selected = get_free_account_for_main_comment(
            db_session,
            exclude_proxies={"http://already-used-proxy"},
        )

        assert selected is not None
        assert selected.id == account.id

    def test_reply_selector_keeps_no_proxy_account_when_excluding_other_proxy(self, db_session: Session):
        account = Account(
            label="reply_no_proxy_with_exclude_proxy",
            status="active",
            cookies="sessionid=reply-no-proxy-session; sid_tt=reply-sid",
            proxy=None,
            last_validation_ok=datetime.now(timezone.utc),
        )
        db_session.add(account)
        db_session.commit()

        selected = get_free_account_for_reply(
            db_session,
            exclude_proxies={"http://already-used-proxy"},
        )

        assert selected is not None
        assert selected.id == account.id

    def test_reply_last_resort_proxy_type_filter_tries_alternate_bucket(
        self,
        db_session: Session,
        monkeypatch: pytest.MonkeyPatch,
    ):
        monkeypatch.setattr(account_service_module, "get_proxy_type_filter", lambda _db: "mobile")
        residential_proxy = "http://reply-residential-last-resort"
        mobile_proxy = "http://reply-mobile-sid-last-resort"
        db_session.add_all(
            [
                Proxy(proxy=residential_proxy, is_mobile="N"),
                Proxy(proxy=mobile_proxy, is_mobile="Y"),
            ]
        )
        runtime_wrong_proxy = Account(
            label="reply_runtime_last_resort_wrong_proxy_type",
            status="active",
            cookies="sessionid=runtime-wrong-proxy; sid_tt=runtime-sid",
            proxy=residential_proxy,
            last_error=json.dumps(
                {
                    "scope": "runtime",
                    "category": "captcha",
                    "reason": "captcha_challenge:slider",
                }
            ),
        )
        sid_guard_mobile = Account(
            label="reply_sid_guard_last_resort_mobile",
            status="active",
            cookies="sid_guard=reply-sid-guard; msToken=reply-ms",
            proxy=mobile_proxy,
            last_validation_ok=datetime.now(timezone.utc),
        )
        db_session.add_all([runtime_wrong_proxy, sid_guard_mobile])
        db_session.commit()

        selected = get_free_account_for_reply(db_session)

        assert selected is not None
        assert selected.id == sid_guard_mobile.id

    def test_runtime_health_grade_classifies_recent_runtime_categories(self, db_session: Session):
        account = Account(
            label="runtime_health_grade_login",
            status="active",
            cookies="sessionid=health-grade",
            last_error=json.dumps(
                {
                    "scope": "runtime",
                    "category": account_service_module.MONITOR_CATEGORY_LOGIN,
                    "reason": "login required during comment publish",
                }
            ),
        )

        state = account_service_module._read_runtime_state(account, datetime.now(timezone.utc))

        assert account_service_module._runtime_health_grade(state) == "login_ui_recent"

    def test_page_loading_ambiguous_is_not_classified_as_page_blocked(self):
        reason = "login_ambiguous:page_loading:tiktok_shell_incomplete:on_video_no_login_btn:video"

        assert account_service_module._infer_runtime_category(reason) == account_service_module.MONITOR_CATEGORY_NETWORK
        assert account_service_module._looks_like_runtime_error(reason) is True

    def test_edge_access_denied_is_classified_as_proxy_not_login_blocked(self):
        reason = "proxy_edge_access_denied:err_http_response_code_failure"

        assert account_service_module._infer_runtime_category(reason) == account_service_module.MONITOR_CATEGORY_PROXY
        assert account_service_module._looks_like_runtime_error(reason) is True
        assert (
            account_service_module._infer_runtime_category("ERR_HTTP_RESPONSE_CODE_FAILURE")
            == account_service_module.MONITOR_CATEGORY_PROXY
        )
        assert account_service_module._looks_like_runtime_error("ERR_HTTP_RESPONSE_CODE_FAILURE") is True

    def test_stored_goto_failed_page_blocked_is_reclassified_as_network(self):
        account = Account(
            label="runtime_old_goto_failed_blocked",
            status="active",
            cookies="sessionid=old-goto",
            last_error=json.dumps(
                {
                    "scope": "runtime",
                    "category": account_service_module.MONITOR_CATEGORY_BLOCKED,
                    "reason": 'login_ambiguous:goto_failed:Page.goto: Timeout 120000ms exceeded.',
                }
            ),
        )

        state = account_service_module._read_runtime_state(account, datetime.now(timezone.utc))

        assert state["category"] == account_service_module.MONITOR_CATEGORY_NETWORK
        assert account_service_module._runtime_health_grade(state) == "risky"

    def test_get_free_account_for_main_comment_prefers_healthy_over_recent_captcha(
        self,
        db_session: Session,
    ):
        degraded = Account(
            label="main_recent_captcha_degraded",
            status="active",
            cookies='{"sessionid":"main-captcha","sid_tt":"sid-captcha","s_v_web_id":"verify-captcha","ttwid":"wid-captcha","msToken":"ms-captcha"}',
            last_error=json.dumps(
                {
                    "scope": "runtime",
                    "category": account_service_module.MONITOR_CATEGORY_CAPTCHA,
                    "reason": "captcha_challenge:slider",
                }
            ),
            created_at=datetime.now(timezone.utc) - timedelta(hours=4),
            last_validation_ok=datetime.now(timezone.utc),
        )
        healthy = Account(
            label="main_recent_captcha_healthy",
            status="active",
            cookies='{"sessionid":"main-healthy","sid_tt":"sid-healthy","s_v_web_id":"verify-healthy","ttwid":"wid-healthy","msToken":"ms-healthy"}',
            created_at=datetime.now(timezone.utc) - timedelta(hours=4),
            last_validation_ok=datetime.now(timezone.utc),
        )
        db_session.add_all([degraded, healthy])
        db_session.commit()

        account = get_free_account_for_main_comment(db_session)

        assert account is not None
        assert account.id == healthy.id

    def test_get_free_account_for_reply_uses_recent_login_as_last_resort(self, db_session: Session):
        degraded = Account(
            label="reply_recent_login_last_resort",
            status="active",
            cookies='{"sessionid":"reply-login","sid_tt":"sid-login","s_v_web_id":"verify-login","ttwid":"wid-login","msToken":"ms-login"}',
            last_error=json.dumps(
                {
                    "scope": "runtime",
                    "category": account_service_module.MONITOR_CATEGORY_LOGIN,
                    "reason": "login required during reply",
                }
            ),
            last_validation_ok=datetime.now(timezone.utc),
        )
        db_session.add(degraded)
        db_session.commit()

        account = get_free_account_for_reply(db_session)

        assert account is not None
        assert account.id == degraded.id

    def test_get_free_account_for_main_comment_skips_runtime_cooldown(self, db_session: Session):
        """Main-comment selector should skip accounts that are still in runtime cooldown."""
        cooldown_until = (datetime.now(timezone.utc) + timedelta(minutes=20)).isoformat()
        cooled = Account(
            label="main_runtime_cooldown",
            status="active",
            cookies="sessionid=main1",
            last_error=json.dumps(
                {
                    "scope": "runtime",
                    "category": "captcha",
                    "reason": "CAPTCHA during comment publish",
                    "cooldown_until": cooldown_until,
                }
            ),
        )
        healthy = Account(
            label="main_runtime_healthy",
            status="active",
            cookies="sessionid=main2",
            last_validation_ok=datetime.now(timezone.utc),
        )
        db_session.add_all([cooled, healthy])
        db_session.commit()

        account = get_free_account_for_main_comment(db_session)
        assert account is not None
        assert account.id == healthy.id

    def test_get_free_account_for_reply_skips_runtime_cooldown(self, db_session: Session):
        """Reply selector should skip accounts in active runtime cooldown."""
        cooldown_until = (datetime.now(timezone.utc) + timedelta(minutes=15)).isoformat()
        cooled = Account(
            label="reply_runtime_cooldown",
            status="active",
            cookies="sessionid=reply1",
            proxy="http://reply-proxy-a",
            last_error=json.dumps(
                {
                    "scope": "runtime",
                    "category": "rate_limit",
                    "reason": "Rate limited while sending reply",
                    "cooldown_until": cooldown_until,
                }
            ),
        )
        healthy = Account(
            label="reply_runtime_healthy",
            status="active",
            cookies="sessionid=reply2",
            proxy="http://reply-proxy-b",
            last_validation_ok=datetime.now(timezone.utc),
        )
        db_session.add_all([cooled, healthy])
        db_session.commit()

        account = get_free_account_for_reply(db_session)
        assert account is not None
        assert account.id == healthy.id

    def test_get_account_runtime_health_reports_scope_summaries(self, db_session: Session):
        """Runtime health endpoint should expose monitor/main/reply pool snapshots."""
        cooldown_until = (datetime.now(timezone.utc) + timedelta(minutes=10)).isoformat()
        accounts = [
            Account(
                label="runtime_health_healthy",
                status="active",
                cookies="sessionid=health1",
                proxy="http://health-proxy-a",
                last_validation_ok=datetime.now(timezone.utc),
            ),
            Account(
                label="runtime_health_runtime_issue",
                status="active",
                cookies="sessionid=health2",
                proxy="http://health-proxy-b",
                last_error=json.dumps(
                    {
                        "scope": "runtime",
                        "category": "proxy_issue",
                        "reason": "Proxy timeout during comment publish",
                        "cooldown_until": cooldown_until,
                    }
                ),
            ),
            Account(
                label="runtime_health_monitor_issue",
                status="active",
                cookies="sessionid=health3",
                proxy="http://health-proxy-c",
                last_error=json.dumps(
                    {
                        "scope": "monitor",
                        "category": "captcha",
                        "reason": "CAPTCHA detected during video parsing",
                        "cooldown_until": cooldown_until,
                    }
                ),
            ),
            Account(
                label="runtime_health_recent_captcha",
                status="active",
                cookies="sessionid=health4",
                proxy="http://health-proxy-d",
                last_error=json.dumps(
                    {
                        "scope": "runtime",
                        "category": "captcha",
                        "reason": "captcha_challenge:slider",
                    }
                ),
            ),
        ]
        db_session.add_all(accounts)
        db_session.commit()

        health = get_account_runtime_health(db_session)
        assert set(health.keys()) >= {"generated_at", "main_comment", "reply", "monitor"}
        assert health["main_comment"]["total_candidates"] == 4
        assert health["reply"]["cooldown"] >= 1
        assert health["monitor"]["cooldown"] >= 1
        assert health["main_comment"]["captcha_recent"] >= 1
        assert "runtime_risky" in health["main_comment"]
        assert health["monitor"]["captcha_recent"] == 0

    def test_get_free_account_for_main_comment_skips_bad_proxy(self, db_session: Session, monkeypatch: pytest.MonkeyPatch):
        """Main selector should skip accounts whose proxy is currently marked bad."""
        monkeypatch.setattr(account_service_module.settings, "USE_PROXY", True, raising=False)
        bad_proxy_account = Account(
            label="main_bad_proxy",
            status="active",
            cookies="sessionid=proxybad1",
            proxy="http://bad-main-proxy",
        )
        healthy_account = Account(
            label="main_good_proxy",
            status="active",
            cookies="sessionid=proxygood1",
            proxy="http://good-main-proxy",
        )
        db_session.add_all([bad_proxy_account, healthy_account])
        db_session.commit()

        monkeypatch.setattr(
            account_service_module,
            "is_proxy_bad",
            lambda proxy: proxy == "http://bad-main-proxy",
        )

        account = get_free_account_for_main_comment(db_session)
        assert account is not None
        assert account.id == healthy_account.id

    def test_get_free_account_for_main_comment_excludes_proxy_for_current_batch(
        self,
        db_session: Session,
        monkeypatch: pytest.MonkeyPatch,
    ):
        """Parallel main selection avoids the exact proxy endpoint already picked in the batch."""
        monkeypatch.setattr(account_service_module.settings, "USE_PROXY", True, raising=False)
        monkeypatch.setattr(account_service_module.settings, "PARALLEL_PROXY_DIVERSIFY_BY_SOURCE_HOST", False, raising=False)
        first_proxy = "http://user:pass@10.20.30.40:1000"
        same_source_proxy = "http://other:pass@10.20.30.40:2000"
        second_proxy = "http://main-batch-proxy-b"
        first_account = Account(
            label="main_batch_proxy_a",
            status="active",
            cookies='{"sessionid":"main-a","sid_tt":"main-a-sid","s_v_web_id":"main-a-verify","ttwid":"main-a-wid","msToken":"main-a-ms"}',
            proxy=first_proxy,
            created_at=datetime.now(timezone.utc) - timedelta(hours=4),
            last_validation_ok=datetime.now(timezone.utc),
        )
        same_source_account = Account(
            label="main_batch_proxy_same_source",
            status="active",
            cookies='{"sessionid":"main-same","sid_tt":"main-same-sid","s_v_web_id":"main-same-verify","ttwid":"main-same-wid","msToken":"main-same-ms"}',
            proxy=same_source_proxy,
            created_at=datetime.now(timezone.utc) - timedelta(hours=4),
            last_validation_ok=datetime.now(timezone.utc),
        )
        second_account = Account(
            label="main_batch_proxy_b",
            status="active",
            cookies='{"sessionid":"main-b","sid_tt":"main-b-sid","s_v_web_id":"main-b-verify","ttwid":"main-b-wid","msToken":"main-b-ms"}',
            proxy=second_proxy,
            created_at=datetime.now(timezone.utc) - timedelta(hours=4),
            last_validation_ok=datetime.now(timezone.utc),
        )
        db_session.add_all([first_account, same_source_account, second_account])
        db_session.commit()

        selected = get_free_account_for_main_comment(
            db_session,
            exclude_proxies={first_proxy},
        )

        assert selected is not None
        assert selected.id == same_source_account.id

    def test_get_free_account_for_main_comment_restricts_to_allowed_proxies(
        self,
        db_session: Session,
        monkeypatch: pytest.MonkeyPatch,
    ):
        """Strict 6x1 wave 2 must only reuse the exact proxies that were rotated."""
        monkeypatch.setattr(account_service_module.settings, "USE_PROXY", True, raising=False)
        monkeypatch.setattr(account_service_module.settings, "PARALLEL_PROXY_DIVERSIFY_BY_SOURCE_HOST", False, raising=False)
        rotated_proxy = "http://user:pass@10.30.40.50:1000"
        other_proxy = "http://user:pass@10.30.40.51:1000"
        rotated_account = Account(
            label="restrict_rotated",
            status="active",
            cookies='{"sessionid":"restrict-a","sid_tt":"restrict-a-sid","s_v_web_id":"restrict-a-verify","ttwid":"restrict-a-wid","msToken":"restrict-a-ms"}',
            proxy=rotated_proxy,
            created_at=datetime.now(timezone.utc) - timedelta(hours=4),
            last_validation_ok=datetime.now(timezone.utc),
        )
        other_account = Account(
            label="restrict_other",
            status="active",
            cookies='{"sessionid":"restrict-b","sid_tt":"restrict-b-sid","s_v_web_id":"restrict-b-verify","ttwid":"restrict-b-wid","msToken":"restrict-b-ms"}',
            proxy=other_proxy,
            created_at=datetime.now(timezone.utc) - timedelta(hours=4),
            last_validation_ok=datetime.now(timezone.utc),
        )
        db_session.add_all([rotated_account, other_account])
        db_session.commit()

        selected = get_free_account_for_main_comment(
            db_session,
            restrict_to_proxies={rotated_proxy},
        )
        assert selected is not None
        assert selected.id == rotated_account.id

        # When only the non-rotated proxy is reachable, selection must fail
        # rather than drift to an endpoint that was never rotated.
        blocked = get_free_account_for_main_comment(
            db_session,
            exclude_ids=[rotated_account.id],
            restrict_to_proxies={rotated_proxy},
        )
        assert blocked is None

    def test_get_free_account_for_main_comment_can_exclude_same_source_host(
        self,
        db_session: Session,
        monkeypatch: pytest.MonkeyPatch,
    ):
        """A stricter opt-in mode can avoid all endpoints on the same proxy host."""
        monkeypatch.setattr(account_service_module.settings, "USE_PROXY", True, raising=False)
        monkeypatch.setattr(account_service_module.settings, "PARALLEL_PROXY_DIVERSIFY_BY_SOURCE_HOST", True, raising=False)
        first_proxy = "http://user:pass@10.20.30.41:1000"
        same_source_proxy = "http://other:pass@10.20.30.41:2000"
        second_proxy = "http://main-batch-proxy-c"
        first_account = Account(
            label="main_batch_proxy_host_a",
            status="active",
            cookies='{"sessionid":"host-main-a","sid_tt":"host-main-a-sid","s_v_web_id":"host-main-a-verify","ttwid":"host-main-a-wid","msToken":"host-main-a-ms"}',
            proxy=first_proxy,
            created_at=datetime.now(timezone.utc) - timedelta(hours=4),
            last_validation_ok=datetime.now(timezone.utc),
        )
        same_source_account = Account(
            label="main_batch_proxy_host_same_source",
            status="active",
            cookies='{"sessionid":"host-main-same","sid_tt":"host-main-same-sid","s_v_web_id":"host-main-same-verify","ttwid":"host-main-same-wid","msToken":"host-main-same-ms"}',
            proxy=same_source_proxy,
            created_at=datetime.now(timezone.utc) - timedelta(hours=4),
            last_validation_ok=datetime.now(timezone.utc),
        )
        second_account = Account(
            label="main_batch_proxy_host_c",
            status="active",
            cookies='{"sessionid":"host-main-c","sid_tt":"host-main-c-sid","s_v_web_id":"host-main-c-verify","ttwid":"host-main-c-wid","msToken":"host-main-c-ms"}',
            proxy=second_proxy,
            created_at=datetime.now(timezone.utc) - timedelta(hours=4),
            last_validation_ok=datetime.now(timezone.utc),
        )
        db_session.add_all([first_account, same_source_account, second_account])
        db_session.commit()

        selected = get_free_account_for_main_comment(
            db_session,
            exclude_proxies={first_proxy},
        )

        assert selected is not None
        assert selected.id == second_account.id

    def test_get_free_account_for_main_comment_can_exclude_only_exact_proxy_when_balancing(
        self,
        db_session: Session,
        monkeypatch: pytest.MonkeyPatch,
    ):
        """Least-used fallback may balance ports on the same source host."""
        monkeypatch.setattr(account_service_module.settings, "USE_PROXY", True, raising=False)
        monkeypatch.setattr(account_service_module.settings, "PARALLEL_PROXY_DIVERSIFY_BY_SOURCE_HOST", True, raising=False)
        first_proxy = "http://user:pass@10.20.30.43:1000"
        same_source_proxy = "http://other:pass@10.20.30.43:2000"
        first_account = Account(
            label="main_balance_exact_a",
            status="active",
            cookies='{"sessionid":"balance-main-a","sid_tt":"balance-main-a-sid","s_v_web_id":"balance-main-a-verify","ttwid":"balance-main-a-wid","msToken":"balance-main-a-ms"}',
            proxy=first_proxy,
            created_at=datetime.now(timezone.utc) - timedelta(hours=4),
            last_validation_ok=datetime.now(timezone.utc),
        )
        same_source_account = Account(
            label="main_balance_exact_same_source",
            status="active",
            cookies='{"sessionid":"balance-main-same","sid_tt":"balance-main-same-sid","s_v_web_id":"balance-main-same-verify","ttwid":"balance-main-same-wid","msToken":"balance-main-same-ms"}',
            proxy=same_source_proxy,
            created_at=datetime.now(timezone.utc) - timedelta(hours=4),
            last_validation_ok=datetime.now(timezone.utc),
        )
        db_session.add_all([first_account, same_source_account])
        db_session.commit()

        selected = get_free_account_for_main_comment(
            db_session,
            exclude_proxies={first_proxy},
            exclude_proxy_source_hosts=False,
        )

        assert selected is not None
        assert selected.id == same_source_account.id

    def test_get_free_account_for_main_comment_scans_past_first_host_filtered_batch(
        self,
        db_session: Session,
        monkeypatch: pytest.MonkeyPatch,
    ):
        """Host diversification should not return None just because the first SQL page shares one host."""
        monkeypatch.setattr(account_service_module.settings, "USE_PROXY", True, raising=False)
        monkeypatch.setattr(account_service_module.settings, "PARALLEL_PROXY_DIVERSIFY_BY_SOURCE_HOST", True, raising=False)
        monkeypatch.setattr(account_service_module.settings, "MAX_CONCURRENT_BROWSERS", 5, raising=False)
        excluded_proxy = "http://user:pass@10.20.30.42:1000"
        same_host_accounts = [
            Account(
                label=f"main_same_host_page_{idx:02d}",
                status="active",
                cookies=f'{{"sessionid":"same-{idx}","sid_tt":"same-sid-{idx}","s_v_web_id":"same-verify-{idx}","ttwid":"same-wid-{idx}","msToken":"same-ms-{idx}"}}',
                proxy=f"http://other:pass@10.20.30.42:{2000 + idx}",
                created_at=datetime.now(timezone.utc) - timedelta(hours=4),
                last_validation_ok=datetime.now(timezone.utc),
            )
            for idx in range(20)
        ]
        target_account = Account(
            label="main_different_host_after_first_page",
            status="active",
            cookies='{"sessionid":"target-main","sid_tt":"target-main-sid","s_v_web_id":"target-main-verify","ttwid":"target-main-wid","msToken":"target-main-ms"}',
            proxy="http://other:pass@10.20.30.99:3000",
            created_at=datetime.now(timezone.utc) - timedelta(hours=4),
            last_validation_ok=datetime.now(timezone.utc),
        )
        db_session.add_all([*same_host_accounts, target_account])
        db_session.commit()

        selected = get_free_account_for_main_comment(
            db_session,
            exclude_proxies={excluded_proxy},
        )

        assert selected is not None
        assert selected.id == target_account.id

    def test_get_free_account_for_reply_excludes_proxy_for_current_batch(
        self,
        db_session: Session,
        monkeypatch: pytest.MonkeyPatch,
    ):
        """Parallel reply selection avoids the exact proxy endpoint already picked in the batch."""
        monkeypatch.setattr(account_service_module.settings, "USE_PROXY", True, raising=False)
        monkeypatch.setattr(account_service_module.settings, "PARALLEL_PROXY_DIVERSIFY_BY_SOURCE_HOST", False, raising=False)
        first_proxy = "http://user:pass@10.20.30.50:1000"
        same_source_proxy = "http://other:pass@10.20.30.50:2000"
        second_proxy = "http://reply-batch-proxy-b"
        first_account = Account(
            label="reply_batch_proxy_a",
            status="active",
            cookies='{"sessionid":"reply-a","sid_tt":"reply-a-sid","s_v_web_id":"reply-a-verify","ttwid":"reply-a-wid","msToken":"reply-a-ms"}',
            proxy=first_proxy,
            created_at=datetime.now(timezone.utc) - timedelta(hours=66),
            last_validation_ok=datetime.now(timezone.utc),
        )
        same_source_account = Account(
            label="reply_batch_proxy_same_source",
            status="active",
            cookies='{"sessionid":"reply-same","sid_tt":"reply-same-sid","s_v_web_id":"reply-same-verify","ttwid":"reply-same-wid","msToken":"reply-same-ms"}',
            proxy=same_source_proxy,
            created_at=datetime.now(timezone.utc) - timedelta(hours=66),
            last_validation_ok=datetime.now(timezone.utc),
        )
        second_account = Account(
            label="reply_batch_proxy_b",
            status="active",
            cookies='{"sessionid":"reply-b","sid_tt":"reply-b-sid","s_v_web_id":"reply-b-verify","ttwid":"reply-b-wid","msToken":"reply-b-ms"}',
            proxy=second_proxy,
            created_at=datetime.now(timezone.utc) - timedelta(hours=66),
            last_validation_ok=datetime.now(timezone.utc),
        )
        db_session.add_all([first_account, same_source_account, second_account])
        db_session.commit()

        selected = get_free_account_for_reply(
            db_session,
            exclude_proxies={first_proxy},
        )

        assert selected is not None
        assert selected.id == same_source_account.id

    def test_get_free_account_for_reply_can_exclude_same_source_host(
        self,
        db_session: Session,
        monkeypatch: pytest.MonkeyPatch,
    ):
        """A stricter opt-in mode can avoid all reply endpoints on the same proxy host."""
        monkeypatch.setattr(account_service_module.settings, "USE_PROXY", True, raising=False)
        monkeypatch.setattr(account_service_module.settings, "PARALLEL_PROXY_DIVERSIFY_BY_SOURCE_HOST", True, raising=False)
        first_proxy = "http://user:pass@10.20.30.51:1000"
        same_source_proxy = "http://other:pass@10.20.30.51:2000"
        second_proxy = "http://reply-batch-proxy-c"
        first_account = Account(
            label="reply_batch_proxy_host_a",
            status="active",
            cookies='{"sessionid":"host-reply-a","sid_tt":"host-reply-a-sid","s_v_web_id":"host-reply-a-verify","ttwid":"host-reply-a-wid","msToken":"host-reply-a-ms"}',
            proxy=first_proxy,
            created_at=datetime.now(timezone.utc) - timedelta(hours=66),
            last_validation_ok=datetime.now(timezone.utc),
        )
        same_source_account = Account(
            label="reply_batch_proxy_host_same_source",
            status="active",
            cookies='{"sessionid":"host-reply-same","sid_tt":"host-reply-same-sid","s_v_web_id":"host-reply-same-verify","ttwid":"host-reply-same-wid","msToken":"host-reply-same-ms"}',
            proxy=same_source_proxy,
            created_at=datetime.now(timezone.utc) - timedelta(hours=66),
            last_validation_ok=datetime.now(timezone.utc),
        )
        second_account = Account(
            label="reply_batch_proxy_host_c",
            status="active",
            cookies='{"sessionid":"host-reply-c","sid_tt":"host-reply-c-sid","s_v_web_id":"host-reply-c-verify","ttwid":"host-reply-c-wid","msToken":"host-reply-c-ms"}',
            proxy=second_proxy,
            created_at=datetime.now(timezone.utc) - timedelta(hours=66),
            last_validation_ok=datetime.now(timezone.utc),
        )
        db_session.add_all([first_account, same_source_account, second_account])
        db_session.commit()

        selected = get_free_account_for_reply(
            db_session,
            exclude_proxies={first_proxy},
        )

        assert selected is not None
        assert selected.id == second_account.id

    def test_get_free_account_for_reply_can_exclude_only_exact_proxy_when_balancing(
        self,
        db_session: Session,
        monkeypatch: pytest.MonkeyPatch,
    ):
        """Reply least-used fallback may balance ports on the same source host."""
        monkeypatch.setattr(account_service_module.settings, "USE_PROXY", True, raising=False)
        monkeypatch.setattr(account_service_module.settings, "PARALLEL_PROXY_DIVERSIFY_BY_SOURCE_HOST", True, raising=False)
        first_proxy = "http://user:pass@10.20.30.52:1000"
        same_source_proxy = "http://other:pass@10.20.30.52:2000"
        first_account = Account(
            label="reply_balance_exact_a",
            status="active",
            cookies='{"sessionid":"balance-reply-a","sid_tt":"balance-reply-a-sid","s_v_web_id":"balance-reply-a-verify","ttwid":"balance-reply-a-wid","msToken":"balance-reply-a-ms"}',
            proxy=first_proxy,
            created_at=datetime.now(timezone.utc) - timedelta(hours=66),
            last_validation_ok=datetime.now(timezone.utc),
        )
        same_source_account = Account(
            label="reply_balance_exact_same_source",
            status="active",
            cookies='{"sessionid":"balance-reply-same","sid_tt":"balance-reply-same-sid","s_v_web_id":"balance-reply-same-verify","ttwid":"balance-reply-same-wid","msToken":"balance-reply-same-ms"}',
            proxy=same_source_proxy,
            created_at=datetime.now(timezone.utc) - timedelta(hours=66),
            last_validation_ok=datetime.now(timezone.utc),
        )
        db_session.add_all([first_account, same_source_account])
        db_session.commit()

        selected = get_free_account_for_reply(
            db_session,
            exclude_proxies={first_proxy},
            exclude_proxy_source_hosts=False,
        )

        assert selected is not None
        assert selected.id == same_source_account.id

    def test_get_monitor_account_skips_bad_proxy(self, db_session: Session, monkeypatch: pytest.MonkeyPatch):
        """Monitor selector should avoid accounts backed by proxies already marked bad."""
        monkeypatch.setattr(account_service_module.settings, "USE_PROXY", True, raising=False)
        bad_proxy_account = Account(
            label="monitor_bad_proxy",
            status="active",
            cookies="sessionid=monitorbad1",
            proxy="http://bad-monitor-proxy",
        )
        healthy_account = Account(
            label="monitor_good_proxy",
            status="active",
            cookies="sessionid=monitorgood1",
            proxy="http://good-monitor-proxy",
            last_validation_ok=datetime.now(timezone.utc),
        )
        db_session.add_all([bad_proxy_account, healthy_account])
        db_session.commit()

        monkeypatch.setattr(
            account_service_module,
            "is_proxy_bad",
            lambda proxy: proxy == "http://bad-monitor-proxy",
        )

        account = get_monitor_account(db_session)
        assert account is not None
        assert account.id == healthy_account.id

    def test_get_monitor_account_excludes_failed_proxy_for_attempt(
        self,
        db_session: Session,
        monkeypatch: pytest.MonkeyPatch,
    ):
        """A rotation failure should exclude the whole proxy for the current monitor attempt."""
        monkeypatch.setattr(account_service_module.settings, "USE_PROXY", True, raising=False)
        proxy_a = Proxy(
            proxy="http://monitor-failed-mobile-proxy",
            is_mobile="Y",
            rotation_url="https://proxy.example/failed",
        )
        proxy_b = Proxy(
            proxy="http://monitor-next-mobile-proxy",
            is_mobile="Y",
            rotation_url="https://proxy.example/next",
        )
        failed_account_1 = Account(
            label="monitor_failed_proxy_1",
            status="active",
            cookies="sessionid=failed1",
            proxy=proxy_a.proxy,
            last_validation_ok=datetime.now(timezone.utc),
        )
        failed_account_2 = Account(
            label="monitor_failed_proxy_2",
            status="active",
            cookies="sessionid=failed2",
            proxy=proxy_a.proxy,
            last_validation_ok=datetime.now(timezone.utc),
        )
        next_account = Account(
            label="monitor_next_proxy",
            status="active",
            cookies="sessionid=nextproxy",
            proxy=proxy_b.proxy,
            last_validation_ok=datetime.now(timezone.utc) - timedelta(minutes=10),
        )
        db_session.add_all([proxy_a, proxy_b, failed_account_1, failed_account_2, next_account])
        db_session.commit()

        monkeypatch.setattr(account_service_module, "get_bad_proxy_set", lambda proxies: set())
        monkeypatch.setattr(account_service_module, "get_locked_proxy_ids", lambda proxy_ids: set())

        account = get_monitor_account(db_session, exclude_proxies={proxy_a.proxy})

        assert account is not None
        assert account.id == next_account.id

    def test_get_monitor_account_skips_locked_mobile_proxy(self, db_session: Session, monkeypatch: pytest.MonkeyPatch):
        """Monitor selector should avoid accounts on a mobile proxy that another worker already locked."""
        monkeypatch.setattr(account_service_module.settings, "USE_PROXY", True, raising=False)
        locked_proxy = Proxy(
            proxy="http://locked-mobile-proxy",
            is_mobile="Y",
            rotation_url="https://proxy.example/locked",
        )
        free_proxy = Proxy(
            proxy="http://free-mobile-proxy",
            is_mobile="Y",
            rotation_url="https://proxy.example/free",
        )
        locked_account = Account(
            label="monitor_locked_mobile",
            status="active",
            cookies="sessionid=lockedmobile",
            proxy=locked_proxy.proxy,
            last_validation_ok=datetime.now(timezone.utc),
        )
        free_account = Account(
            label="monitor_free_mobile",
            status="active",
            cookies="sessionid=freemobile",
            proxy=free_proxy.proxy,
            last_validation_ok=datetime.now(timezone.utc) - timedelta(minutes=5),
        )
        db_session.add_all([locked_proxy, free_proxy, locked_account, free_account])
        db_session.commit()

        monkeypatch.setattr(
            account_service_module,
            "get_locked_proxy_ids",
            lambda proxy_ids: {locked_proxy.id},
        )

        account = get_monitor_account(db_session)
        assert account is not None
        assert account.id == free_account.id

    def test_get_account_runtime_health_counts_unique_bad_proxies(self, db_session: Session, monkeypatch: pytest.MonkeyPatch):
        """Runtime health summary should count bad proxies once and exclude them from ready pool."""
        monkeypatch.setattr(account_service_module.settings, "USE_PROXY", True, raising=False)
        accounts = [
            Account(
                label="runtime_bad_proxy_1",
                status="active",
                cookies="sessionid=runtimebad1",
                proxy="http://shared-bad-proxy",
            ),
            Account(
                label="runtime_bad_proxy_2",
                status="active",
                cookies="sessionid=runtimebad2",
                proxy="http://shared-bad-proxy",
            ),
            Account(
                label="runtime_good_proxy",
                status="active",
                cookies="sessionid=runtimegood",
                proxy="http://healthy-runtime-proxy",
                last_validation_ok=datetime.now(timezone.utc),
            ),
        ]
        db_session.add_all(accounts)
        db_session.commit()

        monkeypatch.setattr(
            account_service_module,
            "is_proxy_bad",
            lambda proxy: proxy == "http://shared-bad-proxy",
        )
        monkeypatch.setattr(
            account_service_module,
            "get_bad_proxy_set",
            lambda proxies: {"http://shared-bad-proxy"},
        )

        health = get_account_runtime_health(db_session)
        assert health["main_comment"]["proxy_issue"] == 2
        assert health["main_comment"]["bad_proxies"] == 1
        assert health["main_comment"]["eligible_now"] == 1

    def test_get_free_account_for_main_comment_prefers_fresh_over_expired(self, db_session: Session):
        """Short-lived runtime should ignore clearly expired accounts when a fresh one exists."""
        expired = Account(
            label="expired_main_account",
            status="active",
            cookies='{"sessionid":"expired"}',
            created_at=datetime.now(timezone.utc) - timedelta(hours=96),
            last_validation_ok=datetime.now(timezone.utc) - timedelta(hours=18),
        )
        fresh = Account(
            label="fresh_main_account",
            status="active",
            cookies='{"sessionid":"fresh"}',
            created_at=datetime.now(timezone.utc) - timedelta(hours=6),
            last_validation_ok=datetime.now(timezone.utc) - timedelta(hours=1),
        )
        db_session.add_all([expired, fresh])
        db_session.commit()

        account = get_free_account_for_main_comment(db_session)
        assert account is not None
        assert account.id == fresh.id

    def test_get_monitor_account_skips_expired_without_recent_validation(self, db_session: Session):
        """Monitoring should not waste time on accounts older than the auth window without a fresh validation."""
        expired = Account(
            label="expired_monitor_account",
            status="active",
            cookies="sessionid=expired-monitor",
            proxy="http://expired-monitor-proxy",
            created_at=datetime.now(timezone.utc) - timedelta(hours=90),
            last_validation_ok=datetime.now(timezone.utc) - timedelta(hours=20),
        )
        healthy = Account(
            label="healthy_monitor_account",
            status="active",
            cookies="sessionid=healthy-monitor",
            proxy="http://healthy-monitor-proxy",
            created_at=datetime.now(timezone.utc) - timedelta(hours=10),
            last_validation_ok=datetime.now(timezone.utc) - timedelta(hours=1),
        )
        db_session.add_all([expired, healthy])
        db_session.commit()

        account = get_monitor_account(db_session)
        assert account is not None
        assert account.id == healthy.id

    def test_runtime_health_exposes_lifecycle_buckets(self, db_session: Session, monkeypatch: pytest.MonkeyPatch):
        """Runtime health should show how many accounts are fresh vs expiring vs expired."""
        monkeypatch.setattr(
            account_service_module.settings,
            "ACCOUNT_SHORT_LIVED_LIFECYCLE_BY_CREATED_AT",
            True,
            raising=False,
        )
        accounts = [
            Account(
                label="fresh_lifecycle_account",
                status="active",
                cookies="sessionid=fresh-life",
                proxy="http://life-proxy-a",
                created_at=datetime.now(timezone.utc) - timedelta(hours=5),
            ),
            Account(
                label="expiring_lifecycle_account",
                status="active",
                cookies="sessionid=expiring-life",
                proxy="http://life-proxy-b",
                created_at=datetime.now(timezone.utc) - timedelta(hours=66),
                last_validation_ok=datetime.now(timezone.utc) - timedelta(hours=2),
            ),
            Account(
                label="expired_lifecycle_account",
                status="active",
                cookies="sessionid=expired-life",
                proxy="http://life-proxy-c",
                created_at=datetime.now(timezone.utc) - timedelta(hours=96),
                last_validation_ok=datetime.now(timezone.utc) - timedelta(hours=16),
            ),
        ]
        db_session.add_all(accounts)
        db_session.commit()

        health = get_account_runtime_health(db_session)
        assert health["main_comment"]["fresh"] == 1
        assert health["main_comment"]["expiring"] >= 1
        assert health["main_comment"]["expired"] == 1

    def test_old_created_at_not_hard_expired_when_short_lived_disabled(
        self, db_session: Session, monkeypatch: pytest.MonkeyPatch
    ):
        """Imported long-lived cookies: DB row age must not empty the pool (regression #169)."""
        monkeypatch.setattr(
            account_service_module.settings,
            "ACCOUNT_SHORT_LIVED_LIFECYCLE_BY_CREATED_AT",
            False,
            raising=False,
        )
        old = Account(
            label="imported_pool_old_row",
            status="active",
            cookies='{"sessionid":"x","sid_tt":"y","s_v_web_id":"v","ttwid":"w","msToken":"m"}',
            created_at=datetime.now(timezone.utc) - timedelta(days=20),
            last_validation_ok=None,
        )
        db_session.add(old)
        db_session.commit()

        lc = account_service_module._read_account_lifecycle(old)
        assert lc["hard_expired"] is False
        assert lc["stage"] == account_service_module.ACCOUNT_LIFECYCLE_EXPIRING

        picked = get_free_account_for_main_comment(db_session)
        assert picked is not None
        assert picked.id == old.id

    def test_mark_success_resets_validation_fail_count(self, db_session: Session):
        """A confirmed success should heal the validation-fail streak too."""
        account = Account(
            label="validation_reset_account",
            status="error",
            cookies="sessionid=reset-me",
            error_count_recent=2,
            validation_fail_count=3,
            last_error="validation failed",
        )
        db_session.add(account)
        db_session.commit()

        mark_success(db_session, account.id)

        db_session.refresh(account)
        assert account.validation_fail_count == 0
        assert account.error_count_recent == 0
        assert account.status == "active"

    def test_main_selector_prefers_http_ready_session_over_session_only(self, db_session: Session):
        """Main-comment selection should favor richer HTTP-ready cookies over bare session-only sets."""
        weak = Account(
            label="weak_session_main_account",
            status="active",
            cookies='{"sessionid":"weak-main"}',
            proxy="http://main-proxy-weak",
            created_at=datetime.now(timezone.utc) - timedelta(hours=4),
            last_validation_ok=datetime.now(timezone.utc) - timedelta(hours=1),
        )
        strong = Account(
            label="strong_session_main_account",
            status="active",
            cookies='{"sessionid":"strong-main","sid_tt":"sid-main","s_v_web_id":"verify-main","ttwid":"wid-main","msToken":"mst-main"}',
            proxy="http://main-proxy-strong",
            created_at=datetime.now(timezone.utc) - timedelta(hours=4),
            last_validation_ok=datetime.now(timezone.utc) - timedelta(hours=1),
        )
        db_session.add_all([weak, strong])
        db_session.commit()

        selected = get_free_account_for_main_comment(db_session)
        assert selected is not None
        assert selected.id == strong.id

        weak_quality = account_service_module._read_account_session_quality(weak, account_service_module.MAIN_SCOPE)
        strong_quality = account_service_module._read_account_session_quality(strong, account_service_module.MAIN_SCOPE)
        assert weak_quality["bucket"] == "dom_only"
        assert strong_quality["bucket"] == "http_strong"

    def test_main_selector_scans_past_sid_guard_only_accounts(
        self,
        db_session: Session,
        monkeypatch: pytest.MonkeyPatch,
    ):
        """Passport-valid but browser-auth-missing accounts should not block deeper good candidates."""
        monkeypatch.setattr(account_service_module.settings, "MAX_CONCURRENT_BROWSERS", 1, raising=False)
        now = datetime.now(timezone.utc)
        weak_accounts = [
            Account(
                label=f"main_sid_guard_only_{idx}",
                status="active",
                cookies=json.dumps({"sid_guard": f"sg-{idx}", "msToken": f"ms-{idx}"}),
                proxy=f"http://main-sid-guard-proxy-{idx}",
                created_at=now - timedelta(hours=5),
                last_validation_ok=now - timedelta(minutes=20),
            )
            for idx in range(25)
        ]
        strong = Account(
            label="main_browser_auth_after_weak_batch",
            status="active",
            cookies=json.dumps(
                {
                    "sessionid": "main-strong-session",
                    "sid_tt": "main-strong-sid",
                    "sid_guard": "main-strong-guard",
                    "s_v_web_id": "main-strong-verify",
                    "ttwid": "main-strong-wid",
                    "msToken": "main-strong-ms",
                    "tt_chain_token": "main-strong-chain",
                }
            ),
            proxy="http://main-strong-after-weak-proxy",
            created_at=now - timedelta(hours=5),
            last_validation_ok=now - timedelta(minutes=20),
        )
        db_session.add_all([*weak_accounts, strong])
        db_session.commit()

        selected = get_free_account_for_main_comment(db_session)

        assert selected is not None
        assert selected.id == strong.id
        weak_quality = account_service_module._read_account_session_quality(
            weak_accounts[0],
            account_service_module.MAIN_SCOPE,
        )
        assert weak_quality["quality_reason"] == "no_browser_auth_sid_guard_only"

    def test_main_selector_scans_past_reserved_top_candidate_in_parallel_batch(
        self,
        db_session: Session,
        monkeypatch: pytest.MonkeyPatch,
    ):
        """A reserved top candidate must not collapse parallel main selection to one browser."""
        monkeypatch.setattr(account_service_module.settings, "MAX_CONCURRENT_BROWSERS", 1, raising=False)
        monkeypatch.setattr(account_service_module.settings, "PARALLEL_COMMENTING_TARGET_BROWSERS", 3, raising=False)
        monkeypatch.setattr(account_service_module.settings, "ALLOW_SID_GUARD_ONLY_PUBLISH_LAST_RESORT", False, raising=False)
        now = datetime.now(timezone.utc)
        first_strong = Account(
            label="main_reserved_top_candidate",
            status="active",
            cookies=json.dumps(
                {
                    "sessionid": "reserved-top-session",
                    "sid_tt": "reserved-top-sid",
                    "sid_guard": "reserved-top-guard",
                    "s_v_web_id": "reserved-top-verify",
                    "ttwid": "reserved-top-wid",
                    "msToken": "reserved-top-ms",
                }
            ),
            proxy="http://main-reserved-top-proxy",
            created_at=now - timedelta(hours=5),
            last_validation_ok=now - timedelta(minutes=20),
        )
        weak_prefix = [
            Account(
                label=f"main_reserved_race_weak_{idx}",
                status="active",
                cookies=json.dumps({"sid_guard": f"race-weak-sg-{idx}", "msToken": f"race-weak-ms-{idx}"}),
                proxy=f"http://main-reserved-race-weak-{idx}",
                created_at=now - timedelta(hours=5),
                last_validation_ok=now - timedelta(minutes=20),
            )
            for idx in range(19)
        ]
        second_strong = Account(
            label="main_second_candidate_after_reserved_top",
            status="active",
            cookies=json.dumps(
                {
                    "sessionid": "second-after-reserved-session",
                    "sid_tt": "second-after-reserved-sid",
                    "sid_guard": "second-after-reserved-guard",
                    "s_v_web_id": "second-after-reserved-verify",
                    "ttwid": "second-after-reserved-wid",
                    "msToken": "second-after-reserved-ms",
                }
            ),
            proxy="http://main-second-after-reserved-proxy",
            created_at=now - timedelta(hours=5),
            last_validation_ok=now - timedelta(minutes=20),
        )
        db_session.add_all([first_strong, *weak_prefix, second_strong])
        db_session.commit()

        reserve_calls: list[int] = []

        def fake_reserve_main_account(account_id: int, *_args, **_kwargs) -> bool:
            reserve_calls.append(int(account_id))
            return int(account_id) != first_strong.id

        monkeypatch.setattr(account_service_module, "reserve_main_account", fake_reserve_main_account)

        selected = get_free_account_for_main_comment(db_session)

        assert selected is not None
        assert selected.id == second_strong.id
        assert reserve_calls[0] == first_strong.id
        assert second_strong.id in reserve_calls

    def test_main_selector_scans_past_large_sid_guard_only_prefix(
        self,
        db_session: Session,
        monkeypatch: pytest.MonkeyPatch,
    ):
        """Large imported weak pools should not make main selection stop before a stronger account."""
        monkeypatch.setattr(account_service_module.settings, "MAX_CONCURRENT_BROWSERS", 1, raising=False)
        now = datetime.now(timezone.utc)
        weak_accounts = [
            Account(
                label=f"main_sid_guard_large_prefix_{idx}",
                status="active",
                cookies=json.dumps({"sid_guard": f"sg-large-{idx}", "msToken": f"ms-large-{idx}"}),
                proxy=f"http://main-large-weak-proxy-{idx}",
                created_at=now - timedelta(hours=5),
                last_validation_ok=now - timedelta(minutes=20),
            )
            for idx in range(825)
        ]
        strong = Account(
            label="main_browser_auth_after_large_weak_prefix",
            status="active",
            cookies=json.dumps(
                {
                    "sessionid": "main-large-strong-session",
                    "sid_tt": "main-large-strong-sid",
                    "sid_guard": "main-large-strong-guard",
                    "s_v_web_id": "main-large-strong-verify",
                    "ttwid": "main-large-strong-wid",
                    "msToken": "main-large-strong-ms",
                    "tt_chain_token": "main-large-strong-chain",
                }
            ),
            proxy="http://main-large-strong-proxy",
            created_at=now - timedelta(hours=5),
            last_validation_ok=now - timedelta(minutes=20),
        )
        db_session.add_all([*weak_accounts, strong])
        db_session.commit()

        selected = get_free_account_for_main_comment(db_session)

        assert selected is not None
        assert selected.id == strong.id

    def test_main_selector_scans_past_legacy_degraded_scan_cap(
        self,
        db_session: Session,
        monkeypatch: pytest.MonkeyPatch,
    ):
        """A large degraded prefix should not trigger runtime last-resort before the full main pool is scanned."""
        monkeypatch.setattr(account_service_module.settings, "MAX_CONCURRENT_BROWSERS", 1, raising=False)
        monkeypatch.setattr(account_service_module.settings, "ACCOUNT_SELECTION_MAX_MAIN_SCAN", 10000, raising=False)
        now = datetime.now(timezone.utc)
        degraded_accounts = [
            Account(
                label=f"main_degraded_large_prefix_{idx}",
                status="active",
                cookies=json.dumps(
                    {
                        "sessionid": f"degraded-session-{idx}",
                        "sid_tt": f"degraded-sid-{idx}",
                        "s_v_web_id": f"degraded-verify-{idx}",
                        "ttwid": f"degraded-wid-{idx}",
                        "msToken": f"degraded-ms-{idx}",
                    }
                ),
                proxy=f"http://main-degraded-prefix-proxy-{idx}",
                last_error=json.dumps(
                    {
                        "scope": "runtime",
                        "category": account_service_module.MONITOR_CATEGORY_CAPTCHA,
                        "reason": "captcha_challenge:slider",
                    }
                ),
                created_at=now - timedelta(hours=5),
                last_validation_ok=now - timedelta(minutes=20),
            )
            for idx in range(2000)
        ]
        strong = Account(
            label="main_healthy_after_degraded_legacy_cap",
            status="active",
            cookies=json.dumps(
                {
                    "sessionid": "main-healthy-after-degraded-session",
                    "sid_tt": "main-healthy-after-degraded-sid",
                    "sid_guard": "main-healthy-after-degraded-guard",
                    "s_v_web_id": "main-healthy-after-degraded-verify",
                    "ttwid": "main-healthy-after-degraded-wid",
                    "msToken": "main-healthy-after-degraded-ms",
                    "tt_chain_token": "main-healthy-after-degraded-chain",
                }
            ),
            proxy="http://main-healthy-after-degraded-proxy",
            created_at=now - timedelta(hours=5),
            last_validation_ok=now - timedelta(minutes=20),
        )
        db_session.add_all([*degraded_accounts, strong])
        db_session.commit()

        selected = get_free_account_for_main_comment(db_session)

        assert selected is not None
        assert selected.id == strong.id

    def test_main_selector_uses_sid_guard_only_as_last_resort_by_default(self, db_session: Session):
        """Main selection may use sid_guard-only accounts only when no stronger browser-auth accounts exist."""
        account = Account(
            label="main_sid_guard_only_last_resort",
            status="active",
            cookies=json.dumps({"sid_guard": "last-resort-guard", "msToken": "last-resort-ms"}),
            proxy="http://main-last-resort-proxy",
            created_at=datetime.now(timezone.utc) - timedelta(hours=5),
            last_validation_ok=datetime.now(timezone.utc) - timedelta(minutes=20),
        )
        db_session.add(account)
        db_session.commit()

        selected = get_free_account_for_main_comment(db_session)

        assert selected is not None
        assert selected.id == account.id

    def test_main_selector_can_disable_sid_guard_only_last_resort(
        self,
        db_session: Session,
        monkeypatch: pytest.MonkeyPatch,
    ):
        """The sid_guard-only fallback remains easy to disable if it proves noisy in production."""
        monkeypatch.setattr(
            account_service_module.settings,
            "ALLOW_SID_GUARD_ONLY_PUBLISH_LAST_RESORT",
            False,
            raising=False,
        )
        account = Account(
            label="main_sid_guard_only_last_resort_disabled",
            status="active",
            cookies=json.dumps({"sid_guard": "last-resort-guard", "msToken": "last-resort-ms"}),
            proxy="http://main-last-resort-proxy-disabled",
            created_at=datetime.now(timezone.utc) - timedelta(hours=5),
            last_validation_ok=datetime.now(timezone.utc) - timedelta(minutes=20),
        )
        db_session.add(account)
        db_session.commit()

        selected = get_free_account_for_main_comment(db_session)

        assert selected is None

    def test_reply_selector_scans_past_sid_guard_only_accounts(
        self,
        db_session: Session,
        monkeypatch: pytest.MonkeyPatch,
    ):
        """Reply selection should also prefer real browser-auth accounts beyond a weak first batch."""
        monkeypatch.setattr(account_service_module.settings, "MAX_CONCURRENT_BROWSERS", 1, raising=False)
        now = datetime.now(timezone.utc)
        weak_accounts = [
            Account(
                label=f"reply_sid_guard_only_{idx}",
                status="active",
                cookies=json.dumps({"sid_guard": f"reply-sg-{idx}", "msToken": f"reply-ms-{idx}"}),
                proxy=f"http://reply-sid-guard-proxy-{idx}",
                created_at=now - timedelta(hours=5),
                last_validation_ok=now - timedelta(minutes=20),
            )
            for idx in range(25)
        ]
        strong = Account(
            label="reply_browser_auth_after_weak_batch",
            status="active",
            cookies=json.dumps(
                {
                    "sessionid": "reply-strong-session",
                    "sid_tt": "reply-strong-sid",
                    "sid_guard": "reply-strong-guard",
                    "s_v_web_id": "reply-strong-verify",
                    "ttwid": "reply-strong-wid",
                    "msToken": "reply-strong-ms",
                    "tt_chain_token": "reply-strong-chain",
                }
            ),
            proxy="http://reply-strong-after-weak-proxy",
            created_at=now - timedelta(hours=5),
            last_validation_ok=now - timedelta(minutes=20),
        )
        db_session.add_all([*weak_accounts, strong])
        db_session.commit()

        selected = get_free_account_for_reply(db_session)

        assert selected is not None
        assert selected.id == strong.id

    def test_reply_selector_uses_sid_guard_only_as_last_resort_by_default(self, db_session: Session):
        """Reply selection may use sid_guard-only accounts only when no stronger browser-auth accounts exist."""
        account = Account(
            label="reply_sid_guard_only_last_resort",
            status="active",
            cookies=json.dumps({"sid_guard": "reply-last-resort-guard", "msToken": "reply-last-resort-ms"}),
            proxy="http://reply-last-resort-proxy",
            created_at=datetime.now(timezone.utc) - timedelta(hours=5),
            last_validation_ok=datetime.now(timezone.utc) - timedelta(minutes=20),
        )
        db_session.add(account)
        db_session.commit()

        selected = get_free_account_for_reply(db_session)

        assert selected is not None
        assert selected.id == account.id

    def test_reply_selector_reserve_skips_process_reserved_account(self, db_session: Session):
        """Reply reservation prevents parallel workers from selecting the same account."""
        owner_id = 99001
        now = datetime.now(timezone.utc)
        accounts = [
            Account(
                label=f"reply_reserved_{idx}",
                status="active",
                cookies=json.dumps(
                    {
                        "sessionid": f"reply-session-{idx}",
                        "sid_tt": f"reply-sid-{idx}",
                        "sid_guard": f"reply-guard-{idx}",
                        "s_v_web_id": f"reply-verify-{idx}",
                        "ttwid": f"reply-wid-{idx}",
                        "msToken": f"reply-ms-{idx}",
                    }
                ),
                owner_user_id=owner_id,
                created_at=now - timedelta(hours=5),
                last_validation_ok=now - timedelta(minutes=20),
            )
            for idx in range(2)
        ]
        db_session.add_all(accounts)
        db_session.commit()

        first = get_free_account_for_reply(db_session, pool_owner_user_id=owner_id, reserve=True)
        second = get_free_account_for_reply(db_session, pool_owner_user_id=owner_id, reserve=True)

        assert first is not None
        assert second is not None
        assert second.id != first.id
        account_service_module.release_reply_account_reservation(first.id)
        account_service_module.release_reply_account_reservation(second.id)

    def test_reply_selector_can_disable_sid_guard_only_last_resort(
        self,
        db_session: Session,
        monkeypatch: pytest.MonkeyPatch,
    ):
        """The sid_guard-only fallback remains easy to disable if it proves noisy in production."""
        monkeypatch.setattr(
            account_service_module.settings,
            "ALLOW_SID_GUARD_ONLY_PUBLISH_LAST_RESORT",
            False,
            raising=False,
        )
        account = Account(
            label="reply_sid_guard_only_last_resort_disabled",
            status="active",
            cookies=json.dumps({"sid_guard": "reply-last-resort-guard", "msToken": "reply-last-resort-ms"}),
            proxy="http://reply-last-resort-proxy-disabled",
            created_at=datetime.now(timezone.utc) - timedelta(hours=5),
            last_validation_ok=datetime.now(timezone.utc) - timedelta(minutes=20),
        )
        db_session.add(account)
        db_session.commit()

        selected = get_free_account_for_reply(db_session)

        assert selected is None

    def test_runtime_health_exposes_session_quality_buckets(self, db_session: Session):
        """Runtime health should surface session-quality mix, not just generic active/error buckets."""
        accounts = [
            Account(
                label="health_session_strong",
                status="active",
                cookies='{"sessionid":"health-strong","sid_tt":"sid-strong","s_v_web_id":"verify-strong","ttwid":"wid-strong","msToken":"mst-strong"}',
                proxy="http://health-session-proxy-a",
                last_validation_ok=datetime.now(timezone.utc),
            ),
            Account(
                label="health_session_dom_only",
                status="active",
                cookies='{"sessionid":"health-dom-only"}',
                proxy="http://health-session-proxy-b",
                last_validation_ok=datetime.now(timezone.utc),
            ),
            Account(
                label="health_session_monitor_weak",
                status="active",
                cookies='{"sid_tt":"health-monitor-weak"}',
                proxy="http://health-session-proxy-c",
                last_validation_ok=datetime.now(timezone.utc),
            ),
        ]
        db_session.add_all(accounts)
        db_session.commit()

        health = get_account_runtime_health(db_session)
        assert health["main_comment"]["session_strong"] >= 1
        assert health["main_comment"]["session_dom_only"] >= 1
        assert health["main_comment"]["avg_session_score"] > 0
        assert health["main_comment"]["top_session_buckets"]
        assert health["monitor"]["session_weak"] >= 1

    def test_monitor_pool_stats_skip_cookie_sets_without_auth_session(self, db_session: Session):
        """Monitor pool should not count non-auth cookie exports as ready-to-use sessions."""
        invalid = Account(
            label="monitor_invalid_cookie_shape",
            status="active",
            cookies='{"foo":"bar","s_v_web_id":"verify-only"}',
            proxy="http://monitor-invalid-proxy",
        )
        healthy = Account(
            label="monitor_valid_cookie_shape",
            status="active",
            cookies='{"sessionid":"monitor-valid","s_v_web_id":"verify-valid"}',
            proxy="http://monitor-valid-proxy",
            last_validation_ok=datetime.now(timezone.utc),
        )
        db_session.add_all([invalid, healthy])
        db_session.commit()

        stats = get_monitor_pool_stats(db_session)
        assert stats["eligible_now"] == 1

    def test_http_execution_strategy_prefers_dom_first_for_main_on_dom_only_session(self, db_session: Session):
        account = Account(
            label="dom_only_main_strategy",
            status="active",
            cookies='{"sessionid":"main-dom-only"}',
            proxy="http://main-dom-only-proxy",
            created_at=datetime.now(timezone.utc) - timedelta(hours=3),
            last_validation_ok=datetime.now(timezone.utc),
        )
        db_session.add(account)
        db_session.commit()

        strategy = account_service_module.get_http_execution_strategy(account, account_service_module.MAIN_SCOPE)

        assert strategy["strategy"] == "dom_first"
        assert strategy["session_bucket"] == "dom_only"

    def test_http_execution_strategy_prefers_http_cautious_for_reply_on_degraded_session(self, db_session: Session):
        account = Account(
            label="degraded_reply_strategy",
            status="active",
            cookies='{"sessionid":"reply-degraded","sid_tt":"sid-degraded"}',
            proxy="http://reply-degraded-proxy",
            created_at=datetime.now(timezone.utc) - timedelta(hours=5),
            last_validation_ok=datetime.now(timezone.utc),
        )
        db_session.add(account)
        db_session.commit()

        strategy = account_service_module.get_http_execution_strategy(account, account_service_module.REPLY_SCOPE)

        assert strategy["strategy"] == "http_cautious"
        assert strategy["session_bucket"] == "http_degraded"

    def test_runtime_health_counts_http_execution_strategies(self, db_session: Session):
        accounts = [
            Account(
                label="strategy_main_http",
                status="active",
                cookies='{"sessionid":"s1","sid_tt":"sid1","s_v_web_id":"verify1","ttwid":"wid1","msToken":"mst1"}',
                proxy="http://strategy-proxy-1",
                last_validation_ok=datetime.now(timezone.utc),
            ),
            Account(
                label="strategy_main_dom",
                status="active",
                cookies='{"sessionid":"s2"}',
                proxy="http://strategy-proxy-2",
                last_validation_ok=datetime.now(timezone.utc),
            ),
            Account(
                label="strategy_main_skip",
                status="dead",
                cookies='{"sessionid":"s3","sid_tt":"sid3","s_v_web_id":"verify3","ttwid":"wid3","msToken":"mst3"}',
                proxy="http://strategy-proxy-3",
            ),
        ]
        db_session.add_all(accounts)
        db_session.commit()

        health = get_account_runtime_health(db_session)

        assert health["main_comment"]["http_first"] >= 1
        assert health["main_comment"]["dom_first"] >= 1
        assert health["main_comment"]["skip_http"] >= 1

    def test_main_selector_prefers_keeper_lane_over_burner_lane(self, db_session: Session):
        burner = Account(
            label="main_burner_lane",
            status="active",
            cookies='{"sessionid":"burn-main","sid_tt":"burn-main-sid","s_v_web_id":"burn-main-verify","ttwid":"burn-main-wid","msToken":"burn-main-ms"}',
            proxy="http://main-burner-proxy",
            created_at=datetime.now(timezone.utc) - timedelta(hours=67),
            last_validation_ok=datetime.now(timezone.utc) - timedelta(hours=2),
        )
        keeper = Account(
            label="main_keeper_lane",
            status="active",
            cookies='{"sessionid":"keep-main","sid_tt":"keep-main-sid","s_v_web_id":"keep-main-verify","ttwid":"keep-main-wid","msToken":"keep-main-ms"}',
            proxy="http://main-keeper-proxy",
            created_at=datetime.now(timezone.utc) - timedelta(hours=5),
            last_validation_ok=datetime.now(timezone.utc) - timedelta(hours=1),
        )
        db_session.add_all([burner, keeper])
        db_session.commit()

        selected = get_free_account_for_main_comment(db_session)

        assert selected is not None
        assert selected.id == keeper.id
        assert account_service_module._read_account_spend_lane(keeper, account_service_module.MAIN_SCOPE)["lane"] == "keeper"
        assert account_service_module._read_account_spend_lane(burner, account_service_module.MAIN_SCOPE)["lane"] == "burner"

    def test_reply_selector_prefers_burner_lane_over_keeper_lane(self, db_session: Session):
        keeper = Account(
            label="reply_keeper_lane",
            status="active",
            cookies='{"sessionid":"keep-reply","sid_tt":"keep-reply-sid","s_v_web_id":"keep-reply-verify","ttwid":"keep-reply-wid","msToken":"keep-reply-ms"}',
            proxy="http://reply-keeper-proxy",
            created_at=datetime.now(timezone.utc) - timedelta(hours=5),
            last_validation_ok=datetime.now(timezone.utc) - timedelta(hours=1),
        )
        burner = Account(
            label="reply_burner_lane",
            status="active",
            cookies='{"sessionid":"burn-reply","sid_tt":"burn-reply-sid","s_v_web_id":"burn-reply-verify","ttwid":"burn-reply-wid","msToken":"burn-reply-ms"}',
            proxy="http://reply-burner-proxy",
            created_at=datetime.now(timezone.utc) - timedelta(hours=66),
            last_validation_ok=datetime.now(timezone.utc) - timedelta(hours=2),
        )
        db_session.add_all([keeper, burner])
        db_session.commit()

        selected = get_free_account_for_reply(db_session)

        assert selected is not None
        assert selected.id == burner.id
        assert account_service_module._read_account_spend_lane(keeper, account_service_module.REPLY_SCOPE)["lane"] == "keeper"
        assert account_service_module._read_account_spend_lane(burner, account_service_module.REPLY_SCOPE)["lane"] == "burner"

    def test_runtime_health_exposes_spend_lanes(self, db_session: Session):
        accounts = [
            Account(
                label="lane_keeper_main",
                status="active",
                cookies='{"sessionid":"lane-keeper","sid_tt":"lane-keeper-sid","s_v_web_id":"lane-keeper-verify","ttwid":"lane-keeper-wid","msToken":"lane-keeper-ms"}',
                proxy="http://lane-keeper-proxy",
                created_at=datetime.now(timezone.utc) - timedelta(hours=4),
                last_validation_ok=datetime.now(timezone.utc),
            ),
            Account(
                label="lane_burner_reply",
                status="active",
                cookies='{"sessionid":"lane-burn","sid_tt":"lane-burn-sid","s_v_web_id":"lane-burn-verify","ttwid":"lane-burn-wid","msToken":"lane-burn-ms"}',
                proxy="http://lane-burn-proxy",
                created_at=datetime.now(timezone.utc) - timedelta(hours=65),
                last_validation_ok=datetime.now(timezone.utc) - timedelta(hours=2),
            ),
            Account(
                label="lane_retire_account",
                status="active",
                cookies='{"sessionid":"lane-retire"}',
                proxy="http://lane-retire-proxy",
                created_at=datetime.now(timezone.utc) - timedelta(hours=96),
                validation_fail_count=3,
            ),
        ]
        db_session.add_all(accounts)
        db_session.commit()

        health = get_account_runtime_health(db_session)

        assert health["main_comment"]["keeper"] >= 1
        assert health["reply"]["burner"] >= 1
        assert health["main_comment"]["retire"] >= 1

    def test_http_execution_strategy_makes_keeper_reply_accounts_cautious(self, db_session: Session):
        account = Account(
            label="keeper_reply_http_cautious",
            status="active",
            cookies='{"sessionid":"keep-http","sid_tt":"keep-http-sid","s_v_web_id":"keep-http-verify","ttwid":"keep-http-wid","msToken":"keep-http-ms"}',
            proxy="http://keep-http-proxy",
            created_at=datetime.now(timezone.utc) - timedelta(hours=3),
            last_validation_ok=datetime.now(timezone.utc),
        )
        db_session.add(account)
        db_session.commit()

        strategy = account_service_module.get_http_execution_strategy(account, account_service_module.REPLY_SCOPE)

        assert strategy["spend_lane"] == "keeper"
        assert strategy["strategy"] == "http_cautious"

    def test_browser_validation_policy_skips_recent_trusted_reply_session(self, db_session: Session):
        account = Account(
            label="reply_trusted_validation",
            status="active",
            cookies=(
                "sessionid=abc; sid_tt=def; sid_guard=guard; "
                "verifyFp=verify; msToken=ms; tt_csrf_token=csrf; uid_tt=user; device_id=123"
            ),
            proxy="http://trusted-proxy",
            created_at=datetime.now(timezone.utc) - timedelta(hours=2),
            last_validation_ok=datetime.now(timezone.utc) - timedelta(seconds=60),
            last_used_at=datetime.now(timezone.utc) - timedelta(seconds=90),
            last_checked_at=datetime.now(timezone.utc) - timedelta(seconds=90),
            validation_fail_count=0,
            error_count_recent=0,
        )
        db_session.add(account)
        db_session.commit()

        policy = get_browser_validation_policy(account, REPLY_SCOPE)

        assert policy["skip_browser_validation"] is True
        assert policy["reason"] == "recent_trusted_reply_session"

    def test_browser_validation_policy_keeps_main_scope_strict(self, db_session: Session):
        account = Account(
            label="main_always_validate",
            status="active",
            cookies=(
                "sessionid=abc; sid_tt=def; sid_guard=guard; "
                "verifyFp=verify; msToken=ms; tt_csrf_token=csrf; uid_tt=user; device_id=123"
            ),
            proxy="http://main-proxy",
            created_at=datetime.now(timezone.utc) - timedelta(hours=2),
            last_validation_ok=datetime.now(timezone.utc) - timedelta(seconds=30),
            last_used_at=datetime.now(timezone.utc) - timedelta(seconds=30),
            validation_fail_count=0,
            error_count_recent=0,
        )
        db_session.add(account)
        db_session.commit()

        policy = get_browser_validation_policy(account, MAIN_SCOPE)

        assert policy["skip_browser_validation"] is False
        assert policy["reason"] == "strict_scope"
