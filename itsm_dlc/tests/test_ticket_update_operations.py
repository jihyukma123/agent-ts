from __future__ import annotations

import asyncio
from functools import wraps
from datetime import datetime, timedelta, timezone

import pytest
import httpx

from app.db import init_db, open_db
from app.ticketing.capability import (
    DelegatedCredential,
    TicketRequestCapability,
    UpdateCredentialRefreshPortV1,
)
from app.ticketing.gateway import GatewayAttemptDeadline, GatewayFailure, GatewayUpdateSuccess, ItsmGateway
from app.ticketing.lifecycle import CurrentTurnEvidenceV1
from app.ticketing.models import ReadForUpdateResultV1, SafeErrorV1, SafeTicketDetailV1
from app.ticketing.repositories import CursorRepository, PendingMutationRepository
from app.ticketing.update_models import TicketPatchInV1, TicketUpdateToolInputV1
from app.ticketing.update_models import UpdateCredentialRefreshReservationV1
from app.ticketing.update_operations import TicketUpdateOperations
from app.ticketing.update_repository import PendingUpdateExecutionRepository


NOW = datetime.now(timezone.utc)


def async_test(function):
    @wraps(function)
    def run(*args, **kwargs):
        return asyncio.run(function(*args, **kwargs))

    return run


def _detail(*, version=3, title="old", urgency="low"):
    return SafeTicketDetailV1(
        id="ticket-generated",
        title=title,
        description="description",
        service="service",
        impact_level="moderate",
        urgency=urgency,
        impact_note="note",
        status="received",
        priority="P3",
        version=version,
        component="component",
        model="M1",
        is_major=False,
    )


def _snapshot(*, version=3, title="old", urgency="low"):
    return ReadForUpdateResultV1(
        detail=_detail(version=version, title=title, urgency=urgency),
        if_match_version=f'"{version}"',
        server_current_values={
            "title": title,
            "description": "description",
            "component_id": 4,
            "model": "M1",
            "impact_level": "moderate",
            "urgency": urgency,
            "impact_note": "note",
            "is_major": False,
        },
    )


def _raw_ticket(*, version=4, title="new", urgency="low"):
    return {
        "id": "ticket-generated",
        "title": title,
        "description": "description",
        "service": {"id": "service", "name": "service"},
        "component": {"id": 4, "name": "component"},
        "model": "M1",
        "impact_level": "moderate",
        "urgency": urgency,
        "impact_note": "note",
        "status": "received",
        "priority": "P3",
        "version": version,
        "is_major": False,
    }


class FakeReads:
    def __init__(self, *snapshots):
        self.snapshots = list(snapshots or [_snapshot()])
        self.calls = 0

    async def read_for_update(self, _capability, _ticket_id):
        self.calls += 1
        return self.snapshots.pop(0) if len(self.snapshots) > 1 else self.snapshots[0]

    @staticmethod
    def _update_values(raw):
        component = raw.get("component")
        return {
            "title": raw["title"],
            "description": raw["description"],
            "component_id": component.get("id") if isinstance(component, dict) else None,
            "model": raw.get("model"),
            "impact_level": raw["impact_level"],
            "urgency": raw["urgency"],
            "impact_note": raw["impact_note"],
            "is_major": raw.get("is_major"),
        }


class FakeGateway:
    def __init__(self, *outcomes):
        self.outcomes = list(outcomes or [GatewayUpdateSuccess(_raw_ticket(), 4)])
        self.calls = []

    async def update_ticket_once(self, _capability, ticket_id, body, if_match, _attempt):
        self.calls.append((ticket_id, body, if_match))
        return self.outcomes.pop(0) if len(self.outcomes) > 1 else self.outcomes[0]


class CountingRefreshPort(UpdateCredentialRefreshPortV1):
    def __init__(self):
        self.calls = 0
        self._verifier = None

    def bind_verifier(self, verifier):
        self._verifier = verifier

    async def refresh_update_capability_once(self, capability, reservation, remaining_deadline):
        self.calls += 1
        return await super().refresh_update_capability_once(capability, reservation, remaining_deadline)


def _cap(*, generation=1, refresh_counter=None):
    async def credential():
        return DelegatedCredential("credential-canary", NOW + timedelta(minutes=10), generation)

    async def refresh():
        if refresh_counter is not None:
            refresh_counter.append(1)
        return DelegatedCredential("refreshed-canary", NOW + timedelta(minutes=10), generation + 1)

    return TicketRequestCapability(
        7,
        "session-generated",
        "conversation-generated",
        credential,
        refresh,
        _utc_now=lambda: NOW,
        _credential_generation=generation,
    )


def _evidence(decision="revise_or_ambiguous", **overrides):
    data = dict(
        turn_id="turn-generated",
        user_id=7,
        session_id="session-generated",
        conversation_id="conversation-generated",
        raw_message_sha256="a" * 64,
        decision=decision,
    )
    data.update(overrides)
    return CurrentTurnEvidenceV1(**data)


def _ops(tmp_path, *, reads=None, gateway=None, refresh_port=None):
    db = tmp_path / "state.db"
    init_db(db)
    pending = PendingMutationRepository(db)
    executions = PendingUpdateExecutionRepository(db)
    if isinstance(refresh_port, CountingRefreshPort):
        refresh_port.bind_verifier(pending)
    reads = reads or FakeReads(_snapshot())
    gateway = gateway or FakeGateway()
    ops = TicketUpdateOperations(
        reads,
        gateway,
        pending,
        executions,
        credential_refresh_port=refresh_port,
        utc_now=lambda: NOW,
        monotonic=lambda: 10.0,
        timeout_seconds=5,
    )
    return ops, pending, executions, gateway, reads


async def _prepare(ops, cap=None, patch=None):
    return await ops.ticket_update(
        cap or _cap(),
        TicketUpdateToolInputV1(
            action="prepare",
            ticket_id="ticket-generated",
            patch=patch or TicketPatchInV1(title="new"),
        ),
        _evidence(),
    )


@async_test
async def test_prepare_publishes_active_review_with_exact_binding(tmp_path):
    ops, pending, executions, gateway, _reads = _ops(tmp_path)
    result = await _prepare(ops)
    assert result.state == "review_required" and result.pending_ref
    assert pending.get(result.pending_ref).status == "pending"
    assert executions.get(result.pending_ref).status == "active"
    assert gateway.calls == []


@async_test
async def test_noop_fails_without_pending_or_patch(tmp_path):
    ops, _pending, _executions, gateway, _reads = _ops(tmp_path)
    result = await _prepare(ops, patch=TicketPatchInV1(title="old"))
    assert result.state == "failed" and result.error.code == "invalid_input"
    assert gateway.calls == []


@async_test
async def test_confirm_requires_current_turn_explicit_command(tmp_path):
    ops, pending, _executions, gateway, _reads = _ops(tmp_path, reads=FakeReads(_snapshot(), _snapshot()))
    review = await _prepare(ops)
    result = await ops.ticket_update(
        _cap(),
        TicketUpdateToolInputV1(action="confirm", ticket_id="ticket-generated", pending_ref=review.pending_ref),
        _evidence(),
    )
    assert result.state == "needs_explicit_confirmation"
    assert pending.get(review.pending_ref).status == "pending"
    assert gateway.calls == []


@async_test
async def test_confirm_success_consumes_once_and_patches_once(tmp_path):
    gateway = FakeGateway(GatewayUpdateSuccess(_raw_ticket(title="new"), 4, "request-generated"))
    ops, pending, executions, gateway, _reads = _ops(tmp_path, gateway=gateway)
    review = await _prepare(ops)
    result = await ops.ticket_update(
        _cap(),
        TicketUpdateToolInputV1(action="confirm", ticket_id="ticket-generated", pending_ref=review.pending_ref),
        _evidence("explicit_confirm"),
    )
    assert result.state == "succeeded" and result.data.new_version == 4
    assert pending.get(review.pending_ref).status == "consumed"
    assert executions.get(review.pending_ref).status == "retired"
    assert len(gateway.calls) == 1 and gateway.calls[0][2] == '"3"'


@async_test
async def test_confirm_binding_mismatch_never_consumes_or_patches(tmp_path):
    ops, pending, _executions, gateway, _reads = _ops(tmp_path)
    review = await _prepare(ops)
    result = await ops.ticket_update(
        _cap(),
        TicketUpdateToolInputV1(action="confirm", ticket_id="other-generated", pending_ref=review.pending_ref),
        _evidence("explicit_confirm"),
    )
    assert result.state == "invalidated"
    assert pending.get(review.pending_ref).status == "pending"
    assert gateway.calls == []


@async_test
async def test_conflict_reconciles_applied_change_without_patch_replay(tmp_path):
    conflict = GatewayFailure(412, "version_conflict", SafeErrorV1(code="conflict", message_key="ticket_conflict", retryable=False))
    reads = FakeReads(_snapshot(), _snapshot(version=4, title="new"))
    ops, _pending, _executions, gateway, _reads = _ops(tmp_path, reads=reads, gateway=FakeGateway(conflict))
    review = await _prepare(ops)
    result = await ops.ticket_update(
        _cap(),
        TicketUpdateToolInputV1(action="confirm", ticket_id="ticket-generated", pending_ref=review.pending_ref),
        _evidence("explicit_confirm"),
    )
    assert result.state == "succeeded"
    assert len(gateway.calls) == 1 and reads.calls == 2


@async_test
async def test_ambiguous_partial_result_creates_new_review_not_patch_replay(tmp_path):
    ambiguous = GatewayFailure(None, None, SafeErrorV1(code="upstream_unavailable", message_key="ticket_service_unavailable", retryable=True), transient=True)
    reads = FakeReads(_snapshot(), _snapshot(version=4, title="old"))
    ops, _pending, _executions, gateway, _reads = _ops(tmp_path, reads=reads, gateway=FakeGateway(ambiguous))
    review = await _prepare(ops)
    result = await ops.ticket_update(
        _cap(),
        TicketUpdateToolInputV1(action="confirm", ticket_id="ticket-generated", pending_ref=review.pending_ref),
        _evidence("explicit_confirm"),
    )
    assert result.state == "review_required" and result.pending_ref != review.pending_ref
    assert len(gateway.calls) == 1


@async_test
async def test_authoritative_token_expiry_allows_one_refresh_and_identical_final_patch(tmp_path):
    first = GatewayFailure(
        401,
        "token_expired",
        SafeErrorV1(code="authentication_required", message_key="authentication_required", retryable=True),
        authoritative_no_apply=True,
    )
    second = GatewayUpdateSuccess(_raw_ticket(title="new"), 4)
    refresh_port = CountingRefreshPort()
    refresh_counter = []
    cap = _cap(refresh_counter=refresh_counter)
    ops, _pending, _executions, gateway, _reads = _ops(
        tmp_path,
        gateway=FakeGateway(first, second),
        refresh_port=refresh_port,
    )
    review = await _prepare(ops, cap=cap)
    result = await ops.ticket_update(
        cap,
        TicketUpdateToolInputV1(action="confirm", ticket_id="ticket-generated", pending_ref=review.pending_ref),
        _evidence("explicit_confirm"),
    )
    assert result.state == "succeeded"
    assert refresh_port.calls == 1 and refresh_counter == [1]
    assert len(gateway.calls) == 2 and gateway.calls[0][1:] == gateway.calls[1][1:]


@pytest.mark.parametrize(
    "scenario",
    (
        "prefix",
        "user",
        "session",
        "conversation",
        "missing_reservation",
        "failed_reservation",
        "missing_execution",
        "retired",
        "unconsumed",
    ),
)
@async_test
async def test_update_refresh_provider_rejects_unauthenticated_durable_authority_before_exchange(
    tmp_path, scenario
):
    exchange_count = []
    capability = _cap(refresh_counter=exchange_count)
    operations, pending, executions, _gateway, _reads = _ops(tmp_path)
    review = await _prepare(operations, cap=capability)
    mutation = operations._pending_update(review.pending_ref)
    assert mutation is not None
    if scenario != "unconsumed":
        assert pending.confirm_and_consume_update(
            operations._binding(mutation), _evidence("explicit_confirm")
        ) is not None
    reservation = executions.reserve_refresh_once(
        mutation.mutation_id, base_credential_generation=capability.credential_generation
    )
    assert reservation is not None
    attempted = reservation
    target_capability = capability
    if scenario == "prefix":
        attempted = reservation.model_copy(update={"refresh_reservation_id": "create.tampered"})
    elif scenario == "user":
        target_capability = _cap(refresh_counter=exchange_count)
        target_capability.user_id = 8
    elif scenario == "session":
        target_capability = _cap(refresh_counter=exchange_count)
        target_capability.session_id = "other-session"
    elif scenario == "conversation":
        target_capability = _cap(refresh_counter=exchange_count)
        target_capability.conversation_id = "other-conversation"
    elif scenario == "missing_reservation":
        with open_db(executions.db_path) as conn, conn:
            conn.execute(
                """UPDATE pending_update_executions
                   SET refresh_reservation_id=NULL, refresh_state=NULL,
                       refresh_used_at=NULL, refresh_base_credential_generation=NULL
                   WHERE mutation_id=?""",
                (mutation.mutation_id,),
            )
    elif scenario == "failed_reservation":
        with open_db(executions.db_path) as conn, conn:
            conn.execute(
                "UPDATE pending_update_executions SET refresh_state='failed' WHERE mutation_id=?",
                (mutation.mutation_id,),
            )
    elif scenario == "missing_execution":
        with open_db(executions.db_path) as conn, conn:
            conn.execute(
                "DELETE FROM pending_update_executions WHERE mutation_id=?",
                (mutation.mutation_id,),
            )
    elif scenario == "retired":
        assert executions.retire(mutation.mutation_id, expected_status="active")

    result = await UpdateCredentialRefreshPortV1(pending).refresh_update_capability_once(
        target_capability, attempted, 5.0
    )
    assert result.ok is False
    assert exchange_count == []


@async_test
async def test_untyped_401_does_not_refresh_or_resend_patch(tmp_path):
    first = GatewayFailure(401, None, SafeErrorV1(code="authentication_required", message_key="authentication_required", retryable=False))
    reads = FakeReads(_snapshot(), SafeErrorV1(code="upstream_unavailable", message_key="ticket_service_unavailable", retryable=True))
    refresh_port = CountingRefreshPort()
    ops, _pending, _executions, gateway, _reads = _ops(tmp_path, reads=reads, gateway=FakeGateway(first), refresh_port=refresh_port)
    review = await _prepare(ops)
    result = await ops.ticket_update(
        _cap(),
        TicketUpdateToolInputV1(action="confirm", ticket_id="ticket-generated", pending_ref=review.pending_ref),
        _evidence("explicit_confirm"),
    )
    assert result.state == "ambiguous"
    assert len(gateway.calls) == 1 and refresh_port.calls == 0


@async_test
async def test_rate_limit_returns_immediately_without_read_or_retry(tmp_path):
    limited = GatewayFailure(429, "rate_limited", SafeErrorV1(code="rate_limited", message_key="ticket_rate_limited", retryable=True, retry_after_seconds=2))
    reads = FakeReads(_snapshot())
    ops, _pending, _executions, gateway, _reads = _ops(tmp_path, reads=reads, gateway=FakeGateway(limited))
    review = await _prepare(ops)
    result = await ops.ticket_update(
        _cap(),
        TicketUpdateToolInputV1(action="confirm", ticket_id="ticket-generated", pending_ref=review.pending_ref),
        _evidence("explicit_confirm"),
    )
    assert result.state == "failed" and result.error.code == "rate_limited"
    assert len(gateway.calls) == 1 and reads.calls == 1


@async_test
async def test_cancel_retires_authority_and_never_patches(tmp_path):
    ops, pending, executions, gateway, _reads = _ops(tmp_path)
    review = await _prepare(ops)
    result = await ops.ticket_update(
        _cap(),
        TicketUpdateToolInputV1(action="cancel", ticket_id="ticket-generated", pending_ref=review.pending_ref),
        _evidence("explicit_cancel"),
    )
    assert result.state == "cancelled"
    assert pending.get(review.pending_ref).status == "cancelled"
    assert executions.get(review.pending_ref).status == "retired"
    assert gateway.calls == []


class WireClient:
    def __init__(self):
        self.calls = []

    async def ticket_request_once(self, method, path, *, delegated_token, **kwargs):
        self.calls.append((method, path, delegated_token, kwargs))
        return httpx.Response(
            200,
            request=httpx.Request(method, f"https://itsm.invalid{path}"),
            json=_raw_ticket(title="new"),
            headers={"x-request-id": "request-generated"},
        )


def test_gateway_sends_exact_patch_bytes_and_if_match_once():
    client = WireClient()
    capability = _cap()
    body = TicketPatchInV1(title="new").canonical_patch().body_bytes
    result = asyncio.run(
        ItsmGateway(client).update_ticket_once(
            capability,
            "ticket-generated",
            body,
            '"3"',
            GatewayAttemptDeadline(20.0, 5.0, lambda: 10.0),
        )
    )
    assert isinstance(result, GatewayUpdateSuccess)
    assert client.calls == [(
        "PATCH",
        "/api/v2/tickets/ticket-generated",
        "credential-canary",
        {"timeout": 5.0, "content": body, "headers": {"Content-Type": "application/json", "If-Match": '"3"'}},
    )]


def test_gateway_rejects_invalid_if_match_before_network():
    client = WireClient()
    with pytest.raises(ValueError, match="If-Match"):
        asyncio.run(
            ItsmGateway(client).update_ticket_once(
                _cap(),
                "ticket-generated",
                b'{"title":"new"}',
                "3",
                GatewayAttemptDeadline(20.0, 5.0, lambda: 10.0),
            )
        )
    assert client.calls == []

# Exercise the complete one-attempt gateway/capability provider regression in
# the exact U3 coverage run because U3 additively changes those shared modules.
from tests.test_ticket_gateway import *  # noqa: F403,E402,F401
