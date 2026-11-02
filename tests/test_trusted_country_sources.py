"""Account country may only be filled from explicit trusted evidence."""
from types import SimpleNamespace

from services.geo_detection_service import (
    detect_account_country,
    detect_country_from_cookies,
    update_account_country_if_needed,
)


def _account(**overrides):
    data = {
        "id": 42,
        "label": "test-42",
        "country_code": None,
        "cookies": "store-region=PL; locale=en-US; currency=USD; tt-target-idc=useast1a",
        "_passport_country_code": None,
        "_account_ssr_region": None,
    }
    data.update(overrides)
    return SimpleNamespace(**data)


def test_existing_database_country_wins_even_when_other_sources_disagree():
    account = _account(
        country_code="UA",
        _passport_country_code="DE",
        _account_ssr_region="FR",
    )
    assert detect_account_country(account) == "UA"


def test_passport_precedes_ssr_and_explicit_store_cookie():
    account = _account(_passport_country_code="DE", _account_ssr_region="FR")
    assert detect_account_country(account) == "DE"


def test_ssr_precedes_explicit_store_cookie_when_passport_unavailable():
    assert detect_account_country(_account(_account_ssr_region="FR")) == "FR"


def test_only_explicit_cookie_region_is_accepted():
    assert detect_country_from_cookies("store-region=pl; locale=en-US") == "PL"
    assert detect_country_from_cookies("tt-target-idc-sign=UA") == "UA"
    assert detect_country_from_cookies("tt-target-idc-sign=useast1a.xxxxxx") is None
    assert detect_country_from_cookies(
        "currency=USD; lang=en_US; locale=en-US; tt-target-idc=useast1a; priority_region=US"
    ) is None


def test_force_does_not_replace_stored_country():
    class GuardDb:
        def commit(self):
            raise AssertionError("country should never be overwritten")
        def rollback(self):
            raise AssertionError("rollback should not be needed")

    account = _account(country_code="UA", _passport_country_code="DE")
    assert not update_account_country_if_needed(GuardDb(), account, force=True)
    assert account.country_code == "UA"


def test_missing_country_can_be_persisted_from_passport_or_ssr():
    class Db:
        commits = 0
        def commit(self):
            self.commits += 1
        def rollback(self):
            raise AssertionError("should not fail")

    db = Db()
    account = _account(cookies="currency=USD", _passport_country_code="DE", _account_ssr_region="FR")
    assert update_account_country_if_needed(db, account)
    assert account.country_code == "DE"
    assert db.commits == 1
