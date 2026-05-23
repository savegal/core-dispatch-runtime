from fastapi import APIRouter

from . import actions, current, dashboard, limits, report, work

router = APIRouter()
router.include_router(dashboard.router)
router.include_router(limits.router)
router.include_router(current.router)
router.include_router(report.router)
router.include_router(actions.router)
router.include_router(work.router)

__all__ = ["router"]
