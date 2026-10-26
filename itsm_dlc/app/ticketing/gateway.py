"""ITSM API v2 티켓 조회의 단일-attempt typed adapter."""

from __future__ import annotations

import asyncio
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal
from urllib.parse import quote

from app.itsm_auth import ItsmAuthClient, ItsmUnavailableError
from app.ticketing.capability import CapabilityAuthenticationError, TicketRequestCapability
from app.ticketing.models import CanonicalTicketQueryV1, SafeErrorV1

_SAFE_REQUEST_ID = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")


@dataclass(frozen=True)
class GatewaySuccess:
    data: dict[str, Any]
    etag: str | None = None
    http_attempted: bool = True


@dataclass(frozen=True)
class GatewayFailure:
    status: int | None
    upstream_code: str | None
    safe_error: SafeErrorV1
    transient: bool = False
    http_attempted: bool = True
    authoritative_no_apply: bool = False


GatewayResult = GatewaySuccess | GatewayFailure


@dataclass(frozen=True)
class GatewayCatalogSuccess:
    items: list[dict[str, Any]]


@dataclass(frozen=True)
class GatewayCreateSuccess:
    ticket_id: str
    idempotent_replayed: bool
    request_id: str | None = None


@dataclass(frozen=True)
class GatewayUpdateSuccess:
    ticket: dict[str, Any]
    new_version: int
    request_id: str | None = None


CatalogGatewayResult = GatewayCatalogSuccess | GatewayFailure
CreateGatewayResult = GatewayCreateSuccess | GatewayFailure
UpdateGatewayResult = GatewayUpdateSuccess | GatewayFailure


@dataclass(frozen=True)
class GatewayAttemptDeadline:
    deadline: float
    timeout_limit: float
    monotonic: Callable[[], float] = time.monotonic

    def remaining(self) -> float:
        return max(0.0, self.deadline - self.monotonic())

    def outbound_timeout(self) -> float:
        return min(self.timeout_limit, self.remaining())


def _request_id(response) -> str | None:
    value = response.headers.get("x-request-id")
    if value and _SAFE_REQUEST_ID.fullmatch(value):
        return value
    try:
        body = response.json()
    except ValueError:
        return None
    candidate = body.get("request_id") if isinstance(body, dict) else None
    return candidate if isinstance(candidate, str) and _SAFE_REQUEST_ID.fullmatch(candidate) else None


def _retry_after(response) -> int | None:
    value = response.headers.get("retry-after")
    if value is None:
        return None
    try:
        seconds = int(value)
    except ValueError:
        return None
    return seconds if seconds >= 0 else None


def _safe_failure(response) -> GatewayFailure:
    try:
        body = response.json()
    except ValueError:
        body = {}
    upstream_code = body.get("code") if isinstance(body, dict) and isinstance(body.get("code"), str) else None
    status = response.status_code
    request_id = _request_id(response)
    if status == 401:
        error = SafeErrorV1(code="authentication_required", message_key="authentication_required", retryable=upstream_code == "token_expired", request_id=request_id)
    elif status == 403:
        error = SafeErrorV1(code="permission_denied", message_key="ticket_permission_denied", retryable=False, request_id=request_id)
    elif status == 404:
        error = SafeErrorV1(code="not_found", message_key="ticket_not_found_or_not_visible", retryable=False, request_id=request_id)
    elif status in (409, 412, 428):
        error = SafeErrorV1(code="conflict", message_key="ticket_conflict", retryable=False, request_id=request_id)
    elif status == 429:
        error = SafeErrorV1(
            code="rate_limited",
            message_key="ticket_rate_limited",
            retryable=True,
            request_id=request_id,
            retry_after_seconds=_retry_after(response),
        )
    elif status in (400, 422):
        error = SafeErrorV1(code="invalid_input", message_key="ticket_input_invalid", retryable=False, request_id=request_id)
    else:
        error = SafeErrorV1(code="upstream_unavailable", message_key="ticket_service_unavailable", retryable=status in (500, 502, 503, 504), request_id=request_id)
    return GatewayFailure(
        status=status,
        upstream_code=upstream_code,
        safe_error=error,
        transient=status in (500, 502, 503, 504),
        authoritative_no_apply=status == 401 and upstream_code == "token_expired",
    )


class ItsmGateway:
    def __init__(self, client: ItsmAuthClient):
        self.client = client

    async def list_once(
        self,
        capability: TicketRequestCapability,
        query: CanonicalTicketQueryV1,
        upstream_cursor: str | None,
        attempt: GatewayAttemptDeadline,
    ) -> GatewayResult:
        params: list[tuple[str, str]] = []
        for name, value in query.model_dump(exclude_none=True).items():
            if isinstance(value, list):
                params.extend((name, str(item.value if hasattr(item, "value") else item)) for item in value)
            elif isinstance(value, bool):
                params.append((name, str(value).lower()))
            elif hasattr(value, "isoformat"):
                params.append((name, value.isoformat().replace("+00:00", "Z")))
            else:
                params.append((name, str(value.value if hasattr(value, "value") else value)))
        if upstream_cursor is not None:
            params.append(("cursor", upstream_cursor))
        return await self._once("/api/v2/tickets", capability, attempt, params=params)

    async def detail_once(
        self, capability: TicketRequestCapability, ticket_id: str, attempt: GatewayAttemptDeadline
    ) -> GatewayResult:
        return await self._once(f"/api/v2/tickets/{quote(ticket_id, safe='')}", capability, attempt)

    async def catalog_services_once(
        self, capability: TicketRequestCapability, attempt: GatewayAttemptDeadline
    ) -> CatalogGatewayResult:
        return await self._catalog_once(
            "/api/v2/catalog/services", capability, attempt, params={"include_deprecated": "false"}
        )

    async def catalog_models_once(
        self, capability: TicketRequestCapability, attempt: GatewayAttemptDeadline
    ) -> CatalogGatewayResult:
        return await self._catalog_once(
            "/api/v2/catalog/models", capability, attempt, params={"include_retired": "false"}
        )

    async def create_ticket_once(
        self,
        capability: TicketRequestCapability,
        canonical_request_bytes: bytes,
        idempotency_key: str,
        attempt: GatewayAttemptDeadline,
    ) -> CreateGatewayResult:
        if not canonical_request_bytes or not idempotency_key:
            raise ValueError("create bytes and idempotency key are required")
        response = await self._response_once(
            "POST",
            "/api/v2/tickets",
            capability,
            attempt,
            content=canonical_request_bytes,
            headers={"Content-Type": "application/json", "Idempotency-Key": idempotency_key},
        )
        if isinstance(response, GatewayFailure):
            return response
        raw = response
        if raw.status_code != 201:
            return GatewayFailure(
                status=raw.status_code,
                upstream_code=None,
                safe_error=SafeErrorV1(
                    code="upstream_unavailable", message_key="ticket_contract_invalid", retryable=False,
                    request_id=_request_id(raw),
                ),
            )
        try:
            data = raw.json()
        except ValueError:
            data = None
        ticket_id = data.get("id") if isinstance(data, dict) else None
        if not isinstance(ticket_id, str) or not ticket_id:
            return GatewayFailure(
                status=raw.status_code,
                upstream_code=None,
                safe_error=SafeErrorV1(
                    code="upstream_unavailable", message_key="ticket_contract_invalid", retryable=False,
                    request_id=_request_id(raw),
                ),
            )
        replayed = raw.headers.get("idempotent-replayed", "false").casefold() == "true"
        return GatewayCreateSuccess(ticket_id=ticket_id, idempotent_replayed=replayed, request_id=_request_id(raw))

    async def update_ticket_once(
        self,
        capability: TicketRequestCapability,
        ticket_id: str,
        canonical_patch_bytes: bytes,
        if_match_version: str,
        attempt: GatewayAttemptDeadline,
    ) -> UpdateGatewayResult:
        """Send exactly one PATCH with byte-exact body and validated If-Match."""
        if not ticket_id or not canonical_patch_bytes:
            raise ValueError("ticket and canonical patch bytes are required")
        if not re.fullmatch(r'"[1-9][0-9]*"', if_match_version):
            raise ValueError("If-Match must be a strong quoted decimal version")
        response = await self._response_once(
            "PATCH",
            f"/api/v2/tickets/{quote(ticket_id, safe='')}",
            capability,
            attempt,
            content=canonical_patch_bytes,
            headers={"Content-Type": "application/json", "If-Match": if_match_version},
        )
        if isinstance(response, GatewayFailure):
            return response
        if response.status_code != 200:
            return GatewayFailure(
                status=response.status_code,
                upstream_code=None,
                safe_error=SafeErrorV1(
                    code="upstream_unavailable",
                    message_key="ticket_contract_invalid",
                    retryable=False,
                    request_id=_request_id(response),
                ),
            )
        try:
            data = response.json()
        except ValueError:
            data = None
        version = data.get("version") if isinstance(data, dict) else None
        if not isinstance(data, dict) or not isinstance(version, int) or isinstance(version, bool) or version < 1:
            return GatewayFailure(
                status=response.status_code,
                upstream_code=None,
                safe_error=SafeErrorV1(
                    code="upstream_unavailable",
                    message_key="ticket_contract_invalid",
                    retryable=False,
                    request_id=_request_id(response),
                ),
            )
        return GatewayUpdateSuccess(ticket=data, new_version=version, request_id=_request_id(response))

    async def _catalog_once(
        self,
        path: str,
        capability: TicketRequestCapability,
        attempt: GatewayAttemptDeadline,
        *,
        params: Any,
    ) -> CatalogGatewayResult:
        response = await self._response_once("GET", path, capability, attempt, params=params)
        if isinstance(response, GatewayFailure):
            return response
        try:
            data = response.json()
        except ValueError:
            data = None
        if not isinstance(data, list) or any(not isinstance(item, dict) for item in data):
            return GatewayFailure(
                status=response.status_code,
                upstream_code=None,
                safe_error=SafeErrorV1(
                    code="upstream_unavailable", message_key="ticket_contract_invalid", retryable=False,
                    request_id=_request_id(response),
                ),
            )
        return GatewayCatalogSuccess(items=data)

    async def _once(
        self,
        path: str,
        capability: TicketRequestCapability,
        attempt: GatewayAttemptDeadline,
        params: Any = None,
    ) -> GatewayResult:
        response = await self._response_once("GET", path, capability, attempt, params=params)
        if isinstance(response, GatewayFailure):
            return response
        if response.is_error:
            return _safe_failure(response)
        try:
            data = response.json()
        except ValueError:
            data = None
        if not isinstance(data, dict):
            return GatewayFailure(
                status=response.status_code,
                upstream_code=None,
                safe_error=SafeErrorV1(code="upstream_unavailable", message_key="ticket_contract_invalid", retryable=False, request_id=_request_id(response)),
            )
        return GatewaySuccess(data=data, etag=response.headers.get("etag"))

    async def _response_once(
        self,
        method: str,
        path: str,
        capability: TicketRequestCapability,
        attempt: GatewayAttemptDeadline,
        *,
        params: Any = None,
        content: bytes | None = None,
        headers: dict[str, str] | None = None,
    ):
        remaining = attempt.remaining()
        if remaining <= 0:
            return self._deadline_failure()
        try:
            delegated_token = await capability._delegated_credential_for_gateway(
                deadline=attempt.deadline, monotonic=attempt.monotonic
            )
        except CapabilityAuthenticationError:
            return GatewayFailure(
                status=None,
                upstream_code=None,
                safe_error=SafeErrorV1(
                    code="authentication_required", message_key="sign_in_required", retryable=False
                ),
                http_attempted=False,
            )
        except (TimeoutError, asyncio.TimeoutError):
            return self._deadline_failure()
        except Exception:
            return GatewayFailure(
                status=None,
                upstream_code=None,
                safe_error=SafeErrorV1(
                    code="authentication_required", message_key="sign_in_required", retryable=False
                ),
                http_attempted=False,
            )
        timeout = attempt.outbound_timeout()
        if timeout <= 0:
            return self._deadline_failure()
        try:
            request_options: dict[str, Any] = {"timeout": timeout}
            if params is not None:
                request_options["params"] = params
            if content is not None:
                request_options["content"] = content
            if headers is not None:
                request_options["headers"] = headers
            response = await self.client.ticket_request_once(
                method,
                path,
                delegated_token=delegated_token,
                **request_options,
            )
        except ItsmUnavailableError:
            return GatewayFailure(
                status=None,
                upstream_code=None,
                safe_error=SafeErrorV1(code="upstream_unavailable", message_key="ticket_service_unavailable", retryable=True),
                transient=True,
            )
        if response.is_error:
            return _safe_failure(response)
        return response

    @staticmethod
    def _deadline_failure() -> GatewayFailure:
        return GatewayFailure(
            status=None,
            upstream_code=None,
            safe_error=SafeErrorV1(
                code="upstream_unavailable", message_key="ticket_service_unavailable", retryable=False
            ),
            transient=False,
            http_attempted=False,
        )
