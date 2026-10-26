"""티켓 조회: 가시성 규칙, 검색·필터, 커서 페이지네이션, 통계, 응답 변환.

가시성 규칙 (행 단위):
- ticket:read:restricted 보유: 모든 티켓
- ticket:read:all 보유: 일반 티켓 + 자기 팀이 담당하는 보안 제한 티켓 + 본인이 요청한 티켓
- 그 외(요청자): 본인이 요청한 티켓 + 주요 장애로 지정된 일반 티켓

볼 수 없는 티켓은 존재 여부를 드러내지 않도록 404 로 응답합니다.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import sqlite3
from dataclasses import asdict, dataclass
from datetime import datetime

from itsm_api import sla
from itsm_api.auth import Principal
from itsm_api.codes import (
    IMPACT_LABELS,
    PENDING_LABELS,
    PRIORITY_LABELS,
    RESOLUTION_LABELS,
    STATUS_LABELS,
    URGENCY_LABELS,
)
from itsm_api.errors import ApiError
from itsm_api.schemas import (
    ComponentRef,
    HistoryEventOut,
    HistoryPage,
    PageOut,
    ProblemRef,
    ResolutionOut,
    ServiceRef,
    SlaOut,
    StatsBucket,
    TeamRef,
    TicketOut,
    TicketPage,
    TicketStatsOut,
    TicketSummaryOut,
    UserRef,
    WorklogOut,
    WorklogPage,
)
from itsm_api.timeutil import to_utc_iso

SELECT_SQL = """
SELECT t.*, s.name AS service_name, c.name AS component_name,
       g.slug AS group_slug, g.name AS group_name,
       a.username AS assignee_username, a.display_name AS assignee_display_name,
       r.username AS requester_username, r.display_name AS requester_display_name,
       p.title AS problem_title, p.status AS problem_status
FROM tickets t
JOIN services s ON s.id = t.service_id
LEFT JOIN components c ON c.id = t.component_id
LEFT JOIN teams g ON g.id = t.assignment_group_id
LEFT JOIN users a ON a.id = t.assignee_id
JOIN users r ON r.id = t.requester_id
LEFT JOIN problems p ON p.id = t.problem_id
"""

_SEARCH_TEXT_SQL = (
    "lower(t.id || ' ' || t.title || ' ' || t.description || ' ' || t.impact_note || ' ' || s.name || ' ' "
    "|| coalesce(c.name, '') || ' ' || coalesce(t.model, ''))"
)

SORTS = ("-created_at", "-updated_at", "priority")
DEFAULT_LIMIT = 20
MAX_LIMIT = 100


def visibility_clause(principal: Principal) -> tuple[str, list]:
    if principal.has("ticket:read:restricted"):
        return "1 = 1", []
    if principal.has("ticket:read:all"):
        return (
            "(t.classification = 'standard' OR t.assignment_group_id = ? OR t.requester_id = ?)",
            [principal.team_id, principal.user_id],
        )
    return "(t.requester_id = ? OR (t.is_major = 1 AND t.classification = 'standard'))", [principal.user_id]


def normalize_ticket_id(ticket_id: str) -> str:
    return ticket_id.strip().upper()


def get_row(conn: sqlite3.Connection, ticket_id: str) -> sqlite3.Row | None:
    return conn.execute(SELECT_SQL + " WHERE t.id = ?", (normalize_ticket_id(ticket_id),)).fetchone()


def get_visible_row(conn: sqlite3.Connection, principal: Principal, ticket_id: str) -> sqlite3.Row | None:
    clause, params = visibility_clause(principal)
    return conn.execute(
        SELECT_SQL + f" WHERE t.id = ? AND {clause}", [normalize_ticket_id(ticket_id), *params]
    ).fetchone()


def require_visible(conn: sqlite3.Connection, principal: Principal, ticket_id: str) -> sqlite3.Row:
    row = get_visible_row(conn, principal, ticket_id)
    if row is None:
        raise ApiError(404, "ticket_not_found", f"티켓 {ticket_id} 을(를) 찾을 수 없습니다.", ticket_id=ticket_id)
    return row


# ---------- 응답 변환 ----------


def _pauses(conn: sqlite3.Connection, ticket_ids: list[str]) -> dict[str, list[tuple[str, str | None]]]:
    result: dict[str, list[tuple[str, str | None]]] = {tid: [] for tid in ticket_ids}
    if not ticket_ids:
        return result
    rows = conn.execute(
        f"SELECT ticket_id, started_at, ended_at FROM sla_pauses WHERE ticket_id IN ({', '.join('?' for _ in ticket_ids)})",
        ticket_ids,
    )
    for r in rows:
        result[r["ticket_id"]].append((r["started_at"], r["ended_at"]))
    return result


def _sla(row: sqlite3.Row, pauses: list[tuple[str, str | None]], now: datetime) -> dict:
    return sla.evaluate(
        priority=row["priority"],
        status=row["status"],
        created_at=row["created_at"],
        responded_at=row["responded_at"],
        resolved_at=row["resolved_at"],
        closed_at=row["closed_at"],
        pauses=pauses,
        now=now,
    )


def _team(row: sqlite3.Row) -> TeamRef | None:
    if row["assignment_group_id"] is None:
        return None
    return TeamRef(id=row["assignment_group_id"], slug=row["group_slug"], name=row["group_name"])


def _summary(row: sqlite3.Row, sla_state: dict) -> TicketSummaryOut:
    return TicketSummaryOut(
        id=row["id"],
        title=row["title"],
        status=row["status"],
        status_label=STATUS_LABELS[row["status"]],
        priority=row["priority"],
        priority_label=PRIORITY_LABELS[row["priority"]],
        service=ServiceRef(id=row["service_id"], name=row["service_name"]),
        component=ComponentRef(id=row["component_id"], name=row["component_name"]) if row["component_id"] else None,
        model=row["model"],
        assignment_group=_team(row),
        assignee=(
            UserRef(id=row["assignee_id"], username=row["assignee_username"], display_name=row["assignee_display_name"])
            if row["assignee_id"]
            else None
        ),
        classification=row["classification"],
        is_major=bool(row["is_major"]),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        sla_breached=sla_state["response_breached"] or sla_state["resolution_breached"],
    )


def worklog_visibility_clause(principal: Principal) -> str:
    return "1 = 1" if principal.has("worklog:internal") else "w.visibility = 'public'"


def to_detail(conn: sqlite3.Connection, principal: Principal, row: sqlite3.Row, now: datetime) -> TicketOut:
    sla_state = _sla(row, _pauses(conn, [row["id"]])[row["id"]], now)
    worklog_count = conn.execute(
        f"SELECT COUNT(*) FROM worklogs w WHERE w.ticket_id = ? AND {worklog_visibility_clause(principal)}",
        (row["id"],),
    ).fetchone()[0]
    clause, params = visibility_clause(principal)
    duplicates = [
        r["id"]
        for r in conn.execute(
            f"SELECT t.id FROM tickets t WHERE t.merged_into = ? AND {clause} ORDER BY t.seq", [row["id"], *params]
        )
    ]
    resolution = None
    if row["resolution_code"]:
        resolution = ResolutionOut(
            code=row["resolution_code"],
            code_label=RESOLUTION_LABELS[row["resolution_code"]],
            note=row["resolution_note"],
            resolved_at=row["resolved_at"],
        )
    return TicketOut(
        **_summary(row, sla_state).model_dump(),
        description=row["description"],
        impact_level=row["impact_level"],
        impact_label=IMPACT_LABELS[row["impact_level"]],
        urgency=row["urgency"],
        urgency_label=URGENCY_LABELS[row["urgency"]],
        impact_note=row["impact_note"],
        occurred_at=row["occurred_at"],
        requester=UserRef(
            id=row["requester_id"], username=row["requester_username"], display_name=row["requester_display_name"]
        ),
        pending_reason=row["pending_reason"],
        pending_reason_label=PENDING_LABELS.get(row["pending_reason"]) if row["pending_reason"] else None,
        resolution=resolution,
        closed_at=row["closed_at"],
        problem=(
            ProblemRef(id=row["problem_id"], title=row["problem_title"], status=row["problem_status"])
            if row["problem_id"]
            else None
        ),
        merged_into=row["merged_into"],
        duplicates=duplicates,
        sla=SlaOut(**sla_state),
        worklog_count=worklog_count,
        version=row["version"],
    )


# ---------- 목록·검색 ----------


@dataclass(frozen=True)
class TicketFilters:
    q: str | None = None
    statuses: tuple[str, ...] = ()
    priorities: tuple[str, ...] = ()
    service_id: str | None = None
    component_id: int | None = None
    model: str | None = None
    assignment_group_id: int | None = None
    assignee: str | None = None  # 사용자 ID, "me", "none"
    requester: str | None = None  # 사용자 ID, "me"
    classification: str | None = None
    is_major: bool | None = None
    created_from: datetime | None = None
    created_to: datetime | None = None
    updated_from: datetime | None = None

    def fingerprint(self) -> str:
        raw = json.dumps(asdict(self), default=str, sort_keys=True)
        return hashlib.sha256(raw.encode()).hexdigest()[:12]


def _user_ref_param(value: str, principal: Principal, field: str) -> int:
    if value == "me":
        if principal.user_id is None:
            raise ApiError(400, "invalid_filter", f"{field}=me 는 사용자 위임 토큰에서만 사용할 수 있습니다.")
        return principal.user_id
    if not value.isdigit():
        raise ApiError(
            400,
            "invalid_filter",
            f"{field} 는 사용자 ID 숫자, 'me' 중 하나여야 합니다." + (" ('none' 도 가능)" if field == "assignee" else ""),
            errors=[{"field": field, "code": "invalid", "message": f"허용되지 않는 값: {value}"}],
        )
    return int(value)


def filter_clause(filters: TicketFilters, principal: Principal) -> tuple[list[str], list]:
    clause, params = visibility_clause(principal)
    where = [clause]
    for term in (filters.q or "").split():
        where.append(f"instr({_SEARCH_TEXT_SQL}, ?) > 0")
        params.append(term.lower())
    if filters.statuses:
        where.append(f"t.status IN ({', '.join('?' for _ in filters.statuses)})")
        params.extend(filters.statuses)
    if filters.priorities:
        where.append(f"t.priority IN ({', '.join('?' for _ in filters.priorities)})")
        params.extend(filters.priorities)
    if filters.service_id:
        where.append("t.service_id = ?")
        params.append(filters.service_id)
    if filters.component_id is not None:
        where.append("t.component_id = ?")
        params.append(filters.component_id)
    if filters.model:
        where.append("t.model = ?")
        params.append(filters.model)
    if filters.assignment_group_id is not None:
        where.append("t.assignment_group_id = ?")
        params.append(filters.assignment_group_id)
    if filters.assignee:
        if filters.assignee == "none":
            where.append("t.assignee_id IS NULL")
        else:
            where.append("t.assignee_id = ?")
            params.append(_user_ref_param(filters.assignee, principal, "assignee"))
    if filters.requester:
        where.append("t.requester_id = ?")
        params.append(_user_ref_param(filters.requester, principal, "requester"))
    if filters.classification:
        where.append("t.classification = ?")
        params.append(filters.classification)
    if filters.is_major is not None:
        where.append("t.is_major = ?")
        params.append(int(filters.is_major))
    if filters.created_from:
        where.append("t.created_at >= ?")
        params.append(to_utc_iso(filters.created_from))
    if filters.created_to:
        where.append("t.created_at < ?")
        params.append(to_utc_iso(filters.created_to))
    if filters.updated_from:
        where.append("t.updated_at >= ?")
        params.append(to_utc_iso(filters.updated_from))
    return where, params


def _encode_cursor(sort: str, key: list, fingerprint: str) -> str:
    raw = json.dumps({"s": sort, "k": key, "f": fingerprint}, separators=(",", ":"))
    return base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")


def _decode_cursor(cursor: str, sort: str, fingerprint: str) -> list:
    try:
        data = json.loads(base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)))
        key, cursor_sort, cursor_fp = data["k"], data["s"], data["f"]
    except (ValueError, KeyError, TypeError, binascii.Error):
        raise ApiError(400, "invalid_cursor", "cursor 값을 해석할 수 없습니다. 첫 페이지부터 다시 조회하세요.")
    if cursor_sort != sort or cursor_fp != fingerprint:
        raise ApiError(
            400,
            "invalid_cursor",
            "cursor 가 현재 검색 조건·정렬과 맞지 않습니다. 조건을 바꿨다면 cursor 없이 첫 페이지부터 조회하세요.",
        )
    return key


_ORDER_BY = {
    "-created_at": "t.created_at DESC, t.seq DESC",
    "-updated_at": "t.updated_at DESC, t.seq DESC",
    "priority": "t.priority ASC, t.created_at DESC, t.seq DESC",
}


def _after_clause(sort: str, key: list) -> tuple[str, list]:
    if sort == "-created_at":
        return "(t.created_at < ? OR (t.created_at = ? AND t.seq < ?))", [key[0], key[0], key[1]]
    if sort == "-updated_at":
        return "(t.updated_at < ? OR (t.updated_at = ? AND t.seq < ?))", [key[0], key[0], key[1]]
    priority, created_at, seq = key
    return (
        "(t.priority > ? OR (t.priority = ? AND (t.created_at < ? OR (t.created_at = ? AND t.seq < ?))))",
        [priority, priority, created_at, created_at, seq],
    )


def _sort_key(sort: str, row: sqlite3.Row) -> list:
    if sort == "-created_at":
        return [row["created_at"], row["seq"]]
    if sort == "-updated_at":
        return [row["updated_at"], row["seq"]]
    return [row["priority"], row["created_at"], row["seq"]]


def list_page(
    conn: sqlite3.Connection,
    principal: Principal,
    filters: TicketFilters,
    *,
    sort: str,
    limit: int,
    cursor: str | None,
    now: datetime,
) -> TicketPage:
    where, params = filter_clause(filters, principal)
    fingerprint = filters.fingerprint()
    if cursor:
        clause, extra = _after_clause(sort, _decode_cursor(cursor, sort, fingerprint))
        where.append(clause)
        params.extend(extra)
    sql = SELECT_SQL + " WHERE " + " AND ".join(where) + f" ORDER BY {_ORDER_BY[sort]} LIMIT ?"
    rows = conn.execute(sql, [*params, limit + 1]).fetchall()
    has_more = len(rows) > limit
    rows = rows[:limit]
    pauses = _pauses(conn, [r["id"] for r in rows])
    items = [_summary(r, _sla(r, pauses[r["id"]], now)) for r in rows]
    next_cursor = _encode_cursor(sort, _sort_key(sort, rows[-1]), fingerprint) if has_more and rows else None
    return TicketPage(items=items, page=PageOut(limit=limit, next_cursor=next_cursor, has_more=has_more))


_GROUPS = {
    "status": ("t.status", lambda key, _row: STATUS_LABELS.get(key, key)),
    "priority": ("t.priority", lambda key, _row: f"{key} {PRIORITY_LABELS.get(key, '')}".strip()),
    "service": ("t.service_id", lambda _key, row: row["label"]),
    "assignment_group": ("t.assignment_group_id", lambda _key, row: row["label"] or "미배정"),
}
_GROUP_LABEL_SQL = {
    "status": "NULL",
    "priority": "NULL",
    "service": "max(s.name)",
    "assignment_group": "max(g.name)",
}


def stats(conn: sqlite3.Connection, principal: Principal, filters: TicketFilters, group_by: str) -> TicketStatsOut:
    where, params = filter_clause(filters, principal)
    column, label_of = _GROUPS[group_by]
    sql = (
        f"SELECT {column} AS key, {_GROUP_LABEL_SQL[group_by]} AS label, COUNT(*) AS n "
        "FROM tickets t JOIN services s ON s.id = t.service_id "
        "LEFT JOIN components c ON c.id = t.component_id "
        "LEFT JOIN teams g ON g.id = t.assignment_group_id "
        f"WHERE {' AND '.join(where)} GROUP BY {column} ORDER BY n DESC, key"
    )
    buckets = [
        StatsBucket(key=None if r["key"] is None else str(r["key"]), label=label_of(r["key"], r), count=r["n"])
        for r in conn.execute(sql, params)
    ]
    return TicketStatsOut(group_by=group_by, total=sum(b.count for b in buckets), buckets=buckets)


# ---------- worklog·이력 ----------


def _id_cursor(cursor: str | None) -> int:
    if cursor is None:
        return 0
    try:
        return int(base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)).decode())
    except (ValueError, binascii.Error):
        raise ApiError(400, "invalid_cursor", "cursor 값을 해석할 수 없습니다. 첫 페이지부터 다시 조회하세요.")


def _encode_id_cursor(value: int) -> str:
    return base64.urlsafe_b64encode(str(value).encode()).decode().rstrip("=")


def worklog_out(row: sqlite3.Row) -> WorklogOut:
    return WorklogOut(
        id=row["id"],
        ticket_id=row["ticket_id"],
        author=UserRef(id=row["author_id"], username=row["username"], display_name=row["display_name"]),
        visibility=row["visibility"],
        kind=row["kind"],
        body=row["body"],
        created_at=row["created_at"],
    )


def list_worklogs(
    conn: sqlite3.Connection,
    principal: Principal,
    ticket_id: str,
    *,
    visibility: str | None,
    limit: int,
    cursor: str | None,
) -> WorklogPage:
    if visibility == "internal" and not principal.has("worklog:internal"):
        raise ApiError(403, "permission_denied", "내부 메모 조회에는 worklog:internal 권한이 필요합니다.",
                       required_permissions=["worklog:internal"])  # fmt: skip
    where = ["w.ticket_id = ?", worklog_visibility_clause(principal), "w.id > ?"]
    params: list = [ticket_id, _id_cursor(cursor)]
    if visibility:
        where.append("w.visibility = ?")
        params.append(visibility)
    rows = conn.execute(
        "SELECT w.*, u.username, u.display_name FROM worklogs w JOIN users u ON u.id = w.author_id "
        f"WHERE {' AND '.join(where)} ORDER BY w.id LIMIT ?",
        [*params, limit + 1],
    ).fetchall()
    has_more = len(rows) > limit
    rows = rows[:limit]
    return WorklogPage(
        items=[worklog_out(r) for r in rows],
        page=PageOut(limit=limit, next_cursor=_encode_id_cursor(rows[-1]["id"]) if has_more else None, has_more=has_more),
    )


def list_history(conn: sqlite3.Connection, ticket_id: str, *, limit: int, cursor: str | None) -> HistoryPage:
    rows = conn.execute(
        """
        SELECT e.*, u.username, u.display_name FROM audit_events e LEFT JOIN users u ON u.id = e.actor_id
        WHERE e.ticket_id = ? AND e.id > ? ORDER BY e.id LIMIT ?
        """,
        (ticket_id, _id_cursor(cursor), limit + 1),
    ).fetchall()
    has_more = len(rows) > limit
    rows = rows[:limit]
    items = [
        HistoryEventOut(
            id=r["id"],
            action=r["action"],
            actor=UserRef(id=r["actor_id"], username=r["username"], display_name=r["display_name"]) if r["actor_id"] else None,
            client_id=r["client_id"],
            changes=json.loads(r["changes"]),
            request_id=r["request_id"],
            at=r["at"],
        )
        for r in rows
    ]
    return HistoryPage(
        items=items,
        page=PageOut(limit=limit, next_cursor=_encode_id_cursor(rows[-1]["id"]) if has_more else None, has_more=has_more),
    )
