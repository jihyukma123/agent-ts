"""Ledger-first ticket-create policy built over U1 public confirmation ports."""

from __future__ import annotations

import hashlib
import json
import secrets
import time
import unicodedata
from collections.abc import Callable
from datetime import datetime, timedelta, timezone

from app.ticketing.capability import (
    ActiveCredentialRefreshReservations,
    CredentialRefreshPortV1,
    CredentialRefreshReservationSigner,
    CredentialRefreshReservationVerifier,
    TicketRequestCapability,
)
from app.ticketing.create_models import (
    CanonicalRequestBytesV1,
    NormalizedTicketCreateV1,
    ResolvedCatalogV1,
    SafeMutationResultV1,
    SafeMutationReviewV1,
    SafeReviewFieldV1,
    TicketCreateAttemptV1,
    TicketMutationToolResultV1,
    TicketWriteDraftV1,
    TicketWriteToolInputV1,
)
from app.ticketing.create_repository import RETENTION, TicketCreateAttemptRepository
from app.ticketing.gateway import (
    GatewayAttemptDeadline,
    GatewayCatalogSuccess,
    GatewayCreateSuccess,
    GatewayFailure,
    ItsmGateway,
)
from app.ticketing.lifecycle import CurrentTurnEvidenceV1
from app.ticketing.models import PendingMutationBindingV1, PendingTicketMutationV1, SafeErrorV1
from app.ticketing.repositories import PendingMutationRepository


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _token() -> str:
    return secrets.token_urlsafe(24)


def _invalid(message_key: str = "ticket_input_invalid") -> TicketMutationToolResultV1:
    return TicketMutationToolResultV1(
        state="failed",
        error=SafeErrorV1(code="invalid_input", message_key=message_key, retryable=False),
    )


class TicketCreateOperations:
    def __init__(
        self,
        gateway: ItsmGateway,
        repository: TicketCreateAttemptRepository,
        pending_repository: PendingMutationRepository,
        *,
        timeout_seconds: float,
        utc_now: Callable[[], datetime] = _utc_now,
        monotonic: Callable[[], float] = time.monotonic,
        random_token: Callable[[], str] = _token,
        refresh_reservation_signer: CredentialRefreshReservationSigner | None = None,
        refresh_reservation_verifier: CredentialRefreshReservationVerifier | None = None,
        credential_refresh_port: CredentialRefreshPortV1 | None = None,
        active_refresh_reservations: ActiveCredentialRefreshReservations | None = None,
    ):
        self.gateway = gateway
        self.repository = repository
        self.pending_repository = pending_repository
        self.timeout_seconds = timeout_seconds
        self.utc_now = utc_now
        self.monotonic = monotonic
        self.random_token = random_token
        self.refresh_reservation_signer = refresh_reservation_signer
        self.refresh_reservation_verifier = refresh_reservation_verifier
        self.credential_refresh_port = credential_refresh_port
        self.active_refresh_reservations = (
            active_refresh_reservations or ActiveCredentialRefreshReservations()
        )

    async def ticket_write(
        self,
        capability: TicketRequestCapability,
        value: TicketWriteToolInputV1,
        turn_evidence: CurrentTurnEvidenceV1,
    ) -> TicketMutationToolResultV1:
        scoped = capability.operation_scope()
        if value.action == "prepare":
            assert value.draft is not None
            return await self.prepare(scoped, value.draft)
        if value.action == "revise":
            assert value.draft is not None and value.pending_ref is not None
            return await self.revise(scoped, value.pending_ref, value.draft)
        if value.action == "cancel":
            assert value.pending_ref is not None
            return self.cancel(scoped, value.pending_ref, turn_evidence)
        assert value.pending_ref is not None
        return await self.confirm(scoped, value.pending_ref, turn_evidence)

    async def prepare(
        self, capability: TicketRequestCapability, draft: TicketWriteDraftV1
    ) -> TicketMutationToolResultV1:
        candidate = await self._validate_draft(capability, draft)
        if isinstance(candidate, TicketMutationToolResultV1):
            return candidate
        normalized, catalog, canonical, captured_now = candidate
        old = self.repository.find_prepared_for_actor(
            user_id=capability.user_id,
            session_id=capability.session_id,
            conversation_id=capability.conversation_id,
        )
        if old is not None:
            if not self.repository.supersede_prepared(
                user_id=capability.user_id,
                session_id=capability.session_id,
                conversation_id=capability.conversation_id,
                expected_create_attempt_id=old.create_attempt_id,
            ):
                return self._conflict()
            self.pending_repository.invalidate(
                user_id=capability.user_id,
                session_id=capability.session_id,
                conversation_id=capability.conversation_id,
            )
        return self._publish_review(
            capability,
            normalized,
            catalog,
            canonical,
            captured_now,
            revised=old is not None,
        )

    async def revise(
        self,
        capability: TicketRequestCapability,
        pending_ref: str,
        draft: TicketWriteDraftV1,
    ) -> TicketMutationToolResultV1:
        pending = self.pending_repository.get(pending_ref)
        if pending is None or not self._pending_matches(capability, pending):
            return self._invalidated()
        candidate = await self._validate_draft(capability, draft)
        if isinstance(candidate, TicketMutationToolResultV1):
            return candidate
        normalized, catalog, canonical, captured_now = candidate
        old = self.repository.find_prepared_for_binding(
            user_id=capability.user_id,
            session_id=capability.session_id,
            conversation_id=capability.conversation_id,
            normalized_payload_hash=pending.normalized_payload_hash,
        )
        if old is None or not self.repository.supersede_prepared(
            user_id=capability.user_id,
            session_id=capability.session_id,
            conversation_id=capability.conversation_id,
            expected_create_attempt_id=old.create_attempt_id,
        ):
            return self._conflict()
        self.pending_repository.invalidate(
            user_id=capability.user_id,
            session_id=capability.session_id,
            conversation_id=capability.conversation_id,
        )
        return self._publish_review(
            capability,
            normalized,
            catalog,
            canonical,
            captured_now,
            revised=True,
        )

    async def _validate_draft(
        self,
        capability: TicketRequestCapability,
        draft: TicketWriteDraftV1,
    ) -> tuple[
        NormalizedTicketCreateV1,
        ResolvedCatalogV1,
        CanonicalRequestBytesV1,
        datetime,
    ] | TicketMutationToolResultV1:
        """Resolve and normalize without changing durable review authority."""
        catalog = await self._resolve_catalog(capability, draft)
        if isinstance(catalog, TicketMutationToolResultV1):
            return catalog
        captured_now = self.utc_now()
        try:
            normalized = NormalizedTicketCreateV1.from_draft(draft, catalog, now=captured_now)
        except ValueError:
            return _invalid()
        canonical = normalized.canonical_request()
        return normalized, catalog, canonical, captured_now

    def _publish_review(
        self,
        capability: TicketRequestCapability,
        normalized: NormalizedTicketCreateV1,
        catalog: ResolvedCatalogV1,
        canonical: CanonicalRequestBytesV1,
        captured_now: datetime,
        *,
        revised: bool,
    ) -> TicketMutationToolResultV1:
        create_attempt_id = self.random_token()
        mutation_id = self.random_token()
        row = TicketCreateAttemptV1(
            create_attempt_id=create_attempt_id,
            user_id=capability.user_id,
            originating_session_id=capability.session_id,
            originating_conversation_id=capability.conversation_id,
            normalized_payload_hash=canonical.sha256,
            canonical_request_bytes=canonical.body_bytes,
            serialization_version=canonical.serialization_version,
            request_bytes_hash=canonical.sha256,
            idempotency_key=self.random_token(),
            created_at=captured_now,
            retained_until=captured_now + RETENTION,
        )
        self.repository.create_prepared(row)
        pending = PendingTicketMutationV1(
            mutation_id=mutation_id,
            operation="create",
            user_id=capability.user_id,
            session_id=capability.session_id,
            conversation_id=capability.conversation_id,
            normalized_payload_hash=canonical.sha256,
            expires_at=captured_now + timedelta(minutes=15),
        )
        try:
            self.pending_repository.save_review(pending)
        except Exception:
            return TicketMutationToolResultV1(
                state="failed",
                error=SafeErrorV1(
                    code="upstream_unavailable", message_key="ticket_review_unavailable", retryable=True
                ),
            )
        return TicketMutationToolResultV1(
            state="revised" if revised else "review_required",
            pending_ref=mutation_id,
            review=self._review(normalized, catalog, canonical.sha256),
        )

    async def confirm(
        self,
        capability: TicketRequestCapability,
        pending_ref: str,
        turn_evidence: CurrentTurnEvidenceV1,
    ) -> TicketMutationToolResultV1:
        if turn_evidence.decision != "explicit_confirm":
            return self._needs_confirmation(capability, pending_ref)
        pending = self.pending_repository.get(pending_ref)
        if pending is None or not self._pending_scope_matches(capability, pending):
            return self._invalidated()
        if pending.status == "consumed":
            return await self._recover_consumed_pending(capability, pending)
        if pending.status != "pending":
            return self._invalidated()
        attempt = self.repository.find_prepared_for_binding(
            user_id=capability.user_id,
            session_id=capability.session_id,
            conversation_id=capability.conversation_id,
            normalized_payload_hash=pending.normalized_payload_hash,
        )
        if attempt is None:
            return self._invalidated()
        binding = PendingMutationBindingV1(
            mutation_id=pending.mutation_id,
            operation="create",
            user_id=capability.user_id,
            session_id=capability.session_id,
            conversation_id=capability.conversation_id,
            normalized_payload_hash=pending.normalized_payload_hash,
        )
        if self.pending_repository.confirm(binding, turn_evidence) is None:
            return self._invalidated()
        evidence = self.pending_repository.consume_once(binding)
        if evidence is None:
            return self._invalidated()
        try:
            bound = self.repository.bind_confirmation_evidence(
                attempt.create_attempt_id,
                evidence,
                user_id=capability.user_id,
                session_id=capability.session_id,
                conversation_id=capability.conversation_id,
                normalized_payload_hash=pending.normalized_payload_hash,
            )
        except Exception:
            return self._republish_existing_review(capability, attempt)
        if bound is None:
            return self._invalidated()
        return await self._send(capability, bound.create_attempt_id)

    async def _recover_consumed_pending(
        self, capability: TicketRequestCapability, pending: PendingTicketMutationV1
    ) -> TicketMutationToolResultV1:
        attempt = self.repository.find_for_consumed_pending(
            user_id=capability.user_id,
            session_id=capability.session_id,
            conversation_id=capability.conversation_id,
            normalized_payload_hash=pending.normalized_payload_hash,
            mutation_id=pending.mutation_id,
        )
        if attempt is None:
            return self._invalidated()
        if attempt.status == "prepared":
            return self._republish_existing_review(capability, attempt)
        if attempt.refresh_state == "reserved" and attempt.refresh_reservation_id:
            if self.active_refresh_reservations.contains(attempt.refresh_reservation_id):
                return self._result_from_attempt(attempt.create_attempt_id)
            base_generation = attempt.refresh_base_credential_generation
            restored = None
            if (
                base_generation is not None
                and attempt.refresh_used_at is not None
                and self.refresh_reservation_verifier is not None
            ):
                restored = self.refresh_reservation_verifier.restore(
                    capability,
                    create_attempt_id=attempt.create_attempt_id,
                    refresh_reservation_id=attempt.refresh_reservation_id,
                    refresh_used_at=attempt.refresh_used_at,
                    base_credential_generation=base_generation,
                )
            if restored is None:
                # A valid reservation created by this process boot is never a
                # recovery candidate, even if the process-local registry entry
                # is absent because the owner was interrupted before register.
                return self._result_from_attempt(attempt.create_attempt_id)
            if capability.credential_generation > base_generation:
                self.repository.record_refresh_committed(
                    attempt.create_attempt_id,
                    attempt.refresh_reservation_id,
                    committed_credential_generation=capability.credential_generation,
                    committed_at=self.utc_now(),
                )
            else:
                self.repository.record_refresh_failed(
                    attempt.create_attempt_id,
                    attempt.refresh_reservation_id,
                    outcome="credential_refresh_commit_not_observed",
                )
                return self._failed_auth()
        return await self._send(capability, attempt.create_attempt_id)

    def _republish_existing_review(
        self, capability: TicketRequestCapability, attempt: TicketCreateAttemptV1
    ) -> TicketMutationToolResultV1:
        """A consume-before-bind crash never grants transport authority.

        The existing U2 attempt remains the source of bytes/key. A fresh U1
        review is published so a later raw turn must explicitly confirm again.
        """
        mutation_id = self.random_token()
        now = self.utc_now()
        self.pending_repository.save_review(
            PendingTicketMutationV1(
                mutation_id=mutation_id,
                operation="create",
                user_id=capability.user_id,
                session_id=capability.session_id,
                conversation_id=capability.conversation_id,
                normalized_payload_hash=attempt.normalized_payload_hash,
                expires_at=now + timedelta(minutes=15),
            )
        )
        try:
            payload = json.loads(attempt.canonical_request_bytes)
        except (TypeError, ValueError):
            return self._failed_unavailable()
        fields = [
            SafeReviewFieldV1(key=key, label_key=key, before=None, after=value)
            for key, value in payload.items()
            if isinstance(value, (str, int, bool)) or value is None
        ]
        return TicketMutationToolResultV1(
            state="needs_explicit_confirmation",
            pending_ref=mutation_id,
            review=SafeMutationReviewV1(
                normalized_payload_hash=attempt.normalized_payload_hash,
                fields=fields,
            ),
        )

    def cancel(
        self,
        capability: TicketRequestCapability,
        pending_ref: str,
        turn_evidence: CurrentTurnEvidenceV1,
    ) -> TicketMutationToolResultV1:
        if turn_evidence.decision != "explicit_cancel":
            return _invalid("ticket_cancel_requires_explicit_command")
        pending = self.pending_repository.get(pending_ref)
        if pending is None or not self._pending_matches(capability, pending):
            return self._invalidated()
        attempt = self.repository.find_prepared_for_binding(
            user_id=capability.user_id,
            session_id=capability.session_id,
            conversation_id=capability.conversation_id,
            normalized_payload_hash=pending.normalized_payload_hash,
        )
        self.pending_repository.invalidate(
            user_id=capability.user_id,
            session_id=capability.session_id,
            conversation_id=capability.conversation_id,
        )
        if attempt is not None:
            self.repository.terminate_unconfirmed(attempt.create_attempt_id, reason="cancelled")
        return TicketMutationToolResultV1(state="cancelled")

    async def _send(
        self, capability: TicketRequestCapability, create_attempt_id: str
    ) -> TicketMutationToolResultV1:
        while True:
            now = self.utc_now()
            claim = self.repository.claim_for_send(
                create_attempt_id,
                claim_id=self.random_token(),
                claim_deadline_at=now + timedelta(seconds=self.timeout_seconds + 1),
            )
            if claim is None:
                return self._result_from_attempt(create_attempt_id)
            attempt = claim.attempt
            if hashlib.sha256(attempt.canonical_request_bytes).hexdigest() != attempt.request_bytes_hash:
                self.repository.record_send_outcome(
                    create_attempt_id,
                    claim.claim_id,
                    status="terminal_failure",
                    last_outcome="request_binding_invalid",
                )
                return self._failed_unavailable()
            result = await self.gateway.create_ticket_once(
                capability,
                attempt.canonical_request_bytes,
                attempt.idempotency_key,
                GatewayAttemptDeadline(
                    self.monotonic() + self.timeout_seconds,
                    self.timeout_seconds,
                    self.monotonic,
                ),
            )
            if isinstance(result, GatewayCreateSuccess):
                if not self.repository.record_send_outcome(
                    create_attempt_id,
                    claim.claim_id,
                    status="completed",
                    last_outcome="idempotent_replayed" if result.idempotent_replayed else "created",
                    result_ticket_id=result.ticket_id,
                ):
                    return self._result_from_attempt(create_attempt_id)
                return TicketMutationToolResultV1(
                    state="succeeded",
                    data=SafeMutationResultV1(
                        ticket_id=result.ticket_id,
                        idempotent_replayed=result.idempotent_replayed,
                        request_id=result.request_id,
                    ),
                )
            assert isinstance(result, GatewayFailure)
            if result.status == 401 and result.upstream_code == "token_expired" and result.safe_error.retryable:
                current = self.repository.get(create_attempt_id)
                if current is None or current.refresh_state != "unused":
                    self.repository.record_send_outcome(
                        create_attempt_id,
                        claim.claim_id,
                        status="terminal_failure",
                        last_outcome="refresh_already_consumed",
                    )
                    return self._failed_auth()
                if self.refresh_reservation_signer is None or self.credential_refresh_port is None:
                    self.repository.record_send_outcome(
                        create_attempt_id,
                        claim.claim_id,
                        status="terminal_failure",
                        last_outcome="refresh_contract_unavailable",
                    )
                    return self._failed_auth()
                if not self.repository.record_send_outcome(
                    create_attempt_id,
                    claim.claim_id,
                    status="retryable_no_apply",
                    last_outcome="token_expired_no_apply",
                ):
                    return self._result_from_attempt(create_attempt_id)
                refresh_used_at = self.utc_now()
                operation_deadline = refresh_used_at + timedelta(seconds=self.timeout_seconds)
                reservation = self.refresh_reservation_signer.mint(
                    capability,
                    create_attempt_id=create_attempt_id,
                    refresh_used_at=refresh_used_at,
                    operation_deadline=operation_deadline,
                    nonce=self.random_token(),
                )
                reserved = self.repository.reserve_refresh(
                    create_attempt_id,
                    reservation_id=reservation.refresh_reservation_id,
                    used_at=refresh_used_at,
                    base_credential_generation=reservation.base_credential_generation,
                )
                if reserved is None:
                    return self._failed_auth()
                if not self.active_refresh_reservations.register(
                    reservation.refresh_reservation_id
                ):
                    return self._result_from_attempt(create_attempt_id)
                try:
                    refresh_result = (
                        await self.credential_refresh_port.refresh_delegated_capability_once(
                            capability,
                            reservation,
                            max(
                                0.0,
                                (operation_deadline - self.utc_now()).total_seconds(),
                            ),
                        )
                    )
                    if not refresh_result.ok or refresh_result.value is None:
                        return TicketMutationToolResultV1(
                            state="failed",
                            error=refresh_result.error or self._failed_auth().error,
                        )
                    refreshed = refresh_result.value
                    if not self.repository.record_refresh_committed(
                        create_attempt_id,
                        reservation.refresh_reservation_id,
                        committed_credential_generation=refreshed.credential_generation,
                        committed_at=self.utc_now(),
                    ):
                        return self._failed_auth()
                finally:
                    self.active_refresh_reservations.discard(
                        reservation.refresh_reservation_id
                    )
                continue
            if result.transient:
                self.repository.record_send_outcome(
                    create_attempt_id,
                    claim.claim_id,
                    status="ambiguous",
                    last_outcome="transport_ambiguous",
                )
                recovery_now = self.utc_now()
                if not self.repository.begin_reconciliation(
                    create_attempt_id,
                    expected_claim_id=claim.claim_id,
                    now=recovery_now,
                ):
                    return self._result_from_attempt(create_attempt_id)
                self.repository.record_reconciliation(
                    create_attempt_id, claim.claim_id, outcome="lookup_unavailable"
                )
                current = self.repository.get(create_attempt_id)
                if current is not None and current.status == "retryable_after_reconcile":
                    continue
                return self._result_from_attempt(create_attempt_id)
            terminal = "terminal_failure"
            self.repository.record_send_outcome(
                create_attempt_id,
                claim.claim_id,
                status=terminal,
                last_outcome=result.safe_error.code,
            )
            return TicketMutationToolResultV1(state="failed", error=result.safe_error)

    async def _resolve_catalog(
        self, capability: TicketRequestCapability, draft: TicketWriteDraftV1
    ) -> ResolvedCatalogV1 | TicketMutationToolResultV1:
        deadline = GatewayAttemptDeadline(
            self.monotonic() + self.timeout_seconds, self.timeout_seconds, self.monotonic
        )
        services = await self.gateway.catalog_services_once(capability, deadline)
        if isinstance(services, GatewayFailure):
            return TicketMutationToolResultV1(state="failed", error=services.safe_error)
        assert isinstance(services, GatewayCatalogSuccess)
        service = self._one_match(services.items, draft.service, active_status="active")
        if service is None:
            return _invalid("ticket_service_invalid")
        component = None
        if draft.component is not None:
            components = service.get("components")
            if not isinstance(components, list):
                return _invalid("ticket_component_invalid")
            component = self._one_match(components, draft.component)
            if component is None:
                return _invalid("ticket_component_invalid")
        model = None
        if draft.model is not None:
            models = await self.gateway.catalog_models_once(
                capability,
                GatewayAttemptDeadline(
                    self.monotonic() + self.timeout_seconds,
                    self.timeout_seconds,
                    self.monotonic,
                ),
            )
            if isinstance(models, GatewayFailure):
                return TicketMutationToolResultV1(state="failed", error=models.safe_error)
            model = self._one_match(models.items, draft.model, active_status="active")
            if model is None:
                return _invalid("ticket_model_invalid")
        return ResolvedCatalogV1(
            service_id=str(service["id"]),
            service_name=str(service.get("name", "")),
            component_id=int(component["id"]) if component is not None else None,
            component_name=str(component.get("name", "")) if component is not None else None,
            model=str(model["id"]) if model is not None else None,
            model_name=str(model.get("name", "")) if model is not None else None,
        )

    @staticmethod
    def _one_match(items, selector, *, active_status: str | None = None):
        def normalized(value: object) -> str:
            return unicodedata.normalize("NFKC", str(value)).strip().casefold()

        matches = []
        for item in items:
            if not isinstance(item, dict):
                continue
            if active_status is not None and item.get("status") != active_status:
                continue
            selected = item.get("id") if selector.id is not None else item.get("name")
            expected = selector.id if selector.id is not None else selector.name
            if selected is not None and normalized(selected) == normalized(expected):
                matches.append(item)
        return matches[0] if len(matches) == 1 else None

    @staticmethod
    def _pending_scope_matches(capability, pending) -> bool:
        return (
            pending.operation == "create"
            and pending.user_id == capability.user_id
            and pending.session_id == capability.session_id
            and pending.conversation_id == capability.conversation_id
        )

    @classmethod
    def _pending_matches(cls, capability, pending) -> bool:
        return pending.status == "pending" and cls._pending_scope_matches(capability, pending)

    @staticmethod
    def _review(normalized, catalog, payload_hash):
        labels = {
            "title": "제목",
            "description": "설명",
            "service_id": "서비스",
            "component_id": "컴포넌트",
            "model": "모델",
            "impact_level": "영향도",
            "urgency": "긴급도",
            "impact_note": "영향 설명",
            "occurred_at": "발생 시각",
            "classification": "분류",
        }
        payload = normalized.canonical_payload()
        display = dict(payload)
        display["service_id"] = catalog.service_name or payload["service_id"]
        if "component_id" in display:
            display["component_id"] = catalog.component_name or display["component_id"]
        if "model" in display:
            display["model"] = catalog.model_name or display["model"]
        return SafeMutationReviewV1(
            normalized_payload_hash=payload_hash,
            fields=[
                SafeReviewFieldV1(key=key, label_key=labels[key], before=None, after=value)
                for key, value in display.items()
            ],
        )

    def _needs_confirmation(self, capability, pending_ref):
        pending = self.pending_repository.get(pending_ref)
        if pending is None or not self._pending_matches(capability, pending):
            return self._invalidated()
        attempt = self.repository.find_prepared_for_binding(
            user_id=capability.user_id,
            session_id=capability.session_id,
            conversation_id=capability.conversation_id,
            normalized_payload_hash=pending.normalized_payload_hash,
        )
        if attempt is None:
            return self._invalidated()
        return TicketMutationToolResultV1(
            state="needs_explicit_confirmation",
            pending_ref=pending_ref,
            review=SafeMutationReviewV1(
                normalized_payload_hash=pending.normalized_payload_hash,
                fields=[],
            ),
        )

    def _result_from_attempt(self, create_attempt_id):
        attempt = self.repository.get(create_attempt_id)
        if attempt is not None and attempt.status == "completed" and attempt.result_ticket_id:
            return TicketMutationToolResultV1(
                state="succeeded",
                data=SafeMutationResultV1(ticket_id=attempt.result_ticket_id),
            )
        if attempt is not None and attempt.status == "ambiguous":
            return TicketMutationToolResultV1(
                state="ambiguous",
                error=SafeErrorV1(
                    code="ambiguous_result", message_key="ticket_create_ambiguous", retryable=False
                ),
            )
        return self._failed_unavailable()

    @staticmethod
    def _invalidated():
        return TicketMutationToolResultV1(
            state="invalidated",
            error=SafeErrorV1(
                code="conflict", message_key="ticket_review_invalidated", retryable=False
            ),
        )

    @staticmethod
    def _conflict():
        return TicketMutationToolResultV1(
            state="conflict",
            error=SafeErrorV1(code="conflict", message_key="ticket_create_conflict", retryable=False),
        )

    @staticmethod
    def _failed_unavailable():
        return TicketMutationToolResultV1(
            state="failed",
            error=SafeErrorV1(
                code="upstream_unavailable", message_key="ticket_service_unavailable", retryable=False
            ),
        )

    @staticmethod
    def _failed_auth():
        return TicketMutationToolResultV1(
            state="failed",
            error=SafeErrorV1(
                code="authentication_required", message_key="authentication_required", retryable=False
            ),
        )
