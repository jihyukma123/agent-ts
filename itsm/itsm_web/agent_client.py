"""ITSM Assistant HTTP client used by the web BFF.

The browser never talks to the separate Assistant service directly. This
client forwards only the Assistant's opaque session cookie and keeps the
current ITSM web user authoritative when establishing an Assistant session.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx

AGENT_SESSION_COOKIE = "itsm_agent_session"


class AgentUnavailableError(Exception):
    """The Assistant service could not be reached."""


@dataclass(frozen=True)
class AgentResponse:
    status_code: int
    data: Any
    session_cookie: str | None = None


class AgentClient:
    def __init__(
        self,
        base_url: str,
        *,
        timeout: float = 120.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self._client = httpx.AsyncClient(
            base_url=self.base_url,
            timeout=timeout,
            transport=transport,
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def request(
        self,
        method: str,
        path: str,
        *,
        session_cookie: str | None = None,
        json: dict[str, Any] | None = None,
    ) -> AgentResponse:
        # Never reuse the shared HTTP client's cookie jar for another browser.
        headers = {"Cookie": f"{AGENT_SESSION_COOKIE}={session_cookie}" if session_cookie else ""}
        try:
            response = await self._client.request(method, path, headers=headers, json=json)
        except httpx.RequestError as exc:
            raise AgentUnavailableError(type(exc).__name__) from None

        try:
            data: Any = response.json() if response.content else None
        except ValueError:
            data = None
        return AgentResponse(
            status_code=response.status_code,
            data=data,
            session_cookie=response.cookies.get(AGENT_SESSION_COOKIE),
        )

    async def health(self) -> AgentResponse:
        return await self.request("GET", "/api/health")

    async def ensure_session(self, user_id: int, session_cookie: str | None) -> AgentResponse:
        """Reuse a same-user Agent session, otherwise switch using server-known identity."""
        current = await self.request("GET", "/api/session", session_cookie=session_cookie)
        if (
            current.status_code == 200
            and isinstance(current.data, dict)
            and isinstance(current.data.get("user"), dict)
            and current.data["user"].get("id") == user_id
        ):
            return current
        if current.status_code not in (200, 401):
            return current
        return await self.request(
            "POST",
            "/api/session",
            session_cookie=session_cookie,
            json={"user_id": user_id},
        )
