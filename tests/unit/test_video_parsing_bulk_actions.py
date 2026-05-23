import pytest
from sqlalchemy import select

from api import video_parsing
from db.models import AppSettings, CreatorSource, CreatorVideo, MonitorOutbox, MonitoredBlogger, TaskTemplate, User
from services.app_settings_service import SOFTWARE_MODE_KEY, SOFTWARE_MODE_PARSING


async def _create_bulk_user(async_db_session, telegram_id: int = 91001) -> User:
    user = User(
        telegram_id=telegram_id,
        username=f"bulk_user_{telegram_id}",
        role="user",
        balance=1000.00,
        is_blocked=False,
    )
    async_db_session.add(user)
    await async_db_session.flush()
    return user


async def _create_creator(
    async_db_session,
    user: User,
    *,
    username: str,
    enabled: bool,
    mode: str,
    default_template_id: int | None = None,
) -> tuple[MonitoredBlogger, CreatorSource]:
    blogger = MonitoredBlogger(username=username)
    creator = CreatorSource(
        user_id=user.id,
        username=username,
        monitored_blogger=blogger,
        enabled=enabled,
        interval_minutes=30,
        mode=mode,
        default_template_id=default_template_id,
    )
    async_db_session.add_all([blogger, creator])
    await async_db_session.flush()
    return blogger, creator


@pytest.mark.unit
@pytest.mark.asyncio
async def test_bulk_disable_auto_creator_without_template_is_allowed(async_db_session):
    user = await _create_bulk_user(async_db_session)
    blogger, creator = await _create_creator(
        async_db_session,
        user,
        username="bulk_disable_no_template",
        enabled=True,
        mode="auto_comment",
        default_template_id=None,
    )
    outbox = MonitorOutbox(
        event_type="autopilot_create",
        status="pending",
        blogger_id=blogger.id,
        creator_id=creator.id,
        payload={"video_ids": ["1"]},
    )
    processing_outbox = MonitorOutbox(
        event_type="autopilot_create",
        status="processing",
        lock_owner="monitor-worker",
        blogger_id=blogger.id,
        creator_id=creator.id,
        payload={"video_ids": ["2"]},
    )
    async_db_session.add_all([outbox, processing_outbox])
    await async_db_session.commit()

    result = await video_parsing.bulk_update_creators(
        video_parsing.CreatorBulkUpdate(
            selector=video_parsing.CreatorBulkSelector(scope="all"),
            changes=video_parsing.CreatorBulkChanges(enabled=False),
            dry_run=False,
        ),
        user_id=user.id,
        auth_user_id=user.id,
        db=async_db_session,
    )

    await async_db_session.refresh(creator)
    await async_db_session.refresh(outbox)
    await async_db_session.refresh(processing_outbox)
    assert result["updated_count"] == 1
    assert result["skipped_count"] == 0
    assert result["cancelled_autopilot_events_for"] == 1
    assert creator.enabled is False
    assert outbox.status == "cancelled"
    assert outbox.last_error == "cancelled_by_bulk_creator_disabled_or_mode_change"
    assert processing_outbox.status == "cancelled"
    assert processing_outbox.lock_owner is None
    assert processing_outbox.last_error == "cancelled_by_bulk_creator_disabled_or_mode_change"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_update_creator_cancels_processing_autopilot_outbox(async_db_session):
    user = await _create_bulk_user(async_db_session, telegram_id=91007)
    blogger, creator = await _create_creator(
        async_db_session,
        user,
        username="single_disable_processing",
        enabled=True,
        mode="auto_comment",
        default_template_id=None,
    )
    outbox = MonitorOutbox(
        event_type="autopilot_create",
        status="processing",
        lock_owner="monitor-worker",
        blogger_id=blogger.id,
        creator_id=creator.id,
        payload={"video_ids": ["1"]},
    )
    async_db_session.add(outbox)
    await async_db_session.commit()

    result = await video_parsing.update_creator(
        creator.id,
        video_parsing.CreatorUpdate(enabled=False),
        user_id=user.id,
        auth_user_id=user.id,
        db=async_db_session,
    )

    await async_db_session.refresh(outbox)
    assert result["enabled"] is False
    assert outbox.status == "cancelled"
    assert outbox.lock_owner is None
    assert outbox.last_error == "cancelled_by_creator_disabled_or_mode_change"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_bulk_interval_does_not_require_auto_template(async_db_session):
    user = await _create_bulk_user(async_db_session, telegram_id=91002)
    await _create_creator(
        async_db_session,
        user,
        username="bulk_interval_no_template",
        enabled=True,
        mode="auto_comment",
        default_template_id=None,
    )
    await async_db_session.commit()

    result = await video_parsing.bulk_update_creators(
        video_parsing.CreatorBulkUpdate(
            selector=video_parsing.CreatorBulkSelector(scope="all"),
            changes=video_parsing.CreatorBulkChanges(interval_minutes=60),
            dry_run=True,
        ),
        user_id=user.id,
        auth_user_id=user.id,
        db=async_db_session,
    )

    assert result["updated_count"] == 1
    assert result["skipped_count"] == 0


@pytest.mark.unit
@pytest.mark.asyncio
async def test_bulk_actions_are_blocked_for_restricted_parsing_mode_user(async_db_session):
    user = await _create_bulk_user(async_db_session, telegram_id=91022)
    await _create_creator(
        async_db_session,
        user,
        username="bulk_restricted_parsing_mode",
        enabled=True,
        mode="notify_only",
    )
    async_db_session.add(AppSettings(key=SOFTWARE_MODE_KEY, value=SOFTWARE_MODE_PARSING))
    await async_db_session.commit()

    with pytest.raises(video_parsing.HTTPException) as exc:
        await video_parsing.bulk_update_creators(
            video_parsing.CreatorBulkUpdate(
                selector=video_parsing.CreatorBulkSelector(scope="all"),
                changes=video_parsing.CreatorBulkChanges(interval_minutes=60),
                dry_run=True,
            ),
            user_id=user.id,
            auth_user_id=user.id,
            db=async_db_session,
        )

    assert exc.value.status_code == 403


@pytest.mark.unit
@pytest.mark.asyncio
async def test_bulk_actions_work_for_full_access_override_in_parsing_mode(async_db_session):
    user = await _create_bulk_user(async_db_session, telegram_id=91023)
    user.full_access_override = True
    await _create_creator(
        async_db_session,
        user,
        username="bulk_full_access_parsing_mode",
        enabled=True,
        mode="notify_only",
    )
    async_db_session.add(AppSettings(key=SOFTWARE_MODE_KEY, value=SOFTWARE_MODE_PARSING))
    await async_db_session.commit()

    result = await video_parsing.bulk_update_creators(
        video_parsing.CreatorBulkUpdate(
            selector=video_parsing.CreatorBulkSelector(scope="all"),
            changes=video_parsing.CreatorBulkChanges(interval_minutes=60),
            dry_run=True,
        ),
        user_id=user.id,
        auth_user_id=user.id,
        db=async_db_session,
    )

    assert result["updated_count"] == 1


@pytest.mark.unit
@pytest.mark.asyncio
async def test_bulk_enable_auto_creator_without_template_is_blocked(async_db_session):
    user = await _create_bulk_user(async_db_session, telegram_id=91003)
    await _create_creator(
        async_db_session,
        user,
        username="bulk_enable_no_template",
        enabled=False,
        mode="auto_comment",
        default_template_id=None,
    )
    await async_db_session.commit()

    result = await video_parsing.bulk_update_creators(
        video_parsing.CreatorBulkUpdate(
            selector=video_parsing.CreatorBulkSelector(scope="all"),
            changes=video_parsing.CreatorBulkChanges(enabled=True),
            dry_run=True,
        ),
        user_id=user.id,
        auth_user_id=user.id,
        db=async_db_session,
    )

    assert result["updated_count"] == 0
    assert result["skipped_count"] == 1
    assert result["skipped_by_reason"] == {"auto_comment_requires_default_template": 1}


@pytest.mark.unit
@pytest.mark.asyncio
async def test_bulk_auto_template_queues_existing_new_videos_for_autopilot(async_db_session):
    user = await _create_bulk_user(async_db_session, telegram_id=91004)
    template = TaskTemplate(
        user_id=user.id,
        name="bulk auto",
        main_comment="Main",
        settings={"text_variants": ["Main", "Second", "Third"]},
    )
    blogger, creator = await _create_creator(
        async_db_session,
        user,
        username="bulk_existing_new_to_auto",
        enabled=True,
        mode="manual_review",
        default_template_id=None,
    )
    async_db_session.add(template)
    await async_db_session.flush()
    video = CreatorVideo(
        creator_id=creator.id,
        video_id="bulk-auto-existing-1",
        url="https://www.tiktok.com/@bulk_existing_new_to_auto/video/bulk-auto-existing-1",
        status="new",
    )
    async_db_session.add(video)
    await async_db_session.commit()

    result = await video_parsing.bulk_update_creators(
        video_parsing.CreatorBulkUpdate(
            selector=video_parsing.CreatorBulkSelector(scope="with_new_videos"),
            changes=video_parsing.CreatorBulkChanges(
                enabled=True,
                mode="auto_comment",
                default_template_id=template.id,
            ),
            dry_run=False,
        ),
        user_id=user.id,
        auth_user_id=user.id,
        db=async_db_session,
    )

    await async_db_session.refresh(creator)
    assert result["updated_count"] == 1
    assert result["autopilot_existing_video_events_queued"] == 1
    assert result["autopilot_existing_video_count"] == 1
    assert creator.mode == "auto_comment"
    assert creator.default_template_id == template.id

    outbox = (
        await async_db_session.execute(
            select(MonitorOutbox).filter(
                MonitorOutbox.creator_id == creator.id,
                MonitorOutbox.event_type == "autopilot_create",
            )
        )
    ).scalar_one()
    assert outbox.status == "pending"
    assert outbox.blogger_id == blogger.id
    assert outbox.payload["video_ids"] == [video.id]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_bulk_auto_template_does_not_duplicate_existing_autopilot_outbox(async_db_session):
    user = await _create_bulk_user(async_db_session, telegram_id=91005)
    template = TaskTemplate(
        user_id=user.id,
        name="bulk auto duplicate",
        main_comment="Main",
        settings={"text_variants": ["Main", "Second", "Third"]},
    )
    blogger, creator = await _create_creator(
        async_db_session,
        user,
        username="bulk_existing_autopilot_dedupe",
        enabled=True,
        mode="manual_review",
        default_template_id=None,
    )
    async_db_session.add(template)
    await async_db_session.flush()
    video = CreatorVideo(
        creator_id=creator.id,
        video_id="bulk-auto-existing-2",
        url="https://www.tiktok.com/@bulk_existing_autopilot_dedupe/video/bulk-auto-existing-2",
        status="new",
    )
    async_db_session.add(video)
    await async_db_session.flush()
    dedupe_key = video_parsing._build_monitor_outbox_dedupe_key(
        "autopilot_create",
        creator.id,
        [video.id],
    )
    async_db_session.add(
        MonitorOutbox(
            event_type="autopilot_create",
            status="pending",
            blogger_id=blogger.id,
            creator_id=creator.id,
            payload={"creator_id": creator.id, "video_ids": [video.id]},
            dedupe_key=dedupe_key,
        )
    )
    await async_db_session.commit()

    result = await video_parsing.bulk_update_creators(
        video_parsing.CreatorBulkUpdate(
            selector=video_parsing.CreatorBulkSelector(scope="with_new_videos"),
            changes=video_parsing.CreatorBulkChanges(
                enabled=True,
                mode="auto_comment",
                default_template_id=template.id,
            ),
            dry_run=False,
        ),
        user_id=user.id,
        auth_user_id=user.id,
        db=async_db_session,
    )

    assert result["updated_count"] == 1
    assert result["autopilot_existing_video_events_queued"] == 0
    assert result["autopilot_existing_video_count"] == 1
    outbox_rows = (
        await async_db_session.execute(
            select(MonitorOutbox).filter(
                MonitorOutbox.creator_id == creator.id,
                MonitorOutbox.event_type == "autopilot_create",
            )
        )
    ).scalars().all()
    assert len(outbox_rows) == 1


@pytest.mark.unit
@pytest.mark.asyncio
async def test_bulk_auto_template_requeues_after_cancelled_autopilot_outbox(async_db_session):
    user = await _create_bulk_user(async_db_session, telegram_id=91008)
    template = TaskTemplate(
        user_id=user.id,
        name="bulk auto cancelled requeue",
        main_comment="Main",
        settings={"text_variants": ["Main", "Second", "Third"]},
    )
    blogger, creator = await _create_creator(
        async_db_session,
        user,
        username="bulk_cancelled_autopilot_requeue",
        enabled=True,
        mode="manual_review",
        default_template_id=None,
    )
    async_db_session.add(template)
    await async_db_session.flush()
    video = CreatorVideo(
        creator_id=creator.id,
        video_id="bulk-auto-cancelled-1",
        url="https://www.tiktok.com/@bulk_cancelled_autopilot_requeue/video/bulk-auto-cancelled-1",
        status="new",
    )
    async_db_session.add(video)
    await async_db_session.flush()
    original_dedupe_key = video_parsing._build_monitor_outbox_dedupe_key(
        "autopilot_create",
        creator.id,
        [video.id],
    )
    async_db_session.add(
        MonitorOutbox(
            event_type="autopilot_create",
            status="cancelled",
            blogger_id=blogger.id,
            creator_id=creator.id,
            payload={"creator_id": creator.id, "video_ids": [video.id]},
            dedupe_key=original_dedupe_key,
        )
    )
    await async_db_session.commit()

    result = await video_parsing.bulk_update_creators(
        video_parsing.CreatorBulkUpdate(
            selector=video_parsing.CreatorBulkSelector(scope="with_new_videos"),
            changes=video_parsing.CreatorBulkChanges(
                enabled=True,
                mode="auto_comment",
                default_template_id=template.id,
            ),
            dry_run=False,
        ),
        user_id=user.id,
        auth_user_id=user.id,
        db=async_db_session,
    )

    assert result["updated_count"] == 1
    assert result["autopilot_existing_video_events_queued"] == 1
    outbox_rows = (
        await async_db_session.execute(
            select(MonitorOutbox).filter(
                MonitorOutbox.creator_id == creator.id,
                MonitorOutbox.event_type == "autopilot_create",
            )
        )
    ).scalars().all()
    assert len(outbox_rows) == 2
    pending = [row for row in outbox_rows if row.status == "pending"]
    assert len(pending) == 1
    assert pending[0].dedupe_key != original_dedupe_key
    assert pending[0].payload["video_ids"] == [video.id]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_bulk_auto_template_queues_when_settings_already_match(async_db_session):
    user = await _create_bulk_user(async_db_session, telegram_id=91006)
    template = TaskTemplate(
        user_id=user.id,
        name="bulk auto already matching",
        main_comment="Main",
        settings={"text_variants": ["Main", "Second", "Third"]},
    )
    async_db_session.add(template)
    await async_db_session.flush()
    blogger, creator = await _create_creator(
        async_db_session,
        user,
        username="bulk_auto_already_matching",
        enabled=True,
        mode="auto_comment",
        default_template_id=template.id,
    )
    video = CreatorVideo(
        creator_id=creator.id,
        video_id="bulk-auto-existing-3",
        url="https://www.tiktok.com/@bulk_auto_already_matching/video/bulk-auto-existing-3",
        status="new",
    )
    async_db_session.add(video)
    await async_db_session.commit()

    result = await video_parsing.bulk_update_creators(
        video_parsing.CreatorBulkUpdate(
            selector=video_parsing.CreatorBulkSelector(scope="auto_comment"),
            changes=video_parsing.CreatorBulkChanges(
                enabled=True,
                mode="auto_comment",
                default_template_id=template.id,
            ),
            dry_run=False,
        ),
        user_id=user.id,
        auth_user_id=user.id,
        db=async_db_session,
    )

    assert result["updated_count"] == 1
    assert result["skipped_count"] == 0
    assert result["autopilot_existing_video_events_queued"] == 1
    outbox = (
        await async_db_session.execute(
            select(MonitorOutbox).filter(
                MonitorOutbox.creator_id == creator.id,
                MonitorOutbox.event_type == "autopilot_create",
            )
        )
    ).scalar_one()
    assert outbox.blogger_id == blogger.id
    assert outbox.payload["video_ids"] == [video.id]
