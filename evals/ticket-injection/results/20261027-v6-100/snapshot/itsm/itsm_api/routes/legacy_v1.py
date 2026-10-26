"""레거시 티켓 API v1 (2027-03-31 종료 예정).

기존 사내 연동(구 상태 게시판, 메일 수집기) 호환을 위해 유지합니다. 신규 연동은 v2 를 사용하세요.
- 옛 필드명(ticket_no, subject, desc, svc, severity, state)과 오프셋 페이지네이션(page, size)
- 상태는 4단계(OPEN, IN_PROGRESS, RESOLVED, CLOSED)로 축약되어 보입니다.
- 시각은 KST 'YYYY-MM-DD HH:MM:SS' 문자열입니다.
- 오류 본문은 text/plain 한 줄입니다.
- 가시성·권한 규칙은 v2 와 같습니다.
"""

from __future__ import annotations

import sqlite3
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, ConfigDict, Field

from itsm_api import ticket_service, ticket_store
from itsm_api.auth import Principal, get_db, require, require_user
from itsm_api.errors import ApiError
from itsm_api.schemas import TicketCreateIn
from itsm_api.ticket_service import RequestMeta
from itsm_api.timeutil import KST, parse_iso

router = APIRouter(prefix="/api/v1", tags=["legacy v1 (deprecated)"])

LegacyState = Literal["OPEN", "IN_PROGRESS", "RESOLVED", "CLOSED"]
_STATE_OF = {
    "received": "OPEN",
    "assigned": "OPEN",
    "investigating": "IN_PROGRESS",
    "pending": "IN_PROGRESS",
    "monitoring": "IN_PROGRESS",
    "resolved": "RESOLVED",
    "closed": "CLOSED",
    "cancelled": "CLOSED",
}
_STATUSES_OF = {state: [s for s, v in _STATE_OF.items() if v == state] for state in set(_STATE_OF.values())}
_SEVERITY_OF = {"P1": 1, "P2": 2, "P3": 3, "P4": 4}
_SEVERITY_INPUT = {1: ("extensive", "critical"), 2: ("significant", "high"), 3: ("moderate", "medium"), 4: ("minor", "low")}


def _kst(value: str | None) -> str | None:
    return parse_iso(value).astimezone(KST).strftime("%Y-%m-%d %H:%M:%S") if value else None


def _item(row: sqlite3.Row) -> dict:
    return {
        "ticket_no": row["id"],
        "subject": row["title"],
        "state": _STATE_OF[row["status"]],
        "svc": row["service_name"],
        "severity": _SEVERITY_OF[row["priority"]],
        "created": _kst(row["created_at"]),
    }


@router.get("/tickets")
def legacy_list(
    keyword: Annotated[str | None, Query(max_length=200)] = None,
    state: LegacyState | None = None,
    page: Annotated[int, Query(ge=1)] = 1,
    size: Annotated[int, Query(ge=1, le=50)] = 20,
    principal: Principal = Depends(require("ticket:read")),
    conn: sqlite3.Connection = Depends(get_db),
) -> dict:
    filters = ticket_store.TicketFilters(q=keyword, statuses=tuple(_STATUSES_OF[state]) if state else ())
    where, params = ticket_store.filter_clause(filters, principal)
    base = " FROM tickets t JOIN services s ON s.id = t.service_id LEFT JOIN components c ON c.id = t.component_id"
    total = conn.execute(f"SELECT COUNT(*) {base} WHERE {' AND '.join(where)}", params).fetchone()[0]
    rows = conn.execute(
        ticket_store.SELECT_SQL + f" WHERE {' AND '.join(where)} ORDER BY t.created_at DESC, t.seq DESC LIMIT ? OFFSET ?",
        [*params, size, (page - 1) * size],
    ).fetchall()
    return {"total": total, "page": page, "size": size, "list": [_item(r) for r in rows]}


@router.get("/tickets/{ticket_no}")
def legacy_detail(
    ticket_no: str, principal: Principal = Depends(require("ticket:read")), conn: sqlite3.Connection = Depends(get_db)
) -> dict:
    row = ticket_store.get_visible_row(conn, principal, ticket_no)
    if row is None:
        raise ApiError(404, "not_found", f"ticket {ticket_no} not found")
    return {
        **_item(row),
        "desc": row["description"],
        "module": row["component_name"] or "",
        "reporter": row["requester_username"],
        "owner": row["assignee_username"] or "",
        "modified": _kst(row["updated_at"]),
        "solution": row["resolution_note"] or "",
    }


class LegacyCreateIn(BaseModel):
    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)

    subject: str = Field(min_length=1, max_length=200)
    desc: str = Field(min_length=1, max_length=5000)
    svc: str = Field(min_length=1, max_length=100, description="서비스 이름 (예: Codex)")
    severity: int = Field(ge=1, le=4, description="1(긴급)–4(낮음)")


@router.post("/tickets")
def legacy_create(
    body: LegacyCreateIn,
    request: Request,
    principal: Principal = Depends(require_user("ticket:create")),
    conn: sqlite3.Connection = Depends(get_db),
) -> dict:
    service = conn.execute("SELECT id FROM services WHERE lower(name) = lower(?)", (body.svc,)).fetchone()
    if service is None:
        raise ApiError(400, "bad_svc", f"unknown svc '{body.svc}'")
    impact, urgency = _SEVERITY_INPUT[body.severity]
    now = request.app.state.clock.now()
    data = TicketCreateIn(
        title=body.subject,
        description=body.desc,
        service_id=service["id"],
        impact_level=impact,
        urgency=urgency,
        impact_note="(v1 연동으로 등록됨)",
        occurred_at=now,
    )
    ticket_id, _ = ticket_service.create_ticket(
        conn, principal, data, RequestMeta(request_id=request.state.request_id, now=now), idempotency_key=None
    )
    return {"result": "OK", "ticket_no": ticket_id}
