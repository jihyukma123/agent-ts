"""사용자·팀 디렉터리."""

from __future__ import annotations

import sqlite3
from typing import Annotated

from fastapi import APIRouter, Depends, Query

from itsm_api.auth import Principal, get_db, require
from itsm_api.errors import ApiError
from itsm_api.schemas import PageOut, TeamOut, TeamRef, UserOut, UserPage
from itsm_api.ticket_store import MAX_LIMIT, _encode_id_cursor, _id_cursor

router = APIRouter(prefix="/api/v2", tags=["directory"])

_USER_SQL = """
SELECT u.*, r.name AS role, r.label AS role_label, t.slug AS team_slug, t.name AS team_name
FROM users u JOIN roles r ON r.id = u.role_id LEFT JOIN teams t ON t.id = u.team_id
"""


def user_out(row: sqlite3.Row) -> UserOut:
    return UserOut(
        id=row["id"],
        username=row["username"],
        display_name=row["display_name"],
        email=row["email"],
        role=row["role"],
        role_label=row["role_label"],
        team=TeamRef(id=row["team_id"], slug=row["team_slug"], name=row["team_name"]) if row["team_id"] else None,
        active=bool(row["active"]),
    )


def load_user(conn: sqlite3.Connection, user_id: int) -> UserOut:
    row = conn.execute(_USER_SQL + " WHERE u.id = ?", (user_id,)).fetchone()
    if row is None:
        raise ApiError(404, "user_not_found", f"사용자 {user_id} 이(가) 없습니다.")
    return user_out(row)


@router.get("/users", response_model=UserPage)
def list_users(
    q: Annotated[str | None, Query(max_length=100, description="username·이름·이메일 부분 일치")] = None,
    team_id: int | None = None,
    role: str | None = None,
    include_inactive: bool = False,
    limit: Annotated[int, Query(ge=1, le=MAX_LIMIT)] = 50,
    cursor: str | None = None,
    _p: Principal = Depends(require("directory:read")),
    conn: sqlite3.Connection = Depends(get_db),
) -> UserPage:
    where, params = ["u.id > ?"], [_id_cursor(cursor)]
    if q:
        where.append("(instr(lower(u.username), ?) > 0 OR instr(u.display_name, ?) > 0 OR instr(lower(u.email), ?) > 0)")
        params += [q.lower(), q, q.lower()]
    if team_id is not None:
        where.append("u.team_id = ?")
        params.append(team_id)
    if role:
        where.append("r.name = ?")
        params.append(role)
    if not include_inactive:
        where.append("u.active = 1")
    rows = conn.execute(_USER_SQL + f" WHERE {' AND '.join(where)} ORDER BY u.id LIMIT ?", [*params, limit + 1]).fetchall()
    has_more = len(rows) > limit
    rows = rows[:limit]
    return UserPage(
        items=[user_out(r) for r in rows],
        page=PageOut(limit=limit, next_cursor=_encode_id_cursor(rows[-1]["id"]) if has_more else None, has_more=has_more),
    )


@router.get("/users/{user_id}", response_model=UserOut)
def get_user(user_id: int, _p: Principal = Depends(require("directory:read")), conn: sqlite3.Connection = Depends(get_db)) -> UserOut:
    return load_user(conn, user_id)


@router.get("/teams", response_model=list[TeamOut])
def list_teams(_p: Principal = Depends(require("directory:read")), conn: sqlite3.Connection = Depends(get_db)) -> list[TeamOut]:
    rows = conn.execute(
        """
        SELECT t.id, t.slug, t.name, COUNT(u.id) AS member_count
        FROM teams t LEFT JOIN users u ON u.team_id = t.id AND u.active = 1
        GROUP BY t.id ORDER BY t.id
        """
    )
    return [TeamOut(**dict(r)) for r in rows]
