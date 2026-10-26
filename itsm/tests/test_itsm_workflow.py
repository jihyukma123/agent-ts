"""ITSM API 상태 전이: 전이 규칙, 권한, 필수 입력, SLA 일시정지, 요청자 취소·재오픈."""

from __future__ import annotations

from itsm_api.timeutil import FixedClock
from tests.conftest import NEW_TICKET, ItsmApi


def transition(api: ItsmApi, ticket_id: str, username: str, **body):
    return api.post(f"/api/v2/tickets/{ticket_id}/transitions", username, json=body,
                    headers={"If-Match": api.version(ticket_id)})  # fmt: skip


def options(api: ItsmApi, ticket_id: str, username: str) -> dict[str, list[str]]:
    res = api.get(f"/api/v2/tickets/{ticket_id}/transitions", username).json()
    return {t["to_status"]: t["required_fields"] for t in res["transitions"]}


def test_available_transitions_depend_on_role(api: ItsmApi):
    assert options(api, "INC-005", "desk01") == {"assigned": ["assignee_id"], "cancelled": ["reason"]}
    assert options(api, "INC-005", "member01") == {"cancelled": ["reason"]}  # 요청자 본인
    assert api.get("/api/v2/tickets/INC-012/transitions", "member01").status_code == 404  # 다른 사람 티켓
    assert set(options(api, "INC-001", "desk01")) == {"pending", "monitoring"}  # L1 은 해결 불가
    assert set(options(api, "INC-001", "idops01")) == {"pending", "monitoring", "resolved"}
    assert options(api, "INC-030", "admin01") == {}  # 병합된 티켓


def test_full_lifecycle(api: ItsmApi, clock: FixedClock):
    ticket_id = api.post("/api/v2/tickets", "member02", json=NEW_TICKET).json()["id"]

    assigned = transition(api, ticket_id, "desk01", to_status="assigned", assignee_id=12)
    assert assigned.status_code == 200, assigned.text
    assert assigned.json()["assignee"]["username"] == "media01"
    assert assigned.json()["sla"]["responded_at"] == "2026-10-26T01:00:00Z"

    assert transition(api, ticket_id, "media01", to_status="investigating").json()["status"] == "investigating"

    clock.advance(hours=1)
    pending = transition(api, ticket_id, "media01", to_status="pending", pending_reason="awaiting_requester",
                         note="재현 영상 요청")  # fmt: skip
    assert pending.json()["sla"]["paused"] is True and pending.json()["sla"]["resolution_due_at"] is None

    clock.advance(hours=4)
    resumed = transition(api, ticket_id, "media01", to_status="investigating").json()
    assert resumed["pending_reason"] is None and resumed["sla"]["paused"] is False
    # 보류했던 4 업무시간만큼 해결 기한이 늦춰집니다 (목 10:00 → 목 14:00 KST).
    assert resumed["sla"]["resolution_due_at"] == "2026-10-29T05:00:00Z"

    resolved = transition(api, ticket_id, "media01", to_status="resolved", resolution_code="fixed",
                          resolution_note="대기열 워커 상한 조정")  # fmt: skip
    assert resolved.json()["resolution"]["code"] == "fixed"
    assert resolved.json()["resolution"]["resolved_at"] == "2026-10-26T06:00:00Z"

    closed = transition(api, ticket_id, "desk01", to_status="closed").json()
    assert closed["status"] == "closed" and closed["closed_at"] == "2026-10-26T06:00:00Z"
    assert closed["version"] == 7

    logs = api.get(f"/api/v2/tickets/{ticket_id}/worklogs", "member02").json()["items"]
    assert [w["kind"] for w in logs] == ["status_change"] * 6
    assert logs[2]["body"] == "상태 변경: 조사 중 → 보류 (요청자 응답 대기)\n재현 영상 요청"


def test_invalid_transition_lists_allowed_targets(api: ItsmApi):
    res = transition(api, "INC-005", "desk01", to_status="resolved", resolution_code="fixed", resolution_note="x")
    assert res.status_code == 409
    body = res.json()
    assert body["code"] == "invalid_transition" and body["current_status"] == "received"
    assert body["allowed_transitions"] == ["assigned", "cancelled"]


def test_transition_not_permitted(api: ItsmApi):
    res = transition(api, "INC-001", "desk01", to_status="resolved", resolution_code="fixed", resolution_note="x")
    assert res.status_code == 403 and res.json()["code"] == "transition_not_permitted"
    assert res.json()["required_permissions"] == ["ticket:resolve"]


def test_transition_field_validation(api: ItsmApi):
    missing = transition(api, "INC-001", "idops01", to_status="resolved")
    assert missing.status_code == 422 and missing.json()["code"] == "transition_fields_invalid"
    assert {e["field"] for e in missing.json()["errors"]} == {"resolution_code", "resolution_note"}
    assert missing.json()["required_fields"] == ["resolution_code", "resolution_note"]

    unexpected = transition(api, "INC-001", "idops01", to_status="monitoring", note="완화", pending_reason="awaiting_vendor")
    assert unexpected.status_code == 422
    assert unexpected.json()["errors"] == [
        {"field": "pending_reason", "code": "not_applicable", "message": "monitoring 전이에서 받지 않는 값"}
    ]

    no_assignee = transition(api, "INC-005", "desk01", to_status="assigned")
    assert [e["field"] for e in no_assignee.json()["errors"]] == ["assignee_id"]
    bad_enum = transition(api, "INC-001", "idops01", to_status="pending", pending_reason="waiting")
    assert bad_enum.status_code == 422 and bad_enum.json()["code"] == "validation_failed"


def test_transition_requires_current_version(api: ItsmApi):
    no_header = api.post("/api/v2/tickets/INC-001/transitions", "idops01", json={"to_status": "monitoring", "note": "x"})
    assert no_header.status_code == 428
    stale = api.post("/api/v2/tickets/INC-001/transitions", "idops01", json={"to_status": "monitoring", "note": "x"},
                     headers={"If-Match": '"9"'})  # fmt: skip
    assert stale.status_code == 412 and stale.json()["current_version"] == 1


def test_requester_cancel_and_reopen(api: ItsmApi, clock: FixedClock):
    assert transition(api, "INC-005", "member01", to_status="cancelled", reason="중복 신고").json()["status"] == "cancelled"
    assert transition(api, "INC-012", "member01", to_status="cancelled", reason="x").status_code == 404

    # INC-023: 2026-10-10 해결 → 기준 시각(10-26)에는 7일이 지나 요청자가 재오픈할 수 없음
    late = transition(api, "INC-023", "member01", to_status="investigating", reason="재발")
    assert late.status_code == 403 and "7일" in late.json()["detail"]

    # INC-008: 2026-10-23 해결 → 요청자(member02) 재오픈 가능
    reopened = transition(api, "INC-008", "member02", to_status="investigating", reason="다시 404 가 납니다")
    assert reopened.status_code == 200
    assert reopened.json()["resolution"] is None and reopened.json()["status"] == "investigating"


def test_closed_ticket_has_no_transitions(api: ItsmApi):
    res = transition(api, "INC-004", "admin01", to_status="investigating", reason="x")
    assert res.status_code == 409 and res.json()["allowed_transitions"] == []
