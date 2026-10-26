"""Strict U3 ticket-update contracts and deterministic patch encoding."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Annotated, Any, Literal

from pydantic import Field, field_validator, model_validator

from app.ticketing.models import SafeErrorV1, StrictModel


PATCH_FIELD_ORDER = (
    "title",
    "description",
    "component_id",
    "model",
    "impact_level",
    "urgency",
    "impact_note",
    "is_major",
)

PATCH_LABELS = {
    "title": "제목",
    "description": "설명",
    "component_id": "컴포넌트",
    "model": "모델",
    "impact_level": "영향 수준",
    "urgency": "긴급도",
    "impact_note": "영향 설명",
    "is_major": "주요 장애",
}


@dataclass(frozen=True)
class CanonicalPatchBytesV1:
    serialization_version: Literal[1]
    body_bytes: bytes = field(repr=False)
    sha256: str

    def __post_init__(self) -> None:
        if self.serialization_version != 1 or not self.body_bytes:
            raise ValueError("invalid canonical patch")
        if hashlib.sha256(self.body_bytes).hexdigest() != self.sha256:
            raise ValueError("canonical patch hash mismatch")


class TicketPatchInV1(StrictModel):
    """Closed ITSM v2 update patch that preserves omitted versus explicit null."""

    title: Annotated[str | None, Field(min_length=1, max_length=200)] = None
    description: Annotated[str | None, Field(min_length=1, max_length=5000)] = None
    component_id: int | None = None
    model: Annotated[str | None, Field(max_length=50)] = None
    impact_level: Literal["extensive", "significant", "moderate", "minor"] | None = None
    urgency: Literal["critical", "high", "medium", "low"] | None = None
    impact_note: Annotated[str | None, Field(min_length=1, max_length=1000)] = None
    is_major: bool | None = None

    @field_validator("title", "description", "impact_note")
    @classmethod
    def reject_blank(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("must not be blank")
        return value

    @model_validator(mode="after")
    def at_least_one_field(self) -> TicketPatchInV1:
        if not self.model_fields_set:
            raise ValueError("patch must contain at least one field")
        return self

    def selected_payload(self) -> dict[str, Any]:
        return {
            name: getattr(self, name)
            for name in PATCH_FIELD_ORDER
            if name in self.model_fields_set
        }

    def actual_diff(self, current_values: dict[str, Any]) -> TicketPatchInV1:
        selected = self.selected_payload()
        changed = {name: value for name, value in selected.items() if current_values.get(name) != value}
        if not changed:
            raise ValueError("patch does not change the current ticket")
        return TicketPatchInV1.model_validate(changed)

    def canonical_patch(self) -> CanonicalPatchBytesV1:
        body = json.dumps(
            self.selected_payload(),
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        return CanonicalPatchBytesV1(
            serialization_version=1,
            body_bytes=body,
            sha256=hashlib.sha256(body).hexdigest(),
        )


class TicketUpdateToolInputV1(StrictModel):
    action: Literal["prepare", "confirm", "revise", "cancel"]
    ticket_id: Annotated[str, Field(min_length=1)]
    patch: TicketPatchInV1 | None = None
    pending_ref: str | None = None

    @field_validator("ticket_id", "pending_ref")
    @classmethod
    def reject_blank_identifier(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("identifier must not be blank")
        return value

    @model_validator(mode="after")
    def action_shape(self) -> TicketUpdateToolInputV1:
        if self.action == "prepare" and (self.patch is None or self.pending_ref is not None):
            raise ValueError("prepare requires patch and forbids pending_ref")
        if self.action == "revise" and (self.patch is None or self.pending_ref is None):
            raise ValueError("revise requires patch and pending_ref")
        if self.action in ("confirm", "cancel") and (self.patch is not None or self.pending_ref is None):
            raise ValueError(f"{self.action} requires pending_ref and forbids patch")
        return self


class SafeUpdateReviewFieldV1(StrictModel):
    key: Literal[
        "title",
        "description",
        "component_id",
        "model",
        "impact_level",
        "urgency",
        "impact_note",
        "is_major",
    ]
    label: str
    before: str | int | bool | None
    after: str | int | bool | None


class SafeUpdateReviewV1(StrictModel):
    ticket_id: str
    base_version: Annotated[int, Field(ge=1)]
    normalized_payload_hash: str
    fields: Annotated[list[SafeUpdateReviewFieldV1], Field(min_length=1)]
    choices: tuple[Literal["확인"], Literal["수정"], Literal["취소"]] = ("확인", "수정", "취소")

    @field_validator("normalized_payload_hash")
    @classmethod
    def validate_hash(cls, value: str) -> str:
        if len(value) != 64 or any(char not in "0123456789abcdef" for char in value):
            raise ValueError("normalized_payload_hash must be lowercase sha256")
        return value


class SafeUpdateResultV1(StrictModel):
    ticket_id: str
    applied_changes: Annotated[list[SafeUpdateReviewFieldV1], Field(min_length=1)]
    new_version: Annotated[int, Field(ge=2)]
    request_id: str | None = None


class TicketUpdateToolResultV1(StrictModel):
    version: Literal[1] = 1
    operation: Literal["update"] = "update"
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
    review: SafeUpdateReviewV1 | None = None
    data: SafeUpdateResultV1 | None = None
    error: SafeErrorV1 | None = None

    @model_validator(mode="after")
    def state_shape(self) -> TicketUpdateToolResultV1:
        if self.state in ("review_required", "needs_explicit_confirmation", "revised"):
            if not self.pending_ref or self.review is None or self.data is not None or self.error is not None:
                raise ValueError("review state requires pending_ref and review only")
        elif self.state == "succeeded":
            if self.data is None or any(value is not None for value in (self.pending_ref, self.review, self.error)):
                raise ValueError("succeeded state requires data only")
        elif self.state in ("failed", "conflict", "ambiguous", "invalidated"):
            if self.error is None or self.data is not None or self.review is not None:
                raise ValueError("failure state requires safe error")
        elif self.state == "cancelled" and any(
            value is not None for value in (self.pending_ref, self.review, self.data, self.error)
        ):
            raise ValueError("cancelled state has no payload")
        return self


class PendingUpdateExecutionV1(StrictModel):
    mutation_id: str
    user_id: int
    originating_session_id: str
    originating_conversation_id: str
    ticket_id: str
    base_version: Annotated[int, Field(ge=1)]
    serialization_version: Literal[1] = 1
    canonical_patch_bytes: bytes = Field(exclude=True, repr=False)
    patch_bytes_hash: str
    if_match_version: str = Field(exclude=True, repr=False)
    status: Literal["staged", "active", "retired"] = "staged"
    created_at: datetime
    expires_at: datetime
    retired_at: datetime | None = None
    refresh_reservation_id: str | None = Field(default=None, exclude=True, repr=False)
    refresh_state: Literal["reserved", "committed", "failed"] | None = None
    refresh_used_at: datetime | None = None
    refresh_base_credential_generation: Annotated[int | None, Field(ge=0)] = None
    refresh_committed_credential_generation: Annotated[int | None, Field(ge=0)] = None
    refresh_committed_at: datetime | None = None

    @field_validator("created_at", "expires_at", "retired_at", "refresh_used_at", "refresh_committed_at")
    @classmethod
    def timezone_required(cls, value: datetime | None) -> datetime | None:
        if value is not None and value.utcoffset() is None:
            raise ValueError("timezone-aware timestamp required")
        return value.astimezone(timezone.utc) if value is not None else None

    @model_validator(mode="after")
    def validate_integrity(self) -> PendingUpdateExecutionV1:
        if self.expires_at <= self.created_at:
            raise ValueError("execution expiry must follow creation")
        if hashlib.sha256(self.canonical_patch_bytes).hexdigest() != self.patch_bytes_hash:
            raise ValueError("stored patch hash mismatch")
        if self.if_match_version != f'"{self.base_version}"':
            raise ValueError("stored If-Match must match base version")
        if self.status == "retired" and self.retired_at is None:
            raise ValueError("retired execution requires retired_at")
        if self.refresh_state is None and any(
            value is not None
            for value in (
                self.refresh_reservation_id,
                self.refresh_used_at,
                self.refresh_base_credential_generation,
                self.refresh_committed_credential_generation,
                self.refresh_committed_at,
            )
        ):
            raise ValueError("refresh evidence requires refresh_state")
        return self

    def verify_patch_hash(self) -> bool:
        return hashlib.sha256(self.canonical_patch_bytes).hexdigest() == self.patch_bytes_hash

    def patch(self) -> TicketPatchInV1:
        if not self.verify_patch_hash() or self.serialization_version != 1:
            raise ValueError("stored patch integrity failure")
        raw = json.loads(self.canonical_patch_bytes.decode("utf-8"))
        if not isinstance(raw, dict):
            raise ValueError("stored patch must be an object")
        patch = TicketPatchInV1.model_validate(raw)
        if patch.canonical_patch().body_bytes != self.canonical_patch_bytes:
            raise ValueError("stored patch is not canonical")
        return patch


class UpdateCredentialRefreshReservationV1(StrictModel):
    mutation_id: str
    refresh_reservation_id: str = Field(exclude=True, repr=False)
    refresh_used_at: datetime
    base_credential_generation: Annotated[int, Field(ge=0)]
    refresh_state: Literal["reserved", "committed", "failed"]
    committed_credential_generation: Annotated[int | None, Field(ge=0)] = None
    refresh_committed_at: datetime | None = None

    @field_validator("refresh_used_at", "refresh_committed_at")
    @classmethod
    def timezone_required(cls, value: datetime | None) -> datetime | None:
        if value is not None and value.utcoffset() is None:
            raise ValueError("timezone-aware timestamp required")
        return value.astimezone(timezone.utc) if value is not None else None

    @model_validator(mode="after")
    def committed_shape(self) -> UpdateCredentialRefreshReservationV1:
        if self.refresh_state == "committed" and (
            self.committed_credential_generation is None or self.refresh_committed_at is None
        ):
            raise ValueError("committed refresh requires generation and timestamp")
        return self


@dataclass
class UpdateAttemptPolicyV1:
    patch_attempt_count: int = 0
    refresh_invocation_count: int = 0

    def record_patch(self) -> None:
        if self.patch_attempt_count >= 2:
            raise RuntimeError("PATCH attempt ceiling exceeded")
        self.patch_attempt_count += 1

    def record_refresh(self) -> None:
        if self.refresh_invocation_count >= 1:
            raise RuntimeError("update refresh ceiling exceeded")
        self.refresh_invocation_count += 1
