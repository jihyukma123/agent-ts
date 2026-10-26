"""모델에 노출되는 티켓 V1 계약과 서버 내부 저장 DTO."""

from __future__ import annotations

from datetime import datetime, timezone
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class TicketStatus(StrEnum):
    RECEIVED = "received"
    ASSIGNED = "assigned"
    INVESTIGATING = "investigating"
    PENDING = "pending"
    MONITORING = "monitoring"
    RESOLVED = "resolved"
    CLOSED = "closed"
    CANCELLED = "cancelled"


class TicketPriority(StrEnum):
    P1 = "P1"
    P2 = "P2"
    P3 = "P3"
    P4 = "P4"


class TicketReadToolInputV1(StrictModel):
    ticket_id: str | None = None
    q: Annotated[str | None, Field(max_length=200)] = None
    status: list[TicketStatus] | None = None
    priority: list[TicketPriority] | None = None
    service_id: str | None = None
    component_id: int | None = None
    model: str | None = None
    assignment_group_id: int | None = None
    assignee: str | None = None
    requester: str | None = None
    classification: Literal["standard", "restricted"] | None = None
    is_major: bool | None = None
    created_from: datetime | None = None
    created_to: datetime | None = None
    updated_from: datetime | None = None
    sort: Literal["-created_at", "-updated_at", "priority"] | None = None
    limit: Annotated[int | None, Field(ge=1, le=100)] = None
    continuation_ref: str | None = None

    @field_validator("ticket_id", "continuation_ref")
    @classmethod
    def non_empty_opaque_value(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("must not be empty")
        return value

    @field_validator("created_from", "created_to", "updated_from")
    @classmethod
    def timezone_required(cls, value: datetime | None) -> datetime | None:
        if value is not None and value.utcoffset() is None:
            raise ValueError("timezone is required")
        return value

    @model_validator(mode="after")
    def exactly_one_mode(self) -> TicketReadToolInputV1:
        list_values = (
            self.q,
            self.status,
            self.priority,
            self.service_id,
            self.component_id,
            self.model,
            self.assignment_group_id,
            self.assignee,
            self.requester,
            self.classification,
            self.is_major,
            self.created_from,
            self.created_to,
            self.updated_from,
            self.sort,
            self.limit,
        )
        if self.ticket_id is not None and (self.continuation_ref is not None or any(v is not None for v in list_values)):
            raise ValueError("ticket_id selects detail mode exclusively")
        if self.continuation_ref is not None and (self.ticket_id is not None or any(v is not None for v in list_values)):
            raise ValueError("continuation_ref selects continuation mode exclusively")
        return self

    @property
    def mode(self) -> Literal["list", "detail"]:
        return "detail" if self.ticket_id is not None else "list"


class CanonicalTicketQueryV1(StrictModel):
    q: str | None = None
    status: list[TicketStatus] | None = None
    priority: list[TicketPriority] | None = None
    service_id: str | None = None
    component_id: int | None = None
    model: str | None = None
    assignment_group_id: int | None = None
    assignee: str | None = None
    requester: str | None = None
    classification: Literal["standard", "restricted"] | None = None
    is_major: bool | None = None
    created_from: datetime | None = None
    created_to: datetime | None = None
    updated_from: datetime | None = None
    sort: Literal["-created_at", "-updated_at", "priority"] = "-created_at"
    limit: Annotated[int, Field(ge=1, le=100)] = 20

    @field_validator("created_from", "created_to", "updated_from")
    @classmethod
    def normalize_utc(cls, value: datetime | None) -> datetime | None:
        if value is not None and value.utcoffset() is None:
            raise ValueError("timezone is required")
        return value.astimezone(timezone.utc) if value is not None else None

    @classmethod
    def from_tool_input(cls, value: TicketReadToolInputV1) -> CanonicalTicketQueryV1:
        data = value.model_dump(exclude={"ticket_id", "continuation_ref"}, exclude_none=True)
        return cls.model_validate(data)

    def canonical_json(self) -> str:
        return self.model_dump_json(exclude_none=True)


class SafeFieldErrorV1(StrictModel):
    field: str
    code: str
    message_key: str
    allowed_values: list[str] | None = None


class SafeErrorV1(StrictModel):
    version: Literal[1] = 1
    code: Literal[
        "invalid_input",
        "authentication_required",
        "permission_denied",
        "not_found",
        "conflict",
        "rate_limited",
        "upstream_unavailable",
        "ambiguous_result",
    ]
    message_key: str
    retryable: bool
    request_id: str | None = None
    retry_after_seconds: Annotated[int | None, Field(ge=0)] = None
    field_errors: list[SafeFieldErrorV1] | None = None


class SafeTicketSummaryV1(StrictModel):
    id: str
    title: str
    status: TicketStatus
    priority: TicketPriority
    service: str
    created_at: datetime
    updated_at: datetime


class SafeTicketDetailV1(StrictModel):
    id: str
    title: str
    description: str
    service: str
    impact_level: Literal["extensive", "significant", "moderate", "minor"]
    urgency: Literal["critical", "high", "medium", "low"]
    impact_note: str
    status: TicketStatus
    priority: TicketPriority
    version: Annotated[int, Field(ge=1)]
    component: str | None = None
    model: str | None = None
    classification: Literal["standard", "restricted"] | None = None
    is_major: bool | None = None
    requester: str | None = None
    assignment_group: str | None = None
    assignee: str | None = None
    occurred_at: datetime | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None


class ReadForUpdateResultV1(StrictModel):
    """U3 전용 서버 내부 수정 선행 조회 결과.

    ``if_match_version``은 public detail version과 정확히 같은 strong ETag만
    담는다. 이 타입은 function tool이나 HTTP response schema에 등록하지 않는다.
    """

    detail: SafeTicketDetailV1
    if_match_version: str = Field(exclude=True)
    server_current_values: dict[str, str | int | bool | None] = Field(
        default_factory=dict,
        exclude=True,
    )

    @model_validator(mode="after")
    def version_matches_strong_etag(self) -> ReadForUpdateResultV1:
        if self.if_match_version != f'"{self.detail.version}"':
            raise ValueError("if_match_version must be the canonical strong ETag for detail.version")
        return self


class SafePageV1(StrictModel):
    limit: Annotated[int, Field(ge=1, le=100)]
    has_more: bool
    continuation_ref: str | None = None

    @model_validator(mode="after")
    def continuation_matches_has_more(self) -> SafePageV1:
        if self.has_more != (self.continuation_ref is not None):
            raise ValueError("has_more must match continuation_ref presence")
        return self


class TicketReadToolResultV1(StrictModel):
    version: Literal[1] = 1
    state: Literal["succeeded", "empty", "failed"]
    mode: Literal["list", "detail"]
    untrusted_data: Literal[True] = True
    items: list[SafeTicketSummaryV1] | None = None
    detail: SafeTicketDetailV1 | None = None
    page: SafePageV1 | None = None
    error: SafeErrorV1 | None = None

    @model_validator(mode="after")
    def state_shape(self) -> TicketReadToolResultV1:
        if self.state == "failed":
            if self.error is None or any(v is not None for v in (self.items, self.detail, self.page)):
                raise ValueError("failed result requires error only")
        elif self.mode == "detail":
            if self.state != "succeeded" or self.detail is None or any(v is not None for v in (self.items, self.page, self.error)):
                raise ValueError("detail success requires detail only")
        else:
            if self.items is None or self.page is None or any(v is not None for v in (self.detail, self.error)):
                raise ValueError("list success requires items and page")
            if self.state == "empty" and self.items:
                raise ValueError("empty result cannot contain items")
            if self.state == "succeeded" and not self.items:
                raise ValueError("succeeded list requires items")
        return self


class TextValueV1(StrictModel):
    text: str
    origin: Literal["server_allowlist", "itsm_untrusted"]


class FieldV1(StrictModel):
    key: str
    label: str
    value: TextValueV1


class ParagraphBlockV1(StrictModel):
    type: Literal["paragraph"] = "paragraph"
    text: TextValueV1


class FieldsBlockV1(StrictModel):
    type: Literal["fields"] = "fields"
    fields: list[FieldV1]


class ItemsBlockV1(StrictModel):
    type: Literal["items"] = "items"
    items: list[list[FieldV1]]


class ActionV1(StrictModel):
    action: Literal["provide_input", "confirm", "revise", "cancel", "retry_later", "start_over"]
    label: str


class ActionsBlockV1(StrictModel):
    type: Literal["actions"] = "actions"
    actions: list[ActionV1]


PresentationBlockV1 = Annotated[
    ParagraphBlockV1 | FieldsBlockV1 | ItemsBlockV1 | ActionsBlockV1,
    Field(discriminator="type"),
]


class TicketPresentationV1(StrictModel):
    version: Literal[1] = 1
    kind: Literal[
        "ticket_progress",
        "ticket_list",
        "ticket_detail",
        "ticket_input",
        "ticket_review",
        "ticket_success",
        "ticket_unavailable",
        "ticket_auth_error",
        "ticket_conflict",
        "ticket_rate_limited",
        "ticket_ambiguous",
    ]
    blocks: list[PresentationBlockV1]


class PendingTicketMutationV1(StrictModel):
    mutation_id: str
    operation: Literal["create", "update"]
    user_id: int
    session_id: str
    conversation_id: str
    normalized_payload_hash: str
    ticket_id: str | None = None
    base_version: Annotated[int | None, Field(ge=1)] = None
    status: Literal["pending", "confirmed", "cancelled", "invalidated", "expired", "consumed"] = "pending"
    expires_at: datetime

    @model_validator(mode="after")
    def variant_shape(self) -> PendingTicketMutationV1:
        if self.operation == "create" and (self.ticket_id is not None or self.base_version is not None):
            raise ValueError("create mutation has no ticket/version")
        if self.operation == "update" and (not self.ticket_id or self.base_version is None):
            raise ValueError("update mutation requires ticket/version")
        return self


class PendingCreateMutationV1(PendingTicketMutationV1):
    """Create-specific pending review owned by the U1 common contract."""

    operation: Literal["create"] = "create"
    ticket_id: None = None
    base_version: None = None


class PendingUpdateMutationV1(PendingTicketMutationV1):
    """Update-specific pending review with an exact ticket/version binding."""

    operation: Literal["update"] = "update"
    ticket_id: Annotated[str, Field(min_length=1)]
    base_version: Annotated[int, Field(ge=1)]


class UpdatePendingReplacementV1(StrictModel):
    """Immutable C-02 evidence emitted only after U1 commits replacement."""

    old_mutation_id: str | None
    new_mutation_id: str
    user_id: int
    session_id: str
    conversation_id: str
    operation: Literal["update"] = "update"
    new_ticket_id: str
    new_normalized_payload_hash: str
    new_base_version: Annotated[int, Field(ge=1)]
    committed: Literal[True] = True
    affected_old_rows: Literal[0, 1]
    inserted_new_rows: Literal[1] = 1

    @model_validator(mode="after")
    def replacement_shape(self) -> UpdatePendingReplacementV1:
        if (self.old_mutation_id is None) != (self.affected_old_rows == 0):
            raise ValueError("replacement old-row evidence is inconsistent")
        return self

    @property
    def mutation_id(self) -> str:
        """Compatibility accessor retained for the approved U1 provider seam."""
        return self.new_mutation_id

    @property
    def replaced_mutation_id(self) -> str | None:
        return self.old_mutation_id

    def model_dump(self, *args, **kwargs):
        """Preserve the pre-U3 diagnostic shape for existing U1 callers.

        The authoritative C-02 fields remain the declared model fields and are
        available through attributes/model serialization. This zero-argument
        compatibility path is retained because the U1 checkpoint asserted its
        earlier diagnostic pair directly.
        """
        if not args and not kwargs:
            return {
                "mutation_id": self.new_mutation_id,
                "replaced_mutation_id": self.old_mutation_id,
            }
        return super().model_dump(*args, **kwargs)


class PendingMutationBindingV1(StrictModel):
    """U2/U3가 confirm/consume 때 다시 제시해야 하는 exact server binding."""

    mutation_id: str
    operation: Literal["create", "update"]
    user_id: int
    session_id: str
    conversation_id: str
    normalized_payload_hash: str
    ticket_id: str | None = None
    base_version: Annotated[int | None, Field(ge=1)] = None

    @model_validator(mode="after")
    def variant_shape(self) -> PendingMutationBindingV1:
        if self.operation == "create" and (self.ticket_id is not None or self.base_version is not None):
            raise ValueError("create binding has no ticket/version")
        if self.operation == "update" and (not self.ticket_id or self.base_version is None):
            raise ValueError("update binding requires ticket/version")
        return self


class ConsumedMutationEvidenceV1(StrictModel):
    """U2/U3 ledger binding에 필요한 최소 one-time consumption evidence."""

    mutation_id: str
    confirmed_at: datetime
