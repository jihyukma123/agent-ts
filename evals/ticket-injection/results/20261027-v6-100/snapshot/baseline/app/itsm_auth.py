"""ITSM API 인증·디렉터리용 최소 HTTP 클라이언트.

에이전트 서비스가 "누가 대화하고 있는지"를 확인하는 데 필요한 호출만 담고 있습니다.
- 서비스 토큰(client_credentials): 사용자 목록 조회
- 위임 토큰(token_exchange): 선택한 사용자를 대신하는 토큰 발급, /api/v2/me 로 역할·권한 확인
티켓 등 업무 API 호출은 아직 구현되어 있지 않습니다. ITSM 연동 문서는 docs/itsm/ 을 보세요.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx


class ItsmError(Exception):
    pass


@dataclass
class ItsmApiError(ItsmError):
    status: int
    code: str
    detail: str
    problem: dict[str, Any] = field(default_factory=dict)

    def __str__(self) -> str:
        return f"ITSM API {self.status} {self.code}: {self.detail}"


@dataclass
class ItsmUnavailableError(ItsmError):
    reason: str

    def __str__(self) -> str:
        return f"ITSM API 연결 실패: {self.reason}"


@dataclass(frozen=True)
class IssuedToken:
    access_token: str = field(repr=False)
    expires_at: datetime


def _parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _error_from(response: httpx.Response) -> ItsmApiError:
    if "json" in response.headers.get("content-type", ""):
        try:
            body = response.json()
        except ValueError:
            body = None
        if isinstance(body, dict):
            return ItsmApiError(
                status=response.status_code,
                code=str(body.get("code") or f"http_{response.status_code}"),
                detail=str(body.get("detail") or body.get("title") or response.reason_phrase),
                problem=body,
            )
    return ItsmApiError(response.status_code, f"http_{response.status_code}", response.text[:300] or response.reason_phrase)


class ItsmAuthClient:
    def __init__(
        self,
        base_url: str,
        client_id: str,
        client_secret: str,
        *,
        timeout: float = 10.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        self.base_url = base_url
        self._client_id = client_id
        self._client_secret = client_secret
        self._http = httpx.AsyncClient(base_url=base_url, timeout=timeout, transport=transport)
        self._service_token: IssuedToken | None = None
        self._service_lock = asyncio.Lock()

    async def aclose(self) -> None:
        await self._http.aclose()

    async def ticket_request_once(
        self,
        method: str,
        path: str,
        *,
        delegated_token: str,
        params: Any = None,
        content: bytes | None = None,
        headers: dict[str, str] | None = None,
        timeout: float | None = None,
    ) -> httpx.Response:
        """사용자 위임 티켓 요청을 정확히 한 번 수행합니다.

        재시도·토큰 갱신·오류 문구 변환은 이 경계 바깥 TicketOperations가
        소유합니다. 원본 응답은 ticket gateway adapter만 소비합니다.
        """
        supplied = headers or {}
        allowed = {"Idempotency-Key", "Content-Type", "If-Match"}
        if any(name not in allowed for name in supplied):
            raise ValueError("ticket request contains a non-allowlisted header")
        request_headers = {"Authorization": f"Bearer {delegated_token}", **supplied}
        try:
            return await self._http.request(
                method,
                path,
                headers=request_headers,
                params=params,
                content=content,
                timeout=timeout,
            )
        except httpx.TimeoutException as exc:
            raise ItsmUnavailableError("timeout") from exc
        except httpx.TransportError as exc:
            raise ItsmUnavailableError("transport") from exc

    async def _request(self, method: str, path: str, *, token: str | None = None, **kwargs: Any) -> Any:
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        try:
            response = await self._http.request(method, path, headers=headers, **kwargs)
        except httpx.TimeoutException as exc:
            raise ItsmUnavailableError(f"시간 초과 ({type(exc).__name__})") from exc
        except httpx.TransportError as exc:
            raise ItsmUnavailableError(f"연결할 수 없음 ({type(exc).__name__})") from exc
        if response.is_error:
            raise _error_from(response)
        return response.json()

    async def _issue(self, grant_type: str, subject: str | None = None) -> IssuedToken:
        body = {"grant_type": grant_type, "client_id": self._client_id, "client_secret": self._client_secret}
        if subject is not None:
            body["subject"] = subject
        data = await self._request("POST", "/api/v2/auth/token", json=body)
        return IssuedToken(access_token=data["access_token"], expires_at=_parse_time(data["expires_at"]))

    async def exchange_token(self, username: str) -> IssuedToken:
        """username 사용자를 대신하는 위임 토큰을 발급받습니다."""
        return await self._issue("token_exchange", username)

    async def _service_get(self, path: str, params: Any = None) -> Any:
        """서비스 토큰으로 GET. ITSM 쪽에서 토큰이 무효화된 경우 한 번 다시 발급받습니다."""
        for attempt in range(2):
            async with self._service_lock:
                soon = datetime.now(timezone.utc) + timedelta(seconds=60)
                if attempt or self._service_token is None or self._service_token.expires_at <= soon:
                    self._service_token = await self._issue("client_credentials")
                token = self._service_token.access_token
            try:
                return await self._request("GET", path, token=token, params=params)
            except ItsmApiError as exc:
                if exc.status != 401 or attempt:
                    raise
        raise AssertionError("unreachable")

    async def list_users(self) -> list[dict]:
        items: list[dict] = []
        params: dict[str, Any] = {"limit": 100}
        while True:
            page = await self._service_get("/api/v2/users", params=dict(params))
            items.extend(page["items"])
            if not page["page"]["has_more"]:
                return items
            params["cursor"] = page["page"]["next_cursor"]

    async def get_user(self, user_id: int) -> dict:
        return await self._service_get(f"/api/v2/users/{user_id}")

    async def me(self, token: str) -> dict:
        return await self._request("GET", "/api/v2/me", token=token)

    async def health(self) -> dict:
        return await self._request("GET", "/api/v2/health")
