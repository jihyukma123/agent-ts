"""테스트 사용자 전환, 서버 측 세션, ITSM 위임 토큰 관리.

- 로그인/비밀번호는 없습니다. POST /api/session 으로 테스트 사용자(ITSM 디렉터리의 사용자)를 선택하면
  에이전트 서비스가 ITSM API 에서 그 사용자의 위임 토큰을 교환해 받고, 세션 테이블에 사용자 정보·토큰을 저장합니다.
  쿠키에는 임의의 세션 ID만 담습니다.
- 그 외 모든 보호된 API는 쿠키의 세션 ID -> sessions 순서로 현재 사용자를 결정합니다.
  클라이언트가 보낸 사용자 ID·역할 값은 사용하지 않습니다.
- ITSM 데이터에 대한 권한·가시성 판단은 ITSM API 가 위임 토큰으로 합니다.
"""

from __future__ import annotations

import json
import secrets
import sqlite3
import uuid
from collections.abc import Iterator
from contextlib import closing
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status

from app.db import SESSION_LIFETIME, open_db
from app.itsm_auth import ItsmApiError, ItsmAuthClient, ItsmUnavailableError
from app.schemas import SessionOut, SessionSwitchIn, UserOut

# ITSM 웹과 같은 호스트에서 실행해도 쿠키가 섞이지 않도록 이름을 따로 씁니다.
SESSION_COOKIE = "itsm_agent_session"
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
    conversation_id: str
    itsm_token: str = field(repr=False)
    itsm_token_expires_at: datetime
    session_expires_at: datetime
    itsm_credential_generation: int = 0
    lifecycle_generation: int = 0

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


def api_error(status_code: int, code: str, message: str) -> HTTPException:
    return HTTPException(status_code=status_code, detail={"code": code, "message": message})


def itsm_http_error(exc: ItsmApiError | ItsmUnavailableError) -> HTTPException:
    if isinstance(exc, ItsmUnavailableError):
        return api_error(status.HTTP_503_SERVICE_UNAVAILABLE, "itsm_unavailable", "ITSM 시스템에 연결하지 못했습니다.")
    if exc.status == 401:
        return api_error(status.HTTP_502_BAD_GATEWAY, "itsm_auth_failed", "ITSM 인증에 실패했습니다.")
    if exc.status in (400, 403, 404, 409, 422):
        return api_error(status.HTTP_502_BAD_GATEWAY, "itsm_request_rejected", "ITSM 요청이 거부되었습니다.")
    return api_error(status.HTTP_503_SERVICE_UNAVAILABLE, "itsm_unavailable", "ITSM 시스템을 사용할 수 없습니다.")


def get_db(request: Request) -> Iterator[sqlite3.Connection]:
    with open_db(request.app.state.settings.db_path) as conn:
        yield conn


def get_itsm(request: Request) -> ItsmAuthClient:
    return request.app.state.itsm


def _parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _format_time(value: datetime) -> str:
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def load_current_user(conn: sqlite3.Connection, session_id: str | None) -> CurrentUser | None:
    if not session_id:
        return None
    row = conn.execute("SELECT * FROM sessions WHERE id = ?", (session_id,)).fetchone()
    if row is None:
        return None
    try:
        session_expires_at = _parse_time(row["session_expires_at"])
    except (TypeError, ValueError):
        session_expires_at = datetime.fromtimestamp(0, timezone.utc)
    return CurrentUser(
        id=row["user_id"],
        username=row["username"],
        display_name=row["display_name"],
        role=row["role"],
        role_label=row["role_label"],
        team=row["team"],
        permissions=frozenset(json.loads(row["permissions"])),
        session_id=row["id"],
        conversation_id=row["conversation_id"],
        itsm_token=row["itsm_token"],
        itsm_token_expires_at=_parse_time(row["itsm_token_expires_at"]),
        itsm_credential_generation=row["itsm_credential_generation"],
        session_expires_at=session_expires_at,
    )


def _cleanup_conversation_history(history_db_path: Path, conversation_id: str) -> None:
    """Presentation companion을 먼저, SDK history를 그 다음 순서로 제거한다."""
    if not history_db_path.exists():
        return
    with closing(sqlite3.connect(history_db_path)) as history:
        history.execute("BEGIN IMMEDIATE")
        tables = {
            row[0]
            for row in history.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
        }
        if "ticket_message_presentations" in tables:
            history.execute(
                "DELETE FROM ticket_message_presentations WHERE conversation_id = ?",
                (conversation_id,),
            )
        if "agent_messages" in tables:
            history.execute("DELETE FROM agent_messages WHERE session_id = ?", (conversation_id,))
        if "agent_sessions" in tables:
            history.execute("DELETE FROM agent_sessions WHERE session_id = ?", (conversation_id,))
        history.commit()


def cleanup_expired_session(
    conn: sqlite3.Connection, user: CurrentUser, history_db_path: Path
) -> None:
    """History를 먼저 지우고 authority state를 한 transaction으로 terminal 처리한다."""
    _cleanup_conversation_history(history_db_path, user.conversation_id)
    with conn:
        conn.execute("DELETE FROM pending_ticket_mutations WHERE session_id = ?", (user.session_id,))
        conn.execute("DELETE FROM ticket_cursor_bindings WHERE session_id = ?", (user.session_id,))
        deleted = conn.execute("DELETE FROM sessions WHERE id = ?", (user.session_id,)).rowcount
        if deleted == 0:
            session_count = conn.execute(
                "SELECT COUNT(*) FROM sessions WHERE id = ?", (user.session_id,)
            ).fetchone()[0]
            pending_count = conn.execute(
                "SELECT COUNT(*) FROM pending_ticket_mutations WHERE session_id = ?",
                (user.session_id,),
            ).fetchone()[0]
            cursor_count = conn.execute(
                "SELECT COUNT(*) FROM ticket_cursor_bindings WHERE session_id = ?",
                (user.session_id,),
            ).fetchone()[0]
            if session_count or pending_count or cursor_count:
                raise sqlite3.IntegrityError("expired session cleanup is incomplete")


def _now(request: Request) -> datetime:
    now = request.app.state.utc_now()
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    return now


def _authentication_required() -> HTTPException:
    return api_error(
        status.HTTP_401_UNAUTHORIZED,
        "authentication_required",
        "사용자 세션이 만료되었습니다. 테스트 사용자를 다시 선택하세요.",
    )


def _terminal_cleanup(request: Request, conn: sqlite3.Connection, user: CurrentUser) -> None:
    try:
        # The caller holds the shared conversation lock. Retire U3 execution
        # authority before deleting its U1 pending binding.
        repository = request.app.state.agent_service.update_execution_repository
        if repository is not None:
            repository.retire_scope(
                session_id=user.session_id,
                conversation_id=user.conversation_id,
                now=_now(request),
            )
        cleanup_expired_session(
            conn, user, request.app.state.settings.agent_history_db_path
        )
        request.app.state.conversation_locks.advance_generation(user.conversation_id)
    except sqlite3.Error:
        raise api_error(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "session_cleanup_failed",
            "만료된 세션을 안전하게 정리하지 못했습니다.",
        ) from None


def revalidate_current_user(request: Request, expected: CurrentUser) -> CurrentUser:
    """Shared conversation lock 아래 persisted authority를 재검증하고 terminal cleanup한다."""
    generation = request.app.state.conversation_locks.generation(expected.conversation_id)
    if generation != expected.lifecycle_generation:
        raise _authentication_required()
    with open_db(request.app.state.settings.db_path) as conn:
        current = load_current_user(conn, expected.session_id)
        if current is not None and (
            current.id != expected.id or current.conversation_id != expected.conversation_id
        ):
            raise _authentication_required()
        if current is None or _now(request) >= current.session_expires_at:
            _terminal_cleanup(request, conn, expected)
            raise _authentication_required()
        return replace(current, lifecycle_generation=generation)


async def get_current_user(request: Request, conn: sqlite3.Connection = Depends(get_db)) -> CurrentUser:
    session_id = request.cookies.get(SESSION_COOKIE)
    user = load_current_user(conn, session_id)
    if user is None:
        if session_id:
            raise _authentication_required()
        raise api_error(
            status.HTTP_401_UNAUTHORIZED,
            "no_session",
            "현재 사용자 세션이 없습니다. 화면 상단에서 테스트 사용자를 선택하세요.",
        )
    generation = request.app.state.conversation_locks.generation(user.conversation_id)
    user = replace(user, lifecycle_generation=generation)
    if _now(request) < user.session_expires_at:
        return user

    # Opaque cookie에서 server-side conversation scope를 찾은 뒤 Runner/history와
    # 동일한 lock을 얻고 persisted state를 다시 읽는다. Lock 밖에서는 history를 지우지 않는다.
    lock = request.app.state.conversation_locks.for_conversation(user.conversation_id)
    async with lock:
        return revalidate_current_user(request, user)


async def itsm_token_for(request: Request, user: CurrentUser) -> str:
    """현재 사용자의 유효한 ITSM 위임 토큰. 만료가 가까우면 다시 교환해 세션에 저장합니다."""
    user = revalidate_current_user(request, user)
    now = request.app.state.utc_now()
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    if user.itsm_token_expires_at - TOKEN_REFRESH_MARGIN > now:
        return user.itsm_token
    token, _expires_at, _generation = await refresh_itsm_credential_for(request, user)
    return token


async def refresh_itsm_token_for(request: Request, user: CurrentUser) -> str:
    """401 token_expired 뒤 operation budget 안에서 위임 토큰을 정확히 한 번 강제 갱신한다."""
    token, _expires_at, _generation = await refresh_itsm_credential_for(request, user)
    return token


async def refresh_itsm_credential_for(
    request: Request, user: CurrentUser
) -> tuple[str, datetime, int]:
    """Capability 내부 갱신용 credential과 expiry. 모델/API 경계에는 반환하지 않는다."""
    user = revalidate_current_user(request, user)
    try:
        issued = await get_itsm(request).exchange_token(user.username)
    except (ItsmApiError, ItsmUnavailableError) as exc:
        raise itsm_http_error(exc) from None
    with open_db(request.app.state.settings.db_path) as conn, conn:
        row = conn.execute(
            """UPDATE sessions
               SET itsm_token = ?, itsm_token_expires_at = ?,
                   itsm_credential_generation = itsm_credential_generation + 1
               WHERE id = ? AND user_id = ?
               RETURNING itsm_credential_generation""",
            (issued.access_token, _format_time(issued.expires_at), user.session_id, user.id),
        ).fetchone()
        if row is None:
            raise api_error(status.HTTP_401_UNAUTHORIZED, "no_session", "현재 사용자 세션이 없습니다.")
    return issued.access_token, issued.expires_at, int(row[0])


router = APIRouter(prefix="/api", tags=["users & session"])


@router.get("/users", response_model=list[UserOut])
async def get_users(request: Request) -> list[UserOut]:
    """전환할 수 있는 테스트 사용자(ITSM 디렉터리의 활성 사용자)."""
    try:
        users = await get_itsm(request).list_users()
    except (ItsmApiError, ItsmUnavailableError) as exc:
        raise itsm_http_error(exc) from None
    return [
        UserOut(
            id=u["id"],
            username=u["username"],
            display_name=u["display_name"],
            role=u["role"],
            role_label=u["role_label"],
            team=u["team"]["name"] if u.get("team") else None,
        )
        for u in users
    ]


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

    전환할 때마다 기존 세션과 그 Agent 대화 이력을 지우고 새 세션·새 대화를 만듭니다.
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
        raise itsm_http_error(exc) from None
    except ItsmUnavailableError as exc:
        raise itsm_http_error(exc) from None

    old = load_current_user(conn, request.cookies.get(SESSION_COOKIE))
    if old is not None:
        old = replace(
            old,
            lifecycle_generation=request.app.state.conversation_locks.generation(old.conversation_id),
        )
    session_id = secrets.token_urlsafe(32)
    conversation_id = uuid.uuid4().hex
    created_at = request.app.state.utc_now()
    if created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=timezone.utc)
    created_at = created_at.astimezone(timezone.utc)
    session_expires_at = created_at + SESSION_LIFETIME
    def commit_new_identity(current: CurrentUser | None = None) -> None:
        with conn:
            if current is not None:
                deleted = conn.execute(
                    "DELETE FROM sessions WHERE id = ? AND user_id = ? AND conversation_id = ?",
                    (current.session_id, current.id, current.conversation_id),
                ).rowcount
                if deleted != 1:
                    raise sqlite3.IntegrityError("session switch actor changed")
            conn.execute(
                """
                INSERT INTO sessions (id, user_id, username, display_name, role, role_label, team, permissions,
                                      conversation_id, itsm_token, itsm_token_expires_at, created_at, session_expires_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                    conversation_id,
                    issued.access_token,
                    _format_time(issued.expires_at),
                    _format_time(created_at),
                    _format_time(session_expires_at),
                ),
            )

    if old is not None:
        await request.app.state.agent_service.lifecycle_transition(request, old, commit_new_identity)
    else:
        commit_new_identity()
    response.set_cookie(SESSION_COOKIE, session_id, httponly=True, samesite="lax", path="/")
    user = load_current_user(conn, session_id)
    assert user is not None
    return SessionOut(user=user.to_out())


@router.delete("/session", status_code=status.HTTP_204_NO_CONTENT)
async def end_session(request: Request, conn: sqlite3.Connection = Depends(get_db)) -> Response:
    """현재 세션을 끝냅니다(세션 없는 상태 확인용). 세션이 없어도 204 를 반환합니다."""
    old = load_current_user(conn, request.cookies.get(SESSION_COOKIE))
    if old is not None:
        old = replace(
            old,
            lifecycle_generation=request.app.state.conversation_locks.generation(old.conversation_id),
        )

        def commit_logout(current: CurrentUser) -> None:
            with conn:
                deleted = conn.execute(
                    "DELETE FROM sessions WHERE id = ? AND user_id = ? AND conversation_id = ?",
                    (current.session_id, current.id, current.conversation_id),
                ).rowcount
                if deleted != 1:
                    raise sqlite3.IntegrityError("logout actor changed")

        await request.app.state.agent_service.lifecycle_transition(request, old, commit_logout)
    response = Response(status_code=status.HTTP_204_NO_CONTENT)
    response.delete_cookie(SESSION_COOKIE, path="/")
    return response
