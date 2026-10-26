"""ITSM API v2 입출력 스키마."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from itsm_api.codes import (
    Classification,
    ImpactLevel,
    PendingReason,
    Priority,
    ProblemStatus,
    ResolutionCode,
    TicketStatus,
    Urgency,
    WorklogKind,
    WorklogVisibility,
)


def _require_timezone(value: datetime | None) -> datetime | None:
    if value is not None and value.tzinfo is None:
        raise ValueError("시간대가 포함된 ISO 8601 이어야 합니다 (예: 2026-10-20T09:00:00+09:00)")
    return value


class StrictIn(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


# ---------- 인증 ----------


class TokenIn(StrictIn):
    grant_type: Literal["client_credentials", "token_exchange"]
    client_id: str
    client_secret: str
    subject: str | None = Field(default=None, description="token_exchange 일 때 대신할 사용자의 username")


class TokenOut(BaseModel):
    access_token: str
    token_type: Literal["Bearer"] = "Bearer"
    expires_in: int
    expires_at: str
    subject: str | None


# ---------- 디렉터리·카탈로그 ----------


class TeamRef(BaseModel):
    id: int
    slug: str
    name: str


class UserRef(BaseModel):
    id: int
    username: str
    display_name: str


class UserOut(UserRef):
    email: str
    role: str
    role_label: str
    team: TeamRef | None
    active: bool


class MeOut(UserOut):
    permissions: list[str]


class UserPage(BaseModel):
    items: list[UserOut]
    page: PageOut


class TeamOut(TeamRef):
    member_count: int


class ComponentOut(BaseModel):
    id: int
    name: str
    owner_team: TeamRef


class ServiceOut(BaseModel):
    id: str
    name: str
    description: str
    status: Literal["active", "deprecated"]
    owner_team: TeamRef
    replacement_service_id: str | None
    components: list[ComponentOut]


class ModelOut(BaseModel):
    id: str
    name: str
    status: Literal["active", "retired"]


# ---------- 티켓 ----------


class ServiceRef(BaseModel):
    id: str
    name: str


class ComponentRef(BaseModel):
    id: int
    name: str


class ProblemRef(BaseModel):
    id: str
    title: str
    status: ProblemStatus


class PageOut(BaseModel):
    limit: int
    next_cursor: str | None
    has_more: bool


class TicketSummaryOut(BaseModel):
    id: str
    title: str
    status: TicketStatus
    status_label: str
    priority: Priority
    priority_label: str
    service: ServiceRef
    component: ComponentRef | None
    model: str | None
    assignment_group: TeamRef | None
    assignee: UserRef | None
    classification: Classification
    is_major: bool
    created_at: str
    updated_at: str
    sla_breached: bool


class SlaOut(BaseModel):
    policy: str
    business_hours: bool
    response_due_at: str
    responded_at: str | None
    response_breached: bool
    resolution_due_at: str | None = Field(description="보류 중이거나 취소된 티켓은 null")
    resolution_breached: bool
    paused: bool


class ResolutionOut(BaseModel):
    code: ResolutionCode
    code_label: str
    note: str
    resolved_at: str | None


class TicketOut(TicketSummaryOut):
    description: str
    impact_level: ImpactLevel
    impact_label: str
    urgency: Urgency
    urgency_label: str
    impact_note: str
    occurred_at: str
    requester: UserRef
    pending_reason: PendingReason | None
    pending_reason_label: str | None
    resolution: ResolutionOut | None
    closed_at: str | None
    problem: ProblemRef | None
    merged_into: str | None
    duplicates: list[str]
    sla: SlaOut
    worklog_count: int
    version: int = Field(description="낙관적 잠금 버전. 변경 요청 시 If-Match 헤더로 보냅니다.")


class TicketPage(BaseModel):
    items: list[TicketSummaryOut]
    page: PageOut


class StatsBucket(BaseModel):
    key: str | None
    label: str
    count: int


class TicketStatsOut(BaseModel):
    group_by: str
    total: int
    buckets: list[StatsBucket]


class TicketCreateIn(StrictIn):
    """티켓 등록 입력.

    요청자·상태·우선순위·담당 그룹·ID·시각(발생 시각 제외)은 서버가 결정하므로 받지 않습니다.
    """

    title: str = Field(min_length=1, max_length=200)
    description: str = Field(min_length=1, max_length=5000)
    service_id: str = Field(min_length=1, max_length=50, description="카탈로그 서비스 ID (GET /api/v2/catalog/services)")
    component_id: int | None = Field(default=None, description="서비스에 속한 컴포넌트 ID")
    model: str | None = Field(default=None, max_length=50, description="관련 모델 ID (해당하는 경우에만)")
    impact_level: ImpactLevel
    urgency: Urgency
    impact_note: str = Field(min_length=1, max_length=1000, description="영향 범위 설명")
    occurred_at: datetime
    classification: Classification = "standard"

    _tz = field_validator("occurred_at")(_require_timezone)

    @field_validator("model")
    @classmethod
    def blank_to_none(cls, value: str | None) -> str | None:
        return value or None


class TicketPatchIn(StrictIn):
    title: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = Field(default=None, min_length=1, max_length=5000)
    component_id: int | None = None
    model: str | None = Field(default=None, max_length=50)
    impact_level: ImpactLevel | None = None
    urgency: Urgency | None = None
    impact_note: str | None = Field(default=None, min_length=1, max_length=1000)
    is_major: bool | None = None


class AssignmentIn(StrictIn):
    assignment_group_id: int
    assignee_id: int | None = None


class TransitionIn(StrictIn):
    to_status: TicketStatus
    assignee_id: int | None = None
    pending_reason: PendingReason | None = None
    resolution_code: ResolutionCode | None = None
    resolution_note: str | None = Field(default=None, min_length=1, max_length=2000)
    reason: str | None = Field(default=None, min_length=1, max_length=1000)
    note: str | None = Field(default=None, min_length=1, max_length=2000)


class TransitionOptionOut(BaseModel):
    to_status: TicketStatus
    to_status_label: str
    required_fields: list[str]
    optional_fields: list[str]


class TransitionsOut(BaseModel):
    ticket_id: str
    current_status: TicketStatus
    current_status_label: str
    version: int
    transitions: list[TransitionOptionOut]


class MergeIn(StrictIn):
    target_id: str = Field(min_length=1, max_length=20)
    note: str | None = Field(default=None, max_length=1000)


class ProblemLinkIn(StrictIn):
    problem_id: str | None = Field(description="null 이면 연결 해제")


class WorklogIn(StrictIn):
    body: str = Field(min_length=1, max_length=5000)
    visibility: WorklogVisibility = "public"


class WorklogOut(BaseModel):
    id: int
    ticket_id: str
    author: UserRef
    visibility: WorklogVisibility
    kind: WorklogKind
    body: str
    created_at: str


class WorklogPage(BaseModel):
    items: list[WorklogOut]
    page: PageOut


class HistoryEventOut(BaseModel):
    id: int
    action: str
    actor: UserRef | None
    client_id: str | None
    changes: dict[str, Any]
    request_id: str | None
    at: str


class HistoryPage(BaseModel):
    items: list[HistoryEventOut]
    page: PageOut


# ---------- 문제(Problem) ----------


class ProblemOut(BaseModel):
    id: str
    title: str
    description: str
    status: ProblemStatus
    status_label: str
    root_cause: str | None
    workaround: str | None
    owner_team: TeamRef
    created_at: str
    updated_at: str
    incident_ids: list[str] = Field(description="현재 주체가 볼 수 있는 연결 티켓만 포함")


class ProblemCreateIn(StrictIn):
    title: str = Field(min_length=1, max_length=200)
    description: str = Field(min_length=1, max_length=5000)
    owner_team_id: int
    workaround: str | None = Field(default=None, max_length=2000)


# ---------- 내보내기 ----------


class ExportFilters(StrictIn):
    status: list[TicketStatus] | None = None
    priority: list[Priority] | None = None
    service_id: str | None = None
    created_from: datetime | None = None
    created_to: datetime | None = None

    _tz = field_validator("created_from", "created_to")(_require_timezone)


class ExportIn(StrictIn):
    format: Literal["csv", "jsonl"] = "csv"
    filters: ExportFilters = Field(default_factory=ExportFilters)


class ExportJobOut(BaseModel):
    id: str
    status: Literal["queued", "running", "succeeded", "failed"]
    progress: int = Field(description="0–100")
    format: Literal["csv", "jsonl"]
    row_count: int | None
    download_url: str | None
    error: str | None
    created_at: str
    finished_at: str | None


UserPage.model_rebuild()
