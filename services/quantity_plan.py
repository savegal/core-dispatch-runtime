"""Publication quantities are independent from reusable text libraries."""
from pydantic import BaseModel, ConfigDict, Field, model_validator

PLAN_KEY = "publication_plan"


def quantity_reply_texts(task):
    """Quantity text libraries do not inherit legacy reply_count truncation."""
    settings = task.settings or {}
    for key in ("replies", "reply_variants"):
        values = settings.get(key)
        if isinstance(values, list):
            texts = [str(value).strip() for value in values if str(value or "").strip()]
            if texts:
                return texts[:20]
    values = [getattr(task, f"reply{index}", None) or settings.get(f"reply{index}") for index in range(1, 21)]
    return [str(value).strip() for value in values if str(value or "").strip()]


class QuantityPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")
    main_count: int = Field(ge=1, le=500, strict=True)
    replies_per_main: int = Field(default=0, ge=0, le=20, strict=True)
    max_dispatches: int | None = Field(default=None, ge=1, le=20000, strict=True)
    max_seconds: int = Field(default=1800, ge=60, le=21600, strict=True)
    traffic_limit_mb: int | None = Field(default=None, ge=1, le=100000, strict=True)

    @model_validator(mode="after")
    def set_attempt_budget(self):
        requested = self.main_count * (1 + self.replies_per_main)
        if self.max_dispatches is None:
            self.max_dispatches = min(20000, requested * 10)
        if self.max_dispatches < requested:
            raise ValueError("Лимит отправок не может быть меньше запрошенного количества публикаций")
        return self


def quantity_plan(settings):
    value = (settings or {}).get(PLAN_KEY)
    return QuantityPlan.model_validate(value) if value is not None else None


def traffic_budget_exhausted(db, task):
    from sqlalchemy import func
    from db.models import TaskTrafficBucket
    plan = quantity_plan(task.settings)
    if plan is None or plan.traffic_limit_mb is None:
        return False
    used = db.query(func.coalesce(func.sum(
        TaskTrafficBucket.received_bytes + TaskTrafficBucket.sent_bytes), 0)).filter(
        TaskTrafficBucket.task_id == task.id).scalar()
    return int(used) >= plan.traffic_limit_mb * 1048576


def traffic_control_reason(db, task, *, checkpointed=True):
    plan = quantity_plan(task.settings)
    if plan is None or plan.traffic_limit_mb is None:
        return None
    if not checkpointed:
        return "quantity_traffic_accounting_unavailable"
    if traffic_budget_exhausted(db, task):
        return "quantity_traffic_budget_exhausted"
    return None


def ensure_quantity_plan(settings, replies):
    """Give older callers a bounded default; new composers pass explicit counts."""
    settings = dict(settings or {})
    if PLAN_KEY not in settings:
        main_count = 1 if settings.get("publish_all_text_variants") is False else 3
        settings[PLAN_KEY] = QuantityPlan(main_count=main_count, replies_per_main=min(20, len(replies)),
            max_seconds=min(21600, max(1800, main_count * (1 + len(replies)) * 100))).model_dump()
    settings["verify_main_visibility"] = True
    settings["verify_reply_visibility"] = True
    return settings
