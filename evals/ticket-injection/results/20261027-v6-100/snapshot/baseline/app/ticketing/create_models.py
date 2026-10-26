"""Strict ticket-create contracts and deterministic V1 request serialization."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Annotated, Any, Literal

from pydantic import Field, field_validator, model_validator

from app.ticketing.models import SafeErrorV1, StrictModel


class CatalogSelectorV1(StrictModel):
    id: str | int | None = None
    name: Annotated[str | None, Field(min_length=1)] = None

    @model_validator(mode="after")
    def exactly_one_selector(self) -> CatalogSelectorV1:
        if (self.id is None) == (self.name is None):
            raise ValueError("exactly one of id or name is required")
        if isinstance(self.id, str) and not self.id.strip():
            raise ValueError("id must not be empty")
        if self.name is not None and not self.name.strip():
            raise ValueError("name must not be blank")
        return self


class OccurrenceTimeInputV1(StrictModel):
    kind: Literal["timestamp", "now"]
    value: datetime | None = None

    @model_validator(mode="after")
    def valid_variant(self) -> OccurrenceTimeInputV1:
        if self.kind == "now" and self.value is not None:
            raise ValueError("now forbids value")
        if self.kind == "timestamp":
            if self.value is None:
                raise ValueError("timestamp requires value")
            if self.value.utcoffset() is None:
                raise ValueError("timezone is required")
        return self

    def resolve(self, now: datetime) -> datetime:
        selected = now if self.kind == "now" else self.value
        assert selected is not None
        if selected.utcoffset() is None:
            raise ValueError("timezone is required")
        return selected.astimezone(timezone.utc)


class TicketWriteDraftV1(StrictModel):
    title: Annotated[str, Field(min_length=1, max_length=200)]
    description: Annotated[str, Field(min_length=1, max_length=5000)]
    service: CatalogSelectorV1
    component: CatalogSelectorV1 | None = None
    model: CatalogSelectorV1 | None = None
    impact_level: Literal["extensive", "significant", "moderate", "minor"]
    urgency: Literal["critical", "high", "medium", "low"]
    impact_note: Annotated[str, Field(min_length=1, max_length=1000)]
    occurred_at: OccurrenceTimeInputV1
    classification: Literal["standard", "restricted"] = "standard"

    @field_validator("title", "description", "impact_note")
    @classmethod
    def reject_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("must not be blank")
        return value


class ResolvedCatalogV1(StrictModel):
    service_id: Annotated[str, Field(min_length=1, max_length=50)]
    service_name: str | None = None
    component_id: int | None = None
    component_name: str | None = None
    model: Annotated[str | None, Field(max_length=50)] = None
    model_name: str | None = None


class NormalizedTicketCreateV1(StrictModel):
    title: Annotated[str, Field(min_length=1, max_length=200)]
    description: Annotated[str, Field(min_length=1, max_length=5000)]
    service_id: Annotated[str, Field(min_length=1, max_length=50)]
    component_id: int | None = None
    model: Annotated[str | None, Field(max_length=50)] = None
    impact_level: Literal["extensive", "significant", "moderate", "minor"]
    urgency: Literal["critical", "high", "medium", "low"]
    impact_note: Annotated[str, Field(min_length=1, max_length=1000)]
    occurred_at: datetime
    classification: Literal["standard", "restricted"] = "standard"

    @field_validator("occurred_at")
    @classmethod
    def utc_time_required(cls, value: datetime) -> datetime:
        if value.utcoffset() is None:
            raise ValueError("timezone is required")
        return value.astimezone(timezone.utc)

    @classmethod
    def from_draft(
        cls, draft: TicketWriteDraftV1, catalog: ResolvedCatalogV1, *, now: datetime
    ) -> NormalizedTicketCreateV1:
        return cls(
            title=draft.title,
            description=draft.description,
            service_id=catalog.service_id,
            component_id=catalog.component_id,
            model=catalog.model,
            impact_level=draft.impact_level,
            urgency=draft.urgency,
            impact_note=draft.impact_note,
            occurred_at=draft.occurred_at.resolve(now),
            classification=draft.classification,
        )

    def canonical_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "title": self.title,
            "description": self.description,
            "service_id": self.service_id,
        }
        if self.component_id is not None:
            payload["component_id"] = self.component_id
        if self.model is not None:
            payload["model"] = self.model
        payload.update(
            {
                "impact_level": self.impact_level,
                "urgency": self.urgency,
                "impact_note": self.impact_note,
                "occurred_at": self.occurred_at.isoformat().replace("+00:00", "Z"),
                "classification": self.classification,
            }
        )
        return payload

    def canonical_request(self) -> CanonicalRequestBytesV1:
        body = json.dumps(
            self.canonical_payload(), ensure_ascii=False, separators=(",", ":")
        ).encode("utf-8")
        return CanonicalRequestBytesV1(
            serialization_version=1,
            body_bytes=body,
            sha256=hashlib.sha256(body).hexdigest(),
        )

    def payload_hash(self) -> str:
        return self.canonical_request().sha256


@dataclass(frozen=True)
class CanonicalRequestBytesV1:
    serialization_version: Literal[1]
    body_bytes: bytes = field(repr=False)
    sha256: str

    def __post_init__(self) -> None:
        if not self.body_bytes:
            raise ValueError("canonical request bytes must not be empty")
        if hashlib.sha256(self.body_bytes).hexdigest() != self.sha256:
            raise ValueError("canonical request hash mismatch")


class SafeReviewFieldV1(StrictModel):
    key: str
    label_key: str
    before: str | int | bool | None
    after: str | int | bool | None


class SafeMutationReviewV1(StrictModel):
    operation: Literal["create"] = "create"
    normalized_payload_hash: str
    fields: list[SafeReviewFieldV1]
    choices: tuple[Literal["확인"], Literal["수정"], Literal["취소"]] = ("확인", "수정", "취소")

    @field_validator("normalized_payload_hash")
    @classmethod
    def valid_hash(cls, value: str) -> str:
        if len(value) != 64 or any(char not in "0123456789abcdef" for char in value):
            raise ValueError("normalized_payload_hash must be lowercase sha256")
        return value


class SafeMutationResultV1(StrictModel):
    operation: Literal["create"] = "create"
    ticket_id: Annotated[str, Field(min_length=1)]
    idempotent_replayed: bool | None = None
    request_id: str | None = None


class TicketMutationToolResultV1(StrictModel):
    version: Literal[1] = 1
    operation: Literal["create"] = "create"
    state: Literal[
        "review_required",
        "needs_explicit_confirmation",
        "revised",
        "cancelled",
        "succeeded",
        "invalidated",
        "conflict",
        "ambiguous",
        "failed",
    ]
    pending_ref: str | None = None
    review: SafeMutationReviewV1 | None = None
    data: SafeMutationResultV1 | None = None
    error: SafeErrorV1 | None = None

    @model_validator(mode="after")
    def valid_state_shape(self) -> TicketMutationToolResultV1:
        if self.state in ("review_required", "needs_explicit_confirmation", "revised"):
            if not self.pending_ref or self.review is None or self.data is not None:
                raise ValueError("review state requires pending_ref and review")
        elif self.state == "succeeded":
            if self.data is None or any(value is not None for value in (self.pending_ref, self.review, self.error)):
                raise ValueError("succeeded state requires data only")
        elif self.state in ("failed", "conflict", "ambiguous", "invalidated"):
            if self.error is None or self.data is not None or self.review is not None:
                raise ValueError("failure state requires safe error only")
        elif self.state == "cancelled" and any(
            value is not None for value in (self.pending_ref, self.review, self.data, self.error)
        ):
            raise ValueError("cancelled state has no payload")
        return self


class TicketWriteToolInputV1(StrictModel):
    action: Literal["prepare", "confirm", "revise", "cancel"]
    draft: TicketWriteDraftV1 | None = None
    pending_ref: str | None = None

    @model_validator(mode="after")
    def action_shape(self) -> TicketWriteToolInputV1:
        if self.action == "prepare" and (self.draft is None or self.pending_ref is not None):
            raise ValueError("prepare requires draft and forbids pending_ref")
        if self.action == "revise" and (self.draft is None or not self.pending_ref):
            raise ValueError("revise requires draft and pending_ref")
        if self.action in ("confirm", "cancel") and (self.draft is not None or not self.pending_ref):
            raise ValueError(f"{self.action} requires pending_ref and forbids draft")
        return self


@dataclass(frozen=True)
class TicketCreateAttemptV1:
    create_attempt_id: str
    user_id: int
    originating_session_id: str
    originating_conversation_id: str
    normalized_payload_hash: str
    canonical_request_bytes: bytes = field(repr=False)
    serialization_version: int = 1
    request_bytes_hash: str = ""
    idempotency_key: str = field(default="", repr=False)
    confirmed_mutation_id: str | None = None
    confirmed_at: datetime | None = None
    attempt_count: int = 0
    status: str = "prepared"
    claim_id: str | None = None
    claim_deadline_at: datetime | None = None
    refresh_reservation_id: str | None = field(default=None, repr=False)
    refresh_used_at: datetime | None = None
    refresh_state: str = "unused"
    refresh_base_credential_generation: int | None = None
    refresh_committed_credential_generation: int | None = None
    refresh_committed_at: datetime | None = None
    last_outcome: str | None = None
    result_ticket_id: str | None = None
    created_at: datetime | None = None
    retained_until: datetime | None = None

    def __post_init__(self) -> None:
        if self.serialization_version != 1 or not 0 <= self.attempt_count <= 2:
            raise ValueError("invalid create attempt version or count")
        if hashlib.sha256(self.canonical_request_bytes).hexdigest() != self.request_bytes_hash:
            raise ValueError("stored request bytes do not match their hash")


@dataclass(frozen=True)
class CreateSendClaimV1:
    attempt: TicketCreateAttemptV1
    claim_id: str
    deadline_at: datetime

