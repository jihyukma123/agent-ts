"""연동 클라이언트 인증, 사용자 위임 토큰, 권한·요청 한도 검사.

- 클라이언트(ITSM 웹, 연동 시스템 등)는 client_id/client_secret 으로 토큰을 발급받습니다.
  - grant_type=client_credentials: 서비스 토큰(사용자 없음). 카탈로그·디렉터리 조회만 가능합니다.
  - grant_type=token_exchange: subject(username) 사용자를 대신하는 위임 토큰. 그 사용자의 역할 권한을 그대로 가집니다.
- 모든 보호된 API 는 `Authorization: Bearer <token>` 으로 호출합니다. 토큰 원문은 저장하지 않습니다.
"""

from __future__ import annotations

import math
import secrets
import sqlite3
import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import timedelta

from fastapi import Depends, Request

from itsm_api.db import hash_secret, open_db
from itsm_api.errors import ApiError
from itsm_api.timeutil import parse_iso, to_utc_iso

SERVICE_TOKEN_PERMISSIONS = frozenset({"catalog:read", "directory:read"})


@dataclass(frozen=True)
class Principal:
    """요청 주체. 위임 토큰이면 user_id 가 있고, 서비스 토큰이면 None 입니다."""

    client_id: str
    user_id: int | None
    username: str | None
    role: str | None
    team_id: int | None
    permissions: frozenset[str]

    def has(self, code: str) -> bool:
        return code in self.permissions


def get_db(request: Request) -> Iterator[sqlite3.Connection]:
    with open_db(request.app.state.settings.db_path) as conn:
        yield conn


def _unauthorized(code: str, detail: str) -> ApiError:
    return ApiError(401, code, detail, headers={"WWW-Authenticate": f'Bearer error="{code}"'})


def authenticate_client(conn: sqlite3.Connection, client_id: str, client_secret: str) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM api_clients WHERE client_id = ?", (client_id,)).fetchone()
    if row is None or not secrets.compare_digest(row["secret_hash"], hash_secret(client_secret)):
        raise _unauthorized("invalid_client", "클라이언트 인증에 실패했습니다.")
    return row


def issue_token(conn: sqlite3.Connection, client_id: str, user_id: int | None, ttl_seconds: int, now) -> tuple[str, str]:
    token = secrets.token_urlsafe(32)
    expires_at = to_utc_iso(now + timedelta(seconds=ttl_seconds))
    with conn:
        conn.execute(
            "INSERT INTO access_tokens (token_hash, client_id, user_id, issued_at, expires_at) VALUES (?, ?, ?, ?, ?)",
            (hash_secret(token), client_id, user_id, to_utc_iso(now), expires_at),
        )
        conn.execute("DELETE FROM access_tokens WHERE expires_at < ?", (to_utc_iso(now - timedelta(days=1)),))
    return token, expires_at


def user_permissions(conn: sqlite3.Connection, role_id: int) -> frozenset[str]:
    return frozenset(
        r["permission_code"]
        for r in conn.execute("SELECT permission_code FROM role_permissions WHERE role_id = ?", (role_id,))
    )


def load_principal(conn: sqlite3.Connection, token: str, now) -> Principal:
    row = conn.execute(
        """
        SELECT t.client_id, t.expires_at, u.id AS user_id, u.username, u.team_id, u.active, u.role_id, r.name AS role
        FROM access_tokens t
        LEFT JOIN users u ON u.id = t.user_id
        LEFT JOIN roles r ON r.id = u.role_id
        WHERE t.token_hash = ?
        """,
        (hash_secret(token),),
    ).fetchone()
    if row is None:
        raise _unauthorized("invalid_token", "유효하지 않은 액세스 토큰입니다.")
    if parse_iso(row["expires_at"]) <= now:
        raise _unauthorized("token_expired", "액세스 토큰이 만료되었습니다. 토큰을 다시 발급받으세요.")
    if row["user_id"] is None:
        return Principal(row["client_id"], None, None, None, None, SERVICE_TOKEN_PERMISSIONS)
    if not row["active"]:
        raise _unauthorized("user_inactive", "비활성화된 사용자의 토큰입니다.")
    return Principal(
        client_id=row["client_id"],
        user_id=row["user_id"],
        username=row["username"],
        role=row["role"],
        team_id=row["team_id"],
        permissions=user_permissions(conn, row["role_id"]),
    )


class RateLimiter:
    """토큰 버킷. 주체(클라이언트+사용자)별로 분당 한도만큼 요청을 허용합니다."""

    def __init__(self, per_minute: int):
        self.capacity = float(per_minute)
        self.rate = per_minute / 60.0
        self._buckets: dict[tuple[str, int | None], tuple[float, float]] = {}
        self._lock = threading.Lock()

    def check(self, key: tuple[str, int | None]) -> tuple[bool, int, int]:
        """(허용 여부, 남은 요청 수, 재시도까지 초)."""
        now = time.monotonic()
        with self._lock:
            tokens, updated = self._buckets.get(key, (self.capacity, now))
            tokens = min(self.capacity, tokens + (now - updated) * self.rate)
            if tokens >= 1:
                self._buckets[key] = (tokens - 1, now)
                return True, int(tokens - 1), 0
            self._buckets[key] = (tokens, now)
            return False, 0, max(1, math.ceil((1 - tokens) / self.rate))


def get_principal(request: Request, conn: sqlite3.Connection = Depends(get_db)) -> Principal:
    header = request.headers.get("authorization", "")
    scheme, _, token = header.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        raise _unauthorized("missing_token", "Authorization: Bearer <token> 헤더가 필요합니다.")
    principal = load_principal(conn, token.strip(), request.app.state.clock.now())

    limiter: RateLimiter = request.app.state.rate_limiter
    allowed, remaining, retry_after = limiter.check((principal.client_id, principal.user_id))
    request.state.rate_limit_remaining = remaining
    if not allowed:
        raise ApiError(
            429,
            "rate_limited",
            f"요청 한도(분당 {int(limiter.capacity)}회)를 초과했습니다. {retry_after}초 후 다시 시도하세요.",
            headers={"Retry-After": str(retry_after)},
            retry_after_seconds=retry_after,
        )
    request.state.principal = principal
    return principal


def require(*codes: str) -> Callable[[Principal], Principal]:
    def dependency(principal: Principal = Depends(get_principal)) -> Principal:
        missing = [code for code in codes if not principal.has(code)]
        if missing:
            raise ApiError(
                403,
                "permission_denied",
                f"현재 주체({principal.role or 'service'})에는 {', '.join(missing)} 권한이 없습니다.",
                required_permissions=missing,
            )
        return principal

    return dependency


def require_user(*codes: str) -> Callable[[Principal], Principal]:
    """사용자 위임 토큰이 필요한 엔드포인트용."""

    def dependency(principal: Principal = Depends(require(*codes))) -> Principal:
        if principal.user_id is None:
            raise ApiError(403, "user_token_required", "이 API 는 사용자 위임 토큰으로만 호출할 수 있습니다.")
        return principal

    return dependency
