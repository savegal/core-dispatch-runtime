import pytest
from fastapi import HTTPException

from api import video_parsing
from db.models import MonitoredBlogger


@pytest.mark.unit
@pytest.mark.asyncio
async def test_unique_creator_limit_blocks_new_blogger_when_pool_is_full(async_db_session, monkeypatch):
    monkeypatch.setattr(video_parsing.settings, "MAX_UNIQUE_CREATORS_TOTAL", 2, raising=False)
    async_db_session.add_all([
        MonitoredBlogger(username="alpha"),
        MonitoredBlogger(username="bravo"),
    ])
    await async_db_session.commit()

    with pytest.raises(HTTPException) as exc_info:
        await video_parsing._ensure_unique_creator_capacity(async_db_session, monitored_blogger=None)

    assert exc_info.value.status_code == 403
    assert "Maximum 2 unique creators total" in exc_info.value.detail
    assert "Current unique creators: 2" in exc_info.value.detail


@pytest.mark.unit
@pytest.mark.asyncio
async def test_unique_creator_limit_allows_existing_blogger_duplicate_when_pool_is_full(
    async_db_session,
    monkeypatch,
):
    monkeypatch.setattr(video_parsing.settings, "MAX_UNIQUE_CREATORS_TOTAL", 1, raising=False)
    existing = MonitoredBlogger(username="alpha")
    async_db_session.add(existing)
    await async_db_session.commit()

    await video_parsing._ensure_unique_creator_capacity(async_db_session, monitored_blogger=existing)
