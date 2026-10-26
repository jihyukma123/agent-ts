"""ITSM API(v2) HTTP 클라이언트.

ITSM 웹은 ITSM 데이터에 이 클라이언트로만 접근합니다(DB 직접 접근 없음).
- 서비스 토큰: 로그인 전 사용자 목록 조회 등 사용자와 무관한 호출에 사용. 클라이언트 안에 캐시합니다.
- 위임 토큰: 특정 사용자를 대신하는 호출. 호출하는 쪽이 토큰을 넘깁니다(ITSM 웹은 세션에 저장).
오류 응답(problem+json)은 ItsmApiError, 연결 실패·시간 초과는 ItsmUnavailableError 로 올립니다. 재시도는 하지 않습니다.
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
    headers: dict[str, str] = field(default_factory=dict)

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
    content_type = response.headers.get("content-type", "")
    if "json" in content_type:
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
                headers=dict(response.headers),
            )
    return ItsmApiError(
        status=response.status_code,
        code=f"http_{response.status_code}",
        detail=response.text[:300] or response.reason_phrase,
        headers=dict(response.headers),
    )


class ItsmClient:
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

    # ---------- 저수준 ----------

    async def request(
        self,
        method: str,
        path: str,
        *,
        token: str | None = None,
        params: Any = None,
        json: Any = None,
        headers: dict[str, str] | None = None,
    ) -> httpx.Response:
        all_headers = dict(headers or {})
        if token is not None:
            all_headers["Authorization"] = f"Bearer {token}"
        try:
            response = await self._http.request(method, path, params=params, json=json, headers=all_headers)
        except httpx.TimeoutException as exc:
            raise ItsmUnavailableError(f"시간 초과 ({type(exc).__name__})") from exc
        except httpx.TransportError as exc:
            raise ItsmUnavailableError(f"연결할 수 없음 ({type(exc).__name__})") from exc
        if response.is_error:
            raise _error_from(response)
        return response

    async def get_json(self, path: str, *, token: str, params: Any = None) -> Any:
        return (await self.request("GET", path, token=token, params=params)).json()

    # ---------- 토큰 ----------

    async def _issue(self, grant_type: str, subject: str | None = None) -> IssuedToken:
        body = {"grant_type": grant_type, "client_id": self._client_id, "client_secret": self._client_secret}
        if subject is not None:
            body["subject"] = subject
        data = (await self.request("POST", "/api/v2/auth/token", json=body)).json()
        return IssuedToken(access_token=data["access_token"], expires_at=_parse_time(data["expires_at"]))

    async def exchange_token(self, username: str) -> IssuedToken:
        """username 사용자를 대신하는 위임 토큰을 발급받습니다."""
        return await self._issue("token_exchange", username)

    async def service_token(self, *, renew: bool = False) -> str:
        async with self._service_lock:
            soon = datetime.now(timezone.utc) + timedelta(seconds=60)
            if renew or self._service_token is None or self._service_token.expires_at <= soon:
                self._service_token = await self._issue("client_credentials")
            return self._service_token.access_token

    async def _service_get(self, path: str, params: Any = None) -> Any:
        """서비스 토큰으로 GET. ITSM 쪽에서 토큰이 무효화된 경우(예: 데이터 초기화) 한 번 다시 발급받습니다."""
        try:
            return await self.get_json(path, token=await self.service_token(), params=params)
        except ItsmApiError as exc:
            if exc.status != 401:
                raise
            return await self.get_json(path, token=await self.service_token(renew=True), params=params)

    # ---------- 디렉터리·카탈로그 (서비스 토큰) ----------

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

    async def list_services(self, include_deprecated: bool = False) -> list[dict]:
        return await self._service_get("/api/v2/catalog/services", params={"include_deprecated": include_deprecated})

    async def list_models(self) -> list[dict]:
        return await self._service_get("/api/v2/catalog/models")

    async def health(self) -> dict:
        return (await self.request("GET", "/api/v2/health")).json()

    # ---------- 사용자 대신 호출 (위임 토큰) ----------

    async def me(self, token: str) -> dict:
        return await self.get_json("/api/v2/me", token=token)

    async def list_tickets(self, token: str, params: Any = None) -> dict:
        return await self.get_json("/api/v2/tickets", token=token, params=params)

    async def get_ticket(self, token: str, ticket_id: str) -> dict:
        return await self.get_json(f"/api/v2/tickets/{ticket_id}", token=token)

    async def list_worklogs(self, token: str, ticket_id: str, params: Any = None) -> dict:
        return await self.get_json(f"/api/v2/tickets/{ticket_id}/worklogs", token=token, params=params)

    async def create_ticket(self, token: str, body: dict, idempotency_key: str | None = None) -> dict:
        headers = {"Idempotency-Key": idempotency_key} if idempotency_key else None
        return (await self.request("POST", "/api/v2/tickets", token=token, json=body, headers=headers)).json()
