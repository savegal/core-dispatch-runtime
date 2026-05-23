import pytest

from executor.comment_outcome import _extract_publish_response_fields


@pytest.mark.unit
def test_extract_publish_fields_top_level_comment():
    payload = {
        "status_code": 0,
        "status_msg": "ok",
        "comment": {
            "cid": "c123",
            "reply_id": "p456",
        },
    }

    extracted = _extract_publish_response_fields(payload)

    assert extracted["status_code"] == 0
    assert extracted["status_msg"] == "ok"
    assert extracted["comment_id"] == "c123"
    assert extracted["parent_comment_id"] == "p456"


@pytest.mark.unit
def test_extract_publish_fields_nested_data_comment():
    payload = {
        "data": {
            "status_code": 0,
            "status_msg": "sent",
            "comment": {
                "comment_id": "cid_nested",
                "parent_comment_id": "parent_nested",
            },
        }
    }

    extracted = _extract_publish_response_fields(payload)

    assert extracted["status_code"] == 0
    assert extracted["status_msg"] == "sent"
    assert extracted["comment_id"] == "cid_nested"
    assert extracted["parent_comment_id"] == "parent_nested"


@pytest.mark.unit
def test_extract_publish_fields_keeps_status_without_comment_id():
    payload = {
        "status_code": 4,
        "status_msg": "rate limited",
        "data": {"reply_id": "0"},
    }

    extracted = _extract_publish_response_fields(payload)

    assert extracted["status_code"] == 4
    assert extracted["status_msg"] == "rate limited"
    assert extracted["comment_id"] is None
    assert extracted["parent_comment_id"] is None
