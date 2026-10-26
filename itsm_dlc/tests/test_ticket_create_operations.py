from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from app.db import init_db
from app.ticketing.capability import (
    ActiveCredentialRefreshReservations,
    CredentialRefreshPortV1,
    CredentialRefreshReservationSigner,
    CredentialRefreshReservationVerifier,
    DelegatedCredential,
    Result,
    TicketRequestCapability,
)
from app.ticketing.create_models import TicketWriteDraftV1, TicketWriteToolInputV1
from app.ticketing.create_operations import TicketCreateOperations
from app.ticketing.create_repository import TicketCreateAttemptRepository
from app.ticketing.gateway import GatewayCatalogSuccess, GatewayCreateSuccess, GatewayFailure
from app.ticketing.lifecycle import CurrentTurnEvidenceV1
from app.ticketing.models import PendingMutationBindingV1, SafeErrorV1
from app.ticketing.repositories import PendingMutationRepository


def draft(title="Generated issue", *, service=None):
    return TicketWriteDraftV1.model_validate(
        {
            "title": title,
            "description": "A generated integration issue",
            "service": service or {"id": "svc-1"},
            "component": {"name": "API"},
            "model": {"name": "Laptop"},
            "impact_level": "moderate",
            "urgency": "medium",
            "impact_note": "Affects a generated workflow",
            "occurred_at": {"kind": "now"},
        }
    )


class FakeGateway:
    def __init__(self, outcomes=None, *, services=None):
        self.services = services or [
            {
                "id": "svc-1",
                "name": "Support",
                "status": "active",
                "components": [{"id": 4, "name": "API"}],
            }
        ]
        self.models = [{"id": "mdl-1", "name": "Laptop", "status": "active"}]
        self.outcomes = list(outcomes or [GatewayCreateSuccess("generated-ticket", False)])
        self.posts = []

    async def catalog_services_once(self, capability, attempt):
        return GatewayCatalogSuccess(self.services)

    async def catalog_models_once(self, capability, attempt):
        return GatewayCatalogSuccess(self.models)

    async def create_ticket_once(self, capability, body, key, attempt):
        self.posts.append((body, key))
        return self.outcomes.pop(0)


@pytest.fixture
def setup(tmp_path):
    now = datetime.now(timezone.utc).replace(microsecond=0)
    path = tmp_path / "state.db"
    init_db(path)
    repository = TicketCreateAttemptRepository(path, utc_now=lambda: now)
    pending = PendingMutationRepository(path)
    counter = iter(f"opaque-{number}" for number in range(100))
    refresh_calls = []

    async def current():
        return DelegatedCredential("delegated-current")

    async def refresh():
        refresh_calls.append(True)
        return DelegatedCredential("delegated-refreshed", generation=1)

    capability = TicketRequestCapability(7, "session-7", "conversation-7", current, refresh)
    keys = {"test-current": b"v" * 32}
    signer = CredentialRefreshReservationSigner(
        kid="test-current", secret=keys["test-current"], creator_boot_id="test-boot"
    )
    verifier = CredentialRefreshReservationVerifier(
        keys,
        current_kid="test-current",
        current_boot_id="test-boot",
        utc_now=lambda: now,
    )

    def make(gateway, **overrides):
        options = {
            "timeout_seconds": 10,
            "utc_now": lambda: now,
            "monotonic": lambda: 100.0,
            "random_token": lambda: next(counter),
            "refresh_reservation_signer": signer,
            "refresh_reservation_verifier": verifier,
            "credential_refresh_port": CredentialRefreshPortV1(verifier),
            "active_refresh_reservations": ActiveCredentialRefreshReservations(),
        }
        options.update(overrides)
        return TicketCreateOperations(
            gateway,
            repository,
            pending,
            **options,
        )

    return make, capability, repository, pending, refresh_calls


def evidence(raw="confirm"):
    return CurrentTurnEvidenceV1.capture(
        user_id=7,
        session_id="session-7",
        conversation_id="conversation-7",
        raw_message=raw,
    )


@pytest.mark.anyio
async def test_prepare_commits_ledger_before_review_and_sends_no_post(setup):
    make, capability, repository, _, _ = setup
    gateway = FakeGateway()
    result = await make(gateway).ticket_write(
        capability, TicketWriteToolInputV1(action="prepare", draft=draft()), evidence("ordinary")
    )
    assert result.state == "review_required" and result.pending_ref
    row = repository.find_prepared_for_binding(
        user_id=7,
        session_id="session-7",
        conversation_id="conversation-7",
        normalized_payload_hash=result.review.normalized_payload_hash,
    )
    assert row is not None and row.attempt_count == 0
    assert gateway.posts == []


@pytest.mark.anyio
async def test_ambiguous_or_retired_catalog_is_rejected_before_ledger(setup):
    make, capability, repository, _, _ = setup
    services = [
        {"id": "svc-a", "name": "Same", "status": "active", "components": []},
        {"id": "svc-b", "name": "Same", "status": "active", "components": []},
        {"id": "svc-c", "name": "Retired", "status": "deprecated", "components": []},
    ]
    result = await make(FakeGateway(services=services)).ticket_write(
        capability,
        TicketWriteToolInputV1(action="prepare", draft=draft(service={"name": "Same"})),
        evidence("ordinary"),
    )
    assert result.state == "failed" and result.error.code == "invalid_input"
    assert repository.find_prepared_for_actor(
        user_id=7, session_id="session-7", conversation_id="conversation-7"
    ) is None


@pytest.mark.anyio
@pytest.mark.parametrize("action", ["prepare", "revise"])
async def test_invalid_replacement_preserves_existing_pending_and_prepared_authority(setup, action):
    make, capability, repository, pending, _ = setup
    gateway = FakeGateway()
    operations = make(gateway)
    prepared = await operations.ticket_write(
        capability, TicketWriteToolInputV1(action="prepare", draft=draft()), evidence("ordinary")
    )
    original_pending = pending.get(prepared.pending_ref)
    original_attempt = repository.find_prepared_for_binding(
        user_id=7,
        session_id="session-7",
        conversation_id="conversation-7",
        normalized_payload_hash=prepared.review.normalized_payload_hash,
    )
    invalid_gateway = FakeGateway(
        services=[
            {"id": "svc-a", "name": "Ambiguous", "status": "active", "components": []},
            {"id": "svc-b", "name": "Ambiguous", "status": "active", "components": []},
        ]
    )
    invalid_operations = make(invalid_gateway)
    value = TicketWriteToolInputV1(
        action=action,
        draft=draft(service={"name": "Ambiguous"}),
        pending_ref=prepared.pending_ref if action == "revise" else None,
    )

    result = await invalid_operations.ticket_write(capability, value, evidence("ordinary"))

    assert result.state == "failed" and result.error.code == "invalid_input"
    assert pending.get(prepared.pending_ref) == original_pending
    assert repository.get(original_attempt.create_attempt_id) == original_attempt
    assert gateway.posts == [] and invalid_gateway.posts == []


@pytest.mark.anyio
async def test_pending_publish_failure_leaves_non_authoritative_orphan_and_zero_post(setup):
    make, capability, repository, pending, _ = setup
    gateway = FakeGateway()
    pending.save_review = lambda mutation: (_ for _ in ()).throw(RuntimeError("synthetic"))
    result = await make(gateway).ticket_write(
        capability, TicketWriteToolInputV1(action="prepare", draft=draft()), evidence("ordinary")
    )
    assert result.state == "failed"
    assert repository.find_prepared_for_actor(
        user_id=7, session_id="session-7", conversation_id="conversation-7"
    ) is not None
    assert gateway.posts == []


@pytest.mark.anyio
async def test_exact_current_turn_confirmation_creates_once_with_stored_binding(setup):
    make, capability, repository, _, _ = setup
    gateway = FakeGateway()
    operations = make(gateway)
    prepared = await operations.ticket_write(
        capability, TicketWriteToolInputV1(action="prepare", draft=draft()), evidence("ordinary")
    )
    result = await operations.ticket_write(
        capability,
        TicketWriteToolInputV1(action="confirm", pending_ref=prepared.pending_ref),
        evidence("  CONFIRM "),
    )
    assert result.state == "succeeded" and result.data.ticket_id == "generated-ticket"
    row = repository.find_prepared_for_binding(
        user_id=7,
        session_id="session-7",
        conversation_id="conversation-7",
        normalized_payload_hash=prepared.review.normalized_payload_hash,
    )
    assert row is None
    completed = next(
        repository.get(identifier) for identifier in ("opaque-0", "opaque-1", "opaque-2")
        if repository.get(identifier) is not None
    )
    assert completed.status == "completed" and completed.attempt_count == 1
    assert gateway.posts == [(completed.canonical_request_bytes, completed.idempotency_key)]


@pytest.mark.anyio
async def test_embedded_confirmation_never_consumes_or_posts(setup):
    make, capability, _, _, _ = setup
    gateway = FakeGateway()
    operations = make(gateway)
    prepared = await operations.ticket_write(
        capability, TicketWriteToolInputV1(action="prepare", draft=draft()), evidence("ordinary")
    )
    result = await operations.ticket_write(
        capability,
        TicketWriteToolInputV1(action="confirm", pending_ref=prepared.pending_ref),
        evidence("확인하고 제목 변경"),
    )
    assert result.state == "needs_explicit_confirmation"
    assert gateway.posts == []


@pytest.mark.anyio
async def test_cancel_terminates_revocable_authority_without_post(setup):
    make, capability, repository, _, _ = setup
    gateway = FakeGateway()
    operations = make(gateway)
    prepared = await operations.ticket_write(
        capability, TicketWriteToolInputV1(action="prepare", draft=draft()), evidence("ordinary")
    )
    result = await operations.ticket_write(
        capability,
        TicketWriteToolInputV1(action="cancel", pending_ref=prepared.pending_ref),
        evidence("취소"),
    )
    assert result.state == "cancelled" and gateway.posts == []
    assert repository.find_prepared_for_actor(
        user_id=7, session_id="session-7", conversation_id="conversation-7"
    ) is None


@pytest.mark.anyio
async def test_transport_ambiguity_reconciles_then_replays_same_bytes_and_key_once(setup):
    make, capability, _, _, _ = setup
    ambiguous = GatewayFailure(
        status=None,
        upstream_code=None,
        safe_error=SafeErrorV1(
            code="upstream_unavailable", message_key="ticket_service_unavailable", retryable=True
        ),
        transient=True,
    )
    gateway = FakeGateway([ambiguous, GatewayCreateSuccess("generated-ticket", True)])
    operations = make(gateway)
    prepared = await operations.ticket_write(
        capability, TicketWriteToolInputV1(action="prepare", draft=draft()), evidence("ordinary")
    )
    result = await operations.ticket_write(
        capability,
        TicketWriteToolInputV1(action="confirm", pending_ref=prepared.pending_ref),
        evidence("confirm"),
    )
    assert result.state == "succeeded" and result.data.idempotent_replayed
    assert len(gateway.posts) == 2 and gateway.posts[0] == gateway.posts[1]


@pytest.mark.anyio
async def test_second_ambiguous_result_is_terminal_and_third_post_is_zero(setup):
    make, capability, _, _, _ = setup
    ambiguous = GatewayFailure(
        status=None,
        upstream_code=None,
        safe_error=SafeErrorV1(
            code="upstream_unavailable", message_key="ticket_service_unavailable", retryable=True
        ),
        transient=True,
    )
    gateway = FakeGateway([ambiguous, ambiguous])
    operations = make(gateway)
    prepared = await operations.ticket_write(
        capability, TicketWriteToolInputV1(action="prepare", draft=draft()), evidence("ordinary")
    )
    result = await operations.ticket_write(
        capability,
        TicketWriteToolInputV1(action="confirm", pending_ref=prepared.pending_ref),
        evidence("confirm"),
    )
    assert result.state == "ambiguous"
    assert len(gateway.posts) == 2


@pytest.mark.anyio
async def test_token_expired_reserves_then_refreshes_once_and_retries_same_binding(setup):
    make, capability, _, _, refresh_calls = setup
    expired = GatewayFailure(
        status=401,
        upstream_code="token_expired",
        safe_error=SafeErrorV1(
            code="authentication_required", message_key="authentication_required", retryable=True
        ),
    )
    gateway = FakeGateway([expired, GatewayCreateSuccess("generated-ticket", False)])
    operations = make(gateway)
    prepared = await operations.ticket_write(
        capability, TicketWriteToolInputV1(action="prepare", draft=draft()), evidence("ordinary")
    )
    result = await operations.ticket_write(
        capability,
        TicketWriteToolInputV1(action="confirm", pending_ref=prepared.pending_ref),
        evidence("confirm"),
    )
    assert result.state == "succeeded"
    assert refresh_calls == [True]
    assert len(gateway.posts) == 2 and gateway.posts[0] == gateway.posts[1]


@pytest.mark.anyio
async def test_create_operations_consumes_the_shared_c01_typed_result_fixture(
    setup, c01_refresh_contract
):
    make, _, _, _, _ = setup
    expired = GatewayFailure(
        status=401,
        upstream_code="token_expired",
        safe_error=SafeErrorV1(
            code="authentication_required",
            message_key="authentication_required",
            retryable=True,
        ),
    )
    gateway = FakeGateway([expired, GatewayCreateSuccess("generated-ticket", False)])
    operations = make(
        gateway,
        utc_now=lambda: c01_refresh_contract["now"],
        refresh_reservation_signer=c01_refresh_contract["signer"],
        refresh_reservation_verifier=c01_refresh_contract["verifier"],
        credential_refresh_port=c01_refresh_contract["port"],
    )
    capability = c01_refresh_contract["capability"]
    prepared = await operations.ticket_write(
        capability,
        TicketWriteToolInputV1(action="prepare", draft=draft()),
        evidence("ordinary"),
    )
    result = await operations.ticket_write(
        capability,
        TicketWriteToolInputV1(action="confirm", pending_ref=prepared.pending_ref),
        evidence("confirm"),
    )
    assert result.state == "succeeded"
    assert c01_refresh_contract["exchange_calls"] == [
        "exchange-and-atomic-session-commit"
    ]


class SimulatedProcessExit(BaseException):
    pass


class CrashBeforeRegisterRegistry(ActiveCredentialRefreshReservations):
    def register(self, reservation_id: str) -> bool:
        raise SimulatedProcessExit


@pytest.mark.anyio
async def test_same_boot_observer_does_not_recover_commit_before_registry(setup):
    make, capability, repository, pending_repository, refresh_calls = setup
    expired = GatewayFailure(
        status=401,
        upstream_code="token_expired",
        safe_error=SafeErrorV1(
            code="authentication_required",
            message_key="authentication_required",
            retryable=True,
        ),
    )
    operations = make(
        FakeGateway([expired]),
        active_refresh_reservations=CrashBeforeRegisterRegistry(),
    )
    prepared = await operations.ticket_write(
        capability,
        TicketWriteToolInputV1(action="prepare", draft=draft()),
        evidence("ordinary"),
    )
    with pytest.raises(SimulatedProcessExit):
        await operations.ticket_write(
            capability,
            TicketWriteToolInputV1(action="confirm", pending_ref=prepared.pending_ref),
            evidence("confirm"),
        )
    pending = pending_repository.get(prepared.pending_ref)
    assert pending is not None
    before = repository.find_for_consumed_pending(
        user_id=7,
        session_id="session-7",
        conversation_id="conversation-7",
        normalized_payload_hash=pending.normalized_payload_hash,
        mutation_id=pending.mutation_id,
    )
    assert before is not None and before.refresh_state == "reserved"

    observer = make(FakeGateway())
    await observer._recover_consumed_pending(capability.operation_scope(), pending)
    after = repository.get(before.create_attempt_id)
    assert after == before
    assert refresh_calls == []


@pytest.mark.anyio
async def test_owner_cancellation_removes_registry_but_same_boot_observer_still_writes_nothing(
    setup,
):
    make, capability, repository, pending_repository, refresh_calls = setup
    expired = GatewayFailure(
        status=401,
        upstream_code="token_expired",
        safe_error=SafeErrorV1(
            code="authentication_required",
            message_key="authentication_required",
            retryable=True,
        ),
    )
    entered = asyncio.Event()
    release = asyncio.Event()
    port_calls = []

    class BlockingPort:
        async def refresh_delegated_capability_once(
            self, capability, reservation, remaining_deadline
        ):
            port_calls.append(reservation.refresh_reservation_id)
            entered.set()
            await release.wait()
            return Result.failure(
                SafeErrorV1(
                    code="authentication_required",
                    message_key="credential_refresh_failed",
                    retryable=False,
                )
            )

    registry = ActiveCredentialRefreshReservations()
    operations = make(
        FakeGateway([expired]),
        credential_refresh_port=BlockingPort(),
        active_refresh_reservations=registry,
    )
    prepared = await operations.ticket_write(
        capability,
        TicketWriteToolInputV1(action="prepare", draft=draft()),
        evidence("ordinary"),
    )
    owner = asyncio.create_task(
        operations.ticket_write(
            capability,
            TicketWriteToolInputV1(action="confirm", pending_ref=prepared.pending_ref),
            evidence("confirm"),
        )
    )
    await entered.wait()
    pending = pending_repository.get(prepared.pending_ref)
    assert pending is not None
    row = repository.find_for_consumed_pending(
        user_id=7,
        session_id="session-7",
        conversation_id="conversation-7",
        normalized_payload_hash=pending.normalized_payload_hash,
        mutation_id=pending.mutation_id,
    )
    assert row is not None and row.refresh_reservation_id is not None
    assert registry.contains(row.refresh_reservation_id)
    active_before = repository.get(row.create_attempt_id)
    await operations._recover_consumed_pending(capability.operation_scope(), pending)
    assert repository.get(row.create_attempt_id) == active_before
    assert len(port_calls) == 1
    owner.cancel()
    with pytest.raises(asyncio.CancelledError):
        await owner
    assert not registry.contains(row.refresh_reservation_id)

    before = repository.get(row.create_attempt_id)
    assert before is not None
    await operations._recover_consumed_pending(capability.operation_scope(), pending)
    assert repository.get(row.create_attempt_id) == before
    assert len(port_calls) == 1 and refresh_calls == []


@pytest.mark.anyio
async def test_different_boot_restart_uses_generation_recovery_without_second_refresh(setup):
    make, capability, repository, pending_repository, refresh_calls = setup
    expired = GatewayFailure(
        status=401,
        upstream_code="token_expired",
        safe_error=SafeErrorV1(
            code="authentication_required",
            message_key="authentication_required",
            retryable=True,
        ),
    )
    first = make(
        FakeGateway([expired]),
        active_refresh_reservations=CrashBeforeRegisterRegistry(),
    )
    prepared = await first.ticket_write(
        capability,
        TicketWriteToolInputV1(action="prepare", draft=draft()),
        evidence("ordinary"),
    )
    with pytest.raises(SimulatedProcessExit):
        await first.ticket_write(
            capability,
            TicketWriteToolInputV1(action="confirm", pending_ref=prepared.pending_ref),
            evidence("confirm"),
        )

    async def committed():
        return DelegatedCredential("committed", generation=1)

    restarted_capability = TicketRequestCapability(
        7,
        "session-7",
        "conversation-7",
        committed,
        committed,
        _credential_generation=1,
    )
    keys = {"test-current": b"v" * 32}
    restarted_verifier = CredentialRefreshReservationVerifier(
        keys,
        current_kid="test-current",
        current_boot_id="different-boot",
        utc_now=lambda: datetime.now(timezone.utc).replace(microsecond=0),
        max_age=timedelta(days=1),
    )
    gateway = FakeGateway([GatewayCreateSuccess("generated-ticket", False)])
    restarted = make(
        gateway,
        refresh_reservation_verifier=restarted_verifier,
        credential_refresh_port=CredentialRefreshPortV1(restarted_verifier),
        active_refresh_reservations=ActiveCredentialRefreshReservations(),
    )
    pending = pending_repository.get(prepared.pending_ref)
    assert pending is not None
    reserved = repository.find_for_consumed_pending(
        user_id=7,
        session_id="session-7",
        conversation_id="conversation-7",
        normalized_payload_hash=pending.normalized_payload_hash,
        mutation_id=pending.mutation_id,
    )
    assert reserved is not None
    result = await restarted._recover_consumed_pending(restarted_capability, pending)
    row = repository.get(reserved.create_attempt_id)
    assert row is not None
    assert result.state == "succeeded"
    assert row.refresh_state == "committed"
    assert row.refresh_committed_credential_generation == 1
    assert len(gateway.posts) == 1 and refresh_calls == []


@pytest.mark.anyio
async def test_rate_limit_has_no_sleep_refresh_or_retry(setup):
    make, capability, _, _, refresh_calls = setup
    limited = GatewayFailure(
        status=429,
        upstream_code="rate_limited",
        safe_error=SafeErrorV1(
            code="rate_limited", message_key="ticket_rate_limited", retryable=True, retry_after_seconds=3
        ),
    )
    gateway = FakeGateway([limited])
    operations = make(gateway)
    prepared = await operations.ticket_write(
        capability, TicketWriteToolInputV1(action="prepare", draft=draft()), evidence("ordinary")
    )
    result = await operations.ticket_write(
        capability,
        TicketWriteToolInputV1(action="confirm", pending_ref=prepared.pending_ref),
        evidence("confirm"),
    )
    assert result.state == "failed" and result.error.code == "rate_limited"
    assert len(gateway.posts) == 1 and refresh_calls == []


@pytest.mark.anyio
async def test_consume_before_bind_crash_republishes_same_attempt_for_new_confirmation(setup):
    make, capability, repository, _, _ = setup
    gateway = FakeGateway()
    operations = make(gateway)
    prepared = await operations.ticket_write(
        capability, TicketWriteToolInputV1(action="prepare", draft=draft()), evidence("ordinary")
    )
    original = repository.find_prepared_for_binding(
        user_id=7,
        session_id="session-7",
        conversation_id="conversation-7",
        normalized_payload_hash=prepared.review.normalized_payload_hash,
    )
    original_bind = repository.bind_confirmation_evidence

    def crash_once(*args, **kwargs):
        repository.bind_confirmation_evidence = original_bind
        raise RuntimeError("synthetic crash boundary")

    repository.bind_confirmation_evidence = crash_once
    recovered = await operations.ticket_write(
        capability,
        TicketWriteToolInputV1(action="confirm", pending_ref=prepared.pending_ref),
        evidence("confirm"),
    )
    current = repository.get(original.create_attempt_id)
    assert recovered.state == "needs_explicit_confirmation"
    assert recovered.pending_ref != prepared.pending_ref
    assert current.canonical_request_bytes == original.canonical_request_bytes
    assert current.idempotency_key == original.idempotency_key
    assert gateway.posts == []


@pytest.mark.anyio
async def test_restart_after_consume_before_bind_republishes_same_attempt_and_requires_new_confirm(setup):
    make, capability, repository, pending, _ = setup
    gateway = FakeGateway()
    prepared = await make(gateway).ticket_write(
        capability, TicketWriteToolInputV1(action="prepare", draft=draft()), evidence("ordinary")
    )
    original = repository.find_prepared_for_binding(
        user_id=7,
        session_id="session-7",
        conversation_id="conversation-7",
        normalized_payload_hash=prepared.review.normalized_payload_hash,
    )
    binding = PendingMutationBindingV1(
        mutation_id=prepared.pending_ref,
        operation="create",
        user_id=7,
        session_id="session-7",
        conversation_id="conversation-7",
        normalized_payload_hash=prepared.review.normalized_payload_hash,
    )
    assert pending.confirm(binding, evidence("confirm")) is not None
    assert pending.consume_once(binding) is not None

    restarted_repository = TicketCreateAttemptRepository(repository.db_path)
    restarted_pending = PendingMutationRepository(repository.db_path)
    restarted_counter = iter(f"restart-{number}" for number in range(20))
    restarted = TicketCreateOperations(
        gateway,
        restarted_repository,
        restarted_pending,
        timeout_seconds=10,
        utc_now=lambda: datetime.now(timezone.utc).replace(microsecond=0),
        monotonic=lambda: 100.0,
        random_token=lambda: next(restarted_counter),
    )
    republished = await restarted.ticket_write(
        capability,
        TicketWriteToolInputV1(action="confirm", pending_ref=prepared.pending_ref),
        evidence("confirm"),
    )
    after_republish = restarted_repository.get(original.create_attempt_id)
    assert republished.state == "needs_explicit_confirmation"
    assert republished.pending_ref != prepared.pending_ref
    assert after_republish.confirmed_mutation_id is None
    assert after_republish.canonical_request_bytes == original.canonical_request_bytes
    assert after_republish.idempotency_key == original.idempotency_key
    assert gateway.posts == []

    completed = await restarted.ticket_write(
        capability,
        TicketWriteToolInputV1(action="confirm", pending_ref=republished.pending_ref),
        evidence("confirm"),
    )
    final = restarted_repository.get(original.create_attempt_id)
    assert completed.state == "succeeded"
    assert final.status == "completed" and final.attempt_count == 1
    assert gateway.posts == [(original.canonical_request_bytes, original.idempotency_key)]
