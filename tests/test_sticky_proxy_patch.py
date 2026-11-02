"""Regressions for account-scoped residential routes and literal braces."""
from types import SimpleNamespace

from services.proxy_normalizer import (
    is_sticky_residential_template,
    resolve_account_sticky_proxy,
)
from services.spintax import expand_spintax, has_spintax


def test_mixed_case_account_placeholder_is_fully_resolved():
    account = SimpleNamespace(id=42, country_code="UA")
    template = "http://user-{SeSsIoN}-{CoUnTrY}:pass@host.example:10000"
    resolved = resolve_account_sticky_proxy(template, account)
    assert is_sticky_residential_template(template)
    assert resolved == "http://user-acc42-ua:pass@host.example:10000"
    assert "{" not in resolved
    assert resolve_account_sticky_proxy(template, SimpleNamespace(id=43, country_code="UA")) != resolved


def test_country_without_account_id_is_not_sticky():
    account = SimpleNamespace(id=42, country_code="UA")
    template = "http://u-{country}:pass@host.example:10000"
    assert not is_sticky_residential_template(template)
    assert resolve_account_sticky_proxy(template, account) == "http://u-ua:pass@host.example:10000"


def test_spintax_does_not_modify_literal_braces():
    assert not has_spintax("Hello {username}")
    assert expand_spintax("Hello {username}") == "Hello {username}"
    expanded = expand_spintax("{Hello|Hi} {username}", seed="example")
    assert expanded in {"Hello {username}", "Hi {username}"}

def test_existing_sticky_country_drift_still_matches_original_source():
    from services.proxy_normalizer import account_sticky_proxy_matches_template

    account = SimpleNamespace(id=42, country_code="DE")
    template = "http://u-{SeSsIoN}-{COUNTRY}:p@gateway.example:8080"
    # The account used UA when its sticky session was first expanded.
    saved = "http://u-acc42-UA:p@gateway.example:8080"
    assert account_sticky_proxy_matches_template(template, account, saved)
    assert not account_sticky_proxy_matches_template(
        template, SimpleNamespace(id=43, country_code="UA"), saved
    )
    assert not account_sticky_proxy_matches_template(
        "http://u-{country}:p@gateway.example:8080", account, saved
    )


def test_provider_auto_session_matches_only_its_owner():
    from services.proxy_normalizer import account_sticky_proxy_matches_template

    template = "http://u-session-auto-{country}:p@gateway.example:8080"
    assert account_sticky_proxy_matches_template(
        template, SimpleNamespace(id=42, country_code="DE"),
        "http://u-session-acc42-UA:p@gateway.example:8080",
    )
