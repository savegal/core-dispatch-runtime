import pytest
from decimal import Decimal
from sqlalchemy.orm import Session

from db.models import Pricing, User
from services.balance_service import (
    add_balance,
    calculate_refund_amount,
    get_pricing,
    refund_balance,
    subtract_balance,
    update_pricing,
)


@pytest.mark.unit
class TestBalanceService:
    def test_get_pricing_returns_default_singleton(self, db_session: Session):
        pricing = get_pricing(db_session)

        assert pricing is not None
        assert pricing.price_success > 0
        assert pricing.price_failure > 0
        assert db_session.query(Pricing).count() == 1

    def test_get_pricing_collapses_duplicate_rows(self, db_session: Session):
        db_session.add_all(
            [
                Pricing(price_success=Decimal("5.00"), price_failure=Decimal("2.00")),
                Pricing(price_success=Decimal("7.00"), price_failure=Decimal("3.00")),
            ]
        )
        db_session.commit()

        pricing = get_pricing(db_session)

        rows = db_session.query(Pricing).order_by(Pricing.id.asc()).all()
        assert len(rows) == 1
        assert rows[0].id == pricing.id
        assert pricing.price_success == Decimal("5.00")
        assert pricing.price_failure == Decimal("2.00")

    def test_add_balance(self, db_session: Session, sample_user: User):
        initial_balance = sample_user.balance

        add_balance(db_session, sample_user.id, Decimal("100.00"), "Test deposit")

        db_session.refresh(sample_user)
        assert sample_user.balance == initial_balance + Decimal("100.00")

    def test_subtract_balance_sufficient(self, db_session: Session, sample_user: User):
        initial_balance = sample_user.balance

        subtract_balance(
            db_session,
            sample_user.id,
            Decimal("50.00"),
            "Test withdrawal",
            transaction_type="withdrawal",
        )

        db_session.refresh(sample_user)
        assert sample_user.balance == initial_balance - Decimal("50.00")

    def test_subtract_balance_insufficient(self, db_session: Session, sample_user: User):
        sample_user.balance = Decimal("10.00")
        db_session.commit()

        result = subtract_balance(
            db_session,
            sample_user.id,
            Decimal("100.00"),
            "Test withdrawal",
            transaction_type="withdrawal",
        )

        assert result is False
        db_session.refresh(sample_user)
        assert sample_user.balance == Decimal("10.00")

    def test_update_pricing_rejects_failure_above_success(self, db_session: Session):
        get_pricing(db_session)

        with pytest.raises(ValueError, match="price_failure cannot exceed price_success"):
            update_pricing(
                db_session,
                price_success=Decimal("5.00"),
                price_failure=Decimal("6.00"),
            )

    def test_refund_balance_rejects_non_positive_amount(self, db_session: Session, sample_user: User):
        initial_balance = sample_user.balance

        assert refund_balance(db_session, sample_user.id, Decimal("0.00")) is False
        assert refund_balance(db_session, sample_user.id, Decimal("-1.00")) is False

        db_session.refresh(sample_user)
        assert sample_user.balance == initial_balance

    def test_calculate_refund_amount_rejects_negative_refund_math(self):
        broken_pricing = Pricing(price_success=Decimal("2.00"), price_failure=Decimal("5.00"))

        with pytest.raises(ValueError, match="price_failure cannot exceed price_success"):
            calculate_refund_amount(1, broken_pricing)
