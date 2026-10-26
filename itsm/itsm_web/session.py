"""테스트 사용자 전환, 서버 측 세션, ITSM 위임 토큰 관리.

- 로그인/비밀번호는 없습니다. POST /api/session 으로 테스트 사용자를 선택하면 ITSM 웹이 ITSM API 에서
  그 사용자의 위임 토큰을 교환해 받고, 세션 테이블에 사용자 정보·토큰을 저장합니다. 쿠키에는 임의의 세션 ID만 담습니다.
- 그 외 모든 보호된 API는 쿠키의 세션 ID -> sessions 순서로 현재 사용자를 결정합니다.
  클라이언트가 보낸 사용자 ID·역할 값은 사용하지 않습니다.
- 권한 검사의 최종 판단은 ITSM API 가 위임 토큰으로 합니다. ITSM 웹의 permissions 는 화면 표시용입니다.
"""

from __future__ import annotations

import json
import secrets
import sqlite3
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status

from itsm_web.db import open_db
from itsm_web.itsm_client import ItsmApiError, ItsmClient, ItsmUnavailableError
from itsm_web.schemas import SessionOut, SessionSwitchIn, UserOut, utc_now_iso

SESSION_COOKIE = "itsm_session"
# 만료까지 이 시간보다 적게 남은 위임 토큰은 미리 다시 교환합니다.
TOKEN_REFRESH_MARGIN = timedelta(seconds=60)


@dataclass(frozen=True)
class CurrentUser:
    """백엔드가 세션으로 확인한 현재 사용자."""

    id: int
    username: str
    display_name: str
    role: str
    role_label: str
    team: str | None
    permissions: frozenset[str]
    session_id: str
    itsm_token: str = field(repr=False)
    itsm_token_expires_at: datetime

    def has_permission(self, code: str) -> bool:
        return code in self.permissions

    def to_out(self) -> UserOut:
        return UserOut(
            id=self.id,
            username=self.username,
            display_name=self.display_name,
            role=self.role,
            role_label=self.role_label,
            team=self.team,
            permissions=sorted(self.permissions),
        )


def api_error(status_code: int, code: str, message: str, **extra) -> HTTPException:
    return HTTPException(status_code=status_code, detail={"code": code, "message": message, **extra})


def itsm_http_error(exc: ItsmApiError | ItsmUnavailableError) -> HTTPException:
    """ITSM API 오류를 ITSM 웹 오류 응답으로 바꿉니다."""
    if isinstance(exc, ItsmUnavailableError):
        return api_error(status.HTTP_503_SERVICE_UNAVAILABLE, "itsm_unavailable", f"ITSM 시스템에 연결하지 못했습니다: {exc.reason}")
    if exc.status == 401:
        return api_error(status.HTTP_502_BAD_GATEWAY, "itsm_auth_failed", f"ITSM 인증에 실패했습니다: {exc.detail}")
    extra = {"errors": exc.problem["errors"]} if exc.problem.get("errors") else {}
    return api_error(exc.status, exc.code, exc.detail, **extra)


def get_db(request: Request) -> Iterator[sqlite3.Connection]:
    with open_db(request.app.state.settings.db_path) as conn:
        yield conn


def get_itsm(request: Request) -> ItsmClient:
    return request.app.state.itsm


def _parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def load_current_user(conn: sqlite3.Connection, session_id: str | None) -> CurrentUser | None:
    if not session_id:
        return None
    row = conn.execute("SELECT * FROM sessions WHERE id = ?", (session_id,)).fetchone()
    if row is None:
        return None
    return CurrentUser(
        id=row["user_id"],
        username=row["username"],
        display_name=row["display_name"],
        role=row["role"],
        role_label=row["role_label"],
        team=row["team"],
        permissions=frozenset(json.loads(row["permissions"])),
        session_id=row["id"],
        itsm_token=row["itsm_token"],
        itsm_token_expires_at=_parse_time(row["itsm_token_expires_at"]),
    )


def get_current_user(request: Request, conn: sqlite3.Connection = Depends(get_db)) -> CurrentUser:
    user = load_current_user(conn, request.cookies.get(SESSION_COOKIE))
    if user is None:
        raise api_error(
            status.HTTP_401_UNAUTHORIZED,
            "no_session",
            "현재 사용자 세션이 없습니다. 화면 상단에서 테스트 사용자를 선택하세요.",
        )
    return user


def require_permission(code: str) -> Callable[[CurrentUser], CurrentUser]:
    """화면용 사전 검사. ITSM API 도 같은 권한을 다시 검사합니다."""

    def dependency(user: CurrentUser = Depends(get_current_user)) -> CurrentUser:
        if not user.has_permission(code):
            raise api_error(
                status.HTTP_403_FORBIDDEN,
                "permission_denied",
                f"현재 사용자의 역할({user.role})에는 '{code}' 권한이 없습니다.",
            )
        return user

    return dependency


async def itsm_token_for(request: Request, user: CurrentUser) -> str:
    """현재 사용자의 유효한 ITSM 위임 토큰. 만료가 가까우면 다시 교환해 세션에 저장합니다."""
    if user.itsm_token_expires_at - TOKEN_REFRESH_MARGIN > datetime.now(timezone.utc):
        return user.itsm_token
    try:
        issued = await get_itsm(request).exchange_token(user.username)
    except (ItsmApiError, ItsmUnavailableError) as exc:
        raise itsm_http_error(exc)
    with open_db(request.app.state.settings.db_path) as conn, conn:
        conn.execute(
            "UPDATE sessions SET itsm_token = ?, itsm_token_expires_at = ? WHERE id = ?",
            (issued.access_token, issued.expires_at.strftime("%Y-%m-%dT%H:%M:%SZ"), user.session_id),
        )
    return issued.access_token


router = APIRouter(prefix="/api", tags=["users & session"])


def _user_out(user: dict, permissions: list[str] | None = None) -> UserOut:
    return UserOut(
        id=user["id"],
        username=user["username"],
        display_name=user["display_name"],
        role=user["role"],
        role_label=user["role_label"],
        team=user["team"]["name"] if user.get("team") else None,
        permissions=permissions,
    )


@router.get("/users", response_model=list[UserOut])
async def get_users(request: Request) -> list[UserOut]:
    """전환할 수 있는 테스트 사용자(ITSM 디렉터리의 활성 사용자)."""
    try:
        users = await get_itsm(request).list_users()
    except (ItsmApiError, ItsmUnavailableError) as exc:
        raise itsm_http_error(exc)
    return [_user_out(u) for u in users]


@router.get("/session", response_model=SessionOut)
def get_session(user: CurrentUser = Depends(get_current_user)) -> SessionOut:
    return SessionOut(user=user.to_out())


@router.post("/session", response_model=SessionOut)
async def switch_session(
    body: SessionSwitchIn,
    request: Request,
    response: Response,
    conn: sqlite3.Connection = Depends(get_db),
) -> SessionOut:
    """현재 사용자를 전환합니다. 이 요청만 클라이언트가 보낸 user_id 를 사용합니다.

    전환할 때마다 기존 세션을 지우고 새 세션을 만듭니다.
    """
    itsm = get_itsm(request)
    try:
        directory_user = await itsm.get_user(body.user_id)
        if not directory_user["active"]:
            raise api_error(status.HTTP_409_CONFLICT, "user_inactive", f"사용자 {body.user_id} 은(는) 비활성 상태입니다.")
        issued = await itsm.exchange_token(directory_user["username"])
        me = await itsm.me(issued.access_token)
    except ItsmApiError as exc:
        if exc.code == "user_not_found":
            raise api_error(status.HTTP_404_NOT_FOUND, "user_not_found", f"테스트 사용자 {body.user_id} 이(가) 없습니다.")
        raise itsm_http_error(exc)
    except ItsmUnavailableError as exc:
        raise itsm_http_error(exc)

    old = load_current_user(conn, request.cookies.get(SESSION_COOKIE))
    session_id = secrets.token_urlsafe(32)
    with conn:
        if old is not None:
            conn.execute("DELETE FROM sessions WHERE id = ?", (old.session_id,))
        conn.execute(
            """
            INSERT INTO sessions (id, user_id, username, display_name, role, role_label, team, permissions,
                                  itsm_token, itsm_token_expires_at, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                session_id,
                me["id"],
                me["username"],
                me["display_name"],
                me["role"],
                me["role_label"],
                me["team"]["name"] if me.get("team") else None,
                json.dumps(me["permissions"]),
                issued.access_token,
                issued.expires_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
                utc_now_iso(),
            ),
        )
    response.set_cookie(SESSION_COOKIE, session_id, httponly=True, samesite="lax", path="/")
    user = load_current_user(conn, session_id)
    assert user is not None
    return SessionOut(user=user.to_out())


@router.delete("/session", status_code=status.HTTP_204_NO_CONTENT)
async def end_session(request: Request, conn: sqlite3.Connection = Depends(get_db)) -> Response:
    """현재 세션을 끝냅니다(세션 없는 상태 확인용). 세션이 없어도 204 를 반환합니다."""
    old = load_current_user(conn, request.cookies.get(SESSION_COOKIE))
    if old is not None:
        with conn:
            conn.execute("DELETE FROM sessions WHERE id = ?", (old.session_id,))
    response = Response(status_code=status.HTTP_204_NO_CONTENT)
    response.delete_cookie(SESSION_COOKIE, path="/")
    return response
