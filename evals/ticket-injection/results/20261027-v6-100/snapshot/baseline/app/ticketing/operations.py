"""ticket_read validation, retry, refresh, projection and cursor policy."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta, timezone
from typing import Any

from pydantic import ValidationError

from app.ticketing.capability import TicketRequestCapability
from app.ticketing.gateway import GatewayAttemptDeadline, GatewayFailure, GatewaySuccess, ItsmGateway
from app.ticketing.models import (
    CanonicalTicketQueryV1,
    ReadForUpdateResultV1,
    SafeErrorV1,
    SafePageV1,
    SafeTicketDetailV1,
    SafeTicketSummaryV1,
    TicketReadToolInputV1,
    TicketReadToolResultV1,
)
from app.ticketing.repositories import CursorRepository

Sleeper = Callable[[float], Awaitable[None]]
EvidenceSink = Callable[[str, int, str], None]
MonotonicClock = Callable[[], float]
UtcClock = Callable[[], datetime]


def _name(value: Any) -> str | None:
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        for key in ("name", "display_name", "label", "id"):
            if value.get(key) is not None:
                return str(value[key])
    return None


def _failed(mode: str, error: SafeErrorV1) -> TicketReadToolResultV1:
    return TicketReadToolResultV1(state="failed", mode=mode, error=error)


class TicketOperations:
    def __init__(
        self,
        gateway: ItsmGateway,
        cursor_repository: CursorRepository,
        *,
        sleeper: Sleeper = asyncio.sleep,
        evidence_sink: EvidenceSink | None = None,
        monotonic: MonotonicClock = time.monotonic,
        utc_now: UtcClock = lambda: datetime.now(timezone.utc),
        timeout_seconds: float = 10.0,
    ):
        self.gateway = gateway
        self.cursors = cursor_repository
        self.sleeper = sleeper
        self.evidence_sink = evidence_sink
        self.monotonic = monotonic
        self.utc_now = utc_now
        self.timeout_seconds = timeout_seconds

    async def ticket_read(
        self, capability: TicketRequestCapability, value: TicketReadToolInputV1
    ) -> TicketReadToolResultV1:
        if value.ticket_id is not None:
            return await self._read_detail(capability, value.ticket_id)
        upstream_cursor = None
        if value.continuation_ref is not None:
            resolved = self.cursors.resolve_once(capability, value.continuation_ref)
            if resolved is None:
                return _failed(
                    "list",
                    SafeErrorV1(code="invalid_input", message_key="ticket_continuation_invalid", retryable=False),
                )
            query, upstream_cursor = resolved.query, resolved.upstream_cursor
        else:
            try:
                query = CanonicalTicketQueryV1.from_tool_input(value)
            except ValidationError:
                return _failed("list", SafeErrorV1(code="invalid_input", message_key="ticket_input_invalid", retryable=False))
        outcome = await self._execute(
            capability,
            lambda scoped, attempt: self.gateway.list_once(scoped, query, upstream_cursor, attempt),
        )
        if isinstance(outcome, GatewayFailure):
            return _failed("list", outcome.safe_error)
        try:
            raw_items = outcome.data["items"]
            raw_page = outcome.data["page"]
            if not isinstance(raw_items, list) or not isinstance(raw_page, dict):
                raise ValueError
            items = [self._summary(item) for item in raw_items]
            has_more = raw_page["has_more"]
            if not isinstance(has_more, bool):
                raise ValueError
            upstream_next = raw_page.get("next_cursor")
            if has_more != (isinstance(upstream_next, str) and bool(upstream_next)):
                raise ValueError
            continuation_ref = self.cursors.issue(capability, query, upstream_next) if has_more else None
            page = SafePageV1(limit=query.limit, has_more=has_more, continuation_ref=continuation_ref)
            return TicketReadToolResultV1(
                state="succeeded" if items else "empty", mode="list", items=items, page=page
            )
        except (KeyError, TypeError, ValueError, ValidationError):
            return _failed("list", SafeErrorV1(code="upstream_unavailable", message_key="ticket_contract_invalid", retryable=False))

    async def read_for_update(
        self, capability: TicketRequestCapability, ticket_id: str
    ) -> ReadForUpdateResultV1 | SafeErrorV1:
        """U3가 review/PATCH 전에 호출하는 strict server-only read port."""
        if not ticket_id.strip():
            return SafeErrorV1(code="invalid_input", message_key="ticket_input_invalid", retryable=False)
        outcome = await self._execute(
            capability, lambda scoped, attempt: self.gateway.detail_once(scoped, ticket_id, attempt)
        )
        if isinstance(outcome, GatewayFailure):
            return outcome.safe_error
        try:
            detail = self._detail(outcome.data)
            return ReadForUpdateResultV1(
                detail=detail,
                if_match_version=outcome.etag,
                server_current_values=self._update_values(outcome.data),
            )
        except (KeyError, TypeError, ValueError, ValidationError):
            return SafeErrorV1(
                code="upstream_unavailable",
                message_key="ticket_update_evidence_invalid",
                retryable=False,
            )

    async def _read_detail(self, capability: TicketRequestCapability, ticket_id: str) -> TicketReadToolResultV1:
        outcome = await self._execute(
            capability, lambda scoped, attempt: self.gateway.detail_once(scoped, ticket_id, attempt)
        )
        if isinstance(outcome, GatewayFailure):
            return _failed("detail", outcome.safe_error)
        try:
            detail = self._detail(outcome.data)
        except (KeyError, TypeError, ValueError, ValidationError):
            return _failed("detail", SafeErrorV1(code="upstream_unavailable", message_key="ticket_contract_invalid", retryable=False))
        if self.evidence_sink and outcome.etag == f'"{detail.version}"':
            self.evidence_sink(detail.id, detail.version, outcome.etag)
        return TicketReadToolResultV1(state="succeeded", mode="detail", detail=detail)

    async def _execute(self, capability: TicketRequestCapability, call) -> GatewaySuccess | GatewayFailure:
        scoped = capability.operation_scope()
        deadline = self.monotonic() + (2 * self.timeout_seconds) + 0.25
        try:
            credential_expires_at = await scoped.credential_expires_at(
                deadline=deadline, monotonic=self.monotonic
            )
            ready = not (
                credential_expires_at is not None
                and credential_expires_at <= self.utc_now() + timedelta(seconds=60)
            ) or await scoped.refresh_once(timeout=deadline - self.monotonic())
        except Exception:
            ready = False
        if not ready:
            return GatewayFailure(
                None,
                None,
                SafeErrorV1(
                    code="authentication_required", message_key="sign_in_required", retryable=False
                ),
                http_attempted=False,
            )
        http_attempts = 0
        while http_attempts < 2:
            if self.monotonic() >= deadline:
                return self._deadline_failure()
            attempt = GatewayAttemptDeadline(deadline, self.timeout_seconds, self.monotonic)
            outcome = await call(scoped, attempt)
            if outcome.http_attempted:
                http_attempts += 1
            if isinstance(outcome, GatewaySuccess):
                return outcome
            if not outcome.http_attempted:
                return outcome
            if (
                http_attempts < 2
                and outcome.status == 401
                and outcome.upstream_code == "token_expired"
            ):
                remaining = deadline - self.monotonic()
                if remaining <= 0 or not await scoped.refresh_once(timeout=remaining):
                    return GatewayFailure(
                        None,
                        None,
                        SafeErrorV1(
                            code="authentication_required", message_key="sign_in_required", retryable=False
                        ),
                        http_attempted=False,
                    )
                if self.monotonic() >= deadline:
                    return self._deadline_failure()
                continue
            if http_attempts < 2 and outcome.transient:
                if deadline - self.monotonic() < 0.25:
                    return self._deadline_failure()
                await self.sleeper(0.25)
                if self.monotonic() >= deadline:
                    return self._deadline_failure()
                continue
            return outcome
        return outcome

    @staticmethod
    def _deadline_failure() -> GatewayFailure:
        return GatewayFailure(
            None,
            None,
            SafeErrorV1(
                code="upstream_unavailable", message_key="ticket_service_unavailable", retryable=False
            ),
            http_attempted=False,
        )

    @staticmethod
    def _summary(raw: Any) -> SafeTicketSummaryV1:
        if not isinstance(raw, dict):
            raise ValueError
        return SafeTicketSummaryV1(
            id=raw["id"],
            title=raw["title"],
            status=raw["status"],
            priority=raw["priority"],
            service=_name(raw["service"]),
            created_at=raw["created_at"],
            updated_at=raw["updated_at"],
        )

    @staticmethod
    def _detail(raw: dict[str, Any]) -> SafeTicketDetailV1:
        return SafeTicketDetailV1(
            id=raw["id"],
            title=raw["title"],
            description=raw["description"],
            service=_name(raw["service"]),
            impact_level=raw["impact_level"],
            urgency=raw["urgency"],
            impact_note=raw["impact_note"],
            status=raw["status"],
            priority=raw["priority"],
            version=raw["version"],
            component=_name(raw.get("component")),
            model=raw.get("model"),
            classification=raw.get("classification"),
            is_major=raw.get("is_major"),
            requester=_name(raw.get("requester")),
            assignment_group=_name(raw.get("assignment_group")),
            assignee=_name(raw.get("assignee")),
            occurred_at=raw.get("occurred_at"),
            created_at=raw.get("created_at"),
            updated_at=raw.get("updated_at"),
        )

    @staticmethod
    def _update_values(raw: dict[str, Any]) -> dict[str, str | int | bool | None]:
        component = raw.get("component")
        component_id: int | None = None
        if isinstance(component, dict):
            candidate = component.get("id")
            if isinstance(candidate, int) and not isinstance(candidate, bool):
                component_id = candidate
        return {
            "title": raw["title"],
            "description": raw["description"],
            "component_id": component_id,
            "model": raw.get("model"),
            "impact_level": raw["impact_level"],
            "urgency": raw["urgency"],
            "impact_note": raw["impact_note"],
            "is_major": raw.get("is_major"),
        }
