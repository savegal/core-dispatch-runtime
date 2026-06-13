from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


@dataclass(slots=True)
class ActionResult:
    success: bool = False
    action: str = ""
    message: str = ""
    code: str = ""
    details: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class LimitsSnapshot:
    mode: str = "active"
    max_create_per_tick: int = 20
    max_create_per_creator: int = 3
    max_create_per_hour: int = 120


@dataclass(slots=True)
class IncidentSummary:
    type: str = ""
    title: str = ""
    severity: str = ""
    state: str = ""
    cause: str = ""
    auto_action: str = ""
    admin_action: str = ""
    cooldown_until: str | None = None


@dataclass(slots=True)
class TaskPreview:
    task_id: int = 0
    creator_id: int | None = None
    creator_username: str = ""
    template_name: str = ""
    status: str = ""
    progress: int = 0
    cost: float = 0.0
    mains_published: int | None = None
    mains_expected: int | None = None
    replies_published: int | None = None
    replies_expected: int | None = None
    mains_confirmed: int = 0
    mains_unconfirmed: int = 0
    replies_confirmed: int = 0
    replies_unconfirmed: int = 0
    needs_review: bool = False
    created_at: datetime | None = None
    updated_at: datetime | None = None
    video_urls: list[str] = field(default_factory=list)


@dataclass(slots=True)
class OutboxPreview:
    id: int = 0
    creator_id: int | None = None
    creator_username: str = ""
    status: str = ""
    video_count: int = 0
    video_urls: list[str] = field(default_factory=list)
    created_at: datetime | None = None


@dataclass(slots=True)
class CurrentWorkSnapshot:
    active_task: TaskPreview | None = None
    processing_tasks: list[TaskPreview] = field(default_factory=list)
    queue_head: list[TaskPreview] = field(default_factory=list)
    outbox_active: list[OutboxPreview] = field(default_factory=list)
    pending_count: int = 0
    processing_count: int = 0
    active_task_id: int | None = None


@dataclass(slots=True)
class SummarySnapshot:
    period: str = "24h"
    total: int = 0
    status_counts: dict[str, int] = field(default_factory=dict)
    published_mains: int | None = None
    published_replies: int | None = None
    cost: float = 0.0
    refunded: float = 0.0
    net_cost: float = 0.0


@dataclass(slots=True)
class TaskCard:
    task_id: int = 0
    status: str = ""
    creator_id: int | None = None
    creator_username: str = ""
    template_name: str = ""
    url: str = ""
    created_at: datetime | None = None
    updated_at: datetime | None = None
    duration_seconds: int | None = None
    mains_published: int | None = None
    mains_expected: int | None = None
    replies_published: int | None = None
    replies_expected: int | None = None
    mains_confirmed: int = 0
    mains_unconfirmed: int = 0
    replies_confirmed: int = 0
    replies_unconfirmed: int = 0
    needs_review: bool = False
    publications: dict[str, Any] = field(default_factory=dict)
    coverage: dict[str, Any] = field(default_factory=dict)
    health: dict[str, Any] = field(default_factory=dict)
    cost: float = 0.0
    refunded: float = 0.0
    net_cost: float = 0.0
    error_reason: str = ""
    error_summary: str = ""


@dataclass(slots=True)
class TaskListResult:
    items: list[TaskCard] = field(default_factory=list)
    total: int = 0
    page: int = 0
    pages: int = 1
    limit: int = 5


@dataclass(slots=True)
class CreatorReportRow:
    creator_id: int = 0
    username: str = ""
    mode: str = ""
    enabled: bool = False
    total: int = 0
    done: int = 0
    partial: int = 0
    error: int = 0
    stopped: int = 0
    cost: float = 0.0
    refunded: float = 0.0
    last_at: datetime | None = None
    last_status: str = ""
    fail_streak: int = 0


@dataclass(slots=True)
class FinanceSnapshot:
    period: str = "24h"
    charged: float = 0.0
    refunded: float = 0.0
    cost: float = 0.0
    task_count: int = 0
    publication_count: int | None = None
    avg_publication_cost: float | None = None
    daily: list[dict[str, Any]] = field(default_factory=list)
    refund_reasons: dict[str, float] = field(default_factory=dict)


@dataclass(slots=True)
class DashboardSnapshot:
    state: str = "ok"
    state_label: str = "Работает"
    state_icon: str = "✅"
    control: LimitsSnapshot = field(default_factory=LimitsSnapshot)
    incidents: list[IncidentSummary] = field(default_factory=list)
    current_work: CurrentWorkSnapshot = field(default_factory=CurrentWorkSnapshot)
    summary: SummarySnapshot = field(default_factory=SummarySnapshot)
    queue_count: int = 0
    redis_alive: bool = True
    monitor_alive: bool = True
    executor_alive: bool = True
    metadata: dict[str, Any] = field(default_factory=dict)
