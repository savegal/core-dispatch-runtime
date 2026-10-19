"""Failure-path regressions for the six HTTP-only order requirements."""
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from executor import engagement_engine as engine, user_schema as schema, publish_action
from executor.http import playwright_http_client as transport
from executor.http.request_profile import RequestProfile
from executor.http.retry_policy import RetryPolicy
from services.execution_attempts import track_publication_requests


URL = "https://www.tiktok.com/@fixture/photo/7635267329407913230"
SHORT = "https://vt.tiktok.com/local-fixture/"
CID = "7641761192486194454"
pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def http_only(monkeypatch):
    monkeypatch.setattr(schema._settings, "TIKTOK_NETWORK_FIRST", True)
    monkeypatch.setattr(schema._settings, "TIKTOK_NETWORK_IMPL_READY", True)
    # Conflicting configuration must never override active HTTP publication.
    monkeypatch.setattr(schema._settings, "PARALLEL_COMMENTING_DOM_FIRST", True)
    monkeypatch.setattr(schema._settings, "USE_PROXY", False)
    monkeypatch.setattr("services.boost_project_client.is_boost_enabled", Mock(return_value=False))


@pytest.mark.parametrize("correlation", ["ordinary", "task_parallel_main_1", "task_parallel_reply_1", "task_engagement_reply_1"])
def test_http_mode_wins_over_dom_first_configuration(correlation):
    assert not schema._parallel_dom_first_for_correlation(correlation)
    assert schema._http_first_for_correlation("main_comment", correlation)


@pytest.mark.asyncio
@pytest.mark.parametrize("reply", [False, True])
async def test_low_level_dom_publish_is_blocked_without_touching_page(reply):
    page = SimpleNamespace(evaluate=AsyncMock(side_effect=AssertionError("DOM touched")))
    publish = publish_action.publish_reply_hybrid if reply else publish_action.publish_comment_hybrid
    result = await publish(page, "fixture")
    assert result.status != "success" and result.error_reason == "http_only_dom_publish_disabled"
    assert not result.network_request_attempted
    page.evaluate.assert_not_awaited()


@pytest.mark.asyncio
async def test_http_mode_bypasses_bas_before_account_selection(monkeypatch):
    monkeypatch.setattr(engine.settings, "USE_BAS", True)
    monkeypatch.setattr(engine, "get_http_execution_strategy", Mock(return_value={"strategy": "skip", "reason": "dead_session"}))
    bas = AsyncMock(side_effect=AssertionError("BAS must not run"))
    monkeypatch.setattr("executor.bas_client.post_comment_via_bas", bas)
    outcome = {}
    cid, error, stage = await engine.attempt_main_comment_on_account(
        None, SimpleNamespace(id=1), URL, "text", 1, "fixture", outcome_dict=outcome,
    )
    assert cid is None and error == "dead_session" and stage == "strategy"
    assert outcome["publication_attempted"] is False
    bas.assert_not_awaited()


class EventPage:
    def __init__(self, mode, body=None, content_type="application/json"):
        self.url, self.mode, self.body, self.content_type = URL, mode, body, content_type
        self.context = SimpleNamespace(cookies=AsyncMock(return_value=[]))
        self.listeners = []
        self.arguments = None

    def on(self, event, callback):
        assert event == "request"
        self.listeners.append(callback)

    def remove_listener(self, event, callback):
        self.listeners.remove(callback)

    async def evaluate(self, script, arguments, **kwargs):
        self.arguments = arguments
        if self.mode == "before":
            raise RuntimeError("Execution context was destroyed before fetch")
        for listener in list(self.listeners):
            listener(SimpleNamespace(url="https://www.tiktok.com/api/comment/publish/", method="POST"))
        if self.mode == "after":
            raise TimeoutError("window.fetch request timeout")
        return {"status": 200, "headers": {"content-type": self.content_type}, "text": self.body}


@pytest.fixture
def client_factory(monkeypatch):
    bundle = SimpleNamespace(mode="native", source="page", x_bogus=None, x_gnarly=None,
                             a_bogus=None, signer_available=False, notes=None)
    monkeypatch.setattr(transport, "resolve_request_signature", AsyncMock(return_value=({}, "https://www.tiktok.com/api/comment/publish/", bundle)))
    return lambda page: transport.PlaywrightHttpClient(SimpleNamespace(fetch=AsyncMock()), page=page, retry_policy=RetryPolicy(max_retries=0))


@pytest.mark.asyncio
@pytest.mark.parametrize("phase,attempted", [("before", False), ("after", True)])
async def test_dispatch_evidence_survives_errors_and_listeners_are_removed(client_factory, phase, attempted):
    page = EventPage(phase)
    client = client_factory(page)
    success, *_ = await client._execute_request(RequestProfile.publish_main_profile(), {"text": "fixture"})
    assert not success
    assert client.last_request_meta["request_attempted"] is attempted
    assert bool(getattr(page, "_publication_attempted", False)) is attempted
    assert page.listeners == []


@pytest.mark.asyncio
@pytest.mark.parametrize("body,content_type", [
    ("", "application/json"), ("", "text/html"), ("", ""),
    ('{"status_code":0}', "application/json"),
    (json.dumps({"status_code": 0, "id": CID}), "application/json"),
    (json.dumps({"status_code": False, "comment": {"cid": CID}}), "application/json"),
    ('{"status_code":0,"comment":{"cid":"undefined"}}', "application/json"),
])
async def test_unconfirmed_responses_never_become_success(client_factory, body, content_type):
    client = client_factory(EventPage("response", body, content_type))
    success, response, error, _ = await client._execute_request(RequestProfile.publish_main_profile(), {"text": "fixture"})
    assert not success and response is None and error
    assert client.last_request_meta["request_attempted"] is True
    if not body:
        assert "stealth-reject" in error


@pytest.mark.asyncio
async def test_window_fetch_header_allowlist_preserves_native_security_sdk(client_factory):
    page = EventPage("response", json.dumps({"status_code": 0, "comment": {"cid": CID}}))
    profile = RequestProfile.publish_main_profile()
    profile.build_headers = Mock(return_value={"Cookie": "sessionid=stale", "x-secsdk-csrf-request": "1",
                                               "x-secsdk-csrf-token": "fake", "accept": "application/json",
                                               "tt-csrf-token": "fresh", "user-agent": "fake"})
    success, *_ = await client_factory(page)._execute_request(profile, {"text": "fixture"})
    assert success
    assert set(page.arguments["headers"]) == {"accept", "tt-csrf-token", "content-type"}


@pytest.mark.parametrize("value", [None, "", "undefined", "0", "0" * 19, "١" * 19, "123", True])
def test_api_comment_identifiers_are_validated(value):
    assert transport._extract_comment_id({"comment": {"cid": value}}) is None
    assert transport._extract_comment_id({"id": CID, "comment": {"cid": CID}}) == CID


def order_result():
    main = schema.PublishedMain("parent", CID, True, False, "http_user_schema", 1, "fixture")
    replies = [schema.ReplyOutcome(
        text=f"reply {variant}", variant_index=variant, matched_main_text="parent", parent_comment_id=CID,
        success=True, method="http_user_schema", error=None, account_id=variant + 1,
        reply_comment_id=str(int(CID) + variant), reply_confirmation="network_comment_id",
    ) for variant in (1, 2)]
    return schema.UserSchemaResult(published_mains=[main], replies=replies, expected_main_publications=1, expected_reply_variants=2)


def test_full_order_with_all_api_receipts_is_success():
    assert order_result().to_dict()["status"] == "success"


@pytest.mark.parametrize("bad", ["missing_id", "duplicate_id", "unplanned_parent", "unplanned_variant", "no_receipt", "local_main"])
def test_boolean_success_and_observer_flags_cannot_forge_order_completion(bad):
    result = order_result()
    result.main_found_by_observer = True
    if bad == "missing_id":
        result.replies[1].reply_comment_id = None
    elif bad == "duplicate_id":
        result.replies[1].reply_comment_id = result.replies[0].reply_comment_id
    elif bad == "unplanned_parent":
        result.replies[1].parent_comment_id = str(int(CID) + 100)
    elif bad == "unplanned_variant":
        result.replies[1].variant_index = 99
    elif bad == "no_receipt":
        result.replies[1].reply_confirmation = None
    else:
        result.published_mains[0].network_confirmed = False
        result.published_mains[0].local_confirmed = True
    assert result.to_dict()["status"] != "success"


@pytest.mark.asyncio
@pytest.mark.parametrize("parallel", [False, True])
async def test_unresolved_order_stops_before_consuming_accounts(monkeypatch, parallel):
    resolver = AsyncMock(return_value=(SHORT, None))
    monkeypatch.setattr(engine, "resolve_order_video_url", resolver)
    select = Mock(side_effect=AssertionError("Must not spend accounts on an unresolved URL"))
    monkeypatch.setattr(schema, "get_free_account_for_main_comment", select)
    execute = schema.execute_user_schema_parallel if parallel else schema.execute_user_schema_full
    result = await execute(None, SimpleNamespace(settings={}), SHORT, ["parent"], 1, boost_enabled=False, boost_quantity=0)
    assert result.error == "video_url_unresolved"
    resolver.assert_awaited_once_with(None, SHORT, None)
    select.assert_not_called()


@pytest.mark.asyncio
async def test_shortlink_resolver_uses_only_the_order_owner_proxy(db_session, sample_user, monkeypatch):
    from db.models import Account
    monkeypatch.setattr(engine.settings, "USE_PROXY", True)
    monkeypatch.setattr("services.dedicated_proxy_service.get_current_dedicated_proxy", Mock(return_value=None))
    db_session.add_all([
        Account(label="shared", proxy="http://shared.invalid:8000", status="active"),
        Account(label="private", owner_user_id=sample_user.id, proxy="http://private.invalid:8000", status="active"),
    ])
    db_session.commit()
    resolve = AsyncMock(return_value=(URL, "7635267329407913230"))
    monkeypatch.setattr(engine, "resolve_video_url", resolve)
    assert await engine.resolve_order_video_url(db_session, SHORT, sample_user.id) == (URL, "7635267329407913230")
    resolve.assert_awaited_once_with(SHORT, proxy="http://private.invalid:8000")


def test_non_publish_requests_do_not_consume_a_publication_attempt():
    page, state = EventPage("before"), {}
    detach = track_publication_requests(page, state)
    callback = page.listeners[0]
    callback(SimpleNamespace(url="https://www.tiktok.com/api/comment/list/", method="GET"))
    callback(SimpleNamespace(url="https://unrelated.invalid/api/comment/publish/", method="POST"))
    assert not state
    callback(SimpleNamespace(url="https://www.tiktok.com/api/comment/publish/", method="POST"))
    assert state["publication_attempted"] is True
    detach()
    assert page.listeners == []


@pytest.mark.asyncio
async def test_legacy_hybrid_replaces_pre_publish_captcha_accounts_without_observers(monkeypatch):
    accounts = [SimpleNamespace(id=i, label=f"account-{i}") for i in (1, 2, 3)]
    select = Mock(side_effect=accounts)
    monkeypatch.setattr(engine.settings, "MAX_RETRY_ACCOUNTS", 3)
    monkeypatch.setattr(engine, "get_free_account_for_main_comment", select)
    monkeypatch.setattr(engine, "release_main_account_reservation", Mock())
    monkeypatch.setattr(engine, "_get_task_boost_config", Mock(return_value=(False, 0)))
    monkeypatch.setattr(engine, "_order_boost_likes", AsyncMock(return_value=(None, None)))
    observer = AsyncMock(side_effect=AssertionError("HTTP orders must not start a DOM observer"))
    monkeypatch.setattr(engine, "verify_comment_dom", observer)
    async def publish(db, account, url, text, task_id, correlation_id, *, outcome_dict):
        if account.id < 3:
            outcome_dict["publication_attempted"] = False
            return None, "captcha_challenge:visible", "warmup"
        outcome_dict.update(publication_attempted=True, posted=True, network_confirmed=True, method="http")
        return CID, None, "http"
    monkeypatch.setattr(engine, "attempt_main_comment_on_account", publish)
    task = SimpleNamespace(settings={"reply_count": 0}, reply1=None, reply2=None, reply3=None)
    result = await engine.execute_hybrid_full(None, task, URL, "parent", 1)
    assert select.call_count == 3
    assert result.to_dict()["status"] == "success"
    observer.assert_not_awaited()


@pytest.mark.parametrize("bad", [None, "missing_id", "duplicate_id", "missing_variant", "local_main"])
def test_legacy_engine_serialization_does_not_resurrect_ghost_receipts(bad):
    result = engine.EngagementResult()
    result.boost_enabled = False
    result.expected_reply_variants = 2
    result.main_comment.update(success=True, posted=True, verified=True, network_confirmed=True, method="http", comment_id=CID)
    result.replies = [dict(success=True, method="http", network_confirmed=True, reply_id=str(int(CID) + variant), variant_index=variant) for variant in (1, 2)]
    if bad == "missing_id":
        result.replies[1]["reply_id"] = None
    elif bad == "duplicate_id":
        result.replies[1]["reply_id"] = result.replies[0]["reply_id"]
    elif bad == "missing_variant":
        result.replies[1]["variant_index"] = 1
    elif bad == "local_main":
        result.main_comment.update(network_confirmed=False, local_confirmed=True)
    payload = result.to_dict()
    assert (payload["status"] == "success") is (bad is None)
    if bad == "local_main":
        assert not payload["engagement"]["main"]["success"]
        assert not payload["engagement"]["main"]["local_confirmed"]
