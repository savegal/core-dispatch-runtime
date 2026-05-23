from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from api.auth import ensure_user_scope, verify_user_header
from db.database import get_db
from services.autopilot.dashboard import get_limits, update_limits

router = APIRouter(prefix="/api/autopilot", tags=["autopilot"])


class AutopilotLimitsUpdate(BaseModel):
    mode: str | None = None
    max_create_per_tick: int | None = Field(None, ge=0, le=1000)
    max_create_per_creator: int | None = Field(None, ge=0, le=1000)
    max_create_per_hour: int | None = Field(None, ge=0, le=10000)


@router.get("/limits")
async def read_limits(
    user_id: int = Query(..., description="Owner user ID"),
    db: AsyncSession = Depends(get_db),
    auth_user_id: int = Depends(verify_user_header),
) -> dict:
    ensure_user_scope(user_id, auth_user_id)
    return await get_limits(db)


@router.post("/limits")
async def write_limits(
    payload: AutopilotLimitsUpdate,
    user_id: int = Query(..., description="Owner user ID"),
    db: AsyncSession = Depends(get_db),
    auth_user_id: int = Depends(verify_user_header),
) -> dict:
    ensure_user_scope(user_id, auth_user_id)
    try:
        return await update_limits(
            db,
            owner_user_id=user_id,
            mode=payload.mode,
            max_create_per_tick=payload.max_create_per_tick,
            max_create_per_creator=payload.max_create_per_creator,
            max_create_per_hour=payload.max_create_per_hour,
            updated_by=auth_user_id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
