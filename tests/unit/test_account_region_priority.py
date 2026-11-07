"""The operator's region preference cannot bypass eligibility or health gates."""
from datetime import datetime, timezone, timedelta
import json

import pytest

from db.models import Account
from services import account_service as accounts


@pytest.fixture
def pool(db_session, monkeypatch):
    monkeypatch.setattr(accounts.settings, "USE_PROXY", False)
    monkeypatch.setattr(accounts.settings, "PREFERRED_ACCOUNT_COUNTRIES", "FR,ES,GB,DE,IT,PL,NL,US")
    accounts._MAIN_ACCOUNT_RESERVED_UNTIL.clear()
    accounts._REPLY_ACCOUNT_RESERVED_UNTIL.clear()
    now = datetime.now(timezone.utc)
    entries = []
    for index, country in enumerate(("JP", "US")):
        row = Account(label=f"region-{index}", country_code=country, status="active",
            cookies=json.dumps({"sessionid": f"{index + 1:032x}", "uid_tt": str(100 + index),
                                "ttwid": "wid", "msToken": "ms", "tt_csrf_token": "csrf"}),
            created_at=now - timedelta(hours=2), last_validation_ok=now - timedelta(minutes=5),
            today_main_comments=0, today_replies=0, error_count_recent=0, validation_fail_count=0)
        db_session.add(row)
        entries.append(row)
    db_session.commit()
    yield db_session, entries
    for row in entries:
        accounts.release_main_account_reservation(row.id)
        accounts.release_reply_account_reservation(row.id)
    accounts._MAIN_ACCOUNT_RESERVED_UNTIL.clear()
    accounts._REPLY_ACCOUNT_RESERVED_UNTIL.clear()


@pytest.mark.parametrize("selector", [accounts.get_free_account_for_main_comment, accounts.get_free_account_for_reply,
                                      accounts.get_monitor_account])
def test_preferred_region_wins_between_equally_healthy_accounts(pool, selector):
    db, rows = pool
    assert selector(db).id == rows[1].id


@pytest.mark.parametrize("selector", [accounts.get_free_account_for_main_comment, accounts.get_free_account_for_reply,
                                      accounts.get_monitor_account])
def test_dead_preferred_account_cannot_hide_healthy_reserve(pool, selector):
    db, rows = pool
    rows[1].status = "dead"
    db.commit()
    assert selector(db).id == rows[0].id


def test_country_preference_is_optional_and_recognizes_uk_alias(monkeypatch):
    monkeypatch.setattr(accounts.settings, "PREFERRED_ACCOUNT_COUNTRIES", "GB")
    assert accounts._account_country_priority(Account(country_code=" uk ")) == 0
    assert accounts._account_country_priority(Account(country_code="JP")) == 1
    monkeypatch.setattr(accounts.settings, "PREFERRED_ACCOUNT_COUNTRIES", "")
    assert accounts._account_country_priority(Account(country_code="JP")) == 0
    assert accounts._account_country_priority(Account(country_code=None)) == 0
