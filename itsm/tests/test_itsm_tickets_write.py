"""ITSM API 티켓 등록·수정·배정: 카탈로그 검증, 우선순위·자동 배정, 멱등키, 낙관적 잠금, 감사·outbox."""

from __future__ import annotations

import json
import sqlite3

import pytest

from itsm_api.config import ItsmApiSettings
from tests.conftest import NEW_TICKET, ItsmApi


def db(settings: ItsmApiSettings) -> sqlite3.Connection:
    conn = sqlite3.connect(settings.db_path)
    conn.row_factory = sqlite3.Row
    return conn


# ---------- 등록 ----------


def test_create_ticket(api: ItsmApi, itsm_settings):
    res = api.post("/api/v2/tickets", "member02", json=NEW_TICKET)
    assert res.status_code == 201, res.text
    t = res.json()
    assert t["id"] == "INC-037" and res.headers["Location"] == "/api/v2/tickets/INC-037"
    assert t["status"] == "received" and t["priority"] == "P3" and t["version"] == 1
    assert t["requester"]["username"] == "member02"
    assert t["assignment_group"]["slug"] == "media" and t["assignee"] is None
    assert t["occurred_at"] == "2026-10-26T00:30:00Z"
    assert t["created_at"] == "2026-10-26T01:00:00Z"
    # P3 업무시간: 월 10:00 KST + 4h = 14:00 KST, 해결 27 업무시간 = 목 10:00 KST
    assert t["sla"]["response_due_at"] == "2026-10-26T05:00:00Z"
    assert t["sla"]["resolution_due_at"] == "2026-10-29T01:00:00Z"

    with db(itsm_settings) as conn:
        audit = conn.execute("SELECT * FROM audit_events WHERE ticket_id = 'INC-037'").fetchall()
        outbox = conn.execute("SELECT * FROM notification_outbox WHERE ticket_id = 'INC-037'").fetchall()
    assert [a["action"] for a in audit] == ["ticket.created"] and audit[0]["client_id"] == "portal"
    assert [o["event_type"] for o in outbox] == ["ticket.created"]
    assert json.loads(outbox[0]["recipients"]) == {"users": [3], "team": 6}


@pytest.mark.parametrize(
    "impact,urgency,priority",
    [("extensive", "critical", "P1"), ("significant", "high", "P2"), ("moderate", "low", "P4"), ("minor", "critical", "P3")],
)
def test_priority_is_computed(api: ItsmApi, impact, urgency, priority):
    body = {**NEW_TICKET, "impact_level": impact, "urgency": urgency}
    assert api.post("/api/v2/tickets", "member01", json=body).json()["priority"] == priority


def test_component_owner_overrides_service_owner(api: ItsmApi):
    body = {**NEW_TICKET, "service_id": "codex", "component_id": 1}
    assert api.post("/api/v2/tickets", "member01", json=body).json()["assignment_group"]["slug"] == "identity"
    body = {**NEW_TICKET, "service_id": "codex", "component_id": None}
    assert api.post("/api/v2/tickets", "member01", json=body).json()["assignment_group"]["slug"] == "codex"


@pytest.mark.parametrize("field,value", [("priority", "P1"), ("requester_id", 1), ("status", "resolved"),
                                         ("assignee_id", 7), ("id", "INC-900")])  # fmt: skip
def test_server_owned_fields_are_rejected(api: ItsmApi, field, value):
    res = api.post("/api/v2/tickets", "member01", json={**NEW_TICKET, field: value})
    assert res.status_code == 422
    assert res.json()["errors"][0]["field"] == field and res.json()["errors"][0]["code"] == "extra_forbidden"


def test_catalog_validation_errors(api: ItsmApi):
    mismatch = api.post("/api/v2/tickets", "member01", json={**NEW_TICKET, "component_id": 1}).json()
    assert mismatch["code"] == "component_service_mismatch"
    assert mismatch["errors"][0]["allowed_values"] == [{"id": 14, "name": "영상 생성"}, {"id": 15, "name": "대기열"}]

    unknown = api.post("/api/v2/tickets", "member01", json={**NEW_TICKET, "service_id": "Sora", "component_id": None}).json()
    assert unknown["code"] == "unknown_service" and "sora" in unknown["errors"][0]["allowed_values"]

    deprecated = api.post("/api/v2/tickets", "member01",
                          json={**NEW_TICKET, "service_id": "assistants-api", "component_id": 20}).json()  # fmt: skip
    assert deprecated["code"] == "service_deprecated"
    assert deprecated["errors"][0]["replacement_service_id"] == "openai-api"

    retired = api.post("/api/v2/tickets", "member01", json={**NEW_TICKET, "model": "gpt-5.5"}).json()
    assert retired["code"] == "model_retired" and "gpt-6" in retired["errors"][0]["allowed_values"]
    assert api.post("/api/v2/tickets", "member01", json={**NEW_TICKET, "model": "gpt-7"}).json()["code"] == "unknown_model"


def test_occurred_at_requires_timezone(api: ItsmApi):
    res = api.post("/api/v2/tickets", "member01", json={**NEW_TICKET, "occurred_at": "2026-10-26T09:30:00"})
    assert res.status_code == 422 and res.json()["errors"][0]["field"] == "occurred_at"


def test_restricted_ticket_goes_to_security_team(api: ItsmApi):
    t = api.post("/api/v2/tickets", "member02", json={**NEW_TICKET, "classification": "restricted"}).json()
    assert t["assignment_group"]["slug"] == "security"
    assert api.get(f"/api/v2/tickets/{t['id']}", "member02").status_code == 200
    assert api.get(f"/api/v2/tickets/{t['id']}", "media01").status_code == 404
    assert api.get(f"/api/v2/tickets/{t['id']}", "sec01").status_code == 200


def test_create_requires_user_token_and_permission(api: ItsmApi):
    assert api.post("/api/v2/tickets", json=NEW_TICKET).status_code == 403  # 서비스 토큰


def test_idempotency_key(api: ItsmApi, itsm_settings):
    headers = {"Idempotency-Key": "form-1"}
    first = api.post("/api/v2/tickets", "member01", json=NEW_TICKET, headers=headers)
    again = api.post("/api/v2/tickets", "member01", json=NEW_TICKET, headers=headers)
    assert first.status_code == again.status_code == 201
    assert first.json()["id"] == again.json()["id"]
    assert again.headers["Idempotent-Replayed"] == "true" and "Idempotent-Replayed" not in first.headers

    reused = api.post("/api/v2/tickets", "member01", json={**NEW_TICKET, "title": "다른 제목"}, headers=headers)
    assert reused.status_code == 422 and reused.json()["code"] == "idempotency_key_reused"
    assert reused.json()["original_ticket_id"] == first.json()["id"]

    # 키는 사용자별로 구분됩니다.
    other = api.post("/api/v2/tickets", "member02", json=NEW_TICKET, headers=headers)
    assert other.json()["id"] != first.json()["id"]
    # 키 없는 재전송은 매번 새로 등록됩니다.
    assert api.post("/api/v2/tickets", "member01", json=NEW_TICKET).json()["id"] not in (first.json()["id"], other.json()["id"])
    with db(itsm_settings) as conn:
        assert conn.execute("SELECT COUNT(*) FROM tickets WHERE title = ?", (NEW_TICKET["title"],)).fetchone()[0] == 3


# ---------- 수정 ----------


def test_patch_requires_if_match(api: ItsmApi):
    res = api.patch("/api/v2/tickets/INC-005", "desk01", json={"urgency": "high"})
    assert res.status_code == 428 and res.json()["code"] == "precondition_required"
    assert res.json()["current_version"] == 1
    stale = api.patch("/api/v2/tickets/INC-005", "desk01", json={"urgency": "high"}, headers={"If-Match": '"0"'})
    assert stale.status_code == 412 and stale.json()["code"] == "version_conflict" and stale.json()["current_version"] == 1


def test_patch_recomputes_priority_and_audits(api: ItsmApi, itsm_settings):
    res = api.patch("/api/v2/tickets/INC-005", "desk01", json={"impact_level": "extensive", "urgency": "critical"},
                    headers={"If-Match": '"1"'})  # fmt: skip
    assert res.status_code == 200, res.text
    assert res.json()["priority"] == "P1" and res.json()["version"] == 2 and res.headers["ETag"] == '"2"'
    with db(itsm_settings) as conn:
        event = conn.execute("SELECT * FROM audit_events WHERE ticket_id = 'INC-005' ORDER BY id DESC").fetchone()
        outbox = conn.execute("SELECT event_type FROM notification_outbox WHERE ticket_id = 'INC-005'").fetchall()
    assert event["action"] == "ticket.updated"
    assert json.loads(event["changes"])["priority"] == ["P3", "P1"]
    assert [o["event_type"] for o in outbox] == ["ticket.priority_changed"]


def test_requester_can_edit_only_own_received_ticket_text(api: ItsmApi):
    ok = api.patch("/api/v2/tickets/INC-005", "member01", json={"description": "추가 설명"}, headers={"If-Match": '"1"'})
    assert ok.status_code == 200
    denied = api.patch("/api/v2/tickets/INC-005", "member01", json={"urgency": "critical"}, headers={"If-Match": '"2"'})
    assert denied.status_code == 403
    not_received = api.patch("/api/v2/tickets/INC-001", "member01", json={"title": "x"}, headers={"If-Match": '"1"'})
    assert not_received.status_code == 403


def test_patch_rules(api: ItsmApi):
    major = api.patch("/api/v2/tickets/INC-005", "desk01", json={"is_major": True}, headers={"If-Match": '"1"'})
    assert major.status_code == 403 and major.json()["required_permissions"] == ["ticket:declare_major"]
    assert api.patch("/api/v2/tickets/INC-005", "lead01", json={"is_major": True}, headers={"If-Match": '"1"'}).json()["is_major"]
    closed = api.patch("/api/v2/tickets/INC-004", "desk01", json={"title": "x"}, headers={"If-Match": '"1"'})
    assert closed.status_code == 409 and closed.json()["code"] == "ticket_closed"
    comp = api.patch("/api/v2/tickets/INC-005", "desk01", json={"component_id": 1}, headers={"If-Match": '"2"'})
    assert comp.json()["code"] == "component_service_mismatch"
    empty = api.patch("/api/v2/tickets/INC-005", "desk01", json={}, headers={"If-Match": '"2"'})
    assert empty.json()["code"] == "empty_update"


# ---------- 배정 ----------


def test_assignment(api: ItsmApi):
    res = api.put("/api/v2/tickets/INC-005/assignment", "desk01", json={"assignment_group_id": 4, "assignee_id": 10},
                  headers={"If-Match": '"1"'})  # fmt: skip
    assert res.status_code == 200 and res.json()["assignee"]["username"] == "gpt01"
    wrong = api.put("/api/v2/tickets/INC-012/assignment", "desk01", json={"assignment_group_id": 5, "assignee_id": 10},
                    headers={"If-Match": '"1"'})  # fmt: skip
    assert wrong.status_code == 422 and wrong.json()["code"] == "assignee_not_in_group"
    assert wrong.json()["errors"][0]["allowed_values"] == [{"id": 11, "username": "codex01", "display_name": "배수아"}]
    inactive = api.put("/api/v2/tickets/INC-012/assignment", "desk01", json={"assignment_group_id": 1, "assignee_id": 17},
                       headers={"If-Match": '"1"'})  # fmt: skip
    assert inactive.json()["code"] == "assignee_not_in_group"
    assert api.put("/api/v2/tickets/INC-012/assignment", "member01", json={"assignment_group_id": 5},
                   headers={"If-Match": '"1"'}).status_code == 403  # fmt: skip


# ---------- 조회자 ----------


def test_viewer_can_only_read(api: ItsmApi):
    me = api.get("/api/v2/me", "viewer01").json()
    assert me["role"] == "viewer" and me["permissions"] == ["ticket:read", "ticket:read:all"]
    # 일반 티켓은 전부 보이고, 보안 제한 티켓과 내부 메모는 보이지 않습니다.
    assert api.get("/api/v2/tickets/stats", "viewer01").json()["total"] == 33
    assert api.get("/api/v2/tickets/INC-012", "viewer01").status_code == 200
    assert api.get("/api/v2/tickets/INC-026", "viewer01").status_code == 404
    logs = api.get("/api/v2/tickets/INC-001/worklogs", "viewer01").json()["items"]
    assert {w["visibility"] for w in logs} == {"public"}

    created = api.post("/api/v2/tickets", "viewer01", json=NEW_TICKET)
    assert created.status_code == 403 and created.json()["required_permissions"] == ["ticket:create"]
    assert api.post("/api/v2/tickets/INC-001/worklogs", "viewer01", json={"body": "x"}).status_code == 403
    moved = api.post("/api/v2/tickets/INC-001/transitions", "viewer01", json={"to_status": "monitoring", "note": "x"},
                     headers={"If-Match": '"1"'})  # fmt: skip
    assert moved.status_code == 403 and moved.json()["code"] == "transition_not_permitted"
    assert api.patch("/api/v2/tickets/INC-001", "viewer01", json={"title": "x"}, headers={"If-Match": '"1"'}).status_code == 403
