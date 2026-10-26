"""티켓 변경 서비스.

모든 티켓 변경은 이 모듈을 거칩니다. 변경 한 건은 하나의 트랜잭션에서
(1) 티켓 갱신과 version 증가, (2) 감사 이벤트(audit_events), (3) 알림 outbox, (필요 시) (4) 상태 변경 worklog 를 함께 기록합니다.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from itsm_api import workflow
from itsm_api.auth import Principal
from itsm_api.codes import FINAL_STATUSES, PENDING_LABELS, RESOLUTION_LABELS, STATUS_LABELS
from itsm_api.errors import ApiError, field_error
from itsm_api.schemas import AssignmentIn, TicketCreateIn, TicketPatchIn, TransitionIn
from itsm_api.sla import compute_priority
from itsm_api.ticket_store import get_row, require_visible
from itsm_api.timeutil import to_utc_iso

IDEMPOTENCY_TTL = timedelta(hours=24)
STAFF_ROLES_SQL = "('agent_l1', 'agent_l2', 'team_lead', 'admin')"


@dataclass(frozen=True)
class RequestMeta:
    request_id: str | None
    now: datetime

    @property
    def now_iso(self) -> str:
        return to_utc_iso(self.now)


# ---------- 공통 ----------


def _audit(conn, ticket_id: str, principal: Principal, meta: RequestMeta, action: str, changes: dict) -> None:
    conn.execute(
        "INSERT INTO audit_events (ticket_id, actor_id, client_id, action, changes, request_id, at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (ticket_id, principal.user_id, principal.client_id, action, json.dumps(changes, ensure_ascii=False),
         meta.request_id, meta.now_iso),
    )  # fmt: skip


def _notify(conn, event_type: str, ticket: sqlite3.Row | dict, meta: RequestMeta, payload: dict | None = None) -> None:
    recipients = sorted({r for r in (ticket["requester_id"], ticket["assignee_id"]) if r})
    conn.execute(
        "INSERT INTO notification_outbox (event_type, ticket_id, recipients, payload, created_at) VALUES (?, ?, ?, ?, ?)",
        (event_type, ticket["id"], json.dumps({"users": recipients, "team": ticket["assignment_group_id"]}),
         json.dumps(payload or {}, ensure_ascii=False), meta.now_iso),
    )  # fmt: skip


def _worklog(conn, ticket_id: str, author_id: int, body: str, meta: RequestMeta, *, kind: str, visibility: str) -> int:
    cur = conn.execute(
        "INSERT INTO worklogs (ticket_id, author_id, visibility, kind, body, created_at) VALUES (?, ?, ?, ?, ?, ?)",
        (ticket_id, author_id, visibility, kind, body, meta.now_iso),
    )
    return cur.lastrowid


def _require_user(principal: Principal) -> int:
    if principal.user_id is None:
        raise ApiError(403, "user_token_required", "티켓 변경은 사용자 위임 토큰으로만 할 수 있습니다.")
    return principal.user_id


def check_version(row: sqlite3.Row, if_match: str | None) -> None:
    if if_match is None:
        raise ApiError(
            428,
            "precondition_required",
            "변경 요청에는 If-Match 헤더(티켓의 현재 version)가 필요합니다. 티켓을 조회해 version 을 확인하세요.",
            current_version=row["version"],
        )
    value = if_match.strip().removeprefix("W/").strip('"')
    if value != str(row["version"]):
        raise ApiError(
            412,
            "version_conflict",
            f"티켓 {row['id']} 이(가) 그 사이 변경되었습니다(요청 version {value}, 현재 version {row['version']}). "
            "다시 조회한 뒤 변경 내용을 확인하고 재시도하세요.",
            current_version=row["version"],
        )


def _reject_final(row: sqlite3.Row) -> None:
    if row["status"] in FINAL_STATUSES:
        raise ApiError(
            409,
            "ticket_closed",
            f"티켓 {row['id']} 은(는) {STATUS_LABELS[row['status']]} 상태라 변경할 수 없습니다. 필요하면 새 티켓을 등록하세요.",
            current_status=row["status"],
        )


def _update(conn, row: sqlite3.Row, values: dict[str, Any], meta: RequestMeta) -> None:
    values = {**values, "updated_at": meta.now_iso, "version": row["version"] + 1}
    assignments = ", ".join(f"{k} = :{k}" for k in values)
    cur = conn.execute(
        f"UPDATE tickets SET {assignments} WHERE id = :_id AND version = :_version",
        {**values, "_id": row["id"], "_version": row["version"]},
    )
    if cur.rowcount != 1:
        raise ApiError(412, "version_conflict", f"티켓 {row['id']} 이(가) 동시에 변경되었습니다. 다시 조회한 뒤 재시도하세요.")


def _diff(row: sqlite3.Row, values: dict[str, Any]) -> dict[str, list]:
    return {k: [row[k], v] for k, v in values.items() if row[k] != v}


# ---------- 카탈로그 검증 ----------


def _validate_service(conn, service_id: str) -> sqlite3.Row:
    service = conn.execute("SELECT * FROM services WHERE id = ?", (service_id,)).fetchone()
    if service is None:
        active = [r["id"] for r in conn.execute("SELECT id FROM services WHERE status = 'active' ORDER BY id")]
        raise ApiError(
            422,
            "unknown_service",
            f"서비스 '{service_id}' 이(가) 카탈로그에 없습니다.",
            errors=[field_error("service_id", "unknown", "카탈로그에 없는 서비스", allowed_values=active)],
        )
    if service["status"] == "deprecated":
        raise ApiError(
            422,
            "service_deprecated",
            f"서비스 '{service['name']}' 은(는) 폐기되어 신규 접수를 받지 않습니다.",
            errors=[field_error("service_id", "deprecated", "폐기된 서비스",
                                replacement_service_id=service["replacement_service_id"])],
        )  # fmt: skip
    return service


def _validate_component(conn, service_id: str, component_id: int | None) -> sqlite3.Row | None:
    if component_id is None:
        return None
    component = conn.execute("SELECT * FROM components WHERE id = ?", (component_id,)).fetchone()
    if component is None or component["service_id"] != service_id:
        valid = [
            {"id": r["id"], "name": r["name"]}
            for r in conn.execute("SELECT id, name FROM components WHERE service_id = ? ORDER BY id", (service_id,))
        ]
        raise ApiError(
            422,
            "component_service_mismatch",
            f"컴포넌트 {component_id} 은(는) 서비스 '{service_id}' 에 속하지 않습니다.",
            errors=[field_error("component_id", "not_in_service", "서비스에 속하지 않는 컴포넌트", allowed_values=valid)],
        )
    return component


def _validate_model(conn, model: str | None) -> None:
    if model is None:
        return
    row = conn.execute("SELECT status FROM models WHERE id = ?", (model,)).fetchone()
    if row is None or row["status"] != "active":
        active = [r["id"] for r in conn.execute("SELECT id FROM models WHERE status = 'active' ORDER BY id")]
        code = "unknown_model" if row is None else "model_retired"
        raise ApiError(
            422,
            code,
            f"모델 '{model}' 은(는) " + ("카탈로그에 없습니다." if row is None else "서비스가 종료된 모델입니다."),
            errors=[field_error("model", code, "사용할 수 없는 모델", allowed_values=active)],
        )


def _validate_assignee(conn, group_id: int, assignee_id: int | None) -> None:
    if conn.execute("SELECT 1 FROM teams WHERE id = ?", (group_id,)).fetchone() is None:
        raise ApiError(422, "unknown_team", f"담당 그룹 {group_id} 이(가) 없습니다.",
                       errors=[field_error("assignment_group_id", "unknown", "없는 팀")])  # fmt: skip
    if assignee_id is None:
        return
    members = conn.execute(
        f"""
        SELECT u.id, u.username, u.display_name FROM users u JOIN roles r ON r.id = u.role_id
        WHERE u.team_id = ? AND u.active = 1 AND r.name IN {STAFF_ROLES_SQL} ORDER BY u.id
        """,
        (group_id,),
    ).fetchall()
    if assignee_id not in {m["id"] for m in members}:
        raise ApiError(
            422,
            "assignee_not_in_group",
            f"사용자 {assignee_id} 은(는) 담당 그룹 {group_id} 의 활성 담당자가 아닙니다.",
            errors=[field_error("assignee_id", "not_in_group", "담당 그룹의 활성 담당자가 아님",
                                allowed_values=[dict(m) for m in members])],
        )  # fmt: skip


# ---------- 등록 ----------


def _request_hash(data: TicketCreateIn) -> str:
    return hashlib.sha256(data.model_dump_json().encode()).hexdigest()


def create_ticket(
    conn: sqlite3.Connection,
    principal: Principal,
    data: TicketCreateIn,
    meta: RequestMeta,
    idempotency_key: str | None,
) -> tuple[str, bool]:
    """(티켓 ID, 멱등 재처리 여부)를 반환합니다."""
    user_id = _require_user(principal)
    if idempotency_key is not None and not (1 <= len(idempotency_key) <= 100):
        raise ApiError(400, "invalid_idempotency_key", "Idempotency-Key 는 1–100자여야 합니다.")

    service = _validate_service(conn, data.service_id)
    component = _validate_component(conn, data.service_id, data.component_id)
    _validate_model(conn, data.model)
    group_id = (component["owner_team_id"] if component else None) or service["owner_team_id"]
    # 보안 제한 티켓은 누구나 신고할 수 있고, 서비스와 관계없이 보안관제팀으로 배정됩니다.
    if data.classification == "restricted":
        group_id = conn.execute("SELECT id FROM teams WHERE slug = 'security'").fetchone()["id"]
    priority = compute_priority(data.impact_level, data.urgency)
    request_hash = _request_hash(data)

    with conn:
        # BEGIN IMMEDIATE: 멱등키 확인·번호 계산·INSERT 사이에 다른 쓰기가 끼어들지 않게 합니다.
        conn.execute("BEGIN IMMEDIATE")
        if idempotency_key is not None:
            existing = conn.execute(
                "SELECT * FROM idempotency_keys WHERE client_id = ? AND user_id = ? AND key = ? AND created_at >= ?",
                (principal.client_id, user_id, idempotency_key, to_utc_iso(meta.now - IDEMPOTENCY_TTL)),
            ).fetchone()
            if existing is not None:
                if existing["request_hash"] != request_hash:
                    raise ApiError(
                        422,
                        "idempotency_key_reused",
                        "같은 Idempotency-Key 로 다른 내용의 등록 요청이 이미 처리되었습니다. 새 요청에는 새 키를 사용하세요.",
                        original_ticket_id=existing["ticket_id"],
                    )
                return existing["ticket_id"], True

        seq = conn.execute("SELECT coalesce(max(seq), 0) + 1 FROM tickets").fetchone()[0]
        ticket_id = f"INC-{seq:03d}"
        row = {
            "id": ticket_id,
            "seq": seq,
            "title": data.title,
            "description": data.description,
            "service_id": data.service_id,
            "component_id": data.component_id,
            "model": data.model,
            "impact_level": data.impact_level,
            "urgency": data.urgency,
            "priority": priority,
            "impact_note": data.impact_note,
            "classification": data.classification,
            "requester_id": user_id,
            "assignment_group_id": group_id,
            "assignee_id": None,
            "occurred_at": to_utc_iso(data.occurred_at),
            "now": meta.now_iso,
        }
        conn.execute(
            """
            INSERT INTO tickets (id, seq, title, description, service_id, component_id, model, status, impact_level,
                                 urgency, priority, impact_note, classification, is_major, requester_id,
                                 assignment_group_id, occurred_at, created_at, updated_at, version)
            VALUES (:id, :seq, :title, :description, :service_id, :component_id, :model, 'received', :impact_level,
                    :urgency, :priority, :impact_note, :classification, 0, :requester_id,
                    :assignment_group_id, :occurred_at, :now, :now, 1)
            """,
            row,
        )
        _audit(conn, ticket_id, principal, meta, "ticket.created",
               {"status": [None, "received"], "priority": [None, priority], "assignment_group_id": [None, group_id]})  # fmt: skip
        _notify(conn, "ticket.created", row, meta, {"priority": priority})
        if idempotency_key is not None:
            conn.execute(
                "INSERT OR REPLACE INTO idempotency_keys (client_id, user_id, key, request_hash, ticket_id, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (principal.client_id, user_id, idempotency_key, request_hash, ticket_id, meta.now_iso),
            )
    return ticket_id, False


# ---------- 수정 ----------

REQUESTER_EDITABLE = {"title", "description", "impact_note"}


def update_ticket(
    conn: sqlite3.Connection, principal: Principal, ticket_id: str, data: TicketPatchIn, if_match: str | None, meta: RequestMeta
) -> None:
    _require_user(principal)
    row = require_visible(conn, principal, ticket_id)
    check_version(row, if_match)
    _reject_final(row)
    fields = data.model_dump(exclude_unset=True)
    if not fields:
        raise ApiError(422, "empty_update", "변경할 필드가 없습니다.")

    is_owner = row["requester_id"] == principal.user_id
    if not principal.has("ticket:update"):
        if not (is_owner and row["status"] == "received" and set(fields) <= REQUESTER_EDITABLE):
            raise ApiError(
                403,
                "permission_denied",
                "요청자는 접수 상태인 본인 티켓의 제목·설명·영향 범위만 수정할 수 있습니다.",
                required_permissions=["ticket:update"],
            )
    if "is_major" in fields and not principal.has("ticket:declare_major"):
        raise ApiError(403, "permission_denied", "주요 장애 지정에는 ticket:declare_major 권한이 필요합니다.",
                       required_permissions=["ticket:declare_major"])  # fmt: skip
    for required in ("title", "description", "impact_note", "impact_level", "urgency", "is_major"):
        if required in fields and fields[required] is None:
            raise ApiError(422, "validation_failed", f"{required} 은(는) null 로 바꿀 수 없습니다.",
                           errors=[field_error(required, "null_not_allowed", "null 불가")])  # fmt: skip

    if "component_id" in fields:
        _validate_component(conn, row["service_id"], fields["component_id"])
    if "model" in fields:
        fields["model"] = fields["model"] or None
        _validate_model(conn, fields["model"])
    if "is_major" in fields:
        fields["is_major"] = int(fields["is_major"])
    impact = fields.get("impact_level", row["impact_level"])
    urgency = fields.get("urgency", row["urgency"])
    fields["priority"] = compute_priority(impact, urgency)

    changes = _diff(row, fields)
    if not changes:
        return
    with conn:
        conn.execute("BEGIN IMMEDIATE")
        _update(conn, row, fields, meta)
        _audit(conn, row["id"], principal, meta, "ticket.updated", changes)
        if "priority" in changes or "is_major" in changes:
            _notify(conn, "ticket.priority_changed" if "priority" in changes else "ticket.major_changed", row, meta,
                    {k: v for k, v in changes.items() if k in ("priority", "is_major")})  # fmt: skip


def assign_ticket(
    conn: sqlite3.Connection, principal: Principal, ticket_id: str, data: AssignmentIn, if_match: str | None, meta: RequestMeta
) -> None:
    _require_user(principal)
    row = require_visible(conn, principal, ticket_id)
    check_version(row, if_match)
    _reject_final(row)
    _validate_assignee(conn, data.assignment_group_id, data.assignee_id)
    values: dict[str, Any] = {"assignment_group_id": data.assignment_group_id, "assignee_id": data.assignee_id}
    changes = _diff(row, values)
    if not changes:
        return
    with conn:
        conn.execute("BEGIN IMMEDIATE")
        _update(conn, row, values, meta)
        _audit(conn, row["id"], principal, meta, "ticket.assigned", changes)
        _notify(conn, "ticket.assigned", {**dict(row), **values}, meta, changes)


# ---------- 상태 전이 ----------


def transition_ticket(
    conn: sqlite3.Connection, principal: Principal, ticket_id: str, data: TransitionIn, if_match: str | None, meta: RequestMeta
) -> None:
    user_id = _require_user(principal)
    row = require_visible(conn, principal, ticket_id)
    check_version(row, if_match)

    options = workflow.allowed(principal, row, meta.now)
    transition = workflow.find(row["status"], data.to_status) if not row["merged_into"] else None
    if transition is None:
        raise ApiError(
            409,
            "invalid_transition",
            f"{STATUS_LABELS[row['status']]}({row['status']}) 상태에서 {data.to_status} 로 바꿀 수 없습니다.",
            current_status=row["status"],
            requested_status=data.to_status,
            allowed_transitions=[t.target for t in options],
        )
    if transition not in options:
        detail = f"현재 주체({principal.role})는 {row['status']} → {data.to_status} 전이를 할 수 없습니다."
        if transition.requester_may and row["requester_id"] == principal.user_id and transition.target == "investigating":
            detail = "해결 후 7일이 지나 요청자가 직접 재오픈할 수 없습니다. 새 티켓을 등록하세요."
        raise ApiError(403, "transition_not_permitted", detail,
                       required_permissions=[transition.permission], allowed_transitions=[t.target for t in options])  # fmt: skip

    provided = {f for f in workflow.ALL_TRANSITION_FIELDS if getattr(data, f) is not None}
    required = set(workflow.required_fields(transition, row))
    accepted = required | set(transition.optional)
    missing = sorted(required - provided)
    unexpected = sorted(provided - accepted)
    if missing or unexpected:
        errors = [field_error(f, "required", f"{data.to_status} 전이에 필요한 값") for f in missing]
        errors += [field_error(f, "not_applicable", f"{data.to_status} 전이에서 받지 않는 값") for f in unexpected]
        raise ApiError(
            422,
            "transition_fields_invalid",
            f"{row['status']} → {data.to_status} 전이 입력이 올바르지 않습니다. 필수: {sorted(required) or '없음'}, "
            f"선택: {sorted(transition.optional) or '없음'}",
            errors=errors,
            required_fields=sorted(required),
            optional_fields=sorted(transition.optional),
        )

    values: dict[str, Any] = {"status": data.to_status}
    target = data.to_status
    if target == "assigned" and data.assignee_id is not None:
        _validate_assignee(conn, row["assignment_group_id"], data.assignee_id)
        values["assignee_id"] = data.assignee_id
    if row["status"] == "received" and row["responded_at"] is None:
        values["responded_at"] = meta.now_iso
    if target == "pending":
        values["pending_reason"] = data.pending_reason
    if row["status"] == "pending":
        values["pending_reason"] = None
    if target == "resolved":
        values.update(resolution_code=data.resolution_code, resolution_note=data.resolution_note, resolved_at=meta.now_iso)
    if row["status"] == "resolved" and target == "investigating":
        values.update(resolution_code=None, resolution_note=None, resolved_at=None)
    if target in FINAL_STATUSES:
        values["closed_at"] = meta.now_iso

    summary = f"상태 변경: {STATUS_LABELS[row['status']]} → {STATUS_LABELS[target]}"
    if data.pending_reason:
        summary += f" ({PENDING_LABELS[data.pending_reason]})"
    if data.resolution_code:
        summary += f"\n해결 유형: {RESOLUTION_LABELS[data.resolution_code]}\n{data.resolution_note}"
    for text in (data.reason, data.note):
        if text:
            summary += f"\n{text}"

    with conn:
        conn.execute("BEGIN IMMEDIATE")
        _update(conn, row, values, meta)
        if target == "pending":
            conn.execute("INSERT INTO sla_pauses (ticket_id, started_at) VALUES (?, ?)", (row["id"], meta.now_iso))
        if row["status"] == "pending":
            conn.execute(
                "UPDATE sla_pauses SET ended_at = ? WHERE ticket_id = ? AND ended_at IS NULL", (meta.now_iso, row["id"])
            )
        _worklog(conn, row["id"], user_id, summary, meta, kind="status_change", visibility="public")
        _audit(conn, row["id"], principal, meta, "ticket.transitioned", _diff(row, values))
        _notify(conn, "ticket.status_changed", {**dict(row), **values}, meta, {"from": row["status"], "to": target})


# ---------- worklog ----------


def add_worklog(
    conn: sqlite3.Connection, principal: Principal, ticket_id: str, body: str, visibility: str, meta: RequestMeta
) -> int:
    user_id = _require_user(principal)
    row = require_visible(conn, principal, ticket_id)
    _reject_final(row)
    if visibility == "internal" and not principal.has("worklog:internal"):
        raise ApiError(403, "permission_denied", "내부 메모 작성에는 worklog:internal 권한이 필요합니다.",
                       required_permissions=["worklog:internal"])  # fmt: skip
    is_owner = row["requester_id"] == user_id
    if not principal.has("ticket:read:all") and not is_owner:
        raise ApiError(403, "comment_not_allowed", "요청자는 본인이 요청한 티켓에만 댓글을 남길 수 있습니다.")
    with conn:
        conn.execute("BEGIN IMMEDIATE")
        worklog_id = _worklog(conn, row["id"], user_id, body, meta, kind="comment", visibility=visibility)
        values: dict[str, Any] = {}
        if visibility == "public" and not is_owner and row["responded_at"] is None:
            values["responded_at"] = meta.now_iso
        _update(conn, row, values, meta)
        _audit(conn, row["id"], principal, meta, "worklog.added", {"worklog_id": [None, worklog_id], "visibility": [None, visibility]})
        if visibility == "public":
            _notify(conn, "ticket.commented", row, meta, {"worklog_id": worklog_id})
    return worklog_id


# ---------- 병합·문제 연결 ----------


def merge_ticket(
    conn: sqlite3.Connection, principal: Principal, ticket_id: str, target_id: str, note: str | None,
    if_match: str | None, meta: RequestMeta,
) -> None:  # fmt: skip
    user_id = _require_user(principal)
    source = require_visible(conn, principal, ticket_id)
    check_version(source, if_match)
    _reject_final(source)
    target = require_visible(conn, principal, target_id)
    if target["id"] == source["id"]:
        raise ApiError(422, "merge_into_self", "티켓을 자기 자신에 병합할 수 없습니다.")
    if target["status"] in FINAL_STATUSES or target["merged_into"]:
        raise ApiError(409, "merge_target_closed", f"병합 대상 {target['id']} 이(가) 이미 종료되었습니다.",
                       target_status=target["status"])  # fmt: skip
    if target["service_id"] != source["service_id"]:
        raise ApiError(422, "merge_service_mismatch", "서비스가 다른 티켓은 병합할 수 없습니다.",
                       source_service_id=source["service_id"], target_service_id=target["service_id"])  # fmt: skip
    values = {
        "status": "closed",
        "merged_into": target["id"],
        "resolution_code": "duplicate",
        "resolution_note": note or f"{target['id']} 과(와) 같은 건으로 병합함.",
        "closed_at": meta.now_iso,
    }
    with conn:
        conn.execute("BEGIN IMMEDIATE")
        _update(conn, source, values, meta)
        conn.execute("UPDATE sla_pauses SET ended_at = ? WHERE ticket_id = ? AND ended_at IS NULL", (meta.now_iso, source["id"]))
        _worklog(conn, source["id"], user_id, f"{target['id']} 으로 병합되어 종료됨.", meta, kind="system", visibility="public")
        _worklog(conn, target["id"], user_id, f"{source['id']} 을(를) 이 티켓으로 병합했습니다.", meta, kind="system", visibility="public")
        target_row = get_row(conn, target["id"])
        _update(conn, target_row, {}, meta)
        _audit(conn, source["id"], principal, meta, "ticket.merged", _diff(source, values))
        _audit(conn, target["id"], principal, meta, "ticket.duplicate_added", {"duplicates": [None, source["id"]]})
        _notify(conn, "ticket.merged", source, meta, {"into": target["id"]})


def link_problem(
    conn: sqlite3.Connection, principal: Principal, ticket_id: str, problem_id: str | None, if_match: str | None, meta: RequestMeta
) -> None:
    _require_user(principal)
    row = require_visible(conn, principal, ticket_id)
    check_version(row, if_match)
    if problem_id is not None:
        problem_id = problem_id.strip().upper()
        if conn.execute("SELECT 1 FROM problems WHERE id = ?", (problem_id,)).fetchone() is None:
            raise ApiError(422, "unknown_problem", f"문제 {problem_id} 이(가) 없습니다.",
                           errors=[field_error("problem_id", "unknown", "없는 문제")])  # fmt: skip
    values = {"problem_id": problem_id}
    changes = _diff(row, values)
    if not changes:
        return
    with conn:
        conn.execute("BEGIN IMMEDIATE")
        _update(conn, row, values, meta)
        _audit(conn, row["id"], principal, meta, "ticket.problem_linked", changes)
