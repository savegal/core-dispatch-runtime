import asyncio
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from db.models import PublicationAttempt, TaskTrafficBucket
from services.publication_journal import observe_publication_request, note_publication_dispatch, utcnow
from services.publication_progress import publication_scope
from services.publication_reports import activity_snapshot, export_report, rolling_statistics
from services.publication_recheck import apply_observations, request_recheck


def attempt(db, task, **kwargs):
    row = PublicationAttempt(id=uuid4().hex, task_id=task.id, video_url=task.video_urls[0],
        kind="main", text="hello", text_hash="h", started_at=utcnow(), status="accepted",
        wire_count=1, comment_id="7704740811904828192", origin="http")
    for key, value in kwargs.items():
        setattr(row, key, value)
    db.add(row)
    db.commit()
    return row


@pytest.mark.asyncio
@pytest.mark.parametrize("dispatch,accepted", [(False, False), (True, False), (True, True)])
async def test_transport_journal_separates_preparation_dispatch_and_receipt(db_session, sample_task, dispatch, accepted):
    class Client:
        page = None
        last_request_meta = {}

        @observe_publication_request
        async def execute(self, **kwargs):
            self.last_request_meta = {"request_attempted": dispatch}
            if dispatch:
                note_publication_dispatch()
            return (True, {"comment": {"cid": "7704740811904828192"}}, None, None) if accepted else (
                False, None, "submitted_unconfirmed:empty_body" if dispatch else "preflight_failed", None)

    with publication_scope(db_session, sample_task, sample_task.video_urls[0]):
        await Client().execute(profile=SimpleNamespace(method="POST", endpoint="/api/comment/publish/"),
            dynamic_params={"text": "hello", "aweme_id": "7704037684280446216"})
    row = db_session.query(PublicationAttempt).one()
    assert row.wire_count == int(dispatch)
    assert row.status == ("accepted" if accepted else "unknown" if dispatch else "not_sent")
    assert row.visibility == "unchecked"
    assert (row.comment_id is not None) == accepted


@pytest.mark.asyncio
async def test_cancelled_dispatch_remains_unknown_and_is_not_replayed(db_session, sample_task):
    class Client:
        page = None
        last_request_meta = {"request_attempted": True}

        @observe_publication_request
        async def execute(self, **kwargs):
            note_publication_dispatch()
            raise asyncio.CancelledError()
    with publication_scope(db_session, sample_task, sample_task.video_urls[0]):
        with pytest.raises(asyncio.CancelledError):
            await Client().execute(profile=SimpleNamespace(method="POST", endpoint="/api/comment/publish/"),
                dynamic_params={"text": "hello"})
    row = db_session.query(PublicationAttempt).one()
    assert row.status == "unknown" and row.wire_count == 1


def test_cid_is_not_public_visibility(db_session, sample_task):
    attempt(db_session, sample_task)
    snapshot = activity_snapshot(db_session, sample_task)
    assert snapshot["summary"]["accepted"] == 1
    assert snapshot["summary"]["visible"] == 0
    assert snapshot["summary"]["pending_checks"] == 1
    assert snapshot["summary"]["visible_percent"] == 0
    assert "Подтверждённые" in export_report(db_session, sample_task, "links")
    assert "7704740811904828192" not in export_report(db_session, sample_task, "links")


def test_legacy_receipt_does_not_invent_attempts(db_session, sample_task):
    sample_task.settings = {"url_results": {sample_task.video_urls[0]: {"engagement": {"main": {
        "comment_id": "7704740811904828192", "network_confirmed": True}}}}}
    db_session.commit()
    snapshot = activity_snapshot(db_session, sample_task)
    assert snapshot["summary"]["accepted"] == 1
    assert snapshot["summary"]["dispatched"] == 0
    assert snapshot["summary"]["visible_percent"] is None
    assert snapshot["summary"]["historical_without_attempt_count"] == 1


@pytest.mark.parametrize("observations,expected", [
    ([{"observer_id": 2, "absent_ids": ["7704740811904828192"], "complete": False}], "inconclusive"),
    ([{"observer_id": 2, "absent_ids": ["7704740811904828192"], "complete": True}] * 2, "inconclusive"),
    ([{"observer_id": 2, "absent_ids": ["7704740811904828192"], "complete": True},
      {"observer_id": 3, "absent_ids": ["7704740811904828192"], "complete": True}], "not_found"),
    ([{"observer_id": 2, "found_ids": ["7704740811904828192"], "complete": False}], "visible"),
    ([{"observer_id": 1, "found_ids": ["7704740811904828192"], "complete": True}], "inconclusive"),
])
def test_recheck_requires_independent_complete_evidence(observations, expected):
    row = SimpleNamespace(comment_id="7704740811904828192", account_id=1, visible_at=None, check_count=0)
    apply_observations([row], observations)
    assert row.visibility == expected
    assert row.check_count == 1


def test_rolling_window_uses_timestamps_not_a_sliding_ttl(db_session, sample_task):
    attempt(db_session, sample_task, started_at=utcnow() - timedelta(hours=25))
    attempt(db_session, sample_task, comment_id="7704740811904828193")
    assert rolling_statistics(db_session, sample_task.user_id, hours=24)["summary"]["dispatched"] == 1
    assert rolling_statistics(db_session, sample_task.user_id, hours=168)["summary"]["dispatched"] == 2


@pytest.mark.parametrize("prefix", ["", " ", "\t", "\r"])
def test_csv_neutralizes_spreadsheet_formulas(db_session, sample_task, prefix):
    attempt(db_session, sample_task, text=prefix + "=HYPERLINK(\"example\")")
    assert "'" + prefix + "=HYPERLINK" in export_report(db_session, sample_task, "csv")


def test_recheck_requests_are_idempotent(db_session, sample_task):
    sample_task.status = "done"
    db_session.commit()
    assert request_recheck(db_session, sample_task.id)["status"] == "pending"
    assert request_recheck(db_session, sample_task.id)["status"] == "pending"
    db_session.commit()


@pytest.mark.asyncio
async def test_traffic_uses_sizes_and_ignores_local_cache(db_session, sample_task):
    from services.traffic_meter import TrafficMeter
    meter = TrafficMeter(db_session.get_bind(), 42)
    request = SimpleNamespace(_traffic_task_id=sample_task.id, resource_type="script", method="GET",
        sizes=AsyncMock(return_value={"responseBodySize": 100, "responseHeadersSize": 10,
                                     "requestBodySize": 0, "requestHeadersSize": 20}))
    await meter.measure(request)
    cached = SimpleNamespace(_traffic_task_id=sample_task.id, _traffic_local=True)
    meter.finished(cached)
    await meter.close()
    row = db_session.query(TaskTrafficBucket).one()
    assert (row.received_bytes, row.sent_bytes, row.requests) == (110, 20, 1)


@pytest.mark.asyncio
async def test_live_view_is_cancelled_before_navigation():
    from bot.services.live_orders import LiveOrderViews
    views = LiveOrderViews(interval=.001)
    gate = asyncio.Event()
    message = SimpleNamespace(chat=SimpleNamespace(id=1), message_id=2, edit_text=AsyncMock())
    async def loader():
        await gate.wait()
        return {"status": "in_progress"}
    await views.watch(message, loader, lambda _: ("new", None), "old")
    await asyncio.sleep(.01)
    await views.stop(1, 2)
    gate.set()
    await asyncio.sleep(.01)
    message.edit_text.assert_not_awaited()
    assert not views.views


def test_live_card_escapes_user_text():
    from bot.services.live_orders import render_live
    result = render_live({"task_id": 1, "latest": [{"text": "<b>text</b>", "kind": "main", "status": "accepted"}]})
    assert "&lt;b&gt;text&lt;/b&gt;" in result
