"""U3 durable prepare/revise/confirm/cancel and optimistic-lock policy."""

from __future__ import annotations

import secrets
import time
from datetime import datetime, timedelta, timezone
from typing import Callable

from app.ticketing.capability import TicketRequestCapability, UpdateCredentialRefreshPortV1
from app.ticketing.gateway import (
    GatewayAttemptDeadline,
    GatewayFailure,
    GatewayUpdateSuccess,
    ItsmGateway,
)
from app.ticketing.lifecycle import CurrentTurnEvidenceV1
from app.ticketing.models import (
    PendingMutationBindingV1,
    PendingUpdateMutationV1,
    ReadForUpdateResultV1,
    SafeErrorV1,
)
from app.ticketing.operations import TicketOperations
from app.ticketing.repositories import PendingMutationRepository
from app.ticketing.update_models import (
    PATCH_LABELS,
    PendingUpdateExecutionV1,
    SafeUpdateResultV1,
    SafeUpdateReviewFieldV1,
    SafeUpdateReviewV1,
    TicketPatchInV1,
    TicketUpdateToolInputV1,
    TicketUpdateToolResultV1,
    UpdateAttemptPolicyV1,
)
from app.ticketing.update_repository import PendingUpdateExecutionRepository


UtcClock = Callable[[], datetime]
MonotonicClock = Callable[[], float]


def _error(code: str, key: str, *, retryable: bool = False) -> SafeErrorV1:
    return SafeErrorV1(code=code, message_key=key, retryable=retryable)


class TicketUpdateOperations:
    def __init__(
        self,
        read_operations: TicketOperations,
        gateway: ItsmGateway,
        pending_repository: PendingMutationRepository,
        execution_repository: PendingUpdateExecutionRepository,
        *,
        credential_refresh_port: UpdateCredentialRefreshPortV1 | None = None,
        timeout_seconds: float = 10.0,
        utc_now: UtcClock = lambda: datetime.now(timezone.utc),
        monotonic: MonotonicClock = time.monotonic,
    ) -> None:
        self.read_operations = read_operations
        self.gateway = gateway
        self.pending = pending_repository
        self.executions = execution_repository
        self.credential_refresh_port = credential_refresh_port
        self.timeout_seconds = timeout_seconds
        self.utc_now = utc_now
        self.monotonic = monotonic

    async def ticket_update(
        self,
        capability: TicketRequestCapability,
        value: TicketUpdateToolInputV1,
        evidence: CurrentTurnEvidenceV1,
    ) -> TicketUpdateToolResultV1:
        if value.action in ("prepare", "revise"):
            assert value.patch is not None
            return await self._prepare_or_revise(capability, value)
        if value.action == "cancel":
            return self._cancel(capability, value, evidence)
        return await self._confirm(capability, value, evidence)

    async def _prepare_or_revise(
        self,
        capability: TicketRequestCapability,
        value: TicketUpdateToolInputV1,
    ) -> TicketUpdateToolResultV1:
        self._recover_staged_access(capability, value.ticket_id)
        if value.action == "prepare":
            recovered = await self._recover_review(capability, value.ticket_id)
            if recovered is not None:
                return recovered
        else:
            old = self._pending_update(value.pending_ref)
            if old is None or not self._actor_ticket_matches(capability, old, value.ticket_id):
                return self._failure("invalidated", "ticket_update_review_invalidated")
        snapshot = await self.read_operations.read_for_update(capability, value.ticket_id)
        if isinstance(snapshot, SafeErrorV1):
            return TicketUpdateToolResultV1(state="failed", error=snapshot)
        assert value.patch is not None
        try:
            patch = value.patch.actual_diff(snapshot.server_current_values)
        except ValueError:
            return self._failure("failed", "ticket_update_no_changes", code="invalid_input")
        return self._publish(
            capability,
            snapshot,
            patch,
            expected_old=value.pending_ref if value.action == "revise" else None,
            revised=value.action == "revise",
        )

    def _recover_staged_access(
        self, capability: TicketRequestCapability, ticket_id: str
    ) -> None:
        """Close the U1-commit/U3-activation crash window on normal access."""
        pending = self.pending.current_update_pending(capability, ticket_id)
        staged = self.executions.find_staged_for_actor_ticket(
            user_id=capability.user_id,
            session_id=capability.session_id,
            conversation_id=capability.conversation_id,
            ticket_id=ticket_id,
        )
        for execution in staged:
            if (
                pending is not None
                and execution.mutation_id == pending.mutation_id
                and self._bindings_match(execution, pending)
            ):
                self.executions.recover_staged_from_pending(execution, pending)
            else:
                self.executions.retire(
                    execution.mutation_id, expected_status="staged", now=self.utc_now()
                )

    def _publish(
        self,
        capability: TicketRequestCapability,
        snapshot: ReadForUpdateResultV1,
        patch: TicketPatchInV1,
        *,
        expected_old: str | None,
        revised: bool,
    ) -> TicketUpdateToolResultV1:
        canonical = patch.canonical_patch()
        now = self.utc_now()
        mutation_id = secrets.token_urlsafe(24)
        execution = PendingUpdateExecutionV1(
            mutation_id=mutation_id,
            user_id=capability.user_id,
            originating_session_id=capability.session_id,
            originating_conversation_id=capability.conversation_id,
            ticket_id=snapshot.detail.id,
            base_version=snapshot.detail.version,
            canonical_patch_bytes=canonical.body_bytes,
            patch_bytes_hash=canonical.sha256,
            if_match_version=snapshot.if_match_version,
            created_at=now,
            expires_at=now + timedelta(minutes=15),
        )
        self.executions.stage(execution)
        pending = PendingUpdateMutationV1(
            mutation_id=mutation_id,
            user_id=capability.user_id,
            session_id=capability.session_id,
            conversation_id=capability.conversation_id,
            ticket_id=snapshot.detail.id,
            normalized_payload_hash=canonical.sha256,
            base_version=snapshot.detail.version,
            expires_at=execution.expires_at,
        )
        replacement = self.pending.replace_update_pending(capability, expected_old, pending)
        if replacement is None:
            self.executions.retire(mutation_id, expected_status="staged", now=now)
            return self._failure("conflict", "ticket_update_review_conflict")
        if not self.executions.activate_replacement(replacement):
            return self._failure("failed", "ticket_update_activation_pending", retryable=True)
        review = self._review(execution, snapshot.server_current_values, patch)
        return TicketUpdateToolResultV1(
            state="revised" if revised else "review_required",
            pending_ref=mutation_id,
            review=review,
        )

    async def _recover_review(
        self,
        capability: TicketRequestCapability,
        ticket_id: str,
    ) -> TicketUpdateToolResultV1 | None:
        execution = self.executions.find_active_for_actor_ticket(
            user_id=capability.user_id,
            session_id=capability.session_id,
            conversation_id=capability.conversation_id,
            ticket_id=ticket_id,
            now=self.utc_now(),
        )
        if execution is None:
            return None
        pending = self._pending_update(execution.mutation_id)
        if pending is None or not self._bindings_match(execution, pending):
            self.executions.retire(execution.mutation_id, expected_status="active", now=self.utc_now())
            return None
        snapshot = await self.read_operations.read_for_update(capability, ticket_id)
        if isinstance(snapshot, SafeErrorV1):
            return self._failure("ambiguous", "ticket_update_recovery_unavailable", code="ambiguous_result")
        if snapshot.detail.version != execution.base_version:
            return TicketUpdateToolResultV1(
                state="conflict",
                pending_ref=execution.mutation_id,
                error=_error("conflict", "ticket_update_version_changed"),
            )
        try:
            patch = execution.patch()
        except (ValueError, TypeError):
            self.executions.retire(execution.mutation_id, expected_status="active", now=self.utc_now())
            return self._failure("invalidated", "ticket_update_review_invalidated")
        return TicketUpdateToolResultV1(
            state="review_required",
            pending_ref=execution.mutation_id,
            review=self._review(execution, snapshot.server_current_values, patch),
        )

    async def _confirm(
        self,
        capability: TicketRequestCapability,
        value: TicketUpdateToolInputV1,
        evidence: CurrentTurnEvidenceV1,
    ) -> TicketUpdateToolResultV1:
        pending = self._pending_update(value.pending_ref)
        execution = self.executions.get(value.pending_ref or "")
        if pending is None or execution is None:
            return self._failure("invalidated", "ticket_update_review_invalidated")
        if execution.status == "staged" and self._bindings_match(execution, pending):
            if self.executions.recover_staged_from_pending(execution, pending):
                execution = self.executions.get(execution.mutation_id)
        if (
            execution is None
            or execution.status != "active"
            or not self._actor_ticket_matches(capability, pending, value.ticket_id)
            or not self._bindings_match(execution, pending)
        ):
            return self._failure("invalidated", "ticket_update_review_invalidated")
        try:
            patch = execution.patch()
        except (ValueError, TypeError):
            self.executions.retire(execution.mutation_id, expected_status="active", now=self.utc_now())
            return self._failure("invalidated", "ticket_update_integrity_invalid")
        if evidence.decision != "explicit_confirm":
            snapshot = await self.read_operations.read_for_update(capability, value.ticket_id)
            if isinstance(snapshot, SafeErrorV1) or snapshot.detail.version != execution.base_version:
                return self._failure("conflict", "ticket_update_version_changed")
            return TicketUpdateToolResultV1(
                state="needs_explicit_confirmation",
                pending_ref=execution.mutation_id,
                review=self._review(execution, snapshot.server_current_values, patch),
            )
        binding = self._binding(pending)
        consumed = self.pending.confirm_and_consume_update(binding, evidence)
        if consumed is None:
            return self._failure("invalidated", "ticket_update_review_invalidated")
        policy = UpdateAttemptPolicyV1()
        deadline = self.monotonic() + (2 * self.timeout_seconds)
        outcome = await self._patch_once(capability, execution, policy, deadline)
        if isinstance(outcome, GatewayUpdateSuccess):
            success = self._validated_success(execution, patch, outcome)
            if success is not None:
                self.executions.retire(execution.mutation_id, expected_status="active", now=self.utc_now())
                return success
            return await self._reconcile(capability, execution, patch)
        if self._can_refresh(outcome, policy):
            reservation = self.executions.reserve_refresh_once(
                execution.mutation_id,
                base_credential_generation=capability.credential_generation,
                now=self.utc_now(),
            )
            if reservation is not None and self.credential_refresh_port is not None:
                policy.record_refresh()
                refreshed = await self.credential_refresh_port.refresh_update_capability_once(
                    capability,
                    reservation,
                    deadline - self.monotonic(),
                )
                if refreshed.ok and refreshed.value is not None:
                    self.executions.mark_refresh_committed(
                        execution.mutation_id,
                        committed_generation=refreshed.value.credential_generation,
                        now=self.utc_now(),
                    )
                    second = await self._patch_once(refreshed.value, execution, policy, deadline)
                    if isinstance(second, GatewayUpdateSuccess):
                        success = self._validated_success(execution, patch, second)
                        if success is not None:
                            self.executions.retire(execution.mutation_id, expected_status="active", now=self.utc_now())
                            return success
                    return await self._reconcile(capability, execution, patch)
        if outcome.status == 429:
            return TicketUpdateToolResultV1(state="failed", error=outcome.safe_error)
        return await self._reconcile(capability, execution, patch)

    async def _patch_once(
        self,
        capability: TicketRequestCapability,
        execution: PendingUpdateExecutionV1,
        policy: UpdateAttemptPolicyV1,
        deadline: float,
    ) -> GatewayUpdateSuccess | GatewayFailure:
        policy.record_patch()
        return await self.gateway.update_ticket_once(
            capability,
            execution.ticket_id,
            execution.canonical_patch_bytes,
            execution.if_match_version,
            GatewayAttemptDeadline(deadline, self.timeout_seconds, self.monotonic),
        )

    async def _reconcile(
        self,
        capability: TicketRequestCapability,
        execution: PendingUpdateExecutionV1,
        patch: TicketPatchInV1,
    ) -> TicketUpdateToolResultV1:
        snapshot = await self.read_operations.read_for_update(capability, execution.ticket_id)
        if isinstance(snapshot, SafeErrorV1):
            return self._failure("ambiguous", "ticket_update_result_ambiguous", code="ambiguous_result")
        if snapshot.detail.version > execution.base_version and self._all_applied(
            patch, snapshot.server_current_values
        ):
            self.executions.retire(execution.mutation_id, expected_status="active", now=self.utc_now())
            return TicketUpdateToolResultV1(
                state="succeeded",
                data=SafeUpdateResultV1(
                    ticket_id=execution.ticket_id,
                    applied_changes=self._result_fields(patch),
                    new_version=snapshot.detail.version,
                ),
            )
        self.executions.retire(execution.mutation_id, expected_status="active", now=self.utc_now())
        try:
            remaining = patch.actual_diff(snapshot.server_current_values)
        except ValueError:
            return self._failure("conflict", "ticket_update_version_changed")
        return self._publish(capability, snapshot, remaining, expected_old=None, revised=False)

    def _cancel(
        self,
        capability: TicketRequestCapability,
        value: TicketUpdateToolInputV1,
        evidence: CurrentTurnEvidenceV1,
    ) -> TicketUpdateToolResultV1:
        pending = self._pending_update(value.pending_ref)
        execution = self.executions.get(value.pending_ref or "")
        if (
            pending is None
            or execution is None
            or not self._actor_ticket_matches(capability, pending, value.ticket_id)
            or not self._bindings_match(execution, pending)
            or not self.pending.cancel_update(self._binding(pending), evidence)
        ):
            return self._failure("invalidated", "ticket_update_review_invalidated")
        self.executions.retire(execution.mutation_id, now=self.utc_now())
        return TicketUpdateToolResultV1(state="cancelled")

    def _validated_success(
        self,
        execution: PendingUpdateExecutionV1,
        patch: TicketPatchInV1,
        outcome: GatewayUpdateSuccess,
    ) -> TicketUpdateToolResultV1 | None:
        if outcome.new_version <= execution.base_version:
            return None
        values = self.read_operations._update_values(outcome.ticket)
        if not self._all_applied(patch, values) or outcome.ticket.get("id") != execution.ticket_id:
            return None
        return TicketUpdateToolResultV1(
            state="succeeded",
            data=SafeUpdateResultV1(
                ticket_id=execution.ticket_id,
                applied_changes=self._result_fields(patch),
                new_version=outcome.new_version,
                request_id=outcome.request_id,
            ),
        )

    @staticmethod
    def _can_refresh(outcome: GatewayFailure, policy: UpdateAttemptPolicyV1) -> bool:
        return all(
            (
                policy.patch_attempt_count == 1,
                policy.refresh_invocation_count == 0,
                outcome.status == 401,
                outcome.upstream_code == "token_expired",
                outcome.authoritative_no_apply,
            )
        )

    @staticmethod
    def _all_applied(patch: TicketPatchInV1, values: dict[str, object]) -> bool:
        return all(values.get(name) == value for name, value in patch.selected_payload().items())

    @staticmethod
    def _result_fields(patch: TicketPatchInV1) -> list[SafeUpdateReviewFieldV1]:
        return [
            SafeUpdateReviewFieldV1(
                key=name,
                label=PATCH_LABELS[name],
                before=None,
                after=value,
            )
            for name, value in patch.selected_payload().items()
        ]

    @staticmethod
    def _review(
        execution: PendingUpdateExecutionV1,
        current_values: dict[str, object],
        patch: TicketPatchInV1,
    ) -> SafeUpdateReviewV1:
        return SafeUpdateReviewV1(
            ticket_id=execution.ticket_id,
            base_version=execution.base_version,
            normalized_payload_hash=execution.patch_bytes_hash,
            fields=[
                SafeUpdateReviewFieldV1(
                    key=name,
                    label=PATCH_LABELS[name],
                    before=current_values.get(name),
                    after=value,
                )
                for name, value in patch.selected_payload().items()
            ],
        )

    def _pending_update(self, mutation_id: str | None) -> PendingUpdateMutationV1 | None:
        if not mutation_id:
            return None
        pending = self.pending.get(mutation_id)
        if pending is None or pending.operation != "update":
            return None
        try:
            return PendingUpdateMutationV1.model_validate(
                pending.model_dump(mode="python")
            )
        except ValueError:
            return None

    @staticmethod
    def _binding(pending: PendingUpdateMutationV1) -> PendingMutationBindingV1:
        return PendingMutationBindingV1(
            mutation_id=pending.mutation_id,
            operation="update",
            user_id=pending.user_id,
            session_id=pending.session_id,
            conversation_id=pending.conversation_id,
            normalized_payload_hash=pending.normalized_payload_hash,
            ticket_id=pending.ticket_id,
            base_version=pending.base_version,
        )

    @staticmethod
    def _actor_ticket_matches(
        capability: TicketRequestCapability,
        pending: PendingUpdateMutationV1,
        ticket_id: str,
    ) -> bool:
        return (
            pending.user_id,
            pending.session_id,
            pending.conversation_id,
            pending.ticket_id,
        ) == (
            capability.user_id,
            capability.session_id,
            capability.conversation_id,
            ticket_id,
        )

    @staticmethod
    def _bindings_match(
        execution: PendingUpdateExecutionV1,
        pending: PendingUpdateMutationV1,
    ) -> bool:
        return all(
            (
                execution.mutation_id == pending.mutation_id,
                execution.user_id == pending.user_id,
                execution.originating_session_id == pending.session_id,
                execution.originating_conversation_id == pending.conversation_id,
                execution.ticket_id == pending.ticket_id,
                execution.patch_bytes_hash == pending.normalized_payload_hash,
                execution.base_version == pending.base_version,
                execution.verify_patch_hash(),
            )
        )

    @staticmethod
    def _failure(
        state: str,
        message_key: str,
        *,
        code: str | None = None,
        retryable: bool = False,
    ) -> TicketUpdateToolResultV1:
        default_codes = {
            "invalidated": "conflict",
            "conflict": "conflict",
            "ambiguous": "ambiguous_result",
            "failed": "upstream_unavailable",
        }
        return TicketUpdateToolResultV1(
            state=state,
            error=_error(code or default_codes[state], message_key, retryable=retryable),
        )
