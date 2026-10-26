"""ITSM v2 티켓 HTTP 경계를 실제 네트워크 없이 검증한다."""

from __future__ import annotations

import json
from collections.abc import Callable

import httpx
import pytest

from app.itsm_auth import ItsmAuthClient
from app.itsm_tickets import ItsmTicketGateway, TicketGatewayError


def _problem(status: int, code: str, **extra) -> httpx.Response:
    return httpx.Response(
        status,
        json={"status": status, "code": code, "detail": "test problem", **extra},
        headers={"content-type": "application/problem+json", **({"Retry-After": "7"} if status == 429 else {})},
    )


def _gateway(handler: Callable[[httpx.Request], httpx.Response], tokens: list[bool] | None = None):
    calls = tokens if tokens is not None else []

    async def token_provider(force_refresh: bool) -> str:
        calls.append(force_refresh)
        return "test-refreshed-token" if force_refresh else "test-delegated-token"

    client = ItsmAuthClient("http://itsm.test", "test-client", "test-secret", transport=httpx.MockTransport(handler))
    return client, ItsmTicketGateway(client, token_provider)


@pytest.mark.anyio
async def test_list_tickets_sends_v2_filters_cursor_and_repeated_values():
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"items": [], "page": {"limit": 2, "next_cursor": "next", "has_more": True}})

    client, gateway = _gateway(handler)
    try:
        result = await gateway.list_tickets(
            {"q": "login", "status": ["received", "assigned"], "priority": ["P1"], "limit": 2, "cursor": "opaque"}
        )
    finally:
        await client.aclose()

    assert seen[0].url.path == "/api/v2/tickets"
    assert seen[0].url.params.get_list("status") == ["received", "assigned"]
    assert seen[0].url.params["cursor"] == "opaque"
    assert seen[0].headers["authorization"].startswith("Bearer test-")
    assert result["page"] == {"limit": 2, "next_cursor": "next", "has_more": True}


@pytest.mark.anyio
async def test_read_ticket_returns_detail_version_and_etag():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET" and request.url.path.startswith("/api/v2/tickets/")
        return httpx.Response(200, json={"id": request.url.path.rsplit("/", 1)[1], "version": 4}, headers={"ETag": '"4"'})

    client, gateway = _gateway(handler)
    try:
        result = await gateway.read_ticket("INC-321")
    finally:
        await client.aclose()
    assert result == {"ticket": {"id": "INC-321", "version": 4}, "etag": '"4"'}


@pytest.mark.anyio
async def test_create_ticket_generates_server_side_idempotency_key():
    headers: list[str] = []
    payload = {"title": "접속 장애", "description": "상세", "service_id": "service", "impact_level": "minor",
               "urgency": "low", "impact_note": "1명", "occurred_at": "2026-10-26T10:00:00+09:00"}  # fmt: skip

    def handler(request: httpx.Request) -> httpx.Response:
        headers.append(request.headers["idempotency-key"])
        assert json.loads(request.content) == payload
        return httpx.Response(201, json={"id": "INC-654", **payload, "version": 1})

    client, gateway = _gateway(handler)
    try:
        result = await gateway.create_ticket(payload)
    finally:
        await client.aclose()
    assert len(headers[0]) >= 16
    assert "idempotency" not in json.dumps(result).lower()
    assert result["ticket"]["id"] == "INC-654"


@pytest.mark.anyio
async def test_update_ticket_uses_if_match_and_only_requested_changes():
    changes = {"component_id": None, "impact_note": "팀 전체"}

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "PATCH"
        assert request.headers["if-match"] == '"5"'
        assert json.loads(request.content) == changes
        return httpx.Response(200, json={"id": "INC-777", "version": 6, **changes}, headers={"ETag": '"6"'})

    client, gateway = _gateway(handler)
    try:
        result = await gateway.update_ticket("INC-777", 5, changes)
    finally:
        await client.aclose()
    assert result["ticket"]["component_id"] is None and result["etag"] == '"6"'


@pytest.mark.anyio
async def test_expired_token_is_refreshed_once_and_original_request_retried_once():
    attempts = 0
    refreshes: list[bool] = []

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return _problem(401, "token_expired")
        assert request.headers["authorization"].endswith("refreshed-token")
        return httpx.Response(200, json={"items": [], "page": {"limit": 20, "next_cursor": None, "has_more": False}})

    client, gateway = _gateway(handler, refreshes)
    try:
        await gateway.list_tickets({"limit": 20})
    finally:
        await client.aclose()
    assert attempts == 2 and refreshes == [False, True]


@pytest.mark.anyio
@pytest.mark.parametrize(
    "status,code,expected,retryable",
    [
        (412, "version_conflict", "version_conflict", True),
        (428, "precondition_required", "precondition_required", False),
        (422, "unknown_model", "unknown_model", False),
        (429, "rate_limited", "rate_limited", True),
    ],
)
async def test_contract_errors_are_normalized_without_write_retry(status, code, expected, retryable):
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return _problem(status, code, errors=[{"field": "model", "message": "invalid"}])

    client, gateway = _gateway(handler)
    try:
        with pytest.raises(TicketGatewayError) as raised:
            await gateway.update_ticket("INC-888", 2, {"model": "retired"})
    finally:
        await client.aclose()
    error = raised.value.as_dict()
    assert calls == 1
    assert error["error_code"] == expected and error["retryable"] is retryable
    assert set(error) == {"error_code", "field", "reason", "retryable", "hint"}
    if status == 429:
        assert "7초" in error["hint"]


@pytest.mark.anyio
async def test_timeout_is_safe_retryable_error():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("test timeout", request=request)

    client, gateway = _gateway(handler)
    try:
        with pytest.raises(TicketGatewayError) as raised:
            await gateway.list_tickets({"limit": 20})
    finally:
        await client.aclose()
    assert raised.value.error_code == "itsm_timeout"
    assert raised.value.retryable is True
    assert "test-delegated-token" not in str(raised.value)


@pytest.mark.anyio
async def test_error_result_never_contains_authorization_or_remote_detail():
    remote_detail = "Bearer test-delegated-token should never escape"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            403,
            json={"status": 403, "code": "permission_denied", "detail": remote_detail},
            headers={"content-type": "application/problem+json"},
        )

    client, gateway = _gateway(handler)
    try:
        with pytest.raises(TicketGatewayError) as raised:
            await gateway.read_ticket("INC-999")
    finally:
        await client.aclose()
    public = json.dumps(raised.value.as_dict(), ensure_ascii=False)
    assert remote_detail not in public
    assert "authorization" not in public.lower() and "test-delegated-token" not in public
