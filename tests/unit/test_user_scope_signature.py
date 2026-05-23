import pytest

from services.user_scope_signature import sign_user_scope, verify_user_scope_signature


@pytest.mark.unit
class TestUserScopeSignature:
    def test_verify_accepts_valid_signature(self):
        secret = "test-secret"
        timestamp = 1_700_000_000
        signature = sign_user_scope(user_id=42, timestamp=timestamp, secret=secret)

        assert verify_user_scope_signature(
            user_id=42,
            timestamp=timestamp,
            signature=signature,
            secret=secret,
            now_ts=timestamp + 10,
        ) is True

    def test_verify_rejects_wrong_signature(self):
        assert verify_user_scope_signature(
            user_id=42,
            timestamp=1_700_000_000,
            signature="bad-signature",
            secret="test-secret",
            now_ts=1_700_000_010,
        ) is False

    def test_verify_rejects_expired_signature(self):
        secret = "test-secret"
        timestamp = 1_700_000_000
        signature = sign_user_scope(user_id=42, timestamp=timestamp, secret=secret)

        assert verify_user_scope_signature(
            user_id=42,
            timestamp=timestamp,
            signature=signature,
            secret=secret,
            max_age_seconds=300,
            now_ts=timestamp + 301,
        ) is False
