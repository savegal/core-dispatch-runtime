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
    increment_main_comment
)
from db.models import Account, Proxy
from config.settings import get_settings

settings = get_settings()


@pytest.fixture(autouse=True)
def reset_account_service_runtime_state(monkeypatch: pytest.MonkeyPatch):
    account_service_module._MAIN_ACCOUNT_RESERVED_UNTIL.clear()
    monkeypatch.setattr(account_service_module.settings, "USE_PROXY", False, raising=False)
    yield
    account_service_module._MAIN_ACCOUNT_RESERVED_UNTIL.clear()


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
        ]
        db_session.add_all(accounts)
        db_session.commit()

        health = get_account_runtime_health(db_session)
        assert set(health.keys()) >= {"generated_at", "main_comment", "reply", "monitor"}
        assert health["main_comment"]["total_candidates"] == 3
        assert health["reply"]["cooldown"] >= 1
        assert health["monitor"]["cooldown"] >= 1

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
