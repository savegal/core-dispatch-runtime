"""Regression tests for conservative network usage defaults.

A task's request budget can be explicitly configured, but a new deployment
should not scan thousands of comments or launch six observers by default.
"""
from config.settings import Settings


def test_fresh_settings_limit_keyword_scans_and_observer_requests(monkeypatch):
    for name in (
        "KEYWORD_REPLY_MAX_SCAN_PAGES",
        "MAIN_VISIBILITY_OBSERVER_ATTEMPTS",
        "REPLY_PUBLICATION_VISIBILITY_OBSERVER_ATTEMPTS",
        "MAIN_VISIBILITY_MAX_PAGES",
        "REPLY_PUBLICATION_VISIBILITY_MAX_PAGES",
    ):
        monkeypatch.delenv(name, raising=False)

    settings = Settings(
        EXECUTOR_API_TOKEN="ci-test-executor-token",
        JWT_SECRET="ci-test-jwt-signing-key",
        _env_file=None,
    )
    assert settings.KEYWORD_REPLY_MAX_SCAN_PAGES == 10
    assert settings.MAIN_VISIBILITY_OBSERVER_ATTEMPTS == 2
    assert settings.REPLY_PUBLICATION_VISIBILITY_OBSERVER_ATTEMPTS == 2
    assert settings.MAIN_VISIBILITY_MAX_PAGES == 5
    assert settings.REPLY_PUBLICATION_VISIBILITY_MAX_PAGES == 5


def test_explicit_budget_overrides_remain_available():
    settings = Settings(
        EXECUTOR_API_TOKEN="ci-test-executor-token",
        JWT_SECRET="ci-test-jwt-signing-key",
        KEYWORD_REPLY_MAX_SCAN_PAGES=4,
        MAIN_VISIBILITY_OBSERVER_ATTEMPTS=4,
        REPLY_PUBLICATION_VISIBILITY_OBSERVER_ATTEMPTS=3,
        MAIN_VISIBILITY_MAX_PAGES=3,
        REPLY_PUBLICATION_VISIBILITY_MAX_PAGES=2,
        _env_file=None,
    )
    assert settings.KEYWORD_REPLY_MAX_SCAN_PAGES == 4
    assert settings.MAIN_VISIBILITY_OBSERVER_ATTEMPTS == 4
    assert settings.REPLY_PUBLICATION_VISIBILITY_OBSERVER_ATTEMPTS == 3
    assert settings.MAIN_VISIBILITY_MAX_PAGES == 3
    assert settings.REPLY_PUBLICATION_VISIBILITY_MAX_PAGES == 2
