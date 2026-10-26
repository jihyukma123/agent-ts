"""ITSM API v2 티켓 업무 경계.

이 모듈은 서버 세션이 제공하는 위임 토큰을 사용해 티켓 HTTP 계약만
다룹니다. 권한·행 가시성·카탈로그 판단은 ITSM API의 응답을 권위 있는
결과로 사용하며 로컬 역할 비교로 확대하지 않습니다.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any, Protocol
from urllib.parse import quote
from uuid import uuid4

from app.itsm_auth import ItsmApiError, ItsmAuthClient, ItsmResponse, ItsmUnavailableError

TokenProvider = Callable[[bool], Awaitable[str]]


class TicketGateway(Protocol):
    async def list_tickets(self, filters: Mapping[str, Any]) -> dict[str, Any]: ...

    async def read_ticket(self, ticket_id: str) -> dict[str, Any]: ...

    async def create_ticket(self, payload: Mapping[str, Any]) -> dict[str, Any]: ...

    async def update_ticket(self, ticket_id: str, expected_version: int, changes: Mapping[str, Any]) -> dict[str, Any]: ...


@dataclass(frozen=True)
class TicketGatewayError(Exception):
    error_code: str
    field: str | None
    reason: str
    retryable: bool
    hint: str

    def as_dict(self) -> dict[str, str | bool | None]:
        return {
            "error_code": self.error_code,
            "field": self.field,
            "reason": self.reason,
            "retryable": self.retryable,
            "hint": self.hint,
        }

    def __str__(self) -> str:
        return f"ITSM ticket request failed ({self.error_code})"


def _first_field(problem: Mapping[str, Any]) -> str | None:
    errors = problem.get("errors")
    if isinstance(errors, list) and errors and isinstance(errors[0], Mapping):
        field = errors[0].get("field")
        return str(field) if field else None
    return None


def _normalize_error(exc: ItsmApiError | ItsmUnavailableError) -> TicketGatewayError:
    if isinstance(exc, ItsmUnavailableError):
        timed_out = "시간 초과" in exc.reason
        return TicketGatewayError(
            error_code="itsm_timeout" if timed_out else "itsm_unavailable",
            field=None,
            reason="ITSM 요청 응답 시간을 초과했습니다." if timed_out else "ITSM 시스템에 연결할 수 없습니다.",
            retryable=True,
            hint="잠시 후 다시 시도하세요.",
        )

    field = _first_field(exc.problem)
    if exc.status == 401:
        return TicketGatewayError("itsm_auth_failed", field, "ITSM 사용자 인증에 실패했습니다.", False, "다시 로그인하세요.")
    if exc.status == 403:
        return TicketGatewayError("permission_denied", field, "현재 사용자에게 이 작업의 ITSM 권한이 없습니다.", False, "권한이 필요하면 서비스데스크에 문의하세요.")
    if exc.status == 404:
        return TicketGatewayError("ticket_not_found", field, "티켓을 찾을 수 없습니다.", False, "ID를 확인하세요. 가시성 제한으로도 같은 응답이 반환됩니다.")
    if exc.status == 412:
        return TicketGatewayError("version_conflict", field, "티켓이 조회 후 변경되었습니다.", True, "ticket_read로 다시 조회하고 변경 내용을 다시 확인하세요.")
    if exc.status == 428:
        return TicketGatewayError("precondition_required", field, "현재 티켓 버전이 필요합니다.", False, "ticket_read의 version을 expected_version으로 사용하세요.")
    if exc.status == 429:
        wait = exc.retry_after_seconds
        hint = f"{wait}초 후 다시 시도하세요." if wait is not None else "잠시 후 다시 시도하세요."
        return TicketGatewayError("rate_limited", field, "ITSM 요청 한도에 도달했습니다.", True, hint)
    if exc.status == 422:
        return TicketGatewayError(exc.code or "validation_failed", field, "ITSM이 입력값을 거부했습니다.", False, "입력값과 카탈로그 관계를 확인하세요.")
    if exc.status == 400:
        return TicketGatewayError(exc.code or "invalid_request", field, "ITSM 요청이 유효하지 않습니다.", False, "검색 조건과 커서를 확인하세요.")
    return TicketGatewayError("itsm_error", field, "ITSM 요청을 처리하지 못했습니다.", exc.status >= 500, "잠시 후 다시 시도하세요.")


class ItsmTicketGateway:
    """현재 세션의 위임 토큰에 결합된 제한 티켓 capability."""

    def __init__(self, client: ItsmAuthClient, token_provider: TokenProvider):
        self._client = client
        self._token_provider = token_provider
        self._token: str | None = None

    async def _request(self, method: str, path: str, **kwargs: Any) -> ItsmResponse:
        if self._token is None:
            self._token = await self._token_provider(False)
        for attempt in range(2):
            try:
                return await self._client.delegated_request(method, path, token=self._token, **kwargs)
            except ItsmApiError as exc:
                if attempt == 0 and exc.status == 401 and exc.code == "token_expired":
                    self._token = await self._token_provider(True)
                    continue
                raise _normalize_error(exc) from None
            except ItsmUnavailableError as exc:
                raise _normalize_error(exc) from None
        raise AssertionError("unreachable")

    async def list_tickets(self, filters: Mapping[str, Any]) -> dict[str, Any]:
        params: list[tuple[str, Any]] = []
        for key, value in filters.items():
            if value is None:
                continue
            if isinstance(value, list):
                params.extend((key, item) for item in value)
            else:
                params.append((key, value))
        response = await self._request("GET", "/api/v2/tickets", params=params)
        return dict(response.data)

    async def read_ticket(self, ticket_id: str) -> dict[str, Any]:
        response = await self._request("GET", f"/api/v2/tickets/{quote(ticket_id, safe='')}")
        ticket = dict(response.data)
        etag = response.etag or (f'"{ticket["version"]}"' if "version" in ticket else None)
        return {"ticket": ticket, "etag": etag}

    async def create_ticket(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        response = await self._request(
            "POST",
            "/api/v2/tickets",
            json=dict(payload),
            headers={"Idempotency-Key": uuid4().hex},
        )
        return {"ticket": dict(response.data), "idempotent_replayed": response.idempotent_replayed}

    async def update_ticket(self, ticket_id: str, expected_version: int, changes: Mapping[str, Any]) -> dict[str, Any]:
        response = await self._request(
            "PATCH",
            f"/api/v2/tickets/{quote(ticket_id, safe='')}",
            json=dict(changes),
            headers={"If-Match": f'"{expected_version}"'},
        )
        return {"ticket": dict(response.data), "etag": response.etag}
