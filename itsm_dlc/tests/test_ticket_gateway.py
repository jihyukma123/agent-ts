from __future__ import annotations

import json
import inspect
import pickle
import time
from dataclasses import MISSING, fields, replace
from datetime import datetime, timedelta, timezone
from typing import get_origin, get_type_hints

import httpx
import pytest

from app.itsm_auth import ItsmAuthClient
from app.ticketing.capability import (
    CredentialRefreshPortV1,
    CredentialRefreshReservationSigner,
    CredentialRefreshReservationVerifier,
    DelegatedCredential,
    Result,
    TicketRequestCapability,
)
from app.ticketing.gateway import (
    GatewayAttemptDeadline,
    GatewayCatalogSuccess,
    GatewayCreateSuccess,
    GatewayFailure,
    GatewaySuccess,
    ItsmGateway,
)
from app.ticketing.models import CanonicalTicketQueryV1, SafeErrorV1


def _client(handler):
    return ItsmAuthClient("http://itsm.test", "client", "secret", transport=httpx.MockTransport(handler))


def _cap(token="delegated-canary", *, expires_at=None, refreshed="delegated-refreshed"):
    async def current():
        return DelegatedCredential(token, expires_at)

    async def refresh():
        return DelegatedCredential(refreshed, datetime(2099, 1, 1, tzinfo=timezone.utc))

    return TicketRequestCapability(7, "session", "conversation", current, refresh)


def _attempt(seconds=10.0):
    return GatewayAttemptDeadline(time.monotonic() + seconds, seconds)


@pytest.mark.anyio
async def test_list_forwards_all_filters_and_repeated_values_with_delegated_token():
    captured = []

    def handler(request):
        captured.append(request)
        return httpx.Response(200, json={"items": [], "page": {"limit": 7, "next_cursor": None, "has_more": False}})

    auth = _client(handler)
    gateway = ItsmGateway(auth)
    query = CanonicalTicketQueryV1(
        q="network",
        status=["received", "pending"],
        priority=["P1", "P2"],
        service_id="service",
        component_id=4,
        model="model",
        assignment_group_id=8,
        assignee="me",
        requester="none",
        classification="standard",
        is_major=False,
        created_from=datetime(2026, 1, 1, tzinfo=timezone.utc),
        created_to=datetime(2026, 2, 1, tzinfo=timezone.utc),
        updated_from=datetime(2026, 1, 2, tzinfo=timezone.utc),
        sort="priority",
        limit=7,
    )
    result = await gateway.list_once(_cap(), query, "raw-cursor", _attempt())
    await auth.aclose()
    assert isinstance(result, GatewaySuccess)
    request = captured[0]
    assert request.headers["authorization"] == "Bearer delegated-canary"
    assert request.url.params.get_list("status") == ["received", "pending"]
    assert request.url.params.get_list("priority") == ["P1", "P2"]
    assert request.url.params["cursor"] == "raw-cursor"
    assert request.url.params["is_major"] == "false"
    assert request.url.params["created_from"].endswith("Z")


@pytest.mark.anyio
async def test_detail_percent_encodes_opaque_identifier():
    seen = []

    def handler(request):
        seen.append(request.url.raw_path)
        return httpx.Response(200, json={"id": "opaque"}, headers={"ETag": '"2"'})

    auth = _client(handler)
    result = await ItsmGateway(auth).detail_once(_cap("delegated"), "opaque/id ?", _attempt())
    await auth.aclose()
    assert isinstance(result, GatewaySuccess) and result.etag == '"2"'
    assert b"opaque%2Fid%20%3F" in seen[0]


@pytest.mark.anyio
@pytest.mark.parametrize(
    "status,code,safe_code,message_key",
    [
        (401, "token_expired", "authentication_required", "authentication_required"),
        (403, "permission_denied", "permission_denied", "ticket_permission_denied"),
        (404, "ticket_not_found", "not_found", "ticket_not_found_or_not_visible"),
        (409, "conflict", "conflict", "ticket_conflict"),
        (422, "validation_failed", "invalid_input", "ticket_input_invalid"),
        (503, "internal_error", "upstream_unavailable", "ticket_service_unavailable"),
    ],
)
async def test_statuses_map_to_allowlisted_safe_errors(status, code, safe_code, message_key):
    canary = "secret-raw-problem-detail"
    auth = _client(lambda request: httpx.Response(status, json={"code": code, "detail": canary}, headers={"X-Request-ID": "safe-123"}))
    result = await ItsmGateway(auth).detail_once(_cap("delegated"), "generated", _attempt())
    await auth.aclose()
    assert isinstance(result, GatewayFailure)
    assert result.safe_error.code == safe_code and result.safe_error.message_key == message_key
    assert canary not in result.safe_error.model_dump_json()
    assert result.safe_error.request_id == "safe-123"


@pytest.mark.anyio
@pytest.mark.parametrize("retry_after,expected", [("7", 7), ("-1", None), ("tomorrow", None), (None, None)])
async def test_rate_limit_never_reflects_invalid_retry_after(retry_after, expected):
    headers = {"Retry-After": retry_after} if retry_after is not None else {}
    auth = _client(lambda request: httpx.Response(429, json={"code": "rate_limited", "detail": "raw"}, headers=headers))
    result = await ItsmGateway(auth).detail_once(_cap("delegated"), "generated", _attempt())
    await auth.aclose()
    assert isinstance(result, GatewayFailure)
    assert result.safe_error.code == "rate_limited"
    assert result.safe_error.retry_after_seconds == expected
    assert result.transient is False


@pytest.mark.anyio
async def test_transport_failure_is_typed_and_contains_no_exception_text():
    def handler(request):
        raise httpx.ConnectError("contains-secret", request=request)

    auth = _client(handler)
    result = await ItsmGateway(auth).detail_once(_cap("delegated"), "generated", _attempt())
    await auth.aclose()
    assert isinstance(result, GatewayFailure) and result.transient
    assert "contains-secret" not in result.safe_error.model_dump_json()


@pytest.mark.anyio
async def test_malformed_success_fails_closed():
    auth = _client(lambda request: httpx.Response(200, content=b"not json"))
    result = await ItsmGateway(auth).detail_once(_cap("delegated"), "generated", _attempt())
    await auth.aclose()
    assert isinstance(result, GatewayFailure)
    assert result.safe_error.code == "upstream_unavailable"


@pytest.mark.anyio
async def test_gateway_public_contract_accepts_capability_and_raw_credential_is_confined():
    delegated = "delegated-secret-boundary-canary"
    refreshed = "refreshed-secret-boundary-canary"
    refresh_calls = []
    seen = []

    async def current():
        return DelegatedCredential(delegated, datetime(2026, 1, 1, tzinfo=timezone.utc))

    async def refresh():
        refresh_calls.append(True)
        return DelegatedCredential(refreshed, datetime(2099, 1, 1, tzinfo=timezone.utc))

    capability = TicketRequestCapability(7, "session", "conversation", current, refresh)
    auth = _client(lambda request: seen.append(request) or httpx.Response(200, json={
        "items": [], "page": {"limit": 20, "next_cursor": None, "has_more": False}
    }))
    result = await ItsmGateway(auth).list_once(
        capability, CanonicalTicketQueryV1(), None, _attempt()
    )
    await auth.aclose()
    assert isinstance(result, GatewaySuccess) and refresh_calls == []
    assert seen[0].headers["authorization"] == f"Bearer {delegated}"
    assert delegated not in repr(capability) and refreshed not in repr(capability)
    with pytest.raises(TypeError):
        pickle.dumps(capability)
    parameters = inspect.signature(ItsmGateway.list_once).parameters
    assert "capability" in parameters and "delegated_token" not in parameters


@pytest.mark.anyio
async def test_create_refresh_port_accepts_shared_c01_schema_and_typed_results(
    c01_refresh_contract,
):
    capability = c01_refresh_contract["capability"]
    reservation = c01_refresh_contract["reservation"]
    port = c01_refresh_contract["port"]
    assert reservation.refresh_reservation_id.startswith("r1.contract-current.")
    assert all(
        raw not in reservation.refresh_reservation_id
        for raw in ("session-7", "conversation-7", "contract-attempt")
    )
    schema_fields = fields(type(reservation))
    assert [item.name for item in schema_fields] == [
        "create_attempt_id",
        "refresh_reservation_id",
        "refresh_used_at",
        "base_credential_generation",
        "refresh_state",
        "committed_credential_generation",
        "refresh_committed_at",
    ]
    assert all(item.default is MISSING for item in schema_fields[:3])
    assert all(item.default is None for item in schema_fields[3:])
    assert list(inspect.signature(port.refresh_delegated_capability_once).parameters) == [
        "capability",
        "reservation",
        "remaining_deadline",
    ]
    assert get_origin(
        get_type_hints(port.refresh_delegated_capability_once)["return"]
    ) is Result

    refreshed = await port.refresh_delegated_capability_once(
        capability,
        reservation,
        1.0,
    )
    replay = await port.refresh_delegated_capability_once(
        capability,
        reservation,
        1.0,
    )

    assert refreshed.ok and refreshed.value is capability and refreshed.error is None
    assert not replay.ok and replay.value is None and isinstance(replay.error, SafeErrorV1)
    assert c01_refresh_contract["exchange_calls"] == ["exchange-and-atomic-session-commit"]
    assert capability.credential_generation == 1


@pytest.mark.anyio
@pytest.mark.parametrize(
    "mismatch",
    ["tamper", "actor", "attempt", "expired", "deadline", "signed-deadline"],
)
async def test_create_refresh_port_rejects_unverified_or_unusable_reservation_before_exchange(mismatch):
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    operation_deadline = now + timedelta(seconds=5)
    refresh_calls = []

    async def provider():
        refresh_calls.append(True)
        return DelegatedCredential("never")

    capability = TicketRequestCapability(7, "session", "conversation", provider, provider)
    verifier_now = now + (
        timedelta(minutes=16)
        if mismatch == "expired"
        else timedelta(seconds=6)
        if mismatch == "signed-deadline"
        else timedelta(0)
    )
    keys = {"kid-current": b"v" * 32}
    signer = CredentialRefreshReservationSigner(
        kid="kid-current", secret=keys["kid-current"], creator_boot_id="boot-current"
    )
    verifier_calls = []

    class CountingVerifier(CredentialRefreshReservationVerifier):
        def remaining_budget(self, capability, reservation):
            verifier_calls.append(reservation.create_attempt_id)
            return super().remaining_budget(capability, reservation)

    verifier = CountingVerifier(
        keys,
        current_kid="kid-current",
        current_boot_id="boot-current",
        utc_now=lambda: verifier_now,
    )
    reservation = signer.mint(
        capability,
        create_attempt_id="attempt-1",
        refresh_used_at=now,
        operation_deadline=operation_deadline,
        nonce="unique-nonce",
    )
    selected_capability = capability
    deadline = 1.0
    if mismatch == "tamper":
        reservation = replace(
            reservation,
            refresh_reservation_id=reservation.refresh_reservation_id[:-1] + "A",
        )
    elif mismatch == "actor":
        selected_capability = TicketRequestCapability(8, "session", "conversation", provider, provider)
    elif mismatch == "attempt":
        reservation = replace(reservation, create_attempt_id="attempt-2")
    elif mismatch == "deadline":
        deadline = 0.0
    result = await CredentialRefreshPortV1(verifier).refresh_delegated_capability_once(
        selected_capability,
        reservation,
        deadline,
    )
    assert not result.ok and isinstance(result.error, SafeErrorV1)
    assert verifier_calls == [reservation.create_attempt_id]
    assert refresh_calls == []
    assert capability.credential_generation == 0
    assert selected_capability.credential_generation == 0


@pytest.mark.anyio
async def test_durable_refresh_claim_blocks_restart_replay_before_u1_port_call():
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    operation_deadline = now + timedelta(seconds=5)
    exchange_calls = []
    port_calls = []
    durable_claims: set[str] = set()

    class DurableU2ReservationFake:
        def __init__(self, persisted_claims: set[str]):
            self._persisted_claims = persisted_claims

        def claim_once(self, create_attempt_id: str) -> bool:
            if create_attempt_id in self._persisted_claims:
                return False
            self._persisted_claims.add(create_attempt_id)
            return True

    async def current():
        return DelegatedCredential("old")

    async def refresh():
        exchange_calls.append("exchange-and-session-commit")
        return DelegatedCredential("new", generation=1)

    async def invoke_after_durable_claim(repository, capability, port, reservation):
        if not repository.claim_once(reservation.create_attempt_id):
            return None
        port_calls.append(reservation.create_attempt_id)
        return await port.refresh_delegated_capability_once(
            capability,
            reservation,
            1.0,
        )

    keys = {"kid-current": b"v" * 32}
    signer = CredentialRefreshReservationSigner(
        kid="kid-current", secret=keys["kid-current"], creator_boot_id="boot-current"
    )
    verifier = CredentialRefreshReservationVerifier(
        keys,
        current_kid="kid-current",
        current_boot_id="boot-current",
        utc_now=lambda: now,
    )
    first_capability = TicketRequestCapability(7, "session", "conversation", current, refresh)
    reservation = signer.mint(
        first_capability,
        create_attempt_id="attempt-durable",
        refresh_used_at=now,
        operation_deadline=operation_deadline,
        nonce="durable-nonce",
    )
    repository = DurableU2ReservationFake(durable_claims)
    first_result = await invoke_after_durable_claim(
        repository,
        first_capability,
        CredentialRefreshPortV1(verifier),
        reservation,
    )
    assert first_result.ok and first_result.value is first_capability

    # A second invocation in the same process is rejected by the durable U2
    # claim before the U1 port can observe the reservation.
    second_capability = TicketRequestCapability(7, "session", "conversation", current, refresh)
    assert await invoke_after_durable_claim(
        repository,
        second_capability,
        CredentialRefreshPortV1(verifier),
        reservation,
    ) is None

    # Process restart: fresh repository/port/capability objects reuse the durable claim store.
    restarted_capability = TicketRequestCapability(7, "session", "conversation", current, refresh)
    assert await invoke_after_durable_claim(
        DurableU2ReservationFake(durable_claims),
        restarted_capability,
        CredentialRefreshPortV1(verifier),
        reservation,
    ) is None
    assert port_calls == ["attempt-durable"]
    assert exchange_calls == ["exchange-and-session-commit"]
    assert second_capability.credential_generation == 0
    assert restarted_capability.credential_generation == 0


def test_refresh_envelope_rotation_overlap_verifies_restart_and_retirement_rejects():
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)

    async def provider():
        return DelegatedCredential("server-only", generation=1)

    original = TicketRequestCapability(7, "session", "conversation", provider, provider)
    signer = CredentialRefreshReservationSigner(
        kid="kid-old", secret=b"o" * 32, creator_boot_id="boot-old"
    )
    reservation = signer.mint(
        original,
        create_attempt_id="attempt-rotation",
        refresh_used_at=now,
        operation_deadline=now + timedelta(seconds=5),
        nonce="rotation-nonce",
    )
    restarted = TicketRequestCapability(
        7,
        "session",
        "conversation",
        provider,
        provider,
        _credential_generation=1,
    )
    overlapping = CredentialRefreshReservationVerifier(
        {"kid-new": b"n" * 32, "kid-old": b"o" * 32},
        current_kid="kid-new",
        current_boot_id="boot-new",
        utc_now=lambda: now + timedelta(seconds=30),
        max_age=timedelta(hours=24),
    )
    restored = overlapping.restore(
        restarted,
        create_attempt_id=reservation.create_attempt_id,
        refresh_reservation_id=reservation.refresh_reservation_id,
        refresh_used_at=reservation.refresh_used_at,
        base_credential_generation=reservation.base_credential_generation,
    )
    assert restored is not None

    retired = CredentialRefreshReservationVerifier(
        {"kid-new": b"n" * 32},
        current_kid="kid-new",
        current_boot_id="boot-new",
        utc_now=lambda: now + timedelta(seconds=30),
        max_age=timedelta(hours=24),
    )
    assert retired.restore(
        restarted,
        create_attempt_id=reservation.create_attempt_id,
        refresh_reservation_id=reservation.refresh_reservation_id,
        refresh_used_at=reservation.refresh_used_at,
        base_credential_generation=reservation.base_credential_generation,
    ) is None


@pytest.mark.anyio
async def test_session_expiry_during_credential_load_cleans_and_blocks_http():
    now = [datetime(2026, 1, 1, tzinfo=timezone.utc)]
    expiry = now[0] + timedelta(seconds=1)
    cleanup_calls = []
    http_calls = []

    async def current():
        now[0] = expiry
        return DelegatedCredential("must-never-reach-http")

    async def refresh():
        raise AssertionError("refresh must not run after session expiry")

    async def cleanup():
        cleanup_calls.append(True)

    capability = TicketRequestCapability(
        7,
        "session",
        "conversation",
        current,
        refresh,
        session_expires_at=expiry,
        _expiry_handler=cleanup,
        _utc_now=lambda: now[0],
    )
    auth = _client(lambda request: http_calls.append(request) or httpx.Response(200, json={}))
    result = await ItsmGateway(auth).detail_once(capability, "generated", _attempt())
    await auth.aclose()
    assert isinstance(result, GatewayFailure)
    assert result.safe_error.code == "authentication_required" and result.http_attempted is False
    assert cleanup_calls == [True] and http_calls == []


@pytest.mark.anyio
async def test_catalog_methods_are_one_attempt_and_return_typed_lists():
    seen = []

    def handler(request):
        seen.append(request)
        if request.url.path.endswith("services"):
            return httpx.Response(200, json=[{"id": "svc", "name": "Service", "status": "active", "components": []}])
        return httpx.Response(200, json=[{"id": "model", "name": "Model", "status": "active"}])

    auth = _client(handler)
    gateway = ItsmGateway(auth)
    services = await gateway.catalog_services_once(_cap(), _attempt())
    models = await gateway.catalog_models_once(_cap(), _attempt())
    await auth.aclose()
    assert isinstance(services, GatewayCatalogSuccess) and services.items[0]["id"] == "svc"
    assert isinstance(models, GatewayCatalogSuccess) and models.items[0]["id"] == "model"
    assert len(seen) == 2
    assert all(request.headers["authorization"] == "Bearer delegated-canary" for request in seen)


@pytest.mark.anyio
async def test_create_sends_exact_bytes_and_key_once_and_returns_created_evidence():
    body = b'{"title":"exact"}'
    captured = []

    def handler(request):
        captured.append(request)
        return httpx.Response(
            201,
            json={"id": "generated-ticket"},
            headers={"Idempotent-Replayed": "true", "X-Request-ID": "safe-123"},
        )

    auth = _client(handler)
    result = await ItsmGateway(auth).create_ticket_once(_cap(), body, "opaque-key", _attempt())
    await auth.aclose()
    assert isinstance(result, GatewayCreateSuccess)
    assert result.ticket_id == "generated-ticket" and result.idempotent_replayed
    assert len(captured) == 1
    assert captured[0].content == body
    assert captured[0].headers["idempotency-key"] == "opaque-key"
    assert captured[0].headers["authorization"] == "Bearer delegated-canary"


@pytest.mark.anyio
@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(200, json={"id": "wrong-status"}),
        httpx.Response(201, json={}),
        httpx.Response(201, content=b"not-json"),
    ],
)
async def test_create_malformed_success_never_becomes_success(response):
    auth = _client(lambda request: response)
    result = await ItsmGateway(auth).create_ticket_once(_cap(), b"{}", "opaque-key", _attempt())
    await auth.aclose()
    assert isinstance(result, GatewayFailure)
    assert result.safe_error.message_key == "ticket_contract_invalid"


@pytest.mark.anyio
async def test_create_401_token_expired_is_safe_authoritative_no_apply_signal():
    raw_canary = "raw-create-problem-secret"
    auth = _client(
        lambda request: httpx.Response(401, json={"code": "token_expired", "detail": raw_canary})
    )
    result = await ItsmGateway(auth).create_ticket_once(_cap(), b"{}", "opaque-key", _attempt())
    await auth.aclose()
    assert isinstance(result, GatewayFailure)
    assert result.upstream_code == "token_expired" and result.safe_error.retryable
    assert raw_canary not in repr(result)


@pytest.mark.anyio
async def test_create_transport_loss_is_typed_ambiguous_input_for_policy():
    calls = 0

    def handler(request):
        nonlocal calls
        calls += 1
        raise httpx.ReadError("raw response loss", request=request)

    auth = _client(handler)
    result = await ItsmGateway(auth).create_ticket_once(_cap(), b"{}", "opaque-key", _attempt())
    await auth.aclose()
    assert isinstance(result, GatewayFailure) and result.transient
    assert calls == 1 and "raw response loss" not in repr(result)
