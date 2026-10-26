"""ITSM API v2 인증·디렉터리·위임 요청용 HTTP 클라이언트.

에이전트 서비스가 "누가 대화하고 있는지"를 확인하는 데 필요한 호출만 담고 있습니다.
- 서비스 토큰(client_credentials): 사용자 목록 조회
- 위임 토큰(token_exchange): 선택한 사용자를 대신하는 토큰 발급, /api/v2/me 로 역할·권한 확인
업무 API는 이 클라이언트의 제한된 ``delegated_request`` 경계를 통해서만
위임 토큰으로 호출합니다. 인증 헤더는 반환값이나 오류에 포함하지 않습니다.
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

_BEARER_PATTERN = re.compile(r"(?i)Bearer\s+[^\s,;]+")
_OPENAI_KEY_PATTERN = re.compile(r"sk-[A-Za-z0-9_\-*]{4,}")


class ItsmError(Exception):
    pass


@dataclass
class ItsmApiError(ItsmError):
    status: int
    code: str
    detail: str
    problem: dict[str, Any] = field(default_factory=dict)
    retry_after_seconds: int | None = None

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


@dataclass(frozen=True)
class ItsmResponse:
    """업무 API에 필요한 응답 데이터와 비밀이 아닌 메타데이터."""

    status: int
    data: Any
    etag: str | None = None
    retry_after_seconds: int | None = None
    idempotent_replayed: bool = False


def _parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _error_from(response: httpx.Response) -> ItsmApiError:
    retry_after = response.headers.get("retry-after")
    retry_after_seconds = int(retry_after) if retry_after and retry_after.isdigit() else None
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
                retry_after_seconds=retry_after_seconds,
            )
    return ItsmApiError(
        response.status_code,
        f"http_{response.status_code}",
        response.reason_phrase,
        retry_after_seconds=retry_after_seconds,
    )


def _redact_error(error: ItsmApiError, *secrets: str | None) -> ItsmApiError:
    def redact_text(value: str) -> str:
        redacted = _BEARER_PATTERN.sub("Bearer ***", _OPENAI_KEY_PATTERN.sub("sk-***", value))
        for secret in secrets:
            if secret:
                redacted = redacted.replace(secret, "***")
        return redacted

    def redact_value(value: Any) -> Any:
        if isinstance(value, str):
            return redact_text(value)
        if isinstance(value, list):
            return [redact_value(item) for item in value]
        if isinstance(value, dict):
            return {key: redact_value(item) for key, item in value.items()}
        return value

    return ItsmApiError(
        status=error.status,
        code=redact_text(error.code),
        detail=redact_text(error.detail),
        problem=redact_value(error.problem),
        retry_after_seconds=error.retry_after_seconds,
    )


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

    async def _request_response(self, method: str, path: str, *, token: str | None = None, **kwargs: Any) -> httpx.Response:
        headers = dict(kwargs.pop("headers", {}))
        if token:
            headers["Authorization"] = f"Bearer {token}"
        try:
            response = await self._http.request(method, path, headers=headers, **kwargs)
        except httpx.TimeoutException as exc:
            raise ItsmUnavailableError(f"시간 초과 ({type(exc).__name__})") from exc
        except httpx.TransportError as exc:
            raise ItsmUnavailableError(f"연결할 수 없음 ({type(exc).__name__})") from exc
        if response.is_error:
            raise _redact_error(_error_from(response), token, self._client_secret)
        return response

    async def _request(self, method: str, path: str, *, token: str | None = None, **kwargs: Any) -> Any:
        response = await self._request_response(method, path, token=token, **kwargs)
        return response.json()

    async def delegated_request(self, method: str, path: str, *, token: str, **kwargs: Any) -> ItsmResponse:
        """사용자 위임 토큰으로 v2 업무 API를 호출합니다.

        토큰은 요청 Authorization 헤더에만 사용되며 반환 객체에는 저장되지 않습니다.
        """
        response = await self._request_response(method, path, token=token, **kwargs)
        retry_after = response.headers.get("retry-after")
        return ItsmResponse(
            status=response.status_code,
            data=response.json(),
            etag=response.headers.get("etag"),
            retry_after_seconds=int(retry_after) if retry_after and retry_after.isdigit() else None,
            idempotent_replayed=response.headers.get("idempotent-replayed", "").lower() == "true",
        )

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
