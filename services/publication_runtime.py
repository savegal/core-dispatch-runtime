"""Operator settings shared by order execution, visibility checks and reports."""
import json

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from config.settings import get_settings
from db.models import AppSettings

RUNTIME_KEY = "publication_runtime"
ORDER_OPTIONS_KEY = "publication_options"
MAX_SEND_WORKERS = 80
MAX_CHECK_WORKERS = 16


def publication_capacity(browser_slots, check_workers=1):
    slots = max(1, int(browser_slots))
    readers = min(check_workers, max(1, slots - 1))
    senders = min(MAX_SEND_WORKERS, max(1, slots - readers))
    return senders, readers


class OrderPublicationOptions(BaseModel):
    """Overrides for one order. Browser capacity is enforced by the executor."""
    model_config = ConfigDict(extra="forbid")

    send_workers: int | None = Field(default=None, ge=1, le=MAX_SEND_WORKERS, strict=True)
    check_workers: int | None = Field(default=None, ge=1, le=MAX_CHECK_WORKERS, strict=True)
    send_interval: float | None = Field(default=None, ge=0, le=30, strict=True)
    check_delay: float | None = Field(default=None, ge=0, le=60, strict=True)
    live_interval: float | None = Field(default=None, ge=2, le=30, strict=True)


class PublicationRuntime(BaseModel):
    model_config = ConfigDict(extra="forbid")

    order_workers: int = Field(default=2, ge=1, le=32, strict=True)
    send_workers: int = Field(default_factory=lambda: publication_capacity(get_settings().MAX_CONCURRENT_BROWSERS)[0],
                              ge=1, le=MAX_SEND_WORKERS, strict=True)
    check_workers: int = Field(default=1, ge=1, le=MAX_CHECK_WORKERS, strict=True)
    check_batch_size: int = Field(default=8, ge=1, le=100, strict=True)
    observer_attempts: int = Field(default=6, ge=2, le=30, strict=True)
    author_checks: int = Field(default=4, ge=0, le=32, strict=True)
    main_pages: int = Field(default=5, ge=1, le=100, strict=True)
    reply_pages: int = Field(default=5, ge=1, le=100, strict=True)
    send_interval: float = Field(default=0, ge=0, le=30)
    check_delay: float = Field(default=0, ge=0, le=60)
    late_workers: int = Field(default=1, ge=1, le=8, strict=True)
    late_poll: float = Field(default=5, ge=1, le=60)
    late_delays: tuple[int, ...] = Field(default=(120, 600, 3600), min_length=1, max_length=8)
    live_interval: float = Field(default=3, ge=2, le=30)

    @field_validator("late_delays")
    @classmethod
    def validate_delays(cls, values):
        if any(value < 5 or value > 86400 for value in values) or tuple(sorted(set(values))) != values:
            raise ValueError("Delays must increase and stay between 5 and 86400 seconds")
        return values

    def limits(self, browser_slots):
        senders, readers = publication_capacity(browser_slots, self.check_workers)
        return min(self.send_workers, senders), readers


def default_runtime():
    settings = get_settings()
    return PublicationRuntime(order_workers=max(1, min(32, int(settings.MAX_THREADS))),
                              main_pages=settings.MAIN_VISIBILITY_MAX_PAGES,
                              reply_pages=settings.REPLY_PUBLICATION_VISIBILITY_MAX_PAGES)


def load_publication_runtime(db=None):
    fallback = default_runtime()
    if not isinstance(db, Session):
        return fallback
    raw = db.scalar(select(AppSettings.value).where(AppSettings.key == RUNTIME_KEY))
    if raw:
        try:
            return PublicationRuntime.model_validate(fallback.model_dump(exclude_unset=True) | json.loads(raw))
        except (ValidationError, ValueError, TypeError):
            pass
    return fallback


def order_runtime(settings, defaults):
    options = OrderPublicationOptions.model_validate((settings or {}).get(ORDER_OPTIONS_KEY) or {})
    return defaults.model_copy(update=options.model_dump(exclude_none=True))


def normalize_order_options(settings):
    if ORDER_OPTIONS_KEY in settings:
        options = OrderPublicationOptions.model_validate(settings[ORDER_OPTIONS_KEY]).model_dump(exclude_none=True)
        if options:
            settings[ORDER_OPTIONS_KEY] = options
        else:
            settings.pop(ORDER_OPTIONS_KEY)
    return settings


def update_publication_runtime(db, changes):
    from sqlalchemy.dialects.postgresql import insert as pg_insert
    from sqlalchemy.dialects.sqlite import insert as sqlite_insert

    insert = sqlite_insert if db.get_bind().dialect.name == "sqlite" else pg_insert
    db.execute(insert(AppSettings).values(key=RUNTIME_KEY, value="{}")
               .on_conflict_do_nothing(index_elements=["key"]))
    row = db.scalar(select(AppSettings).where(AppSettings.key == RUNTIME_KEY).with_for_update()
                    .execution_options(populate_existing=True))
    current = load_publication_runtime(db).model_dump(exclude_unset=True)
    runtime = PublicationRuntime.model_validate(current | changes)
    row.value = runtime.model_dump_json(exclude_unset=True)
    db.commit()
    return runtime


def runtime_payload(runtime):
    slots = max(1, int(get_settings().MAX_CONCURRENT_BROWSERS))
    senders, readers = runtime.limits(slots)
    return runtime.model_dump(mode="json") | dict(browser_slots=slots, effective_senders=senders,
                                                  effective_readers=readers)
