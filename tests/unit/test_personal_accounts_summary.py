from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from api import personal_accounts
from db.models import Account


def _settings(**overrides):
    base = {
        "ERROR_THRESHOLD": 3,
        "USE_PROXY": False,
        "MAX_ACTIONS_PER_ACCOUNT": 999999,
        "ACCOUNT_COOLDOWN_SECONDS": 60,
    }
    base.update(overrides)
    return SimpleNamespace(**base)


@pytest.mark.unit
def test_personal_account_available_main_respects_daily_limit(monkeypatch):
    monkeypatch.setattr(personal_accounts, "get_settings", lambda: _settings())
    account = Account(
        status="active",
        cookies='{"sessionid":"x"}',
        today_main_comments=5,
        daily_main_limit=5,
        today_replies=0,
        daily_reply_limit=10,
    )

    now = datetime.now(timezone.utc)

    assert personal_accounts._is_personal_account_available_now(account, scope="main", now=now) is False
    assert personal_accounts._is_personal_account_available_now(account, scope="reply", now=now) is True


@pytest.mark.unit
def test_personal_account_available_main_respects_cooldown(monkeypatch):
    monkeypatch.setattr(personal_accounts, "get_settings", lambda: _settings())
    now = datetime.now(timezone.utc)
    account = Account(
        status="active",
        cookies='{"sessionid":"x"}',
        today_main_comments=0,
        daily_main_limit=5,
        last_main_comment_at=now,
    )

    assert personal_accounts._is_personal_account_available_now(account, scope="main", now=now) is False


@pytest.mark.unit
def test_personal_account_available_requires_proxy_when_enabled(monkeypatch):
    monkeypatch.setattr(personal_accounts, "get_settings", lambda: _settings(USE_PROXY=True))
    account = Account(
        status="active",
        cookies='{"sessionid":"x"}',
        proxy=None,
        today_main_comments=0,
        daily_main_limit=5,
    )

    assert personal_accounts._is_personal_account_available_now(
        account,
        scope="main",
        now=datetime.now(timezone.utc),
    ) is False
