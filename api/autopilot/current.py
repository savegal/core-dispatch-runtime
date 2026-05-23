from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from api.auth import ensure_user_scope, verify_user_header
from db.database import get_db
from services.autopilot.dashboard import collect_current

router = APIRouter(prefix="/api/autopilot", tags=["autopilot"])


@router.get("/current")
async def get_current(
    user_id: int = Query(..., description="Owner user ID"),
    db: AsyncSession = Depends(get_db),
    auth_user_id: int = Depends(verify_user_header),
) -> dict:
    ensure_user_scope(user_id, auth_user_id)
    return await collect_current(db, user_id)
