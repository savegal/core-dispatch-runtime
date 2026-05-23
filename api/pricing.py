"""
API для управления ценами за успешные и неуспешные заказы.
"""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, field_validator, model_validator
from sqlalchemy.ext.asyncio import AsyncSession
from db.database import get_db
from api.auth import verify_admin_header
from decimal import Decimal
import logging
from services.balance_service import get_pricing_async, update_pricing_async

router = APIRouter(prefix="/api/pricing", tags=["pricing"])
logger = logging.getLogger(__name__)


class PricingResponse(BaseModel):
    """Модель ответа с ценами."""
    price_success: Decimal
    price_failure: Decimal


class PricingUpdate(BaseModel):
    """Модель для обновления цен."""
    price_success: Decimal | None = None
    price_failure: Decimal | None = None
    
    @field_validator('price_success', 'price_failure')
    @classmethod
    def validate_price(cls, v: Decimal | None) -> Decimal | None:
        """Валидирует цену."""
        if v is not None:
            if v < 0:
                raise ValueError('Price cannot be negative')
            if v > 10000:
                raise ValueError('Price cannot exceed 10000')
        return v

    @model_validator(mode="after")
    def validate_pricing_relationship(self):
        success = self.price_success
        failure = self.price_failure
        if success is not None and failure is not None and failure > success:
            raise ValueError("price_failure cannot exceed price_success")
        return self


@router.get("", response_model=PricingResponse)
async def get_pricing(
    db: AsyncSession = Depends(get_db),
    _: None = Depends(verify_admin_header)
):
    """
    Получает текущие цены за успешные и неуспешные заказы.
    """
    pricing = await get_pricing_async(db)
    
    return PricingResponse(
        price_success=pricing.price_success,
        price_failure=pricing.price_failure
    )


@router.put("", response_model=PricingResponse)
async def update_pricing(
    pricing_data: PricingUpdate,
    db: AsyncSession = Depends(get_db),
    admin_token: str = Depends(verify_admin_header)
):
    """
    Обновляет цены за успешные и неуспешные заказы.
    Требует админ токен.
    """
    try:
        pricing = await update_pricing_async(
            db,
            price_success=pricing_data.price_success,
            price_failure=pricing_data.price_failure,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
        
    logger.info(f"Pricing updated: success={pricing.price_success}, failure={pricing.price_failure}")
        
    return PricingResponse(
        price_success=pricing.price_success,
        price_failure=pricing.price_failure
    )
