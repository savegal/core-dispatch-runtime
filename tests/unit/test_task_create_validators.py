import pytest
from pydantic import ValidationError
from types import SimpleNamespace

from api.tasks import TaskCreate, _active_duplicate_matches_pool, _normalize_account_pool_mode_from_settings
from config.settings import get_settings


def _base_payload() -> dict:
    return {
        "user_id": 1,
        "main_comment": "main",
        "text_variants": ["a", "b", "c"],
        "reply1": None,
        "reply2": None,
        "reply3": None,
        "likes": 0,
        "video_urls": ["https://www.tiktok.com/@demo/video/1234567890123456789"],
        "settings": {},
        "kind": "engagement_only",
        "engagement_comment_id": "cid_1",
        "engagement_username": "@demo_user",
        "engagement_comment_text": "sample text",
    }


@pytest.mark.unit
def test_task_create_normalizes_engagement_username():
    model = TaskCreate(**_base_payload())
    assert model.engagement_username == "demo_user"


@pytest.mark.unit
def test_task_create_rejects_too_long_engagement_comment_id():
    payload = _base_payload()
    payload["engagement_comment_id"] = "x" * 129

    with pytest.raises(ValidationError):
        TaskCreate(**payload)


@pytest.mark.unit
def test_task_create_rejects_too_long_engagement_comment_text():
    payload = _base_payload()
    settings = get_settings()
    payload["engagement_comment_text"] = "x" * (settings.MAX_COMMENT_LENGTH + 1)

    with pytest.raises(ValidationError):
        TaskCreate(**payload)


@pytest.mark.unit
def test_task_create_deduplicates_and_normalizes_video_urls():
    payload = _base_payload()
    payload["video_urls"] = [
        "www.tiktok.com/@demo/video/1234567890123456789",
        "https://www.tiktok.com/@demo/video/1234567890123456789",
    ]

    model = TaskCreate(**payload)

    assert model.video_urls == ["https://www.tiktok.com/@demo/video/1234567890123456789"]


@pytest.mark.unit
def test_task_create_allows_empty_main_comment_for_engagement_only():
    payload = _base_payload()
    payload["main_comment"] = ""

    model = TaskCreate(**payload)

    assert model.kind == "engagement_only"
    assert model.main_comment == ""


@pytest.mark.unit
def test_task_create_engagement_only_allows_comment_text_without_author_or_id():
    payload = _base_payload()
    payload["engagement_comment_id"] = None
    payload["engagement_username"] = None
    payload["engagement_comment_text"] = "exact visible comment"

    model = TaskCreate(**payload)

    assert model.kind == "engagement_only"
    assert model.engagement_comment_text == "exact visible comment"
    assert model.engagement_username is None
    assert model.engagement_comment_id is None


@pytest.mark.unit
def test_task_create_requires_main_comment_for_full_kind():
    payload = _base_payload()
    payload["kind"] = "full"
    payload["main_comment"] = ""

    with pytest.raises(ValidationError):
        TaskCreate(**payload)


@pytest.mark.unit
def test_task_create_engagement_only_requires_locator_fields():
    payload = _base_payload()
    payload["engagement_comment_id"] = None
    payload["engagement_username"] = None
    payload["engagement_comment_text"] = None

    with pytest.raises(ValidationError):
        TaskCreate(**payload)


@pytest.mark.unit
def test_account_pool_mode_normalizes_unknown_values_to_shared():
    assert _normalize_account_pool_mode_from_settings({"account_pool_mode": "personal"}) == "personal"
    assert _normalize_account_pool_mode_from_settings({"account_pool_mode": "weird"}) == "shared"
    assert _normalize_account_pool_mode_from_settings({}) == "shared"


@pytest.mark.unit
def test_active_duplicate_must_match_account_pool_mode():
    personal_task = SimpleNamespace(settings={"account_pool_mode": "personal"})
    shared_task = SimpleNamespace(settings={"account_pool_mode": "shared"})

    assert _active_duplicate_matches_pool(personal_task, "personal") is True
    assert _active_duplicate_matches_pool(personal_task, "shared") is False
    assert _active_duplicate_matches_pool(shared_task, "personal") is False
