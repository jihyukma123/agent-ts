"""티켓 API v2."""

from __future__ import annotations

import sqlite3
from datetime import datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Header, Query, Request, Response, status

from itsm_api import ticket_service, ticket_store, workflow
from itsm_api.auth import Principal, get_db, require, require_user
from itsm_api.codes import Classification, Priority, STATUS_LABELS, TicketStatus
from itsm_api.errors import ApiError
from itsm_api.schemas import (
    AssignmentIn,
    HistoryPage,
    MergeIn,
    ProblemLinkIn,
    TicketCreateIn,
    TicketOut,
    TicketPage,
    TicketPatchIn,
    TicketStatsOut,
    TransitionIn,
    TransitionOptionOut,
    TransitionsOut,
    WorklogIn,
    WorklogOut,
    WorklogPage,
)
from itsm_api.ticket_service import RequestMeta
from itsm_api.ticket_store import DEFAULT_LIMIT, MAX_LIMIT, TicketFilters

router = APIRouter(prefix="/api/v2/tickets", tags=["tickets"])

IfMatch = Annotated[str | None, Header(alias="If-Match", description='티켓의 현재 version (예: "3")')]


def request_meta(request: Request) -> RequestMeta:
    return RequestMeta(request_id=request.state.request_id, now=request.app.state.clock.now())


def _require_tz(name: str, value: datetime | None) -> datetime | None:
    if value is not None and value.tzinfo is None:
        raise ApiError(
            400,
            "invalid_filter",
            f"{name} 에는 시간대가 포함되어야 합니다 (예: 2026-10-20T00:00:00+09:00).",
            errors=[{"field": name, "code": "timezone_required", "message": "시간대 필요"}],
        )
    return value


def ticket_filters(
    q: Annotated[str | None, Query(max_length=200, description="ID·제목·설명·영향 범위·서비스·컴포넌트·모델 검색어 (공백 구분 AND)")] = None,
    status_: Annotated[list[TicketStatus] | None, Query(alias="status", description="상태 코드 (반복 가능, OR)")] = None,
    priority: Annotated[list[Priority] | None, Query(description="우선순위 (반복 가능, OR)")] = None,
    service_id: str | None = None,
    component_id: int | None = None,
    model: str | None = None,
    assignment_group_id: int | None = None,
    assignee: Annotated[str | None, Query(description="담당자 사용자 ID, 'me', 'none'")] = None,
    requester: Annotated[str | None, Query(description="요청자 사용자 ID 또는 'me'")] = None,
    classification: Classification | None = None,
    is_major: bool | None = None,
    created_from: Annotated[datetime | None, Query(description="생성 시각 하한 (포함, 시간대 필수)")] = None,
    created_to: Annotated[datetime | None, Query(description="생성 시각 상한 (미포함, 시간대 필수)")] = None,
    updated_from: Annotated[datetime | None, Query(description="갱신 시각 하한 (포함, 시간대 필수)")] = None,
) -> TicketFilters:
    return TicketFilters(
        q=q,
        statuses=tuple(status_ or ()),
        priorities=tuple(priority or ()),
        service_id=service_id,
        component_id=component_id,
        model=model,
        assignment_group_id=assignment_group_id,
        assignee=assignee,
        requester=requester,
        classification=classification,
        is_major=is_major,
        created_from=_require_tz("created_from", created_from),
        created_to=_require_tz("created_to", created_to),
        updated_from=_require_tz("updated_from", updated_from),
    )


def _detail(conn, principal: Principal, ticket_id: str, request: Request, response: Response | None = None) -> TicketOut:
    row = ticket_store.require_visible(conn, principal, ticket_id)
    ticket = ticket_store.to_detail(conn, principal, row, request.app.state.clock.now())
    if response is not None:
        response.headers["ETag"] = f'"{ticket.version}"'
    return ticket


@router.get("", response_model=TicketPage)
def list_tickets(
    request: Request,
    filters: TicketFilters = Depends(ticket_filters),
    sort: Literal["-created_at", "-updated_at", "priority"] = "-created_at",
    limit: Annotated[int, Query(ge=1, le=MAX_LIMIT)] = DEFAULT_LIMIT,
    cursor: Annotated[str | None, Query(description="이전 응답의 page.next_cursor")] = None,
    principal: Principal = Depends(require("ticket:read")),
    conn: sqlite3.Connection = Depends(get_db),
) -> TicketPage:
    """현재 주체가 볼 수 있는 티켓을 검색합니다. 총 건수가 필요하면 /stats 를 사용하세요."""
    return ticket_store.list_page(
        conn, principal, filters, sort=sort, limit=limit, cursor=cursor, now=request.app.state.clock.now()
    )


@router.get("/stats", response_model=TicketStatsOut)
def ticket_stats(
    group_by: Literal["status", "priority", "service", "assignment_group"] = "status",
    filters: TicketFilters = Depends(ticket_filters),
    principal: Principal = Depends(require("ticket:read")),
    conn: sqlite3.Connection = Depends(get_db),
) -> TicketStatsOut:
    """목록과 같은 조건으로 집계합니다. 볼 수 있는 티켓만 셉니다."""
    return ticket_store.stats(conn, principal, filters, group_by)


@router.post("", response_model=TicketOut, status_code=status.HTTP_201_CREATED)
def create_ticket(
    body: TicketCreateIn,
    request: Request,
    response: Response,
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key", description="재시도 시 중복 등록 방지 키")] = None,
    principal: Principal = Depends(require_user("ticket:create")),
    conn: sqlite3.Connection = Depends(get_db),
) -> TicketOut:
    ticket_id, replayed = ticket_service.create_ticket(conn, principal, body, request_meta(request), idempotency_key)
    response.headers["Location"] = f"/api/v2/tickets/{ticket_id}"
    if replayed:
        response.headers["Idempotent-Replayed"] = "true"
    return _detail(conn, principal, ticket_id, request, response)


@router.get("/{ticket_id}", response_model=TicketOut)
def read_ticket(
    ticket_id: str,
    request: Request,
    response: Response,
    principal: Principal = Depends(require("ticket:read")),
    conn: sqlite3.Connection = Depends(get_db),
) -> TicketOut:
    return _detail(conn, principal, ticket_id, request, response)


@router.patch("/{ticket_id}", response_model=TicketOut)
def update_ticket(
    ticket_id: str,
    body: TicketPatchIn,
    request: Request,
    response: Response,
    if_match: IfMatch = None,
    principal: Principal = Depends(require_user("ticket:read")),
    conn: sqlite3.Connection = Depends(get_db),
) -> TicketOut:
    ticket_service.update_ticket(conn, principal, ticket_id, body, if_match, request_meta(request))
    return _detail(conn, principal, ticket_id, request, response)


@router.put("/{ticket_id}/assignment", response_model=TicketOut)
def assign_ticket(
    ticket_id: str,
    body: AssignmentIn,
    request: Request,
    response: Response,
    if_match: IfMatch = None,
    principal: Principal = Depends(require_user("ticket:assign")),
    conn: sqlite3.Connection = Depends(get_db),
) -> TicketOut:
    ticket_service.assign_ticket(conn, principal, ticket_id, body, if_match, request_meta(request))
    return _detail(conn, principal, ticket_id, request, response)


@router.get("/{ticket_id}/transitions", response_model=TransitionsOut)
def list_transitions(
    ticket_id: str,
    request: Request,
    principal: Principal = Depends(require("ticket:read")),
    conn: sqlite3.Connection = Depends(get_db),
) -> TransitionsOut:
    """현재 주체가 이 티켓에 할 수 있는 상태 전이와 각 전이의 필수 입력."""
    row = ticket_store.require_visible(conn, principal, ticket_id)
    options = workflow.allowed(principal, row, request.app.state.clock.now())
    return TransitionsOut(
        ticket_id=row["id"],
        current_status=row["status"],
        current_status_label=STATUS_LABELS[row["status"]],
        version=row["version"],
        transitions=[
            TransitionOptionOut(
                to_status=t.target,
                to_status_label=STATUS_LABELS[t.target],
                required_fields=list(workflow.required_fields(t, row)),
                optional_fields=list(t.optional),
            )
            for t in options
        ],
    )


@router.post("/{ticket_id}/transitions", response_model=TicketOut)
def transition_ticket(
    ticket_id: str,
    body: TransitionIn,
    request: Request,
    response: Response,
    if_match: IfMatch = None,
    principal: Principal = Depends(require_user("ticket:read")),
    conn: sqlite3.Connection = Depends(get_db),
) -> TicketOut:
    ticket_service.transition_ticket(conn, principal, ticket_id, body, if_match, request_meta(request))
    return _detail(conn, principal, ticket_id, request, response)


@router.get("/{ticket_id}/worklogs", response_model=WorklogPage)
def list_worklogs(
    ticket_id: str,
    visibility: Literal["public", "internal"] | None = None,
    limit: Annotated[int, Query(ge=1, le=MAX_LIMIT)] = DEFAULT_LIMIT,
    cursor: str | None = None,
    principal: Principal = Depends(require("ticket:read")),
    conn: sqlite3.Connection = Depends(get_db),
) -> WorklogPage:
    """오래된 순서. 내부 메모는 worklog:internal 권한이 있을 때만 포함됩니다."""
    row = ticket_store.require_visible(conn, principal, ticket_id)
    return ticket_store.list_worklogs(conn, principal, row["id"], visibility=visibility, limit=limit, cursor=cursor)


@router.post("/{ticket_id}/worklogs", response_model=WorklogOut, status_code=status.HTTP_201_CREATED)
def add_worklog(
    ticket_id: str,
    body: WorklogIn,
    request: Request,
    principal: Principal = Depends(require_user("worklog:write")),
    conn: sqlite3.Connection = Depends(get_db),
) -> WorklogOut:
    worklog_id = ticket_service.add_worklog(conn, principal, ticket_id, body.body, body.visibility, request_meta(request))
    row = conn.execute(
        "SELECT w.*, u.username, u.display_name FROM worklogs w JOIN users u ON u.id = w.author_id WHERE w.id = ?",
        (worklog_id,),
    ).fetchone()
    return ticket_store.worklog_out(row)


@router.get("/{ticket_id}/history", response_model=HistoryPage)
def ticket_history(
    ticket_id: str,
    limit: Annotated[int, Query(ge=1, le=MAX_LIMIT)] = DEFAULT_LIMIT,
    cursor: str | None = None,
    principal: Principal = Depends(require("audit:read")),
    conn: sqlite3.Connection = Depends(get_db),
) -> HistoryPage:
    row = ticket_store.require_visible(conn, principal, ticket_id)
    return ticket_store.list_history(conn, row["id"], limit=limit, cursor=cursor)


@router.post("/{ticket_id}/merge", response_model=TicketOut)
def merge_ticket(
    ticket_id: str,
    body: MergeIn,
    request: Request,
    response: Response,
    if_match: IfMatch = None,
    principal: Principal = Depends(require_user("ticket:merge")),
    conn: sqlite3.Connection = Depends(get_db),
) -> TicketOut:
    """이 티켓(중복)을 target_id 티켓에 병합하고 종료합니다. If-Match 는 이 티켓의 version 입니다."""
    ticket_service.merge_ticket(conn, principal, ticket_id, body.target_id, body.note, if_match, request_meta(request))
    return _detail(conn, principal, ticket_id, request, response)


@router.put("/{ticket_id}/problem", response_model=TicketOut)
def link_problem(
    ticket_id: str,
    body: ProblemLinkIn,
    request: Request,
    response: Response,
    if_match: IfMatch = None,
    principal: Principal = Depends(require_user("problem:write")),
    conn: sqlite3.Connection = Depends(get_db),
) -> TicketOut:
    ticket_service.link_problem(conn, principal, ticket_id, body.problem_id, if_match, request_meta(request))
    return _detail(conn, principal, ticket_id, request, response)
