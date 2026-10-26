"""문제(Problem) 관리: 반복 장애의 근본 원인 추적."""

from __future__ import annotations

import sqlite3
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request, status

from itsm_api.auth import Principal, get_db, require, require_user
from itsm_api.codes import PROBLEM_STATUS_LABELS, ProblemStatus
from itsm_api.errors import ApiError, field_error
from itsm_api.schemas import ProblemCreateIn, ProblemOut, TeamRef
from itsm_api.ticket_store import visibility_clause

router = APIRouter(prefix="/api/v2/problems", tags=["problems"])


def _problem_out(conn: sqlite3.Connection, principal: Principal, row: sqlite3.Row) -> ProblemOut:
    clause, params = visibility_clause(principal)
    incidents = [
        r["id"]
        for r in conn.execute(f"SELECT t.id FROM tickets t WHERE t.problem_id = ? AND {clause} ORDER BY t.seq", [row["id"], *params])
    ]
    return ProblemOut(
        id=row["id"],
        title=row["title"],
        description=row["description"],
        status=row["status"],
        status_label=PROBLEM_STATUS_LABELS[row["status"]],
        root_cause=row["root_cause"],
        workaround=row["workaround"],
        owner_team=TeamRef(id=row["owner_team_id"], slug=row["team_slug"], name=row["team_name"]),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        incident_ids=incidents,
    )


_SQL = "SELECT p.*, t.slug AS team_slug, t.name AS team_name FROM problems p JOIN teams t ON t.id = p.owner_team_id"


@router.get("", response_model=list[ProblemOut])
def list_problems(
    q: Annotated[str | None, Query(max_length=200)] = None,
    status_: Annotated[list[ProblemStatus] | None, Query(alias="status")] = None,
    principal: Principal = Depends(require("problem:read")),
    conn: sqlite3.Connection = Depends(get_db),
) -> list[ProblemOut]:
    where, params = ["1 = 1"], []
    for term in (q or "").split():
        where.append("instr(lower(p.title || ' ' || p.description), ?) > 0")
        params.append(term.lower())
    if status_:
        where.append(f"p.status IN ({', '.join('?' for _ in status_)})")
        params.extend(status_)
    rows = conn.execute(_SQL + f" WHERE {' AND '.join(where)} ORDER BY p.seq DESC", params).fetchall()
    return [_problem_out(conn, principal, r) for r in rows]


@router.get("/{problem_id}", response_model=ProblemOut)
def get_problem(
    problem_id: str, principal: Principal = Depends(require("problem:read")), conn: sqlite3.Connection = Depends(get_db)
) -> ProblemOut:
    row = conn.execute(_SQL + " WHERE p.id = ?", (problem_id.strip().upper(),)).fetchone()
    if row is None:
        raise ApiError(404, "problem_not_found", f"문제 {problem_id} 을(를) 찾을 수 없습니다.")
    return _problem_out(conn, principal, row)


@router.post("", response_model=ProblemOut, status_code=status.HTTP_201_CREATED)
def create_problem(
    body: ProblemCreateIn,
    request: Request,
    principal: Principal = Depends(require_user("problem:write")),
    conn: sqlite3.Connection = Depends(get_db),
) -> ProblemOut:
    if conn.execute("SELECT 1 FROM teams WHERE id = ?", (body.owner_team_id,)).fetchone() is None:
        raise ApiError(422, "unknown_team", f"팀 {body.owner_team_id} 이(가) 없습니다.",
                       errors=[field_error("owner_team_id", "unknown", "없는 팀")])  # fmt: skip
    now = request.app.state.clock.now_iso()
    with conn:
        conn.execute("BEGIN IMMEDIATE")
        seq = conn.execute("SELECT coalesce(max(seq), 0) + 1 FROM problems").fetchone()[0]
        problem_id = f"PRB-{seq:03d}"
        conn.execute(
            """
            INSERT INTO problems (id, seq, title, description, status, root_cause, workaround, owner_team_id, created_at, updated_at)
            VALUES (?, ?, ?, ?, 'open', NULL, ?, ?, ?, ?)
            """,
            (problem_id, seq, body.title, body.description, body.workaround, body.owner_team_id, now, now),
        )
    return get_problem(problem_id, principal, conn)
